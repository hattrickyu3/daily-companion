"""月カレンダーの組み立て.

DBから引いた日記と予定を、月グリッドに並べられる形に整える。
表示に必要な計算をここに集めて、テンプレート側にロジックを持ち込まない。
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from companion.db.events import list_events_between
from companion.db.repository import list_entries_between

# 日曜始まり。日本のカレンダーの慣習に合わせる。
FIRST_WEEKDAY = calendar.SUNDAY
WEEKDAY_LABELS = ("日", "月", "火", "水", "木", "金", "土")
SNIPPET_LENGTH = 40


@dataclass(frozen=True)
class DayCell:
    """カレンダーの1マス."""

    day: date | None
    has_entry: bool = False
    snippet: str = ""
    event_count: int = 0
    is_today: bool = False

    @property
    def is_blank(self) -> bool:
        """月の外にあたる空きマスか."""
        return self.day is None

    @property
    def is_sunday(self) -> bool:
        return self.day is not None and self.day.weekday() == calendar.SUNDAY

    @property
    def is_saturday(self) -> bool:
        return self.day is not None and self.day.weekday() == calendar.SATURDAY


@dataclass(frozen=True)
class MonthView:
    """1か月分の表示データ."""

    year: int
    month: int
    weeks: tuple[tuple[DayCell, ...], ...]
    prev_month: str
    next_month: str
    entry_count: int
    event_count: int

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月"

    @property
    def key(self) -> str:
        """URLに載せる YYYY-MM 形式."""
        return f"{self.year:04d}-{self.month:02d}"


def parse_month(value: str | None, fallback: date) -> tuple[int, int]:
    """YYYY-MM を (年, 月) にする. 不正なら fallback の年月を返す."""
    if not value:
        return fallback.year, fallback.month
    try:
        year, month = value.split("-")
        parsed = date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return fallback.year, fallback.month
    return parsed.year, parsed.month


def shift_month(year: int, month: int, delta: int) -> str:
    """前後の月を YYYY-MM で返す."""
    total = (year * 12 + (month - 1)) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """その月の初日と末日を返す."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _snippet(text: str) -> str:
    """マスに載せる短い抜粋."""
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_LENGTH:
        return flat
    return flat[:SNIPPET_LENGTH] + "…"


def build(conn: sqlite3.Connection, year: int, month: int, today: date) -> MonthView:
    """月表示のデータを組み立てる."""
    start, end = month_bounds(year, month)

    entries = {
        entry.entry_date: entry for entry in list_entries_between(conn, start, end)
    }

    # 複数日にまたがる予定は、その期間の全ての日で数える。
    # 初日だけに数えると、カレンダー上で2日目以降が空いているように見える。
    events_per_day: dict[date, int] = {}
    for event in list_events_between(conn, start, end):
        day = max(event.event_date, start)
        last = min(event.last_date, end)
        while day <= last:
            events_per_day[day] = events_per_day.get(day, 0) + 1
            day += timedelta(days=1)

    grid = calendar.Calendar(firstweekday=FIRST_WEEKDAY)
    weeks: list[tuple[DayCell, ...]] = []

    for week in grid.monthdatescalendar(year, month):
        cells: list[DayCell] = []
        for day in week:
            if day.month != month:
                # 前後の月にはみ出したマスは空欄にする。
                # 数字を出すと、その月の日付と見間違えるため。
                cells.append(DayCell(day=None))
                continue

            entry = entries.get(day)
            cells.append(
                DayCell(
                    day=day,
                    has_entry=entry is not None,
                    snippet=_snippet(entry.summary or entry.raw_text) if entry else "",
                    event_count=events_per_day.get(day, 0),
                    is_today=day == today,
                )
            )
        weeks.append(tuple(cells))

    return MonthView(
        year=year,
        month=month,
        weeks=tuple(weeks),
        prev_month=shift_month(year, month, -1),
        next_month=shift_month(year, month, 1),
        entry_count=len(entries),
        event_count=sum(events_per_day.values()),
    )
