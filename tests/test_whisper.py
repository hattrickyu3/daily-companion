"""Whisperまわりのうち、モデルを読まずに確かめられるところ."""

from __future__ import annotations

import numpy as np
from faster_whisper import vad

from companion.integrations.whisper import LONG_VAD_PARAMETERS, find_speech

RATE = 16000


def test_声は区切りごとに探し直し録音の先頭からの位置で返す(monkeypatch) -> None:
    calls: list[tuple[int, float]] = []

    def fake_timestamps(audio, options):
        calls.append((len(audio), options.threshold))
        return [{"start": 100, "end": 200}]

    monkeypatch.setattr(vad, "get_speech_timestamps", fake_timestamps)

    chunks = find_speech(np.zeros(RATE * 25, dtype=np.float32), block_sec=10)

    assert calls == [(RATE * 10, LONG_VAD_PARAMETERS["threshold"])] * 2 + [(RATE * 5, LONG_VAD_PARAMETERS["threshold"])]
    assert chunks == [
        {"start": 100, "end": 200},
        {"start": RATE * 10 + 100, "end": RATE * 10 + 200},
        {"start": RATE * 20 + 100, "end": RATE * 20 + 200},
    ]


def test_声が無ければ空(monkeypatch) -> None:
    monkeypatch.setattr(vad, "get_speech_timestamps", lambda audio, options: [])

    assert find_speech(np.zeros(RATE * 3, dtype=np.float32)) == []
