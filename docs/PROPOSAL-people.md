# 인물 도시에 (people dossier) — v1 (2026-07-18)

## 문제

`/person?addr=` 는 "이 사람과 주고받은 메일 목록"일 뿐, "이 사람과 지금 뭘 함께
하고 뭐가 미결인가"를 보여주지 못한다. 재료는 이미 다 있는데 사람 중심으로 재조립된
화면이 없었다:

- 수확이 뽑은 **인물 신호**는 `distill_signals`(store.py) 에 쌓이지만 **읽는 코드가
  없었다**(write-only) — 화면에 안 나왔다.
- `decisions`(장기기억)의 `decider` 는 "누가 결정했나"를 알지만 사람 관점 조회가 없었다.
- report.py 의 사람별 집계(교신량·응답성·왕복·증발)는 전부 "전원 대상 + 시간창"이라
  단일 인물 진입점이 없었다.

## v1 — 결정론 재조립 (AI·새 테이블 없음)

새 최상위 **'인물'** 메뉴. 초기 v1은 스키마 변경 0, 재수집 0으로 기존 테이블만
읽었다. 아래 업무 어휘 지도 개선은 Outlook 재수집 없이 재구축 가능한 파생
테이블을 추가한다.

- **랜딩 `/people`**: 최근 6개월 **교류 강도**순(미결 우선 아님). 각 행 = 이름 ·
  수신/발신 통수 · 미결 배지 · 최근 접촉. 봇/자동발송(ignore/blocked)만 제외,
  외부 협력사는 남긴다(도시에 대상). 내 주소는 people 에서 이미 빠짐.
  - **교류 강도는 갈아끼우기 쉽게 분리**: 데이터 수집(`store.person_window_counts`)
    과 점수 공식(`report._intensity` — 순수 함수)을 나눴다. v1 기본은 수신 빈도
    위주(`recv*1.0 + sent*0.5`). 정렬을 바꾸려면 `_intensity` 한 곳만 고친다.
  - 기간 = `cfg.opt("dossier","window_weeks", default=26)` (config 무수정 조정).
    창은 DB 최신 메일(asof) 기준 상대 → 결정론·테스트 안정.
- **도시에 `/people?addr=`**: 결정론 6카드(재료 없는 카드는 안 그림).

  | 카드 | 원천 |
  |---|---|
  | 관계 수치 | **시각화**: ① 주고받기 균형 막대(recv/sent 비율) ② 회신 속도 비교 막대(이 사람/나 중앙값) ③ 주별 교신 스파크라인 + 최근 접촉. `report.person_metrics`(recv/sent·`_reply_pairs`/`_their_pairs` 중앙값 + 주별 시계열) |
  | 진행 중 | `report.sig_pingpong` 를 이 addr 참여 스레드로 필터 |
  | 서로의 미결 | ① `actions.classify_threads` source 발신자=이 사람·REQUIRED ② `report.sig_evaporated` addr 필터 |
  | 관여한 결정 | `store.person_decisions(addr,name)` — decider 매치를 참여 스레드로 교집합 |
  | 최근 변화 | `store.person_signals(addr,name)` — distill_signals **첫 소비처** |
  | 업무 어휘 지도 | 최근 6개월 본인 발신 메일의 문서 빈도·다른 인물 대비 점수·반복 구문·공기어 군집·상승어·사람 언급 — `terms.analyze` |

- 모든 항목에 **근거 스레드 링크 `[#nnn]`**. 도시에 하단 "전체 왕래 메일 →" 로
  기존 `/person` 메일 목록에 도달. 스레드 상세의 이름 클릭도 `/person` → `/people`
  (도시에)로 승격.

### 관계 수치 시각화

숫자를 읽지 않아도 관계의 모양이 보이도록 카드 본문을 작은 시각화 3요소로 한다
(`web._relmetrics_html`). 재료 없는 요소는 생략(graceful) — 셋 다 비면 카드 자체가
안 나온다.

- **① 주고받기 균형 막대**: 받은/보낸을 한 pill 막대의 두 색 세그먼트(받은=accent,
  보낸=accent2)로 비율 표시 → "누가 더 보내나"가 즉시.
