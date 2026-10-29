"""議事録の場面（ゼミ・授業・打ち合わせ・ひとりメモ）.

場面によって、記録の見出し（録音中の下書きと清書）と、ノートを書き出す場所が変わる。

    ゼミ        MINUTES_DIR（既定は保管庫の 研究\\ゼミ議事録）
    授業        保管庫の <学期>\\<授業>\\議事録\\
    打ち合わせ  保管庫の _授業以外\\打ち合わせ\\
    ひとりメモ  保管庫の _授業以外\\ひとりメモ\\（2026-10-25に本人が了承. それまではアプリの中だけに置いていた）
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from companion.db.meetings import Meeting
from companion.services.minutes_draft import SECTIONS

SEMINAR = "seminar"
LECTURE = "lecture"
MEETING = "meeting"
SOLO = "solo"

# 授業の記録を置く、授業フォルダの中のフォルダ.
LECTURE_SUBFOLDER = "議事録"
MEETING_FOLDER = Path("_授業以外") / "打ち合わせ"
SOLO_FOLDER = Path("_授業以外") / "ひとりメモ"
# 授業の一覧に出さない、保管庫の一番上のフォルダ（学期ではないもの）.
NON_TERM_FOLDERS = frozenset({"研究"})
NOTES_SUBFOLDER = "ノート"


@dataclass(frozen=True)
class Scene:
    """場面1つぶんの書き方."""

    key: str
    label: str
    default_title: str
    sections: tuple[str, ...]
    # 清書（Sonnet）に渡す、見出しの並びと書き方.
    polish_headings: str
    # 録音中の下書き（Haiku）に渡す、見出しごとの書き方.
    live_rules: str
    # 声で誰が話したかを聞き分けるか. ひとりメモは1人なので聞き分けない.
    diarize: bool = True


_MEETING_HEADINGS = """## 要点（3〜5行）
## 話したこと（話題ごとに ### の小見出しを付けて箇条書き）
## 決まったこと
## やること（表: 誰が / 何を / いつまでに。わからない所は「未定」）
## 次に向けて
## 保留・疑問"""

_MEETING_RULES = """- 話したことは原則 @@+ で書き足す。直前の項目を直すときだけ @@ で全体を置き換える。
- 決まったこと・やること・次に向けては、はっきり決まったこと・頼まれたこと・約束したことだけ書く。
- やることは「誰が・何を・いつまでに」。わからない部分は「（未定）」と書く。
- 保留・疑問には、結論が出なかった話と、答えが出ていない疑問を書く。"""

SCENES: dict[str, Scene] = {
    SEMINAR: Scene(
        key=SEMINAR, label="ゼミ", default_title="ゼミ", sections=SECTIONS,
        polish_headings=_MEETING_HEADINGS, live_rules=_MEETING_RULES,
    ),
    LECTURE: Scene(
        key=LECTURE, label="授業", default_title="授業",
        sections=("要点", "内容", "大事な用語", "テストに出そうな所", "課題・連絡", "疑問"),
        polish_headings="""## 要点（3〜5行）
## 内容（話題ごとに ### の小見出しを付けて箇条書き）
## 大事な用語（表: 用語 / 意味。授業で説明された意味で書く）
## テストに出そうな所（先生が「大事」「テストに出る」「覚えておいて」などと言った所だけ。根拠のないものは書かない）
## 課題・連絡（表: 何を / いつまでに / どこに出す。わからない所は「未定」）
## 疑問（説明が足りなかった所、あとで調べること）""",
        live_rules="""- 内容は原則 @@+ で書き足す。直前の項目を直すときだけ @@ で全体を置き換える。
- 大事な用語は「用語: 意味」の形で、授業で説明された意味を書く。
- テストに出そうな所は、先生が「大事」「テストに出る」「覚えておいて」などと言った所だけ書く。
- 課題・連絡は、課題・提出物・締切・次回の持ち物などの連絡だけ書く。
- 疑問には、説明が足りなかった所や、あとで調べたほうがよい所を書く。""",
    ),
    MEETING: Scene(
        key=MEETING, label="打ち合わせ", default_title="打ち合わせ", sections=SECTIONS,
        polish_headings=_MEETING_HEADINGS, live_rules=_MEETING_RULES,
    ),
    SOLO: Scene(
        key=SOLO, label="ひとりメモ", default_title="ひとりメモ",
        sections=("要点", "考えたこと", "気づき", "次にやること", "迷っていること"),
        polish_headings="""## 要点（3行以内）
## 考えたこと（話題ごとに ### の小見出しを付けて箇条書き。話した本人の言い回しをなるべく残す）
## 気づき
## 次にやること（はっきり「やる」と言ったものだけ）
## 迷っていること（決めきれていないこと、まだ答えが出ていない問い）""",
        live_rules="""- 1人で考えを話しているのを記録する。話した本人の言い回しをなるべく残す。
- 考えたことは原則 @@+ で書き足す。直前の項目を直すときだけ @@ で全体を置き換える。
- 気づきには、話しているうちに見えてきたこと・腑に落ちたことを書く。
- 次にやることは、はっきり「やる」と言ったものだけ書く。
- 迷っていることには、決めきれていないこと・答えが出ていない問いを書く。""",
        diarize=False,
    ),
}


