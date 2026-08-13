"""BizrouterVideoProvider / BizrouterTTSProvider — 전부 requests를 mock,
네트워크 호출 없음.

과금 API를 실제로 부르는 테스트는 이 파일에 하나도 없다. requests.post/get은
전부 MagicMock으로 패치한다. Video provider는 실제로 돈이 오가는 비동기(202+폴링)
경로이므로, Task 13(gemini.py)에서 세 라운드에 걸쳐 다듬어진 이중 과금 방어
규율을 그대로 재현하는지가 그 부분의 핵심 관심사다. TTS provider(Task 16)는
동기 API라(POST 한 번으로 오디오 바이트가 바로 온다) 이중장부 로직은 없지만,
"제출 전 로컬 검증"과 "안전차단은 BlockedError로 구분" 두 원칙은 동일하게
적용된다.
"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src import constants as C
from src.providers.base import AmbiguousSubmissionError, BlockedError


def _resp(status_code=200, json_data=None, content=b""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.content = content
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error")
    else:
        r.raise_for_status.side_effect = None
    return r


def _post_accepted(operation_id="op_1", cost_krw=608, status="pending"):
    return _resp(202, {"operation_id": operation_id, "status": status,
                        "cost_krw": cost_krw})


def _get_status(status, cost_krw=608, video_uri="https://example.com/v.mp4",
                extra=None):
    data = {"status": status, "video_uri": video_uri,
            "duration_seconds": 4, "cost_krw": cost_krw}
    if extra:
        data.update(extra)
    return _resp(200, data)


def _get_download(content=b"fake-mp4-bytes"):
    return _resp(200, content=content)


# --- 정상 경로: 제출 → 폴링 → completed → 다운로드 → pending 비워짐 ---

def test_generate_submits_polls_downloads_and_clears_pending(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    post_resp = _post_accepted(operation_id="op_1", cost_krw=608)
    get_responses = [
        _get_status("processing"),
        _get_status("processing"),
        _get_status("completed"),
    ]
    with patch("src.providers.bizrouter.requests.post", return_value=post_resp) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=get_responses + [_get_download()]) as m_get, \
         patch("src.providers.bizrouter.time.sleep"):
        BizrouterVideoProvider(api_key="k", pending_path=pending, poll_seconds=1).generate(
            "a factory", tmp_path / "01.mp4", 4, "720p", "16:9", None)

    m_post.assert_called_once()
    assert m_get.call_count == 4  # 폴링 3회(processing, processing, completed) + 다운로드 1회
    assert (tmp_path / "01.mp4").exists()
    assert (tmp_path / "01.mp4").read_bytes() == b"fake-mp4-bytes"
    assert json.loads(pending.read_text()).get("01.mp4") is None


def test_generate_sends_negative_prompt_as_dedicated_field_not_in_prompt(tmp_path):
    """긍정 prompt 문자열에는 부정어를 섞지 않는다 — negative_prompt는
    요청 바디의 전용 필드로 넘어가야 한다(prompts.py 참고)."""
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted()) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
            "a factory", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    body = m_post.call_args.kwargs["json"]
    assert body["prompt"] == "a factory"
    assert body["negative_prompt"] == C.VEO_NEGATIVE_PROMPT
    assert "no " not in body["prompt"]


def test_generate_passes_model_duration_resolution_aspect(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted()) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    body = m_post.call_args.kwargs["json"]
    assert body["model"] == C.BIZROUTER_VIDEO_MODEL
    assert body["duration_seconds"] == 4
    assert body["resolution"] == "720p"
    assert body["aspect_ratio"] == "16:9"


def test_generate_sends_bearer_auth_header_without_exposing_key_in_pending_file(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted()) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="super-secret-key", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert m_post.call_args.kwargs["headers"]["Authorization"] == "Bearer super-secret-key"
    # pending.json은 디스크에 남는 파일이다 — API 키가 거기 새어 들어가면 안 된다.
    assert "super-secret-key" not in pending.read_text()


# --- 이중 과금 방어: 제출 성공 직후 크래시 → 이어받기(재제출 없음) ---

def test_resume_after_crash_does_not_resubmit_polls_instead(tmp_path):
    """지난 실행이 POST까지는 성공해서 operation_id를 pending에 기록해뒀지만
    그 뒤 죽었다고 가정한다. 다음 호출은 POST를 다시 하지 않고 GET으로
    이어받아야 한다 — 이게 이중 과금 방어의 핵심이다."""
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({
        "01.mp4": {"operation_id": "op_resumed", "cost_krw": 608,
                   "submitted_at": "2026-08-12T00:00:00+00:00"}
    }))
    with patch("src.providers.bizrouter.requests.post") as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]) as m_get:
        BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()
    assert m_get.call_count == 2
    assert (tmp_path / "01.mp4").exists()
    assert json.loads(pending.read_text()).get("01.mp4") is None


# --- POST 자체가 예외 ---

def test_post_exception_raises_ambiguous_and_leaves_sentinel(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               side_effect=TimeoutError("client read timeout")):
        with pytest.raises(AmbiguousSubmissionError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    entry = json.loads(pending.read_text())["01.mp4"]
    assert entry.get("operation_id") is None  # 진짜 operation_id를 모른다


def test_sentinel_only_entry_is_not_auto_resubmitted(tmp_path):
    """이전 실행이 제출 도중(성공 여부를 모르는 채로) 끊겨서 마커만 남아 있는
    경우, 다음 실행은 절대 자동으로 재제출하면 안 된다."""
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               side_effect=RuntimeError("boom")):
        with pytest.raises(AmbiguousSubmissionError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)

    with patch("src.providers.bizrouter.requests.post") as m_post2:
        with pytest.raises(AmbiguousSubmissionError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post2.assert_not_called()
    assert json.loads(pending.read_text())["01.mp4"].get("operation_id") is None


def test_4xx_rejection_is_not_ambiguous_and_leaves_no_sentinel(tmp_path):
    """4xx(429 제외)는 명확한 거절이다 — 애매(과금됐을 수도)가 아니다.

    Task 17에서 veo-3.1-lite로 1컷을 시도했다가 실측으로 드러났다: 서버가
    400("`negativePrompt` isn't supported by this model")을 돌려줬는데
    AmbiguousSubmissionError로 분류돼 "이미 과금됐을 수 있다"는 틀린 경고가
    나왔고, sentinel이 남아 샷이 막혔으며, 거절 사유가 담긴 응답 본문은
    버려져서 무엇이 잘못됐는지 알 수 없었다.

    거절은 과금 0원이고, 같은 요청을 다시 보내면 같은 거절만 돌아온다.
    따라서 (1) 전용 예외로 구분하고 (2) sentinel을 남기지 않고 (3) 응답
    본문을 메시지에 실어야 한다.
    """
    from src.providers.base import RejectedSubmissionError
    from src.providers.bizrouter import BizrouterVideoProvider

    pending = tmp_path / "p.json"
    body = {"error": {"code": "bad_request",
                      "message": "`negativePrompt` isn't supported by this model"}}
    rejected = _resp(400, body)
    rejected.text = json.dumps(body)
    p = BizrouterVideoProvider(api_key="k", pending_path=pending)

    with patch("src.providers.bizrouter.requests.post", return_value=rejected):
        with pytest.raises(RejectedSubmissionError) as ei:
            p.generate("prompt", tmp_path / "01.mp4", 4, "720p", "16:9", None)

    assert "negativePrompt" in str(ei.value), "거절 사유(응답 본문)가 실려야 한다"
    assert "과금되지 않았습니다" in str(ei.value)
    assert json.loads(pending.read_text()).get("01.mp4") is None, \
        "거절은 sentinel을 남기지 않는다 — 남기면 고칠 것도 없는 샷이 막힌다"


def test_negative_prompt_omitted_for_models_that_reject_it(tmp_path):
    """veo-3.1-lite는 negative_prompt 필드를 보내면 400으로 거절한다(Task 17 실측).
    모델별 지원 여부에 따라 필드를 넣고 뺀다."""
    from src.providers.bizrouter import BizrouterVideoProvider

    for model, expect_field in (("google/veo-3.1-fast", True),
                                ("google/veo-3.1-lite", False)):
        p = BizrouterVideoProvider(api_key="k", model=model,
                                   pending_path=tmp_path / f"{model[-4:]}.json")
        with patch("src.providers.bizrouter.requests.post",
                   return_value=_post_accepted()) as m_post, \
             patch("src.providers.bizrouter.requests.get",
                   return_value=_resp(200, {"operation_id": "op_1",
                                            "status": "completed",
                                            "cost_krw": 608})), \
             patch.object(BizrouterVideoProvider, "_download",
                          lambda self, op, dst, key, pending: dst.write_bytes(b"x" * 100)):
            p.generate("prompt", tmp_path / f"{model[-4:]}.mp4", 4, "720p", "16:9", None)
        sent = m_post.call_args.kwargs["json"]
        assert ("negative_prompt" in sent) is expect_field, \
            f"{model}: negative_prompt 포함 여부가 {expect_field} 여야 한다"


def test_malformed_202_response_missing_operation_id_raises_ambiguous_intentionally(tmp_path):
    """Minor 6(Task 15 리뷰): 202가 왔는데 응답에 operation_id가 없으면(스키마
    위반) data["operation_id"] 접근이 KeyError를 던진다. 이걸 try 블록 밖에서
    맞으면 sentinel이 우연히 그대로 남아 다음 이어받기 때 "어쩌다"
    AmbiguousSubmissionError로 분류될 뿐이다 — 지금 이 호출 자체가 의도적으로
    AmbiguousSubmissionError를 던져야 한다(202를 받았다는 건 서버에 도달은
    했다는 뜻이라 이미 과금됐을 수 있다)."""
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    malformed = _resp(202, {"status": "pending", "cost_krw": 608})  # operation_id 없음
    with patch("src.providers.bizrouter.requests.post", return_value=malformed):
        with pytest.raises(AmbiguousSubmissionError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    entry = json.loads(pending.read_text())["01.mp4"]
    assert entry.get("operation_id") is None  # sentinel 그대로 — 진짜 id를 모른다


# --- 30분 다운로드 창 만료 ---

def test_resume_past_30min_window_raises_clear_error_without_resubmitting(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    stale_completed_at = time.time() - (C.BIZROUTER_DOWNLOAD_WINDOW_SECONDS + 60)
    pending.write_text(json.dumps({
        "01.mp4": {"operation_id": "op_old", "cost_krw": 608,
                   "submitted_at": "2026-08-12T00:00:00+00:00",
                   "completed_at": stale_completed_at}
    }))
    with patch("src.providers.bizrouter.requests.post") as m_post, \
         patch("src.providers.bizrouter.requests.get") as m_get:
        with pytest.raises(RuntimeError, match="30분"):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()
    m_get.assert_not_called()  # 만료가 확실하므로 다운로드 시도조차 하지 않는다
    # 사람이 직접 지울 때까지 항목은 남아 있어야 한다.
    assert json.loads(pending.read_text())["01.mp4"]["operation_id"] == "op_old"


def test_resume_within_30min_window_downloads_without_repolling(tmp_path):
    """completed_at이 기록돼 있고 아직 창 안이면, 상태를 다시 폴링할 필요 없이
    곧장 다운로드를 시도해야 한다."""
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    fresh_completed_at = time.time() - 60
    pending.write_text(json.dumps({
        "01.mp4": {"operation_id": "op_recent", "cost_krw": 608,
                   "submitted_at": "2026-08-12T00:00:00+00:00",
                   "completed_at": fresh_completed_at}
    }))
    with patch("src.providers.bizrouter.requests.post") as m_post, \
         patch("src.providers.bizrouter.requests.get",
               return_value=_get_download()) as m_get:
        BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()
    assert m_get.call_count == 1  # 다운로드 1회만 — 상태 재조회 없음
    assert (tmp_path / "01.mp4").exists()
    assert json.loads(pending.read_text()).get("01.mp4") is None


# --- status=failed ---

def test_failed_status_with_safety_marker_raises_blocked_and_clears_pending(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_blocked", cost_krw=608)), \
         patch("src.providers.bizrouter.requests.get",
               return_value=_get_status("failed", cost_krw=608,
                                        video_uri=None,
                                        extra={"error": "blocked by content policy"})):
        with pytest.raises(BlockedError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    # 종결 상태다 — 더 이어받을 게 없으므로 pending에서 지워야 다음 실행이 새로
    # 제출할 수 있다(옛 실패한 operation에 영원히 묶이지 않는다).
    assert json.loads(pending.read_text()).get("01.mp4") is None


def test_failed_status_without_safety_marker_raises_terminal_clears_pending_but_records_cost(tmp_path):
    """failed는 환불을 뜻하지 않는다 — cost_krw는 접수 시점에 이미 청구됐다.
    실패한 생성도 예산 가드의 누적 지출에는 반드시 반영돼야 한다.

    Task 15 리뷰 Critical 1: 평범한 RuntimeError를 던지면 generate_clips가
    "일시적 오류"로 오인해 최대 max_retries번 재시도하며 매번 새 POST(=새
    과금)를 낸다 — TerminalGenerationError여야 재시도 없이 한 번만 시도된다
    (전체 파이프라인 레벨 회귀 테스트는 tests/test_generate.py 참고)."""
    from src.providers.base import TerminalGenerationError
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_failed", cost_krw=608)), \
         patch("src.providers.bizrouter.requests.get",
               return_value=_get_status("failed", cost_krw=608,
                                        video_uri=None,
                                        extra={"error": "internal server error"})):
        with pytest.raises(TerminalGenerationError) as exc_info:
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert not isinstance(exc_info.value, BlockedError)
    data = json.loads(pending.read_text())
    assert data.get("01.mp4") is None  # 터미널 실패 — 항목은 정리된다
    # 하지만 청구된 608원은 누적 지출 장부(예산 가드용)에는 남아 있어야 한다.
    assert data["_total_spent_krw"] == 608


# --- 다운로드 실패/빈 파일 ---

def test_download_error_keeps_pending_entry_and_does_not_resubmit(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_dl_fail")) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"),
                            requests.exceptions.ConnectionError("network blip")]):
        with pytest.raises(requests.exceptions.ConnectionError):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    entry = json.loads(pending.read_text())["01.mp4"]
    assert entry["operation_id"] == "op_dl_fail"  # 이미 과금됨 — 유지
    assert not (tmp_path / "01.mp4").exists()

    # 다음 호출: 다운로드가 이번엔 성공 — 재제출 없이 이어받는다.
    with patch("src.providers.bizrouter.requests.post") as m_post2, \
         patch("src.providers.bizrouter.requests.get",
               return_value=_get_download()):
        BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post2.assert_not_called()
    assert (tmp_path / "01.mp4").exists()
    assert json.loads(pending.read_text()).get("01.mp4") is None


def test_empty_download_keeps_pending_entry(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_empty")), \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download(content=b"")]):
        with pytest.raises(RuntimeError, match="비어"):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text())["01.mp4"]["operation_id"] == "op_empty"


# --- 제출 전 로컬 검증 (조합 위반, 과금 전에 막는다) ---

def test_rejects_4s_at_1080p_without_calling_api(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="1080p"):
            BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
                "x", tmp_path / "01.mp4", 4, "1080p", "16:9", None)
    m_post.assert_not_called()


def test_rejects_unsupported_duration_without_calling_api(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError):
            BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
                "x", tmp_path / "01.mp4", 5, "720p", "16:9", None)
    m_post.assert_not_called()


def _dummy_images(tmp_path, n):
    imgs = []
    for i in range(n):
        p = tmp_path / f"ref{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 8)
        imgs.append(p)
    return imgs


def test_rejects_reference_images_with_non_8s_duration(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    imgs = _dummy_images(tmp_path, 1)
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="reference_images"):
            BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", imgs)
    m_post.assert_not_called()


def test_rejects_more_than_three_reference_images(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    imgs = _dummy_images(tmp_path, 4)
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="3"):
            BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
                "x", tmp_path / "01.mp4", 8, "720p", "16:9", imgs)
    m_post.assert_not_called()


def test_accepts_exactly_three_reference_images_at_8s(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    imgs = _dummy_images(tmp_path, 3)
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted()) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
            "x", tmp_path / "01.mp4", 8, "720p", "16:9", imgs)
    assert len(m_post.call_args.kwargs["json"]["reference_images"]) == 3


def test_rejects_unknown_resolution_without_calling_api(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="알 수 없는 해상도"):
            BizrouterVideoProvider(api_key="k", pending_path=tmp_path / "p.json").generate(
                "x", tmp_path / "01.mp4", 4, "480p", "16:9", None)
    m_post.assert_not_called()


def test_rejects_model_not_in_price_table_without_calling_api(tmp_path):
    """Important 3(Task 15 리뷰): 가격표(BIZROUTER_KRW_PER_SEC)에 없는 모델을
    조용히 0원으로 취급하면 예산 가드가 무력화된다("spent + 0 <= budget"은
    항상 참). budget_krw를 아예 안 줬어도 가격을 모르는 모델은 제출 자체를
    막는다 — 예산 가드 여부와 무관하게 청구액을 모르는 채로 돈을 쓰지
    않는다."""
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="알 수 없는 model"):
            BizrouterVideoProvider(
                api_key="k", model="unpriced/made-up-model",
                pending_path=tmp_path / "p.json", budget_krw=100000,
            ).generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()


def test_missing_cost_krw_in_post_response_does_not_silently_zero_out_ledger(tmp_path):
    """Important 3(Task 15 리뷰): cost_krw가 POST 응답에 없으면 0원으로 조용히
    처리하면 안 된다 — 예산 가드가 무력화된다. operation_id는 보존해서(이미
    제출됐을 수 있으므로) 다음 호출이 재제출이 아니라 이어받기를 하도록
    한다."""
    import json as _json
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    resp = _resp(202, {"operation_id": "op_no_cost", "status": "pending"})  # cost_krw 없음
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(RuntimeError, match="cost_krw"):
            BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    data = _json.loads(pending.read_text())
    assert data["01.mp4"]["operation_id"] == "op_no_cost"
    assert data.get("_total_spent_krw", 0) == 0  # 알 수 없는 비용을 0으로 세지 않는다

    # 다음 호출: 재제출 없이 이어받아야 한다(operation_id를 이미 알고 있으므로).
    # 폴링 응답에서 cost_krw를 알게 되면 그제서야 장부에 반영한다.
    with patch("src.providers.bizrouter.requests.post") as m_post2, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed", cost_krw=608), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post2.assert_not_called()
    assert _json.loads(pending.read_text())["_total_spent_krw"] == 608


# --- 원화 예산 가드 ---

def test_budget_exceeded_blocks_before_post(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider, BudgetExceededError
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(BudgetExceededError):
            BizrouterVideoProvider(
                api_key="k", pending_path=tmp_path / "p.json", budget_krw=500,
            ).generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()  # 4초 * 152원/초 = 608원 > 500원 상한


def test_budget_guard_allows_exact_task17_boundary_0_plus_608_le_608(tmp_path):
    """Important 4(Task 15 리뷰): Task 17의 실제 실행 설정은
    `--budget-krw 608 --limit 1`이고 4초*152원/초=608원짜리 샷 하나다 — 즉
    0(누적) + 608(추정) <= 608(상한). 코드가 <=를 쓰므로 통과해야 정상이지만
    (< 였다면 막혔을 것이다), 실제 돈이 오갈 정확한 설정이라 경계값을 직접
    테스트해서 확인해둔다."""
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(cost_krw=608)) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed", cost_krw=608), _get_download()]):
        BizrouterVideoProvider(
            api_key="k", pending_path=tmp_path / "p.json", budget_krw=608,
            total_shots=1,
        ).generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_called_once()


def test_budget_not_exceeded_allows_submission(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(cost_krw=608)) as m_post, \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(
            api_key="k", pending_path=tmp_path / "p.json", budget_krw=1000,
        ).generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    m_post.assert_called_once()


def test_budget_accumulates_across_shots_and_persists_in_pending_file(tmp_path):
    """예산 가드는 추정치가 아니라 응답으로 온 실제 cost_krw를 누적해야 하고,
    그 누적치는 provider 인스턴스가 바뀌어도(=새 CLI 실행) pending.json을
    통해 이어져야 한다."""
    from src.providers.bizrouter import BizrouterVideoProvider, BudgetExceededError
    pending = tmp_path / "pending.json"

    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_a", cost_krw=608)), \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=pending, budget_krw=1000).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)

    assert json.loads(pending.read_text())["_total_spent_krw"] == 608

    # 새 provider 인스턴스(=새 실행)로 두 번째 샷을 시도 — 누적 608원 + 이번
    # 추정 608원 = 1216원 > 1000원 상한이므로 막혀야 한다.
    with patch("src.providers.bizrouter.requests.post") as m_post2:
        with pytest.raises(BudgetExceededError):
            BizrouterVideoProvider(api_key="k", pending_path=pending, budget_krw=1000).generate(
                "x", tmp_path / "02.mp4", 4, "720p", "16:9", None)
    m_post2.assert_not_called()


def test_budget_exceeded_message_reports_spent_and_remaining_shots_in_korean(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider, BudgetExceededError
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(BudgetExceededError) as exc_info:
            BizrouterVideoProvider(
                api_key="k", pending_path=tmp_path / "p.json", budget_krw=100,
                total_shots=33,
            ).generate("x", tmp_path / "05.mp4", 4, "720p", "16:9", None)
    m_post.assert_not_called()
    msg = str(exc_info.value)
    assert "100" in msg  # 상한
    assert "샷" in msg


# --- pending.json 원자적 쓰기 ---

def test_pending_write_is_atomic_no_stray_tmp_file(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted()), \
         patch("src.providers.bizrouter.requests.get",
               side_effect=[_get_status("completed"), _get_download()]):
        BizrouterVideoProvider(api_key="k", pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert pending.exists()
    assert not (tmp_path / "pending.json.tmp").exists()


def test_poll_times_out_and_records_operation_id_for_resume(tmp_path):
    from src.providers.bizrouter import BizrouterVideoProvider
    pending = tmp_path / "pending.json"
    with patch("src.providers.bizrouter.requests.post",
               return_value=_post_accepted(operation_id="op_slow", cost_krw=608)), \
         patch("src.providers.bizrouter.requests.get",
               return_value=_get_status("processing")), \
         patch("src.providers.bizrouter.time.sleep"):
        with pytest.raises(TimeoutError):
            BizrouterVideoProvider(
                api_key="k", pending_path=pending, poll_seconds=1, timeout_seconds=2,
            ).generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    entry = json.loads(pending.read_text())["01.mp4"]
    assert entry["operation_id"] == "op_slow"  # 재제출 대신 이어받을 수 있도록 기록됨


# --- cli.py 배선: --provider / --budget-krw / --limit ---
#
# tests/test_cli.py는 Task 15의 수정 허용 목록에 없으므로(기존 --fake/--live
# 게이트 테스트를 그대로 보존해야 한다), 새로 추가되는 배선(provider 선택,
# 예산 가드, --limit)에 대한 테스트는 이 파일에 둔다.

def test_cli_provider_bizrouter_without_key_exits_before_generation(tmp_path, monkeypatch):
    monkeypatch.delenv("BIZROUTER_API_KEY", raising=False)
    from src.cli import main
    with pytest.raises(SystemExit):
        main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(tmp_path),
              "--live", "--provider", "bizrouter"])


def test_cli_limit_truncates_shots_passed_to_generate_clips(tmp_path, monkeypatch):
    """--limit N은 generate_clips에 넘기는 shotlist 자체를 잘라야 한다 —
    generate_clips의 재시도/이어받기 정책은 건드리지 않는다."""
    import src.cli as cli
    from src.generate import GenerateResult

    captured = {}

    def spy(sl, b, provider, out_dir, *a, **kw):
        captured["n_shots"] = len(sl.shots)
        return GenerateResult()

    monkeypatch.setattr(cli, "generate_clips", spy)
    out = tmp_path / "out"
    assert cli.main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(out),
                     "--limit", "1"]) == 0
    assert captured["n_shots"] == 1


def test_cli_no_limit_passes_full_shotlist_to_generate_clips(tmp_path, monkeypatch):
    import src.cli as cli
    from src.generate import GenerateResult
    from src import constants as C

    captured = {}

    def spy(sl, b, provider, out_dir, *a, **kw):
        captured["n_shots"] = len(sl.shots)
        return GenerateResult()

    monkeypatch.setattr(cli, "generate_clips", spy)
    out = tmp_path / "out"
    assert cli.main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(out)]) == 0
    assert captured["n_shots"] == C.TOTAL_CUTS


def test_cli_budget_exceeded_prints_korean_message_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    """예산 가드가 트립되면 raw traceback이 아니라 한글 안내를 찍고 0이 아닌
    종료 코드를 내야 한다."""
    import src.cli as cli
    from src.providers.bizrouter import BudgetExceededError

    def raiser(sl, b, provider, out_dir, *a, **kw):
        raise BudgetExceededError("예산 초과: 누적 청구액 608원 + 이번 샷 예상 608원이 상한 500원을 넘습니다.")

    monkeypatch.setattr(cli, "generate_clips", raiser)
    out = tmp_path / "out"
    code = cli.main(["generate", "--brief", "brief/hanbit.yaml", "--out", str(out)])
    text = capsys.readouterr().out
    assert code == 1
    assert "예산 초과" in text


def test_providers_live_bizrouter_with_key_constructs_bizrouter_provider(monkeypatch):
    """Important 5(Task 15 리뷰): 지금까지 _providers()의 타입 단언은 Fake
    경로만 커버했다 — 이게 정확히 Task 17이 실행할 배선(--live --provider
    bizrouter, 키 있음)인데 아무 테스트도 실제로 BizrouterVideoProvider가
    만들어지는지 확인하지 않았다. 네트워크 호출 없이 생성자만 확인한다."""
    from src.cli import _providers
    from src.providers.bizrouter import BizrouterVideoProvider

    monkeypatch.setenv("BIZROUTER_API_KEY", "dummy-key-for-construction-test-only")
    vprov, tprov = _providers(True, "bizrouter", budget_krw=608, total_shots=1)
    assert isinstance(vprov, BizrouterVideoProvider)
    assert vprov.api_key == "dummy-key-for-construction-test-only"
    assert vprov.budget_krw == 608
    assert vprov.total_shots == 1


def test_pending_ledger_lives_under_out_dir_not_cwd(monkeypatch, tmp_path):
    """장부는 --out 아래(out/clips/)에 있어야 한다.

    Task 17 실제 1컷 발사에서 발견: cli.py 가 pending_path 를 --out 과 무관한
    상대경로 Path("clips/...")로 넘겨서, 장부가 저장소 루트에 떨어졌다.
    다른 cwd 에서 실행하면 진행 중인 유료 작업 기록을 통째로 못 찾고 재제출한다
    (= 이중 과금). 서로 다른 --out 이 같은 샷 인덱스 키로 충돌하는 문제도 같다.

    기존 테스트가 전부 pending_path 를 명시로 넘겨서 CLI 자체의 경로 배선이
    한 번도 검증되지 않았다 — 실제 실행에서야 드러난 종류의 결함이다.
    """
    from src.cli import _providers

    monkeypatch.setenv("BIZROUTER_API_KEY", "dummy-key-for-construction-test-only")
    vprov, _ = _providers(True, "bizrouter", out=tmp_path / "build_x")
    assert vprov.pending_path == tmp_path / "build_x" / "clips" / ".pending-bizrouter.json"

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    gprov, _ = _providers(True, "gemini", out=tmp_path / "build_y")
    assert gprov.pending_path == tmp_path / "build_y" / "clips" / ".pending.json"


def test_providers_live_bizrouter_with_key_constructs_bizrouter_tts_provider(monkeypatch):
    """Task 16: --live --provider bizrouter가 TTS 쪽도 더 이상
    _UnimplementedBizrouterTTS(SystemExit)가 아니라 실제 BizrouterTTSProvider를
    반환해야 한다."""
    from src.cli import _providers
    from src.providers.bizrouter import BizrouterTTSProvider

    monkeypatch.setenv("BIZROUTER_API_KEY", "dummy-key-for-construction-test-only")
    vprov, tprov = _providers(True, "bizrouter")
    assert isinstance(tprov, BizrouterTTSProvider)
    assert tprov.api_key == "dummy-key-for-construction-test-only"


# --- BizrouterTTSProvider (Task 16) ---
#
# /v1/audio/speech 는 video provider와 달리 동기(sync) API다: POST 한 번으로
# 오디오 바이트가 바로 온다(202+폴링도, 다운로드 창도, cost_krw도 없다) —
# 브리프가 명시한 그대로다. 그래서 pending.json 이중장부는 필요 없지만,
# "제출 전 로컬 검증"(voice 화이트리스트)과 "안전차단은 BlockedError로
# 구분해 재시도 안 함" 두 원칙은 video provider와 동일하게 지킨다.

def _tts_resp(status_code=200, content=b"RIFFfake-wav-bytes-not-real-audio",
              json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.content = content
    r.text = text
    r.headers = headers if headers is not None else {}
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("응답에 JSON 바디가 없음(바이너리 응답)")
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error")
    else:
        r.raise_for_status.side_effect = None
    return r


def test_tts_happy_path_sends_bearer_header_five_field_body_and_writes_bytes_verbatim(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "nested" / "seg00.wav"
    resp = _tts_resp(200, content=b"RIFFfake-wav-bytes-not-real-audio")
    with patch("src.providers.bizrouter.requests.post", return_value=resp) as m_post:
        BizrouterTTSProvider(api_key="secret-key-abc",
                             instructions="차분하고 권위 있는 톤").synthesize(
            "안녕하세요, 반갑습니다", out, "ash")

    m_post.assert_called_once()
    args, kwargs = m_post.call_args
    assert args[0] == "https://api.bizrouter.ai/v1/audio/speech"
    assert kwargs["headers"] == {"Authorization": "Bearer secret-key-abc"}
    assert kwargs["json"] == {
        "model": "openai/gpt-4o-mini-tts",
        "input": "안녕하세요, 반갑습니다",
        "voice": "ash",
        "instructions": "차분하고 권위 있는 톤",
        "response_format": "wav",
    }
    # 부모 디렉터리가 없었는데도(tmp_path / "nested") 자동으로 생성됐다.
    assert out.exists()
    assert out.read_bytes() == b"RIFFfake-wav-bytes-not-real-audio"
    # API 키가 출력 파일에 흘러들지 않는다.
    assert b"secret-key-abc" not in out.read_bytes()


def test_tts_rejects_unsupported_gemini_voice_locally_before_any_request(tmp_path):
    """Gemini 음성("Charon")은 gpt-4o-mini-tts 화이트리스트에 없다 — 그대로
    보내면 API가 400을 낼 값을 로컬에서 먼저, 한글 에러로 막는다. 요청 자체가
    나가지 않아야 한다.

    파이프라인 기본 음성은 C.TTS_VOICE("ash")로 바뀌었지만(실제 샘플 청취 후
    확정), 이 방어는 그대로 필요하다: brief나 CLI가 임의 음성을 넘길 수 있다."""
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "seg00.wav"
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError, match="Charon"):
            BizrouterTTSProvider(api_key="k").synthesize(
                "안녕하세요", out, "Charon")
    m_post.assert_not_called()
    assert not out.exists()


def test_tts_error_message_is_korean(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    with patch("src.providers.bizrouter.requests.post") as m_post:
        with pytest.raises(ValueError) as exc_info:
            BizrouterTTSProvider(api_key="k").synthesize(
                "안녕하세요", tmp_path / "seg00.wav", "Charon")
    assert "지원" in str(exc_info.value)
    m_post.assert_not_called()


def test_tts_non_2xx_raises_with_response_body_included_in_message(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "seg00.wav"
    resp = _tts_resp(400, json_data={"error": {"message": "invalid voice for this model"}})
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(RuntimeError, match="invalid voice for this model"):
            BizrouterTTSProvider(api_key="k").synthesize("텍스트", out, "ash")
    assert not out.exists()


def test_tts_safety_refusal_raises_blocked_error_not_generic(tmp_path):
    """안전/정책 차단은 BlockedError로 떼야 generate 계열이 이걸 "일시적
    오류"로 오인해 재시도(재과금 위험)하지 않는다 — video provider의
    _is_safety_error와 동일한 원칙."""
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "seg00.wav"
    resp = _tts_resp(400, json_data={
        "error": {"code": "content_policy_violation",
                  "message": "요청이 moderation policy에 의해 blocked 되었습니다"}})
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(BlockedError):
            BizrouterTTSProvider(api_key="k").synthesize("텍스트", out, "ash")
    assert not out.exists()


def test_tts_empty_response_body_raises_without_writing_zero_byte_file(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "seg00.wav"
    resp = _tts_resp(200, content=b"")
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(RuntimeError):
            BizrouterTTSProvider(api_key="k").synthesize("텍스트", out, "ash")
    assert not out.exists()


def test_tts_content_length_mismatch_raises_without_writing_partial_file(tmp_path):
    """서버가 예고한 길이(Content-Length)보다 실제로 받은 바이트가 짧으면
    (전송 중 잘림) 부분 파일을 그대로 저장하지 않는다 — 나중에 assemble/
    verify가 이걸 멀쩡한 오디오로 오인하면 안 된다."""
    from src.providers.bizrouter import BizrouterTTSProvider
    out = tmp_path / "seg00.wav"
    resp = _tts_resp(200, content=b"short", headers={"Content-Length": "999999"})
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(RuntimeError):
            BizrouterTTSProvider(api_key="k").synthesize("텍스트", out, "ash")
    assert not out.exists()


def test_tts_api_key_never_appears_in_exception_message(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    secret = "sk-super-secret-do-not-leak-0000000000000000"
    out = tmp_path / "seg00.wav"
    resp = _tts_resp(401, json_data={"error": "unauthorized"})
    with patch("src.providers.bizrouter.requests.post", return_value=resp):
        with pytest.raises(RuntimeError) as exc_info:
            BizrouterTTSProvider(api_key=secret).synthesize("텍스트", out, "ash")
    assert secret not in str(exc_info.value)
    assert not out.exists()


def test_tts_uses_configured_model_and_instructions(tmp_path):
    from src.providers.bizrouter import BizrouterTTSProvider
    resp = _tts_resp(200, content=b"bytes")
    with patch("src.providers.bizrouter.requests.post", return_value=resp) as m_post:
        BizrouterTTSProvider(api_key="k", model="openai/gpt-4o-mini-tts",
                             instructions="테스트 지시문").synthesize(
            "텍스트", tmp_path / "seg00.wav", "verse")
    body = m_post.call_args.kwargs["json"]
    assert body["model"] == "openai/gpt-4o-mini-tts"
    assert body["instructions"] == "테스트 지시문"
    assert body["voice"] == "verse"
