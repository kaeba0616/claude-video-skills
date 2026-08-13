#!/usr/bin/env python3
"""bizrouter Veo 비디오 생성 클라이언트.

제출 → 폴링 → 완료 즉시 다운로드를 한 프로세스에서 원자적으로 수행한다.
(완료된 작업과 다운로드 링크는 1~2분 내 만료되므로 분리 실행 금지)
"""
import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("BIZROUTER_API_BASE", "https://api.bizrouter.ai/v1")
VALID_RESOLUTIONS = ("720p", "1080p", "4k")
VALID_ASPECT_RATIOS = ("16:9", "9:16")
VALID_DURATIONS = (4, 6, 8)
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[4]
MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".webp": "image/webp"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def load_api_key(search_dirs=None):
    """환경변수 우선, 없으면 .env 파일에서 BIZROUTER_API_KEY를 읽는다."""
    key = os.environ.get("BIZROUTER_API_KEY")
    if key:
        return key
    if search_dirs is None:
        search_dirs = [pathlib.Path.cwd(), PROJECT_ROOT]
    for d in search_dirs:
        env = pathlib.Path(d) / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith("BIZROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("BIZROUTER_API_KEY가 없습니다. 환경변수 또는 프로젝트 루트 .env에 설정하세요.")


def load_image(path):
    """이미지 파일을 API의 VideoImageInput({mime_type, data_base64})으로 변환한다."""
    path = pathlib.Path(path)
    mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime is None:
        raise ValueError(f"지원하지 않는 이미지 형식입니다 ({', '.join(MIME_BY_SUFFIX)}): {path}")
    return {"mime_type": mime, "data_base64": base64.b64encode(path.read_bytes()).decode()}


def build_payload(model, prompt, resolution, aspect_ratio, duration,
                  image=None, last_frame=None, reference_images=None,
                  negative_prompt=None):
    if not prompt or not prompt.strip():
        raise ValueError("prompt가 비어 있습니다")
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(f"resolution은 {VALID_RESOLUTIONS} 중 하나여야 합니다: {resolution}")
    if aspect_ratio not in VALID_ASPECT_RATIOS:
        raise ValueError(f"aspect_ratio는 {VALID_ASPECT_RATIOS} 중 하나여야 합니다: {aspect_ratio}")
    if duration not in VALID_DURATIONS:
        raise ValueError(f"duration_seconds는 {VALID_DURATIONS} 중 하나여야 합니다: {duration}")
    # API 제약: 프레임 지정(image/last_frame)과 참조 이미지는 배타적인 두 모드다
    if reference_images and (image is not None or last_frame is not None):
        raise ValueError("reference_images는 image/last_frame과 함께 쓸 수 없습니다 "
                         "(프레임 지정 모드와 참조 이미지 모드 중 하나만 선택)")
    payload = {"model": model, "prompt": prompt, "resolution": resolution,
               "aspect_ratio": aspect_ratio, "duration_seconds": duration}
    # 선택 필드는 값이 있을 때만 넣는다 (null을 보내면 역직렬화에서 거부됨)
    if image is not None:
        payload["image"] = load_image(image)
    if last_frame is not None:
        payload["last_frame"] = load_image(last_frame)
    if reference_images:
        payload["reference_images"] = [load_image(p) for p in reference_images]
    if negative_prompt and negative_prompt.strip():
        payload["negative_prompt"] = negative_prompt
    return payload


class Api:
    """bizrouter HTTP 전송 계층. 테스트에서는 이 클래스 대신 Fake를 주입한다."""

    def __init__(self, key, base=API_BASE):
        self.key = key
        self.base = base

    def request(self, method, path, payload=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        data = None
        if payload is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(payload).encode()
        try:
            with urllib.request.urlopen(req, data=data, timeout=180) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            raise ApiError(e.code, detail) from None
        return body if raw else json.loads(body)


def faststart_remux(path, runner=subprocess.run):
    """moov 아톰을 파일 앞으로 옮긴다 (무손실, 재인코딩 없음).

    Veo 원본은 moov가 mdat 뒤에 있어 일부 플레이어(특히 Windows에서 WSL 네트워크
    경로로 열 때)에서 재생되지 않는다. 클립은 재생성 비용이 크고 원본 링크도 이미
    만료됐으므로, 리먹스가 실패하면 경고만 하고 **원본을 그대로 보존**한다.
    """
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".faststart.tmp.mp4")
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-c", "copy",
           "-movflags", "+faststart", "-y", str(tmp)]
    try:
        proc = runner(cmd, capture_output=True)
    except FileNotFoundError:
        print("ffmpeg이 없어 faststart 리먹스를 건너뜁니다 (클립 자체는 정상)",
              file=sys.stderr)
        return False
    ok = proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0
    if not ok:
        print(f"faststart 리먹스 실패 — 원본을 유지합니다: {path}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(path)  # 성공했을 때만 교체
    return True


def generate(api, payload, out_path, poll_interval=5, timeout=600, faststart=True):
    """제출→폴링→완료 즉시 다운로드. 결과 메타데이터 dict를 반환한다."""
    sub = api.request("POST", "/video/generation", payload)
    op = sub["operation_id"]
    print(f"제출됨: operation_id={op} cost_krw={sub.get('cost_krw')}", file=sys.stderr)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = api.request("GET", f"/video/generation/{op}")
        status = st.get("status")
        if status == "completed":
            # 만료 전 즉시 다운로드 — 여기서 다른 작업을 끼워넣지 말 것
            data = api.request("GET", f"/video/generation/{op}/download", raw=True)
            out_path = pathlib.Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            # 다운로드를 끝낸 뒤에 리먹스 (만료 위험 구간을 벗어난 후)
            remuxed = faststart_remux(out_path) if faststart else False
            return {"operation_id": op, "cost_krw": sub.get("cost_krw"),
                    "bytes": len(data), "out": str(out_path),
                    "faststart": remuxed}
        if status == "failed":
            raise RuntimeError(f"생성 실패: {st}")
        time.sleep(poll_interval)
    raise TimeoutError(f"{timeout}초 내에 완료되지 않음: operation_id={op}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="bizrouter Veo 클립 생성")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt")
    src.add_argument("--prompt-file", type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="google/veo-3.1-lite")
    ap.add_argument("--resolution", default="720p", choices=VALID_RESOLUTIONS)
    ap.add_argument("--aspect-ratio", default="9:16", choices=VALID_ASPECT_RATIOS)
    ap.add_argument("--duration", type=int, default=8, choices=VALID_DURATIONS)
    ap.add_argument("--image", type=pathlib.Path,
                    help="첫 프레임으로 쓸 이미지 (image-to-video). 제품 광고에 사용")
    ap.add_argument("--last-frame", type=pathlib.Path,
                    help="마지막 프레임으로 쓸 이미지 (클립 간 이음새 연결)")
    ap.add_argument("--reference-image", type=pathlib.Path, action="append",
                    dest="reference_images", metavar="PATH",
                    help="피사체 외형 일관성용 참조 이미지 (여러 번 지정 가능)")
    ap.add_argument("--negative-prompt", help="영상에서 배제할 요소")
    ap.add_argument("--no-faststart", dest="faststart", action="store_false",
                    help="moov 아톰을 앞으로 옮기는 무손실 리먹스를 건너뛴다 "
                         "(기본은 수행 — 안 하면 일부 플레이어에서 재생 실패)")
    args = ap.parse_args(argv)

    prompt = args.prompt if args.prompt else args.prompt_file.read_text()
    payload = build_payload(args.model, prompt, args.resolution,
                            args.aspect_ratio, args.duration,
                            image=args.image, last_frame=args.last_frame,
                            reference_images=args.reference_images,
                            negative_prompt=args.negative_prompt)
    api = Api(load_api_key())
    try:
        result = generate(api, payload, args.out, faststart=args.faststart)
    except ApiError as e:
        if e.status == 403 or "blacklisted" in str(e.message):
            sys.exit("모델이 조직에서 차단되어 있습니다. bizrouter 콘솔의 "
                     "조직/모델 설정에서 해당 모델을 허용하세요.\n" + str(e))
        raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
