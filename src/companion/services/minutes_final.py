"""録音が終わったあとの清書と、Obsidianの保管庫への書き出し.

Sonnet 5.5 に文字起こしの全文と録音中の下書きを渡し、議事録を清書させる。
清書に失敗しても、下書きと文字起こしでノートは作っておく（あとで「清書をやり直す」）。

清書の見出しと書き出す場所は場面で変わる（services/minutes_scenes.py）。ゼミなら MINUTES_DIR の中に:
    2026-10-02 ゼミ.md              議事録
    2026-10-02 ゼミ 文字起こし.md    文字起こしの全文
    ゼミ議事録まとめ.md              一覧（無ければ作る）
ひとりメモは書き出さない。
録音（m4a）は保管庫の外の RECORDINGS_DIR に置く。保管庫はOneDriveの中にあり、容量が足りなくなるため（2026-10-15）。
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from companion.db.meetings import Meeting, Segment
from companion.integrations.claude_runner import run_claude
from companion.integrations.diarization import SpeakerTurn, speaker_for
from companion.integrations.whisper import TimedText
from companion.services.minutes_draft import render_markdown
from companion.services.minutes_scenes import NoteTarget, Scene, course_name, get_scene, scene_label
from companion.services.minutes_speakers import parse_participants, speaker_label

log = logging.getLogger(__name__)

# 別名の "sonnet" は Sonnet 5 を指すので、5.5 は名前で指定する（2026-10-02 確認）.
POLISH_MODEL = "claude-sonnet-5-5"
POLISH_TIMEOUT_SEC = 900
WEEKDAYS = "月火水木金土日"
# ファイル名に使えない・Obsidianのリンクを壊す文字（保管庫の CLAUDE.md の決まり）.
UNSAFE_NAME_CHARS = re.compile(r'[#^\[\]|\\/:*?"<>]')

_POLISH_HEAD = """あなたは会話や会議の記録を清書する係です。
文字起こしの全文と、録音中に書いた下書きを渡すので、読みやすい記録に仕上げてください。
"""

_POLISH_RULES = """守ること:
- 文字起こしに無いことは書かない。下書きと文字起こしが食い違えば文字起こしを優先する。
- 文字起こしには聞き間違いがある。文脈から明らかな誤りは直してよい。聞き取れず意味が通らない所は「（聞き取り不明）」と書く。
- 使ってよいのは見出し・箇条書き・太字・表だけ。HTML・Mermaid・コードブロックは使わない。
"""

_POLISH_SPEAKERS = """話者について:
- 文字起こしの「話者1:」などは、声で聞き分けた番号。同じ番号は同じ人だが、聞き分けを間違うこともある。
- 参加者の名前が渡されたら、会話の中身（名前を呼ばれて返事をした、自己紹介した、など）から番号と名前を結び付ける。
  根拠のない当て推量はしない。結び付けられない人は「話者1」のまま書く。
- 議事録の本文では、わかる人は名前で書く（例: 田中さんが報告した）。
- 応答のいちばん最後の行に、番号と名前の対応を次の形で1行だけ書く（わからない人は「不明」）:
  @@話者 話者1=田中, 話者2=不明
