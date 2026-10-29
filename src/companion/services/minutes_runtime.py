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
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Any
from zoneinfo import ZoneInfo

from companion.db import meetings as db
from companion.integrations.claude_runner import ClaudeError
from companion.integrations.diarization import Diarizer, DiarizationError, SpeakerTurn
from companion.integrations.whisper import TimedText, Transcriber, WhisperError
from companion.services.minutes_audio import AudioError, AudioStore, encode_m4a, read_samples
from companion.services.minutes_draft import render_markdown
from companion.services.minutes_editor import LiveEditor
from companion.services.minutes_final import (
    polish,
    texts_by_segment,
    timed_lines,
    transcript_lines,
    write_notes,
)
from companion.services.minutes_hub import MinutesHub
from companion.services.minutes_scenes import SOLO, Destinations, Scene, get_scene
from companion.services.minutes_words import Vocabulary, load_vocabulary
from companion.services.minutes_speakers import apply_speaker_names, parse_participants, split_speaker_map

log = logging.getLogger(__name__)

WORKER_IDLE_SEC = 60
# 録音を終えたとき、文字起こし待ちの切れ端が片付くのを待つ. 減り続けている間は待ち、
# この時間まったく減らなければ（文字起こしが止まっている）待つのをやめる.
FINISH_STALL_SEC = 300
M4A_NAME = "録音.m4a"

