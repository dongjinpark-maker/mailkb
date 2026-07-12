# PROPOSAL: 인라인 이미지 표시와 본문 수명주기 — 설계안 v1 (인터뷰 확정)

> 상태: **1단계 구현 완료 (2026-07-13)** — 스키마(message_html 분리·
> auto_vacuum=INCREMENTAL)·주입(정제 후 cid 임베드·중복 생략)·컷오프 게이트·
> 프룬(하루 1회·마커·incremental_vacuum)·렌더(마커+텍스트·빈 본문 가드)·
> 설정 노브·fake 픽스처, WSL 검증(테스트 248건·demo 시각 확인).
> **2단계(outlook_com cid 추출 — PC 스모크 필요) 대기.**
> 목표: 인라인 이미지를 최근 N일(기본 60) 동안 Minerva 에서 바로 보고,
> 그 이후는 본문을 텍스트 수준으로 압축해 **저장 용량을 늘리지 않으면서**
> 장기 검색·AI QnA·Outlook 원문 연결은 그대로 유지한다.

## 0. 진단 — 왜 지금 이미지가 안 보이나

정제기(`sanitize_html`)는 `data:image/*` 를 이미 통과시키고 CSP(`img-src 'self'
data:`)도 허용한다. 그런데도 안 보이는 이유: **Outlook 인라인 이미지의 대부분은
base64 가 아니라 `cid:` 참조**다 — HTML 에는 `<img src="cid:image001@…">` 만 있고
실제 바이트는 첨부 파트(`item.Attachments`)에 있다. 현재 정제기는 cid: 를 원격
이미지로 취급해 차단 마크(`data-blocked-src`)로 바꾼다.
→ 핵심 작업 = **sync 때 cid 첨부에서 바이트를 꺼내 임베드**하는 것.

## 1. 확정 결정 (인터뷰, 2026-07-12)

| # | 질문 | 결정 |
|---|---|---|
| 1 | 이미지 저장 방식 | **A. DB 임베드** — sync 때 cid→base64 로 body_html 에 인라인. 열람 지연 0·오프라인·백업 단순(파일 1개). (B 파일 저장·C 열람 시 Outlook 실시간은 기각 — C 는 단일 스레드 서버 블로킹이 치명적) |
| 2 | 보존 기간 후 본문 | **텍스트만 (진짜 TUI)** — N일 경과 시 body_html 폐기, `new_content` 텍스트로 표시. 서식·표까지 회수해 DB 최소화. 검색(FTS)·AI QnA 재료는 원래 new_content 라 **무손실** |
| 3 | 이미지 용량 상한 | **무제한** — N일이면 어차피 삭제. (특정 달 대용량 사진 메일이 몰리면 DB 가 일시적으로 수백 MB 까지 갈 수 있음 — 인지된 선택. 상한이 필요해지면 `image_max_kb` 노브 추가) |
| 4 | 제거 자리 표시 | **자리마다 안내** — 단, 결정 2(폐기)와의 충돌은 아래 '마커' 방식으로 해소 |

## 2. 본문 수명주기 (메시지 단위)

```
ingest (sync)                          N일 경과 (sync 가 매회 판정)
──────────────                         ─────────────────────────────
HTMLBody 의 cid: 참조를                 body_html := 초경량 마커 또는 ""
Attachments 에서 찾아 base64 임베드      ├─ 이미지 있었음 → "🖼 이미지 N장 —
→ sanitize → body_html 저장             │   보존 기간 경과, Outlook에서 확인" 한 줄
(원격 http 이미지는 지금처럼 차단)        └─ 이미지 없었음 → "" (완전 폐기)
                                       표시는 new_content 텍스트 + (마커 배너)
                                       strip 후 VACUUM 으로 공간 즉시 회수
```

- **검색·AI 무영향**: FTS 는 `subject+new_content`, 요약·수확·분류도 new_content
  기반 — body_html 폐기가 어떤 지식 기능에도 영향 없음.
- **Outlook 연결 유지**: 기존 'Outlook 열기'(EntryID→Message-ID 폴백) 그대로.

