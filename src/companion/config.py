"""設定の読み込みと検証.

.env から値を読み、型と必須項目を検証したうえで不変の設定オブジェクトを返す。
検証は起動時に一度だけ行い、以降はどこからでも get_config() で参照する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
CREDENTIALS_DIR = PROJECT_ROOT / "credentials"
DB_PATH = DATA_DIR / "companion.db"
BACKUP_DIR = DATA_DIR / "backups"

DEFAULT_SERVICE_ACCOUNT_FILE = CREDENTIALS_DIR / "service-account.json"
# 読み書き両対応のスコープ。将来の予定登録で共有設定をやり直さずに済むよう最初から取る。
GOOGLE_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)

DEFAULT_CLAUDE_TIMEOUT_SEC = 300
DEFAULT_WEB_PORT = 8765
CALENDAR_SYNC_WINDOW_DAYS = 7
# 日次バックアップを1週間分保つ。DBは数百KBなので世代を増やしても負担にならず、
# 「おかしくなったのが数日前」に気づいても戻せる余地が残る。
BACKUP_GENERATIONS = 7

VALID_NEWS_TOPICS = ("ai_tech", "job_hunting", "domestic", "world")


class ConfigError(Exception):
    """設定が不足または不正なときに送出する."""


@dataclass(frozen=True)
class Config:
    """アプリ全体で共有する設定. 生成後は変更しない."""

    ntfy_topic: str
    ntfy_server: str
    google_service_account_file: Path
    google_calendar_ids: tuple[str, ...]
    web_host: str
    web_port: int
    news_topics: tuple[str, ...]
    claude_timeout_sec: int
    timezone: ZoneInfo
    db_path: Path = field(default=DB_PATH)

    @property
    def has_calendar(self) -> bool:
        """カレンダー連携が設定済みか（鍵ファイルが実在するか）."""
        return self.google_service_account_file.exists()

    @property
    def has_notify(self) -> bool:
        """通知が設定済みか."""
        return bool(self.ntfy_topic)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    """カンマ区切り文字列を空要素を除いたタプルにする."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _read_int(name: str, default: int) -> int:
    """環境変数を整数として読む. 数値でなければ ConfigError."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} は整数で指定してください（現在の値: {raw!r}）") from exc


def _validate_news_topics(topics: tuple[str, ...]) -> tuple[str, ...]:
    """ニュースジャンルが既知の値かを検証する."""
    unknown = [topic for topic in topics if topic not in VALID_NEWS_TOPICS]
    if unknown:
        raise ConfigError(
            f"NEWS_TOPICS に未知のジャンルがあります: {', '.join(unknown)}\n"
            f"指定できるのは {', '.join(VALID_NEWS_TOPICS)} です。"
        )
    return topics or ("ai_tech",)


def load_config() -> Config:
    """.env を読み込んで Config を組み立てる."""
    load_dotenv(PROJECT_ROOT / ".env")

    timezone_name = os.getenv("TIMEZONE", "Asia/Tokyo").strip() or "Asia/Tokyo"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - ZoneInfo は複数の例外型を投げる
        raise ConfigError(f"TIMEZONE が不正です: {timezone_name!r}") from exc

    key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

    return Config(
        ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
        ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/"),
        google_service_account_file=Path(key_file) if key_file else DEFAULT_SERVICE_ACCOUNT_FILE,
        google_calendar_ids=_split_csv(os.getenv("GOOGLE_CALENDAR_IDS")),
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=_read_int("WEB_PORT", DEFAULT_WEB_PORT),
        news_topics=_validate_news_topics(_split_csv(os.getenv("NEWS_TOPICS"))),
        claude_timeout_sec=_read_int("CLAUDE_TIMEOUT_SEC", DEFAULT_CLAUDE_TIMEOUT_SEC),
        timezone=timezone,
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """プロセス内で共有する Config を返す."""
    return load_config()
