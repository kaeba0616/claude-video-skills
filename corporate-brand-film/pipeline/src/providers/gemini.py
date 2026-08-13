"""Gemini API Provider — Veo 3.1 영상 생성 + Gemini TTS.

Veo는 long-running operation이라 폴링해야 한다 (실측 레이턴시 11초~6분).
결과가 비어 있으면 안전필터 차단으로 간주한다 (차단은 과금되지 않는다) —
operation.error 가 채워진 경우는 대개 진짜 API 오류(쿼터·서버 오류 등)지만,
메시지에 안전/정책 관련 표현이 보이면 그것도 차단으로 분류한다(§ op.error가
안전필터를 이 경로로 보고할 수도 있다는 보고가 있음 — 애매하면 재시도 안
하는 쪽으로 분류해서 같은 프롬프트에 두 번 돈을 쓰지 않는다).

Veo 결과는 생성 후 2일이 지나면 서버에서 삭제된다. 폴링이 timeout_seconds를
넘기면 재제출 대신 operation 이름을 pending_path에 남기고 다음 실행에서
이어받는다 — 재제출하면 돈이 또 나가기 때문이다. 이어받으려는 operation이
이미 만료됐으면(서버가 404) 자동으로 재제출하지 않고 사람이 읽을 수 있는
에러만 던진다.

과금 지점은 generate_videos() 제출 그 자체다 — 응답이 왔든 안 왔든 서버가
요청을 받았으면 그때 이미 돈이 나간다. 그래서 폴링 타임아웃뿐 아니라
"제출은 됐는데 뭔가 실패한" 모든 경우를 pending_path에 기록해야 한다:
- 제출 직전에 마커(sentinel)를 남기고, 제출이 성공적으로 반환되면 즉시
  진짜 operation 이름으로 덮어쓴다(폴링 루프 진입 전, Ctrl-C/OOM 등으로
  중간에 죽어도 이어받을 수 있도록).
- generate_videos() 호출 자체가 예외를 던지면(클라이언트 타임아웃 등) 서버에
  도달했는지 알 수 없다 — 마커를 그대로 남겨두고 AmbiguousSubmissionError를
  던진다. generate_clips는 이걸 재시도하지 않는다(재시도하면 진짜 이중
  과금이 날 수 있다).
- 다음 실행에서 마커만 남아 있고(진짜 operation 이름을 모름) 있으면, 자동
  재제출은 절대 하지 않고 사람이 확인하게 한다.
"""
import json
import time
import wave
from pathlib import Path

from google import genai
from google.genai import errors, types

from src import constants as C
from src.providers.base import (AmbiguousSubmissionError, RESOLUTION_SIZE,
                                BlockedError, TerminalGenerationError)

# Veo 3.1 확인된 제약 (ai.google.dev/gemini-api/docs/veo, 2026-08 기준):
# durationSeconds는 4/6/8만 허용되고, 1080p 이상 해상도는 duration=8을 강제한다.
# 과금 제출 전에 여기서 막아야 잘못된 조합 하나로 돈을 날리지 않는다.
_VALID_DURATIONS = (4, 6, 8)
_FORCED_8S_RESOLUTIONS = ("1080p", "4k")

# reference_images 관련 제약은 공식 문서에 다 나오지 않는다 — 커뮤니티에서
# 실측된 내용까지 반영했다(discuss.ai.google.dev의 "Veo 3.1 Reference Images"
# 스레드, 2026-08 확인). Task 13 리뷰에서 지적된 대로 STYLE 타입은 Veo 3.1에서
# 아예 지원되지 않는다("Veo 3.1 models don't support referenceImages.style")
# — ASSET만 된다. reference_images는 8초 길이에서만 동작한다는 보고도 있어
# 로컬에서 막는다(과금 후 400을 맞는 것보다 미리 막는 게 안전하다는 원칙).
_REFERENCE_IMAGE_TYPE = "asset"
_MAX_REFERENCE_IMAGES = 3

