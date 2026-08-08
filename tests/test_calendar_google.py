"""Googleカレンダーの時刻解釈のテスト.

APIが返す時刻はカレンダー側のタイムゾーン設定に左右されるため、
どのオフセットで来ても日本時間に揃うことを保証する。
実カレンダーが空でも検証できるよう、APIのレスポンス形式を直接与える。
"""

from __future__ import annotations

from datetime import date

from companion.integrations.calendar_google import CalendarEvent, _parse_endpoint, _to_event


def test_utcの時刻が日本時間に変換される() -> None:
    # Arrange: カレンダーがUTC設定のときAPIが返す形式（日本時間の10:00）
    raw = {"dateTime": "2026-08-09T01:00:00Z"}

    # Act
    moment, day, is_all_day = _parse_endpoint(raw)

    # Assert
    assert is_all_day is False
    assert moment is not None
    assert (moment.hour, moment.minute) == (10, 0)
    assert day == date(2026, 8, 9)


def test_日本時間の時刻はそのまま保たれる() -> None:
    # Arrange: カレンダーがAsia/Tokyo設定のときAPIが返す形式
    raw = {"dateTime": "2026-08-09T10:00:00+09:00"}

    # Act
    moment, day, _ = _parse_endpoint(raw)

    # Assert
    assert moment is not None
    assert (moment.hour, moment.minute) == (10, 0)
    assert day == date(2026, 8, 9)


def test_日付をまたぐ変換で日付も繰り上がる() -> None:
    # Arrange: UTCの2026-08-08T16:00Z は日本時間で翌日の01:00
    raw = {"dateTime": "2026-08-08T16:00:00Z"}

    # Act
    moment, day, _ = _parse_endpoint(raw)

    # Assert
    assert moment is not None
    assert moment.hour == 1
    assert day == date(2026, 8, 9)


def test_終日予定は時刻を持たない() -> None:
    # Arrange
    raw = {"date": "2026-08-09"}

    # Act
    moment, day, is_all_day = _parse_endpoint(raw)

    # Assert
    assert is_all_day is True
    assert moment is None
    assert day == date(2026, 8, 9)


def test_apiのレスポンスから予定を組み立てる() -> None:
    # Arrange
    raw = {
        "id": "abc123",
        "summary": "ゼミ",
        "location": "3号館",
        "start": {"dateTime": "2026-08-09T01:00:00Z"},
        "end": {"dateTime": "2026-08-09T02:30:00Z"},
    }

    # Act
    event = _to_event(raw)

    # Assert
    assert event is not None
    assert event.uid == "abc123"
    assert event.title == "ゼミ"
    assert event.location == "3号館"
    assert event.is_all_day is False
    assert event.time_label() == "10:00-11:30"


def test_開始日時のない予定は取り込まない() -> None:
    # Arrange: 予定が削除されるなどして start が欠けたケース
    raw = {"id": "broken", "summary": "壊れた予定", "start": {}}

    # Act / Assert
    assert _to_event(raw) is None


def test_件名のない予定にも既定の名前がつく() -> None:
    # Arrange
    raw = {"id": "x", "start": {"date": "2026-08-09"}, "end": {"date": "2026-08-10"}}

    # Act
    event = _to_event(raw)

    # Assert
    assert event is not None
    assert event.title == "(件名なし)"
    assert event.time_label() == "終日"


def test_終了時刻のない予定は開始時刻だけ表示する() -> None:
    # Arrange
    event = CalendarEvent(
        uid="x",
        title="打ち合わせ",
        event_date=date(2026, 8, 9),
        starts_at=None,
        ends_at=None,
        location=None,
        is_all_day=True,
    )

    # Act / Assert
    assert event.time_label() == "終日"
