"""メール通知ツール（mail-notifier）の判定結果を読む.

大学の Outlook に来たメールを mail-notifier が 8:00 / 21:00 に AI で仕分け、
結果を JSON に書き出している。このアプリはそれを読んで見せるだけで、書き込まない。

結果ファイルは別のアプリが書くので、無い・壊れている・一部の行がおかしい、
のどれでも画面は落とさず、読める分だけ見せる。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CATEGORIES = ("要対応", "確認", "不要")
# 締切の書いていない「要対応」は、届いてからこの日数だけ今日の画面に出す
UNDATED_ACTION_DAYS = 3
RECEIVED_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class MailItem:
    """仕分け済みのメール1通."""

    received: datetime
    category: str
    deadline: date | None
    summary: str
    sender: str
    subject: str

    def days_left(self, today: date) -> int | None:
        """締切までの日数. 締切が無ければ None."""
        return (self.deadline - today).days if self.deadline else None


@dataclass(frozen=True)
class MailInbox:
    """結果ファイル1つ分. items は受信の新しい順."""

    updated_at: datetime | None
    items: tuple[MailItem, ...]

    def filtered(self, category: str | None) -> tuple[MailItem, ...]:
        """区分で絞り込む. None ならすべて."""
        if category is None:
            return self.items
        return tuple(item for item in self.items if item.category == category)

    def count(self, category: str) -> int:
        return len(self.filtered(category))

    def action_items(self, today: date) -> list[MailItem]:
        """今日の画面に出す「要対応」. 締切が過ぎたものと、古い締切なしのものは除く."""
        recent_since = today - timedelta(days=UNDATED_ACTION_DAYS)

        def still_open(item: MailItem) -> bool:
            if item.deadline is not None:
                return item.deadline >= today
            return item.received.date() >= recent_since

        actions = [item for item in self.filtered("要対応") if still_open(item)]
        return sorted(actions, key=lambda item: (item.deadline or date.max, item.received))


def _parse_deadline(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_item(raw: dict) -> MailItem | None:
    """1行を読む. 受信日時か区分が読めない行は None（飛ばす）."""
    try:
        received = datetime.strptime(str(raw["received"]), RECEIVED_FORMAT)
    except (KeyError, ValueError):
        return None
    category = raw.get("category")
    if category not in CATEGORIES:
        return None
    return MailItem(
        received=received,
        category=category,
        # 締切の書式だけが崩れているなら、メール自体は見せたいので締切なしとして残す
        deadline=_parse_deadline(raw.get("deadline")),
        summary=str(raw.get("summary") or raw.get("subject") or ""),
        sender=str(raw.get("sender") or ""),
        subject=str(raw.get("subject") or ""),
    )


def _parse_updated_at(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def load(path: Path | None) -> MailInbox | None:
    """結果ファイルを読む. 未設定なら None、読めなければ空の MailInbox."""
    if path is None:
        return None
    empty = MailInbox(updated_at=None, items=())
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not read mail results: %s", path, exc_info=True)
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("mails"), list):
        return empty

    items = (_parse_item(row) for row in data["mails"] if isinstance(row, dict))
    parsed = sorted((item for item in items if item is not None),
                    key=lambda item: item.received, reverse=True)
    return MailInbox(updated_at=_parse_updated_at(data.get("updated_at")), items=tuple(parsed))