- **② 회신 속도 비교**: 이 사람 vs 나 응답 중앙값을 공통 스케일 막대로. 길수록
  느림 → 병목이 보인다. 표본 한쪽만 있으면 그 행만.
- **③ 주별 교신 스파크라인**: 주별 (recv+sent) 총량을 자족적 인라인 SVG polyline
  (`web._spark_svg`, 3주 미만·전부 0이면 생략)로 → 달아오르나 식나. 옆에 최근 접촉.
- **데이터**: `report.person_metrics` 가 스칼라(recv/sent·응답 중앙값·최근 접촉)에
  더해 **주별 시계열**(`recv_series`/`sent_series`)을 준다 — 기존 주 축(`d["wk"]`)
  재사용이라 추가 비용 거의 0. 새 테이블·쿼리 없음.
- **자족적 마크업**: web.py 팔레트 토큰만 쓰고 report.py 차트 CSS(`/stats` 전용)에
  의존하지 않아 라이트/다크 자동 대응. 관찰 가능한 사실만(평가·추정 없음).

### 업무 어휘 지도 v1.1 (관찰 가능한 관여 영역)

역량·성과 "평가"는 배제하되, 그 사람이 **실제로 무엇을 다뤘나**는 사실이므로 넣는다.
본인이 **발신한** 메일(`is_sent=0 AND sender_addr=addr`)을 최근 26주로 제한해,
단순 단어 크기 대신 "이 사람이 반복해서 다루고 다른 사람보다 두드러지는 것"을
작은 어휘 지도로 보여준다. AI·외부 API·형태소 분석기 없이 stdlib 로만 계산한다.

- **메일 단위 지지도**: 한 메일에서 같은 단어를 20번 써도 1통으로 센다. 최소 2통에
  나타난 표현만 후보라 긴 한 통·붙여넣기 문구가 특징을 독점하지 않는다.
- **대조 점수**: 대상 메일 지지도와 최근 6개월 상위 교신자 메일의 지지도를 비교한다.
  전원이 쓰는 공통어는 자동 강등하고 그 사람에게 집중된 표현을 올린다.
- **반복 구문·공기어 군집**: 정제된 문장 안 인접 2어절을 반복 구문으로 잡고,
  메일 동시 출현(NPMI + 직접 인접도)을 평균 연결로 묶어 관련 어휘를 한 행에 둔다.
- **최근 상승**: 6개월 전체와 최근 6주를 비교해, 과거 표본이 충분하고 최근 지지율이
  1.8배 이상인 표현만 별도 표시한다. 시간만으로 원자료 상태를 바꾸지 않는 읽기 오버레이다.
- **근거와 사람 분리**: 모든 표현은 최신 근거 스레드로 연결한다. `people`에 등록된
  다른 사람 이름은 특징어에서 빼고 "함께 언급"으로 분리한다. 미등록 이름은 정보 손실을
  피하려고 일반 후보로 남는다.
- **입력 정제**: `strip_preserved` → 서명/고지 꼬리 제거 → URL·이메일 제거 →
  한글 2자+/영문 3자+ 토큰 → 어미·호칭·조사 스트립 → 불용어. 제목은 같은 사람이
  같은 스레드에서 반복 회신해도 한 번만 반영한다.
- **불용어**: 한/영 표준은 **stopwords-iso**(MIT, github.com/stopwords-iso)에서
  발췌 — 한국어 조사·기능어(에서·으로…)와 영어 기능어(the·and·for·is·in…),
  거기에 업무메일 상투어·직함·서명 라벨, 웹/툴명(http·https·www·confluence·jira·
  첨부 확장자)을 더했다.
- **표시 임계**: 발신 통수 `cfg.opt("dossier","wordcloud_min_mails",default=8)` 이상만.
  상위 후보 수 `wordcloud_top`(기본 25). 기존 설정 키를 유지해 업그레이드 시
  config 변경이 필요 없다. 실메일 튜닝은 `word_stop_extra`로 한다.
