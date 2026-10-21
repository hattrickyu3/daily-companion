"""ノート画面のテスト.

保管庫は一時フォルダに作った小さなものに差し替える。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from web_client import own_device_client

from companion.services.notes import Vault


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "大学"
    (root / "統計学" / "ノート").mkdir(parents=True)
    (root / "統計学" / "ノート" / "仮説検定.md").write_text(
        "# 仮説検定\n検定の流れ\n\n![[表.png]]", encoding="utf-8"
    )
    (root / "統計学" / "表.png").write_bytes(b"\x89PNG")
    return root


@pytest.fixture
def client(vault_root, monkeypatch) -> Iterator[TestClient]:
    from companion.web import app as web_app
    from companion.web import notes_routes

    monkeypatch.setattr(notes_routes, "get_vault", lambda: Vault(vault_root))
    with own_device_client(web_app.app) as test_client:
        yield test_client


def test_ノートの一覧に授業とノートが出る(client) -> None:
    response = client.get("/notes")

    assert response.status_code == 200
    assert "統計学" in response.text
    assert "仮説検定" in response.text


def test_ノートを開くと本文がHTMLで出る(client) -> None:
    response = client.get("/notes/view/" + quote("統計学/ノート/仮説検定.md"))

    assert response.status_code == 200
    assert "<h1" in response.text
    assert "検定の流れ" in response.text
    assert "<img" in response.text


def test_本文に題名があれば画面の見出しは重ねて出さない(client) -> None:
    response = client.get("/notes/view/" + quote("統計学/ノート/仮説検定.md"))

    assert response.text.count(">仮説検定</h1>") == 1


def test_無いノートを開くと一覧に戻る(client) -> None:
    response = client.get("/notes/view/" + quote("統計学/無い.md"), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/notes"


def test_埋め込まれた画像を取り出せる(client) -> None:
    response = client.get("/notes/file/" + quote("統計学/表.png"))

    assert response.status_code == 200
    assert response.content == b"\x89PNG"
    assert response.headers["content-type"] == "image/png"


def test_PDFは画面の中で開ける(client, vault_root) -> None:
    (vault_root / "統計学" / "資料.pdf").write_bytes(b"%PDF")

    response = client.get("/notes/file/" + quote("統計学/資料.pdf"))

    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" not in response.headers.get("content-disposition", "")


@pytest.mark.parametrize("name", ["罠.html", "罠.svg", "罠.js"])
def test_画像とPDF以外はダウンロードとして渡し画面で実行させない(client, vault_root, name) -> None:
    (vault_root / "統計学" / name).write_text("<script>alert(1)</script>", encoding="utf-8")

    response = client.get("/notes/file/" + quote(f"統計学/{name}"))

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_隠しフォルダには区切り文字を変えても入れない(client, vault_root) -> None:
    (vault_root / ".obsidian").mkdir()
    (vault_root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")

    response = client.get("/notes/file/" + quote("統計学\\..\\.obsidian\\app.json", safe=""))

    assert response.status_code == 404


def test_保管庫の外のファイルは取り出せない(client, vault_root) -> None:
    (vault_root.parent / "秘密.txt").write_text("見せない", encoding="utf-8")

    response = client.get("/notes/file/" + quote("../秘密.txt", safe=""))

    assert response.status_code == 404


def test_保管庫が未設定なら設定方法を案内する(monkeypatch) -> None:
    from companion.web import app as web_app
    from companion.web import notes_routes

    monkeypatch.setattr(notes_routes, "get_vault", lambda: None)
    with own_device_client(web_app.app) as test_client:
        response = test_client.get("/notes")

    assert response.status_code == 200
    assert "NOTES_DIR" in response.text


def test_ノートを検索できる(client) -> None:
    response = client.get("/notes", params={"q": "流れ"})

    assert response.status_code == 200
    assert "1件" in response.text
    assert "<mark>流れ</mark>" in response.text
    assert 'href="/notes/view/' in response.text


def test_検索で見つからなければそう伝える(client) -> None:
    response = client.get("/notes", params={"q": "存在しない言葉"})

    assert "見つかりませんでした" in response.text


def test_検索語に書かれたHTMLは実行させない(client) -> None:
    response = client.get("/notes", params={"q": "<script>"})

    assert "<script>" not in response.text.split("</head>")[1].split("<script src")[0]


def test_メニューにノートが出る(client) -> None:
    response = client.get("/notes")

    assert 'href="/notes"' in response.text
