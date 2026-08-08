"""朝のブリーフィング生成のテスト.

Claudeが落ちても毎朝必ず何かが届くこと、
昨日の記録漏れがClaudeの文面に依存せず必ず伝わることを確認する。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from companion.db.briefings import get_briefing, save_briefing
from companion.db.events import upsert_events
from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
from companion.integrations.calendar_google import CalendarEvent
from companion.integrations.claude_runner import ClaudeError, ClaudeResult
from companion.services import briefing

TODAY = date(2026, 8, 8)
YESTERDAY = TODAY - timedelta(days=1)


def make_event(uid: str, title: str, hour: int = 10) -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title=title,
        event_date=TODAY,
        starts_at=datetime.fromisoformat(f"2026-08-08T{hour:02d}:00:00+09:00"),
        ends_at=datetime.fromisoformat(f"2026-08-08T{hour + 1:02d}:00:00+09:00"),
        location=None,
        is_all_day=False,
    )


def fake_claude(text: str):
    return lambda prompt, **kwargs: ClaudeResult(text=text, session_id=None)


def test_claudeの本文がそのまま使われる(conn, monkeypatch) -> None:
    # Arrange
    save_raw_text(conn, YESTERDAY, "昨日は実装した")
    monkeypatch.setattr(briefing, "run_claude", fake_claude("今日はゼミが10時から。"))

    # Act
    body = briefing.generate(conn, TODAY)

    # Assert
    assert body == "今日はゼミが10時から。"


def test_claudeが落ちても予定を並べたブリーフィングが出る(conn, monkeypatch) -> None:
    # Arrange
    upsert_events(conn, [make_event("a", "ゼミ")])
    save_raw_text(conn, YESTERDAY, "昨日は実装した")

    def boom(prompt, **kwargs):
        raise ClaudeError("claude down")

    monkeypatch.setattr(briefing, "run_claude", boom)

    # Act
    body = briefing.generate(conn, TODAY)

    # Assert: 空にはならず、予定は伝わる
    assert "ゼミ" in body
    assert "10:00-11:00" in body


def test_claudeが空文字を返してもフォールバックする(conn, monkeypatch) -> None:
    # Arrange
    upsert_events(conn, [make_event("a", "ゼミ")])
    save_raw_text(conn, YESTERDAY, "昨日は実装した")
    monkeypatch.setattr(briefing, "run_claude", fake_claude("   "))

    # Act
    body = briefing.generate(conn, TODAY)

    # Assert
    assert "ゼミ" in body


def test_昨日の記録がなければ必ず知らせる(conn, monkeypatch) -> None:
    # Arrange: Claudeが触れなくても、こちらで必ず添える
    monkeypatch.setattr(briefing, "run_claude", fake_claude("今日は予定なし。"))

    # Act
    body = briefing.generate(conn, TODAY)

    # Assert
    assert "昨日の記録がまだです" in body


def test_昨日の記録があれば余計な催促をしない(conn, monkeypatch) -> None:
    # Arrange
    save_raw_text(conn, YESTERDAY, "昨日は実装した")
    monkeypatch.setattr(briefing, "run_claude", fake_claude("今日は予定なし。"))

    # Act
    body = briefing.generate(conn, TODAY)

    # Assert
    assert "昨日の記録がまだです" not in body


def test_直近の申し送りを材料に渡す(conn, monkeypatch) -> None:
    # Arrange
    entry_id = save_raw_text(conn, YESTERDAY, "昨日は実装した")
    apply_structure(
        conn, entry_id, StructuredEntry(summary="実装した", carry_over="明日はWeb画面から")
    )

    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text="今日もがんばりましょう。", session_id=None)

    monkeypatch.setattr(briefing, "run_claude", capture)

    # Act
    briefing.generate(conn, TODAY)

    # Assert
    assert "明日はWeb画面から" in captured["prompt"]


def test_予定がなければその旨を材料に渡す(conn, monkeypatch) -> None:
    # Arrange
    captured: dict[str, str] = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return ClaudeResult(text="本文", session_id=None)

    monkeypatch.setattr(briefing, "run_claude", capture)

    # Act
    briefing.generate(conn, TODAY)

    # Assert
    assert "（予定なし）" in captured["prompt"]


def test_予定は上限件数までしか渡さない(conn) -> None:
    # Arrange
    events = [make_event(f"e{i}", f"予定{i}", hour=8 + i) for i in range(10)]

    # Act
    formatted = briefing.format_events(events)

    # Assert
    assert len(formatted.splitlines()) == briefing.MAX_EVENTS


def test_ブリーフィングを保存して読み出せる(conn) -> None:
    # Arrange / Act
    save_briefing(conn, TODAY, "今日はゼミが10時から。")

    # Assert
    saved = get_briefing(conn, TODAY)
    assert saved is not None
    assert saved.body == "今日はゼミが10時から。"


def test_同じ日に再実行すると上書きされる(conn) -> None:
    # Arrange
    save_briefing(conn, TODAY, "最初の内容")

    # Act
    save_briefing(conn, TODAY, "作り直した内容")

    # Assert
    saved = get_briefing(conn, TODAY)
    assert saved is not None
    assert saved.body == "作り直した内容"
