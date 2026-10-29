"""議事録の場面（ゼミ・授業・打ち合わせ・ひとりメモ）を後から変える.

minutes_routes.py が長くなったので分けている。会議の読み込みは minutes_routes の関数を使う
（テストで差し替えたものがそのまま効くよう、モジュール経由で呼ぶ）。
"""

from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from companion.db import meetings as db
from companion.web import minutes_routes as base
from companion.web.minutes_scene_form import read_choice

router = APIRouter(prefix="/minutes")


@router.post("/{meeting_id}/scene")
def update_scene(meeting_id: int, kind: str = Form(""), course: str = Form("")) -> RedirectResponse:
    """場面と授業を変える. 次の清書（やり直し）から、その場面の書き方と置き場所になる.

    録音中・清書中は変えない。録音中の下書きを書く係が、始めたときの場面の見出しで書いているため。
    """
    base._load_meeting(meeting_id)
    choice = read_choice(kind, course)
    with base.session() as conn:
        db.set_scene(conn, meeting_id, choice.kind, choice.course)  # 録音中・清書中なら何もしない
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)
