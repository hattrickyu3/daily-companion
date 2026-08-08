"""ニュース収集の並列化と所要時間のテスト.

朝のジョブ全体がタスクスケジューラの実行上限(15分)に当たると、
ニュースだけでなくブリーフィングの通知ごと打ち切られる。
ジャンルを増やしても上限に収まることを、数字で固定しておく。
"""

from __future__ import annotations

import math
import threading
import time
from datetime import date

import pytest

from companion.jobs import news_digest
from companion.services.news import NewsItem, NewsResult

TODAY = date(2026, 8, 8)
TASK_LIMIT_SEC = 15 * 60


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """DBと設定を差し替えた収集関数."""
    from companion.db.connection import apply_schema, session

    db_path = tmp_path / "news.db"
    with session(db_path) as conn:
        apply_schema(conn)

    monkeypatch.setattr(news_digest, "session", lambda: session(db_path))
    return news_digest


def set_topics(monkeypatch, module, topics: tuple[str, ...]) -> None:
    class FakeConfig:
        news_topics = topics

    monkeypatch.setattr(module, "get_config", lambda: FakeConfig())


def items(count: int) -> tuple[NewsItem, ...]:
    return tuple(
        NewsItem(title=f"記事{i}", summary="要約", url=f"https://example.com/{i}")
        for i in range(count)
    )


def test_全ジャンルを収集して件数を報告する(collector, monkeypatch) -> None:
    # Arrange
    module = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting", "social"))
    monkeypatch.setattr(
        module, "generate", lambda topic, today: NewsResult(topic=topic, items=items(5))
    )

    # Act
    message = module.collect(TODAY)

    # Assert
    assert "ai_tech:5件" in message
    assert "job_hunting:5件" in message
    assert "social:5件" in message


def test_ジャンルは同時に走る(collector, monkeypatch) -> None:
    # Arrange: 3ジャンルが順番ではなく重なって実行されることを確認する
    module = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting", "social"))

    running = 0
    peak = 0
    lock = threading.Lock()

    def slow(topic, today):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return NewsResult(topic=topic, items=items(1))

    monkeypatch.setattr(module, "generate", slow)

    # Act
    module.collect(TODAY)

    # Assert: 逐次なら peak は1のまま
    assert peak > 1


def test_同時実行数に上限がある(collector, monkeypatch) -> None:
    # Arrange: claude -p を無制限に並べるとPCが厳しいので上限を守る
    module = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting", "social", "domestic", "world"))

    running = 0
    peak = 0
    lock = threading.Lock()

    def slow(topic, today):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1
        return NewsResult(topic=topic, items=items(1))

    monkeypatch.setattr(module, "generate", slow)

    # Act
    module.collect(TODAY)

    # Assert
    assert peak <= module.MAX_CONCURRENT


def test_1ジャンルが例外を投げても他は保存される(collector, monkeypatch) -> None:
    # Arrange
    module = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting"))

    def flaky(topic, today):
        if topic == "ai_tech":
            raise RuntimeError("claude down")
        return NewsResult(topic=topic, items=items(2))

    monkeypatch.setattr(module, "generate", flaky)

    # Act
    message = module.collect(TODAY)

    # Assert
    assert "ai_tech" not in message
    assert "job_hunting:2件" in message


def test_一部が空でも他のジャンルは保存される(collector, monkeypatch) -> None:
    # Arrange
    module = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting"))
    monkeypatch.setattr(
        module,
        "generate",
        lambda topic, today: NewsResult(topic=topic, items=())
        if topic == "ai_tech"
        else NewsResult(topic=topic, items=items(1)),
    )

    # Act
    message = module.collect(TODAY)

    # Assert
    assert "ai_tech" not in message
    assert "job_hunting:1件" in message


def test_全滅しても例外にしない(collector, monkeypatch) -> None:
    # Arrange
    module = collector
    set_topics(monkeypatch, module, ("ai_tech",))
    monkeypatch.setattr(
        module, "generate", lambda topic, today: NewsResult(topic=topic, items=())
    )

    # Act / Assert
    assert module.collect(TODAY) == "ニュースを取得できませんでした"


def test_ジャンル未設定でも落ちない(collector, monkeypatch) -> None:
    # Arrange
    module = collector
    set_topics(monkeypatch, module, ())

    # Act / Assert
    assert module.collect(TODAY) == "対象ジャンルが設定されていません"


def test_全ジャンルを選んでも朝のジョブが上限に収まる() -> None:
    # Arrange: 設定できる全ジャンルを有効にした最悪の場合を見る。
    # 同時実行なので、所要時間はジャンル数ではなく「波の数」で決まる。
    from companion.config import VALID_NEWS_TOPICS
    from companion.services.briefing import BRIEFING_TIMEOUT_SEC
    from companion.services.news import NEWS_TIMEOUT_SEC

    overhead_sec = 60  # バックアップ・カレンダー同期・DB書き込み・通知

    # Act
    waves = math.ceil(len(VALID_NEWS_TOPICS) / news_digest.MAX_CONCURRENT)
    worst_case = waves * NEWS_TIMEOUT_SEC + BRIEFING_TIMEOUT_SEC + overhead_sec

    # Assert
    assert worst_case < TASK_LIMIT_SEC, (
        f"最悪 {worst_case}秒 かかりうるが、タスクの上限は {TASK_LIMIT_SEC}秒"
    )
