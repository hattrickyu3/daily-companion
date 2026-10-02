"""起動したままの claude プロセスと、続けてやり取りする.

claude -p は呼ぶたびに起動で数秒かかる。録音中は数秒おきに頼みたいので、
--input-format stream-json で1つ起動したままにし、標準入力からメッセージを足していく。
応答は --include-partial-messages で1文字ずつ受け取り、書いている途中を画面に出す。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
from collections.abc import Callable

from companion.integrations.claude_runner import ClaudeError, describe_error, find_claude_binary

log = logging.getLogger(__name__)

STDERR_KEEP_LINES = 20
# 考える段階（thinking）を切る. 入れたままだと書き始めまでに約10秒、切ると約0.6秒（2026-10-02に計測）.
# 議事録の書き足しは考え込む必要のない作業なので、速さを取る。
NO_THINKING_ENV = {"MAX_THINKING_TOKENS": "0"}


class ClaudeStreamSession:
    """claude プロセス1つ. ask() は同時に呼ばない前提（呼び出し側で順番に呼ぶ）."""

    def __init__(self, *, model: str, system_prompt: str) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.turns = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_tail: list[str] = []

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start(self) -> None:
        binary = find_claude_binary()
        if binary is None:
            raise ClaudeError("claude コマンドが見つかりません。")
        args = [
            binary, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--model", self.model,
            "--setting-sources", "project",
            "--tools", "",
            "--system-prompt", self.system_prompt,
        ]
        self._process = subprocess.Popen(  # noqa: S603 - 引数は自前で組み立てており shell も使わない
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, **NO_THINKING_ENV},
        )
        self._lines = queue.Queue()
        self.turns = 0
        threading.Thread(target=self._pump_stdout, args=(self._process, self._lines), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(self._process,), daemon=True).start()

    @staticmethod
    def _pump_stdout(process: subprocess.Popen[bytes], lines: queue.Queue[bytes | None]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)  # 終了の合図

    def _pump_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._stderr_tail = [*self._stderr_tail, line.decode("utf-8", "replace").rstrip()][-STDERR_KEEP_LINES:]

    def ask(self, text: str, *, on_delta: Callable[[str], None], timeout_sec: float) -> str:
        """メッセージを送り、応答の全文を返す. 書かれた分は on_delta に順に渡す."""
        if not self.alive:
            self._start()
        assert self._process is not None and self._process.stdin is not None

        message = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self._process.stdin.flush()
        except OSError as exc:
            self.close()
            raise ClaudeError(f"Claudeにメッセージを送れませんでした: {exc}") from exc
        self.turns += 1

        while True:
            try:
                line = self._lines.get(timeout=timeout_sec)
            except queue.Empty as exc:
                self.close()
                raise ClaudeError(f"Claudeが{int(timeout_sec)}秒以内に応答しませんでした。") from exc
            if line is None:
                tail = " / ".join(self._stderr_tail[-3:])
                self.close()
                raise ClaudeError(f"Claudeが途中で終了しました。{tail}")
            result = self._handle_line(line, on_delta)
            if result is not None:
                return result

    @staticmethod
    def _handle_line(line: bytes, on_delta: Callable[[str], None]) -> str | None:
        """1行を処理する. 応答が終わったら全文を、まだ続くなら None を返す."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event", {})
            delta = inner.get("delta", {}) if inner.get("type") == "content_block_delta" else {}
            if delta.get("type") == "text_delta":
                on_delta(delta.get("text", ""))
            return None
        if kind == "result":
            text = event.get("result") or ""
            if event.get("is_error"):
                raise ClaudeError(describe_error(text))
            return text
        return None

    def close(self) -> None:
        """プロセスを終わらせる. 次の ask() で新しく起動し直す."""
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
