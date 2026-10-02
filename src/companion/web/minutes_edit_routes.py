"""清書した議事録を手で直す画面.

minutes_routes.py が300行を超えたので分けている。会議の読み込みや議事録係は
minutes_routes の関数を使う（テストで差し替えたものがそのまま効くよう、モジュール経由で呼ぶ）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from companion.clock import now_local
from companion.db import meetings as db
from companion.services.minutes_edit import (
    EditError,
    backup_body,
    current_body,
    normalize_body,
    write_note_body,
)
from companion.web import minutes_routes as base

router = APIRouter(prefix="/minutes")


def _editable(meeting: db.Meeting) -> bool:
    """清書が済んだ会議だけ直せる. 録音中・清書中は直しても上書きされてしまう."""
    return meeting.status in (db.DONE, db.FAILED) and meeting.final_text is not None


def _render(request: Request, meeting: db.Meeting, body: str, error: str = "") -> HTMLResponse:
    return base.templates.TemplateResponse(
        request, "minutes_edit.html",
        {"meeting": meeting, "body": body, "error": error, "has_note": bool(meeting.note_path)},
        status_code=400 if error else 200,
    )


@router.get("/{meeting_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, meeting_id: int) -> HTMLResponse:
    meeting = base._load_meeting(meeting_id)
    if not _editable(meeting):
        return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)
    return _render(request, meeting, current_body(meeting.note_path, meeting.final_text))


@router.post("/{meeting_id}/edit", response_class=HTMLResponse)
def save_edit(request: Request, meeting_id: int, body: str = Form("")) -> HTMLResponse:
    """直した本文を、アプリの中とObsidianのノートの両方に保存する. 直す前の本文は控えておく."""
    meeting = base._load_meeting(meeting_id)
    if not _editable(meeting):
        return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)
    try:
        cleaned = normalize_body(body)
    except EditError as exc:
        return _render(request, meeting, body, str(exc))

    previous = current_body(meeting.note_path, meeting.final_text)
    if cleaned == previous:
        return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)
    backup_body(base.get_runtime().audio.meeting_dir(meeting_id), previous, now_local())

    note = Path(meeting.note_path) if meeting.note_path else None
    if note is not None and note.exists():
        try:
            write_note_body(note, cleaned)
        except OSError as exc:
            return _render(request, meeting, body, f"ノートに書き込めませんでした（{exc}）。もう一度保存してください。")
    with base.session() as conn:
        db.save_hand_edit(conn, meeting_id, cleaned)
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)
