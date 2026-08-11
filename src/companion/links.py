"""通知からタップして開くURLの組み立て.

もとは3つのジョブが同じ関数を持ち、家のLAN内のIPを直接埋めていた。
外出先で通知をタップすると、そのIPには届かず開けない。

APP_BASE_URL を設定すればそれを使い、未設定なら今まで通りLANのIPを使う。
Tailscaleを入れたら `http://<PC名>:8765` のように、家の中でも外でも同じ
名前で引けるURLを入れておく。
"""

from __future__ import annotations

from companion.config import get_config
from companion.web.serve import local_ip


def base_url() -> str:
    """アプリの入口URL. 末尾のスラッシュは付けない.

    設定の読み込み時にも末尾スラッシュを落としているが、ここでも落とす。
    URLの組み立てが `//` になるのを一箇所で防いでおきたいため。
    """
    configured = get_config().app_base_url
    if configured:
        return configured.rstrip("/")
    # 未設定なら従来どおり家のLAN内のIP。外出先からは開けない。
    return f"http://{local_ip()}:{get_config().web_port}"


def app_url(path: str = "/") -> str:
    """通知からタップして開くURLを組み立てる."""
    if not path.startswith("/"):
        path = "/" + path
    return base_url() + path
