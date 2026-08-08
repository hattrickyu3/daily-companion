"""過去のニュースを表で見るための組み立て.

保存単位は「日 × ジャンル」だが、表で読むときは1記事1行のほうが追いやすい。
ここで記事単位に開いてから渡す。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from companion.db.news import list_digests, list_used_topics
from companion.services.news import topic_label

DEFAULT_ROW_LIMIT = 300


@dataclass(frozen=True)
class NewsRow:
    """表の1行（記事1件）."""

    digest_date: date
    topic: str
    title: str
    summary: str
    url: str

    @property
    def topic_name(self) -> str:
        return topic_label(self.topic)

    @property
    def date_label(self) -> str:
        return f"{self.digest_date:%m/%d}"

    @property
    def weekday(self) -> str:
        return "月火水木金土日"[self.digest_date.weekday()]


@dataclass(frozen=True)
class NewsHistory:
    """表示に必要なものひとまとめ."""

    rows: tuple[NewsRow, ...]
    topics: tuple[tuple[str, str], ...]
    active_topic: str | None

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def day_count(self) -> int:
        return len({row.digest_date for row in self.rows})


def build(
    conn: sqlite3.Connection, *, topic: str | None = None, limit: int = DEFAULT_ROW_LIMIT
) -> NewsHistory:
    """過去のニュースを1記事1行に開いて返す."""
    rows: list[NewsRow] = []

    for digest in list_digests(conn, topic=topic):
        for source in digest.sources:
            rows.append(
                NewsRow(
                    digest_date=digest.digest_date,
                    topic=digest.topic,
                    title=source.title,
                    summary=source.summary,
                    url=source.url,
                )
            )
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    topics = tuple((name, topic_label(name)) for name in list_used_topics(conn))
    return NewsHistory(rows=tuple(rows), topics=topics, active_topic=topic)
