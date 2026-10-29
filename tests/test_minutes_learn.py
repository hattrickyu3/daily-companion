"""議事録を手で直したところから、辞書の候補（間違えやすい書き方 → 正しい書き方）を拾うテスト."""

from __future__ import annotations

from pathlib import Path

from companion.db import words as words_db
from companion.db.connection import apply_schema, session
from companion.services.minutes_learn import MAX_SUGGESTIONS_PER_EDIT, find_corrections


def test_言葉の一部を直したら言葉全体の組にする() -> None:
    before = "## 要点\n\n- 田名かさんが形体素解析の結果を報告した\n"
    after = "## 要点\n\n- 田中さんが形態素解析の結果を報告した\n"

    assert find_corrections(before, after) == [("田名か", "田中"), ("形体素解析", "形態素解析")]


def test_カタカナや英字の言葉も拾う() -> None:
    assert find_corrections("- ジーピーティーで要約した", "- GPTで要約した") == [("ジーピーティー", "GPT")]
    assert find_corrections("- チャットGBTを使う", "- チャットGPTを使う") == [("チャットGBT", "チャットGPT")]


def test_文の書き直しや言い回しだけの直しは拾わない() -> None:
    # ひらがなだけの直し（言い回し・活用）
    assert find_corrections("- 報告しました", "- 報告した") == []
    assert find_corrections("- 来週までにやる", "- 来週までにやります") == []
    # 長い書き直し
    assert find_corrections("- 実験の結果がよくなかったので来週もう一度やる", "- 次回までに条件を変えて再実験することになった") == []
    # 句読点だけ
    assert find_corrections("- 田中さん、報告", "- 田中さん。報告") == []
    # 足しただけ・消しただけ
    assert find_corrections("- 報告した", "- 田中さんが報告した") == []


def test_行が増えたり減ったりしても対応する行の中で拾う() -> None:
    before = "## 要点\n\n- 田名かさんが報告\n- 次は来週\n"
    after = "## 要点\n\n- 新しく足した行\n- 田中さんが報告\n- 次は来週\n"

    assert find_corrections(before, after) == [("田名か", "田中")]


def test_同じ直しは1つにして多すぎれば切る() -> None:
    before = "\n".join(f"- 田名かさん{i}" for i in range(3)) + "\n" + "\n".join(f"- 用語{i:02d}エー" for i in range(40))
    after = "\n".join(f"- 田中さん{i}" for i in range(3)) + "\n" + "\n".join(f"- 用語{i:02d}ビー" for i in range(40))

    found = find_corrections(before, after)

    assert found[0] == ("田名か", "田中")
    assert found.count(("田名か", "田中")) == 1
    assert len(found) <= MAX_SUGGESTIONS_PER_EDIT


def test_候補をためて登録するといらないで分けられる(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "田中", ("棚か",), "")
        words_db.add_suggestions(conn, [("田名か", "田中"), ("形体素", "形態素")], course="3年３Q/自然言語処理", meeting_id=1)
        words_db.add_suggestions(conn, [("田名か", "田中")], course="", meeting_id=2)  # 同じ直しは回数だけ増やす
        pending = words_db.list_suggestions(conn)
        tanaka = next(s for s in pending if s.right == "田中")
        words_db.accept_suggestion(conn, tanaka.id)
        other = next(s for s in pending if s.right == "形態素")
        words_db.dismiss_suggestion(conn, other.id)
        words_db.add_suggestions(conn, [("形体素", "形態素")], course="", meeting_id=3)  # いらないと言ったものは出し直さない
        left = words_db.list_suggestions(conn)
        words = {w.word: w.variants for w in words_db.list_words(conn)}

    assert (tanaka.count, tanaka.course) == (2, "3年３Q/自然言語処理")
    assert words["田中"] == ("棚か", "田名か")  # 前からある書き方に足す
    assert left == []


def test_もう辞書にある直しは候補にしない(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "田中", ("田名か",), "")
        words_db.add_suggestions(conn, [("田名か", "田中")], course="", meeting_id=1)

        assert words_db.list_suggestions(conn) == []


def test_文字起こしを壊しやすい直しは拾わない() -> None:
    # ひらがなだけの間違いの形（「ごきょうりょく」の中の「きょう」まで置き換えてしまう）
    assert find_corrections("- きょうは晴れ", "- 今日は晴れ") == []
    # 送り仮名・活用だけの違い
    assert find_corrections("- 図書館に行った", "- 図書館に行く") == []
    # ひらがなが混ざる2文字
    assert find_corrections("- 名かさん", "- 中さん") == []


def test_大きすぎる直しや長い行からは拾わず速く終わる() -> None:
    import time

    from companion.services.minutes_learn import MAX_BODY_CHARS

    long_line = "あ" * 20_000
    started = time.monotonic()
    assert find_corrections(f"- 田名か{long_line}", f"- 田中{long_line}") == []
    assert find_corrections("x" * (MAX_BODY_CHARS + 1), "y" * (MAX_BODY_CHARS + 1)) == []
    rewritten_before = "\n".join(f"- 前の文{i:03d}をまるごと書いた" for i in range(300))
    rewritten_after = "\n".join(f"- 後で全部書き換えた{i:03d}の文章" for i in range(300))
    find_corrections(rewritten_before, rewritten_after)

    assert time.monotonic() - started < 3


def test_辞書とぶつかる候補は足さず語を消すとまた出る(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "田中", (), "")
        words_db.save_word(conn, "形態素", ("形体素",), "")
        # 正しい書き方を間違いの形にしない / ほかの語の間違いの形と重ねない
        words_db.add_suggestions(conn, [("田中", "田仲"), ("形体素", "型体素")], course="", meeting_id=1)
        assert words_db.list_suggestions(conn) == []

        words_db.add_suggestions(conn, [("田名か", "田中")], course="", meeting_id=1)
        words_db.accept_suggestion(conn, words_db.list_suggestions(conn)[0].id)
        tanaka = next(w for w in words_db.list_words(conn) if w.word == "田中")
        words_db.delete_word(conn, tanaka.id)
        words_db.add_suggestions(conn, [("田名か", "田中")], course="", meeting_id=2)

        assert [(s.wrong, s.right) for s in words_db.list_suggestions(conn)] == [("田名か", "田中")]


def test_文章から作った正しい書き方だけの候補を登録できる(tmp_path: Path) -> None:
    with session(tmp_path / "w.db") as conn:
        apply_schema(conn)
        words_db.save_word(conn, "BCP", (), "3年３Q/リスク＆クライシスマネジメント")
        words_db.add_suggestions(conn, [("", "形態素解析")], course="3年３Q/自然言語処理", meeting_id=None)
        words_db.add_suggestions(conn, [("", "BCP")], course="3年３Q/リスク＆クライシスマネジメント", meeting_id=None)
        pending = words_db.list_suggestions(conn)
        words_db.accept_suggestion(conn, pending[0].id)
        words = [(w.word, w.variants, w.course) for w in words_db.list_words(conn)]

    assert [(s.wrong, s.right) for s in pending] == [("", "形態素解析")]  # もう辞書にあるBCPは出さない
    assert ("形態素解析", (), "3年３Q/自然言語処理") in words
