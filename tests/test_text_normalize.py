"""Claude出力の正規化のテスト.

画面はプレーンテキストとして表示するため、マークダウンの見出し記号が
そのまま見えてしまう。プロンプトで禁じても付いてくるので、
表示前に必ず落とすことを保証する。
"""

from __future__ import annotations

from companion.integrations.claude_runner import strip_markdown_headings


def test_見出し記号を落とす() -> None:
    # Arrange
    text = "# 今週の傾向\n実装が中心だった。"

    # Act / Assert
    assert strip_markdown_headings(text) == "今週の傾向\n実装が中心だった。"


def test_複数階層の見出しも落とす() -> None:
    assert strip_markdown_headings("### 見出し") == "見出し"


def test_箇条書きは残す() -> None:
    # Arrange: 「- 」は読みやすさに寄与するので消さない
    text = "よかったこと\n- 日記アプリを作成した\n- テストを書いた"

    # Act / Assert
    assert strip_markdown_headings(text) == text


def test_本文中のシャープは消さない() -> None:
    # Arrange: 行頭でなければ見出しではない
    text = "C#の勉強をした"

    # Act / Assert
    assert strip_markdown_headings(text) == text


def test_空行を潰さない() -> None:
    # Arrange: 段落の区切りは残す
    text = "今週の傾向\n\n実装が中心だった。"

    # Act / Assert
    assert strip_markdown_headings(text) == text


def test_記号がなければそのまま返す() -> None:
    assert strip_markdown_headings("今日はゼミが10時から。") == "今日はゼミが10時から。"


def test_ブリーフィングにも適用されている(conn, monkeypatch) -> None:
    # Arrange
    from companion.db.repository import save_raw_text
    from companion.integrations.claude_runner import ClaudeResult
    from companion.services import briefing
    from datetime import date, timedelta

    today = date(2026, 8, 8)
    save_raw_text(conn, today - timedelta(days=1), "昨日は実装した")
    monkeypatch.setattr(
        briefing,
        "run_claude",
        lambda prompt, **kwargs: ClaudeResult(text="# 今日の予定\nゼミが10時から。", session_id=None),
    )

    # Act
    body = briefing.generate(conn, today)

    # Assert
    assert body.startswith("今日の予定")


def test_週次レポートにも適用されている(conn, monkeypatch) -> None:
    # Arrange
    from companion.db.repository import save_raw_text
    from companion.integrations.claude_runner import ClaudeResult
    from companion.services import weekly_review
    from datetime import date

    monday = date(2026, 8, 3)
    save_raw_text(conn, date(2026, 8, 5), "実装した")
    monkeypatch.setattr(
        weekly_review,
        "run_claude",
        lambda prompt, **kwargs: ClaudeResult(text="# 今週の傾向\n実装が中心。", session_id=None),
    )

    # Act
    body = weekly_review.generate(conn, monday)

    # Assert
    assert body is not None
    assert body.startswith("今週の傾向")
