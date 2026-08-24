# CLAUDE.md — 이 저장소에서 작업할 때

AI 코딩 에이전트가 먼저 읽는 문서. 무엇을 만들고 있는지는
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), 사용법은 [README.md](README.md).

## 한 줄

클래식 Outlook(COM) 위에 얹는 개인 메일 지식 계층. 메일을 SQLite/FTS5로 색인하고,
**읽기 전용** 로컬 웹 UI(코드명 **Minerva**)로 AI 와 함께 읽고(현안 브리핑·인물
프로필·쟁점 분석), 암묵지를 사람이 승인해 지식 md(`vault/knowledge/`)로 남기고,
더 깊은 분석은 Claude Code 스킬(`/mail-research`)이 세션에서 직접 한다.
수·발신·보관은 Outlook 에 그대로 둔다.

## 깨면 안 되는 것

1. **표준 라이브러리만.** 코어에 서드파티 의존을 추가하지 않는다. 예외는
   Windows의 `pywin32`(COM) 하나뿐이고, 그것도 `sources/outlook_com.py` 안에서만
   import 한다. `requirements.txt`가 없는 것은 의도다.
2. **AI는 opt-in.** AI 호출은 사용자가 명시적으로 고른 명령에서만 일어나고, 전부
   `review.ai_run`(stdin→stdout subprocess)을 거친다. SDK도 API 키도 쓰지 않는다.
   새 AI 기능은 반드시 이 계약 위에 얹는다.

   관문은 **`--ai` 플래그 또는 명령 자체**다. `ask`(분석)와 `diagnose`(백엔드 점검)는
   그 자체가 AI 명령이라 플래그가 없다 — `diagnose`는 `--ai` 인자가 아예 없는데도
   **역할이 쓰는 백엔드마다** 시험 호출을 보낸다(기본 설정이면 sonnet·opus 2콜). 그 밖의 모든 명령은 `--ai` 없이 네트워크 호출이 0이다.

   ```
   호출 0      init · sync · ls · search · show · thread · review · weekly
               note · audit · noise · stats · block · unblock · hide · open
               attach · serve · doctor · ask --history · ask --show · ask --context
   플래그로    search --ai · review --ai · weekly --ai
   플래그 없이 ask <질문> · ask --person · thread-diag · person-diag · diagnose
   ```

   `serve`는 **명령 자체**가 0이라는 뜻이다. 웹 UI는 AI 기능이 모인 곳이고
   (분석 질문 · 메일 분석 · 스레드 쟁점 분석 · 현안 브리핑(스레드·인물) · AI 회고 ·
   주간 보고 · AI 검색 · 인물 프로필 · 심층 분석 · 지식 저장 시 보강 ·
   **설정 › AI 백엔드 상태의 [응답 시험]** — PATH 에 있는 백엔드마다 1콜)
   사용자가 누르면 나간다.
   화면을 열어 두는 것만으로는 안 돈다 — 배경의 일간 회고 자동 생성은 `ai=False`다.

   **아웃바운드는 AI 뿐이다 — 예외 없음**(2026-08-15). 원격 이미지를 서버가
   대신 받아오는 프록시를 잠깐 뒀다가 걷어냈다: 사내망은 직접 나가는 길을 막아
   프록시 경유가 필요했는데, 프록시를 거치면 목적지 IP 를 프록시가 해석해
   SSRF 방어의 근거(접속한 실제 피어 IP 검사)가 무너진다. **방어를 낮춰야만
   되는 기능은 넣지 않는다.** 지금은 사용자가 [위험을 감수하고 보기]를 누른
   화면에서만 CSP 의 `img-src` 를 풀어 **브라우저가 직접** 받는다(브라우저는
   시스템 프록시를 쓰므로 사내망에서도 된다). 서버는 소켓을 열지 않는다.

   **AI 호출 지점을 새로 만들면 이 목록을 함께 고친다.** 여기가 어긋나면 에이전트가
   비용과 개인정보 노출 범위를 잘못 판단한다. `docs/ARCHITECTURE.md` §8 표와
   `agent-guides/`의 목록도 같은 사실을 말해야 한다.