# 진짜 operation 이름이 아니라 "제출은 했는데 결과를 모른다"는 사실 자체를
# 표시하는 값. 실제 operation 이름은 "operations/..." 형태라 절대 겹치지
# 않는다.
_SUBMITTING_SENTINEL = "__SUBMITTING__"

# operation.error 메시지/상태에서 안전필터 차단을 시사하는 표현들. 정확한
# 실제 응답 형태는 Task 14의 실제 호출 전까지 검증되지 않았으므로, 애매하면
# (마커가 하나라도 걸리면) 차단으로 분류해서 재시도하지 않는 쪽을 택한다 —
# 잘못 분류된 진짜 오류를 재시도 못 하는 것보다, 차단된 프롬프트에 두 번
# 돈을 쓰는 게 더 나쁘다.
_SAFETY_ERROR_MARKERS = ("safety", "blocked", "policy", "prohibited", "rai")

_RESUME_EXPIRED_MSG = (
    "이어받으려던 operation({name})을 서버에서 찾을 수 없습니다(404). "
    "Veo 결과는 생성 후 2일이 지나면 서버에서 삭제되므로 이 작업은 이미 "
    "만료됐을 가능성이 큽니다. 자동으로 재제출하지 않습니다 — 실제로는 "
    "아직 진행 중인 작업일 수도 있는데 자동 재제출하면 중복 과금이 날 "
    "수 있기 때문입니다. 정말 새로 만들고 싶으면 {pending_path} 에서 "
    "'{key}' 항목을 직접 지우고 다시 실행하세요(추가 과금 발생)."
)

_AMBIGUOUS_SUBMISSION_MSG = (
    "제출 요청(generate_videos) 도중 오류가 나서 요청이 실제로 Google 서버에 "
    "도달했는지 알 수 없습니다 — 도달했다면 이미 과금됐을 수 있습니다. "
    "https://console.cloud.google.com 사용량을 확인하세요. 이 샷은 자동으로 "
    "재시도/재제출하지 않습니다. 확인 후 다시 시도하려면(추가 과금 가능) "
    "{pending_path} 에서 '{key}' 항목을 직접 지운 뒤 재실행하세요."
)

_AMBIGUOUS_RESUME_MSG = (
    "'{key}' 항목이 이전 실행에서 제출 도중(제출됐는지 모르는 상태로) 끊긴 "
    "채로 남아 있습니다 — 진짜 operation 이름을 모르므로 이어받을 수도 "
    "없습니다. 자동으로 재제출하지 않습니다: 실제로는 이미 과금된 작업이 "
    "진행 중일 수도 있기 때문입니다. https://console.cloud.google.com "
    "사용량을 확인한 뒤, 다시 시도하려면(추가 과금 가능) {pending_path} 에서 "
    "'{key}' 항목을 직접 지우고 재실행하세요."
)


def _is_safety_error(error) -> bool:
    text = json.dumps(error, ensure_ascii=False, default=str).lower() \
        if isinstance(error, dict) else str(error).lower()
    return any(marker in text for marker in _SAFETY_ERROR_MARKERS)


