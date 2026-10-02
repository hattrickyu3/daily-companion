"""清書した議事録を、手で直す.

直す対象は議事録ノートの「本文」（最初の「## 」の見出しから下）。題名・戻りリンク・日時などの
頭の部分はアプリが作ったものなので、そのまま残す。

編集の画面にはノートのファイルの中身を読み込む。PCのObsidianで直した内容も、
アプリから直すときに消えないようにするため。保存するたびに直す前の本文を控えとして残す。

    data/minutes/<会議id>/edits/<日時>.md   直す前の本文の控え
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

MAX_BODY_LENGTH = 200_000
EDITS_DIRNAME = "edits"


class EditError(ValueError):
    """直した内容を保存できないときに送出する."""


def split_note(text: str) -> tuple[str, str]:
    """ノートを (頭の部分, 本文) に分ける. 本文は最初の「## 」の行から. 見出しが無ければ全体が頭."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return "".join(lines[:index]), "".join(lines[index:])
    return text, ""


def current_body(note_path: str | None, final_text: str | None) -> str:
    """いまの本文. ノートのファイルがあればそちらを正とする（Obsidianで直した分を拾うため）."""
    if note_path:
        path = Path(note_path)
        if path.exists():
            _, body = split_note(path.read_text(encoding="utf-8"))
            if body.strip():
                return body.strip()
    return (final_text or "").strip()


def normalize_body(raw: str) -> str:
    """画面から届いた本文をそろえる. 改行を \\n に、前後の空白を落とす."""
    body = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise EditError("議事録が空です。全部消したいときは、内容を1行以上残してください。")
    if len(body) > MAX_BODY_LENGTH:
        raise EditError("議事録が長すぎます。")
    return body


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup_body(meeting_dir: Path, body: str, now: datetime) -> Path:
    """直す前の本文を控えておく."""
    folder = meeting_dir / EDITS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{now:%Y%m%d-%H%M%S}.md"
    _write_atomic(target, body + "\n")
    return target


def write_note_body(note_path: Path, body: str) -> None:
    """ノートの本文だけを差し替える. 頭の部分（題名・日時・リンク）はそのまま."""
    header, _ = split_note(note_path.read_text(encoding="utf-8"))
    header = header if header.endswith("\n\n") or not header else header.rstrip("\n") + "\n\n"
    _write_atomic(note_path, header + body + "\n")
