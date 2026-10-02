"""録音中に、Haikuが議事録を書き進める係.

文字起こしが1つ届くたびに notify() で起こされ、まだ反映していない発言をまとめてHaikuに渡す。
Haikuが前の発言を処理している間に届いた発言は、次の1回にまとめて渡すので遅れが積み上がらない。

Haikuが失敗しても、文字起こしと音声には影響しない。反映できなかった発言は
「未反映」のまま残り、次の回に改めて渡される。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from companion.db.connection import transaction
from companion.db.meetings import Segment, get_meeting, mark_drafted, save_draft, undrafted_segments
from companion.integrations.claude_runner import ClaudeError
from companion.integrations.claude_stream import ClaudeStreamSession
from companion.services.minutes_draft import (
    SECTIONS,
    apply_ops,
    editing_section,
    normalize_draft,
    parse_ops,
    section_label,
)
from companion.services.minutes_hub import MinutesHub

log = logging.getLogger(__name__)

HAIKU_MODEL = "haiku"
# 発言が立て続けに届いたとき、少し待ってからまとめて渡す.
DEBOUNCE_SEC = 0.8
TURN_TIMEOUT_SEC = 90
# やり取りが長くなるほど1回が重くなるので、この回数ごとに新しく起動し直す（今の議事録を渡して続きから）.
ROTATE_AFTER_TURNS = 20
# 書いている途中を画面に送る間隔. 1文字ごとに送ると多すぎる.
PUBLISH_INTERVAL_SEC = 0.15
# 発言が来ないまましばらく経ったら、プロセスを閉じておく（次に来たら起動し直す）.
IDLE_CLOSE_SEC = 20 * 60
MAX_BACKOFF_SEC = 60

SYSTEM_PROMPT = f"""あなたは会話や会議の記録係です。発言の文字起こしが少しずつ届きます。
届くたびに、記録のうち変わる見出しだけを書き直してください。

場面はゼミ・授業・打ち合わせ・家族や友人との会話などいろいろです。
最初に渡す「会議の名前」と話の中身から場面を考え、それに合った書き方にしてください。
会議らしくない場面（雑談など）では、決まったこと・やることを無理に作らない。

記録の見出し: {" / ".join(SECTIONS)}

出力の形式（これ以外は一切書かない。前置き・説明・コードブロックは不要）:
@@ 見出し     その見出しの中身を丸ごと置き換える
@@+ 見出し    その見出しの末尾に書き足す
変える所がなければ「@@ 変更なし」とだけ書く。

