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
    assert "マイクのノイズ除去を使う" in page.text
    assert "オンライン会議の相手の声も録る" in page.text


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


def test_録音の無い会議は一覧から消せる(env) -> None:
    client, db_path, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})
    assert 'action="/minutes/1/delete"' in client.get("/minutes").text

    response = client.post("/minutes/1/delete", follow_redirects=False)

    assert response.status_code == 303
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1) is None


def test_録音のある会議は消せず音声も残る(env) -> None:
    client, db_path, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})
    client.post(f"/minutes/1/segments?seg={SEG}", content=make_wav())
    assert 'action="/minutes/1/delete"' not in client.get("/minutes").text

    response = client.post("/minutes/1/delete")

    assert response.status_code == 409
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1) is not None
    assert runtime.audio.segment_path(1, SEG).exists()


def test_ファイルだけ残っている会議も消せない(env) -> None:
    client, db_path, runtime = env
    client.post("/minutes", data={"title": "ゼミ"})
    runtime.audio.save_segment(1, SEG, make_wav())  # DBに記録される前にアプリが落ちた状態

    assert client.post("/minutes/1/delete").status_code == 409
    assert runtime.audio.segment_path(1, SEG).exists()


def test_再起動の直後に会議の画面をいきなり開いても足りない列をそろえる(tmp_path, monkeypatch) -> None:
    from datetime import datetime

    from companion.web import app as web_app
    from companion.web import minutes_routes

    # 「手で直したか」の列がまだ無い、前の版のデータベース
    db_path = tmp_path / "old.db"
    with session(db_path) as conn:
        apply_schema(conn)
        db.create_meeting(conn, "ゼミ", datetime(2026, 10, 2, 18, 30))
        conn.execute("ALTER TABLE meetings DROP COLUMN hand_edited")

    def build_runtime():
        with session(db_path) as conn:
            apply_schema(conn)  # 本物の _build_runtime と同じく、作るときにテーブルをそろえる
        return make_runtime(db_path, tmp_path, FakeTranscriber([]))

    monkeypatch.setattr(minutes_routes, "session", lambda: session(db_path))
    monkeypatch.setattr(minutes_routes, "_runtime", None)
    monkeypatch.setattr(minutes_routes, "_build_runtime", build_runtime)

    with TestClient(web_app.app) as client:
        assert client.get("/minutes/1").status_code == 200
        assert client.get("/minutes/1/edit", follow_redirects=False).status_code == 303


def test_一覧にボイスメモを送る欄がある(env) -> None:
    client, _, _ = env

    response = client.get("/minutes")

    assert 'action="/minutes/import"' in response.text
    assert 'enctype="multipart/form-data"' in response.text


def test_ボイスメモを送ると録り終えた時刻から会議を作り清書する(env, tmp_path: Path) -> None:
    from datetime import datetime

    from test_minutes_import import make_m4a
    from test_minutes_runtime import TZ

    client, db_path, runtime = env
    m4a = make_m4a(tmp_path, 65)
    saved_at = datetime(2026, 10, 15, 15, 0, 0, tzinfo=TZ)

    response = client.post(
        "/minutes/import",
        data={"title": "班のインタビュー", "participants": "自分、かなと", "recorded_at": str(int(saved_at.timestamp() * 1000))},
        files={"audio": ("新規録音.m4a", m4a.read_bytes(), "audio/x-m4a")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/minutes/1"
    with session(db_path) as conn:
        meeting = db.get_meeting(conn, 1)
        assert meeting.title == "班のインタビュー"
        assert meeting.participants == "自分、かなと"
        # ファイルの日時は録り終えた時刻なので、長さの分だけさかのぼる
        assert abs((saved_at - meeting.started_at).total_seconds() - 65) <= 1
        assert len(db.list_segments(conn, 1)) == 3
        wait_until(lambda: db.get_meeting(conn, 1).status == "done")
    # 送られてきた元のファイルも残す
    assert (runtime.audio.meeting_dir(1) / "元の録音.m4a").read_bytes() == m4a.read_bytes()


def test_音声でないファイルを送ると会議を作らず理由を出す(env) -> None:
    client, db_path, runtime = env

    response = client.post(
        "/minutes/import",
        data={"title": "ゼミ"},
        files={"audio": ("memo.txt", "ただのメモ".encode() * 50, "text/plain")},
    )

    assert response.status_code == 400
    assert "音声ファイルとして読めませんでした" in response.text
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []
    assert not any(runtime.audio.root.rglob("*.txt"))


def test_取り込みの途中で思わぬエラーが起きても清書中のまま固まらない(env, tmp_path: Path, monkeypatch) -> None:
    from test_minutes_import import make_m4a

    client, db_path, runtime = env

    def broken(*args, **kwargs):
        raise OSError("ディスクがいっぱい")

    monkeypatch.setattr(runtime, "import_recording", broken)

    response = client.post(
        "/minutes/import",
        data={"title": "ゼミ"},
        files={"audio": ("新規録音.m4a", make_m4a(tmp_path, 3).read_bytes(), "audio/x-m4a")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session(db_path) as conn:
        meeting = db.get_meeting(conn, 1)
    assert meeting.status == "failed"
    assert "ディスクがいっぱい" in meeting.error
    assert (runtime.audio.meeting_dir(1) / "元の録音.m4a").exists()


def test_録音アプリに入れるWebDAVの設定を一覧に出す(env, monkeypatch) -> None:
    from dataclasses import replace

    from companion.config import get_config
    from companion.web import minutes_routes

    from companion.web import app as web_app

    config = replace(get_config(), app_base_url="http://pc.example.ts.net:8765", webdav_user="recorder", webdav_password="pw-123")
    monkeypatch.setattr(minutes_routes, "get_config", lambda: config)

    from_iphone = TestClient(web_app.app, client=("100.65.177.94", 50000)).get("/minutes")
    from_home_wifi = TestClient(web_app.app, client=("192.168.3.40", 50000)).get("/minutes")

    assert "pc.example.ts.net/webdav" in from_iphone.text
    assert "WebDAVで直接送る" in from_iphone.text
    assert "pw-123" in from_iphone.text
    # 家のWi-Fiの中のほかの機器には、パスワードを見せない
    assert "pc.example.ts.net/webdav" in from_home_wifi.text
    assert "pw-123" not in from_home_wifi.text


def test_見張るフォルダもWebDAVも無ければ設定のしかたを案内する(env, monkeypatch) -> None:
    from dataclasses import replace

    from companion.config import get_config
    from companion.web import minutes_routes

    client, _, _ = env
    config = replace(get_config(), webdav_password="", recorder_folder=None)
    monkeypatch.setattr(minutes_routes, "get_config", lambda: config)

    text = client.get("/minutes").text

    assert "RECORDER_FOLDER" in text
    assert "WebDAVで直接送る" not in text


def test_見張っているフォルダを一覧に出す(env, monkeypatch) -> None:
    from dataclasses import replace

    from companion.config import get_config
    from companion.web import minutes_routes

    client, _, _ = env
    config = replace(get_config(), recorder_folder=Path(r"G:\マイドライブ\Recorder Plus"))
    monkeypatch.setattr(minutes_routes, "get_config", lambda: config)

    response = client.get("/minutes")

    assert r"G:\マイドライブ\Recorder Plus" in response.text
    assert "Done, all in" in response.text
