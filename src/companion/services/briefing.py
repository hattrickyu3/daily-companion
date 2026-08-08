"""朝のブリーフィングの生成.

材料は「今日の予定」「昨日の日記」「直近の申し送り」の3つだけに絞る。
情報を詰め込むほど読まれなくなるので、通知1画面に収まる長さを守らせる。

Claudeが使えないときでも、予定だけを並べた最低限のブリーフィングは出す。
毎朝必ず何かが届くことのほうが、内容の良さより続ける上で効くためである。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from companion.db.events import list_events_for
from companion.db.repository import Entry, get_entry
from companion.integrations.calendar_google import CalendarEvent
from companion.integrations.claude_runner import ClaudeError, run_claude

log = logging.getLogger(__name__)

BRIEFING_TIMEOUT_SEC = 180
MAX_EVENTS = 5
CARRY_OVER_LOOKBACK_DAYS = 3

SYSTEM_PROMPT = (
    "あなたは利用者本人のための朝のアシスタントです。"
    "与えられた材料だけを根拠にし、推測で予定や事実を作らないでください。"
    "励ましや説教は書かず、事実と提案を短く伝えてください。"
)

PROMPT_TEMPLATE = """今日({today})の朝のブリーフィングを書いてください。

<今日の予定>
{events}
</今日の予定>

<昨日の日記>
{yesterday}
</昨日の日記>

<直近の申し送り>
{carry_over}
</直近の申し送り>

書き方:
- 全体で4行以内。1行は40字以内。
- 1行目は今日の予定の要点。予定がなければ「今日は予定なし」と書く。
- 2行目は昨日の振り返りを1行。日記がなければ省略してよい。
- 3行目は申し送りを踏まえた今日の提案を1行。
- 箇条書き記号や見出しは使わず、そのまま読める文章にする。
- 材料にないことは書かない。
"""


def format_events(events: list[CalendarEvent]) -> str:
    """予定をプロンプト・通知の両方で使う形に整える."""
    if not events:
        return "（予定なし）"
    return "\n".join(f"{e.time_label()} {e.title}" for e in events[:MAX_EVENTS])


def _format_entry(entry: Entry | None) -> str:
    """昨日の日記をプロンプト用に整える."""
    if entry is None:
        return "（記録なし）"
    lines = [entry.summary or entry.raw_text[:200]]
    if entry.done:
        lines.append("やったこと: " + "、".join(entry.done))
    return "\n".join(lines)


def find_recent_carry_over(conn: sqlite3.Connection, today: date) -> str:
    """直近数日分の申し送りを集める. なければ空文字."""
    notes: list[str] = []
    for offset in range(1, CARRY_OVER_LOOKBACK_DAYS + 1):
        entry = get_entry(conn, today - timedelta(days=offset))
        if entry is not None and entry.carry_over:
            notes.append(f"{entry.entry_date.isoformat()}: {entry.carry_over}")
    return "\n".join(notes) if notes else "（なし）"


def build_fallback(events: list[CalendarEvent], yesterday_missing: bool) -> str:
    """Claudeが使えないときの最低限のブリーフィング."""
    lines = ["今日の予定:", format_events(events)]
    if yesterday_missing:
        lines.append("昨日の記録がまだです。")
    return "\n".join(lines)


def generate(conn: sqlite3.Connection, today: date) -> str:
    """今日のブリーフィング本文を作る. Claudeが失敗しても必ず何かを返す."""
    events = list_events_for(conn, today)
    yesterday = get_entry(conn, today - timedelta(days=1))

    prompt = PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        events=format_events(events),
        yesterday=_format_entry(yesterday),
        carry_over=find_recent_carry_over(conn, today),
    )

    try:
        result = run_claude(
            prompt, system_prompt=SYSTEM_PROMPT, timeout_sec=BRIEFING_TIMEOUT_SEC
        )
    except ClaudeError as exc:
        log.warning("briefing generation failed, using fallback: %s", exc)
        return build_fallback(events, yesterday is None)

    body = result.text.strip()
    if not body:
        log.warning("briefing generation returned empty text, using fallback")
        return build_fallback(events, yesterday is None)

    # 昨日の記録漏れは、Claudeの文面に頼らず必ず自分で1行添える。
    if yesterday is None:
        body = f"{body}\n昨日の記録がまだです。"
    return body
