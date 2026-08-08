"""ジャンル設定のテスト.

ラベルと実際に集まるものが食い違わないこと、
ニュース性のないジャンルに「直近1週間」の縛りをかけないことを確認する。
"""

from __future__ import annotations

from datetime import date

from companion.config import VALID_NEWS_TOPICS
from companion.integrations.claude_runner import ClaudeResult
from companion.services import news

TODAY = date(2026, 8, 8)


def capture_prompt(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text='{"items": []}', session_id=None)

    monkeypatch.setattr(news, "run_claude", capture)
    return captured


def test_設定できる全ジャンルにラベルがある() -> None:
    # Arrange / Act / Assert: .env で指定できるのに表示名がない状態を防ぐ
    for topic in VALID_NEWS_TOPICS:
        assert topic in news.TOPIC_LABELS
        assert topic in news.TOPIC_QUERIES


def test_実践知ジャンルのラベルにXを含めない() -> None:
    # Arrange: 実測でXからは1件も取れなかったため、Xを名乗らない
    label = news.topic_label("social")

    # Act / Assert
    assert "X" not in label


def test_件数は5件を要求する(monkeypatch) -> None:
    # Arrange
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("ai_tech", TODAY)

    # Assert
    assert news.MAX_ITEMS == 5
    assert "5件" in captured["prompt"]


def test_ニュース系は直近1週間に絞る(monkeypatch) -> None:
    # Arrange
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("ai_tech", TODAY)

    # Assert
    assert "直近1週間以内" in captured["prompt"]


def test_実践知は期間で絞らない(monkeypatch) -> None:
    # Arrange: ESの書き方のような知見は、古くても役に立つものは役に立つ
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("social", TODAY)

    # Assert
    assert "直近1週間以内" not in captured["prompt"]
    assert "social" in news.EVERGREEN_TOPICS


def test_実践知には探し方の指示を添える(monkeypatch) -> None:
    # Arrange
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("social", TODAY)

    # Assert
    assert "アフィリエイト" in captured["prompt"]
    assert "開けなかったURLは載せないでください" in captured["prompt"]


def test_ニュース系には探し方の指示を混ぜない(monkeypatch) -> None:
    # Arrange
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("ai_tech", TODAY)

    # Assert
    assert "アフィリエイト" not in captured["prompt"]


def test_件数を無理に揃えさせない(monkeypatch) -> None:
    # Arrange: 5件に満たないときに水増しされるほうが困る
    captured = capture_prompt(monkeypatch)

    # Act
    news.generate("job_hunting", TODAY)

    # Assert
    assert "無理に数を揃えるより" in captured["prompt"]