3. **숨긴 스레드는 AI 프롬프트에 싣지 않는다.** 재료를 모으는 새 경로를 만들면
   조립 **전에** `store.hidden_thread_ids()`로 거른다. 숨김은 "조용히 하라"라서
   목록에서 빼는 것으로 끝나지 않는다 — 2026-08-02 점검에서 `weekly.collect`
   하나만 거르고 롤링 요약·수확·분석·AI 검색·인물 요약이 숨긴 원문을 그대로
   싣고 있었다. 예외는 사용자가 그 스레드를 **직접 지목한** 온디맨드 분석뿐이다
   (`ask(allow_tids=…)`). 표시 축(집계·목록 렌더)은 이 규칙과 무관하다.
4. **AI 실패는 우아하게.** AI가 없거나 실패해도 결정론 경로는 그대로 동작해야 한다.
   AI 산출은 초안이고 확정은 사람이 한다.
5. **재수집(re-sync)을 강요하지 않는다.** 스키마·파생 로직을 바꿔도 사용자가
   `sync --full`을 다시 돌릴 필요가 없어야 한다. 새 테이블은
   `CREATE TABLE IF NOT EXISTS`, 파생 결과는 버전 키로 무효화한다
   (`store._feature_version` / `_action_version`). 회사 PC의 재수집은 수십 분짜리
   작업이라 사실상 회귀다.

   **뒤늦게 생긴 컬럼 위의 인덱스는 `_SCHEMA`에 두지 않는다.** `CREATE TABLE`은
   `IF NOT EXISTS`로 건너뛰는데 그 아래 `CREATE INDEX`는 아직 없는 컬럼을 참조해
   `executescript` 전체가 죽는다 — 구 DB가 **아예 열리지 않는다**(2026-08-13
   `ingest_seq` 도입 때 실제로 그랬고, 데모 DB가 그중 하나였다). 컬럼을 붙이는
   `_ensure_late_columns()` 다음의 `_ensure_late_indexes()`에서 만든다.
6. **개인·회사 정보를 코드나 공개 문서에 넣지 않는다.** 예시는 전부 가상값을
   쓴다(가상 회사 `누리소프트`/`nurisoft.co.kr`, 가상 인물 `김도현`). 실제 값은
   `data/`(gitignore) 안에만 존재한다. 커밋 전 새 문자열이 가상인지 확인한다.
7. **인용 검증은 코드가 한다.** 답변·요약이 메일을 인용하면 그 인용이 실제 본문에
   있는지 코드로 대조하고, 통과한 것만 남긴다(환각 차단). 이 검사를 프롬프트
   지시로 대체하지 않는다. 적용 지점: 분석·수확·주간, 그리고 **사실 슬롯만** —
   현안 브리핑(코드에선 `diagnose*`)의 `문제`·`배경`, 인물 프로필의 `맡은 일`·
   `요즘 하는 일`(2026-08-16·18). **판단 문장에는 인용을 요구하지 않는다** —
   여러 통을 종합한 문장은 원문에 그대로 있을 수 없어, 강제하면 종합을 깎는다.
   검증할 수 없는 산출은 대신 **기각 가능한 모양**으로 낸다 — 방향마다 얻는 것·
   잃는 것·되돌리기를 달고, 판단을 뒤집을 조건은 '모르는 것'으로 선언한다.
   **인용을 못 걸면 수치를 건다** — 일간 머리글(`_exec_verify`)과 분석 재작성
   (`ask._grounded`)은 판단 문장이라 인용을 요구할 수 없으므로, 재료 밖의
   수량·날짜를 쓴 문장을 코드가 버린다. 검사 대상은 **단위가 붙은 수량**이어야
   한다 — 자릿수만 보면 `NPX-200`·`CVE-2026-31337` 의 숫자를 수량으로 오인해
   멀쩡한 결론 문장을 지운다(2026-08-25 하네스에서 실제로 잡혔다).
   **같은 판정을 AI 에게 두 번 묻지 않는다** — 분석의 2차 의미 검증은 1차가 방금
   통과시킨 근거 8개 중 5개를 다시 탈락시켰고, 그 목록을 코드는 쓰지도 않았다.
