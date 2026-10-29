"""音声入力ツールの本体. キーの合図・録音・送信・貼り付けと、状態を出す小窓.

    待ち ─右Ctrl→ 録音中 ─右Ctrl→ 残りを入れている → 待ちへ
                     └─Esc→ やめて待ちへ（入れた分は残る）

録っている間も、話の切れ目（息つぎ）ごとに切った分を順に /dictate へ送り、返ってきた文字から
カーソルの位置へ入れていく（2026-10-25. それまでは止めてから全部を送っていて、止めるまで何も出ず遅かった）。
入れた文字は後から直さないので、切れ端の終わりの「。」だけは次の切れ端まで持っておき、間でしっかり黙っていたときだけ
付ける（dictation/period.py）。止めたら持っていた「。」を最後に入れ、やめたら捨てる。

キーの見張り（pynput）・録音（sounddevice）・送信はそれぞれ別のスレッドで動くので、出来事は queue に入れ、
画面のスレッド（tkinter）がそれを順に処理する。tkinter は画面のスレッドからしか触らない。

文字は「クリップボードに入れて Ctrl+V」で入れる。1回の音声入力で入れ終わったら、元のクリップボードの文字を戻す
（画像など文字以外が入っていたときは戻せない）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk

import numpy as np

from companion.dictation import windows
from companion.dictation.client import DictationError, extend_context, transcribe
from companion.dictation.period import PeriodHolder
from companion.dictation.recorder import Recorder, to_wav
from companion.dictation.tap import TapDetector

log = logging.getLogger(__name__)

POLL_MS = 50
# 1回の音声入力の上限. 切れ端ごとに送るので長くてもよいが、止め忘れでマイクをつかみ続けないように.
MAX_SECONDS = 30 * 60
NOTICE_MS = 3500
# 入力先の窓を戻してから Ctrl+V を送るまでの間（戻るのを待つ）.
FOCUS_SETTLE_MS = 80
# 続けて入れるとき、前の Ctrl+V を貼り付け先が読み終わるのを待つ間（先にクリップボードを替えると前の分が化ける）.
PASTE_GAP_MS = 350
# 最後の Ctrl+V のあと、貼り付け先が読み終わるのを待ってから元のクリップボードに戻す（重いアプリでも間に合うよう長めに）.
RESTORE_DELAY_MS = 1500
# 自分で送った Ctrl+V を、合図と取り違えないよう無視する時間.
PASTE_QUIET_SEC = 0.5
TEXT = "#23221E"
LINE = "#D6D3D0"
DANGER = "#E01E5A"
FONT = ("Yu Gothic UI", 12, "bold")


class DictationApp:
    """1つだけ動かす. run() で画面のループに入る."""

    def __init__(self, base_url: str, key, key_label: str) -> None:
        self._base_url = base_url
        self._key_label = key_label
        self._tap = TapDetector(key)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        # (どの音声入力の分か, WAV, その切れ端の前に黙っていたミリ秒)
        self._outbox: queue.Queue[tuple[int, bytes, int]] = queue.Queue()
        self._recorder = Recorder()
        self._state = "idle"
        self._target = 0
        self._notice_id = 0
        self._quiet_until = 0.0
        # 1回の音声入力ごとの番号. やめたあとに届いた文字を入れないため（送る係のスレッドも読む）.
        self._session = 0
        self._waiting = 0  # 送ってまだ文字が返っていない切れ端の数
        self._to_paste: list[str] = []
        self._pasting = False
        self._pasted = 0
        self._failed = 0
        self._saved_clipboard: str | None = None
        self._restore_after: str | None = None
        # 送る係だけが使う: (どの音声入力の分か, その音声入力でここまでに返ってきた文の後ろの部分).
        self._context: tuple[int, str] = (0, "")
        self._periods = PeriodHolder()  # まだ入れていない終わりの「。」（画面のスレッドだけが使う）
        self._root = tk.Tk()
        self._root.withdraw()
        self._window, self._label, self._hwnd = self._build_window()
        threading.Thread(target=self._sender, name="dictation-sender", daemon=True).start()

    # ---- 小窓 ----

    def _build_window(self):
        window = tk.Toplevel(self._root, bg=LINE)
        window.overrideredirect(True)
        label = tk.Label(window, text="", font=FONT, fg=TEXT, bg="#FFFFFF", padx=14, pady=8)
        label.pack(padx=1, pady=1)  # 1pxの枠線（背景の線の色が見える）
        hwnd = windows.make_passive(window)
        windows.hide(hwnd)
        return window, label, hwnd

    def _show(self, text: str, *, danger: bool = False) -> None:
        self._label.configure(text=text, fg=DANGER if danger else TEXT)
        self._window.update_idletasks()
        width, height = self._window.winfo_reqwidth(), self._window.winfo_reqheight()
        x = (self._window.winfo_screenwidth() - width) // 2
        y = self._window.winfo_screenheight() - height - 90  # タスクバーの少し上
        self._window.geometry(f"+{x}+{y}")
        windows.show_passive(self._hwnd)

    def _hide(self) -> None:
        windows.hide(self._hwnd)

    def _notice(self, text: str, *, danger: bool = False) -> None:
        """しばらく出して消える知らせ. 次の録音が始まっていたり、新しい知らせが出ていたら消さない."""
        self._notice_id += 1
        mine = self._notice_id
        self._show(text, danger=danger)
        self._root.after(NOTICE_MS, lambda: self._hide() if self._state == "idle" and self._notice_id == mine else None)

    # ---- キー（別スレッドから呼ばれる） ----

    def on_press(self, key) -> None:
        if time.monotonic() < self._quiet_until:
            return  # 自分で送った Ctrl+V
        self._tap.press(key)
        if getattr(key, "name", None) == "esc":
            self._events.put(("cancel", None))

    def on_release(self, key) -> None:
        if time.monotonic() < self._quiet_until:
            return
        if self._tap.release(key):
            self._events.put(("tap", None))

    # ---- 画面のスレッド ----

    def run(self) -> None:
        self._root.after(POLL_MS, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        try:
            self._drain()
        except Exception:  # noqa: BLE001 - ここで落ちると以後の合図を受けなくなる. 待ちに戻して続ける
            log.exception("unexpected error in dictation tool")
            self._reset()
            self._notice("うまくいきませんでした。もう一度押してください。", danger=True)
        finally:
            self._root.after(POLL_MS, self._poll)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self._state == "recording":
            seconds = self._recorder.seconds
            if seconds >= MAX_SECONDS:
                self._handle("tap", None)
            else:
                self._show(self._recording_label(int(seconds)))

    def _recording_label(self, seconds: int) -> str:
        busy = f"　文字にしています（{self._waiting}）" if self._waiting else ""
        return f"● 録音中 {seconds // 60}:{seconds % 60:02d}{busy}　{self._key_label}で止める／Escでやめる"

    def _reset(self) -> None:
        """思わぬ失敗のあと、待ちに戻す. マイクをつかんだままにしない."""
        self._state = "idle"
        self._session += 1
        self._waiting, self._to_paste, self._pasting = 0, [], False
        self._periods = PeriodHolder()
        try:
            self._recorder.stop()
        except Exception:  # noqa: BLE001 - 止められなくても待ちには戻る
            log.exception("could not stop the recorder")
        self._restore_clipboard()

    def _handle(self, kind: str, payload: object) -> None:
        if kind == "tap" and self._state == "idle":
            self._start()
        elif kind == "tap" and self._state == "recording":
            self._finish()
        elif kind == "cancel" and self._state in ("recording", "finishing"):
            self._cancel()
        elif kind == "queued" and payload == self._session:
            self._waiting += 1
        elif kind == "piece":
            self._receive(*payload)  # type: ignore[misc]

    # ---- 録る ----

    def _start(self) -> None:
        if self._restore_after is not None:
            # 前の音声入力のクリップボードをまだ戻していない. 今の中身（前に入れた文字）を控えにしないよう先に戻す.
            self._root.after_cancel(self._restore_after)
            self._restore_clipboard()
        self._target = windows.foreground_window()
        self._session += 1
        self._waiting, self._to_paste, self._pasted, self._failed = 0, [], 0, 0
        self._periods = PeriodHolder()
        self._saved_clipboard = self._clipboard_text()
        session = self._session
        try:
            self._recorder.start(on_segment=lambda pcm, pause_ms: self._on_segment(session, pcm, pause_ms))
        except Exception as exc:  # noqa: BLE001 - マイクまわりは失敗の型がまちまち
            log.exception("could not start recording")
            self._notice(f"マイクが使えません: {exc}", danger=True)
            return
        self._state = "recording"
        log.info("recording started")
        self._show(self._recording_label(0))

    def _on_segment(self, session: int, pcm: np.ndarray, pause_ms: int) -> None:
        """録音のスレッドから呼ばれる. 数を数えるのは画面のスレッドに任せ、送る係に渡す."""
        self._events.put(("queued", session))
        self._outbox.put((session, to_wav(pcm), pause_ms))

    def _finish(self) -> None:
        self._recorder.stop()  # 最後の切れ端もここで送る係に渡る
        self._state = "finishing"
        log.info("recording stopped")
        # 止めた直後に積まれた知らせ（最後の切れ端の「送った」）を先に数える. 数え漏れで早く終わったことにしないため.
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self._maybe_done()

    def _cancel(self) -> None:
        self._recorder.stop()
        self._session += 1
        self._waiting, self._to_paste = 0, []
        self._periods = PeriodHolder()  # やめたら、持っていた「。」は入れない
        self._state = "idle"
        log.info("dictation cancelled after %d pieces", self._pasted)
        self._notice("やめました（入れた分は残っています）" if self._pasted else "やめました")
        self._schedule_restore()

    def _receive(self, session: int, text: str, error: str | None, pause_ms: int) -> None:
        """切れ端1つ分の文字が返ってきた. 終わりの「。」は持っておき、前に持っていた「。」を付けるか決める."""
        if session != self._session:
            return  # やめた音声入力の分
        self._waiting -= 1
        if error:
            self._failed += 1
            log.warning("a piece could not be transcribed: %s", error)
        # 文字にできなかった分も、黙っていた長さは数える（何も聞き取れなかったのと同じ扱い）.
        self._periods, pasted = self._periods.take("" if error else text, pause_ms)
        if pasted:
            self._to_paste.append(pasted)
            self._paste_next()
        self._maybe_done()

    def _maybe_done(self) -> None:
        """止めたあと、全部の切れ端が文字になって入れ終わったら、持っていた「。」を入れてから待ちに戻る."""
        if self._state != "finishing":
            return
        if not (self._waiting or self._to_paste or self._pasting):
            self._periods, last = self._periods.finish()
            if last:
                self._to_paste.append(last)
                self._paste_next()  # 入れ終わると、もう一度ここに来て待ちに戻る
                return
        if self._waiting or self._to_paste or self._pasting:
            self._show(f"文字にしています…（あと{self._waiting + len(self._to_paste)}）")
            return
        self._state = "idle"
        if self._failed:
            self._notice(f"文字にできなかった部分が{self._failed}か所あります", danger=True)
        elif not self._pasted:
            self._notice("聞き取れませんでした")
        else:
            self._hide()
        self._schedule_restore()

    # ---- 送る（別スレッド） ----

    def _sender(self) -> None:
        """切れ端を1つずつ順に送る. 順に送るので、文字も話した順に返る."""
        while True:
            session, wav, pause_ms = self._outbox.get()
            if session != self._session:
                continue  # やめた音声入力の分は送らない
            self._send(session, wav, pause_ms)

    def _send(self, session: int, wav: bytes, pause_ms: int) -> None:
        """切れ端1つを、同じ音声入力の前の文と一緒に送り、返ってきた文字を画面のスレッドに渡す.

        pause_ms（その切れ端の前に黙っていた長さ）は、そのまま画面のスレッドに渡す（「。」を付けるかに使う）.

        前の文は音声入力ごとに始め直す（前の音声入力の話を続きとして読ませない）。文字にできなかった分は足さない.
        """
        owner, previous = self._context
        if owner != session:
            previous = ""
        started = time.monotonic()
        try:
            text, error = transcribe(wav, self._base_url, previous=previous), None
        except DictationError as exc:
            text, error = "", str(exc)
        except Exception:  # noqa: BLE001 - 知らせずに終わると「文字にしています…」のまま固まる
            log.exception("dictation request failed")
            text, error = "", "文字にできませんでした。"
        self._context = (session, extend_context(previous, text))
        log.info("piece took %.1fs (%d chars)", time.monotonic() - started, len(text))
        self._events.put(("piece", (session, text, error, pause_ms)))

    # ---- 入れる ----

    def _paste_next(self) -> None:
        """入れる順番待ちの先頭を入れる. 入れている途中なら、終わってから次を入れる."""
        if self._pasting or not self._to_paste:
            return
        self._pasting = True
        text = self._to_paste.pop(0)
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        self._root.update()
        windows.bring_back(self._target)
        self._root.after(FOCUS_SETTLE_MS, self._press)

    def _press(self) -> None:
        self._quiet_until = time.monotonic() + PASTE_QUIET_SEC
        _press_paste()
        self._pasted += 1
        self._root.after(PASTE_GAP_MS, self._pasted_one)

    def _pasted_one(self) -> None:
        self._pasting = False
        self._paste_next()
        self._maybe_done()

    def _clipboard_text(self) -> str | None:
        try:
            return self._root.clipboard_get()
        except tk.TclError:  # 空、または画像など文字以外
            return None

    def _schedule_restore(self) -> None:
        if self._pasted and self._saved_clipboard is not None:
            self._restore_after = self._root.after(RESTORE_DELAY_MS, self._restore_clipboard)

    def _restore_clipboard(self) -> None:
        self._restore_after = None
        text, self._saved_clipboard = self._saved_clipboard, None
        if text is None:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        self._root.update()


def _press_paste() -> None:
    from pynput.keyboard import Controller, Key

    keyboard = Controller()
    with keyboard.pressed(Key.ctrl):
        keyboard.tap("v")
