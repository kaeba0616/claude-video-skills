"""Beatsheet → Shotlist.

reference/00_분석리포트.md 4-2절에서 관찰된 샷사이즈 5종을 로테이션한다.
규칙: 각 막의 첫 컷은 와이드로 연다(섹션 오프닝), 이후 인접 컷은 절대 같은 사이즈를 쓰지 않는다.
"""
from dataclasses import dataclass

from src.beats import Beatsheet
from src.brief import Brief

SIZES = ("aerial_wide", "extreme_close", "person_medium", "cg_diagram", "studio_product")
# 와이드 다음에 도는 순서 (와이드는 막 첫 컷 전용)
CYCLE = ("extreme_close", "cg_diagram", "person_medium", "studio_product")


@dataclass
class Shot:
    index: int
    beat: str
    start: float
    seconds: float
    size: str
    subject: str
    label_ko: str | None
    label_en: str | None


@dataclass
class Shotlist:
    shots: list[Shot]


def _subjects_for(beat_name: str, brief: Brief) -> list[str]:
    if beat_name == "cold_open":
        return list(brief.cold_open_subjects)
    if beat_name.startswith("chapter_"):
        return list(brief.chapters[int(beat_name.split("_")[1])].subjects)
    return []


# 나레이션 막 중 챕터가 아닌 곳에 쓰는 범용 피사체 (brief.business를 문맥으로 씀)
GENERIC = {
    "title": ["slow push in on an empty modern factory atrium, morning light through high windows"],
    "definition": [
        "extreme aerial drone shot over an industrial complex at dawn, long shadows",
        "extreme macro of a razor-thin metal edge, single specular highlight",
        "3D style visualization of a city grid lighting up node by node in dark space",
        "medium shot of an engineer studying a monitor, face lit by cool screen light",
        "white cyclorama studio shot of a thin metal sheet suspended, rotating slowly",
        "extreme close up of a gloved hand lifting a foil sample against light",
    ],
    "evidence": [
        "aerial shot of a long factory roofline stretching to the horizon",
        # 글자를 부르는 피사체(명판·증서·눈금 숫자)는 쓰지 않는다 — Task 18 에서
        # "vintage rolling machine nameplate" 가 화면 한가운데 ROLLING MACHINE
        # 간판을 만들어냈다. negative_prompt 로 "글자 없음"을 요구하면서 피사체로
        # 글자가 적힌 물건을 지정하면 모순된 지시가 되고, Veo 는 피사체를 따른다.
        "extreme close up of worn painted steel machinery, chipped paint and rivets",
        "3D style visualization of two parallel lines converging to an impossibly fine gap in dark space",
        "medium shot of a quality inspector at a microscope in a bright lab",
        "white cyclorama studio shot of thin metal sheets stacked in a neat pile, soft gradient light",
        "extreme macro of a caliper closing on a foil edge",
    ],
    "pivot": [
        "wide shot of a dark factory floor, single work light switching on",
        "extreme close up of an eye reflecting a moving production line",
    ],
    "climax": [
        "extreme aerial shot of a container port at sunset, ships departing",
        "3D style visualization of glowing shipping routes spreading across a dark globe",
        "medium shot of workers walking toward camera through a bright factory corridor",
    ],
    "closing": ["slow drift across a clean dark surface, single soft highlight, empty negative space"],
}


def build_shotlist(bs: Beatsheet, brief: Brief) -> Shotlist:
    shots: list[Shot] = []
    idx = 1
    prev_size: str | None = None
    cycle_pos = 0

    for beat in bs.beats:
        subjects = _subjects_for(beat.name, brief) or GENERIC[beat.name]
        per = beat.seconds / beat.cuts
        t = beat.start
        for i in range(beat.cuts):
            if i == 0 and beat.name not in ("title", "closing"):
                size = "aerial_wide"
            else:
                size = CYCLE[cycle_pos % len(CYCLE)]
                cycle_pos += 1
                if size == prev_size:
                    size = CYCLE[cycle_pos % len(CYCLE)]
                    cycle_pos += 1
            subject = subjects[i % len(subjects)]

            label_ko = label_en = None
            if beat.name.startswith("chapter_") and i == 1:
                ch = brief.chapters[int(beat.name.split("_")[1])]
                label_ko, label_en = ch.title_ko, ch.title_en

            shots.append(Shot(idx, beat.name, t, per, size, subject, label_ko, label_en))
            prev_size = size
            t += per
            idx += 1

    # 불변식을 코드로 못박는다. 막 첫 컷은 무조건 aerial_wide 이므로, 컷이 1개뿐인
    # 막이 새로 생기면 그 막의 유일한 컷과 다음 막의 첫 컷이 둘 다 와이드가 되어
    # 조용히 규칙을 깬다. beats.CUTS 를 바꿨을 때 여기서 즉시 터지게 한다.
    for a, b in zip(shots, shots[1:]):
        if a.size == b.size:
            raise ValueError(
                f"인접 샷 사이즈 중복: #{a.index}({a.beat})와 #{b.index}({b.beat}) 둘 다 {a.size}. "
                f"beats.CUTS 에서 컷이 1개뿐인 막이 생겼는지 확인하세요."
            )

    return Shotlist(shots=shots)
