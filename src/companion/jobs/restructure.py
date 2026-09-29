"""要約が付いていない日記をまとめて作り直す.

    uv run python -m companion.jobs.restructure

日記の保存時に走る構造化は、失敗しても原文だけ残して先に進む設計になっている。
そのため認証切れやタイムアウトが続くと、要約の無い日が静かに溜まる。
原因を直したあとにこれを走らせて、取りこぼした日を埋める。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from companion.clock import today_local
from companion.db.connection import session
from companion.db.repository import Entry, list_entries_between
from companion.jobs.base import run_job
from companion.services.diary import structure_entry

log = logging.getLogger(__name__)

JOB_NAME = "restructure"

# これより古い日記は、今さら要約を足しても読み返されない。
RECENT_DAYS = 14


def find_unstructured(conn, today: date) -> list[Entry]:
    """直近の、要約がまだ付いていない日記を古い順に返す."""
    entries = list_entries_between(conn, today - timedelta(days=RECENT_DAYS - 1), today)
    return [entry for entry in entries if not entry.is_structured]


def run() -> str:
    """要約待ちの日記を順に処理する. 実行結果のメッセージを返す."""
    with session() as conn:
        targets = find_unstructured(conn, today_local())
        if not targets:
            return "要約待ちの日記はありません"

        done: list[str] = []
        for entry in targets:
            reason = structure_entry(conn, entry.id, entry.entry_date, entry.raw_text)
            if reason is not None:
                # 認証切れのように原因が共通なら残りも必ず失敗する。
                # 無駄に呼び続けず、理由を持って止める。
                log.warning("stopping restructure at %s: %s", entry.entry_date, reason)
                remaining = len(targets) - len(done)
                return f"{len(done)}件を要約、{remaining}件は失敗（{reason}）"
            done.append(entry.entry_date.isoformat())

    return f"{len(done)}件を要約（{'、'.join(done)}）"


def main() -> int:
    return run_job(JOB_NAME, run)


if __name__ == "__main__":
    raise SystemExit(main())
