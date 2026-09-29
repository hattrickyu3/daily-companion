"""ノート（Obsidianの保管庫）を読む画面.

app.py が400行に近いので、ノート関連の画面はこのファイルに分けている。
見るだけの画面で、ノートの編集はPCのObsidianで行う。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from companion.config import get_config
from companion.services.notes import Vault
from companion.services.notes_render import render
from companion.services.notes_search import search as search_notes

TEMPLATES_DIR = Path(__file__).parent / "templates"
# 画面の中でそのまま開いてよい種類. SVGは中にスクリプトを書けるので入れない。
INLINE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".pdf"})
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/notes")


def get_vault() -> Vault | None:
    """設定された保管庫. NOTES_DIR が空なら None."""
    notes_dir = get_config().notes_dir
    return Vault(notes_dir) if notes_dir else None


@router.get("", response_class=HTMLResponse)
def notes_page(request: Request, q: str = "") -> HTMLResponse:
    """授業（フォルダ）ごとのノート一覧. q があれば検索結果を出す."""
    vault = get_vault()
    query = q.strip()
    searching = vault is not None and bool(query)
    return templates.TemplateResponse(
        request,
        "notes.html",
        {
            "configured": vault is not None,
            "query": query,
            "searching": searching,
            "hits": search_notes(vault, query) if searching else [],
            "groups": vault.groups() if vault and not searching else [],
        },
    )


@router.get("/view/{rel:path}", response_class=HTMLResponse)
def note_page(request: Request, rel: str) -> HTMLResponse:
    """1枚のノート. 見つからなければ一覧に戻す."""
    vault = get_vault()
    text = vault.read_note(rel) if vault else None
    if vault is None or text is None:
        return RedirectResponse("/notes", status_code=303)

    path = PurePosixPath(rel)
    return templates.TemplateResponse(
        request,
        "note.html",
        {
            "title": path.stem,
            # 本文が `# 題名` で始まるなら、画面の見出しは出さない（同じ題名が2回並ぶため）。
            "has_own_title": text.lstrip().startswith("# "),
            "folder": "/".join(path.parts[:-1]),
            "body": render(text, rel, vault),
        },
    )


@router.get("/file/{rel:path}")
def note_file(rel: str) -> FileResponse:
    """ノートに埋め込まれた画像や、リンクされたPDFなど.

    画面の中で開くのは画像とPDFだけにする。HTMLやSVGをそのまま返すと、
    保管庫に紛れ込んだファイルのスクリプトがこのアプリの画面として動き、
    認証のない日記の保存や予定の登録を勝手に呼べてしまう。それ以外はダウンロードとして渡す。
    """
    vault = get_vault()
    path = vault.file_path(rel) if vault else None
    if path is None:
        raise HTTPException(status_code=404)

    headers = {"X-Content-Type-Options": "nosniff"}
    if path.suffix.lower() in INLINE_SUFFIXES:
        return FileResponse(path, headers=headers)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers=headers,
    )
