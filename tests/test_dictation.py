"""PCの音声入力ツールのうち、キーボード・マイク・画面を使わずに確かめられる部分のテスト."""

from __future__ import annotations

import io
import wave
from urllib.parse import unquote

import httpx
import numpy as np
import pytest

from companion.dictation.client import (
    MAX_PREVIOUS_CHARS,
    PREVIOUS_HEADER,
    DictationError,
    extend_context,
    transcribe,
)
from companion.dictation.period import SENTENCE_PAUSE_MS, PeriodHolder
from companion.dictation.recorder import SAMPLE_RATE, to_wav
from companion.dictation.tap import TAP_MAX_SEC, TapDetector


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_右Ctrlを1回押して離すと合図になる() -> None:
    clock = FakeClock()
    tap = TapDetector("ctrl_r", clock=clock)

    tap.press("ctrl_r")
    clock.now = 0.1

    assert tap.release("ctrl_r") is True


def test_ほかのキーと一緒に押したときは合図にしない() -> None:
    tap = TapDetector("ctrl_r", clock=FakeClock())

    tap.press("ctrl_r")
    tap.press("c")  # Ctrl+C
    tap.release("c")

    assert tap.release("ctrl_r") is False
    # 次に1回押したときは、ふつうに合図になる
    tap.press("ctrl_r")
    assert tap.release("ctrl_r") is True


def test_長く押したままのときや押したままの繰り返しは合図にしない() -> None:
    clock = FakeClock()
    tap = TapDetector("ctrl_r", clock=clock)

    tap.press("ctrl_r")
    clock.now = 0.3
    tap.press("ctrl_r")  # 押したままの自動の繰り返し. 押し始めの時刻は変えない
    clock.now = TAP_MAX_SEC + 0.1

    assert tap.release("ctrl_r") is False
    assert tap.release("ctrl_r") is False  # 押していないのに離した、は数えない


def test_録った音声は16kHzモノラルのWAVになる() -> None:
    pcm = np.zeros(SAMPLE_RATE, dtype=np.int16)

    with wave.open(io.BytesIO(to_wav(pcm))) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getnframes()) == (1, SAMPLE_RATE, 2, SAMPLE_RATE)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_録音を送って文字を受け取る() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="明日の予定です\n")

    text = transcribe(b"RIFF....", "http://127.0.0.1:8765", client=_client(handler))

    assert text == "明日の予定です"
    assert str(seen[0].url) == "http://127.0.0.1:8765/dictate"
    assert seen[0].headers["content-type"] == "audio/wav"
    assert PREVIOUS_HEADER not in seen[0].headers  # 1つ目の切れ端は前の文が無い


def test_前の切れ端の文を見出しに入れて送る_長ければ後ろだけ() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="続き")

    transcribe(b"x", "http://x", previous="形態素解析の話です。", client=_client(handler))
    transcribe(b"x", "http://x", previous="あ" * 300 + "最後", client=_client(handler))

    assert unquote(seen[0].headers[PREVIOUS_HEADER]) == "形態素解析の話です。"
    sent = unquote(seen[1].headers[PREVIOUS_HEADER])
    assert len(sent) == MAX_PREVIOUS_CHARS and sent.endswith("最後")


@pytest.mark.parametrize(
    ("previous", "text", "expected"),
    [
        ("", "一つ目。", "一つ目。"),
        ("一つ目。", "二つ目。", "一つ目。二つ目。"),
        ("一つ目。", "", "一つ目。"),
    ],
)
def test_前の文に返ってきた文をつなげる(previous: str, text: str, expected: str) -> None:
    assert extend_context(previous, text) == expected


def test_つなげた前の文は上限の長さで後ろだけ残す() -> None:
    joined = extend_context("あ" * MAX_PREVIOUS_CHARS, "最後")

    assert len(joined) == MAX_PREVIOUS_CHARS and joined.endswith("最後")


