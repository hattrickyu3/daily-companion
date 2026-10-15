"""議事録の画面.

iPhoneのSafariで /minutes/{id} を開いて録音する。音声は数秒ごとの切れ端で届き、
文字起こしとHaikuの編集の様子は Server-Sent Events（/events）で画面に流す。

マイクはHTTPSのページでしか使えないので、iPhoneからは Tailscale の
https://<PC名>.<tailnet>.ts.net/minutes で開く（tailscale serve で8765番へ流している）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from companion.clock import now_local
from companion.config import DIARIZATION_MODELS_DIR, get_config
from companion.integrations.notify import send_notification
from companion.links import app_url
from companion.db import meetings as db
from companion.db.connection import apply_schema, session
from companion.integrations.diarization import Diarizer
from companion.integrations.whisper import Transcriber
from companion.services.minutes_audio import AudioError, AudioStore
from companion.services.minutes_draft import SECTIONS, normalize_draft, section_label
from companion.services.minutes_import import MAX_IMPORT_SEC, decode_chunks, probe_duration_ms, repair_caf
from companion.services.minutes_runtime import M4A_NAME, MinutesRuntime
from companion.services.minutes_speakers import parse_participants

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# 生のHTMLは通さない（AIの書いた文章がそのままスクリプトとして動かないように）.
markdown = MarkdownIt("commonmark", {"html": False}).enable("table")

DEFAULT_TITLE = "ゼミ"
MAX_TITLE_LENGTH = 60
MAX_PARTICIPANTS_LENGTH = 200
KEEPALIVE_SEC = 15
# seg_id はiPhone側の時刻（エポックミリ秒）. 2001年〜2286年の範囲だけ受ける.
SEG_ID_MIN, SEG_ID_MAX = 10**12, 10**13

# ボイスメモなどの音声ファイルの取り込み. 届いたファイルはまずここに置き、読めるとわかってから会議のフォルダへ移す.
INCOMING_DIR_NAME = "_取り込み中"
ORIGINAL_NAME = "元の録音"
MAX_IMPORT_BYTES = 1024**3  # ボイスメモの「ロスレス」でも4時間で1GBに届かない
UPLOAD_CHUNK_BYTES = 1024 * 1024
# 拡張子はファイル名から取るが、保存先の名前に使うので英数字だけ受ける.
AUDIO_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}")
# ファイルの日時（録り終えた時刻）として受ける範囲. 未来は1日先まで（端末の時計のずれ）.
FUTURE_SLACK = timedelta(days=1)

# Tailscaleが機器に割り当てるアドレスの範囲.
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))

# 改行などの制御文字. 題名はファイル名とダウンロード名に使うので落とす.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

router = APIRouter(prefix="/minutes")
_runtime: MinutesRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> MinutesRuntime:
    """アプリ全体で1つの議事録係. 最初に使うときに作って動かす."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _build_runtime()
    return _runtime


def _build_runtime() -> MinutesRuntime:
    config = get_config()
    # 議事録のテーブルや列を後から足しているので、使い始める前に必ずそろえる（何度実行しても安全）.
    with session() as conn:
        apply_schema(conn)
    runtime = MinutesRuntime(
        open_db=session,
        audio=AudioStore(config.minutes_audio_dir),
        transcriber=Transcriber(config.whisper_model),
        final_transcriber=Transcriber(config.whisper_final_model),
        diarizer=Diarizer(DIARIZATION_MODELS_DIR),
        tz=config.timezone,
        minutes_dir=config.minutes_dir,
        recordings_dir=config.recordings_dir,
    )
    runtime.set_notifier(_notify_minutes)
    runtime.start()
    return runtime


def _notify_minutes(meeting_id: int | None, status: str, error: str | None) -> None:
    """録音アプリから自動で届いた録音の議事録ができたら（できなかったら）iPhoneに知らせる."""
    if meeting_id is None:
        send_notification(error or "録音を議事録にできませんでした。", title="録音を議事録にできませんでした",
                          tags=["warning"], click_url=app_url("/minutes"))
        return
    with session() as conn:
        meeting = db.get_meeting(conn, meeting_id)
    name = f"{meeting.started_at:%m/%d %H:%M}の「{meeting.title}」" if meeting else "録音"
    if status == db.DONE:
        message, title, tags = f"{name}の議事録ができました。", "議事録ができました", ["memo"]
    else:
        message, title, tags = f"{name}の議事録に確認が必要です。\n{error or ''}", "議事録を確認してください", ["warning"]
    send_notification(message, title=title, tags=tags, click_url=app_url(f"/minutes/{meeting_id}"))


