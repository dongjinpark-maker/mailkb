# Minerva CLI Reference

조사 에이전트가 Minerva의 현재 명령을 정확히 선택하도록 하기 위한 **명령 참조**다.
어떤 도구를 언제 쓰는지·조사 절차·답변 규율은
[minerva-researcher.md](minerva-researcher.md)에 있다.
예시는 모두 가상 값이며 실제 메일, 이름, 주소, 사내 경로를 포함하지 않는다.

## 실행 규칙

저장소 루트에서 실행한다.

```bash
<PYTHON> -m mailkb [--home <HOME>] <COMMAND> [OPTIONS]
```

- `<PYTHON>`: Windows는 보통 `python`, Linux/WSL은 보통 `python3`
- `--home`은 하위 명령보다 앞에 둔다.
- 데이터 홈 우선순위: `--home` > `MAILKB_HOME` > 저장소의 `data/`
- 옵션이 불확실하면 `<PYTHON> -m mailkb <COMMAND> --help`로 확인한다.
- 실제 운영 홈 경로를 답변이나 저장소 문서에 기록하지 않는다.

## 조회 명령 (AI 호출 0)

### `ls`: 최근 메일 목록

```bash
<PYTHON> -m mailkb ls --limit 30
<PYTHON> -m mailkb ls --today --limit 50
<PYTHON> -m mailkb ls --unanswered
```

`--today`는 오늘 메일, `--unanswered`는 미답변 스레드를 표시한다. 구조화 출력은
지원하지 않으므로 대량 조사보다 최근 상태 확인에 사용한다.

### `search`: 메일 검색

```bash
<PYTHON> -m mailkb search '프로젝트 알파 일정' --limit 20 --json
<PYTHON> -m mailkb search 'from:sender@example.invalid after:2026-01 "최종 일정"' --json
<PYTHON> -m mailkb search 'thread:42 변경' --json
```

기본 검색은 로컬 FTS/필터만 사용한다. 에이전트 조사는 출력이 안정적인 `--json`을
우선한다(`--ai` 는 AI 검색 — 조회가 아니다).

`--json` 결과의 주요 필드:

| 필드 | 의미 |
|---|---|
| `id` | `show`에 사용할 메일 ID |
| `thread_id` | `thread`에 사용할 스레드 ID |
| `subject` | 제목 |
| `sender`, `sender_addr` | 발신자 표시명과 주소 |
| `date` | 발신 시각(분 단위 표시) |
| `is_sent` | 내가 보낸 메일 여부 |
| `has_attach` | 첨부 존재 여부 |
| `tier` | 검색 완화 단계. 낮을수록 직접적인 일치. 1 연속구 · 2 단어 AND · 3 부분일치 AND · 4 단어 OR(관련 낮음). 본문 검색어 없이 필터만 쓴 질의는 0 |
| `snippet` | 후보 확인용 발췌. `⟪...⟫`는 일치 강조 |

### 검색 DSL

| 표현 | 의미 | 예시 |
|---|---|---|
| 일반 단어 | 제목·본문 검색 | `납기 검토` |
| `"정확한 구"` | 연속 구 검색 | `"최종 승인"` |
| `from:` | 발신자 이름·주소 | `from:sender@example.invalid` |
| `to:` / `cc:` | 수신·참조 대상 | `to:"프로젝트 팀"` |
| `after:` | 날짜 이상, 포함 | `after:2026-01-01` |
| `before:` | 날짜 미만, 제외 | `before:2026-07-01` |
| `on:` | 해당 일·월·연도 | `on:2026-06` |
| `is:` | 상태·방향 | `is:unread`, `is:sent`, `is:flagged` |
| `has:attachment` | 첨부가 있는 메일 | `has:attachment` |
| `file:` | 첨부 파일명 | `file:schedule.xlsx` |
| `thread:` | 특정 스레드 | `thread:42` |

`is:`는 `unread`, `read`, `sent`, `received`, `flagged`를 지원한다. 날짜는
`YYYY`, `YYYY-MM`, `YYYY-MM-DD`를 사용할 수 있다. OR 문법은 없으므로 변경 신호는
필요하면 여러 번 검색한다.

현재 상태를 묻는 질문의 권장 검색 순서:

```bash
<PYTHON> -m mailkb search '프로젝트 알파 "최종 일정" after:2026-01' --json
<PYTHON> -m mailkb search '프로젝트 알파 변경 after:2026-01' --json
<PYTHON> -m mailkb search '프로젝트 알파 취소 after:2026-01' --json
<PYTHON> -m mailkb search '프로젝트 알파 보류 after:2026-01' --json
```

### `show`: 한 메일의 인용 제거 본문

```bash
<PYTHON> -m mailkb show 481
<PYTHON> -m mailkb show '<MESSAGE-ID>'
```

