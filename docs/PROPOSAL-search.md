# PROPOSAL: 검색 개선 (search) — 설계안 v1

> 상태: **Phase 1·2 구현 완료** — P1(2026-07-14): DSL 파서(`search.py`) + 단계적
> 엔진(`store.search`) + 스마트 검색 UI + 헤더 검색창 + CLI(`--json`).
> **P2(2026-07-15)**: AI 흐릿한 기억 검색 — 번역→검색→재순위(+자기교정)→심층읽기.
> 제약(사용자 확정): **로컬 · stdlib 전용 · 임베딩/RAG 범위 밖**. skill 추출은 뒤 단계.
> 노브 확정: 제목:본문 = **3:1** · 완화 4단계 · 관련도 기본(OR만 '관련 낮음') ·
> AI=sonnet · 블로킹 · 캐시 지속 · DSL 표시·편집 · 심층읽기 상위 5건.

## 0. 문제

Outlook 등에서 "예전 메일 찾기"가 가장 큰 불만. 원인: (a) 한국어 부분일치가 약함,
(b) 발신자·기간·첨부 같은 구조 조건과 본문 키워드를 **한 번에** 못 건다, (c) 결과가
정렬·근거(스니펫) 없이 나열된다.

## 1. 큰 그림 — 2단 구조

| 단계 | 정체 | 상태 |
|---|---|---|
| **Phase 1** | 결정론 엔진 = trigram FTS5 + bm25 가중 + 단계적 완화 + 구조화 필터 + 스니펫 | **완료** |
| **Phase 2** | AI 계층 = 자연어→DSL 번역 + 유의어 확장 + 반복 + 후보 우선순위 정리(본문 합성 없음) | 예정 |

Phase 1 은 "제약 안에서의 천장". Phase 2 는 그 엔진을 **도구로 몰아** 흐릿한 기억을
메꾼다. skill = Phase 1 엔진(도구) + Phase 2 절차(방법)를 패키징한 것.

## 2. DSL — 검색식 문법

맨 토큰 = 키워드. `"..."` = 연속 구. 나머지는 연산자:

| 연산자 | 의미 | SQL |
|---|---|---|
| `from:X` | 보낸사람(이름/주소) | `REPLACE(sender_name,' ','') LIKE` OR `sender_addr LIKE` |
| `to:X` `cc:X` | 받는사람/참조 | 주소만 저장 → 한글이름은 **people 로 주소 해석** 후 `to_addrs LIKE` |
| `after:D` | D 이후(포함) | `sent_on >= floor(D)` |
| `before:D` | D 이전(배타) | `sent_on < floor(D)` |
| `on:D` | D 기간 내 | `floor(D) <= sent_on < ceil(D)` |
| `is:unread\|read` | 읽음 상태 | `read_at = ''` / `!= ''` |
| `is:sent\|received` | 방향 | `is_sent = 1 / 0` |
| `is:flagged` | 플래그 | `threads.flagged = 1` |
| `has:attachment` | 첨부 있음 | `attach_names != ''` |
| `file:X` | 첨부 파일명 | `attach_names LIKE` |
| `thread:N` | 스레드 번호 | `thread_id = N` |

`D` = `YYYY` | `YYYY-MM` | `YYYY-MM-DD`. 여러 `from:` 은 OR. 알 수 없는 `key:val`
(예: `http://…`)은 키워드로 취급. 주소 LIKE 는 ASCII 라 대소문자 무시(SQLite 기본).

## 3. 한국어의 두 함정 (실증 확인)

trigram FTS5 는 **3글자 미만 토큰을 색인 못 함** — `모델`·`평가`·`서빙`·`전환`·`침투`
같은 2자어는 개별 `MATCH` 가 **0건**. 검증(demo/db.sqlite, 192통):

```
MATCH '"모델"'      → 0      MATCH '"모델 평가"'  → 12   (붙어 있으면 5자 → 색인됨)
MATCH '"침투"'      → 0      MATCH '"침투테스트"' → 7
MATCH '"vLLM"'     → 12     MATCH '"리포트"'     → n
```

