---
name: corporate-brand-film
description: Use when the user wants to produce a Korean corporate brand film / 기업 홍보영상 / 회사 소개영상 from a company description — generates the scenario, shot list, AI video clips (Veo 3.1), TTS narration, Korean typography, and assembles a finished mp4. Also use when editing an existing film made this way (rewriting narration, regenerating specific shots, changing music or the end card).
---

# 기업 홍보영상 제작

한국 기업 홍보영상 28편을 전사·컷분석해서 뽑은 문법으로 시나리오를 쓰고,
Veo 3.1로 컷을 생성해 88초 완성본까지 조립하는 파이프라인.

**입력은 YAML 한 파일.** 회사명·업종·창업연도·숫자 앵커·사업부 2개를 적으면
대본·비트시트·샷리스트·영문 프롬프트·자막이 전부 생성된다.

## ⛔ 돈이 나가는 파이프라인이다

Veo 3.1 Fast는 **초당 152원**. 33컷 완성본 하나가 **약 2만원**이다.
아래 순서를 건너뛰지 마라. 각 단계는 실제로 돈을 날린 사고에서 나왔다.

1. **Fake로 전 구간 먼저** — `--fake`(기본값)로 88초 완성본이 나오는지 확인.
   컬러바 배경이지만 편집·자막·타이밍·믹스가 전부 실물이다. 무료.
2. **드라이런** — `--live --limit 0`. 키 게이트와 provider 생성만 확인, 제출 0회.
3. **1컷 시험 발사** — `--live --limit 1 --budget-krw <1컷값>`. 약 600원.
   화질·톤·프롬프트 반영도를 사용자에게 보여주고 승인받는다.
4. **재실행으로 이어받기 확인** — 같은 명령을 한 번 더. 청구액이 안 늘어야 한다.
5. **전체 실행** — 사용자에게 예상 비용을 말하고 **명시적 승인**을 받은 뒤에만.

**`--budget-krw`를 항상 준다.** 응답의 실제 청구액을 누적해 상한을 넘으면
제출 전에 중단한다. 추정이 아니라 서버가 알려준 값이다.

`--limit N`은 샷리스트 앞 N개만 자르는 방식이라 **이어받기 상황에서는 무용지물**이다
(이미 만든 클립이 상한을 잡아먹는다). 드라이런과 1컷 발사에만 쓰고,
진짜 안전장치는 `--budget-krw`로 삼아라.

## 설치 확인

```bash
scripts/setup.sh          # ffmpeg · 한글 폰트 · Python 의존성 점검
```

`ffmpeg`/`ffprobe`, 나눔고딕(Regular + ExtraBold), Python 3.12+가 필요하다.
폰트가 없으면 자막이 **조용히 다른 폰트로 렌더링된다** — 파이프라인이 조립
전에 막지만, 미리 확인하는 게 낫다.

## 사용법

`./video <command>` 또는 `./video:<command>` 둘 다 된다. 래퍼가 절차를 강제한다. 돈이 나가는 명령은 예상 비용을 보여주고
확인 문구를 받아야 진행한다.

```bash
./video prepare                          # 실행 환경 점검
./video new 우리회사              # 템플릿 복사 → brief/우리회사.yaml

export VIDEO_BRIEF=brief/우리회사.yaml
./video script                          # 대본·비트·샷리스트·프롬프트 (무료·즉시)
./video preview                       # Fake 로 88초 완성본 (무료, 약 5분)

export BIZROUTER_API_KEY=...
./video testshot                       # 드라이런 → 1컷 (약 600원, 확인 받음)
./video produce                          # 전체 33컷 (약 2만원, 확인 받음)
./video verify                          # 레퍼런스 대역 판정
```

### 만든 뒤 고치기

**재조립은 무료다.** 클립과 음성이 있으면 자막·타이포·배경음 수정은 돈이 안 든다.
돈이 드는 건 클립 재생성뿐이다.

```bash
./video shots                  # 33컷 컨택트시트 → 어느 컷이 문제인지 지목 (무료)
./video stills 45 86           # 완성본에서 그 시점 스틸 (무료)

# 대본·자막·배경음을 고쳤다면
./video rebuild                # 재조립만 (무료. 나레이션을 고쳤으면 그 막만 TTS 재합성)

# 특정 컷의 그림이 마음에 안 들면
#   1) brief 의 해당 subject 문구를 고치고
#   2) 그 컷만 다시 생성
./video redo 5 13 27           # 컷당 약 600원, 나머지는 건드리지 않는다
./video rebuild                # 그 다음 재조립
```

