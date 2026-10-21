"""夜の催促ジョブのテスト.

通知は実際には送らず、送られたかどうかと優先度だけを検証する。
優先度は「気づいてもらえるか」を左右する要で、下げると無音配信になってしまう。
"""

from __future__ import annotations

from datetime import date

import pytest

from companion.db.connection import apply_schema, session
from companion.db.repository import save_raw_text
from companion.jobs import evening_prompt

TODAY = date(2026, 8, 8)


@pytest.fixture
def job(tmp_path, monkeypatch):
    """DB・通知を差し替えたジョブモジュール."""
    db_path = tmp_path / "job.db"
    with session(db_path) as conn:
        apply_schema(conn)

    sent: list[dict] = []

    monkeypatch.setattr(evening_prompt, "session", lambda: session(db_path))
    monkeypatch.setattr(evening_prompt, "today_local", lambda: TODAY)
    monkeypatch.setattr(evening_prompt, "app_url", lambda path="/": "http://test/")
    monkeypatch.setattr(
        evening_prompt,
        "send_notification",
        lambda body, **kwargs: sent.append({"body": body, **kwargs}),
    )

    return evening_prompt, db_path, sent


def test_未記入なら催促を送る(job) -> None:
    # Arrange
    module, _, sent = job

    # Act
    message = module.run()

    # Assert
    assert message == "催促を送信"
    assert len(sent) == 1
    assert "今日は何をしましたか" in sent[0]["body"]


def test_記録済みなら何も送らない(job) -> None:
    # Arrange: 「もう書いてあります」は通知する価値がないので送らない
    module, db_path, sent = job
    with session(db_path) as conn:
        save_raw_text(conn, TODAY, "今日は実装した")

    # Act
    message = module.run()

    # Assert
    assert message == "記録済みのため送信せず"
    assert sent == []


def test_forceなら記録済みでも送る(job) -> None:
    # Arrange
    module, db_path, sent = job
    with session(db_path) as conn:
        save_raw_text(conn, TODAY, "今日は実装した")

    # Act
    message = module.run(force=True)

    # Assert
    assert message == "催促を送信（強制）"
    assert len(sent) == 1


def test_通知はバナーが出る優先度で送られる(job) -> None:
    # Arrange: 優先度1と2はntfyが無音配信するため、催促には使ってはいけない
    module, _, sent = job

    # Act
    module.run()

    # Assert
    assert sent[0]["priority"] >= module.PRIORITY_DEFAULT
    assert module.PRIORITY_DEFAULT >= 3


def test_予定の中身は通知に載せない(job, monkeypatch) -> None:
    # Arrange: 通知は外のサーバー（ntfy.sh）を通るので、予定の題名は送らない
    module, _, sent = job

    # Act
    module.run()

    # Assert
    assert sent[0]["body"] == "今日は何をしましたか。ひとことでも大丈夫です。"
