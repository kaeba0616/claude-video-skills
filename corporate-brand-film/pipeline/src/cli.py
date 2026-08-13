"""파이프라인 CLI.

기본은 --fake (API 호출 없음). 실제 생성은 --live 를 명시해야 한다.
--fake 와 --live 는 상호배타 — 둘 다 주면 argparse가 즉시 거부한다.
--live 일 때 어떤 실제 provider 를 쓸지는 --provider 로 고른다(기본 gemini,
하위호환 — 과거엔 --live 만으로 Gemini를 썼다).

서브커맨드: script / beats / shots / prompts / generate / voice / assemble / verify / all
"""
import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from src import constants as C
from src.assemble import assemble
from src.beats import build_beatsheet
from src.brief import load_brief
from src.generate import generate_clips
from src.overlay import build_ass
from src.prompts import build_all
from src.providers.base import BudgetExceededError, FakeTTSProvider, FakeVideoProvider
from src.script import build_script
from src.shotlist import Shotlist, build_shotlist
from src.verify import judge, measure
from src.voice import build_narration_track, narration_audio_seconds


def _ctx(brief_path: str):
    b = load_brief(brief_path)
    sc = build_script(b)
    bs = build_beatsheet(sc)
    sl = build_shotlist(bs, b)
    return b, sc, bs, sl


