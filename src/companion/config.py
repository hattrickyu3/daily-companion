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
# 議事録の録音。音声は議事録づくりがどこで失敗しても残したいので、DBとは別にファイルで持つ。
MINUTES_AUDIO_DIR = DATA_DIR / "minutes"
# 話者分離のモデル. 初めて使うときに sherpa-onnx の GitHub の配布物から落とす.
DIARIZATION_MODELS_DIR = DATA_DIR / "models" / "diarization"
# 議事録ノートの置き場所（NOTES_DIR からの相対）。MINUTES_DIR が空のときに使う。
DEFAULT_MINUTES_SUBDIR = Path("研究") / "ゼミ議事録"
# 清書した会議の録音（m4a）を置く場所. 保管庫（OneDrive）の容量を使わないよう、PCの中に置く。
DEFAULT_RECORDINGS_DIR = Path.home() / "録音"
# 録音アプリ（Recorder Plus）からWebDAVで届いた録音の受け箱.
WEBDAV_INBOX_DIR = DATA_DIR / "webdav"
DEFAULT_WEBDAV_USER = "recorder"
DEFAULT_WHISPER_MODEL = "large-v3-turbo"
# 録音を終えたあと、録音全体を文字にし直すモデル. 遅いぶん正確.
DEFAULT_WHISPER_FINAL_MODEL = "large-v3"

DEFAULT_SERVICE_ACCOUNT_FILE = CREDENTIALS_DIR / "service-account.json"
# 読み書き両対応のスコープ。将来の予定登録で共有設定をやり直さずに済むよう最初から取る。
GOOGLE_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)

DEFAULT_CLAUDE_TIMEOUT_SEC = 300
DEFAULT_WEB_PORT = 8765
# 同期範囲は前後で非対称にする。予定は先にあるものを見たいので未来を長く取る。
# 当初は前後7日だったが、月カレンダーを見るには短すぎて、
# 2週間先の予定が画面に出てこなかった。
CALENDAR_SYNC_PAST_DAYS = 30
CALENDAR_SYNC_FUTURE_DAYS = 120
# 日次バックアップを1週間分保つ。DBは数百KBなので世代を増やしても負担にならず、
# 「おかしくなったのが数日前」に気づいても戻せる余地が残る。
BACKUP_GENERATIONS = 7

VALID_NEWS_TOPICS = ("ai_tech", "job_hunting", "social", "economy", "domestic", "world")


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
    # 通知からタップして開くURLの入口（例: http://mypc:8765）。
    # 空なら家のLAN内のIPを使う（＝外出先からは開けない）。
    app_base_url: str
    news_topics: tuple[str, ...]
    claude_timeout_sec: int
    timezone: ZoneInfo
    db_path: Path = field(default=DB_PATH)
    # Obsidianの保管庫のフォルダ。スマホからノートを読むための画面で使う。
    # 空ならノート画面は「未設定」と案内するだけで、ほかの機能には影響しない。
    notes_dir: Path | None = None
    # mail-notifier が書き出すメールの判定結果（JSON）。
    # 空ならメール画面は「未設定」と案内するだけで、今日の画面にもメール欄を出さない。
    mail_results_file: Path | None = None
    # 清書した議事録ノートを書き出すフォルダ。None なら書き出さず、アプリの中にだけ残す。
    minutes_dir: Path | None = None
    recordings_dir: Path = field(default=DEFAULT_RECORDINGS_DIR)
    # 録音アプリからWebDAVで録音を受け取るときのユーザー名とパスワード. パスワードが空なら受け口を閉じておく.
    webdav_user: str = DEFAULT_WEBDAV_USER
    webdav_password: str = ""
    webdav_inbox_dir: Path = field(default=WEBDAV_INBOX_DIR)
    # 録音アプリ（Recorder Plus）がGoogleドライブに上げた録音が届く、PCのフォルダ. None なら見張らない.
    recorder_folder: Path | None = None
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_final_model: str = DEFAULT_WHISPER_FINAL_MODEL
    minutes_audio_dir: Path = field(default=MINUTES_AUDIO_DIR)

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


def _resolve_minutes_dir(raw: str, notes_dir: Path | None) -> Path | None:
    """議事録ノートの置き場所. 未指定なら保管庫の中の既定の場所にする."""
    if raw:
        return Path(raw)
    return notes_dir / DEFAULT_MINUTES_SUBDIR if notes_dir else None


def load_config() -> Config:
    """.env を読み込んで Config を組み立てる."""
    load_dotenv(PROJECT_ROOT / ".env")

    timezone_name = os.getenv("TIMEZONE", "Asia/Tokyo").strip() or "Asia/Tokyo"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - ZoneInfo は複数の例外型を投げる
        raise ConfigError(f"TIMEZONE が不正です: {timezone_name!r}") from exc

    key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    notes_dir = os.getenv("NOTES_DIR", "").strip()
    mail_results_file = os.getenv("MAIL_RESULTS_FILE", "").strip()
    notes_path = Path(notes_dir) if notes_dir else None

    return Config(
        ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
        ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/"),
        google_service_account_file=Path(key_file) if key_file else DEFAULT_SERVICE_ACCOUNT_FILE,
        google_calendar_ids=_split_csv(os.getenv("GOOGLE_CALENDAR_IDS")),
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=_read_int("WEB_PORT", DEFAULT_WEB_PORT),
        # 末尾のスラッシュは組み立て時に二重にならないよう落としておく。
        app_base_url=os.getenv("APP_BASE_URL", "").strip().rstrip("/"),
        news_topics=_validate_news_topics(_split_csv(os.getenv("NEWS_TOPICS"))),
        claude_timeout_sec=_read_int("CLAUDE_TIMEOUT_SEC", DEFAULT_CLAUDE_TIMEOUT_SEC),
        timezone=timezone,
        notes_dir=notes_path,
        mail_results_file=Path(mail_results_file) if mail_results_file else None,
        minutes_dir=_resolve_minutes_dir(os.getenv("MINUTES_DIR", "").strip(), notes_path),
        recordings_dir=Path(os.getenv("RECORDINGS_DIR", "").strip() or DEFAULT_RECORDINGS_DIR),
        webdav_user=os.getenv("WEBDAV_USER", "").strip() or DEFAULT_WEBDAV_USER,
        webdav_password=os.getenv("WEBDAV_PASSWORD", "").strip(),
        recorder_folder=Path(folder) if (folder := os.getenv("RECORDER_FOLDER", "").strip()) else None,
        whisper_model=os.getenv("WHISPER_MODEL", "").strip() or DEFAULT_WHISPER_MODEL,
        whisper_final_model=os.getenv("WHISPER_FINAL_MODEL", "").strip() or DEFAULT_WHISPER_FINAL_MODEL,
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """プロセス内で共有する Config を返す."""
    return load_config()
