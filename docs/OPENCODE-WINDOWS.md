# Windows에서 opencode를 AI 백엔드로 쓰기

`internal` 백엔드(opencode)를 Windows PC에서 실제로 쓰기 위한 설정과, 그걸
가능하게 하려고 코드에 넣은 것. 전제는 **opencode가 WSL 안에만 설치돼 있는**
환경이다 — 회사 PC의 실제 모양이고, Windows 네이티브 설치라면 §1의 `cmd`만
`["opencode", "run", "--pure"]`로 줄이면 나머지는 같다.

측정값은 전부 2026-08-30 Windows 11 + WSL2(Ubuntu) + opencode 1.18.25 실기기다.

---

## 1. 설정 — 이것만 하면 돈다

### `<home>/config.toml`

```toml
[ai.backends.internal]
cmd = ["wsl.exe", "-e", "bash", "-lc",
       'exec opencode run --pure --dir /var/tmp/minerva-oc --agent minerva "$@"', "oc"]
effort_flag = "--variant"      # opencode의 추론 강도: high / max / minimal
```

네 조각이 각각 하나씩 막는다.

| 조각 | 없으면 |
|---|---|
| `bash -lc` | `wsl -e opencode` 는 `execvpe(opencode) failed` — 로그인 셸이 없으면 `~/.opencode/bin`이 PATH에 없다 |
| `"$@"` 와 끝의 `"oc"` | 코드가 argv 뒤에 붙이는 플래그(`--format json`·`effort_flag`)가 `$0`·`$1`이 되어 **조용히 사라진다** |
| `--dir` | opencode가 `AGENTS.md`/`CLAUDE.md`를 읽는다 — **cwd 뿐 아니라 위로 올라가며** 찾는다(§1.1). 저장소에서 띄우면 **코딩 규칙이 메일 분석 프롬프트에 실린다** |
| `--agent minerva` | 기본 `build` 에이전트가 콜마다 툴 스키마로 **~7,000 토큰**을 태우고, 메일 본문이 들어가는 프롬프트에 툴이 열려 있다 |

`--format json`은 config에 쓰지 않는다. 코드(`_ai_run_stream_oc`)가 필요할 때만
붙인다 — 직접 넣으면 진행 이벤트가 없는 블로킹 경로에서 NDJSON이 답으로 나가
하류 파서가 전부 깨진다.

### `/var/tmp/minerva-oc/.opencode/agent/minerva.md` (WSL 안)

**손으로 쓰지 않는다** — 저장소가 나르는 것을 이름만 바꿔 복사한다. 내용이 저장소와
어긋나면 도구 목록이 조용히 낡는다.

```bash
mkdir -p /var/tmp/minerva-oc/.opencode/agent
cp /mnt/c/<저장소>/tools/opencode/minerva-agent.md \
   /var/tmp/minerva-oc/.opencode/agent/minerva.md
```

내용은 이렇다.

```markdown
---
description: Minerva 메일 분석 전용 — 도구 없이 주어진 텍스트만 읽는다
mode: primary
tools:
  bash: false
  edit: false
  write: false
  read: false
  grep: false
  glob: false
  list: false
  patch: false
  apply_patch: false
  batch: false
  task: false
  todowrite: false
  todoread: false
  question: false
  webfetch: false
  lsp: false
  skill: false
---
주어진 텍스트만 읽고 답한다. 도구를 쓰지 않는다. 파일·셸·네트워크에 접근하지 않는다.
```

claude 백엔드의 `--tools ""`에 해당한다(`review._ai_request`). opencode는 그 채널이
없어 에이전트 파일로 같은 일을 한다. 분석(`ask`)은 한 질문에 최대 12콜이라 이 차이가
그대로 곱해진다 — 토큰만이 아니라 **콜 시간도 같이 는다**.

한 단어 답 1콜의 입력 토큰(2026-08-30 실측, 같은 질문):

| 설정 | 입력 합 |
|---|---|
| 아무것도 안 함(기본 `build`) | 7,765 |
| 설정 `tools` 키로 도구만 끄기 | 2,909 |
| **전용 에이전트**(이 절) | **1,059** |

가운데 줄은 **검토했으나 안 쓴 길**이다. opencode 설정 스키마에 최상위 `tools`
(`{도구이름: false}`)가 있어 `--agent` 없이 JSON 한 장으로 끌 수 있다 — 그런데 절반만
줄고(2,909), 목록을 더 늘려도 안 줄었다(전부 끄나 절반만 끄나 2,910 대 2,909). 남는
몫은 도구가 아니라 `build` 에이전트 정의 자체가 싣는 것이라 에이전트를 갈아야 사라진다.
설정을 **전역**(`~/.config/opencode/opencode.jsonc`)에 두는 길도 있지만, 그러면
사용자가 opencode 로 코딩할 때도 도구가 꺼진다 — mailkb 만 조용해야 한다.

### 1.1 함정 둘 — 둘 다 조용하다