메일 ID 또는 Message-ID를 받는다. 제목, 발신자, 수신자, 시각, 스레드 ID,
첨부 파일명, **Message-ID**, 인용 제거 본문을 평문으로 출력한다. 수신자와 참조는
있으면 **이메일 주소 그대로** 나온다 — 질문에 필요 없는 식별자는 답변이나 로그에
옮기지 않는다(개인정보 최소화). 최종 답변의 짧은 원문 인용은 이 출력에서 확인한다.

### `thread`: 스레드 전체 시간선

```bash
<PYTHON> -m mailkb thread 42
```

스레드의 현안 브리핑(있으면)과 메시지를 시간순 평문으로 출력한다. 변경 전후,
회신 여부, 최신 발언을 확인할 때 사용한다. 긴 스레드는 출력이 커질 수 있고
JSON·메시지 수 제한 옵션은 아직 없다.

### `ask --history` / `ask --show`: 저장된 분석 열람

```bash
<PYTHON> -m mailkb ask --history
<PYTHON> -m mailkb ask --show 42
```

이 두 형태는 **AI를 호출하지 않고** 이미 저장된 분석만 읽는다. `--history`는
번호·시각·상태·근거 수·질문을 나열하고, `--show`는 그 답을 근거와 인용까지 그대로
출력한다. 조사를 시작하기 전에 같은 질문이 이미 처리됐는지 확인하면 중복 조사를
피할 수 있다.

`--show` 출력은 이 순서다 — **상태 · 한 줄 결론 · 경위 · (쟁점) · (부딪히는 근거) ·
근거 · 열린 것 · 여기부터 보면 됩니다 · 조사 범위**. 근거는 **결론 → 이유 → 배경**
순으로 묶여 있고 각 인용에 원문 앞뒤 문맥이 붙는다. '쟁점' 절은 웹의 스레드 쟁점
분석 결과에만 있다 — `제목 [상태]` 형식이고 상태는 합의·해소·진행 중·보류·평행선
다섯 어휘뿐이다(상태가 비어 있으면 엔진이 판정을 유보한 것이니 그대로 옮긴다).

출력 머리의 `(저장된 답변 · 이후 새 메일 N통)`은 그 답이 만들어진 뒤 들어온 메일
수다. N이 크면 낡았을 수 있으므로 결론을 그대로 옮기지 말고 최신 근거를 다시
확인한다. 저장된 답은 그때의 판정이다 — 오래된 기록일수록 인용을 `show`로 직접
대조한 뒤 인용한다.

### `ask --context`: 엔진이 실을 지침·노트·지식 보기

```bash
<PYTHON> -m mailkb ask --context '프로젝트 알파 최종 일정 뭐였지?'
```

**AI 를 호출하지 않고** 엔진이 그 질문의 프롬프트에 실을 결정론 문맥을 같은
함수로 보여 준다 — `[사용자 지침]`(`<HOME>/ai-rules.md`, 주석 제거) ·
`[내 노트 — 관련]` · `[지식 — 관련]`. 숨긴 스레드는 엔진과 같이 빠진다. 비어
있으면 왜 비었는지(지침 파일 경로·색인 건수)를 말한다. 노트·지식 색인을 파일에
맞추는 재색인이 먼저 돈다(색인은 파일의 미러 — 되돌릴 것이 없다). 질문 없이
`--context` 만 주면 지침과 색인 건수만 낸다.

### 그 밖의 조회

- `audit [--sample --report]`: 분류 판정과 근거 확인 (`--label` 은 대화형 쓰기 — 제외)
- `noise [--limit]`: 발신자별 수신량·차단 후보 — 이 명령 자체는 아무것도 안 바꾼다
- `stats`: 메시지·스레드·인물 수, DB 크기, FTS 상태

## AI 를 부르는 명령 — 플래그가 없어도 부른다

**다섯이다**: `ask <질문>` · `ask --person` (각 최대 12콜) · `thread-diag` ·
`person-diag` (각 1콜/건) · `diagnose` (백엔드마다 시험 호출 1회). 그 밖에
`--ai` 옵션(`search --ai` · `review --ai` · `weekly --ai`)이 있다. 플래그 유무로
판단하지 말고 이 목록을 기준으로 삼는다. **사용자가 명시적으로 요청할 때만
실행한다**(researcher 규칙 — 조사의 기본 경로는 조회 확보 + 세션 직접 분석이고,
엔진의 기존 산출·재료를 읽는 `--history`/`--show`/`--context` 는 조회다).

### `ask`: 근거 달린 답 (최대 12콜)

```bash
<PYTHON> -m mailkb ask '프로젝트 알파 최종 일정 뭐였지?'
<PYTHON> -m mailkb ask '그럼 누가 승인했지?' --follow 42
<PYTHON> -m mailkb ask --person sender@example.invalid
<PYTHON> -m mailkb ask '프로젝트 알파 최종 일정?' --fresh --backend sonnet
```

