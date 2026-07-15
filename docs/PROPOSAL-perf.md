# PROPOSAL: 성능 개선 (perf) — 스케일·런타임 지연

> 상태: **Batch 1·2 구현 완료(2026-07-15)**. 원칙(사용자 확정): **기존 동작 결과는
> 100% 동일, 속도만 개선**. 결과가 달라지거나 DB 재싱크가 필요한 것은 이 배치에서 제외.

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

## 2-b. 목록 신호 캐시 — 매 렌더 전수 스캔 제거 (구현됨, 결과 불변)

`/mail`·`/threads` 렌더가 매번 수신 전수를 훑던 3개 신호를 (설정·데이터) 지문으로
게이트해 캐시. **측정(30k통): render_mail 364→31ms, render_threads 345→19ms.**
- **noise**(스레드 단위 + 메시지 단위 한 스캔): 키 = (db·`ignore/blocked/internal/subject_strong`
  해시·`MAX(rowid)`). 설정 변경(차단·규칙)·새 수집일 때만 재계산. 값 자체를 해시하므로
  경로(웹 차단·파일 편집·git pull) 무관하게 감지 → **staleness 없음**(라이브와 동일).
  `_noise_sets`/`_noise_thread_ids`; render_mail 은 메시지 단위 세트로 `cfg.is_noise`
  재계산 제거(74→~17ms).
- **응답대기·기한/요청**(`_signal_sets`): 지문 = (db·my_addr·`MAX(rowid)`). 기한은 수신
  본문 전수+정규식(측정 **317ms**)이라 이득이 가장 큼. **hidden 필터는 캐시에서 빼고
  호출부 쿼리가 라이브 제외** → 숨김/해제가 캐시 무효화 없이 즉시 반영(신호는 메시지
  데이터의 순수 함수). `test_signal_cache_hide_unhide_correct` 가 이 경계를 가드.
- 스레드 안전: 백그라운드 잡(수집)은 캐시를 만지지 않고 `MAX(rowid)`만 올림 → 다음
  렌더가 감지·재계산. 락은 dict 갱신 방어용. 잔여 비용: 새 수집 직후 **1회 렌더**만
  재계산(그 외 렌더는 히트). 테스트 4종(설정·데이터 무효화·히트·hide/unhide).

## 3. Batch 2 — sync 백그라운드화 (구현됨)

`/sync`(수동)·`/autosync`(주기)가 서빙 스레드에서 COM 수집을 돌려 **UI 전체를
프리즈**시키던 것을 전용 스레드로 이관(리뷰·AI검색과 동일 패턴). 수집 **결과는 동일**.

- `_do_sync(store, cfg)` — 수집+프룬 순수 동작(잡·테스트 공용, 수집 실패에도 프룬 보장).
- `_run_sync_job` — 스레드에서 `CoInitialize`(COM 은 스레드마다 필요) 후 `_do_sync`.
- `_start_sync` — 단일 슬롯 가드. `/sync`→대기화면(`/sync/status`)+`hookSyncPolling`,
  `/autosync`→논블로킹(`started`/`busy`)+`watchSyncToast` 가 완료 시 '신규 N' 토스트.
- 완료 msg 는 `data-sync-msg` 로 실어 수동/자동 모두 토스트 보존.

## 4. 적용 제외 (이번 배치) — 이유

| 항목 | 제외 이유 |
|---|---|
| 전체 연결 재사용(요청간 1 커넥션) | keep-alive 로 안전한 절반 확보. 나머지는 트랜잭션 상태 관리 위험 → 결과 불변 확신 부족 |
| `new_content` 별도 테이블 분리 | 스키마 변경 = **DB 재싱크 필요**('git pull 즉시 반영' 원칙 위배) |
| 구 메일 HTMLBody 스킵 / 헤더 생략 | 저장 본문(검색 텍스트) 서식이 달라짐 = **결과 변경** |
| Outlook `Items.SetColumns` | 결과 불변이나 Windows-COM 전용이라 WSL 에서 검증 불가 → 실기 검증 후 |
| keyset 페이지네이션 | 결과 동일·안전하나 깊은 스크롤에만 이득 → 후순위 |
| `ANALYZE` 주기 실행 | 안전. 스케일 도달 후 계획 품질용으로 후속 |

## 5. 남은 것 (후속, 실 스케일 도달 시)

- 멀티 GB 실측 후: `new_content` 분리(재싱크 1회 감수) · SetColumns(실기) · keyset 페이지네이션.
- 무거운 GET(/통계)도 필요하면 백그라운드+폴링(AI검색 패턴 재사용).
