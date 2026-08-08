"""月カレンダーの組み立てのテスト.

月の境界と、前後の月にはみ出したマスの扱いを重点的に確認する。
ここを間違えると、隣の月の日付を今月のものと見間違える。
"""

from __future__ import annotations

from datetime import date

from companion.db.events import upsert_events
from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
from companion.integrations.calendar_google import CalendarEvent
from companion.services import month_view

TODAY = date(2026, 8, 8)


def make_event(uid: str, day: date) -> CalendarEvent:
    return CalendarEvent(
        uid=uid, title="ゼミ", event_date=day, starts_at=None, ends_at=None,
        location=None, is_all_day=True,
    )


def cells_of(view: month_view.MonthView) -> list[month_view.DayCell]:
    return [cell for week in view.weeks for cell in week if not cell.is_blank]


def test_その月の日数分のマスができる(conn) -> None:
    # Act: 2026年8月は31日
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    assert len(cells_of(view)) == 31


def test_前後の月のマスは空欄になる(conn) -> None:
    # Arrange / Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert: 2026/08/01は土曜なので、先頭に6日分の空欄が入る
    first_week = view.weeks[0]
    assert [cell.is_blank for cell in first_week] == [True] * 6 + [False]


def test_日記のある日に印がつく(conn) -> None:
    # Arrange
    entry_id = save_raw_text(conn, TODAY, "今日は実装した")
    apply_structure(conn, entry_id, StructuredEntry(summary="実装した1日"))

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    written = [cell for cell in cells_of(view) if cell.has_entry]
    assert len(written) == 1
    assert written[0].day == TODAY
    assert written[0].snippet == "実装した1日"


def test_要約がなければ原文の抜粋を出す(conn) -> None:
    # Arrange: 整理が終わる前でも何か書いてあることが分かるように
    save_raw_text(conn, TODAY, "まだ整理されていない原文")

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    cell = next(c for c in cells_of(view) if c.day == TODAY)
    assert cell.snippet == "まだ整理されていない原文"


def test_長い抜粋は切り詰める(conn) -> None:
    # Arrange
    save_raw_text(conn, TODAY, "あ" * 200)

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    cell = next(c for c in cells_of(view) if c.day == TODAY)
    assert cell.snippet.endswith("…")
    assert len(cell.snippet) == month_view.SNIPPET_LENGTH + 1


def test_抜粋の改行は空白に潰す(conn) -> None:
    # Arrange: マスの中で改行されると崩れる
    save_raw_text(conn, TODAY, "1行目\n2行目")

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    cell = next(c for c in cells_of(view) if c.day == TODAY)
    assert "\n" not in cell.snippet
    assert cell.snippet == "1行目 2行目"


def test_予定の件数が日ごとに集計される(conn) -> None:
    # Arrange
    upsert_events(conn, [make_event("a", TODAY), make_event("b", TODAY),
                         make_event("c", date(2026, 8, 9))])

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    counts = {cell.day: cell.event_count for cell in cells_of(view) if cell.event_count}
    assert counts == {TODAY: 2, date(2026, 8, 9): 1}


def test_隣の月のデータを混ぜない(conn) -> None:
    # Arrange
    save_raw_text(conn, date(2026, 7, 31), "先月末")
    save_raw_text(conn, date(2026, 9, 1), "来月頭")

    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    assert view.entry_count == 0
    assert not any(cell.has_entry for cell in cells_of(view))


def test_今日が分かる(conn) -> None:
    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    marked = [cell for cell in cells_of(view) if cell.is_today]
    assert len(marked) == 1
    assert marked[0].day == TODAY


def test_別の月を見ているときは今日を印さない(conn) -> None:
    # Act
    view = month_view.build(conn, 2026, 9, TODAY)

    # Assert
    assert not any(cell.is_today for cell in cells_of(view))


def test_前後の月へのリンクを持つ(conn) -> None:
    # Act
    view = month_view.build(conn, 2026, 8, TODAY)

    # Assert
    assert view.prev_month == "2026-07"
    assert view.next_month == "2026-09"


def test_年をまたぐ移動ができる() -> None:
    assert month_view.shift_month(2026, 1, -1) == "2025-12"
    assert month_view.shift_month(2026, 12, 1) == "2027-01"


def test_月指定を解釈できる() -> None:
    assert month_view.parse_month("2026-03", TODAY) == (2026, 3)


def test_不正な月指定は今月にフォールバックする() -> None:
    # Arrange / Act / Assert: URLを手で書き換えられても落ちない
    for bad in (None, "", "2026-13", "hello", "2026", "abcd-ef"):
        assert month_view.parse_month(bad, TODAY) == (2026, 8)


def test_うるう年の2月は29日ある(conn) -> None:
    # Act
    view = month_view.build(conn, 2028, 2, TODAY)

    # Assert
    assert len(cells_of(view)) == 29


def test_土日が判別できる(conn) -> None:
    # Arrange / Act: 2026/08/08は土曜、08/09は日曜
    view = month_view.build(conn, 2026, 8, TODAY)
    cells = {cell.day: cell for cell in cells_of(view)}

    # Assert
    assert cells[date(2026, 8, 8)].is_saturday
    assert cells[date(2026, 8, 9)].is_sunday