- **성능·캐시**: 표본 수와 대조군은 줄이지 않는다. `message_term_features`가
  분석 대상인 수신 메일의 설정 독립 문장 토큰을 sync에서 한 번 추출하고,
  `message_term_bags`가 현재 이름·추가 불용어를 반영한 compact bag을 저장한다.
  두 JSON은 zlib BLOB으로 압축한다. `person_term_window`는 현재 26주 본문 DF를
  `(kind,term,sender)` 기본키로 직접 유지한다. 새 메일은 더하고 창 밖 메일 bag은
  빼므로 일·월 버킷이나 조회 시 전체 GROUP BY가 필요 없다.
- **후보 조회**: 대상에게 2통 이상 나타난 term/phrase만 대조 DF가 필요하다.
  임시 후보 테이블을 바깥 루프로 고정해 rolling 기본키를 후보별로 탐색한다.
  제목은 `message_term_subject_delta`의 `subject-body` 차집합을 창 안
  sender/thread 첫 메시지에서만 더한다. eligible 전체 후보 DF는 메모리에
  지연 캐시하고 대상 DF를 빼 여러 인물 조회에 재사용한다. 상위 K phrase 제한은
  하지 않아 종전 결과와 같다.
- **수명주기**: 기존 DB의 토큰 백필은 웹 시작이 아니라 다음 sync 트랜잭션에서
  수행한다. 최근 설정 기간의 수신 메일만 파생 저장하고, 창 밖 feature·bag·DF는
  제거한다. 준비 전이나 기간 확대 직후에는 종전 원문 경로로 폴백한다.
  이름·불용어·규칙 버전이 바뀌면 필요한 파생 단계만 재구축하며 Outlook
  재수집은 없다.
  최종 `people_word_profiles` 캐시는 대상 최신 ID와 실제 26주 대조 메일 집합
  지문을 사용한다. 날짜 문자열만 바뀌어 실제 집합이 같으면 캐시를 유지하고,
  새 대조 메일이나 창 이탈로 점수가 달라지면 정확히 무효화한다.
- **측정**: 고유 어휘·300토큰·50인·고유 스레드 합성 데이터에서 5천 통 최초
  fast 계산 70ms(대상 읽기+후보+대조+분석), raw 분석 2.88초, 정확한 지문을
  포함한 대조 재사용 4.2ms, 장문 1통 증분 sync 46ms였다. DB 전체 81.0MiB 중
  어휘 파생 구조는 34.3MiB였다. 1만 통은 fast 146ms, raw 5.84초였고,
  DB 전체 157.5MiB 중 어휘 파생 구조는 68.0MiB였다. 최초 1만 통 파생 구축
  31.0초는 sync에서 1회 수행한다. fast/raw 결과는 동일했다. 재현은
  `tests/benchmark_wordmap.py`.
- **한계**: 무형태소라 복합어·띄어쓰기 변형을 완전히 합치지 못한다. 서명 구분자나
  고지 패턴이 없는 조직 고유 서명은 `word_stop_extra`가 필요할 수 있다. 점수는
  업무 관여 신호이지 역량·성과·성격 평가가 아니다.

## 인물 식별 (동명이인·별칭)

**정체성 기준 = 이메일 주소.** 도시에는 addr 당 하나.
- **동명이인**: addr 기반 데이터(관계 수치·진행중·미결)는 자동 분리. 이름 매칭
  카드(결정·변화)만 위험 → **참여 스레드 교집합**으로 방어(`store.person_thread_ids`).
  랜딩에서 표시 이름이 겹치면 도메인 접미사로 구분.
- **별칭(한 사람 여러 주소)**: v1 은 주소별 분리 유지(내 별칭만 `my_addresses` 로
  합쳐짐). v2 에서 별칭 맵으로 병합(알려진 한계).

## 손댄 곳

- `store.py`: `person_thread_ids` · `person_window_counts` · `person_signals` ·
  `person_decisions` · 메일별 어휘 특징/bag · rolling DF · 파생 프로필 캐시.
- `report.py`: `_intensity`(분리) · `rank_people` · `person_metrics`(addr 어댑터).
- `terms.py`: 문서 빈도·대조 점수·구문·공기어 군집·상승어·언급 분리 순수 코어.
- `web.py`: `render_people_page` · `render_dossier` · `_wordmap_html` +
  스레드 이름 링크 대상 변경. CSS `.plist/.prow/.dcard/.wordmap` 등.
