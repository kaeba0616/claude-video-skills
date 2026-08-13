#!/usr/bin/env python3
"""프롬프트 목록 → 프레임 체이닝 생성 → 조립까지 한 번에 (시연용 원샷 파이프라인).

클립 N의 마지막 프레임을 클립 N+1의 첫 프레임(--image)으로 넣어 잔·소품·구도를
물리적으로 잇는다. 이미 있는 클립은 건너뛰므로(이어하기) 실패한 장면만 재생성되고,
전부 있으면 과금 없이 조립만 다시 한다.

    python3 chain.py --prompts prompts/scene_01.txt prompts/scene_02.txt ... \
        --out-dir output/<폴더> [--subtitles subs.json]

주의: 자동 체인은 중간 프레임 검수 없이 진행된다. 프롬프트가 실제 마지막
프레임과 어긋나면 이어짐이 어색해진다 — 처음 만드는 소재는 SKILL.md 5-0처럼
한 클립씩 생성하며 프레임을 보고 다음 프롬프트를 조정하는 쪽이 안전하고,
검증된 프롬프트 세트의 재실행·시연에 이 스크립트를 쓴다.
"""
import argparse
import json
import pathlib
import subprocess
import sys

import assemble as assemble_mod
import veo_generate


def extract_last_frame(clip, png):
    """클립의 마지막 프레임을 png로 추출한다 (다음 클립의 --image 입력)."""
    png.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-sseof", "-0.2", "-i", str(clip),
         "-frames:v", "1", "-update", "1", "-y", str(png)],
        check=True)
    return png


def main(argv=None):
    ap = argparse.ArgumentParser(description="체이닝 생성 + 조립 원샷 파이프라인")
    ap.add_argument("--prompts", nargs="+", required=True, type=pathlib.Path,
                    help="장면 순서대로 프롬프트 파일들")
    ap.add_argument("--out-dir", required=True, type=pathlib.Path)
    ap.add_argument("--subtitles", help="subs.json 경로 (없으면 자막 없이 조립)")
    ap.add_argument("--model", default="google/veo-3.1-lite")
    ap.add_argument("--resolution", default="720p",
                    choices=veo_generate.VALID_RESOLUTIONS)
    ap.add_argument("--aspect-ratio", default="9:16",
                    choices=veo_generate.VALID_ASPECT_RATIOS)
    ap.add_argument("--duration", type=int, default=8,
                    choices=veo_generate.VALID_DURATIONS)
    ap.add_argument("--font", default=assemble_mod.DEFAULT_FONT)
    ap.add_argument("--no-chain", dest="chain", action="store_false",
                    help="체이닝 없이 각 장면을 독립(text-to-video)으로 생성")
    ap.add_argument("--yes", action="store_true", help="비용 확인 프롬프트를 건너뛴다")
    args = ap.parse_args(argv)

    for pf in args.prompts:
        if not pf.is_file():
            sys.exit(f"프롬프트 파일 없음: {pf}")

    clips_dir = args.out_dir / "clips"
    frames_dir = args.out_dir / "frames"
    n = len(args.prompts)
    clips = [clips_dir / f"scene_{i + 1:02d}.mp4" for i in range(n)]
    todo = [i for i in range(n) if not clips[i].is_file()]

    # ✋ 승인 게이트 — 돈이 나가는 건 여기부터다
    if todo:
        print(f"{len(todo)}/{n}개 클립을 생성합니다 (제출 시점 과금, 실패해도 과금, "
              f"lite 720p 실측 클립당 약 615원).", file=sys.stderr)
        if not args.yes:
            ans = input("계속하려면 'chain' 을 입력하세요: ")
            if ans.strip() != "chain":
                sys.exit("취소했습니다.")
    else:
        print("모든 클립이 이미 있습니다 — 과금 없이 조립만 다시 합니다.", file=sys.stderr)

    api = veo_generate.Api(veo_generate.load_api_key()) if todo else None
    total_cost = 0
    generated = 0
    prev_last = None
    for i in range(n):
        clip = clips[i]
        if clip.is_file():
            print(f"[{i + 1}/{n}] 있음 — 건너뜀: {clip}", file=sys.stderr)
        else:
            print(f"[{i + 1}/{n}] 생성: {args.prompts[i]}"
                  + (f"  (첫 프레임: {prev_last})" if prev_last else ""),
                  file=sys.stderr)
            prompt = args.prompts[i].read_text(encoding="utf-8")
            payload = veo_generate.build_payload(
                args.model, prompt, args.resolution, args.aspect_ratio,
                args.duration, image=prev_last)
            r = veo_generate.generate(api, payload, clip)
            total_cost += r.get("cost_krw") or 0
            generated += 1
        if args.chain and i < n - 1:
            prev_last = extract_last_frame(
                clip, frames_dir / f"scene_{i + 1:02d}_last.png")

    asm = ["--clips", *map(str, clips), "--out", str(args.out_dir / "final.mp4"),
           "--font", args.font]
    if args.subtitles:
        asm += ["--subtitles", args.subtitles]
    rc = assemble_mod.main(asm)
    if rc:
        sys.exit(rc)
    print(json.dumps({"final": str(args.out_dir / "final.mp4"),
                      "generated": generated, "skipped": n - generated,
                      "cost_krw_total": total_cost}, ensure_ascii=False))


if __name__ == "__main__":
    main()
