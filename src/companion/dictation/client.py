"""Webアプリの /dictate に録音を送り、文字を受け取る.

切れ端は数秒ずつなので、そのままだとWhisperが話の流れを知らずに漢字を当てる. 同じ音声入力のうちに
前の切れ端で返ってきた文を見出し（PREVIOUS_HEADER）に入れて送り、続きとして文字にしてもらう（2026-10-26）。
見出しには日本語をそのまま入れられないので、%エンコードする。本文（録音）は今までどおり.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

# 長い話（5分）でも、GPUのWhisperなら数十秒で返る. モデルを読み直す時間も見ておく.
TIMEOUT_SEC = 120
PREVIOUS_HEADER = "X-Dictation-Previous"
# Whisperが前の文として読むのは後ろの200文字まで（integrations/whisper.py）. それより長く送っても使われない.
MAX_PREVIOUS_CHARS = 200


class DictationError(Exception):
    """文字にできなかった. 画面に出す1行を持つ."""


def extend_context(previous: str, text: str) -> str:
    """前の文に、返ってきた文をつなげる. 長くなったら後ろの MAX_PREVIOUS_CHARS 文字だけ残す."""
    return (previous + text)[-MAX_PREVIOUS_CHARS:]


def transcribe(wav: bytes, base_url: str, *, previous: str = "", client: httpx.Client | None = None) -> str:
    """録音を送って文字を受け取る. 何も聞き取れなければ空の文字.

    previous には同じ音声入力の前の切れ端の文を渡す（後ろの MAX_PREVIOUS_CHARS 文字だけ送る）. 空なら送らない.

    Raises:
        DictationError: Webアプリが動いていない・断られた.
    """
    headers = {"Content-Type": "audio/wav"}
    if previous:
        headers[PREVIOUS_HEADER] = quote(previous[-MAX_PREVIOUS_CHARS:])
    owner = client is None
    http = client or httpx.Client(timeout=TIMEOUT_SEC)
    try:
        response = http.post(f"{base_url}/dictate", content=wav, headers=headers)
    except httpx.HTTPError as exc:
        raise DictationError("Webアプリにつながりません。PCのdaily-companionが動いているか確かめてください。") from exc
    finally:
        if owner:
            http.close()
    if response.status_code != 200:
        raise DictationError(response.text.strip() or f"文字にできませんでした（{response.status_code}）。")
    return response.text.strip()
