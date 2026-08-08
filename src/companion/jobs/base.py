"""ジョブ実行の共通基盤.

タスクスケジューラから無人で走るため、失敗しても誰も見ていない。
成否と所要時間を必ず job_runs に残し、後から「今朝ちゃんと動いたか」を追えるようにする。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from companion.config import LOG_DIR
from companion.db.connection import session

log = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
MAX_MESSAGE_LENGTH = 500


def setup_logging(job_name: str) -> None:
    """ジョブごとのログファイルを設定する."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"{job_name}.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def record_run(job_name: str, status: str, message: str, duration_ms: int) -> None:
    """実行結果を job_runs に残す. ここでの失敗はジョブ本体を巻き込まない."""
    try:
        with session() as conn:
            conn.execute(
                "INSERT INTO job_runs (job_name, status, message, duration_ms) VALUES (?, ?, ?, ?)",
                (job_name, status, message[:MAX_MESSAGE_LENGTH], duration_ms),
            )
    except Exception:  # noqa: BLE001 - ログが書けないだけで失敗扱いにはしない
        log.exception("could not record job run for %s", job_name)


def run_job(job_name: str, work: Callable[[], str]) -> int:
    """ジョブを実行し、結果を記録して終了コードを返す.

    work は完了メッセージを返す。例外はここで捕まえて failed として記録する。
    """
    setup_logging(job_name)
    log.info("start %s", job_name)
    started = time.monotonic()

    try:
        message = work()
    except Exception as exc:  # noqa: BLE001 - どんな失敗もジョブログに残して終える
        duration_ms = int((time.monotonic() - started) * 1000)
        log.exception("%s failed", job_name)
        record_run(job_name, STATUS_FAILED, str(exc), duration_ms)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    log.info("done %s (%dms): %s", job_name, duration_ms, message)
    record_run(job_name, STATUS_SUCCESS, message, duration_ms)
    return 0