"""


def polish_system_prompt(scene: Scene) -> str:
    """場面ごとの、清書係への指示. ひとりメモは1人で話しているので、話者の結び付けは頼まない."""
    parts = [
        _POLISH_HEAD,
        f"場面: {scene.label}\n会議らしくない話（雑談など）では、決まったこと・やることを無理に作らない。\n",
        "出力はMarkdownの本文だけ。題名（# の見出し）は書かず、「## 要点」から始める。\n"
        f"見出しはこの順で、中身がない見出しは省く:\n{scene.polish_headings}\n",
        _POLISH_RULES,
    ]
    if scene.diarize:
        parts.append(_POLISH_SPEAKERS)
    return "\n".join(parts)


# 場面を指定しないとき（ゼミ）の指示.
POLISH_SYSTEM_PROMPT = polish_system_prompt(get_scene(None))


@dataclass(frozen=True)
class NoteFiles:
    """書き出したファイルの場所."""

    minutes: Path
    transcript: Path
    audio: Path | None


def transcript_lines(segments: list[Segment], tz: ZoneInfo) -> list[str]:
    """文字起こしを「[時:分:秒] 本文」の行にする. 失敗した切れ端は印を付けて残す."""
    lines = []
    for s in segments:
        stamp = f"{datetime.fromtimestamp(s.seg_id / 1000, tz):%H:%M:%S}"
        if s.status == "failed":
            lines.append(f"[{stamp}] （文字起こしに失敗）")
        elif s.text:
            lines.append(f"[{stamp}] {s.text}")
    return lines


def timed_lines(
    timed: list[TimedText],
    offsets: list[tuple[float, int]],
    tz: ZoneInfo,
    turns: list[SpeakerTurn] | None = None,
) -> list[str]:
    """録音を通しで文字にした結果を「[時:分:秒] 本文」の行にする.

    つないだ録音には切れ端の間の空白が入っていないので、位置から時刻を出すには
    その位置を含む切れ端の録り始めの時刻を基準にする。
    """
    starts = [position for position, _ in offsets]
    lines = []
    for item in timed:
        position, seg_id = _segment_at(starts, offsets, item.start)
        moment = datetime.fromtimestamp(seg_id / 1000 + (item.start - position), tz)
        speaker = speaker_for(item.start, item.end, turns) if turns else None
        who = f"{speaker_label(speaker)}: " if speaker is not None else ""
        lines.append(f"[{moment:%H:%M:%S}] {who}{item.text}")
    return lines


def texts_by_segment(timed: list[TimedText], offsets: list[tuple[float, int]]) -> dict[int, str]:
    """録音を通しで文字にした結果を、切れ端ごとの文字起こしに分ける. {seg_id: 本文}（話していない切れ端は空）."""
    starts = [position for position, _ in offsets]
    texts: dict[int, list[str]] = {seg_id: [] for _, seg_id in offsets}
    for item in timed:
        if offsets:
            texts[_segment_at(starts, offsets, item.start)[1]].append(item.text)
    return {seg_id: " ".join(parts) for seg_id, parts in texts.items()}


def _segment_at(starts: list[float], offsets: list[tuple[float, int]], position: float) -> tuple[float, int]:
    """つないだ録音の position 秒目を含む切れ端の (始まる位置, seg_id)."""
    index = max(bisect_right(starts, position) - 1, 0)
    return offsets[index] if offsets else (0.0, 0)


def polish(meeting: Meeting, transcript: str, terms: tuple[str, ...] = ()) -> str:
    """Sonnet 5.5 に清書させる. terms は辞書の語（正しい書き方）. 失敗すれば ClaudeError."""
    scene = get_scene(meeting.kind)
    draft = render_markdown(meeting.draft, scene.sections) or "（下書きなし）"
    names = parse_participants(meeting.participants)
    course = f"授業: {course_name(meeting.course)}\n" if meeting.course else ""
    glossary = f"用語（正しい書き方。文字起こしで崩れていたらこの書き方に直す）: {'、'.join(terms)}\n" if terms else ""
    prompt = (
        f"会議: {meeting.title}（{meeting.started_at:%Y-%m-%d %H:%M}〜）\n{course}"
        f"参加者: {'、'.join(names) if names else '（入力なし）'}\n{glossary}\n"
        f"# 録音中に書いた下書き\n{draft}\n\n"
        f"# 文字起こしの全文\n{transcript}\n"
    )
    result = run_claude(
        prompt, system_prompt=polish_system_prompt(scene), model=POLISH_MODEL, timeout_sec=POLISH_TIMEOUT_SEC
    )
    return result.text.strip()


def safe_name(text: str) -> str:
    return UNSAFE_NAME_CHARS.sub("", text).strip() or "会議"


def note_stem(meeting: Meeting, folder: Path, existing: str | None) -> str:
    """議事録ノートのファイル名（拡張子なし）. 同じ日に同じ題名があれば番号を付ける.

    清書のやり直しでは、前に書き出したファイル名をそのまま使う（リンクを切らないため）。
    場面を変えて置き場所が移るときも、移った先に同じ名前が無ければ同じ名前にする。
    """
    if existing:
        previous = Path(existing)
        if previous.parent == folder or not (folder / previous.name).exists():
            return previous.stem
    base = f"{meeting.started_at:%Y-%m-%d} {safe_name(meeting.title)}"
    stem, number = base, 2
    while (folder / f"{stem}.md").exists():
        stem, number = f"{base} ({number})", number + 1
    return stem


def _duration_label(total_ms: int) -> str:
    minutes = total_ms // 60000
    return f"{minutes // 60}時間{minutes % 60}分" if minutes >= 60 else f"{minutes}分"


def build_minutes_note(
    meeting: Meeting, stem: str, target: NoteTarget, body: str, *,
    total_ms: int, audio_path: Path | None, polished: bool,
) -> str:
    """議事録ノートの全文. 頭の箇条書き（場面・授業・参加者）は、Obsidianで検索して探すときの手がかりにもなる."""
    start = meeting.started_at
    end = f"〜{meeting.ended_at:%H:%M}" if meeting.ended_at else ""
    lines = [
        f"# {stem}",
        f"← [[{target.index_name}]]",
        "",
        f"- 日時：{start:%Y-%m-%d}（{WEEKDAYS[start.weekday()]}）{start:%H:%M}{end}（録音 {_duration_label(total_ms)}）",
        f"- 場面：{scene_label(meeting)}",
    ]
    if target.course_note:
        lines.append(f"- 授業：[[{target.course_note}]]")
    if meeting.participants:
        lines.append(f"- 参加者：{meeting.participants}")
    lines.append(f"- 文字起こし：[[{stem} 文字起こし]]")
    if audio_path is not None:
        # 保管庫の外なのでObsidianのリンクにはできない. 場所だけ書く.
        lines.append(f"- 録音：PCの `{audio_path}`")
    lines += ["", "> [!note] この議事録について", "> 録音の文字起こしからAI（Claude）が作ったもの。聞き間違いが混ざっていることがある。"]
    if not polished:
        lines += [
            "",
            "> [!warning] 清書できていません",
            "> 清書（Sonnet 5.5）に失敗したので、録音中の下書きをそのまま置いている。アプリの議事録画面から「清書をやり直す」を押すと書き直す。",
        ]
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def build_transcript_note(stem: str, lines: list[str]) -> str:
    body = "\n\n".join(lines) if lines else "（文字起こしなし）"
    return f"# {stem} 文字起こし\n← [[{stem}]]\n\n{body}\n"


def add_to_index(index_path: Path, stem: str, intro: str = "ゼミの議事録の一覧。") -> None:
    """一覧ノートにリンクを足す. 既にあれば何もしない. 無ければ作る."""
    link = f"- [[{stem}]]"
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8")
        if link in text.splitlines():
            return
        index_path.write_text(text.rstrip("\n") + "\n" + link + "\n", encoding="utf-8")
    else:
        index_path.write_text(f"# {index_path.stem}\n\n{intro}\n\n{link}\n", encoding="utf-8")


def remove_notes(note_path: Path) -> None:
    """前に書き出した議事録と文字起こしを消し、同じフォルダの一覧ノートからリンクを外す.

    場面を変えて置き場所が移ったとき（ひとりメモに変えて保管庫から出すときも）に使う。
    中身はアプリの中に残っているので、移った先にはいつでも書き出し直せる。
    """
    stem, folder = note_path.stem, note_path.parent
    note_path.unlink(missing_ok=True)
    (folder / f"{stem} 文字起こし.md").unlink(missing_ok=True)
    link = f"- [[{stem}]]"
    for index_path in folder.glob("*まとめ.md"):
        lines = index_path.read_text(encoding="utf-8").splitlines()
        if link in lines:
            index_path.write_text("\n".join(line for line in lines if line != link) + "\n", encoding="utf-8")


def write_notes(
    target: NoteTarget, meeting: Meeting, *, body: str, polished: bool,
    transcript: list[str], total_ms: int, audio_m4a: Path | None, audio_dir: Path | None = None,
) -> NoteFiles:
    """議事録と文字起こしを保管庫に書き出し、録音を audio_dir に置く（None なら録音は置かない）.

    前に別の場所へ書き出していたら（場面を変えた）、新しい場所に書いてから前のものを消す。
    """
    folder = target.folder
    folder.mkdir(parents=True, exist_ok=True)
    stem = note_stem(meeting, folder, meeting.note_path)

    audio_target = None
    if audio_dir is not None and audio_m4a is not None and audio_m4a.exists():
        audio_target = audio_dir / f"{stem}.m4a"
        audio_target.parent.mkdir(parents=True, exist_ok=True)
        audio_target.write_bytes(audio_m4a.read_bytes())

    minutes_path = folder / f"{stem}.md"
    transcript_path = folder / f"{stem} 文字起こし.md"
    transcript_path.write_text(build_transcript_note(stem, transcript), encoding="utf-8")
    minutes_path.write_text(
        build_minutes_note(meeting, stem, target, body, total_ms=total_ms,
                           audio_path=audio_target, polished=polished),
        encoding="utf-8",
    )
    add_to_index(folder / f"{target.index_name}.md", stem, target.index_intro)
    if meeting.note_path and Path(meeting.note_path) != minutes_path:
        previous = Path(meeting.note_path)
        # 新しい場所への書き出しは済んでいる. 前のものを消せなくても（Obsidianで開いている等）新しい場所を正にする.
        # ここで失敗扱いにすると、次の清書で移った先に「(2)」の写しがもう1つできてしまう。
        try:
            remove_notes(previous)
            if audio_dir is not None and previous.stem != stem:
                (audio_dir / f"{previous.stem}.m4a").unlink(missing_ok=True)
        except OSError:
            log.exception("could not remove the previous minutes note %s", previous)
    return NoteFiles(minutes=minutes_path, transcript=transcript_path, audio=audio_target)
