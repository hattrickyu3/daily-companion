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
