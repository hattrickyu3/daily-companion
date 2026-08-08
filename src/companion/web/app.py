"""FastAPI Webアプリ.

同じWi-Fi内のスマホから開くことを前提にした、JSフレームワークなしのHTML。
日記の保存だけは絶対に失敗させたくないので、原文を保存してから即リダイレクトし、
時間のかかるClaudeの構造化はバックグラウンドで走らせる。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from companion.clock import today_local
from companion.db.briefings import get_briefing
from companion.db.connection import session
from companion.db.events import list_events_for
from companion.db.jobs import latest_run_per_job, list_recent_runs
from companion.db.news import list_digests_for
from companion.db.repository import (
    get_entry,
    list_all_tags,
    list_entries,
    search_entries,
)
from companion.db.reviews import list_reviews
from companion.services.diary import save_entry, structure_entry
from companion.services.news import topic_label
from companion.services.schedule import get_events_for

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="daily-companion", docs_url=None, redoc_url=None)


def _structure_in_background(entry_id: int, entry_date: date, raw_text: str) -> None:
    """バックグラウンドでClaudeに構造化させる. 失敗しても原文は残る."""
    try:
        with session() as conn:
            structure_entry(conn, entry_id, entry_date, raw_text)
    except Exception:  # noqa: BLE001 - バックグラウンドでの失敗はログのみ
        log.exception("background structuring failed for entry %s", entry_id)


@app.get("/", response_class=HTMLResponse)
def today_page(request: Request) -> HTMLResponse:
    """今日の予定と日記の入力欄."""
    day = today_local()
    with session() as conn:
        entry = get_entry(conn, day)
        recent = list_entries(conn, limit=5)
        briefing = get_briefing(conn, day)
        digests = list_digests_for(conn, day)

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "day": day,
            "events": get_events_for(day),
            "entry": entry,
            "briefing": briefing,
            "digests": digests,
            "topic_label": topic_label,
            "recent": [e for e in recent if e.entry_date != day],
        },
    )


@app.post("/entries")
def save_entry_route(
    background: BackgroundTasks,
    raw_text: str = Form(...),
    entry_date: str = Form(...),
) -> RedirectResponse:
    """日記を保存する. 原文を書いたらすぐ返し、整理は裏で行う."""
    day = date.fromisoformat(entry_date)
    text = raw_text.strip()
    if not text:
        return RedirectResponse("/", status_code=303)

    with session() as conn:
        entry_id = save_entry(conn, day, text)

    background.add_task(_structure_in_background, entry_id, day, text)
    return RedirectResponse(f"/entries/{day.isoformat()}", status_code=303)


@app.get("/entries", response_class=HTMLResponse)
def entries_page(request: Request, tag: str | None = None) -> HTMLResponse:
    """日記の一覧. tag で絞り込める."""
    with session() as conn:
        entries = list_entries(conn, tag=tag)
        tags = list_all_tags(conn)

    return templates.TemplateResponse(
        request, "entries.html", {"entries": entries, "tags": tags, "active_tag": tag}
    )


@app.get("/entries/{entry_date}", response_class=HTMLResponse)
def entry_page(request: Request, entry_date: str) -> HTMLResponse:
    """1日分の詳細. 整理中なら数秒ごとに自動で読み直す."""
    try:
        day = date.fromisoformat(entry_date)
    except ValueError:
        return RedirectResponse("/entries", status_code=303)

    with session() as conn:
        entry = get_entry(conn, day)
        briefing = get_briefing(conn, day)
        # 過去日は同期済みのキャッシュを見る。今日だけは最新をAPIから取る。
        events = get_events_for(day) if day == today_local() else list_events_for(conn, day)

    return templates.TemplateResponse(
        request,
        "entry.html",
        {
            "day": day,
            "entry": entry,
            "briefing": briefing,
            "events": events,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
        },
    )


@app.get("/reviews", response_class=HTMLResponse)
def reviews_page(request: Request) -> HTMLResponse:
    """週次レポートの一覧."""
    with session() as conn:
        reviews = list_reviews(conn)

    return templates.TemplateResponse(request, "reviews.html", {"reviews": reviews})


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    """定期ジョブが動いているかの確認画面. 時刻は日本時間で表示する."""
    with session() as conn:
        latest = latest_run_per_job(conn)
        history = list_recent_runs(conn)

    return templates.TemplateResponse(
        request, "status.html", {"latest": latest, "history": history}
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "") -> HTMLResponse:
    """日記の全文検索."""
    with session() as conn:
        results = search_entries(conn, q) if q.strip() else []

    return templates.TemplateResponse(request, "search.html", {"query": q, "results": results})
