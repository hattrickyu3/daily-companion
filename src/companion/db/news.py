"""ニュース要約の永続化.

出典URLは必ず一緒に保存する。要約はWeb上の外部情報をClaudeが読んだ結果であり、
正確さを保証できない。あとから原典を確認できる状態を保つのが前提条件になる。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NewsSource:
    """ニュース1件と、その出典.

    要約(summary)も一緒に持つ。画面はこれだけを見て描画できるので、
    body(読み物としての本文)と見出しが二重に並ぶのを避けられる。
    """

    title: str
    url: str
    summary: str = ""


@dataclass(frozen=True)
class NewsDigest:
    """1ジャンル分の要約."""

    digest_date: date
    topic: str
    body: str
    sources: tuple[NewsSource, ...] = ()
    created_at: str = ""


def _load_sources(raw: str | None) -> tuple[NewsSource, ...]:
    """出典のJSONを戻す. 壊れていれば空にする."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        NewsSource(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            summary=str(item.get("summary", "")),
        )
        for item in parsed
        if isinstance(item, dict) and item.get("url")
    )


def save_digest(
    conn: sqlite3.Connection,
    digest_date: date,
    topic: str,
    body: str,
    sources: tuple[NewsSource, ...] = (),
) -> None:
    """要約を保存する. 同じ日・同じジャンルなら上書きする."""
    payload = json.dumps(
        [{"title": s.title, "url": s.url, "summary": s.summary} for s in sources],
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO news_digests (digest_date, topic, body, sources) VALUES (?, ?, ?, ?)
        ON CONFLICT (digest_date, topic) DO UPDATE SET
            body = excluded.body,
            sources = excluded.sources,
            created_at = datetime('now')
        """,
        (digest_date.isoformat(), topic, body, payload),
    )


def _to_digest(row: sqlite3.Row) -> NewsDigest:
    return NewsDigest(
        digest_date=date.fromisoformat(row["digest_date"]),
        topic=row["topic"],
        body=row["body"],
        sources=_load_sources(row["sources"]),
        created_at=row["created_at"],
    )


def list_digests_for(conn: sqlite3.Connection, digest_date: date) -> list[NewsDigest]:
    """指定日の全ジャンルの要約を返す."""
    rows = conn.execute(
        "SELECT * FROM news_digests WHERE digest_date = ? ORDER BY topic",
        (digest_date.isoformat(),),
    ).fetchall()
    return [_to_digest(row) for row in rows]


def get_digest(conn: sqlite3.Connection, digest_date: date, topic: str) -> NewsDigest | None:
    """指定日・指定ジャンルの要約を返す. なければ None."""
    row = conn.execute(
        "SELECT * FROM news_digests WHERE digest_date = ? AND topic = ?",
        (digest_date.isoformat(), topic),
    ).fetchone()
    return _to_digest(row) if row else None