`redo` 는 이전 클립을 지우지 않고 `clips_prev/` 로 옮긴다 — 새로 만든 게 더
나쁠 수 있다. 되돌리려면 그 파일을 `clips/` 로 되돌려 놓고 `rebuild` 하면 된다.

**사용자가 "이 컷이 이상하다"고 하면 `shots` 로 먼저 번호를 확인하게 하라.**
번호 없이 추측해서 `redo` 하면 멀쩡한 컷에 돈을 쓴다.

`VIDEO_OUT`(기본 build) · `VIDEO_PROVIDER`(기본 bizrouter) · `VIDEO_MUSIC` 으로
기본값을 바꾼다.

**사용자를 대신해 `produce`을 실행하지 마라.** 예상 비용을 말하고 승인을 받은 뒤,
사용자가 직접 치게 하거나 승인을 명시적으로 확인하고 실행한다.

플래그를 직접 쓰려면 `pipeline/` 에서 `python3 -m src.cli <stage> ...` 를 부른다.
래퍼는 그 위의 얇은 껍데기다.

배경음이 필요하면 `scripts/make_bed.sh assets/bed.wav 88`로 만든다.
사용자가 준 음원이 있으면 그걸 `--music`에 넘겨라 — 합성 베드는 대용품이다.

## 브리프 작성

`brief/template.yaml`에 규칙이 주석으로 다 있다. 특히 지키라:

- **`style_prefix`와 `subjects`는 영어로만.** 한글이 섞이면 제출 전에 거부된다.
- **글자가 적힌 피사체를 쓰지 마라** — 명판·간판·증서·눈금. 부정 프롬프트로
  "글자 없음"을 요구하면서 피사체로 글자 물건을 지정하면 모순이고, Veo는
  피사체를 따른다. 한글 타이포는 전부 오버레이로 얹으므로 클립은 깨끗한 판이어야 한다.
- **`style_prefix`에 필름 관련 단어를 넣지 마라** — `35mm film grain`이라고 썼더니
  Veo가 "35mm 필름을 보여줘"로 읽고 33컷 중 10컷에 퍼포레이션 구멍을 그렸다.
- 숫자 앵커는 **서로 다른 자릿수**로. 43개국과 43년이 같이 있으면 안 읽힌다.

## 검증

완성본을 레퍼런스 28편과 **같은 계측기**로 재서 같은 대역에 넣는다.

| 지표 | 대역 | 근거 |
|---|---|---|
| 컷/분 | 10.2~31.5 | 나레이션 중심 22편의 10~90 백분위 |
| 평균 샷 | 1.9~5.9초 | 〃 |
| 무나레이션 | 9~40% | 〃 |
| 발화속도 | 3.5~4.7 자/초 | 유형별 목표치(표준 기업 나레이션) |
| 발화 누락 | 막별 90% 이상 | 대본 대비 실제 발화 |

**대역을 만져서 통과시키지 마라.** 벗어나면 숫자를 그대로 보고하고,
고칠 거면 대본이나 비트 배분을 고쳐라.

발화 누락 검사는 **TTS가 문장을 통째로 흘리는 사고**를 잡는 유일한 장치다.
그 사고는 길이·발화속도·자막·전사 어느 것도 잡지 못한다 — 요청한 텍스트와
실제 발화를 막 단위로 대조해야만 보인다. `--transcribe`가 필요하다
(faster-whisper + GPU).

## 더 읽을 것

- `references/reference-analysis.md` — 28편 해부. 5막 구조, 8가지 오프닝 훅,
  전환 장치의 위치, 타이포 3계층. **시나리오를 손보기 전에 읽어라.**
- `references/pitfalls.md` — 실제로 돈을 날렸거나 완성본을 망친 함정들.
  프롬프트를 바꾸거나 새 provider를 붙이기 전에 읽어라.

## 구조

```
pipeline/src/
  brief.py      YAML → Brief          script.py    5막 대본
  beats.py      막별 초·컷 역산        shotlist.py  샷 33개, 사이즈 5종 로테이션
  prompts.py    영문 Veo 프롬프트      overlay.py   타이포 3계층 ASS
  voice.py      막별 TTS + 타임라인 정렬
  assemble.py   트림·이어붙이기·자막·믹스
  verify.py     측정 + 대역 판정
  providers/    bizrouter · gemini · fake
```

`script → beats → shotlist` 는 순수 함수라 API 없이 전부 단위 테스트된다.
`pipeline/tests/` 215개가 붙어 있다 — 파이프라인을 고치면 돌려라.
