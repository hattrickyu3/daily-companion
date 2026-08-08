"""時刻の取得を一箇所に集約する.

システムのローカル時刻ではなく、設定されたタイムゾーンを常に使う。
「今日」の境界がPCのロケール設定で変わってしまうのを防ぐため。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from companion.config import get_config


def now_local() -> datetime:
    """設定タイムゾーンでの現在日時."""
    return datetime.now(tz=get_config().timezone)


def today_local() -> date:
    """設定タイムゾーンでの今日の日付."""
    return now_local().date()


def week_start(day: date) -> date:
    """その日を含む週の月曜日を返す."""
    return day - timedelta(days=day.weekday())


def parse_date(value: str) -> date:
    """YYYY-MM-DD をパースする. 不正なら ValueError."""
    return date.fromisoformat(value)


def utc_to_local(stored: str) -> datetime:
    """SQLiteの datetime('now') が返すUTC文字列を、設定タイムゾーンの日時にする.

    DBにはUTCで保存するのが正しいが、そのまま画面に出すと9時間ずれて見え、
    「今朝ジョブが動いたか」を読み違える。表示の直前にここで必ず変換する。
    """
    return datetime.fromisoformat(stored).replace(tzinfo=UTC).astimezone(get_config().timezone)