def test_断られたら理由をそのまま出しつながらなければ案内する() -> None:
    refused = _client(lambda request: httpx.Response(400, text="送れませんでした: 音声が空です。"))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(DictationError, match="音声が空です"):
        transcribe(b"x", "http://127.0.0.1:8765", client=refused)
    with pytest.raises(DictationError, match="Webアプリにつながりません"):
        transcribe(b"x", "http://127.0.0.1:8765", client=_client(down))


# ---- 話の切れ目で切る ----

from companion.dictation.segmenter import MAX_SEGMENT_MS, Segmenter  # noqa: E402


def _tone(seconds: float, volume: float = 0.3) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * volume * 32767).astype(np.int16)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)


def test_話して少し黙ると切れ端になる() -> None:
    pieces: list[np.ndarray] = []
    segmenter = Segmenter(lambda pcm, _pause_ms: pieces.append(pcm))

    segmenter.push(_silence(2.0))  # 話す前の無音は送らない
    segmenter.push(_tone(1.5))
    segmenter.push(_silence(0.8))
    segmenter.push(_tone(1.2))

    assert len(pieces) == 1
    assert 1.5 < len(pieces[0]) / SAMPLE_RATE < 2.8  # 話す前の無音はほとんど入らない
    segmenter.flush()
    assert len(pieces) == 2  # 止めたら残りも切れ端にする


def test_黙っているだけなら切れ端にしない() -> None:
    pieces: list[np.ndarray] = []
    segmenter = Segmenter(lambda pcm, _pause_ms: pieces.append(pcm))

    segmenter.push(_silence(20.0))
    segmenter.flush()

    assert pieces == []


def test_話し続けても長くなりすぎる前に切る() -> None:
    pieces: list[np.ndarray] = []
    segmenter = Segmenter(lambda pcm, _pause_ms: pieces.append(pcm))

    for _ in range(40):  # 小さな塊で届く（マイクと同じ）
        segmenter.push(_tone(1.0))

    assert len(pieces) >= 2
    assert all(len(p) / SAMPLE_RATE * 1000 <= MAX_SEGMENT_MS + 20 for p in pieces)


def test_切れ端ごとに前の切れ端から黙っていた長さを渡す() -> None:
    pauses: list[int] = []
    segmenter = Segmenter(lambda _pcm, pause_ms: pauses.append(pause_ms))

    segmenter.push(_silence(2.0))  # 話し始める前の無音は数えない
    segmenter.push(_tone(1.5))
    segmenter.push(_silence(0.8))
    segmenter.push(_tone(1.2))
    segmenter.push(_silence(1.5))
    segmenter.push(_tone(1.0))
    segmenter.flush()

    assert len(pauses) == 3
    assert pauses[0] == 0
    assert abs(pauses[1] - 800) <= 40
    assert abs(pauses[2] - 1500) <= 40


def test_長くなりすぎて切ったときは黙っていた長さが0になる() -> None:
    pauses: list[int] = []
    segmenter = Segmenter(lambda _pcm, pause_ms: pauses.append(pause_ms))

    for _ in range(40):
        segmenter.push(_tone(1.0))

    assert len(pauses) >= 2 and all(pause == 0 for pause in pauses)


# ---- 終わりの「。」を持っておく ----


def test_文の途中で切れたときは終わりの丸を付けず_止めたら最後に入れる() -> None:
    holder = PeriodHolder()

    holder, first = holder.take("今日は形態素解析の。", 0)
    holder, second = holder.take("話をします。", 300)
    holder, last = holder.finish()

    assert (first, second, last) == ("今日は形態素解析の", "話をします", "。")
    assert holder == PeriodHolder()


@pytest.mark.parametrize(
    ("pause_ms", "expected"),
    [
        (SENTENCE_PAUSE_MS, "。次の話です"),
        (SENTENCE_PAUSE_MS - 1, "次の話です"),
    ],
)
def test_しっかり黙っていたら持っていた丸を次の切れ端の前に付ける(pause_ms: int, expected: str) -> None:
    holder, _ = PeriodHolder().take("一つ目の話です。", 0)

    _, text = holder.take("次の話です", pause_ms)

    assert text == expected


