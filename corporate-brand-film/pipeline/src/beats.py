"""Script → Beatsheet.

나레이션 막의 길이는 글자수/발화속도*여백계수로 역산한다.
콜드오픈·타이틀은 고정. 합계가 TOTAL_SECONDS를 넘지 않도록 검증한 뒤,
남는 초를 콜드오픈에 흡수시켜 정확히 88초로 맞춘다.
컷 수는 막당 고정 배분(합계 33)이며 s/컷이 후반으로 갈수록 짧아지도록 설계돼 있다.
"""
import re
from dataclasses import dataclass

from src import constants as C
from src.script import Script

# 막별 컷 수 — 합계 33. reference/00_분석리포트.md 3절 표와 동일.
CUTS = {
    "cold_open": C.COLD_OPEN_CUTS,
    "title": C.TITLE_CUTS,
    "definition": 6,
    "evidence": 6,
    "pivot": 2,
    "chapter_0": 5,
    "chapter_1": 5,
    "climax": 3,
    "closing": 1,
}
FIXED_SECONDS = {"cold_open": C.COLD_OPEN_SECONDS, "title": C.TITLE_SECONDS}


@dataclass
class Beat:
    name: str
    start: float
    seconds: float
    cuts: int
    chars: int
    lines: list[str]
    narrated: bool


@dataclass
class Beatsheet:
    beats: list[Beat]

    @property
    def total_seconds(self) -> float:
        return sum(b.seconds for b in self.beats)

    @property
    def total_cuts(self) -> int:
        return sum(b.cuts for b in self.beats)

    @property
    def silent_ratio(self) -> float:
        silent = sum(b.seconds for b in self.beats if not b.narrated)
        return silent / self.total_seconds


def _chars(lines: list[str]) -> int:
    return len(re.sub(r"[\s,.?!]", "", "".join(lines)))


def build_beatsheet(script: Script) -> Beatsheet:
    unknown = [a.name for a in script.acts if a.name not in CUTS]
    if unknown:
        raise ValueError(
            f"컷 배분이 정의되지 않은 막: {', '.join(unknown)}. "
            f"이 포맷은 챕터 2개({C.TOTAL_CUTS}컷) 전용입니다. "
            f"챕터 수를 바꾸려면 beats.CUTS 와 constants.TOTAL_CUTS 를 함께 수정하세요."
        )

    raw: list[tuple[str, float, int, int, list[str], bool]] = []
    for act in script.acts:
        cuts = CUTS[act.name]
        chars = _chars(act.lines)
        if act.name in FIXED_SECONDS:
            secs = float(FIXED_SECONDS[act.name])
        else:
            # 글자수 비례항 + 세그먼트당 고정 오버헤드 (constants.py 참고 —
            # 실제 TTS 실측 회귀로 뽑은 두 항이다).
            secs = chars / C.SPEECH_RATE_PURE + C.SEGMENT_OVERHEAD
        raw.append((act.name, secs, cuts, chars, act.lines, act.narrated))

    total = sum(r[1] for r in raw)
    if total > C.TOTAL_SECONDS:
        raise ValueError(
            f"대본이 포맷보다 깁니다: {total:.0f}초 필요, {C.TOTAL_SECONDS}초 한도. "
            f"약 {(total - C.TOTAL_SECONDS) * C.SPEECH_RATE_PURE:.0f}자를 줄이세요."
        )

    # 남는 시간은 나레이션 막들에 고르게 나눠 준다 — 나레이션 사이의 호흡이
    # 되고, 실제 TTS 가 예상보다 길어졌을 때의 완충이기도 하다.
    #
    # 예전에는 전부 콜드오픈이 흡수했는데, 실측 모델로 바꾸니 콜드오픈이 17초로
    # 늘어 컷당 4.3초가 됐다. Veo 클립이 4초라 조립 단계에서 정지 프레임으로
    # 늘려야 하는데(assemble 의 tpad), 움직이는 드론 샷이 잠깐 멈추는 건
    # 눈에 띈다. 콜드오픈은 레퍼런스 28편에서 뽑은 고정 길이를 지키고,
    # 여유는 나레이션 쪽에 두는 편이 구조와 화면 양쪽에 낫다.
    # 나레이션 막이 하나도 없으면 남는 시간을 줄 곳이 없어 합계가 TOTAL_SECONDS
    # 에 못 미친다. 예전 모델(전부 콜드오픈이 흡수)은 이 경우에도 88초를
    # 보장했으므로, 조용히 짧은 완성본을 내놓지 않도록 여기서 막는다.
    narrated_names = [r[0] for r in raw if r[5]]
    if not narrated_names:
        raise ValueError(
            "나레이션 막이 하나도 없습니다 — 남는 시간을 배분할 곳이 없어 "
            f"{C.TOTAL_SECONDS}초를 채울 수 없습니다. "
            "script.py 의 narrated 플래그를 확인하세요.")
    share = (C.TOTAL_SECONDS - total) / len(narrated_names)

    beats, t = [], 0.0
    for name, secs, cuts, chars, lines, narrated in raw:
        if narrated:
            secs += share
        beats.append(Beat(name, t, secs, cuts, chars, lines, narrated))
        t += secs
    return Beatsheet(beats=beats)
