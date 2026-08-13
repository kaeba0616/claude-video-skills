"""샷리스트 전체를 Provider로 생성한다.

- 이미 존재하는 클립은 건너뛴다 (이어받기)
- 일시적 오류는 재시도
- 안전필터 차단은 기록만 하고 계속 진행 (차단은 과금되지 않는다)
- 제출이 성공했는지 알 수 없는 애매한 오류(AmbiguousSubmissionError)도 기록만
  하고 계속 진행하지만, blocked와는 절대 합치지 않는다 — 차단은 과금되지
  않지만 애매한 제출은 이미 과금됐을 수 있어서 의미가 정반대다.
- provider가 종결된 실패(TerminalGenerationError — 이미 과금됐고 더 기다려도
  같은 결과다)를 던지면 마찬가지로 재시도하지 않고 기록만 한다. blocked/
  ambiguous 어느 쪽과도 합치지 않는다: blocked=과금 안 됨, ambiguous=과금
  여부 불명, terminal=확실히 과금됨. 이 구분을 섞으면(Task 15 리뷰 Critical
  1) 이미 과금된 실패가 "일시적 오류"로 오인되어 generate_clips 자체의
  재시도 루프가 매번 새 제출(=새 과금)을 낸다.
- BudgetExceededError는 로컬 계산(제출 전)이라 재시도해도 같은 결과만 나온다
  — 재시도하지 않고 즉시 전체 실행을 중단한다.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from src import constants as C
from src.brief import Brief
from src.prompts import build_prompt
from src.providers.base import (AmbiguousSubmissionError, BlockedError,
                                BudgetExceededError, RejectedSubmissionError,
                                TerminalGenerationError, VideoProvider)
from src.shotlist import Shotlist


@dataclass
class GenerateResult:
    made: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    blocked: list[int] = field(default_factory=list)
    ambiguous: list[int] = field(default_factory=list)
    terminal: list[int] = field(default_factory=list)
    rejected: list[int] = field(default_factory=list)


def generate_clips(shotlist: Shotlist, brief: Brief, provider: VideoProvider,
                   out_dir: Path, reference_images: list[Path] | None = None,
                   max_retries: int = 2) -> GenerateResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = GenerateResult()
    tmp_dir = out_dir / ".tmp"

    try:
        for shot in shotlist.shots:
            target = out_dir / f"{shot.index:02d}.mp4"
            if target.exists():
                result.skipped.append(shot.index)
                continue

            # 생성은 .tmp 디렉토리에 하고 성공했을 때만 원자적으로 최종 위치로 이동한다.
            # 중간에 죽어도 최종 이름에는 잘린 파일이 남지 않으므로,
            # 다음 실행의 exists() 건너뛰기가 깨진 클립을 통과시키지 않는다.
            tmp_dir.mkdir(parents=True, exist_ok=True)
            partial = tmp_dir / f"{shot.index:02d}.mp4"
            prompt = build_prompt(shot, brief)
            try:
                for attempt in range(max_retries + 1):
                    try:
                        provider.generate(prompt, partial, C.VEO_DURATION,
                                          C.VEO_RESOLUTION, C.VEO_ASPECT, reference_images)
                        partial.replace(target)
                        result.made.append(shot.index)
                        break
                    except BlockedError:
                        result.blocked.append(shot.index)
                        break
                    except AmbiguousSubmissionError:
                        # 재시도하면 진짜로 두 번 과금될 수 있으므로 여기서는
                        # 절대 재시도하지 않는다 — 기록만 하고 다음 샷으로 넘어간다.
                        result.ambiguous.append(shot.index)
                        break
                    except RejectedSubmissionError:
                        # 서버가 명확히 거절했다(4xx) — 과금 0원. 같은 요청을
                        # 다시 보내봐야 같은 거절만 돌아오므로 재시도하지
                        # 않는다. 과금된 terminal 과 같은 버킷에 넣으면 지출
                        # 보고가 틀어지므로 따로 기록한다.
                        result.rejected.append(shot.index)
                        break
                    except TerminalGenerationError:
                        # 이미 과금된 종결 실패다 — 재시도하면 새 제출(=새
                        # 과금)이 나간다. 기록만 하고 다음 샷으로 넘어간다.
                        result.terminal.append(shot.index)
                        break
                    except BudgetExceededError:
                        # 로컬 계산이라 재시도해도 같은 결과만 나온다 — 재시도는
                        # 시도만 낭비하고 리포트만 흐리게 만든다. 예산은 이번
                        # 실행 전체에 적용되는 상한이므로 이 샷만 건너뛰지 않고
                        # 즉시 전체 실행을 중단한다(POST는 애초에 나가지 않았으
                        # 므로 안전하게 멈출 수 있다).
                        raise
                    except Exception:
                        if attempt == max_retries:
                            raise
            finally:
                partial.unlink(missing_ok=True)
    finally:
        # 중간에 터져도 무엇이 만들어졌고 무엇이 막혔는지는 남겨야 한다.
        report = {"made": result.made, "skipped": result.skipped,
                  "blocked": result.blocked, "ambiguous": result.ambiguous,
                  "terminal": result.terminal,
                  "rejected": result.rejected}
        # provider가 실제 청구액을 추적하면(bizrouter — spent_krw 속성) 리포트에
        # 반영한다. 이 값은 실패/종결(terminal)로 끝난 샷의 비용도 포함한다
        # (접수 시점에 확정 청구되므로) — 사용자가 내부 pending.json을 열어보지
        # 않아도 실제로 얼마를 썼는지 알 수 있어야 한다. Fake/Gemini처럼 원화
        # 청구액 개념이 없는 provider는 이 속성이 없으므로 조용히 생략된다
        # (VideoProvider Protocol에 없는 선택적 속성 — duck typing).
        spent_krw = getattr(provider, "spent_krw", None)
        if spent_krw is not None:
            report["spent_krw"] = spent_krw
        (out_dir / "_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    return result
