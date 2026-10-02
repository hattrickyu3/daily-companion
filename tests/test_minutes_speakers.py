"""話者の聞き分けと、名前の結び付けのテスト."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from companion.integrations.diarization import SpeakerTurn, speaker_for
from companion.integrations.whisper import TimedText
from companion.services.minutes_final import timed_lines
from companion.services.minutes_speakers import (
    apply_speaker_names,
    parse_participants,
    split_speaker_map,
)

TZ = ZoneInfo("Asia/Tokyo")
SEG = int(datetime(2026, 10, 2, 18, 30, tzinfo=TZ).timestamp() * 1000)


def test_参加者はいろいろな区切りで入れられ重複は1つにする() -> None:
    assert parse_participants(" 自分、田中,佐藤 先生・田中 ") == ["自分", "田中", "佐藤", "先生"]
    assert parse_participants("") == []


def test_いちばん長く重なる話者を選ぶ() -> None:
    turns = [SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(1.5, 5.0, 1)]

    assert speaker_for(1.0, 4.0, turns) == 1
    assert speaker_for(0.0, 1.0, turns) == 0
    assert speaker_for(6.0, 7.0, turns) is None


def test_文字起こしの行に話者の番号を付ける() -> None:
    timed = [TimedText(0.0, 2.0, "始めます"), TimedText(2.5, 4.0, "はい"), TimedText(9.0, 9.5, "うん")]
    turns = [SpeakerTurn(0.0, 2.2, 0), SpeakerTurn(2.3, 4.5, 1)]

    lines = timed_lines(timed, [(0.0, SEG)], TZ, turns)

    assert lines == ["[18:30:00] 話者1: 始めます", "[18:30:02] 話者2: はい", "[18:30:09] うん"]


def test_話者の聞き分けが無ければ番号は付けない() -> None:
    assert timed_lines([TimedText(0.0, 1.0, "始めます")], [(0.0, SEG)], TZ) == ["[18:30:00] 始めます"]


def test_清書の最後の対応表を取り出して本文から消す() -> None:
    body, mapping = split_speaker_map("## 要点\n\n- 田中さんが報告\n\n@@話者 話者1=田中, 話者2=不明, 話者3 = 佐藤")

    assert body == "## 要点\n\n- 田中さんが報告"
    assert mapping == {"話者1": "田中", "話者3": "佐藤"}


def test_対応表が無ければ本文はそのまま() -> None:
    assert split_speaker_map("## 要点\n\n- x") == ("## 要点\n\n- x", {})


def test_わかった名前を文字起こしに入れる() -> None:
    lines = ["[18:30:00] 話者1: 始めます", "[18:30:02] 話者2: はい", "[18:30:09] うん"]

    renamed = apply_speaker_names(lines, {"話者1": "田中"})

    assert renamed == ["[18:30:00] 田中（話者1）: 始めます", "[18:30:02] 話者2: はい", "[18:30:09] うん"]


def test_話者10と話者1を取り違えない() -> None:
    assert apply_speaker_names(["[1] 話者10: x"], {"話者1": "田中"}) == ["[1] 話者10: x"]
