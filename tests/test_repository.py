"""日記リポジトリのテスト."""

from __future__ import annotations

from datetime import date

from companion.db.repository import (
    StructuredEntry,
    apply_structure,
    get_entry,
    list_all_tags,
    list_entries,
    list_entries_between,
    save_raw_text,
    search_entries,
)

DAY = date(2026, 8, 8)


def test_原文を保存して取り出せる(conn) -> None:
    # Arrange / Act
    save_raw_text(conn, DAY, "今日はカレンダー連携を実装した")

    # Assert
    entry = get_entry(conn, DAY)
    assert entry is not None
    assert entry.raw_text == "今日はカレンダー連携を実装した"
    assert entry.is_structured is False


def test_同じ日に保存すると上書きされる(conn) -> None:
    # Arrange
    save_raw_text(conn, DAY, "最初の内容")

    # Act
    save_raw_text(conn, DAY, "書き直した内容")

    # Assert
    assert len(list_entries(conn)) == 1
    entry = get_entry(conn, DAY)
    assert entry is not None
    assert entry.raw_text == "書き直した内容"


def test_原文を書き直すと古い要約は破棄される(conn) -> None:
    # Arrange: 要約とタグまで付いた状態を作る
    entry_id = save_raw_text(conn, DAY, "最初の内容")
    apply_structure(conn, entry_id, StructuredEntry(summary="最初の要約", tags=("開発",)))

    # Act
    save_raw_text(conn, DAY, "まったく違う内容")

    # Assert: 原文と食い違う要約が残らない
    entry = get_entry(conn, DAY)
    assert entry is not None
    assert entry.summary is None
    assert entry.tags == ()


def test_構造化結果を反映できる(conn) -> None:
    # Arrange
    entry_id = save_raw_text(conn, DAY, "朝から実装、夜に詰まった")

    # Act
    apply_structure(
        conn,
        entry_id,
        StructuredEntry(
            summary="実装を進めたが夜に詰まった",
            done=("カレンダー連携", "テスト作成"),
            insights=("先に疎通確認をすると早い",),
            carry_over="明日はWeb画面から",
            mood="やや疲れ",
            tags=("開発", "daily-companion"),
        ),
    )

    # Assert
    entry = get_entry(conn, DAY)
    assert entry is not None
    assert entry.is_structured is True
    assert entry.done == ("カレンダー連携", "テスト作成")
    assert entry.insights == ("先に疎通確認をすると早い",)
    assert entry.carry_over == "明日はWeb画面から"
    assert entry.tags == ("daily-companion", "開発")


def test_タグは重複せず登録される(conn) -> None:
    # Arrange
    first = save_raw_text(conn, DAY, "1日目")
    second = save_raw_text(conn, date(2026, 8, 9), "2日目")

    # Act: 同じタグを2日分に付ける
    apply_structure(conn, first, StructuredEntry(summary="A", tags=("開発", "開発")))
    apply_structure(conn, second, StructuredEntry(summary="B", tags=("開発",)))

    # Assert
    assert list_all_tags(conn) == [("開発", 2)]


def test_タグでエントリを絞り込める(conn) -> None:
    # Arrange
    first = save_raw_text(conn, DAY, "開発の日")
    second = save_raw_text(conn, date(2026, 8, 9), "就活の日")
    apply_structure(conn, first, StructuredEntry(summary="A", tags=("開発",)))
    apply_structure(conn, second, StructuredEntry(summary="B", tags=("就活",)))

    # Act
    found = list_entries(conn, tag="就活")

    # Assert
    assert [e.raw_text for e in found] == ["就活の日"]


def test_一覧は新しい順に返る(conn) -> None:
    # Arrange
    save_raw_text(conn, date(2026, 8, 6), "6日")
    save_raw_text(conn, date(2026, 8, 8), "8日")
    save_raw_text(conn, date(2026, 8, 7), "7日")

    # Act
    found = list_entries(conn)

    # Assert
    assert [e.raw_text for e in found] == ["8日", "7日", "6日"]


def test_期間指定は古い順に返る(conn) -> None:
    # Arrange
    for day in (6, 7, 8, 9):
        save_raw_text(conn, date(2026, 8, day), f"{day}日")

    # Act
    found = list_entries_between(conn, date(2026, 8, 7), date(2026, 8, 8))

    # Assert
    assert [e.raw_text for e in found] == ["7日", "8日"]


def test_日本語で全文検索できる(conn) -> None:
    # Arrange
    save_raw_text(conn, DAY, "今日はカレンダー連携を実装した")
    save_raw_text(conn, date(2026, 8, 9), "今日は面接の準備をした")

    # Act
    found = search_entries(conn, "カレンダー")

    # Assert
    assert len(found) == 1
    assert found[0].entry_date == DAY


def test_2文字の検索語でもヒットする(conn) -> None:
    # Arrange: trigramは3文字未満を索引できないためLIKEに落ちる経路
    save_raw_text(conn, DAY, "面接の練習をした")

    # Act
    found = search_entries(conn, "面接")

    # Assert
    assert len(found) == 1


def test_検索語が空なら何も返さない(conn) -> None:
    # Arrange
    save_raw_text(conn, DAY, "何か書いた")

    # Act / Assert
    assert search_entries(conn, "   ") == []


def test_検索語の記号でクエリが壊れない(conn) -> None:
    # Arrange: FTS5の構文として解釈されうる文字を含む
    save_raw_text(conn, DAY, 'AND OR "引用符" を含む文章')

    # Act / Assert: 例外を出さず検索できる
    assert len(search_entries(conn, '"引用符"')) == 1
    assert search_entries(conn, "AND NOT") == []


def test_要約を書き換えても原文は変わらない(conn) -> None:
    # Arrange
    entry_id = save_raw_text(conn, DAY, "書いたままの原文")

    # Act
    apply_structure(conn, entry_id, StructuredEntry(summary="要約に置き換わったりしない"))

    # Assert
    entry = get_entry(conn, DAY)
    assert entry is not None
    assert entry.raw_text == "書いたままの原文"


def test_存在しない日はNoneを返す(conn) -> None:
    assert get_entry(conn, DAY) is None
