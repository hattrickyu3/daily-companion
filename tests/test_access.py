"""本人の機器以外からのアクセスを断る仕組みのテスト. 小さな練習用のアプリに付けて確かめる."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from companion.config import get_config
from companion.web import access

APP_BASE_URL = "http://pc.example.ts.net:8765"
FROM_PC = ("127.0.0.1", 50000)
FROM_IPHONE = ("100.65.177.94", 50000)
FROM_IPHONE_V6 = ("fd7a:115c:a1e0::1234", 50000)
FROM_HOME_WIFI = ("192.168.3.40", 50000)


@pytest.fixture
def make_client(monkeypatch):
    config = replace(get_config(), app_base_url=APP_BASE_URL)
    monkeypatch.setattr(access, "get_config", lambda: config)

    app = FastAPI()
    app.add_middleware(access.OwnDeviceOnlyMiddleware)

    @app.get("/")
    def read() -> PlainTextResponse:
        return PlainTextResponse("日記")

    @app.post("/entries")
    def write() -> PlainTextResponse:
        return PlainTextResponse("保存しました")

    def build(client=FROM_PC, base_url="http://localhost") -> TestClient:
        return TestClient(app, client=client, base_url=base_url)

    return build


@pytest.mark.parametrize("client", [FROM_PC, FROM_IPHONE, FROM_IPHONE_V6])
def test_このPCとTailscaleの中からは開ける(make_client, client) -> None:
    response = make_client(client=client).get("/")

    assert response.status_code == 200
    assert response.text == "日記"


def test_家のWiFiのほかの機器からは開けない(make_client) -> None:
    response = make_client(client=FROM_HOME_WIFI).get("/")

    assert response.status_code == 403
    assert "日記" not in response.text


def test_書き込みも家のWiFiからは断る(make_client) -> None:
    response = make_client(client=FROM_HOME_WIFI).post("/entries")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "https://pc.example.ts.net",
        "http://pc.example.ts.net:8765",
        "http://pc:8765",
        "http://100.65.177.1:8765",
    ],
)
def test_自分の名前で指したときは開ける(make_client, base_url) -> None:
    assert make_client(base_url=base_url).get("/").status_code == 200


def test_IPv6のこのPCの名前でも開ける() -> None:
    # TestClient は IPv6 のURLを扱えないので、判定を直接確かめる
    reason = access.refusal_reason(
        client="::1", method="POST", host="[::1]:8765", origin="http://[::1]:8765", fetch_site=None,
        app_base_url=APP_BASE_URL,
    )

    assert reason is None


@pytest.mark.parametrize("base_url", ["http://evil.example", "http://192.168.3.10:8765"])
def test_よその名前でこのPCを指させても開けない(make_client, base_url) -> None:
    # DNSリバインディング: 攻撃者の名前が 127.0.0.1 を指すように切り替えられても、Host が違うので断れる
    assert make_client(base_url=base_url).get("/").status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://localhost:8765", "https://pc.example.ts.net", "http://pc.example.ts.net:8765"]
)
def test_アプリ自身の画面からの書き込みは通す(make_client, origin) -> None:
    response = make_client().post("/entries", headers={"Origin": origin})

    assert response.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://192.168.3.10:8765"])
def test_よそのページから送らせた書き込みは断る(make_client, origin) -> None:
    response = make_client().post("/entries", headers={"Origin": origin})

    assert response.status_code == 403


def test_Originが無くてもよそのサイトからと分かる書き込みは断る(make_client) -> None:
    response = make_client().post("/entries", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403


def test_ブラウザ以外からの書き込みは通す(make_client) -> None:
    # 録音アプリのWebDAVなどは Origin を送らない. 相手が本人の機器であることは別に確かめている
    assert make_client().post("/entries").status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PROPFIND", "MOVE", "DELETE"])
def test_録音アプリのWebDAVの操作は通す(make_client, method) -> None:
    # Origin を送らない機器からの書き込みは、相手と Host の確認だけで通す. 断られるのは口が無いせいではないことを見る
    response = make_client().request(method, "/entries")

    assert response.status_code != 403


def test_形の崩れたOriginは断る(make_client) -> None:
    response = make_client().post("/entries", headers={"Origin": "http://[::1"})

    assert response.status_code == 403


def test_読むだけならよそのページからでも中身は返す(make_client) -> None:
    # 読み出しはブラウザの同一オリジンポリシーが中身を渡さない. 画像やリンクで開いたときに壊さない
    response = make_client().get("/", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("100.65.177.94", True),
        ("fd7a:115c:a1e0::1", True),
        ("192.168.3.40", False),
        ("8.8.8.8", False),
        ("testclient", False),
        ("", False),
    ],
)
def test_本人の機器かどうか(host, expected) -> None:
    assert access.is_own_device(host) is expected
