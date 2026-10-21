"""日記の追記と、過去日の記入のテスト.

書き足したときに前の内容が消えないこと、
要約が書き足した部分だけでなく全文から作り直されることを確認する。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

from companion.db.connection import apply_schema, session
from companion.db.repository import (
    StructuredEntry,
    append_raw_text,
    apply_structure,
    get_entry,
    list_entries,
)

TODAY = date(2026, 8, 8)
PAST = date(2026, 8, 5)


def test_既存の日記に書き足せる(conn) -> None:
    # Arrange
    append_raw_text(conn, TODAY, "朝はゼミに出た")

    # Act
    append_raw_text(conn, TODAY, "夜は実装した")

    # Assert: 両方残っている
    entry = get_entry(conn, TODAY)
    assert entry is not None
    assert "朝はゼミに出た" in entry.raw_text
    assert "夜は実装した" in entry.raw_text


def test_書き足しても行数は1件のまま(conn) -> None:
    # Arrange
    append_raw_text(conn, TODAY, "1回目")

    # Act
    append_raw_text(conn, TODAY, "2回目")

    # Assert
    assert len(list_entries(conn)) == 1


def test_区切りは空行だけで時刻を挿し込まない(conn) -> None:
    # Arrange: 原文は本人が書いたものだけ、という前提を崩さない
    append_raw_text(conn, TODAY, "朝はゼミ")

    # Act
    append_raw_text(conn, TODAY, "夜は実装")

    # Assert
    entry = get_entry(conn, TODAY)
    assert entry is not None
    assert entry.raw_text == "朝はゼミ\n\n夜は実装"


def test_日記がない日への追記は新規作成になる(conn) -> None:
    # Arrange / Act
    append_raw_text(conn, PAST, "あとから書いた")

    # Assert
    entry = get_entry(conn, PAST)
    assert entry is not None
    assert entry.raw_text == "あとから書いた"


def test_書き足すと古い要約は破棄される(conn) -> None:
    # Arrange: 書き足した内容が反映されていない要約は残さない
    entry_id = append_raw_text(conn, TODAY, "朝はゼミ")
    apply_structure(conn, entry_id, StructuredEntry(summary="ゼミに出た", tags=("大学",)))

    # Act
    append_raw_text(conn, TODAY, "夜は実装")

    # Assert
    entry = get_entry(conn, TODAY)
    assert entry is not None
    assert entry.summary is None
    assert entry.tags == ()


def test_前後の余分な空白は詰める(conn) -> None:
    # Arrange
    append_raw_text(conn, TODAY, "朝はゼミ\n\n\n")

    # Act
    append_raw_text(conn, TODAY, "   夜は実装   ")

    # Assert
    entry = get_entry(conn, TODAY)
    assert entry is not None
    assert entry.raw_text == "朝はゼミ\n\n夜は実装"


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
    monkeypatch.setattr(web_app, "_structure_in_background", lambda *args: None)

    with own_device_client(web_app.app) as test_client:
        yield test_client


def test_画面から書き足せる(client) -> None:
    # Arrange
    client.post("/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "朝はゼミ"})

    # Act
    response = client.post(
        "/entries/append", data={"entry_date": TODAY.isoformat(), "raw_text": "夜は実装"}
    )

    # Assert
    assert "朝はゼミ" in response.text
    assert "夜は実装" in response.text


def test_構造化は全文に対して行われる(client, monkeypatch) -> None:
    # Arrange: 書き足した部分だけを渡すと、要約が一部しか指さなくなる
    from companion.web import app as web_app

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        web_app,
        "_structure_in_background",
        lambda entry_id, day, text: captured.update({"text": text}),
    )
    client.post("/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "朝はゼミ"})

    # Act
    client.post("/entries/append", data={"entry_date": TODAY.isoformat(), "raw_text": "夜は実装"})

    # Assert
    assert "朝はゼミ" in captured["text"]
    assert "夜は実装" in captured["text"]


def test_空の書き足しは無視される(client) -> None:
    # Arrange
    client.post("/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "朝はゼミ"})

    # Act
    client.post("/entries/append", data={"entry_date": TODAY.isoformat(), "raw_text": "   "})

    # Assert
    detail = client.get(f"/entries/{TODAY.isoformat()}")
    assert "朝はゼミ" in detail.text


def test_過去日の画面に入力欄がある(client) -> None:
    # Act
    response = client.get(f"/entries/{PAST.isoformat()}")

    # Assert: 忘れた日をあとから書けること
    assert "この日の日記を書く" in response.text
    assert 'action="/entries"' in response.text


def test_過去日の日記を保存できる(client) -> None:
    # Act
    response = client.post(
        "/entries", data={"entry_date": PAST.isoformat(), "raw_text": "3日前のことを思い出した"}
    )

    # Assert
    assert response.url.path == f"/entries/{PAST.isoformat()}"
    assert "3日前のことを思い出した" in response.text


def test_書き足しフォームは記録済みの日にだけ出る(client) -> None:
    # Arrange
    client.post("/entries", data={"entry_date": TODAY.isoformat(), "raw_text": "朝はゼミ"})

    # Act
    written = client.get(f"/entries/{TODAY.isoformat()}")
    blank = client.get(f"/entries/{PAST.isoformat()}")

    # Assert
    assert 'action="/entries/append"' in written.text
    assert 'action="/entries/append"' not in blank.text


def test_不正な日付の書き足しは無視される(client) -> None:
    # Act
    response = client.post(
        "/entries/append", data={"entry_date": "not-a-date", "raw_text": "本文"}
    )

    # Assert
    assert response.url.path == "/"
