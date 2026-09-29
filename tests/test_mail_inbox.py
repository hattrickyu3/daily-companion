"""メール通知ツールの判定結果を読む処理のテスト.

結果ファイルは別のアプリが書くので、壊れていても画面が落ちないことを確かめる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from companion.services.mail_inbox import load

TODAY = date(2026, 9, 30)


def _write(path: Path, mails: list[dict], updated_at: str = "2026-09-30T08:01:00") -> Path:
    path.write_text(
        json.dumps({"updated_at": updated_at, "mails": mails}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _mail(**overrides) -> dict:
    base = {
        "received": "2026-09-29 15:22",
        "category": "要対応",
        "deadline": "2026-10-07",
        "summary": "GPS-Academic受検",
        "sender": "portalmail@tcu.ac.jp",
        "subject": "【学部3年全員受検】GPS-Academic 受検のご案内",
    }
    return {**base, **overrides}


def test_設定されていなければNoneを返す() -> None:
    assert load(None) is None


def test_ファイルがまだ無ければ空の結果を返す(tmp_path) -> None:
    inbox = load(tmp_path / "results.json")

    assert inbox is not None
    assert inbox.items == ()
    assert inbox.updated_at is None


def test_壊れたファイルでも落ちずに空の結果を返す(tmp_path) -> None:
    path = tmp_path / "results.json"
    path.write_text("{ 書きかけ", encoding="utf-8")

    inbox = load(path)

    assert inbox is not None
    assert inbox.items == ()


def test_メールを読み込める(tmp_path) -> None:
    inbox = load(_write(tmp_path / "results.json", [_mail()]))

    assert inbox is not None
    assert len(inbox.items) == 1
    item = inbox.items[0]
    assert item.category == "要対応"
    assert item.deadline == date(2026, 10, 7)
    assert item.summary == "GPS-Academic受検"
    assert inbox.updated_at is not None and inbox.updated_at.hour == 8


def test_不正な行は飛ばしてほかの行は読む(tmp_path) -> None:
    mails = [
        _mail(received="日付ではない"),
        _mail(category="よくわからない"),
        _mail(deadline="10月7日"),
        _mail(summary="残る行"),
    ]

    inbox = load(_write(tmp_path / "results.json", mails))

    assert inbox is not None
    summaries = [item.summary for item in inbox.items]
    # 締切の書式だけが崩れた行は、締切なしとして残す
    assert summaries == ["GPS-Academic受検", "残る行"]
    assert inbox.items[0].deadline is None


def test_区分で絞り込める(tmp_path) -> None:
    mails = [_mail(), _mail(category="確認", summary="説明会"), _mail(category="不要", summary="宣伝")]
    inbox = load(_write(tmp_path / "results.json", mails))

    assert inbox is not None
    assert [i.summary for i in inbox.filtered("確認")] == ["説明会"]
    assert len(inbox.filtered(None)) == 3
    assert inbox.count("不要") == 1


def test_要対応は締切が過ぎたものを除き締切の近い順に並ぶ(tmp_path) -> None:
    mails = [
        _mail(summary="遠い締切", deadline="2026-10-20"),
        _mail(summary="過ぎた締切", deadline="2026-09-29"),
        _mail(summary="近い締切", deadline="2026-10-01"),
        _mail(summary="確認のもの", category="確認", deadline="2026-10-01"),
    ]
    inbox = load(_write(tmp_path / "results.json", mails))

    assert inbox is not None
    assert [i.summary for i in inbox.action_items(TODAY)] == ["近い締切", "遠い締切"]


def test_締切のない要対応は最近届いたものだけ出す(tmp_path) -> None:
    mails = [
        _mail(summary="最近の連絡", deadline=None, received="2026-09-29 10:00"),
        _mail(summary="古い連絡", deadline=None, received="2026-09-20 10:00"),
    ]
    inbox = load(_write(tmp_path / "results.json", mails))

    assert inbox is not None
    assert [i.summary for i in inbox.action_items(TODAY)] == ["最近の連絡"]


def test_締切までの日数を返す(tmp_path) -> None:
    inbox = load(_write(tmp_path / "results.json", [_mail(deadline="2026-10-02")]))

    assert inbox is not None
    assert inbox.items[0].days_left(TODAY) == 2