書き方:
- 中身は「- 」で始まる箇条書き。1項目1行、40字くらいまで。
- 話したことは原則 @@+ で書き足す。直前の項目を直すときだけ @@ で全体を置き換える。
- @@+ で書き足すのは、今回届いた発言の内容だけ。前に書いた項目は繰り返さない。
- 書くことがない見出しには触れない（「なし」「変更なし」と中身に書かない）。
- 要点は全体を3行以内で。話題が変わったときだけ更新する。
- 決まったこと・やること・次に向けては、はっきり決まったこと・頼まれたこと・約束したことだけ書く。
- やることは「誰が・何を・いつまでに」。わからない部分は「（未定）」と書く。
- 保留・疑問には、結論が出なかった話と、答えが出ていない疑問を書く。
- 文字起こしには聞き間違いがある。文脈から明らかな誤りは直してよいが、話されていないことは書かない。
- 人の名前は、文字起こしで呼ばれていてわかるときだけ書く。
- 相づちや雑談だけなら「@@ 変更なし」。
"""


def format_lines(segments: list[Segment], tz: ZoneInfo) -> str:
    """発言を「[時:分:秒] 本文」の行にする. 空の切れ端は飛ばす."""
    return "\n".join(
        f"[{datetime.fromtimestamp(s.seg_id / 1000, tz):%H:%M:%S}] {s.text}" for s in segments if s.text
    )


def build_message(lines: str, draft: dict[str, str], *, fresh: bool, title: str = "") -> str:
    """Haikuに渡す1回分の本文. 起動し直した直後は、会議の名前と今の記録も渡して続きから書かせる."""
    if not fresh:
        return f"新しい発言（この内容だけを反映する）:\n{lines}"
    current = "\n".join(
        f"@@ {s}\n{draft[s].strip()}" for s in SECTIONS if draft.get(s, "").strip()
    ) or "（まだ何もない）"
    return (
        f"会議の名前: {title or '（なし）'}\n\n"
        f"これまでの記録:\n{current}\n\n新しい発言（この内容だけを反映する）:\n{lines}"
    )


class LiveEditor:
    """1つの会議の議事録を、録音中に書き進める."""

    def __init__(
        self,
        meeting_id: int,
        *,
        hub: MinutesHub,
        open_db: Callable[[], AbstractContextManager[Connection]],
        tz: ZoneInfo,
        session: ClaudeStreamSession | None = None,
    ) -> None:
        self.meeting_id = meeting_id
        self._hub = hub
        self._open_db = open_db
        self._tz = tz
        self._session = session or ClaudeStreamSession(model=HAIKU_MODEL, system_prompt=SYSTEM_PROMPT)
        self._wake = threading.Event()
        self._stopping = False
        self._last_activity = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"minutes-editor-{meeting_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify(self) -> None:
        """新しい文字起こしが届いたことを知らせる."""
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def _run(self) -> None:
        backoff = 5.0
        while not self._stopping:
            woke = self._wake.wait(timeout=60)
            if self._stopping:
                break
            if not woke:
                if time.monotonic() - self._last_activity > IDLE_CLOSE_SEC:
                    self._session.close()
                continue
            self._wake.clear()
            time.sleep(DEBOUNCE_SEC)
            try:
                self.run_turn()
                backoff = 5.0
            except ClaudeError as exc:
                log.warning("live minutes editing failed for meeting %s: %s", self.meeting_id, exc)
                self._hub.publish(self.meeting_id, {
                    "type": "notice",
                    "message": f"AIの編集が止まっています（{int(backoff)}秒後にやり直します）。文字起こしと録音は続いています。{exc}",
                })
                self._session.close()
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
                self._wake.set()  # 反映できなかった発言をもう一度渡す
            except Exception:  # noqa: BLE001 - このスレッドが落ちると以後の編集が止まる
                log.exception("unexpected error in live editor for meeting %s", self.meeting_id)
                time.sleep(backoff)
        self._session.close()

    def run_turn(self) -> None:
        """未反映の発言をHaikuに渡し、返ってきた編集を議事録に当てる."""
        with self._open_db() as conn:
            segments = undrafted_segments(conn, self.meeting_id)
            meeting = get_meeting(conn, self.meeting_id)
        if not segments or meeting is None:
            return
        self._last_activity = time.monotonic()

        if self._session.turns >= ROTATE_AFTER_TURNS:
            self._session.close()
        fresh = not self._session.alive
        lines = format_lines(segments, self._tz)
        base = normalize_draft(meeting.draft)
        if lines:
            text = self._ask(build_message(lines, base, fresh=fresh, title=meeting.title), base)
            draft = apply_ops(base, parse_ops(text))
        else:
            draft = base  # 無音だけの切れ端. Haikuに渡すものがない

        # 下書きの保存と「反映済み」の印は一緒に書く. 片方だけだと、次の回に同じ発言を二重に書き足す.
        with self._open_db() as conn, transaction(conn):
            save_draft(conn, self.meeting_id, draft)
            mark_drafted(conn, self.meeting_id, [s.seg_id for s in segments])
        self._hub.publish(self.meeting_id, {"type": "draft", "draft": draft, "editing": None})

    def _ask(self, message: str, base: dict[str, str]) -> str:
        """Haikuに頼み、書いている途中の議事録を画面に流す."""
        received: list[str] = []
        last_publish = 0.0

        def on_delta(chunk: str) -> None:
            nonlocal last_publish
            received.append(chunk)
            now = time.monotonic()
            if now - last_publish < PUBLISH_INTERVAL_SEC:
                return
            last_publish = now
            ops = parse_ops("".join(received), final=False)
            section = editing_section(ops)
            self._hub.publish(self.meeting_id, {
                "type": "draft",
                "draft": apply_ops(base, ops),
                "editing": section,
                "editing_label": section_label(section) if section else None,
            })

        return self._session.ask(message, on_delta=on_delta, timeout_sec=TURN_TIMEOUT_SEC)
