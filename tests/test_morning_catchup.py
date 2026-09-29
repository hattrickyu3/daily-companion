"""朝のブリーフィングの追いかけ実行の判定."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from companion.db.briefings import save_briefing
from companion.jobs.morning_catchup import should_catchup

JST = ZoneInfo("Asia/Tokyo")
TODAY = date(2026, 8, 12)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute, tzinfo=JST)


def test_runs_when_todays_briefing_is_missing(conn):
    # Arrange: 今日のブリーフィングは未送信

    # Act
    should_run, _ = should_catchup(conn, at(8))

    # Assert
    assert should_run


def test_skips_before_the_scheduled_time(conn):
    # Arrange: 早朝の再起動（7:30より前）

    # Act
    should_run, reason = should_catchup(conn, at(3, 40))

    # Assert
    assert not should_run
    assert "07:30" in reason


def test_skips_when_briefing_was_already_sent(conn):
    # Arrange
    save_briefing(conn, TODAY, "おはようございます")

    # Act
    should_run, reason = should_catchup(conn, at(8))

    # Assert
    assert not should_run
    assert "送信済み" in reason


def test_skips_after_the_catchup_deadline(conn):
    # Arrange: 夕方に起動しても朝の通知は今さら鳴らさない

    # Act
    should_run, reason = should_catchup(conn, at(20))

    # Assert
    assert not should_run
    assert "期限" in reason


def test_runs_at_the_scheduled_time_itself(conn):
    # Arrange: 境界（7:30ちょうど）は実行する側に倒す

    # Act
    should_run, _ = should_catchup(conn, at(7, 30))

    # Assert
    assert should_run
