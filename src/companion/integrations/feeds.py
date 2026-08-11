"""RSS/Atomフィードから記事の候補を集める.

以前はClaudeにWebSearchを許可してニュースを探させていたが、
検索結果が1ターンごとに文脈へ積み上がり、1ジャンルあたり数百万トークンの
キャッシュ読み書きが発生していた（実測で1日6ドル相当）。

見出しを集めるのは検索エンジンでなくてもできるので、
ここで無料のRSSから候補を集め、Claudeには「選ぶ・要約する」だけをさせる。
これで1回の呼び出しに収まり、コストは1/100以下になる。

フィードの中身は第三者が書いた文章である。ここでは解釈せず、
タグを落として素のテキストにするところまでを担当する。
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)

# フィード1本あたりの取得上限。RSSは通常20〜50件返るが、
# 古いものまで拾っても選定の役に立たないので頭だけ見る。
PER_FEED_LIMIT = 15
FETCH_TIMEOUT_SEC = 15
# 抜粋の長さ。プロンプトに40件並べるので、1件あたりを短く保つ。
SNIPPET_MAX_CHARS = 160

USER_AGENT = "daily-companion/0.1 (personal RSS reader)"

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
# Google NewsのRSSは見出しを「記事タイトル - 媒体名」の形で返す。
# 媒体名は別に持つので、見出しからは落とす。
_SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,30}$")


@dataclass(frozen=True)
class Feed:
    """取得元のフィード1本."""

    name: str
    url: str


@dataclass(frozen=True)
class Article:
    """候補の記事1件.

    published は取れないフィードもあるため None を許す。
    """

    title: str
    url: str
    source: str
    snippet: str = ""
    published: datetime | None = None


def _google_news(query: str) -> str:
    """Google ニュースの検索RSS. 日本語・日本版で引く."""
    return (
        f"https://news.google.com/rss/search?q={quote(query)}"
        "&hl=ja&gl=JP&ceid=JP:ja"
    )


def _hatena(query: str) -> str:
    """はてなブックマークの検索RSS. 人気順にすると読まれた記事が上に来る.

    語をスペースで繋いだ検索（「ES 面接 対策」など）は人気順だと
    ほぼ0件になる。実測で件数が出たのは単語1つの検索だけなので、
    ここに複数語のクエリを足さないこと。
    """
    return f"https://b.hatena.ne.jp/q/{quote(query)}?mode=rss&sort=popular"


# ジャンルごとの取得元。1ジャンルに複数本置いて、出典が偏らないようにする。
TOPIC_FEEDS: dict[str, tuple[Feed, ...]] = {
    "ai_tech": (
        Feed("Googleニュース", _google_news("AI OR LLM OR 生成AI OR 開発ツール")),
        Feed("ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"),
        Feed("Publickey", "https://www.publickey1.jp/atom.xml"),
        Feed("Zenn", "https://zenn.dev/topics/ai/feed"),
    ),
    "job_hunting": (
        Feed("Googleニュース", _google_news("新卒採用 OR 就活 OR 27卒 OR インターンシップ")),
        Feed("Googleニュース", _google_news("採用 内定 大手企業 動向")),
        Feed("ITmedia ビジネス", "https://rss.itmedia.co.jp/rss/2.0/business.xml"),
    ),
    "social": (
        Feed("はてなブックマーク", _hatena("就活")),
        Feed("はてなブックマーク", _hatena("新卒")),
        Feed("Qiita", "https://qiita.com/tags/%E5%B0%B1%E6%B4%BB/feed"),
        Feed("Zenn", "https://zenn.dev/topics/%E5%B0%B1%E6%B4%BB/feed"),
    ),
    "economy": (
        Feed("Googleニュース", _google_news("日銀 OR FRB OR 金融政策 OR 為替")),
        Feed("Googleニュース", _google_news("景気 OR 雇用統計 OR 決算 OR 株式市場")),
        # ロイターの配信RSSは403を返すので使わない。
        # 背景まで書いてある記事の供給元として東洋経済を置く。
        Feed("東洋経済", "https://toyokeizai.net/list/feed/rss"),
    ),
    "domestic": (
        Feed("NHK", "https://www.nhk.or.jp/rss/news/cat0.xml"),
        Feed("Googleニュース", "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"),
    ),
    "world": (
        Feed("NHK国際", "https://www.nhk.or.jp/rss/news/cat6.xml"),
        Feed("Googleニュース", _google_news("国際情勢 OR 外交 OR 地政学")),
        Feed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ),
}


def _clean_text(raw: str | None) -> str:
    """HTMLタグと実体参照を落として1行にする."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _SPACE_RE.sub(" ", text).strip()