조사 라운드 루프(조사 최대 6 → 답변 → 의미 검증 → 조건부 재작성)를 돌려 근거가 달린
답을 만들고 결과를 분석 이력에 저장한다. 인용 대조와 반전 검색(변경·취소·최종)을
프롬프트 지시가 아니라 **코드로** 강제하고, 상태는 강등만 한다(승격 없음).
재작성본의 마지막 관문도 AI 가 아니라 코드다 — 검증을 통과한 근거 밖의 수량·날짜가
문장에 나타나면 그 문장을 버리고, 버린 수를 조사 범위 줄에 `근거 밖 문장 N개 제외`
로 밝힌다(2026-08-25).

- `--follow <번호>`: 그 답에 이어 묻는다. 이전 조사의 정독 목록과 질의를 승계한다.
- `--person <주소>`: 그 사람과의 최근 교신으로 범위를 고정한 브리핑.
- `--fresh`: 캐시를 무시하고 다시 조사한다.

### `thread-diag` / `person-diag`: 현안 브리핑 (1콜/건)

```bash
<PYTHON> -m mailkb thread-diag 42          # 또는 --pick 8 (최근 활동 우선 자동 선정)
<PYTHON> -m mailkb person-diag sender@example.invalid
```

웹 [현안 브리핑] 버튼과 같은 산출을 터미널로. 슬롯은 정리·문제·원인·방향·먼저 할
일·배경·모르는 것이고, 문제·배경의 근거만 원문과 대조해 통과한 것이 남는다.
`person-diag` 는 그 사람과 주고받은 최근 스레드 원문만 재료로 쓴다(프로필 등
AI 산출은 넣지 않는다). 환경 점검용 `diagnose` 와 다른 명령이다.

### `diagnose`: 환경 진단 (백엔드마다 시험 호출 1회)

역할이 쓰는 백엔드마다 짧은 시험 호출을 보낸다 — 기본 설정이면 sonnet·opus 2콜.
`--backend <이름>` 을 주면 그것 하나만. **`doctor` 와 헷갈리지 않는다: `diagnose`
는 AI 를 부르고 `doctor` 는 부르지 않는다.**

## 부작용 명령 — 사용자가 요청할 때만

| 명령 | 부작용 |
|---|---|
| `init` | 데이터 홈·설정 파일 생성 |
| `sync [--source --since --full]` | 사서함을 읽어 DB·파생 상태 갱신 — Windows Outlook COM, 수 분 걸릴 수 있다 |
| `note <스레드ID>` | vault 에 지식 노트 템플릿 생성 — **쌓기만 하는** 작업이라 자율 실행 예외 |
| `review [--date --ai]` | 일간 회고 — vault 에 파일을 쓴다. `--ai` 는 3콜 고정(보통 1~4분 · 바쁜 날 최대 10분) |
| `weekly [--weeks --ai]` | 주간 보고 — **`--ai` 없이도** vault 에 파일을 쓴다 |
| `hide <스레드ID> [--undo]` | 표시·추적 판정이 바뀌고 **AI 재료에서도 빠진다**(아래) |
| `block` / `unblock <주소>` | 로컬 차단 목록 변경 (Outlook 수신 규칙은 안 만든다) |
| `open <번호>` / `attach <스레드ID>` | Windows Outlook 을 연다 / 첨부를 vault 에 저장 |
| `serve [--port --open --app]` | 장기 실행 로컬 웹 서버 |
| `audit --label` | 대화형으로 `<HOME>/labels.jsonl` 수정 |
| `doctor` | AI 호출 0 이지만 Windows 에서 Outlook 을 열어 보안 경고가 뜰 수 있다 |

**숨긴 스레드는 AI 재료에서도 빠진다** — 회고·수확·분석·AI 검색·인물 요약 전부.
그래서 `ask` 가 "근거 부족"이라 해도 **메일에 없다는 뜻이 아니다**. CLI
`search`/`show` 는 숨김을 보므로 어긋나면 그쪽으로 교차 확인한다. 예외는
사용자가 그 메일에서 [분석]을 직접 누른 경우뿐이다.

## 현재 한계

- `search --json`만 안정적인 구조화 출력을 제공한다. `show`, `thread` 등은 평문이다.
- CLI 조회 명령도 현재 DB 연결 자체는 엄격한 SQLite read-only 모드가 아니다 —
  위 분류를 권한 보장이 아닌 에이전트 행동 제한으로 취급한다.
- 결정 기록과 인물 dossier를 전용 JSON으로 조회하는 명령은 아직 없다.
- 첨부 본문, Jira, Confluence 자료를 읽는 명령은 아직 없다.
- 긴 본문·스레드의 출력 크기 제한이 없으므로 후보를 먼저 좁혀야 한다.

이 한계를 넘는 정보가 필요하면 추측하지 말고 어떤 조회 기능이 부족한지 답변에
명시한다.