# 清書が終わったことを知らせる係. (会議の id（会議を作る前の失敗なら None）, 状態, エラー文).
Notifier = Callable[[int | None, str, str | None], None]


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
        recordings_dir: Path | None = None,
        final_transcriber: Transcriber | None = None,
        diarizer: Diarizer | None = None,
        hub: MinutesHub | None = None,
        editor_factory: Callable[[int], LiveEditor] | None = None,
        polisher: Callable[[db.Meeting, str], str] | None = None,
        notes_dir: Path | None = None,
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
        # ノートを書き出す場所. ゼミは minutes_dir、授業と打ち合わせは保管庫（notes_dir）の中.
        self._destinations = Destinations(minutes_dir=minutes_dir, notes_dir=notes_dir)
        # 清書した会議の録音（m4a）を置くフォルダ. None なら置かない（アプリの中には残る）.
        self._recordings_dir = recordings_dir
        # 清書係. 既定では辞書の語も渡して、用語の書き方をそろえさせる.
        self._polisher = polisher or (
            lambda meeting, transcript: polish(meeting, transcript, terms=self._vocabulary(meeting).terms)
        )
        self._editor_factory = editor_factory or (
            lambda meeting_id: LiveEditor(
                meeting_id, hub=self.hub, open_db=open_db, tz=tz, scene=self._scene_of(meeting_id)
            )
        )
        self._queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self._queued: set[tuple[int, int]] = set()
        self._editors: dict[int, LiveEditor] = {}
        self._finishing: set[int] = set()
        # 取り込んだ音声ファイルが途中から読めなかった会議. 清書のあとに「要確認」として知らせる.
        self._import_warnings: dict[int, str] = {}
        # 清書が終わったらiPhoneに知らせる会議（録音アプリから自動で届いたもの）と、知らせる係.
        self._notify_ids: set[int] = set()
        self._notifier: Notifier | None = None
        # 切れ端ごとの文字起こしを後回しにした会議. 清書で録音全体を文字にし直せたら、切れ端ごとには文字起こししない.
        self._deferred: set[int] = set()
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
        # 清書中の会議は録音全体を文字にし直すので、切れ端ごとの文字起こしは後回しにする.
        deferred = set(unfinished) if self._final_transcriber is not None else set()
        with self._lock:
            self._deferred |= deferred
        for segment in leftovers:
            if segment.meeting_id not in deferred:
                self._enqueue(segment.meeting_id, segment.seg_id)
        threading.Thread(target=self._worker, name="minutes-transcriber", daemon=True).start()
        for meeting_id in unfinished:
            self.finish(meeting_id)
        self.export_unwritten_memos()

    def export_unwritten_memos(self) -> int:
        """できあがっているのに保管庫に無いひとりメモを書き出す. 書き出した数を返す.

        ひとりメモは2026-10-25まで保管庫に出していなかったので、その分を埋める。清書はし直さない（手直しも残る）。
        """
        with self._open_db() as conn:
            memos = [
                m for m in db.list_meetings(conn, limit=10_000)
                if get_scene(m.kind).key == SOLO and m.status == db.DONE and not m.note_path and m.final_text
            ]
        written = 0
        for memo in memos:
            m4a = self.audio.meeting_dir(memo.id) / M4A_NAME
            with self._open_db() as conn:
                segments = db.list_segments(conn, memo.id)
            errors: list[str] = []
            note_path = self._export(
                memo, body=memo.final_text, polished=memo.polished, lines=transcript_lines(segments, self._tz),
                total_ms=sum(s.duration_ms for s in segments), m4a=m4a if m4a.exists() else None, errors=errors,
            )
            if note_path and not errors:
                with self._open_db() as conn:
                    db.set_note_path(conn, memo.id, note_path)
                written += 1
        return written

    def create_text_memo(self, pieces: list[tuple[int, int, str]], *, title: str) -> int:
        """音声入力のページで文字にした話から、ひとりメモを作って清書を裏で始める. 会議の番号を返す.

        pieces は (話し始めのエポックミリ秒, 長さのミリ秒, 文字) の並び。音声入力は録音を残さないので、
        文字だけを済みの切れ端として入れる（通しの文字起こしはし直さない）。
        """
        start = datetime.fromtimestamp(pieces[0][0] / 1000, self._tz)
        with self._open_db() as conn:
            meeting_id = db.create_meeting(conn, title, start, kind=SOLO)
            for seg_id, duration_ms, text in pieces:
                db.add_segment(conn, meeting_id, seg_id, duration_ms)
                db.finish_segment(conn, meeting_id, seg_id, text)
        self.finish(meeting_id)
        return meeting_id

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

    def set_notifier(self, notifier: Notifier) -> None:
        """清書が終わったことを知らせる係を決める（iPhoneへの通知）."""
        self._notifier = notifier

    def notify(self, meeting_id: int | None, status: str, error: str | None) -> None:
        """知らせる. 会議を作る前に失敗したときは meeting_id が None. 通知の失敗で議事録づくりは止めない."""
        if self._notifier is None:
            return
        try:
            self._notifier(meeting_id, status, error)
        except Exception:  # noqa: BLE001 - 通知が届かないだけで議事録はできている
            log.exception("could not notify about meeting %s", meeting_id)

    def import_recording(
        self, meeting_id: int, chunks: Iterable[bytes], start_ms: int, *, notify: bool = False
    ) -> int:
        """録り終えた音声（切れ端に分けたWAV）を取り込み、清書まで進める. 取り込めた切れ端の数を返す.

        録音中とちがい、Haikuに少しずつ編集させる必要はないので、全部を保存して清書中にしてから
        文字起こしの順番待ちに入れる（清書中の会議には編集係を作らない）。
        清書で録音全体を正確なモデルで文字にし直すときは、切れ端ごとの文字起こしはしない
        （同じ録音を二度文字にすることになるため）。文字にし直せなかったときだけ、切れ端ごとに文字起こしする。

        notify が True なら、清書が終わったときに知らせる（画面を見ていない、自動で届いた録音のため）。

        Raises:
            AudioError: 1つも取り込めなかった.
        """
        seg_ids: list[int] = []
        offset_ms = 0
        try:
            for data in chunks:
                seg_id = start_ms + offset_ms
                duration_ms = self.audio.save_segment(meeting_id, seg_id, data)
                with self._open_db() as conn:
                    db.add_segment(conn, meeting_id, seg_id, duration_ms)
                seg_ids.append(seg_id)
                offset_ms += duration_ms
        except AudioError:
            if not seg_ids:
                raise
            # 途中で読めなくなったファイル. 読めたところまでで議事録を作り、そのことを知らせる.
            log.warning("imported audio for meeting %s broke after %d segments", meeting_id, len(seg_ids))
            self._import_warnings[meeting_id] = (
                f"録音ファイルが途中から読めなかったので、はじめの約{offset_ms // 60000}分だけで議事録を作りました。"
            )
        if not seg_ids:
            raise AudioError("音声が入っていませんでした。")
        defer = self._final_transcriber is not None
        with self._lock:
            if notify:
                self._notify_ids.add(meeting_id)
            if defer:
                self._deferred.add(meeting_id)
        self.finish(meeting_id)
        if not defer:
            for seg_id in seg_ids:
                self._enqueue(meeting_id, seg_id)
        return len(seg_ids)

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

    def _vocabulary(self, meeting: db.Meeting | None) -> Vocabulary:
        """会議で使う辞書（人の名前・用語・つなぎ言葉）. 辞書は画面から変わるので、使うたびに読む."""
        with self._open_db() as conn:
            return load_vocabulary(conn, meeting)

    def dictate(self, samples, *, previous: str = "") -> str:
        """音声入力. 短い音声を、議事録と同じWhisper（速いモデル）と辞書で文字にして返す.

        会議には入れない。辞書は「すべての場面」の語だけを使う。
        previous は同じ音声入力の前の切れ端の文. 録音中の文字起こしと同じく、続きとしてWhisperに渡す.
        録音中の文字起こしと同じモデルを順番に使うので、会議の文字起こしが混んでいると少し待つ。
        """
        vocabulary = self._vocabulary(None)
        raw = self._transcriber.transcribe(samples, prompt=previous, hotwords=vocabulary.hotwords())
        return vocabulary.fix(raw)

    def _transcribe_one(self, meeting_id: int, seg_id: int) -> None:
        with self._open_db() as conn:
            prompt = db.previous_text(conn, meeting_id, seg_id)
            vocabulary = load_vocabulary(conn, db.get_meeting(conn, meeting_id))
        try:
            raw = self._transcriber.transcribe(
                read_samples(self.audio.segment_path(meeting_id, seg_id)), prompt=prompt, hotwords=vocabulary.hotwords()
            )
            text = vocabulary.fix(raw)
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

    def _scene_of(self, meeting_id: int) -> Scene:
        with self._open_db() as conn:
            meeting = db.get_meeting(conn, meeting_id)
        return get_scene(meeting.kind if meeting else None)

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
            wants_notice = meeting_id in self._notify_ids
            self._notify_ids.discard(meeting_id)
        self.hub.publish(meeting_id, {"type": "status", "status": status, "error": error})
        if wants_notice:
            self.notify(meeting_id, status, error)

    def _wait_for_transcription(self, meeting_id: int) -> None:
        # 文字起こしの順番待ちは全部の会議で1列. ほかの会議（長い録音の取り込みなど）の分が
        # 進んでいる間も「止まっていない」とみなす.
        last_progress, last_change = None, time.monotonic()
        while time.monotonic() - last_change < FINISH_STALL_SEC:
            with self._open_db() as conn:
                count = db.count_pending(conn, meeting_id)
            if count == 0:
                return
            progress = (count, self._queue.qsize())
            if progress != last_progress:
                last_progress, last_change = progress, time.monotonic()
            time.sleep(1)

    def _finish(self, meeting_id: int, *, keep_previous: bool = False) -> tuple[str, str | None]:
        """清書して書き出す. (状態, エラー文) を返す."""
        with self._lock:
            deferred = meeting_id in self._deferred
            self._deferred.discard(meeting_id)
        if not deferred:
            self._wait_for_transcription(meeting_id)
        m4a = self._prepare_audio(meeting_id)

        with self._open_db() as conn:
            meeting = db.get_meeting(conn, meeting_id)
        if meeting is None:
            return db.FAILED, "会議が見つかりません。"
        errors: list[str] = []
        if warning := self._import_warnings.pop(meeting_id, None):
            errors.append(warning)
        lines = self._retranscribe(meeting)
        if lines is None and deferred:
            # 録音全体を文字にし直せなかった. 後回しにしていた切れ端ごとの文字起こしをして、それを使う.
            self._transcribe_pending(meeting_id)
        with self._open_db() as conn:
            segments = db.list_segments(conn, meeting_id)
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
        note_path = self._export(meeting, body=body, polished=polished, lines=lines,
                                 total_ms=sum(s.duration_ms for s in segments), m4a=m4a, errors=errors)

        with self._open_db() as conn:
            db.save_final(conn, meeting_id, body, note_path, polished=polished)
        return (db.FAILED, " / ".join(errors)) if errors else (db.DONE, None)

    def _export(
        self, meeting: db.Meeting, *, body: str, polished: bool, lines: list[str],
        total_ms: int, m4a: Path | None, errors: list[str],
    ) -> str | None:
        """場面に合った場所へノートを書き出し、ノートの場所を返す.

        書き出す場所が無い（保管庫が未設定）、または書き出せなかったときは前の場所のまま（エラーは errors に足す）。
        """
        target = self._destinations.for_meeting(meeting)
        try:
            if target is None:
                return meeting.note_path
            files = write_notes(
                target, meeting, body=body, polished=polished, transcript=lines,
                total_ms=total_ms, audio_m4a=m4a, audio_dir=self._recordings_dir,
            )
            return str(files.minutes)
        except OSError as exc:
            log.exception("could not write minutes note for meeting %s", meeting.id)
            errors.append(f"ノートを書き出せませんでした: {exc}")
            return meeting.note_path

    def _retranscribe(self, meeting: db.Meeting) -> list[str] | None:
        """録音全体を正確なモデルで文字にし直し、話者を付ける. できなければ None（録音中の文字起こしを使う）."""
        merged = self.audio.merged_path(meeting.id)
        if self._final_transcriber is None or not merged.exists():
            return None
        self.hub.publish(meeting.id, {"type": "notice", "message": "録音全体を、より正確なモデルで文字起こしし直しています。"})
        vocabulary = self._vocabulary(meeting)
        # 辞書の語はヒントに渡さない. 正確なモデル（large-v3）は語の一覧をそのまま書き出し、
        # 話の中身を落とした（2026-10-28、96分の講義が約1000字になった）. 辞書は下の書き直しと清書で使う.
        try:
            samples = read_samples(merged)
            timed = self._final_transcriber.transcribe_long(samples)
        except (WhisperError, OSError) as exc:
            log.warning("final transcription failed for meeting %s: %s", meeting.id, exc)
            return None
        # つなぎ言葉を消して、間違えやすい書き方を直す. つなぎ言葉だけだった区間は落とす.
        timed = [t for t in (TimedText(t.start, t.end, vocabulary.fix(t.text)) for t in timed) if t.text]
        offsets = self.audio.segment_offsets(meeting.id)
        self._fill_pending(meeting.id, texts_by_segment(timed, offsets))
        turns = self._diarize(meeting, samples)
        return timed_lines(timed, offsets, self._tz, turns) or None

    def _fill_pending(self, meeting_id: int, texts: dict[int, str]) -> None:
        """まだ文字起こししていない切れ端に、通しで文字にした結果を入れる（会議の画面の文字起こし欄に出すため）."""
        filled = []
        with self._open_db() as conn:
            for seg_id, text in texts.items():
                if db.fill_pending_segment(conn, meeting_id, seg_id, text):
                    filled.append(db.get_segment(conn, meeting_id, seg_id))
        for segment in filled:
            self.hub.publish(meeting_id, self.segment_event(segment))

    def _transcribe_pending(self, meeting_id: int) -> None:
        """後回しにしていた切れ端ごとの文字起こしをして、終わるのを待つ."""
        with self._open_db() as conn:
            seg_ids = [s.seg_id for s in db.list_segments(conn, meeting_id) if s.status == db.SEGMENT_PENDING]
        for seg_id in seg_ids:
            self._enqueue(meeting_id, seg_id)
        self._wait_for_transcription(meeting_id)

    def _diarize(self, meeting: db.Meeting, samples) -> list[SpeakerTurn]:
        """誰が話したかを聞き分ける. 失敗しても話者なしで先へ進む."""
        if self._diarizer is None or not get_scene(meeting.kind).diarize:
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
            return render_markdown(meeting.draft, get_scene(meeting.kind).sections) or "（下書きなし）", False

    # ---- 画面用 ----

    def segment_event(self, segment: db.Segment) -> dict[str, Any]:
        return {
            "type": "segment",
            "seg_id": segment.seg_id,
            "time": f"{datetime.fromtimestamp(segment.seg_id / 1000, self._tz):%H:%M:%S}",
            "status": segment.status,
            "text": segment.text,
        }
