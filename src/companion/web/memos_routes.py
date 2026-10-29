"""ひとりメモの画面と、iPhoneのSafariで使う音声入力の画面.

    GET  /memos            ひとりメモだけの一覧（議事録の一覧からは分けた）
    GET  /memos/talk       音声入力. 話の切れ目ごとに /dictate へ送り、返ってきた文字をすぐ画面に足す
    POST /memos/from-text  音声入力で文字にした話を、ひとりメモにする（清書は裏で進める）

音声入力の録音は、ほかの音声入力と同じく文字にしたらすぐ消す（/dictate）。ひとりメモにしても録音は残らない。
iPhoneのショートカットは止めるまで録音を渡さないので、話しながら文字を見るにはこの画面を使う。
画面が消えると録音も止まる（Safariの決まり）ので、長い話は議事録の画面かボイスメモで録る。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from companion.db import meetings as db
from companion.services.memos import memo_rows
from companion.services.minutes_scenes import SOLO, get_scene
from companion.web import minutes_routes as base

router = APIRouter(prefix="/memos")

# 音声入力1回ぶんの上限. 切れ端は長くても20秒なので、2000個あれば10時間を超える.
MAX_PIECES = 2000
MAX_PIECE_TEXT = 2000
MAX_PIECE_MS = 60_000
MAX_TOTAL_TEXT = 100_000


@router.get("", response_class=HTMLResponse)
def memos_page(request: Request, q: str = "") -> HTMLResponse:
    base.get_runtime()  # テーブルをそろえる
    query = q.strip()[: base.MAX_QUERY_LENGTH]
    with base.session() as conn:
        everything = db.list_meetings(conn, limit=base.LIST_LIMIT)
    rows = memo_rows(everything, query=query)
    labels = {"recording": "録音中", "finishing": "清書中", "done": "完成", "failed": "要確認"}
    return base.templates.TemplateResponse(
        request, "memos.html",
        {
            "rows": [
                {"memo": r, "duration": base.duration_label(r.meeting.total_ms),
                 "status": labels.get(r.meeting.status, r.meeting.status)}
                for r in rows
            ],
            "query": query,
            "total": len(memo_rows(everything)) if query else len(rows),
            "solo_kind": SOLO,
        },
    )


@router.get("/talk", response_class=HTMLResponse)
def talk_page(request: Request) -> HTMLResponse:
    return base.templates.TemplateResponse(request, "talk.html", {})


class PieceError(ValueError):
    """届いた文字の並びが受けられない."""


def read_pieces(payload: object) -> list[tuple[int, int, str]]:
    """{"pieces": [{"at": 話し始めのエポックミリ秒, "ms": 長さ, "text": 文字}, ...]} を確かめて並べる.

    空の文字は落とす。同じ時刻が重なったら1ミリ秒ずつずらす（切れ端の番号に使うため）。
    """
    items = payload.get("pieces") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise PieceError("文字がありません。")
    if len(items) > MAX_PIECES:
        raise PieceError("長すぎます。")
    pieces: list[tuple[int, int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise PieceError("形がちがいます。")
        at, ms, text = item.get("at"), item.get("ms"), item.get("text")
        if not isinstance(at, int) or not base.SEG_ID_MIN <= at < base.SEG_ID_MAX:
            raise PieceError("時刻がちがいます。")
        if not isinstance(ms, int) or not 0 <= ms <= MAX_PIECE_MS:
            raise PieceError("長さがちがいます。")
        if not isinstance(text, str) or len(text) > MAX_PIECE_TEXT:
            raise PieceError("文字がちがいます。")
        cleaned = base.CONTROL_CHARS.sub(" ", text).strip()
        if cleaned:
            pieces.append((at, ms, cleaned))
    if not pieces:
        raise PieceError("文字がありません。")
    if sum(len(t) for _, _, t in pieces) > MAX_TOTAL_TEXT:
        raise PieceError("長すぎます。")
    pieces.sort(key=lambda p: p[0])
    ordered: list[tuple[int, int, str]] = []
    for at, ms, text in pieces:
        at = max(at, ordered[-1][0] + 1) if ordered else at
        ordered.append((at, ms, text))
    return ordered


@router.post("/from-text")
async def memo_from_text(request: Request) -> JSONResponse:
    """音声入力の文字からひとりメモを作る. 返すのは開くページ."""
    try:
        pieces = read_pieces(await request.json())
    except (PieceError, ValueError) as exc:
        message = str(exc) if isinstance(exc, PieceError) else "形がちがいます。"
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    runtime = await run_in_threadpool(base.get_runtime)
    start = datetime.fromtimestamp(pieces[0][0] / 1000, base.get_config().timezone)
    title = f"{get_scene(SOLO).default_title} {start:%H時%M分}"
    meeting_id = await run_in_threadpool(runtime.create_text_memo, pieces, title=title)
    return JSONResponse({"ok": True, "url": f"/minutes/{meeting_id}"})