class GeminiVideoProvider:
    """Veo 3.1 text-to-video 어댑터."""

    def __init__(self, model: str = "veo-3.1-lite-generate-preview",
                 poll_seconds: int = 10, timeout_seconds: int = 420,
                 pending_path: Path | None = None):
        self.model = model
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.pending_path = Path(pending_path) if pending_path else None
        self.client = genai.Client()

    def _pending(self) -> dict[str, str]:
        if self.pending_path and self.pending_path.exists():
            return json.loads(self.pending_path.read_text())
        return {}

    def _write_pending(self, data: dict[str, str]) -> None:
        """pending.json을 원자적으로(temp write + rename) 갱신한다.

        generate.py의 클립 승격과 같은 이유다 — 쓰는 도중 죽으면(디스크
        가득 참, kill -9 등) 파일이 반쯤 쓰인 채로 남을 수 있는데, 이 파일은
        "지금 서버에 어떤 유료 작업이 떠 있는지"의 유일한 기록이다. 반쯤
        잘린 JSON이 남으면 다음 실행이 그 기록 전체를 잃어버리고 모든 진행
        중 작업을 다시 제출해서 중복 과금을 낸다.
        """
        if not self.pending_path:
            return
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pending_path.with_name(self.pending_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.pending_path)

    def _validate(self, duration: int, resolution: str,
                  reference_images: list[Path] | None) -> None:
        if resolution not in RESOLUTION_SIZE:
            raise ValueError(
                f"알 수 없는 해상도: {resolution!r}. "
                f"지원: {', '.join(RESOLUTION_SIZE)}"
            )
        if duration not in _VALID_DURATIONS:
            raise ValueError(
                f"Veo 는 durationSeconds 로 {_VALID_DURATIONS} 만 허용합니다 "
                f"(요청: {duration}초). 과금 전에 로컬에서 막습니다.")
        if resolution in _FORCED_8S_RESOLUTIONS and duration != 8:
            raise ValueError(
                f"Veo는 {resolution} 해상도에서 8초 길이만 지원합니다 "
                f"(요청: {duration}초). 과금 전에 로컬에서 막습니다.")
        if reference_images and duration != 8:
            raise ValueError(
                "Veo는 reference_images 사용 시 8초 길이에서만 동작한다는 "
                f"보고가 있습니다(요청: {duration}초). 문서화되지 않은 "
                "제약이라 확실치 않지만, 과금 후 400 오류를 맞는 것보다 "
                "미리 막는 쪽을 택합니다.")
        if reference_images and len(reference_images) > _MAX_REFERENCE_IMAGES:
            raise ValueError(
                f"Veo reference_images는 최대 {_MAX_REFERENCE_IMAGES}장까지만 "
                f"지원합니다(받은 개수: {len(reference_images)}). 조용히 "
                "잘라서 보내면 사용자가 의도한 이미지 중 일부가 빠졌는지도 "
                "모른 채 과금하게 되므로, 여기서 막습니다.")

    def _submit(self, prompt: str, duration: int, resolution: str, aspect: str,
                reference_images: list[Path] | None, key: str,
                pending: dict[str, str]) -> types.GenerateVideosOperation:
        cfg_kwargs = {
            "duration_seconds": duration,
            "resolution": resolution,
            "aspect_ratio": aspect,
            # 부정어는 긍정 프롬프트 문자열에 섞지 않는다(prompts.py 참고 —
            # 긍정 프롬프트 안의 부정어는 그 요소를 오히려 불러오는 역효과가
            # 있다). Veo SDK는 negative_prompt 전용 필드를 지원하므로 여기서
            # 직접 넘긴다.
            "negative_prompt": C.VEO_NEGATIVE_PROMPT,
        }
        if reference_images:
            # _validate()가 이미 개수(<=3)와 duration(==8)을 검증했다 — 여기서
            # 다시 자르지 않는다(자르면 사용자가 의도한 이미지가 조용히
            # 빠진 채로 과금될 수 있다). Veo 3.1은 STYLE 레퍼런스 이미지를
            # 지원하지 않는다(공식/커뮤니티 보고 둘 다 확인, gemini.py 상단
            # 주석 참고) — ASSET만 쓴다.
            cfg_kwargs["reference_images"] = [
                types.VideoGenerationReferenceImage(
                    image=types.Image.from_file(location=str(p)),
                    reference_type=_REFERENCE_IMAGE_TYPE)
                for p in reference_images
            ]

        # 제출 직전에 마커를 남긴다 — generate_videos() 호출이 응답 전에
        # 죽으면(클라이언트 타임아웃, 프로세스 강제종료 등) 요청이 서버에
        # 도달했는지 알 방법이 없다. 마커가 없으면 다음 실행이 그대로
        # 재제출해서 중복 과금이 날 수 있다.
        pending[key] = _SUBMITTING_SENTINEL
        self._write_pending(pending)
        try:
            op = self.client.models.generate_videos(
                model=self.model, prompt=prompt,
                config=types.GenerateVideosConfig(**cfg_kwargs))
        except Exception as e:
            raise AmbiguousSubmissionError(_AMBIGUOUS_SUBMISSION_MSG.format(
                pending_path=self.pending_path, key=key)) from e

        # 제출이 성공적으로 반환됐다 — 이제 진짜 operation 이름을 안다.
        # 폴링 도중 죽어도(Ctrl-C, OOM 등) 다음 실행이 재제출 대신 이걸로
        # 이어받을 수 있도록 폴링 루프 진입 전에 즉시 기록한다.
        pending[key] = op.name
        self._write_pending(pending)
        return op

    def generate(self, prompt: str, out_path: Path, duration: int,
                 resolution: str, aspect: str,
                 reference_images: list[Path] | None) -> None:
        self._validate(duration, resolution, reference_images)

        key = out_path.name
        pending = self._pending()

        if key in pending:
            if pending[key] == _SUBMITTING_SENTINEL:
                # 지난 실행이 제출 도중(성공했는지도 모르는 채로) 끊겼다 —
                # 진짜 operation 이름이 없어서 이어받을 수도 없다. 자동
                # 재제출은 하지 않는다.
                raise AmbiguousSubmissionError(_AMBIGUOUS_RESUME_MSG.format(
                    pending_path=self.pending_path, key=key))
            # 지난 실행에서 타임아웃된 작업 — 재제출하지 않고 이어받는다.
            # 저장해둔 건 operation 이름뿐이므로, get()이 요구하는 .name
            # 속성을 가진 최소 Operation 객체를 새로 만들어 넘긴다.
            try:
                op = self.client.operations.get(
                    types.GenerateVideosOperation(name=pending[key]))
            except errors.ClientError as e:
                if e.code == 404:
                    raise RuntimeError(_RESUME_EXPIRED_MSG.format(
                        name=pending[key], pending_path=self.pending_path,
                        key=key)) from e
                raise
        else:
            op = self._submit(prompt, duration, resolution, aspect,
                              reference_images, key, pending)

        waited = 0
        while not op.done:
            if waited >= self.timeout_seconds:
                # op.name은 _submit()이 폴링 진입 전에 이미 기록해뒀다 —
                # 여기서 다시 쓸 필요가 없다.
                raise TimeoutError(
                    f"{self.timeout_seconds}초 초과: {op.name} — "
                    f"다음 실행에서 이어받습니다(재제출하지 않습니다).")
            time.sleep(self.poll_seconds)
            waited += self.poll_seconds
            op = self.client.operations.get(op)

        # 폴링이 끝났다(op.done) — 하지만 pending을 언제 지울지는 결과가 어떤
        # "종류"의 완료인지에 따라 갈린다. 세 상태를 절대 섞으면 안 된다:
        #
        # 1) operation 자체가 실패로 끝났다(op.error, 안전필터 차단 포함) —
        #    다운로드할 결과물이 아예 없다. 더 기다려도, 다시 조회해도 같은
        #    결과가 나올 뿐인 완전히 끝난 상태다 — in-flight가 아니므로 pending을
        #    지운다(안 지우면 이 샷이 영원히 예전 실패한 operation에 묶여버린다,
        #    예: 프롬프트를 고쳐도 다음 실행이 여전히 옛 결과를 이어받으려 한다).
        #    단, "종결됐다"가 "재시도해도 안전하다"는 뜻은 아니다 — 안전필터
        #    차단이 아닌 진짜 API 오류도 이미 제출은 서버에 도달해서 처리까지
        #    끝난 것이므로, 과금 여부는 API 정책에 달려 있고 우리가 안전하다고
        #    가정할 수 없다. Task 15 리뷰 Critical 1: 예전엔 이걸 평범한
        #    RuntimeError로 던져서 generate_clips가 "일시적 오류"로 오인해 최대
        #    max_retries번 재시도했고, pending이 이미 비어 있으니 매 재시도가
        #    새 generate_videos() 호출(=새 제출)로 이어졌다. TerminalGenerationError
        #    로 명시해 재시도를 원천 차단한다.
        # 2) operation은 성공했는데(영상이 실제로 생김 = 과금 확정) 로컬
        #    다운로드/저장이 실패했다 — 서버에는 이미 다 만들어진 결과가
        #    있으므로 재제출하면 진짜 이중 과금이다. 이 경로만 pending을
        #    다운로드+저장+검증이 전부 성공할 때까지 남겨 둔다.
        if op.error:
            # 종류 1: operation 자체의 실패 — 더 이상 이어받을 게 없다.
            pending.pop(key, None)
            self._write_pending(pending)
            if _is_safety_error(op.error):
                # 안전필터 차단으로 보이는 오류 — 재시도해도 어차피 다시
                # 막힐 프롬프트에 두 번 돈을 쓰지 않도록 BlockedError로 뗀다.
                raise BlockedError(
                    f"Veo 생성이 안전정책에 의해 차단된 것으로 보입니다: {op.error}")
            # 안전필터 차단이 아니라 진짜 API 오류 — 그래도 종결된 실패이고
            # 이미 제출은 서버에 도달했다. TerminalGenerationError로 명시해
            # generate_clips가 재시도(=새 제출=새 과금 위험)하지 않게 한다.
            raise TerminalGenerationError(f"Veo 생성 실패(operation.error): {op.error}")

        videos = getattr(op.response, "generated_videos", None) or []
        if not videos:
            # 종류 1과 동일 — 안전필터에 걸러져 다운로드할 결과가 없다.
            pending.pop(key, None)
            self._write_pending(pending)
            reasons = getattr(op.response, "rai_media_filtered_reasons", None)
            detail = f" — 필터 사유: {reasons}" if reasons else ""
            raise BlockedError(f"{prompt}{detail}")

        # 여기부터는 종류 2 — operation이 성공해서 다운로드할 실제 결과가
        # 있다(=이미 과금 확정). 아래에서 실패하면(다운로드 오류, 빈 파일)
        # pending을 지우지 않는다 — 재제출이 아니라 재다운로드로 이어받아야
        # 한다.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        video = videos[0].video
        # Gemini Developer API는 완료된 영상을 uri로만 돌려준다 — download()로
        # video_bytes를 먼저 채워야 save()가 동작한다. 안 하면 매번
        # NotImplementedError("Saving remote videos is not supported.")가 난다.
        self.client.files.download(file=video)
        video.save(str(out_path))
        if not out_path.exists() or out_path.stat().st_size == 0:
            # generate_clips는 이 함수가 정상 반환하면 partial.replace(target)로
            # 곧장 최종본으로 승격시킨다 — 빈 파일이 승격되면 안 되므로 여기서 막는다.
            raise RuntimeError(
                f"{out_path} 저장에 실패했습니다(파일이 비어 있거나 없음).")

        # 다운로드+저장+검증까지 전부 끝났다 — 이제서야 in-flight 기록을 지운다.
        pending.pop(key, None)
        self._write_pending(pending)


class GeminiTTSProvider:
    """Gemini TTS 어댑터 — 대본을 한국어 성우 톤 낭독으로 합성한다."""

    def __init__(self, model: str = "gemini-2.5-flash-preview-tts"):
        self.model = model
        self.client = genai.Client()

    def synthesize(self, text: str, out_path: Path, voice: str) -> None:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=(
                "다음 대본을 한국어 기업홍보영상 성우 톤으로 낭독해줘. "
                "차분하고 신뢰감 있게, 문장 사이에 충분히 쉬면서:\n\n" + text
            ),
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice))),
            ),
        )

        candidates = getattr(resp, "candidates", None) or []
        parts = candidates[0].content.parts if candidates and candidates[0].content else []
        if not candidates or not parts:
            reason = candidates[0].finish_reason if candidates else None
            raise BlockedError(f"TTS 응답이 비어 있습니다 (finish_reason={reason}): {text[:50]!r}")

        pcm = parts[0].inline_data.data
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Gemini TTS는 24kHz 16bit mono PCM을 반환한다. wave.open을 with로
        # 닫아야 헤더까지 포함해 완전한 파일이 디스크에 남는다 — 여기서 함수가
        # 반환되면 파일은 항상 완결된 상태다.
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
