"""ひとりメモを、議事録とは別に見せるための読み方.

ひとりメモは議事録（会議）と同じ仕組みで録って清書するが、見る場所は分ける:
    /memos           ひとりメモだけの一覧（要点を並べて、開かなくても中身がわかるようにする）
    /entries/<日付>   その日の日記のページに、その日のひとりメモを出す
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from companion.db.meetings import Meeting
from companion.services.minutes_scenes import SOLO, get_scene

# 一覧に出す要点の行数. 清書の「要点」は3行以内の約束.
MAX_POINTS = 3
MAX_POINT_LENGTH = 120
POINTS_HEADING = "## 要点"


@dataclass(frozen=True)
class MemoRow:
    """一覧に出すひとりメモ1つ."""

    meeting: Meeting
    points: tuple[str, ...]


def is_memo(meeting: Meeting) -> bool:
    return get_scene(meeting.kind).key == SOLO


def memo_points(meeting: Meeting) -> tuple[str, ...]:
    """要点の箇条書き. 清書があれば清書の「## 要点」から、無ければ録音中の下書きの要点から."""
    text = meeting.final_text or ""
    if POINTS_HEADING in text:
        text = text.split(POINTS_HEADING, 1)[1].split("\n## ", 1)[0]
        # 見出しの残り（「（3行以内）」など）は1行目にあるので飛ばす.
        text = text.split("\n", 1)[1] if "\n" in text else ""
    else:
        text = meeting.draft.get("要点", "")
    points = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-・*").strip()
        if not stripped or stripped.startswith("#"):
            continue
        points.append(stripped[:MAX_POINT_LENGTH] + ("…" if len(stripped) > MAX_POINT_LENGTH else ""))
        if len(points) == MAX_POINTS:
            break
    return tuple(points)


def memo_rows(meetings: Iterable[Meeting], *, query: str = "") -> list[MemoRow]:
    """ひとりメモだけを、新しい順のまま一覧の行にする. 言葉があれば題名と中身で絞る."""
    words = query.split()
    rows = []
    for m in meetings:
        if not is_memo(m):
            continue
        haystack = "\n".join([m.title, m.final_text or "", *m.draft.values()])
        if any(word not in haystack for word in words):
            continue
        rows.append(MemoRow(meeting=m, points=memo_points(m)))
    return rows


def memos_on(meetings: Iterable[Meeting], day: date) -> list[MemoRow]:
    """その日に録ったひとりメモ. 日記のページに出すので、朝から順に並べる."""
    rows = [r for r in memo_rows(meetings) if r.meeting.started_at.date() == day]
    return sorted(rows, key=lambda r: r.meeting.started_at)