8. **`id`는 신원이지 카운터가 아니다.** `messages.id`·`threads.id`는 메일의 시각에서
   계산한 값이라(`store.next_id` — 날짜 + 15분 슬롯 + 슬롯 내 순번), **삽입 순서도
   개수도 아니다**. 도착 순서가 필요하면 `messages.ingest_seq`를 쓴다.

   ```
   금지                              대신
   MAX(id) - basis   = 그새 온 개수   store.count_after(basis)  ← COUNT(*)
   MAX(id)           = 워터마크       MAX(ingest_seq)
   MIN(id)           = 먼저 넣은 것    MIN(ingest_seq)
   WHERE id > 워터마크 = 새 행         WHERE ingest_seq > 워터마크
   ```

   왜 함정인가: **오류가 안 난다.** 날짜 기반 번호는 차(差)가 개수와 무관하고
   (하루만 넘어가도 100,000), 백필된 옛 메일은 워터마크보다 **아래**에 꽂혀 증분
   조회에서 조용히 빠진다. 2026-08-13 작업에서 이 한 부류로 네 곳이 깨졌다 —
   `ask.py`의 '이후 새 메일 N통'(3이 나올 자리에 1,200,000), 노이즈 캐시 증분,
   `_reclean_quotes`의 mid-join 판정(**유일본 인용 체인이 잘렸다**), `ask_basis`.
   `sent_on` 뒤의 동점 처리(`ORDER BY sent_on, id`)와 0에서 시작하는 페이지네이션
   커서는 안전하다. 상세는 `docs/ARCHITECTURE.md` §4 '번호 체계'.

9. **읽기 경로에서 쓰지 않는다.** 웹은 요청마다 `Store`를 열고, 배경 `sync`는
   100통 청크마다 쓰기 잠금을 쥔다. 열기·조회 경로에 쓰기가 한 줄이라도 있으면
   그 요청이 `busy_timeout`(30초)을 다 쓰고 `database is locked`로 죽는다 —
   사용자에겐 "화면이 멈췄다"로 보인다(2026-08-15 실사용 보고, 세 곳에서 발견).
   **0행 UPDATE 도 쓰기 트랜잭션이고, 값이 같은 PRAGMA 재설정도 그렇다.**
   조건 없이 쓰지 말고 인덱스를 타는 읽기로 먼저 판별한다.

   ```
   금지                                    대신
   UPDATE … WHERE 조건            (매번)   SELECT 1 … LIMIT 1 로 있을 때만 UPDATE
   PRAGMA auto_vacuum=…           (매번)   새 DB(sqlite_master 빈 상태)에서만
   ```

   WAL 이라 **읽기는 잠금과 무관**하다 — 이 규칙만 지키면 sync 중에도 화면이
   즉답한다(실측: 고치기 전 스레드 화면 11초 대기 → 고친 뒤 0.004초).

## 코드 관례

- 주석·docstring·UI 문자열은 **한국어**. 주변 코드의 주석 밀도와 어조를 따른다.
- 주석은 "무엇"이 아니라 **"왜"**를 적는다. 이 저장소의 주석은 대부분 결정의
  근거이지 코드 번역이 아니다.
- 모듈은 크지만 단일 책임이다. 새 화면은 `web.py`, 새 CLI는 `cli.py`에 붙인다.
- **끝 표식 없이 블록을 치환하지 않는다.** 시작만 잡고 끝을 안 잡는 편집으로 이
  저장소에서 코드가 유실되거나 **같은 함수가 두 벌** 붙는 일이 반복됐다
  (2026-08-19 정리: `review.py` 의 `_exec_facts`·`ai_exec_summary`). 파이썬은 뒤의
  정의를 쓰므로 **앞의 것을 고치면 아무 일도 안 일어난다** — 조용한 실패다.
  최상위 중복 정의는 테스트가 막는다(`test_no_duplicate_top_level_definitions`).
- **검색 DSL 문법을 바꾸면 표 두 개를 함께 고친다** — `docs/ARCHITECTURE.md` §9와
  `agent-guides/minerva-cli-reference.md`. 조사 에이전트는 후자만 읽고 일하므로
  자족적이어야 해서 일부러 중복해 둔 것이고, 그래서 어긋나면 에이전트가 없는
  문법을 쓴다(AI 관문 목록을 세 곳에서 맞추는 것과 같은 이유).
- 웹 UI는 서버 렌더 HTML + 인라인 문자열 JS 조합이다. 빌드 단계가 없다. CSP상
  인라인 `<script>`는 막혀 있으므로 JS는 **자산으로 서빙되는 것만** 동작한다 —
  목록은 `web._JS_ASSETS` 하나뿐이고 라우팅·C1 회귀 테스트가 거기서 나온다
  (`/appwin.js` 창 수명 · `/app.js` SPA · `/report.js` 통계).
