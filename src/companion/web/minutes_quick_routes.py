"""iPhoneの「ショートカット」から、アプリを開かずにひとりメモを送る受け口.

ショートカットが録音して、そのファイルを POST /minutes/quick に送る（「URLの内容を取得」）。
受け取ったらすぐに短い文を返し、ショートカットはそれを画面に出して終わる。文字起こしと清書は裏で進め、
できたら通知する（自動で届いた録音と同じ）。返事も通知も中身は載せない（通知は外のサーバーを通るため）。
送り方は広く受ける（web/audio_upload.py）。

ほかの画面と同じく、このPCとTailscaleの中からしか届かない（web/access.py）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from companion.services.minutes_audio import AudioError
from companion.services.minutes_scenes import SOLO, get_scene
from companion.web import minutes_routes as base
from companion.web.audio_upload import receive_audio
from companion.web.minutes_scene_form import SceneChoice

router = APIRouter(prefix="/minutes")


@router.post("/quick", response_class=PlainTextResponse)
async def quick_memo(request: Request) -> PlainTextResponse:
    """ひとりメモの録音を受け取って、清書まで裏で進める. 返すのはショートカットが画面に出す1行."""
    runtime = await run_in_threadpool(base.get_runtime)
    try:
        staging, duration_ms = await receive_audio(runtime, request, label="quick memo")
    except AudioError as exc:
        return PlainTextResponse(f"送れませんでした: {exc}", status_code=400)
    # ショートカットの録音は止めた直後に届くので、いま録り終えたものとして始まりの時刻を出す.
    start = base._recording_start(0, duration_ms)
    await run_in_threadpool(
        base.start_imported, runtime, staging,
        title=f"{get_scene(SOLO).default_title} {start:%H時%M分}", participants="",
        start=start, duration_ms=duration_ms, notify=True, scene=SceneChoice(SOLO, ""),
    )
    return PlainTextResponse("受け取りました。できたら通知します。")
