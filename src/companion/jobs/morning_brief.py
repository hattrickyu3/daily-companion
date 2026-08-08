"""朝のブリーフィング.

    uv run python -m companion.jobs.morning_brief

毎朝7:30に実行する。処理は5段階で、前の段が失敗しても次に進む。
  1. DBをバックアップ（失敗しても続行）
  2. Googleカレンダーをローカルに同期（失敗してもキャッシュで続行）
  3. ニュースを収集（失敗しても続行）
  4. Claudeにブリーフィングを書かせる（失敗しても予定の一覧だけは出す）
  5. DBに保存してntfyで通知

ニュース収集をこのジョブに含めているのは、朝の通知を1回にまとめるため。
通知が朝に2回鳴ると、それ自体が無視される原因になる。
"""

from __future__ import annotations

import logging

from companion.clock import today_local
from companion.config import get_config
from companion.db.briefings import save_briefing
from companion.db.connection import session
from companion.db.news import list_digests_for
from companion.integrations.notify import send_notification
from companion.jobs.backup import run as backup_run
from companion.jobs.base import run_job
from companion.jobs.news_digest import collect
from companion.services.briefing import generate
from companion.services.calendar_sync import sync_window
from companion.web.serve import local_ip

log = logging.getLogger(__name__)

JOB_NAME = "morning_brief"
PRIORITY_DEFAULT = 3
MAX_HEADLINES = 3


def app_url(path: str = "/") -> str:
    """通知からタップして開くURL."""
    return f"http://{local_ip()}:{get_config().web_port}{path}"


def backup_safely() -> str:
    """バックアップを取る. 失敗しても朝の通知は止めない."""
    try:
        return backup_run()
    except Exception as exc:  # noqa: BLE001 - バックアップの失敗で通知を落としたくない
        log.warning("backup failed, continuing: %s", exc)
        return "バックアップ失敗"


def collect_news_safely(day) -> str:
    """ニュースを集める. 失敗してもブリーフィングを止めない."""
    try:
        return collect(day)
    except Exception as exc:  # noqa: BLE001 - ニュースは無くても朝の通知は届けたい
        log.warning("news collection failed, continuing: %s", exc)
        return "ニュース収集に失敗"


def build_notification(body: str, headlines: list[str]) -> str:
    """通知本文を組み立てる. 見出しだけ添え、本文はWebで読ませる."""
    if not headlines:
        return body
    lines = [body, "", "ニュース:"]
    lines += [f"・{headline}" for headline in headlines[:MAX_HEADLINES]]
    return "\n".join(lines)


def collect_headlines(conn, day) -> list[str]:
    """保存済みのニュースから見出しだけを取り出す."""
    headlines: list[str] = []
    for digest in list_digests_for(conn, day):
        headlines += [source.title for source in digest.sources]
    return headlines


def run() -> str:
    """ブリーフィングを作って通知する. 実行結果のメッセージを返す."""
    day = today_local()

    # 1日の最初に、前日までの内容を確実に控えておく。
    backup_note = backup_safely()

    with session() as conn:
        sync = sync_window(conn, day)

    news_note = collect_news_safely(day)

    with session() as conn:
        body = generate(conn, day)
        save_briefing(conn, day, body)
        headlines = collect_headlines(conn, day)

    send_notification(
        build_notification(body, headlines),
        title="おはようございます",
        priority=PRIORITY_DEFAULT,
        tags=["sunrise"],
        click_url=app_url("/"),
    )
    return f"ブリーフィングを送信（{backup_note} / {sync.describe()} / {news_note}）"


def main() -> int:
    return run_job(JOB_NAME, run)


if __name__ == "__main__":
    raise SystemExit(main())
