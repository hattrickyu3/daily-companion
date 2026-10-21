"""Webアプリの起動コマンド.

    uv run python -m companion.web.serve

WEB_HOST=0.0.0.0 で待ち受けるので、同じWi-Fi内のスマホからも開ける。
"""

from __future__ import annotations

import socket

import uvicorn

from companion.config import get_config


def local_ip() -> str:
    """LAN内で使えるこのPCのIPアドレスを推定する."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            # 実際には送信しない。経路選択のためだけに接続先を指定する。
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main() -> None:
    config = get_config()
    print(f"PCから      : http://localhost:{config.web_port}")
    # 家のWi-Fiの中のIPからは開けない（access で断る）. スマホはTailscale経由で開く.
    print(f"スマホから  : {config.app_base_url or '.env の APP_BASE_URL（TailscaleのURL）を設定してください'}")
    print("停止するには Ctrl+C")
    uvicorn.run("companion.web.app:app", host=config.web_host, port=config.web_port)


if __name__ == "__main__":
    main()
