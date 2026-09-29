"""要約が付いていない日記の作り直し."""

from __future__ import annotations

from datetime import date, timedelta

from companion.db.repository import save_raw_text
from companion.jobs import restructure

TODAY = date(2026, 8, 14)


def test_finds_only_entries_without_a_summary(conn):
    # Arrange
    save_raw_text(conn, TODAY, "今日の分")
    old = TODAY - timedelta(days=30)
    save_raw_text(conn, old, "古い分")

    # Act
    targets = restructure.find_unstructured(conn, TODAY)

    # Assert: 直近14日の外は対象にしない
    assert [entry.entry_date for entry in targets] == [TODAY]


def test_structures_every_pending_entry(conn, monkeypatch):
    # Arrange
    save_raw_text(conn, TODAY - timedelta(days=1), "昨日の分")
    save_raw_text(conn, TODAY, "今日の分")
    calls: list[date] = []

    def fake_structure(_conn, _entry_id, entry_date, _raw_text):
        calls.append(entry_date)
        return None

    monkeypatch.setattr(restructure, "structure_entry", fake_structure)
    monkeypatch.setattr(restructure, "session", _fixed_session(conn))
    monkeypatch.setattr(restructure, "today_local", lambda: TODAY)

    # Act
    message = restructure.run()

    # Assert: 古い順に処理する
    assert calls == [TODAY - timedelta(days=1), TODAY]
    assert "2件を要約" in message


def test_stops_at_the_first_failure(conn, monkeypatch):
    # Arrange: 認証切れなら後続も必ず失敗するので呼び続けない
    save_raw_text(conn, TODAY - timedelta(days=1), "昨日の分")
    save_raw_text(conn, TODAY, "今日の分")
    calls: list[date] = []

    def failing_structure(_conn, _entry_id, entry_date, _raw_text):
        calls.append(entry_date)
        return "Claudeの認証が切れています。"

    monkeypatch.setattr(restructure, "structure_entry", failing_structure)
    monkeypatch.setattr(restructure, "session", _fixed_session(conn))
    monkeypatch.setattr(restructure, "today_local", lambda: TODAY)

    # Act
    message = restructure.run()

    # Assert
    assert len(calls) == 1
    assert "2件は失敗" in message
    assert "認証が切れています" in message


def test_says_nothing_to_do_when_all_entries_have_summaries(conn, monkeypatch):
    # Arrange: 日記が1件も無い状態
    monkeypatch.setattr(restructure, "session", _fixed_session(conn))
    monkeypatch.setattr(restructure, "today_local", lambda: TODAY)

    # Act
    message = restructure.run()

    # Assert
    assert message == "要約待ちの日記はありません"


def _fixed_session(conn):
    """テスト用DBを返す session() の代役."""
    from contextlib import contextmanager

    @contextmanager
    def _session():
        yield conn

    return _session