def _load_meeting(meeting_id: int) -> db.Meeting:
    # 後から足した列があるので、読む前に必ずテーブルをそろえる（議事録係を作るときにそろう）.
    # 再起動の直後に一覧を通さず会議の画面を開くと、列が無くて落ちていた。
    get_runtime()
    with session() as conn:
        meeting = db.get_meeting(conn, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="会議が見つかりません。")
    return meeting


def _note_link(note_path: str | None) -> str | None:
    """書き出したノートを、ノート画面で開くURL. 保管庫の外なら None."""
    notes_dir = get_config().notes_dir
    if not note_path or notes_dir is None:
        return None
    try:
        rel = Path(note_path).relative_to(notes_dir)
    except ValueError:
        return None
    return "/notes/view/" + rel.as_posix()


def snapshot(runtime: MinutesRuntime, meeting_id: int) -> dict[str, Any]:
    """画面の全体. つないだ直後とつなぎ直したときに送る."""
    with session() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        segments = db.list_segments(conn, meeting_id)
    if meeting is None:
        return {"type": "snapshot", "missing": True}
    return {
        "type": "snapshot",
        "status": meeting.status,
        "error": meeting.error,
        "draft": normalize_draft(meeting.draft),
        "segments": [runtime.segment_event(s) for s in segments],
    }


SUMMARY_LENGTH = 50


def duration_label(total_ms: int) -> str:
    """録音の長さを「48秒」「12分05秒」「1時間12分」の形にする."""
    seconds = total_ms // 1000
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds:02d}秒"
    return f"{minutes // 60}時間{minutes % 60}分"


def summary_line(meeting: db.Meeting) -> str:
    """一覧で会議を見分けるための1行. 清書の「要点」の最初の項目、無ければ録音中の下書きの要点."""
    text = meeting.final_text or ""
    if "## 要点" in text:
        text = text.split("## 要点", 1)[1]
    else:
        text = meeting.draft.get("要点", "")
    for line in text.splitlines():
        stripped = line.strip().lstrip("-・*").strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:SUMMARY_LENGTH] + ("…" if len(stripped) > SUMMARY_LENGTH else "")
        if stripped.startswith("##"):
            break
    return ""


def is_deletable(meeting: db.Meeting, store: AudioStore) -> bool:
    """消してよい会議か. 録音が1つも無いものだけ（音声は必ず残す約束なので、録音のある会議は消させない）."""
    return (
        meeting.segment_count == 0
        and meeting.status != db.FINISHING
        and not store.segment_ids_on_disk(meeting.id)
    )


def _clean_participants(raw: str) -> str:
    """参加者の入力を「田中、佐藤」の形にそろえる."""
    return "、".join(parse_participants(CONTROL_CHARS.sub(" ", raw)[:MAX_PARTICIPANTS_LENGTH]))


def _clean_title(raw: str) -> str:
    return CONTROL_CHARS.sub("", raw).strip()[:MAX_TITLE_LENGTH] or DEFAULT_TITLE


@router.get("", response_class=HTMLResponse)
def minutes_page(request: Request) -> HTMLResponse:
    return _list_page(request)


def _webdav_info(request: Request) -> dict[str, str | None] | None:
    """録音アプリ（Recorder Plus）のWebDAVの設定に入れるもの. 受け口が閉じていれば None.

    この画面は家のWi-Fiの中からも開けるので、パスワードは本人の機器（このPCとTailscaleの中）にだけ見せる。
    """
    config = get_config()
    if not config.webdav_password:
        return None
    # iPhoneからは Tailscale の https（tailscale serve で8765番へ流している）で届く.
    host = urlsplit(config.app_base_url).hostname or "（Tailscaleのhttpsのアドレス）"
    client = request.client.host if request.client else ""
    password = config.webdav_password if _is_own_device(client) else None
    return {"host": f"{host}/webdav", "user": config.webdav_user, "password": password}