def test_何も聞き取れなかった切れ端を挟んでも黙っていた長さを覚えている() -> None:
    holder, _ = PeriodHolder().take("一つ目。", 0)

    holder, nothing = holder.take("", SENTENCE_PAUSE_MS + 500)
    _, text = holder.take("二つ目", 200)

    assert nothing == ""
    assert text == "。二つ目"


@pytest.mark.parametrize("text", ["本当ですか？", "すごい！", "えーと、"])
def test_丸で終わらなければ持たずにそのまま入れる(text: str) -> None:
    holder, pasted = PeriodHolder().take(text, 0)

    _, later = holder.take("次", SENTENCE_PAUSE_MS * 2)

    assert pasted == text
    assert later == "次"


def test_持っていても元のものは変わらない() -> None:
    empty = PeriodHolder()

    held, _ = empty.take("一つ目。", 0)

    assert empty == PeriodHolder()
    assert held != empty


def test_黙っていた長さが負ならエラーにする() -> None:
    with pytest.raises(ValueError, match="0以上"):
        PeriodHolder().take("一つ目。", -1)


# ---- 切れ端ごとに入れていく流れ（画面・キー・マイクは偽物） ----


class FakeRoot:
    """tkinter の代わり. after はすぐ動かす. クリップボードの中身と貼り付けた文字を覚える."""

    def __init__(self, clipboard: str) -> None:
        self.clipboard = clipboard
        self.typed: list[str] = []

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, text: str) -> None:
        self.clipboard += text

    def clipboard_get(self) -> str:
        return self.clipboard

    def update(self) -> None:
        pass

    def after(self, _ms, fn) -> str:
        fn()
        return "after-id"

    def after_cancel(self, _id) -> None:
        pass


class FakeRecorder:
    def __init__(self) -> None:
        self.on_segment = None
        self.seconds = 0.0

    def start(self, on_segment=None) -> None:
        self.on_segment = on_segment

    def stop(self) -> None:
        return None


def _app(monkeypatch, clipboard: str = "元の文字"):
    import queue as queue_module

    from companion.dictation import app as app_module

    root = FakeRoot(clipboard)
    monkeypatch.setattr(app_module.windows, "foreground_window", lambda: 1)
    monkeypatch.setattr(app_module.windows, "bring_back", lambda hwnd: None)
    monkeypatch.setattr(app_module, "_press_paste", lambda: root.typed.append(root.clipboard))
    app = object.__new__(app_module.DictationApp)
    app.__dict__.update(
        _base_url="http://x", _key_label="右Ctrl", _events=queue_module.Queue(), _outbox=queue_module.Queue(),
        _recorder=FakeRecorder(), _state="idle", _target=0, _notice_id=0, _quiet_until=0.0, _session=0,
        _waiting=0, _to_paste=[], _pasting=False, _pasted=0, _failed=0, _saved_clipboard=None,
        _restore_after=None, _root=root, _context=(0, ""), _periods=PeriodHolder(),
    )
    shown: list[str] = []
    app._show = lambda text, danger=False: shown.append(text)
    app._notice = lambda text, danger=False: shown.append(text)
    app._hide = lambda: None
    return app, root, shown


def _drain(app) -> None:
    import queue as queue_module

    try:
        while True:
            app._handle(*app._events.get_nowait())
    except queue_module.Empty:
        pass


def test_話しながら切れ端ごとに順に入れて終わったらクリップボードを戻す(monkeypatch) -> None:
    app, root, _ = _app(monkeypatch)
    app._handle("tap", None)
    session = app._session

    app._recorder.on_segment(_tone(1.0), 0)
    app._recorder.on_segment(_tone(1.0), 0)
    _drain(app)
    app._events.put(("piece", (session, "一つ目、", None, 0)))
    _drain(app)
    assert root.typed == ["一つ目、"]  # 止める前から入る

    app._handle("tap", None)  # 止める
    assert app._state == "finishing"
    app._events.put(("piece", (session, "二つ目", None, 0)))
    _drain(app)

    assert root.typed == ["一つ目、", "二つ目"]
    assert app._state == "idle"
    assert root.clipboard == "元の文字"
    assert app._outbox.qsize() == 2