- **우리가 내보내는 모든 문서는 창 수명 프로토콜에 참여한다.** `_head` 가
  `/appwin.js` 를 싣고 `<!doctype` 이 그 한 곳뿐이라 예외를 둘 수 없다. 창 수명을
  `app.js` 에 두면 좌/우 셸 없는 전폭 페이지가 빠져 **창이 열린 채 서버가 죽는다**
  (2026-08-10 통계 페이지). 예외는 403 차단 페이지 하나(남의 오리진에서 렌더된다).
- **CSS 문자열은 raw 가 아니다.** `web._CSS`·`report.CSS`·`report.REPORT_JS` 는
  일반 문자열이라(`_APP_JS`·`_APPWIN_JS` 만 `r"""`) CSS 이스케이프를 `content: "\201C"` 로 쓰면
  파이썬이 `\201`을 8진수로 먹어 제어문자 U+0081 + 문자 `C`가 되고 브라우저엔
  두부가 뜬다(2026-07-26 실제 발생 — 분석 근거의 인용 부호가 깨져 있었다).
  백슬래시를 두 번 쓴다. `_CSS`·`report.CSS` 와 `_JS_ASSETS` 전부에 C1 제어문자가
  없는지 회귀 테스트가 지킨다(자산이 늘면 목록에서 자동으로 따라온다).

## 테스트

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

외부 의존이 없고 수 초 안에 끝난다. **기능을 바꾸면 테스트도 함께 바꾼다** —
이 저장소는 테스트가 사양서 역할을 한다(파일 하나에 1,100건 이상).
JS를 만졌으면 문법 검사도 한다:

```bash
for a in appwin app; do
  python3 -c "import sys;sys.path.insert(0,'.');from mailkb import web
print(web._APPWIN_JS if '$a' == 'appwin' else web._APP_JS)" > /tmp/$a.js
  node --check /tmp/$a.js || echo "$a.js 문법 오류"
done
```

## 여기서 검증할 수 없는 것

개발은 보통 Linux/WSL에서 하지만 **Outlook COM은 Windows에서만** 동작한다.
그래서 실제 메일 동기화와 `open`은 여기서 확인할 수 없다. 대신:

- **데모 홈**으로 기능을 돌린다 — `--home ./demo`, `sync --source fake --full`
- **웹 흐름은 HTTP 에뮬레이션**으로 확인한다. 임시 홈 + 각본대로 답하는 가짜 AI
  백엔드(`[ai.backends.*] cmd`에 스크립트를 지정)를 만들고 `serve --port`로 띄운 뒤
  `curl`로 실제 요청을 태운다. 폴링·리다이렉트·패널 주입까지 이 방식으로 잡힌다.
- **화면을 눈으로 볼 수 있다.** Windows가 딸린 WSL이면 헤드리스 브라우저로 로컬
  서버를 캡처한다. 색·대비·레이아웃처럼 마크업 검사로는 안 잡히는 것을 이걸로
  확인한다 — 다크 모드 색 문제와 CSS 이스케이프 버그를 이 방법으로 잡았다.

  **Edge가 아니라 Chrome을 쓴다** (2026-08-04 확인, 08-07 재확인). Edge는
  `--screenshot`이 exit 0을 주면서 파일을 안 만든다 — `--headless=new`/`=old`/구형
  셋 다, `file:///`을 찍어도 같다(즉 WSL localhost 문제가 아니라 Edge 쪽이다).
  Chrome은 같은 상자에서 문제없이 찍힌다.

  ```
  CHROME="/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
  "$CHROME" --headless=new --disable-gpu --no-sandbox --window-size=1250,900 \
    --force-device-scale-factor=1.3 --hide-scrollbars --virtual-time-budget=8000 \
    --user-data-dir="C:\Users\<사용자>\AppData\Local\Temp\cr-shot" \
    --screenshot="C:\Users\<사용자>\AppData\Local\Temp\shot.png" \
    "http://127.0.0.1:<포트>/<경로>"
  ```

  **상호작용까지 확인하려면 CDP로 몬다.** 선택·클릭·키 입력처럼 캡처로는 못
  보는 것을 실제 브라우저에서 확인할 수 있다(선택 검색·강조를 이렇게 검증했다).
  `--remote-debugging-port`는 **Windows 쪽에서만 닿는다** — WSL에서 그 포트로
  붙으면 실패하므로 `powershell.exe`로 `ClientWebSocket`을 열어 `Runtime.evaluate`를
  보낸다. 함정 둘: 명령 JSON은 PowerShell의 `ConvertTo-Json` 대신 **파이썬으로
  만들어 파일로 넘기고**(긴 스크립트에서 멈춘다), Task는 `.Wait(ms)` 말고
  `.GetAwaiter().GetResult()`로 기다린다.

  **과거 스크린샷이 남아 있으면 날짜부터 본다** — 낡은 `probe.png`를 보고
  "캡처가 된다"고 오판한 적이 있다. 안 되면 사용자에게 넘기고 확인한 척하지 않는다.

  `--user-data-dir`이 없으면 사용자 브라우저가 떠 있을 때 프로필 잠금으로 실패한다.
  UI가 `/app.js`로 그려지는 SPA라 `--virtual-time-budget` 없이 찍으면 빈 패널이
  나올 수 있다. 출력은 Windows 경로로 지정하고 `/mnt/c/...`로 읽으면 된다.
  **끝나면 내가 띄운 것만 골라 끈다** — 프로필 경로(`--user-data-dir`)로 프로세스를
  찾아 종료한다. 이름으로 죽이면 사용자 브라우저까지 닫힌다.

  **테마를 바꿨으면**: `POST /settings/theme`로 바꾸면 서버가 설정을 다시 읽지만
  (`_Handler.cfg` 갱신), `overrides.json`을 파일로 직접 고쳤으면 서버가 모른다 —
  그때는 다시 띄운다.