def _is_own_device(host: str) -> bool:
    """このPC自身か、Tailscaleの中（本人の機器だけがつながる）からのアクセスか."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in TAILNET_NETWORKS)


def _list_page(request: Request, *, import_error: str | None = None, status_code: int = 200) -> HTMLResponse:
    store = get_runtime().audio  # テーブルをそろえるため、一覧を読む前に動かす
    with session() as conn:
        meetings = db.list_meetings(conn)
    return templates.TemplateResponse(
        request, "minutes.html",
        {
            "import_error": import_error,
            "webdav": _webdav_info(request),
            "recorder_folder": get_config().recorder_folder,
            "rows": [
                {
                    "meeting": m,
                    "duration": duration_label(m.total_ms),
                    "summary": summary_line(m),
                    "deletable": is_deletable(m, store),
                }
                for m in meetings
            ],
            "default_title": DEFAULT_TITLE,
            "minutes_dir": get_config().minutes_dir,
        },
        status_code=status_code,
    )


@router.post("")
def create_meeting(title: str = Form(DEFAULT_TITLE), participants: str = Form("")) -> RedirectResponse:
    get_runtime()
    with session() as conn:
        meeting_id = db.create_meeting(
            conn, _clean_title(title), now_local(), participants=_clean_participants(participants)
        )
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)


@router.post("/import", response_model=None)
async def import_recording(
    request: Request,
    audio: UploadFile = File(...),
    title: str = Form(DEFAULT_TITLE),
    participants: str = Form(""),
    recorded_at: int = Form(0),
) -> HTMLResponse | RedirectResponse:
    """ボイスメモなどの録り終えた音声ファイルから議事録を作る.

    recorded_at はファイルの日時（エポックミリ秒. 画面のスクリプトが入れる）。ボイスメモでは録り終えた時刻なので、
    録音の長さの分だけさかのぼった時刻を会議の始まりにする。読めないファイルなら会議は作らず、一覧に理由を出す。
    """
    runtime = await run_in_threadpool(get_runtime)
    suffix = audio.filename and Path(audio.filename).suffix
    suffix = suffix.lower() if suffix and AUDIO_SUFFIX.fullmatch(suffix) else ".audio"
    staging = runtime.audio.root / INCOMING_DIR_NAME / f"{uuid.uuid4().hex}{suffix}"
    try:
        await run_in_threadpool(_save_upload, audio, staging)
        await run_in_threadpool(repair_caf, staging)
        duration_ms = await run_in_threadpool(probe_duration_ms, staging)
        if duration_ms > MAX_IMPORT_SEC * 1000:
            raise AudioError(f"{MAX_IMPORT_SEC // 3600}時間より長い録音は送れません。")
    except AudioError as exc:
        staging.unlink(missing_ok=True)
        return await run_in_threadpool(_list_page, request, import_error=str(exc), status_code=400)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    meeting_id = await run_in_threadpool(
        start_imported, runtime, staging,
        title=_clean_title(title), participants=_clean_participants(participants),
        start=_recording_start(recorded_at, duration_ms), duration_ms=duration_ms,
    )
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)


def _save_upload(upload: UploadFile, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with target.open("wb") as out:
            while chunk := upload.file.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > MAX_IMPORT_BYTES:
                    raise AudioError(f"ファイルが大きすぎます（{MAX_IMPORT_BYTES // 1024**3}GBまで）。")
                out.write(chunk)
    except OSError as exc:
        raise AudioError(f"ファイルを保存できませんでした: {exc}") from exc


def _recording_start(recorded_at_ms: int, duration_ms: int) -> datetime:
    """録音の始まり. ファイルの日時がおかしければ、いま録り終えたものとみなす."""
    now = now_local()
    end = now
    if SEG_ID_MIN <= recorded_at_ms <= (now + FUTURE_SLACK).timestamp() * 1000:
        end = datetime.fromtimestamp(recorded_at_ms / 1000, now.tzinfo)
    return end - timedelta(milliseconds=duration_ms)


def start_imported(
    runtime: MinutesRuntime, staging: Path, *, title: str, participants: str, start: datetime, duration_ms: int,
    notify: bool = False,
) -> int:
    """会議を作り、元のファイルを会議のフォルダへ移して取り込む. 会議の id を返す.

    notify が True なら、清書が終わったとき（取り込めなかったときも）iPhoneに知らせる。
    """
    with session() as conn:
        meeting_id = db.create_meeting(conn, title, start, participants=participants)
        db.set_ended(conn, meeting_id, start + timedelta(milliseconds=duration_ms))
        # 取り込んでいる間に「止めて清書する」を押されたり、録音中として編集係が動いたりしないよう、はじめから清書中にする.
        db.set_status(conn, meeting_id, db.FINISHING)
    try:
        original = runtime.audio.meeting_dir(meeting_id) / f"{ORIGINAL_NAME}{staging.suffix}"
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, original)
        runtime.import_recording(meeting_id, decode_chunks(original), int(start.timestamp() * 1000), notify=notify)
    except Exception as exc:  # noqa: BLE001 - 状態を「失敗」にしないと清書中のまま固まる
        log.exception("importing audio for meeting %s failed", meeting_id)
        # 会議のフォルダへ移せずに失敗したときは、staging が唯一の写しなので消さない.
        reason = str(exc) if isinstance(exc, AudioError) else f"取り込みの途中でエラーが起きました: {exc}"
        error = f"音声を読み込めませんでした: {reason}"
        with session() as conn:
            db.set_status(conn, meeting_id, db.FAILED, error=error)
        if notify:
            runtime.notify(meeting_id, db.FAILED, error)
    return meeting_id


@router.get("/{meeting_id}", response_class=HTMLResponse)
def meeting_page(request: Request, meeting_id: int) -> HTMLResponse:
    meeting = _load_meeting(meeting_id)
    get_runtime()  # 前回の残りの文字起こしを拾い直すため、画面を開いた時点で動かしておく
    return templates.TemplateResponse(
        request, "meeting.html",
        {
            "meeting": meeting,
            "sections": [(s, section_label(s)) for s in SECTIONS],
            "final_html": markdown.render(meeting.final_text) if meeting.final_text else "",
            "note_link": _note_link(meeting.note_path),
        },
    )


@router.post("/{meeting_id}/segments")
async def upload_segment(request: Request, meeting_id: int, seg: int) -> JSONResponse:
    """音声の切れ端を受け取る. 保存できたら 200 を返し、iPhone側はそこで手元の控えを消す."""
    if not SEG_ID_MIN <= seg < SEG_ID_MAX:
        return JSONResponse({"ok": False, "error": "切れ端の番号が不正です。"}, status_code=400)
    await run_in_threadpool(_load_meeting, meeting_id)
    data = await request.body()
    try:
        await run_in_threadpool(get_runtime().receive_segment, meeting_id, seg, data)
    except AudioError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@router.get("/{meeting_id}/events")
async def meeting_events(request: Request, meeting_id: int) -> StreamingResponse:
    await run_in_threadpool(_load_meeting, meeting_id)
    runtime = await run_in_threadpool(get_runtime)

    async def stream():
        queue = runtime.hub.subscribe(meeting_id)
        try:
            # 先に購読してから全体を送る. 逆だと、その間に起きた変化を取りこぼす.
            first = await run_in_threadpool(snapshot, runtime, meeting_id)
            yield _sse(first)
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SEC)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        finally:
            runtime.hub.unsubscribe(meeting_id, queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/{meeting_id}/finish")
def finish_meeting(meeting_id: int) -> JSONResponse:
    meeting = _load_meeting(meeting_id)
    if meeting.status != db.RECORDING:
        return JSONResponse({"ok": True, "status": meeting.status})
    get_runtime().finish(meeting_id)
    return JSONResponse({"ok": True, "status": db.FINISHING})


@router.post("/{meeting_id}/participants")
def update_participants(meeting_id: int, participants: str = Form("")) -> RedirectResponse:
    """参加者を直す. 次の清書（やり直し）から使う."""
    _load_meeting(meeting_id)
    with session() as conn:
        db.set_participants(conn, meeting_id, _clean_participants(participants))
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)


@router.post("/{meeting_id}/delete")
def delete_meeting(meeting_id: int) -> RedirectResponse:
    """録音の入っていない会議を消す. 録音があれば何もしない."""
    store = get_runtime().audio
    with session() as conn:
        meeting = next((m for m in db.list_meetings(conn) if m.id == meeting_id), None)
        if meeting is None:
            raise HTTPException(status_code=404, detail="会議が見つかりません。")
        if not is_deletable(meeting, store):
            raise HTTPException(status_code=409, detail="録音のある会議は消せません。")
        db.delete_meeting(conn, meeting_id)
    folder = store.meeting_dir(meeting_id)
    if folder.is_dir() and not any(folder.rglob("*.wav")):
        shutil.rmtree(folder)
    return RedirectResponse("/minutes", status_code=303)


@router.post("/{meeting_id}/retry")
def retry_polish(meeting_id: int) -> RedirectResponse:
    """清書をやり直す. あとから届いた切れ端もここで議事録に入る."""
    meeting = _load_meeting(meeting_id)
    if meeting.status in (db.DONE, db.FAILED):
        get_runtime().finish(meeting_id)
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)


@router.post("/{meeting_id}/retry-transcription")
def retry_transcription(meeting_id: int) -> RedirectResponse:
    _load_meeting(meeting_id)
    get_runtime().retry_transcription(meeting_id)
    return RedirectResponse(f"/minutes/{meeting_id}", status_code=303)


@router.get("/{meeting_id}/audio")
def meeting_audio(meeting_id: int) -> FileResponse:
    """録音のダウンロード. m4aがまだ無ければ、その場で切れ端をつないだWAVを渡す."""
    meeting = _load_meeting(meeting_id)
    store = get_runtime().audio
    m4a = store.meeting_dir(meeting_id) / M4A_NAME
    path = m4a if m4a.exists() else store.merge(meeting_id)
    if path is None:
        raise HTTPException(status_code=404, detail="録音がありません。")
    filename = f"{meeting.started_at:%Y-%m-%d} {meeting.title}{path.suffix}"
    return FileResponse(path, filename=filename)
