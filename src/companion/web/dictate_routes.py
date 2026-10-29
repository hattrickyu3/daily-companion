"""音声入力の受け口. 短い録音を文字にして、その文字だけを返す.

使うのは2つ:
    PCの音声入力ツール（companion.dictation）  右Ctrlで録って送り、返ってきた文字をカーソルの位置に入れる
    iPhoneのショートカット                      録って送り、返ってきた文字をクリップボードに入れる

議事録と同じWhisperと辞書（つなぎ言葉を消す・間違えやすい書き方を直す）を使う。
PCのツールは、前の切れ端の文を見出し（PREVIOUS_HEADER、%エンコード）に入れてくる. 続きとしてWhisperに渡す.
録音も前の文も、文字にしたらすぐ捨てる（音声入力は記録として残さない）。送り方は広く受ける（web/audio_upload.py）。
ほかの画面と同じく、このPCとTailscaleの中からしか届かない（web/access.py）。
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from companion.dictation.client import MAX_PREVIOUS_CHARS, PREVIOUS_HEADER
from companion.integrations.whisper import WhisperError
from companion.services.minutes_audio import AudioError
from companion.services.minutes_import import decode_samples
from companion.web import minutes_routes as base
from companion.web.audio_upload import receive_audio

log = logging.getLogger(__name__)

router = APIRouter()

# 音声入力は話したことをすぐ文字にする用途. 長い話は議事録（ひとりメモ）で録る.
MAX_DICTATION_SEC = 5 * 60


@router.post("/dictate", response_class=PlainTextResponse)
async def dictate(request: Request) -> PlainTextResponse:
    """録音を文字にして返す. 何も聞き取れなければ空の文字を返す. 失敗したら 400 / 500 と理由の1行."""
    runtime = await run_in_threadpool(base.get_runtime)
    try:
        staging, duration_ms = await receive_audio(runtime, request, label="dictation")
    except AudioError as exc:
        return PlainTextResponse(f"送れませんでした: {exc}", status_code=400)
    try:
        if duration_ms > MAX_DICTATION_SEC * 1000:
            return PlainTextResponse(
                f"送れませんでした: 音声入力は{MAX_DICTATION_SEC // 60}分までです。長い話はひとりメモで録ってください。",
                status_code=400,
            )
        samples = await run_in_threadpool(decode_samples, staging)
        text = await run_in_threadpool(lambda: runtime.dictate(samples, previous=_previous_text(request)))
    except AudioError as exc:
        return PlainTextResponse(f"送れませんでした: {exc}", status_code=400)
    except WhisperError as exc:
        log.warning("dictation failed: %s", exc)
        return PlainTextResponse("文字にできませんでした。少し待ってからもう一度話してください。", status_code=500)
    finally:
        staging.unlink(missing_ok=True)
    return PlainTextResponse(text)


def _previous_text(request: Request) -> str:
    """見出しで届いた前の文. 無ければ空. 長ければ後ろの MAX_PREVIOUS_CHARS 文字だけ（Whisperはそこまでしか読まない）."""
    return unquote(request.headers.get(PREVIOUS_HEADER, "")).strip()[-MAX_PREVIOUS_CHARS:]
