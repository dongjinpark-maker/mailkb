# Windows 에서 opencode 쓰기

**Minerva 의 AI 는 기본이 claude 다**(`[ai]` 의 sonnet·opus). opencode 는 사용자가 어떤 역할을
`internal` 로 돌렸을 때만 쓰는 **부가 백엔드**이고, 이 문서는 그 경우의 이야기다 — 이 문서의
어떤 설정도 claude 로 도는 역할을 바꾸지 않는다.

opencode 를 **Windows 에 직접 깔아** 쓰는 두 방식이다. A 는 mailkb 가 opencode 를 AI 백엔드로
부르는 것이고, B 는 사람이 opencode 를 띄워 저장소를 쓰는 것이다. 2026-11-23 부터 opencode 를
쓸 때는 이쪽이 기본이다. 그 전까지 쓰던 WSL 안의 opencode 는
[부록 C — 옛 방식](#부록-c--옛-방식-wsl-안의-opencode)으로 내렸다. 옛 설정도 그대로 돈다.

| | A. AI 백엔드 | B. opencode TUI |
|---|---|---|
| 흐름 | mailkb → opencode | 사람 → opencode → mailkb |
| 쓰는 곳 | 웹의 분석·회고·브리핑 | 터미널에서 조사·코딩 |
| 설정 | `config.toml` 의 `internal` 한 줄 | 저장소 `.opencode/` (따로 할 것 없음) |
| 에이전트 | `minerva` — 도구 없음, **코드가 호출마다 정의를 싣는다** | `mailkb` — 도구 있음 |

측정값은 2026-11-23 Windows 11 · opencode 1.19.6 과 2.3.2(npm 설치) 실기기다.

---

## A. mailkb 가 opencode 를 부른다 — AI 백엔드(`internal`)

### A1. 설정 — 판을 먼저 본다

```
opencode --version
```

**1.x 와 2.x 는 `opencode run` 이 아는 옵션이 다르다.** 모르는 옵션이 하나라도 있으면 도움말만
찍고 실패하므로, 판에 맞는 한 줄을 고른다.

`<home>/config.toml` (보통 `data\config.toml`):

```toml
# opencode 1.x  (1.19.6 에서 확인)
[ai.backends.internal]
cmd = ["opencode", "run", "--pure"]
effort_flag = "--variant"      # 추론 강도 high / max / minimal
```

```toml
# opencode 2.x  (2.3.2 에서 확인)
[ai.backends.internal]
cmd = ["opencode", "run", "--standalone"]
# effort_flag 는 적지 않는다 — 2.x 에는 --variant 가 없다(추론 강도는 --model 'p/m#variant')
```

| 조각 | 1.x | 2.x | 없으면 |
|---|---|---|---|
| `--pure` | 있음 | **없음** | 외부 플러그인이 콜마다 뜬다 (2.x 에 넣으면 모든 호출 실패) |
| `--standalone` | **없음** | 있음 | 2.x 는 배경 서비스에 붙는데, 서비스는 자기 환경으로 떠 있어 아래 A2 의 정의가 **전달되지 않는다** → `Agent not found: "minerva"` |
| `--variant`(effort_flag) | 있음 | **없음** | 어려운 콜에 추론 강도가 안 붙을 뿐이다 |

**`opencode` 가 PATH 에 없으면** 그 자리에 전체 경로를 쓴다(`where opencode` 가 알려 준다 —
`…\npm\opencode.cmd` 처럼 `.cmd` 로 끝나도 된다).

`--format json` 은 적지 않는다. 코드(`_ai_run_stream_oc`)가 진행 표시가 필요한 호출에만
붙인다 — 직접 넣으면 진행 이벤트가 없는 경로에서 JSON 줄이 그대로 답이 되어 하류가 깨진다.

### A2. 코드가 호출마다 붙이는 것 — 사람이 맞출 것이 아니다

종전(WSL)에는 사람이 지시문이 안 걸리는 폴더(`--dir`), 그 안에 복사한 에이전트 파일,
`--agent minerva` 를 손으로 맞췄다. 하나라도 어긋나면 **조용히** 도구가 열린 기본 에이전트로
돌았다(입력 토큰 7배, 메일 본문이 든 프롬프트에 도구가 열림). 이제 코드가 붙인다
(`review._child_env` · `_child_cwd` · `_ai_request`).

| 붙이는 것 | 막는 것 |
|---|---|
| `--agent minerva` (설정이 `--agent` 를 적었으면 그대로 둔다) | 기본 에이전트의 도구·긴 시스템 프롬프트 |
| 환경변수 `OPENCODE_CONFIG_CONTENT` 에 minerva 정의(도구 `"*": false`, 프롬프트 한 줄) | 에이전트 파일 복사 · 저장소가 바뀌어도 복사본이 낡는 것 |
| `OPENCODE_DISABLE_CLAUDE_CODE=1` | 내 **Claude Code 지시문**(`~/.claude/CLAUDE.md`)이 메일 분석 콜에 실리는 것 |
| `OPENCODE_DISABLE_PROJECT_CONFIG=1` | 작업 폴더와 **그 부모들**의 `AGENTS.md`·`.opencode/` 가 실리는 것 |
| 작업 폴더 `%TEMP%\minerva-oc` (비어 있는 전용 폴더) | opencode 가 웹 서버를 띄운 저장소 폴더를 '프로젝트'로 잡아 되돌리기용 스냅숏을 뜨는 것 |

**왜 환경변수인가 — 실측**(가짜 홈에 "답 끝에 표식을 붙여라"라는 지시문을 심고 답을 봤다):

| 심은 자리 | 아무것도 안 하면 | 위 변수를 주면 |
|---|---|---|
| `~/.claude/CLAUDE.md` (1.19.6 · 2.3.2) | **답에 표식이 붙었다** | 안 붙었다 |
| 작업 폴더의 부모 `C:\Users\Public\AGENTS.md` (1.19.6) | **붙었다** — 홈 밖이어도 드라이브 루트까지 올라갔다 | 안 붙었다 |
| 같은 자리 (2.3.2) | 안 붙었다 | 안 붙었다 |

WSL 에서 부를 때는 WSL 홈이라 `~/.claude/CLAUDE.md` 가 대개 없었다. **Windows 에서 직접 부르면
Windows 홈이다** — Claude Code 를 같이 쓰는 PC 면 그 파일이 있다. 끄는 스위치가 환경변수뿐이라
(설정 파일 키가 없다) 코드가 붙인다. `PROJECT_CONFIG` 는 폴더의 에이전트 파일까지 끄므로
에이전트도 파일이 아니라 환경변수로 준다 — 두 판 다 `agent` 키로 먹었다(2.3.2 문서는
`agents` 라 적지만 그 키는 먹지 않았다).

**끄지 못하는 것 하나** — 전역 `%USERPROFILE%\.config\opencode\AGENTS.md`. 스위치가 없다.
`mailkb diagnose --backend internal` 이 있으면 `▲` 로 알린다.

### A3. 확인

```
python -m mailkb diagnose --backend internal
```

```
[AI 백엔드]
  internal — opencode run --pure
    opencode 1.19.6 · 에이전트 minerva(코드가 정의를 싣는다)
    ● 응답: 'OK'
```

- 둘째 줄에 `▲` 가 있으면 판과 설정이 안 맞는 것이다 — 그 줄이 무엇을 고칠지 말한다.
- `● 응답` 이면 끝이다. 웹은 설정 › **이 PC에서 쓸 수 있는 AI** 의 `[응답 시험]` 이 같은 호출이다.
- 상한은 `review.AITEST_TIMEOUT_OPENCODE`(150초)다. opencode 는 콜드 스타트가 길다.

### A4. 안 될 때

| 보이는 것 | 뜻 | 할 일 |
|---|---|---|
| `▲ opencode … 에는 없는 옵션: …` · 실패에 opencode 도움말 | 판과 `cmd` 가 안 맞는다 | A1 에서 판에 맞는 줄로 |
| `Agent not found: "minerva"` | 2.x 가 배경 서비스에 붙었다 | `cmd` 에 `"--standalone"` |
| `▲ 설정 안 먹음` · `에이전트 'minerva' 를 못 찾아…` | 1.x 가 정의를 못 받았다 | `diagnose` 결과를 그대로 알려 주세요(새 경우다) |
| `■ 실패` · `AI 사용량 한도` · `Free usage limit reached` | opencode 무료 모델 한도 | 회사 모델(공급자)을 연결했는지 `opencode` 로 확인 |
| `▲ 무응답` | 150초 안에 답이 없다 | 다시 돌려 본다. 계속이면 `opencode run` 을 직접 돌려 로그인·모델을 본다 |
| `▲ 전역 지시문 … 이 모든 메일 분석 콜에 실립니다` | 위 A2 의 끄지 못하는 파일 | 그 파일이 opencode 코딩용이면 그대로 두고 감수하거나, 내용을 옮긴다 |

**무료 모델에서 본 것** — 이 PC 의 무료 모델(`opencode/big-pickle`)은 도구를 끈 에이전트의 요청을
`Free usage limit reached` 로 거절하고 기본 에이전트는 받다가, 시험을 거듭하자 기본 에이전트도
거절했다. 정책인지 한도인지 가르지 못했다. 회사 모델을 연결해 쓰면 상관없다.

**MCP 를 쓴다면** — 전역 설정에 MCP 서버가 있으면 `opencode run` 이 콜마다 그 서버를 띄운다.
도구가 프롬프트에 실리는 것은 `"*": false` 가 막지만 **프로세스가 뜨는 것은 못 막는다**
(시간만 든다). 종전엔 폴더 설정(`.opencode/opencode.json`)으로 껐는데, 이제 폴더 설정을
읽지 않으므로 그 길은 없다. 필요해지면 알려 주세요 — 정의에 MCP 끄기를 함께 싣는 길이 있다.

### A5. WSL 에서 옮겨 올 때

1. `config.toml` 의 `[ai.backends.internal] cmd` 를 A1 의 한 줄로 바꾼다(옛 `wsl.exe …` 줄은 지운다).
2. `python -m mailkb diagnose --backend internal` 로 확인한다.
3. WSL 쪽 `/var/tmp/minerva-oc` 는 이제 안 쓴다 — 지워도 된다(`rm -rf /var/tmp/minerva-oc`).

되돌리려면 1 의 줄을 옛 줄로 돌려놓는다. 코드는 두 꼴을 다 알아본다(부록 C).

---

## B. 사람이 opencode 를 쓰고, opencode 가 mailkb 를 부른다 — TUI

### B1. 실행

저장소 폴더에서(Windows 터미널):

```
cd <저장소>
opencode --agent mailkb
```

```
/mail-research NPX-200 양자화 최종 결정이 뭐였지?
```

| 파일 | 하는 일 |
|---|---|
| `.opencode/agent/mailkb.md` | mailkb 는 `python -X utf8 -m mailkb …` 로 실행 · DB 파일 직접 접근 차단 · 임시 파일은 `temp/` |
| `.opencode/command/mail-research.md` | `/mail-research` — Claude Code 스킬과 같은 조사 절차 |
| `.opencode/command/mail-wiki.md` | `/mail-wiki` — 등록한 주제의 위키 페이지를 만들고 갱신(계약: `agent-guides/minerva-wiki.md`) |

1.19.6 과 2.3.2 둘 다 `mailkb` 에이전트를 읽는 것을 확인했다(`opencode agent list` ·
`opencode debug agents`). 명령(`/mail-research`)이 2.x 에서 보이는지는 확인하지 못했다.

### B2. 지킬 것

- **`-X utf8` 을 뺀 `python -m mailkb` 는 한글이 깨진다.** 파이프로 받은 Windows 파이썬의
  출력은 cp949 인데 opencode 는 UTF-8 로 읽는다. 에이전트 파일이 모델에게 이 형식을 쓰게 한다.
- DB 파일(`*.sqlite`)은 직접 열지 않는다 — 웹 서버가 늘 열어 두므로 mailkb 명령으로만 읽는다.
- 데모로 시험하려면 "`--home demo` 로" 라고 말한다. 말하지 않으면 실제 데이터(`data/`)다.
- `.opencode/` 파일을 고치면 opencode 를 다시 띄워야 반영된다.

### B3. 안 될 때

| 보이는 것 | 할 일 |
|---|---|
| 한글이 `?ㅼ젙` 처럼 깨짐 | 명령에 `-X utf8` 이 있는지 본다 |
| `/mail-research` 가 안 보임 | opencode 를 저장소 안에서 띄웠는지 본다 |
| `python3` 이 막혔다는 거절 | 정상이다 — `python` 을 쓴다(옛 WSL 울타리) |

---

## 부록 C — 옛 방식: WSL 안의 opencode

2026-11-22 까지의 회사 PC 모양이다. **지금도 돈다** — 다만 손으로 맞출 것이 많아 기본에서 내렸다.

```toml
[ai.backends.internal]
cmd = ["wsl.exe", "-e", "bash", "-lc",
       'exec opencode run --pure --dir /var/tmp/minerva-oc --agent minerva "$@"', "oc"]
effort_flag = "--variant"
```

| 조각 | 없으면 |
|---|---|
| `bash -lc` | `wsl -e opencode` 는 `execvpe(opencode) failed` — 로그인 셸이 없으면 `~/.opencode/bin` 이 PATH 에 없다 |
| `"$@"` 와 끝의 `"oc"` | 코드가 argv 뒤에 붙이는 플래그(`--format json`·`effort_flag`)가 `$0`·`$1` 이 되어 **조용히 사라진다** |
| `--dir` | 저장소 폴더가 작업 폴더가 된다(A2 의 스냅숏) |

**에이전트 파일은 이제 복사하지 않아도 된다.** A2 의 환경변수는 `WSLENV` 를 타고 WSL 로 건너간다
(Windows 파이썬 → `wsl.exe` 에서 값이 그대로 도착하는 것을 확인했다). 그래서 옛 줄 그대로도
코드가 실은 정의로 돈다 — 폴더(`/var/tmp/minerva-oc/.opencode/agent/minerva.md`)는 읽지 않는다.
`--dir` 폴더 자체는 있어야 한다(없으면 `Failed to change directory`).

옛 방식의 TUI 는 WSL 셸에서 `/mnt/c/<저장소>` 로 들어가 `opencode --agent mailkb` 였다. 지금
에이전트 파일은 Windows 를 기준으로 쓰여 있어 WSL 에서 띄우면 `python` 이 없다고 실패한다 —
그때는 `powershell.exe -NoProfile -Command "chcp 65001 | Out-Null; python -m mailkb … | Out-File
-Encoding utf8 temp\x.txt"` 로 돌리고 파일을 `utf-8-sig` 로 읽는다. **WSL 의 python3 로 mailkb 를
돌리지 않는다** — Windows 디스크의 DB 를 Windows 프로세스가 쥐고 있으면 `disk I/O error` 다(실측
12/12).

---

## 부록 D — 개발자용

### 코드에 들어간 것

| 곳 | 무엇 | 왜 |
|---|---|---|
| `review._child_env` | opencode 호출에만 A2 의 환경변수, `WSLENV` 에 이름 추가 | 지시문 차단·에이전트 정의. 사용자가 이미 준 `OPENCODE_CONFIG_CONTENT` 는 남기고 에이전트만 더한다 |
| `review._child_cwd` | `%TEMP%\minerva-oc` | 스냅숏 대상. 만들지 못하면 상속으로 돈다(실패시키지 않는다) |
| `review._ai_request` | `--agent minerva` | 설정의 어느 토큰에든 `--agent` 가 있으면 안 붙인다(옛 래퍼는 bash 문자열 안) |
| `review._kill_tree` | Windows 에서 `taskkill /F /T` | npm 설치 CLI 는 `.cmd` 셔틀이라 cmd.exe → node → CLI 다. `proc.kill()` 은 cmd.exe 만 끈다 |
| `review._ai_run_once` | `subprocess.run(timeout=)` 대신 `Popen` + `communicate` | run 은 시간 초과 뒤 파이프가 닫히기를 상한 없이 기다린다 |
| `review.opencode_version` · `opencode_notes` | 판과 `cmd` 의 옵션 대조 | `diagnose` 가 호출 전에 말한다(doctor 는 프로세스를 띄우지 않는 계약이라 넣지 않았다) |
| `review._is_opencode_cmd` | **명령 전체**를 보는 판별자 | 옛 래퍼에선 실행 파일이 `wsl.exe` 다 |
| `review._ai_run_stream_oc` | `--format json` 파서 | 1.x 와 2.x 의 이벤트 모양이 같았다(아래) |

**셔틀 실측**(가짜 셔틀 `opencode.cmd` → node → 20초 자는 자식, Windows 파이썬):

| | 고치기 전 | 고친 뒤 |
|---|---|---|
| 블로킹 호출 3초 상한 | **20.2초** 뒤에야 돌아옴 | 3.5초 |
| 스트림 [중지] 뒤 남은 자식 | **1개 — 계속 돎** | 0개 |

`claude` 도 npm 설치면 같은 셔틀이라 함께 고쳐진다.

#### 이벤트 대응표

| opencode (`--format json`) | 조건 | 중립 어휘 |
|---|---|---|
| `step_start` | 단계마다 | `phase: thinking` |
| `reasoning` | `--thinking` 필요 | `phase: thinking` + `delta` |
| `text` | `time.end` 있을 때 = **완결분 1건** | `phase: writing` + `delta`(text 포함) |
| `tool_use` | 툴 완료/오류 | `phase: tool` |
| `step_finish` | `tokens`·`cost` | `usage` |
| `error` | 2.x 는 에이전트 없음·한도도 이걸로 내고 exit 1 | `AIError` |

2.3.2 의 `text`·`step_finish` 모양은 1.x 와 같았다(`time.end`, `tokens{input,output,reasoning,
cache{read,write}}`). **바이트 단위 델타가 없다** — text 는 다 쓴 뒤 한 번에 온다. 그래서 수신량은
답 직전에 0에서 한 번에 뛰고, 진행바는 끝까지 인디터미닛이며, 모델 배지는 빈다.

`--format json` 이 셸 래퍼에 삼켜지면(부록 C 의 `"$@"` 누락) opencode 는 평문을 뱉는다. 그때도
**실패시키지 않고 그대로 답으로 쓴다** — 진행 표시만 잃는다.

### 아직 안 한 것

- **웹 `[응답 시험]` 은 판 대조(`opencode_notes`)를 보이지 않는다** — `diagnose` 만 한다.
- **`ask_max_input_tokens`** — 기본 120,000 은 Claude 창 기준이다. 회사 모델의 창에 맞춰 낮춰야 할 수 있다.
- **2.x 배경 서비스에 붙기(`--standalone` 없이)** — 콜당 기동 비용이 줄지만 환경변수가 서비스에
  안 닿는다. 정의를 서비스 설정(`opencode service set env`)에 두는 길은 사용자의 다른 opencode 사용까지
  바꾸므로 넣지 않았다.
