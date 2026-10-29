"""議事録の辞書（固有名詞）と、つなぎ言葉を消す処理のテスト."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from companion.db import meetings as db
from companion.db import words as words_db
from companion.db.connection import apply_schema, session
from companion.services.minutes_words import (
    DEFAULT_FILLERS,
    MAX_HOTWORDS_CHARS,
    Vocabulary,
    apply_replacements,
    clean_word_input,
    load_vocabulary,
    parse_fillers,
    remove_fillers,
)

TZ = ZoneInfo("Asia/Tokyo")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("えーっと、今日は中間発表の話です", "今日は中間発表の話です"),
        ("あのー資料が届きました", "資料が届きました"),
        ("それで、えー、次は実験です", "それで、次は実験です"),
        ("えーーと、始めます", "始めます"),
        ("うーん、そうですね", "そうですね"),
        ("そうですね、えー。", "そうですね。"),
        ("えっと えっと 待ってください", "待ってください"),
        ("えーっと今日は休みです", "今日は休みです"),
        # 「…」で間をあけたとき（2026-10-26、音声入力で「えー...」が残っていた）
        ("音声入力に関しては、えー...辞書は使えているの?", "音声入力に関しては、辞書は使えているの?"),
        ("えー…辞書です", "辞書です"),
        ("あのー‥はい", "はい"),
        ("そうですね...えー...次です", "そうですね...次です"),
    ],
)
def test_決めておいたつなぎ言葉だけを消す(text: str, expected: str) -> None:
    assert remove_fillers(text, DEFAULT_FILLERS) == expected


@pytest.mark.parametrize(
    "text",
    [
        "あの人が来ました",       # 「あの」は指す言葉としても使う
        "へえーすごい",           # 言葉の途中の「えー」
        "ええ、そうです",         # 返事
        "まあまあの出来でした",
        "あのうさぎが走る",       # 2026-10-24 のレビューで壊していた例
        "えーんえーんと泣く",
        "あーあ、疲れた",
        "あーるぐらい",
        "えっとう",
        "ええと思います",
        "えーえー",
    ],
)
def test_言葉の一部や意味のある言葉は消さない(text: str) -> None:
    assert remove_fillers(text, DEFAULT_FILLERS) == text


def test_自分で足した短いつなぎ言葉は後ろに区切りがあるときだけ消す() -> None:
    assert remove_fillers("ま、いいか", ("ま",)) == "いいか"
    assert remove_fillers("まず始めます", ("ま",)) == "まず始めます"


def test_間違えやすい書き方を正しい書き方に置き換える_長いほうから() -> None:
    replacements = (("田名か", "田中"), ("形体素", "形態素"), ("形体素解析", "形態素解析"))

    assert apply_replacements("田名かさんが形体素解析を説明した", replacements) == "田中さんが形態素解析を説明した"


def test_ヒントには参加者と辞書の語を重ねずに入れ長すぎれば切る() -> None:
    vocabulary = Vocabulary(terms=("田中", "形態素解析"), participants=("自分", "田中", "佐藤"))

    assert vocabulary.hotwords() == "田中、佐藤、形態素解析"  # 参加者の名前が先
    many = Vocabulary(terms=tuple(f"用語{i:03d}" for i in range(100)))
    assert len(many.hotwords()) <= MAX_HOTWORDS_CHARS
    assert Vocabulary().hotwords() is None


def test_文字起こしを直すとつなぎ言葉を消してから置き換える() -> None:
    vocabulary = Vocabulary(replacements=(("田名か", "田中"),), fillers=("えーと",))

    assert vocabulary.fix("えーと、田名かさんどうぞ") == "田中さんどうぞ"
    assert vocabulary.fix("  ") == ""


def test_つなぎ言葉の一覧は読点や改行で区切って受ける() -> None:
    assert parse_fillers("えーと、あのー\nうーん, えっと  ") == ("えーと", "あのー", "うーん", "えっと")


def test_辞書の入力をそろえ短すぎる間違いは受けない() -> None:
    assert clean_word_input(" 田中 ", "田名か、棚、 たなか ") == ("田中", ("田名か", "たなか"))
    assert clean_word_input("", "x") is None
    # 正しい書き方そのものは間違いとして持たない（置き換えが空回りする）
    assert clean_word_input("田中", "田中、田名か") == ("田中", ("田名か",))


def test_会議に合った辞書を読む(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "田中", ("田名か",), "")
        words_db.save_word(conn, "形態素解析", ("形体素解析",), "3年３Q/自然言語処理")
        words_db.save_word(conn, "サプライチェーン", (), "3年３Q/サプライチェーンデザイン演習")
        meeting_id = db.create_meeting(
            conn, "自然言語処理", datetime(2026, 10, 24, tzinfo=TZ),
            participants="自分、佐藤", kind="lecture", course="3年３Q/自然言語処理",
        )
        meeting = db.get_meeting(conn, meeting_id)
        vocabulary = load_vocabulary(conn, meeting)
        words_db.set_setting(conn, words_db.FILLERS_KEY, "えーと")
        changed = load_vocabulary(conn, meeting)

    assert vocabulary.terms == ("形態素解析", "田中")  # その授業の語が先
    assert vocabulary.participants == ("自分", "佐藤")
    assert ("形体素解析", "形態素解析") in vocabulary.replacements
    assert vocabulary.fillers == DEFAULT_FILLERS  # まだ一覧を保存していなければ決まったもの
    assert changed.fillers == ("えーと",)


def test_同じ語を登録し直すと間違えやすい書き方を置き換える(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "田中", ("田名か",), "")
        words_db.save_word(conn, "田中", ("棚か",), "")
        saved = words_db.list_words(conn)
        words_db.delete_word(conn, saved[0].id)
        left = words_db.list_words(conn)

    assert [(w.word, w.variants) for w in saved] == [("田中", ("棚か",))]
    assert left == []


def test_正しい書き方の中にある間違いの形は置き換えず置き換えを重ねない() -> None:
    replacements = (("田中", "田中太郎"), ("エーアイ", "AI"), ("AI", "人工知能"))

    assert apply_replacements("田中太郎です", replacements) == "田中太郎です"
    assert apply_replacements("エーアイの話", replacements) == "AIの話"


def test_正しい書き方に含まれる間違いの形は登録しない() -> None:
    assert clean_word_input("田中太郎", "田中、たなか") == ("田中太郎", ("たなか",))


def test_つなぎ言葉だけの文は句点も残さない() -> None:
    assert remove_fillers("えっと。", DEFAULT_FILLERS) == ""


def test_つなぎ言葉が長く続いても速く終わる() -> None:
    import time

    started = time.monotonic()
    remove_fillers("えー、" * 20000 + "本題", DEFAULT_FILLERS)
    remove_fillers("えーえー" * 20000, DEFAULT_FILLERS)

    assert time.monotonic() - started < 2


def test_録音中の文字起こしに出てきた語をヒントに優先して入れる() -> None:
    terms = tuple(f"用語{i:03d}" for i in range(60)) + ("形態素解析", "気仙沼")
    vocabulary = Vocabulary(terms=terms, replacements=(("けせんぬま", "気仙沼"),), participants=("佐藤",))

    plain = vocabulary.hotwords()
    seen = vocabulary.hotwords(seen="今日は形態素解析と、けせんぬまの話をした")

    assert "形態素解析" not in plain  # 何も知らなければ、後ろの語は上限で入らない
    assert seen.startswith("佐藤、形態素解析、気仙沼")  # 間違えやすい書き方で出てきた語も拾う


def test_止めたあとの文字起こしではヒントを広げられる() -> None:
    from companion.services.minutes_words import FINAL_HOTWORDS_CHARS

    vocabulary = Vocabulary(terms=tuple(f"用語{i:03d}" for i in range(100)))

    assert MAX_HOTWORDS_CHARS < len(vocabulary.hotwords(limit=FINAL_HOTWORDS_CHARS)) <= FINAL_HOTWORDS_CHARS


def test_長い語が入らなくても後ろの短い語は入れる() -> None:
    vocabulary = Vocabulary(terms=("あ" * 115, "い" * 10, "BCP"))

    assert vocabulary.hotwords() == "あ" * 115 + "、BCP"
