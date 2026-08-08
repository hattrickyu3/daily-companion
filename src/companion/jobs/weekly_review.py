"""週次の振り返りレポート.

    uv run python -m companion.jobs.weekly_review

毎週日曜21:00に、その日を含む週（月曜〜日曜）を振り返る。
日記が1件もない週は、レポートも通知も出さない。
書くことがないのに通知だけ届くと、通知そのものが軽くなるためである。
"""

from __future__ import annotations

from companion.clock import today_local, week_start
from companion.config import get_config
from companion.db.connection import session
from companion.db.reviews import save_review
from companion.integrations.notify import send_notification
from companion.jobs.base import run_job
from companion.services.weekly_review import generate
from companion.web.serve import local_ip

JOB_NAME = "weekly_review"
PRIORITY_DEFAULT = 3


def app_url(path: str = "/") -> str:
    """通知からタップして開くURL."""
    return f"http://{local_ip()}:{get_config().web_port}{path}"


def run() -> str:
    """今週のレポートを作って通知する. 実行結果のメッセージを返す."""
    start = week_start(today_local())

    with session() as conn:
        body = generate(conn, start)
        if body is None:
            return "今週は日記がないため作成せず"
        save_review(conn, start, body)

    send_notification(
        body,
        title="今週の振り返り",
        priority=PRIORITY_DEFAULT,
        tags=["books"],
        click_url=app_url("/reviews"),
    )
    return f"{start.isoformat()}週のレポートを送信"


def main() -> int:
    return run_job(JOB_NAME, run)


if __name__ == "__main__":
    raise SystemExit(main())