## 3. 상세 설계

### 3-A. ingest 임베드 — 치환은 정제 '후' (2026-07-12 리스크 검토 반영)
- **COM(outlook_com)은 바이트만 동봉**: Attachments 에서 Content-ID
  (PR_ATTACH_CONTENT_ID, PropertyAccessor) 매칭 → `SaveAsFile`(임시) →
  `MailRecord.inline_images = {cid: (mime, bytes)}`. HTML 치환은 하지 않는다.
- **컷오프 게이트 (2026-07-12, 5개월 --full 시뮬레이션 반영)**: fetch 에
  `image_cutoff`(오늘−retain일)를 전달, **컷오프 이전 메일은 추출 자체를 스킵**
  — 대량 백필에서 곧 폐기될 이미지에 COM 수 분·DB 일시 팽창을 쓰지 않는다.
- **store 가 sanitize(인용 절단) 후 주입**: 절단에서 살아남은 cid 참조
  (`data-blocked-src="cid:…"`)에만 base64 주입 — 재인용 체인 속 이미지가
  중복 임베드되지 않고, 정제기가 수 MB base64 HTML 을 파싱하지도 않는다.
  **메일 내 동일 payload 는 1회만** 임베드(중복은 안내 마커).
- 실패(Content-ID 없음·winmail.dat·SaveAsFile 오류)는 전부 현행 차단 마크로
  **graceful 폴백** + sync 출력에 `이미지 임베드 n · 실패 m` 카운터 — PC 검증이
  어려운 상황의 방어선. 첫 `--full` 백필은 이미지 수백 장 저장으로 수 분 추가 가능.
- 텍스트 경로 안전(실측): `html_to_markdown` 은 `<img>` 를 통째로 무시 —
  base64 가 new_content/FTS/AI 프롬프트로 유입될 경로가 구조적으로 없음.
  표는 마크다운 표로 변환되어 60일 후 텍스트 뷰에서도 의미 보존(실측 확인).
- fake.py: 합성 PNG(코드 생성 색상 박스, 수백 바이트)를 data: 로 인라인한
  픽스처 2~3통 — 최근 구간 1통 + 보존 기간 경과 구간 1통(마커 시연).

### 3-B. 정제기 (clean.py) — 변화 최소
- `data:image/*` 통과는 이미 구현됨. cid 치환이 ingest 에서 끝나므로 정제기
  수정은 사실상 없음(치환 실패 cid: 의 차단 마크 동작 확인만).

### 3-B-2. 저장 위치 — 별도 테이블 분리 (1GB+ 대비, 2026-07-12 추가)
- SQLite 는 1GB+ 에서도 문제없지만(한계 281TB), **큰 blob 뒤의 컬럼을 읽으면
  오버플로 페이지를 건너 읽는 비용**이 있다. 현재 `body_html` 뒤에 `read_at` 이
  있고 목록·카운트가 매 렌더 `read_at` 전수 스캔 → 이미지 임베드 시 목록이
  느려질 수 있음.
- 해결: 임베드 본문을 **`message_html(message_id PK, html TEXT)` 별도 테이블**로
  분리, 스레드 열람 때만 조인. 목록·카운트·통계 스캔은 이미지 크기와 무관해짐.
- **clean start (2026-07-12 확정)**: 마이그레이션 기능 불요 — 새 스키마로 바로
  가고, 기존 DB(PC·demo)는 삭제 후 `sync --full` 재수집. messages.body_html
  컬럼은 스키마에서 제거(본문 HTML 의 유일 저장소 = message_html).

### 3-C. 보존 기간 strip (store.py, sync 마다)
- **트리거: 모든 sync(수동·웹 자동 동기화) 종료 시 자동** — `sync_state.
  last_image_prune`(날짜) 마커로 하루 1회 가드. 건너뛴 날은 다음 sync 가
  경과일 기준으로 한 번에 처리(누락 없음). 별도 명령·스케줄 불요.
