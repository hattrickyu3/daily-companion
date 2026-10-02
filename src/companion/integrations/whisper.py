"""Whisper（faster-whisper）による文字起こし.

GPU（CUDA）で動かす。CUDAのライブラリは pip の nvidia-cublas-cu12 / nvidia-cudnn-cu12 で
入れているので、そのDLLの場所をWindowsに教えてからモデルを読む。
GPUが使えないときはCPUで動かす（遅くはなるが止まりはしない）。

モデルは最初の切れ端が届いたときに読み（約3秒）、しばらく使わなければ手放してVRAMを空ける。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CUDA_PACKAGES = ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc")
IDLE_UNLOAD_SEC = 15 * 60
# 録音を通しで文字にし直すときの、声を拾う感度（既定は0.5）. 下げると小さい声も拾う.
# 2026-10-02 の騒がしい録音で比べ、0.3 で取りこぼしもでっち上げも少なかった.
LONG_VAD_PARAMETERS = {"threshold": 0.3, "min_speech_duration_ms": 100, "speech_pad_ms": 400}

# 無音や雑音に対してWhisperがよくでっち上げる決まり文句. これだけの切れ端は捨てる。
HALLUCINATIONS = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "最後までご覧いただきありがとうございます",
    "おやすみなさい",
    "次の動画でお会いしましょう",
    "高評価",
    "この映像は",
)


class WhisperError(Exception):
    """文字起こしに失敗したときに送出する."""


def _add_cuda_dll_dirs() -> None:
    """pip で入れたCUDAライブラリのDLLを読めるようにする. 何度呼んでもよい."""
    for package in CUDA_PACKAGES:
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            continue
        for base in (spec.submodule_search_locations or []) if spec else []:
            bin_dir = Path(base) / "bin"
            if bin_dir.is_dir() and str(bin_dir) not in os.environ["PATH"]:
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def clean_text(text: str) -> str:
    """文字起こし結果から、でっち上げの決まり文句だけの行を落とす."""
    stripped = text.strip()
    if not stripped:
        return ""
    if any(phrase in stripped for phrase in HALLUCINATIONS) and len(stripped) <= 30:
        return ""
    return stripped


@dataclass(frozen=True)
class TimedText:
    """録音の先頭からの区間（秒）と、そこで話されたこと."""

    start: float
    end: float
    text: str


class Transcriber:
    """モデルを1つだけ持ち、切れ端を1つずつ文字にする. 同時に呼ばれても順番に処理する."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.device = ""
        self._model = None
        self._lock = threading.Lock()
        self._last_used = 0.0

    def _load(self):
        from faster_whisper import WhisperModel  # 重いので使うときだけ読む

        _add_cuda_dll_dirs()
        try:
            model = WhisperModel(self.model_name, device="cuda", compute_type="float16")
            self.device = "cuda"
        except Exception as exc:  # noqa: BLE001 - CUDAまわりは失敗の型がまちまち
            log.warning("whisper could not use the GPU, falling back to CPU: %s", exc)
            model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            self.device = "cpu"
        log.info("whisper model %s loaded on %s", self.model_name, self.device)
        return model

    def transcribe(self, samples: np.ndarray, *, prompt: str = "") -> str:
        """16kHzの音声を文字にする. prompt には直前の文字起こしを渡すと続きが自然になる."""
        with self._lock:
            try:
                if self._model is None:
                    self._model = self._load()
                segments, _ = self._model.transcribe(
                    samples,
                    language="ja",
                    beam_size=5,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    initial_prompt=prompt[-200:] or None,
                )
                text = "".join(segment.text for segment in segments)
            except Exception as exc:  # noqa: BLE001 - 失敗はまとめて呼び出し側に伝える
                log.exception("whisper transcription failed")
                raise WhisperError(f"文字起こしに失敗しました: {exc}") from exc
            finally:
                self._last_used = time.monotonic()
        return clean_text(text)

    def transcribe_long(self, samples: np.ndarray) -> list[TimedText]:
        """録音を通しで文字にする. 前後の話の流れを使えるので、切れ端ごとより正確になる.

        終わったらモデルを手放す（録音中に使う速いモデルと一緒にVRAMを使い続けないため）.
        """
        with self._lock:
            try:
                if self._model is None:
                    self._model = self._load()
                segments, _ = self._model.transcribe(
                    samples,
                    language="ja",
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters=LONG_VAD_PARAMETERS,
                    condition_on_previous_text=False,  # 同じ文の繰り返しに陥るのを防ぐ
                )
                timed = [TimedText(start=s.start, end=s.end, text=clean_text(s.text)) for s in segments]
            except Exception as exc:  # noqa: BLE001 - 失敗はまとめて呼び出し側に伝える
                log.exception("whisper long transcription failed")
                raise WhisperError(f"文字起こしのやり直しに失敗しました: {exc}") from exc
            finally:
                self._model = None
        return [t for t in timed if t.text]

    def unload_if_idle(self) -> None:
        """しばらく使っていなければモデルを手放す."""
        with self._lock:
            if self._model is not None and time.monotonic() - self._last_used > IDLE_UNLOAD_SEC:
                self._model = None
                log.info("whisper model unloaded after idle")