def test_やめたあとに返ってきた文字は入れない(monkeypatch) -> None:
    app, root, shown = _app(monkeypatch)
    app._handle("tap", None)
    session = app._session
    app._recorder.on_segment(_tone(1.0), 0)
    _drain(app)

    app._handle("cancel", None)
    app._events.put(("piece", (session, "遅れて届いた", None, 0)))
    _drain(app)

    assert root.typed == []
    assert app._state == "idle"
    assert shown[-1] == "やめました"


def test_文字にできなかった切れ端があれば数を知らせる(monkeypatch) -> None:
    app, root, shown = _app(monkeypatch)
    app._handle("tap", None)
    session = app._session
    app._recorder.on_segment(_tone(1.0), 0)
    app._recorder.on_segment(_tone(1.0), 0)

    app._handle("tap", None)
    app._events.put(("piece", (session, "", "Webアプリにつながりません。", 0)))
    app._events.put(("piece", (session, "入った", None, 0)))
    _drain(app)

    assert root.typed == ["入った"]
    assert app._state == "idle"
    assert shown[-1] == "文字にできなかった部分が1か所あります"


def test_同じ音声入力のあいだは前の切れ端の文を一緒に送り_次の音声入力では送らない(monkeypatch) -> None:
    from companion.dictation import app as app_module

    sent: list[str] = []
    replies = iter(["一つ目。", "二つ目。", "新しい話。"])

    def fake_transcribe(wav, base_url, *, previous=""):
        sent.append(previous)
        return next(replies)

    monkeypatch.setattr(app_module, "transcribe", fake_transcribe)
    app, _, _ = _app(monkeypatch)
    app._session = 1
    app._send(1, b"x", 0)
    app._send(1, b"x", 0)
    app._session = 2  # 止めてもう一度録り始めた
    app._send(2, b"x", 0)

    assert sent == ["", "一つ目。", ""]
    assert app._context == (2, "新しい話。")


def test_文字にできなかった切れ端は前の文に足さない(monkeypatch) -> None:
    from companion.dictation import app as app_module

    sent: list[str] = []
    replies = iter(["一つ目。", DictationError("つながりません"), "三つ目。"])

    def fake_transcribe(wav, base_url, *, previous=""):
        sent.append(previous)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(app_module, "transcribe", fake_transcribe)
    app, _, _ = _app(monkeypatch)
    app._session = 1
    for _ in range(3):
        app._send(1, b"x", 0)

    assert sent == ["", "一つ目。", "一つ目。"]


def test_切れ端の終わりの丸は次の切れ端で付けるか決め_止めたら最後に入れる(monkeypatch) -> None:
    app, root, _ = _app(monkeypatch)
    app._handle("tap", None)
    session = app._session
    for _ in range(3):
        app._recorder.on_segment(_tone(1.0), 0)
    _drain(app)

    app._events.put(("piece", (session, "今日は形態素解析の。", None, 0)))
    app._events.put(("piece", (session, "話をします。", None, 300)))
    app._events.put(("piece", (session, "次の話です。", None, 1500)))
    _drain(app)
    app._handle("tap", None)  # 止める

    assert root.typed == ["今日は形態素解析の", "話をします", "。次の話です", "。"]
    assert app._state == "idle"
    assert root.clipboard == "元の文字"


def test_やめたときは持っていた丸を入れない(monkeypatch) -> None:
    app, root, _ = _app(monkeypatch)
    app._handle("tap", None)
    session = app._session
    app._recorder.on_segment(_tone(1.0), 0)
    _drain(app)
    app._events.put(("piece", (session, "一つ目。", None, 0)))
    _drain(app)

    app._handle("cancel", None)
    app._handle("tap", None)  # もう一度録り始める
    app._recorder.on_segment(_tone(1.0), 0)
    _drain(app)
    app._events.put(("piece", (app._session, "新しい話", None, SENTENCE_PAUSE_MS * 2)))
    _drain(app)

    assert root.typed == ["一つ目", "新しい話"]
