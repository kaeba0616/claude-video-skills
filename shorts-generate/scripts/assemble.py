#!/usr/bin/env python3
"""Veo 클립들을 쇼츠(9:16)로 조립: 정규화 → concat → 한글 ASS 자막 번인."""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

DEFAULT_W, DEFAULT_H = 720, 1280
# 시스템 한글 폰트. Linux 는 나눔고딕(fc-list :lang=ko 로 확인), Windows 는
# 기본 탑재된 맑은 고딕 — 나눔고딕이 설치돼 있으면 --font NanumGothic 으로 지정.
DEFAULT_FONT = "Malgun Gothic" if sys.platform == "win32" else "NanumGothic"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds):
    """초(float) → ASS 타임스탬프 'h:mm:ss.cc'."""
    cs = round(seconds * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def subs_to_ass(subs, font=DEFAULT_FONT, play_w=DEFAULT_W, play_h=DEFAULT_H):
    """[{start, end, text}] → ASS 문서 문자열. 하단 1/3 위치, 큰 외곽선 자막."""
    size = round(play_h * 0.05)          # 1280 기준 64pt
    margin_v = round(play_h * 0.22)      # 하단에서 22% 위 (쇼츠 UI 겹침 회피)
    out = [ASS_HEADER.format(w=play_w, h=play_h, font=font, size=size,
                             margin_v=margin_v)]
    for s in subs:
        text = str(s["text"]).replace("\n", r"\N")
        out.append(f"Dialogue: 0,{ass_time(float(s['start']))},"
                   f"{ass_time(float(s['end']))},Shorts,,0,0,0,,{text}\n")
    return "".join(out)


def probe(path):
    """ffprobe로 width/height/duration 조회."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return {"width": d["streams"][0]["width"],
            "height": d["streams"][0]["height"],
            "duration": float(d["format"]["duration"])}


def build_ffmpeg_cmd(clips, out, w, h, ass_path=None):
    """혼합 화면비 클립들을 w×h로 정규화 후 concat, 필요시 ASS 번인."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", str(c)]
    parts = []
    for i in range(len(clips)):
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps=30,format=yuv420p[v{i}];"
            f"[{i}:a]aresample=48000[a{i}];")
    pairs = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    parts.append(f"{pairs}concat=n={len(clips)}:v=1:a=1[vc][ac]")
    if ass_path:
        # ffmpeg 필터 인자의 특수문자 이스케이프
        esc = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        parts.append(f";[vc]ass='{esc}'[vout]")
        vmap = "[vout]"
    else:
        vmap = "[vc]"
    cmd += ["-filter_complex", "".join(parts),
            "-map", vmap, "-map", "[ac]",
            "-c:v", "libx264", "-crf", "19", "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(out)]
    return cmd


def main(argv=None):
    ap = argparse.ArgumentParser(description="쇼츠 조립 (concat + ASS 자막)")
    ap.add_argument("--clips", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--subtitles", help="subs.json 경로 (최종 타임라인 기준)")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--font", default=DEFAULT_FONT)
    args = ap.parse_args(argv)

    for c in args.clips:
        if not pathlib.Path(c).is_file():
            sys.exit(f"클립 없음: {c}")

    with tempfile.TemporaryDirectory() as td:
        ass_path = None
        if args.subtitles:
            subs = json.loads(pathlib.Path(args.subtitles).read_text())
            ass_path = pathlib.Path(td) / "subs.ass"
            ass_path.write_text(subs_to_ass(subs, font=args.font,
                                            play_w=args.width, play_h=args.height))
        cmd = build_ffmpeg_cmd(args.clips, args.out, args.width, args.height,
                               ass_path=ass_path)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print("ffmpeg 실패", file=sys.stderr)
            return r.returncode

    info = probe(args.out)
    if (info["width"], info["height"]) != (args.width, args.height):
        print(f"경고: 출력 해상도 불일치 {info}", file=sys.stderr)
        return 1
    print(json.dumps({"out": args.out, **info}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
