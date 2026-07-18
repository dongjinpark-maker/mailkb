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

새 최상위 **'인물'** 메뉴. 스키마 변경 0, 재수집 0 — 기존 테이블만 읽는다.

- **랜딩 `/people`**: 최근 3개월 **교류 강도**순(미결 우선 아님). 각 행 = 이름 ·
  수신/발신 통수 · 미결 배지 · 최근 접촉. 봇/자동발송(ignore/blocked)만 제외,
  외부 협력사는 남긴다(도시에 대상). 내 주소는 people 에서 이미 빠짐.
  - **교류 강도는 갈아끼우기 쉽게 분리**: 데이터 수집(`store.person_window_counts`)
    과 점수 공식(`report._intensity` — 순수 함수)을 나눴다. v1 기본은 수신 빈도
    위주(`recv*1.0 + sent*0.5`). 정렬을 바꾸려면 `_intensity` 한 곳만 고친다.
  - 기간 = `cfg.opt("dossier","window_weeks", default=13)` (config 무수정 조정).
    창은 DB 최신 메일(asof) 기준 상대 → 결정론·테스트 안정.
- **도시에 `/people?addr=`**: 결정론 6카드(재료 없는 카드는 안 그림).

  | 카드 | 원천 |
  |---|---|
  | 관계 수치 | 창 내 recv/sent·최근 접촉 + `report._reply_pairs`/`_their_pairs` addr 필터 중앙값 |
  | 진행 중 | `report.sig_pingpong` 를 이 addr 참여 스레드로 필터 |
  | 서로의 미결 | ① `actions.classify_threads` source 발신자=이 사람·REQUIRED ② `report.sig_evaporated` addr 필터 |
  | 관여한 결정 | `store.person_decisions(addr,name)` — decider 매치를 참여 스레드로 교집합 |
  | 최근 변화 | `store.person_signals(addr,name)` — distill_signals **첫 소비처** |
  | 주요 어휘 | 본인 발신어 빈도 태그 클라우드 — `store.person_sent_texts` + `report.top_words` |

- 모든 항목에 **근거 스레드 링크 `[#nnn]`**. 도시에 하단 "전체 왕래 메일 →" 로
  기존 `/person` 메일 목록에 도달. 스레드 상세의 이름 클릭도 `/person` → `/people`
  (도시에)로 승격.

### 주요 어휘 (관찰 가능한 관여 영역)

역량·성과 "평가"는 배제하되, 그 사람이 **실제로 무엇을 다뤘나**는 사실이므로 넣는다.
본인이 **발신한** 메일(`is_sent=0 AND sender_addr=addr`)의 정제 본문에서 단어 빈도를
뽑아 태그 클라우드로 보여준다 — 도메인 어휘(SoC·타이밍·양자화·CVSS…)가 드러나
"누구한테 물어보나"에 답한다.

- **결정론·무AI.** 형태소 분석기(mecab/konlpy) 없이 stdlib 로만: 토큰 분리 →
  어미·호칭·조사 스트립(어간 2자+ 가드로 결과→결 과잉절단 방지) → 불용어(상투어·
  업무동사·직함·서명 라벨) → 1회성 제외(min_count) → 상위 N. **형태소 분석이 아니라
  빈도 뷰**라 이름을 '주요 어휘'로 한다.
- **표시 임계**: 발신 통수 `cfg.opt("dossier","wordcloud_min_mails",default=8)` 이상일
  때만(표본 적으면 상투어만 남음). 상위 수 `wordcloud_top`(기본 25).
- **제외**: 내 이름·본인 이름(서명 누출)·봇 발신자(하드 노이즈). 실메일 튜닝은
  `cfg.opt("dossier","word_stop_extra")` 로 config 무수정 추가.
- **한계**: 무형태소라 복합어·띄어쓰기 오류는 못 잡는다. 단어별 근거 링크는 여러
  스레드에 걸쳐 v1 생략(발신 통수만 표기). 진짜 공기어 군집화는 v2 AI 영역.

## 인물 식별 (동명이인·별칭)

**정체성 기준 = 이메일 주소.** 도시에는 addr 당 하나.
- **동명이인**: addr 기반 데이터(관계 수치·진행중·미결)는 자동 분리. 이름 매칭
  카드(결정·변화)만 위험 → **참여 스레드 교집합**으로 방어(`store.person_thread_ids`).
  랜딩에서 표시 이름이 겹치면 도메인 접미사로 구분.
- **별칭(한 사람 여러 주소)**: v1 은 주소별 분리 유지(내 별칭만 `my_addresses` 로
  합쳐짐). v2 에서 별칭 맵으로 병합(알려진 한계).

## 손댄 곳

- `store.py`: `person_thread_ids` · `person_window_counts` · `person_signals` ·
  `person_decisions`.
- `report.py`: `_intensity`(분리) · `rank_people` · `person_metrics`(addr 어댑터).
- `web.py`: `render_people_page` · `render_dossier` · nav/route/paneFor/tops +
  스레드 이름 링크 대상 변경. CSS `.plist/.prow/.dcard` 등.
- 원 설계(`PROPOSAL-distill.md` 3-D)는 도시에를 `/person` 상단 카드로 뒀으나,
  이번엔 **새 최상위 메뉴**로 간다(의식적 divergence).

## v2 (다음)

v1 결정론 5카드는 토대로 남고 위에 AI 계층이 얹힌다(실패·노후여도 v1 카드는 항상
표시 — graceful).

1. **AI 도시에 요약 카드**(최상단): 역할·조직 추정 + "지금 함께 하는 일" + **제3자
   병목**. `people_dossier` 캐시에서 읽음.
2. **캐시 + 증분 갱신**: `people_dossier(addr PK, dossier_md, updated,
   basis_msg_count)`(PROPOSAL-distill.md:137-139). 주간 잡이 메시지 늘어난 상위
   ~15명만 재생성 → 비용 통제. 데일리 잡의 백그라운드+폴링 패턴 재사용.
3. **AI 출력은 구조화 주장으로 제한**: `{claim, confidence, thread_ids, quote}`
   (장기기억·액션 분류기와 같은 환각 차단). **사내/로컬 백엔드만**.
4. 랜딩에 캐시된 역할 한 줄. 최근 변화 → AI 서술(+`distill_signals.consumed`).
5. 타임라인 카드. 사용자 수정 보호(AI 재생성이 덮지 않음).

**가드레일**: 관찰 가능한 업무 사실 + 메일 근거만. 성격 평가·감정 추정·성과 점수
같은 감시성 정보 제외. 도시에 ≠ 인물 업무 브리핑(3-G, on-demand 심층 문서).
