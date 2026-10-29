"""議事録（会議と音声の切れ端）の永続化.

音声ファイルの保存は services/minutes_audio.py が受け持ち、ここは文字と状態だけを扱う。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

RECORDING = "recording"
FINISHING = "finishing"
DONE = "done"
FAILED = "failed"

SEGMENT_PENDING = "pending"
SEGMENT_DONE = "done"
SEGMENT_FAILED = "failed"


@dataclass(frozen=True)
class Meeting:
    """1回分の会議."""

    id: int
    title: str
    started_at: datetime
    status: str
    participants: str = ""
    # 場面（seminar / lecture / meeting / solo）と、授業のときの授業フォルダ. services/minutes_scenes.py を参照.
    kind: str = "seminar"
    course: str = ""
    ended_at: datetime | None = None
    draft: dict[str, str] = field(default_factory=dict)
    final_text: str | None = None
    polished: bool = False
    hand_edited: bool = False
    note_path: str | None = None
    error: str | None = None
    # 一覧用の集計. 単体で取ったときは 0 のまま.
    segment_count: int = 0
    total_ms: int = 0


@dataclass(frozen=True)
class Segment:
    """音声の切れ端1つと、その文字起こし."""

    meeting_id: int
    seg_id: int
    duration_ms: int
    status: str
    text: str = ""
    error: str | None = None
    drafted: bool = False


def _parse_draft(raw: str | None) -> dict[str, str]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _to_meeting(row: sqlite3.Row) -> Meeting:
    keys = row.keys()
    return Meeting(
        id=row["id"],
        title=row["title"],
        participants=row["participants"],
        kind=row["kind"],
        course=row["course"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        status=row["status"],
        draft=_parse_draft(row["draft"]),
        final_text=row["final_text"],
        polished=bool(row["polished"]),
        hand_edited=bool(row["hand_edited"]),
        note_path=row["note_path"],
        error=row["error"],
        segment_count=row["segment_count"] if "segment_count" in keys else 0,
        total_ms=row["total_ms"] if "total_ms" in keys else 0,
    )


def _to_segment(row: sqlite3.Row) -> Segment:
    return Segment(
        meeting_id=row["meeting_id"],
        seg_id=row["seg_id"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        text=row["text"],
        error=row["error"],
        drafted=bool(row["drafted"]),
    )


def create_meeting(
    conn: sqlite3.Connection, title: str, started_at: datetime, *,
    participants: str = "", kind: str = "seminar", course: str = "",
) -> int:
    """会議を作って id を返す."""
    cursor = conn.execute(
        "INSERT INTO meetings (title, participants, kind, course, started_at) VALUES (?, ?, ?, ?, ?)",
        (title, participants, kind, course, started_at.isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def delete_meeting(conn: sqlite3.Connection, meeting_id: int) -> None:
    """会議を消す. 録音の有無は呼び出し側で確かめること（音声のある会議は消さない約束）."""
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))


def set_participants(conn: sqlite3.Connection, meeting_id: int, participants: str) -> None:
    conn.execute("UPDATE meetings SET participants = ? WHERE id = ?", (participants, meeting_id))


def set_scene(conn: sqlite3.Connection, meeting_id: int, kind: str, course: str) -> bool:
    """場面と授業を変える. 次の清書（やり直し）から、その場面の書き方と置き場所になる.

    録音中・清書中は変えない（変えたら True）. 状態の確認と書き換えを1つの文で行い、
    その間に「清書をやり直す」が割り込んでも、清書中の会議の場面が変わらないようにする。
    """
    cursor = conn.execute(
        "UPDATE meetings SET kind = ?, course = ? WHERE id = ? AND status IN (?, ?)",
        (kind, course, meeting_id, DONE, FAILED),
    )
    return cursor.rowcount > 0


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> Meeting | None:
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return _to_meeting(row) if row else None


def list_meetings(conn: sqlite3.Connection, limit: int = 100) -> list[Meeting]:
    """新しい順の一覧. 切れ端の数と録音の長さも付ける."""
    rows = conn.execute(
        """
        SELECT m.*, COUNT(s.seg_id) AS segment_count, COALESCE(SUM(s.duration_ms), 0) AS total_ms
        FROM meetings m LEFT JOIN meeting_segments s ON s.meeting_id = m.id
        GROUP BY m.id ORDER BY m.started_at DESC, m.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_to_meeting(row) for row in rows]


def set_status(
    conn: sqlite3.Connection, meeting_id: int, status: str, *, error: str | None = None
) -> None:
    conn.execute("UPDATE meetings SET status = ?, error = ? WHERE id = ?", (status, error, meeting_id))


def set_ended(conn: sqlite3.Connection, meeting_id: int, ended_at: datetime) -> None:
    """終了時刻を記録する. 清書のやり直しでは最初の終了時刻を残す."""
    conn.execute(
        "UPDATE meetings SET ended_at = COALESCE(ended_at, ?) WHERE id = ?",
        (ended_at.isoformat(timespec="seconds"), meeting_id),
    )


def save_draft(conn: sqlite3.Connection, meeting_id: int, draft: dict[str, str]) -> None:
    conn.execute(
        "UPDATE meetings SET draft = ? WHERE id = ?",
        (json.dumps(draft, ensure_ascii=False), meeting_id),
    )


def save_final(
    conn: sqlite3.Connection, meeting_id: int, text: str, note_path: str | None, *, polished: bool
) -> None:
    conn.execute(
        "UPDATE meetings SET final_text = ?, note_path = ?, polished = ?, hand_edited = 0 WHERE id = ?",
        (text, note_path, int(polished), meeting_id),
    )


