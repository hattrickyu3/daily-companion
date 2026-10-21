"""claude -p の応答の解釈."""

from __future__ import annotations

import pytest

from companion.integrations.claude_runner import (
    AUTH_ERROR_MESSAGE,
    ClaudeError,
    describe_error,
    result_from_payload,
)


def test_returns_the_result_text_of_a_normal_response():
    # Arrange
    payload = {"result": "できました", "session_id": "abc", "is_error": False}

    # Act
    result = result_from_payload(payload)

    # Assert
    assert result.text == "できました"
    assert result.session_id == "abc"


def test_raises_when_the_envelope_reports_an_error():
    # Arrange: 認証切れでも claude はJSON本体を返す
    payload = {
        "is_error": True,
        "result": "Failed to authenticate. API Error: 401 OAuth access token has expired.",
    }

    # Act / Assert
    with pytest.raises(ClaudeError) as excinfo:
        result_from_payload(payload)
    assert AUTH_ERROR_MESSAGE in str(excinfo.value)


def test_keeps_the_original_text_for_an_unknown_error():
    # Arrange

    # Act
    message = describe_error("Something else went wrong")

    # Assert
    assert "Something else went wrong" in message
    assert message != AUTH_ERROR_MESSAGE


def _capture_args(monkeypatch) -> list[list[str]]:
    """claude -p を実際には動かさず、渡す引数だけを控える."""
    import subprocess

    from companion.integrations import claude_runner

    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b'{"result": "ok", "is_error": false}', stderr=b"")

    monkeypatch.setattr(claude_runner, "find_claude_binary", lambda: "claude")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)
    return calls


def test_ツールを指定しなければ一切使わせない(monkeypatch):
    # Arrange: ニュースの記事など、よその人が書いた文章を読ませるので、ファイルを読むツールも渡さない
    from companion.integrations.claude_runner import run_claude

    calls = _capture_args(monkeypatch)

    # Act
    run_claude("要約して")

    # Assert
    args = calls[0]
    assert args[args.index("--tools") + 1] == ""
    assert "--allowedTools" not in args


def test_ツールを指定したらそれだけを許す(monkeypatch):
    # Arrange
    from companion.integrations.claude_runner import run_claude

    calls = _capture_args(monkeypatch)

    # Act
    run_claude("調べて", allowed_tools="WebSearch")

    # Assert
    args = calls[0]
    assert args[args.index("--allowedTools") + 1] == "WebSearch"
    assert "--tools" not in args
