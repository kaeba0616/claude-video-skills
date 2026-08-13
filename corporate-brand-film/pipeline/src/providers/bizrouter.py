"""bizrouter API Provider — Veo 3.1(bizrouter 우회) 영상 생성 어댑터.

엔드포인트/스키마는 추정이 아니다: `GET /v1/models` 조회와 공식 문서
(bizrouter.ai/docs/video-output, 2026-08 확인)로 확보된 값이다.

    POST /v1/video/generation               → 202 {operation_id, status, cost_krw}
    GET  /v1/video/generation/:id           → {status, video_uri, duration_seconds, cost_krw}
    GET  /v1/video/generation/:id/download  → video/mp4 바이너리

Task 13(gemini.py)에서 세 라운드에 걸쳐 다듬어진 이중 과금 방어 규율을 그대로
따른다 — provider가 바뀐다고 완화될 이유가 없다:

- 제출(POST) 직전에 sentinel을 pending.json에 남긴다. POST 자체가 예외를
  던지면 서버에 도달했는지 알 수 없으므로 sentinel을 그대로 두고
  AmbiguousSubmissionError를 던진다 — 자동 재제출은 절대 하지 않는다.
- POST가 202로 응답하면 operation_id·cost_krw·submitted_at을 폴링 진입 전에
  즉시 기록한다(Ctrl-C/OOM 등으로 폴링 도중 죽어도 이어받을 수 있도록).
- pending 항목은 다운로드가 완전히 기록되고 검증된 뒤에만 지운다. 다운로드
  실패는 이미 과금된 완료 결과를 다시 받아야 하므로 항목을 남겨 다음 호출이
  재제출이 아니라 재다운로드로 이어받게 한다.
- status=="failed"는 다르다 — 더 기다려도 같은 결과만 나오는 종결 상태라
  in-flight가 아니다. pending을 즉시 지워야 이 샷이 옛 실패에 영원히 묶이지
  않는다. 단 cost_krw는 접수 시점에 이미 청구됐으므로(failed != 환불) 별도의
  누적 지출 장부(_total_spent_krw)에는 반드시 남는다.

bizrouter 고유의 위험 두 가지:

1. **30분 다운로드 창.** Gemini 직결(2일)보다 훨씬 짧다. completed 관찰 시각을
   pending에 기록해두고, 그 뒤 30분이 지나 이어받으면(=다운로드를 다시 시도할
   기회조차 없다고 판단되면) 조용히 재제출하지 않고 사람이 읽을 수 있는
   한글 에러를 던진다 — 이미 과금됐고 서버 파일은 사라졌으므로 재생성은
   재과금이다.
2. **failed도 과금된다.** cost_krw는 접수(POST) 시점에 확정 청구되므로,
   `_submit()`에서 202 응답을 받는 즉시(성공/실패 여부와 무관하게) 누적 지출
   장부에 더한다 — 같은 cost_krw를 두 번 더하지 않는다(이중 계산 방지). 만약
   202 응답에 cost_krw가 없었다면(Important 3, Task 15 리뷰) 그 자리에서
   0원으로 조용히 넘기지 않고 크게 실패시키며, 이후 폴링 응답에서 cost_krw를
   알게 되는 첫 순간에 딱 한 번 장부에 반영한다.

세 번째 상태 — 종결된 실패(TerminalGenerationError, Task 15 리뷰 Critical 1):
`status=="failed"`(안전/정책 사유가 아닌 경우)는 pending을 정리하되, 절대
평범한 RuntimeError로 두지 않는다. generate_clips는 평범한 예외를 "일시적
오류"로 보고 최대 max_retries번 재시도하는데, pending이 이미 비어 있으니
매 재시도가 새 POST(=새 과금)로 이어진다. TerminalGenerationError는
BlockedError(차단, 과금 안 됨)·AmbiguousSubmissionError(과금 여부 불명)와도
다른, "확실히 과금된 종결 실패"라는 세 번째 의미이고 generate_clips가
이것도 재시도하지 않는다. 동일한 결함이 gemini.py의 operation.error 경로에도
있었다 — 둘 다 고쳤다.
"""
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src import constants as C
from src.providers.base import (AmbiguousSubmissionError, BlockedError,
                                BudgetExceededError, RejectedSubmissionError,
                                RESOLUTION_SIZE, TerminalGenerationError)

