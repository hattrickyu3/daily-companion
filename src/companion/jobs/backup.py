"""データベースのバックアップ.

    uv run python -m companion.jobs.backup

朝のブリーフィング(morning_brief)から毎日呼ばれるほか、単体でも実行できる。

ファイルをコピーするのではなく、SQLiteのオンラインバックアップAPIを使う。
WALモードで動いている最中にファイルをコピーすると、
まだ本体に反映されていない書き込みを取りこぼした複製ができてしまうためである。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from companion.clock import now_local
from companion.config import BACKUP_DIR, BACKUP_GENERATIONS, DB_PATH
from companion.db.connection import connect
from companion.jobs.base import run_job

log = logging.getLogger(__name__)

JOB_NAME = "backup"
BACKUP_PREFIX = "companion-"
BACKUP_SUFFIX = ".db"


def backup_path(moment: datetime, backup_dir: Path = BACKUP_DIR) -> Path:
    """バックアップ先のパス. 1日1ファイルになる名前にする."""
    return backup_dir / f"{BACKUP_PREFIX}{moment:%Y%m%d}{BACKUP_SUFFIX}"


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """既存のバックアップを新しい順に返す."""
    if not backup_dir.exists():
        return []
    found = backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
    return sorted(found, key=lambda path: path.name, reverse=True)


def prune_backups(
    backup_dir: Path = BACKUP_DIR, generations: int = BACKUP_GENERATIONS
) -> list[Path]:
    """世代数を超えた古いバックアップを消し、消したパスを返す."""
    stale = list_backups(backup_dir)[generations:]
    for path in stale:
        path.unlink(missing_ok=True)
        log.info("removed old backup: %s", path.name)
    return stale


def create_backup(
    source: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, moment: datetime | None = None
) -> Path:
    """DBを複製して、その保存先を返す.

    書き込み中でも安全に複製できるよう、SQLiteのバックアップAPIを使う。
    """
    if not source.exists():
        raise FileNotFoundError(f"バックアップ元が見つかりません: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_path(moment or now_local(), backup_dir)

    # sqlite3の接続を with に直接渡すとトランザクション管理になり、接続は閉じられない。
    # ファイルハンドルを確実に手放すため closing で包む。
    with closing(connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)

    log.info("backup created: %s (%d bytes)", target.name, target.stat().st_size)
    return target


def run() -> str:
    """バックアップを取り、古い世代を掃除する. 実行結果のメッセージを返す."""
    target = create_backup()
    removed = prune_backups()
    size_kb = target.stat().st_size / 1024

    message = f"{target.name} を作成（{size_kb:.0f}KB）"
    if removed:
        message += f"、古い{len(removed)}件を削除"
    return message


def main() -> int:
    return run_job(JOB_NAME, run)


if __name__ == "__main__":
    raise SystemExit(main())