**`--dir` 은 `$HOME` 아래에 두면 안 된다.** opencode 는 지시문 파일을 `--dir` 에서만
찾지 않고 **위로 올라가며** 찾는다. 세 번 재서 확인했다(2026-08-30, 1.18.25 —
부모에 canary 지시문을 심고 답 끝에 그 표시가 붙는지 봤다).

| 실험 | 결과 |
|---|---|
| 두 칸 위 `CLAUDE.md` | 붙는다 — **깊이로는 못 막는다** |
| 가까운 `AGENTS.md` · 먼 `CLAUDE.md` | 가까운 것만 — 거리가 아니라 **종류** 우선순위였다 |
| 가까운 `AGENTS.md` · 먼 `AGENTS.md` | **둘 다 붙는다** — 위아래가 합쳐진다 |

세 번째가 결론이다. 같은 종류는 합쳐지므로 가까운 자리에 빈 `AGENTS.md` 를 방패로
두는 것도 소용없다. 위로 올라가도 나올 것이 없는 자리를 써야 하고, `/var/tmp` 가
그렇다(`/var/tmp` → `/var` → `/`). `$HOME/.minerva-oc` 는 부모가 `$HOME` 이라
사용자가 홈에 `AGENTS.md`·`CLAUDE.md` 를 두는 순간 **모든 메일 분석 콜에 실린다** —
개발자 홈에는 흔한 파일이다.

설정으로는 막을 수 없다. 스키마의 `instructions` 키는 *"Additional instruction files"*
라 **더하기만 하고 끄지 못한다.**

**에이전트 파일이 없으면 실패하지 않는다.** 이름이 틀리거나 자리가 어긋나면 이렇게
된다(실측).

```
! agent "minerva" not found. Falling back to default agent   ← stderr, 그리고 계속 간다
{"type":"step_start", …}
{"type":"step_finish", … "tokens":{"input":5411, … "cache":{"read":2353}}}
종료코드 0
```

**exit 0 에 정상 NDJSON 이라 mailkb 는 아무 이상을 못 본다.** 대가는 입력 1,059 →
7,764 토큰(약 7배)이고, 그보다 **도구가 열린 채로 메일 본문이 들어간다**. 이 저장소가
`--tools ""` 와 전용 에이전트로 막아 온 바로 그 축이 조용히 열린다.

지금은 화면에 신호가 없다. 의심되면 `[응답 시험]` 이나 `mailkb diagnose --backend
internal` 을 돌리고, 그 콜의 stderr 에 위 경고가 있는지 본다. (계측을 붙이는 것은
§5 참고 — `_ai_run_once` 가 성공 시 stderr 를 버리고 있어 그 계약부터 건드려야 한다.)

---

## 2. 코드에 들어간 것

| 곳 | 무엇 | 왜 |
|---|---|---|
| `review._is_opencode_cmd` | **명령 전체**를 보는 백엔드 판별자 | `_is_claude_cmd`처럼 `cmd[:2]`만 보면 실행 파일이 `wsl.exe`라 영영 못 찾는다 |
| `review._ai_run_stream_oc` | `--format json` NDJSON 파서 | 진행 이벤트를 claude와 **같은 중립 어휘**로 흘린다 |
| `review._usage_of_oc` | `step_finish` → 토큰 계측 | 입력 합산식을 claude와 같게(신규+캐시생성+캐시읽기) — 한 화면의 숫자가 백엔드마다 다른 것을 세면 비교가 안 된다 |
| `review.aitest_timeout` | 응답 시험 상한을 백엔드별로 | 30초는 claude 기준이다. opencode는 콜드 스타트만 ~20초라 멀쩡한 백엔드가 늘 '무응답'으로 떴다 |
| `web._job_live_line` | 무수신 워치독에 `stream` 게이트 | `last_ev`는 콜 시작(`ev:call`)으로도 찍히고 그건 백엔드와 무관하다 — 게이트가 없으면 이벤트 0건인 백엔드가 정상 진행 중에 경고를 받는다 |
| `web._STALL_SECS_OPENCODE` | 무수신 기준 30→90초 | opencode는 첫 이벤트가 20~46초 뒤에 온다. 같은 기준을 쓰면 정상 구간마다 경고가 떠 배경음이 된다 |
| `web._job_stream_event` | `ev:call`에서 `phase`·`recv` 리셋 | 콜 단위 리셋을 원래 `model` 이벤트가 했는데 **opencode는 모델 이름을 안 흘린다** |
| `web._job_live_line` | `phase: tool` → `도구 사용 중` | 메일 분석에 툴이 돌면 그건 메일 본문이 유발했다는 뜻이다. 조용히 넘기지 않는다 |

### 이벤트 대응표

opencode 1.18.25 `run` 핸들러 기준.

| opencode | 조건 | 중립 어휘 |
|---|---|---|
| `step_start` | 단계마다 | `phase: thinking` |
| `reasoning` | `--thinking` 필요 | `phase: thinking` + `delta` |
| `text` | `time.end` 있을 때 = **완결분 1건** | `phase: writing` + `delta`(text 포함) |
| `tool_use` | 툴 완료/오류 | `phase: tool` |
| `step_finish` | `tokens`·`cost` | `usage` |
| `error` | `session.error`, exit 1 동반 | `AIError` |

