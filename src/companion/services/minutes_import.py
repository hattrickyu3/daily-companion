"""録り終えた音声ファイル（iPhoneのボイスメモのm4aなど）を議事録の切れ端にする.

Safariで録ると画面を消したときに止まるので、裏でも録れるボイスメモで録っておき、
あとからファイルを送ってもらう。届いたファイルは16kHz・モノラル・16bitのWAVに直し、
録音中に届く切れ端と同じ30秒ずつに分ける。そこから先（保存・文字起こし・清書）は録音と同じ流れに乗せる。
"""

from __future__ import annotations

import io
import logging
import re
import wave
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from companion.services.minutes_audio import SAMPLE_RATE, SAMPLE_WIDTH, AudioError

log = logging.getLogger(__name__)

IMPORT_SEGMENT_SEC = 30
# これより長い録音は受けない（文字にし直すときに録音全体をメモリに載せるため）.
MAX_IMPORT_SEC = 4 * 60 * 60
UNREADABLE = "音声ファイルとして読めませんでした。"
# ファイル名の中の日時（2026-10-15 14-00-00 / 20261015_140005 / 2026-10-15 14.30 など）.
NAME_DATETIME = re.compile(
    r"(?P<y>20\d{2})[-_.]?(?P<mo>\d{2})[-_.]?(?P<d>\d{2})[ _T-]?"
    r"(?P<h>\d{2})[-_.:]?(?P<mi>\d{2})(?:[-_.:]?(?P<s>\d{2}))?"
)
# 届いた時刻より先の日時は、端末の時計のずれとして1日まで受ける.
FUTURE_SLACK = timedelta(days=1)


def probe_duration_ms(path: Path) -> int:
    """音声ファイルの長さ（ミリ秒）. 中身を全部は読まず、ファイルに書いてある長さを見る.

    Raises:
        AudioError: 音声として読めない、または音声が入っていない.
    """
    import av  # faster-whisper と一緒に入る. 重いので使うときだけ読む.

    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise AudioError("音声の入っていないファイルです。")
            stream = container.streams.audio[0]
            if stream.duration and stream.time_base:
                return int(stream.duration * stream.time_base * 1000)
            if container.duration:
                return container.duration // 1000  # マイクロ秒で入っている
            return 0
    except av.FFmpegError as exc:
        raise AudioError(UNREADABLE) from exc


def decode_chunks(path: Path, *, segment_sec: int = IMPORT_SEGMENT_SEC) -> Iterator[bytes]:
    """音声ファイルを segment_sec 秒ずつのWAV（16kHz・モノラル・16bit）にして順に返す.

    一度に全部をメモリに載せないよう、たまった分から返す。

    Raises:
        AudioError: 音声として読めない（途中で読めなくなったときは、そこまでの分を返したあとに送出する）.
    """
    import av

    chunk_samples = segment_sec * SAMPLE_RATE
    max_samples = MAX_IMPORT_SEC * SAMPLE_RATE
    pending: list[np.ndarray] = []
    pending_samples = total = 0
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise AudioError("音声の入っていないファイルです。")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in _frames(container, resampler):
                pcm = frame.to_ndarray().reshape(-1)[: max_samples - total]
                pending.append(pcm)
                pending_samples += len(pcm)
                total += len(pcm)
                while pending_samples >= chunk_samples:
                    joined = np.concatenate(pending)
                    yield _to_wav(joined[:chunk_samples])
                    pending, pending_samples = [joined[chunk_samples:]], len(joined) - chunk_samples
                if total >= max_samples:
                    break
    except av.FFmpegError as exc:
        raise AudioError(UNREADABLE) from exc
    if pending_samples:
        yield _to_wav(np.concatenate(pending))


def _frames(container, resampler) -> Iterator:
    for frame in container.decode(container.streams.audio[0]):
        yield from resampler.resample(frame)
    yield from resampler.resample(None)  # 変換しきれずに残っている分


def _to_wav(pcm: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())
    return buffer.getvalue()


def recording_start(path: Path, duration_ms: int, *, received_at: datetime) -> datetime:
    """録り始めの時刻. ファイル名の日時 → ファイルに書かれた作成日時 → 届いた時刻から長さの分さかのぼる、の順に使う.

    録音アプリはWi-Fiのときにまとめて送ることがあるので、届いた時刻だけでは録った時刻とずれる。
    """
    tz = received_at.tzinfo
    latest = received_at + FUTURE_SLACK
    for candidate in (_name_datetime(path.stem, tz), _metadata_datetime(path, tz)):
        if candidate is not None and datetime(2001, 1, 1, tzinfo=tz) <= candidate <= latest:
            return candidate
    return received_at - timedelta(milliseconds=duration_ms)


def _name_datetime(stem: str, tz) -> datetime | None:
    match = NAME_DATETIME.search(stem)
    if match is None:
        return None
    try:
        return datetime(
            int(match["y"]), int(match["mo"]), int(match["d"]),
            int(match["h"]), int(match["mi"]), int(match["s"] or 0), tzinfo=tz,
        )
    except ValueError:  # 13月など、日時でない数字の並び
        return None


def _metadata_datetime(path: Path, tz) -> datetime | None:
    """m4aなどに書かれている作成日時（creation_time. 録り始めの時刻）."""
    import av

    try:
        with av.open(str(path)) as container:
            raw = container.metadata.get("creation_time")
    except av.FFmpegError:
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        log.warning("unexpected creation_time %r in %s", raw, path.name)
        return None


def repair_caf(path: Path) -> bool:
    """Recorder Plus のCAFを、ffmpeg（PyAV）で読める形に直す. 直したら True.

    Recorder Plus は IMA4（1パケットの大きさが決まっている形式）で録り、パケット表（pakt）に
    「0個」と書いて残す。決まった大きさの形式ならパケット表は要らないが、ffmpeg は「0個」の表を
    壊れたファイルとして断る。そこで表を空き（free）に書き換える。中身の音声には触らない。
    """
    try:
        with path.open("r+b") as handle:
            if handle.read(8)[:4] != b"caff":
                return False
            bytes_per_packet = frames_per_packet = 0
            while header := handle.read(12):
                if len(header) < 12:
                    return False
                kind, size = header[:4], int.from_bytes(header[4:], "big", signed=True)
                if kind == b"desc":
                    desc = handle.read(size)
                    bytes_per_packet = int.from_bytes(desc[16:20], "big")
                    frames_per_packet = int.from_bytes(desc[20:24], "big")
                    continue
                if kind == b"pakt":
                    packets = int.from_bytes(handle.read(8), "big", signed=True)
                    if packets != 0 or not (bytes_per_packet and frames_per_packet):
                        return False
                    handle.seek(-20, 1)  # 種類の4バイトの位置へ戻る
                    handle.write(b"free")
                    return True
                if kind == b"data" or size < 0:
                    return False  # 表より先に音声が来た（直すところが無い）
                handle.seek(size, 1)
    except OSError:
        return False
    return False
