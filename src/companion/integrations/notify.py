"""ntfy.sh によるスマホPush通知.

トピック名を知っていれば誰でも購読できる仕組みなので、
トピック名そのものが実質的な秘密になる。.env 以外の場所（ログを含む）に書かないこと。
また通知は外のサーバーを通るので、日記・予定・会議の中身は載せず「できました」とリンクだけを送る。
"""

from __future__ import annotations

import logging

import httpx

from companion.config import get_config

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SEC = 15.0


class NotifyError(Exception):
    """通知の送信に失敗したときに送出する."""


def send_notification(
    message: str,
    *,
    title: str | None = None,
    priority: int = 3,
    tags: list[str] | None = None,
    click_url: str | None = None,
) -> None:
    """ntfy にメッセージを送る.

    Args:
        message: 本文.
        title: 通知のタイトル.
        priority: 1(最低) - 5(最高). 既定の3が通常。
        tags: 絵文字に変換される短いタグ（例: ["sunrise"]）.
        click_url: 通知タップ時に開くURL.

    Raises:
        NotifyError: 未設定、またはHTTPエラーのとき.
    """
    config = get_config()
    if not config.has_notify:
        raise NotifyError("NTFY_TOPIC が未設定です。.env を確認してください。")

    # ntfy はヘッダ値をLatin-1として扱うため、日本語タイトルはUTF-8のまま送れない。
    # 本文は問題ないので、日本語タイトルは本文の先頭に畳み込む。
    headers: dict[str, str] = {"Priority": str(priority)}
    body = message
    if title:
        if title.isascii():
            headers["Title"] = title
        else:
            body = f"{title}\n\n{message}"
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url

    url = f"{config.ntfy_server}/{config.ntfy_topic}"
    try:
        response = httpx.post(
            url, content=body.encode("utf-8"), headers=headers, timeout=REQUEST_TIMEOUT_SEC
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("ntfy send failed: %s", exc)
        raise NotifyError(f"通知の送信に失敗しました: {exc}") from exc

    log.info("notification sent")
