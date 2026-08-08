"""予定の取得（短時間キャッシュつき）.

Web画面を開くたびにGoogle APIを叩くと表示が遅くなるので、
同じ日の問い合わせは数分キャッシュする。予定は分単位で変わるものではない。
"""

from __future__ import annotations

import time
from datetime import date

from companion.integrations.calendar_google import CalendarEvent, fetch_events_safe

CACHE_TTL_SEC = 300

_cache: dict[tuple[date, date], tuple[float, list[CalendarEvent]]] = {}


def get_events(start: date, end: date) -> list[CalendarEvent]:
    """期間内の予定を返す. 取得失敗時は空リスト（縮退動作）."""
    key = (start, end)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    events = fetch_events_safe(start, end)
    _cache[key] = (now, events)
    return events


def get_events_for(day: date) -> list[CalendarEvent]:
    """1日分の予定を返す."""
    return get_events(day, day)


def clear_cache() -> None:
    """キャッシュを捨てる. テストと手動更新で使う."""
    _cache.clear()
