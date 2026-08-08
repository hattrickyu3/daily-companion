"""週次レポートの生成.

1週間分の日記をまとめてClaudeに渡し、傾向・やり残し・来週の提案を書かせる。

日記が1日もない週はレポートを作らない。材料がないまま書かせると、
それらしい一般論が出てきてしまい、続けるほど信用できなくなるためである。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from companion.db.repository import Entry, list_entries_between
from companion.integrations.claude_runner import ClaudeError, run_claude, strip_markdown_headings

log = logging.getLogger(__name__)

REVIEW_TIMEOUT_SEC = 240
DAYS_IN_WEEK = 7

SYSTEM_PROMPT = (
    "あなたは利用者本人のための振り返りアシスタントです。"
    "渡された日記だけを根拠にし、書かれていないことを推測で足さないでください。"
    "褒めも説教もせず、事実の整理と具体的な提案だけを書いてください。"
)

PROMPT_TEMPLATE = """{start}から{end}までの1週間の日記です。振り返りをまとめてください。

<日記>
{entries}
</日記>

<やり残し（各日の申し送り）>
{carry_over}
</やり残し>

次の4つの見出しで書いてください。見出しはそのまま使ってください。

今週の傾向
（何に時間を使ったか。2〜3文）

よかったこと
（うまくいったこと。2〜3項目、各1行）

やり残し
（片付いていない申し送り。なければ「なし」）

来週の提案
（やり残しと傾向を踏まえた具体的な提案。2項目まで、各1行）

制約:
- 日記に書かれていないことは書かないでください。
- 全体で400字以内にしてください。
- 一般論や励ましは書かないでください。
- 見出しに # などの記号を付けないでください。そのまま表示されます。
"""


def _format_entries(entries: list[Entry]) -> str:
    """日記をプロンプト用に整える."""
    blocks: list[str] = []
    for entry in entries:
        lines = [f"[{entry.entry_date:%m/%d (%a)}]"]
        lines.append(entry.summary or entry.raw_text[:200])
        if entry.done:
            lines.append("やったこと: " + "、".join(entry.done))
        if entry.insights:
            lines.append("気づき: " + "、".join(entry.insights))
        if entry.tags:
            lines.append("タグ: " + "、".join(entry.tags))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_carry_over(entries: list[Entry]) -> str:
    """申し送りを日付つきで並べる."""
    notes = [
        f"{entry.entry_date:%m/%d}: {entry.carry_over}" for entry in entries if entry.carry_over
    ]
    return "\n".join(notes) if notes else "（なし）"


def collect_entries(conn: sqlite3.Connection, week_start: date) -> list[Entry]:
    """その週の日記を古い順に返す."""
    return list_entries_between(conn, week_start, week_start + timedelta(days=DAYS_IN_WEEK - 1))


def generate(conn: sqlite3.Connection, week_start: date) -> str | None:
    """週次レポートの本文を作る. 材料がなければ None."""
    entries = collect_entries(conn, week_start)
    if not entries:
        log.info("no entries for week starting %s, skipping review", week_start)
        return None

    prompt = PROMPT_TEMPLATE.format(
        start=week_start.isoformat(),
        end=(week_start + timedelta(days=DAYS_IN_WEEK - 1)).isoformat(),
        entries=_format_entries(entries),
        carry_over=_format_carry_over(entries),
    )

    try:
        result = run_claude(prompt, system_prompt=SYSTEM_PROMPT, timeout_sec=REVIEW_TIMEOUT_SEC)
    except ClaudeError as exc:
        log.warning("weekly review generation failed: %s", exc)
        return None

    body = strip_markdown_headings(result.text.strip()).strip()
    if not body:
        log.warning("weekly review generation returned empty text")
        return None
    return body
