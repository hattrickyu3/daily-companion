"""議事録の辞書とつなぎ言葉の一覧の永続化."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

FILLERS_KEY = "minutes_fillers"


@dataclass(frozen=True)
class DictionaryWord:
    """辞書の1語."""

    id: int
    word: str
    variants: tuple[str, ...] = ()
    course: str = ""


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part for part in (p.strip() for p in raw.split("、")) if part)


def _to_word(row: sqlite3.Row) -> DictionaryWord:
    return DictionaryWord(id=row["id"], word=row["word"], variants=_split(row["variants"]), course=row["course"])


def list_words(conn: sqlite3.Connection) -> list[DictionaryWord]:
    """全部の語. どの場面でも使う語を先に、その中は登録した順."""
    rows = conn.execute("SELECT * FROM dictionary_words ORDER BY course != '', course, id").fetchall()
    return [_to_word(row) for row in rows]


def words_for(conn: sqlite3.Connection, course: str) -> list[DictionaryWord]:
    """ある会議で使う語. その授業の語を先に、どの場面でも使う語をあとに（ヒントの長さの上限で切れるとき、授業の語を残す）."""
    rows = conn.execute(
        "SELECT * FROM dictionary_words WHERE course = '' OR course = ? ORDER BY course = '', id", (course,)
    ).fetchall()
    return [_to_word(row) for row in rows]


def save_word(conn: sqlite3.Connection, word: str, variants: tuple[str, ...], course: str) -> None:
    """語を足す. 同じ語（同じ授業）があれば、間違えやすい書き方を置き換える."""
    conn.execute(
        """
        INSERT INTO dictionary_words (word, variants, course) VALUES (?, ?, ?)
        ON CONFLICT (word, course) DO UPDATE SET variants = excluded.variants
        """,
        (word, "、".join(variants), course),
    )


def delete_word(conn: sqlite3.Connection, word_id: int) -> None:
    """語を消す. その語に登録した候補の記録も消し、同じ直しをしたらまた候補に出るようにする."""
    row = conn.execute("SELECT word FROM dictionary_words WHERE id = ?", (word_id,)).fetchone()
    conn.execute("DELETE FROM dictionary_words WHERE id = ?", (word_id,))
    if row is not None:
        conn.execute("DELETE FROM word_suggestions WHERE right = ? AND status = 'accepted'", (row["word"],))


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (key, value),
    )


# ---- 手で直したところから拾った候補 ----

SUGGESTION_PENDING = "pending"
SUGGESTION_ACCEPTED = "accepted"
SUGGESTION_DISMISSED = "dismissed"


@dataclass(frozen=True)
class Suggestion:
    """辞書の候補1つ."""

    id: int
    wrong: str
    right: str
    course: str
    meeting_id: int | None
    count: int


def _clashes(words: list[DictionaryWord], wrong: str) -> bool:
    """辞書とぶつかる直しか.

    wrong がもう辞書のどこかに載っていれば足さない: 正しい書き方として載っている（「田中」を一度「田仲」に
    直しても、田中を間違いの形にはしない）か、間違いの形として載っている（もう辞書にある直し、
    またはほかの語の間違いの形. 置き換え先が2つになる）。
    """
    return any(w.word == wrong or wrong in w.variants for w in words)


def add_suggestions(
    conn: sqlite3.Connection, pairs: list[tuple[str, str]], *, course: str, meeting_id: int | None
) -> int:
    """候補をためる. 同じ組は回数を増やすだけ. 辞書とぶつかる組と「いらない」と言った組は足さない.

    新しく足した組と、回数を増やした組の数を返す。
    """
    words = list_words(conn)
    added = 0
    for wrong, right in pairs:
        if wrong and _clashes(words, wrong):
            continue
        if not wrong and any(w.word == right and w.course in ("", course) for w in words):
            continue  # 正しい書き方だけの候補（文章から作ったもの）で、もう辞書にある語
        cursor = conn.execute(
            """
            INSERT INTO word_suggestions (wrong, right, course, meeting_id) VALUES (?, ?, ?, ?)
            ON CONFLICT (wrong, right) DO UPDATE SET count = count + 1 WHERE status = 'pending'
            """,
            (wrong, right, course, meeting_id),
        )
        added += cursor.rowcount
    return added


def list_suggestions(conn: sqlite3.Connection) -> list[Suggestion]:
    """まだ選んでいない候補. 何度も直したものを先に."""
    rows = conn.execute(
        "SELECT * FROM word_suggestions WHERE status = ? ORDER BY count DESC, id", (SUGGESTION_PENDING,)
    ).fetchall()
    return [
        Suggestion(id=r["id"], wrong=r["wrong"], right=r["right"], course=r["course"],
                   meeting_id=r["meeting_id"], count=r["count"])
        for r in rows
    ]


def count_suggestions(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM word_suggestions WHERE status = ?", (SUGGESTION_PENDING,)).fetchone()
    return int(row["n"])


def add_variant(conn: sqlite3.Connection, word: str, variant: str, course: str) -> None:
    """辞書の語に間違えやすい書き方を1つ足す. 語が無ければ作る. 前からある書き方は残す."""
    row = conn.execute("SELECT variants FROM dictionary_words WHERE word = ? AND course = ?", (word, course)).fetchone()
    variants = _split(row["variants"]) if row else ()
    if variant not in variants:
        save_word(conn, word, (*variants, variant), course)


def accept_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> bool:
    """候補を辞書に登録する. 登録したら True（もう選んだ候補なら False）."""
    row = conn.execute(
        "SELECT * FROM word_suggestions WHERE id = ? AND status = ?", (suggestion_id, SUGGESTION_PENDING)
    ).fetchone()
    if row is None:
        return False
    if not row["wrong"]:
        # 正しい書き方だけの候補（文章から作ったもの）. 語が無ければ足す（もうあれば何もしない）.
        exists = conn.execute(
            "SELECT 1 FROM dictionary_words WHERE word = ? AND course IN ('', ?)", (row["right"], row["course"])
        ).fetchone()
        if not exists:
            save_word(conn, row["right"], (), row["course"])
        conn.execute("UPDATE word_suggestions SET status = ? WHERE id = ?", (SUGGESTION_ACCEPTED, suggestion_id))
        return True
    if _clashes(list_words(conn), row["wrong"]):
        # 候補をためたあとに辞書が変わり、ぶつかるようになった. 登録せずに候補からも外す.
        conn.execute("UPDATE word_suggestions SET status = ? WHERE id = ?", (SUGGESTION_DISMISSED, suggestion_id))
        return False
    # 同じ語が「すべての場面」の語としてもうあれば、そこに足す（人の名前などを授業ごとに分けてしまわない）.
    shared = conn.execute("SELECT 1 FROM dictionary_words WHERE word = ? AND course = ''", (row["right"],)).fetchone()
    add_variant(conn, row["right"], row["wrong"], "" if shared else row["course"])
    conn.execute("UPDATE word_suggestions SET status = ? WHERE id = ?", (SUGGESTION_ACCEPTED, suggestion_id))
    return True


def dismiss_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> None:
    """候補を「いらない」にする. 同じ組は二度と出さない."""
    conn.execute(
        "UPDATE word_suggestions SET status = ? WHERE id = ? AND status = ?",
        (SUGGESTION_DISMISSED, suggestion_id, SUGGESTION_PENDING),
    )
