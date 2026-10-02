"""録音が終わったあとの清書と、Obsidianの保管庫への書き出し.

Sonnet 5.5 に文字起こしの全文と録音中の下書きを渡し、議事録を清書させる。
清書に失敗しても、下書きと文字起こしでノートは作っておく（あとで「清書をやり直す」）。

書き出すもの（MINUTES_DIR の中）:
    2026-10-02 ゼミ.md              議事録
    2026-10-02 ゼミ 文字起こし.md    文字起こしの全文
    音声\\2026-10-02 ゼミ.m4a         録音
    ゼミ議事録まとめ.md              一覧（無ければ作る）
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
from companion.services.minutes_speakers import parse_participants, speaker_label

log = logging.getLogger(__name__)

# 別名の "sonnet" は Sonnet 5 を指すので、5.5 は名前で指定する（2026-10-02 確認）.
POLISH_MODEL = "claude-sonnet-5-5"
POLISH_TIMEOUT_SEC = 900
AUDIO_SUBDIR = "音声"
WEEKDAYS = "月火水木金土日"
# ファイル名に使えない・Obsidianのリンクを壊す文字（保管庫の CLAUDE.md の決まり）.
UNSAFE_NAME_CHARS = re.compile(r'[#^\[\]|\\/:*?"<>]')

POLISH_SYSTEM_PROMPT = """あなたは会話や会議の記録を清書する係です。
文字起こしの全文と、録音中に書いた下書きを渡すので、読みやすい記録に仕上げてください。

場面はゼミ・授業・打ち合わせ・家族や友人との会話などいろいろです。
渡す「会議の名前」と話の中身から場面を判断し、それに合った書き方にしてください。
会議らしくない場面（雑談など）では、決まったこと・やることを無理に作らない。

出力はMarkdownの本文だけ。題名（# の見出し）は書かず、「## 要点」から始める。
見出しはこの順で、中身がない見出しは省く:
## 要点（3〜5行）
## 話したこと（話題ごとに ### の小見出しを付けて箇条書き）
## 決まったこと
## やること（表: 誰が / 何を / いつまでに。わからない所は「未定」）
## 次に向けて
## 保留・疑問

守ること:
- 文字起こしに無いことは書かない。下書きと文字起こしが食い違えば文字起こしを優先する。
- 文字起こしには聞き間違いがある。文脈から明らかな誤りは直してよい。聞き取れず意味が通らない所は「（聞き取り不明）」と書く。
- 使ってよいのは見出し・箇条書き・太字・表だけ。HTML・Mermaid・コードブロックは使わない。

話者について:
- 文字起こしの「話者1:」などは、声で聞き分けた番号。同じ番号は同じ人だが、聞き分けを間違うこともある。
- 参加者の名前が渡されたら、会話の中身（名前を呼ばれて返事をした、自己紹介した、など）から番号と名前を結び付ける。
  根拠のない当て推量はしない。結び付けられない人は「話者1」のまま書く。
- 議事録の本文では、わかる人は名前で書く（例: 田中さんが報告した）。
- 応答のいちばん最後の行に、番号と名前の対応を次の形で1行だけ書く（わからない人は「不明」）:
  @@話者 話者1=田中, 話者2=不明
