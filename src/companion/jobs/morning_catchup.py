"""朝のブリーフィングの追いかけ実行.

    uv run python -m companion.jobs.morning_catchup

PC起動のたびに走らせ、今日のブリーフィングがまだ無ければ送り直す。

タスクスケジューラの StartWhenAvailable は「PCが落ちていて実行時刻を逃した」
場合しか追いかけてくれない。7:30ちょうどにWindows Updateの再起動が重なると、
タスクは一度起動してから失敗する(0x8007045B)ため「逃した」扱いにならず、
その日のブリーフィングが丸ごと消える。2026-08-12に実際に発生した。
その穴をここで塞ぐ。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time

from companion.clock import now_local
from companion.db.briefings import get_briefing
from companion.db.connection import session
from companion.jobs import morning_brief
from companion.jobs.base import run_job, setup_logging

log = logging.getLogger(__name__)

JOB_NAME = morning_brief.JOB_NAME

# scripts\register_tasks.ps1 の morning タスクの時刻と合わせること。
BRIEF_TIME = time(7, 30)

# これを過ぎてからの起動では、もう朝の通知を鳴らさない。
# 夕方にPCを点けた人に「おはようございます」を送っても邪魔になるだけ。
CATCHUP_DEADLINE = time(15, 0)


def should_catchup(conn: sqlite3.Connection, now: datetime) -> tuple[bool, str]:
    """追いかけ実行すべきかを返す. 見送る場合は理由も返す."""
    if now.time() < BRIEF_TIME:
        return False, "まだ07:30前"
    if now.time() > CATCHUP_DEADLINE:
        return False, f"追いかけの期限({CATCHUP_DEADLINE:%H:%M})を過ぎている"
    if get_briefing(conn, now.date()) is not None:
        return False, "今日のブリーフィングは送信済み"
    return True, ""


def main() -> int:
    setup_logging(JOB_NAME)

    with session() as conn:
        should_run, reason = should_catchup(conn, now_local())

    if not should_run:
        log.info("skip catchup: %s", reason)
        return 0

    log.info("morning brief is missing for today, running catchup")
    return run_job(JOB_NAME, morning_brief.run)


if __name__ == "__main__":
    raise SystemExit(main())
