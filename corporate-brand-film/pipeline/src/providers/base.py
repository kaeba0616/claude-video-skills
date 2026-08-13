"""영상·음성 생성 Provider 프로토콜과 테스트용 Fake 구현."""
import re
import subprocess
from pathlib import Path
from typing import Protocol

from src import constants as C


class BlockedError(Exception):
    """안전필터에 의해 생성이 차단됨. 과금되지 않는다."""


class AmbiguousSubmissionError(Exception):
    """제출 요청(예: Veo generate_videos) 도중 오류가 나서 요청이 서버에 실제로
    도달했는지(=이미 과금됐는지) 알 수 없는 상태. 안전필터 차단(BlockedError,
    과금 안 됨)과 달리 이미 돈이 나갔을 수 있으므로, 자동으로 재시도·재제출하면
    안 되고 사람이 확인해야 한다."""


class TerminalGenerationError(Exception):
    """provider가 종결된 실패(터미널 상태 — 예: status=="failed",
    operation.error)에 도달했다는 뜻이다. 더 기다리거나 다시 조회해도 같은
    결과만 나오는 완전히 끝난 상태라 provider는 pending 항목을 이미 정리했다.
    하지만 BlockedError(안전필터 차단, 과금 안 됨)와는 다르다 — 이 실패는
    "과금은 이미 됐다"는 뜻이다(bizrouter의 cost_krw는 접수 시점에 확정
    청구되고, failed도 환불이 아니다). AmbiguousSubmissionError(과금 여부
    불명)와도 다르다 — 이건 과금 여부가 불명한 게 아니라 확실히 과금됐다는
    뜻이다. 세 예외는 서로 다른 의미이므로 절대 섞이면 안 된다.

    generate_clips는 이 예외를 재시도하지 않는다 — 재시도하면 이미 끝난
    실패에 새 제출(=새 과금)을 또 낸다(Task 15 리뷰 Critical 1)."""


class RejectedSubmissionError(Exception):
    """서버가 제출 요청을 4xx(429 제외)로 명확히 거절했다 — 요청이 도달했고,
    작업은 만들어지지 않았고, 과금되지 않았다.

    AmbiguousSubmissionError(도달 여부 불명, 과금됐을 수 있음)와 혼동하면 안
    된다. 거절은 애매하지 않다: 고쳐야 할 건 요청 내용이고, 같은 요청을 다시
    보내면 같은 거절만 돌아온다. 그래서 generate_clips는 재시도하지 않고,
    provider는 sentinel을 남기지 않는다(남기면 고칠 것도 없는 샷이 막힌다).

    TerminalGenerationError(확실히 과금됨)와도 다르다 — 이건 과금 0원이다.
    지출 보고가 정확하려면 둘을 같은 버킷에 넣으면 안 된다.

    (Task 17에서 veo-3.1-lite 1컷 비교 중 발견: 400을 받았는데 애매로
     분류돼 "과금됐을 수 있다"는 틀린 경고가 나왔고, 거절 사유가 담긴
     응답 본문은 버려져서 무엇이 잘못됐는지 알 수 없었다.)"""


class BudgetExceededError(Exception):
    """제출(POST) 전에 예산 가드가 "누적 청구액 + 이번 샷 예상 비용"이 사용자가
    설정한 원화 상한을 넘는다고 판단해 제출을 막았다. 로컬 계산이라 재시도해도
    같은 결과만 나온다 — generate_clips는 이 예외를 재시도하지 않고 즉시 전체
    실행을 중단한다(POST가 애초에 나가지 않았으므로 안전하게 멈출 수 있다)."""


# Veo 가 실제로 내보내는 프레임 크기. Fake 도 반드시 같은 크기를 내보내야
# Task 10 의 1920x1080 리스케일 체인이 테스트에서 실제로 동작한다.
# Fake 가 곧장 1080p 를 뱉으면 scale 필터가 no-op 이 되어, 리스케일이 깨져 있어도
# 모든 Fake 테스트가 통과하고 첫 유료 실행에서야 터진다.
RESOLUTION_SIZE = {"720p": (1280, 720), "1080p": (1920, 1080), "4k": (3840, 2160)}


class VideoProvider(Protocol):
    def generate(self, prompt: str, out_path: Path, duration: int,
                 resolution: str, aspect: str,
                 reference_images: list[Path] | None) -> None: ...


class TTSProvider(Protocol):
    def synthesize(self, text: str, out_path: Path, voice: str) -> None: ...


class FakeVideoProvider:
    """API 없이 파이프라인 전 구간을 돌리기 위한 컬러바 생성기."""

    def __init__(self, block_prompts: list[str] | None = None):
        self.block_prompts = block_prompts or []
        self.calls: list[str] = []

    def generate(self, prompt: str, out_path: Path, duration: int,
                 resolution: str, aspect: str,
                 reference_images: list[Path] | None) -> None:
        if any(b in prompt for b in self.block_prompts):
            raise BlockedError(prompt)
        if resolution not in RESOLUTION_SIZE:
            raise ValueError(
                f"알 수 없는 해상도: {resolution!r}. "
                f"지원: {', '.join(RESOLUTION_SIZE)}"
            )
        w, h = RESOLUTION_SIZE[resolution]
        # 씬 검출(ffmpeg select=gt(scene,...))은 휘도(밝기) 차이만 본다 —
        # 실측 결과 hue 회전(색상만 바꿈)은 인접 프레임의 scene_score가
        # 0에 가깝게 나와 컷으로 전혀 잡히지 않았다. 밝기를 호출 순번 홀짝으로
        # 크게 뒤집으면 인접 클립이 반드시 크게 갈라진다.
        brightness = 0.35 if len(self.calls) % 2 == 0 else -0.35
        self.calls.append(prompt)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"testsrc=size={w}x{h}:rate={C.FPS}:duration={duration}",
            "-vf", f"eq=brightness={brightness}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out_path),
        ], check=True)


class FakeTTSProvider:
    """API 없이 나레이션 트랙을 만들기 위한 가청 톤(사인파) 생성기.

    seconds 를 주면 그 길이로 고정하고, 주지 않으면 실제 TTS 처럼
    글자수/발화속도로 길이를 낸다 — 고정 길이면 막별 세그먼트가 전부
    같은 길이가 돼서 길이 초과 버그를 Fake 로 잡을 수 없다.
    무음(anullsrc)이 아니라 낮은 볼륨의 사인파를 내보낸다 — silencedetect
    기반 무나레이션 비율 계측을 Fake로 검증하려면 "소리가 있다"는 성질
    자체가 필요하다.
    """

    def __init__(self, seconds: float | None = None, rate: float = C.SPEECH_RATE):
        self.seconds = seconds
        self.rate = rate

    def duration_for(self, text: str) -> float:
        if self.seconds is not None:
            return self.seconds
        chars = len(re.sub(r"[\s,.?!]", "", text))
        return max(chars / self.rate, 0.1)

    def synthesize(self, text: str, out_path: Path, voice: str) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=220:r=48000",
            "-t", f"{self.duration_for(text):.3f}",
            "-af", "volume=0.3", "-c:a", "pcm_s16le", str(out_path),
        ], check=True)
