"""Web画面のテスト.

Claude呼び出しとGoogleカレンダーはモックし、画面と保存の流れだけを検証する。
DBもテスト用の一時ファイルに差し替えて、本物の日記に触れないようにする。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from companion.db.connection import apply_schema, session

TODAY = date(2026, 8, 8)


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """DB・カレンダー・日付を差し替えたテスト用クライアント."""
    from companion.web import app as web_app

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)

    monkeypatch.setattr(web_app, "session", lambda: session(db_path))
    monkeypatch.setattr(web_app, "today_local", lambda: TODAY)
    monkeypatch.setattr(web_app, "get_events_for", lambda day: [])

    with TestClient(web_app.app) as test_client:
        yield test_client


def test_今日の画面が開ける(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "2026年08月08日" in response.text
    assert "今日は何をしましたか" in response.text


def test_日記を保存すると詳細画面に飛ぶ(client, monkeypatch) -> None:
    # Arrange: 構造化は走らせない
    from companion.web import app as web_app

    monkeypatch.setattr(web_app, "_structure_in_background", lambda *args: None)

    # Act
    response = client.post(
        "/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "カレンダー連携を実装した"}
    )

    # Assert
    assert response.status_code == 200
    assert response.url.path == "/entries/2026-08-08"
    assert "カレンダー連携を実装した" in response.text


def test_保存直後は整理中と表示される(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    monkeypatch.setattr(web_app, "_structure_in_background", lambda *args: None)

    # Act
    response = client.post(
        "/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "何か書いた"}
    )

    # Assert: 整理が終わるまで自動更新する
    assert "Claudeが整理しています" in response.text
    assert 'http-equiv="refresh"' in response.text


def test_空の日記は保存されない(client, monkeypatch) -> None:
    # Arrange
    from companion.web import app as web_app

    monkeypatch.setattr(web_app, "_structure_in_background", lambda *args: None)

    # Act
    response = client.post("/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "   "})

    # Assert: 今日の画面に戻され、一覧にも増えていない
    assert response.url.path == "/"
    assert "まだ日記がありません" in client.get("/entries").text


def test_構造化済みの日記は要約と項目が出る(client, tmp_path) -> None:
    # Arrange
    from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
    from companion.web import app as web_app

    with web_app.session() as conn:
        entry_id = save_raw_text(conn, TODAY, "朝から実装した")
        apply_structure(
            conn,
            entry_id,
            StructuredEntry(
                summary="実装を進めた1日",
                done=("カレンダー連携",),
                insights=("先に疎通確認をすると早い",),
                carry_over="明日はWeb画面",
                tags=("開発",),
            ),
        )

    # Act
    response = client.get(f"/entries/{TODAY.isoformat()}")

    # Assert
    assert "実装を進めた1日" in response.text
    assert "カレンダー連携" in response.text
    assert "明日はWeb画面" in response.text
    # 整理が済んだので自動更新は止まる
    assert 'http-equiv="refresh"' not in response.text


def test_一覧をタグで絞り込める(client) -> None:
    # Arrange
    from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
    from companion.web import app as web_app

    with web_app.session() as conn:
        first = save_raw_text(conn, TODAY, "開発の日")
        second = save_raw_text(conn, date(2026, 8, 7), "就活の日")
        apply_structure(conn, first, StructuredEntry(summary="開発した", tags=("開発",)))
        apply_structure(conn, second, StructuredEntry(summary="面接した", tags=("就活",)))

    # Act
    response = client.get("/entries", params={"tag": "就活"})

    # Assert
    assert "面接した" in response.text
    assert "開発した" not in response.text


def test_日記を検索できる(client) -> None:
    # Arrange
    from companion.db.repository import save_raw_text
    from companion.web import app as web_app

    with web_app.session() as conn:
        save_raw_text(conn, TODAY, "今日はカレンダー連携を実装した")

    # Act
    response = client.get("/search", params={"q": "カレンダー"})

    # Assert
    assert "2026/08/08" in response.text


def test_日記のない日でも詳細画面は開ける(client) -> None:
    response = client.get("/entries/2020-01-01")

    assert response.status_code == 200
    assert "この日の日記はまだありません" in response.text


def test_不正な日付は一覧に戻される(client) -> None:
    response = client.get("/entries/not-a-date")

    assert response.url.path == "/entries"
