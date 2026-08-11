"""繰り返し予定のテスト.

繰り返しは間違えたときの被害が非対称である。
「1回きりのつもりが毎週になった」はカレンダーが埋まって消すのが大変で、
「繰り返しのつもりが1回だけになった」は予定を落とす。
どちらにも倒れないよう、読み取れないときは1回きりに寄せることを確認する。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from companion.db.connection import apply_schema, session
from companion.integrations.claude_runner import ClaudeResult
from companion.services import event_draft
from companion.services.event_draft import EventDraft, Recurrence, build_recurrence

TODAY = date(2026, 8, 9)
# 2026-08-16 は日曜日
SUNDAY = date(2026, 8, 16)


def fake_claude(text: str):
    return lambda prompt, **kwargs: ClaudeResult(text=text, session_id=None)


# --- RRULE の組み立て ---


def test_毎週日曜のRRULEを組み立てる() -> None:
    # Arrange
    repeat = Recurrence(freq="WEEKLY", weekdays=("SU",))

    # Act / Assert
    assert repeat.to_rrule(SUNDAY) == "RRULE:FREQ=WEEKLY;BYDAY=SU"


def test_曜日の指定がなければ開始日の曜日を使う() -> None:
    # Arrange: 「毎週ゼミ」のように曜日が書かれていない場合
    repeat = Recurrence(freq="WEEKLY")

    # Act / Assert: 開始日が日曜なので日曜になる
    assert repeat.to_rrule(SUNDAY) == "RRULE:FREQ=WEEKLY;BYDAY=SU"


def test_隔週は間隔を入れる() -> None:
    repeat = Recurrence(freq="WEEKLY", interval=2, weekdays=("TU",))

    assert repeat.to_rrule(SUNDAY) == "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"


def test_間隔が1なら省略する() -> None:
    # Arrange / Act / Assert: INTERVAL=1 は既定値なので付けない
    assert "INTERVAL" not in Recurrence(freq="DAILY").to_rrule(SUNDAY)


def test_期限を指定できる() -> None:
    repeat = Recurrence(freq="MONTHLY", until=date(2026, 12, 31))

    assert repeat.to_rrule(SUNDAY) == "RRULE:FREQ=MONTHLY;UNTIL=20261231T235959Z"


def test_毎日と毎月には曜日を入れない() -> None:
    # Arrange / Act / Assert: BYDAY は毎週のときだけ意味がある
    assert "BYDAY" not in Recurrence(freq="DAILY", weekdays=("SU",)).to_rrule(SUNDAY)
    assert "BYDAY" not in Recurrence(freq="MONTHLY", weekdays=("SU",)).to_rrule(SUNDAY)


def test_複数の曜日を指定できる() -> None:
    repeat = Recurrence(freq="WEEKLY", weekdays=("MO", "WE", "FR"))

    assert repeat.to_rrule(SUNDAY) == "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"


# --- 確認画面の表示 ---


def test_繰り返しの説明を出す() -> None:
    assert Recurrence(freq="WEEKLY", weekdays=("SU",)).label(SUNDAY) == "毎週日曜"
    assert Recurrence(freq="DAILY").label(SUNDAY) == "毎日"
    assert Recurrence(freq="MONTHLY").label(SUNDAY) == "毎月"


def test_間隔と期限つきの説明を出す() -> None:
    repeat = Recurrence(freq="WEEKLY", interval=2, weekdays=("TU",), until=date(2026, 12, 31))

    assert repeat.label(SUNDAY) == "2週ごと火曜（2026年12月31日まで）"


# --- Claudeの応答の解釈 ---


def test_毎週日曜の予定を読み取れる(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "ゼミ", "date": "2026-08-16",'
            ' "end_date": null, "start_time": "10:00", "end_time": "12:00", "location": null,'
            ' "repeat": {"freq": "weekly", "interval": 1, "weekdays": ["sun"], "until": null}}'
        ),
    )

    # Act
    draft = event_draft.parse("毎週日曜10時からゼミ", TODAY)

    # Assert
    assert draft is not None
    assert draft.repeats is True
    assert draft.recurrence_rules() == ["RRULE:FREQ=WEEKLY;BYDAY=SU"]


def test_繰り返しでなければNoneになる(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(
        event_draft,
        "run_claude",
        fake_claude(
            '{"understood": true, "title": "面接", "date": "2026-08-16",'
            ' "start_time": "10:00", "repeat": null}'
        ),
    )

    # Act
    draft = event_draft.parse("8月16日10時から面接", TODAY)

    # Assert
    assert draft.repeats is False
    assert draft.recurrence_rules() is None


def test_repeatの項目がなくても落ちない() -> None:
    # Arrange: 古い形の応答が来ても1回きりとして扱う
    draft = event_draft.build_draft(
        {"understood": True, "title": "面接", "date": "2026-08-16"}
    )

    # Assert
    assert draft is not None
    assert draft.repeats is False


def test_知らない繰り返し方は1回きりにする() -> None:
    # Arrange: 「毎年」は未対応。勝手に近い規則に読み替えない
    assert build_recurrence({"freq": "yearly"}) is None
    assert build_recurrence({"freq": ""}) is None
    assert build_recurrence("weekly") is None
    assert build_recurrence(None) is None


def test_曜日の表記ゆれを吸収する() -> None:
    # Arrange / Act
    repeat = build_recurrence({"freq": "weekly", "weekdays": ["sun", "MONDAY", "TU"]})

    # Assert
    assert repeat.weekdays == ("SU", "MO", "TU")


def test_読めない曜日は捨てる() -> None:
    repeat = build_recurrence({"freq": "weekly", "weekdays": ["sun", "にちようび"]})

    assert repeat.weekdays == ("SU",)


def test_同じ曜日が重複しても1つにする() -> None:
    repeat = build_recurrence({"freq": "weekly", "weekdays": ["sun", "SU", "sunday"]})

    assert repeat.weekdays == ("SU",)


def test_間隔が異常でも上限で止める() -> None:
    # Arrange: 読み違いで「毎1000週」が来ても、そのまま送らない
    assert build_recurrence({"freq": "weekly", "interval": 1000}).interval == event_draft.MAX_INTERVAL
    assert build_recurrence({"freq": "weekly", "interval": 0}).interval == 1
    assert build_recurrence({"freq": "weekly", "interval": "毎週"}).interval == 1


def test_期限が開始日より前なら繰り返しを捨てる() -> None:
    # Arrange: 1回も発生しない予定になってしまう
    payload = {
        "understood": True,
        "title": "ゼミ",
        "date": "2026-08-16",
        "repeat": {"freq": "weekly", "until": "2026-08-01"},
    }

    # Act
    draft = event_draft.build_draft(payload)

    # Assert: 繰り返しは捨てるが、予定そのものは残す
    assert draft is not None
    assert draft.repeats is False
    assert draft.day == SUNDAY


def test_プロンプトで繰り返しを求めている() -> None:
    # Arrange / Act / Assert: 黙って1回分だけ登録される状態に戻っていないこと
    assert "repeat" in event_draft.PROMPT_TEMPLATE
    assert "毎週日曜" in event_draft.PROMPT_TEMPLATE
    # 書かれていないのに繰り返しにされるのが一番困るので、その歯止めがあること
    assert "繰り返しかどうか書かれていない場合は repeat を null" in event_draft.PROMPT_TEMPLATE


# --- 登録 ---


def test_登録時にRRULEを渡す(monkeypatch) -> None:
    # Arrange
    from companion.services import event_register

    captured: dict = {}

    def capture(calendar_id, title, starts_at, ends_at, **kwargs):
        captured.update(kwargs)
        return "id1"

    monkeypatch.setattr(event_register, "default_calendar_id", lambda: "cal")
    monkeypatch.setattr(event_register, "create_event", capture)
    monkeypatch.setattr(event_register, "_resync", lambda draft: None)

    draft = EventDraft(
        title="ゼミ",
        day=SUNDAY,
        start_time=time(10, 0),
        repeat=Recurrence(freq="WEEKLY", weekdays=("SU",)),
    )

    # Act
    event_register.register(draft)

    # Assert
    assert captured["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=SU"]


def test_繰り返し予定は登録後に取り直す(monkeypatch) -> None:
    # Arrange: 繰り返しはGoogle側で別IDの個別予定に展開されるため、
    # 作成時に返る大元のIDを控えても1回目しか画面に出ない
    from companion.services import event_register

    resynced: list = []
    cached: list = []
    monkeypatch.setattr(event_register, "default_calendar_id", lambda: "cal")
    monkeypatch.setattr(event_register, "create_all_day_event", lambda *a, **k: "id1")
    monkeypatch.setattr(event_register, "_resync", lambda draft: resynced.append(draft))
    monkeypatch.setattr(event_register, "_cache_locally", lambda draft, eid: cached.append(draft))

    # Act
    event_register.register(
        EventDraft(title="ゼミ", day=SUNDAY, repeat=Recurrence(freq="WEEKLY"))
    )

    # Assert
    assert len(resynced) == 1
    assert cached == []


def test_1回きりの予定はこれまで通り控える(monkeypatch) -> None:
    # Arrange
    from companion.services import event_register

    resynced: list = []
    cached: list = []
    monkeypatch.setattr(event_register, "default_calendar_id", lambda: "cal")
    monkeypatch.setattr(event_register, "create_all_day_event", lambda *a, **k: "id1")
    monkeypatch.setattr(event_register, "_resync", lambda draft: resynced.append(draft))
    monkeypatch.setattr(event_register, "_cache_locally", lambda draft, eid: cached.append(draft))

    # Act
    event_register.register(EventDraft(title="面接", day=SUNDAY))

    # Assert
    assert resynced == []
    assert len(cached) == 1


# --- 画面からの登録 ---


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

    with TestClient(web_app.app) as test_client:
        yield test_client, created


def test_画面から毎週の繰り返しを登録できる(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={
            "title": "ゼミ",
            "day": "2026-08-16",
            "repeat_freq": "WEEKLY",
            "repeat_interval": "1",
            "repeat_weekdays": ["SU"],
        },
        follow_redirects=False,
    )

    # Assert
    assert created[0].recurrence_rules() == ["RRULE:FREQ=WEEKLY;BYDAY=SU"]


def test_繰り返しなしを選べば1回きりになる(client) -> None:
    # Arrange
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={"title": "面接", "day": "2026-08-16", "repeat_freq": ""},
        follow_redirects=False,
    )

    # Assert
    assert created[0].repeats is False


def test_画面で期限を開始日より前にしても登録は通す(client) -> None:
    # Arrange: 弾いて入力し直させるより、無期限の繰り返しにするほうが予定を落とさない
    test_client, created = client

    # Act
    test_client.post(
        "/events/create",
        data={
            "title": "ゼミ",
            "day": "2026-08-16",
            "repeat_freq": "WEEKLY",
            "repeat_until": "2026-08-01",
        },
        follow_redirects=False,
    )

    # Assert
    assert created[0].repeats is True
    assert created[0].repeat.until is None


def test_確認画面に繰り返しの入力欄がある(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    test_client, _ = client
    monkeypatch.setattr(
        web_app,
        "parse_event",
        lambda text, today: EventDraft(
            title="ゼミ", day=SUNDAY, repeat=Recurrence(freq="WEEKLY", weekdays=("SU",))
        ),
    )

    # Act
    response = test_client.post("/events/parse", data={"text": "毎週日曜にゼミ"})

    # Assert
    assert 'name="repeat_freq"' in response.text
    assert "毎週日曜" in response.text
