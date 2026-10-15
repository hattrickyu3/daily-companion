"""テスト共通のフィクスチャ."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from companion.db.connection import apply_schema


@pytest.fixture
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """テスト専用の使い捨てDB. 本物の data/companion.db には触れない."""
    connection = sqlite3.connect(tmp_path / "test.db", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _no_real_recorder_folder(monkeypatch) -> None:
    """本物の .env に見張るフォルダが書いてあっても、テスト中は本物のGoogleドライブを見に行かない."""
    monkeypatch.setenv("RECORDER_FOLDER", "")
