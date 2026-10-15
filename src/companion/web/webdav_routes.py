"""録音アプリ（Recorder Plus）の自動バックアップを受け取るWebDAVの受け口.

iPhoneのRecorder Plusは、録り終えた録音をWebDAVのサーバーへ自動で送れる。
その送り先をこのアプリにして、届いた録音を受け箱（data/webdav/）に置き、音声なら自動で議事録にする。
ボイスメモやSafariの録音とちがい、画面を消していても録れて、送るのも自動。

    Recorder Plus → Tailscale（https://<PC名>.ts.net/webdav）→ 受け箱 → 議事録 → iPhoneに通知

録音アプリが使うのはWebDAVのごく一部（接続テストの PROPFIND、フォルダを作る MKCOL、送る PUT など）なので、
それだけを受ける。Basic認証を必ずかけ、パスワード（.env の WEBDAV_PASSWORD）が無ければ受け口ごと閉じる。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response

from companion.config import get_config
from companion.web import recording_intake as intake

log = logging.getLogger(__name__)

PREFIX = "/webdav"
REALM = "daily-companion"
METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL", "GET", "HEAD", "PUT", "DELETE", "MOVE"]
MAX_UPLOAD_BYTES = 1024**3
# Windowsのファイル名に使えない文字と制御文字.
UNSAFE_NAME = re.compile(r'[\\:*?"<>|\x00-\x1f\x7f]')
# Windowsで特別な意味を持つ名前（CON、NUL、COM1 など. 拡張子が付いていても同じ）.
RESERVED_NAME = re.compile(r"(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$")
# 送っている途中のファイル. 一覧には出さない.
UPLOADING_SUFFIX = ".uploading"

router = APIRouter(prefix=PREFIX)
_scanned = False
_scan_lock = threading.Lock()


@dataclass(frozen=True)
class WebdavSettings:
    user: str
    password: str
    inbox: Path


def get_settings() -> WebdavSettings | None:
    """受け口の設定. パスワードが無ければ None（受け口を閉じる）."""
    config = get_config()
    if not config.webdav_password:
        return None
    return WebdavSettings(config.webdav_user, config.webdav_password, config.webdav_inbox_dir)


class BadPath(ValueError):
    """受け箱の外を指している、またはファイル名に使えない文字がある."""


def resolve(inbox: Path, raw: str) -> tuple[str, Path]:
    """URLの中の場所を、受け箱の中のファイルにする. (「/」区切りの場所, ファイル) を返す."""
    parts = [part for part in raw.split("/") if part]
    for part in parts:
        if part in (".", "..") or UNSAFE_NAME.search(part) or part.endswith((" ", ".")) or RESERVED_NAME.match(part):
            raise BadPath(raw)
    target = inbox.joinpath(*parts)
    if not target.resolve().is_relative_to(inbox.resolve()):
        raise BadPath(raw)
    return "/".join(parts), target


@router.api_route("", methods=METHODS)
@router.api_route("/{path:path}", methods=METHODS)
async def webdav(request: Request, path: str = "") -> Response:
    settings = get_settings()
    if settings is None:
        return Response(status_code=404)
    if not _authorized(request, settings):
        return Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'})
    settings.inbox.mkdir(parents=True, exist_ok=True)
    _scan_once(settings.inbox)
    try:
        rel, target = resolve(settings.inbox, path)
    except BadPath:
        return Response(status_code=400)

    method = request.method
    if method == "OPTIONS":
        return Response(headers={"DAV": "1", "Allow": ", ".join(METHODS), "MS-Author-Via": "DAV"})
    if method == "PROPFIND":
        return await run_in_threadpool(_propfind, settings.inbox, target, request.headers.get("Depth", "1"))
    if method == "PROPPATCH":
        # 更新日時などを書き換えたいという頼み. 受け箱では使わないので、受けたことにだけする.
        return _multistatus([_response_xml(_href(rel, target.is_dir()), [])]) if target.exists() else Response(status_code=404)
    if method == "MKCOL":
        return await run_in_threadpool(_mkcol, target)
    if method in ("GET", "HEAD"):
        if not target.is_file():
            return Response(status_code=404 if not target.exists() else 405)
        return FileResponse(target) if method == "GET" else Response(headers={"Content-Length": str(target.stat().st_size)})
    if method == "PUT":
        return await _put(request, settings.inbox, rel, target)
    if method == "DELETE":
        return await run_in_threadpool(_delete, settings.inbox, target)
    if method == "MOVE":
        return await run_in_threadpool(_move, request, settings.inbox, target)
    return Response(status_code=405)


def _authorized(request: Request, settings: WebdavSettings) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    # 1文字ずつ比べると、かかる時間から正解を探られるので、時間が変わらない比べ方をする.
    return secrets.compare_digest(user.encode(), settings.user.encode()) and secrets.compare_digest(
        password.encode(), settings.password.encode()
    )


# ---- 一覧（PROPFIND） ----


def _propfind(inbox: Path, target: Path, depth: str) -> Response:
    if not target.exists():
        return Response(status_code=404)
    entries = [target]
    if target.is_dir() and depth != "0":
        entries += sorted(child for child in target.iterdir() if _visible(child))
    responses = []
    for entry in entries:
        try:
            responses.append(_entry_xml(inbox, entry))
        except FileNotFoundError:  # 一覧を作っている間に消された
            continue
    return _multistatus(responses)


def _visible(path: Path) -> bool:
    return not path.name.startswith(".") and not path.name.endswith(UPLOADING_SUFFIX)


def _href(rel: str, is_dir: bool) -> str:
    href = PREFIX + "/" + quote(rel)
    return href.rstrip("/") + "/" if is_dir else href


def _entry_xml(inbox: Path, entry: Path) -> str:
    rel = entry.relative_to(inbox).as_posix() if entry != inbox else ""
    stat = entry.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, UTC)
    props = [
        f"<D:displayname>{escape(entry.name if rel else 'webdav')}</D:displayname>",
        f"<D:getlastmodified>{format_datetime(modified, usegmt=True)}</D:getlastmodified>",
        f"<D:creationdate>{modified.strftime('%Y-%m-%dT%H:%M:%SZ')}</D:creationdate>",
    ]
    if entry.is_dir():
        props.append("<D:resourcetype><D:collection/></D:resourcetype>")
    else:
        props += [
            "<D:resourcetype/>",
            f"<D:getcontentlength>{stat.st_size}</D:getcontentlength>",
            "<D:getcontenttype>application/octet-stream</D:getcontenttype>",
            f'<D:getetag>"{stat.st_size:x}-{stat.st_mtime_ns:x}"</D:getetag>',
        ]
    return _response_xml(_href(rel, entry.is_dir()), props)


def _response_xml(href: str, props: list[str]) -> str:
    return (
        f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>{''.join(props)}</D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )


def _multistatus(responses: list[str]) -> Response:
    body = '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
    return Response(body, status_code=207, media_type="application/xml; charset=utf-8")


# ---- 書き込み ----


def _mkcol(target: Path) -> Response:
    if target.exists():
        return Response(status_code=405)
    if not target.parent.is_dir():
        return Response(status_code=409)
    target.mkdir()
    return Response(status_code=201)


async def _put(request: Request, inbox: Path, rel: str, target: Path) -> Response:
    if not rel or target.is_dir():
        return Response(status_code=405)
    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # 途中の名前がファイルになっている
        return Response(status_code=409)
    # 途中で切れても壊れた録音が残らないよう、別の名前で受けてから置き換える.
    # 名前を長くしすぎないよう、元の名前は入れない.
    tmp = target.with_name(f".{uuid.uuid4().hex[:12]}{UPLOADING_SUFFIX}")
    written = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    return Response(status_code=413)
                # ディスクへの書き込みで、ほかの画面の応答を止めない.
                await run_in_threadpool(out.write, chunk)
        os.replace(tmp, target)
    except PermissionError:
        # 議事録にするためにコピーしている最中など. 録音アプリは少しあとで送り直す.
        return Response(status_code=503, headers={"Retry-After": "30"})
    finally:
        tmp.unlink(missing_ok=True)
    log.info("received %s (%d bytes) over WebDAV", rel, written)
    _import_later(rel, target)
    return Response(status_code=204 if existed else 201)


def _delete(inbox: Path, target: Path) -> Response:
    if target == inbox or not target.exists():
        return Response(status_code=404 if not target.exists() else 403)
    # 受け箱から消しても、議事録にした録音は会議のフォルダにコピーしてあるので残る.
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return Response(status_code=204)


def _move(request: Request, inbox: Path, source: Path) -> Response:
    destination = urlsplit(request.headers.get("Destination", "")).path
    if not source.exists() or source == inbox:
        return Response(status_code=404)
    if not destination.startswith(PREFIX + "/"):
        return Response(status_code=502)  # 受け箱の外へは動かせない
    try:
        rel, target = resolve(inbox, unquote(destination[len(PREFIX):]))
    except BadPath:
        return Response(status_code=400)
    if target == inbox or target.is_dir():
        return Response(status_code=409)  # 受け箱そのものやフォルダの上には動かさない
    existed = target.exists()
    if existed and request.headers.get("Overwrite", "T").upper() == "F":
        return Response(status_code=412)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    except PermissionError:
        return Response(status_code=503, headers={"Retry-After": "30"})
    except OSError:
        return Response(status_code=409)
    for path in [target] if target.is_file() else sorted(target.rglob("*")):
        if path.is_file():
            _import_later(path.relative_to(inbox).as_posix(), path)
    return Response(status_code=204 if existed else 201)


# ---- 届いた録音を議事録にする ----

SOURCE = "webdav"


def _import_later(rel: str, path: Path) -> None:
    """録音なら、裏で議事録にする. 録音アプリへの返事は待たせない."""
    if intake.is_audio(path):
        threading.Thread(
            target=intake.import_safely, args=(SOURCE, rel, path), name="webdav-import", daemon=True
        ).start()


def _scan_once(inbox: Path) -> None:
    """アプリが落ちていた間に届いて、まだ議事録にしていない録音を拾う. 起動して最初の1回だけ."""
    global _scanned
    with _scan_lock:
        if _scanned:
            return
        _scanned = True
    threading.Thread(target=_scan, args=(inbox,), name="webdav-scan", daemon=True).start()


def _scan(inbox: Path) -> None:
    for path in sorted(inbox.rglob("*")):
        rel_parts = path.relative_to(inbox).parts
        if not intake.is_audio(path) or not all(_visible(Path(part)) for part in rel_parts):
            continue
        rel = "/".join(rel_parts)
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:  # 調べている間に消された
            continue
        if not intake.is_received(SOURCE, rel, size):
            intake.import_safely(SOURCE, rel, path)
