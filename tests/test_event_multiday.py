"""複数日にわたる予定のテスト.

合宿や旅行のように何日か続く予定は、登録できることだけでなく
「期間中の全ての日に出ること」まで揃わないと使い物にならない。
初日にしか出ないと、2日目にその日を開いたときに予定が無いように見える。
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from web_client import own_device_client

from companion.db.connection import apply_schema, connect, session
from companion.db.events import (
    list_events_between,
    list_events_for,
    prune_events,
    upsert_events,
)
from companion.integrations.calendar_google import CalendarEvent, _to_event
from companion.services import event_draft, month_view
from companion.services.event_draft import EventDraft

TODAY = date(2026, 8, 9)
CAMP_START = date(2026, 8, 20)
CAMP_END = date(2026, 8, 23)


def fake_claude(text: str):
    from companion.integrations.claude_runner import ClaudeResult

    return lambda prompt, **kwargs: ClaudeResult(text=text, session_id=None)


def camp_event(uid: str = "camp") -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title="ゼミ合宿",
        event_date=CAMP_START,
        starts_at=None,
        ends_at=None,
        location=None,
        is_all_day=True,
        end_date=CAMP_END,
    )


# --- 解釈（Claudeの応答から予定案を作る） ---


def test_複数日の予定を読み取れる(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "ゼミ合宿", "date": "2026-08-20",'
            ' "end_date": "2026-08-23", "start_time": null, "end_time": null,'
            ' "location": "山中湖"}'
        ),
    )

    # Act
    draft = event_draft.parse("8月20日から23日までゼミ合宿 山中湖", TODAY)

    # Assert
    assert draft is not None
    assert draft.day == CAMP_START
    assert draft.end_day == CAMP_END
    assert draft.spans_days is True


def test_最終日がなければ単日として扱う(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "面接", "date": "2026-08-20",'
            ' "end_date": null, "start_time": "10:00", "end_time": null, "location": null}'
        ),
    )

    # Act
    draft = event_draft.parse("8月20日10時から面接", TODAY)

    # Assert
    assert draft is not None
    assert draft.end_day is None
    assert draft.spans_days is False


def test_最終日が開始日より前なら捨てる() -> None:
    # Arrange: 読み違いをそのまま通すと、開始日より前に終わる予定ができてしまう
    payload = {
        "understood": True,
        "title": "合宿",
        "date": "2026-08-20",
        "end_date": "2026-08-18",
    }

    # Act
    draft = event_draft.build_draft(payload)

    # Assert: 単日に戻す。予定を落とすより害が小さい
    assert draft is not None
    assert draft.end_day is None
    assert draft.last_day == CAMP_START


def test_最終日が開始日と同じなら単日として扱う() -> None:
    payload = {
        "understood": True,
        "title": "合宿",
        "date": "2026-08-20",
        "end_date": "2026-08-20",
    }

    assert event_draft.build_draft(payload).end_day is None


def test_プロンプトで複数日の指定を求めている() -> None:
    # Arrange / Act / Assert: 単日しか読めない状態に戻っていないこと
    assert "end_date" in event_draft.PROMPT_TEMPLATE
    assert "複数日" in event_draft.PROMPT_TEMPLATE


# --- 確認画面のラベル ---


def test_確認画面に期間を出す() -> None:
    # Arrange
    draft = EventDraft(title="ゼミ合宿", day=CAMP_START, end_day=CAMP_END)

    # Act / Assert: 何日から何日までかが読めること
    assert draft.label() == "2026年08月20日 〜 2026年08月23日 終日"


def test_単日はこれまで通りの表示() -> None:
    draft = EventDraft(title="面接", day=CAMP_START)

    assert draft.label() == "2026年08月20日 終日"


def test_時刻つきで日をまたぐ予定の終了日時() -> None:
    # Arrange: 20日22時から21日6時まで
    draft = EventDraft(
        title="夜行バス",
        day=CAMP_START,
        end_day=date(2026, 8, 21),
        start_time=time(22, 0),
        end_time=time(6, 0),
    )

    # Act / Assert: 最終日が指定されているので、そちらの6時になる
    assert draft.ends_at().date() == date(2026, 8, 21)
    assert draft.ends_at().hour == 6


def test_最終日の指定がなくても深夜またぎは翌日になる() -> None:
    # Arrange: 「22時から1時」は翌1時のこと
    draft = EventDraft(
        title="作業",
        day=CAMP_START,
        start_time=time(22, 0),
        end_time=time(1, 0),
    )

    # Act / Assert
    assert draft.ends_at().date() == date(2026, 8, 21)


# --- Googleカレンダーとの変換 ---


def test_終日予定の終了日は1日戻して最終日にする() -> None:
    # Arrange: APIの終日予定は end が翌日を指す（排他的）
    raw = {
        "id": "abc",
        "summary": "ゼミ合宿",
        "start": {"date": "2026-08-20"},
        "end": {"date": "2026-08-24"},
    }

    # Act
    event = _to_event(raw)

    # Assert: 24日ではなく23日が最終日
    assert event is not None
    assert event.end_date == CAMP_END
    assert event.last_date == CAMP_END


def test_単日の終日予定は最終日を持たない() -> None:
    # Arrange
    raw = {
        "id": "abc",
        "summary": "面接",
        "start": {"date": "2026-08-20"},
        "end": {"date": "2026-08-21"},
    }

    # Act / Assert
    assert _to_event(raw).end_date is None


def test_時刻つきで日をまたぐ予定の最終日を拾う() -> None:
    # Arrange
    raw = {
        "id": "abc",
        "summary": "夜行バス",
        "start": {"dateTime": "2026-08-20T22:00:00+09:00"},
        "end": {"dateTime": "2026-08-21T06:00:00+09:00"},
    }

    # Act / Assert
    assert _to_event(raw).end_date == date(2026, 8, 21)


def test_複数日の予定にラベルで期間を出す() -> None:
    # Act / Assert
    assert camp_event().time_label() == "08/20〜08/23 終日"


# --- 保存と取り出し ---


def test_期間中の全ての日に予定が出る(conn) -> None:
    # Arrange
    upsert_events(conn, [camp_event()])

    # Act / Assert: 初日・中日・最終日のどれで開いても出ること
    for day in (CAMP_START, date(2026, 8, 21), date(2026, 8, 22), CAMP_END):
        assert [e.title for e in list_events_for(conn, day)] == ["ゼミ合宿"], day


def test_期間の外には出ない(conn) -> None:
    # Arrange
    upsert_events(conn, [camp_event()])

    # Act / Assert
    assert list_events_for(conn, CAMP_START - timedelta(days=1)) == []
    assert list_events_for(conn, CAMP_END + timedelta(days=1)) == []


def test_開始日が期間外でも重なっていれば拾う(conn) -> None:
    # Arrange: 月をまたぐ予定が、次の月の画面に出ないと困る
    upsert_events(conn, [camp_event()])

    # Act
    found = list_events_between(conn, date(2026, 8, 22), date(2026, 8, 31))

    # Assert
    assert [e.title for e in found] == ["ゼミ合宿"]


def test_期間に重なる予定は同期で消さない(conn) -> None:
    # Arrange: 開始日が同期範囲の外にあっても、まだ続いている予定を消さないこと
    upsert_events(conn, [camp_event()])

    # Act
    removed = prune_events(conn, date(2026, 8, 22), date(2026, 8, 31), {"camp"})

    # Assert
    assert removed == 0
    assert len(list_events_for(conn, date(2026, 8, 22))) == 1


def test_複数日の予定は月カレンダーの各日に数える(conn) -> None:
    # Arrange
    upsert_events(conn, [camp_event()])

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)
    counts = {
        cell.day: cell.event_count
        for week in view.weeks
        for cell in week
        if cell.day is not None
    }

    # Assert: 4日間すべてで1件と数える
    assert [counts[date(2026, 8, d)] for d in (20, 21, 22, 23)] == [1, 1, 1, 1]
    assert counts[date(2026, 8, 19)] == 0
    assert counts[date(2026, 8, 24)] == 0


# --- 既存DBの移行 ---


def test_end_date列がない既存DBにも列を足す(tmp_path) -> None:
    # Arrange: 移行前のスキーマでテーブルを作る
    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    conn.execute(
        """
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            event_date TEXT NOT NULL,
            starts_at TEXT,
            ends_at TEXT,
            title TEXT NOT NULL,
            location TEXT,
            is_all_day INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO calendar_events (uid, event_date, title) VALUES ('old', '2026-08-01', '古い予定')"
    )

    # Act
    apply_schema(conn)

    # Assert: 既存の行を残したまま列が増えること
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(calendar_events)")}
    assert "end_date" in columns
    assert list_events_for(conn, date(2026, 8, 1))[0].title == "古い予定"
    conn.close()