- 원 설계(`PROPOSAL-distill.md` 3-D)는 도시에를 `/person` 상단 카드로 뒀으나,
  이번엔 **새 최상위 메뉴**로 간다(의식적 divergence).

## v2 코어 (구현됨)

v1 결정론 카드는 토대로 남고 위에 AI 계층이 얹힌다(실패·노후여도 v1 카드는 항상
표시 — graceful).

- **AI 도시에 요약 카드**(도시에 최상단): `## 역할`·`## 지금 함께 하는 일`·`## 병목`.
  `people_dossier` 캐시에서 읽어 렌더. 없으면 카드 미표시(v1 카드만).
  `web._dossier_ai_card`.
- **캐시 테이블**: `people_dossier(addr PK, dossier_md, updated, basis_msg_count,
  validator_version)`.
  파생 아님(AI 산출물) → 백필/버전 변경에도 살아남고, 스키마는 `CREATE TABLE IF NOT
  EXISTS` 라 `git pull` 후 재싱크 불필요. `validator_version`은 구 DB에 경량
  `ALTER TABLE`로 자동 추가한다.
- **발화자까지 검증**: AI 출력의 각 줄은 `- [#N] 서술 · 인용: "발췌"` 형식.
  도시에 전용 `_PersonQuoteChecker`가 **인용이 대상 인물이 직접 발신한 단일
  메시지의 신규 작성분**에 실제로 있을 때만 채택한다. 내 발언·제3자 발언·
  `PRESERVED_MARK` 아래 전달/보존 인용·메시지 경계를 합친 문자열은 버린다.
  입력 프롬프트도 `person_thread_context`가 대상 발신 최근 2통만 제공한다.
  표시부엔 인용 꼬리를 떼고 서술+`#N`(근거 링크)만 남긴다.
  `distill._PersonQuoteChecker`/`_gen_dossier`/`_sanitize_dossier`.
- **검증 캐시 수명주기**: 현재 규약은 `DOSSIER_VALIDATOR_VERSION=2`. 구버전
  카드는 즉시 숨기고 다음 AI 실행에서 최대 6명씩 점진 재생성한다. AI 호출 성공 후
  전부 검증 탈락(`empty`)하거나 대상 발췌가 없으면 basis를 전진시켜 동일 입력
  재과금을 막고, 호출 자체가 실패한 경우만 다음 실행에서 재시도한다.
- **증분 갱신(비용 통제)**: `distill.refresh_people_dossiers` — 상위 ~15명 중
  `person_msg_count` 가 basis 이후 늘어난 사람만, 활동 증가분 큰 순 최대
  `cfg.opt("dossier","refresh_max_per_run",default=6)` 명. **데일리 AI 계층의 7번째
  단계**로 실행(별도 스케줄러 없이 자동). 새 메일이 없고 검증 버전도 같으면 콜
  자체가 없음. 버전 불일치는 새 메일 증가분보다 우선 갱신한다.
- **랜딩 역할 한 줄**: `store.dossier_roles()` — 캐시된 `## 역할` 첫 서술을 인물
  목록 각 행에 표시(생성 아니라 캐시 읽기라 비용 0).
- **백엔드**: 요약용(`ai_summary_backend`, 사내/로컬) — 회사 메일 발췌가 외부로
  나가면 안 됨.

## v2.1 (미룸)

- **최근 변화 → AI 서술** + `distill_signals.consumed` 소비 완성(현재 v1 은 원문
  나열).
- **타임라인 카드**(역할·담당 변화 시계열).
- **사용자 수정 보호**: 사람이 고친 줄은 AI 재생성이 덮지 않음(장기기억 원장의
  confirmed 수명주기 패턴). 현재 코어는 재생성 시 캐시 전체 교체.
- **엄격한 구조화 주장 JSON**(`{claim, confidence, thread_ids, quote}`): 코어는
  마크다운 + 인용 검증으로 등가 효과를 내되, `confidence` 필드는 아직 없음.

**가드레일**: 관찰 가능한 업무 사실 + 메일 근거만. 성격 평가·감정 추정·성과 점수
같은 감시성 정보 제외. 도시에 ≠ 인물 업무 브리핑(3-G, on-demand 심층 문서).
