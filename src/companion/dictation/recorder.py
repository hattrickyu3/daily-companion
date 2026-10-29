"""マイクの録音. Whisperにそのまま使える形（16kHz・モノラル・16bit）で録り、WAVにして渡す."""

from __future__ import annotations

import io
import threading
import wave
from collections.abc import Callable

import numpy as np

SAMPLE_RATE = 16000
# これより短い録音は、押し間違いとして送らない.
MIN_SECONDS = 0.4


def to_wav(pcm: np.ndarray) -> bytes:
    """16bitの音声をWAVのバイト列にする."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())
    return buffer.getvalue()


class Recorder:
    """既定のマイク（Windowsの設定のもの）から録る.

    start → stop で録った分のWAVを返す。start に on_segment を渡すと、話の切れ目ごとに切った音と、その前に
    黙っていたミリ秒を、録っている間に順に渡す（そのときは録った分を貯めず、stop は None を返す）。
    on_segment は録音のスレッドから呼ばれる。
    """

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._segmenter = None
        self._lock = threading.Lock()
        self._stream = None

    @property
    def seconds(self) -> float:
        with self._lock:
            return self._samples / SAMPLE_RATE

    def start(self, on_segment: Callable[[np.ndarray, int], None] | None = None) -> None:
        """録り始める. マイクが使えなければ sounddevice の例外（PortAudioError）."""
        import sounddevice as sd  # 重いので使うときだけ読む

        from companion.dictation.segmenter import Segmenter

        with self._lock:
            self._chunks = []
            self._samples = 0
            self._segmenter = Segmenter(on_segment) if on_segment else None
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=self._on_audio)
        try:
            stream.start()
        except BaseException:
            stream.close()  # 開いたままだとマイクをつかんだままになる
            raise
        self._stream = stream

    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ARG002 - sounddevice の決まった形
        with self._lock:
            self._samples += frames
            if self._segmenter is not None:
                self._segmenter.push(indata[:, 0].copy())
            else:
                self._chunks.append(indata[:, 0].copy())

    def stop(self) -> bytes | None:
        """録るのをやめてWAVを返す. 短すぎれば None. 切れ目ごとに渡しているときは、最後の切れ端を渡して None."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        with self._lock:
            segmenter, self._segmenter = self._segmenter, None
        if segmenter is not None:
            segmenter.flush()
            return None
        with self._lock:
            pcm = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.int16)
            self._chunks = []
        return to_wav(pcm) if len(pcm) >= MIN_SECONDS * SAMPLE_RATE else None
