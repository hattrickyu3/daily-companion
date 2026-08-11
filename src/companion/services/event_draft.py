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
# 繰り返しの間隔の上限。「毎1000週」のような読み違いをそのまま送らないための歯止め。
MAX_INTERVAL = 99

# RFC5545(iCalendar)の曜日コード。date.weekday() は月曜が0。
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = {"MO": "月", "TU": "火", "WE": "水", "TH": "木", "FR": "金", "SA": "土", "SU": "日"}
VALID_FREQ = {"DAILY": "毎日", "WEEKLY": "毎週", "MONTHLY": "毎月"}

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
  "date": "YYYY-MM-DD"（開始日）,
  "end_date": "YYYY-MM-DD"（最終日。1日で終わるなら null）,
  "start_time": "HH:MM"（時刻が書かれていなければ null）,
  "end_time": "HH:MM"（終了時刻が書かれていなければ null）,
  "location": "場所"（書かれていなければ null）,
  "repeat": 繰り返す場合だけ次の形。繰り返さないなら null
    {{
      "freq": "daily" か "weekly" か "monthly",
      "interval": 間隔（「隔週」なら2、ふつうは1）,
      "weekdays": ["sun", "mon", ...]（weekly のときだけ。曜日が書かれていなければ空配列）,
      "until": "YYYY-MM-DD"（「〜まで」と書かれていなければ null）
    }}
}}

制約:
- 日付が読み取れない場合は understood を false にしてください。
- 「20日から23日まで」「3日間」のように複数日にわたる場合は、
  end_date に最終日（その日を含む）を入れてください。1日で終わるなら null にしてください。
- 「毎週日曜」「毎月1日」「隔週火曜」のように繰り返す場合は repeat を埋めてください。
  このとき date には最初の1回目の日付を入れてください。
- 繰り返しかどうか書かれていない場合は repeat を null にしてください。
  一度きりの予定を繰り返しにしてしまうと、カレンダーが埋め尽くされて戻すのが大変です。
