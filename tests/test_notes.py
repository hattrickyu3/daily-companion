"""Obsidianの保管庫を読む処理と、ノートをHTMLにする処理のテスト.

本物の保管庫（OneDrive\\大学）には触れず、一時フォルダに小さな保管庫を作って確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.services.notes import Vault
from companion.services.notes_render import render


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """統計学のノート2枚と画像・PDF、Obsidianの設定フォルダを持つ保管庫."""
    root = tmp_path / "大学"
    (root / "統計学" / "ノート").mkdir(parents=True)
    (root / "統計学" / "ノート" / "統計学まとめ.md").write_text("# まとめ\n[[確率分布]]", encoding="utf-8")
    (root / "統計学" / "ノート" / "確率分布.md").write_text("## 正規分布\n本文", encoding="utf-8")
    (root / "統計学" / "表.png").write_bytes(b"png")
    (root / "統計学" / "資料.pdf").write_bytes(b"pdf")
    (root / "機械学習").mkdir()
    (root / "機械学習" / "確率分布.md").write_text("別の授業の同名ノート", encoding="utf-8")
    (root / "直下のメモ.md").write_text("メモ", encoding="utf-8")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    return Vault(root)


# ---- 保管庫の一覧 ----


def test_ノートは一番上のフォルダごとにまとまる(vault) -> None:
    groups = {group.folder: [note.path for note in group.notes] for group in vault.groups()}

    assert groups["統計学"] == ["統計学/ノート/確率分布.md", "統計学/ノート/統計学まとめ.md"]
    assert groups["機械学習"] == ["機械学習/確率分布.md"]
    assert groups[""] == ["直下のメモ.md"]


def test_授業の議事録フォルダも授業のノートと一緒にまとまる(tmp_path) -> None:
    root = tmp_path / "大学"
    for rel in ["3年３Q/自然言語処理/ノート/自然言語処理まとめ.md", "3年３Q/自然言語処理/議事録/2026-10-24 自然言語処理.md"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("本文", encoding="utf-8")

    groups = {group.folder: len(group.notes) for group in Vault(root).groups()}

    assert groups == {"3年３Q/自然言語処理": 2}


def test_学期のフォルダの中でも授業ごとにまとまり新しい学期が先に来る(tmp_path) -> None:
    root = tmp_path / "大学"
    for rel in [
        "2年まで/統計学/ノート/仮説検定.md",
        "2年まで/統計学/ノート/統計学まとめ.md",
        "3年３Q/自然言語処理/ノート/自然言語処理まとめ.md",
        "3年３Q/ウェブデータ処理/ノート/ウェブデータ処理まとめ.md",
        "CLAUDE.md",
    ]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("本文", encoding="utf-8")

    folders = [group.folder for group in Vault(root).groups()]

    assert folders == [
        "3年３Q/ウェブデータ処理",
        "3年３Q/自然言語処理",
        "2年まで/統計学",
        "",
    ]


def test_ノートの表示名は拡張子を除いたファイル名(vault) -> None:
    statistics = next(group for group in vault.groups() if group.folder == "統計学")

    assert [note.name for note in statistics.notes] == ["確率分布", "統計学まとめ"]


def test_Obsidianの設定フォルダは一覧に出ない(vault) -> None:
    folders = [group.folder for group in vault.groups()]

    assert ".obsidian" not in folders


def test_保管庫のフォルダが無ければ一覧は空(tmp_path) -> None:
    assert Vault(tmp_path / "存在しない").groups() == []


# ---- リンク先の解決（Obsidianと同じ探し方） ----


def test_名前だけのリンクは同じフォルダのノートを優先する(vault) -> None:
    resolved = vault.resolve("確率分布", current="統計学/ノート/統計学まとめ.md")

    assert resolved == "統計学/ノート/確率分布.md"


def test_同名が複数あれば今のノートから近い方を選ぶ(vault) -> None:
    resolved = vault.resolve("確率分布", current="機械学習/別のノート.md")

    assert resolved == "機械学習/確率分布.md"


def test_拡張子付きのリンクはそのファイルを指す(vault) -> None:
    assert vault.resolve("表.png", current="統計学/ノート/統計学まとめ.md") == "統計学/表.png"


def test_フォルダ付きのリンクも解決できる(vault) -> None:
    assert vault.resolve("機械学習/確率分布", current="直下のメモ.md") == "機械学習/確率分布.md"


def test_見つからないリンクはNone(vault) -> None:
    assert vault.resolve("存在しないノート", current="直下のメモ.md") is None


# ---- ファイルの取り出し（保管庫の外は見せない） ----


def test_保管庫の中のファイルは取り出せる(vault) -> None:
    path = vault.file_path("統計学/表.png")

    assert path is not None and path.read_bytes() == b"png"


@pytest.mark.parametrize(
    "unsafe",
    [
        "../秘密.txt",
        "統計学/../../秘密.txt",
        ".obsidian/app.json",
        "統計学",
        "存在しない.md",
        "",
        # Windowsでは \ も区切り文字になるので、/ だけ見ていると隠しフォルダに入られる
        "統計学\\..\\.obsidian\\app.json",
        ".OBSIDIAN/app.json",
        # NTFSの代替データストリーム
        "直下のメモ.md:secret",
    ],
)
def test_保管庫の外や隠しファイルやフォルダは取り出せない(vault, tmp_path, unsafe) -> None:
    (tmp_path / "秘密.txt").write_text("見せてはいけない", encoding="utf-8")

    assert vault.file_path(unsafe) is None


def test_ノートの本文を読める(vault) -> None:
    assert vault.read_note("統計学/ノート/確率分布.md") == "## 正規分布\n本文"


def test_md以外はノートとして読まない(vault) -> None:
    assert vault.read_note("統計学/資料.pdf") is None


# ---- HTMLへの変換 ----

CURRENT = "統計学/ノート/統計学まとめ.md"


def test_内部リンクはノートの画面へのリンクになる(vault) -> None:
    html = render("[[確率分布]]", CURRENT, vault)

    assert 'href="/notes/view/%E7%B5%B1' in html
    assert ">確率分布</a>" in html


def test_別名つきのリンクは別名を表示する(vault) -> None:
    html = render("[[確率分布|分布の一覧]]", CURRENT, vault)

    assert ">分布の一覧</a>" in html


def test_見出しへのリンクはページ内の位置まで飛ぶ(vault) -> None:
    html = render("[[確率分布#正規分布]]", CURRENT, vault)

    assert "#%E6%AD%A3%E8%A6%8F%E5%88%86%E5%B8%83" in html


def test_見出しにはリンク先と同じidが付く(vault) -> None:
    html = render("## 正規分布", CURRENT, vault)

    assert 'id="正規分布"' in html


def test_PDFへのリンクはファイルを開く(vault) -> None:
    html = render("[[資料.pdf|講義スライド]]", CURRENT, vault)

    assert 'href="/notes/file/' in html
    assert ">講義スライド</a>" in html


def test_画像の埋め込みは画像として表示する(vault) -> None:
    html = render("![[表.png]]", CURRENT, vault)

    assert "<img" in html
    assert 'src="/notes/file/%E7%B5%B1%E8%A8%88%E5%AD%A6/%E8%A1%A8.png"' in html


def test_見つからないリンクはリンクにしない(vault) -> None:
    html = render("[[まだ無いノート]]", CURRENT, vault)

    assert "<a" not in html
    assert "まだ無いノート" in html


def test_数式はそのまま残してブラウザ側で描く(vault) -> None:
    html = render("$a_1 * b_2$ と\n\n$$\nx^2\n$$", CURRENT, vault)

    assert '<span class="math inline">a_1 * b_2</span>' in html
    assert "math block" in html


def test_表を表示できる(vault) -> None:
    html = render("| a | b |\n|---|---|\n| 1 | 2 |", CURRENT, vault)

    assert "<table>" in html and "<td>1</td>" in html


def test_コールアウトは見出しつきの枠になる(vault) -> None:
    html = render("> [!warning] 注意点\n> 本文", CURRENT, vault)

    assert '<blockquote class="callout">' in html
    assert "<strong>注意点</strong>" in html
    assert "[!warning]" not in html


def test_ノートに書かれたHTMLは実行させない(vault) -> None:
    html = render("<script>alert(1)</script>\n\n[[<b>x</b>]]", CURRENT, vault)

    assert "<script>" not in html
    assert "<b>" not in html
