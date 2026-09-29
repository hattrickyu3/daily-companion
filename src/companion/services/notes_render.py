"""Obsidianのノート（Markdown）をスマホで読むためのHTMLにする.

普通のMarkdownに加えて、Obsidian独自の書き方のうち、実際のノートで使うものだけに対応する。
- `[[ノート名]]` `[[ノート名#見出し|別名]]` の内部リンク
- `![[画像.png]]` の埋め込み
- `> [!warning] 題名` のコールアウト
- `$...$` `$$...$$` の数式（ここでは印を付けるだけで、描くのはブラウザ側のKaTeX）

ノートに書かれた生のHTMLは表示しない。ノートはClaudeが資料から作ることもあり、
外から来た文章がそのままスクリプトとして動くのを防ぐため。
"""

from __future__ import annotations

import re
from html import escape
from pathlib import PurePosixPath
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin

from companion.services.notes import NOTE_SUFFIX, Vault

VIEW_PREFIX = "/notes/view/"
FILE_PREFIX = "/notes/file/"
# SVGは入れない（中にスクリプトを書けるので、ファイルはダウンロードとして渡している）。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic"})

WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+)\]\]")
CALLOUT = re.compile(r"^\[!(\w+)\][+-]?[ \t]*([^\n]*)\n?")
CALLOUT_LABELS = {
    "note": "メモ",
    "info": "情報",
    "tip": "ヒント",
    "warning": "注意",
    "caution": "注意",
    "danger": "危険",
    "question": "質問",
    "example": "例",
}


def slugify(heading: str) -> str:
    """見出しのid. 日本語はそのまま残し、空白だけ - にする.

    `[[ノート#見出し]]` のリンク先と同じ規則で作らないとページ内で飛べない。
    """
    return re.sub(r"\s+", "-", heading.strip())


def _wikilink_rule(state: StateInline, silent: bool) -> bool:
    """`[[...]]` と `![[...]]` を1つの部品として切り出す."""
    match = WIKILINK.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        token = state.push("wikilink", "", 0)
        token.meta = {"embed": bool(match.group(1)), "target": match.group(2)}
    state.pos = match.end()
    return True


def _callout_rule(state: StateCore) -> None:
    """`> [!type] 題名` で始まる引用を、題名を太字にした枠にする."""
    tokens = state.tokens
    for index, token in enumerate(tokens[:-2]):
        inline = tokens[index + 2]
        if token.type != "blockquote_open" or inline.type != "inline":
            continue
        match = CALLOUT.match(inline.content)
        if match is None:
            continue
        kind = match.group(1).lower()
        title = match.group(2).strip() or CALLOUT_LABELS.get(kind, kind)
        rest = inline.content[match.end():]
        token.attrSet("class", "callout")
        inline.content = f"**{title}**" + (f"\n{rest}" if rest else "")


def _split_target(raw: str) -> tuple[str, str, str]:
    """`名前#見出し|別名` を (名前, 見出し, 別名) に分ける."""
    target, _, alias = raw.partition("|")
    name, _, heading = target.partition("#")
    return name.strip(), heading.strip(), alias.strip()


def _render_wikilink(renderer, tokens, idx, options, env) -> str:  # noqa: ANN001 - markdown-itの決まった形
    meta = tokens[idx].meta
    vault: Vault = env["vault"]
    name, heading, alias = _split_target(meta["target"])
    anchor = f"#{quote(slugify(heading))}" if heading else ""

    if not name:  # [[#見出し]] は同じページの中
        label = escape(alias or heading)
        return f'<a href="{anchor}">{label}</a>' if anchor else label

    label = escape(alias or (f"{name} › {heading}" if heading else name))
    rel = vault.resolve(name, current=env["current"])
    if rel is None:
        return f'<span class="missing">{label}</span>'

    suffix = PurePosixPath(rel).suffix.lower()
    if meta["embed"] and suffix in IMAGE_SUFFIXES:
        alt = escape(alias or name)
        return f'<img src="{FILE_PREFIX}{quote(rel)}" alt="{alt}" loading="lazy">'
    if suffix == NOTE_SUFFIX:
        return f'<a href="{VIEW_PREFIX}{quote(rel)}{anchor}">{label}</a>'
    return f'<a href="{FILE_PREFIX}{quote(rel)}">{label}</a>'


def _build_parser() -> MarkdownIt:
    # breaks: Obsidianの既定と同じく、1回の改行もそのまま改行として見せる。
    md = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable("table")
    md.use(dollarmath_plugin)
    md.use(anchors_plugin, min_level=1, max_level=6, slug_func=slugify)
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.core.ruler.after("block", "callout", _callout_rule)
    md.add_render_rule("wikilink", _render_wikilink)
    return md


_PARSER = _build_parser()


def render(text: str, current: str, vault: Vault) -> str:
    """ノートの本文をHTMLにする. current は今のノートの相対パス（リンク先を探す起点）."""
    return _PARSER.render(text, {"vault": vault, "current": current})
