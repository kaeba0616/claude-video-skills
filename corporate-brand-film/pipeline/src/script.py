"""Brief → 5막 나레이션 대본.

공식이 고정하는 문장(근거·전환·클로징)은 템플릿이고,
창작이 필요한 문장(은유 도입·챕터 본문·클라이맥스)은 brief에서 그대로 가져온다.
템플릿 문구는 reference/00_분석리포트.md 2-3 / 2-5 / 2-6절에서 추출한 것.
"""
import re
from dataclasses import dataclass

from src.brief import Brief

# 파이프라인 기준 연도 — 고정 상수 (Task 3: 비트시트, Task 9: TTS 등에서 참조)
CURRENT_YEAR = 2026

TPL_EVIDENCE_ORIGIN = "{year}년 {place}의 {origin}에서 시작해, {years}년간 단 하나의 기준을 지켰습니다."
TPL_EVIDENCE_ANCHOR = "{a0}. {a1}."
TPL_PIVOT = "하지만 우리는 멈추지 않았습니다."
TPL_CLOSING_SLOGAN = "{slogan}."
TPL_CLOSING_NAME = "{name}."


@dataclass
class Act:
    name: str
    lines: list[str]
    narrated: bool


@dataclass
class Script:
    acts: list[Act]
    brief: Brief

    @property
    def total_chars(self) -> int:
        text = "".join(l for a in self.acts for l in a.lines)
        return len(re.sub(r"[\s,.?!]", "", text))


def build_script(brief: Brief) -> Script:
    years = CURRENT_YEAR - brief.founded_year
    acts = [
        Act("cold_open", [], False),
        Act("title", [], False),
        Act("definition", list(brief.opening_lines), True),
        Act("evidence", [
            TPL_EVIDENCE_ORIGIN.format(
                year=brief.founded_year, place=brief.founded_place,
                origin=brief.founded_origin, years=years),
            TPL_EVIDENCE_ANCHOR.format(a0=brief.anchors[0], a1=brief.anchors[1]),
        ], True),
        Act("pivot", [TPL_PIVOT, brief.pivot_question], True),
    ]
    for i, ch in enumerate(brief.chapters):
        acts.append(Act(f"chapter_{i}", list(ch.lines), True))
    acts.append(Act("climax", list(brief.climax_lines), True))
    acts.append(Act("closing", [
        TPL_CLOSING_SLOGAN.format(slogan=brief.slogan_ko),
        TPL_CLOSING_NAME.format(name=brief.name_ko),
    ], True))
    return Script(acts=acts, brief=brief)
