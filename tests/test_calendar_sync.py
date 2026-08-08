"""予定の同期とキャッシュのテスト.

Google APIの一時的な失敗でキャッシュを壊さないことを重点的に確認する。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from companion.db.events import list_events_for, prune_events, upsert_events
from companion.integrations.calendar_google import CalendarEvent
from companion.services import calendar_sync

TODAY = date(2026, 8, 8)
JST = "+09:00"


def make_event(uid: str, title: str, day: date = TODAY, hour: int = 10) -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title=title,
        event_date=day,
        starts_at=datetime.fromisoformat(f"{day.isoformat()}T{hour:02d}:00:00{JST}"),
        ends_at=datetime.fromisoformat(f"{day.isoformat()}T{hour + 1:02d}:00:00{JST}"),
        location=None,
        is_all_day=False,
    )


def test_予定を保存して取り出せる(conn) -> None:
    # Arrange / Act
    upsert_events(conn, [make_event("a", "ゼミ")])

    # Assert
    events = list_events_for(conn, TODAY)
    assert [e.title for e in events] == ["ゼミ"]
    assert events[0].time_label() == "10:00-11:00"


def test_同じUIDは重複せず更新される(conn) -> None:
    # Arrange
    upsert_events(conn, [make_event("a", "ゼミ")])

    # Act: Google側で件名が変わったケース
    upsert_events(conn, [make_event("a", "ゼミ（教室変更）")])

    # Assert
    events = list_events_for(conn, TODAY)
    assert len(events) == 1
    assert events[0].title == "ゼミ（教室変更）"


def test_終日予定が先に並ぶ(conn) -> None:
    # Arrange
    all_day = CalendarEvent(
        uid="b", title="合宿", event_date=TODAY, starts_at=None, ends_at=None,
        location=None, is_all_day=True,
    )
    upsert_events(conn, [make_event("a", "ゼミ"), all_day])

    # Act
    events = list_events_for(conn, TODAY)

    # Assert
    assert [e.title for e in events] == ["合宿", "ゼミ"]


def test_Google側で消えた予定はキャッシュからも消える(conn) -> None:
    # Arrange
    upsert_events(conn, [make_event("a", "ゼミ"), make_event("b", "バイト", hour=14)])

    # Act: 次の同期で b が返ってこなかった
    removed = prune_events(conn, TODAY, TODAY, {"a"})

    # Assert
    assert removed == 1
    assert [e.title for e in list_events_for(conn, TODAY)] == ["ゼミ"]


def test_期間外の予定は削除対象にしない(conn) -> None:
    # Arrange
    upsert_events(conn, [make_event("old", "先月の予定", day=date(2026, 7, 1))])

    # Act: 今日だけを突き合わせる
    removed = prune_events(conn, TODAY, TODAY, set())

    # Assert
    assert removed == 0
    assert len(list_events_for(conn, date(2026, 7, 1))) == 1


def test_同期に失敗してもキャッシュを消さない(conn, monkeypatch) -> None:
    # Arrange: 先に取れていた予定がある状態でAPIが落ちる
    upsert_events(conn, [make_event("a", "ゼミ")])

    def boom(start, end):
        raise RuntimeError("API down")

    monkeypatch.setattr(calendar_sync, "fetch_events", boom)

    # Act
    result = calendar_sync.sync_window(conn, TODAY)

    # Assert: 失敗を報告しつつ、予定は残っている
    assert result.failed is True
    assert "失敗" in result.describe()
    assert len(list_events_for(conn, TODAY)) == 1


def test_同期に成功したら件数を報告する(conn, monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        calendar_sync, "fetch_events", lambda start, end: [make_event("a", "ゼミ")]
    )

    # Act
    result = calendar_sync.sync_window(conn, TODAY)

    # Assert
    assert result.failed is False
    assert result.fetched == 1
    assert "予定1件を同期" in result.describe()


def test_同期範囲は前後7日をカバーする(conn, monkeypatch) -> None:
    # Arrange
    captured: dict[str, date] = {}

    def capture(start, end):
        captured["start"] = start
        captured["end"] = end
        return []

    monkeypatch.setattr(calendar_sync, "fetch_events", capture)

    # Act
    calendar_sync.sync_window(conn, TODAY)

    # Assert
    assert captured["start"] == TODAY - timedelta(days=7)
    assert captured["end"] == TODAY + timedelta(days=7)