- 대상: `sent_on < 오늘−N일 AND html != ''` (message_html).
  - `data:image` 포함 → 개수 세고 body_html := `<div class='imgnote'>🖼 이미지
    {n}장 — 보존 기간({N}일) 경과, 원본은 Outlook에서</div>` (수백 바이트 잔존).
  - 미포함 → body_html := "" (완전 폐기 — 서식 HTML 회수).
- 공간 회수: **풀 VACUUM 금지** — 단일 스레드 서버가 수십 초 정지(웹 자동
  동기화 중 치명적). clean start 이점으로 **DB 생성 시 `PRAGMA
  auto_vacuum=INCREMENTAL`**(생성 시에만 설정 가능) + strip 후
  `incremental_vacuum` 조각 회수.
- **설정 관리**: `[web] image_retain_days = 60` (cfg.opt — config.py 무수정).
  주 변경 경로 = **웹 설정 '표시 · 동기화'에 숫자 입력 추가**(기존 _SETTINGS_INTS
  패턴) → overrides.json 영구 + 즉시 재로드. toml 직접 편집은 서버 재시작 필요.
  demo 는 14 로 설정해 두 상태를 한 화면에서 시연.
- **값 변경의 비대칭**: 줄이면 다음 strip(최대 하루 지연)에서 일괄 정리.
  **늘려도 이미 strip 된 과거분은 자동 복구 안 됨** — 복구 경로는 `sync --full`
  재수집(Outlook 에서 재임베드). 0 = 기능 끔(임베드 안 함), 큰 값 = 사실상 영구.

### 3-D. 표시 (web.py render_thread)
- body_html 이 **마커만** 남은 메일: 마커 배너를 위에, `new_content` 텍스트를
  아래에 함께 렌더 (현행 "html 있으면 html만" 분기에 마커 예외 추가).
- body_html == "": 현행 텍스트 폴백 그대로 (마크다운 토글 포함).
- **빈 본문 가드**: strip 후 new_content 도 사실상 빈 메일(이미지-전용 —
  스크린샷/조직도 공유)은 "본문 없음 — Outlook에서 확인" 안내로 렌더.
  한계 명시: 이미지-전용 메일의 지식은 지금도 검색·AI 인덱스 밖(OCR 없음) —
  장기 검색·QnA 약속은 텍스트가 있는 메일에 한정.
- 용량 안내: 설정 페이지 '정보' 옆에 "이미지 보존 N일 · 현재 임베드 용량 X MB"
  한 줄(선택 — 구현 시 판단).

### 3-E. 배포 (clean start) · 대량 백필 수칙
- PC: 기존 db.sqlite 삭제 → `sync --full` 재수집 (새 스키마 + 최근 N일 이미지
  임베드가 한 번에). vault·config·overrides 는 유지.
- **--full 은 serve 를 내리고 실행** (웹 autosync 와 쓰기 경합 방지).
- 기존 DB 에 --full 재실행은 이미지 소급이 안 됨(message_id 중복 스킵) —
  이미지 백필은 반드시 clean start 로.
- 수개월 백필 시 Outlook **캐시 모드 기간**(OST 보관 창)이 대상 기간을 덮는지
  사전 확인 — 캐시 밖 메일은 통당 온라인 왕복으로 급감속하거나 누락된다.

## 4. 용량 추정 (상한 무제한 기준)

하루 이미지 메일 ~10통 × 평균 원본 300KB × base64(+33%) ≈ 4MB/일 →
**60일 롤링 ≈ 240MB 상변** (통상 수십 MB). 텍스트 폐기분 회수(−)와 상쇄되어
장기 순증가는 없음. 폭증 시 대응: `image_retain_days` 축소 또는 상한 노브 추가.

## 5. 구현 순서 (착수 시)

1. ~~fake 픽스처 + strip/마커/렌더~~ ✅ 완료 (2026-07-13)
2. outlook_com cid 추출 (PC 검증 필요 — PropertyAccessor 스모크) ← 다음
3. demo 재생성 + 스크린샷 갱신(README 이미지에 인라인 이미지 보이면 홍보 효과)
