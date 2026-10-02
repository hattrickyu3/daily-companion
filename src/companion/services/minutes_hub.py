"""議事録の画面へ、変化をすぐ届けるための配信口.

文字起こしやHaikuの編集は別スレッドで進むので、そこから publish() すると
開いている画面（Server-Sent Events）それぞれのキューへ入る。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# 画面が詰まって読まなくなったとき、溜め込みすぎないための上限. 溢れた分は捨てる。
# 画面はつなぎ直すと最初に全体を受け取り直すので、捨てても表示は戻る。
SUBSCRIBER_QUEUE_SIZE = 1000

Event = dict[str, Any]


class MinutesHub:
    """会議ごとの購読者に出来事を配る."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Event]]]] = {}

    def subscribe(self, meeting_id: int) -> asyncio.Queue[Event]:
        """イベントループの中から呼ぶ. 受け取り用のキューを返す."""
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE))
        with self._lock:
            self._subscribers[meeting_id] = [*self._subscribers.get(meeting_id, []), entry]
        return entry[1]

    def unsubscribe(self, meeting_id: int, target: asyncio.Queue[Event]) -> None:
        with self._lock:
            remaining = [e for e in self._subscribers.get(meeting_id, []) if e[1] is not target]
            if remaining:
                self._subscribers[meeting_id] = remaining
            else:
                self._subscribers.pop(meeting_id, None)

    def publish(self, meeting_id: int, event: Event) -> None:
        """どのスレッドから呼んでもよい."""
        with self._lock:
            targets = list(self._subscribers.get(meeting_id, []))
        for loop, target in targets:
            try:
                loop.call_soon_threadsafe(_put_or_drop, target, event)
            except RuntimeError:  # 画面を閉じてループが止まっている
                self.unsubscribe(meeting_id, target)


def _put_or_drop(target: asyncio.Queue[Event], event: Event) -> None:
    try:
        target.put_nowait(event)
    except asyncio.QueueFull:
        log.warning("minutes subscriber queue full; dropping event")
