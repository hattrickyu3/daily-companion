"""Googleドライブのフォルダを見張って、届いた録音を議事録にするテスト.

Googleドライブは録音を少しずつ書き込む（同期する）ので、大きさが変わらなくなってから取り込む。
議事録にした録音はフォルダから消す（Googleドライブのごみ箱に入り、容量を使い続けない）。
"""

from __future__ import annotations

from pathlib import Path

from companion.web.folder_watch import FolderWatcher
from companion.web.recording_intake import Outcome
from test_minutes_import import make_m4a
from test_minutes_runtime import wait_until
from test_webdav import env, meetings  # noqa: F401 - env はフィクスチャ


class FakeImport:
    def __init__(self, outcome: Outcome = Outcome.IMPORTED) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def __call__(self, source: str, rel: str, path: Path) -> Outcome:
        self.calls.append(rel)
        return self.outcome


def test_同期が終わって大きさが変わらなくなった録音を取り込み消す(tmp_path: Path) -> None:
    folder = tmp_path / "Recorder Plus"
    folder.mkdir()
    fake = FakeImport()
    watcher = FolderWatcher(folder, tmp_path / "録音", import_file=fake)
    recording = folder / "2026-10-15 14-00-00.m4a"
    recording.write_bytes(b"x" * 10)

    watcher.poll()
    assert fake.calls == []  # 見つけた直後はまだ同期中かもしれない
    watcher.poll()

    assert fake.calls == ["2026-10-15 14-00-00.m4a"]
    assert not recording.exists()


def test_大きくなっている途中の録音は待つ(tmp_path: Path) -> None:
    fake = FakeImport()
    watcher = FolderWatcher(tmp_path, tmp_path.parent / "録音", import_file=fake)
    recording = tmp_path / "a.m4a"

    recording.write_bytes(b"x" * 10)
    watcher.poll()
    recording.write_bytes(b"x" * 20)
    watcher.poll()
    assert fake.calls == []
    watcher.poll()

    assert fake.calls == ["a.m4a"]


def test_録音でないものや隠しファイルや空のファイルは相手にしない(tmp_path: Path) -> None:
    fake = FakeImport()
    watcher = FolderWatcher(tmp_path, tmp_path.parent / "録音", import_file=fake)
    (tmp_path / "設定.json").write_text("{}")
    (tmp_path / ".tmp.m4a").write_bytes(b"x")
    (tmp_path / ".sync").mkdir()
    (tmp_path / ".sync" / "b.m4a").write_bytes(b"x")
    (tmp_path / "empty.m4a").write_bytes(b"")

    watcher.poll()
    watcher.poll()

    assert fake.calls == []


def test_前に議事録にした録音はもう一度は作らず消す(tmp_path: Path) -> None:
    fake = FakeImport(Outcome.DUPLICATE)
    watcher = FolderWatcher(tmp_path, tmp_path.parent / "録音", import_file=fake)
    (tmp_path / "a.m4a").write_bytes(b"x")

    watcher.poll()
    watcher.poll()

    assert not (tmp_path / "a.m4a").exists()


def test_読めない録音は消さず何度も試さない(tmp_path: Path) -> None:
    folder = tmp_path / "drive"
    folder.mkdir()
    fake = FakeImport(Outcome.UNREADABLE)
    watcher = FolderWatcher(folder, tmp_path / "録音", import_file=fake)
    (folder / "a.m4a").write_bytes(b"x")

    for _ in range(20):
        watcher.poll()

    assert fake.calls == ["a.m4a"]
    assert (folder / "a.m4a").exists()


def test_失敗した録音は消さずしばらくしてから試し直す(tmp_path: Path) -> None:
    from companion.web.folder_watch import RETRY_AFTER_POLLS

    folder = tmp_path / "drive"
    folder.mkdir()
    fake = FakeImport(Outcome.FAILED)
    watcher = FolderWatcher(folder, tmp_path / "録音", import_file=fake)
    (folder / "a.m4a").write_bytes(b"x")

    # 見つける・取り込む（失敗）・見送る×RETRY_AFTER_POLLS・試し直す
    for _ in range(3 + RETRY_AFTER_POLLS):
        watcher.poll()

    assert fake.calls == ["a.m4a", "a.m4a"]
    assert (folder / "a.m4a").exists()


