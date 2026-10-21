"""本人の機器以外からのアクセスを断る.

ログインの仕組みは持たない。代わりに、届いてよい相手を「このPC自身」と
「Tailscaleの中（本人の機器だけがつながる）」に絞る。
家のWi-Fiにつながったほかの機器（来客のスマホ、家電など）からは、読むことも書くこともできない。

あわせて次の2つも断る。どちらも本人のブラウザを踏み台にする攻撃で、相手の確認だけでは防げない。
- よそのWebページに仕込んだフォームから、日記の上書きや予定の登録を送らせる（CSRF）
- よその名前がこのPCを指すように切り替えて、ブラウザに日記を読ませる（DNSリバインディング）
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from companion.config import get_config

log = logging.getLogger(__name__)

# Tailscaleが機器に割り当てるアドレスの範囲.
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# 読むだけの操作. 中身はブラウザの同一オリジンポリシーがよそのページに渡さないので、送り元は問わない.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REFUSED_MESSAGE = "このアプリは、このPCとTailscaleでつないだ本人の機器からだけ開けます。"


def is_own_device(host: str) -> bool:
    """このPC自身か、Tailscaleの中（本人の機器だけがつながる）のアドレスか."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in TAILNET_NETWORKS)


def _own_names(app_base_url: str) -> frozenset[str]:
    """このPCを指す名前. Tailscaleの名前は長い形（pc.xxx.ts.net）と短い形（pc）の両方で引ける."""
    names = {"localhost"}
    hostname = urlsplit(app_base_url).hostname
    if hostname:
        names |= {hostname, hostname.split(".")[0]}
    return frozenset(names)


def _hostname(authority: str) -> str | None:
    """Host ヘッダや Origin の「名前:ポート」から名前だけを取り出す. 読めなければ None."""
    try:
        hostname = urlsplit(f"//{authority}").hostname
    except ValueError:
        return None
    return hostname.rstrip(".") if hostname else None


def _is_own_name(hostname: str | None, names: frozenset[str]) -> bool:
    # 数字のアドレスは攻撃者の名前に化けられないので、本人の機器の範囲かだけを見る.
    return hostname is not None and (hostname in names or is_own_device(hostname))


def refusal_reason(
    *, client: str, method: str, host: str | None, origin: str | None, fetch_site: str | None, app_base_url: str
) -> str | None:
    """断る理由. 通してよければ None."""
    if not is_own_device(client):
        return f"client {client or '?'} is outside this PC and the tailnet"
    names = _own_names(app_base_url)
    if not _is_own_name(_hostname(host or ""), names):
        return f"host {host!r} does not name this PC"
    if method in SAFE_METHODS:
        return None
    if origin is not None:
        # "null" は送り元を伏せたページ（sandbox の iframe など）. アプリ自身の画面は伏せない.
        origin_host = None if origin == "null" else _hostname(origin.split("://", 1)[-1])
        if not _is_own_name(origin_host, names):
            return f"origin {origin!r} is not this app"
    elif fetch_site == "cross-site":
        return "cross-site request without origin"
    return None


class OwnDeviceOnlyMiddleware:
    """本人の機器からの、本人の画面が送ったリクエストだけをアプリに通す."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # いまは WebSocket の口が無いので http だけを見る. WebSocket を足すときはここで同じ確認をすること.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]}
        client = scope.get("client")
        reason = refusal_reason(
            client=client[0] if client else "",
            method=scope["method"],
            host=headers.get("host"),
            origin=headers.get("origin"),
            fetch_site=headers.get("sec-fetch-site"),
            app_base_url=get_config().app_base_url,
        )
        if reason is None:
            await self.app(scope, receive, send)
            return
        log.warning("refused %s %s: %s", scope["method"], scope["path"], reason)
        await PlainTextResponse(REFUSED_MESSAGE, status_code=403)(scope, receive, send)
