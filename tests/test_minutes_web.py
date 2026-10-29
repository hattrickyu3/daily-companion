"""議事録の画面のテスト. DBと議事録係はテスト用に差し替える."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

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
    # 授業の一覧は本物の保管庫ではなく、テスト用の保管庫から作る.
    from dataclasses import replace

    from companion.config import get_config
    from companion.web import minutes_scene_form

    vault = tmp_path / "vault"
    (vault / "3年３Q" / "自然言語処理" / "ノート").mkdir(parents=True)
    test_config = replace(get_config(), notes_dir=vault)
    monkeypatch.setattr(minutes_scene_form, "get_config", lambda: test_config)
    from companion.web import minutes_words_routes

    monkeypatch.setattr(minutes_words_routes, "get_config", lambda: test_config)

    with own_device_client(web_app.app) as client:
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

    with own_device_client(web_app.app) as client:
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

    from_iphone = own_device_client(web_app.app, client=("100.65.177.94", 50000)).get("/minutes")
    from_home_wifi = own_device_client(web_app.app, client=("192.168.3.40", 50000)).get("/minutes")

    assert "pc.example.ts.net/webdav" in from_iphone.text
    assert "WebDAVで直接送る" in from_iphone.text
    assert "pw-123" in from_iphone.text
    # 家のWi-Fiの中のほかの機器には、画面ごと見せない
    assert from_home_wifi.status_code == 403
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


def test_議事録の通知に会議の題名やエラーの中身を載せない(env, monkeypatch) -> None:
    # Arrange: 通知は外のサーバー（ntfy.sh）を通るので、題名は画面で読ませる
    from companion.web import minutes_routes

    client, _, _ = env
    client.post("/minutes", data={"title": "A社の最終面接"})
    sent: list[dict] = []
    monkeypatch.setattr(minutes_routes, "send_notification", lambda body, **kwargs: sent.append({"body": body, **kwargs}))

    # Act
    minutes_routes._notify_minutes(1, db.DONE, None)
    minutes_routes._notify_minutes(1, db.FAILED, "C:/Users/me/録音/A社.m4a を読めません")
    minutes_routes._notify_minutes(None, db.FAILED, "C:/Users/me/録音/A社.m4a を読めません")

    # Assert
    assert len(sent) == 3
    for notification in sent:
        assert "A社" not in notification["body"]
    assert sent[0]["click_url"].endswith("/minutes/1")


def test_大きすぎる切れ端は読み切る前に断る(env, monkeypatch) -> None:
    # Arrange: 上限を超えた分までメモリに溜めないよう、届く途中で打ち切る
    from companion.web import minutes_routes

    client, db_path, _ = env
    client.post("/minutes", data={"title": "ゼミ"})
    monkeypatch.setattr(minutes_routes, "MAX_SEGMENT_BYTES", 1024)

    def chunks():
        for _ in range(64):
            yield b"\0" * 1024

    # Act
    response = client.post(f"/minutes/1/segments?seg={SEG}", content=chunks())

    # Assert
    assert response.status_code == 413
    with session(db_path) as conn:
        assert db.list_segments(conn, 1) == []


# ---- 場面（ゼミ・授業・打ち合わせ・ひとりメモ） ----


def _created(client, data: dict) -> db.Meeting:
    from companion.web import minutes_routes

    location = client.post("/minutes", data=data, follow_redirects=False).headers["location"]
    with minutes_routes.session() as conn:
        return db.get_meeting(conn, int(location.rsplit("/", 1)[1]))


def test_録音を始める欄で場面と授業を選べる(env) -> None:
    client, _, _ = env

    page = client.get("/minutes").text

    assert 'name="kind"' in page and ">授業</option>" in page and ">ひとりメモ</option>" in page
    assert 'value="3年３Q/自然言語処理"' in page
    assert "/static/minutes/scene_fields.js" in page


def test_授業を選んで始めると題名が空なら授業名にする(env) -> None:
    client, _, _ = env

    created = _created(client, {"title": "", "kind": "lecture", "course": "3年３Q/自然言語処理"})

    assert (created.kind, created.course, created.title) == ("lecture", "3年３Q/自然言語処理", "自然言語処理")


def test_一覧に無い授業や知らない場面は受け付けない(env) -> None:
    client, _, _ = env

    outside = _created(client, {"title": "x", "kind": "lecture", "course": "../../Windows"})
    unknown = _created(client, {"title": "x", "kind": "evil", "course": "3年３Q/自然言語処理"})

    assert (outside.kind, outside.course) == ("lecture", "")
    assert (unknown.kind, unknown.course) == ("seminar", "")


def test_一覧を場面と言葉で絞り込める(env) -> None:
    client, _, _ = env
    client.post("/minutes", data={"title": "ゼミ第5回", "kind": "seminar"})
    client.post("/minutes", data={"title": "", "kind": "lecture", "course": "3年３Q/自然言語処理"})
    client.post("/minutes", data={"title": "進路のこと", "kind": "solo"})

    lectures = client.get("/minutes", params={"kind": "lecture"}).text
    words = client.get("/minutes", params={"q": "第5回"}).text
    solo = client.get("/minutes", params={"kind": "solo"}).text
    nothing = client.get("/minutes", params={"q": "存在しない言葉"}).text

    assert "授業・自然言語処理" in lectures and "ゼミ第5回" not in lectures
    assert "ゼミ第5回" in words and "授業・自然言語処理" not in words
    assert "進路のこと" in solo  # 場面でひとりメモを選んだときだけ出す
    assert "0件（全2件）" in nothing and "条件に合う記録はありません" in nothing


def test_清書が済んだ会議は場面を変えられ録音中は変えない(env) -> None:
    client, db_path, _ = env
    recording = _created(client, {"title": "ゼミ"})
    done = _created(client, {"title": "ゼミ"})
    with session(db_path) as conn:
        db.set_status(conn, done.id, db.DONE)

    client.post(f"/minutes/{done.id}/scene", data={"kind": "lecture", "course": "3年３Q/自然言語処理"})
    client.post(f"/minutes/{recording.id}/scene", data={"kind": "solo"})

    with session(db_path) as conn:
        changed, unchanged = db.get_meeting(conn, done.id), db.get_meeting(conn, recording.id)
    assert (changed.kind, changed.course) == ("lecture", "3年３Q/自然言語処理")
    assert unchanged.kind == "seminar"
    page = client.get(f"/minutes/{done.id}").text
    assert "場面：授業・自然言語処理" in page
    assert "大事な用語" in page  # 下書きの欄も授業の見出しになる


def test_ボイスメモも場面を選んで送れる(env, tmp_path: Path) -> None:
    client, db_path, runtime = env
    wav = make_wav(1.0)

    response = client.post(
        "/minutes/import",
        data={"title": "", "kind": "meeting", "recorded_at": "0"},
        files={"audio": ("memo.wav", wav, "audio/wav")},
        follow_redirects=False,
    )

    meeting_id = int(response.headers["location"].rsplit("/", 1)[1])
    with session(db_path) as conn:
        created = db.get_meeting(conn, meeting_id)
    assert (created.kind, created.title) == ("meeting", "打ち合わせ")


def test_授業以外の場面で絞り込むときは隠れた授業の欄を使わない(env) -> None:
    client, _, _ = env
    client.post("/minutes", data={"title": "ゼミ第5回", "kind": "seminar"})

    page = client.get("/minutes", params={"kind": "seminar", "course": "3年３Q/自然言語処理"}).text

    assert "ゼミ第5回" in page and "1件（全1件）" in page


# ---- ショートカットから送るひとりメモ ----


def test_ショートカットから送った録音はひとりメモとして清書し終わったら知らせる(env, tmp_path: Path) -> None:
    from test_minutes_import import make_m4a

    client, db_path, runtime = env
    notices: list[tuple] = []
    runtime.set_notifier(lambda meeting_id, status, error: notices.append((meeting_id, status)))
    m4a = make_m4a(tmp_path, 5)

    response = client.post("/minutes/quick", files={"audio": ("録音.m4a", m4a.read_bytes(), "audio/x-m4a")})

    assert response.status_code == 200
    assert response.text == "受け取りました。できたら通知します。"
    with session(db_path) as conn:
        meeting = db.get_meeting(conn, 1)
        assert meeting.kind == "solo"
        assert meeting.title.startswith("ひとりメモ ")
        wait_until(lambda: db.get_meeting(conn, 1).status == "done")
        assert "ひとりメモ" in db.get_meeting(conn, 1).note_path  # 保管庫の _授業以外\ひとりメモ に書き出す
    wait_until(lambda: notices == [(1, "done")])


def test_ショートカットから読めないファイルが届いたら理由を1行で返す(env) -> None:
    client, db_path, _ = env

    response = client.post("/minutes/quick", files={"audio": ("memo.txt", "ただのメモ".encode() * 50, "text/plain")})

    assert response.status_code == 400
    assert response.text.startswith("送れませんでした: ")
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []


def test_ひとりメモの通知はひとりメモと書き中身は載せない(env, monkeypatch) -> None:
    from companion.web import minutes_routes

    client, _, _ = env
    client.post("/minutes", data={"title": "転職の悩み", "kind": "solo"})
    sent: list[dict] = []
    monkeypatch.setattr(minutes_routes, "send_notification", lambda body, **kwargs: sent.append({"body": body, **kwargs}))

    minutes_routes._notify_minutes(1, db.DONE, None)

    assert sent[0]["title"] == "ひとりメモができました"
    assert "のひとりメモができました。" in sent[0]["body"]
    assert "転職" not in sent[0]["body"]


def test_ショートカットのキーの名前が違っても録音を受け取る(env, tmp_path: Path) -> None:
    from test_minutes_import import make_m4a

    client, db_path, _ = env
    m4a = make_m4a(tmp_path, 3)

    # iPhoneのキーボードは頭を大文字にするので、キーが Audio になりやすい
    response = client.post("/minutes/quick", files={"Audio": ("録音.m4a", m4a.read_bytes(), "audio/x-m4a")})

    assert response.status_code == 200
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1).kind == "solo"


def test_ショートカットが本文に録音をそのまま入れて送っても受け取る(env, tmp_path: Path) -> None:
    from test_minutes_import import make_m4a

    client, db_path, runtime = env
    m4a = make_m4a(tmp_path, 3)

    response = client.post("/minutes/quick", content=m4a.read_bytes(), headers={"Content-Type": "audio/x-m4a"})

    assert response.status_code == 200
    with session(db_path) as conn:
        assert db.get_meeting(conn, 1).kind == "solo"
    assert (runtime.audio.meeting_dir(1) / "元の録音.m4a").exists()


def test_ショートカットのフォームに録音が無ければ直し方を返す(env) -> None:
    client, db_path, _ = env

    form = client.post("/minutes/quick", data={"audio": "録音されたオーディオ"})
    empty = client.post("/minutes/quick")

    assert form.status_code == 400 and "種類「ファイル」" in form.text
    assert empty.status_code == 400 and "録音ファイルが入っていません" in empty.text
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []


# ---- 辞書 ----


def test_辞書に語を足して消せる(env) -> None:
    client, db_path, _ = env
    from companion.db import words as words_db

    page = client.get("/minutes/words")
    added = client.post("/minutes/words", data={"word": "形態素解析", "variants": "形体素解析、け", "course": "3年３Q/自然言語処理"}, follow_redirects=False)
    outside = client.post("/minutes/words", data={"word": "田中", "course": "../../Windows"})
    listed = client.get("/minutes/words").text

    assert page.status_code == 200 and "消すつなぎ言葉" in page.text
    assert added.status_code == 303
    with session(db_path) as conn:
        saved = words_db.list_words(conn)
    assert [(w.word, w.variants, w.course) for w in saved] == [("田中", (), ""), ("形態素解析", ("形体素解析",), "3年３Q/自然言語処理")]
    assert outside.status_code == 200
    assert "自然言語処理" in listed

    client.post(f"/minutes/words/{saved[0].id}/delete")
    with session(db_path) as conn:
        assert [w.word for w in words_db.list_words(conn)] == ["形態素解析"]


def test_正しい書き方が空なら理由を出す(env) -> None:
    client, _, _ = env

    response = client.post("/minutes/words", data={"word": "  "})

    assert response.status_code == 400
    assert "正しい書き方を入れてください" in response.text


def test_辞書の語は消す確認の文にそのまま埋め込まない(env) -> None:
    client, _, _ = env
    client.post("/minutes/words", data={"word": "');alert(1);//"})

    page = client.get("/minutes/words").text

    assert "confirm(this.dataset.confirm)" in page
    assert "');alert(1);//" not in page


def test_つなぎ言葉の一覧を保存できる(env) -> None:
    client, db_path, _ = env
    from companion.db import words as words_db

    client.post("/minutes/words/fillers", data={"fillers": "えーと、あのー\nうーん"})

    with session(db_path) as conn:
        assert words_db.get_setting(conn, words_db.FILLERS_KEY) == "えーと、あのー、うーん"
    assert "えーと、あのー、うーん" in client.get("/minutes/words").text


def test_一覧から辞書の画面に行ける(env) -> None:
    client, _, _ = env

    assert 'href="/minutes/words"' in client.get("/minutes").text


def test_ショートカットから大きすぎると申告されたものは読む前に断る(env, monkeypatch) -> None:
    from companion.web import minutes_routes

    client, db_path, _ = env
    monkeypatch.setattr(minutes_routes, "MAX_IMPORT_BYTES", 10)

    response = client.post("/minutes/quick", files={"audio": ("録音.m4a", b"x" * 100, "audio/x-m4a")})

    assert response.status_code == 400 and "大きすぎます" in response.text
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []


# ---- 音声入力 ----


def test_音声入力は録音を文字にして返し録音は残さない(env) -> None:
    from companion.db import words as words_db

    client, db_path, runtime = env
    runtime._transcriber.texts = ["えーっと、田名かさんに送ってください"]
    with session(db_path) as conn:
        words_db.save_word(conn, "田中", ("田名か",), "")

    response = client.post("/dictate", content=make_wav(1.0), headers={"Content-Type": "audio/wav"})

    assert response.status_code == 200
    assert response.text == "田中さんに送ってください"
    assert runtime._transcriber.hotwords == "田中"
    assert runtime._transcriber.prompts[-1] == ""  # 前の文が届かなければ何も渡さない
    assert not any(runtime.audio.root.rglob("*.wav"))  # 文字にしたら消す
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []  # 議事録には入れない


def test_音声入力は前の切れ端の文をWhisperに渡す_長ければ後ろだけ(env) -> None:
    from urllib.parse import quote

    from companion.dictation.client import MAX_PREVIOUS_CHARS, PREVIOUS_HEADER

    client, _, runtime = env
    wav = make_wav(1.0)

    client.post("/dictate", content=wav, headers={"Content-Type": "audio/wav", PREVIOUS_HEADER: quote("形態素解析の話。")})
    client.post("/dictate", content=wav, headers={"Content-Type": "audio/wav", PREVIOUS_HEADER: quote("あ" * 500 + "末")})

    assert runtime._transcriber.prompts[-2] == "形態素解析の話。"
    assert len(runtime._transcriber.prompts[-1]) == MAX_PREVIOUS_CHARS
    assert runtime._transcriber.prompts[-1].endswith("末")


def test_音声入力はショートカットのフォームからも受ける(env, tmp_path: Path) -> None:
    from test_minutes_import import make_m4a

    client, _, runtime = env
    runtime._transcriber.texts = ["明日の予定"]

    response = client.post("/dictate", files={"Audio": ("録音.m4a", make_m4a(tmp_path, 2).read_bytes(), "audio/x-m4a")})

    assert response.status_code == 200 and response.text == "明日の予定"


def test_長すぎる音声入力は断る(env, monkeypatch) -> None:
    from companion.web import dictate_routes

    client, _, runtime = env
    monkeypatch.setattr(dictate_routes, "MAX_DICTATION_SEC", 1)

    response = client.post("/dictate", content=make_wav(2.0), headers={"Content-Type": "audio/wav"})

    assert response.status_code == 400 and "ひとりメモ" in response.text
    assert not any(runtime.audio.root.rglob("*.wav"))


def test_音声入力で録音が入っていなければ理由を返す(env) -> None:
    client, _, _ = env

    response = client.post("/dictate")

    assert response.status_code == 400 and "録音ファイルが入っていません" in response.text


# ---- ひとりメモの画面と話して入力 ----


def test_ひとりメモは議事録の一覧から分けて専用の一覧に要点つきで出す(env) -> None:
    client, db_path, _ = env
    client.post("/minutes", data={"title": "ゼミ第5回", "kind": "seminar"})
    client.post("/minutes", data={"title": "進路のこと", "kind": "solo"})
    with session(db_path) as conn:
        db.save_final(conn, 2, "## 要点（3行以内）\n\n- 院に行くか迷っている\n\n## 考えたこと\n\n- 長い本文の中身", None, polished=True)
        db.set_status(conn, 2, db.DONE)

    minutes = client.get("/minutes").text
    memos = client.get("/memos").text
    found = client.get("/memos", params={"q": "院に行く"}).text
    nothing = client.get("/memos", params={"q": "存在しない言葉"}).text

    assert "ゼミ第5回" in minutes and "進路のこと" not in minutes
    assert "進路のこと" in memos and "院に行くか迷っている" in memos and "ゼミ第5回" not in memos
    assert "長い本文の中身" not in memos  # 一覧に出すのは要点だけ
    assert "進路のこと" in found
    assert "条件に合うひとりメモはありません" in nothing
    assert 'href="/memos"' in memos  # 左のメニューに入口がある


def test_話して入力の画面が開ける(env) -> None:
    client, _, _ = env

    page = client.get("/memos/talk").text

    assert "/static/minutes/recorder.js" in page and "/static/memos/talk.js" in page
    assert "ひとりメモにする" in page and "コピー" in page


def test_話して入力の文字からひとりメモを作る(env) -> None:
    client, db_path, _ = env

    response = client.post("/memos/from-text", json={"pieces": [
        {"at": SEG + 3000, "ms": 1500, "text": "二つ目。"},
        {"at": SEG, "ms": 2000, "text": "一つ目。"},
        {"at": SEG + 5000, "ms": 800, "text": "  "},
    ]})

    assert response.status_code == 200
    url = response.json()["url"]
    memo_id = int(url.rsplit("/", 1)[1])
    wait_until(lambda: client.get(url).status_code == 200 and _status(db_path, memo_id) == "done")
    with session(db_path) as conn:
        memo = db.get_meeting(conn, memo_id)
        texts = [s.text for s in db.list_segments(conn, memo_id)]
    assert memo.kind == "solo" and memo.title.startswith("ひとりメモ ")
    assert texts == ["一つ目。", "二つ目。"]


def _status(db_path: Path, meeting_id: int) -> str:
    with session(db_path) as conn:
        return db.get_meeting(conn, meeting_id).status


@pytest.mark.parametrize("payload", [
    {},
    {"pieces": []},
    {"pieces": [{"at": 5, "ms": 100, "text": "x"}]},
    {"pieces": [{"at": SEG, "ms": -1, "text": "x"}]},
    {"pieces": [{"at": SEG, "ms": 100, "text": "   "}]},
    {"pieces": [{"at": SEG, "ms": 100, "text": "x" * 3000}]},
    ["not", "a", "dict"],
])
def test_話して入力の文字が変なら作らない(env, payload) -> None:
    client, db_path, _ = env

    response = client.post("/memos/from-text", json=payload)

    assert response.status_code == 400 and response.json()["ok"] is False
    with session(db_path) as conn:
        assert db.list_meetings(conn) == []

