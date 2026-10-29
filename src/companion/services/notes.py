"""Obsidianの保管庫（ノートのフォルダ）を読む.

iPhoneのObsidianはOneDriveと直接同期できないので、PCにある保管庫を
このアプリ経由でスマホから見られるようにする。読むだけで書き込みはしない。

保管庫の中身はリクエストごとにディスクから読み直す（Vault は画面を開くたびに作る）。
ノートは数百枚程度なので十分速く、Obsidianで書き足した内容がすぐ反映されるほうが大事なため。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

NOTE_SUFFIX = ".md"
# 授業フォルダの中で、まとめノートを置いているフォルダと、議事録アプリが授業の記録を置くフォルダの名前。
# どちらも授業の一部として扱い、ノート画面では授業ごとに1つにまとめて出す。
NOTES_SUBFOLDER = "ノート"
COURSE_SUBFOLDERS = (NOTES_SUBFOLDER, "議事録")


@dataclass(frozen=True)
class NoteLink:
    """一覧に出す1枚のノート. path は保管庫からの相対パス（/区切り）."""

    name: str
    path: str


@dataclass(frozen=True)
class NoteGroup:
    """一番上のフォルダ（＝授業）ごとのノート. 保管庫の直下は folder が空."""

    folder: str
    notes: tuple[NoteLink, ...]


def _is_hidden(parts: tuple[str, ...]) -> bool:
    """.obsidian などの隠しフォルダ・隠しファイルか."""
    return any(part.startswith(".") for part in parts)


def course_folder(note: PurePosixPath) -> str:
    """ノートが属する授業のフォルダ. まとめ用の `ノート` と授業の記録の `議事録` は授業の一部として扱う."""
    parts = note.parent.parts
    if parts and parts[-1] in COURSE_SUBFOLDERS:
        parts = parts[:-1]
    return "/".join(parts)


def _shared_depth(a: PurePosixPath, b: PurePosixPath) -> int:
    """2つのフォルダが先頭から何階層ぶん同じか."""
    depth = 0
    for left, right in zip(a.parts, b.parts):
        if left != right:
            break
        depth += 1
    return depth


class Vault:
    """1つの保管庫. root が存在しなくても落ちず、空として振る舞う."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @cached_property
    def _files(self) -> list[str]:
        """保管庫の全ファイルの相対パス. 隠しフォルダには入らない.

        1枚のノートにリンクが何十個あっても、フォルダを読むのは1回で済ませる。
        """
        if not self.root.is_dir():
            return []

        found: list[str] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            base = Path(current).relative_to(self.root)
            found.extend(
                (base / name).as_posix() for name in files if not name.startswith(".")
            )
        return sorted(found)

    def groups(self) -> list[NoteGroup]:
        """ノートを授業のフォルダごとにまとめる.

        授業のフォルダは、ノートが入っているフォルダ（`ノート` フォルダならその1つ上）。
        `3年３Q/自然言語処理/ノート/` のように学期のフォルダの下に授業があっても授業ごとに分かれる。
        並びは一番上のフォルダ（学期）の新しい順、その中は名前順。保管庫の直下のノートは最後。
        """
        by_folder: dict[str, list[NoteLink]] = {}
        for rel in self._files:
            path = PurePosixPath(rel)
            if path.suffix.lower() != NOTE_SUFFIX:
                continue
            by_folder.setdefault(course_folder(path), []).append(
                NoteLink(name=path.stem, path=rel)
            )

        # 学期の名前（「2年まで」「3年３Q」）は後の学期ほど大きくなるので、逆順に並べる。
        # sorted は安定なので、先に名前順にしておけば同じ学期の中は名前順のまま残る。
        ordered = sorted(by_folder)
        ordered = sorted(ordered, key=lambda folder: folder.split("/")[0], reverse=True)
        return [NoteGroup(folder=f, notes=tuple(by_folder[f])) for f in ordered]

    def resolve(self, target: str, current: str) -> str | None:
        """Obsidianのリンク先 `[[target]]` を相対パスにする. 見つからなければ None.

        Obsidianと同じく、拡張子が無ければノート（.md）として探し、
        同じ名前が複数あれば今のノートに近いフォルダのものを選ぶ。
        """
        wanted = target.strip().strip("/")
        if not wanted:
            return None

        names = (wanted, wanted + NOTE_SUFFIX)
        candidates = [
            rel
            for rel in self._files
            if any(rel == name or rel.endswith("/" + name) for name in names)
        ]
        if not candidates:
            return None

        here = PurePosixPath(current).parent
        return min(
            candidates,
            key=lambda rel: (-_shared_depth(PurePosixPath(rel).parent, here), len(rel), rel),
        )

    def file_path(self, rel: str) -> Path | None:
        """相対パスを実ファイルにする. 保管庫の外・隠しファイル・フォルダは None."""
        # ":" は NTFS の代替データストリーム（note.md:secret）やドライブ指定になる。
        if not rel or ":" in rel:
            return None

        root = self.root.resolve()
        path = (root / rel).resolve()
        # 隠しファイルかどうかは、実際に解決した後のパスで判定する。
        # 入力の文字列を / で分けて見るだけだと、Windowsでは \ も区切りになるため、
        # `統計学\..\.obsidian\app.json` のような指定で隠しフォルダに入られてしまう。
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return None
        if not parts or _is_hidden(parts) or not path.is_file():
            return None
        return path

    def read_note(self, rel: str) -> str | None:
        """ノートの本文. ノートでない・読めないときは None."""
        if PurePosixPath(rel).suffix.lower() != NOTE_SUFFIX:
            return None
        path = self.file_path(rel)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            # OneDriveの「オンラインのみ」のファイルは、同期アプリが動いていないと読めない。
            return None
