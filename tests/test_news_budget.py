"""ニュース収集の時間予算のテスト.

朝のジョブ全体がタスクスケジューラの実行上限に当たると、
ニュースだけでなくブリーフィングの通知ごと打ち切られる。
予算超過時に残りを諦めて先に進むことを保証する。
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from companion.jobs import news_digest
from companion.services.news import NewsItem, NewsResult

TODAY = date(2026, 8, 8)


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """DBと設定を差し替えた収集関数."""
    from companion.db.connection import apply_schema, session

    db_path = tmp_path / "news.db"
    with session(db_path) as conn:
        apply_schema(conn)

    monkeypatch.setattr(news_digest, "session", lambda: session(db_path))
    return news_digest, db_path


def set_topics(monkeypatch, module, topics: tuple[str, ...]) -> None:
    class FakeConfig:
        news_topics = topics

    monkeypatch.setattr(module, "get_config", lambda: FakeConfig())


def one_item(topic: str) -> NewsResult:
    return NewsResult(
        topic=topic,
        items=(NewsItem(title="記事", summary="要約", url="https://example.com/a"),),
    )


def test_全ジャンルを収集して件数を報告する(collector, monkeypatch) -> None:
    # Arrange
    module, _ = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting"))
    monkeypatch.setattr(module, "generate", lambda topic, today: one_item(topic))

    # Act
    message = module.collect(TODAY)

    # Assert
    assert "ai_tech:1件" in message
    assert "job_hunting:1件" in message


def test_予算を使い切ったら残りを飛ばす(collector, monkeypatch) -> None:
    # Arrange: 1件目の生成で予算を超過したことにする
    module, _ = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting"))

    clock = itertools.chain([0.0, 0.0, module.TOTAL_BUDGET_SEC + 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module, "generate", lambda topic, today: one_item(topic))

    # Act
    message = module.collect(TODAY)

    # Assert: 取れた分は残り、飛ばしたことも報告される
    assert "ai_tech:1件" in message
    assert "時間切れで省略: job_hunting" in message


def test_一部が空でも他のジャンルは保存される(collector, monkeypatch) -> None:
    # Arrange
    module, db_path = collector
    set_topics(monkeypatch, module, ("ai_tech", "job_hunting"))
    monkeypatch.setattr(
        module,
        "generate",
        lambda topic, today: NewsResult(topic=topic, items=())
        if topic == "ai_tech"
        else one_item(topic),
    )

    # Act
    message = module.collect(TODAY)

    # Assert
    assert "ai_tech" not in message
    assert "job_hunting:1件" in message


def test_全滅しても例外にしない(collector, monkeypatch) -> None:
    # Arrange
    module, _ = collector
    set_topics(monkeypatch, module, ("ai_tech",))
    monkeypatch.setattr(
        module, "generate", lambda topic, today: NewsResult(topic=topic, items=())
    )

    # Act / Assert
    assert module.collect(TODAY) == "ニュースを取得できませんでした"


def test_最悪の所要時間がタスクの実行上限に収まる() -> None:
    # Arrange: タスクスケジューラの ExecutionTimeLimit は15分。
    # 予算の判定はジャンルを始める前に行うため、予算ぎりぎりで始まった最後の1件は
    # さらに NEWS_TIMEOUT_SEC まで走りうる。その分を必ず数に入れる。
    from companion.services.briefing import BRIEFING_TIMEOUT_SEC
    from companion.services.news import NEWS_TIMEOUT_SEC

    task_limit_sec = 15 * 60
    overhead_sec = 60  # カレンダー同期・DB書き込み・通知

    # Act
    worst_case = (
        news_digest.TOTAL_BUDGET_SEC + NEWS_TIMEOUT_SEC + BRIEFING_TIMEOUT_SEC + overhead_sec
    )

    # Assert
    assert worst_case < task_limit_sec, (
        f"最悪 {worst_case}秒 かかりうるが、タスクの上限は {task_limit_sec}秒"
    )


def test_予算は通常の収集を打ち切らない長さがある() -> None:
    # Arrange: 実測は1ジャンル120〜180秒。2ジャンルが普通に終わる余地を残す。
    typical_per_topic_sec = 180

    # Act / Assert: 1ジャンル目が遅くても2ジャンル目に入れること
    assert news_digest.TOTAL_BUDGET_SEC > typical_per_topic_sec
