"""音声入力ツールの起動. ログインしたときにタスクスケジューラが pythonw で動かす（画面に黒い窓を出さない）.

    .venv\\Scripts\\pythonw.exe -m companion.dictation

使うキーは .env の DICTATION_KEY（pynput のキーの名前. 既定は ctrl_r = 右Ctrl）。
"""

from __future__ import annotations

import logging
import os
import socket
import sys

from dotenv import load_dotenv

from companion.config import LOG_DIR, PROJECT_ROOT, get_config

DEFAULT_KEY = "ctrl_r"
KEY_LABELS = {"ctrl_r": "右Ctrl", "alt_r": "右Alt", "shift_r": "右Shift", "pause": "Pause", "scroll_lock": "ScrollLock"}
# 2つ目を起動したときに気づくための、このPCの中だけの番号（受け付けはしない）.
SINGLE_INSTANCE_PORT = 8799

log = logging.getLogger("companion.dictation")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "dictation.log", level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _hold_single_instance() -> socket.socket | None:
    """2つ同時に動くと1回の合図で2回貼り付くので、すでに動いていれば None."""
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        guard.close()
        return None
    return guard


def _resolve_key(name: str):
    from pynput.keyboard import Key, KeyCode

    if hasattr(Key, name):
        return getattr(Key, name)
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise SystemExit(f"DICTATION_KEY のキーの名前がわかりません: {name}")


def main() -> int:
    _setup_logging()
    guard = _hold_single_instance()
    if guard is None:
        log.info("dictation tool is already running")
        return 0
    load_dotenv(PROJECT_ROOT / ".env")
    key_name = os.getenv("DICTATION_KEY", "").strip() or DEFAULT_KEY
    base_url = f"http://127.0.0.1:{get_config().web_port}"

    from pynput import keyboard

    from companion.dictation.app import DictationApp

    app = DictationApp(base_url, _resolve_key(key_name), KEY_LABELS.get(key_name, key_name))
    listener = keyboard.Listener(on_press=app.on_press, on_release=app.on_release)
    listener.start()
    log.info("dictation tool started (key=%s, url=%s)", key_name, base_url)
    try:
        app.run()
    finally:
        listener.stop()
        guard.close()
    return 0


def run() -> int:
    """pythonw には画面の出力が無いので、起動に失敗した理由もログに残す."""
    try:
        return main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            log.error("dictation tool could not start: %s", exc.code)
        raise
    except Exception:
        log.exception("dictation tool stopped unexpectedly")
        return 1


if __name__ == "__main__":
    sys.exit(run())