- 時刻が書かれていない場合は start_time を null にしてください（終日予定として扱います）。
- 件名は入力に書かれた言葉を使ってください。言い換えたり足したりしないでください。
"""


@dataclass(frozen=True)
class Recurrence:
    """繰り返しの規則.

    Googleカレンダーには RRULE の文字列で渡すが、アプリ内では組み立て済みの
    文字列ではなく部品のまま持つ。確認画面で1項目ずつ直せるようにするため。
    """

    freq: str  # DAILY / WEEKLY / MONTHLY
    interval: int = 1
    # WEEKLY のときの曜日。空なら開始日の曜日を使う。
    weekdays: tuple[str, ...] = ()
    # 繰り返しの最終日（その日を含む）。None なら無期限。
    until: date | None = None

    def resolved_weekdays(self, day: date) -> tuple[str, ...]:
        """実際に使う曜日. 指定がなければ開始日の曜日にする."""
        if self.freq != "WEEKLY":
            return ()
        return self.weekdays or (WEEKDAY_CODES[day.weekday()],)

    def to_rrule(self, day: date, *, all_day: bool = False) -> str:
        """Googleカレンダーに渡す RRULE 文字列を組み立てる.

        UNTIL の書式は開始側の型に合わせる必要がある（RFC 5545）。
        終日予定は日付だけ、時刻つきは日時で書かないと弾かれる。
        """
        parts = [f"FREQ={self.freq}"]
        if self.interval > 1:
            parts.append(f"INTERVAL={self.interval}")
        weekdays = self.resolved_weekdays(day)
        if weekdays:
            parts.append(f"BYDAY={','.join(weekdays)}")
        if self.until is not None:
            # その日を含めて終わらせたいので、時刻つきのときは日の終わりを指す。
            stamp = f"{self.until:%Y%m%d}" if all_day else f"{self.until:%Y%m%d}T235959Z"
            parts.append(f"UNTIL={stamp}")
        return "RRULE:" + ";".join(parts)

    def label(self, day: date) -> str:
        """確認画面に出す説明."""
        head = VALID_FREQ.get(self.freq, self.freq)
        if self.interval > 1:
            head = {"DAILY": f"{self.interval}日ごと", "WEEKLY": f"{self.interval}週ごと"}.get(
                self.freq, f"{self.interval}か月ごと"
            )
        weekdays = self.resolved_weekdays(day)
        if weekdays:
            head += "".join(WEEKDAY_NAMES.get(code, code) for code in weekdays) + "曜"
        if self.until is not None:
            return f"{head}（{self.until:%Y年%m月%d日}まで）"
        return head


@dataclass(frozen=True)
class EventDraft:
    """確認前の予定案."""

    title: str
    day: date
    start_time: time | None = None
    end_time: time | None = None
    location: str | None = None
    # 最終日（その日を含む）。合宿のように複数日にわたる予定で使う。
    # 1日で終わるなら None。
    end_day: date | None = None
    # 繰り返しの規則。1回きりなら None。
    repeat: Recurrence | None = None

    @property
    def repeats(self) -> bool:
        return self.repeat is not None

    def recurrence_rules(self) -> list[str] | None:
        """Googleカレンダーに渡す recurrence. 繰り返さないなら None."""
        if not self.repeat:
            return None
        return [self.repeat.to_rrule(self.day, all_day=self.is_all_day)]

    def repeat_label(self) -> str:
        """確認画面に出す繰り返しの説明. 繰り返さないなら空."""
        return self.repeat.label(self.day) if self.repeat else ""

    @property
    def is_all_day(self) -> bool:
        return self.start_time is None

    @property
    def last_day(self) -> date:
        """最終日. 開始日より前の値が来ても開始日まで戻す."""
        if self.end_day is None or self.end_day < self.day:
            return self.day
        return self.end_day

    @property
    def spans_days(self) -> bool:
        """複数日にまたがるか."""
        return self.last_day > self.day

    def starts_at(self) -> datetime:
        """開始日時. 終日予定では0時を返す."""
        tz = get_config().timezone
        return datetime.combine(self.day, self.start_time or time(0, 0), tzinfo=tz)

    def ends_at(self) -> datetime:
        """終了日時. 終了時刻がなければ既定の長さを足す."""
        if self.end_time is not None:
            end = datetime.combine(self.last_day, self.end_time, tzinfo=get_config().timezone)
            # 「22時から翌1時」のように日をまたぐ場合を拾う。
            # 最終日が明示されているならそちらが正しいので、足さない。
            if end <= self.starts_at() and not self.spans_days:
                end += timedelta(days=1)
            return end
        if self.spans_days:
            # 最終日だけ指定されて終了時刻がない場合は、その日の同じ時刻まで。
            return datetime.combine(
                self.last_day, self.start_time or time(0, 0), tzinfo=get_config().timezone
            ) + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        return self.starts_at() + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    def label(self) -> str:
        """確認画面に出す1行の要約."""
        span = f"{self.day:%Y年%m月%d日}"
        if self.spans_days:
            span += f" 〜 {self.last_day:%Y年%m月%d日}"
        if self.is_all_day:
            return f"{span} 終日"
        return f"{span} {self.starts_at():%H:%M}-{self.ends_at():%H:%M}"


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


def _parse_weekdays(value: object) -> tuple[str, ...]:
    """曜日の配列を RFC5545 のコードにする. 読めないものは捨てる."""
    if not isinstance(value, list):
        return ()
    lookup = {code.lower(): code for code in WEEKDAY_CODES}
    # "sun" / "sunday" / "SU" のどれで来ても拾えるようにする。
    lookup |= {"sunday": "SU", "monday": "MO", "tuesday": "TU", "wednesday": "WE",
               "thursday": "TH", "friday": "FR", "saturday": "SA"}
    lookup |= {"sun": "SU", "mon": "MO", "tue": "TU", "wed": "WE",
               "thu": "TH", "fri": "FR", "sat": "SA"}

    codes = []
    for item in value:
        code = lookup.get(str(item).strip().lower())
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def build_recurrence(raw: object) -> Recurrence | None:
    """繰り返しの指定を Recurrence にする. 読み取れなければ None（＝1回きり）."""
    if not isinstance(raw, dict):
        return None

    freq = str(raw.get("freq", "")).strip().upper()
    if freq not in VALID_FREQ:
        return None

    try:
        interval = int(raw.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    interval = max(1, min(interval, MAX_INTERVAL))

    return Recurrence(
        freq=freq,
        interval=interval,
        weekdays=_parse_weekdays(raw.get("weekdays")),
        until=_parse_date(raw.get("until")),
    )


def build_draft(payload: dict) -> EventDraft | None:
    """ClaudeのJSONを EventDraft にする. 読み取れなければ None."""
    if payload.get("understood") is False:
        return None

    day = _parse_date(payload.get("date"))
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_LENGTH]
    if day is None or not title:
        return None

    location = payload.get("location")
    # 開始日より前の最終日は読み違いなので捨てる。単日として扱ったほうが害が小さい。
    end_day = _parse_date(payload.get("end_date"))
    if end_day is not None and end_day <= day:
        end_day = None

    # 繰り返しの期限が開始日より前なら、規則ごと捨てて1回きりにする。
    # 1回も発生しない予定を作っても、入れたはずの予定が消えるだけになる。
    repeat = build_recurrence(payload.get("repeat"))
    if repeat is not None and repeat.until is not None and repeat.until < day:
        repeat = None

    return EventDraft(
        title=title,
        day=day,
        start_time=_parse_time(payload.get("start_time")),
        end_time=_parse_time(payload.get("end_time")),
        location=str(location).strip() if location else None,
        end_day=end_day,
        repeat=repeat,
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