# Veo 3.1 제약(공식 문서로 확인, 2026-08): durationSeconds는 4/6/8만 허용되고,
# 1080p 이상 해상도는(=4/6초에는 허용되지 않는 해상도는) duration=8을 강제한다.
# 과금 제출 전에 여기서 막아야 잘못된 조합 하나로 돈을 날리지 않는다.
_VALID_DURATIONS = (4, 6, 8)
_FORCED_8S_RESOLUTIONS = ("1080p", "4k")

# reference_images는 최대 3장·8초 전용 — Task 13이 SDK 소스로 추론했던 결론이
# 이번엔 공식 문서로 독립 확인됐다(더 이상 "보고가 있다" 수준의 추정이 아니다).
_MAX_REFERENCE_IMAGES = 3

# pending.json 안에서 샷별 항목(파일명 키, 예: "01.mp4")과 겹치지 않도록
# 밑줄로 시작하는 예약 키 — 이번 실행에 국한되지 않고 이 pending.json이
# 살아있는 동안 누적된 실제 청구액 총합을 담는다. 개별 샷 항목은
# (다운로드 완료·터미널 실패 등으로) pending에서 지워지지만, 이 총합은
# 지워지지 않는다 — 예산 가드는 "지금 진행 중인 것"이 아니라 "지금까지
# 실제로 낸 돈"을 기준으로 판단해야 한다.
_TOTAL_SPENT_KEY = "_total_spent_krw"

# 제출은 했지만 응답을 받았는지 모르는 sentinel 상태는 별도 문자열 상수
# 대신 {"operation_id": None, ...} 딕셔너리로 표현한다 — operation_id가
# None이면 sentinel이다(아래 generate()/_submit() 참고).

# GET 응답이 담을 수 있는 실패 사유 필드(status/error/reason/message 등,
# 정확한 실제 형태는 실호출 전까지 검증되지 않았다) 전체를 문자열로 뒤져서
# 정책/안전 차단을 시사하는 표현이 있는지 본다. gemini.py의 _is_safety_error와
# 같은 원칙: 애매하면 차단으로 분류해서 같은 프롬프트에 두 번 돈을 쓰지 않는다.
_SAFETY_ERROR_MARKERS = ("safety", "blocked", "policy", "prohibited", "content_policy",
                         "moderation")

_AMBIGUOUS_SUBMISSION_MSG = (
    "제출 요청(POST /v1/video/generation) 도중 오류가 나서 요청이 실제로 "
    "bizrouter 서버에 도달했는지 알 수 없습니다 — 도달했다면 이미 과금됐을 "
    "수 있습니다. 이 샷은 자동으로 재시도/재제출하지 않습니다. 확인 후 다시 "
    "시도하려면(추가 과금 가능) {pending_path} 에서 '{key}' 항목을 직접 지운 "
    "뒤 재실행하세요."
)

_AMBIGUOUS_RESUME_MSG = (
    "'{key}' 항목이 이전 실행에서 제출 도중(제출됐는지 모르는 상태로) 끊긴 "
    "채로 남아 있습니다 — 진짜 operation_id를 모르므로 이어받을 수도 "
    "없습니다. 자동으로 재제출하지 않습니다: 실제로는 이미 과금된 작업이 "
    "진행 중일 수도 있기 때문입니다. 다시 시도하려면(추가 과금 가능) "
    "{pending_path} 에서 '{key}' 항목을 직접 지우고 재실행하세요."
)

_WINDOW_EXPIRED_MSG = (
    "'{key}' 항목의 다운로드 창(30분)이 만료됐습니다. 이 영상은 이미 "
    "과금됐고(operation_id={operation_id}) bizrouter 서버에서 삭제됐을 "
    "것입니다. 자동으로 재생성하지 않습니다 — 재생성하면 추가로 과금됩니다. "
    "정말 다시 만들고 싶으면 {pending_path} 에서 '{key}' 항목을 직접 지우고 "
    "재실행하세요(재과금)."
)