def set_note_path(conn: sqlite3.Connection, meeting_id: int, note_path: str) -> None:
    """書き出したノートの場所だけを入れる（清書の中身はそのまま）."""
    conn.execute("UPDATE meetings SET note_path = ? WHERE id = ?", (note_path, meeting_id))


def save_hand_edit(conn: sqlite3.Connection, meeting_id: int, text: str) -> None:
    """手で直した議事録を保存する."""
    conn.execute(
        "UPDATE meetings SET final_text = ?, hand_edited = 1 WHERE id = ?", (text, meeting_id)
    )


def add_segment(conn: sqlite3.Connection, meeting_id: int, seg_id: int, duration_ms: int) -> bool:
    """切れ端を登録する. 送り直しで既にあれば何もせず False."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO meeting_segments (meeting_id, seg_id, duration_ms) VALUES (?, ?, ?)",
        (meeting_id, seg_id, duration_ms),
    )
    return cursor.rowcount > 0


def get_segment(conn: sqlite3.Connection, meeting_id: int, seg_id: int) -> Segment | None:
    row = conn.execute(
        "SELECT * FROM meeting_segments WHERE meeting_id = ? AND seg_id = ?", (meeting_id, seg_id)
    ).fetchone()
    return _to_segment(row) if row else None


def finish_segment(conn: sqlite3.Connection, meeting_id: int, seg_id: int, text: str) -> None:
    conn.execute(
        "UPDATE meeting_segments SET status = ?, text = ?, error = NULL WHERE meeting_id = ? AND seg_id = ?",
        (SEGMENT_DONE, text, meeting_id, seg_id),
    )


def fill_pending_segment(conn: sqlite3.Connection, meeting_id: int, seg_id: int, text: str) -> bool:
    """文字起こしが済んでいない切れ端に本文を入れて済みにする. 済みにしたら True."""
    cursor = conn.execute(
        "UPDATE meeting_segments SET status = ?, text = ?, error = NULL WHERE meeting_id = ? AND seg_id = ? AND status = ?",
        (SEGMENT_DONE, text, meeting_id, seg_id, SEGMENT_PENDING),
    )
    return cursor.rowcount > 0


def fail_segment(conn: sqlite3.Connection, meeting_id: int, seg_id: int, error: str) -> None:
    conn.execute(
        "UPDATE meeting_segments SET status = ?, error = ? WHERE meeting_id = ? AND seg_id = ?",
        (SEGMENT_FAILED, error, meeting_id, seg_id),
    )


def list_segments(conn: sqlite3.Connection, meeting_id: int) -> list[Segment]:
    rows = conn.execute(
        "SELECT * FROM meeting_segments WHERE meeting_id = ? ORDER BY seg_id", (meeting_id,)
    ).fetchall()
    return [_to_segment(row) for row in rows]


def previous_text(conn: sqlite3.Connection, meeting_id: int, seg_id: int) -> str:
    """直前の切れ端の文字起こし. 文字起こしの続きを自然にするために使う."""
    row = conn.execute(
        """
        SELECT text FROM meeting_segments
        WHERE meeting_id = ? AND seg_id < ? AND status = ? AND text != ''
        ORDER BY seg_id DESC LIMIT 1
        """,
        (meeting_id, seg_id, SEGMENT_DONE),
    ).fetchone()
    return row["text"] if row else ""


def count_pending(conn: sqlite3.Connection, meeting_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM meeting_segments WHERE meeting_id = ? AND status = ?",
        (meeting_id, SEGMENT_PENDING),
    ).fetchone()
    return int(row["n"])


def pending_segments(conn: sqlite3.Connection) -> list[Segment]:
    """全会議の、文字起こしが済んでいない切れ端. 再起動後の追いかけに使う."""
    rows = conn.execute(
        "SELECT * FROM meeting_segments WHERE status = ? ORDER BY meeting_id, seg_id",
        (SEGMENT_PENDING,),
    ).fetchall()
    return [_to_segment(row) for row in rows]


def undrafted_segments(conn: sqlite3.Connection, meeting_id: int) -> list[Segment]:
    """文字起こし済みで、まだ議事録に反映していない切れ端."""
    rows = conn.execute(
        "SELECT * FROM meeting_segments WHERE meeting_id = ? AND status = ? AND drafted = 0 ORDER BY seg_id",
        (meeting_id, SEGMENT_DONE),
    ).fetchall()
    return [_to_segment(row) for row in rows]


def mark_drafted(conn: sqlite3.Connection, meeting_id: int, seg_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE meeting_segments SET drafted = 1 WHERE meeting_id = ? AND seg_id = ?",
        [(meeting_id, seg_id) for seg_id in seg_ids],
    )


def retry_failed_segments(conn: sqlite3.Connection, meeting_id: int) -> list[int]:
    """失敗した切れ端を文字起こし待ちに戻し、その seg_id を返す."""
    rows = conn.execute(
        "SELECT seg_id FROM meeting_segments WHERE meeting_id = ? AND status = ?",
        (meeting_id, SEGMENT_FAILED),
    ).fetchall()
    conn.execute(
        "UPDATE meeting_segments SET status = ?, error = NULL WHERE meeting_id = ? AND status = ?",
        (SEGMENT_PENDING, meeting_id, SEGMENT_FAILED),
    )
    return [row["seg_id"] for row in rows]
