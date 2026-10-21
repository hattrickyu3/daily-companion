"""週次レポートのテスト.

材料がない週に、それらしい一般論を書かせないことを重点的に確認する。
"""

from __future__ import annotations

from datetime import date, timedelta

from companion.clock import week_start
from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
from companion.db.reviews import get_review, list_reviews, save_review
from companion.integrations.claude_runner import ClaudeError, ClaudeResult
from companion.services import weekly_review

MONDAY = date(2026, 8, 3)
SUNDAY = date(2026, 8, 9)


def fake_claude(text: str):
    return lambda prompt, **kwargs: ClaudeResult(text=text, session_id=None)


def write_entry(conn, day: date, raw: str, **structure) -> None:
    entry_id = save_raw_text(conn, day, raw)
    if structure:
        apply_structure(conn, entry_id, StructuredEntry(**structure))


def test_週の開始は月曜になる() -> None:
    assert week_start(SUNDAY) == MONDAY
    assert week_start(MONDAY) == MONDAY
    assert week_start(date(2026, 8, 6)) == MONDAY


def test_日記があればレポートを作る(conn, monkeypatch) -> None:
    # Arrange
    write_entry(conn, date(2026, 8, 5), "実装した", summary="実装した")
    monkeypatch.setattr(weekly_review, "run_claude", fake_claude("今週の傾向\n実装が中心。"))

    # Act
    body = weekly_review.generate(conn, MONDAY)

    # Assert
    assert body == "今週の傾向\n実装が中心。"


def test_日記が1件もない週は作らない(conn, monkeypatch) -> None:
    # Arrange: 材料なしで書かせると一般論が出てくるので、そもそも呼ばない
    called: list[str] = []

    def should_not_run(prompt, **kwargs):
        called.append(prompt)
        return ClaudeResult(text="それらしい一般論", session_id=None)

    monkeypatch.setattr(weekly_review, "run_claude", should_not_run)

    # Act
    body = weekly_review.generate(conn, MONDAY)

    # Assert
    assert body is None
    assert called == []


def test_週の外の日記は材料に入れない(conn, monkeypatch) -> None:
    # Arrange
    write_entry(conn, MONDAY - timedelta(days=1), "先週の日曜", summary="先週の話")
    write_entry(conn, SUNDAY + timedelta(days=1), "翌週の月曜", summary="来週の話")
    write_entry(conn, date(2026, 8, 5), "今週の水曜", summary="今週の話")

    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text="本文", session_id=None)

    monkeypatch.setattr(weekly_review, "run_claude", capture)

    # Act
    weekly_review.generate(conn, MONDAY)

    # Assert
    assert "今週の話" in captured["prompt"]
    assert "先週の話" not in captured["prompt"]
    assert "来週の話" not in captured["prompt"]


def test_申し送りを材料に渡す(conn, monkeypatch) -> None:
    # Arrange
    write_entry(
        conn, date(2026, 8, 5), "実装した", summary="実装した", carry_over="テストを書く"
    )
    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text="本文", session_id=None)

    monkeypatch.setattr(weekly_review, "run_claude", capture)

    # Act
    weekly_review.generate(conn, MONDAY)

    # Assert
    assert "テストを書く" in captured["prompt"]


def test_claudeが落ちたらレポートを作らない(conn, monkeypatch) -> None:
    # Arrange
    write_entry(conn, date(2026, 8, 5), "実装した", summary="実装した")

    def boom(prompt, **kwargs):
        raise ClaudeError("claude down")

    monkeypatch.setattr(weekly_review, "run_claude", boom)

    # Act / Assert: 空のレポートを保存するくらいなら作らない
    assert weekly_review.generate(conn, MONDAY) is None


def test_claudeが空文字を返してもレポートを作らない(conn, monkeypatch) -> None:
    # Arrange
    write_entry(conn, date(2026, 8, 5), "実装した", summary="実装した")
    monkeypatch.setattr(weekly_review, "run_claude", fake_claude("   "))

    # Act / Assert
    assert weekly_review.generate(conn, MONDAY) is None


def test_レポートを保存して読み出せる(conn) -> None:
    # Arrange / Act
    save_review(conn, MONDAY, "今週の傾向\n実装が中心。")

    # Assert
    review = get_review(conn, MONDAY)
    assert review is not None
    assert review.week_end == SUNDAY
    assert review.label() == "2026/08/03 - 08/09"


def test_同じ週に再実行すると上書きされる(conn) -> None:
    # Arrange
    save_review(conn, MONDAY, "1回目")

    # Act
    save_review(conn, MONDAY, "2回目")

    # Assert
    assert len(list_reviews(conn)) == 1
    review = get_review(conn, MONDAY)
    assert review is not None
    assert review.body == "2回目"


def test_一覧は新しい週から返る(conn) -> None:
    # Arrange
    save_review(conn, MONDAY - timedelta(days=7), "先週")
    save_review(conn, MONDAY, "今週")

    # Act / Assert
    assert [r.body for r in list_reviews(conn)] == ["今週", "先週"]


def test_通知には振り返りの本文を載せない(monkeypatch) -> None:
    # Arrange: 通知は外のサーバー（ntfy.sh）を通るので、日記から作った本文はWebで読ませる
    from contextlib import nullcontext

    from companion.jobs import weekly_review as job

    sent: list[dict] = []
    monkeypatch.setattr(job, "session", lambda: nullcontext(None))
    monkeypatch.setattr(job, "today_local", lambda: MONDAY)
    monkeypatch.setattr(job, "generate", lambda conn, start: "今週は就活の面接が3つあった。")
    monkeypatch.setattr(job, "save_review", lambda conn, start, body: None)
    monkeypatch.setattr(job, "app_url", lambda path="/": "http://test" + path)
    monkeypatch.setattr(job, "send_notification", lambda body, **kwargs: sent.append({"body": body, **kwargs}))

    # Act
    job.run()

    # Assert
    assert "面接" not in sent[0]["body"]
    assert sent[0]["click_url"] == "http://test/reviews"
