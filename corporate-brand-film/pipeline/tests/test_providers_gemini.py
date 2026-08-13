"""GeminiVideoProvider / GeminiTTSProvider — 전부 mock, 네트워크 호출 없음.

과금 API를 실제로 부르는 테스트는 이 파일에 하나도 없다. google.genai.Client는
전부 MagicMock으로 패치한다.
"""
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors
from src.providers.base import AmbiguousSubmissionError, BlockedError


def _write_dummy_video_bytes(path) -> None:
    Path(path).write_bytes(b"fake-mp4-bytes")


def _fake_client(done=True, videos=1):
    """videos[i].video.save()가 실제로 바이트를 쓰도록 side_effect를 건다 —
    call count만 세면 generate_clips의 원자적 rename(partial.replace(target))이
    실제로 뭘 옮기는지 검증할 수 없다."""
    op = MagicMock()
    op.done = done
    op.error = None
    gen_videos = []
    for _ in range(videos):
        gv = MagicMock()
        gv.video.save.side_effect = _write_dummy_video_bytes
        gen_videos.append(gv)
    op.response.generated_videos = gen_videos
    op.response.rai_media_filtered_reasons = None
    client = MagicMock()
    client.models.generate_videos.return_value = op
    client.operations.get.return_value = op
    return client, op