"""


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
        index = max(bisect_right(starts, item.start) - 1, 0)
        position, seg_id = offsets[index] if offsets else (0.0, 0)
        moment = datetime.fromtimestamp(seg_id / 1000 + (item.start - position), tz)
        speaker = speaker_for(item.start, item.end, turns) if turns else None
        who = f"{speaker_label(speaker)}: " if speaker is not None else ""
        lines.append(f"[{moment:%H:%M:%S}] {who}{item.text}")
    return lines


def polish(meeting: Meeting, transcript: str) -> str:
    """Sonnet 5.5 に清書させる. 失敗すれば ClaudeError."""
    draft = render_markdown(meeting.draft) or "（下書きなし）"
    names = parse_participants(meeting.participants)
    prompt = (
        f"会議: {meeting.title}（{meeting.started_at:%Y-%m-%d %H:%M}〜）\n"
        f"参加者: {'、'.join(names) if names else '（入力なし）'}\n\n"
        f"# 録音中に書いた下書き\n{draft}\n\n"
        f"# 文字起こしの全文\n{transcript}\n"
    )
    result = run_claude(prompt, system_prompt=POLISH_SYSTEM_PROMPT, model=POLISH_MODEL, timeout_sec=POLISH_TIMEOUT_SEC)
    return result.text.strip()


def safe_name(text: str) -> str:
    return UNSAFE_NAME_CHARS.sub("", text).strip() or "会議"


def note_stem(meeting: Meeting, folder: Path, existing: str | None) -> str:
    """議事録ノートのファイル名（拡張子なし）. 同じ日に同じ題名があれば番号を付ける.

    清書のやり直しでは、前に書き出したファイル名をそのまま使う（リンクを切らないため）。
    """
    if existing:
        return Path(existing).stem
    base = f"{meeting.started_at:%Y-%m-%d} {safe_name(meeting.title)}"
    stem, number = base, 2
    while (folder / f"{stem}.md").exists():
        stem, number = f"{base} ({number})", number + 1
    return stem


def _duration_label(total_ms: int) -> str:
    minutes = total_ms // 60000
    return f"{minutes // 60}時間{minutes % 60}分" if minutes >= 60 else f"{minutes}分"


def build_minutes_note(
    meeting: Meeting, stem: str, index_name: str, body: str, *,
    total_ms: int, has_audio: bool, polished: bool,
) -> str:
    """議事録ノートの全文."""
    start = meeting.started_at
    end = f"〜{meeting.ended_at:%H:%M}" if meeting.ended_at else ""
    lines = [
        f"# {stem}",
        f"← [[{index_name}]]",
        "",
        f"- 日時：{start:%Y-%m-%d}（{WEEKDAYS[start.weekday()]}）{start:%H:%M}{end}（録音 {_duration_label(total_ms)}）",
        f"- 文字起こし：[[{stem} 文字起こし]]",
    ]
    if has_audio:
        lines.append(f"- 録音：[[{AUDIO_SUBDIR}/{stem}.m4a|{stem}.m4a]]")
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


def add_to_index(index_path: Path, stem: str) -> None:
    """一覧ノートにリンクを足す. 既にあれば何もしない. 無ければ作る."""
    link = f"- [[{stem}]]"
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8")
        if link in text.splitlines():
            return
        index_path.write_text(text.rstrip("\n") + "\n" + link + "\n", encoding="utf-8")
    else:
        index_path.write_text(f"# {index_path.stem}\n\nゼミの議事録の一覧。\n\n{link}\n", encoding="utf-8")


def write_notes(
    folder: Path, meeting: Meeting, *, body: str, polished: bool,
    transcript: list[str], total_ms: int, audio_m4a: Path | None,
) -> NoteFiles:
    """議事録・文字起こし・録音を保管庫に書き出す."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = note_stem(meeting, folder, meeting.note_path)
    index_name = f"{folder.name}まとめ"

    audio_target = None
    if audio_m4a is not None and audio_m4a.exists():
        audio_target = folder / AUDIO_SUBDIR / f"{stem}.m4a"
        audio_target.parent.mkdir(parents=True, exist_ok=True)
        audio_target.write_bytes(audio_m4a.read_bytes())

    minutes_path = folder / f"{stem}.md"
    transcript_path = folder / f"{stem} 文字起こし.md"
    transcript_path.write_text(build_transcript_note(stem, transcript), encoding="utf-8")
    minutes_path.write_text(
        build_minutes_note(meeting, stem, index_name, body, total_ms=total_ms,
                           has_audio=audio_target is not None, polished=polished),
        encoding="utf-8",
    )
    add_to_index(folder / f"{index_name}.md", stem)
    return NoteFiles(minutes=minutes_path, transcript=transcript_path, audio=audio_target)
