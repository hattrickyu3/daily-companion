"""ノートの全文検索（Obsidianの検索と同じ感覚で使えるもの）.

- スペース（全角も可）で区切った言葉を**すべて**含むノートを探す
- 全角・半角、大文字・小文字は区別しない（「Ｔ検定」で「t検定」が見つかる）
- 題名に言葉を含むノートを上に出す
- ヒットした行を前後の文脈つきで抜き出し、言葉の位置に印を付ける

索引は作らず、検索のたびに全ノートを読む。ノートは数百枚程度で十分速く、
Obsidianで書き足した直後でも必ず最新の内容で探せるため。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from companion.services.notes import NoteLink, Vault, course_folder

MAX_RESULTS = 50
MAX_SNIPPETS = 3
# 長い行は、最初にヒットした言葉の前後この文字数だけ見せる。
SNIPPET_RADIUS = 30
ELLIPSIS = "…"

# 行頭の見出し・引用・箇条書きの記号、コールアウトの印（> [!warning]）
LINE_PREFIX = re.compile(r"^(?:[#>]+\s*|[-*+]\s+|\[![\w-]+\][+-]?\s*)+")
WIKILINK = re.compile(r"!?\[\[([^\[\]\n]+)\]\]")
EMPHASIS = re.compile(r"\*\*|__|==")


@dataclass(frozen=True)
class Segment:
    """抜き出した行の一部分. hit なら検索語に当たった部分."""

    text: str
    hit: bool


@dataclass(frozen=True)
class SearchHit:
    note: NoteLink
    folder: str
    title_hit: bool
    count: int
    snippets: tuple[tuple[Segment, ...], ...]


def _normalize(text: str) -> tuple[str, list[int]]:
    """照合用の文字列と、その各文字が元の何文字目から来たかの対応表.

    1文字ずつ正規化するので、「㌔」→「キロ」のように文字数が変わっても
    元の表記の位置に戻せる。印は元の表記のまま付けたいため。
    """
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        normalized = unicodedata.normalize("NFKC", char).casefold()
        chars.append(normalized)
        origin.extend([index] * len(normalized))
    return "".join(chars), origin


def _find_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """text の中で検索語に当たる範囲（元の表記での位置）. 重なりはまとめる."""
    normalized, origin = _normalize(text)
    spans: list[tuple[int, int]] = []
    for term in terms:
        start = normalized.find(term)
        while start != -1:
            end = start + len(term)
            spans.append((origin[start], origin[end - 1] + 1))
            start = normalized.find(term, end)

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _snippet(line: str, spans: list[tuple[int, int]]) -> tuple[Segment, ...]:
    """1行を、最初のヒットの周りだけ切り出して印付きの部品にする."""
    first_start, first_end = spans[0]
    start = max(0, first_start - SNIPPET_RADIUS)
    end = min(len(line), first_end + SNIPPET_RADIUS)

    segments: list[Segment] = [Segment(ELLIPSIS, False)] if start > 0 else []
    cursor = start
    for span_start, span_end in spans:
        if span_end <= start or span_start >= end:
            continue
        span_start, span_end = max(span_start, start), min(span_end, end)
        if cursor < span_start:
            segments.append(Segment(line[cursor:span_start], False))
        segments.append(Segment(line[span_start:span_end], True))
        cursor = span_end
    if cursor < end:
        segments.append(Segment(line[cursor:end], False))
    if end < len(line):
        segments.append(Segment(ELLIPSIS, False))
    return tuple(segments)


def _wikilink_label(match: re.Match[str]) -> str:
    """`[[名前#見出し|別名]]` を、画面でリンクとして見える文字にする."""
    target, _, alias = match.group(1).partition("|")
    name, _, heading = target.partition("#")
    if alias:
        return alias
    return f"{name} › {heading}" if name and heading else name or heading


def _plain_line(raw: str) -> str:
    """抜き出し用に、Markdownの記号を落として読める文にする.

    抜き出しはHTMLにせず文字のまま見せるので、`**` や `[[ ]]` や表の `|` が残ると読みにくい。
    """
    line = raw.strip()
    line = LINE_PREFIX.sub("", line)
    line = WIKILINK.sub(_wikilink_label, line)
    line = EMPHASIS.sub("", line)
    line = line.replace("$", "")
    if line.startswith("|"):
        line = " ／ ".join(cell.strip() for cell in line.strip("|").split("|"))
    return line.strip()


def _match(note: NoteLink, text: str, terms: list[str]) -> SearchHit | None:
    """1枚のノートを調べる. すべての言葉を含まなければ None."""
    title, _ = _normalize(note.name)
    body, _ = _normalize(text)
    if not all(term in title or term in body for term in terms):
        return None

    snippets: list[tuple[Segment, ...]] = []
    count = 0
    for raw_line in text.splitlines():
        line = _plain_line(raw_line)
        spans = _find_spans(line, terms) if line else []
        if not spans:
            continue
        count += len(spans)
        if len(snippets) < MAX_SNIPPETS:
            snippets.append(_snippet(line, spans))

    path = PurePosixPath(note.path)
    return SearchHit(
        note=note,
        folder=course_folder(path),
        title_hit=any(term in title for term in terms),
        count=count,
        snippets=tuple(snippets),
    )


def search(vault: Vault, query: str) -> list[SearchHit]:
    """ノートを探す. 題名に当たったもの → ヒット数が多いもの → パス順に並べる."""
    terms = [_normalize(word)[0] for word in query.split()]
    if not terms:
        return []

    hits: list[SearchHit] = []
    for group in vault.groups():
        for note in group.notes:
            text = vault.read_note(note.path)
            if text is None:
                continue
            hit = _match(note, text, terms)
            if hit is not None:
                hits.append(hit)

    hits.sort(key=lambda hit: (not hit.title_hit, -hit.count, hit.note.path))
    return hits[:MAX_RESULTS]