def test_video_provider_saves_result(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        p = GeminiVideoProvider()
        p.generate("a factory", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    op.response.generated_videos[0].video.save.assert_called_once()
    assert (tmp_path / "01.mp4").exists()
    assert (tmp_path / "01.mp4").stat().st_size > 0


def test_video_provider_downloads_before_saving(tmp_path):
    """Gemini Developer API는 완료된 영상을 uri로만 돌려준다 — save()가 성공하려면
    files.download()로 video_bytes를 먼저 채워야 한다(안 하면 SDK가
    NotImplementedError('Saving remote videos is not supported.')를 던진다)."""
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    video = op.response.generated_videos[0].video
    client.files.download.assert_called_once_with(file=video)


def test_video_provider_raises_blocked_when_no_videos_returned(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client(videos=0)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)


def test_video_provider_does_not_download_when_blocked(tmp_path):
    """차단이면 다운로드할 영상 자체가 없다 — files.download를 부르면 안 된다."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client(videos=0)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    client.files.download.assert_not_called()


def test_video_provider_raises_on_operation_error_not_blocked(tmp_path):
    """operation.error가 채워진 건 진짜 API 오류(쿼터, 서버 오류 등)지 안전필터
    차단이 아니다 — BlockedError로 잘못 분류하면 generate_clips가 재시도 없이
    그냥 건너뛰어 버린다. 반드시 구분되는 예외(TerminalGenerationError)여야
    한다."""
    from src.providers.base import TerminalGenerationError
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    op.error = {"code": 8, "message": "Resource exhausted"}
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(TerminalGenerationError) as exc_info:
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert not isinstance(exc_info.value, BlockedError)
    op.response.generated_videos[0].video.save.assert_not_called()


def test_video_provider_passes_duration_and_resolution(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    cfg = client.models.generate_videos.call_args.kwargs["config"]
    assert cfg.duration_seconds == 4
    assert cfg.resolution == "720p"
    assert cfg.aspect_ratio == "16:9"


def test_video_provider_passes_negative_prompt_as_dedicated_field(tmp_path):
    """부정어는 긍정 prompt 문자열이 아니라 negative_prompt 전용 필드로 넘어가야
    한다 — 긍정 프롬프트에 섞인 부정어는 오히려 그 요소를 불러오는 역효과가
    있다고 알려져 있다(prompts.py 참고)."""
    from src import constants as C
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider().generate(
            "a factory, no subtitles", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    call = client.models.generate_videos.call_args
    cfg = call.kwargs["config"]
    assert cfg.negative_prompt == C.VEO_NEGATIVE_PROMPT
    # prompt 인자 자체는 provider가 건드리지 않고 그대로 전달돼야 한다 —
    # negative_prompt를 prompt 문자열에 다시 이어붙이면 안 된다.
    assert call.kwargs["prompt"] == "a factory, no subtitles"


def test_video_provider_rejects_1080p_with_non_8s_duration_without_calling_api(tmp_path):
    """1080p/4k는 8초만 허용된다(Veo 문서 확인 완료) — 이 조합을 로컬에서
    막지 않으면 잘못된 요청 하나가 그대로 과금 제출까지 간다."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="1080p"):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "1080p", "16:9", None)
    client.models.generate_videos.assert_not_called()


def test_video_provider_rejects_unsupported_duration_without_calling_api(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 5, "720p", "16:9", None)
    client.models.generate_videos.assert_not_called()


def test_video_provider_rejects_unknown_resolution_without_calling_api(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="알 수 없는 해상도"):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "480p", "16:9", None)
    client.models.generate_videos.assert_not_called()


def _dummy_images(tmp_path, n=5):
    imgs = []
    for i in range(n):
        p = tmp_path / f"ref{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 8)
        imgs.append(p)
    return imgs


def test_video_provider_rejects_more_than_three_reference_images_without_calling_api(tmp_path):
    """예전엔 4장째부터 조용히 잘라서 보냈다 — 그러면 사용자가 의도한
    레퍼런스 이미지 중 일부가 빠진 채로 그냥 과금됐다. 이제는 로컬에서
    막고 API를 아예 부르지 않는다."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    imgs = _dummy_images(tmp_path, n=5)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="3"):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 8, "720p", "16:9", imgs)
    client.models.generate_videos.assert_not_called()


def test_video_provider_accepts_exactly_three_reference_images(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    imgs = _dummy_images(tmp_path, n=3)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        # reference_images는 8초 길이에서만 동작한다는 보고가 있어 duration=8로 호출한다.
        GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 8, "720p", "16:9", imgs)
    cfg = client.models.generate_videos.call_args.kwargs["config"]
    assert len(cfg.reference_images) == 3


def test_video_provider_uses_asset_reference_type_not_style(tmp_path):
    """Task 13 리뷰에서 확인됨: Veo 3.1은 STYLE 레퍼런스 이미지를 지원하지
    않는다 — ASSET만 써야 첫 실호출에서 400을 맞지 않는다.

    소스에서는 소문자 "asset"을 넘기지만, google-genai의
    CaseInSensitiveEnum이 대문자 정규형(ASSET)으로 정규화한다 — 실제로
    STYLE이 아니라 ASSET 계열인지가 검증 대상이지, 대소문자 자체가 아니다
    (대소문자가 실제 API에 영향을 주는지는 라이브 호출 없이는 확인 불가 —
    task-13-report.md 참고)."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    imgs = _dummy_images(tmp_path, n=1)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 8, "720p", "16:9", imgs)
    cfg = client.models.generate_videos.call_args.kwargs["config"]
    ref_type = cfg.reference_images[0].reference_type
    assert ref_type.upper() == "ASSET"
    assert ref_type.upper() != "STYLE"


def test_video_provider_rejects_reference_images_with_non_8s_duration(tmp_path):
    """reference_images는 8초에서만 동작한다는 보고가 있다 — 4초(기본 클립
    길이)로 잘못 호출하면 로컬에서 막아야 과금 제출 후 400을 피한다."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    imgs = _dummy_images(tmp_path, n=1)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(ValueError, match="reference_images"):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", imgs)
    client.models.generate_videos.assert_not_called()


def test_video_provider_times_out(tmp_path):
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client(done=False)
    with patch("src.providers.gemini.genai.Client", return_value=client), \
         patch("src.providers.gemini.time.sleep"):
        with pytest.raises(TimeoutError):
            GeminiVideoProvider(poll_seconds=1, timeout_seconds=3).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)


def test_timeout_records_pending_operation(tmp_path):
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=False)
    op.name = "operations/abc123"
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client), \
         patch("src.providers.gemini.time.sleep"):
        with pytest.raises(TimeoutError):
            GeminiVideoProvider(poll_seconds=1, timeout_seconds=2,
                                pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text())["01.mp4"] == "operations/abc123"


def test_pending_operation_is_resumed_instead_of_resubmitted(tmp_path):
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=True)
    op.name = "operations/abc123"
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"01.mp4": "operations/abc123"}))
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider(pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    client.models.generate_videos.assert_not_called()
    assert json.loads(pending.read_text()) == {}


def test_resumed_operation_expired_raises_clear_error_without_resubmitting(tmp_path):
    """Veo 결과는 생성 후 2일이 지나면 서버에서 삭제된다. 만료된 operation을
    이어받으려 하면 서버가 404를 준다 — 이때 자동으로 재제출하면 이미 끝났을
    수도 있는 작업에 중복 과금이 날 수 있으므로, 절대 재제출하지 않고 사람이
    읽을 수 있는 에러만 던져야 한다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client(done=True)
    client.operations.get.side_effect = errors.ClientError(
        404, {"error": {"message": "Operation not found", "status": "NOT_FOUND"}})
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"01.mp4": "operations/expired"}))
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(Exception, match="만료"):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    client.models.generate_videos.assert_not_called()
    # 재시도 시 다시 같은 항목을 이어받도록(=재제출하지 않도록) pending 항목은 그대로 남는다.
    assert json.loads(pending.read_text())["01.mp4"] == "operations/expired"


