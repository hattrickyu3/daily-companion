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
    normalize_draft,
    render_markdown,
)
from companion.services.minutes_editor import build_message, format_lines

TZ = ZoneInfo("Asia/Tokyo")


def test_置き換えと書き足しを読み取る() -> None:
    text = "@@ 決まったこと\n- 中間発表は10月9日\n@@+ 話したこと\n- 実験結果の報告\n"

    ops = parse_ops(text)

    assert [(o.section, o.mode, o.text) for o in ops] == [
        ("決まったこと", REPLACE, "- 中間発表は10月9日"),
        ("話したこと", APPEND, "- 実験結果の報告"),
    ]


def test_変更なしと知らない見出しと前置きは捨てる() -> None:
    text = "わかりました。\n@@ 変更なし\n@@ 感想\n- たのしかった\n"

    assert parse_ops(text) == []


def test_なしだけの中身は空として扱う() -> None:
    ops = parse_ops("@@ 決まったこと\n- （なし）\n@@ 保留・疑問\n- 変更なし")

    assert [o.text for o in ops] == ["", ""]


def test_飾りの付いた見出しも既知の見出しに寄せる() -> None:
    ops = parse_ops("@@ やること（誰が・何を・いつまでに）\n- 田中: 資料作成（未定）")

    assert ops[0].section == "やること"


def test_前の見出しの名前で返されても今の見出しに読み替える() -> None:
    ops = parse_ops("@@ 宿題\n- 田中: 資料作成\n@@+ 話し合ったこと\n- 報告\n@@ 次回\n- 10月9日\n@@ 保留・質問\n- F値")

    assert [o.section for o in ops] == ["やること", "話したこと", "次に向けて", "保留・疑問"]


def test_前の見出しの名前で保存された下書きも読める() -> None:
    old = {"要点": "- a", "話し合ったこと": "- b", "宿題": "- c", "次回": "- d", "保留・質問": "- e"}

    assert normalize_draft(old) == {"要点": "- a", "話したこと": "- b", "やること": "- c", "次に向けて": "- d", "保留・疑問": "- e"}
    assert render_markdown(old).startswith("## 要点\n\n- a\n\n## 話したこと\n\n- b")


def test_届いている途中の書きかけの見出しは中身として扱わない() -> None:
    ops = parse_ops("@@ 要点\n- ゼミ開始\n@@", final=False)

    assert [(o.section, o.text) for o in ops] == [("要点", "- ゼミ開始")]
    assert editing_section(ops) == "要点"


def test_編集を当てても元の下書きは変わらない() -> None:
    draft = {"話したこと": "- 前半の話", "要点": "- 古い要点"}
    ops = parse_ops("@@+ 話したこと\n- 後半の話\n@@ 要点\n- 新しい要点")

    updated = apply_ops(draft, ops)

    assert updated == {"話したこと": "- 前半の話\n- 後半の話", "要点": "- 新しい要点"}
    assert draft == {"話したこと": "- 前半の話", "要点": "- 古い要点"}


def test_空の書き足しでは何も変えない() -> None:
    assert apply_ops({"要点": "- a"}, parse_ops("@@+ 要点\n")) == {"要点": "- a"}


def test_下書きをMarkdownにすると決まった順で並ぶ() -> None:
    text = render_markdown({"決まったこと": "- x", "要点": "- y", "やること": ""})

    assert text == "## 要点\n\n- y\n\n## 決まったこと\n\n- x"


def test_Haikuに渡す発言には時刻が付き無音は飛ばす() -> None:
    seg_id = int(datetime(2026, 10, 2, 18, 30, 5, tzinfo=TZ).timestamp() * 1000)
    segments = [
        Segment(meeting_id=1, seg_id=seg_id, duration_ms=1000, status="done", text="始めます"),
        Segment(meeting_id=1, seg_id=seg_id + 1, duration_ms=1000, status="done", text=""),
    ]

    assert format_lines(segments, TZ) == "[18:30:05] 始めます"


def test_起動し直した直後は会議の名前と今の記録も渡す() -> None:
    fresh = build_message("[18:30:05] 続き", {"要点": "- 前半"}, fresh=True, title="父との会話")
    ongoing = build_message("[18:30:05] 続き", {"要点": "- 前半"}, fresh=False, title="父との会話")

    assert "会議の名前: 父との会話" in fresh
    assert "@@ 要点\n- 前半" in fresh
    assert "これまでの記録" not in ongoing


def test_AIへの指示は選んだ場面の見出しで書かせる() -> None:
    from companion.services.minutes_editor import system_prompt
    from companion.services.minutes_final import polish_system_prompt
    from companion.services.minutes_scenes import LECTURE, SEMINAR, SOLO, get_scene

    for build in (system_prompt, polish_system_prompt):
        seminar, lecture, solo = (build(get_scene(k)) for k in (SEMINAR, LECTURE, SOLO))
        assert "場面: ゼミ" in seminar and "決まったこと" in seminar
        assert "場面: 授業" in lecture and "テストに出そうな所" in lecture and "決まったこと\n" not in lecture
        assert "場面: ひとりメモ" in solo and "考えたこと" in solo
        assert all("宿題" not in p for p in (seminar, lecture, solo))
    # ひとりメモは1人なので、清書で話者の名前を結び付けさせない
    assert "@@話者" not in polish_system_prompt(get_scene(SOLO))
    assert "@@話者" in polish_system_prompt(get_scene(SEMINAR))


def test_場面の見出しに無い下書きも落とさず清書に渡す() -> None:
    from companion.services.minutes_scenes import LECTURE, get_scene

    draft = {"要点": "- 形態素解析", "決まったこと": "- 前の場面で書いた"}

    text = render_markdown(draft, get_scene(LECTURE).sections)

    assert text.index("## 要点") < text.index("## 決まったこと")
    assert "- 前の場面で書いた" in text


def test_授業の下書きでは授業の見出しを受け取る() -> None:
    from companion.services.minutes_scenes import LECTURE, get_scene

    ops = parse_ops("@@+ 大事な用語\n- 形態素: 意味の最小単位\n@@ 決まったこと\n- 無視される", sections=get_scene(LECTURE).sections)

    assert [(op.section, op.text) for op in ops] == [("大事な用語", "- 形態素: 意味の最小単位")]


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
