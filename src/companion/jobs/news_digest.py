"""ニュース要約の収集.

    uv run python -m companion.jobs.news_digest

朝のブリーフィング(morning_brief)から呼ばれるほか、単体でも実行できる。
このジョブ自体は通知しない。朝の通知を1回にまとめるためである。
結果はWeb画面で読む。

ジャンルは同時に走らせる。1ジャンルあたり2〜5分かかるので、
順番に処理するとジャンルを増やしたときに朝のジョブが
タスクスケジューラの実行上限(15分)に当たってしまう。
同時実行なら、かかる時間はジャンル数ではなく一番遅いジャンルで決まる。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from companion.clock import today_local
from companion.config import get_config
from companion.db.connection import session
from companion.db.news import save_digest
from companion.jobs.base import run_job
from companion.services.news import NewsResult, generate

log = logging.getLogger(__name__)

JOB_NAME = "news_digest"

# 同時に走らせる claude -p の数。
# ジャンル数がこれを超えると2巡目が発生し、所要時間がほぼ倍になる。
# 実際に使うジャンル数（現在4）を下回らない値にしておくこと。
# 各プロセスはWeb検索の待ちが大半でCPUは張り付かないが、
# 無制限に並べるとメモリと同時接続が増えるので上限自体は残す。
MAX_CONCURRENT = 4


def _generate_one(topic: str, today: date) -> NewsResult:
    """1ジャンルを生成する. 例外は外に出さない（他のジャンルを巻き込まないため）."""
    try:
        return generate(topic, today)
    except Exception as exc:  # noqa: BLE001 - 1ジャンルの失敗で全体を止めない
        log.warning("news generation raised for %s: %s", topic, exc)
        return NewsResult(topic=topic, items=())


def collect(today: date) -> str:
    """設定された全ジャンルを同時に収集して保存する. 結果の説明を返す.

    1ジャンルが失敗しても他は続ける。全滅しても例外にはしない。
    """
    topics = get_config().news_topics
    if not topics:
        return "対象ジャンルが設定されていません"

    workers = min(len(topics), MAX_CONCURRENT)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda topic: _generate_one(topic, today), topics))

    collected: list[str] = []
    for result in results:
        if result.is_empty:
            log.warning("no news collected for %s", result.topic)
            continue
        with session() as conn:
            save_digest(conn, today, result.topic, result.to_body(), result.to_sources())
        collected.append(f"{result.topic}:{len(result.items)}件")

    if not collected:
        return "ニュースを取得できませんでした"
    return "収集: " + "、".join(collected)


def main() -> int:
    return run_job(JOB_NAME, lambda: collect(today_local()))


if __name__ == "__main__":
    raise SystemExit(main())
