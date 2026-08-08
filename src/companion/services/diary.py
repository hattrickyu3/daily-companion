"""日記の保存とClaudeによる構造化.

保存は2段階に分ける。
  1. 原文をDBに書く（ここは絶対に失敗させない）
  2. Claudeに投げて要約・タグ・申し送りを付ける（失敗しても原文は残る）

Claudeの応答は外部から来る不定形のデータとして扱い、
JSONとして読めなければ何も反映せずに諦める。原文が無傷なら後からやり直せる。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

from companion.db.repository import StructuredEntry, apply_structure, save_raw_text
from companion.integrations.claude_runner import ClaudeError, extract_json_block, run_claude

log = logging.getLogger(__name__)

MAX_TAGS = 5
MAX_LIST_ITEMS = 6
STRUCTURE_TIMEOUT_SEC = 180

SYSTEM_PROMPT = (
    "あなたは利用者本人のための日記アシスタントです。"
    "書かれた内容だけを根拠にし、推測で事実を足さないでください。"
    "評価や説教はせず、淡々と整理してください。"
)

PROMPT_TEMPLATE = """以下は{entry_date}の日記の原文です。これを構造化してください。

<日記>
{raw_text}
</日記>

次のJSONだけを出力してください。前置きも説明もコードフェンスも不要です。

{{
  "summary": "全体を1〜2文で要約",
  "done": ["その日にやったことを箇条書きで最大{max_items}件"],
  "insights": ["気づき・学び・反省を最大{max_items}件。なければ空配列"],
  "carry_over": "明日に持ち越すこと。なければ null",
  "mood": "その日の気分を10文字以内で。書かれていなければ null",
  "tags": ["内容を表す短いタグを最大{max_tags}件。日本語の名詞で"]
}}

制約:
- 原文に書かれていないことを足さないでください。
- summary は必ず埋めてください。
- 配列の要素は簡潔な体言止めにしてください。
"""


def _clean_list(value: object, limit: int) -> tuple[str, ...]:
    """Claudeが返した配列を、文字列だけのタプルに整える."""
    if not isinstance(value, list):
        return ()
    items = [str(item).strip() for item in value if str(item).strip()]
    return tuple(dict.fromkeys(items))[:limit]


def _clean_text(value: object) -> str | None:
    """Claudeが返した文字列を整える. 空や null は None にする."""
    if value is None or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    return text or None


def parse_structure(payload: dict) -> StructuredEntry | None:
    """ClaudeのJSONを StructuredEntry にする. summary が無ければ None."""
    summary = _clean_text(payload.get("summary"))
    if summary is None:
        return None

    return StructuredEntry(
        summary=summary,
        done=_clean_list(payload.get("done"), MAX_LIST_ITEMS),
        insights=_clean_list(payload.get("insights"), MAX_LIST_ITEMS),
        carry_over=_clean_text(payload.get("carry_over")),
        mood=_clean_text(payload.get("mood")),
        tags=_clean_list(payload.get("tags"), MAX_TAGS),
    )


def structure_text(entry_date: date, raw_text: str) -> StructuredEntry | None:
    """Claudeに日記を構造化させる. 失敗したら None を返す（例外は投げない）."""
    prompt = PROMPT_TEMPLATE.format(
        entry_date=entry_date.isoformat(),
        raw_text=raw_text,
        max_items=MAX_LIST_ITEMS,
        max_tags=MAX_TAGS,
    )
    try:
        result = run_claude(
            prompt, system_prompt=SYSTEM_PROMPT, timeout_sec=STRUCTURE_TIMEOUT_SEC
        )
    except ClaudeError as exc:
        log.warning("structuring failed for %s: %s", entry_date, exc)
        return None

    payload = extract_json_block(result.text)
    if payload is None:
        log.warning("structuring returned unparseable output for %s", entry_date)
        return None

    return parse_structure(payload)


def save_entry(conn: sqlite3.Connection, entry_date: date, raw_text: str) -> int:
    """原文だけを保存してIDを返す. 構造化は呼び出し側が別途行う."""
    return save_raw_text(conn, entry_date, raw_text)


def structure_entry(conn: sqlite3.Connection, entry_id: int, entry_date: date, raw_text: str) -> bool:
    """保存済みのエントリを構造化して反映する. 反映できたら True."""
    structured = structure_text(entry_date, raw_text)
    if structured is None:
        return False
    apply_structure(conn, entry_id, structured)
    log.info("structured entry %s (%s)", entry_id, entry_date)
    return True
