"""朝のブリーフィングの永続化."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Briefing:
    """1日分のブリーフィング."""

    brief_date: date
    body: str
    created_at: str = ""


def save_briefing(conn: sqlite3.Connection, brief_date: date, body: str) -> None:
    """ブリーフィングを保存する. 同じ日に再実行したら上書きする."""
    conn.execute(
        """
        INSERT INTO briefings (brief_date, body) VALUES (?, ?)
        ON CONFLICT (brief_date) DO UPDATE SET
            body = excluded.body,
            created_at = datetime('now')
        """,
        (brief_date.isoformat(), body),
    )


def get_briefing(conn: sqlite3.Connection, brief_date: date) -> Briefing | None:
    """指定日のブリーフィングを返す. なければ None."""
    row = conn.execute(
        "SELECT * FROM briefings WHERE brief_date = ?", (brief_date.isoformat(),)
    ).fetchone()
    if row is None:
        return None
    return Briefing(
        brief_date=date.fromisoformat(row["brief_date"]),
        body=row["body"],
        created_at=row["created_at"],
    )
