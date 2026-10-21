"""予定の解釈と登録のテスト.

カレンダーへの書き込みは取り消しにくく、日付の読み違いは気づきにくい。
確認画面を必ず挟むこと、解釈できないときに勝手に登録しないことを重点的に確認する。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, time

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

from companion.db.connection import apply_schema, session
from companion.integrations.claude_runner import ClaudeError, ClaudeResult
from companion.services import event_draft
from companion.services.event_draft import EventDraft

TODAY = date(2026, 8, 9)


def fake_claude(text: str):
    return lambda prompt, **kwargs: ClaudeResult(text=text, session_id=None)


def test_時刻つきの予定を読み取れる(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "ゼミ", "date": "2026-08-10",'
            ' "start_time": "10:00", "end_time": "12:00", "location": "3号館"}'
        ),
    )

    # Act
    draft = event_draft.parse("明日10時から12時までゼミ 3号館", TODAY)

    # Assert
    assert draft is not None
    assert draft.title == "ゼミ"
    assert draft.day == date(2026, 8, 10)
    assert draft.is_all_day is False
    assert draft.label() == "2026年08月10日 10:00-12:00"


def test_時刻がなければ終日になる(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "合宿", "date": "2026-08-20",'
            ' "start_time": null, "end_time": null, "location": null}'
        ),
    )

    # Act
    draft = event_draft.parse("8/20 合宿", TODAY)

    # Assert
    assert draft is not None
    assert draft.is_all_day is True
    assert draft.label() == "2026年08月20日 終日"


def test_終了時刻がなければ1時間にする() -> None:
    # Arrange
    draft = EventDraft(title="面接", day=TODAY, start_time=time(15, 0))

    # Act / Assert
    assert draft.ends_at().hour == 16


def test_日をまたぐ時間帯を拾える() -> None:
    # Arrange: 「22時から翌1時」を、終了が開始より前と誤解しない
    draft = EventDraft(title="作業", day=TODAY, start_time=time(22, 0), end_time=time(1, 0))

    # Act / Assert
    assert draft.ends_at().date() == date(2026, 8, 10)


def test_日付が読み取れなければ登録案を作らない(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft, "run_claude", fake_claude('{"understood": false, "title": "ゼミ"}')
    )

    # Act / Assert: 推測で日付を埋めない
    assert event_draft.parse("そのうちゼミ", TODAY) is None


def test_日付が壊れていれば登録案を作らない() -> None:
    payload = {"understood": True, "title": "ゼミ", "date": "明日"}

    assert event_draft.build_draft(payload) is None


def test_件名がなければ登録案を作らない() -> None:
    payload = {"understood": True, "title": "  ", "date": "2026-08-10"}

    assert event_draft.build_draft(payload) is None


def test_claudeが落ちても例外を投げない(monkeypatch) -> None:
    # Arrange
    def boom(prompt, **kwargs):
        raise ClaudeError("claude down")

    monkeypatch.setattr(event_draft, "run_claude", boom)

    # Act / Assert
    assert event_draft.parse("明日10時にゼミ", TODAY) is None


def test_今日の日付を基準として渡す(monkeypatch) -> None:
    # Arrange: 「明日」を計算させるのに必要
    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text='{"understood": false}', session_id=None)

    monkeypatch.setattr(event_draft, "run_claude", capture)

    # Act
    event_draft.parse("明日10時", TODAY)

    # Assert
    assert "2026-08-09" in captured["prompt"]
    assert "日曜日" in captured["prompt"]


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, list]]:
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


def test_確認画面では登録しない(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    monkeypatch.setattr(
        web_app,
        "parse_event",
        lambda text, today: EventDraft(title="ゼミ", day=date(2026, 8, 10), start_time=time(10, 0)),
    )
    test_client, created = client

    # Act
    response = test_client.post("/events/parse", data={"text": "明日10時にゼミ"})

    # Assert: 内容は出るが、まだカレンダーには入れない
    assert "ゼミ" in response.text
    assert "この内容で登録しますか" in response.text
    assert created == []


def test_確認後に登録される(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={
            "title": "ゼミ",
            "day": "2026-08-10",
            "start_time": "10:00",
            "end_time": "12:00",
            "location": "3号館",
        },
    )

    # Assert
    assert len(created) == 1
    assert created[0].title == "ゼミ"
    assert created[0].start_time == time(10, 0)
    assert created[0].location == "3号館"


def test_確認画面で内容を直せる(client) -> None:
    # Arrange: Claudeが読み違えても、送信されるのはフォームの値
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={"title": "面接に変更", "day": "2026-08-15", "start_time": "", "end_time": ""},
    )

    # Assert
    assert created[0].title == "面接に変更"
    assert created[0].day == date(2026, 8, 15)
    assert created[0].is_all_day is True


def test_読み取れなければ案内を出す(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    monkeypatch.setattr(web_app, "parse_event", lambda text, today: None)
    test_client, created = client

    # Act
    response = test_client.post("/events/parse", data={"text": "そのうちゼミ"})

    # Assert
    assert "読み取れませんでした" in response.text
    assert created == []


def test_件名が空なら登録しない(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    response = test_client.post("/events/create", data={"title": "  ", "day": "2026-08-10"})

    # Assert
    assert created == []
    assert response.url.path == "/events/new"


def test_日付が壊れていれば登録しない(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post("/events/create", data={"title": "ゼミ", "day": "not-a-date"})

    # Assert
    assert created == []


def test_カレンダー登録に失敗したら知らせる(client, monkeypatch) -> None:
    # Arrange
    from companion.integrations.calendar_google import CalendarError
    from companion.web import app as web_app

    def boom(draft):
        raise CalendarError("権限がありません")

    monkeypatch.setattr(web_app, "register_event", boom)
    test_client, _ = client

    # Act
    response = test_client.post(
        "/events/create", data={"title": "ゼミ", "day": "2026-08-10", "start_time": "10:00"}
    )

    # Assert: 落ちずにエラー表示へ誘導する
    assert response.status_code == 200
    assert "登録に失敗しました" in response.text


def test_入力画面が開ける(client) -> None:
    test_client, _ = client

    response = test_client.get("/events/new")

    assert response.status_code == 200
    assert "予定を追加" in response.text
