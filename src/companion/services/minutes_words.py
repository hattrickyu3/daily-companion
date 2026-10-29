"""議事録の辞書（人の名前・授業の用語）と、つなぎ言葉を消す処理.

文字起こしはAIに書き直させない。決まった手順でだけ直す（もっともらしい言葉を足されないため）:

    1. 正しい書き方と参加者の名前を、Whisperにヒント（hotwords）として渡す → はじめから正しく出やすくなる
    2. 文字起こしのあとで、決めておいたつなぎ言葉（えーと・あのー など）を消す
    3. 間違えやすい書き方（「田名か」）を正しい書き方（「田中」）に置き換える

つなぎ言葉は「文の頭」か「読点・句点・空白のあと」にあるときだけ消す。「あの人」の「あの」や
「へえー」の中の「えー」のように、言葉の一部や意味のある言葉は消さない。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from companion.db import words as words_db
from companion.db.meetings import Meeting
from companion.services.minutes_speakers import parse_participants

# 消すつなぎ言葉の初めの一覧. 画面の「辞書」から変えられる.
# 「あの」「まあ」「なんか」は意味のある言葉としても使うので入れない（伸ばした「あのー」だけ）.
DEFAULT_FILLERS = ("えーっと", "えーと", "えっと", "ええと", "えー", "あのー", "あのう", "うーん", "んー", "あー")
# Whisperに渡すヒントの長さ. 録音中は直前の文字起こしも一緒に渡すので、その場所を空けておく.
MAX_HOTWORDS_CHARS = 120
# 録音を止めたあとの正確な文字起こしでは直前の文字起こしを渡さないので、ヒントを広げられる.
# faster-whisper はヒントを223トークンで切る（日本語は1文字1〜2トークン）ので、それに収まる長さにする.
FINAL_HOTWORDS_CHARS = 180
# 間違えやすい書き方は2文字から. 1文字だと関係のない所まで置き換えてしまう.
MIN_VARIANT_LENGTH = 2
MAX_WORD_LENGTH = 40
FILLER_SEPARATORS = re.compile(r"[、，,\n\r]+")
# つなぎ言葉の前に来てよいもの（文の頭はこれとは別に扱う）.
_BEFORE = r"(?:(?<=^)|(?<=[、。，．！？!?「」\s.…‥]))"


@dataclass(frozen=True)
class Vocabulary:
    """1つの会議で使う辞書."""

    terms: tuple[str, ...] = ()
    replacements: tuple[tuple[str, str], ...] = ()
    fillers: tuple[str, ...] = field(default=DEFAULT_FILLERS)
    participants: tuple[str, ...] = ()

    def hotwords(self, *, limit: int = MAX_HOTWORDS_CHARS, seen: str = "") -> str | None:
        """Whisperに渡すヒント. 何も無ければ None.

        長さに上限があるので、大事な順に入るだけ入れる:
            参加者の名前 → seen（録音中の文字起こし）に出てきた辞書の語 → そのほかの辞書の語（授業の語が先）
        seen に出てきたかは、正しい書き方か間違えやすい書き方のどちらかが含まれているかで見る。
        録音を止めたあとの正確な文字起こしでこれを使えば、辞書が何百語あっても、その会議に出てくる語がヒントに入る。
        """
        found = {word for wrong, word in self.replacements if wrong in seen} | {w for w in self.terms if w in seen}
        ordered = [*self.participants, *(w for w in self.terms if w in found), *(w for w in self.terms if w not in found)]
        picked: list[str] = []
        length = 0
        for word in ordered:
            if word in picked or word == "自分":
                continue
            extra = len(word) + (1 if picked else 0)
            if length + extra > limit:
                continue  # 長い語が入らなくても、あとの短い語は入るかもしれない
            picked.append(word)
            length += extra
        return "、".join(picked) or None

    def fix(self, text: str) -> str:
        """文字起こしを直す. つなぎ言葉を消してから、間違えやすい書き方を置き換える."""
        return apply_replacements(remove_fillers(text, self.fillers), self.replacements).strip()


def _filler_pattern(filler: str) -> str:
    """つなぎ言葉の正規表現. 「ー」は何個伸ばしても同じとみなす（えーと / えーーと）."""
    parts = ["ー+" if char == "ー" else re.escape(char) for char in filler]
    return "".join(parts) + r"[ー〜~]*"


# つなぎ言葉のすぐ後ろに区切りが無くても消してよい、終わり方（えー明日 / えーっと今日 / えーと資料）.
# そのときも、後ろが漢字・カタカナ・英数字のときだけ。ひらがなが続くと言葉の一部のことがある
# （あーあ・あーる・えーんえーん・えっとう）. 2026-10-24 のレビューで、区切りを見ずに消して言葉を壊していた.
_DIRECT_ENDINGS = ("ー", "っと", "ーと")
_WORD_START = r"(?=[一-鿿㐀-䶿゠-ヿA-Za-z0-9Ａ-Ｚａ-ｚ０-９々])"
# 「…」「...」で間をあけたときも区切りとみなし、つなぎ言葉と一緒に消す（えー...辞書は → 辞書は. 2026-10-26）.
_AFTER = r"(?:[、，,]\s*|(?:\.{2,}|[…‥]+)\s*|\s+|(?=[。．!?！？])|$)"


def _filler_regex(fillers: tuple[str, ...]) -> re.Pattern[str]:
    # 長いほうから試す（「えーっと」を「えー」より先に）.
    ordered = sorted(fillers, key=len, reverse=True)
    every = "|".join(_filler_pattern(f) for f in ordered)
    direct = "|".join(_filler_pattern(f) for f in ordered if f.endswith(_DIRECT_ENDINGS))
    item = f"(?:{every}){_AFTER}" + (f"|(?:{direct}){_WORD_START}" if direct else "")
    # 「えっと えっと」のように続くものも1回でまとめて消す（何度も見直すと長い文で遅くなる）.
    return re.compile(rf"{_BEFORE}(?:{item})+")


def remove_fillers(text: str, fillers: tuple[str, ...]) -> str:
    """決めておいたつなぎ言葉を消す.

    前が文の頭か読点・句点・空白で、後ろが読点・空白・句点・文の終わりのときだけ消す。
    「ー」で終わるものなど（えー・えーっと）は、後ろに漢字・カタカナが続いても消す（えー明日は）。
    """
    if not fillers or not text:
        return text
    text = _filler_regex(fillers).sub("", text)
    text = re.sub(r"、+([。．！？!?])", r"\1", text)
    text = re.sub(r"、{2,}", "、", text)
    return re.sub(r"^[、，,。．\s]+", "", text)


def apply_replacements(text: str, replacements: tuple[tuple[str, str], ...]) -> str:
    """間違えやすい書き方を正しい書き方に置き換える.

    1回で全部を見て置き換える（長いほうを優先）。順に置き換えると、置き換えた結果をさらに別の語で
    置き換えたり、正しい書き方の中にある間違いの形をもう一度置き換えたりしてしまう。
    """
    mapping = {wrong: right for wrong, right in replacements if wrong and wrong not in right}
    if not mapping:
        return text
    pattern = re.compile("|".join(re.escape(w) for w in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda match: mapping[match.group(0)], text)


def parse_fillers(raw: str) -> tuple[str, ...]:
    """画面から届いたつなぎ言葉の一覧. 読点・カンマ・改行で区切る. 同じものは1つにする."""
    found: list[str] = []
    for part in FILLER_SEPARATORS.split(raw):
        word = part.strip()[:MAX_WORD_LENGTH]
        if word and word not in found:
            found.append(word)
    return tuple(found)


def clean_word_input(word: str, variants: str) -> tuple[str, tuple[str, ...]] | None:
    """画面から届いた語をそろえる. 正しい書き方が空なら None.

    間違えやすい書き方は「、」区切り。短すぎるもの（1文字）と、正しい書き方の中に含まれるもの
    （「田中太郎」に対する「田中」. 置き換えると「田中太郎太郎」になる）は捨てる。
    """
    correct = word.strip()[:MAX_WORD_LENGTH]
    if not correct:
        return None
    kept: list[str] = []
    for part in parse_fillers(variants):
        if len(part) >= MIN_VARIANT_LENGTH and part not in correct and part not in kept:
            kept.append(part)
    return correct, tuple(kept)


def load_fillers(conn: sqlite3.Connection) -> tuple[str, ...]:
    """保存したつなぎ言葉の一覧. まだ保存していなければ初めの一覧."""
    saved = words_db.get_setting(conn, words_db.FILLERS_KEY)
    return DEFAULT_FILLERS if saved is None else parse_fillers(saved)


def load_vocabulary(conn: sqlite3.Connection, meeting: Meeting | None) -> Vocabulary:
    """会議で使う辞書. どの場面でも使う語と、その会議の授業の語、参加者の名前.

    置き換えは apply_replacements が、正しい書き方の中に含まれる間違いの形を飛ばす。
    """
    course = meeting.course if meeting else ""
    words = words_db.words_for(conn, course)
    return Vocabulary(
        terms=tuple(dict.fromkeys(w.word for w in words)),
        replacements=tuple((variant, w.word) for w in words for variant in w.variants),
        fillers=load_fillers(conn),
        participants=tuple(parse_participants(meeting.participants)) if meeting else (),
    )
