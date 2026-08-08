"""ジョブ状況の表示のテスト.

UTCで保存された時刻が、日本時間で表示されることを保証する。
ここがずれていると「今朝ジョブが動いたか」の判断を誤る。
"""

from __future__ import annotations

from companion.db.jobs import latest_run_per_job, list_recent_runs


def insert_run(conn, job_name: str, status: str, message: str, ran_at: str, duration_ms=1200):
    conn.execute(
        "INSERT INTO job_runs (job_name, status, message, duration_ms, ran_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (job_name, status, message, duration_ms, ran_at),
    )


def test_UTCの記録が日本時間で読み出される(conn) -> None:
    # Arrange: SQLiteの datetime('now') はUTCで入る
    insert_run(conn, "morning_brief", "success", "送信", "2026-08-07T22:30:00")

    # Act
    runs = list_recent_runs(conn)

    # Assert: 日本時間では翌日の07:30
    assert runs[0].ran_at.strftime("%m/%d %H:%M") == "08/08 07:30"


def test_所要時間が秒で表示される(conn) -> None:
    # Arrange
    insert_run(conn, "morning_brief", "success", "送信", "2026-08-07T22:30:00", duration_ms=2395)

    # Act / Assert
    assert list_recent_runs(conn)[0].duration_label == "2.4秒"


def test_失敗した実行を区別できる(conn) -> None:
    # Arrange
    insert_run(conn, "morning_brief", "failed", "API down", "2026-08-07T22:30:00")

    # Act
    run = list_recent_runs(conn)[0]

    # Assert
    assert run.is_success is False
    assert run.message == "API down"


def test_ジョブごとの最新だけを取り出せる(conn) -> None:
    # Arrange
    insert_run(conn, "morning_brief", "failed", "1回目", "2026-08-06T22:30:00")
    insert_run(conn, "morning_brief", "success", "2回目", "2026-08-07T22:30:00")
    insert_run(conn, "evening_prompt", "success", "催促", "2026-08-07T13:00:00")

    # Act
    latest = latest_run_per_job(conn)

    # Assert
    assert {run.job_name: run.message for run in latest} == {
        "morning_brief": "2回目",
        "evening_prompt": "催促",
    }


def test_履歴は新しい順に返る(conn) -> None:
    # Arrange
    insert_run(conn, "a", "success", "古い", "2026-08-06T00:00:00")
    insert_run(conn, "b", "success", "新しい", "2026-08-07T00:00:00")

    # Act / Assert
    assert [run.message for run in list_recent_runs(conn)] == ["新しい", "古い"]


def test_実行がなければ空を返す(conn) -> None:
    assert list_recent_runs(conn) == []
    assert latest_run_per_job(conn) == []
