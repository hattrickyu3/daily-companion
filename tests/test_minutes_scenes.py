"""議事録の場面（ゼミ・授業・打ち合わせ・ひとりメモ）と、場面ごとの置き場所のテスト."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from companion.services.minutes_scenes import (
    LECTURE,
    MEETING,
    SEMINAR,
    SOLO,
    Destinations,
    clean_course,
    clean_kind,
    filter_meetings,
    get_scene,
    list_courses,
)

TZ = ZoneInfo("Asia/Tokyo")
START = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "大学"
    for folder in (
        "3年３Q/自然言語処理/ノート", "3年３Q/ウェブデータ処理", "2年まで/統計学/ノート",
        "2年まで/_授業がわからないもの", "研究/ゼミ議事録", "_授業以外/就活", ".obsidian",
    ):
        (root / folder).mkdir(parents=True)
    (root / "3年３Q/自然言語処理/ノート/自然言語処理まとめ.md").write_text("# 自然言語処理まとめ\n", encoding="utf-8")
    return root


def make_meeting(kind: str = SEMINAR, course: str = "", **extra) -> db.Meeting:
    return db.Meeting(id=1, title="記録", started_at=START, status=db.DONE, kind=kind, course=course, **extra)


def test_知らない場面はゼミとして扱う() -> None:
    assert get_scene("lecture").label == "授業"
    assert get_scene("unknown").key == SEMINAR
    assert clean_kind("solo") == SOLO
    assert clean_kind("../evil") == SEMINAR


def test_場面ごとに見出しが違う() -> None:
    assert "決まったこと" in get_scene(SEMINAR).sections
    assert "テストに出そうな所" in get_scene(LECTURE).sections
    assert "考えたこと" in get_scene(SOLO).sections
    assert all(get_scene(k).sections[0] == "要点" for k in (SEMINAR, LECTURE, MEETING, SOLO))


def test_授業の一覧は学期の新しい順で研究や授業以外を含めない(vault: Path) -> None:
    courses = list_courses(vault)

    assert [c.path for c in courses] == ["3年３Q/ウェブデータ処理", "3年３Q/自然言語処理", "2年まで/統計学"]
    assert courses[1].name == "自然言語処理"
    assert courses[1].term == "3年３Q"


def test_保管庫が無ければ授業の一覧は空(tmp_path: Path) -> None:
    assert list_courses(tmp_path / "無い") == []
    assert list_courses(None) == []


def test_一覧に無い授業は受け付けない(vault: Path) -> None:
    assert clean_course(vault, LECTURE, "3年３Q/自然言語処理") == "3年３Q/自然言語処理"
    assert clean_course(vault, LECTURE, "../../Windows") == ""
    assert clean_course(vault, LECTURE, "研究/ゼミ議事録") == ""
    # 授業でない場面では授業を持たない
    assert clean_course(vault, SEMINAR, "3年３Q/自然言語処理") == ""


def test_ゼミは今までどおりの場所に置く(vault: Path) -> None:
    dest = Destinations(minutes_dir=vault / "研究" / "ゼミ議事録", notes_dir=vault)

    target = dest.for_meeting(make_meeting(SEMINAR))

    assert target.folder == vault / "研究" / "ゼミ議事録"
    assert target.index_name == "ゼミ議事録まとめ"


def test_授業はその授業のフォルダの議事録に置き授業のまとめにつなぐ(vault: Path) -> None:
    dest = Destinations(minutes_dir=vault / "研究" / "ゼミ議事録", notes_dir=vault)

    target = dest.for_meeting(make_meeting(LECTURE, "3年３Q/自然言語処理"))

    assert target.folder == vault / "3年３Q" / "自然言語処理" / "議事録"
    assert target.index_name == "自然言語処理 議事録まとめ"
    assert target.course_note == "自然言語処理まとめ"


def test_まとめノートの無い授業ではまとめにつながない(vault: Path) -> None:
    dest = Destinations(minutes_dir=None, notes_dir=vault)

    assert dest.for_meeting(make_meeting(LECTURE, "2年まで/統計学")).course_note is None


def test_打ち合わせは授業以外のフォルダに置く(vault: Path) -> None:
    dest = Destinations(minutes_dir=None, notes_dir=vault)

    target = dest.for_meeting(make_meeting(MEETING))

    assert target.folder == vault / "_授業以外" / "打ち合わせ"
    assert target.index_name == "打ち合わせまとめ"


def test_ひとりメモは授業以外のひとりメモのフォルダに置く(vault: Path) -> None:
    dest = Destinations(minutes_dir=vault / "研究" / "ゼミ議事録", notes_dir=vault)

    target = dest.for_meeting(make_meeting(SOLO))

    assert target.folder == vault / "_授業以外" / "ひとりメモ"
    assert target.index_name == "ひとりメモまとめ"


def test_保管庫が無ければゼミ以外は書き出さない() -> None:
    dest = Destinations(minutes_dir=None, notes_dir=None)

    assert dest.for_meeting(make_meeting(MEETING)) is None
    assert dest.for_meeting(make_meeting(SEMINAR)) is None


def test_場面と授業と言葉で一覧を絞り込む() -> None:
    meetings = [
        make_meeting(SEMINAR, final_text="## 要点\n- 中間発表の日程"),
        make_meeting(LECTURE, "3年３Q/自然言語処理", final_text="## 要点\n- 形態素解析"),
        make_meeting(LECTURE, "3年３Q/ウェブデータ処理"),
        make_meeting(SOLO, participants="自分"),
    ]

    assert len(filter_meetings(meetings)) == 4
    assert filter_meetings(meetings, kind=LECTURE) == meetings[1:3]
    assert filter_meetings(meetings, kind=LECTURE, course="3年３Q/自然言語処理") == [meetings[1]]
    assert filter_meetings(meetings, query="形態素") == [meetings[1]]
    assert filter_meetings(meetings, query="自分") == [meetings[3]]


def test_会議に場面と授業を持たせ後から変えられる(tmp_path: Path) -> None:
    with session(tmp_path / "db.sqlite") as conn:
        apply_schema(conn)
        meeting_id = db.create_meeting(conn, "自然言語処理", START, kind=LECTURE, course="3年３Q/自然言語処理")
        created = db.get_meeting(conn, meeting_id)
        refused = db.set_scene(conn, meeting_id, SOLO, "")  # 録音中は変えない
        db.set_status(conn, meeting_id, db.DONE)
        accepted = db.set_scene(conn, meeting_id, MEETING, "")
        changed = db.get_meeting(conn, meeting_id)
        legacy_id = db.create_meeting(conn, "ゼミ", START)
        legacy = db.get_meeting(conn, legacy_id)

    assert (created.kind, created.course) == (LECTURE, "3年３Q/自然言語処理")
    assert (refused, accepted) == (False, True)
    assert (changed.kind, changed.course) == (MEETING, "")
    assert (legacy.kind, legacy.course) == (SEMINAR, "")


def test_前のノートを消せなくても新しい場所への書き出しは成功にする(vault: Path, monkeypatch) -> None:
    from companion.services import minutes_final

    old_folder = vault / "研究" / "ゼミ議事録"
    old_note = old_folder / "2026-10-24 記録.md"
    old_note.write_text("# 前のノート\n", encoding="utf-8")
    audio_dir = vault.parent / "録音"
    audio_dir.mkdir()
    meeting = make_meeting(MEETING, note_path=str(old_note))
    target = Destinations(minutes_dir=old_folder, notes_dir=vault).for_meeting(meeting)

    def locked(path):
        raise PermissionError("Obsidianで開いている")

    monkeypatch.setattr(minutes_final, "remove_notes", locked)
    files = minutes_final.write_notes(
        target, meeting, body="## 要点\n\n- x", polished=True, transcript=[], total_ms=0, audio_m4a=None, audio_dir=audio_dir,
    )

    assert files.minutes == vault / "_授業以外" / "打ち合わせ" / "2026-10-24 記録.md"
    assert files.minutes.exists()


def test_移った先で名前が変わったら前の名前の録音を消す(vault: Path) -> None:
    from companion.services import minutes_final

    old_folder = vault / "研究" / "ゼミ議事録"
    old_note = old_folder / "2026-10-24 記録.md"
    old_note.write_text("# 前のノート\n", encoding="utf-8")
    new_folder = vault / "_授業以外" / "打ち合わせ"
    new_folder.mkdir(parents=True)
    (new_folder / "2026-10-24 記録.md").write_text("# 別の記録\n", encoding="utf-8")  # 同じ名前がもうある
    audio_dir = vault.parent / "録音"
    audio_dir.mkdir()
    (audio_dir / "2026-10-24 記録.m4a").write_bytes(b"old")
    m4a = vault.parent / "録音.m4a"
    m4a.write_bytes(b"audio")
    meeting = make_meeting(MEETING, note_path=str(old_note))
    target = Destinations(minutes_dir=old_folder, notes_dir=vault).for_meeting(meeting)

    files = minutes_final.write_notes(
        target, meeting, body="## 要点\n\n- x", polished=True, transcript=[], total_ms=0, audio_m4a=m4a, audio_dir=audio_dir,
    )

    assert files.minutes.name == "2026-10-24 記録 (2).md"
    assert (new_folder / "2026-10-24 記録.md").read_text(encoding="utf-8") == "# 別の記録\n"  # 別の記録は触らない
    assert not old_note.exists()
    assert not (audio_dir / "2026-10-24 記録.m4a").exists()
    assert (audio_dir / "2026-10-24 記録 (2).m4a").exists()
