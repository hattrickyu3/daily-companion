"""SQLite接続の生成とスキーマ適用.

PCの突然の電源断が起きうる環境なので、WALモードと外部キー制約を必ず有効にする。
WALは書き込み中のクラッシュに強く、読み書きの同時実行も許す。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from companion.config import DB_PATH

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_MS = 5000


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """設定済みのSQLite接続を返す. 呼び出し側で close すること."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """接続を確実に閉じるコンテキストマネージャ."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """明示的なトランザクション. 例外時はロールバックする."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def apply_schema(conn: sqlite3.Connection) -> None:
    """schema.sql を適用する. 何度実行しても安全."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
