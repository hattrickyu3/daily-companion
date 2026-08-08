"""予定のキャッシュ.

真実の源はGoogleカレンダー側。ここに置くのは
「予定が消されたり書き換えられたあとでも、その日に何があったかを日記と一緒に振り返れる」
ようにするための控えである。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from companion.integrations.calendar_google import CalendarEvent


def _to_event(row: sqlite3.Row) -> CalendarEvent:
    """SELECT の1行を CalendarEvent に戻す."""
    return CalendarEvent(
        uid=row["uid"],
        title=row["title"],
        event_date=date.fromisoformat(row["event_date"]),
        starts_at=datetime.fromisoformat(row["starts_at"]) if row["starts_at"] else None,
        ends_at=datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else None,
        location=row["location"],
        is_all_day=bool(row["is_all_day"]),
    )


def upsert_events(conn: sqlite3.Connection, events: list[CalendarEvent]) -> int:
    """予定を登録・更新し、書き込んだ件数を返す."""
    for event in events:
        conn.execute(
            """
            INSERT INTO calendar_events
                (uid, event_date, starts_at, ends_at, title, location, is_all_day)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (uid) DO UPDATE SET
                event_date = excluded.event_date,
                starts_at  = excluded.starts_at,
                ends_at    = excluded.ends_at,
                title      = excluded.title,
                location   = excluded.location,
                is_all_day = excluded.is_all_day,
                synced_at  = datetime('now')
            """,
            (
                event.uid,
                event.event_date.isoformat(),
                event.starts_at.isoformat() if event.starts_at else None,
                event.ends_at.isoformat() if event.ends_at else None,
                event.title,
                event.location,
                int(event.is_all_day),
            ),
        )
    return len(events)


def prune_events(conn: sqlite3.Connection, start: date, end: date, keep_uids: set[str]) -> int:
    """期間内でGoogle側に存在しなくなった予定を消し、消した件数を返す.

    同期のたびに期間内を突き合わせることで、削除された予定が残り続けるのを防ぐ。
    """
    rows = conn.execute(
        "SELECT uid FROM calendar_events WHERE event_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    stale = [row["uid"] for row in rows if row["uid"] not in keep_uids]
    for uid in stale:
        conn.execute("DELETE FROM calendar_events WHERE uid = ?", (uid,))
    return len(stale)


def list_events_for(conn: sqlite3.Connection, day: date) -> list[CalendarEvent]:
    """指定日の予定を時刻順に返す."""
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE event_date = ?"
        " ORDER BY is_all_day DESC, starts_at, title",
        (day.isoformat(),),
    ).fetchall()
    return [_to_event(row) for row in rows]


def list_events_between(conn: sqlite3.Connection, start: date, end: date) -> list[CalendarEvent]:
    """期間内の予定を日付・時刻順に返す."""
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE event_date BETWEEN ? AND ?"
        " ORDER BY event_date, is_all_day DESC, starts_at, title",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [_to_event(row) for row in rows]
