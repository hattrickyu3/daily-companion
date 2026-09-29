"""メール画面と、今日の画面の「要対応メール」欄のテスト."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion.db.connection import apply_schema, session
from companion.services.mail_inbox import load

TODAY = date(2026, 9, 30)


@pytest.fixture
def results_file(tmp_path: Path) -> Path:
    path = tmp_path / "results.json"
    mails = [
        {"received": "2026-09-29 15:22", "category": "要対応", "deadline": "2026-10-07",
         "summary": "GPS-Academic受検", "sender": "portalmail@tcu.ac.jp", "subject": "受検のご案内"},
        {"received": "2026-09-29 17:13", "category": "確認", "deadline": None,
         "summary": "交換留学説明会 9/30", "sender": "portalmail@tcu.ac.jp", "subject": "説明会"},
        {"received": "2026-09-29 20:03", "category": "不要", "deadline": None,
         "summary": "就活コラム", "sender": "レバテックルーキー", "subject": "<script>x</script>"},
    ]
    path.write_text(json.dumps({"updated_at": "2026-09-30T08:01:00", "mails": mails},
                               ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def client(tmp_path, results_file, monkeypatch) -> Iterator[TestClient]:
    from companion.web import app as web_app
    from companion.web import mail_routes

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)

    monkeypatch.setattr(web_app, "session", lambda: session(db_path))
    monkeypatch.setattr(web_app, "today_local", lambda: TODAY)
    monkeypatch.setattr(web_app, "get_events_for", lambda day: [])
    monkeypatch.setattr(mail_routes, "get_inbox", lambda: load(results_file))
    monkeypatch.setattr(mail_routes, "today_local", lambda: TODAY)

    with TestClient(web_app.app) as test_client:
        yield test_client


def test_メール画面に判定結果の表が出る(client) -> None:
    response = client.get("/mail")

    assert response.status_code == 200
    assert "GPS-Academic受検" in response.text
    assert "交換留学説明会 9/30" in response.text
    assert "就活コラム" in response.text


def test_区分で絞り込める(client) -> None:
    response = client.get("/mail", params={"category": "要対応"})

    assert "GPS-Academic受検" in response.text
    assert "交換留学説明会 9/30" not in response.text


def test_知らない区分は絞り込まずに全件出す(client) -> None:
    response = client.get("/mail", params={"category": "なにか"})

    assert response.status_code == 200
    assert "就活コラム" in response.text


def test_件名はHTMLとして解釈せずに出す(client) -> None:
    response = client.get("/mail")

    assert "<script>x</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_今日の画面に要対応メールが出る(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "要対応メール" in response.text
    assert "GPS-Academic受検" in response.text
    assert "就活コラム" not in response.text


def test_未設定ならメール画面は設定方法を案内する(client, monkeypatch) -> None:
    from companion.web import mail_routes

    monkeypatch.setattr(mail_routes, "get_inbox", lambda: None)
    response = client.get("/mail")

    assert response.status_code == 200
    assert "MAIL_RESULTS_FILE" in response.text


def test_未設定なら今日の画面にメール欄を出さない(client, monkeypatch) -> None:
    from companion.web import mail_routes

    monkeypatch.setattr(mail_routes, "get_inbox", lambda: None)
    response = client.get("/")

    assert response.status_code == 200
    assert "要対応メール" not in response.text