def _clean_title(raw: str) -> str:
    """見出しから媒体名の接尾辞を落とす."""
    title = _clean_text(raw)
    return _SOURCE_SUFFIX_RE.sub("", title).strip() or title


def _strip_ns(tag: str) -> str:
    """`{http://...}item` から `item` を取り出す."""
    return tag.rsplit("}", 1)[-1]


def _find(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    """名前空間を無視して直下の子要素を探す."""
    for child in element:
        if _strip_ns(child.tag) == name:
            return child
    return None


def _text_of(element: ElementTree.Element, name: str) -> str:
    child = _find(element, name)
    return child.text or "" if child is not None else ""


def _link_of(element: ElementTree.Element) -> str:
    """記事URLを取り出す. RSSは要素の中身、Atomは href 属性に入る."""
    for child in element:
        if _strip_ns(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    """RFC822(RSS)とISO8601(Atom)の両方を受ける. 読めなければ None."""
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    # 比較できるよう、タイムゾーンのない値はUTC扱いに揃える。
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_date(entry: ElementTree.Element) -> datetime | None:
    for name in ("pubDate", "published", "updated", "date"):
        parsed = _parse_date(_text_of(entry, name))
        if parsed is not None:
            return parsed
    return None


def _entry_summary(entry: ElementTree.Element) -> str:
    for name in ("description", "summary", "content"):
        text = _clean_text(_text_of(entry, name))
        if text:
            return text[:SNIPPET_MAX_CHARS]
    return ""


def parse_feed(xml_text: str, source: str) -> tuple[Article, ...]:
    """フィードのXMLから記事を取り出す. 壊れていれば空で返す.

    RSS 2.0(channel/item)、RSS 1.0(rdf:RDF/item)、Atom(feed/entry)を受ける。
    形式ごとに分岐せず、名前空間を無視して item / entry を拾えば足りる。
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        log.warning("unparseable feed from %s: %s", source, exc)
        return ()

    entries = [
        element
        for element in root.iter()
        if _strip_ns(element.tag) in ("item", "entry")
    ]

    articles: list[Article] = []
    for entry in entries[:PER_FEED_LIMIT]:
        url = _link_of(entry)
        title = _clean_title(_text_of(entry, "title"))
        if not url.startswith(("http://", "https://")) or not title:
            continue
        articles.append(
            Article(
                title=title,
                url=url,
                source=source,
                snippet=_entry_summary(entry),
                published=_entry_date(entry),
            )
        )
    return tuple(articles)


def fetch_feed(feed: Feed, client: httpx.Client) -> tuple[Article, ...]:
    """フィード1本を取得する. 落ちても例外は投げず空で返す."""
    try:
        response = client.get(feed.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("feed fetch failed for %s: %s", feed.name, exc)
        return ()
    return parse_feed(response.text, feed.name)


def _is_recent(article: Article, *, cutoff: datetime | None) -> bool:
    """期限より新しいか. 日付が取れない記事は落とさず残す."""
    if cutoff is None or article.published is None:
        return True
    return article.published >= cutoff


def _interleave(groups: list[tuple[Article, ...]]) -> list[Article]:
    """フィードごとの一覧を順番に1件ずつ取り出して混ぜる.

    そのまま連結すると先頭のフィードだけで上限に達し、出典が偏る。
    """
    merged: list[Article] = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                merged.append(group[index])
    return merged


def _dedupe(articles: list[Article]) -> list[Article]:
    """URLと見出しで重複を落とす. 同じ記事が複数の媒体から来る."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Article] = []
    for article in articles:
        title_key = _SPACE_RE.sub("", article.title)
        if article.url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(article.url)
        seen_titles.add(title_key)
        unique.append(article)
    return unique


def collect(
    topic: str,
    *,
    limit: int,
    max_age_days: int | None,
    now: datetime | None = None,
) -> tuple[Article, ...]:
    """ジャンルの候補記事を集める. 取得できなければ空で返す.

    Args:
        topic: ジャンル名. 未知なら空を返す.
        limit: 返す件数の上限.
        max_age_days: これより古い記事を落とす. None なら期間で絞らない.
        now: 現在時刻. テストから差し替える.
    """
    feeds = TOPIC_FEEDS.get(topic, ())
    if not feeds:
        log.warning("no feeds configured for topic %s", topic)
        return ()

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max_age_days) if max_age_days else None

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        timeout=FETCH_TIMEOUT_SEC, follow_redirects=True, headers=headers
    ) as client:
        with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
            groups = list(pool.map(lambda feed: fetch_feed(feed, client), feeds))

    fresh = [
        tuple(article for article in group if _is_recent(article, cutoff=cutoff))
        for group in groups
    ]
    return tuple(_dedupe(_interleave(fresh))[:limit])