대응:
- **공백 정규화** — 이름/키워드 비교 시 양쪽 공백 제거(`REPLACE(...,' ','')`). `"김 부장"`
  저장돼 있어도 `from:김부장` 이 맞는다.
- **<3자 라우팅** — 2자 이하 키워드는 FTS 가 아니라 **LIKE** 로 처리(tier3). 붙어 있는
  2자어(`모델 평가`)는 phrase tier(tier1)가 별도로 건진다.
- **people 주소 해석** — 수신자는 표시명이 없어 한글 `to:이름` 은 people(addr,name)로
  주소를 먼저 찾는다.

## 4. 단계적 완화 (정밀 → 느슨) — tier

한 DSL 질의 안에서 정밀한 tier 부터 채우고, 상위가 부족할 때만 다음으로 내려간다.
tier 가 1차 정렬키, 같은 tier 안에서는 `bm25(fts, 3.0, 1.0)`(제목:본문=3:1) → 최신순.

| tier | 방식 | MATCH 예 (`모델 평가 리포트`) | 표시 |
|---|---|---|---|
| 1 | 연속 구(phrase) | `"모델 평가 리포트"` | 정확 |
| 2 | FTS AND | `"리포트"` (+2자어는 LIKE AND) | 정확 |
| 3 | LIKE AND(부분일치, 2자어 포함 전부) | `subject/body LIKE '%모델%' AND …` | 정확 |
| 4 | FTS OR(하나라도) | `"리포트" OR …` | **관련 낮음** |

구조화 필터는 **모든 tier 에 AND**. id 로 중복 제거. `is:flagged` 외에는 hidden 스레드도
검색은 포함(명시적 recall). 스니펫: `snippet(fts, 1, '⟪','⟫','…', 12)`.

두 겹 완화가 핵심: **엔진**이 tier(렉시컬) 완화, **AI**(Phase 2)가 유의어·이름 추정
같은 의미 수준 재구성. 임베딩 없이 의미 격차를 이 조합으로 메운다.

## 5. UI — 스마트 단일 검색창 + 접이식 상세 (사용자 확정)

전체 필드 폼(무겁고 대부분 빈칸)도, 순수 검색식(문법 암기 강요)도 아닌 하이브리드:

- **단일 검색창** = 연산자 인라인 인식. 맨 텍스트=키워드.
- **힌트 한 줄** = 자주 쓰는 연산자 노출(discoverability).
- **접이식 '상세'** = 사람/기간/방향/첨부/안읽음. 별도 쿼리 경로가 아니라 **검색식을
  만들어 검색창에 병합**(단일 진실원). 서버가 `f_*` 필드를 DSL 토큰으로 합쳐 다시
  검색창 value 로 보여줌 → 편집 가능. `_search_effective()`.
- **사람 자동완성** = `<datalist>` (people 왕래순 상위 200, 서버 렌더). 이름 표기·
  띄어쓰기 실수를 UI 로 방어.
- **결과 뒤 패싯 칩** = 상위 발신자 카운트. 클릭 시 `from:` 추가로 좁힘(서버 링크, JS 무관).

CSP 안전(인라인 스크립트 0) — 기존 app.js 의 링크/GET폼 가로채기만으로 동작. DB
재싱크 불필요(쿼리·렌더 계층) → `git pull` 즉시 반영.

## 6. 엔드포인트·인터페이스

- Web: `GET /search?q=<DSL>` (+상세는 `f_from`/`f_period`/`f_dir`/`f_has`/`f_unread`).
- CLI: `mailkb search '<DSL>' [--limit N] [--json]`. `--json` = 구조화 출력
  `[{id,thread_id,subject,sender,sender_addr,date,is_sent,has_attach,tier,snippet}]`
  — Phase 2/skill 이 소비할 도구 계약.

