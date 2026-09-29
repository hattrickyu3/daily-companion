"""メール（mail-notifier の判定結果）を見る画面.

app.py が400行に近いので、ノートと同じくこのファイルに分けている。
見るだけの画面で、メールの仕分けや通知は mail-notifier が行う。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from companion.clock import today_local
from companion.config import get_config
from companion.services.mail_inbox import CATEGORIES, MailInbox, MailItem, load

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/mail")


def get_inbox() -> MailInbox | None:
    """設定された結果ファイル. MAIL_RESULTS_FILE が空なら None."""
    return load(get_config().mail_results_file)


def action_items_for(day: date) -> list[MailItem] | None:
    """今日の画面に出す「要対応」メール. 未設定なら None（欄ごと出さない）."""
    inbox = get_inbox()
    return None if inbox is None else inbox.action_items(day)


@router.get("", response_class=HTMLResponse)
def mail_page(request: Request, category: str | None = None) -> HTMLResponse:
    """判定結果の一覧. category で絞り込める（知らない値なら絞り込まない）."""
    inbox = get_inbox()
    active = category if category in CATEGORIES else None
    return templates.TemplateResponse(
        request,
        "mail.html",
        {
            "inbox": inbox,
            "categories": CATEGORIES,
            "active": active,
            "rows": inbox.filtered(active) if inbox else (),
            "today": today_local(),
        },
    )
