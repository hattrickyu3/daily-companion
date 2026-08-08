"""Googleカレンダーとローカルキャッシュの同期.

取得できなかったときは既存のキャッシュを消さずにそのまま残す。
APIの一時的な失敗で「予定がなかったこと」にしてしまうほうが害が大きいためである。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from companion.config import CALENDAR_SYNC_WINDOW_DAYS
from companion.db.events import list_events_for, prune_events, upsert_events
from companion.integrations.calendar_google import CalendarEvent, fetch_events

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    """同期の結果."""

    fetched: int
    removed: int
    failed: bool

    def describe(self) -> str:
        """ログや通知に出す1行の説明."""
        if self.failed:
            return "カレンダー同期に失敗（キャッシュを維持）"
        return f"予定{self.fetched}件を同期、{self.removed}件を削除"


def sync_window(conn: sqlite3.Connection, today: date) -> SyncResult:
    """前後 CALENDAR_SYNC_WINDOW_DAYS 日分をGoogleと突き合わせる."""
    delta = timedelta(days=CALENDAR_SYNC_WINDOW_DAYS)
    start, end = today - delta, today + delta

    try:
        events = fetch_events(start, end)
    except Exception as exc:  # noqa: BLE001 - Google APIは多様な例外を投げる
        log.warning("calendar sync failed, keeping cached events: %s", exc)
        return SyncResult(fetched=0, removed=0, failed=True)

    fetched = upsert_events(conn, events)
    removed = prune_events(conn, start, end, {event.uid for event in events})
    log.info("calendar synced: %d fetched, %d removed", fetched, removed)
    return SyncResult(fetched=fetched, removed=removed, failed=False)


def events_for(conn: sqlite3.Connection, day: date) -> list[CalendarEvent]:
    """キャッシュから指定日の予定を返す."""
    return list_events_for(conn, day)
