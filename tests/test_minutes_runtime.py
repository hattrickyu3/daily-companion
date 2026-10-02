"""議事録づくりの流れのテスト.

Whisper・Haiku・Opus は偽物に差し替え、受け取り→文字起こし→清書→書き出しの流れと、
途中で失敗したときに音声と文字起こしが残ることを確かめる。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from companion.integrations.claude_runner import ClaudeError
from companion.integrations.whisper import TimedText, WhisperError, clean_text
from companion.services.minutes_audio import AudioStore
from companion.services.minutes_editor import LiveEditor
from companion.services.minutes_final import timed_lines
from companion.services.minutes_hub import MinutesHub
from companion.services.minutes_runtime import MinutesRuntime
from test_minutes_audio import make_wav

TZ = ZoneInfo("Asia/Tokyo")
START = datetime(2026, 10, 2, 18, 30, tzinfo=TZ)
SEG = int(START.timestamp() * 1000)


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("時間内に終わらなかった")


class FakeTranscriber:
    def __init__(self, texts: list[str | Exception]) -> None:
        self.texts = texts
        self.prompts: list[str] = []

    def transcribe(self, samples, *, prompt: str = "") -> str:
        self.prompts.append(prompt)
        result = self.texts.pop(0) if self.texts else "続き"
        if isinstance(result, Exception):
            raise result
        return result

    def unload_if_idle(self) -> None:
        pass


class FakeEditor:
    def __init__(self) -> None:
        self.notified = 0
        self.stopped = False

    def start(self) -> None: ...
    def notify(self) -> None: self.notified += 1
    def stop(self) -> None: self.stopped = True


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "minutes.db"
    with session(path) as conn:
        apply_schema(conn)
    return path


class FakeLongTranscriber:
    def __init__(self, result: list[TimedText] | Exception) -> None:
        self.result = result
        self.calls = 0

    def transcribe_long(self, samples) -> list[TimedText]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_runtime(
    db_path: Path, tmp_path: Path, transcriber, *, polisher=None, editor=None, final_transcriber=None, diarizer=None
) -> MinutesRuntime:
    runtime = MinutesRuntime(
        open_db=lambda: session(db_path),
        audio=AudioStore(tmp_path / "audio"),
        transcriber=transcriber,
        tz=TZ,
        minutes_dir=tmp_path / "vault" / "研究" / "ゼミ議事録",
        final_transcriber=final_transcriber,
        diarizer=diarizer,
        editor_factory=lambda _id: editor or FakeEditor(),
        polisher=polisher or (lambda meeting, transcript: "## 要点\n\n- 清書した"),
    )
    runtime.start()
    return runtime


def new_meeting(db_path: Path) -> int:
    with session(db_path) as conn:
        return db.create_meeting(conn, "ゼミ", START)


def segments(db_path: Path, meeting_id: int) -> list[db.Segment]:
    with session(db_path) as conn:
        return db.list_segments(conn, meeting_id)


def meeting(db_path: Path, meeting_id: int) -> db.Meeting:
    with session(db_path) as conn:
        return db.get_meeting(conn, meeting_id)


def test_届いた切れ端を文字起こしして編集係を起こす(db_path, tmp_path) -> None:
    editor = FakeEditor()
    transcriber = FakeTranscriber(["始めます", "田中さんどうぞ"])
    runtime = make_runtime(db_path, tmp_path, transcriber, editor=editor)
    meeting_id = new_meeting(db_path)

    runtime.receive_segment(meeting_id, SEG, make_wav())
    runtime.receive_segment(meeting_id, SEG + 2000, make_wav())

    wait_until(lambda: all(s.status == "done" for s in segments(db_path, meeting_id)) and len(segments(db_path, meeting_id)) == 2)
    assert [s.text for s in segments(db_path, meeting_id)] == ["始めます", "田中さんどうぞ"]
    assert transcriber.prompts[1] == "始めます"  # 直前の文字起こしを続きの手がかりに渡す
    assert editor.notified == 2


def test_送り直された切れ端は二重に文字起こししない(db_path, tmp_path) -> None:
    transcriber = FakeTranscriber(["一度だけ"])
    runtime = make_runtime(db_path, tmp_path, transcriber)
    meeting_id = new_meeting(db_path)

    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")
    runtime.receive_segment(meeting_id, SEG, make_wav())
    time.sleep(0.3)

    assert len(transcriber.prompts) == 1
    assert len(segments(db_path, meeting_id)) == 1


def test_文字起こしに失敗しても音声は残りやり直せる(db_path, tmp_path) -> None:
    transcriber = FakeTranscriber([WhisperError("GPUが落ちた"), "やり直せた"])
    runtime = make_runtime(db_path, tmp_path, transcriber)
    meeting_id = new_meeting(db_path)

    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "failed")
    assert runtime.audio.segment_path(meeting_id, SEG).exists()

    assert runtime.retry_transcription(meeting_id) == 1
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")
    assert segments(db_path, meeting_id)[0].text == "やり直せた"


def test_起動し直すと文字起こし待ちの切れ端を拾い直す(db_path, tmp_path) -> None:
    meeting_id = new_meeting(db_path)
    AudioStore(tmp_path / "audio").save_segment(meeting_id, SEG, make_wav())
    with session(db_path) as conn:
        db.add_segment(conn, meeting_id, SEG, 1000)  # 文字起こしの前にアプリが落ちた状態

    make_runtime(db_path, tmp_path, FakeTranscriber(["拾い直した"]))

    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")


def test_録音を終えると清書して保管庫に書き出す(db_path, tmp_path) -> None:
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["中間発表は10月9日です"]))
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav(2.0))
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    folder = tmp_path / "vault" / "研究" / "ゼミ議事録"
    note = (folder / "2026-10-02 ゼミ.md").read_text(encoding="utf-8")
    assert note.startswith("# 2026-10-02 ゼミ\n← [[ゼミ議事録まとめ]]")
    assert "- 清書した" in note
    assert "[[音声/2026-10-02 ゼミ.m4a|2026-10-02 ゼミ.m4a]]" in note
    assert "中間発表は10月9日です" in (folder / "2026-10-02 ゼミ 文字起こし.md").read_text(encoding="utf-8")
    assert (folder / "音声" / "2026-10-02 ゼミ.m4a").exists()
    assert "- [[2026-10-02 ゼミ]]" in (folder / "ゼミ議事録まとめ.md").read_text(encoding="utf-8")
    assert meeting(db_path, meeting_id).note_path == str(folder / "2026-10-02 ゼミ.md")


def test_清書に失敗しても下書きでノートを作り失敗として残す(db_path, tmp_path) -> None:
    def broken_polisher(meeting, transcript):
        raise ClaudeError("Claudeの認証が切れています。")

    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["話した"]), polisher=broken_polisher)
    meeting_id = new_meeting(db_path)
    with session(db_path) as conn:
        db.save_draft(conn, meeting_id, {"要点": "- 下書きの要点"})
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "failed")

    result = meeting(db_path, meeting_id)
    assert "認証が切れています" in result.error
    note = (tmp_path / "vault" / "研究" / "ゼミ議事録" / "2026-10-02 ゼミ.md").read_text(encoding="utf-8")
    assert "清書できていません" in note
    assert "- 下書きの要点" in note
    assert runtime.audio.merged_path(meeting_id).exists()


def test_清書をやり直すと同じノートを上書きする(db_path, tmp_path) -> None:
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["話した"]))
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")
    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    with session(db_path) as conn:
        db.set_status(conn, meeting_id, db.DONE)
    assert runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    folder = tmp_path / "vault" / "研究" / "ゼミ議事録"
    assert not (folder / "2026-10-02 ゼミ (2).md").exists()


def test_Whisperのでっち上げの決まり文句は捨てる() -> None:
    assert clean_text(" ご視聴ありがとうございました ") == ""
    assert clean_text("実験の結果を報告します") == "実験の結果を報告します"


class FakeSession:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.turns = 0
        self.alive = False
        self.messages: list[str] = []

    def ask(self, text, *, on_delta, timeout_sec):
        self.messages.append(text)
        self.alive = True
        self.turns += 1
        for chunk in (self.reply[:5], self.reply[5:]):
            on_delta(chunk)
        return self.reply

    def close(self) -> None:
        self.alive = False


def test_編集係は未反映の発言を渡して下書きを更新する(db_path) -> None:
    meeting_id = new_meeting(db_path)
    with session(db_path) as conn:
        db.add_segment(conn, meeting_id, SEG, 1000)
        db.finish_segment(conn, meeting_id, SEG, "次回は10月9日に中間発表")
    hub = MinutesHub()
    fake = FakeSession("@@ 次回\n- 10月9日 中間発表")
    editor = LiveEditor(meeting_id, hub=hub, open_db=lambda: session(db_path), tz=TZ, session=fake)

    editor.run_turn()

    assert "これまでの議事録" in fake.messages[0]  # 最初の1回は今の議事録も渡す
    assert "[18:30:00] 次回は10月9日に中間発表" in fake.messages[0]
    assert meeting(db_path, meeting_id).draft == {"次回": "- 10月9日 中間発表"}
    with session(db_path) as conn:
        assert db.undrafted_segments(conn, meeting_id) == []


def test_編集係が失敗した発言は未反映のまま残る(db_path) -> None:
    meeting_id = new_meeting(db_path)
    with session(db_path) as conn:
        db.add_segment(conn, meeting_id, SEG, 1000)
        db.finish_segment(conn, meeting_id, SEG, "話した")

    class BrokenSession(FakeSession):
        def ask(self, text, *, on_delta, timeout_sec):
            raise ClaudeError("落ちた")

    editor = LiveEditor(meeting_id, hub=MinutesHub(), open_db=lambda: session(db_path), tz=TZ, session=BrokenSession(""))

    with pytest.raises(ClaudeError):
        editor.run_turn()
    with session(db_path) as conn:
        assert len(db.undrafted_segments(conn, meeting_id)) == 1


def test_読めない音声ファイルは失敗にして待ちのまま残さない(db_path, tmp_path) -> None:
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber([]))
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")
    runtime.audio.segment_path(meeting_id, SEG).write_bytes(b"broken")

    with session(db_path) as conn:
        conn.execute("UPDATE meeting_segments SET status = 'failed'")
    runtime.retry_transcription(meeting_id)

    wait_until(lambda: segments(db_path, meeting_id)[0].status == "failed")


def test_清書のやり直しに失敗しても前の清書を残す(db_path, tmp_path) -> None:
    replies: list[str | Exception] = ["## 要点\n\n- 最初の清書", ClaudeError("落ちた")]

    def polisher(meeting, transcript):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["話した"]), polisher=polisher)
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")
    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "failed")

    result = meeting(db_path, meeting_id)
    assert result.final_text == "## 要点\n\n- 最初の清書"
    assert "前の清書はそのまま" in result.error
    note = (tmp_path / "vault" / "研究" / "ゼミ議事録" / "2026-10-02 ゼミ.md").read_text(encoding="utf-8")
    assert "- 最初の清書" in note


def test_文字起こしに失敗した部分があれば清書後も要確認にする(db_path, tmp_path) -> None:
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber([WhisperError("だめ"), "二つ目"]))
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    runtime.receive_segment(meeting_id, SEG + 2000, make_wav())
    wait_until(lambda: all(s.status != "pending" for s in segments(db_path, meeting_id)))

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status in ("done", "failed"))

    result = meeting(db_path, meeting_id)
    assert result.status == "failed"
    assert "1か所" in result.error


def test_録音を終えた会議には編集係を新しく作らない(db_path, tmp_path) -> None:
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber([]))
    meeting_id = new_meeting(db_path)
    runtime._finishing.add(meeting_id)

    assert runtime._editor_for(meeting_id) is None


def test_通しの文字起こしの位置を録り始めの時刻に直す() -> None:
    # 切れ端は 18:30:00 から1秒、18:31:00 から2秒. つないだ録音の1.5秒目は2つ目の0.5秒目.
    offsets = [(0.0, SEG), (1.0, SEG + 60_000)]
    timed = [TimedText(start=0.2, end=0.7, text="始めます"), TimedText(start=1.5, end=2.0, text="続きです")]

    assert timed_lines(timed, offsets, TZ) == ["[18:30:00] 始めます", "[18:31:00] 続きです"]


def test_録音を終えると録音全体を文字にし直してノートに使う(db_path, tmp_path) -> None:
    final = FakeLongTranscriber([TimedText(start=0.0, end=0.5, text="正確に聞き取れた文")])
    seen: list[str] = []

    def polisher(meeting, transcript):
        seen.append(transcript)
        return "## 要点\n\n- 清書した"

    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["雑な聞き取り"]), polisher=polisher, final_transcriber=final)
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    assert seen == ["[18:30:00] 正確に聞き取れた文"]
    transcript = (tmp_path / "vault" / "研究" / "ゼミ議事録" / "2026-10-02 ゼミ 文字起こし.md").read_text(encoding="utf-8")
    assert "正確に聞き取れた文" in transcript
    assert "雑な聞き取り" not in transcript


def test_文字にし直せなければ録音中の文字起こしを使う(db_path, tmp_path) -> None:
    final = FakeLongTranscriber(WhisperError("VRAMが足りない"))
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["録音中の聞き取り"]), final_transcriber=final)
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    transcript = (tmp_path / "vault" / "研究" / "ゼミ議事録" / "2026-10-02 ゼミ 文字起こし.md").read_text(encoding="utf-8")
    assert "録音中の聞き取り" in transcript
    assert final.calls == 1


def test_文字にし直せば録音中に失敗した部分があっても完成にする(db_path, tmp_path) -> None:
    final = FakeLongTranscriber([TimedText(start=0.0, end=0.5, text="全部聞き取れた")])
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber([WhisperError("だめ")]), final_transcriber=final)
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "failed")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status in ("done", "failed"))

    assert meeting(db_path, meeting_id).status == "done"


class FakeDiarizer:
    def __init__(self, turns) -> None:
        self.turns = turns
        self.num_speakers = "未呼び出し"

    def diarize(self, samples, *, num_speakers=None):
        self.num_speakers = num_speakers
        return self.turns


def test_参加者の人数で聞き分け清書がわかった名前を文字起こしに入れる(db_path, tmp_path) -> None:
    from companion.integrations.diarization import SpeakerTurn

    final = FakeLongTranscriber([
        TimedText(start=0.0, end=0.4, text="田中さんどうぞ"),
        TimedText(start=0.5, end=0.9, text="はい田中です"),
    ])
    diarizer = FakeDiarizer([SpeakerTurn(0.0, 0.45, 0), SpeakerTurn(0.45, 1.0, 1)])
    seen: list[str] = []

    def polisher(meeting, transcript):
        seen.append(transcript)
        return "## 要点\n\n- 田中さんが報告した\n\n@@話者 話者1=不明, 話者2=田中"

    runtime = make_runtime(
        db_path, tmp_path, FakeTranscriber(["x"]), polisher=polisher, final_transcriber=final, diarizer=diarizer
    )
    with session(db_path) as conn:
        meeting_id = db.create_meeting(conn, "ゼミ", START, participants="先生、田中")
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    assert diarizer.num_speakers == 2
    assert seen == ["[18:30:00] 話者1: 田中さんどうぞ\n[18:30:00] 話者2: はい田中です"]
    folder = tmp_path / "vault" / "研究" / "ゼミ議事録"
    transcript = (folder / "2026-10-02 ゼミ 文字起こし.md").read_text(encoding="utf-8")
    assert "話者1: 田中さんどうぞ" in transcript
    assert "田中（話者2）: はい田中です" in transcript
    note = (folder / "2026-10-02 ゼミ.md").read_text(encoding="utf-8")
    assert "@@話者" not in note
    assert meeting(db_path, meeting_id).final_text == "## 要点\n\n- 田中さんが報告した"


def test_参加者が無ければ人数は自動にする(db_path, tmp_path) -> None:
    diarizer = FakeDiarizer([])
    final = FakeLongTranscriber([TimedText(start=0.0, end=0.4, text="話した")])
    runtime = make_runtime(db_path, tmp_path, FakeTranscriber(["x"]), final_transcriber=final, diarizer=diarizer)
    meeting_id = new_meeting(db_path)
    runtime.receive_segment(meeting_id, SEG, make_wav())
    wait_until(lambda: segments(db_path, meeting_id)[0].status == "done")

    runtime.finish(meeting_id)
    wait_until(lambda: meeting(db_path, meeting_id).status == "done")

    assert diarizer.num_speakers is None
