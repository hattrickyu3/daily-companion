"""ニュース要約の収集.

    uv run python -m companion.jobs.news_digest

朝のブリーフィング(morning_brief)から呼ばれるほか、単体でも実行できる。
このジョブ自体は通知しない。朝の通知を1回にまとめるためである。
結果はWeb画面で読む。
"""

from __future__ import annotations

import logging
import time
from datetime import date

from companion.clock import today_local
from companion.config import get_config
from companion.db.connection import session
from companion.db.news import save_digest
from companion.jobs.base import run_job
from companion.services.news import generate

log = logging.getLogger(__name__)

JOB_NAME = "news_digest"

# 全ジャンル合わせた時間予算。これを超えたら残りのジャンルは諦める。
# 朝のジョブ全体がタスクスケジューラの実行上限(15分)に当たると、
# ニュースだけでなくブリーフィングの通知ごと打ち切られてしまうため。
#
# 注意: 予算の判定はジャンルを始める前に行うので、予算ぎりぎりで始まった最後の1件は
# さらに NEWS_TIMEOUT_SEC だけ走りうる。実際の最悪値は
#   TOTAL_BUDGET_SEC + NEWS_TIMEOUT_SEC + ブリーフィング + 同期・通知
# になる。この合計が15分に収まる値にしてある（test_news_budget.py で検証）。
TOTAL_BUDGET_SEC = 300


def collect(today: date) -> str:
    """設定された全ジャンルを収集して保存する. 結果の説明を返す.

    1ジャンルが失敗しても他は続ける。全滅しても例外にはしない。
    時間予算を超えた場合は残りを飛ばし、そのことを結果に含める。
    """
    topics = get_config().news_topics
    started = time.monotonic()

    collected: list[str] = []
    skipped: list[str] = []

    for topic in topics:
        if time.monotonic() - started > TOTAL_BUDGET_SEC:
            log.warning("news budget exhausted, skipping %s", topic)
            skipped.append(topic)
            continue

        result = generate(topic, today)
        if result.is_empty:
            log.warning("no news collected for %s", topic)
            continue

        with session() as conn:
            save_digest(conn, today, topic, result.to_body(), result.to_sources())
        collected.append(f"{topic}:{len(result.items)}件")

    if not collected and not skipped:
        return "ニュースを取得できませんでした"

    parts = []
    if collected:
        parts.append("収集: " + "、".join(collected))
    if skipped:
        parts.append(f"時間切れで省略: {'、'.join(skipped)}")
    return " / ".join(parts)


def main() -> int:
    return run_job(JOB_NAME, lambda: collect(today_local()))


if __name__ == "__main__":
    raise SystemExit(main())
