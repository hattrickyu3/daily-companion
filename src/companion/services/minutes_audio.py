"""議事録の音声ファイル.

iPhoneから届く切れ端（16kHz・モノラル・16bitのWAV）を、受け取った時点でまず
ディスクに書く。文字起こしや議事録づくりはそのあとの工程なので、どこで失敗しても
音声は残る。録音が終わったら切れ端を1本のWAVにつなぎ、聞き返し用にm4aも作る。

    data/minutes/<会議id>/segments/<seg_id>.wav   届いた切れ端
    data/minutes/<会議id>/録音.wav                つないだもの（原本）
"""

from __future__ import annotations

import io
import logging
import os
import threading
import uuid
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16bit
# 切れ端はiPhone側で長くても30秒で切る. 30秒 = 約1MB なので余裕を見て2MBまで受ける.
MAX_SEGMENT_BYTES = 2 * 1024 * 1024
M4A_BIT_RATE = 48000
MERGED_WAV_NAME = "録音.wav"


class AudioError(ValueError):
    """受け取った音声が想定の形式でないときに送出する."""


def parse_wav(data: bytes) -> int:
    """切れ端のWAVを検証し、長さ（ミリ秒）を返す."""
    if len(data) > MAX_SEGMENT_BYTES:
        raise AudioError("音声の切れ端が大きすぎます。")
    try:
        with wave.open(io.BytesIO(data)) as wav:
            channels, width, rate, frames = (
                wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes(),
            )
    except (wave.Error, EOFError) as exc:
        raise AudioError("WAVとして読めませんでした。") from exc
    if (channels, width, rate) != (1, SAMPLE_WIDTH, SAMPLE_RATE):
        raise AudioError(f"16kHz・モノラル・16bitのWAVだけを受け付けます（届いたのは {rate}Hz・{channels}ch・{width * 8}bit）。")
    if frames == 0:
        raise AudioError("音声が空です。")
    return frames * 1000 // SAMPLE_RATE


def _tmp_path(path: Path) -> Path:
    """書き込み途中の一時ファイル名. 同じファイルへの書き込みが重なってもぶつからないよう毎回変える."""
    return path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")


def _write_atomic(path: Path, data: bytes) -> None:
    """一時ファイルに書いて fsync してから置き換える. 途中で落ちても壊れたファイルを残さない."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_samples(path: Path) -> np.ndarray:
    """WAVを -1.0〜1.0 の float32 にする（Whisperにそのまま渡せる形）."""
    with wave.open(str(path)) as wav:
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


class AudioStore:
    """会議ごとの音声フォルダを扱う."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # つなぐ作業（清書の前と、録音のダウンロード）が重ならないようにする.
        self._merge_lock = threading.Lock()

    def meeting_dir(self, meeting_id: int) -> Path:
        return self.root / str(meeting_id)

    def segment_path(self, meeting_id: int, seg_id: int) -> Path:
        return self.meeting_dir(meeting_id) / "segments" / f"{seg_id}.wav"

    def merged_path(self, meeting_id: int) -> Path:
        return self.meeting_dir(meeting_id) / MERGED_WAV_NAME

    def save_segment(self, meeting_id: int, seg_id: int, data: bytes) -> int:
        """切れ端を検証して保存し、長さ（ミリ秒）を返す. 送り直しなら上書きするだけ."""
        duration_ms = parse_wav(data)
        _write_atomic(self.segment_path(meeting_id, seg_id), data)
        return duration_ms

    def segment_ids_on_disk(self, meeting_id: int) -> list[int]:
        folder = self.meeting_dir(meeting_id) / "segments"
        if not folder.is_dir():
            return []
        return sorted(int(p.stem) for p in folder.glob("*.wav") if p.stem.isdigit())

    def segment_offsets(self, meeting_id: int) -> list[tuple[float, int]]:
        """つないだ録音の中で、各切れ端が始まる位置（秒）と seg_id. merge() と同じ順."""
        offsets, position = [], 0.0
        for seg_id in self.segment_ids_on_disk(meeting_id):
            with wave.open(str(self.segment_path(meeting_id, seg_id))) as part:
                offsets.append((position, seg_id))
                position += part.getnframes() / SAMPLE_RATE
        return offsets

    def merge(self, meeting_id: int) -> Path | None:
        """切れ端を録り始めた順につないで1本のWAVにする. 切れ端が無ければ None.

        切れ端そのものは消さない。つないだファイルが壊れても、元から作り直せるようにしておく。
        """
        with self._merge_lock:
            return self._merge(meeting_id)

    def _merge(self, meeting_id: int) -> Path | None:
        seg_ids = self.segment_ids_on_disk(meeting_id)
        if not seg_ids:
            return None
        target = self.merged_path(meeting_id)
        tmp = _tmp_path(target)
        with wave.open(str(tmp), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            for seg_id in seg_ids:
                with wave.open(str(self.segment_path(meeting_id, seg_id))) as part:
                    out.writeframes(part.readframes(part.getnframes()))
        os.replace(tmp, target)
        return target


def encode_m4a(wav_path: Path, target: Path) -> Path:
    """WAVを聞き返し用のm4a（AAC）にする. 1時間で約20MB."""
    import av  # faster-whisper と一緒に入る. 重いので使うときだけ読む.

    with wave.open(str(wav_path)) as wav:
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    container = av.open(str(tmp), "w", format="mp4")
    try:
        stream = container.add_stream("aac", rate=SAMPLE_RATE, layout="mono")
        stream.bit_rate = M4A_BIT_RATE
        # 1時間を超える録音を1枚のフレームで渡すとメモリを食うので、1秒ずつ渡す。
        for start in range(0, len(pcm), SAMPLE_RATE):
            frame = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(pcm[start : start + SAMPLE_RATE]).reshape(1, -1),
                format="s16", layout="mono",
            )
            frame.sample_rate = SAMPLE_RATE
            frame.pts = start
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
    os.replace(tmp, target)
    return target