## 7. Phase 2 — 흐릿한 기억 (예정, RAG 없음)

**구현됨 (`review.ai_search`)**. 진입은 명시적 opt-in — 일반검색(무료) 결과 위
`🤖 AI로 다시 찾기` 버튼(웹) / `search --ai`(CLI). 파이프라인:

1. **캐시**(`ai_search` 테이블, 질의 정규화 키) 히트면 즉시 반환 → 뒤로가기·반복 무과금
2. **번역**(sonnet) NL→`{dsl, fallback_dsl, expansions, note}`, 파서로 검증(빈/무의미면 원문 폴백)
3. **엔진 검색** `store.search(dsl)`, 0~2건이면 `fallback_dsl`로 엔진만 완화(AI 추가호출 없음)
4. **재순위**(sonnet, iii) 후보 제목·발신·날짜·스니펫만 보고 정렬+이유+관련여부
5. **자기교정**(iii) 관련 후보 0이면 재번역·재검색 **1회**
6. **심층읽기**(sonnet, iv-lite) 상위 **5건** 본문까지 읽어 확정·재정렬(무관 탈락)

AI 호출 보통 3회(번역·재순위·확정), 자기교정 시 5회 — ~$0.03–0.06/검색. 동시성=블로킹
(단일 스레드; app.js가 "AI가 찾는 중…" 표시). AI는 **DSL만** 출력하고 파서가 정화(원시
SQL 금지). CLI 부재·타임아웃은 `AIError`→일반검색 폴백. 해석 DSL은 화면에 노출·편집 가능
(오해석을 AI 재호출 없이 교정). **범위**: '찾는 메일' 판단까지 — 답 합성(지식검색)은 별도.

## 8. Skill 추출 (뒷단계)

"임의의 SQLite DB를 효과적으로 검색"하는 재사용 skill 로 일반화 가능. skill 이 AI 에게
주는 것: ① 하드닝된 검색 도구(`search --json`, DSL·랭킹·완화 내장 — 원시 FTS 금지)
② 스키마 매핑(컬럼 역할·가중치) ③ 연산자 사전 ④ 절차(제약 추출→유의어 확장→엄격
우선→부족하면 완화 반복→상위 근거 제시) ⑤ 폴백 읽기전용 SQL. mailkb 는 파라미터
인스턴스(스키마·`from:`↔`sender_*`·τ·연산자 사전만 갈아끼움).

## 9. 파일

| 파일 | 역할 |
|---|---|
| `mailkb/search.py` (신규) | 순수 DSL 파서 + 날짜 경계 + tier MATCH 생성 |
| `mailkb/store.py` `search()` | 엔진: 필터 조립 + 단계적 FTS/LIKE + bm25·스니펫 + `_resolve_addr`·`frequent_people` |
| `mailkb/web.py` | 헤더 검색창·`render_search`(상세·패싯·스니펫)·`render_aisearch`·`/search?ai=1` |
| `mailkb/cli.py` `cmd_search` | 스니펫 출력 + `--json` + `--ai` |
| `mailkb/review.py` (P2) | `ai_translate_query`·`ai_rank_candidates`·`ai_confirm_top`·`ai_search` + 프롬프트 |
| `mailkb/store.py` (P2) | `ai_search` 캐시 테이블·`ai_search_get/put/recent`·`messages_by_ids` |
| `mailkb/config.py` (P2) | `ai_search_backend`(기본 sonnet) |

## 10. 남은 것 (후속)

- bm25 recency 블렌드는 현재 '동점 시 최신순' 정도 — 필요 시 `−bm25 + w·exp(−age/τ)`
  시간감쇠로 강화(τ 는 skill 파라미터).
- 실데이터로 제목:본문 3:1 · 완화 임계 · AI 프롬프트 튜닝.
- '최근 AI 검색' 목록 노출(테이블·`ai_search_recent` 이미 있음).
- **skill 추출**(§8) — `search --json`/`--ai` 계약이 기반.
