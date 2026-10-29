"""iPhoneのショートカットやPCの音声入力ツールから届く音声の受け取り.

ショートカットの設定のずれで送れないことが多いので、送り方は広く受ける（2026-10-24、最初の試しで
キーが audio にならず全部断っていた。iPhoneのキーボードが頭を大文字にするため）:
    フォーム（multipart）   ファイルの欄なら、キーの名前は問わない
    本文そのもの             本文を録音として受ける（ショートカットの本文「ファイル」、PCのツール）
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request
from starlette.datastructures import UploadFile

from companion.services.minutes_audio import AudioError
from companion.services.minutes_runtime import MinutesRuntime
from companion.web import minutes_routes as base

log = logging.getLogger(__name__)

FORM_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
# 本文そのものが録音のとき、届いた種類から付ける拡張子（置き場所の名前に使うだけ. 中身は読んで確かめる）.
SUFFIX_BY_TYPE = {"audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
                  "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/x-caf": ".caf"}
NO_FILE_MESSAGE = "録音ファイルが入っていません。「URLの内容を取得」のフォームに、種類「ファイル」で「録音されたオーディオ」を入れてください。"


class NoAudioError(AudioError):
    """届いたものに録音ファイルが入っていない."""


async def receive_audio(runtime: MinutesRuntime, request: Request, *, label: str) -> tuple[Path, int]:
    """届いた録音を受け取り箱に置いて確かめる. (置いた場所, 長さミリ秒).

    Raises:
        AudioError: 録音が入っていない・読めない・大きすぎる. どう届いたかをログに残す（中身は残さない）.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        # フォームは読み終えるまで大きさを確かめられないので、先に申告された大きさで断る.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > base.MAX_IMPORT_BYTES:
            raise AudioError(f"ファイルが大きすぎます（{base.MAX_IMPORT_BYTES // 1024**3}GBまで）。")
        if content_type in FORM_TYPES:
            form = await request.form()
            uploads = [value for value in form.values() if isinstance(value, UploadFile)]
            if not uploads:
                raise NoAudioError(NO_FILE_MESSAGE)
            return await base.stage_upload(runtime, uploads[0])
        return await _stage_body(runtime, request, content_type)
    except AudioError as exc:
        fields = list((await request.form()).keys()) if content_type in FORM_TYPES else []
        log.warning("%s was not accepted (type=%s, fields=%s): %s", label, content_type or "-", fields, exc)
        raise


async def _stage_body(runtime: MinutesRuntime, request: Request, content_type: str) -> tuple[Path, int]:
    """本文そのものを録音として受け取り箱に置く. 上限を超えたら読むのをやめる."""
    staging = base.staging_path(runtime, f"録音{SUFFIX_BY_TYPE.get(content_type, '')}")
    staging.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with staging.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > base.MAX_IMPORT_BYTES:
                    raise AudioError(f"ファイルが大きすぎます（{base.MAX_IMPORT_BYTES // 1024**3}GBまで）。")
                out.write(chunk)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise AudioError(f"ファイルを保存できませんでした: {exc}") from exc
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    if written == 0:
        staging.unlink(missing_ok=True)
        raise NoAudioError(NO_FILE_MESSAGE)
    return staging, await base.check_staged(staging)
