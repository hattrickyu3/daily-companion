"""Googleカレンダーの読み書き（サービスアカウント方式）.

サービスアカウントを使う理由は、無人で毎朝動くジョブだから。
OAuthのユーザー認証と違ってブラウザでの再同意もリフレッシュトークンの失効もなく、
一度カレンダーを共有すれば動き続ける。

前提: 自分のGoogleカレンダーを、サービスアカウントのメールアドレスに
「予定の変更権限」で共有しておくこと。共有されていないカレンダーは一切見えない。

APIの失敗はアプリを止めない（fetch_events_safe を使うこと）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from companion.config import GOOGLE_CALENDAR_SCOPES, get_config

log = logging.getLogger(__name__)

MAX_EVENTS_PER_REQUEST = 250


class CalendarError(Exception):
    """カレンダー操作に失敗したときに送出する."""


@dataclass(frozen=True)
class CalendarEvent:
    """1件の予定."""

    uid: str
    title: str
    event_date: date
    starts_at: datetime | None
    ends_at: datetime | None
    location: str | None
    is_all_day: bool
    # 最終日（その日を含む）。単日の予定では None。
    # Google APIの end は終日予定だと翌日を指す（排他的）が、
    # ここでは「最終日」に直して持つ。画面に出すのは人間が読む日付なので、
    # 排他的な値をアプリ内で持ち回ると必ずどこかで1日ずれる。
    end_date: date | None = None

    @property
    def last_date(self) -> date:
        """最終日. 単日なら開始日と同じ."""
        return self.end_date or self.event_date

    @property
    def spans_days(self) -> bool:
        """複数日にまたがるか."""
        return self.last_date > self.event_date

    def time_label(self) -> str:
        """一覧表示用の時刻ラベル."""
        if self.spans_days:
            span = f"{self.event_date:%m/%d}〜{self.last_date:%m/%d}"
            if self.is_all_day or self.starts_at is None:
                return f"{span} 終日"
            return f"{span} {self.starts_at:%H:%M}-{self.ends_at:%H:%M}" if self.ends_at else span
        if self.is_all_day or self.starts_at is None:
            return "終日"
        if self.ends_at is None:
            return self.starts_at.strftime("%H:%M")
        return f"{self.starts_at:%H:%M}-{self.ends_at:%H:%M}"


def _build_service():
    """認証済みのCalendar APIクライアントを返す."""
    config = get_config()
    key_file = config.google_service_account_file

    if not key_file.exists():
        raise CalendarError(
            f"サービスアカウントの鍵ファイルが見つかりません: {key_file}\n"
            "Google Cloudでダウンロードした JSON をこのパスに置いてください。"
        )

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(key_file), scopes=GOOGLE_CALENDAR_SCOPES
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except (GoogleAuthError, ValueError) as exc:
        raise CalendarError(f"サービスアカウントの認証に失敗しました: {exc}") from exc


def service_account_email() -> str:
    """鍵ファイルからサービスアカウントのメールアドレスを読む. 共有設定の案内用."""
    config = get_config()
    key_file = config.google_service_account_file
    if not key_file.exists():
        raise CalendarError(f"鍵ファイルが見つかりません: {key_file}")

    import json

    try:
        return json.loads(key_file.read_text(encoding="utf-8")).get("client_email", "")
    except (json.JSONDecodeError, OSError) as exc:
        raise CalendarError(f"鍵ファイルを読めません: {exc}") from exc


def list_calendars() -> list[tuple[str, str]]:
    """アクセスできるカレンダーの (ID, 表示名) 一覧を返す.

    GOOGLE_CALENDAR_IDS が設定されていれば、そのIDを直接引く。
    サービスアカウントは共有の招待を承諾しないため calendarList には何も並ばないことが多く、
    ID指定のほうが確実だからである。未設定のときだけ calendarList を見る。
    """
    service = _build_service()
    configured = get_config().google_calendar_ids

    if configured:
        found: list[tuple[str, str]] = []
        for calendar_id in configured:
            try:
                info = service.calendars().get(calendarId=calendar_id).execute()
            except HttpError as exc:
                raise CalendarError(
                    f"カレンダー {calendar_id} にアクセスできません: {exc}\n"
                    "IDが正しいか、サービスアカウントに共有されているか確認してください。"
                ) from exc
            found.append((calendar_id, info.get("summary", "(名前なし)")))
        return found

    try:
        response = service.calendarList().list().execute()
    except HttpError as exc:
        raise CalendarError(f"カレンダー一覧の取得に失敗しました: {exc}") from exc

    return [(item["id"], item.get("summary", "(名前なし)")) for item in response.get("items", [])]


def _target_calendar_ids(service) -> list[str]:
    """読み取り対象のカレンダーIDを決める.

    .env で明示されていればそれを使い、なければ calendarList に載っているものを対象にする。
    """
    configured = get_config().google_calendar_ids
    if configured:
        return list(configured)

    try:
        response = service.calendarList().list().execute()
    except HttpError as exc:
        raise CalendarError(f"カレンダー一覧の取得に失敗しました: {exc}") from exc
    return [item["id"] for item in response.get("items", [])]


def _parse_endpoint(raw: dict) -> tuple[datetime | None, date | None, bool]:
    """APIの start / end を (日時, 日付, 終日か) に分解する.

    APIはカレンダー側のタイムゾーンでオフセットを返すため、必ず設定のタイムゾーンに
    変換してから使う。カレンダーがUTC設定のままでも表示時刻がずれないようにするため。
    """
    if "dateTime" in raw:
        moment = datetime.fromisoformat(raw["dateTime"]).astimezone(get_config().timezone)
        return moment, moment.date(), False
    if "date" in raw:
        # 終日予定は時刻を持たないので、変換せずそのまま日付として扱う。
        return None, date.fromisoformat(raw["date"]), True
    return None, None, False


def _to_event(raw: dict) -> CalendarEvent | None:
    """APIのレスポンス1件を CalendarEvent に変換する. 変換できなければ None."""
    starts_at, start_date, is_all_day = _parse_endpoint(raw.get("start", {}))
    if start_date is None:
        return None
    ends_at, end_date, end_is_all_day = _parse_endpoint(raw.get("end", {}))

    # 終日予定の end は翌日を指す（排他的）ので、1日戻して最終日にする。
    if end_date is not None and end_is_all_day:
        end_date -= timedelta(days=1)
    elif ends_at is not None:
        end_date = ends_at.date()

    return CalendarEvent(
        uid=raw.get("id", ""),
        title=raw.get("summary", "(件名なし)"),
        event_date=start_date,
        starts_at=starts_at,
        ends_at=ends_at,
        location=raw.get("location"),
        is_all_day=is_all_day,
        end_date=end_date if end_date and end_date > start_date else None,
    )


def fetch_events(start: date, end: date) -> list[CalendarEvent]:
    """指定期間の予定を取得する. 失敗時は CalendarError.

    繰り返し予定は singleEvents=True で個別の予定に展開される。
    """
    config = get_config()
    service = _build_service()

    time_min = datetime.combine(start, datetime.min.time(), tzinfo=config.timezone).isoformat()
    time_max = datetime.combine(end, datetime.max.time(), tzinfo=config.timezone).isoformat()

    events: list[CalendarEvent] = []
    for calendar_id in _target_calendar_ids(service):
        try:
            response = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=MAX_EVENTS_PER_REQUEST,
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("calendar %s fetch failed: %s", calendar_id, exc)
            continue

        for raw in response.get("items", []):
            event = _to_event(raw)
            if event is not None:
                events.append(event)

    events.sort(key=lambda e: (e.event_date, e.starts_at.timestamp() if e.starts_at else -1))
    return events


def fetch_events_safe(start: date, end: date) -> list[CalendarEvent]:
    """予定を取得する. 失敗しても例外を投げず空リストを返す（縮退動作）."""
    try:
        return fetch_events(start, end)
    except (CalendarError, HttpError) as exc:
        log.warning("calendar fetch failed, continuing without events: %s", exc)
        return []


def default_calendar_id() -> str:
    """書き込み先のカレンダーID.

    .env で明示されていればその先頭、なければ共有されている最初のカレンダー。
    書き込み先が決まらない状態で黙って別の場所に作らないよう、見つからなければ失敗させる。
    """
    configured = get_config().google_calendar_ids
    if configured:
        return configured[0]

    calendars = list_calendars()
    if not calendars:
        raise CalendarError(
            "書き込み先のカレンダーが見つかりません。"
            ".env の GOOGLE_CALENDAR_IDS を設定してください。"
        )
    return calendars[0][0]


def create_all_day_event(
    calendar_id: str,
    title: str,
    day: date,
    *,
    end_day: date | None = None,
    location: str | None = None,
    recurrence: list[str] | None = None,
) -> str:
    """終日予定を1件作成し、そのIDを返す.

    end_day は最終日（その日を含む）。合宿のように複数日にまたがる予定で使う。
    Google Calendar APIの終日予定は end が翌日になる（排他的）ため、+1日して渡す。
    recurrence は RRULE の一覧（例: ["RRULE:FREQ=WEEKLY;BYDAY=SU"]）。
    """
    service = _build_service()
    last_day = max(end_day or day, day)
    body: dict = {
        "summary": title,
        "start": {"date": day.isoformat()},
        "end": {"date": (last_day + timedelta(days=1)).isoformat()},
    }
    if location:
        body["location"] = location
    if recurrence:
        body["recurrence"] = recurrence

    try:
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
    except HttpError as exc:
        raise CalendarError(
            f"予定の作成に失敗しました: {exc}\n"
            "カレンダーの共有権限が「予定の変更権限」になっているか確認してください。"
        ) from exc
    return created.get("id", "")


def create_event(
    calendar_id: str,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    *,
    location: str | None = None,
    description: str | None = None,
    recurrence: list[str] | None = None,
) -> str:
    """時刻つきの予定を1件作成し、そのIDを返す.

    サービスアカウントからのゲスト招待はドメイン全体の委任が必要なため、
    ここでは招待者なしの予定のみを想定している。
    recurrence は RRULE の一覧（例: ["RRULE:FREQ=WEEKLY;BYDAY=SU"]）。
    """
    service = _build_service()
    # 繰り返し予定はタイムゾーン名がないと 400 になる。各回を展開するとき、
    # オフセット(+09:00)だけではサマータイムの扱いが決まらないためである。
    # 単発でも付けて困らないので、常に入れて分岐を持たない。
    zone = get_config().timezone.key
    body: dict = {
        "summary": title,
        "start": {"dateTime": starts_at.isoformat(), "timeZone": zone},
        "end": {"dateTime": ends_at.isoformat(), "timeZone": zone},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if recurrence:
        body["recurrence"] = recurrence

    try:
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
    except HttpError as exc:
        raise CalendarError(
            f"予定の作成に失敗しました: {exc}\n"
            "カレンダーの共有権限が「予定の変更権限」になっているか確認してください。"
        ) from exc
    return created.get("id", "")