def test_operation_name_is_recorded_immediately_at_submission_not_only_at_timeout(tmp_path):
    """Critical 1 회귀 테스트: 제출(generate_videos)이 성공한 순간 바로
    pending.json에 진짜 operation 이름이 남아야 한다. 타임아웃 분기에서만
    기록하면, Ctrl-C/OOM/일반 예외 등 타임아웃이 아닌 방식으로 폴링 도중
    중단됐을 때 아무 기록도 없이 유료 작업이 사라진다.

    time.sleep이 (타임아웃이 아니라) 임의의 예외로 죽는 상황을 흉내 내서,
    TimeoutError 분기를 아예 타지 않고도 pending.json에 진짜 operation
    이름이 이미 남아 있는지 확인한다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=False)
    op.name = "operations/submitted-then-crashed"
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client), \
         patch("src.providers.gemini.time.sleep", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            GeminiVideoProvider(poll_seconds=1, timeout_seconds=999,
                                pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text())["01.mp4"] == "operations/submitted-then-crashed"


def test_pending_write_is_atomic_no_stray_tmp_file(tmp_path):
    """pending.json 갱신은 temp write + rename이어야 한다 — 쓰는 도중 죽어도
    기존 파일이 반쯤 잘린 채로 남으면 안 된다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=True)
    op.name = "operations/xyz"
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider(pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert pending.exists()
    assert json.loads(pending.read_text()) == {}
    assert not (tmp_path / "pending.json.tmp").exists()


def test_pending_entry_survives_a_failed_download_and_is_not_resubmitted(tmp_path):
    """Critical 1 잔여 이슈(2차 리뷰): 폴링(op.done)이 성공한 뒤에도
    files.download()가 실패할 수 있다(네트워크 오류 등) — 그 시점엔 이미
    Google 서버에서 생성이 끝나서 과금까지 된 상태다. pending을 폴링 직후에
    지워버리면 이 완료된(=이미 과금된) operation의 기록이 사라지고,
    generate_clips의 재시도가 완전히 새로운 유료 생성을 또 제출한다.

    폴링 성공 → 다운로드 실패 → pending 항목이 살아남는지, 그리고 다음
    호출이 재제출 없이 operations.get()으로 이어받는지까지 확인한다.
    "다시는 generate_videos가 불리지 않는다"는 단언이 핵심이다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=True)
    op.name = "operations/download-fails"
    client.files.download.side_effect = RuntimeError("network blip during download")
    pending = tmp_path / "pending.json"

    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(RuntimeError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    # 이미 완료된(=과금된) operation의 기록이 지워지면 안 된다.
    assert json.loads(pending.read_text())["01.mp4"] == "operations/download-fails"
    assert not (tmp_path / "01.mp4").exists()
    # 여기까지 generate_videos는 최초 제출 1번만 불렸어야 한다(다운로드
    # 실패는 폴링 뒤에 났으므로 제출 자체는 이미 끝난 뒤다).
    submits_after_first_call = client.models.generate_videos.call_count
    assert submits_after_first_call == 1

    # 다음 호출: 다운로드가 이번엔 성공한다고 하자 — 재제출 없이 이어받아야 한다.
    client.files.download.side_effect = None
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiVideoProvider(pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    # 핵심 단언: 두 번째 호출이 재제출하지 않았다 — 호출 횟수가 그대로 1이다.
    assert client.models.generate_videos.call_count == submits_after_first_call
    assert (tmp_path / "01.mp4").exists()
    assert json.loads(pending.read_text()) == {}


def test_pending_entry_survives_when_saved_file_ends_up_empty(tmp_path):
    """다운로드는 성공했지만 save()가 어떤 이유로든 빈 파일만 남기는 경우도
    "다운로드가 durable하게 끝났다"고 볼 수 없다 — 이 경우도 pending을
    지우면 안 된다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(done=True)
    op.name = "operations/empty-save"
    op.response.generated_videos[0].video.save.side_effect = None  # 아무것도 안 씀
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(RuntimeError, match="비어"):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text())["01.mp4"] == "operations/empty-save"


