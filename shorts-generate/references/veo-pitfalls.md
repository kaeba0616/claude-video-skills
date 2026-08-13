# Veo 프롬프트 함정

실측으로 확인한 것들. 프롬프트를 쓰기 전에 읽어라.

## 인과 동작을 시키지 마라 ★

Veo는 **한 물체가 다른 물체를 변형시키는 동작**에 약하다 — 자르기, 부수기, 뚫기, 찌르기.
원인과 결과를 별개 사건으로 처리해서 순서가 어긋난다.

실측: `the blade slices cleanly through the apple in one smooth motion` 으로 요청했더니
칼이 사과를 **통과하지 않고 옆으로 지나갔다.** 칼끝이 도마에 닿았는데 사과는 온전했고,
다음 순간 갑자기 갈라진 상태로 건너뛰었다. 칼도 수직이 아니라 45도로 누워
물리적으로 자를 수 없는 각도였다.

**대책: 변형의 결과에서 시작한다.**

```
✘ 칼이 사과를 자른다 → 단면이 드러난다
✔ 이미 갈라진 사과에서 시작 → 두 반쪽을 벌린다 → 단면이 드러난다
   + "no cutting action, nothing enters the frame from above"
```

같은 반전(속에 벌집)을 유지하면서 동작만 바꾸자 여섯 프레임이 끊김 없이 이어졌다.
**보여주고 싶은 게 결과라면 과정을 요구하지 마라.**

같은 이유로 안전한 동작들: 떨어지기·퍼지기·흐르기·회전·카메라 이동·벌어지기.
피할 동작들: 자르기·부수기·터뜨리기·조립·손으로 물체 변형.

## 과정 영상에서 결과가 미리 튀어나온다 ★

인과 동작 약점의 변형. "고양이를 그린다"고 하면 Veo 는 선을 순서대로 긋는 게
아니라 **완성된 고양이를 클립 초반에 앞당겨 렌더링**한다. 우유 붓기에서도
"라떼 아트"라는 맥락만으로 붓는 도중 완성 무늬가 표면에 나타난다 (실측:
plain canvas 를 요청했는데 붓기 중에 고양이 그림이 갑자기 떠올랐다).

**대책: 클립마다 진행량을 명시하고, 아직 없어야 할 것을 못박는다.** (실측 검증)

```
붓기 컷:   "the surface stays completely plain: no latte art pattern, no rosetta,
           no tulip, no drawing, no design of any kind forms — just a blank canvas"
드로잉 컷: "one single continuous line, the line appearing only where the pen tip
           has already passed, no lines appear ahead of the pen" +
           "by the end of the clip only this outline exists — no eyes, no nose yet"
추가 컷:   "nothing else is added or changed; all existing lines stay exactly as
           they are"
```

세 요소가 다 필요하다: ① 선이 펜 끝을 따라서만 생긴다 ② 이 클립의 종료 상태
(어디까지) ③ 아직 그려지면 안 되는 것들의 명시적 부정. 프레임 체이닝과 함께
쓰면 여러 클립에 걸친 드로잉 과정이 순서대로 이어진다.

## 클립이 이어지지 않으면 프레임 체이닝을 써라

스타일 문구 블록을 모든 프롬프트에 반복해도 text-to-video 는 클립마다 잔·소품·
구도를 새로 뽑는다. 같은 장소에서 연속 동작이 진행되는 영상(요리·드로잉·만들기)은
**이전 클립의 마지막 프레임을 `--image` 로 다음 클립의 첫 프레임에 박아라.**

```bash
ffmpeg -v error -sseof -0.2 -i clips/scene_01.mp4 -frames:v 1 -update 1 frames/scene_01_last.png
# → 다음 클립에 --image frames/scene_01_last.png
```

실측 (라떼 에칭 3클립): 같은 잔·같은 테이블·같은 구도가 그대로 유지됐다.
`--image` 는 lite 도 지원한다 (`reference_images` 와 달리 모델 제약 없음).

- 다음 프롬프트는 **추출한 프레임을 실제로 보고** 써라. 마지막 프레임의 상태
  (도구가 프레임에 남아 있나, 동작이 어디까지 진행됐나)가 대본과 다를 수 있다.
- 서두에 `Continuing seamlessly from the first frame:` + `camera locked in place`.
- 순차 생성이므로 병렬 불가 — 클립 수 × 1~2분.

## 스타일 단어가 소재로 해석된다

`subtle 35mm film grain` 이라고 썼더니 "필름 그레인 질감"이 아니라
**"35mm 필름을 보여줘"** 로 읽고 퍼포레이션 구멍과 필름 엣지 마킹(`-35`, `35.MM`)을
화면에 그렸다(33컷 중 10컷). `subtle fine grain` 으로 충분하다.

매체·포맷 이름(35mm, VHS, Polaroid, IMAX)은 그 매체의 **물리적 외형**을 부른다.

## 글자를 부르는 피사체를 쓰지 마라

명판·간판·증서·눈금·라벨은 정의상 글자가 적힌 물건이다.
`No on-screen text.` 를 붙여놓고 `rolling machine nameplate` 를 피사체로 지정하면
모순이고, Veo 는 **피사체를 따른다** — 화면 한가운데 간판이 생겼다.

자막은 항상 ffmpeg 로 번인한다. Veo 의 글자 렌더링은 신뢰할 수 없다.

## 모델별 필드 제약 (위반 시 생성 전 400)

| | lite | fast | veo-3.1 |
|---|---|---|---|
| `negative_prompt` | ✘ | ✔ | ✔ |
| `reference_images` | ✘ | ✔ (최대 3장, **8초 전용**) | ✔ |

- `reference_images` + `image`/`last_frame` 동시 사용 불가
- `reference_images` 를 쓰면 `duration=8` 강제 → 4초 샷을 쓰려면 절반을 버리므로 **비용 2배**

## 오디오 큐는 실제로 반영된다

프롬프트 끝에 `Audio: crisp droplet impact, thick honey stretching, quiet room tone,
ASMR clarity.` 를 넣으면 영상과 함께 사운드가 생성된다. ASMR·제품 영상에서는
반드시 넣어라 — 안 넣으면 무음이거나 엉뚱한 소리가 붙는다.

## 세로(9:16)는 구도를 프롬프트에 명시하라

`--aspect-ratio 9:16` 만으로는 부족하다. `in a tall 9:16 vertical composition that
emphasizes the straight downward fall` 처럼 **세로 프레임을 어떻게 쓸지**를 적어야
피사체가 가운데 작게 놓이지 않는다.

`--image`(첫 프레임)를 쓸 땐 입력 이미지를 목표 화면비로 미리 크롭할 것.
9:16 에 정사각 이미지를 넣으면 검은 레터박스가 영상 내내 남는다.

## 실패했을 때

- 클립 단위로 재생성한다(성공한 클립은 보존 — 중복 과금 방지).
- **프롬프트를 고치지 않고 재생성하면 같은 실패가 반복될 가능성이 높다.**
  먼저 프레임을 촘촘히 뽑아(`ffmpeg -ss <t> -frames:v 1`) 어디서 어긋나는지 보고,
  그 지점을 프롬프트에서 없애라.
- 생성 실패(`failed`)도 과금된다. 환불이 아니다.
