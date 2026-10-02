"""録音中の議事録（下書き）の形と、Haikuが返す編集指示の読み取り.

Haikuには議事録を丸ごと書き直させず、変わる見出しだけを次の形で返させる。
会議が長くなっても1回の応答が短く済み、すぐ画面に出せる。

    @@ 決まったこと        ← この見出しの中身を丸ごと置き換える
    - 中間発表は10月9日
    @@+ 話し合ったこと     ← この見出しの末尾に書き足す
    - 田中さんが実験結果を報告した

応答は1文字ずつ届くので、途中まで届いた状態でも「いまどこを書いているか」と
「書きかけの中身」を出せるようにしてある。
"""

from __future__ import annotations

from dataclasses import dataclass

SECTIONS = ("要点", "話し合ったこと", "決まったこと", "宿題", "次回", "保留・質問")
# 画面に出す見出し. 宿題は何を書く欄かを見出しで伝える。
SECTION_LABELS = {"宿題": "宿題（誰が・何を・いつまでに）"}

REPLACE = "replace"
APPEND = "append"
# 書くことがない見出しにHaikuが入れてくる埋め草. 中身としては扱わない。
PLACEHOLDERS = frozenset({"なし", "（なし）", "(なし)", "特になし", "変更なし", "（変更なし）"})


@dataclass(frozen=True)
class EditOp:
    """見出し1つぶんの編集."""

    section: str
    mode: str
    text: str


def section_label(section: str) -> str:
    return SECTION_LABELS.get(section, section)


def _parse_header(line: str) -> tuple[str, str] | None:
    """「@@ 見出し」「@@+ 見出し」を (見出し, 置換/追記) にする. 見出し行でなければ None."""
    stripped = line.strip()
    if not stripped.startswith("@@"):
        return None
    mode = APPEND if stripped.startswith("@@+") else REPLACE
    name = stripped[3:] if mode == APPEND else stripped[2:]
    return name.strip(), mode


def _normalize_section(name: str) -> str | None:
    """見出しの名前を既知のものに寄せる. 「宿題（誰が…）」のような飾り付きも受ける."""
    for section in SECTIONS:
        if name == section or name.startswith(section):
            return section
    return None


def parse_ops(text: str, *, final: bool = True) -> list[EditOp]:
    """応答の文字列から編集を取り出す.

    final=False のときは届いている途中とみなし、最後の行がまだ書きかけの見出し（@@ だけ等）なら
    中身として扱わない。知らない見出しの中身と、見出しより前の前置きは捨てる。
    """
    lines = text.split("\n")
    if not final and lines and lines[-1].lstrip().startswith("@"):
        lines = lines[:-1]

    ops: list[EditOp] = []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            kept = [line for line in body if line.strip().lstrip("-・ ").strip() not in PLACEHOLDERS]
            ops.append(EditOp(section=current[0], mode=current[1], text="\n".join(kept).strip()))

    for line in lines:
        header = _parse_header(line)
        if header is None:
            if current is not None:
                body.append(line.rstrip())
            continue
        flush()
        section = _normalize_section(header[0])
        current = (section, header[1]) if section else None
        body = []
    flush()
    return ops


def apply_ops(draft: dict[str, str], ops: list[EditOp]) -> dict[str, str]:
    """編集を当てた新しい下書きを返す. 元の下書きは変えない."""
    updated = dict(draft)
    for op in ops:
        if op.mode == APPEND:
            if op.text:
                updated[op.section] = "\n".join(filter(None, [updated.get(op.section, "").rstrip(), op.text]))
        else:
            updated[op.section] = op.text
    return updated


def editing_section(ops: list[EditOp]) -> str | None:
    """いま書いている途中の見出し（最後の編集）."""
    return ops[-1].section if ops else None


def render_markdown(draft: dict[str, str]) -> str:
    """下書きをMarkdownにする. 清書の材料と、清書に失敗したときの代わりに使う."""
    blocks = [f"## {section_label(s)}\n\n{draft[s].strip()}" for s in SECTIONS if draft.get(s, "").strip()]
    return "\n\n".join(blocks)
