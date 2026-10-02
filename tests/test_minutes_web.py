"""議事録の画面のテスト. DBと議事録係はテスト用に差し替える."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from test_minutes_audio import make_wav
from test_minutes_runtime import SEG, FakeTranscriber, make_runtime, wait_until


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Iterator[tuple[TestClient, Path, object]]:
    from companion.web import app as web_app
    from companion.web import minutes_routes

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["よろしくお願いします"]))
    monkeypatch.setattr(minutes_routes, "session", lambda: session(db_path))
    monkeypatch.setattr(minutes_routes, "get_runtime", lambda: runtime)

    with TestClient(web_app.app) as client:
        yield client, db_path, runtime


def test_新しく録音すると会議の画面に移る(env) -> None:
    client, db_path, _ = env

    response = client.post("/minutes", data={"title": "  ゼミ（第3回）  "}, follow_redirects=False)

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "ゼミ（第3回）" in page.text
    assert "録音を始める" in page.text
    assert "/static/minutes/recorder.js" in page.text
    assert "iPhoneのノイズ除去を使う" in page.text


def test_一覧に会議が出る(env) -> None:
    client, _, _ = env
    client.post("/minutes", data={"title": "ゼミ"})

    response = client.get("/minutes")

    assert response.status_code == 200
    assert "録音中" in response.text


def test_切れ端を受け取ると保存して文字起こしする(env) -> None:
    client, db_path, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})

    response = client.post(f"/minutes/1/segments?seg={SEG}", content=make_wav(), headers={"Content-Type": "audio/wav"})

    assert response.json() == {"ok": True}
    assert runtime.audio.segment_path(1, SEG).exists()
    with session(db_path) as conn:
        wait_until(lambda: db.list_segments(conn, 1)[0].status == "done")


def test_形式の違う音声は400で断る(env) -> None:
    client, _, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})

    response = client.post(f"/minutes/1/segments?seg={SEG}", content=b"broken")

    assert response.status_code == 400
    assert not runtime.audio.segment_path(1, SEG).exists()


def test_おかしな切れ端の番号は断る(env) -> None:
    client, _, _ = env
    client.post("/minutes", data={"title": "ゼミ"})

    assert client.post("/minutes/1/segments?seg=12", content=make_wav()).status_code == 400


def test_無い会議には送れない(env) -> None:
    client, _, _ = env

    assert client.post(f"/minutes/99/segments?seg={SEG}", content=make_wav()).status_code == 404


def test_録音を終えると清書中になり完成すると議事録が出る(env) -> None:
    client, db_path, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})
    client.post(f"/minutes/1/segments?seg={SEG}", content=make_wav())

    assert client.post("/minutes/1/finish").json()["status"] == "finishing"
    with session(db_path) as conn:
        wait_until(lambda: db.get_meeting(conn, 1).status == "done")

    page = client.get("/minutes/1")
    assert "清書した" in page.text
    assert "清書をやり直す" in page.text
    audio = client.get("/minutes/1/audio")
    assert audio.status_code == 200
    assert audio.content[4:8] == b"ftyp"  # m4a


def test_題名の改行などは落とす(env) -> None:
    client, db_path, _ = env

    client.post("/minutes", data={"title": "ゼミ\r\n第3回"})

    with session(db_path) as conn:
        assert db.get_meeting(conn, 1).title == "ゼミ第3回"


def test_参加者を入れて録音を始め後から直せる(env) -> None:
    client, db_path, _ = env

    client.post("/minutes", data={"title": "ゼミ", "participants": " 自分, 田中  佐藤 "})
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1).participants == "自分、田中、佐藤"

    client.post("/minutes/1/participants", data={"participants": "自分、田中、佐藤、先生"})
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1).participants == "自分、田中、佐藤、先生"
    assert "参加者：自分、田中、佐藤、先生" in client.get("/minutes/1").text


def test_録音の長さを秒までわかる形にする() -> None:
    from companion.web.minutes_routes import duration_label

    assert duration_label(48_900) == "48秒"
    assert duration_label(725_000) == "12分05秒"
    assert duration_label(4_320_000) == "1時間12分"


def test_一覧には要点の最初の1行を出す() -> None:
    from datetime import datetime

    from companion.web.minutes_routes import summary_line

    base = dict(id=1, title="ゼミ", started_at=datetime(2026, 10, 2), status="done")
    polished = db.Meeting(**base, final_text="## 要点\n\n- 田中さんが報告した。\n- 次回は10月9日\n\n## 話し合ったこと\n- x")
    drafted = db.Meeting(**base, draft={"要点": "- 下書きの要点"})

    assert summary_line(polished) == "田中さんが報告した。"
    assert summary_line(drafted) == "下書きの要点"
    assert summary_line(db.Meeting(**base)) == ""


def test_一覧の各行に開くボタンがある(env) -> None:
    client, _, _ = env
    client.post("/minutes", data={"title": "ゼミ"})

    page = client.get("/minutes").text

    assert 'href="/minutes/1" aria-label="ゼミを開く"' in page
    assert "0秒" in page
