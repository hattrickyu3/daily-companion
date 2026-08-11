"""夜の日記催促.

    uv run python -m companion.jobs.evening_prompt
    uv run python -m companion.jobs.evening_prompt --force   # 動作確認用

毎晩22:00に「今日は何した？」と通知する。
催促は1日1回きりにする。しつこく送るとプレッシャーになって続かなくなるため。
既に書き終えている日は何も送らない。「もう書いてあります」という通知は
用がないうえ、通知が鳴ることに慣れて本当の催促を無視するようになるからである。
"""

from __future__ import annotations

import sys

from companion.clock import today_local
from companion.db.connection import session
from companion.db.repository import get_entry
from companion.integrations.notify import send_notification
from companion.jobs.base import run_job
from companion.links import app_url
from companion.services.schedule import get_events_for

JOB_NAME = "evening_prompt"
MAX_EVENTS_IN_MESSAGE = 5

# ntfyの優先度は1〜5。1と2はバナーも音も出さずに配信されるため、
# 気づいてほしい通知には必ず3以上を使う。
PRIORITY_DEFAULT = 3



def build_prompt_message() -> tuple[str, str]:
    """催促通知の (タイトル, 本文) を組み立てる."""
    lines = ["今日は何をしましたか。ひとことでも大丈夫です。"]

    events = get_events_for(today_local())
    if events:
        lines.append("")
        lines.append("今日の予定:")
        lines += [f"・{e.time_label()} {e.title}" for e in events[:MAX_EVENTS_IN_MESSAGE]]

    return ("今日の振り返り", "\n".join(lines))


def send_prompt() -> None:
    """催促通知を送る."""
    title, body = build_prompt_message()
    send_notification(
        body, title=title, priority=PRIORITY_DEFAULT, tags=["memo"], click_url=app_url("/")
    )


def run(*, force: bool = False) -> str:
    """催促が必要なら送る. 実行結果のメッセージを返す.

    force=True のときは、記録済みでも送る（動作確認用）。
    """
    day = today_local()
    with session() as conn:
        entry = get_entry(conn, day)

    if entry is not None and not force:
        return "記録済みのため送信せず"

    send_prompt()
    return "催促を送信（強制）" if entry is not None else "催促を送信"


def main(argv: list[str] | None = None) -> int:
    force = "--force" in (argv if argv is not None else sys.argv[1:])
    return run_job(JOB_NAME, lambda: run(force=force))


if __name__ == "__main__":
    raise SystemExit(main())
