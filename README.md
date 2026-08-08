# mailkb — Outlook 위의 기억 계층

> **EN** — *mailkb* ("Minerva") is a personal knowledge layer on top of classic Outlook (COM, Windows):
> it indexes mail into SQLite/FTS5, distills daily decisions into a human-approved long-term ledger,
> answers questions with verified quotes, and serves a local web UI —
> **Python stdlib only**, AI strictly opt-in via CLI subprocess. Korean-first.

## **"그래서 그 결정 뭐였지?"에 근거를 달아 답한다**

업무 메일은 조직의 결정이 실제로 일어나는 곳인데, 메일 클라이언트는 그것을
기억해 주지 않는다. mailkb 는 **읽고 쓰는 일은 Outlook 에 그대로 두고**, 6개월치를
검색하고 결정을 장기기억으로 남기고 하루 끝에 회고를 쓴다.

- **모든 인용은 코드가 원문과 대조**한 것만 남는다 — 환각을 프롬프트가 아니라
  실행 경로에서 막는다
- **Python 표준 라이브러리만** (Windows 에서 `pywin32` 하나 추가) · `pip install` 없음
- **AI 는 opt-in** — 쓰지 않으면 네트워크 호출 자체가 0. 호출은 `claude`·`opencode`
  같은 **CLI 에 subprocess 로 위임**하므로 SDK·API 키가 필요 없다

![분석 화면 — 한 줄 결론, 경위, 역할별 근거, 각 인용의 원문 앞뒤 문맥](docs/home-light.png)

*(합성 데모 데이터 — 가상 팹리스 '누리소프트'의 1개월치 메일 약 280통. 실제 조사
결과이고, 근거마다 **진한 부분이 모델이 지목한 인용**, 흐린 앞뒤가 코드가 원문에서
그대로 떼어 붙인 문맥이다. 인용은 원문과 대조해 통과한 것만 남는다)*

## 시작하기

### 먼저 확인 — 30초

| 조건 | 왜 | 아니면 |
|---|---|---|
| Windows + **클래식** Outlook 데스크톱 | COM 으로 사서함을 읽는다. 제목 표시줄의 **'새 Outlook' 토글을 끄면** 클래식이다 — 새 Outlook(`olk.exe`)에는 COM 이 없다 | **아래 데모만** 가능 |
| Windows 네이티브 **Python 3.11+** | `tomllib` 을 쓴다. **WSL 로는 안 된다** — COM 이 안 붙는다 | 설치 불가 |
| `pip` 로 **pywin32** 설치 가능 | 코어에서 유일하게 필요한 외부 패키지. 프록시 뒤라면 `--proxy` 로 | 설치 불가 |
| `claude` (또는 사내 CLI) 가 PATH 에 | 내장 백엔드가 `claude -p --model sonnet` 이다 | **AI 만 빠지고 나머지는 다 된다** |

