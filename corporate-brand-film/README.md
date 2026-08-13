# corporate-brand-film

한국 기업 홍보영상을 만드는 Claude Code 스킬. 회사 정보 YAML 하나로
시나리오·샷리스트·AI 영상·나레이션·한글 타이포·배경음을 거쳐 88초 완성본까지 간다.

실제 기업 홍보영상 **28편**(총 117분)을 전사·컷분석해서 뽑은 문법을 따른다 —
5막 구조, 전환 장치의 위치, 컷 리듬 대역, 타이포 3계층.

## 설치

```bash
# 개인 스킬로
cp -r corporate-brand-film ~/.claude/skills/

# 또는 저장소 스킬로 (팀과 공유)
cp -r corporate-brand-film <프로젝트>/.claude/skills/
```

```bash
~/.claude/skills/corporate-brand-film/scripts/setup.sh
```

필요한 것: `ffmpeg`, 나눔고딕(Regular + ExtraBold), Python 3.12+, `pyyaml`, `requests`.
영상 생성에는 [bizrouter](https://bizrouter.ai) 또는 Google Gemini API 키.

## 쓰는 법

Claude Code에서 그냥 말하면 된다:

> 우리 회사 홍보영상 만들어줘. 이차전지 소재 회사고 1998년 창업했어.

스킬이 걸리면 브리프 작성부터 무료 시험 실행, 1컷 발사, 전체 생성까지
비용 안전 절차를 지키며 진행한다.

직접 돌리려면:

```bash
cd ~/.claude/skills/corporate-brand-film
./video prepare                     # 환경 점검
./video new 우리회사         # 브리프 템플릿 복사 → 채운다

export VIDEO_BRIEF=brief/우리회사.yaml
./video preview                  # Fake 로 88초 완성본 (무료)
./video verify
```

돈을 쓰려면:

```bash
export BIZROUTER_API_KEY=...
./video testshot                  # 1컷만, 약 600원 — 화질 확인
./video produce                     # 전체 33컷, 약 2만원
```

두 명령 다 예상 비용을 보여주고 확인 문구를 받아야 진행한다.

만든 뒤 고칠 때 (재조립은 무료):

```bash
./video shots                    # 33컷 컨택트시트 — 어느 컷이 문제인지
./video rebuild                  # 대본·자막·배경음 수정 → 재조립 (무료)
./video redo 5 13 27             # 그 컷만 다시 생성 (컷당 약 600원)
```

## 비용

Veo 3.1 Fast 기준 **초당 152원**, 33컷 완성본 하나가 **약 2만원**이다.
파이프라인은 기본이 `--fake`(무료)이고, 실제 호출은 `--live`를 명시해야 한다.
`--budget-krw`로 원화 상한을 걸면 서버가 알려준 실제 청구액을 누적해
넘기 전에 멈춘다.

중간에 끊겨도 이어받기가 되므로 재과금이 없다.

## 무엇이 들어 있나

```
SKILL.md                        발동 조건 · 워크플로 · 비용 안전 규칙
references/
  reference-analysis.md         28편 해부 (5막 구조, 8가지 훅, 대역 근거)
  pitfalls.md                   실제로 돈을 날리거나 완성본을 망친 함정들
pipeline/
  src/                          파이프라인 (9개 모듈)
  brief/template.yaml           브리프 템플릿 (규칙이 주석으로)
  tests/                        215개
  scripts/make_bed.sh           배경음 생성
scripts/setup.sh                실행 환경 점검
```

## 만들어지는 것

88초 · 33컷 · 1920×1080 · 5막 구조

```
0-14초   콜드오픈 (나레이션 없음, 음악만)
14-18초  타이틀 카드
18-30초  정의 — 회사가 무엇인지
30-43초  근거 — 창업연도 + 숫자 앵커
43-50초  전환 — "하지만 우리는 멈추지 않았습니다"
50-73초  사업부 2개 챕터
73-82초  클라이맥스
82-88초  클로징 + 로고 엔드카드
```

완성본은 레퍼런스 28편과 같은 계측기로 재서 같은 대역에 넣고 판정한다 —
컷/분, 평균 샷 길이, 발화속도, 무나레이션 비율, 대본 대비 실제 발화.

## 라이선스

파이프라인 코드는 자유롭게 쓰고 고쳐도 된다.
`references/reference-analysis.md`는 공개된 영상들을 분석한 결과물이며,
분석 대상 영상의 저작권은 각 제작사와 클라이언트에 있다.

## 여러 영상 스킬을 함께 쓸 때

`bin/video` 를 PATH 에 두면 타입별로 갈라진 하나의 진입점이 된다.

```bash
cp bin/video ~/.local/bin/ && chmod +x ~/.local/bin/video
ln -sf video ~/.local/bin/video:brand-film
ln -sf video ~/.local/bin/video:shorts
```

```bash
video brand-film preview      # 88초 기업 홍보영상 (16:9)
video shorts assemble ...     # 세로형 쇼츠 (9:16)
video:brand-film verify       # 콜론 형태도 된다
```

`~/.claude/skills/` 아래에서 스킬을 찾는다(`CLAUDE_SKILLS` 로 바꿀 수 있다).
설치된 스킬만 동작하고, 없으면 설치 방법을 알려준다.
