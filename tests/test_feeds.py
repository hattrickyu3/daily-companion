"""RSS取得のテスト.

フィードは外部から来る不定形データなので、
壊れていても落ちないこと・出典が偏らないことを重点的に確認する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from companion.config import VALID_NEWS_TOPICS
from companion.integrations import feeds

NOW = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>テスト媒体</title>
    <item>
      <title>新モデル発表 - ITmedia</title>
      <link>https://example.com/a</link>
      <description>&lt;p&gt;性能が&lt;b&gt;大幅に&lt;/b&gt;向上した。&lt;/p&gt;</description>
      <pubDate>Mon, 10 Aug 2026 09:00:00 +0900</pubDate>
    </item>
    <item>
      <title>古い記事</title>
      <link>https://example.com/old</link>
      <description>去年の話。</description>
      <pubDate>Sat, 10 Aug 2025 09:00:00 +0900</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atomの記事</title>
    <link href="https://example.org/atom-1"/>
    <summary>Atom形式の抜粋。</summary>
    <updated>2026-08-10T00:00:00Z</updated>
  </entry>
</feed>
"""

RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item>
    <title>RSS1.0の記事</title>
    <link>https://example.net/rdf-1</link>
    <description>RDF形式の抜粋。</description>
    <dc:date>2026-08-10T00:00:00+09:00</dc:date>
  </item>
</rdf:RDF>
"""


def article(
    title: str, url: str, source: str = "テスト", published: datetime | None = NOW
) -> feeds.Article:
    return feeds.Article(title=title, url=url, source=source, published=published)


def test_RSSから記事を取り出せる() -> None:
    # Act
    articles = feeds.parse_feed(RSS, "テスト媒体")

    # Assert
    assert articles[0].url == "https://example.com/a"
    assert articles[0].source == "テスト媒体"


def test_見出しから媒体名の接尾辞を落とす() -> None:
    # Arrange: Google ニュースは「タイトル - 媒体名」で返してくる

    # Act
    articles = feeds.parse_feed(RSS, "テスト媒体")

    # Assert
    assert articles[0].title == "新モデル発表"


def test_抜粋のHTMLタグと実体参照を落とす() -> None:
    # Act
    articles = feeds.parse_feed(RSS, "テスト媒体")

    # Assert
    assert articles[0].snippet == "性能が 大幅に 向上した。"


def test_Atom形式も読める() -> None:
    # Arrange / Act: Atomはリンクが href 属性に入る
    articles = feeds.parse_feed(ATOM, "Zenn")

    # Assert
    assert articles[0].url == "https://example.org/atom-1"
    assert articles[0].title == "Atomの記事"


def test_RSS1_0形式も読める() -> None:
    # Arrange / Act: ロイターなどが使う rdf:RDF 形式
    articles = feeds.parse_feed(RDF, "ロイター")

    # Assert
    assert articles[0].url == "https://example.net/rdf-1"
    assert articles[0].published is not None


def test_壊れたXMLでも空を返す() -> None:
    # Arrange / Act / Assert: 1本のフィードの不調で朝のジョブを止めない
    assert feeds.parse_feed("<rss><channel><item>", "壊れた媒体") == ()
    assert feeds.parse_feed("", "空の応答") == ()


def test_URLのない項目は捨てる() -> None:
    # Arrange
    xml = "<rss><channel><item><title>URLなし</title></item></channel></rss>"

    # Act / Assert
    assert feeds.parse_feed(xml, "テスト") == ()


def test_見出しのない項目は捨てる() -> None:
    # Arrange
    xml = "<rss><channel><item><link>https://example.com/a</link></item></channel></rss>"

    # Act / Assert
    assert feeds.parse_feed(xml, "テスト") == ()


def test_期限より古い記事は落とす() -> None:
    # Arrange
    old = article("古い", "https://example.com/old", published=NOW - timedelta(days=30))
    cutoff = NOW - timedelta(days=7)

    # Act / Assert
    assert feeds._is_recent(old, cutoff=cutoff) is False
    assert feeds._is_recent(article("新しい", "https://example.com/new"), cutoff=cutoff) is True


def test_日付が取れない記事は残す() -> None:
    # Arrange: 日付を出さないフィードがあり、落とすと候補がまるごと消える
    undated = article("日付なし", "https://example.com/x", published=None)

    # Act / Assert
    assert feeds._is_recent(undated, cutoff=NOW - timedelta(days=7)) is True


def test_期限なしなら全部残す() -> None:
    # Arrange: 実用情報のジャンルは古くても役に立つ
    old = article("古い", "https://example.com/old", published=NOW - timedelta(days=400))

    # Act / Assert
    assert feeds._is_recent(old, cutoff=None) is True


def test_フィードをまたいで1件ずつ混ぜる() -> None:
    # Arrange: そのまま連結すると先頭のフィードだけで上限に達し、出典が偏る
    groups = [
        (article("A1", "https://a/1"), article("A2", "https://a/2")),
        (article("B1", "https://b/1"),),
    ]

    # Act
    merged = feeds._interleave(groups)

    # Assert
    assert [item.title for item in merged] == ["A1", "B1", "A2"]


def test_同じURLの記事は1件にまとめる() -> None:
    # Arrange
    duplicated = [article("見出しA", "https://example.com/a"), article("別の見出し", "https://example.com/a")]

    # Act / Assert
    assert len(feeds._dedupe(duplicated)) == 1


def test_同じ見出しの記事は1件にまとめる() -> None:
    # Arrange: 同じ記事が複数の媒体から流れてくる
    duplicated = [
        article("新モデル発表", "https://a.example.com/x"),
        article("新モデル 発表", "https://b.example.com/y"),
    ]

    # Act / Assert
    assert len(feeds._dedupe(duplicated)) == 1


def test_設定できる全ジャンルに取得元がある() -> None:
    # Arrange / Act / Assert: .env で指定できるのにフィードがない状態を防ぐ
    for topic in VALID_NEWS_TOPICS:
        assert feeds.TOPIC_FEEDS.get(topic), f"{topic} に取得元が設定されていない"


def test_未知のジャンルでも落ちない() -> None:
    # Act / Assert
    assert feeds.collect("unknown_topic", limit=10, max_age_days=7) == ()
