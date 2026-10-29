"""録音を話の切れ目（息つぎ）ごとに切る係. 切った分から順に文字にして、カーソルの位置へ入れていくため.

iPhoneの議事録の録音（static/minutes/recorder.js）と同じ考え方:
    20ミリ秒ごとに音の大きさを測り、周りの雑音の大きさをゆっくり追う
    話したあと、少し黙ったら切る. 長く話し続けているときは短い切れ目でも切る
    声の入っていない部分は切れ端にしない（送っても何も返らないので）

切れ端ごとに、前の切れ端の声が終わってから、この切れ端の声が始まるまで黙っていた長さも渡す.
終わりの「。」を付けるかを、それで決める（dictation/period.py）. 最初の切れ端は0.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from companion.dictation.recorder import SAMPLE_RATE

FRAME = SAMPLE_RATE // 50  # 20ミリ秒
PAUSE_MS = 600  # これだけ黙ったら切る
SHORT_PAUSE_MS = 250  # 長く話し続けているときは、短い切れ目でも切る
LONG_SEGMENT_MS = 8000
MIN_SEGMENT_MS = 1000
MAX_SEGMENT_MS = 15000
# 話す前の無音は、話し始めの直前のこれだけを残して捨てる（頭の音が欠けないように）.
LEAD_MS = 300
MIN_THRESHOLD = 0.006  # 小さすぎる音は声とみなさない（-1〜1 の大きさで）
FLOOR_RISE = 1.0008  # 雑音の大きさの見積もりを、1フレームごとにこれだけ上げていく


class Segmenter:
    """16bitの音を push で受け取り、切れ端ができたら on_segment(16bitの音, 前に黙っていたミリ秒) を呼ぶ."""

    def __init__(self, on_segment: Callable[[np.ndarray, int], None]) -> None:
        self._on_segment = on_segment
        self._frames: list[np.ndarray] = []
        self._has_speech = False
        self._silent_frames = 0
        # 前の切れ端の声が終わってから黙っているフレームの数. 切れ端をまたいで数える. まだ誰も話していなければ None.
        self._quiet_frames: int | None = None
        self._pause_before_ms = 0  # 今の切れ端の前に黙っていた長さ
        self._floor = 0.01
        self._pending = np.zeros(0, dtype=np.int16)

    def push(self, pcm: np.ndarray) -> None:
        buffer = np.concatenate([self._pending, pcm.astype(np.int16, copy=False)])
        whole = len(buffer) // FRAME * FRAME
        for start in range(0, whole, FRAME):
            self._frame(buffer[start:start + FRAME])
        self._pending = buffer[whole:]

    def flush(self) -> None:
        """止めたとき. 残りを最後の切れ端にする（声が入っていれば）."""
        if len(self._pending):
            self._frames.append(self._pending)
            self._pending = np.zeros(0, dtype=np.int16)
        self._emit()

    def _frame(self, frame: np.ndarray) -> None:
        samples = frame.astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(samples * samples)))
        self._floor = rms if rms < self._floor else self._floor * FLOOR_RISE
        threshold = max(MIN_THRESHOLD, self._floor * 3)
        self._frames.append(frame)
        if rms > threshold:
            if not self._has_speech:
                self._pause_before_ms = (self._quiet_frames or 0) * 20
            self._has_speech = True
            self._silent_frames = 0
            self._quiet_frames = 0
        else:
            self._silent_frames += 1
            if self._quiet_frames is not None:
                self._quiet_frames += 1
        if not self._has_speech:
            # 話し始める前の無音は、直前の少しだけ残す.
            keep = LEAD_MS // 20
            if len(self._frames) > keep:
                self._frames = self._frames[-keep:]
            return
        length_ms = len(self._frames) * 20
        silent_ms = self._silent_frames * 20
        if (
            (length_ms >= MIN_SEGMENT_MS and silent_ms >= PAUSE_MS)
            or (length_ms >= LONG_SEGMENT_MS and silent_ms >= SHORT_PAUSE_MS)
            or length_ms >= MAX_SEGMENT_MS
        ):
            self._emit()

    def _emit(self) -> None:
        frames, has_speech = self._frames, self._has_speech
        self._frames, self._has_speech, self._silent_frames = [], False, 0
        if has_speech and frames:
            self._on_segment(np.concatenate(frames), self._pause_before_ms)
