"""過去のニュース一覧のテスト.

保存単位は「日 × ジャンル」だが、表では1記事1行にして読むので、
その展開が正しいことと、絞り込みが効くことを確認する。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

from companion.db.connection import apply_schema, session
from companion.db.news import NewsSource, save_digest
from companion.services import news_history

DAY1 = date(2026, 8, 8)
DAY2 = date(2026, 8, 9)


def source(title: str, summary: str = "要約", url: str = "https://example.com/a") -> NewsSource:
    return NewsSource(title=title, url=url, summary=summary)


def test_1記事1行に展開される(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "ai_tech", "本文", (source("記事A"), source("記事B")))

    # Act
    history = news_history.build(conn)

    # Assert
    assert [row.title for row in history.rows] == ["記事A", "記事B"]


def test_新しい日から順に並ぶ(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "ai_tech", "本文", (source("古い"),))
    save_digest(conn, DAY2, "ai_tech", "本文", (source("新しい"),))

    # Act
    history = news_history.build(conn)

    # Assert
    assert [row.title for row in history.rows] == ["新しい", "古い"]


def test_ジャンルで絞り込める(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "ai_tech", "本文", (source("技術の話"),))
    save_digest(conn, DAY1, "job_hunting", "本文", (source("就活の話"),))

    # Act
    history = news_history.build(conn, topic="job_hunting")

    # Assert
    assert [row.title for row in history.rows] == ["就活の話"]
    assert history.active_topic == "job_hunting"


def test_絞り込みの選択肢は記録のあるジャンルだけ(conn) -> None:
    # Arrange: 設定にあっても記録がなければ出さない
    save_digest(conn, DAY1, "ai_tech", "本文", (source("記事"),))

    # Act
    history = news_history.build(conn)

    # Assert
    assert [name for name, _ in history.topics] == ["ai_tech"]


def test_ジャンル名は表示用に変換される(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "job_hunting", "本文", (source("記事"),))

    # Act
    history = news_history.build(conn)

    # Assert
    assert history.rows[0].topic_name == "就活・業界動向"
    assert history.topics[0][1] == "就活・業界動向"


def test_日付と曜日が表示用に整う(conn) -> None:
    # Arrange: 2026-08-08 は土曜
    save_digest(conn, DAY1, "ai_tech", "本文", (source("記事"),))

    # Act
    row = news_history.build(conn).rows[0]

    # Assert
    assert row.date_label == "08/08"
    assert row.weekday == "土"


def test_日数を数えられる(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "ai_tech", "本文", (source("A"), source("B")))
    save_digest(conn, DAY2, "ai_tech", "本文", (source("C"),))

    # Act
    history = news_history.build(conn)

    # Assert
    assert len(history.rows) == 3
    assert history.day_count == 2


def test_行数の上限を超えたら切る(conn) -> None:
    # Arrange
    save_digest(conn, DAY1, "ai_tech", "本文", tuple(source(f"記事{i}") for i in range(10)))

    # Act
    history = news_history.build(conn, limit=4)

    # Assert
    assert len(history.rows) == 4


def test_記録がなければ空になる(conn) -> None:
    # Act
    history = news_history.build(conn)

    # Assert
    assert history.is_empty is True
    assert history.topics == ()


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, object]]:
    from companion.web import app as web_app

    db_path = tmp_path / "web.db"
    with session(db_path) as conn:
        apply_schema(conn)

    monkeypatch.setattr(web_app, "session", lambda: session(db_path))
    monkeypatch.setattr(web_app, "today_local", lambda: DAY2)
    monkeypatch.setattr(web_app, "get_events_for", lambda day: [])

    with own_device_client(web_app.app) as test_client:
        yield test_client, db_path


def test_画面に表が出る(client) -> None:
    # Arrange
    test_client, db_path = client
    with session(db_path) as conn:
        save_digest(conn, DAY1, "ai_tech", "本文", (source("新モデル発表", "性能が向上した。"),))

    # Act
    response = test_client.get("/news")

    # Assert
    assert response.status_code == 200
    assert "<table" in response.text
    assert "新モデル発表" in response.text
    assert "性能が向上した。" in response.text
    assert "08/08" in response.text


def test_画面でジャンルを絞り込める(client) -> None:
    # Arrange
    test_client, db_path = client
    with session(db_path) as conn:
        save_digest(conn, DAY1, "ai_tech", "本文", (source("技術の話"),))
        save_digest(conn, DAY1, "job_hunting", "本文", (source("就活の話"),))

    # Act
    response = test_client.get("/news", params={"topic": "job_hunting"})

    # Assert
    assert "就活の話" in response.text
    assert "技術の話" not in response.text


def test_記録がなくても画面は開ける(client) -> None:
    # Arrange
    test_client, _ = client

    # Act
    response = test_client.get("/news")

    # Assert
    assert response.status_code == 200
    assert "まだニュースの記録がありません" in response.text
