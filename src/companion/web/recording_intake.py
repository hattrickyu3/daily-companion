"""録音アプリから自動で届いた録音を議事録にする.

届き方は2つある（どちらもここに渡す）。
    WebDAV          Recorder Plus（有料版）→ このアプリの /webdav（webdav_routes.py）
    見張るフォルダ   Recorder Plus（無料）→ Googleドライブ → PCのGoogleドライブのフォルダ（folder_watch.py）

届いた録音はまずコピーし、以後はコピーだけを読む（読んでいる間に送り直されても取り違えない）。
同じ録音は二度議事録にしない。できたら（できなかったら）iPhoneに知らせる。
"""

from __future__ import annotations

import logging
import shutil
import threading
import unicodedata
import uuid
from enum import Enum
from pathlib import Path

from companion.clock import now_local
from companion.db import meetings as db
from companion.db import received
from companion.db.connection import session
from companion.services.minutes_audio import AudioError
from companion.services.minutes_import import MAX_IMPORT_SEC, probe_duration_ms, recording_start, repair_caf
from companion.web import minutes_routes

log = logging.getLogger(__name__)

# 議事録にする録音の拡張子. それ以外のファイル（録音アプリの設定など）は相手にしない.
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".mp4", ".ogg", ".flac", ".amr", ".3gp"}

# 取り込みは1つずつ. 録音がまとめて届いても、PCのメモリやGPUを取り合わない.
_import_lock = threading.Lock()


class Outcome(Enum):
    IMPORTED = "imported"      # 議事録づくりを始めた
    DUPLICATE = "duplicate"    # 前に受け取った録音（議事録はもうある）
    UNREADABLE = "unreadable"  # 音声として読めない
    GONE = "gone"              # 読む前に消された・名前が変わった
    FAILED = "failed"          # 途中で思わぬエラー（次に届いたとき・起動し直したときにもう一度試す）


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def dedupe_key(source: str, rel: str) -> str:
    """同じ録音かを見分けるための場所. 大文字小文字や、濁点の表し方（NFC/NFD）の違いはそろえる."""
    return unicodedata.normalize("NFC", f"{source}/{rel}").casefold()


def is_received(source: str, rel: str, size: int) -> bool:
    with session() as conn:
        return received.is_received(conn, dedupe_key(source, rel), size)


def import_safely(source: str, rel: str, path: Path) -> Outcome:
    """届いた録音を議事録にする. 失敗しても例外は出さない（裏のスレッドから呼ばれるので）."""
    with _import_lock:
        try:
            return _import(source, rel, path)
        except Exception:  # noqa: BLE001 - 裏のスレッドなので、落ちると誰にも気づかれない
            log.exception("could not import %s from %s", rel, source)
            return Outcome.FAILED


def _import(source: str, rel: str, path: Path) -> Outcome:
    runtime = minutes_routes.get_runtime()
    # 名前は日時を読むためにそのまま使う.
    staging = runtime.audio.root / minutes_routes.INCOMING_DIR_NAME / uuid.uuid4().hex / path.name
    staging.parent.mkdir(parents=True)
    try:
        shutil.copyfile(path, staging)
    except FileNotFoundError:
        shutil.rmtree(staging.parent)
        return Outcome.GONE
    key, size = dedupe_key(source, rel), staging.stat().st_size
    with session() as conn:
        if not received.claim(conn, key, size):
            log.info("%s was already received; not making minutes twice", rel)
            shutil.rmtree(staging.parent)
            return Outcome.DUPLICATE
    try:
        meeting_id = _import_staged(runtime, staging, path.name)
    except Exception:
        # 番を取ったまま失敗すると、二度と議事録にならない. 番を返してから知らせる.
        with session() as conn:
            received.release(conn, key, size)
        if staging.exists():
            shutil.rmtree(staging.parent)  # 元の録音はまだ届いた場所にある
        runtime.notify(None, db.FAILED, f"届いた録音「{path.name}」を議事録にする途中でエラーが起きました（録音は消していません）。")
        raise
    if meeting_id is None:
        return Outcome.UNREADABLE
    with session() as conn:
        received.set_meeting(conn, key, size, meeting_id)
    if staging.exists():
        # 会議のフォルダへ移せなかった（議事録は「要確認」になっている）. 届いた録音を消させない.
        log.warning("original of %s was not stored in meeting %s; keeping it at %s", rel, meeting_id, staging)
        return Outcome.FAILED
    staging.parent.rmdir()  # 中身は会議のフォルダへ移った
    return Outcome.IMPORTED


def _import_staged(runtime, staging: Path, name: str) -> int | None:
    """コピーした録音を議事録にする. 読めない録音なら知らせて None（読めないものは何度読んでも読めないので番は返さない）."""
    repair_caf(staging)  # Recorder Plus のCAFは、そのままでは読めない（コピーを直すので元のファイルには触らない）
    try:
        duration_ms = probe_duration_ms(staging)
        if duration_ms > MAX_IMPORT_SEC * 1000:
            raise AudioError(f"{MAX_IMPORT_SEC // 3600}時間より長い録音です。")
    except AudioError as exc:
        log.warning("could not read received recording %s: %s", name, exc)
        shutil.rmtree(staging.parent)
        runtime.notify(None, db.FAILED, f"届いた録音「{name}」を議事録にできませんでした: {exc}（録音は消していません）")
        return None
    start = recording_start(staging, duration_ms, received_at=now_local())
    return minutes_routes.start_imported(
        runtime, staging, title=f"録音 {start:%H時%M分}", participants="",
        start=start, duration_ms=duration_ms, notify=True,
    )
