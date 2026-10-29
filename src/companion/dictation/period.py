"""切れ端の終わりの「。」を、次の切れ端が来るまで持っておく係.

切れ端は息つぎごとに切るので、文の途中でも切れる. Whisperは切れ端を1つの話として文字にするので、
文の途中でも終わりに「。」を付けがち（「今日は形態素解析の。話をします。」）. 入れた文字は後から直せないので、
終わりの「。」だけはすぐ入れずに持っておき、次の切れ端の前にしっかり黙っていたときだけ付ける（2026-10-26）.

「？」「！」「、」で終わるときは持たない（話の区切りか、文の途中にあってもおかしくない）.
"""

from __future__ import annotations

from dataclasses import dataclass

# これだけ黙っていたら文の区切りとみなし、持っていた「。」を付ける. 短い息つぎ（0.6秒で切れる）より長くしておく.
SENTENCE_PAUSE_MS = 1000
PERIOD = "。"


@dataclass(frozen=True)
class PeriodHolder:
    """持っている「。」と、持ってから黙っていた一番長い時間.

    約束:
      - いつでも: pause_ms は0以上. 「。」を持っていなければ pause_ms は0
      - take(): 入れる文字と、新しい PeriodHolder を返す（自分は変わらない）
          - 持っていた「。」は、黙っていた長さが SENTENCE_PAUSE_MS 以上のときだけ、文字の前に付ける
          - 返ってきた文字が「。」で終わっていれば、その「。」は外して持つ
          - 何も聞き取れなかった切れ端（空の文字）は、黙っていた長さだけ覚えておく
      - finish(): 持っていた「。」を入れる文字にして、何も持たない PeriodHolder を返す
    """

    held: bool = False
    pause_ms: int = 0

    def __post_init__(self) -> None:
        if self.pause_ms < 0:
            raise ValueError(f"黙っていた長さは0以上にしてください（受け取った値：{self.pause_ms}）")
        if not self.held and self.pause_ms:
            raise ValueError("「。」を持っていないときは、黙っていた長さを覚えません")

    def take(self, text: str, pause_before_ms: int) -> tuple[PeriodHolder, str]:
        """切れ端1つ分の文字を受け取る. pause_before_ms はその切れ端の前に黙っていた長さ（ミリ秒）."""
        if pause_before_ms < 0:
            raise ValueError(f"黙っていた長さは0以上にしてください（受け取った値：{pause_before_ms}）")
        pause = max(self.pause_ms, pause_before_ms)
        if not text:
            return (PeriodHolder(held=True, pause_ms=pause) if self.held else self), ""
        head = PERIOD if self.held and pause >= SENTENCE_PAUSE_MS else ""
        if text.endswith(PERIOD):
            return PeriodHolder(held=True), head + text[: -len(PERIOD)]
        return PeriodHolder(), head + text

    def finish(self) -> tuple[PeriodHolder, str]:
        """止めたとき. 持っていた「。」を返す（無ければ空）."""
        return PeriodHolder(), PERIOD if self.held else ""
