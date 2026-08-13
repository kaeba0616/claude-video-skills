"""brief YAML → Brief 데이터클래스."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REQUIRED = [
    "name_ko", "name_en", "business", "founded_year", "founded_place",
    "founded_origin", "slogan_ko", "slogan_en", "anchors", "opening_lines",
    "pivot_question", "climax_lines", "style_prefix", "cold_open_subjects",
    "chapters",
]


@dataclass
class Chapter:
    title_ko: str
    title_en: str
    lines: list[str]
    subjects: list[str]


@dataclass
class Brief:
    name_ko: str
    name_en: str
    business: str
    founded_year: int
    founded_place: str
    founded_origin: str
    slogan_ko: str
    slogan_en: str
    anchors: list[str]
    opening_lines: list[str]
    pivot_question: str
    climax_lines: list[str]
    style_prefix: str
    cold_open_subjects: list[str]
    chapters: list[Chapter] = field(default_factory=list)


def load_brief(path: str) -> Brief:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"필수 필드 누락: {', '.join(missing)}")
    chapters = [Chapter(**c) for c in raw.pop("chapters")]
    return Brief(chapters=chapters, **raw)
