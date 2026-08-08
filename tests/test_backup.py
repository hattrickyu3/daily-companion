"""バックアップのテスト.

WALモードで書き込み中でも、取りこぼしのない複製ができることを確認する。
単なるファイルコピーではこれが保証できない。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from companion.db.connection import apply_schema, session
from companion.db.repository import get_entry, save_raw_text
from companion.jobs import backup

MOMENT = datetime(2026, 8, 8, 7, 30)


@pytest.fixture
def db_with_entry(tmp_path) -> Path:
    """日記が1件入ったDB."""
    db_path = tmp_path / "companion.db"
    with session(db_path) as conn:
        apply_schema(conn)
        save_raw_text(conn, date(2026, 8, 8), "今日は実装した")
    return db_path


def read_entry(db_path: Path, day: date):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return get_entry(connection, day)
    finally:
        connection.close()


def test_バックアップに日記が含まれる(db_with_entry, tmp_path) -> None:
    # Arrange
    backup_dir = tmp_path / "backups"

    # Act
    target = backup.create_backup(db_with_entry, backup_dir, MOMENT)

    # Assert: 複製からも同じ日記が読める
    entry = read_entry(target, date(2026, 8, 8))
    assert entry is not None
    assert entry.raw_text == "今日は実装した"


def test_未コミットの書き込みも取りこぼさない(db_with_entry, tmp_path) -> None:
    # Arrange: WAL に書き込んだ直後の状態で複製する
    backup_dir = tmp_path / "backups"
    with session(db_with_entry) as conn:
        save_raw_text(conn, date(2026, 8, 9), "翌日の分")

    # Act
    target = backup.create_backup(db_with_entry, backup_dir, MOMENT)

    # Assert
    entry = read_entry(target, date(2026, 8, 9))
    assert entry is not None
    assert entry.raw_text == "翌日の分"


def test_ファイル名は日付になる(db_with_entry, tmp_path) -> None:
    # Arrange / Act
    target = backup.create_backup(db_with_entry, tmp_path / "backups", MOMENT)

    # Assert
    assert target.name == "companion-20260808.db"


def test_同じ日に2回取っても増えない(db_with_entry, tmp_path) -> None:
    # Arrange
    backup_dir = tmp_path / "backups"

    # Act
    backup.create_backup(db_with_entry, backup_dir, MOMENT)
    backup.create_backup(db_with_entry, backup_dir, MOMENT)

    # Assert
    assert len(backup.list_backups(backup_dir)) == 1


def test_世代数を超えた古いものを消す(tmp_path) -> None:
    # Arrange: 10日分のバックアップがある状態
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in range(1, 11):
        (backup_dir / f"companion-202608{day:02d}.db").write_bytes(b"x")

    # Act
    removed = backup.prune_backups(backup_dir, generations=7)

    # Assert: 新しい7件だけが残る
    remaining = [path.name for path in backup.list_backups(backup_dir)]
    assert len(removed) == 3
    assert len(remaining) == 7
    assert remaining[0] == "companion-20260810.db"
    assert "companion-20260801.db" not in remaining


def test_世代数以内なら何も消さない(tmp_path) -> None:
    # Arrange
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "companion-20260808.db").write_bytes(b"x")

    # Act / Assert
    assert backup.prune_backups(backup_dir, generations=7) == []


def test_バックアップ元がなければエラーにする(tmp_path) -> None:
    # Arrange / Act / Assert: 空のファイルを作って成功したことにしない
    with pytest.raises(FileNotFoundError):
        backup.create_backup(tmp_path / "missing.db", tmp_path / "backups", MOMENT)


def test_バックアップが落ちても朝の通知は止めない(monkeypatch) -> None:
    # Arrange
    from companion.jobs import morning_brief

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(morning_brief, "backup_run", boom)

    # Act
    note = morning_brief.backup_safely()

    # Assert
    assert "失敗" in note


def test_保存先がなければ作る(db_with_entry, tmp_path) -> None:
    # Arrange: backups ディレクトリが存在しない状態
    backup_dir = tmp_path / "not-yet" / "backups"

    # Act
    target = backup.create_backup(db_with_entry, backup_dir, MOMENT)

    # Assert
    assert target.exists()
