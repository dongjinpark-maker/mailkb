# PROPOSAL: 성능 개선 (perf) — 스케일·런타임 지연

> 상태: **Batch 1·2·3 구현 완료(2026-07-17)**. 원칙(사용자 확정): **기존 동작 결과는
> 100% 동일, 속도만 개선**. 결과가 달라지거나 DB 재싱크가 필요한 것은 이 배치에서 제외.
>
> 예외 1건(의도) — Batch 3 에서 `/thread/N` **전체 로드**의 좌측이 홈 → 스레드 목록으로
> 바뀌었다(`_panes` + app.js `leftCur`). 오른쪽이 스레드인데 왼쪽이 대시보드면 어색해서다.
> 앱 내 클릭 동선은 불변. 그 외 렌더는 구/신 바이트 동일함을 확인했다.

## 0. 문제

2년치가 쌓이면 DB 가 수 GB → (a) sync 기간이 길수록 느려지고, (b) 목록·통계·데일리
쿼리가 전수 스캔으로 커지며, (c) 웹 상호작용 중에도 지연·프리즈가 생긴다.

## 1. 진단 — 시간이 실제로 드는 곳 (코드 근거)

| 축 | 병목 | 근거 |
|---|---|---|
| Sync | **Outlook COM 왕복** (통당 15~25회: HTMLBody·전송헤더·수신자 SMTP) | `sources/outlook_com.py:_to_record` |
| 읽기 | `date(sent_on)=?` 가 인덱스 못 씀 → 전수 스캔 | 데일리·홈 쿼리 (측정: 20k행 **513x**) |
| 읽기 | /통계가 **전 이력 본문**을 매번 로드 | `report.load` (측정: 30k통 160→46ms) |
| 웹 런타임 | **sync 가 서빙 스레드 블로킹** → UI 전체 프리즈 | `/autosync`·`/sync` 인라인 |
| 웹 런타임 | 요청마다 Store 열고 닫음 → 마지막 연결 close 시 WAL 체크포인트 | 측정: 요청당 ~2.2ms |
| 웹 런타임 | `/latest` 60초마다 `COUNT(*)` 전수 스캔 | do_GET `/latest` |

## 2. Batch 1 — 결과 불변, 재싱크 불필요 (구현됨)

- **성능 PRAGMA** (`store.py`): `synchronous=NORMAL`(WAL 표준 — 앱 크래시 안전, 이 DB 는
  Outlook 재수집 가능 캐시라 정전 유실도 멱등 복구) · `cache_size=16MB` · `temp_store=MEMORY`
  · `mmap_size=256MB`. 읽기·쓰기 양쪽 가속(테스트 스위트 5.5→2.6s).
- **`date(sent_on)` → 범위** (`store.py` 4개 쿼리): `col >= ? AND col < date(?, '+1 day')`.
  ISO 문자열이라 증명 가능 등가. `idx_messages_sent_on` 범위 스캔 → **20k행 513x**. 경계
  등가성 테스트(`test_date_range_queries_exact_boundaries`).
- **`report.load()` 창 한정** (`report.py`): 전 이력은 메타(주소·is_sent·날짜)만, 큰
  `new_content` 는 검토 창 안만 로드. 출력 동일(별칭 my_addrs·기간축은 전 이력 유지).
  `test_period_bounds_dataset` 가 불변식 가드. **30k통 160→46ms**.
- **`/latest` COUNT 제거**: append-only 라 `MAX(rowid)` 만으로 변경 감지 등가.
- **keep-alive 연결** (`serve()`): idle 읽기 연결 하나를 상시 열어 요청 close 가
  '마지막 연결'이 아니게 함 → 요청당 WAL 체크포인트 폭주 제거(~2x).

## 2-b. 목록 신호 메모리 캐시 (Batch 3 이전 구현)

초기 구현은 `(설정·MAX(rowid))` 지문이 같을 때 수신 전수 스캔을 건너뛰어 반복 렌더를
빠르게 했다. 그러나 새 메일 한 건에도 지문이 바뀌어 다음 렌더가 1만 건 본문을 다시
읽었다. 프로세스 재시작도 항상 cold였다. 정확성을 단순하게 지키는 이점은 있었지만,
주기 sync 환경에서는 무효화 단위가 지나치게 컸다. Batch 3에서 본문 신호는 영속 파생
상태로, 설정 의존 노이즈는 append-only high-water 증분 캐시로 대체했다.

## 3. Batch 2 — sync 백그라운드화 (구현됨)

`/sync`(수동)·`/autosync`(주기)가 서빙 스레드에서 COM 수집을 돌려 **UI 전체를
프리즈**시키던 것을 전용 스레드로 이관(리뷰·AI검색과 동일 패턴). 수집 **결과는 동일**.

- `_do_sync(store, cfg)` — 수집+프룬 순수 동작(잡·테스트 공용, 수집 실패에도 프룬 보장).
- `_run_sync_job` — 스레드에서 `CoInitialize`(COM 은 스레드마다 필요) 후 `_do_sync`.
- `_start_sync` — 단일 슬롯 가드. `/sync`→대기화면(`/sync/status`)+`hookSyncPolling`,
  `/autosync`→논블로킹(`started`/`busy`)+`watchSyncToast` 가 완료 시 '신규 N' 토스트.