def _write_scenario(sc, out: Path) -> None:
    lines = ["# 나레이션 대본\n"]
    for act in sc.acts:
        lines.append(f"\n## {act.name}" + ("" if act.narrated else " (무나레이션)"))
        lines.extend(f"- {l}" for l in act.lines)
    (out / "scenario.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pending_path(out: Path | None, name: str) -> Path:
    """pending 장부 경로 — 생성될 클립과 같은 디렉터리(out/clips/).

    out 이 없으면(테스트가 _providers 를 직접 부르는 경우) 관례상 cwd 기준
    clips/ 를 쓴다. 실제 CLI 경로는 항상 out 을 넘긴다.
    """
    return (Path(out) if out else Path(".")) / "clips" / name


def _providers(live: bool, provider: str = "gemini",
               budget_krw: int | None = None, total_shots: int | None = None,
               out: Path | None = None):
    """(VideoProvider, TTSProvider) 를 만든다.

    out 은 --out 디렉터리다. pending 장부는 반드시 그 아래
    (out/clips/)에 둔다 — 장부는 "지금 서버에 어떤 유료 작업이 떠 있는지"의
    유일한 기록이고, 그 작업이 만들어낼 클립과 같은 위치에 있어야 한다.
    상대경로로 두면 다른 cwd 에서 실행했을 때 장부를 통째로 못 찾아
    진행 중인 유료 작업을 잊고 재제출한다(= 이중 과금). 서로 다른 --out 끼리
    샷 인덱스 키가 충돌하는 문제도 같이 막는다.
    (Task 17 실제 1컷 발사에서 발견 — 테스트는 전부 pending_path 를 명시로
    넘겨서 CLI 자체의 경로 배선이 한 번도 검증되지 않았다.)

    live=False(기본)면 무조건 Fake — 네트워크 호출도, 과금도 없다. --provider는
    live=True 일 때만 의미가 있다: 어떤 실제 provider(gemini/bizrouter)를 쓸지
    고른다. 두 provider 모두 필요한 API 키가 없으면 어떤 provider도 만들지 않고
    여기서 즉시 SystemExit — 실제 생성 호출부(generate_clips/voice 단계)에
    닿기 전에 막는다.

    FakeTTSProvider() 는 seconds 를 고정하지 않는다 — 글자수/발화속도로
    길이를 내는 실제 TTS 충실도를 유지해야 build_narration_track()의
    "막 길이 초과" 방어 로직과 최종 산출물 길이 계산이 의미 있게 검증된다
    (seconds 고정이면 모든 막이 같은 길이가 되어 초과 버그를 못 잡는다).
    """
    if not live:
        return FakeVideoProvider(), FakeTTSProvider()
    if provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("--live 에는 GEMINI_API_KEY 환경변수가 필요합니다.")
        from src.providers.gemini import GeminiTTSProvider, GeminiVideoProvider
        return (GeminiVideoProvider(pending_path=_pending_path(out, ".pending.json")),
                GeminiTTSProvider())
    if provider == "bizrouter":
        api_key = os.environ.get("BIZROUTER_API_KEY")
        if not api_key:
            raise SystemExit("--provider bizrouter 에는 BIZROUTER_API_KEY 환경변수가 필요합니다.")
        from src.providers.bizrouter import BizrouterTTSProvider, BizrouterVideoProvider
        vprov = BizrouterVideoProvider(
            api_key=api_key,
            pending_path=_pending_path(out, ".pending-bizrouter.json"),
            budget_krw=budget_krw, total_shots=total_shots)
        tprov = BizrouterTTSProvider(api_key=api_key)
        return vprov, tprov
    raise SystemExit(f"알 수 없는 --provider: {provider!r} (지원: fake, gemini, bizrouter)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="videogen")
    ap.add_argument("stage", choices=["script", "beats", "shots", "prompts",
                                      "generate", "voice", "assemble", "verify", "all"])
    ap.add_argument("--brief", default="brief/hanbit.yaml")
    ap.add_argument("--out", default="build")
    ap.add_argument("--music", default=None)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fake", action="store_true", help="Fake provider 사용 (기본값, API 호출 없음)")
    g.add_argument("--live", action="store_true", help="실제 API 호출 — 과금됨")
    ap.add_argument("--provider", choices=["fake", "bizrouter", "gemini"], default="fake",
                    help="--live 일 때 쓸 실제 provider (기본 fake — --live 없이는 항상 Fake)")
    ap.add_argument("--budget-krw", type=int, default=None,
                    help="누적 원화 상한 — 넘으면 제출 전에 중단(bizrouter). "
                         "--limit 과는 다른 사고를 막는다(총 지출 vs 폭주 루프)")
    ap.add_argument("--limit", type=int, default=None,
                    help="이번 실행에서 새로 생성할 샷 개수 상한(폭주 방지). "
                         "이미 만들어진 클립을 건너뛰는 이어받기와는 별개다")
    ap.add_argument("--transcribe", action="store_true",
                    help="검증에서 faster-whisper 로 전사해 발화속도·무나레이션 비율을 "
                         "재측정한다. 기본값은 끄기 — 이 두 지표는 silencedetect 로 "
                         "ML 없이도 실측되고, whisper 는 모델 다운로드와 GPU 가 필요하다")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    b, sc, bs, sl = _ctx(a.brief)
    live = a.live
    provider = a.provider
    if live and provider == "fake":
        # 하위호환: 과거엔 --live 만으로 Gemini를 썼다. --provider 를 명시하지
        # 않으면(기본값 그대로면) 여전히 Gemini로 간주한다 — --provider의
        # 기본값(fake)은 어디까지나 "--live 없이는 절대 과금 안 됨"을 보장하기
        # 위한 것이지, --live와 짝지어졌을 때 아무것도 안 고른다는 뜻이 아니다.
        provider = "gemini"

    # --limit: 총 지출이 아니라 "이번 실행에서 몇 개나 새로 시도할지"를 막는
    # 별도 가드다(폭주 방지). generate_clips 자체(재시도/이어받기 정책)는
    # 건드리지 않고, 여기서 넘길 shotlist 를 미리 잘라서 넘긴다 — assemble 등
    # 다른 단계는 여전히 전체 sl 을 쓴다(부분 생성으로 최종본을 조립하지 않는다).
    gen_sl = Shotlist(shots=sl.shots[:a.limit]) if a.limit is not None else sl

    if a.stage in ("script", "all"):
        _write_scenario(sc, out)
    if a.stage in ("beats", "all"):
        (out / "beatsheet.json").write_text(
            json.dumps([asdict(x) for x in bs.beats], ensure_ascii=False, indent=2),
            encoding="utf-8")
    if a.stage in ("shots", "all"):
        (out / "shotlist.json").write_text(
            json.dumps([asdict(s) for s in sl.shots], ensure_ascii=False, indent=2),
            encoding="utf-8")
    if a.stage in ("prompts", "all"):
        pdir = out / "prompts"
        pdir.mkdir(exist_ok=True)
        for i, p in build_all(sl, b).items():
            (pdir / f"{i:02d}.txt").write_text(p + "\n", encoding="utf-8")

    if a.stage in ("generate", "voice", "assemble", "all"):
        vprov, tprov = _providers(live, provider, budget_krw=a.budget_krw,
                                  total_shots=len(gen_sl.shots), out=out)

    if a.stage in ("generate", "all"):
        try:
            r = generate_clips(gen_sl, b, vprov, out / "clips")
        except BudgetExceededError as e:
            # 예산 가드는 제출(POST) 전에 막으므로, 여기 도달했다는 건 이미
            # 상한을 넘긴 어떤 추가 과금도 나가지 않았다는 뜻이다 — 원시
            # traceback 대신 한글 안내로 깔끔하게 중단한다.
            print(f"⚠ {e}")
            return 1
        # Important 2(Task 15 리뷰): 종결 실패(terminal — 이미 과금됨)도 개수를
        # 보여주고, 실제 누적 청구액(bizrouter처럼 spent_krw를 노출하는
        # provider에 한해)도 요약 줄에 찍는다. 그전엔 _total_spent_krw가
        # pending.json 내부에만 있어서 예산 안에서 끝난 실행도 사용자가 실제로
        # 얼마를 냈는지 알 방법이 없었다.
        summary = (f"생성 {len(r.made)} · 건너뜀 {len(r.skipped)} · "
                  f"차단 {len(r.blocked)} · 거절 {len(r.rejected)} · "
                  f"종결실패 {len(r.terminal)} · "
                  f"애매 {len(r.ambiguous)}")
        spent_krw = getattr(vprov, "spent_krw", None)
        if spent_krw is not None:
            summary += f" · 누적 청구액 {spent_krw}원"
        print(summary)
    if a.stage in ("voice", "all"):
        # synthesize_narration(전체 대본 미리듣기, 타임라인 미정렬)이 아니라
        # build_narration_track 을 쓴다: overlay.py의 ASS 자막이 beat.start
        # 기준으로 깔리므로, 최종 조립에 들어가는 음성도 같은 격자에 맞춰
        # 타임라인 정렬되어야 한다. 정렬 안 된 트랙을 assemble()에 넘기면
        # 자막·클립과 나레이션이 서로 어긋난다.
        build_narration_track(bs, tprov, out / "vo.wav", work_dir=out / "_vo")
    if a.stage in ("assemble", "all"):
        # 폰트가 없으면 libass 가 조용히 다른 폰트로 폴백한다 — 에러 없이
        # 한글이 전부 틀린 폰트로 렌더링된 완성본이 나온다. 조립 전에 막는다.
        if missing := C.font_check():
            for msg in missing:
                print(f"⚠ {msg}")
            raise SystemExit("폰트가 없어 조립을 중단합니다.")
        # 자막은 막의 예약 길이가 아니라 실제 나레이션 음성 길이에 맞춰 깐다 —
        # 예약 길이에는 안전 여유가 들어 있어서, 그대로 쓰면 음성이 끝난 뒤에도
        # 자막이 남아 막마다 최대 2.6초까지 밀린다.
        audio = narration_audio_seconds(bs, out / "_vo")
        assemble(sl, out / "clips", build_ass(bs, sl, b, audio_seconds=audio),
                 out / "vo.wav", out / "final.mp4",
                 Path(a.music) if a.music else None, out / "_work")

    if a.stage in ("verify", "all"):
        # speech_rate 를 계산하려면 measure()에 대본의 실제 글자수를 넘겨야
        # 한다 — 안 넘기면 speech_rate가 None으로 나와 판정에서 통째로
        # 빠진다. narrated 막의 chars 합계가 실제로 발화된 글자수다.
        chars = sum(beat.chars for beat in bs.beats if beat.narrated)
        # transcribe 를 live 에 묶으면 안 된다: 실제 생성을 했다는 것과 whisper 로
        # 전사할 수 있다는 건 별개 조건이다. 무나레이션 비율·발화속도는 Task 11b
        # 이후 silencedetect 로 ML 없이 실측하므로 whisper 는 선택적 정밀화다.
        # (실제 실행에서 발견 — 33컷을 다 만들고 조립까지 끝낸 뒤 검증 단계에서
        #  faster-whisper 미설치로 죽었다.)
        # 오디오 지표는 배경음이 섞인 믹스가 아니라 나레이션 트랙에서 잰다
        # (verify.measure 의 voice_path 참고). vo.wav 가 없으면 최종본으로 잰다.
        vo = out / "vo.wav"
        m = measure(out / "final.mp4", chars=chars, transcribe=a.transcribe,
                    voice_path=vo if vo.exists() else None)
        if a.transcribe:
            # 막 단위로 대본과 실제 발화를 대조한다 — TTS 가 문장을 통째로
            # 흘려도 완성본 전체 비율로는 묻힌다(실측: 4자 누락 = 전체의 1.6%).
            from src.verify import narration_coverage
            from src.voice import narration_segments
            segs = [(next(x.name for x in bs.beats if abs(x.start - s) < 0.01), txt)
                    for s, txt in narration_segments(bs)]
            m.coverage = narration_coverage(segs, out / "_vo")
        v = judge(m)
        parts = [f"{m.duration:.1f}s", f"{m.cuts}컷", f"{m.cuts_per_min:.1f}컷/분",
                 f"평균 {m.avg_shot:.2f}s"]
        if m.speech_rate is not None:
            parts.append(f"발화속도 {m.speech_rate:.2f}자/초")
        if m.silent_ratio is not None:
            parts.append(f"무나레이션 {m.silent_ratio * 100:.1f}%")
        if m.heard_ratio is not None:
            name, want, heard = m.worst_beat
            parts.append(f"발화 최저 {name} {heard}/{want}자"
                         f"({m.heard_ratio * 100:.0f}%)")
        print(" · ".join(parts))
        for k, ok in v.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {k}")
        # all은 종료 코드로 실패를 알리지 않으므로(아래 참고), 표를 훑다
        # 놓치기 쉬운 FAIL 행 하나를 별도 배너로 못박는다 — 통과/실패 여부가
        # 배너의 유무만으로 항상 분명해야 한다(배너가 없으면 = 통과, 절대
        # 애매하게 두지 않는다). 다음 태스크부터 실제 과금이 시작되므로,
        # 이 배너는 verify뿐 아니라 all에서도 반드시 찍는다.
        failed = [k for k, ok in v.items() if not ok]
        if failed:
            print(f"⚠ 검증 실패: {', '.join(failed)} — `videogen verify` 로 확인하세요")
        else:
            print("✓ 검증 통과: 모든 지표가 대역 안입니다")
        # verify 단독 실행만 판정으로 종료 코드를 낸다. all은 산출물 생성이
        # 목적이므로(판정은 정보 제공), 파이프라인 자체가 성공하면 0을
        # 돌려준다 — Fake TTS의 알려진 silent_ratio 미달(PAUSE_FACTOR 여백을
        # 평탄한 톤이 못 쓰는 성질, 이미 분석됨)이 `all --fake`의 성공 여부를
        # 가리면 안 된다. `verify`를 따로 돌리면 그 실패가 그대로 드러난다.
        if a.stage == "verify":
            return 0 if all(v.values()) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