_BUDGET_EXCEEDED_MSG = (
    "예산 초과: 누적 청구액 {spent}원 + 이번 샷 예상 {estimated}원이 상한 "
    "{budget}원을 넘습니다. 제출하지 않고 중단합니다."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_safety_error(data) -> bool:
    text = json.dumps(data, ensure_ascii=False, default=str).lower() \
        if isinstance(data, dict) else str(data).lower()
    return any(marker in text for marker in _SAFETY_ERROR_MARKERS)


def _encode_reference_image(path: Path) -> str:
    # bizrouter 문서에 reference_images의 정확한 인코딩(base64 문자열 vs.
    # data URI vs. 업로드 URL)이 명시돼 있지 않다 — 가장 흔한 REST 관례인
    # base64 원본 바이트를 쓴다. Task 15는 이 경로를 실제로 호출하지 않으므로
    # (Step 3 테스트는 전부 로컬 검증/거부 시나리오다) 이 인코딩은 검증되지
    # 않은 채로 남는다 — 실제로 reference_images를 쓰는 첫 실호출 전에
    # 문서를 다시 확인해야 한다.
    return base64.b64encode(path.read_bytes()).decode("ascii")


# BudgetExceededError/TerminalGenerationError는 src/providers/base.py에 있다
# (generate.py도 provider별로 다른 모듈을 알 필요 없이 이 두 예외를 공통으로
# import해서 재시도 정책을 결정해야 하므로 — Task 15 리뷰 Critical 1).
# 여기서는 위 import로 이름만 재노출한다(기존 `from src.providers.bizrouter
# import BudgetExceededError` 사용처와의 하위호환).


class BizrouterVideoProvider:
    """bizrouter 경유 Veo 3.1 text-to-video 어댑터."""

    def __init__(self, api_key: str, model: str = C.BIZROUTER_VIDEO_MODEL,
                 pending_path: Path | None = None, poll_seconds: int = 10,
                 timeout_seconds: int = 420, budget_krw: int | None = None,
                 total_shots: int | None = None):
        self.api_key = api_key
        self.model = model
        self.pending_path = Path(pending_path) if pending_path else None
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.budget_krw = budget_krw
        self.total_shots = total_shots
        self._shots_seen = 0

    # --- pending.json (원자적 읽기/쓰기) ---

    def _pending(self) -> dict:
        if self.pending_path and self.pending_path.exists():
            return json.loads(self.pending_path.read_text())
        return {}

    def _write_pending(self, data: dict) -> None:
        """pending.json을 원자적으로(temp write + rename) 갱신한다 — 이 파일이
        "지금 서버에 어떤 유료 작업이 떠 있는지"의 유일한 기록이다. 쓰는 도중
        죽어서 반쯤 잘린 JSON이 남으면 다음 실행이 진행 중인 유료 작업 전체를
        잃어버리고 그대로 재제출해서 이중 과금을 낸다."""
        if not self.pending_path:
            return
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pending_path.with_name(self.pending_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.pending_path)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def spent_krw(self) -> int:
        """지금까지 이 pending.json이 살아있는 동안 실제로 청구된 총액(원).
        Important 2(Task 15 리뷰): 이전엔 pending.json 내부에만 있어서
        BudgetExceededError 메시지 바깥에서는 사용자가 볼 방법이 없었다 —
        generate.py/cli.py가 이 속성을 duck-typing으로 읽어(getattr) _report.json
        과 CLI 요약 줄에 노출한다. 매번 파일을 다시 읽는다 — provider 인스턴스가
        여러 개일 수 있고(예: 재시도), 이 값의 유일한 진실 원천은 파일이다."""
        return self._pending().get(_TOTAL_SPENT_KEY, 0)

    # --- 제출 전 로컬 검증 ---

    def _validate(self, duration: int, resolution: str,
                  reference_images: list[Path] | None) -> None:
        if resolution not in RESOLUTION_SIZE:
            raise ValueError(
                f"알 수 없는 해상도: {resolution!r}. "
                f"지원: {', '.join(RESOLUTION_SIZE)}"
            )
        if duration not in _VALID_DURATIONS:
            raise ValueError(
                f"bizrouter(Veo)는 duration_seconds로 {_VALID_DURATIONS}만 "
                f"허용합니다 (요청: {duration}초). 과금 전에 로컬에서 막습니다.")
        if resolution in _FORCED_8S_RESOLUTIONS and duration != 8:
            raise ValueError(
                f"bizrouter(Veo)는 {resolution} 해상도에서 8초 길이만 지원합니다 "
                f"(요청: {duration}초). 과금 전에 로컬에서 막습니다.")
        if reference_images and duration != 8:
            raise ValueError(
                "bizrouter(Veo)의 reference_images는 8초 길이에서만 동작합니다 "
                f"(요청: {duration}초). 과금 후 400 오류를 맞는 것보다 미리 "
                "막습니다.")
        if reference_images and len(reference_images) > _MAX_REFERENCE_IMAGES:
            raise ValueError(
                f"bizrouter(Veo)의 reference_images는 최대 {_MAX_REFERENCE_IMAGES}장까지만 "
                f"지원합니다(받은 개수: {len(reference_images)}). 조용히 잘라서 "
                "보내면 사용자가 의도한 이미지 중 일부가 빠졌는지도 모른 채 "
                "과금하게 되므로, 여기서 막습니다.")
        if self.model not in C.BIZROUTER_KRW_PER_SEC:
            # Important 3(Task 15 리뷰): 가격표에 없는 모델을 조용히 0원으로
            # 취급하면 예산 가드가 사실상 무력화된다("spent + 0 <= budget"은
            # 항상 참이라 상한을 넘는 제출도 그냥 통과한다). 가격을 모르는
            # 모델은 예산 가드 여부와 무관하게 아예 제출을 막는다.
            raise ValueError(
                f"알 수 없는 model 가격: {self.model!r}. BIZROUTER_KRW_PER_SEC에 "
                "없는 모델은 예산 가드가 무력화되므로(추정 비용이 조용히 0원 "
                f"처리됨) 제출하지 않습니다. 지원: {', '.join(C.BIZROUTER_KRW_PER_SEC)}")

    # --- 예산 가드 (제출 전, POST 호출 0회로 차단) ---

    def _check_budget(self, duration: int, pending: dict) -> None:
        if self.budget_krw is None:
            return
        # _validate()가 이미 self.model이 가격표에 있음을 보장했다 — 여기서
        # .get(..., 0)으로 다시 조용히 0원 처리하지 않는다.
        estimated = duration * C.BIZROUTER_KRW_PER_SEC[self.model]
        spent = pending.get(_TOTAL_SPENT_KEY, 0)
        if spent + estimated <= self.budget_krw:
            return
        msg = _BUDGET_EXCEEDED_MSG.format(spent=spent, estimated=estimated,
                                          budget=self.budget_krw)
        if self.total_shots is not None:
            # 이 샷 포함, 이번 실행에서 더 이상 시도되지 않을 샷 수.
            remaining = self.total_shots - self._shots_seen + 1
            msg += f" 남은 샷 {remaining}개는 생성되지 않았습니다."
        raise BudgetExceededError(msg)

    # --- 제출 ---

    def _submit(self, prompt: str, duration: int, resolution: str, aspect: str,
                reference_images: list[Path] | None, key: str,
                pending: dict) -> tuple[str, dict]:
        body = {
            "model": self.model,
            "prompt": prompt,
            "duration_seconds": duration,
            "resolution": resolution,
            "aspect_ratio": aspect,
        }
        # 부정어는 긍정 prompt 문자열에 섞지 않는다(prompts.py 참고) — Veo 전용
        # negative_prompt 필드로 보낸다. 단 모델별로 지원 여부가 다르다:
        # veo-3.1-lite 는 이 필드가 있으면 400 으로 거절한다(Task 17 실측).
        # 지원하지 않는 모델에 억지로 넣으면 요청 자체가 죽고, 빼면 화면에 글자가
        # 섞여 들어올 수 있다 — 어느 쪽이든 조용히 넘어가면 안 되므로 모델별
        # 지원 여부를 상수로 명시하고 여기서 분기한다.
        if self.model in C.BIZROUTER_NEGATIVE_PROMPT_MODELS:
            body["negative_prompt"] = C.VEO_NEGATIVE_PROMPT
        if reference_images:
            # _validate()가 이미 개수(<=3)와 duration(==8)을 검증했다 — 여기서
            # 다시 자르지 않는다(자르면 사용자가 의도한 이미지가 조용히
            # 빠진 채로 과금될 수 있다).
            body["reference_images"] = [
                _encode_reference_image(p) for p in reference_images]

        # 제출 직전에 sentinel을 남긴다 — POST가 응답 전에 죽으면(클라이언트
        # 타임아웃, 프로세스 강제종료 등) 요청이 서버에 도달했는지 알 방법이
        # 없다. sentinel이 없으면 다음 실행이 그대로 재제출해서 중복 과금이
        # 날 수 있다.
        submitted_at = _now_iso()
        pending[key] = {"operation_id": None, "cost_krw": None,
                        "submitted_at": submitted_at}
        self._write_pending(pending)
        try:
            resp = requests.post(f"{C.BIZROUTER_BASE}/video/generation",
                                 json=body, headers=self._headers())
            # 4xx(429 제외)는 "애매"가 아니다 — 요청이 서버에 도달했고 서버가
            # 명확히 거절했다는 뜻이다. 작업이 만들어지지 않았으니 과금되지도
            # 않는다. 이걸 AmbiguousSubmissionError로 뭉뚱그리면 sentinel이
            # 남아 샷이 막히고, 사용자에게 "과금됐을 수 있다"는 틀린 경고를
            # 준다. 실제로 고쳐야 할 건 요청 내용이므로, 응답 본문(=이유가
            # 적힌 곳)을 반드시 메시지에 실어 보낸다.
            # (Task 17 Lite 1컷 비교에서 발견 — 400을 받고도 이유를 알 수
            #  없었고, 과금되지 않았는데 애매로 분류됐다.)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                pending.pop(key, None)
                self._write_pending(pending)
                raise RejectedSubmissionError(
                    f"bizrouter가 생성 요청을 거절했습니다 "
                    f"(HTTP {resp.status_code}, model={self.model}, "
                    f"duration={duration}, resolution={resolution}). "
                    f"과금되지 않았습니다. 응답: {resp.text[:1000]}")
            resp.raise_for_status()
            data = resp.json()
            # Minor 6(Task 15 리뷰): operation_id 접근을 이 try 블록 안에 둔다 —
            # 202가 왔는데 응답에 operation_id가 없는(스키마 위반) 경우, try
            # 밖에서 KeyError를 던지면 sentinel이 우연히 그대로 남아서 다음
            # 이어받기 때 "어쩌다" AmbiguousSubmissionError로 분류될 뿐이다.
            # 여기서 즉시, 의도적으로 AmbiguousSubmissionError를 던진다 —
            # 202를 받았다는 건 서버에 도달은 했다는 뜻이라 이미 과금됐을 수
            # 있다.
            op_id = data["operation_id"]
        except RejectedSubmissionError:
            raise          # 거절은 애매하지 않다 — 아래에서 다시 감싸지 않는다.
        except Exception as e:
            raise AmbiguousSubmissionError(_AMBIGUOUS_SUBMISSION_MSG.format(
                pending_path=self.pending_path, key=key)) from e

        # 202가 성공적으로 반환됐다 — 이제 진짜 operation_id를 안다. cost_krw는
        # 접수 시점에 확정 청구되므로(실패해도 환불되지 않는다) 성공/실패
        # 분기와 무관하게 지금 누적 지출 장부에 더한다 — 여기서 딱 한 번만.
        #
        # Important 3(Task 15 리뷰): 응답에 cost_krw가 없다고 .get(..., 0)으로
        # 조용히 0원 처리하면 예산 가드가 무력화된다. 하지만 이 시점엔 이미
        # POST가 서버에 도달해서 operation_id까지 받았다 — "제출 자체를
        # 거부"할 수는 없다(이미 벌어진 일이다). 대신: operation_id는 반드시
        # 보존해서(이미 과금됐을 실제 영상을 잃지 않도록) pending에 남기고,
        # cost_krw는 모른다는 뜻으로 None을 두고(장부에는 더하지 않는다),
        # 크게 실패시켜 사람이 알아채게 한다. 다음 호출은 operation_id가
        # 있으므로 재제출이 아니라 이어받기로 진행된다 — _poll_until_terminal
        # 이 폴링 응답에서 cost_krw를 다시 얻으면 그때 장부에 반영한다(아래
        # 참고).
        cost = data.get("cost_krw")
        entry = {"operation_id": op_id, "cost_krw": cost, "submitted_at": submitted_at}
        pending[key] = entry
        if cost is not None:
            pending[_TOTAL_SPENT_KEY] = pending.get(_TOTAL_SPENT_KEY, 0) + cost
        self._write_pending(pending)
        if cost is None:
            raise RuntimeError(
                f"bizrouter POST 응답(operation_id={op_id})에 cost_krw가 "
                f"없습니다: {data}. 실제 청구액을 모르면 예산 가드가 조용히 "
                "무력화되므로(0원으로 오인) 여기서 멈춥니다. 이 작업은 이미 "
                "서버에 제출됐을 수 있습니다 — bizrouter 콘솔에서 실제 청구 "
                "여부를 확인하세요. 다음 실행은 재제출하지 않고 이 "
                "operation_id로 이어받습니다.")
        return op_id, entry

    # --- 폴링 ---

    def _poll_until_terminal(self, op_id: str, key: str, pending: dict,
                             entry: dict) -> None:
        """completed/failed 중 하나에 도달할 때까지 폴링한다.

        completed면 entry에 completed_at을 기록만 하고 반환한다(다운로드는
        호출부가 한다). failed면(종결 상태 — 더 기다려도 같은 결과다) pending을
        즉시 지우고 BlockedError(과금 안 됨)/TerminalGenerationError(과금됨,
        Task 15 리뷰 Critical 1 — 예전엔 RuntimeError였는데 generate_clips가
        이걸 "일시적 오류"로 오인해 최대 max_retries번 재시도하며 매번 새
        POST를 냈다)를 던진다. cost_krw는 보통 이미 _submit()에서 누적 지출
        장부에 반영됐으므로 여기서 다시 더하지 않는다(이중 계산 방지) — 단,
        _submit() 응답에 cost_krw가 없어 그때 반영하지 못했다면(Important 3)
        폴링 응답에서라도 알게 되는 즉시 장부에 반영한다(0원으로 영구히
        누락되지 않도록)."""
        waited = 0
        while True:
            resp = requests.get(
                f"{C.BIZROUTER_BASE}/video/generation/{op_id}", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")

            if entry.get("cost_krw") is None and data.get("cost_krw") is not None:
                cost = data["cost_krw"]
                entry["cost_krw"] = cost
                pending[key] = entry
                pending[_TOTAL_SPENT_KEY] = pending.get(_TOTAL_SPENT_KEY, 0) + cost
                self._write_pending(pending)

            if status in ("pending", "processing"):
                if waited >= self.timeout_seconds:
                    # op_id는 _submit()이 폴링 진입 전에 이미 기록해뒀다 —
                    # 여기서 다시 쓸 필요가 없다.
                    raise TimeoutError(
                        f"{self.timeout_seconds}초 초과: {op_id} — "
                        f"다음 실행에서 이어받습니다(재제출하지 않습니다).")
                time.sleep(self.poll_seconds)
                waited += self.poll_seconds
                continue

            if status == "completed":
                entry["completed_at"] = time.time()
                pending[key] = entry
                self._write_pending(pending)
                return

            if status == "failed":
                pending.pop(key, None)
                self._write_pending(pending)
                if _is_safety_error(data):
                    raise BlockedError(
                        f"bizrouter 생성이 정책/안전 사유로 차단된 것으로 보입니다: {data}")
                # 종결된 실패이지만 이미 과금됐다(cost_krw는 접수 시점에 확정
                # 청구, failed != 환불). TerminalGenerationError로 명시해
                # generate_clips가 재시도(=새 POST=새 과금)하지 않게 한다.
                raise TerminalGenerationError(
                    f"bizrouter 생성 실패(status=failed, 이미 과금됨): {data}")

            raise RuntimeError(f"알 수 없는 status 값: {status!r} — {data}")

    # --- 다운로드 ---

    def _download(self, op_id: str, out_path: Path, key: str, pending: dict) -> None:
        # 여기서 예외가 나면(네트워크 오류, 4xx/5xx 등) pending을 지우지 않고
        # 그대로 전파한다 — 서버에는 이미 완성된(=과금된) 결과가 있으므로
        # 재제출이 아니라 재다운로드로 이어받아야 한다.
        resp = requests.get(
            f"{C.BIZROUTER_BASE}/video/generation/{op_id}/download",
            headers=self._headers())
        resp.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        if not out_path.exists() or out_path.stat().st_size == 0:
            # generate_clips는 이 함수가 정상 반환하면 partial.replace(target)로
            # 곧장 최종본으로 승격시킨다 — 빈 파일이 승격되면 안 되므로 여기서 막는다.
            raise RuntimeError(
                f"{out_path} 저장에 실패했습니다(파일이 비어 있거나 없음).")

        # 다운로드+저장+검증까지 전부 끝났다 — 이제서야 in-flight 기록을 지운다.
        pending.pop(key, None)
        self._write_pending(pending)

    # --- 공개 API ---

    def generate(self, prompt: str, out_path: Path, duration: int,
                 resolution: str, aspect: str,
                 reference_images: list[Path] | None) -> None:
        self._validate(duration, resolution, reference_images)
        self._shots_seen += 1

        key = out_path.name
        pending = self._pending()

        if key in pending:
            entry = pending[key]
            if entry.get("operation_id") is None:
                # 지난 실행이 제출 도중(성공했는지도 모르는 채로) 끊겼다 —
                # 진짜 operation_id가 없어서 이어받을 수도 없다. 자동
                # 재제출은 하지 않는다.
                raise AmbiguousSubmissionError(_AMBIGUOUS_RESUME_MSG.format(
                    pending_path=self.pending_path, key=key))
            op_id = entry["operation_id"]
        else:
            # 새 제출 경로에서만 예산 가드를 건다 — 이어받기(resume)는 이미
            # 과거에 과금이 끝난 작업이므로 여기서 다시 막으면 안 된다(이미
            # 낸 돈에 대한 결과물을 못 받게 되는 게 더 나쁘다).
            self._check_budget(duration, pending)
            op_id, entry = self._submit(prompt, duration, resolution, aspect,
                                        reference_images, key, pending)

        completed_at = entry.get("completed_at")
        if completed_at is not None:
            # 이미 completed로 관찰된 적이 있다 — 30분 다운로드 창을 확인한다.
            # 창이 지났으면 다운로드 시도조차 하지 않는다: 서버 영상이 이미
            # 삭제됐을 것이므로 시도해봐야 실패할 뿐이고, 조용히 재제출하면
            # 안 되므로 사람이 읽을 수 있는 에러만 던진다.
            elapsed = time.time() - completed_at
            if elapsed > C.BIZROUTER_DOWNLOAD_WINDOW_SECONDS:
                raise RuntimeError(_WINDOW_EXPIRED_MSG.format(
                    key=key, operation_id=op_id, pending_path=self.pending_path))
        else:
            # 아직 completed를 관찰한 적이 없다 — 폴링한다(새 제출이든,
            # 폴링 도중 끊긴 이어받기든 동일 경로).
            self._poll_until_terminal(op_id, key, pending, entry)

        self._download(op_id, out_path, key, pending)


# --- TTS (openai/gpt-4o-mini-tts, /v1/audio/speech) ---
#
# video와 결정적으로 다른 점: 이 엔드포인트는 동기(sync)다. POST 한 번에
# 오디오 바이트가 바로 온다 — 202+operation_id+폴링도, 다운로드 창도,
# cost_krw 필드도 없다(공식 문서, 2026-08 확인. 과금은 사후 산출 오디오
# 토큰 집계 기준이라 응답에 실시간 청구액이 실리지 않는다). 그래서
# pending.json 이중장부(AmbiguousSubmissionError/TerminalGenerationError/
# 예산 가드)는 이 provider엔 적용되지 않는다 — 추적할 in-flight 상태
# 자체가 없다. 다만 "제출 전 로컬 검증"과 "안전차단은 BlockedError로
# 구분해 재시도 안 함" 두 원칙은 video와 동일하게 지킨다.

# gpt-4o-mini-tts가 지원하는 음성 8종(공식 문서, 2026-08 확인). 파이프라인의
# 기존 기본값(voice.py의 "Charon")은 Gemini 전용 음성이라 이 목록에 없다 —
# 그대로 보내면 API가 400으로 거부한다(브리프가 명시하듯, 미지원 파라미터는
# 무시가 아니라 거부다). 새 음성은 컨트롤러가 후보 4종의 샘플을 듣고
# 고른다(task-16-brief.md 두 번째 항목) — 이 목록 자체는 API 사양이라
# 컨트롤러의 선택과 무관하게 고정이다.
_TTS_SUPPORTED_VOICES = ("alloy", "ash", "ballad", "coral", "echo",
                         "sage", "shimmer", "verse")


def _tts_error_detail(resp):
    """오류 응답 바디를 사람이 읽을 수 있는 형태로 뽑는다. /v1/audio/speech는
    성공 시 JSON이 아니라 바이너리(오디오)를 돌려주지만, 오류 응답의 정확한
    스키마는 문서에 명시돼 있지 않다 — REST 관례상 JSON 오류 객체일 가능성이
    높으므로 우선 시도하고, 아니면 원문 텍스트로 대체한다."""
    try:
        return resp.json()
    except Exception:
        return resp.text


class BizrouterTTSProvider:
    """bizrouter 경유 OpenAI gpt-4o-mini-tts 어댑터."""

    def __init__(self, api_key: str, model: str = C.BIZROUTER_TTS_MODEL,
                 instructions: str = C.BIZROUTER_TTS_INSTRUCTIONS):
        self.api_key = api_key
        self.model = model
        self.instructions = instructions

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _validate_voice(self, voice: str) -> None:
        if voice in _TTS_SUPPORTED_VOICES:
            return
        raise ValueError(
            f"bizrouter TTS({self.model})가 지원하지 않는 voice입니다: {voice!r}. "
            f"지원 목록: {', '.join(_TTS_SUPPORTED_VOICES)}. Gemini 전용 음성 "
            "이름(예: 'Charon')을 그대로 보내면 API가 400으로 거부하므로, "
            "요청을 내기 전에 여기서 먼저 막습니다.")

    def synthesize(self, text: str, out_path: Path, voice: str) -> None:
        self._validate_voice(voice)

        body = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "instructions": self.instructions,
            # ffmpeg 파이프라인(voice.py의 build_narration_track)이 곧장 읽을
            # 수 있도록 wav로 고정한다 — response_format이 지원하는 다른 값
            # (mp3/opus/aac/flac/pcm)은 이 파이프라인에서 쓰지 않는다.
            "response_format": "wav",
        }
        resp = requests.post(f"{C.BIZROUTER_BASE}/audio/speech", json=body,
                             headers=self._headers())
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            detail = _tts_error_detail(resp)
            if _is_safety_error(detail):
                # gemini.py/BizrouterVideoProvider와 같은 원칙: 안전/정책
                # 차단은 평범한 예외가 아니라 BlockedError로 뗀다 — 이
                # 엔드포인트는 무료 재시도가 아니라 매 시도가 새 과금이므로,
                # generate 계열이 같은 텍스트에 두 번 돈을 쓰면 안 된다.
                raise BlockedError(
                    f"bizrouter TTS 생성이 정책/안전 사유로 차단된 것으로 "
                    f"보입니다: {detail}") from e
            raise RuntimeError(
                f"bizrouter TTS 요청 실패 (status={resp.status_code}): "
                f"{detail}") from e

        content = resp.content
        content_length = resp.headers.get("Content-Length")
        if not content or (content_length is not None
                           and int(content_length) != len(content)):
            # build_narration_track이 이 결과를 곧장 ffmpeg 입력으로 쓰고,
            # verify.py의 silencedetect가 그 산출물을 측정한다 — 비어 있거나
            # 잘린 파일을 써 두면 "무음/짧은 나레이션"으로 조용히 오판정될
            # 뿐 원인이 드러나지 않는다. 여기서 즉시, 크게 실패시킨다.
            raise RuntimeError(
                f"bizrouter TTS 응답 바디가 비어 있거나 잘렸습니다"
                f"(받은 바이트 {len(content)}, Content-Length {content_length!r})"
                f" — {out_path}에 파일을 쓰지 않습니다.")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content)
