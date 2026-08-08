# Minerva CLI Reference

이 문서는 Claude CLI가 Minerva의 현재 명령을 정확히 선택하도록 하기 위한 참조다.
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

## 조사에 사용하는 명령

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
우선하고, `--ai`는 중첩 AI 호출과 캐시 기록이 생기므로 사용하지 않는다.

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
있으면 **이메일 주소 그대로** 나온다. 최종 답변의 짧은 원문 인용은 이 출력에서
확인한다. JSON과 본문 길이 제한 옵션은 아직 없다.

출력에 주소와 Message-ID가 섞여 있으므로 **질문에 필요 없는 식별자는 답변이나
로그에 옮기지 않는다**(개인정보 최소화 — `minerva-researcher.md`의 같은 규칙).

### `thread`: 스레드 전체 시간선

```bash
<PYTHON> -m mailkb thread 42
```

스레드의 누적 요약과 메시지를 시간순 평문으로 출력한다. 변경 전후, 회신 여부,
최신 발언을 확인할 때 사용한다. 긴 스레드는 출력이 커질 수 있고 JSON·메시지 수
제한 옵션은 아직 없다.


### `audit`: 분류 판정 확인

```bash
<PYTHON> -m mailkb audit --sample 20
<PYTHON> -m mailkb audit --report
```

현재 분류와 근거를 표시하거나 기존 로컬 라벨과 혼동 행렬을 비교한다. `--label`은
대화형으로 `<HOME>/labels.jsonl`을 수정하므로 사용자의 명시적 요청이 필요하다.

### `noise`: 발신자 분포

```bash
<PYTHON> -m mailkb noise --limit 30
```

발신자별 수신량, 내 답장 수, 노이즈·차단 상태를 보여준다. 발신자 차단 여부를
판단하는 참고 자료이며, 이 명령 자체는 차단 목록을 바꾸지 않는다.

### `stats`: 저장소 통계

```bash
<PYTHON> -m mailkb stats
```

메시지·스레드·인물 수, DB 크기, FTS 상태, 인용 제거 절감률을 표시한다.

### `ask --history` / `ask --show`: 저장된 분석 열람

```bash
<PYTHON> -m mailkb ask --history
<PYTHON> -m mailkb ask --show 42
```

이 두 형태는 **AI를 호출하지 않고** 이미 저장된 분석만 읽는다. `--history`는
번호·시각·상태·근거 수·질문을 나열하고, `--show`는 그 답을 근거와 인용까지 그대로
출력한다. 조사를 시작하기 전에 같은 질문이 이미 처리됐는지 확인하면 중복 조사를
피할 수 있다.

`--show` 출력은 이 순서다 — **상태 · 한 줄 결론 · 경위 · (부딪히는 근거) · 근거 ·
열린 것 · 여기부터 보면 됩니다 · 조사 범위**. 근거는 **결론 → 이유 → 배경** 순으로
묶여 있고 각 인용에 원문 앞뒤 문맥이 붙는다. **'열린 것'은 답이 아니라 아직
안 닫힌 것**이니 결론으로 옮겨 적지 않는다.

출력 머리의 `(저장된 답변 · 이후 새 메일 N통)`은 그 답이 만들어진 뒤 들어온 메일
수다. N이 크면 낡았을 수 있으므로 결론을 그대로 옮기지 말고 위의 조회 명령으로
최신 근거를 다시 확인한다.

저장된 답은 그때의 판정이다. CLI 출력만으로는 그 답이 어느 검증까지 거쳤는지
구분할 수 없으므로(그 구분은 웹 UI에만 표시된다), 오래된 기록일수록 인용을
`show`로 직접 대조한 뒤 인용한다.

## 사용자가 요청할 때만 실행할 명령

### 초기화와 동기화

```bash
<PYTHON> -m mailkb init
<PYTHON> -m mailkb sync
<PYTHON> -m mailkb sync --source outlook
<PYTHON> -m mailkb sync --source fake --since 2026-07-01
<PYTHON> -m mailkb sync --full
```

- `init`: 홈과 기본 설정 파일을 생성한다.
- `sync`: Outlook 또는 fake source에서 메일을 수집하고 DB와 파생 상태를 갱신한다.
- `--since`: 지정 날짜 이후를 시험 수집한다.
- `--full`: 증분 기준을 무시하고 전체를 다시 읽는다. 비용이 클 수 있다.

동기화는 운영 데이터 변경이며 Windows Outlook COM을 사용할 수 있으므로 자동으로
실행하지 않는다.

### 노트와 리뷰

```bash
<PYTHON> -m mailkb note 42
<PYTHON> -m mailkb review --date 2026-07-01
<PYTHON> -m mailkb review --ai --backend sonnet
```

- `note`: 스레드 지식 노트 템플릿을 vault에 생성한다.
- `review`: 데일리 리뷰를 출력하고 vault에 저장한다.
- `review --ai`: AI 누적 요약, 결정·신호 수확, 하루 요약을 추가한다.

### 분석과 주간 보고

```bash
<PYTHON> -m mailkb ask '프로젝트 알파 최종 일정 뭐였지?'
<PYTHON> -m mailkb ask '그럼 누가 승인했지?' --follow 42
<PYTHON> -m mailkb ask --person sender@example.invalid
<PYTHON> -m mailkb ask '프로젝트 알파 최종 일정?' --fresh --backend sonnet
<PYTHON> -m mailkb weekly --weeks 2
<PYTHON> -m mailkb weekly --ai --date 2026-07-01
```

