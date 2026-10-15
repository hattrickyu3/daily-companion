"""Googleドライブのフォルダを見張って、Recorder Plus から届いた録音を議事録にする.

    Recorder Plus（無料）→ Googleドライブ → PCの「Googleドライブ パソコン版」のフォルダ → ここ

1分ごとにフォルダを見て、録音の大きさが前回から変わっていなければ（同期が終わっていれば）取り込む。
取り込む前に、まずPCの録音フォルダの中の「届いた録音」へ写しを取る（どこで失敗しても録音がPCに残るように）。
議事録にした録音はフォルダから消す。Googleドライブのごみ箱に入るので30日は戻せ、
容量（無料で15GB）も使い続けない。

見張るフォルダは .env の RECORDER_FOLDER。空なら見張らない。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from companion.config import get_config
from companion.web import recording_intake as intake
from companion.web.recording_intake import Outcome

log = logging.getLogger(__name__)

SOURCE = "folder"
POLL_SEC = 60
# 失敗した録音を試し直すまでに見送る回数（10分）.
RETRY_AFTER_POLLS = 10
ARCHIVE_SUBDIR = "届いた録音"
# 議事録にした（前に議事録にした）ので、フォルダから消してよい録音.
REMOVABLE = {Outcome.IMPORTED, Outcome.DUPLICATE}
# もう試さない録音. 失敗（FAILED・GONE）は一時的なことが多いので、次に見たときにまた試す.
SETTLED = {Outcome.IMPORTED, Outcome.DUPLICATE, Outcome.UNREADABLE}

_started = False
_start_lock = threading.Lock()


class FolderWatcher:
    """1つのフォルダを見張る. poll() を呼ぶたびに1回見る."""

    def __init__(
        self, folder: Path, archive: Path, *,
        import_file: Callable[[str, str, Path], Outcome] = intake.import_safely,
    ) -> None:
        self.folder = folder
        # 届いた録音の写しを置くPCのフォルダ. ここに写しが取れるまで、届いた録音は消さない.
        self.archive = archive
        self._import_file = import_file
        # 前回見たときの (大きさ, 更新時刻). 2回続けて同じなら同期が終わったとみなす.
        self._last: dict[Path, tuple[int, int]] = {}
        # このプロセスで扱い終えた録音. 読めない録音を1分ごとに試して、通知を出し続けないため.
        self._handled: set[tuple[Path, tuple[int, int]]] = set()
        # 失敗した録音は、何回か見送ってから試し直す（失敗の通知を1分ごとに出さない）.
        self._retry_wait: dict[tuple[Path, tuple[int, int]], int] = {}

    def poll(self) -> None:
        seen: dict[Path, tuple[int, int]] = {}
        for path in self._recordings():
            try:
                stat = path.stat()
            except OSError:  # 見ている間に消された
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            seen[path] = signature
            ready = stat.st_size > 0 and self._last.get(path) == signature
            key = (path, signature)
            if not ready or key in self._handled:
                continue
            if self._retry_wait.get(key, 0) > 0:
                self._retry_wait[key] -= 1
                continue
            outcome = self._take(path, signature)
            if outcome in SETTLED:
                self._handled.add(key)
                self._retry_wait.pop(key, None)
            else:
                self._retry_wait[key] = RETRY_AFTER_POLLS
        self._last = seen
        # 消えた録音のことは忘れる（いつまでも覚えておかない）.
        self._handled &= set(seen.items())
        self._retry_wait = {key: wait for key, wait in self._retry_wait.items() if key in seen.items()}

    def _recordings(self) -> list[Path]:
        if not self.folder.is_dir():
            return []  # Googleドライブがまだ起動していないなど
        found = []
        for path in sorted(self.folder.rglob("*")):
            parts = path.relative_to(self.folder).parts
            if intake.is_audio(path) and not any(part.startswith(".") for part in parts) and path.is_file():
                found.append(path)
        return found

    def _take(self, path: Path, signature: tuple[int, int]) -> Outcome:
        rel = path.relative_to(self.folder).as_posix()
        archived = self._archive(path, rel, signature[0])
        if archived is None:
            return Outcome.FAILED
        outcome = self._import_file(SOURCE, rel, path)
        log.info("%s in the recorder folder: %s", rel, outcome.value)
        if outcome in REMOVABLE:
            self._remove(path, signature, archived)
        return outcome

    def _archive(self, path: Path, rel: str, size: int) -> Path | None:
        """届いた録音の写しをPCに取る. 取れなければ None（そのときは届いた録音を消さない）."""
        target = self.archive / rel
        if target.is_file() and target.stat().st_size == size:
            return target  # 前に写しを取ってある
        number = 2
        while target.exists():
            target = (self.archive / rel).with_stem(f"{Path(rel).stem} ({number})")
            number += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.copying")
            shutil.copyfile(path, tmp)
            if tmp.stat().st_size != size:  # 写している間に中身が変わった
                tmp.unlink()
                return None
            tmp.replace(target)
        except OSError as exc:
            log.warning("could not copy %s to %s: %s", path, target, exc)
            return None
        return target

    def _remove(self, path: Path, signature: tuple[int, int], archived: Path) -> None:
        """議事録にした録音をフォルダから消す. 写しと同じもので、取り込みの間に変わっていないときだけ."""
        try:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != signature or archived.stat().st_size != stat.st_size:
                log.warning("%s changed while making minutes; not removing it", path)
                return
            path.unlink()
        except OSError as exc:
            log.warning("could not remove %s after making minutes: %s", path, exc)


def start_folder_watch() -> None:
    """RECORDER_FOLDER が設定されていれば、裏で見張り始める. 何度呼んでもよい."""
    global _started
    folder = get_config().recorder_folder
    if folder is None:
        return
    with _start_lock:
        if _started:
            return
        _started = True
    archive = get_config().recordings_dir / ARCHIVE_SUBDIR
    log.info("watching %s for recordings (copies go to %s)", folder, archive)
    watcher = FolderWatcher(folder, archive)
    threading.Thread(target=_run, args=(watcher,), name="recorder-folder", daemon=True).start()


def _run(watcher: FolderWatcher) -> None:
    while True:
        try:
            watcher.poll()
        except Exception:  # noqa: BLE001 - 見張りが止まると、以後の録音が議事録にならない
            log.exception("error while watching %s", watcher.folder)
        time.sleep(POLL_SEC)
