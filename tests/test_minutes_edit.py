"""議事録を手で直す機能のテスト."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from companion.services.minutes_edit import EditError, normalize_body, split_note, write_note_body
from test_minutes_audio import make_wav
from test_minutes_runtime import SEG, FakeTranscriber, make_runtime, wait_until

NOTE = "# 2026-10-02 ゼミ\n← [[ゼミ議事録まとめ]]\n\n- 日時：2026-10-02\n\n## 要点\n\n- 話者1が報告した\n"


def test_ノートを頭の部分と本文に分ける() -> None:
    header, body = split_note(NOTE)

    assert header == "# 2026-10-02 ゼミ\n← [[ゼミ議事録まとめ]]\n\n- 日時：2026-10-02\n\n"
    assert body == "## 要点\n\n- 話者1が報告した\n"


def test_見出しの無いノートは全体が頭の部分() -> None:
    assert split_note("# 題名\n本文") == ("# 題名\n本文", "")


def test_本文だけを差し替え頭の部分は残す(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(NOTE, encoding="utf-8")

    write_note_body(note, "## 要点\n\n- 田中さんが報告した")

    assert note.read_text(encoding="utf-8") == (
        "# 2026-10-02 ゼミ\n← [[ゼミ議事録まとめ]]\n\n- 日時：2026-10-02\n\n## 要点\n\n- 田中さんが報告した\n"
    )


def test_画面からの改行をそろえ空は断る() -> None:
    assert normalize_body("## 要点\r\n\r\n- a\r\n") == "## 要点\n\n- a"
    with pytest.raises(EditError):
        normalize_body("  \r\n ")


@pytest.fixture
def finished(tmp_path: Path, monkeypatch) -> Iterator[tuple[TestClient, Path, object]]:
    """清書まで済んだ会議が1つある状態."""
    from companion.web import app as web_app
    from companion.web import minutes_routes

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["話した"]))
    monkeypatch.setattr(minutes_routes, "session", lambda: session(db_path))
    monkeypatch.setattr(minutes_routes, "get_runtime", lambda: runtime)

    with own_device_client(web_app.app) as client:
        client.post("/minutes", data={"title": "ゼミ"})
        client.post(f"/minutes/1/segments?seg={SEG}", content=make_wav())
        client.post("/minutes/1/finish")
        with session(db_path) as conn:
            wait_until(lambda: db.get_meeting(conn, 1).status == "done")
        yield client, db_path, runtime


def note_path(db_path: Path) -> Path:
    with session(db_path) as conn:
        return Path(db.get_meeting(conn, 1).note_path)


def test_直す画面にはノートの本文が出る(finished) -> None:
    client, db_path, _ = finished

    page = client.get("/minutes/1/edit")

    assert page.status_code == 200
    assert "- 清書した" in page.text
    assert "← [[" not in page.text  # 頭の部分は出さない


def test_Obsidianで直した内容を読み込む(finished) -> None:
    client, db_path, _ = finished
    path = note_path(db_path)
    path.write_text(path.read_text(encoding="utf-8").replace("清書した", "Obsidianで直した"), encoding="utf-8")

    assert "Obsidianで直した" in client.get("/minutes/1/edit").text


def test_保存するとアプリとノートの両方が変わり前の版を控える(finished) -> None:
    client, db_path, runtime = finished

    response = client.post("/minutes/1/edit", data={"body": "## 要点\r\n\r\n- 田中さんが報告した"}, follow_redirects=False)

    assert response.status_code == 303
    with session(db_path) as conn:
        meeting = db.get_meeting(conn, 1)
    assert meeting.final_text == "## 要点\n\n- 田中さんが報告した"
    assert meeting.hand_edited
    note = note_path(db_path).read_text(encoding="utf-8")
    assert note.startswith("# ") and "\n← [[ゼミ議事録まとめ]]\n" in note  # 頭の部分は残る
    assert note.endswith("## 要点\n\n- 田中さんが報告した\n")
    backups = list((runtime.audio.meeting_dir(1) / "edits").glob("*.md"))
    assert len(backups) == 1
    assert "- 清書した" in backups[0].read_text(encoding="utf-8")
    page = client.get("/minutes/1").text
    assert "手で直した版" in page
    assert "手で直した内容は消えて" in page


def test_空にして保存しようとすると断り何も変えない(finished) -> None:
    client, db_path, _ = finished

    response = client.post("/minutes/1/edit", data={"body": "   "})

    assert response.status_code == 400
    assert "議事録が空です" in response.text
    assert "- 清書した" in note_path(db_path).read_text(encoding="utf-8")


def test_清書をやり直すと手で直した印は消える(finished) -> None:
    client, db_path, _ = finished
    client.post("/minutes/1/edit", data={"body": "## 要点\n\n- 手で直した"})

    client.post("/minutes/1/retry")
    with session(db_path) as conn:
        wait_until(lambda: db.get_meeting(conn, 1).status == "done" and not db.get_meeting(conn, 1).hand_edited)


def test_録音中の会議は直す画面を開けない(tmp_path, monkeypatch) -> None:
    from companion.web import app as web_app
    from companion.web import minutes_routes

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)
    monkeypatch.setattr(minutes_routes, "session", lambda: session(db_path))
    monkeypatch.setattr(minutes_routes, "get_runtime", lambda: make_runtime(db_path, tmp_path, FakeTranscriber([])))
    with own_device_client(web_app.app) as client:
        client.post("/minutes", data={"title": "ゼミ"})

        response = client.get("/minutes/1/edit", follow_redirects=False)

    assert response.status_code == 303