- 완료 msg 는 `data-sync-msg` 로 실어 수동/자동 모두 토스트 보존.

## 2-c. `idx_threads_last_date` 인덱스 (구현됨, 무재싱크·무위험)

`/threads` 목록의 `ORDER BY t.last_date DESC LIMIT` 이 인덱스 없이 전수 스캔+임시
정렬(`USE TEMP B-TREE`)하던 것을 인덱스로. **30k 스레드 2.06→0.03ms(70x)**, EXPLAIN
이 `USING INDEX idx_threads_last_date` 확인. 스레드 수에 비례해 커지는 유일한 부분이라
스케일 보험. `CREATE INDEX IF NOT EXISTS`(빌드 8.6ms/30k, 다음 서버 시작 시 자동, 재싱크
불필요). `test_threads_last_date_index` 가드.

## 4. Batch 3 — 영속 파생 상태·증분 갱신 (구현됨)

1만 통·2,500 스레드에서 홈 313ms(SQL 2,640회), 메일함/스레드 cold 약 460ms를 확인했다.
DB를 `message_html`로 20.9MB→979MB까지 키워도 시간은 같아 파일 크기보다 행 수와 반복
계산이 원인이었다.

- `message_features`: 기한·결정·요청·질문·내 수신 여부를 `new_content` 정제 직후 1회 계산.
- `thread_state`: 첫/최신 메시지 id, 송수신·미개봉·기한·참여 카운트를 수집 트랜잭션에서
  증분 UPSERT. 역순 메일은 `(sent_on,id)` 비교로 첫/최신만 정확히 교체.
- `derived_version`: 규칙 또는 내 주소가 바뀔 때만 기존 `messages`에서 1회 로컬 백필.
  재수집·DB 삭제 불필요. `serve()`의 keep-alive Store가 브라우저를 열기 전에 수행한다.
- `_signal_sets`: 전체 본문 정규식 제거, `thread_state`와 최신 메시지의 좁은 조인만 수행.
- `_noise_sets`: 새 `rowid` 이후만 판정. 설정 변경·프로세스 시작 때만 좁은 메타 전수.
- 홈: `unanswered`의 `_last_to_me` N+1 제거, 최신 메시지·카운트는 `thread_state` 조인,
  정체 후보 메시지는 800개 단위 배치 조회. 홈 SQL 2,640→8회.
- 목록: 스레드 행의 5개 상관 서브쿼리를 조인으로, 메일함 탭 카운트를 SQL 집계로 전환.
- 최신 메시지 인덱스 `messages(thread_id, sent_on DESC, id DESC)` 추가.

합성 10k/2.5k 재측정: **홈 313→57.7ms**, **메일함 cold 468→22.3ms**,
warm 7.1ms, **새 메일 한 건 직후 8.3ms**, **스레드 462→4.0ms**. 초기 백필 55.8ms.
기존 375건 + 파생/증분 회귀 4건, 총 379건 통과. Windows 실 DB 시간은 별도 확인한다.

## 5. 재싱크 가정 시 순위 재평가 (측정으로 반증·정정)

사용자 질의: "재싱크한다면 runtime 이득 순위". 측정 결과 대부분 재싱크가 불필요하거나
가치가 없다고 판명:

| 후보 | 판정 | 근거 |
|---|---|---|
| **sync COM — HTMLBody 조건부** | **reject** | 오래된 메일 저장 텍스트가 마크다운→평문 = **결과 변경**. 헤더 생략도 스레딩 결과 위험 |
| sync COM — `SetColumns`·SMTP 캐시 | 보류 | 선택적 최적화의 실제 이득·결과 불변성은 별도 벤치마크 필요 |
| **`new_content` 분리** | **비권장** | 목록 SELECT * 0.19 vs 컬럼선택 0.05ms — LIMIT 쿼리는 오버플로 안 읽어 이득 ~0 |
| **threads 표시필드 파생 상태** | **채택(Batch 3)** | 목록뿐 아니라 홈이 전체 스레드 최신값·카운트를 반복 계산. 영속 `thread_state`로 공용 해결 |
| 전체 연결 재사용 | 보류 | keep-alive 로 안전한 절반 확보, 나머지는 트랜잭션 상태 위험 |
| keyset 페이지네이션 / `ANALYZE` | 후속 | 안전하나 후순위 |

**결론: read 이득은 Batch 1 + Batch 3 파생 상태 + last_date 인덱스로 재싱크 없이 확보.**
파생 상태는 기존 DB에서 로컬 백필되며 Outlook 전체 재수집은 필요 없다.

## 6. 남은 것 (후속, 실 스케일 도달 시)

- sync COM SetColumns/SMTP 캐시 — 적용 전 성능·결과 불변성 벤치마크.
- 무거운 GET(/통계)도 필요하면 백그라운드+폴링(AI검색 패턴 재사용).
- keyset 페이지네이션 · `ANALYZE` — 스케일 도달 후.
