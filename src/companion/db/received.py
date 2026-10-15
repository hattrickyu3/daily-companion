"""録音アプリからWebDAVで届いた録音の記録. 送り直しで議事録を二重に作らないために使う."""

from __future__ import annotations

import sqlite3


def claim(conn: sqlite3.Connection, path: str, size: int) -> bool:
    """この録音を議事録にする番を取る. 前に受け取ったことのある録音なら False."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO received_recordings (path, size) VALUES (?, ?)", (path, size)
    )
    return cursor.rowcount == 1


def set_meeting(conn: sqlite3.Connection, path: str, size: int, meeting_id: int) -> None:
    conn.execute(
        "UPDATE received_recordings SET meeting_id = ? WHERE path = ? AND size = ?",
        (meeting_id, path, size),
    )


def is_received(conn: sqlite3.Connection, path: str, size: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM received_recordings WHERE path = ? AND size = ?", (path, size)
    ).fetchone()
    return row is not None


def release(conn: sqlite3.Connection, path: str, size: int) -> None:
    """議事録にする途中で失敗したので番を返す. 送り直されたり起動し直したりしたときに、もう一度試せる."""
    conn.execute("DELETE FROM received_recordings WHERE path = ? AND size = ?", (path, size))
