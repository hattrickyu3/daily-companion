"""Googleカレンダーとローカルキャッシュの同期.

取得できなかったときは既存のキャッシュを消さずにそのまま残す。
APIの一時的な失敗で「予定がなかったこと」にしてしまうほうが害が大きいためである。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta

from companion.config import CALENDAR_SYNC_FUTURE_DAYS, CALENDAR_SYNC_PAST_DAYS
from companion.db.events import list_events_for, prune_events, upsert_events
from companion.integrations.calendar_google import CalendarEvent, fetch_events

log = logging.getLogger(__name__)

# 同じ範囲を画面を開くたびに取りに行かないための間隔。
REFRESH_INTERVAL_SEC = 300

_last_refresh: dict[tuple[date, date], float] = {}


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
    """定期同期の範囲をGoogleと突き合わせる."""
    start = today - timedelta(days=CALENDAR_SYNC_PAST_DAYS)
    end = today + timedelta(days=CALENDAR_SYNC_FUTURE_DAYS)
    return sync_range(conn, start, end)


def sync_range(conn: sqlite3.Connection, start: date, end: date) -> SyncResult:
    """指定期間をGoogleと突き合わせる."""
    try:
        events = fetch_events(start, end)
    except Exception as exc:  # noqa: BLE001 - Google APIは多様な例外を投げる
        log.warning("calendar sync failed, keeping cached events: %s", exc)
        return SyncResult(fetched=0, removed=0, failed=True)

    fetched = upsert_events(conn, events)
    removed = prune_events(conn, start, end, {event.uid for event in events})
    log.info("calendar synced: %d fetched, %d removed", fetched, removed)
    return SyncResult(fetched=fetched, removed=removed, failed=False)


def refresh_if_stale(conn: sqlite3.Connection, start: date, end: date) -> None:
    """画面を開いた期間を取り込み直す.

    定期同期の範囲外の月を見ても空にならないようにするためのもの。
    短い間隔で何度も叩かないよう、同じ範囲は REFRESH_INTERVAL_SEC 秒あけて取りに行く。
    失敗しても何もしない（キャッシュの内容がそのまま使われる）。
    """
    key = (start, end)
    now = time.monotonic()

    last = _last_refresh.get(key)
    if last is not None and now - last < REFRESH_INTERVAL_SEC:
        return

    _last_refresh[key] = now
    sync_range(conn, start, end)


def clear_refresh_marks() -> None:
    """取り込み間隔の記録を捨てる. テストと手動更新で使う."""
    _last_refresh.clear()


def events_for(conn: sqlite3.Connection, day: date) -> list[CalendarEvent]:
    """キャッシュから指定日の予定を返す."""
    return list_events_for(conn, day)
