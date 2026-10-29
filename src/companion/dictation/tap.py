"""キーを「1回だけ押して離した」ことを見分ける.

右Ctrlは Ctrl+C のように組み合わせでも使うので、ほかのキーと一緒に押したときや、
長く押したままのときは合図にしない。押して、ほかのキーを押さずに、すぐ離したときだけ合図にする。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable

# これより長く押していたら合図にしない（組み合わせで使うつもりで押したまま、など）.
TAP_MAX_SEC = 0.6


class TapDetector:
    """1つのキーの「1回押し」を見分ける. press / release にキーを渡すと、合図のときだけ True を返す."""

    def __init__(self, key: Hashable, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._key = key
        self._clock = clock
        self._pressed_at: float | None = None
        self._spoiled = False

    def press(self, key: Hashable) -> bool:
        if key == self._key:
            if self._pressed_at is None:  # 押したままの自動の繰り返しは数えない
                self._pressed_at, self._spoiled = self._clock(), False
        elif self._pressed_at is not None:
            self._spoiled = True  # 組み合わせで使った（Ctrl+C など）
        return False

    def release(self, key: Hashable) -> bool:
        if key != self._key or self._pressed_at is None:
            return False
        held = self._clock() - self._pressed_at
        self._pressed_at = None
        return not self._spoiled and held <= TAP_MAX_SEC
