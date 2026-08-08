"""DB初期化コマンド.

    uv run python -m companion.db.init

既存のDBがあっても壊さず、足りないテーブルだけを追加する。
"""

from __future__ import annotations

from companion.config import DB_PATH
from companion.db.connection import apply_schema, session


def main() -> int:
    with session() as conn:
        apply_schema(conn)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

    print(f"データベースを初期化しました: {DB_PATH}")
    print(f"テーブル数: {len(tables)}")
    for name in tables:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