def get_scene(key: str | None) -> Scene:
    """場面. 知らない値（前に作った会議など）はゼミとして扱う."""
    return SCENES.get(key or "", SCENES[SEMINAR])


def clean_kind(raw: str | None) -> str:
    """画面から届いた場面を、知っている値にそろえる."""
    return get_scene((raw or "").strip()).key


# ---- 授業 ----


@dataclass(frozen=True)
class Course:
    """保管庫の授業フォルダ1つ. path は保管庫からの相対（/ 区切り）."""

    path: str
    term: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name}（{self.term}）"


def _visible_dirs(folder: Path) -> list[str]:
    """隠しフォルダと「_」で始まるフォルダ（授業がわからないもの・授業以外）を除いた、中のフォルダの名前."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    return sorted(e.name for e in entries if e.is_dir() and not e.name.startswith((".", "_")))


def list_courses(notes_dir: Path | None) -> list[Course]:
    """保管庫の授業の一覧. 構成は <学期>\\<授業>\\. 学期の新しい順、その中は名前順.

    学期の名前（「2年まで」「3年３Q」）は後の学期ほど大きくなるので、逆順に並べる（ノート画面と同じ）。
    """
    if notes_dir is None:
        return []
    courses = []
    for term in sorted(_visible_dirs(notes_dir), reverse=True):
        if term in NON_TERM_FOLDERS:
            continue
        courses += [Course(path=f"{term}/{name}", term=term, name=name) for name in _visible_dirs(notes_dir / term)]
    return courses


def clean_course(notes_dir: Path | None, kind: str, raw: str | None) -> str:
    """画面から届いた授業を確かめる. 一覧に無いもの（保管庫の外を指すものも）は受けない. 授業以外の場面では空."""
    if kind != LECTURE:
        return ""
    wanted = (raw or "").strip()
    return wanted if any(c.path == wanted for c in list_courses(notes_dir)) else ""


def course_name(course: str) -> str:
    return course.rsplit("/", 1)[-1] if course else ""


def scene_label(meeting: Meeting) -> str:
    """一覧などに出す場面の名前. 授業は授業名も付ける."""
    scene = get_scene(meeting.kind)
    return f"{scene.label}・{course_name(meeting.course)}" if scene.key == LECTURE and meeting.course else scene.label


# ---- 書き出す場所 ----


@dataclass(frozen=True)
class NoteTarget:
    """議事録ノートを書き出す場所."""

    folder: Path
    index_name: str
    index_intro: str
    # 授業のまとめノートの名前（あれば議事録からつなぐ）.
    course_note: str | None = None


class Destinations:
    """会議の場面から、ノートを書き出す場所を決める."""

    def __init__(self, *, minutes_dir: Path | None, notes_dir: Path | None) -> None:
        self._minutes_dir = minutes_dir
        self._notes_dir = notes_dir

    def for_meeting(self, meeting: Meeting) -> NoteTarget | None:
        """書き出す場所. None なら書き出さない（アプリの中にだけ残す）."""
        kind = get_scene(meeting.kind).key
        if kind == SEMINAR:
            folder = self._minutes_dir
            return NoteTarget(folder, f"{folder.name}まとめ", "ゼミの議事録の一覧。") if folder else None
        if self._notes_dir is None:
            return None
        if kind == MEETING:
            return NoteTarget(self._notes_dir / MEETING_FOLDER, "打ち合わせまとめ", "打ち合わせの記録の一覧。")
        if kind == SOLO:
            return NoteTarget(self._notes_dir / SOLO_FOLDER, "ひとりメモまとめ", "ひとりメモの一覧。")
        return self._lecture_target(meeting.course)

    def _lecture_target(self, course: str) -> NoteTarget:
        valid = clean_course(self._notes_dir, LECTURE, course)
        if not valid:
            # 授業が選ばれていない（あとから授業に変えた等）. 授業フォルダの外にまとめて置く.
            return NoteTarget(self._notes_dir / "_授業以外" / "授業の記録", "授業の記録まとめ", "授業を選ばずに作った授業の記録の一覧。")
        name = course_name(valid)
        course_dir = self._notes_dir / Path(valid)
        summary = f"{name}まとめ"
        has_summary = (course_dir / NOTES_SUBFOLDER / f"{summary}.md").exists()
        return NoteTarget(
            course_dir / LECTURE_SUBFOLDER, f"{name} 議事録まとめ", f"{name}の授業の記録の一覧。",
            course_note=summary if has_summary else None,
        )


# ---- 一覧の絞り込み ----


def filter_meetings(
    meetings: Iterable[Meeting], *, kind: str = "", course: str = "", query: str = ""
) -> list[Meeting]:
    """場面・授業・言葉（題名・参加者・議事録の中身）で絞り込む. 空の条件は使わない."""
    words = query.split()
    found = []
    for m in meetings:
        if kind and get_scene(m.kind).key != kind:
            continue
        if course and m.course != course:
            continue
        haystack = "\n".join([m.title, m.participants, m.final_text or "", *m.draft.values()])
        if any(word not in haystack for word in words):
            continue
        found.append(m)
    return found
