"""話者の番号と、参加者の名前の結び付け.

声で聞き分けた話者には「話者1」「話者2」と番号しか付かない。
清書のときに Sonnet が会話の中身（名前を呼ばれて返事した、自己紹介した等）から
番号と名前を結び付け、応答の最後に次の1行を付けて返す。

    @@話者 話者1=田中, 話者2=佐藤, 話者3=不明

この行は議事録の本文から取り除き、文字起こしのノートの「話者1:」を「田中（話者1）:」に置き換えるのに使う。
"""

from __future__ import annotations

import re

SPEAKER_MAP_PREFIX = "@@話者"
UNKNOWN_NAMES = frozenset({"", "不明", "?", "？", "わからない"})
# 参加者の区切り. 「自分、田中、佐藤」「自分,田中」「自分 田中」のどれでもよい。
PARTICIPANT_SEPARATORS = re.compile(r"[、，,\s・/]+")
MAX_PARTICIPANTS = 20
SPEAKER_LABEL = re.compile(r"話者(\d+):")


def parse_participants(raw: str) -> list[str]:
    """入力された参加者の名前を並べる. 同じ名前は1つにする."""
    names: list[str] = []
    for name in PARTICIPANT_SEPARATORS.split(raw.strip()):
        if name and name not in names:
            names.append(name)
    return names[:MAX_PARTICIPANTS]


def speaker_label(index: int) -> str:
    """0から始まる話者の番号を「話者1」の形にする."""
    return f"話者{index + 1}"


def split_speaker_map(text: str) -> tuple[str, dict[str, str]]:
    """清書の応答から「@@話者 …」の行を取り出す. (本文, {"話者1": "田中"}) を返す."""
    body_lines, mapping = [], {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(SPEAKER_MAP_PREFIX):
            body_lines.append(line)
            continue
        for pair in stripped[len(SPEAKER_MAP_PREFIX):].split(","):
            label, _, name = pair.partition("=")
            label, name = label.strip(), name.strip()
            if re.fullmatch(r"話者\d+", label) and name not in UNKNOWN_NAMES:
                mapping[label] = name
    return "\n".join(body_lines).strip(), mapping


def apply_speaker_names(lines: list[str], mapping: dict[str, str]) -> list[str]:
    """文字起こしの「話者1:」を「田中（話者1）:」にする. 名前のわからない話者はそのまま."""
    if not mapping:
        return lines

    def rename(match: re.Match[str]) -> str:
        label = f"話者{match.group(1)}"
        name = mapping.get(label)
        return f"{name}（{label}）:" if name else match.group(0)

    return [SPEAKER_LABEL.sub(rename, line, count=1) for line in lines]
