"""録音アプリ（Recorder Plus）からWebDAVで届く録音の受け口のテスト.

届いた録音は受け箱に置き、音声なら自動で議事録にする。同じ録音が送り直されても議事録は1つだけ。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from test_minutes_import import make_m4a
from test_minutes_runtime import TZ, FakeTranscriber, make_runtime, wait_until

USER, PASSWORD = "recorder", "s3cret-pass"


def auth(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Iterator[tuple[TestClient, Path, object, Path, list]]:
    from companion.web import app as web_app
    from companion.web import minutes_routes, recording_intake, webdav_routes

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)
    notified: list[tuple[int, str]] = []
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber([]))
    runtime.set_notifier(lambda meeting_id, status, error: notified.append((meeting_id, status)))
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(minutes_routes, "session", lambda: session(db_path))
    monkeypatch.setattr(minutes_routes, "get_runtime", lambda: runtime)
    monkeypatch.setattr(recording_intake, "session", lambda: session(db_path))
    monkeypatch.setattr(
        webdav_routes, "get_settings", lambda: webdav_routes.WebdavSettings(USER, PASSWORD, inbox)
    )
    monkeypatch.setattr(webdav_routes, "_scanned", True)

    with TestClient(web_app.app) as client:
        yield client, db_path, runtime, inbox, notified


def meetings(db_path: Path) -> list[db.Meeting]:
    with session(db_path) as conn:
        return db.list_meetings(conn)


def test_パスワードが無ければ断りBasic認証を求める(env) -> None:
    client, *_ = env

    response = client.request("PROPFIND", "/webdav/", headers={"Depth": "0"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert client.request("PROPFIND", "/webdav/", headers={"Depth": "0", **auth(password="違う")}).status_code == 401


def test_設定が無ければ受け口は無いことにする(env, monkeypatch) -> None:
    from companion.web import webdav_routes

    client, *_ = env
    monkeypatch.setattr(webdav_routes, "get_settings", lambda: None)

    assert client.request("PROPFIND", "/webdav/", headers=auth()).status_code == 404


def test_接続テストに答える(env) -> None:
    client, *_ = env

    options = client.request("OPTIONS", "/webdav/", headers=auth())
    root = client.request("PROPFIND", "/webdav/", headers={"Depth": "0", **auth()})

    assert options.status_code == 200
    assert "1" in options.headers["DAV"]
    assert "PROPFIND" in options.headers["Allow"]
    assert root.status_code == 207
    assert "<D:collection/>" in root.text


def test_フォルダを作って中身を一覧できる(env) -> None:
    client, _, _, inbox, _ = env

    assert client.request("MKCOL", "/webdav/RecorderPlus", headers=auth()).status_code == 201
    assert client.request("MKCOL", "/webdav/RecorderPlus", headers=auth()).status_code == 405
    client.put("/webdav/RecorderPlus/メモ.txt", content=b"hello", headers=auth())
    listing = client.request("PROPFIND", "/webdav/RecorderPlus/", headers={"Depth": "1", **auth()})

    assert (inbox / "RecorderPlus").is_dir()
    assert listing.status_code == 207
    assert "/webdav/RecorderPlus/%E3%83%A1%E3%83%A2.txt" in listing.text
    assert "<D:getcontentlength>5</D:getcontentlength>" in listing.text
    assert client.request("PROPFIND", "/webdav/無い.m4a", headers={"Depth": "0", **auth()}).status_code == 404


def test_録音が届くと自動で議事録にして通知する(env, tmp_path: Path) -> None:
    client, db_path, runtime, inbox, notified = env
    m4a = make_m4a(tmp_path, 65).read_bytes()

    response = client.put("/webdav/RecorderPlus/2026-10-15 14-00-00.m4a", content=m4a, headers=auth())

    assert response.status_code == 201
    assert (inbox / "RecorderPlus" / "2026-10-15 14-00-00.m4a").read_bytes() == m4a
    wait_until(lambda: len(meetings(db_path)) == 1 and meetings(db_path)[0].status == "done")
    meeting = meetings(db_path)[0]
    # ファイル名の日時を録り始めの時刻にする
    assert meeting.started_at == datetime(2026, 10, 15, 14, 0, tzinfo=TZ)
    assert meeting.title == "録音 14時00分"
    assert (runtime.audio.meeting_dir(meeting.id) / "元の録音.m4a").read_bytes() == m4a
    wait_until(lambda: notified == [(meeting.id, "done")])


def test_同じ録音が送り直されても議事録は1つ(env, tmp_path: Path) -> None:
    client, db_path, *_ = env
    m4a = make_m4a(tmp_path, 3).read_bytes()

    client.put("/webdav/a.m4a", content=m4a, headers=auth())
    wait_until(lambda: len(meetings(db_path)) == 1)
    client.put("/webdav/a.m4a", content=m4a, headers=auth())
    wait_until(lambda: meetings(db_path)[0].status == "done")

    assert len(meetings(db_path)) == 1


def test_音声でないファイルは置くだけで議事録にしない(env) -> None:
    client, db_path, _, inbox, _ = env

    assert client.put("/webdav/settings.json", content=b"{}", headers=auth()).status_code == 201

    assert (inbox / "settings.json").exists()
    assert meetings(db_path) == []


def test_読めない録音は議事録を作らず受け箱に残す(env) -> None:
    client, db_path, _, inbox, notified = env

    assert client.put("/webdav/壊れた.m4a", content=b"not audio" * 100, headers=auth()).status_code == 201

    wait_until(lambda: notified == [(None, "failed")])
    assert meetings(db_path) == []
    assert (inbox / "壊れた.m4a").exists()


def test_一時的な名前で送ってから名前を変えても議事録にする(env, tmp_path: Path) -> None:
    client, db_path, _, inbox, _ = env
    m4a = make_m4a(tmp_path, 3).read_bytes()

    client.put("/webdav/rec.m4a.part", content=m4a, headers=auth())
    response = client.request(
        "MOVE", "/webdav/rec.m4a.part",
        headers={"Destination": "https://example.ts.net/webdav/rec.m4a", **auth()},
    )

    assert response.status_code == 201
    assert (inbox / "rec.m4a").exists() and not (inbox / "rec.m4a.part").exists()
    wait_until(lambda: len(meetings(db_path)) == 1)


def test_受け箱のファイルを取り出して消せる(env) -> None:
    client, _, _, inbox, _ = env
    client.put("/webdav/memo.txt", content=b"hello", headers=auth())

    assert client.get("/webdav/memo.txt", headers=auth()).content == b"hello"
    assert client.request("DELETE", "/webdav/memo.txt", headers=auth()).status_code == 204
    assert not (inbox / "memo.txt").exists()


@pytest.mark.parametrize(
    "path",
    ["/webdav/../secret.txt", "/webdav/a/../../secret.txt", "/webdav/a:b.txt", "/webdav/%2e%2e/x", "/webdav/nul.m4a", "/webdav/COM1"],
)
def test_受け箱の外や使えない名前には書かせない(env, path: str) -> None:
    client, _, _, inbox, _ = env

    response = client.put(path, content=b"x", headers=auth())

    assert response.status_code in (400, 404)
    assert not (inbox.parent / "secret.txt").exists()


def test_議事録にする途中で失敗したら送り直しでもう一度試せる(env, tmp_path: Path, monkeypatch) -> None:
    from companion.web import minutes_routes

    client, db_path, _, _, notified = env
    m4a = make_m4a(tmp_path, 3).read_bytes()
    real = minutes_routes.start_imported

    def broken(*args, **kwargs):
        raise OSError("ディスクがいっぱい")

    monkeypatch.setattr(minutes_routes, "start_imported", broken)
    client.put("/webdav/a.m4a", content=m4a, headers=auth())
    wait_until(lambda: notified == [(None, "failed")])

    monkeypatch.setattr(minutes_routes, "start_imported", real)
    client.put("/webdav/a.m4a", content=m4a, headers=auth())
    wait_until(lambda: len(meetings(db_path)) == 1)


def test_名前の大文字小文字が違うだけの送り直しは同じ録音とみなす(env, tmp_path: Path) -> None:
    client, db_path, *_ = env
    m4a = make_m4a(tmp_path, 3).read_bytes()

    client.put("/webdav/Rec.m4a", content=m4a, headers=auth())
    wait_until(lambda: len(meetings(db_path)) == 1)
    client.put("/webdav/rec.M4A", content=m4a, headers=auth())
    wait_until(lambda: meetings(db_path)[0].status == "done")

    assert len(meetings(db_path)) == 1


def test_フォルダの上や受け箱そのものへは動かせない(env) -> None:
    client, _, _, inbox, _ = env
    client.put("/webdav/memo.txt", content=b"x", headers=auth())
    client.request("MKCOL", "/webdav/dir", headers=auth())

    for destination in ("http://h/webdav/", "http://h/webdav/dir"):
        response = client.request("MOVE", "/webdav/memo.txt", headers={"Destination": destination, **auth()})
        assert response.status_code == 409
    assert (inbox / "memo.txt").exists()
