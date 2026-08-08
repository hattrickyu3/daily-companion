"""Claudeの応答を構造化データに変換する部分のテスト.

Claudeの出力は外部から来る不定形データなので、
壊れた形で来ても落ちないこと・原文が守られることを重点的に確認する。
"""

from __future__ import annotations

from companion.integrations.claude_runner import extract_json_block
from companion.services.diary import MAX_LIST_ITEMS, MAX_TAGS, parse_structure


def test_正常なJSONを構造化データにできる() -> None:
    # Arrange
    payload = {
        "summary": "実装を進めた1日",
        "done": ["カレンダー連携", "テスト作成"],
        "insights": ["先に疎通確認をすると早い"],
        "carry_over": "明日はWeb画面",
        "mood": "充実",
        "tags": ["開発", "daily-companion"],
    }

    # Act
    structured = parse_structure(payload)

    # Assert
    assert structured is not None
    assert structured.summary == "実装を進めた1日"
    assert structured.done == ("カレンダー連携", "テスト作成")
    assert structured.carry_over == "明日はWeb画面"
    assert structured.tags == ("開発", "daily-companion")


def test_summaryがなければ構造化しない() -> None:
    # Arrange: 要約が抜けた応答は使わず、原文だけを残す判断にする
    payload = {"done": ["何か"], "tags": ["開発"]}

    # Act / Assert
    assert parse_structure(payload) is None


def test_summaryが空文字なら構造化しない() -> None:
    assert parse_structure({"summary": "   "}) is None


def test_配列でない値が来ても空になる() -> None:
    # Arrange: Claudeが文字列で返してしまったケース
    payload = {"summary": "要約", "done": "カレンダー連携", "tags": None}

    # Act
    structured = parse_structure(payload)

    # Assert
    assert structured is not None
    assert structured.done == ()
    assert structured.tags == ()


def test_件数の上限を超えたら切り詰める() -> None:
    # Arrange
    payload = {
        "summary": "要約",
        "done": [f"項目{i}" for i in range(20)],
        "tags": [f"タグ{i}" for i in range(20)],
    }

    # Act
    structured = parse_structure(payload)

    # Assert
    assert structured is not None
    assert len(structured.done) == MAX_LIST_ITEMS
    assert len(structured.tags) == MAX_TAGS


def test_重複した項目は取り除かれる() -> None:
    # Arrange
    payload = {"summary": "要約", "tags": ["開発", "開発", " 開発 "]}

    # Act
    structured = parse_structure(payload)

    # Assert
    assert structured is not None
    assert structured.tags == ("開発",)


def test_nullの申し送りはNoneになる() -> None:
    # Arrange
    payload = {"summary": "要約", "carry_over": None, "mood": ""}

    # Act
    structured = parse_structure(payload)

    # Assert
    assert structured is not None
    assert structured.carry_over is None
    assert structured.mood is None


def test_コードフェンス付きの応答からJSONを取り出せる() -> None:
    # Arrange: 指示しても前置きやフェンスが付いてくることがある
    text = '整理しました。\n```json\n{"summary": "要約"}\n```\n以上です。'

    # Act
    payload = extract_json_block(text)

    # Assert
    assert payload == {"summary": "要約"}


def test_JSONでない応答はNoneになる() -> None:
    assert extract_json_block("すみません、うまく整理できませんでした。") is None
