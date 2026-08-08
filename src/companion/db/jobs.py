"""ジョブ実行履歴の読み出し.

書き込みは jobs/base.py が行う。ここは画面に出すための参照専用。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from companion.clock import utc_to_local

DEFAULT_HISTORY_LIMIT = 20


@dataclass(frozen=True)
class JobRun:
    """1回分の実行記録."""

    job_name: str
    status: str
    message: str | None
    duration_ms: int | None
    ran_at: datetime

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    @property
    def duration_label(self) -> str:
        """所要時間の表示. 記録がなければ空文字."""
        if self.duration_ms is None:
            return ""
        return f"{self.duration_ms / 1000:.1f}秒"


def _to_run(row: sqlite3.Row) -> JobRun:
    return JobRun(
        job_name=row["job_name"],
        status=row["status"],
        message=row["message"],
        duration_ms=row["duration_ms"],
        ran_at=utc_to_local(row["ran_at"]),
    )


def list_recent_runs(
    conn: sqlite3.Connection, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[JobRun]:
    """新しい順に実行記録を返す."""
    rows = conn.execute(
        "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_to_run(row) for row in rows]


def latest_run_per_job(conn: sqlite3.Connection) -> list[JobRun]:
    """ジョブごとの最新の実行記録を返す. 「今朝動いたか」を一目で見るため."""
    rows = conn.execute(
        "SELECT * FROM job_runs WHERE id IN"
        " (SELECT max(id) FROM job_runs GROUP BY job_name) ORDER BY job_name"
    ).fetchall()
    return [_to_run(row) for row in rows]
