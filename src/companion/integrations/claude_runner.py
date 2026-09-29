"""claude -p のヘッドレス実行.

既存の discord-claude-bot で運用実績のある呼び出し方を踏襲する。
- プロンプトは引数ではなく stdin で渡す（長文でもコマンドライン長制限に引っかからない）
- --setting-sources project でユーザーレベルのルール読み込みを省き、トークンを節約する
- JSONで受け取り、result フィールドを取り出す
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from companion.config import get_config

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"


class ClaudeError(Exception):
    """claude -p の実行に失敗したときに送出する."""


@dataclass(frozen=True)
class ClaudeResult:
    """claude -p の実行結果."""

    text: str
    session_id: str | None


# claude -p は認証切れでもJSON本体を返し、result にエラー文が入る（rc=1、stderrは空）。
# これを普通の応答として扱うと「要約だけが黙って作られない」状態になり、
# ログを見ても原因がわからない。2026-08-14にOAuthトークンの期限切れで実際に起きた。
# ここで必ず失敗として扱う。
AUTH_ERROR_MARKERS = ("authenticate", "oauth", "401", "/login", "api key")

AUTH_ERROR_MESSAGE = (
    "Claudeの認証が切れています。"
    "ターミナルで claude を起動し、/login でログインし直してください。"
)


def describe_error(text: str) -> str:
    """claude が返したエラー文を、対処のわかるメッセージにする."""
    if any(marker in text.lower() for marker in AUTH_ERROR_MARKERS):
        return AUTH_ERROR_MESSAGE
    return f"Claudeがエラーを返しました: {text.strip()[:200]}"


def result_from_payload(payload: dict) -> ClaudeResult:
    """claude -p のJSONを結果にする. エラーを伝えていれば ClaudeError を投げる."""
    text = payload.get("result") or ""
    if payload.get("is_error"):
        log.error("claude returned an error: %s", text[:500])
        raise ClaudeError(describe_error(text))
    return ClaudeResult(text=text, session_id=payload.get("session_id"))


def find_claude_binary() -> str | None:
    """PATH から claude 実行ファイルを探す. 見つからなければ None."""
    return shutil.which("claude")


def run_claude(
    prompt: str,
    *,
    system_prompt: str | None = None,
    allowed_tools: str = "",
    model: str = DEFAULT_MODEL,
    timeout_sec: int | None = None,
) -> ClaudeResult:
    """claude -p を同期実行して結果を返す.

    Args:
        prompt: Claudeに渡す本文. stdin 経由で送る.
        system_prompt: 役割を指定する追加のシステムプロンプト.
        allowed_tools: 許可するツール（例: "WebSearch,WebFetch"）. 空なら全て拒否.
        model: 使用するモデルID.
        timeout_sec: タイムアウト秒. None なら設定値を使う.

    Raises:
        ClaudeError: バイナリが見つからない、タイムアウト、実行失敗のいずれか.
    """
    binary = find_claude_binary()
    if binary is None:
        raise ClaudeError(
            "claude コマンドが見つかりません。Claude Code がインストールされ、PATHに含まれているか確認してください。"
        )

    config = get_config()
    timeout = timeout_sec if timeout_sec is not None else config.claude_timeout_sec

    args = [binary, "-p", "--output-format", "json", "--model", model, "--setting-sources", "project"]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]

    try:
        completed = subprocess.run(  # noqa: S603 - 引数は自前で組み立てており shell も使わない
            args,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"Claudeが{timeout}秒以内に応答しませんでした。") from exc

    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 and not stdout:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        log.error("claude failed (rc=%s): %s", completed.returncode, stderr[:500])
        raise ClaudeError(f"Claudeの実行に失敗しました (終了コード {completed.returncode}): {stderr[:200]}")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("unparseable claude output: %s", stdout[:500])
        raise ClaudeError("Claudeの応答を解釈できませんでした。") from exc

    return result_from_payload(payload)


def strip_markdown_headings(text: str) -> str:
    """行頭の見出し記号(#)を落とす.

    プレーンテキストとして表示する画面では「# 今週の傾向」のように記号が見えてしまう。
    プロンプトで禁じても付いてくることがあるので、表示前に必ずここを通す。
    箇条書きの「- 」は読みやすさに寄与するので残す。
    """
    lines = [line.lstrip("#").lstrip() if line.lstrip().startswith("#") else line
             for line in text.splitlines()]
    return "\n".join(lines)


def extract_json_block(text: str) -> dict | None:
    """Claudeの応答からJSONオブジェクトを取り出す.

    コードフェンスや前置きが混ざっていても拾えるよう、最初の { から最後の } までを試す。
    解釈できなければ None を返す（呼び出し側は原文だけ保存して縮退する）。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
