"""ノートの全文検索のテスト."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.services.notes import Vault
from companion.services.notes_search import search


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "大学"
    (root / "統計学").mkdir(parents=True)
    (root / "統計学" / "仮説検定.md").write_text(
        "# 仮説検定\n"
        "帰無仮説を立てる。\n"
        "\n"
        "t検定は母平均の検定に使う。p値が有意水準より小さければ棄却する。\n"
        "カイ二乗検定はクロス表に使う。",
        encoding="utf-8",
    )
    (root / "統計学" / "確率分布.md").write_text(
        "正規分布は平均と分散で決まる。検定でも使う。", encoding="utf-8"
    )
    (root / "機械学習").mkdir()
    (root / "機械学習" / "SVM.md").write_text("マージン最大化。正規分布は出てこない。", encoding="utf-8")
    (root / "統計学" / "資料.pdf").write_bytes("検定".encode("utf-8"))
    return Vault(root)


def _paths(hits) -> list[str]:
    return [hit.note.path for hit in hits]


def test_本文に言葉を含むノートが見つかる(vault) -> None:
    assert _paths(search(vault, "正規分布")) == ["機械学習/SVM.md", "統計学/確率分布.md"]


def test_題名に含むノートを先に出す(vault) -> None:
    hits = search(vault, "検定")

    assert _paths(hits)[0] == "統計学/仮説検定.md"
    assert hits[0].title_hit


def test_md以外のファイルは探さない(vault) -> None:
    assert "統計学/資料.pdf" not in _paths(search(vault, "検定"))


def test_スペースで区切るとすべての言葉を含むノートだけ(vault) -> None:
    assert _paths(search(vault, "正規分布 検定")) == ["統計学/確率分布.md"]


def test_全角スペースでも区切れる(vault) -> None:
    assert _paths(search(vault, "正規分布　検定")) == ["統計学/確率分布.md"]


def test_全角半角と大文字小文字を区別しない(vault) -> None:
    assert _paths(search(vault, "Ｔ検定")) == ["統計学/仮説検定.md"]
    assert _paths(search(vault, "svm")) == ["機械学習/SVM.md"]


def test_空の検索語では何も返さない(vault) -> None:
    assert search(vault, "   ") == []


def test_見つからなければ空(vault) -> None:
    assert search(vault, "ベイズ") == []


def test_ヒットした行を抜き出し言葉に印を付ける(vault) -> None:
    hit = search(vault, "p値")[0]

    snippet = hit.snippets[0]
    assert "".join(segment.text for segment in snippet).startswith("t検定は母平均")
    assert [segment.text for segment in snippet if segment.hit] == ["p値"]


def test_抜き出しは元の表記のまま見せる(vault) -> None:
    hit = search(vault, "Ｔ検定")[0]

    marked = [segment.text for segment in hit.snippets[0] if segment.hit]
    assert marked == ["t検定"]


def test_長い行は言葉の周りだけ切り出す(tmp_path) -> None:
    root = tmp_path / "v"
    root.mkdir()
    (root / "長い.md").write_text("あ" * 200 + "目印" + "い" * 200, encoding="utf-8")

    snippet = search(Vault(root), "目印")[0].snippets[0]
    text = "".join(segment.text for segment in snippet)

    assert len(text) < 100
    assert text.startswith("…") and text.endswith("…")


def test_抜き出しは1枚につき3行まで(tmp_path) -> None:
    root = tmp_path / "v"
    root.mkdir()
    (root / "多い.md").write_text("\n".join(f"{i}行目の検定" for i in range(10)), encoding="utf-8")

    hit = search(Vault(root), "検定")[0]

    assert len(hit.snippets) == 3
    assert hit.count == 10


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ("- **中心極限定理**：正規分布に近づく", "中心極限定理：正規分布に近づく"),
        ("→ [[確率分布#正規分布|正規分布の節]]", "→ 正規分布の節"),
        ("→ [[確率分布#正規分布]]", "→ 確率分布 › 正規分布"),
        ("| 量的 | z検定 | 正規分布 |", "量的 ／ z検定 ／ 正規分布"),
        ("尖度（$\\sigma^4$、正規分布で0）", "尖度（\\sigma^4、正規分布で0）"),
    ],
)
def test_抜き出しではMarkdownの記号を落として見せる(tmp_path, line, shown) -> None:
    root = tmp_path / "v"
    root.mkdir()
    (root / "メモ.md").write_text(line, encoding="utf-8")

    snippet = search(Vault(root), "正規分布")[0].snippets[0]

    assert "".join(segment.text for segment in snippet) == shown


def test_題名だけに含むノートも見つかる(vault) -> None:
    hit = search(vault, "SVM")[0]

    assert hit.title_hit
    assert hit.snippets == ()