- `ask <질문>`: 조사 라운드 루프를 돌려 근거가 달린 답을 만든다. AI를 최대 12콜
  (조사 7 + 답변 + 의미 검증) 호출하고 결과를 DB에 저장한다.
- `--follow <번호>`: 그 답에 이어 묻는다. 이전 조사의 정독 목록과 질의를 승계한다.
- `--person <주소>`: 그 사람과의 최근 교신으로 범위를 고정한 브리핑.
- `--fresh`: 캐시를 무시하고 다시 조사한다.
- `weekly`: 내가 관여한 사안을 토픽별 진행·이슈·향후로 묶는다. `--ai` 없이도
  결정론 뼈대를 만들고, **어느 쪽이든 vault에 파일을 쓴다** — 조회 명령이 아니다.

`ask`는 아래 '조사 절차'가 사람 손으로 하는 일을 엔진이 직접 하는 명령이다.
인용 대조와 반전 검색(변경·취소·최종 신호)을 프롬프트 지시가 아니라 코드로
강제하고, 상태는 강등만 하며(승격 없음), 결과가 Minerva 분석 목록에 남는다.

그런데도 에이전트가 조사에 이 명령을 쓰지 않는 이유는 하나다 — **AI 호출이
중첩되기 때문**이다. 에이전트 자신이 이미 LLM인데 그 안에서 또 다른 백엔드를
최대 12콜 부르면 비용과 개인정보 노출 경로가 늘고, 어느 층이 무엇을 판단했는지
추적하기 어려워진다. 그래서 조사는 위의 조회 명령으로 직접 하고 `ask` 실행은
사용자가 요청했을 때만 한다.

다만 같은 질문이 반복될 것 같거나 근거 검증을 코드로 보장받는 편이 나은
질문이라면, 답변 끝에 `mailkb ask` 로 조사해 두기를 사용자에게 제안한다.
실행 여부는 사용자가 정한다.

### 표시 상태와 발신자 관리

```bash
<PYTHON> -m mailkb hide 42
<PYTHON> -m mailkb hide 42 --undo
<PYTHON> -m mailkb block sender@example.invalid
<PYTHON> -m mailkb unblock sender@example.invalid
```

`hide`는 스레드 표시·추적 상태를, `block`과 `unblock`은 로컬 차단 목록 파일을
변경한다. `block`은 Outlook 수신 규칙을 만들지 않는다.

**숨긴 스레드는 AI 재료에서도 빠진다**(2026-08-02) — 회고·수확·분석·AI 검색·인물
요약 전부. 그래서 `ask` 가 "근거 부족"이라 해도 **메일에 없다는 뜻이 아니다**.
사용자가 그 메일에서 [분석]을 직접 누른 경우만 예외다.

### Outlook과 첨부

```bash
<PYTHON> -m mailkb open 481
<PYTHON> -m mailkb attach 42
```

- `open`: Windows Outlook에서 원문을 연다.
- `attach`: 스레드 첨부를 vault에 저장한다.

첨부 파일명은 검색할 수 있지만 첨부 내용 자체를 읽거나 색인하는 CLI는 아직 없다.

### 웹과 진단

```bash
<PYTHON> -m mailkb serve --port 8765
<PYTHON> -m mailkb serve --app
<PYTHON> -m mailkb diagnose
<PYTHON> -m mailkb diagnose --backend sonnet
```

- `serve`: localhost 웹 UI를 시작한다. `--open`은 브라우저, `--app`은 Edge 앱 모드다.
- `diagnose`: 저장소 상태를 진단한다. **`--ai` 인자가 없는데도 설정된 AI 백엔드에
  짧은 시험 호출을 반드시 보낸다** — 플래그가 없다고 안전한 조회로 오해하지 않는다.
  백엔드가 설정돼 있지 않을 때만 호출이 생략된다.

장기 실행 프로세스나 외부 프로그램을 시작할 수 있으므로 자동 조사에서는 제외한다.

## AI 옵션

```bash
<PYTHON> -m mailkb search '흐릿한 기억' --ai
<PYTHON> -m mailkb review --ai
<PYTHON> -m mailkb weekly --ai
```

`--ai` 명령은 별도 AI backend를 호출하고 일부 결과를 저장한다. Claude 조사
에이전트 안에서 다시 AI 검색을 호출하면 비용, 개인정보 노출 범위, 결과 해석 경로가
불필요하게 늘어난다. 사용자가 특정 AI 기능 실행을 요청한 경우에만 사용한다.

**`--ai` 플래그가 없어도 AI를 부르는 명령이 셋 있다** — `ask <질문>`,
`ask --person`(각 최대 12콜), 그리고 `diagnose`(짧은 시험 호출 1회).
플래그 유무로 판단하지 말고 위의 '분석과
주간 보고' 항목을 기준으로 삼는다. 반면 `ask --history`와 `ask --show`는
AI를 부르지 않는 조회다.

## 현재 한계

- `search --json`만 안정적인 구조화 출력을 제공한다.
- `show`, `thread` 등은 사람이 읽는 평문이다.
- CLI 조회 명령도 현재 DB 연결 자체는 엄격한 SQLite read-only 모드가 아니다.
- 결정 기록과 인물 dossier를 전용 JSON으로 조회하는 명령은 아직 없다.
- 첨부 본문, Jira, Confluence 자료를 읽는 명령은 아직 없다.
- 긴 본문·스레드의 출력 크기 제한이 없으므로 후보를 먼저 좁혀야 한다.

이 한계를 넘는 정보가 필요하면 추측하지 말고 어떤 조회 기능이 부족한지 답변에
명시한다.
