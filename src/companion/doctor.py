"""環境の疎通確認コマンド.

    uv run python -m companion.doctor

設定・DB・外部連携をひととおり確認し、どこが未設定かを日本語で表示する。
一部が失敗しても最後まで実行し、まとめて結果を出す。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from companion.config import DB_PATH, ConfigError, get_config

OK = "[ OK ]"
NG = "[ NG ]"
SKIP = "[skip]"


@dataclass
class CheckResult:
    """1項目の確認結果."""

    name: str
    status: str
    detail: str


def _check_config() -> CheckResult:
    try:
        config = get_config()
    except ConfigError as exc:
        return CheckResult("設定 (.env)", NG, str(exc))

    missing = []
    if not config.has_notify:
        missing.append("NTFY_TOPIC")
    if not config.has_calendar:
        missing.append(f"サービスアカウント鍵 ({config.google_service_account_file})")
    if missing:
        return CheckResult("設定 (.env)", NG, f"未設定: {', '.join(missing)}")
    # 通知のリンク先。未設定だと外出先で通知をタップしても開けないので、
    # 異常ではないが分かるように出しておく。
    from companion.links import base_url

    link = base_url() if config.app_base_url else f"{base_url()}（家のWi-Fi内のみ）"
    return CheckResult(
        "設定 (.env)",
        OK,
        f"ニュース={','.join(config.news_topics)} / Web={config.web_host}:{config.web_port}\n"
        f"    通知のリンク先: {link}",
    )


def _check_database() -> CheckResult:
    from companion.db.connection import session

    if not DB_PATH.exists():
        return CheckResult(
            "データベース", NG, "未作成です。uv run python -m companion.db.init を実行してください。"
        )
    try:
        with session() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table'"
            ).fetchone()["n"]
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - sqlite3 は複数の例外型を投げる
        return CheckResult("データベース", NG, f"接続できません: {exc}")
    return CheckResult("データベース", OK, f"テーブル{count}個 / journal_mode={mode}")


def _check_fts() -> CheckResult:
    """日本語が全文検索に引っかかるかを実データで確認する."""
    from companion.db.connection import session

    try:
        with session() as conn:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO entries (entry_date, raw_text) VALUES ('1900-01-01', ?)",
                ("疎通確認のためのダミー日記です",),
            )
            hits = conn.execute(
                "SELECT count(*) AS n FROM entries_fts WHERE entries_fts MATCH ?", ("ダミー",)
            ).fetchone()["n"]
            conn.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("全文検索 (FTS5)", NG, f"確認に失敗しました: {exc}")

    if hits == 0:
        return CheckResult("全文検索 (FTS5)", NG, "日本語がヒットしませんでした")
    return CheckResult("全文検索 (FTS5)", OK, "日本語の部分一致を確認")


def _check_claude() -> CheckResult:
    from companion.integrations.claude_runner import ClaudeError, find_claude_binary, run_claude

    binary = find_claude_binary()
    if binary is None:
        return CheckResult("Claude Code", NG, "claude コマンドが PATH にありません")
    try:
        result = run_claude(
            "「疎通確認OK」とだけ返してください。他には何も出力しないでください。",
            timeout_sec=120,
        )
    except ClaudeError as exc:
        return CheckResult("Claude Code", NG, str(exc))
    return CheckResult("Claude Code", OK, f"応答: {result.text.strip()[:40]}")


def _check_notify() -> CheckResult:
    from companion.integrations.notify import NotifyError, send_notification

    config = get_config()
    if not config.has_notify:
        return CheckResult("通知 (ntfy)", SKIP, "NTFY_TOPIC が未設定のため省略")
    try:
        send_notification(
            "daily-companion の疎通確認です。この通知が届けば設定は完了しています。",
            title="疎通確認",
            tags=["white_check_mark"],
        )
    except NotifyError as exc:
        return CheckResult("通知 (ntfy)", NG, str(exc))
    # トピック名は知っていれば誰でも通知を読める秘密なので、画面にも出さない.
    return CheckResult("通知 (ntfy)", OK, "送信しました")


def _check_calendar() -> CheckResult:
    from companion.integrations.calendar_google import (
        CalendarError,
        fetch_events,
        list_calendars,
        service_account_email,
    )

    config = get_config()
    if not config.has_calendar:
        return CheckResult(
            "Googleカレンダー",
            SKIP,
            f"鍵ファイルが未配置のため省略 ({config.google_service_account_file})",
        )
    try:
        email = service_account_email()
        calendars = list_calendars()
    except CalendarError as exc:
        return CheckResult("Googleカレンダー", NG, str(exc))
    except Exception as exc:  # noqa: BLE001 - Google APIは多様な例外を投げる
        return CheckResult("Googleカレンダー", NG, f"予期しないエラー: {exc}")

    if not calendars:
        return CheckResult(
            "Googleカレンダー",
            NG,
            "認証は成功しましたが、対象カレンダーが0個です。\n"
            f"       (1) Googleカレンダーの「設定と共有」で {email} に共有を追加してください。\n"
            "       (2) 共有済みなら .env の GOOGLE_CALENDAR_IDS にカレンダーIDを直接指定してください。\n"
            "           サービスアカウントは共有の招待を承諾しないため、一覧には現れません。",
        )

    names = ", ".join(name for _, name in calendars[:4])
    try:
        today = date.today()
        events = fetch_events(today, today)
    except CalendarError as exc:
        return CheckResult("Googleカレンダー", NG, f"予定の取得に失敗: {exc}")

    return CheckResult(
        "Googleカレンダー", OK, f"カレンダー{len(calendars)}個 ({names}) / 今日の予定 {len(events)}件"
    )


CHECKS: tuple[tuple[str, Callable[[], CheckResult]], ...] = (
    ("config", _check_config),
    ("database", _check_database),
    ("fts", _check_fts),
    ("claude", _check_claude),
    ("notify", _check_notify),
    ("calendar", _check_calendar),
)


def main() -> int:
    print("daily-companion 疎通確認")
    print("=" * 60)

    results: list[CheckResult] = []
    for _, check in CHECKS:
        try:
            result = check()
        except Exception as exc:  # noqa: BLE001 - どの項目で落ちても続行する
            result = CheckResult(check.__name__, NG, f"想定外のエラー: {exc}")
        results.append(result)
        print(f"{result.status} {result.name}")
        print(f"       {result.detail}")

    print("=" * 60)
    failed = [r for r in results if r.status == NG]
    skipped = [r for r in results if r.status == SKIP]
    if failed:
        print(f"{len(failed)}件が未完了です: {', '.join(r.name for r in failed)}")
        return 1
    if skipped:
        print(f"すべて成功しました（{len(skipped)}件は未設定のため省略）。")
        return 0
    print("すべて成功しました。Phase 0 完了です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
