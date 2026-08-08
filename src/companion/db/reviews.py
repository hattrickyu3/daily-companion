"""週次レポートの永続化.

週は月曜始まりとし、その週の月曜日を主キーにする。
週の途中で何度実行しても同じ行を更新するだけになる。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class WeeklyReview:
    """1週間分のレポート."""

    week_start: date
    body: str
    created_at: str = ""

    @property
    def week_end(self) -> date:
        """その週の日曜日."""
        return self.week_start + timedelta(days=6)

    def label(self) -> str:
        """一覧に出す期間の表示."""
        return f"{self.week_start:%Y/%m/%d} - {self.week_end:%m/%d}"


def save_review(conn: sqlite3.Connection, week_start: date, body: str) -> None:
    """レポートを保存する. 同じ週なら上書きする."""
    conn.execute(
        """
        INSERT INTO weekly_reviews (week_start, body) VALUES (?, ?)
        ON CONFLICT (week_start) DO UPDATE SET
            body = excluded.body,
            created_at = datetime('now')
        """,
        (week_start.isoformat(), body),
    )


def _to_review(row: sqlite3.Row) -> WeeklyReview:
    return WeeklyReview(
        week_start=date.fromisoformat(row["week_start"]),
        body=row["body"],
        created_at=row["created_at"],
    )


def get_review(conn: sqlite3.Connection, week_start: date) -> WeeklyReview | None:
    """指定週のレポートを返す. なければ None."""
    row = conn.execute(
        "SELECT * FROM weekly_reviews WHERE week_start = ?", (week_start.isoformat(),)
    ).fetchone()
    return _to_review(row) if row else None


def list_reviews(conn: sqlite3.Connection, *, limit: int = 20) -> list[WeeklyReview]:
    """新しい週から順にレポートを返す."""
    rows = conn.execute(
        "SELECT * FROM weekly_reviews ORDER BY week_start DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_to_review(row) for row in rows]
