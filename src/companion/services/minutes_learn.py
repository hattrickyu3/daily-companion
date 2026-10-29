"""議事録を手で直したところから、辞書の候補（間違えやすい書き方 → 正しい書き方）を拾う.

直す前と後を行ごとに突き合わせ、対応する行の中で1文字ずつ違う所を探す。違う所を、前後に続く
漢字・カタカナ・英数字まで広げて「言葉」にする（「田名か」→「田中」は、違う所「名か」→「中」に
前の「田」を足した組になる）。

拾わないもの: 文の書き直し（長すぎる）、言い回しや活用だけの直し（直した後がひらがなだけ）、
句読点だけの直し、足しただけ・消しただけの直し。拾ったものは辞書の画面で本人が選んでから登録する。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

MIN_WORD_LENGTH = 2
MAX_WORD_LENGTH = 20
MAX_SUGGESTIONS_PER_EDIT = 20
# 直す前と後の行がこれ以上似ていれば「言葉を直した行」とみなす. 下回るのは文の書き直し.
MIN_LINE_SIMILARITY = 0.5
# 比べる量の上限. 1文字ずつの比べは長い文で急に遅くなる（2万字の1行で16秒かかった. 2026-10-24 レビュー）.
# 議事録の本文はふつう数千字なので、これを超える大きな直しからは拾わない。
MAX_BODY_CHARS = 30_000
MAX_LINE_CHARS = 400
# 書き換えた行のまとまりで、比べる組の数（前の行数 × 後の行数）の上限. 大きな書き直しからは拾わない.
MAX_BLOCK_PAIRS = 400
# 言葉の続きとみなす文字（漢字・カタカナ・長音・英数字）. ひらがなと記号は言葉の切れ目とみなす.
_WORD_CHAR = re.compile(r"[一-鿿㐀-䶿々゠-ヿーA-Za-z0-9Ａ-Ｚａ-ｚ０-９]")
# 直した後の言葉が、辞書に載せる意味のあるもの（漢字・カタカナ・英字を含む）か.
_TRAILING_HIRAGANA = re.compile(r"[ぁ-ゟ]+$")
_MEANINGFUL = re.compile(r"[一-鿿㐀-䶿々ァ-ヺA-Za-zＡ-Ｚａ-ｚ]")


def _is_word_char(char: str) -> bool:
    return bool(_WORD_CHAR.fullmatch(char))


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _line_pairs(before: list[str], after: list[str]) -> list[tuple[str, str]]:
    """直す前と後で対応する行の組.

    書き換えた行のまとまりの中で、直す前の行ごとにいちばん似ている後の行を選ぶ（行を足した・消した直しにも対応）。
    似ていない行（文の書き直し）は組にしない。言葉を直しただけなら、行のほとんどはそのまま残っている。
    """
    pairs = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag != "replace":
            continue
        if i2 - i1 == j2 - j1:
            # 行の数が同じなら順に組にする（同じ名前を何十行も直したときも、比べる量は行の数だけで済む）.
            pairs += [
                (old, new) for old, new in zip(before[i1:i2], after[j1:j2])
                if len(old) <= MAX_LINE_CHARS and len(new) <= MAX_LINE_CHARS
                and _similarity(old, new) >= MIN_LINE_SIMILARITY
            ]
            continue
        if (i2 - i1) * (j2 - j1) > MAX_BLOCK_PAIRS:
            continue
        candidates = [new for new in after[j1:j2] if len(new) <= MAX_LINE_CHARS]
        for old in before[i1:i2]:
            if len(old) > MAX_LINE_CHARS or not candidates:
                continue
            # 大まかな似かた（速い）で足りないものは、細かく比べない.
            close = [new for new in candidates
                     if SequenceMatcher(None, old, new, autojunk=False).quick_ratio() >= MIN_LINE_SIMILARITY]
            if not close:
                continue
            best = max(close, key=lambda new: _similarity(old, new))
            if _similarity(old, best) >= MIN_LINE_SIMILARITY:
                pairs.append((old, best))
    return pairs


def _word_changes(old: str, new: str) -> list[tuple[str, str]]:
    """1行の中で置き換えた言葉の組."""
    found = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "replace":
            continue
        # 前後に同じ文字で続く言葉の部分まで広げる.
        while i1 > 0 and j1 > 0 and old[i1 - 1] == new[j1 - 1] and _is_word_char(old[i1 - 1]):
            i1, j1 = i1 - 1, j1 - 1
        while i2 < len(old) and j2 < len(new) and old[i2] == new[j2] and _is_word_char(old[i2]):
            i2, j2 = i2 + 1, j2 + 1
        found.append((old[i1:i2], new[j1:j2]))
    return found


def _is_hiragana(char: str) -> bool:
    return "ぁ" <= char <= "ゟ"


def _stem(text: str) -> str:
    """後ろのひらがな（送り仮名・活用）を落とした部分."""
    return _TRAILING_HIRAGANA.sub("", text)


def _acceptable(wrong: str, right: str) -> bool:
    """辞書に載せても文字起こしを壊しにくい組か.

    登録すると、以後の文字起こしで wrong を全部 right に置き換える。だからふつうの言葉の一部に
    なりやすい形は拾わない（2026-10-24 レビュー: 「きょう」→「今日」は「ごきょうりょく」を壊す）:
        ひらがなだけの wrong / ひらがなが混ざる2文字の wrong / 送り仮名・活用だけの違い（行った→行く）
    """
    if not (MIN_WORD_LENGTH <= len(wrong) <= MAX_WORD_LENGTH and MIN_WORD_LENGTH <= len(right) <= MAX_WORD_LENGTH):
        return False
    if wrong == right or wrong in right:  # 正しい書き方の一部は間違いの形にしない（置き換えると言葉が壊れる）
        return False
    if any(char.isspace() for char in wrong + right):
        return False
    if not all(_is_word_char(c) or _is_hiragana(c) for c in wrong + right):  # 記号を含まない
        return False
    if not _MEANINGFUL.search(right) or all(_is_hiragana(c) for c in wrong):
        return False
    if any(_is_hiragana(c) for c in wrong) and len(wrong) < 3:
        return False
    return _stem(wrong) != _stem(right)


def find_corrections(before: str, after: str) -> list[tuple[str, str]]:
    """直す前と後の本文から、(間違えやすい書き方, 正しい書き方) の組を見つけた順に返す. 同じ組は1つ."""
    if len(before) > MAX_BODY_CHARS or len(after) > MAX_BODY_CHARS:
        return []
    found: list[tuple[str, str]] = []
    for old_line, new_line in _line_pairs(before.splitlines(), after.splitlines()):
        for wrong, right in _word_changes(old_line, new_line):
            if _acceptable(wrong, right) and (wrong, right) not in found:
                found.append((wrong, right))
                if len(found) >= MAX_SUGGESTIONS_PER_EDIT:
                    return found
    return found