Windows 실기기 확인이 필요한 변경은 그렇다고 말하고 넘긴다 — 확인한 척하지 않는다.

## 검증하다 망가뜨리지 않기

위 방법들은 사용자의 실제 환경에서 돈다. 두 가지는 되돌릴 수 없다.

- 데모/에뮬 서버는 **띄울 때 PID를 적어 두고 그걸로 종료**한다(`serve`는
  `<home>/minerva.pid`도 남긴다). `pkill -f`나 `pgrep -f "mailkb" | xargs kill`
  같은 패턴 매칭은 쓰지 않는다 — 패턴이 에이전트 자신의 셸 명령줄까지 잡아
  같이 죽는다(실제로 겪었다). 헤드리스 브라우저도 이름이 아니라 `--user-data-dir`
  프로필로 골라 끈다 — 이름으로 죽이면 사용자 브라우저까지 닫힌다.
- **사용자의 `demo/`를 함부로 지우지 않는다.** `.gitignore`는 데모 산출물을
  "재생성 가능"이라 적지만 그건 **메일 얘기**다. 그 DB에는 재수집으로 복구되지
  않는 `knowledge_candidates`(암묵지 후보의 저장/유보 이력)·`ask_cache`(분석
  기록)·`report_done`(리포트에서 '처리함'으로 접은 항목)이 함께 쌓인다. 새
  코퍼스가 필요하면 임시 홈을 따로 만들고, 꼭 데모를 갈아야 하면 그 세 테이블을
  먼저 빼내고 되돌린다.

## 커밋

- 커밋·푸시는 사용자가 요청할 때만 한다.
- 메시지는 한국어. 제목은 `feat:`/`fix:`/`refactor:`/`docs:`/`style:` 접두어,
  본문은 **무엇을 바꿨는지보다 왜 바꿨는지**와 검증 방법을 적는다.
- 다른 에이전트의 작업을 커밋할 때는 그 사실을 본문이나 트레일러에 남긴다
  (예: `Assisted-by: OpenAI Codex`).

## 더 읽을 것

| 문서 | 내용 |
|---|---|
| [README.md](README.md) | 설치·데모·명령 — 사람이 따라 하는 순서 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 목적·모듈 지도·데이터 모델·파이프라인·기능 상세·설계 결정 |
| [agent-guides/](agent-guides/) | 조사 에이전트용 CLI 계약 — 어떤 명령이 읽기 전용이고 어떤 것이 AI를 부르는지 |
| [.claude/skills/mail-research/](.claude/skills/mail-research/) | 사용자용 조사 스킬(`/mail-research`) — **계약의 원본이 아니다**. agent-guides 를 가리키기만 하므로, 그쪽 파일 이름이 바뀌면 여기도 고친다(테스트가 막는다) |
