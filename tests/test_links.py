"""通知リンクのURL組み立てのテスト.

外出先で通知をタップして開けないと、通知が届く意味が半分なくなる。
設定した入口が使われること、未設定でも従来どおり動くことを確認する。
"""

from __future__ import annotations

import pytest

from companion import links


@pytest.fixture
def with_base_url(monkeypatch):
    """APP_BASE_URL を差し替える."""

    def use(value: str, port: int = 8765):
        class FakeConfig:
            app_base_url = value
            web_port = port

        monkeypatch.setattr(links, "get_config", lambda: FakeConfig())
        monkeypatch.setattr(links, "local_ip", lambda: "192.168.1.20")

    return use


def test_設定した入口を使う(with_base_url) -> None:
    # Arrange
    with_base_url("http://mypc:8765")

    # Act / Assert
    assert links.app_url("/entries") == "http://mypc:8765/entries"


def test_未設定なら家のIPを使う(with_base_url) -> None:
    # Arrange: 設定前でも今まで通り動くこと（家の中では引き続き開ける）
    with_base_url("")

    # Act / Assert
    assert links.app_url("/") == "http://192.168.1.20:8765/"


def test_末尾のスラッシュが二重にならない(with_base_url) -> None:
    # Arrange: .env に「http://mypc:8765/」と書かれても壊れないこと
    with_base_url("http://mypc:8765/")

    # Act / Assert
    assert links.app_url("/reviews") == "http://mypc:8765/reviews"


def test_スラッシュなしのパスも受ける(with_base_url) -> None:
    # Arrange
    with_base_url("http://mypc:8765")

    # Act / Assert
    assert links.app_url("reviews") == "http://mypc:8765/reviews"


def test_既定はルート(with_base_url) -> None:
    with_base_url("http://mypc:8765")

    assert links.app_url() == "http://mypc:8765/"


def test_設定の末尾スラッシュは読み込み時に落とす(monkeypatch) -> None:
    # Arrange: config 側で落としているので、links 側では気にしなくてよい
    from companion.config import load_config

    monkeypatch.setenv("APP_BASE_URL", "http://mypc:8765/")

    # Act / Assert
    assert load_config().app_base_url == "http://mypc:8765"


def test_未設定なら空文字になる(monkeypatch) -> None:
    # Arrange
    from companion.config import load_config

    monkeypatch.setenv("APP_BASE_URL", "   ")

    # Act / Assert
    assert load_config().app_base_url == ""


def test_全てのジョブが共通の組み立てを使う() -> None:
    # Arrange: もとは3つのジョブが同じ関数を各自持っていた。
    # 片方だけ直して片方が家のIPのまま、という状態に戻らないようにする。
    import inspect

    from companion.jobs import evening_prompt, morning_brief, weekly_review

    for module in (evening_prompt, morning_brief, weekly_review):
        source = inspect.getsource(module)
        assert "def app_url" not in source, f"{module.__name__} が独自の app_url を持っている"
        assert "local_ip" not in source, f"{module.__name__} が家のIPを直接使っている"
        assert module.app_url is links.app_url
