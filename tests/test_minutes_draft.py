"""録音中の議事録（Haikuの編集指示の読み取り）と、Haikuとのやり取りのテスト."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from companion.db.meetings import Segment
from companion.integrations.claude_runner import ClaudeError
from companion.integrations.claude_stream import ClaudeStreamSession
from companion.services.minutes_draft import (
    APPEND,
    REPLACE,
    apply_ops,
    editing_section,
    parse_ops,
    render_markdown,
)
from companion.services.minutes_editor import build_message, format_lines

TZ = ZoneInfo("Asia/Tokyo")


def test_置き換えと書き足しを読み取る() -> None:
    text = "@@ 決まったこと\n- 中間発表は10月9日\n@@+ 話し合ったこと\n- 実験結果の報告\n"

    ops = parse_ops(text)

    assert [(o.section, o.mode, o.text) for o in ops] == [
        ("決まったこと", REPLACE, "- 中間発表は10月9日"),
        ("話し合ったこと", APPEND, "- 実験結果の報告"),
    ]


def test_変更なしと知らない見出しと前置きは捨てる() -> None:
    text = "わかりました。\n@@ 変更なし\n@@ 感想\n- たのしかった\n"

    assert parse_ops(text) == []


def test_なしだけの中身は空として扱う() -> None:
    ops = parse_ops("@@ 決まったこと\n- （なし）\n@@ 保留・質問\n- 変更なし")

    assert [o.text for o in ops] == ["", ""]


def test_飾りの付いた見出しも既知の見出しに寄せる() -> None:
    ops = parse_ops("@@ 宿題（誰が・何を・いつまでに）\n- 田中: 資料作成（未定）")

    assert ops[0].section == "宿題"


def test_届いている途中の書きかけの見出しは中身として扱わない() -> None:
    ops = parse_ops("@@ 要点\n- ゼミ開始\n@@", final=False)

    assert [(o.section, o.text) for o in ops] == [("要点", "- ゼミ開始")]
    assert editing_section(ops) == "要点"


def test_編集を当てても元の下書きは変わらない() -> None:
    draft = {"話し合ったこと": "- 前半の話", "要点": "- 古い要点"}
    ops = parse_ops("@@+ 話し合ったこと\n- 後半の話\n@@ 要点\n- 新しい要点")

    updated = apply_ops(draft, ops)

    assert updated == {"話し合ったこと": "- 前半の話\n- 後半の話", "要点": "- 新しい要点"}
    assert draft == {"話し合ったこと": "- 前半の話", "要点": "- 古い要点"}


def test_空の書き足しでは何も変えない() -> None:
    assert apply_ops({"要点": "- a"}, parse_ops("@@+ 要点\n")) == {"要点": "- a"}


def test_下書きをMarkdownにすると決まった順で並ぶ() -> None:
    text = render_markdown({"決まったこと": "- x", "要点": "- y", "宿題": ""})

    assert text == "## 要点\n\n- y\n\n## 決まったこと\n\n- x"


def test_Haikuに渡す発言には時刻が付き無音は飛ばす() -> None:
    seg_id = int(datetime(2026, 10, 2, 18, 30, 5, tzinfo=TZ).timestamp() * 1000)
    segments = [
        Segment(meeting_id=1, seg_id=seg_id, duration_ms=1000, status="done", text="始めます"),
        Segment(meeting_id=1, seg_id=seg_id + 1, duration_ms=1000, status="done", text=""),
    ]

    assert format_lines(segments, TZ) == "[18:30:05] 始めます"


def test_起動し直した直後は今の議事録も渡す() -> None:
    fresh = build_message("[18:30:05] 続き", {"要点": "- 前半"}, fresh=True)
    ongoing = build_message("[18:30:05] 続き", {"要点": "- 前半"}, fresh=False)

    assert "@@ 要点\n- 前半" in fresh
    assert "これまでの議事録" not in ongoing


def _line(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


def test_ストリームの文字の差分を渡し結果で全文を返す() -> None:
    received: list[str] = []
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "@@ 要"}}}
    thinking = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "..."}}}

    assert ClaudeStreamSession._handle_line(_line(delta), received.append) is None
    assert ClaudeStreamSession._handle_line(_line(thinking), received.append) is None
    assert ClaudeStreamSession._handle_line(_line({"type": "result", "result": "@@ 要点"}), received.append) == "@@ 要点"
    assert received == ["@@ 要"]


def test_ストリームがエラーを伝えたら失敗にする() -> None:
    with pytest.raises(ClaudeError):
        ClaudeStreamSession._handle_line(
            _line({"type": "result", "is_error": True, "result": "Please run /login"}), lambda _: None
        )