def test_消す前にPCの録音フォルダへ写しを取る(tmp_path: Path) -> None:
    folder = tmp_path / "drive"
    (folder / "sub").mkdir(parents=True)
    archive = tmp_path / "録音" / "届いた録音"
    watcher = FolderWatcher(folder, archive, import_file=FakeImport())
    (folder / "sub" / "a.m4a").write_bytes(b"audio")

    watcher.poll()
    watcher.poll()

    assert (archive / "sub" / "a.m4a").read_bytes() == b"audio"
    assert not (folder / "sub" / "a.m4a").exists()


def test_写しが取れなければ取り込まず消さない(tmp_path: Path, monkeypatch) -> None:
    import shutil

    folder = tmp_path / "drive"
    folder.mkdir()
    fake = FakeImport()
    watcher = FolderWatcher(folder, tmp_path / "録音", import_file=fake)
    (folder / "a.m4a").write_bytes(b"audio")

    def no_space(*args, **kwargs):
        raise OSError("ディスクがいっぱい")

    monkeypatch.setattr(shutil, "copyfile", no_space)
    watcher.poll()
    watcher.poll()

    assert fake.calls == []
    assert (folder / "a.m4a").exists()


def test_取り込みの間に録音が変わったら消さない(tmp_path: Path) -> None:
    folder = tmp_path / "drive"
    folder.mkdir()
    recording = folder / "a.m4a"
    recording.write_bytes(b"audio")

    def rewrite(source: str, rel: str, path: Path) -> Outcome:
        path.write_bytes(b"new audio!")  # Googleドライブが新しい中身で上書きした
        return Outcome.IMPORTED

    watcher = FolderWatcher(folder, tmp_path / "録音", import_file=rewrite)
    watcher.poll()
    watcher.poll()

    assert recording.read_bytes() == b"new audio!"


def test_フォルダがまだ無くても止まらない(tmp_path: Path) -> None:
    watcher = FolderWatcher(tmp_path / "G" / "マイドライブ", tmp_path / "録音", import_file=FakeImport())

    watcher.poll()  # Googleドライブが起動する前など. 例外を出さない


def test_本物の取り込みにつなぐと議事録ができる(env, tmp_path: Path) -> None:  # noqa: F811
    _, db_path, _, _, notified = env
    folder = tmp_path / "drive"
    folder.mkdir()
    (folder / "2026-10-15 14-00-00.m4a").write_bytes(make_m4a(tmp_path, 3).read_bytes())
    watcher = FolderWatcher(folder, tmp_path / "録音")

    watcher.poll()
    watcher.poll()

    wait_until(lambda: len(meetings(db_path)) == 1 and meetings(db_path)[0].status == "done")
    wait_until(lambda: notified == [(meetings(db_path)[0].id, "done")])
    assert not (folder / "2026-10-15 14-00-00.m4a").exists()


def test_RecorderPlusのCAFが届いても議事録になる(env, tmp_path: Path) -> None:  # noqa: F811
    from test_minutes_import import make_recorder_plus_caf

    _, db_path, _, _, _ = env
    folder = tmp_path / "drive"
    folder.mkdir()
    name = "2026_10_15_22_36_F1BCAE95-57F2-423A-A0C5-86FD2FEF0743.caf"
    original = make_recorder_plus_caf(folder / name).read_bytes()
    watcher = FolderWatcher(folder, tmp_path / "録音")

    watcher.poll()
    watcher.poll()

    wait_until(lambda: len(meetings(db_path)) == 1 and meetings(db_path)[0].status == "done")
    assert meetings(db_path)[0].title == "録音 22時36分"
    # PCに取った写しは、届いたままの中身（直したのは議事録用のコピーだけ）
    assert (tmp_path / "録音" / name).read_bytes() == original