앞의 셋이 안 되면 실사용은 못 하지만 [데모](#그냥-구경만--데모)는 어느 컴퓨터에서나 돈다.

### 회사 PC 에 설치 — 실사용

PowerShell 에서 여섯 줄이다.

```powershell
git clone https://github.com/dongjinpark-maker/mailkb
cd mailkb
pip install pywin32
python -m mailkb init                   # 코드 폴더 옆에 data\ 생성
notepad data\config.toml                # 내 주소·이름·사내 도메인 — 아래 주의
python -m mailkb sync --since (Get-Date).AddMonths(-6).ToString("yyyy-MM-dd")
python -m mailkb serve --app            # Edge 앱 모드(주소창 없는 창)
```

**설정 편집이 `sync` 보다 먼저인 것이 중요하다.** `my_addresses` 는 **수집할 때
소비되는** 값이라 나중에 채우면 발신 판정이 안 돼 회고·미답변·내 약속이 전부 비어
보이고, 고치려면 재수집해야 한다.

**왜 6개월인가** — 인물 화면이 최근 6개월 교류를 창으로 쓰므로 그만큼 넣으면 모든
화면이 채워진다. 첫 수집은 사서함 크기에 따라 수 분~수십 분이고, 이후 `sync` 는
증분이라 빠르다. `--since` 없이 돌리면 사서함 전체를 백필한다. (cmd.exe 라면
날짜를 직접 적는다: `--since 2026-02-01`)

`data/` 는 gitignore 라 이후 `git pull` 이 실데이터·설정을 건드리지 않는다.
설정 항목별 의미, 자동 동기화 등록(`schtasks`), 아이콘 실행, 첫 환경 점검 목록은
**[docs/DEPLOY-WINDOWS.md](docs/DEPLOY-WINDOWS.md)** 에 있다.

### Claude 에게 시키기

회사 PC 의 **빈 작업 폴더**에서 Claude 세션을 열고 아래를 그대로 붙여 넣는다
(저장소를 아직 안 받았어도 된다 — 받는 것부터 시킨다).
**설정값을 대신 채우지 못하게** 하고 **수집 전에 확인을 받게** 하는 것이 요지다.

````markdown
mailkb 를 이 PC 에 설치하려고 한다. 아래 순서대로 하고 **5번에서 반드시 멈춰라.**

1. 전제 확인 — Windows 네이티브 Python 3.11+ 인지(WSL 아님), 클래식 Outlook 이
   실행 중인지, `claude` 가 PATH 에 있는지. 하나라도 아니면 거기서 멈추고 알려 줘.
2. git clone https://github.com/dongjinpark-maker/mailkb
   그 폴더로 들어가 README.md 와 docs/DEPLOY-WINDOWS.md 를 읽어라.
3. pip install pywin32
4. python -m mailkb init
5. **여기서 멈춘다.** data\config.toml 의 my_addresses · my_names ·
   internal_domains 는 내 실제 값이다. 네가 지어내지 말고 무엇을 넣어야 하는지
   물어봐. 내가 넣었다고 답하면 그때 6번으로 간다.
6. 최근 6개월만 수집한다. **사서함 전체를 읽는 작업이니 실행 전에 확인을 받아라.**
   python -m mailkb sync --since (Get-Date).AddMonths(-6).ToString("yyyy-MM-dd")
7. python -m mailkb serve --app 로 띄우고 화면이 뜨면 알려 줘.

설치 과정에는 AI 호출이 없다(init·sync·serve 는 네트워크 호출 0).
ask 와 diagnose 는 그 자체가 AI 명령이니 내가 요청할 때만 실행해라.
````

### 그냥 구경만 — 데모

Outlook 도 Windows 도 필요 없다. Python 3.11+ 만 있으면 되고
**설치할 패키지가 없다.** 실제 사서함 대신 합성 코퍼스를 쓰므로 회사 PC 가
아니어도 화면과 회고·분석을 전부 볼 수 있다.

```bash
git clone https://github.com/dongjinpark-maker/mailkb
cd mailkb
python3 -m mailkb --home ./demo init      # 실사용 홈(data/)과 별개
```

**`demo/config.toml` 에서 두 줄을 고친다.** 합성 코퍼스의 '나'를 알려주는
단계로, 이걸 빼면 발신 판정이 안 돼 회고·미답변이 비어 보인다
(수집할 때 쓰는 값이라 **`sync` 전에** 넣어야 한다):

```toml
my_addresses = ["dohyun.kim@nurisoft.co.kr", "dhkim@nurisoft.co.kr"]
my_names     = ["김도현"]
```

이제 메일을 넣고 열어 본다. 여기까지 **AI 호출 0회**다.

```bash
python3 -m mailkb --home ./demo sync --source fake --full   # 1초 내
python3 -m mailkb --home ./demo review                      # 일간 회고
python3 -m mailkb --home ./demo serve --open                # 웹 UI
```

보이는 것 — 회고에 **내 약속 6건**(내가 "하겠습니다"라고 쓴 문장을 인용과 함께),
미답변 46건, 웹에서는 메일함·스레드·인물·기억·통계. 코퍼스는 약 280통/170스레드에
긴 스레드(12~14통)·스팸·시스템 알림·정체 사례를 일부러 섞어 두었다. 날짜는 **실행일
기준 상대값**이라 회고의 "오늘"이 언제 돌려도 비어 있지 않다(대신 통수가 조금씩 다르다).

**AI 기능까지 보려면** `demo/config.toml` 의 `[ai.backends.*]` 에 쓰는 CLI 를
지정하고 `--ai` 를 붙인다.

```bash
python3 -m mailkb --home ./demo review --ai
python3 -m mailkb --home ./demo ask '양자화 최종 결정 뭐였지?'
```

> **데모를 새 코드로 다시 만들 때** — `sync --full` 은 이미 있는 메일(Message-ID
> 기준)을 건너뛰므로 본문 생성 규칙이 바뀌어도 기존 데모에는 반영되지 않는다.
> `demo/db.sqlite` 를 지우고 다시 `sync` 해야 한다. 그 파일에 쌓인 **분석 기록·
> 장기기억·플래그는 함께 사라지니**, 남길 것이 있으면 먼저 복사해 둔다.

테스트는 외부 의존 없이 수 초면 끝난다.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## AI 를 켤 때 알아 둘 것 넷

- **숨긴 스레드(`hide`)는 AI 프롬프트에 실리지 않는다** — 회고·수확·분석·AI
  검색·인물 요약 전부. 그 메일에서 [분석]을 **직접 누른 경우만** 예외다.
- **`<home>/ai-rules.md`** 에 적은 지침(사내 용어, 우선순위 등)은 수확뿐 아니라
  **분석과 주간 보고**에도 실린다. 검증 단계에는 안 들어간다.
- **분석이 한 번에 싣는 입력량**은 `[ai] ask_max_input_tokens` 로 정한다
  (기본 120000 — Claude 기준, 0=제한 없음). **값은 곧 백엔드 컨텍스트 창**이고,
  통당 배분은 코드가 알아서 한다 — 3통만 읽으면 사실상 전문을, 24통을 읽으면
  그만큼 나눠 싣는다. 사내 백엔드 창이 작으면 그 값으로 낮춘다.
- **`[ai.backends.<이름>] effort_flag = "--effort"`** 를 선언하면 어려운 콜에
  추론 강도를 붙인다. 선언하지 않는 것이 기본이고, 그때는 아무것도 안 붙는다 —
  CLI 가 그 플래그를 지원하는지 `diagnose` 로 먼저 확인하고 켠다.

## 웹 UI — Minerva

`mailkb serve`는 Outlook 유사 **좌/우 분할**의 로컬 웹 앱이다. 왼쪽이 목록,
오른쪽이 읽기 패널이고, 브라우저가 렌더를 담당하므로 OS를 타지 않는다.

| 메뉴 | 내용 |
|---|---|
| **분석** (첫 화면) | 질문하면 메일을 찾아 읽고 인용을 원문과 대조해 근거가 달린 답을 쓴다. 한 줄 결론 + 경위 + 근거(인용의 **원문 앞뒤 문맥**까지) + 열린 것. 이어 묻기·인물 브리핑. 대화 목록엔 답마다 '이후 N통'(낡음), 하단엔 기준선(색인 통수·동기화 시각·쓰는 AI) |
| **메일함 · 스레드** | 필터 탭(미개봉·플래그·숨김), 키보드 이동, 자동 동기화, 메일별 AI 분석. 발신인 뒤에 **관계 배지**(`↩ 0` = 이 사람에게 한 번도 안 보냄 · `첫 메일`) — 규칙이 아니라 내 왕래 기록에서 나온다. 참조로 중간 합류한 스레드는 **인용 안의 앞선 대화를 턴으로** 펼친다. 본문에서 말을 고르면 **그 말이 다른 메일에서 어떻게 쓰였는지** 옆에 띄운다(읽던 자리는 그대로) |
| **인물** | 사람별 카드 — 관계 수치·진행 중·**이 사람에게 한 내 약속**·관여한 결정·업무 어휘 지도 |
| **기억** | 일간 회고 · 주간 보고(둘 다 ◀▶ 인접 차수 이동, 항목별 "처리함") · 장기기억(반영/유보) |
| **통계** | 부하 진단 — 응답 중앙값(나/상대) · 볼륨 추세 · 활동 히트맵 · 받은 메일 구성 · 왕복 많은 논의 · **기억 커버리지**(지식이 안 쌓인 날) |

다크 모드도 같은 화면이다. 통계는 "지금 부하가 어느 쪽으로 기울었나"만 답하도록
추린 것이라 지표가 여섯뿐이다 — 세어서 재미있는 값이 아니라 **보고 나면 뭘 할지가
나오는 값**만 남겼다.

![통계 — 응답 중앙값, 볼륨 추세, 활동 히트맵, 받은 메일 구성](docs/stats-dark.png)

보안: 의존성 0의 stdlib `http.server`, **localhost 바인딩만**, CSP로 원격 이미지
(추적 픽셀)와 인라인 스크립트 차단, POST Origin 검사.

화면별 상세와 키보드 단축키는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 있다.

## 명령

| 명령 | 하는 일 | AI |
|---|---|---|
| `sync [--full]` | 증분 수집 + 인용 제거 + 파생 갱신 | ✗ |
| `ls [--unanswered\|--today]` | 목록 / 미답변 스레드 | ✗ |
| `search <질의> [--json --ai]` | 전문검색 (연산자 DSL 지원) | 선택 |
| `show <번호>` · `thread <ID>` | 본문(인용 제거본) · 스레드 타임라인 | ✗ |
| `ask <질문> [--follow --person]` | **분석** — 근거 달린 답 | ✓ |
| `ask --history` · `--show <번호>` | 저장된 분석 열람 | ✗ |
| `review [--ai]` | 일간 회고 → vault/daily | 선택 |
| `weekly [--weeks 1 --ai]` | 원문 근거 기반 주간 보고 → vault/weekly | 선택 |
| `note <스레드ID>` | 지식 노트 템플릿 생성 | ✗ |
| `audit` · `noise` · `stats` | 분류 감사 · 발신자 분포 · 저장소 통계 | ✗ |
| `diagnose [--backend]` | 진단 — 스레딩·본문품질(인용 절단 실패 의심·재절단 백업)·요약 커버리지·**AI 백엔드 시험 호출** | ✓ |
| `block` · `unblock` · `hide` | 발신자 제외 · 스레드 숨김(목록·추적·**AI 프롬프트**에서 제외) | ✗ |
| `open <번호>` · `attach <스레드ID>` | Outlook 원문 열기 · 첨부 추출 (Windows) | ✗ |
| `serve [--port --open --app]` | Minerva 웹 UI | ✗ |

전체 옵션은 `python -m mailkb <명령> --help`.

## 더 읽을 것

| 문서 | 내용 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | **목적·구조·데이터 모델·파이프라인·기능 상세·설계 결정** |
| [docs/DEPLOY-WINDOWS.md](docs/DEPLOY-WINDOWS.md) | 회사 PC 배포 상세 |
| [CLAUDE.md](CLAUDE.md) | AI 코딩 에이전트용 작업 규칙·불변식 |
| [agent-guides/](agent-guides/) | 조사 에이전트용 CLI 계약 |
| [docs/REVERTED-quote-diff.md](docs/REVERTED-quote-diff.md) | 되돌린 실험 하나의 기록 — 다시 시도할 때의 함정 목록 |

## AI 에게 메일 조사를 시키려면

`agent-guides/`의 두 문서는 저장된 메일을 조사하는 에이전트의 **권한·절차 계약**이다.
읽히지 않으면 아무 효력이 없으므로, 새 세션 첫 프롬프트에서 명시적으로 읽게 한다.

```
agent-guides/minerva-researcher.md 와 agent-guides/minerva-cli-reference.md 를
먼저 읽고 그 계약대로 조사해줘. 저장소 루트에서 python3 -m mailkb 로 실행하고,
승인 없이 실행해도 되는 조회 명령만 써. 질문: "NPX-200 양자화 최종 결정이 뭐였지?"
```

핵심은 **어떤 명령이 AI를 부르는지**다 — `ask`·`diagnose`는 플래그 없이도 호출하므로
사용자 요청이 있을 때만 쓰고, 조사는 `search --json` · `show` · `thread`로 직접 한다.

## 백업

데이터 폴더(`<mailkb>/data` — `db.sqlite` + `config.toml` + `vault/`) 복사 한 번.
연 200~300MB 수준이다.

메일 본문은 Outlook에 있으니 `sync`로 다시 만들어지지만, **`db.sqlite`에는 재수집으로
복구되지 않는 것도 들어 있다** — 장기기억(결정 원장) · 분석 이력 · 인물 요약 ·
신호 해제 · 플래그/숨김 · 리포트에서 "처리함"으로 접은 항목. 그래서 이 파일
삭제는 백업 없이 되돌릴 수 없다.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
