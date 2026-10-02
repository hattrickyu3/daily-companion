"""議事録づくりの全体の流れ.

    切れ端が届く → ①ファイルに保存 → ②文字起こし（Whisper） → ③Haikuが議事録を編集
    録音終了     → 文字起こしを待つ → 音声をつなぐ → Sonnet 5.5 が清書 → 保管庫へ書き出し

①が済んだ時点でiPhoneに「受け取った」と返す。②③はあとの工程なので、
どこで失敗しても音声は残り、やり直せる。アプリが落ちても、起動し直したときに
文字起こし待ちの切れ端と清書中の会議を拾い直す。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Any
from zoneinfo import ZoneInfo

from companion.db import meetings as db
from companion.integrations.claude_runner import ClaudeError
from companion.integrations.diarization import Diarizer, DiarizationError, SpeakerTurn
from companion.integrations.whisper import Transcriber, WhisperError
from companion.services.minutes_audio import AudioStore, encode_m4a, read_samples
from companion.services.minutes_draft import render_markdown
from companion.services.minutes_editor import LiveEditor
from companion.services.minutes_final import polish, timed_lines, transcript_lines, write_notes
from companion.services.minutes_hub import MinutesHub
from companion.services.minutes_speakers import apply_speaker_names, parse_participants, split_speaker_map

log = logging.getLogger(__name__)

WORKER_IDLE_SEC = 60
# 録音を終えたとき、文字起こし待ちの切れ端が片付くのを待つ. 減り続けている間は待ち、
# この時間まったく減らなければ（文字起こしが止まっている）待つのをやめる.
FINISH_STALL_SEC = 300
M4A_NAME = "録音.m4a"


class MinutesRuntime:
    """アプリ全体で1つ. 文字起こしの順番待ちと、会議ごとの編集係を持つ."""

    def __init__(
        self,
        *,
        open_db: Callable[[], AbstractContextManager[Connection]],
        audio: AudioStore,
        transcriber: Transcriber,
        tz: ZoneInfo,
        minutes_dir: Path | None,
        final_transcriber: Transcriber | None = None,
        diarizer: Diarizer | None = None,
        hub: MinutesHub | None = None,
        editor_factory: Callable[[int], LiveEditor] | None = None,
        polisher: Callable[[db.Meeting, str], str] = polish,
    ) -> None:
        self.hub = hub or MinutesHub()
        self.audio = audio
        self._open_db = open_db
        self._transcriber = transcriber
        # 録音を終えたあと、録音全体を正確なモデルで文字にし直す係. None なら録音中の文字起こしをそのまま使う.
        self._final_transcriber = final_transcriber
        # 誰が話したかを声で聞き分ける係. None なら話者は付けない.
        self._diarizer = diarizer
        self._tz = tz
        self._minutes_dir = minutes_dir
        self._polisher = polisher
        self._editor_factory = editor_factory or (
            lambda meeting_id: LiveEditor(meeting_id, hub=self.hub, open_db=open_db, tz=tz)
        )
        self._queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self._queued: set[tuple[int, int]] = set()
        self._editors: dict[int, LiveEditor] = {}
        self._finishing: set[int] = set()
        self._lock = threading.Lock()
        self._started = False

    # ---- 起動 ----

    def start(self) -> None:
        """作業スレッドを動かし、前回の残りを拾い直す. 何度呼んでもよい."""
        with self._lock:
            if self._started:
                return
            self._started = True
        with self._open_db() as conn:
            leftovers = db.pending_segments(conn)
            unfinished = [m.id for m in db.list_meetings(conn) if m.status == db.FINISHING]
        for segment in leftovers:
            self._enqueue(segment.meeting_id, segment.seg_id)
        threading.Thread(target=self._worker, name="minutes-transcriber", daemon=True).start()
        for meeting_id in unfinished:
            self.finish(meeting_id)

    # ---- 切れ端の受け取りと文字起こし ----

    def receive_segment(self, meeting_id: int, seg_id: int, data: bytes) -> None:
        """切れ端を保存して文字起こしの順番待ちに入れる. 送り直しでも安全.

        Raises:
            AudioError: WAVの形式が想定と違う.
        """
        duration_ms = self.audio.save_segment(meeting_id, seg_id, data)
        with self._open_db() as conn:
            db.add_segment(conn, meeting_id, seg_id, duration_ms)
            segment = db.get_segment(conn, meeting_id, seg_id)
        if segment is not None and segment.status == db.SEGMENT_PENDING:
            self.hub.publish(meeting_id, self.segment_event(segment))
            self._enqueue(meeting_id, seg_id)

    def retry_transcription(self, meeting_id: int) -> int:
        with self._open_db() as conn:
            seg_ids = db.retry_failed_segments(conn, meeting_id)
        for seg_id in seg_ids:
            self._enqueue(meeting_id, seg_id)
        return len(seg_ids)

    def _enqueue(self, meeting_id: int, seg_id: int) -> None:
        key = (meeting_id, seg_id)
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
        self._queue.put(key)

    def _worker(self) -> None:
        while True:
            try:
                meeting_id, seg_id = self._queue.get(timeout=WORKER_IDLE_SEC)
            except queue.Empty:
                self._transcriber.unload_if_idle()
                continue
            try:
                self._transcribe_one(meeting_id, seg_id)
            except Exception:  # noqa: BLE001 - このスレッドが落ちると以後の文字起こしが止まる
                log.exception("unexpected error transcribing %s/%s", meeting_id, seg_id)
            finally:
                with self._lock:
                    self._queued.discard((meeting_id, seg_id))

    def _transcribe_one(self, meeting_id: int, seg_id: int) -> None:
        with self._open_db() as conn:
            prompt = db.previous_text(conn, meeting_id, seg_id)
        try:
            text = self._transcriber.transcribe(read_samples(self.audio.segment_path(meeting_id, seg_id)), prompt=prompt)
        except Exception as exc:  # noqa: BLE001 - 読めないWAVなども「失敗」にしないと、待ちのまま残り続ける
            log.warning("transcription failed for %s/%s: %s", meeting_id, seg_id, exc)
            with self._open_db() as conn:
                db.fail_segment(conn, meeting_id, seg_id, str(exc))
                segment = db.get_segment(conn, meeting_id, seg_id)
        else:
            with self._open_db() as conn:
                db.finish_segment(conn, meeting_id, seg_id, text)
                segment = db.get_segment(conn, meeting_id, seg_id)
                meeting = db.get_meeting(conn, meeting_id)
            editor = self._editor_for(meeting_id) if meeting is not None and meeting.status == db.RECORDING else None
            if editor is not None:
                editor.notify()
        if segment is not None:
            self.hub.publish(meeting_id, self.segment_event(segment))

    def _editor_for(self, meeting_id: int) -> LiveEditor | None:
        """録音中の会議の編集係. 録音を終えたところなら None（新しく起動しない）."""
        with self._lock:
            if meeting_id in self._finishing:
                return None
            editor = self._editors.get(meeting_id)
            if editor is None:
                editor = self._editor_factory(meeting_id)
                self._editors[meeting_id] = editor
                editor.start()
        return editor

    # ---- 録音の終了と清書 ----

    def finish(self, meeting_id: int) -> bool:
        """清書を裏で始める. すでに清書中なら何もせず False."""
        with self._lock:
            if meeting_id in self._finishing:
                return False
            self._finishing.add(meeting_id)
            editor = self._editors.pop(meeting_id, None)
        if editor is not None:
            editor.stop()
        try:
            with self._open_db() as conn:
                previous = db.get_meeting(conn, meeting_id)
                db.set_status(conn, meeting_id, db.FINISHING)
                db.set_ended(conn, meeting_id, datetime.now(self._tz))
        except Exception:
            with self._lock:
                self._finishing.discard(meeting_id)
            raise
        # 清書のやり直しで失敗したとき、前のうまくいった清書を下書きで上書きしないための印.
        keep_previous = previous is not None and previous.polished
        self.hub.publish(meeting_id, {"type": "status", "status": db.FINISHING})
        threading.Thread(target=self._finish_safely, args=(meeting_id, keep_previous), daemon=True).start()
        return True

    def _finish_safely(self, meeting_id: int, keep_previous: bool = False) -> None:
        try:
            status, error = self._finish(meeting_id, keep_previous=keep_previous)
        except Exception as exc:  # noqa: BLE001 - 状態を「失敗」にしないと清書中のまま固まる
            log.exception("finishing meeting %s failed", meeting_id)
            status, error = db.FAILED, f"清書の途中でエラーが起きました: {exc}"
        with self._open_db() as conn:
            db.set_status(conn, meeting_id, status, error=error)
        with self._lock:
            self._finishing.discard(meeting_id)
        self.hub.publish(meeting_id, {"type": "status", "status": status, "error": error})

    def _wait_for_transcription(self, meeting_id: int) -> None:
        last_count, last_change = -1, time.monotonic()
        while time.monotonic() - last_change < FINISH_STALL_SEC:
            with self._open_db() as conn:
                count = db.count_pending(conn, meeting_id)
            if count == 0:
                return
            if count != last_count:
                last_count, last_change = count, time.monotonic()
            time.sleep(1)

    def _finish(self, meeting_id: int, *, keep_previous: bool = False) -> tuple[str, str | None]:
        """清書して書き出す. (状態, エラー文) を返す."""
        self._wait_for_transcription(meeting_id)
        m4a = self._prepare_audio(meeting_id)

        with self._open_db() as conn:
            meeting = db.get_meeting(conn, meeting_id)
            segments = db.list_segments(conn, meeting_id)
        if meeting is None:
            return db.FAILED, "会議が見つかりません。"
        errors: list[str] = []
        lines = self._retranscribe(meeting)
        pending = sum(1 for s in segments if s.status == db.SEGMENT_PENDING)
        failed = sum(1 for s in segments if s.status == db.SEGMENT_FAILED)
        if lines is None:
            lines = transcript_lines(segments, self._tz)
        else:
            pending = failed = 0  # 通しの文字起こしには、録音中に失敗した部分も入っている
        if pending or failed:
            errors.append(
                f"文字起こしできていない部分が{pending + failed}か所あります（音声は残っています）。"
                "「失敗分をやり直す」のあと「清書をやり直す」を押してください。"
            )

        body, polished = self._polish(meeting, lines, errors)
        body, speaker_names = split_speaker_map(body)
        lines = apply_speaker_names(lines, speaker_names)
        if not polished and keep_previous:
            errors.append("前の清書はそのまま残しています。")
            return db.FAILED, " / ".join(errors)
        note_path = meeting.note_path
        if self._minutes_dir is not None:
            try:
                files = write_notes(
                    self._minutes_dir, meeting, body=body, polished=polished, transcript=lines,
                    total_ms=sum(s.duration_ms for s in segments), audio_m4a=m4a,
                )
                note_path = str(files.minutes)
            except OSError as exc:
                log.exception("could not write minutes note for meeting %s", meeting_id)
                errors.append(f"ノートを書き出せませんでした: {exc}")

        with self._open_db() as conn:
            db.save_final(conn, meeting_id, body, note_path, polished=polished)
        return (db.FAILED, " / ".join(errors)) if errors else (db.DONE, None)

    def _retranscribe(self, meeting: db.Meeting) -> list[str] | None:
        """録音全体を正確なモデルで文字にし直し、話者を付ける. できなければ None（録音中の文字起こしを使う）."""
        merged = self.audio.merged_path(meeting.id)
        if self._final_transcriber is None or not merged.exists():
            return None
        self.hub.publish(meeting.id, {"type": "notice", "message": "録音全体を、より正確なモデルで文字起こしし直しています。"})
        try:
            samples = read_samples(merged)
            timed = self._final_transcriber.transcribe_long(samples)
        except (WhisperError, OSError) as exc:
            log.warning("final transcription failed for meeting %s: %s", meeting.id, exc)
            return None
        turns = self._diarize(meeting, samples)
        return timed_lines(timed, self.audio.segment_offsets(meeting.id), self._tz, turns) or None

    def _diarize(self, meeting: db.Meeting, samples) -> list[SpeakerTurn]:
        """誰が話したかを聞き分ける. 失敗しても話者なしで先へ進む."""
        if self._diarizer is None:
            return []
        self.hub.publish(meeting.id, {"type": "notice", "message": "誰が話したかを聞き分けています。"})
        num_speakers = len(parse_participants(meeting.participants)) or None
        try:
            return self._diarizer.diarize(samples, num_speakers=num_speakers)
        except DiarizationError as exc:
            log.warning("diarization failed for meeting %s: %s", meeting.id, exc)
            return []

    def _prepare_audio(self, meeting_id: int) -> Path | None:
        """切れ端をつないで聞き返し用のm4aを作る. 失敗しても切れ端は残っているので先へ進む."""
        try:
            merged = self.audio.merge(meeting_id)
            if merged is None:
                return None
            return encode_m4a(merged, self.audio.meeting_dir(meeting_id) / M4A_NAME)
        except Exception:  # noqa: BLE001 - 音声の変換に失敗しても議事録は作る
            log.exception("could not prepare audio for meeting %s", meeting_id)
            return None

    def _polish(self, meeting: db.Meeting, lines: list[str], errors: list[str]) -> tuple[str, bool]:
        if not lines:
            return "（文字起こしが空でした）", True
        try:
            return self._polisher(meeting, "\n".join(lines)), True
        except ClaudeError as exc:
            errors.append(f"清書に失敗しました: {exc}")
            return render_markdown(meeting.draft) or "（下書きなし）", False

    # ---- 画面用 ----

    def segment_event(self, segment: db.Segment) -> dict[str, Any]:
        return {
            "type": "segment",
            "seg_id": segment.seg_id,
            "time": f"{datetime.fromtimestamp(segment.seg_id / 1000, self._tz):%H:%M:%S}",
            "status": segment.status,
            "text": segment.text,
        }
