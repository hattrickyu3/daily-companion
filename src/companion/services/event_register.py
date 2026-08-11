"""確認済みの予定をGoogleカレンダーに登録する.

登録したあとローカルのキャッシュにも入れる。
次の朝の同期を待たないと画面に出ない、という状態を避けるため。
キャッシュへの反映に失敗しても、Google側には入っているので成功として扱う。
"""

from __future__ import annotations

import logging

from companion.db.connection import session
from companion.db.events import upsert_events
from companion.integrations.calendar_google import (
    CalendarEvent,
    create_all_day_event,
    create_event,
    default_calendar_id,
)
from companion.services.calendar_sync import sync_window
from companion.services.event_draft import EventDraft
from companion.services.schedule import clear_cache

log = logging.getLogger(__name__)


def register(draft: EventDraft) -> str:
    """予定を登録し、作成された予定のIDを返す."""
    calendar_id = default_calendar_id()

    recurrence = draft.recurrence_rules()

    if draft.is_all_day:
        event_id = create_all_day_event(
            calendar_id,
            draft.title,
            draft.day,
            end_day=draft.end_day,
            location=draft.location,
            recurrence=recurrence,
        )
    else:
        event_id = create_event(
            calendar_id,
            draft.title,
            draft.starts_at(),
            draft.ends_at(),
            location=draft.location,
            recurrence=recurrence,
        )

    log.info("event created: %s (%s)", draft.title, draft.label())
    if draft.repeats:
        _resync(draft)
    else:
        _cache_locally(draft, event_id)
    return event_id


def _resync(draft: EventDraft) -> None:
    """繰り返し予定を作ったあと、その回分をGoogleから取り直す.

    繰り返し予定はGoogle側で個別の回に展開され、それぞれ別のIDを持つ。
    作成時に返るのは大元のIDなので、そのまま控えても1回目しか画面に出ない。
    取り直しに失敗してもGoogle側には入っているので、成功扱いにする
    （翌朝の定期同期で埋まる）。
    """
    try:
        with session() as conn:
            sync_window(conn, draft.day)
        clear_cache()
    except Exception as exc:  # noqa: BLE001 - Google側には入っているので失敗扱いにしない
        log.warning("could not resync after creating a recurring event: %s", exc)


def _cache_locally(draft: EventDraft, event_id: str) -> None:
    """作った予定をローカルにも入れ、すぐ画面に出るようにする."""
    event = CalendarEvent(
        uid=event_id,
        title=draft.title,
        event_date=draft.day,
        starts_at=None if draft.is_all_day else draft.starts_at(),
        ends_at=None if draft.is_all_day else draft.ends_at(),
        location=draft.location,
        is_all_day=draft.is_all_day,
        end_date=draft.last_day if draft.spans_days else None,
    )
    try:
        with session() as conn:
            upsert_events(conn, [event])
        # 今日の予定はAPI直取りをキャッシュしているので、そちらも捨てる。
        clear_cache()
    except Exception as exc:  # noqa: BLE001 - Google側には入っているので失敗扱いにしない
        log.warning("could not cache created event locally: %s", exc)