**바이트 단위 델타가 없다.** text는 다 쓴 뒤 한 번에 온다 — 그래서 수신량은 답
직전에 0에서 한 번에 뛰고, 진행바는 끝까지 인디터미닛이며, 모델 배지는 빈다
(이 포맷에 모델 이름이 없다). 관측되지 않는 것은 지어내지 않고 `.waitslot:empty`가
그 슬롯을 접는다.

### 평문 폴백

`--format json`이 셸 래퍼에 삼켜지면(§1의 `"$@"` 누락) opencode는 평문을 뱉는다.
그때 `_ai_run_stream_oc`는 **실패시키지 않고 그대로 답으로 쓴다** — 진행 표시만
잃는다. 설정 한 줄의 실수가 AI 기능 전체를 죽이면 안 된다. claude 경로가
2026-07-28에 이 폴백이 없어 분석이 전부 죽었던 그 자리와 같은 판단이다.

---

## 3. 실측 숫자

| | |
|---|---|
| WSL interop 오버헤드 | 0.1초 — 무시해도 된다 |
| opencode 프로세스 콜드 스타트 | ~20초 (웜 ~6초) |
| 첫 진행 이벤트까지 | 20~46초 |
| 한 단어 답 1콜 총시간 | **3초 ~ 60초** — 무료 모델 티어라 편차가 크다 |
| 입력 토큰 (`build` / 툴 없는 에이전트) | 8,129 / 1,204 |

콜 하나가 60초를 넘는 일이 흔하다는 것이 이 백엔드의 성격이고, 그래서 진행 표시가
claude보다 **더** 중요하다. 그냥 두면 화면이 몇 분간 죽은 것처럼 보인다.

---

## 4. 확인

```bash
python -m mailkb --home <home> diagnose --backend internal
```

`● 응답`이면 끝이다. `▲ 무응답`이면 늦는 것이지 고장이 아닐 수 있다 — 상한은
`review.AITEST_TIMEOUT_OPENCODE`(150초)이고, 그걸 넘으면 WSL에서 `opencode run`을
직접 돌려 로그인·모델 설정을 본다.

웹은 설정 › **이 PC에서 쓸 수 있는 AI**의 `[응답 시험]`이 같은 일을 한다.

### 안 될 때 — 증상이 셋뿐이다

| 보이는 것 | 뜻 | 할 일 |
|---|---|---|
| `Failed to change directory to …` (exit 1) | `--dir` 폴더가 없다 | §1 의 `mkdir -p` |
| `▲ 설정 안 먹음` · `에이전트 '…' 를 못 찾아 기본값으로 돌았습니다` | 에이전트 파일이 없거나 이름이 다르다 | §1 의 `cp`, 이름은 `minerva.md` |
| `■ 실패` · `execvpe(opencode) failed` | 로그인 셸을 안 거쳐 PATH 에 없다 | `cmd` 에 `bash -lc` 가 있는지 |

셋 다 **조용하지 않다.** 특히 가운데는 종전에 조용했다 — opencode 가 에이전트를
못 찾아도 실패하지 않고 기본 `build` 로 떨어져 `● 응답` 으로 보였다(§1.1).
`[응답 시험]`과 `diagnose` 가 성공 경로의 stderr 를 보고 `▲ 설정 안 먹음` 으로
가른다(2026-08-31). 대답이 왔다고 다 된 것이 아니다.

### 자리를 옮긴 뒤

옛 자리(`~/.minerva-oc`)는 지워도 된다.

```bash
rm -rf ~/.minerva-oc
ls -la ~/AGENTS.md ~/CLAUDE.md    # 있었다면 지금까지 메일 프롬프트에 실려 왔다
```

---

## 5. 아직 안 한 것

- **점검 콜이 실사용과 다른 길을 탄다** — `[응답 시험]`은 opencode 에만 `on_event`
  를 넘긴다(setup 경고를 받으려고). claude 는 종전 블로킹 그대로다 — 넘기면 점검이
  `stream-json` 으로 바뀌어 **잘 쓰던 사람에게 없던 위험**을 만든다. 점검이 실사용과
  같은 길을 타는 쪽이 원래는 옳지만, 그건 claude 경로를 따로 확인한 뒤의 일이다.
- **`[응답 시험]` 결과가 재시작을 못 넘긴다** — 인메모리라 서버를 다시 띄우면
  래퍼 백엔드가 다시 `○ 확인 안 됨` 으로 돌아간다. 잘 쓰는 사람이 매번 그 표시를
  본다.
- **`ask_max_input_tokens`** — 기본 120,000은 Claude 창 기준이다. opencode가 부르는
  모델에 맞춰 낮춰야 할 수 있다.
- **`opencode serve` + `--attach`** — 콜당 기동 비용(~6초)을 없앨 수 있지만 프로세스
  수명 관리가 늘고 "아웃바운드는 AI 뿐"(CLAUDE.md §2) 근거를 다시 따져야 한다.
