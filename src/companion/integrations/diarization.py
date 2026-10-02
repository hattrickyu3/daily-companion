"""話者分離（誰がいつ話したかを声で聞き分ける）.

sherpa-onnx で pyannote の区切りモデルと、声の特徴を取るモデルを組み合わせて動かす。
CPUで動くので、文字起こし（GPU）とグラボを取り合わない。

モデルは sherpa-onnx の GitHub の配布物から、初めて使うときに data/models/diarization/ へ落とす。
Hugging Face の利用登録が要らないものを選んでいる。

人数がわかっているときは渡すと精度が大きく上がる。2026-10-02 に試した騒がしい録音では、
人数を渡さないと2人の会話が5〜16人に分かれた。渡すと2人に正しく分かれた。
"""

from __future__ import annotations

import logging
import os
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger(__name__)

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_ARCHIVE = f"{RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_MODEL = Path("sherpa-onnx-pyannote-segmentation-3-0") / "model.onnx"
# 中国語・英語で学習した汎用の声の特徴モデル. 日本語の3人の会話で10文すべて正しく聞き分けた.
EMBEDDING_MODEL = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
EMBEDDING_URL = f"{RELEASES}/speaker-recongition-models/{EMBEDDING_MODEL}"
DOWNLOAD_TIMEOUT_SEC = 300

# 人数がわからないときの、同じ人とみなす近さ. 小さいほど人数を多く見積もる.
# 0.5 だと騒がしい録音で人数が膨らみ、0.7 だと静かな3人の会話を2人にまとめてしまった。
AUTO_THRESHOLD = 0.6
NUM_THREADS = 4


class DiarizationError(Exception):
    """話者分離に失敗したときに送出する."""


@dataclass(frozen=True)
class SpeakerTurn:
    """ある話者が話していた区間（録音の先頭からの秒）."""

    start: float
    end: float
    speaker: int


def _download(url: str, target: Path) -> None:
    tmp = target.with_name(target.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SEC) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    os.replace(tmp, target)


def ensure_models(models_dir: Path) -> None:
    """モデルが無ければ落としてくる. 何度呼んでもよい."""
    models_dir.mkdir(parents=True, exist_ok=True)
    if not (models_dir / SEGMENTATION_MODEL).exists():
        archive = models_dir / "segmentation.tar.bz2"
        _download(SEGMENTATION_ARCHIVE, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(models_dir, filter="data")
        archive.unlink()
    if not (models_dir / EMBEDDING_MODEL).exists():
        _download(EMBEDDING_URL, models_dir / EMBEDDING_MODEL)


class Diarizer:
    """録音全体を話者ごとの区間に分ける. 同時に呼ばれても順番に処理する."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self._lock = threading.Lock()

    def _build(self, num_speakers: int | None):
        import sherpa_onnx  # 使うときだけ読む

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(self.models_dir / SEGMENTATION_MODEL)
                ),
                num_threads=NUM_THREADS,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.models_dir / EMBEDDING_MODEL), num_threads=NUM_THREADS
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers or -1, threshold=AUTO_THRESHOLD
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise DiarizationError("話者分離のモデルが見つかりません。")
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    def diarize(self, samples: np.ndarray, *, num_speakers: int | None = None) -> list[SpeakerTurn]:
        """16kHzの録音を話者ごとの区間にする. 話者の番号は最初に話した順に0から振り直す."""
        with self._lock:
            try:
                ensure_models(self.models_dir)
                result = self._build(num_speakers).process(samples).sort_by_start_time()
            except DiarizationError:
                raise
            except Exception as exc:  # noqa: BLE001 - ダウンロード・ONNXまわりは失敗の型がまちまち
                log.exception("speaker diarization failed")
                raise DiarizationError(f"話者の聞き分けに失敗しました: {exc}") from exc
        order: dict[int, int] = {}
        turns = []
        for r in result:
            speaker = order.setdefault(r.speaker, len(order))
            turns.append(SpeakerTurn(start=r.start, end=r.end, speaker=speaker))
        return turns


def speaker_for(start: float, end: float, turns: list[SpeakerTurn]) -> int | None:
    """区間 [start, end] にいちばん長く重なる話者. 重なりが無ければ None."""
    overlap: dict[int, float] = {}
    for turn in turns:
        length = min(end, turn.end) - max(start, turn.start)
        if length > 0:
            overlap[turn.speaker] = overlap.get(turn.speaker, 0.0) + length
    return max(overlap, key=overlap.__getitem__) if overlap else None
