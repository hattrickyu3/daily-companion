"""ntfy への通知のテスト. 実際には送らない."""

from __future__ import annotations

import logging
from dataclasses import replace

import httpx

from companion.config import get_config
from companion.integrations import notify

TOPIC = "secret-topic-1234567890"


def test_トピック名をログに残さない(monkeypatch, caplog) -> None:
    # Arrange: トピック名を知っていれば誰でも通知を読めるので、ログにも書かない
    config = replace(get_config(), ntfy_topic=TOPIC, ntfy_server="https://ntfy.example")
    monkeypatch.setattr(notify, "get_config", lambda: config)
    posted: list[str] = []

    def fake_post(url, **_kwargs):
        posted.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    # Act
    with caplog.at_level(logging.INFO, logger=notify.__name__):
        notify.send_notification("できました", title="お知らせ")

    # Assert
    assert posted == [f"https://ntfy.example/{TOPIC}"]
    assert "notification sent" in caplog.text
    assert TOPIC not in caplog.text
