"""議事録の辞書の画面（人の名前・授業の用語と、消すつなぎ言葉）.

/minutes/{meeting_id} より先に登録すること（app.py）。でないと /minutes/words が会議の番号として読まれる。
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from companion.config import get_config
from companion.db import words as words_db
from companion.services.minutes_scenes import LECTURE, clean_course, course_name, list_courses
from companion.services.minutes_words import DEFAULT_FILLERS, clean_word_input, load_fillers, parse_fillers
from companion.web import minutes_routes as base

router = APIRouter(prefix="/minutes/words")

MAX_FILLERS_LENGTH = 2000


@router.get("", response_class=HTMLResponse)
def words_page(request: Request, error: str = "") -> HTMLResponse:
    base.get_runtime()  # テーブルをそろえる
    with base.session() as conn:
        words = words_db.list_words(conn)
        fillers = load_fillers(conn)
        suggestions = words_db.list_suggestions(conn)
    return base.templates.TemplateResponse(
        request, "minutes_words.html",
        {
            "words": [(w, course_name(w.course) or "すべて") for w in words],
            "suggestions": [(s, course_name(s.course) or "すべて") for s in suggestions],
            "courses": list_courses(get_config().notes_dir),
            "fillers": "、".join(fillers),
            "default_fillers": "、".join(DEFAULT_FILLERS),
            "error": error,
        },
        status_code=400 if error else 200,
    )


@router.post("", response_model=None)
def add_word(
    request: Request, word: str = Form(""), variants: str = Form(""), course: str = Form("")
) -> HTMLResponse | RedirectResponse:
    """語を足す. 同じ語（同じ授業）があれば、間違えやすい書き方を置き換える."""
    cleaned = clean_word_input(word, variants)
    if cleaned is None:
        return words_page(request, error="正しい書き方を入れてください。")
    # 授業は保管庫の授業の一覧にあるものだけ受ける（空ならどの場面でも使う）.
    scope = clean_course(get_config().notes_dir, LECTURE, course)
    with base.session() as conn:
        words_db.save_word(conn, cleaned[0], cleaned[1], scope)
    return RedirectResponse("/minutes/words", status_code=303)


@router.post("/{word_id}/delete")
def delete_word(word_id: int) -> RedirectResponse:
    with base.session() as conn:
        words_db.delete_word(conn, word_id)
    return RedirectResponse("/minutes/words", status_code=303)


@router.post("/suggestions/{suggestion_id}/accept")
def accept_suggestion(suggestion_id: int) -> RedirectResponse:
    """手で直したところから拾った候補を、辞書に登録する."""
    with base.session() as conn:
        words_db.accept_suggestion(conn, suggestion_id)
    return RedirectResponse("/minutes/words", status_code=303)


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: int) -> RedirectResponse:
    """候補を「いらない」にする. 同じ組は二度と出さない."""
    with base.session() as conn:
        words_db.dismiss_suggestion(conn, suggestion_id)
    return RedirectResponse("/minutes/words", status_code=303)


@router.post("/fillers")
def save_fillers(fillers: str = Form("")) -> RedirectResponse:
    """消すつなぎ言葉の一覧を保存する. 空にすると何も消さない."""
    cleaned = parse_fillers(fillers[:MAX_FILLERS_LENGTH])
    with base.session() as conn:
        words_db.set_setting(conn, words_db.FILLERS_KEY, "、".join(cleaned))
    return RedirectResponse("/minutes/words", status_code=303)