# --- 画面からの登録 ---


@pytest.fixture
def client(tmp_path, monkeypatch):
    """カレンダー登録を差し替えたテスト用クライアント."""
    from companion.web import app as web_app

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)

    created: list[EventDraft] = []
    monkeypatch.setattr(web_app, "session", lambda: session(db_path))
    monkeypatch.setattr(web_app, "today_local", lambda: TODAY)
    monkeypatch.setattr(web_app, "get_events_for", lambda day: [])
    monkeypatch.setattr(web_app, "register_event", lambda draft: created.append(draft) or "id1")

    with own_device_client(web_app.app) as test_client:
        yield test_client, created


def test_画面から最終日を指定して登録できる(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={"title": "ゼミ合宿", "day": "2026-08-20", "end_day": "2026-08-23"},
        follow_redirects=False,
    )

    # Assert
    assert created[0].end_day == CAMP_END
    assert created[0].spans_days is True


def test_最終日を空にすると単日になる(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={"title": "面接", "day": "2026-08-20", "end_day": ""},
        follow_redirects=False,
    )

    # Assert
    assert created[0].end_day is None


def test_画面で最終日を開始日より前にしても登録は通す(client) -> None:
    # Arrange: 弾いて入力し直させるより、単日として登録するほうが予定を落とさない
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={"title": "合宿", "day": "2026-08-20", "end_day": "2026-08-18"},
        follow_redirects=False,
    )

    # Assert
    assert created[0].end_day is None


def test_確認画面に最終日の入力欄がある(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    test_client, _ = client
    monkeypatch.setattr(
        web_app,
        "parse_event",
        lambda text, today: EventDraft(title="ゼミ合宿", day=CAMP_START, end_day=CAMP_END),
    )

    # Act
    response = test_client.post("/events/parse", data={"text": "8月20日から23日まで合宿"})

    # Assert
    assert 'name="end_day"' in response.text
    assert "2026-08-23" in response.text


def test_移行は何度実行しても安全(tmp_path) -> None:
    # Arrange
    db_path = tmp_path / "twice.db"
    with session(db_path) as conn:
        apply_schema(conn)

        # Act / Assert: 2回目で ALTER TABLE が失敗しないこと
        apply_schema(conn)
        upsert_events(conn, [camp_event()])
        assert len(list_events_for(conn, date(2026, 8, 21))) == 1
