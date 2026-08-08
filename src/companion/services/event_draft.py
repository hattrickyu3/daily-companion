"""予定の自然言語入力の解釈.

「明日10時からゼミ」のような文をClaudeに構造化させる。

解釈結果をそのままカレンダーに書き込むことはしない。
日付や時刻の読み違いは、書き込んだ本人が気づきにくいわりに実害が大きい
（予定を落とす）。必ず確認画面を挟み、修正できる状態にしてから登録する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from companion.config import get_config
from companion.integrations.claude_runner import ClaudeError, extract_json_block, run_claude

log = logging.getLogger(__name__)

PARSE_TIMEOUT_SEC = 90
DEFAULT_DURATION_MINUTES = 60
MAX_TITLE_LENGTH = 100

SYSTEM_PROMPT = (
    "あなたは予定の入力を補助するアシスタントです。"
    "書かれていない情報を勝手に作らず、読み取れたものだけを返してください。"
    "日付や時刻が特定できない場合は、推測せずその旨を返してください。"
)

PROMPT_TEMPLATE = """次の文から予定の情報を読み取ってください。

今日は{today}（{weekday}曜日）です。「明日」「来週の月曜」などはこれを基準に計算してください。

<入力>
{text}
</入力>

次のJSONだけを出力してください。前置きも説明もコードフェンスも不要です。

{{
  "understood": true か false,
  "title": "予定の件名",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM"（時刻が書かれていなければ null）,
  "end_time": "HH:MM"（終了時刻が書かれていなければ null）,
  "location": "場所"（書かれていなければ null）
}}

制約:
- 日付が読み取れない場合は understood を false にしてください。
- 時刻が書かれていない場合は start_time を null にしてください（終日予定として扱います）。
- 件名は入力に書かれた言葉を使ってください。言い換えたり足したりしないでください。
"""


@dataclass(frozen=True)
class EventDraft:
    """確認前の予定案."""

    title: str
    day: date
    start_time: time | None = None
    end_time: time | None = None
    location: str | None = None

    @property
    def is_all_day(self) -> bool:
        return self.start_time is None

    def starts_at(self) -> datetime:
        """開始日時. 終日予定では0時を返す."""
        tz = get_config().timezone
        return datetime.combine(self.day, self.start_time or time(0, 0), tzinfo=tz)

    def ends_at(self) -> datetime:
        """終了日時. 終了時刻がなければ既定の長さを足す."""
        if self.end_time is not None:
            end = datetime.combine(self.day, self.end_time, tzinfo=get_config().timezone)
            # 「22時から翌1時」のように日をまたぐ場合を拾う。
            if end <= self.starts_at():
                end += timedelta(days=1)
            return end
        return self.starts_at() + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    def label(self) -> str:
        """確認画面に出す1行の要約."""
        if self.is_all_day:
            return f"{self.day:%Y年%m月%d日} 終日"
        return f"{self.day:%Y年%m月%d日} {self.starts_at():%H:%M}-{self.ends_at():%H:%M}"


def _parse_time(value: object) -> time | None:
    """HH:MM を time にする. 読めなければ None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_date(value: object) -> date | None:
    """YYYY-MM-DD を date にする. 読めなければ None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def build_draft(payload: dict) -> EventDraft | None:
    """ClaudeのJSONを EventDraft にする. 読み取れなければ None."""
    if payload.get("understood") is False:
        return None

    day = _parse_date(payload.get("date"))
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_LENGTH]
    if day is None or not title:
        return None

    location = payload.get("location")
    return EventDraft(
        title=title,
        day=day,
        start_time=_parse_time(payload.get("start_time")),
        end_time=_parse_time(payload.get("end_time")),
        location=str(location).strip() if location else None,
    )


def parse(text: str, today: date) -> EventDraft | None:
    """自然言語から予定案を作る. 解釈できなければ None（例外は投げない）."""
    weekday = "月火水木金土日"[today.weekday()]
    prompt = PROMPT_TEMPLATE.format(today=today.isoformat(), weekday=weekday, text=text)

    try:
        result = run_claude(prompt, system_prompt=SYSTEM_PROMPT, timeout_sec=PARSE_TIMEOUT_SEC)
    except ClaudeError as exc:
        log.warning("event parsing failed: %s", exc)
        return None

    payload = extract_json_block(result.text)
    if payload is None:
        log.warning("event parsing returned unparseable output")
        return None

    return build_draft(payload)
