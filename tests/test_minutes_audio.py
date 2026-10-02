"""議事録の音声ファイルのテスト. 音声は議事録づくりがどこで失敗しても残ることが一番大事."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from companion.services.minutes_audio import AudioError, AudioStore, encode_m4a, parse_wav, read_samples


def make_wav(seconds: float = 1.0, *, rate: int = 16000, channels: int = 1, value: int = 1000) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.full(frames * channels, value, dtype=np.int16).tobytes())
    return buffer.getvalue()


def test_16kHzモノラルのWAVは長さを返す() -> None:
    assert parse_wav(make_wav(1.5)) == 1500


@pytest.mark.parametrize(
    "data",
    [make_wav(rate=48000), make_wav(channels=2), b"not a wav", make_wav(0)],
    ids=["48kHz", "stereo", "not-wav", "empty"],
)
def test_想定外の音声は受け付けない(data: bytes) -> None:
    with pytest.raises(AudioError):
        parse_wav(data)


def test_切れ端を保存すると読み戻せる(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)

    store.save_segment(1, 1_790_000_000_000, make_wav(0.5))

    path = store.segment_path(1, 1_790_000_000_000)
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    assert len(read_samples(path)) == 8000


def test_同じ切れ端を送り直しても1つのまま(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)

    store.save_segment(1, 1_790_000_000_000, make_wav(0.5))
    store.save_segment(1, 1_790_000_000_000, make_wav(0.5))

    assert store.segment_ids_on_disk(1) == [1_790_000_000_000]


def test_切れ端を録った順につなぎ元の切れ端は残す(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    store.save_segment(1, 1_790_000_002_000, make_wav(1.0, value=2000))
    store.save_segment(1, 1_790_000_000_000, make_wav(0.5, value=1000))

    merged = store.merge(1)

    samples = read_samples(merged)
    assert len(samples) == 24000
    assert samples[0] == pytest.approx(1000 / 32768)
    assert samples[-1] == pytest.approx(2000 / 32768)
    assert len(store.segment_ids_on_disk(1)) == 2


def test_切れ端が無ければつながない(tmp_path: Path) -> None:
    assert AudioStore(tmp_path).merge(1) is None


def test_聞き返し用のm4aを作れる(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    store.save_segment(1, 1_790_000_000_000, make_wav(2.0))

    target = encode_m4a(store.merge(1), tmp_path / "out.m4a")

    assert target.exists() and target.stat().st_size > 0


def test_つないだ録音の中での各切れ端の位置がわかる(tmp_path: Path) -> None:
    store = AudioStore(tmp_path)
    store.save_segment(1, 1_790_000_002_000, make_wav(2.0))
    store.save_segment(1, 1_790_000_000_000, make_wav(0.5))

    assert store.segment_offsets(1) == [(0.0, 1_790_000_000_000), (0.5, 1_790_000_002_000)]
