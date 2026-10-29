"""議事録の画面で、場面（ゼミ・授業・打ち合わせ・ひとりメモ）と授業を選ぶ欄."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from companion.config import get_config
from companion.services.minutes_scenes import (
    LECTURE,
    SCENES,
    clean_course,
    clean_kind,
    course_name,
    get_scene,
    list_courses,
)


@dataclass(frozen=True)
class SceneChoice:
    """画面から選ばれた場面と授業（確かめたあと）."""

    kind: str
    course: str

    def default_title(self) -> str:
        """題名が空のときの題名. 授業は授業名にする."""
        return course_name(self.course) if self.course else get_scene(self.kind).default_title


def read_choice(kind: str, course: str) -> SceneChoice:
    """画面から届いた場面と授業を確かめる. 授業の一覧に無いもの（保管庫の外を指すものも）は受けない."""
    cleaned = clean_kind(kind)
    return SceneChoice(cleaned, clean_course(get_config().notes_dir, cleaned, course))


def scene_context() -> dict[str, Any]:
    """場面を選ぶ欄に渡すもの."""
    return {
        "scenes": [(s.key, s.label, s.default_title) for s in SCENES.values()],
        "courses": list_courses(get_config().notes_dir),
        "lecture_kind": LECTURE,
    }
