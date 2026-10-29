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


# 既存のDBに後から足した列。schema.sql の CREATE TABLE は
# IF NOT EXISTS なので、既にあるテーブルには新しい列が入らない。
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (テーブル, 列名, 型) — 複数日の予定に対応するため 2026-08-11 に追加
    ("calendar_events", "end_date", "TEXT"),
    # 議事録の参加者の名前 — 話者の聞き分けのため 2026-10-02 に追加
    ("meetings", "participants", "TEXT NOT NULL DEFAULT ''"),
    # 議事録を手で直したか — 清書のやり直しで上書きする前に警告するため 2026-10-03 に追加
    ("meetings", "hand_edited", "INTEGER NOT NULL DEFAULT 0"),
    # 議事録の場面と授業 — ゼミ以外（授業・打ち合わせ・ひとりメモ）にも使うため 2026-10-24 に追加
    ("meetings", "kind", "TEXT NOT NULL DEFAULT 'seminar'"),
    ("meetings", "course", "TEXT NOT NULL DEFAULT ''"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """後から足した列が無ければ追加する. 何度実行しても安全."""
    for table, column, column_type in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # テーブル自体がまだ無い（新規DB）なら schema.sql 側で作られる
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def apply_schema(conn: sqlite3.Connection) -> None:
    """schema.sql を適用する. 何度実行しても安全."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _add_missing_columns(conn)
