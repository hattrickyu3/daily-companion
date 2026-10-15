"""録り終えた音声ファイル（ボイスメモのm4aなど）を議事録の切れ端に分けるテスト."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.services.minutes_audio import AudioError, encode_m4a, parse_wav
from companion.services.minutes_import import decode_chunks, probe_duration_ms, recording_start
from test_minutes_audio import make_wav


def make_m4a(tmp_path: Path, seconds: float) -> Path:
    wav = tmp_path / "src.wav"
    wav.write_bytes(make_wav(seconds))
    return encode_m4a(wav, tmp_path / "ボイスメモ.m4a")


def test_m4aの長さがわかる(tmp_path: Path) -> None:
    path = make_m4a(tmp_path, 65)

    assert abs(probe_duration_ms(path) - 65000) < 200


def test_m4aを30秒ずつの16kHzモノラルのWAVに分ける(tmp_path: Path) -> None:
    path = make_m4a(tmp_path, 65)

    durations = [parse_wav(chunk) for chunk in decode_chunks(path)]

    assert durations[:2] == [30000, 30000]
    assert len(durations) == 3
    assert abs(sum(durations) - 65000) < 200


@pytest.mark.parametrize("content", [b"", b"this is not audio at all" * 100])
def test_音声として読めないファイルは断る(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "broken.m4a"
    path.write_bytes(content)

    with pytest.raises(AudioError):
        probe_duration_ms(path)
    with pytest.raises(AudioError):
        list(decode_chunks(path))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2026-10-15 14-00-00.m4a", (2026, 10, 15, 14, 0, 0)),
        ("20261015_140005.m4a", (2026, 10, 15, 14, 0, 5)),
        ("録音 2026-10-15 14.30.m4a", (2026, 10, 15, 14, 30, 0)),
    ],
)
def test_ファイル名の日時を録り始めの時刻にする(tmp_path: Path, name: str, expected: tuple[int, ...]) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Tokyo")
    path = tmp_path / name
    path.write_bytes(make_m4a(tmp_path, 1).read_bytes())

    start = recording_start(path, 1000, received_at=datetime(2026, 10, 15, 20, 0, tzinfo=tz))

    assert start == datetime(*expected, tzinfo=tz)


def test_日時がわからなければ届いた時刻から長さの分さかのぼる(tmp_path: Path) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    received = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    path = tmp_path / "New Recording 3.m4a"
    path.write_bytes(make_m4a(tmp_path, 1).read_bytes())

    assert recording_start(path, 65000, received_at=received) == received - timedelta(seconds=65)


def make_recorder_plus_caf(path: Path, seconds: float = 3.0) -> Path:
    """Recorder Plus が書くCAF（IMA4・16kHz・ステレオ）. パケット表に「0個」と書いてあり、そのままでは読めない."""
    import struct

    channels, frames_per_packet, bytes_per_packet = 2, 64, 34 * 2
    packets = int(seconds * 16000) // frames_per_packet
    desc = struct.pack(">d4sIIIII", 16000.0, b"ima4", 0, bytes_per_packet, frames_per_packet, channels, 0)
    # 最後のパケットの余り（64フレーム）を除いた数が「有効なフレーム数」として書いてある
    pakt = struct.pack(">qqii", 0, (packets - 1) * frames_per_packet, 0, frames_per_packet)
    chan = struct.pack(">III", (101 << 16) | 2, 0, 0)  # ステレオ
    data = b"\x00\x00\x00\x01" + b"\x00" * (packets * bytes_per_packet)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return kind + struct.pack(">q", len(body)) + body

    path.write_bytes(
        b"caff\x00\x01\x00\x00" + chunk(b"desc", desc) + chunk(b"chan", chan) + chunk(b"pakt", pakt)
        + chunk(b"free", b"\x00" * 3956) + chunk(b"data", data)
    )
    return path


def test_RecorderPlusのCAFを直してから読む(tmp_path: Path) -> None:
    from companion.services.minutes_import import repair_caf

    path = make_recorder_plus_caf(tmp_path / "2026_10_15_22_36_F1BC.caf", 3.0)
    with pytest.raises(AudioError):
        probe_duration_ms(path)

    assert repair_caf(path) is True

    assert abs(probe_duration_ms(path) - 3000) < 100
    durations = [parse_wav(chunk) for chunk in decode_chunks(path)]
    assert abs(sum(durations) - 3000) < 100


def test_直す必要のないファイルには触らない(tmp_path: Path) -> None:
    from companion.services.minutes_import import repair_caf

    m4a = make_m4a(tmp_path, 1)
    before = m4a.read_bytes()
    broken = tmp_path / "x.caf"
    broken.write_bytes(b"not a caf")

    assert repair_caf(m4a) is False
    assert repair_caf(broken) is False
    assert m4a.read_bytes() == before


def test_RecorderPlusのファイル名の日時を読む(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Tokyo")
    path = make_recorder_plus_caf(tmp_path / "2026_10_15_22_36_F1BCAE95-57F2-423A-A0C5-86FD2FEF0743.caf")

    assert recording_start(path, 3000, received_at=datetime(2026, 10, 15, 23, 0, tzinfo=tz)) == datetime(2026, 10, 15, 22, 36, tzinfo=tz)
