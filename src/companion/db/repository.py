"""日記エントリの永続化.

原文 (raw_text) と、Claudeが生成した構造化結果を分けて扱う。
構造化に失敗しても原文だけは必ず残るよう、保存を2段階に分けている。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

MIN_FTS_QUERY_LENGTH = 3
DEFAULT_LIST_LIMIT = 50


@dataclass(frozen=True)
class Entry:
    """1日分の日記. 生成後は変更しない."""

    id: int
    entry_date: date
    raw_text: str
    summary: str | None = None
    done: tuple[str, ...] = ()
    insights: tuple[str, ...] = ()
    carry_over: str | None = None
    mood: str | None = None
    tags: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_structured(self) -> bool:
        """Claudeによる整理が済んでいるか."""
        return self.summary is not None


@dataclass(frozen=True)
class StructuredEntry:
    """Claudeが生成した構造化結果."""

    summary: str
    done: tuple[str, ...] = ()
    insights: tuple[str, ...] = ()
    carry_over: str | None = None
    mood: str | None = None
    tags: tuple[str, ...] = field(default=())


def _load_list(raw: str | None) -> tuple[str, ...]:
    """JSON配列のカラムをタプルに戻す. 壊れていれば空にする."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def _dump_list(values: tuple[str, ...] | list[str]) -> str | None:
    """タプルをJSON配列のテキストにする. 空なら NULL 相当."""
    return json.dumps(list(values), ensure_ascii=False) if values else None


def _to_entry(row: sqlite3.Row, tags: tuple[str, ...]) -> Entry:
    """SELECT の1行を Entry に変換する."""
    return Entry(
        id=row["id"],
        entry_date=date.fromisoformat(row["entry_date"]),
        raw_text=row["raw_text"],
        summary=row["summary"],
        done=_load_list(row["done"]),
        insights=_load_list(row["insights"]),
        carry_over=row["carry_over"],
        mood=row["mood"],
        tags=tags,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fetch_tags(conn: sqlite3.Connection, entry_id: int) -> tuple[str, ...]:
    """エントリに紐づくタグ名を返す."""
    rows = conn.execute(
        "SELECT t.name FROM tags t"
        " JOIN entry_tags et ON et.tag_id = t.id"
        " WHERE et.entry_id = ? ORDER BY t.name",
        (entry_id,),
    )
    return tuple(row["name"] for row in rows)


def save_raw_text(conn: sqlite3.Connection, entry_date: date, raw_text: str) -> int:
    """原文を保存し、エントリIDを返す.

    既にその日のエントリがあれば原文を差し替える。
    構造化結果は「原文と食い違う古い要約」を残さないため、その時点で消す。
    """
    row = conn.execute(
        """
        INSERT INTO entries (entry_date, raw_text) VALUES (?, ?)
        ON CONFLICT (entry_date) DO UPDATE SET
            raw_text   = excluded.raw_text,
            summary    = NULL,
            done       = NULL,
            insights   = NULL,
            carry_over = NULL,
            mood       = NULL,
            updated_at = datetime('now')
        RETURNING id
        """,
        (entry_date.isoformat(), raw_text),
    ).fetchone()

    entry_id = row["id"]
    conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
    return entry_id


def apply_structure(conn: sqlite3.Connection, entry_id: int, structured: StructuredEntry) -> None:
    """Claudeの構造化結果をエントリに反映する."""
    conn.execute(
        """
        UPDATE entries SET
            summary    = ?,
            done       = ?,
            insights   = ?,
            carry_over = ?,
            mood       = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            structured.summary,
            _dump_list(structured.done),
            _dump_list(structured.insights),
            structured.carry_over,
            structured.mood,
            entry_id,
        ),
    )
    replace_tags(conn, entry_id, structured.tags)


def replace_tags(conn: sqlite3.Connection, entry_id: int, names: tuple[str, ...]) -> None:
    """エントリのタグを与えられた集合で置き換える."""
    conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
    for name in dict.fromkeys(name.strip() for name in names if name.strip()):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)", (entry_id, tag_id)
        )


def get_entry(conn: sqlite3.Connection, entry_date: date) -> Entry | None:
    """指定日のエントリを返す. なければ None."""
    row = conn.execute(
        "SELECT * FROM entries WHERE entry_date = ?", (entry_date.isoformat(),)
    ).fetchone()
    return _to_entry(row, _fetch_tags(conn, row["id"])) if row else None


def get_entry_by_id(conn: sqlite3.Connection, entry_id: int) -> Entry | None:
    """IDでエントリを返す. なければ None."""
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _to_entry(row, _fetch_tags(conn, row["id"])) if row else None


def list_entries(
    conn: sqlite3.Connection, *, limit: int = DEFAULT_LIST_LIMIT, tag: str | None = None
) -> list[Entry]:
    """新しい順にエントリを返す. tag を指定するとそのタグを持つものに絞る."""
    if tag:
        rows = conn.execute(
            "SELECT e.* FROM entries e"
            " JOIN entry_tags et ON et.entry_id = e.id"
            " JOIN tags t ON t.id = et.tag_id"
            " WHERE t.name = ? ORDER BY e.entry_date DESC LIMIT ?",
            (tag, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entries ORDER BY entry_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_to_entry(row, _fetch_tags(conn, row["id"])) for row in rows]


def list_entries_between(conn: sqlite3.Connection, start: date, end: date) -> list[Entry]:
    """期間内のエントリを古い順に返す. 週次レポートで使う."""
    rows = conn.execute(
        "SELECT * FROM entries WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [_to_entry(row, _fetch_tags(conn, row["id"])) for row in rows]


def search_entries(
    conn: sqlite3.Connection, query: str, *, limit: int = DEFAULT_LIST_LIMIT
) -> list[Entry]:
    """日記を全文検索する.

    trigram トークナイザは3文字未満を索引できないため、
    短い語のときは LIKE による部分一致にフォールバックする。
    """
    keyword = query.strip()
    if not keyword:
        return []

    if len(keyword) < MIN_FTS_QUERY_LENGTH:
        rows = conn.execute(
            "SELECT * FROM entries WHERE raw_text LIKE ? ESCAPE '\\'"
            " ORDER BY entry_date DESC LIMIT ?",
            (f"%{_escape_like(keyword)}%", limit),
        ).fetchall()
    else:
        # FTS5 の構文解釈を避けるため、常にフレーズとして引用符で囲む。
        phrase = '"' + keyword.replace('"', '""') + '"'
        rows = conn.execute(
            "SELECT e.* FROM entries e"
            " JOIN entries_fts f ON f.rowid = e.id"
            " WHERE entries_fts MATCH ? ORDER BY e.entry_date DESC LIMIT ?",
            (phrase, limit),
        ).fetchall()

    return [_to_entry(row, _fetch_tags(conn, row["id"])) for row in rows]


def _escape_like(value: str) -> str:
    """LIKE のワイルドカードを打ち消す."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_all_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """使用中のタグを (名前, 件数) で多い順に返す."""
    rows = conn.execute(
        "SELECT t.name, count(*) AS n FROM tags t"
        " JOIN entry_tags et ON et.tag_id = t.id"
        " GROUP BY t.name ORDER BY n DESC, t.name"
    ).fetchall()
    return [(row["name"], row["n"]) for row in rows]