def test_terminal_op_error_clears_pending_so_next_call_submits_fresh(tmp_path):
    """2차 리뷰 후속: op.done인데 op.error(안전 무관 진짜 실패)가 난 건
    "in-flight"가 아니라 완전히 끝난 상태다 — 더 기다려도, 다시 조회해도
    같은 결과만 나온다. 다운로드할 결과물 자체가 없으므로 pending을 안
    지우면 이 샷이 옛 실패한 operation에 영원히 묶인다(예: 프롬프트를
    고쳐도 다음 실행이 여전히 그 실패를 이어받으려 한다). 그래서 이 경로는
    pending을 즉시 지운다.

    다만 이게 "자동 재시도해도 안전하다"는 뜻은 아니다(Task 15 리뷰 Critical
    1) — 여기서 확인하는 건 provider가 종결 상태를 TerminalGenerationError로
    명시해서 던진다는 것과, 사람(또는 명시적으로 다시 부른 호출자)이 의도적
    으로 두 번째 generate()를 부르면 그건 정당한 새 제출이라는 것이다.
    generate_clips 자체가 이 예외를 자동으로 재시도하지 않는다는 건
    test_generate.py의 파이프라인 레벨 테스트가 확인한다."""
    import json
    from src.providers.base import TerminalGenerationError
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    op.name = "operations/failed"
    op.error = {"code": 8, "message": "Resource exhausted"}
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(TerminalGenerationError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    # 종결된 실패다 — pending에 남아 있으면 안 된다.
    assert json.loads(pending.read_text()) == {}

    # 사람이 의도적으로 다시 부른 두 번째 호출은 (이어받기가 아니라) 새로
    # 제출해야 한다.
    client2, op2 = _fake_client()
    op2.name = "operations/fresh-attempt"
    with patch("src.providers.gemini.genai.Client", return_value=client2):
        GeminiVideoProvider(pending_path=pending).generate(
            "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    client2.models.generate_videos.assert_called_once()


def test_safety_blocked_op_error_clears_pending_so_shot_is_not_wedged(tmp_path):
    """안전필터 차단도 op.error 경로처럼 종결된 상태다 — BlockedError로
    분류되어 generate_clips가 재시도 없이 기록만 하고 넘어가지만, pending에
    남아 있으면 이 샷은 프롬프트를 고쳐도 영원히 옛 차단된 operation에
    묶인다. 차단은 과금 걱정이 없는 경로이므로 지우는 데 비용 안전상
    걸림돌이 없다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    op.name = "operations/blocked"
    op.error = {"code": 3, "message": "Request blocked due to safety policy violation"}
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text()) == {}


def test_empty_generated_videos_block_clears_pending_so_shot_is_not_wedged(tmp_path):
    """빈 generated_videos로 인한 차단(위와 다른 코드경로)도 마찬가지로
    종결 상태다 — pending에 남으면 안 된다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client(videos=0)
    op.name = "operations/filtered"
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert json.loads(pending.read_text()) == {}


def test_submission_failure_raises_ambiguous_not_generic_exception(tmp_path):
    """Critical 2: generate_videos() 호출 자체가 예외를 던지면(클라이언트
    타임아웃 등) 서버에 요청이 도달했는지 알 수 없다 — generate_clips의
    일반 재시도(retry) 경로를 타면 안 되므로 반드시 AmbiguousSubmissionError
    여야 한다(BlockedError도 아니고 그냥 Exception도 아니다)."""
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    client.models.generate_videos.side_effect = TimeoutError("client read timeout")
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(AmbiguousSubmissionError):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)


def test_submission_failure_leaves_marker_so_next_run_does_not_resubmit(tmp_path):
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    client.models.generate_videos.side_effect = TimeoutError("client read timeout")
    pending = tmp_path / "pending.json"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(AmbiguousSubmissionError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    marker = json.loads(pending.read_text())["01.mp4"]
    assert marker != ""
    assert not marker.startswith("operations/")  # 진짜 operation 이름이 아니다


def test_leftover_submission_marker_is_not_auto_resubmitted(tmp_path):
    """이전 실행이 제출 도중(성공 여부를 모르는 채로) 끊겨서 마커만 남아 있는
    경우 — 다음 실행은 절대 자동으로 재제출하면 안 된다. 이미 과금됐을 수
    있는 작업에 또 돈을 쓸 위험이 있다."""
    import json
    from src.providers.gemini import GeminiVideoProvider
    client, _ = _fake_client()
    pending = tmp_path / "pending.json"
    # 마커를 직접 심어서 "제출 직후 크래시"를 재현한다 — 실제 마커 값은
    # 구현 세부사항이므로 먼저 정상 크래시 경로로 실제 값을 얻어와 재사용한다.
    client.models.generate_videos.side_effect = RuntimeError("boom")
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(AmbiguousSubmissionError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)

    client2, _ = _fake_client()
    with patch("src.providers.gemini.genai.Client", return_value=client2):
        with pytest.raises(AmbiguousSubmissionError):
            GeminiVideoProvider(pending_path=pending).generate(
                "x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    client2.models.generate_videos.assert_not_called()
    # 마커는 사람이 직접 지울 때까지 그대로 남아 있어야 한다.
    assert json.loads(pending.read_text())["01.mp4"] != ""


def test_op_error_with_safety_marker_is_classified_as_blocked_not_retried(tmp_path):
    """Important 4(Task 13 2차 리뷰): operation.error가 안전/정책 관련 표현을
    담고 있으면 TerminalGenerationError(과금됨, 재시도 안 됨)가 아니라
    BlockedError(과금 안 됨, 재시도 안 됨)로 분류해야 한다 — 차단된 프롬프트를
    다시 시도해도 어차피 또 막힐 뿐이므로 굳이 돈을 쓰지 않는다."""
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    op.error = {"code": 3, "message": "Request blocked due to safety policy violation"}
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)


def test_op_error_without_safety_marker_is_terminal_not_generic_retryable(tmp_path):
    """대조군(Task 15 리뷰 Critical 1로 갱신): 안전 관련 표현이 없는 오류(쿼터
    초과 등)도 BlockedError/AmbiguousSubmissionError는 아니지만, 그렇다고
    generate_clips의 일반 재시도 경로(평범한 Exception)를 타면 안 된다 —
    operation.error는 이미 서버에서 종결된 실패이고, 재시도는 새 제출(=새
    과금)이 된다. TerminalGenerationError로 명시해야 generate_clips가
    재시도하지 않는다."""
    from src.providers.base import TerminalGenerationError
    from src.providers.gemini import GeminiVideoProvider
    client, op = _fake_client()
    op.error = {"code": 8, "message": "Resource exhausted"}
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(TerminalGenerationError) as exc_info:
            GeminiVideoProvider().generate("x", tmp_path / "01.mp4", 4, "720p", "16:9", None)
    assert not isinstance(exc_info.value, BlockedError)
    assert not isinstance(exc_info.value, AmbiguousSubmissionError)


def _fake_tts_client(pcm=b"\x00\x01" * 100, blocked=False, finish_reason=None):
    client = MagicMock()
    resp = MagicMock()
    if blocked:
        resp.candidates = []
    else:
        candidate = MagicMock()
        candidate.finish_reason = finish_reason
        candidate.content.parts = [MagicMock()]
        candidate.content.parts[0].inline_data.data = pcm
        resp.candidates = [candidate]
    client.models.generate_content.return_value = resp
    return client, resp


def test_tts_provider_writes_valid_wav(tmp_path):
    from src.providers.gemini import GeminiTTSProvider
    pcm = b"\x11\x22" * 500
    client, _ = _fake_tts_client(pcm=pcm)
    out = tmp_path / "vo.wav"
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiTTSProvider().synthesize("안녕하세요", out, "Charon")
    assert out.exists()
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        assert w.readframes(w.getnframes()) == pcm


def test_tts_provider_passes_model_and_voice(tmp_path):
    from src.providers.gemini import GeminiTTSProvider
    client, _ = _fake_tts_client()
    with patch("src.providers.gemini.genai.Client", return_value=client):
        GeminiTTSProvider(model="gemini-2.5-flash-preview-tts").synthesize(
            "hello", tmp_path / "vo.wav", "Kore")
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash-preview-tts"
    voice_cfg = kwargs["config"].speech_config.voice_config.prebuilt_voice_config
    assert voice_cfg.voice_name == "Kore"


def test_tts_provider_raises_blocked_when_no_candidates(tmp_path):
    from src.providers.gemini import GeminiTTSProvider
    client, _ = _fake_tts_client(blocked=True)
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiTTSProvider().synthesize("x", tmp_path / "vo.wav", "Charon")


def test_tts_provider_raises_blocked_on_safety_finish_reason(tmp_path):
    from src.providers.gemini import GeminiTTSProvider
    client, resp = _fake_tts_client(finish_reason="SAFETY")
    resp.candidates[0].content.parts = []
    with patch("src.providers.gemini.genai.Client", return_value=client):
        with pytest.raises(BlockedError):
            GeminiTTSProvider().synthesize("x", tmp_path / "vo.wav", "Charon")
