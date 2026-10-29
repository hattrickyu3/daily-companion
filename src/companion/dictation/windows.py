"""Windowsの窓まわり（ctypes）. 録り始めた時の窓を覚えて戻すことと、状態の小窓が入力先を奪わないこと."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080  # タスクバーに出さない
WS_EX_NOACTIVATE = 0x08000000  # 押しても表に出ない（入力先を奪わない）
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4


def _load_user32():
    """自分専用の user32. 64bitの窓の番号を正しく受け渡すよう、引数と戻り値の型を決めておく."""
    if not hasattr(ctypes, "WinDLL"):
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return user32


_user32 = _load_user32()


def foreground_window() -> int:
    """いま入力を受けている窓. 取れなければ 0."""
    return int(_user32.GetForegroundWindow() or 0) if _user32 else 0


def bring_back(hwnd: int) -> None:
    """録り始めたときの窓に入力先を戻す. Windowsに断られることもあるが、そのときはそのまま進める."""
    if _user32 and hwnd and int(_user32.GetForegroundWindow() or 0) != hwnd:
        _user32.SetForegroundWindow(hwnd)


def make_passive(tk_window) -> int:
    """tkinter の窓を「いちばん手前に出るが入力先を奪わない」窓にして、その窓の番号を返す."""
    tk_window.attributes("-topmost", True)  # 並び順を変えるのは tkinter に任せる（拡張スタイルだけでは手前に来ない）
    if not _user32:
        return 0
    tk_window.update_idletasks()
    hwnd = int(_user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id())
    style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    return hwnd


def show_passive(hwnd: int) -> None:
    if _user32 and hwnd:
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def hide(hwnd: int) -> None:
    if _user32 and hwnd:
        _user32.ShowWindow(hwnd, SW_HIDE)
