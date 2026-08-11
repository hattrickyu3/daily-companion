"""FastAPI Webアプリ.

同じWi-Fi内のスマホから開くことを前提にした、JSフレームワークなしのHTML。
日記の保存だけは絶対に失敗させたくないので、原文を保存してから即リダイレクトし、
時間のかかるClaudeの構造化はバックグラウンドで走らせる。
"""

from __future__ import annotations

import logging
from datetime import date, time, timedelta
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
from companion.services.diary import append_entry, save_entry, structure_entry
from companion.integrations.calendar_google import CalendarError
from companion.services.event_draft import EventDraft, Recurrence, build_recurrence
from companion.services.event_draft import parse as parse_event
from companion.services.event_register import register as register_event
from companion.services.calendar_sync import refresh_if_stale as refresh_calendar
from companion.services.month_view import WEEKDAY_LABELS
from companion.services.month_view import build as build_month
from companion.services.month_view import month_bounds, parse_month
from companion.services.news import topic_label
from companion.services.news_history import build as build_news_history
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


def _parse_day(value: str) -> date | None:
    """フォームから来た日付を解釈する. 不正なら None."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_clock(value: str) -> time | None:
    """フォームから来た時刻を解釈する. 空や不正なら None（終日扱い）."""
    if not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _store_and_structure(
    background: BackgroundTasks, day: date, text: str, *, append: bool
) -> RedirectResponse:
    """原文を保存し、全文の構造化を裏で走らせてから詳細画面へ送る.

    構造化は書き足した部分だけでなく、常にその日の全文に対して行う。
    そうしないと要約が一部だけを指すものになってしまう。
    """
    with session() as conn:
        entry_id = append_entry(conn, day, text) if append else save_entry(conn, day, text)
        entry = get_entry(conn, day)

    full_text = entry.raw_text if entry else text
    background.add_task(_structure_in_background, entry_id, day, full_text)
    return RedirectResponse(f"/entries/{day.isoformat()}", status_code=303)


@app.post("/entries")
def save_entry_route(
    background: BackgroundTasks,
    raw_text: str = Form(...),
    entry_date: str = Form(...),
) -> RedirectResponse:
    """日記を全文で保存し直す. 既存の内容は置き換わる."""
    day = _parse_day(entry_date)
    text = raw_text.strip()
    if day is None or not text:
        return RedirectResponse("/", status_code=303)

    return _store_and_structure(background, day, text, append=False)


@app.post("/entries/append")
def append_entry_route(
    background: BackgroundTasks,
    raw_text: str = Form(...),
    entry_date: str = Form(...),
) -> RedirectResponse:
    """日記に書き足す. 既に書いた内容は消さない."""
    day = _parse_day(entry_date)
    text = raw_text.strip()
    if day is None or not text:
        return RedirectResponse("/", status_code=303)

    return _store_and_structure(background, day, text, append=True)


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


@app.get("/events/new", response_class=HTMLResponse)
def new_event_page(request: Request, day: str | None = None) -> HTMLResponse:
    """予定の入力画面. 自然言語で書いてもらう."""
    return templates.TemplateResponse(
        request, "event_new.html", {"day": day or today_local().isoformat()}
    )


@app.post("/events/parse", response_class=HTMLResponse)
def parse_event_route(request: Request, text: str = Form(...)) -> HTMLResponse:
    """入力をClaudeに解釈させ、確認画面を出す. ここではまだ登録しない."""
    written = text.strip()
    if not written:
        return RedirectResponse("/events/new", status_code=303)

    draft = parse_event(written, today_local())
    return templates.TemplateResponse(
        request, "event_confirm.html", {"draft": draft, "original": written}
    )


def _build_repeat(
    freq: str, interval: str, weekdays: list[str], until: str, day: date
) -> Recurrence | None:
    """フォームの入力から繰り返しの規則を組み立てる. 「なし」なら None."""
    payload = {
        "freq": freq,
        "interval": interval,
        "weekdays": weekdays,
        "until": until,
    }
    repeat = build_recurrence(payload)
    # 期限が開始日より前だと1回も発生しない。規則ごと捨てて1回きりにする。
    if repeat is not None and repeat.until is not None and repeat.until < day:
        return Recurrence(freq=repeat.freq, interval=repeat.interval, weekdays=repeat.weekdays)
    return repeat


@app.post("/events/create")
def create_event_route(
    title: str = Form(...),
    day: str = Form(...),
    end_day: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    location: str = Form(""),
    repeat_freq: str = Form(""),
    repeat_interval: str = Form("1"),
    repeat_weekdays: list[str] = Form(default=[]),
    repeat_until: str = Form(""),
) -> RedirectResponse:
    """確認済みの内容でGoogleカレンダーに登録する."""
    target_day = _parse_day(day)
    name = title.strip()
    if target_day is None or not name:
        return RedirectResponse("/events/new?error=input", status_code=303)

    # 最終日が開始日より前なら単日として扱う。入力ミスで予定を落とすより害が小さい。
    last_day = _parse_day(end_day)
    if last_day is not None and last_day <= target_day:
        last_day = None

    draft = EventDraft(
        title=name,
        day=target_day,
        start_time=_parse_clock(start_time),
        end_time=_parse_clock(end_time),
        location=location.strip() or None,
        end_day=last_day,
        repeat=_build_repeat(repeat_freq, repeat_interval, repeat_weekdays, repeat_until, target_day),
    )

    try:
        register_event(draft)
    except CalendarError as exc:
        log.warning("event creation failed: %s", exc)
        return RedirectResponse("/events/new?error=calendar", status_code=303)

    return RedirectResponse(f"/entries/{target_day.isoformat()}", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, month: str | None = None) -> HTMLResponse:
    """月カレンダー. 日記の有無と予定の件数を一覧する."""
    today = today_local()
    year, month_number = parse_month(month, today)

    with session() as conn:
        # 定期同期の範囲外の月を見ても空にならないよう、その月を取り込み直す。
        start, end = month_bounds(year, month_number)
        refresh_calendar(conn, start, end)
        view = build_month(conn, year, month_number, today)

    return templates.TemplateResponse(
        request, "calendar.html", {"view": view, "weekdays": WEEKDAY_LABELS}
    )


@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request, topic: str | None = None) -> HTMLResponse:
    """過去のニュースを表で見る画面."""
    with session() as conn:
        history = build_news_history(conn, topic=topic)

    return templates.TemplateResponse(request, "news.html", {"history": history})


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
