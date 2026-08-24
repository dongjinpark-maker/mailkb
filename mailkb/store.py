"""SQLite 저장소 — 인덱스 계층 + 사람의 산출물.

메일 원본은 Outlook(hot)에 있고, 여기의 메타·new_content·FTS·롤링 요약과 모든
파생 테이블은 지워도 sync 로 다시 만들어진다.

다만 이 파일에는 **재수집으로 복구되지 않는 것**도 함께 들어 있다 —
knowledge_candidates(암묵지 후보) · ask_cache(분석 이력) · people_dossier(인물 요약) ·
action_overrides(신호 해제) · threads.flagged/hidden(플래그·숨김).
그래서 db.sqlite 삭제는 백업 없이 되돌릴 수 없다. 연 200~300MB, 백업은 파일 복사 한 번.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import search as search_mod
from . import terms as terms_mod
from .clean import (CLEAN_VERSION, PRESERVED_MARK, extract_new_content,
                    inject_inline_images, normalize_subject, sanitize_html,
                    strip_preserved)
from .features import FEATURE_VERSION, classify_message
from .sources.base import MailRecord

# 인물 도시에 AI 캐시의 근거 검증 규약. 저장된 버전이 이 값과 다르면 웹에서
# 표시하지 않고 다음 AI 실행 때 새 검증기로 점진 재생성한다.
DOSSIER_VALIDATOR_VERSION = 3      # 3: 슬롯 계약(한 줄·맡은 일·요즘·방식, 2026-08-18)

# 재절단 백업 보존일 — 절단 규칙 오탐을 되돌릴 수 있는 창(그 뒤 프룬이 지운다)
RECLEAN_BACKUP_DAYS = 45


# 재절단이 이만큼 줄였을 때만 그 스레드의 롤링 요약을 다시 만들게 한다.
# 서명·꼬리 몇 줄은 요약 내용을 바꾸지 않는다 — 인용 체인이 빠진 경우만 의미 있다.
_RESUMMARIZE_MIN_CUT = 300

# 파생 테이블 DDL 은 _SCHEMA 와 버전 마이그레이션(drop+재생성)이 공유한다.
# 파생 테이블은 messages 에서 결과 불변으로 재구축 가능하므로 ALTER 대신
# drop+재생성 — SQLite 의 ADD COLUMN IF NOT EXISTS 부재를 우회한다.
_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS message_features (
    message_id          INTEGER PRIMARY KEY,
    has_deadline        INTEGER NOT NULL DEFAULT 0,
    has_decision        INTEGER NOT NULL DEFAULT 0,
    has_request         INTEGER NOT NULL DEFAULT 0,
    has_strong_request  INTEGER NOT NULL DEFAULT 0,
    has_weak_request    INTEGER NOT NULL DEFAULT 0,
    has_question        INTEGER NOT NULL DEFAULT 0,
    has_completion      INTEGER NOT NULL DEFAULT 0,
    has_withdrawal      INTEGER NOT NULL DEFAULT 0,
    mentions_me         INTEGER NOT NULL DEFAULT 0,
    mentions_group      INTEGER NOT NULL DEFAULT 0,
    is_trivial          INTEGER NOT NULL DEFAULT 0,
    subject_has_request INTEGER NOT NULL DEFAULT 0,
    addressed_to_me     INTEGER NOT NULL DEFAULT 0
);
"""

_THREAD_STATE_DDL = """
CREATE TABLE IF NOT EXISTS thread_state (
    thread_id              INTEGER PRIMARY KEY,
    first_message_id       INTEGER NOT NULL,
    first_sent_on          TEXT NOT NULL DEFAULT '',
    latest_message_id      INTEGER NOT NULL,
    latest_sent_on         TEXT NOT NULL DEFAULT '',
    message_count          INTEGER NOT NULL DEFAULT 0,
    sent_count             INTEGER NOT NULL DEFAULT 0,
    received_count         INTEGER NOT NULL DEFAULT 0,
    unread_received_count  INTEGER NOT NULL DEFAULT 0,
    addressed_to_me_count  INTEGER NOT NULL DEFAULT 0,
    deadline_count         INTEGER NOT NULL DEFAULT 0,
    -- 액션 상태기계 (docs/ARCHITECTURE.md §6.2): 열린 요청 슬롯은 스레드당 1개.
    -- 내 실질 회신(is_trivial 아님)·명시적 철회만 닫는다. 상대의 완료 통보는
    -- 닫지 않고 completion_after_action 표시만(잘못 닫힘 = 조용히 놓친 공).
    action_source_id        INTEGER NOT NULL DEFAULT 0,   -- 0 = 열린 액션 없음
    action_strength         TEXT NOT NULL DEFAULT '',     -- 'strong' | 'weak'
    action_kind             TEXT NOT NULL DEFAULT '',     -- 'decide' | 'respond'
    action_has_deadline     INTEGER NOT NULL DEFAULT 0,
    completion_after_action INTEGER NOT NULL DEFAULT 0
);
"""

_TERM_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS message_term_features (
    message_id   INTEGER PRIMARY KEY,
    feature_json BLOB NOT NULL DEFAULT X''
);
CREATE TABLE IF NOT EXISTS message_term_bags (
    message_id       INTEGER PRIMARY KEY,
    body_bag_json    BLOB NOT NULL DEFAULT X'',
    subject_bag_json BLOB NOT NULL DEFAULT X''
);
CREATE TABLE IF NOT EXISTS message_term_subject_delta (
    message_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,       -- 'term' | 'phrase'
    term       TEXT NOT NULL,
    PRIMARY KEY (message_id, kind, term)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS person_term_window (
    sender_addr TEXT NOT NULL,
    term        TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- 'term' | 'phrase'
    mail_df     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, term, sender_addr)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_messages_word_thread
    ON messages(is_sent, sender_addr, thread_id, sent_on, id);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    message_id   TEXT NOT NULL UNIQUE,
    entry_id     TEXT DEFAULT '',
    thread_id    INTEGER NOT NULL,
    subject      TEXT DEFAULT '',
    sender_name  TEXT DEFAULT '',
    sender_addr  TEXT DEFAULT '',
    to_addrs     TEXT DEFAULT '',      -- ';' 연결
    cc_addrs     TEXT DEFAULT '',
    sent_on      TEXT DEFAULT '',      -- ISO8601
    is_sent      INTEGER DEFAULT 0,    -- 내가 보낸 메일
    attach_names TEXT DEFAULT '',      -- 파일명만; 내용은 Outlook 에서 O(1) 조회
    new_content  TEXT DEFAULT '',      -- 인용 제거된 신규 텍스트
    read_at      TEXT DEFAULT '',      -- 웹에서 스레드 열람 시각 (빈값=미읽음)
    gone_at      TEXT,                 -- Outlook 에서 못 찾은 시각 (NULL=정상)
    raw_chars    INTEGER DEFAULT 0,    -- 절감 측정용 원본 길이
    folder       TEXT DEFAULT '',
    -- **적재 순서**(1부터). id 가 날짜 기반이 된 뒤(next_id) id 는 발신 시각순이라
    -- '먼저 넣은 것'을 더는 못 말한다. 그런데 mid-join 인용 보존은 "이 스레드에서
    -- 내가 **먼저 보유한** 메일"이 기준이다 — 시각으로 잡으면 나중에 백필된 더
    -- 오래된 메일이 first 로 뽑혀 진짜 첫 보유분의 유일한 인용 체인이 잘린다
    -- (2026-07-31 리뷰가 잡았던 그 버그). 신원과 도착 순서를 갈라 둔다.
    ingest_seq   INTEGER
);
-- idx_messages_ingest_seq 는 여기 두지 않는다 — ingest_seq 는 뒤늦게 생긴 컬럼이라
-- 구 DB 에는 없고, 그 위의 CREATE INDEX 가 executescript 전체를 죽인다
-- ("no such column: ingest_seq" — 2026-08-13 이전 DB 는 아예 안 열렸다).
-- _ensure_late_columns() 가 컬럼을 붙인 **뒤** _ensure_late_indexes() 가 만든다.
-- 표시용 HTML(이미지 임베드 포함)은 별도 테이블 — 큰 blob 이 messages 행에
-- 끼면 목록·카운트 전수 스캔이 오버플로 페이지를 건너 읽어 느려진다
-- (docs/ARCHITECTURE.md §6.1). 스레드 열람 때만 조인.
CREATE TABLE IF NOT EXISTS message_html (
    message_id INTEGER PRIMARY KEY,   -- messages.id
    html       TEXT DEFAULT ''        -- 정제·이미지 임베드된 HTML (프룬 대상)
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_sent_on ON messages(sent_on);
CREATE INDEX IF NOT EXISTS idx_messages_thread_date
    ON messages(thread_id, sent_on DESC, id DESC);
-- 인물 업무 어휘 지도: 6개월 창의 특정 발신자 본문만 읽는다. LOWER() 없이
-- 저장 단계에서 정규화한 sender_addr를 그대로 조회해 대형 DB에서도 범위 탐색.
CREATE INDEX IF NOT EXISTS idx_messages_sender_date
    ON messages(is_sent, sender_addr, sent_on DESC, id DESC);

CREATE TABLE IF NOT EXISTS threads (
    id                INTEGER PRIMARY KEY,
    norm_subject      TEXT DEFAULT '',
    conversation_key  TEXT DEFAULT '',
    first_date        TEXT DEFAULT '',
    last_date         TEXT DEFAULT '',
    status            TEXT DEFAULT 'open',   -- 레거시(구 추적제외 dismissed) — 항상 open
    flagged           INTEGER DEFAULT 0,     -- 수동 플래그(중요 표시)
    hidden            INTEGER DEFAULT 0,     -- 숨김: 추적·메일함·스레드 기본목록에서 제외
    rolling_summary   TEXT DEFAULT '',
    summary_msg_count INTEGER DEFAULT 0,     -- 요약에 반영된 메시지 수 (증분 갱신용)
    summary_updated   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_threads_norm ON threads(norm_subject);
CREATE INDEX IF NOT EXISTS idx_threads_conv ON threads(conversation_key);
-- 스레드 목록 ORDER BY last_date DESC LIMIT — 인덱스 없으면 전수 스캔+임시정렬.
-- 스레드 수에 비례해 커지는 유일한 부분이라 스케일 보험(30k 스레드 2.06→0.03ms).
CREATE INDEX IF NOT EXISTS idx_threads_last_date ON threads(last_date);

CREATE TABLE IF NOT EXISTS people (
    addr        TEXT PRIMARY KEY,
    name        TEXT DEFAULT '',
    from_count  INTEGER DEFAULT 0,   -- 이 사람이 나에게
    to_count    INTEGER DEFAULT 0,   -- 내가 이 사람에게
    first_seen  TEXT DEFAULT '',
    last_seen   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 신호 수동 해제 오버레이 (구 UI 의 칩 ✕ 기록. 해제 UI 는 2026-07-30 제거,
-- 남은 기록은 주간 보고가 계속 존중한다) — 파생 테이블이 아니라
-- 백필(drop+재생성)·재접기에 살아남는다. source_id(해제 당시의 요청 메시지)에
-- 키가 걸려 있어 같은 스레드에 새 요청이 오면(action_source_id 변경) 자동으로
-- 무시된다 = 신호 자동 복귀. 숨김(스레드 전체)과 달리 이 요청 건만 끈다.
CREATE TABLE IF NOT EXISTS action_overrides (
    thread_id        INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    dismiss_action   INTEGER NOT NULL DEFAULT 0,   -- 회신 필요·확인 후보 해제(⏰ 포함)
    dismiss_deadline INTEGER NOT NULL DEFAULT 0    -- ⏰ 만 해제
);


-- 인용 재절단 백업 — 절단 규칙 승격(_reclean_quotes)이 덮어쓰기 전의
-- new_content 원본. sync 는 이미 있는 message_id 를 건너뛰므로 sync --full
-- 로도 본문이 복원되지 않는다(실측) → 규칙 오탐 시 이 표가 유일한 되돌리기.
-- 복구(순서대로):
--   UPDATE messages SET new_content=(SELECT old_content FROM reclean_backup
--     WHERE message_id=messages.id) WHERE id IN (SELECT message_id FROM reclean_backup);
--   INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
--   DELETE FROM sync_state WHERE key IN ('clean_version','feature_version',
--     'term_feature_version');            -- 없으면 신호·어휘가 잘린 본문 기준으로 굳는다
--   DELETE FROM people_word_profiles;      -- 어휘 지도 캐시는 본문을 키에 안 넣는다
-- 그 뒤 다음 열기가 재절단(새 규칙)·재분류를 다시 수행한다.
-- 보존 기간: RECLEAN_BACKUP_DAYS 일 (sync 의 프룬 훅이 지운다). 3만통 실측 35.8MB 라
-- 무한 보존은 DB 를 부풀린다 — 오탐은 며칠 안에 드러나므로 창을 둔다.
-- 리포트에서 "처리함"으로 접은 항목. 대개 스레드가 아니라 **그 항목 하나**를
-- 가리킨다(한 스레드에 약속이 여러 개 생길 수 있고, 하나를 지켰다고 나머지가
-- 사라지면 안 된다). 예외는 'stalled' 로, 정체는 스레드 단위 사실이라 스레드로 건다.
-- kind: 'promise'(내 약속) | 'stalled'(정체·막힘) | 'deadline'(기한)
--
-- **알려진 한계**: 저장된 key_hash 는 안 움직이지만 화면에서 **다시 계산하는 쪽**
-- 키는 움직일 수 있다. promise·deadline 은 본문 문장으로 키를 만들므로
-- CLEAN_VERSION 이 올라 new_content 가 재절단되면(그 문장이 절단 경계에 걸릴 때)
-- 키가 달라져 접은 항목이 되살아난다. stalled 는 스레드 번호뿐이라 영향이 없다.
CREATE TABLE IF NOT EXISTS report_done (
    kind       TEXT NOT NULL,
    key_hash   TEXT NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    label      TEXT DEFAULT '',        -- 되돌리기 목록에 보여 줄 한 줄
    done_at    TEXT DEFAULT '',
    PRIMARY KEY (kind, key_hash)
);

CREATE TABLE IF NOT EXISTS reclean_backup (
    message_id  INTEGER PRIMARY KEY,
    old_content TEXT NOT NULL,
    created     TEXT DEFAULT '',
    from_version INTEGER DEFAULT 0     -- 이 값 직전의 CLEAN_VERSION. 앞선 백업이
                                       -- 만료된 뒤 다시 담긴 행은 '최초 원본'이
                                       -- 아니다 — 복구 전에 이 값을 확인한다.
);

-- 인물·프로젝트 신호 (데일리 수확 → Phase 2 주간 증류가 소화)
CREATE TABLE IF NOT EXISTS distill_signals (
    id        INTEGER PRIMARY KEY,
    date      TEXT NOT NULL,          -- 수확한 데일리 날짜
    kind      TEXT NOT NULL,          -- person | project
    who       TEXT DEFAULT '',        -- person: 이름/주소
    thread_id INTEGER,
    signal    TEXT DEFAULT '',        -- 신호 한 줄
    quote     TEXT DEFAULT '',
    consumed  INTEGER DEFAULT 0,      -- 주간 증류가 소화하면 1
    created   TEXT DEFAULT ''
);

-- 인물 도시에 AI 요약 캐시 (v2) — addr당 1행. 결정론 카드 위에 얹는 AI 카드의 원천.
-- 파생 테이블 아님(AI 산출물이라 재구축 불가) → 백필/버전 변경에도 살아남는다.
-- basis_msg_count = 마지막 생성/검증 시점의 그 사람 관련 메시지 수. 증분 갱신 가드 —
-- 이 값보다 메시지가 늘어난 사람만 재생성해 비용을 통제한다(검증 0건 재호출도 방지).
CREATE TABLE IF NOT EXISTS people_dossier (
    addr            TEXT PRIMARY KEY,
    dossier_md      TEXT DEFAULT '',    -- 근거 검증 통과한 요약(마크다운)
    updated         TEXT DEFAULT '',
    basis_msg_count INTEGER DEFAULT 0,
    validator_version INTEGER DEFAULT 1 -- 인용 출처 검증 규약 버전
);

-- 인물 업무 어휘 지도 최종 파생 캐시. 원문은 복제하지 않고 표시용 점수·근거
-- ID만 저장한다. 실제 26주 대조 메일 집합이나 규칙이 바뀔 때만 갱신한다.
CREATE TABLE IF NOT EXISTS people_word_profiles (
    addr             TEXT PRIMARY KEY,
    profile_json     TEXT DEFAULT '',
    basis_message_id INTEGER DEFAULT 0,
    window_end       TEXT DEFAULT '',
    window_weeks     INTEGER DEFAULT 26,
    feature_version  TEXT DEFAULT '',
    updated          TEXT DEFAULT ''
);

-- AI 검색 결과 캐시 (Phase 2) — 질의별 지속 저장. 뒤로가기·반복 질의 재과금 방지 +
-- '최근 AI 검색' 목록. q = 정규화된 자연어 질의(소문자·공백 정리).
CREATE TABLE IF NOT EXISTS ai_search (
    q           TEXT PRIMARY KEY,     -- 정규화 질의(캐시 키)
    raw_q       TEXT DEFAULT '',      -- 원문 질의(표시용)
    dsl         TEXT DEFAULT '',      -- AI 가 해석한 DSL(투명성·편집용)
    result_json TEXT DEFAULT '',      -- 렌더용 최종 결과(순위·이유·id)
    backend     TEXT DEFAULT '',      -- 사용 모델
    created     TEXT DEFAULT ''
);

-- 질문하기(ask) 캐시 — 키에 기준선(MAX rowid)을 넣어 새 메일이 오면 자연 무효화.
-- ai_search 와 분리: 검색 이력 목록(ai_search_recent)에 질문이 섞이지 않게.
CREATE TABLE IF NOT EXISTS ask_cache (
    key         TEXT PRIMARY KEY,     -- 정규화 질문 + 기준선
    question    TEXT DEFAULT '',      -- 원문 질문(표시용)
    result_json TEXT DEFAULT '',      -- 렌더용 최종 결과(답변·근거·상태)
    backend     TEXT DEFAULT '',
    created     TEXT DEFAULT ''
);

-- 스레드 노트 색인(2026-08-11) — 원본은 vault/notes/*.md (사람이 외부 편집기로
-- 수정). 여기는 검색·AI 문맥용 미러라 지워도 notes.reindex 가 복구한다.
-- content 는 frontmatter·기계 절을 뗀 사람 본문(notes.note_body).
CREATE TABLE IF NOT EXISTS notes (
    thread_id INTEGER PRIMARY KEY,
    path      TEXT NOT NULL,
    mtime     REAL NOT NULL,
    content   TEXT NOT NULL DEFAULT ''
);

-- 암묵지 후보(2026-08-14) — 수확이 캐낸 조직 노하우. 사람이 회고 화면에서
-- [지식으로 저장]을 눌러야 md 가 생긴다(승인 전에는 파일이 없다 — vault 가
-- 초안으로 어지러워지지 않는다).
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id        INTEGER PRIMARY KEY,
    date      TEXT NOT NULL,          -- 수확한 회고 날짜
    source    TEXT DEFAULT 'daily',   -- daily | weekly
    title     TEXT NOT NULL,
    body      TEXT DEFAULT '',
    threads   TEXT DEFAULT '',        -- ';' 연결 스레드 번호
    quote     TEXT DEFAULT '',        -- 검증 통과한 원문 인용
    status    TEXT DEFAULT 'pending', -- pending | saved | dismissed
    path      TEXT DEFAULT '',        -- 저장 후 md 경로
    created   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_knowledge_cand_status
    ON knowledge_candidates(status, date);

-- 지식 색인(2026-08-14) — 원본은 vault/knowledge/*.md. notes 와 같은 계약:
-- 미러라 지워도 knowledge.reindex 가 복구한다. 향후 지식 관리 메뉴가 목록을
-- 그릴 때 필요한 것(title·path·mtime·content·threads)을 처음부터 담는다.
CREATE TABLE IF NOT EXISTS knowledge (
    path      TEXT PRIMARY KEY,       -- vault 상대 아님, 절대 경로(notes 관례)
    title     TEXT NOT NULL DEFAULT '',
    threads   TEXT DEFAULT '',        -- ';' 연결 스레드 번호(frontmatter)
    mtime     REAL NOT NULL,
    content   TEXT NOT NULL DEFAULT ''
);
"""

_FTS_TRIGRAM = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, new_content, content='messages', content_rowid='id',
    tokenize='trigram'
);
"""
# trigram 미지원(구버전 SQLite) 시 폴백 — 한글 부분일치 품질은 낮음
_FTS_FALLBACK = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, new_content, content='messages', content_rowid='id',
    tokenize='unicode61'
);
"""

_FTS_SYNC = """
INSERT INTO messages_fts(rowid, subject, new_content)
VALUES (?, ?, ?)
"""


# ── 메일·스레드 번호 = 날짜 + 그날 순번 (2026-08-11) ────────────────────
# 왜: 종전 rowid 는 **전체 코퍼스의 삽입 순서**라, 수집 시작 날짜를 바꿔 다시
# 받으면 앞이 잘린 만큼 뒤가 전부 앞당겨진다. 그러면 vault 에 박아 둔 참조
# (`weekly.py` 의 "[메일 #174 · 스레드 #96]")가 **다른 메일을 가리킨다** — 오류가
# 아니라 오답이라 눈치채지 못한다. 번호를 하루 안에 가두면 수집 범위와 무관해진다
# (데모 282통 실측: 시작 날짜만 바꾼 재수집에서 rowid 보존 0%, 이 방식 100%).
#
# 정수라 SQLite rowid 요건을 만족한다 — 11개 FK 컬럼을 하나도 안 바꾼다.
# 표시는 web._no() 가 `260726-018` 로 옮긴다.
DAY_SPAN = 100000            # 하루 한 칸. 넘으면 다음 날과 PK 충돌이다.
SLOT_SPAN = 1000             # 15분 슬롯 한 칸 — 슬롯당 999건
SLOTS_PER_DAY = 96           # 24시간 / 15분


def day_key(sent_on: str) -> int:
    """sent_on → YYMMDD 정수. 날짜를 모르면 0(= `000000` 버킷).

    Outlook 이 SentOn/ReceivedTime 을 안 주는 항목이 드물게 있다
    (`sources/outlook_com.py:654` — 없으면 빈 문자열). 그런 메일도 번호는 받아야
    저장이 된다 — id 는 PK 라 NULL 이 없다.
    """
    d = (sent_on or "")[:10].replace("-", "")
    return int(d[2:]) if len(d) == 8 and d.isdigit() else 0


def slot_key(sent_on: str) -> int:
    """sent_on → 그날의 15분 슬롯(0..95). 시각을 모르면 0.

    번호를 **시각으로** 잡는 이유: 종전에는 '그날 앞에 몇 통 있었나'가 순번이라
    앞의 메일 하나가 빠지면 뒤가 전부 밀렸다. DB 를 다시 만들면 vault 에 박아 둔
    참조가 어긋난다는 뜻이다. 시각으로 자리를 잡으면 번호가 **자기 데이터만으로**
    정해져 남이 지워져도 구멍만 남는다.
    (데모 282통 실측 — 10% 삭제 후 재구축 보존율 58% → 98%.)
    """
    t = (sent_on or "")[11:16]
    if len(t) == 5 and t[:2].isdigit() and t[3:].isdigit():
        return min((int(t[:2]) * 60 + int(t[3:])) // 15, SLOTS_PER_DAY - 1)
    return 0


def next_id(db, table: str, sent_on: str) -> int:
    """id = YYMMDD*DAY_SPAN + 15분슬롯*SLOT_SPAN + 슬롯 내 순번(1부터).

    슬롯이 차면 **그날 안에서** 다음 슬롯으로 흘린다. 예외를 던지면 `_insert` 를
    뚫고 나가 `ingest` 의 `_flush()`·워터마크 갱신을 건너뛰고, 다시 동기화해도
    같은 메일에서 또 죽어 사용자가 손쓸 수 없다("도구는 항상 산다").
    넘친 메일은 표시 슬롯이 실제 시각보다 뒤가 되는 것으로 값을 치른다.

    **다음 날로는 절대 넘기지 않는다** — 넘기면 다른 날 메일과 번호가 충돌한다.
    그날을 다 쓰면 그때 예외다(하루 95,904건, 종전 상한과 사실상 같다).

    보통 범위 조회 1회. 수집이 전역 시간순이라(`_fetch` 의 heapq.merge) 삽입은
    거의 오름차순 = B-tree 말단 추가가 유지된다.
    """
    day = day_key(sent_on) * DAY_SPAN
    for slot in range(slot_key(sent_on), SLOTS_PER_DAY):
        base = day + slot * SLOT_SPAN
        row = db.execute(f"SELECT MAX(id) FROM {table} WHERE id BETWEEN ? AND ?",
                         (base, base + SLOT_SPAN - 1)).fetchone()
        nxt = (row[0] + 1) if row and row[0] else base + 1
        if nxt < base + SLOT_SPAN:
            return nxt
    raise ValueError(
        f"{table}: 하루 {SLOTS_PER_DAY * (SLOT_SPAN - 1):,}건을 넘었습니다 "
        f"({(sent_on or '')[:10]})")


@dataclass
class SyncStats:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    new_threads: int = 0
    raw_chars: int = 0
    kept_chars: int = 0
    img_embedded: int = 0   # 인라인 이미지 임베드 수
    img_failed: int = 0     # cid 매칭 실패(차단 마크 잔존) — PC 관찰용


def image_cutoff_for(retain_days: int) -> str:
    """ingest 이미지 게이트용 컷오프(YYYY-MM-DD).

    retain_days <= 0 은 기능 끔 — 모든 메일이 컷오프 이전이 되는 sentinel 반환.
    """
    if retain_days <= 0:
        return "9999-12-31"
    return (datetime.now() - timedelta(days=retain_days)).date().isoformat()


# message_features 컬럼 (INSERT 공용) — 스키마 _FEATURES_DDL 와 순서 무관 이름 매칭.
_FEATURE_COLS = (
    "has_deadline", "has_decision", "has_request", "has_strong_request",
    "has_weak_request", "has_question", "has_completion", "has_withdrawal",
    "mentions_me", "mentions_group", "is_trivial", "subject_has_request",
    "addressed_to_me",
)

# 액션 상태 기본값 — '열린 요청 없음'.
_EMPTY_ACTION = {
    "action_source_id": 0, "action_strength": "", "action_kind": "",
    "action_has_deadline": 0, "completion_after_action": 0,
}
_ACTION_COLS = tuple(_EMPTY_ACTION)


def fold_action(state: dict, msg) -> dict:
    """스레드 액션 상태 전이 — 메시지 1통 적용. 증분(_update_thread_state)과
    재접기(_refold_thread_actions)·백필이 같은 함수를 써서 정의상 등가.

    전이 규칙 (docs/ARCHITECTURE.md §6.2):
      내 실질 발신       → 해소 (++수신인 추가·빈 본문 등 trivial 은 유지)
      수신 + 명시적 철회 → 해소 (같은 메일의 새 요청은 아래에서 다시 연다)
      수신 + 요청 증거   → 열기/갱신 — 최신 요청 메일이 source, 강도·기한은 열린
                           창 안에서 단조(강한 요청 뒤 약한 재촉이 격하시키지 않음)
      수신 + 완료 통보만 → 열려 있으면 completion_after_action=1 (해소 아님 —
                           잘못 닫힘은 조용히 놓친 공이라 '확인 후보' 강등까지만)
      그 외(FYI·일반)    → 유지
    """
    if msg["is_sent"]:
        if not msg["is_trivial"]:
            return dict(_EMPTY_ACTION)
        return state
    if msg["has_withdrawal"]:
        state = dict(_EMPTY_ACTION)
    # 이름 지목(mentions_me)도 약한 증거 — "김OO님, 자료 공유드립니다"는 훑어볼
    # 가치가 있다(확인 후보). 요청 신호 없이 지목만이면 L3 가 MAYBE 까지만 올린다.
    evidence = (msg["has_strong_request"] or msg["has_weak_request"]
                or msg["has_decision"] or msg["has_question"]
                or msg["has_deadline"] or msg["mentions_me"])
    if evidence:
        was_open = bool(state["action_source_id"])
        strong = bool(msg["has_strong_request"] or msg["has_decision"]
                      or (was_open and state["action_strength"] == "strong"))
        decide = bool(msg["has_decision"]
                      or (was_open and state["action_kind"] == "decide"))
        return {
            "action_source_id": msg["id"],
            "action_strength": "strong" if strong else "weak",
            "action_kind": "decide" if decide else "respond",
            "action_has_deadline": int(bool(
                msg["has_deadline"]
                or (was_open and state["action_has_deadline"]))),
            "completion_after_action": 0,
        }
    if msg["has_completion"] and state["action_source_id"]:
        return {**state, "completion_after_action": 1}
    return state


class Store:
    # 기본 대기(30초)는 sync 의 청크 커밋과 경합하는 **정당한** 쓰기용이다.
    # 열람 표시처럼 미뤄도 되는 쓰기는 READ_MARK_WAIT_MS 만 기다리고 넘어간다 —
    # 화면이 30초 서다 죽는 것보다 '다음 열람에 표시'가 낫다(2026-08-15).
    BUSY_TIMEOUT_MS = 30_000    # 배경 잡의 정당한 쓰기 — 기다려도 되는 쪽
    READ_MARK_WAIT_MS = 200     # 열람 표시 — 미뤄도 되는 쓰기(다음 열람에 재시도)
    UI_WRITE_WAIT_MS = 5_000    # 사용자 조작(플래그·숨김·노트) — 단일 스레드라
                                # 길게 기다리면 화면 전체가 함께 멈춘다

    def __init__(self, db_path: Path, my_addresses: list[str],
                 my_names: list[str] | tuple = (), noise=None):
        self.db_path = db_path
        self.my_addresses = {a.lower() for a in my_addresses}
        # 본문 '나 지목' 판정용 이름 — 설정 이름 + 내 주소 로컬파트(설정 의존이라
        # _feature_version 해시에 포함, 바뀌면 message_features 백필).
        self.my_names = sorted({n.strip().lower() for n in my_names if n.strip()})
        self._signal_names = tuple(self.my_names) + tuple(
            a.split("@")[0] for a in sorted(self.my_addresses))
        # 확실한 노이즈(hard) 판정자 — 보통 Config. 액션 fold 가 노이즈 메시지를
        # 무시하는 데 쓴다: 자동회신·시스템 알림이 열린 요청의 source 를 탈취하거나
        # ('7월 20일까지 부재…부탁드립니다') 완료 문구로 강등시키는 것 방지.
        # 판정 표면(ignore/blocked/subject_strong)은 _action_version 에 포함 —
        # 노이즈 설정이 바뀌면 본문 재분류 없이 액션만 재접기(_refold_all_actions).
        self._noise = noise
        # timeout=30 은 sqlite busy_timeout 30000ms 와 동일한 busy handler 로,
        # 연결 생성 시점에 설치되어 아래 PRAGMA·DDL 포함 전 구문을 보호한다.
        # 기본 5s 로는 부족: 백그라운드 sync 의 ingest 가 전체 배치를 한 트랜잭션으로
        # 잡는 동안(Outlook fetch 포함, 수십 초 가능) 다른 연결의 쓰기가 5s 대기 후
        # 'database is locked' 로 실패했다(앱 모드에서 관측).
        self.db = sqlite3.connect(db_path, timeout=self.BUSY_TIMEOUT_MS / 1000)
        # 화면을 세울 자격이 없는 쓰기(열람 표시)가 넘어간 횟수 — 운영 중 빈도 관측용
        self.skipped_read_marks = 0
        self.db.row_factory = sqlite3.Row
        # incremental vacuum: 이미지 프룬이 지운 공간을 조각 단위로 회수 —
        # 풀 VACUUM(수십 초 배타 잠금)은 단일 스레드 웹 서버를 세우므로 금지.
        # 이 PRAGMA 는 새 DB(테이블 생성 전)에서만 효력이라 **그때만 건다.**
        # 기존 DB 에 다시 걸면 값이 이미 INCREMENTAL 이어도 SQLite 가 쓰기
        # 트랜잭션을 열어, 백그라운드 잡(sync ingest 등)이 쓰기를 쥔 동안 여는
        # 모든 연결이 busy_timeout(30s)을 다 쓰고 'database is locked' 로 죽었다
        # — 웹은 요청마다 Store 를 열므로 화면 전체가 멈춘다(2026-08-15 실사용
        # 보고, 실측 재현). 값을 못 바꾸는 기존 DB 에선 어차피 무효라 건너뛰어도
        # 잃는 것이 없다. 판별은 읽기 한 번(sqlite_master)이라 잠금을 안 잡는다.
        if not self.db.execute(
                "SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        # 성능 PRAGMA(결과 불변, 속도만) — synchronous=NORMAL: WAL 에서 표준 권장.
        # 앱 크래시엔 안전, OS 크래시/정전 시에만 마지막 트랜잭션 유실 가능한데
        # 이 DB 는 Outlook 에서 재수집 가능한 캐시(message_id UNIQUE 로 멱등)라
        # 안전하다. 커밋마다 fsync 제거 → sync·열람 쓰기 대폭 가속.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-16384")   # 16MB 페이지 캐시(기본 2MB)
        self.db.execute("PRAGMA temp_store=MEMORY")    # 정렬·임시 결과 RAM
        self.db.execute("PRAGMA mmap_size=268435456")  # 256MB 메모리맵 읽기
        self.db.executescript(_SCHEMA)
        self._ensure_late_columns()
        self._ensure_late_indexes()
        # 파생 테이블(재구축 가능) — 버전 마이그레이션이 drop+재생성으로 스키마를 바꾼다.
        self.db.executescript(_FEATURES_DDL)
        self.db.executescript(_THREAD_STATE_DDL)
        self.db.executescript(_TERM_FEATURES_DDL)
        # 일반 스키마 개편은 clean start 원칙. 목록용 파생 테이블만 원본 messages에서
        # 결과 불변으로 재생성할 수 있어 _ensure_derived_state가 버전별 1회 백필한다.
        try:
            self.db.execute(_FTS_TRIGRAM)
            self.fts_tokenizer = "trigram"
        except sqlite3.OperationalError:
            self.db.execute(_FTS_FALLBACK)
            self.fts_tokenizer = "unicode61"
        self.db.commit()
        self.recleaned = 0        # 이번 열기의 재절단 건수 (sync·serve 가 알린다)
        self._reclean_quotes()    # 반드시 _ensure_derived_state 보다 먼저 (아래 docstring)
        self._ensure_derived_state()
        self._term_features_ready = self._term_features_are_current()
        self._term_bags_ready = self._term_bags_are_current()
        self._word_background_cache: dict[tuple, dict] = {}

    def close(self) -> None:
        self.db.close()

    def _ensure_late_columns(self) -> None:
        """나중에 생긴 컬럼을 구 DB에 경량 추가한다 (재수집 불필요).

        여기 오는 테이블은 drop/rebuild 로 못 되살리는 것들이다 — AI 산출물이거나
        (people_dossier) 사람이 승인한 자산이다. SQLite 는
        ADD COLUMN IF NOT EXISTS 가 없으므로 table_info 로 한 번 확인한다.
        """
        specs = (
            ("reclean_backup", "from_version", "INTEGER DEFAULT 0"),
            ("people_dossier", "validator_version", "INTEGER DEFAULT 1"),
            # Outlook 에서 사라진 메일 표시. 기본값 없이 NULL 이 '정상'이다 —
            # 확인한 적 없는 것을 '있다'고도 '없다'고도 말하지 않는다.
            ("messages", "gone_at", "TEXT"),
            # 적재 순서(messages DDL 주석 참고). 구 DB 는 id 가 곧 적재 순서였으므로
            # 아래에서 id 로 되메운다 — 그게 정확히 옳은 값이다.
            ("messages", "ingest_seq", "INTEGER"),
        )
        for table, col, decl in specs:
            cols = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})")}
            if not cols or col in cols:
                continue               # 테이블이 아직 없거나(신규 DB) 이미 있다
            try:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as e:
                # 웹과 CLI가 구 DB를 동시에 처음 열면 둘 다 table_info 에서 빠진
                # 컬럼을 볼 수 있다. 먼저 끝난 쪽의 ADD만 인정하고 다른 오류는 전파.
                if "duplicate column name" not in str(e).lower():
                    raise
        # 구 DB 되메우기 — 옛 rowid 는 삽입 순서 그 자체라 그대로 옮기면 정확하다.
        # **되메울 행이 있을 때만 쓴다.** UPDATE 는 0행이어도 쓰기 트랜잭션을
        # 열어, 이 한 줄이 모든 Store 열기를 잠재적 writer 로 만들었다(웹은
        # 요청마다 연다) — 백그라운드 잡이 쓰기를 쥔 동안 요청이 30초 대기 후
        # 'database is locked' 로 죽는다(2026-08-15). 탐지는 읽기 한 번이다.
        if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='messages'").fetchone() and self.db.execute(
                "SELECT 1 FROM messages WHERE ingest_seq IS NULL "
                "LIMIT 1").fetchone():
            self.db.execute(
                "UPDATE messages SET ingest_seq = id WHERE ingest_seq IS NULL")

    def _ensure_late_indexes(self) -> None:
        """뒤늦게 생긴 컬럼 위의 인덱스 — 반드시 _ensure_late_columns 다음에.

        스키마(_SCHEMA)에 두면 구 DB 에서 executescript 가 통째로 죽는다:
        CREATE TABLE 은 IF NOT EXISTS 로 건너뛰는데 그 아래 CREATE INDEX 는
        아직 없는 컬럼을 참조하기 때문이다. 재수집 강요 금지(규칙 5)의 실패
        사례였다 — 2026-08-13 이전 DB 가 열리지 않았고 데모도 그중 하나였다.
        """
        if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='messages'").fetchone():
            self.db.execute("CREATE INDEX IF NOT EXISTS "
                            "idx_messages_ingest_seq ON messages(ingest_seq)")

    def _is_hard_noise(self, sender: str, subject: str) -> bool:
        """액션 fold 가 무시할 확실한 노이즈 메시지인가 (판정자 없으면 항상 False)."""
        return bool(self._noise) and (
            self._noise.is_noise_sender_hard(sender or "")
            or self._noise.is_noise_subject_strong(subject or ""))

    # 파생 캐시의 수명주기는 둘로 나뉜다(2026-07-17). 새 설정을 추가할 때 어느
    # 쪽인지는 **누가 그 값을 읽는가**로 정한다:
    #   classify_message 가 읽는다  → _feature_version (본문 재분류 필요)
    #   _is_hard_noise 가 읽는다    → _action_version  (재접기만 필요)
    # 둘 다 아니면(질의 시점에만 쓰이면) 어느 버전에도 넣지 않는다 —
    # external_allowlist 가 그 예로, actions.evaluate 가 매번 새로 판정한다.
    def _feature_version(self) -> str:
        """본문 사실 캐시(message_features)와 스레드 집계의 버전.

        입력 = classify_message 가 읽는 것: 규칙 버전 + 내 주소(addressed_to_me)
        + 내 이름(mentions_me — 저장 비트라 이름이 바뀌면 낡은 지목 판정이 남는다).
        노이즈 설정은 여기 없다 — 발신자를 차단해도 본문에서 뽑은 사실(요청·기한·
        완료 문장)은 그대로다.
        """
        sig = hashlib.sha256(
            ("\0".join(sorted(self.my_addresses))
             + "\1" + "\0".join(self.my_names)).encode("utf-8")
        ).hexdigest()[:12]
        return f"{FEATURE_VERSION}:{sig}"

    def _action_version(self) -> str:
        """액션 상태(thread_state 의 action_* 컬럼)의 버전.

        입력 = _is_hard_noise 가 읽는 것뿐 — fold 가 노이즈 메시지를 건너뛰므로
        차단 목록·자동발송 패턴·강한 제목이 바뀌면 재접기가 필요하다. 본문 재분류는
        불필요: 저장된 신호로 다시 접기만 하면 된다(1만 통 기준 ~9s → ~85ms).
        """
        if self._noise is None:
            return "-"
        return hashlib.sha256("\0".join(
            "\1".join(str(p) for p in lst) for lst in (
                sorted(self._noise.ignore_senders),
                sorted(self._noise.blocked_senders),
                sorted(self._noise.subject_noise_strong))
        ).encode("utf-8")).hexdigest()[:12]

    def _addressed_to_me(self, to_addrs: str, cc_addrs: str) -> int:
        addrs = {a.lower() for a in (to_addrs + ";" + cc_addrs).split(";") if a}
        return int(bool(addrs & self.my_addresses))

    def _insert_features(self, message_id: int, feats: dict) -> None:
        cols = ", ".join(_FEATURE_COLS)
        marks = ",".join("?" * (len(_FEATURE_COLS) + 1))
        self.db.execute(
            f"INSERT INTO message_features (message_id, {cols}) VALUES ({marks})",
            (message_id, *[feats[c] for c in _FEATURE_COLS]),
        )

    def _term_feature_version(self) -> str:
        return str(terms_mod.WORD_FEATURE_VERSION)

    def _word_window_weeks(self) -> int:
        raw = (self._noise.opt("dossier", "window_weeks", default=26)
               if self._noise is not None and hasattr(self._noise, "opt")
               else 26)
        try:
            return max(1, min(260, int(raw or 26)))
        except (TypeError, ValueError):
            return 26

    def _term_window_is_current(self) -> bool:
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='term_window_weeks'"
        ).fetchone()
        return bool(row and row["value"] == str(self._word_window_weeks()))

    def _word_bounds(self, window_weeks: int | None = None
                     ) -> tuple[str, str]:
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return "", ""
        weeks = self._word_window_weeks() if window_weeks is None else window_weeks
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{int(weeks) * 7} days")
        ).fetchone()[0]
        return latest, since

    def _term_features_are_current(self) -> bool:
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='term_feature_version'"
        ).fetchone()
        return bool(
            row and row["value"] == self._term_feature_version()
            and self._term_window_is_current()
        )

    def _insert_term_features(self, message_id: int, new_content: str,
                              subject: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO message_term_features "
            "(message_id, feature_json) VALUES (?, ?)",
            (message_id, terms_mod.encode_features(new_content, subject)),
        )

    def _sync_term_features(self, message_ids: list[int]) -> None:
        """메일별 어휘 사실을 sync 트랜잭션 안에서 버전 백필한다.

        웹 Store 초기화에서는 호출하지 않는다. 새 버전 배포 후에도 페이지 시작을
        막지 않고, 다음 Outlook sync가 최근 분석 창의 본문만 한 번 읽는다.
        """
        version = self._term_feature_version()
        have = self.db.execute(
            "SELECT value FROM sync_state WHERE key='term_feature_version'"
        ).fetchone()
        full = not (
            have and have["value"] == version
            and self._term_window_is_current()
        )
        _, since = self._word_bounds()
        if full:
            self.db.execute("DELETE FROM message_term_features")
            self.db.execute("DELETE FROM message_term_bags")
            self.db.execute("DELETE FROM message_term_subject_delta")
            self.db.execute("DELETE FROM person_term_window")
            self.db.execute(
                "DELETE FROM sync_state WHERE key='term_bag_version'")
            for row in self.db.execute(
                """SELECT id, new_content, subject
                   FROM messages
                   WHERE is_sent=0 AND sent_on >= ?
                   ORDER BY id""", (since or "9999",)):
                self._insert_term_features(
                    row["id"], row["new_content"] or "",
                    row["subject"] or "")
        else:
            self.db.execute(
                """DELETE FROM message_term_features
                   WHERE message_id IN (
                     SELECT id FROM messages
                     WHERE is_sent!=0 OR sent_on < ?
                   )""", (since or "9999",))
            for pos in range(0, len(message_ids), 500):
                chunk = message_ids[pos:pos + 500]
                marks = ",".join("?" * len(chunk))
                rows = self.db.execute(
                    f"""SELECT id, new_content, subject
                        FROM messages
                        WHERE is_sent=0 AND sent_on >= ?
                          AND id IN ({marks})
                        ORDER BY id""",
                    [since or "9999", *chunk],
                ).fetchall()
                for row in rows:
                    self._insert_term_features(
                        row["id"], row["new_content"] or "",
                        row["subject"] or "")
        self.db.execute(
            "INSERT INTO sync_state(key, value) VALUES('term_feature_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
        self.db.execute(
            "INSERT INTO sync_state(key, value) VALUES('term_window_weeks', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self._word_window_weeks()),))
        self._term_features_ready = True

    def word_people_names(self) -> dict[str, str]:
        """어휘에서 사람 언급을 분리할 현재 주소록. 하드 노이즈는 제외."""
        out = {}
        for row in self.db.execute(
                "SELECT addr, name FROM people WHERE name != ''"):
            addr = (row["addr"] or "").lower()
            if addr and not self._is_hard_noise(addr, ""):
                out[addr] = row["name"] or addr
        return out

    def _word_extra_stop(self) -> list[str]:
        extra = list(self.my_names)
        extra.extend(a.split("@")[0] for a in self.my_addresses)
        if self._noise is not None and hasattr(self._noise, "opt"):
            extra.extend(
                self._noise.opt(
                    "dossier", "word_stop_extra", default=[]) or [])
        return extra

    def _term_bag_version(self) -> str:
        payload = {
            "feature": terms_mod.WORD_FEATURE_VERSION,
            "projection": terms_mod.WORD_BAG_VERSION,
            "window_weeks": self._word_window_weeks(),
            "names": sorted(self.word_people_names().items()),
            "stop": sorted(str(v).strip().lower()
                           for v in self._word_extra_stop() if str(v).strip()),
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:20]

    def _term_bags_are_current(self) -> bool:
        if not self._term_features_are_current():
            return False
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='term_bag_version'"
        ).fetchone()
        return bool(row and row["value"] == self._term_bag_version())

    @staticmethod
    def _phrase_term(pair) -> str:
        return "\x1f".join(pair)

    def _term_analysis_context(self) -> dict:
        return terms_mod.analysis_context(
            self.word_people_names(), self._word_extra_stop())

    def _build_term_bags(self, rows, context: dict) -> None:
        bag_rows = []
        subject_delta_rows = []
        window_df: Counter = Counter()
        for row in rows:
            feature = terms_mod.decode_features(row["feature_json"])
            body = terms_mod.document_bags(
                feature, row["sender_addr"], context, ("body",))
            subject = terms_mod.document_bags(
                feature, row["sender_addr"], context, ("subject",))
            bag_rows.append((
                row["id"], terms_mod.encode_bag(body),
                terms_mod.encode_bag(subject)))
            subject_delta_rows.extend(
                (row["id"], "term", term)
                for term in subject["terms"] - body["terms"])
            subject_delta_rows.extend(
                (row["id"], "phrase", self._phrase_term(phrase))
                for phrase in subject["phrases"] - body["phrases"])
            addr = row["sender_addr"]
            if not addr:
                continue
            for term in body["terms"]:
                window_df[(addr, term, "term")] += 1
            for phrase in body["phrases"]:
                window_df[
                    (addr, self._phrase_term(phrase), "phrase")] += 1
        if bag_rows:
            self.db.executemany(
                """INSERT OR REPLACE INTO message_term_bags
                   (message_id, body_bag_json, subject_bag_json)
                   VALUES (?, ?, ?)""", bag_rows)
        if subject_delta_rows:
            self.db.executemany(
                """INSERT OR REPLACE INTO message_term_subject_delta
                   (message_id, kind, term) VALUES (?, ?, ?)""",
                subject_delta_rows)
        if window_df:
            self.db.executemany(
                """INSERT INTO person_term_window
                   (sender_addr, term, kind, mail_df)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(kind, term, sender_addr)
                   DO UPDATE SET mail_df=mail_df+excluded.mail_df""",
                [(*key, count) for key, count in window_df.items()])

    def _subtract_expired_term_bags(self, since: str) -> None:
        """rolling window 밖으로 나간 메일의 본문 DF를 정확히 차감한다."""
        last_id = 0
        while True:
            expired = self.db.execute(
                """SELECT m.id, m.sender_addr, b.body_bag_json
                   FROM message_term_bags b
                   JOIN messages m ON m.id=b.message_id
                   WHERE (m.is_sent!=0 OR m.sent_on < ?) AND m.id > ?
                   ORDER BY m.id LIMIT 250""",
                (since or "9999", last_id),
            ).fetchall()
            if not expired:
                break
            removed: Counter = Counter()
            for row in expired:
                body = terms_mod.decode_bag(row["body_bag_json"])
                addr = row["sender_addr"]
                for term in body["terms"]:
                    removed[(addr, term, "term")] += 1
                for phrase in body["phrases"]:
                    removed[
                        (addr, self._phrase_term(phrase), "phrase")] += 1
            self.db.executemany(
                """UPDATE person_term_window
                   SET mail_df=mail_df-?
                   WHERE sender_addr=? AND term=? AND kind=?""",
                [(count, *key) for key, count in removed.items()],
            )
            last_id = expired[-1]["id"]
        self.db.execute(
            "DELETE FROM person_term_window WHERE mail_df <= 0")

    def _rebuild_term_bags(self, version: str) -> None:
        self.db.execute("DELETE FROM message_term_bags")
        self.db.execute("DELETE FROM message_term_subject_delta")
        self.db.execute("DELETE FROM person_term_window")
        context = self._term_analysis_context()
        last_id = 0
        while True:
            rows = self.db.execute(
                """SELECT m.id, m.sender_addr, m.sent_on, f.feature_json
                   FROM message_term_features f
                   JOIN messages m ON m.id=f.message_id
                   WHERE f.message_id > ?
                   ORDER BY f.message_id LIMIT 250""", (last_id,)).fetchall()
            if not rows:
                break
            self._build_term_bags(rows, context)
            last_id = rows[-1]["id"]
        self.db.execute(
            "INSERT INTO sync_state(key, value) VALUES('term_bag_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
        self._term_bags_ready = True

    def _sync_term_bags(self, message_ids: list[int]) -> None:
        """설정별 compact bag과 rolling 문서 빈도를 sync에서 증분 유지한다."""
        version = self._term_bag_version()
        have = self.db.execute(
            "SELECT value FROM sync_state WHERE key='term_bag_version'"
        ).fetchone()
        if not have or have["value"] != version:
            self._rebuild_term_bags(version)
            return
        _, since = self._word_bounds()
        self._subtract_expired_term_bags(since)
        self.db.execute(
            """DELETE FROM message_term_subject_delta
               WHERE NOT EXISTS (
                 SELECT 1 FROM message_term_features f
                 WHERE f.message_id=message_term_subject_delta.message_id
               )""")
        self.db.execute(
            """DELETE FROM message_term_bags
               WHERE NOT EXISTS (
                 SELECT 1 FROM message_term_features f
                 WHERE f.message_id=message_term_bags.message_id
               )""")
        if not message_ids:
            self._term_bags_ready = True
            return
        context = self._term_analysis_context()
        for pos in range(0, len(message_ids), 500):
            chunk = message_ids[pos:pos + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"""SELECT m.id, m.sender_addr, m.sent_on, f.feature_json
                    FROM messages m
                    JOIN message_term_features f ON f.message_id=m.id
                    WHERE m.is_sent=0 AND m.id IN ({marks})""",
                chunk,
            ).fetchall()
            self._build_term_bags(rows, context)
        self._term_bags_ready = True

    def _write_action_state(self, thread_id: int, st: dict) -> None:
        self.db.execute(
            "UPDATE thread_state SET action_source_id=?, action_strength=?, "
            "action_kind=?, action_has_deadline=?, completion_after_action=? "
            "WHERE thread_id=?",
            (st["action_source_id"], st["action_strength"], st["action_kind"],
             st["action_has_deadline"], st["completion_after_action"], thread_id),
        )

    def _fold_all_actions(self) -> None:
        """전 스레드의 액션 상태를 시간순 fold 로 재계산 (호출자가 트랜잭션 보유).

        한 번의 정렬 스캔으로 전 스레드를 접는다(스레드 경계에서 플러시). 증분
        경로와 같은 fold_action, hard 노이즈 스킵도 동일.

        빈 상태(열린 액션 없음)도 **반드시 쓴다** — 액션 전용 재접기는 테이블이
        새것이 아니라, 건너뛰면 차단으로 사라져야 할 옛 액션이 그대로 남는다.
        """
        rows = self.db.execute(
            """SELECT m.thread_id, m.id AS id, m.is_sent,
                      m.sender_addr, m.subject, f.*
               FROM messages m JOIN message_features f ON f.message_id=m.id
               ORDER BY m.thread_id, m.sent_on, m.id""").fetchall()
        cur_tid, state = None, dict(_EMPTY_ACTION)
        for m in rows:
            if m["thread_id"] != cur_tid:
                if cur_tid is not None:
                    self._write_action_state(cur_tid, state)
                cur_tid, state = m["thread_id"], dict(_EMPTY_ACTION)
            if self._is_hard_noise(m["sender_addr"], m["subject"]):
                continue
            state = fold_action(state, m)
        if cur_tid is not None:
            self._write_action_state(cur_tid, state)

    def _refold_all_actions(self, version: str) -> None:
        """노이즈 설정만 바뀐 경우 — 저장된 신호로 액션 상태만 제자리 재계산.

        본문을 읽지 않으므로 본문 크기와 무관하게 빠르다(전체 백필의 1/25 이하).
        message_features·집계 컬럼은 손대지 않는다 — 차단은 '이 메시지를 액션
        계산에서 뺄지'만 바꾸지 본문의 사실을 바꾸지 않기 때문.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._fold_all_actions()
            self.db.execute(
                "INSERT INTO sync_state(key, value) VALUES('action_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _reclean_quotes(self) -> None:
        """clean.CLEAN_VERSION 승격 시 저장된 new_content 에 새 절단을 1회 소급.

        원문(body_text)은 저장하지 않지만, 절단 실패분은 인용 체인이 new_content
        안에 그대로 남아 있으므로 저장값 재절단으로 복구된다(재수집 불필요 — 불변식 4).
        - 스레드 첫 보유 메일은 ingest 와 같게 preserve_quotes=True (mid-join 보존
          — 새 규칙이 이제야 찾은 절단점 아래를 버리지 않고 마커로 접는다).
        - PRESERVED_MARK 가 이미 있으면 구 절단이 성공했던 메일 — 건너뜀.
        - 재절단 결과가 비면 옛 값 유지 — 마이그레이션은 파괴하지 않는다(새 규칙
          오탐이 과거 메일을 통째 지우는 것 방지. 신규 수집 경로는 ingest 그대로).
        - 저장된 HTML(message_html)은 손대지 않는다 — 표시 전용이고 되돌릴
          백업이 없다. 그래서 재절단된 메일은 텍스트만 줄고 화면의 인용 접기는
          예전 그대로다(AI 입력은 new_content 라 비용 문제는 해결된다).
        - 바꾸기 전 값은 reclean_backup 에 남긴다. sync 는 이미 있는 메일을
          건너뛰므로 sync --full 로도 본문이 되돌아오지 않는다 — 규칙 오탐이
          드러났을 때 이 표가 유일한 복구 수단이다(복구 SQL 은 스키마 주석).
        변경이 있으면 FTS 는 'rebuild' 로 통째 재색인 — 외부 콘텐츠 테이블의 행 단위
        delete 프로토콜은 옛 값이 정확히 일치해야 해서 대량 갱신엔 rebuild 가 안전.
        재분류·어휘는 여기서 직접 안 한다: 바뀐 게 있을 때만 두 버전 스탬프를
        지워 각자의 백필(_ensure_derived_state · 다음 sync 의 _sync_term_features)이
        이어받는다 — 그래서 이 메서드는 __init__ 에서 _ensure_derived_state 보다
        먼저 돌아야 한다.
        """
        ver = str(CLEAN_VERSION)
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='clean_version'").fetchone()
        if row and row["value"] == ver:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # 웹·CLI 가 동시에 처음 열면 둘 다 여기 올 수 있다 — 잠금 획득 후
            # 재확인해 두 번째는 그냥 스탬프만 확인하고 나간다.
            row = self.db.execute(
                "SELECT value FROM sync_state WHERE key='clean_version'").fetchone()
            if row and row["value"] == ver:
                self.db.commit()
                return
            # ingest 의 preserve 판정은 '스레드를 만든 메일'(= 그 스레드에서
            # 먼저 적재된 것)이다. **발신 시각순으로 잡으면 안 된다** — 나중에
            # 백필된 더 오래된 메일이 first 로 뽑혀 진짜 첫 보유분의 유일한 인용
            # 체인이 잘려 나간다(--since 로 최근분을 먼저 모은 뒤 --full 을 돌리는
            # 실제 순서. 2026-07-31 리뷰 실증).
            # 그래서 id 가 아니라 **ingest_seq** 를 본다. 번호가 날짜 기반이 된
            # 뒤로 id 는 발신 시각순이라 적재 순서를 더는 못 말한다(2026-08-11).
            firsts: dict[int, int] = {}
            for r in self.db.execute(
                    "SELECT thread_id, id FROM messages WHERE ingest_seq IN "
                    "(SELECT MIN(ingest_seq) FROM messages GROUP BY thread_id)"):
                firsts[r["thread_id"]] = r["id"]
            changed = 0
            threads: set[int] = set()
            now = datetime.now().isoformat(timespec="seconds")
            # 청크 순회 — 같은 연결로 UPDATE 하므로 커서를 열어 둘 수 없고,
            # fetchall() 로 전 본문을 올리면 3만통에서 peak 564MB 다(리뷰 계측).
            last = 0
            while True:
                rows = self.db.execute(
                    "SELECT id, thread_id, new_content FROM messages "
                    "WHERE id > ? ORDER BY id LIMIT 500", (last,)).fetchall()
                if not rows:
                    break
                last = rows[-1]["id"]
                for m in rows:
                    old = m["new_content"] or ""
                    if not old or PRESERVED_MARK in old:
                        continue
                    preserve = firsts.get(m["thread_id"]) == m["id"]
                    new = extract_new_content(old, preserve_quotes=preserve)
                    if not new or new == old:
                        continue
                    # OR IGNORE — 여러 번 승격돼도 **최초 원본**을 남긴다(2차
                    # 절단의 입력을 백업하면 되돌려도 이미 잘린 값이다).
                    self.db.execute(
                        "INSERT OR IGNORE INTO reclean_backup"
                        "(message_id, old_content, created, from_version)"
                        " VALUES (?,?,?,?)", (m["id"], old, now, CLEAN_VERSION))
                    self.db.execute(
                        "UPDATE messages SET new_content=? WHERE id=?",
                        (new, m["id"]))
                    # 요약 재생성은 **실질 변경**일 때만 — 서명 한 줄(20~30자)이
                    # 사라졌다고 스레드를 다시 요약하면 AI 비용이 헛나간다
                    # (실기기 사본에서 변경 88건이 전부 서명 제거였다).
                    if len(old) - len(new) >= _RESUMMARIZE_MIN_CUT:
                        threads.add(m["thread_id"])
                    changed += 1
            if changed:
                # 부풀었던 본문으로 만들어진 롤링 요약은 증분 가드
                # (summary_msg_count) 때문에 영영 재생성되지 않는다 — 바뀐
                # 스레드만 가드를 풀어 다음 회고가 다시 요약하게 한다.
                self.db.executemany(
                    "UPDATE threads SET summary_msg_count=0 WHERE id=?",
                    [(t,) for t in threads])
                self.db.execute(
                    "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                # 본문이 바뀐 DB 에서만 파생을 다시 만든다. 버전 해시에
                # CLEAN_VERSION 을 넣으면 **바뀐 게 없는 사용자까지** 전체
                # 재분류(1만통 ~11s, 3만통 ~32s)를 치르고 그동안 웹 첫 화면이
                # 비어 있다(2026-07-31 리뷰 계측). 스탬프만 지우면 비용이
                # 실제 변경이 있는 DB 에만 붙는다.
                self.db.execute(
                    "DELETE FROM sync_state WHERE key IN "
                    "('feature_version', 'term_feature_version')")
                # 업무 어휘 지도 캐시는 키에 **본문이 없다**(메시지 id·통수·
                # 날짜 지문뿐) — 재절단은 그 셋을 하나도 안 바꾸므로 캐시가
                # 그대로 히트해, 지워진 인용 체인의 어휘가 인물 화면에 계속
                # 남는다(2026-07-31 리뷰 실증). 파생이라 지우면 재생성된다.
                self.db.execute("DELETE FROM people_word_profiles")
            if changed:
                self.db.execute(
                    "INSERT INTO sync_state(key, value) VALUES('last_reclean', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{now}:{changed}",))
            self.db.execute(
                "INSERT INTO sync_state(key, value) VALUES('clean_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ver,))
            self.db.commit()
            self.recleaned = changed
        except Exception:
            self.db.rollback()
            raise

    def suspect_uncut_quotes(self, limit: int = 10) -> list[dict]:
        """인용 절단 실패 의심 스레드 — diagnose 표시용(읽기 전용).

        후속 메일의 new_content 가 직전 메일 본문을 통째로 다시 담고 있으면
        헤더 라벨을 못 알아본 것이다(미지원 언어 클라이언트 등). 언어와 무관한
        결정론 신호라, 새 라벨이 필요한 스레드를 실기기에서 표본으로 찾는 창구."""
        sus = []
        for t in self.db.execute(
                "SELECT thread_id FROM messages GROUP BY thread_id "
                "HAVING COUNT(*) >= 3"):
            msgs = self.db.execute(
                "SELECT sender_addr, subject, new_content FROM messages "
                "WHERE thread_id=? ORDER BY sent_on ASC, id ASC",
                (t["thread_id"],)).fetchall()
            pairs = 0
            for a, b in zip(msgs, msgs[1:]):
                pa = " ".join((a["new_content"] or "").split())[:200]
                pb = " ".join((b["new_content"] or "").split())
                # 직전 메일 앞 200자가 다음 메일에 통째로 → 체인 재포함 의심.
                # 짧은 상투구 오탐을 막으려 지문 최소 120자·본문 최소 2000자.
                if len(pa) >= 120 and len(pb) >= 2000 and pa in pb:
                    pairs += 1
            if pairs:
                sus.append({
                    "thread_id": t["thread_id"], "pairs": pairs,
                    "subject": (msgs[0]["subject"] or "")[:40],
                    "domain": (msgs[-1]["sender_addr"] or "").split("@")[-1],
                })
        sus.sort(key=lambda d: -d["pairs"])
        return sus[:limit]

    def _ensure_derived_state(self) -> None:
        """파생 행을 버전당 1회 백필 — 무거운 쪽과 가벼운 쪽을 분리해 판정.

        feature 불일치 → 파생 테이블 drop+재생성 + 전 메일 재분류(스키마 변경까지
        흡수. 재구축 가능한 테이블이라 안전 — Outlook 재수집·DB 삭제 불필요).
        action 만 불일치 → 재분류 없이 액션 상태만 재접기.
        executescript 는 진행 중 트랜잭션을 커밋해 버리므로 여기선 execute 만.
        """
        fv, av = self._feature_version(), self._action_version()
        have = {r["key"]: r["value"] for r in self.db.execute(
            "SELECT key, value FROM sync_state "
            "WHERE key IN ('feature_version', 'action_version')")}
        if have.get("feature_version") == fv:
            if have.get("action_version") != av:
                self._refold_all_actions(av)
            return

        self.db.execute("BEGIN IMMEDIATE")
        try:
            # 잠금 획득 후 재확인 — 여러 프로세스가 같은 DB 를 동시에 처음 열면
            # 먼저 끝난 쪽이 이미 백필해 놨는데 나머지가 같은 일을 반복하고,
            # 그 시간이 busy_timeout 을 넘겨 'database is locked' 로 죽는다
            # (3만통 5프로세스 동시 열기에서 실측, 2026-07-31 리뷰).
            done = {r["key"]: r["value"] for r in self.db.execute(
                "SELECT key, value FROM sync_state "
                "WHERE key IN ('feature_version', 'action_version')")}
            if done.get("feature_version") == fv:
                self.db.commit()
                # 저장된 스탬프와 비교한다 — self._action_version() 은 순수
                # 함수라 av 와 항상 같아 재접기가 영영 안 돌았다(리뷰 지적).
                if done.get("action_version") != av:
                    self._refold_all_actions(av)
                return
            self.db.execute("DROP TABLE IF EXISTS message_features")
            self.db.execute("DROP TABLE IF EXISTS thread_state")
            self.db.execute(_FEATURES_DDL)
            self.db.execute(_THREAD_STATE_DDL)
            for m in self.db.execute(
                    "SELECT id, subject, to_addrs, cc_addrs, new_content "
                    "FROM messages"):
                feats = classify_message(
                    m["new_content"], m["subject"] or "", self._signal_names)
                feats["addressed_to_me"] = self._addressed_to_me(
                    m["to_addrs"] or "", m["cc_addrs"] or "")
                self._insert_features(m["id"], feats)

            self.db.execute(
                """INSERT INTO thread_state
                   (thread_id, first_message_id, first_sent_on,
                    latest_message_id, latest_sent_on, message_count,
                    sent_count, received_count, unread_received_count,
                    addressed_to_me_count, deadline_count)
                   SELECT t.id,
                          (SELECT id FROM messages WHERE thread_id=t.id
                           ORDER BY sent_on ASC, id ASC LIMIT 1),
                          (SELECT sent_on FROM messages WHERE thread_id=t.id
                           ORDER BY sent_on ASC, id ASC LIMIT 1),
                          (SELECT id FROM messages WHERE thread_id=t.id
                           ORDER BY sent_on DESC, id DESC LIMIT 1),
                          (SELECT sent_on FROM messages WHERE thread_id=t.id
                           ORDER BY sent_on DESC, id DESC LIMIT 1),
                          COUNT(m.id),
                          COALESCE(SUM(CASE WHEN m.is_sent=1 THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(CASE WHEN m.is_sent=0 THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(CASE WHEN m.is_sent=0 AND
                              (m.read_at IS NULL OR m.read_at='') THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(f.addressed_to_me), 0),
                          COALESCE(SUM(CASE WHEN m.is_sent=0 THEN f.has_deadline ELSE 0 END), 0)
                   FROM threads t
                   JOIN messages m ON m.thread_id=t.id
                   JOIN message_features f ON f.message_id=m.id
                   GROUP BY t.id"""
            )
            self._fold_all_actions()      # 액션 상태 — 액션 전용 경로와 같은 함수
            for key, value in (("feature_version", fv), ("action_version", av)):
                self.db.execute(
                    "INSERT INTO sync_state(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # ------------------------------------------------------------- sync

    def last_sync(self) -> str | None:
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='last_sync'"
        ).fetchone()
        return row["value"] if row else None

    def basis_info(self) -> dict:
        """분석 사이드바용 — 색인 통수와 마지막 '성공한 동기화 실행' 시각.

        stats() 를 쓰지 않는다: 거기엔 SUM(LENGTH(new_content)) 가 있어 대형 DB 에서
        본문을 전부 훑는다. 화면을 그릴 때마다 낼 비용이 아니다. 여기는 인덱스로
        끝나는 COUNT(*) 와 키 조회 둘뿐이다.

        checked_at 은 last_sync(수신 메일 워터마크)가 아니다 — 새 메일이 0통이어도
        전진하므로 '언제까지 확인된 상태인지'를 정직하게 말한다. 키가 없으면 ""."""
        n = self.db.execute(
            "SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='last_sync_checked_at'"
        ).fetchone()
        return {"messages": n, "checked_at": (row["value"] if row else "")}

    def ingest(self, records, progress=None,
               image_cutoff: str | None = None,
               chunk_size: int = 100) -> SyncStats:
        """MailRecord 스트림을 인덱싱. 시간순 입력을 가정.

        progress(stats) 가 주어지면 레코드마다 호출된다(CLI 라이브 카운터용).
        image_cutoff(YYYY-MM-DD): 이 날짜 이전 메일은 인라인 이미지를 임베드하지
        않는다(대량 백필에서 곧 프룬될 이미지 낭비 방지). None 이면 게이트 없음.

        chunk_size: 이 통수마다 커밋해 쓰기 잠금을 놓는다. 배치 전체를 한
        트랜잭션으로 묶으면 Outlook COM fetch(제너레이터)가 트랜잭션 안에서 도는
        동안(수십 초) 잠금을 쥐어, UI 쓰기(플래그·숨김·신호 토글)가 busy_timeout
        후 'database is locked' 로 실패한다. **불변식: 청크 커밋마다 어휘 피처와
        last_sync 워터마크를 함께 커밋한다** — 크래시가 나도 정합적 prefix 만
        남고 다음 sync 가 이어받는다(message_id UNIQUE 로 멱등). 이렇게 쪼갠 ingest
        는 '작은 sync 를 N 번 연속 실행'과 동일하다(증분 경로 상시 사용과 같은 계약).
        """
        stats = SyncStats()
        max_seen = self.last_sync() or ""
        self._pending_term_ids: list[int] = []

        def _flush() -> None:
            # 파생 어휘(피처·bag) 동기화 + 워터마크 전진을 한 커밋으로 — 이 셋이
            # 같은 트랜잭션이라야 크래시에도 커밋된 메일이 어휘·워터마크와 어긋나지 않음.
            self._sync_term_features(self._pending_term_ids)
            self._sync_term_bags(self._pending_term_ids)
            self._pending_term_ids = []
            if max_seen:
                self.db.execute(
                    "INSERT INTO sync_state(key, value) VALUES('last_sync', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (max_seen,),
                )
            self.db.commit()

        try:
            since_commit = 0
            for rec in records:
                stats.fetched += 1
                if self._insert(rec, stats, image_cutoff):
                    stats.inserted += 1
                    since_commit += 1
                else:
                    stats.skipped += 1
                if rec.sent_on > max_seen:
                    max_seen = rec.sent_on
                if progress:
                    progress(stats)
                if chunk_size and since_commit >= chunk_size:
                    _flush()                  # 잠금 해제 지점 — UI 쓰기가 끼어들 수 있음
                    since_commit = 0
            _flush()                          # 잔여 + 워터마크·버전 스탬프(0통이어도)
            # 성공한 '실행' 시각 — last_sync(수집한 메일의 sent_on 워터마크)와 역할이
            # 다르다. 새 메일이 0통이어도 갱신되고, 예외로 빠지면 갱신되지 않는다.
            # 둘을 섞으면 "방금 동기화했는데 시각이 안 바뀐다"가 된다.
            self.db.execute(
                "INSERT INTO sync_state(key, value) "
                "VALUES('last_sync_checked_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (datetime.now().isoformat(timespec="seconds"),),
            )
            self.db.commit()
            self._word_background_cache.clear()
            return stats
        except Exception:
            self.db.rollback()                # 미커밋 청크만 되돌림(앞 청크는 유지)
            raise

    def _insert(self, rec: MailRecord, stats: SyncStats,
                image_cutoff: str | None = None) -> bool:
        exists = self.db.execute(
            "SELECT 1 FROM messages WHERE message_id=?", (rec.message_id,)
        ).fetchone()
        if exists:
            return False

        thread_id, t_created = self._assign_thread(rec, stats)
        # mid-join 보존 (docs/ARCHITECTURE.md §6.1): 새 스레드를 만든 메일 = 그
        # 스레드의 '내 첫 보유분'(fetch 가 시간순 병합 입력이라는 전제). 그 인용
        # 체인은 DB 에 없는 유일본이므로 절단 대신 보존한다 — 텍스트는 마커,
        # HTML 은 접힘. 기존 스레드 합류분은 종전대로 절단(중복 제거 철학).
        new_content = extract_new_content(rec.body_text, preserve_quotes=t_created)
        body_html = (sanitize_html(rec.body_html, preserve_quotes=t_created)
                     if rec.body_html else "")
        # 인라인 이미지 주입 — 정제(인용 절단) '후' 살아남은 cid 에만 (중복 1회).
        # 컷오프 이전 메일은 스킵 (어차피 프룬 대상 — 대량 백필 낭비 방지).
        if body_html and rec.inline_images and not (
                image_cutoff and (rec.sent_on or "")[:10] < image_cutoff):
            body_html, n_emb, n_fail = inject_inline_images(
                body_html, rec.inline_images)
            stats.img_embedded += n_emb
            stats.img_failed += n_fail
        stats.raw_chars += len(rec.body_text)
        stats.kept_chars += len(new_content)
        is_sent = int(rec.sender_addr.lower() in self.my_addresses)

        cur = self.db.execute(
            """INSERT INTO messages
               (id, message_id, entry_id, thread_id, subject, sender_name, sender_addr,
                to_addrs, cc_addrs, sent_on, is_sent, attach_names, new_content,
                raw_chars, folder, ingest_seq)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       (SELECT COALESCE(MAX(ingest_seq), 0) + 1 FROM messages))""",
            (
                next_id(self.db, "messages", rec.sent_on),
                rec.message_id, rec.entry_id, thread_id, rec.subject,
                rec.sender_name, rec.sender_addr.lower(),
                ";".join(a.lower() for a in rec.to),
                ";".join(a.lower() for a in rec.cc),
                rec.sent_on, is_sent, ";".join(rec.attachments),
                new_content, len(rec.body_text), rec.folder,
            ),
        )
        if body_html:
            self.db.execute(
                "INSERT INTO message_html(message_id, html) VALUES (?, ?)",
                (cur.lastrowid, body_html))
        if not is_sent:
            self._pending_term_ids.append(cur.lastrowid)
        feats = classify_message(new_content, rec.subject or "",
                                 self._signal_names)
        feats["addressed_to_me"] = self._addressed_to_me(
            ";".join(a.lower() for a in rec.to),
            ";".join(a.lower() for a in rec.cc),
        )
        self._insert_features(cur.lastrowid, feats)
        self.db.execute(_FTS_SYNC, (cur.lastrowid, rec.subject, new_content))
        self._touch_thread(thread_id, rec.sent_on)
        self._update_thread_state(
            thread_id, cur.lastrowid, rec.sent_on, is_sent, feats,
            hard_noise=self._is_hard_noise(rec.sender_addr, rec.subject))
        # 새 수신 메일이 숨긴 스레드에 오면 자동 숨김 해제 — "지금은 조용히,
        # 새 소식 오면 다시" (구 추적제외의 자동 복귀를 숨김이 흡수, 2026-07-12).
        # 내가 보낸 답장(is_sent=1)으로는 해제하지 않음 — "처리 중인데 다시
        # 뜨는" 혼란을 피함. 노이즈 스레드는 해제돼도 노이즈 필터가 목록에서 거름.
        if not is_sent:
            self.db.execute(
                "UPDATE threads SET hidden=0 WHERE id=? AND hidden=1",
                (thread_id,),
            )
        self._update_people(rec, is_sent)
        return True

    def _assign_thread(self, rec: MailRecord, stats: SyncStats) -> tuple[int, bool]:
        """스레드 배정 — (thread_id, 새로 만들었나).

        created=True 는 '이 메일이 그 스레드의 내 첫 보유분'이라는 뜻
        (시간순 입력 전제) — _insert 가 mid-join 인용 보존 트리거로 쓴다.
        """
        # 1순위: References/In-Reply-To 가 가리키는 기존 메시지의 스레드
        refs = list(rec.references)
        if rec.in_reply_to:
            refs.append(rec.in_reply_to)
        for ref in refs:
            row = self.db.execute(
                "SELECT thread_id FROM messages WHERE message_id=?", (ref,)
            ).fetchone()
            if row:
                return row["thread_id"], False
        # 2순위: 소스가 준 대화 키 (Outlook ConversationIndex 루트)
        if rec.conversation_key:
            row = self.db.execute(
                "SELECT id FROM threads WHERE conversation_key=?",
                (rec.conversation_key,),
            ).fetchone()
            if row:
                return row["id"], False
        # 3순위: 정규화 제목 일치 (최근 30일 내 활동 스레드만)
        norm = normalize_subject(rec.subject)
        if norm:
            row = self.db.execute(
                """SELECT id FROM threads WHERE norm_subject=?
                   AND last_date >= datetime(?, '-30 days')
                   ORDER BY last_date DESC LIMIT 1""",
                (norm, rec.sent_on or "9999"),
            ).fetchone()
            if row:
                return row["id"], False
        # 새 스레드 — 번호는 이 메일의 날짜 기준. 나중에 더 오래된 메일이 백필로
        # 합류해 first_date 가 앞당겨져도 **id 는 재계산하지 않는다**. 스레드 번호는
        # vault·URL 이 참조하는 값이라, 바뀌는 순간 그 참조들이 전부 어긋난다.
        cur = self.db.execute(
            """INSERT INTO threads (id, norm_subject, conversation_key,
                                    first_date, last_date)
               VALUES (?,?,?,?,?)""",
            (next_id(self.db, "threads", rec.sent_on),
             norm, rec.conversation_key, rec.sent_on, rec.sent_on),
        )
        stats.new_threads += 1
        return cur.lastrowid, True

    def _touch_thread(self, thread_id: int, sent_on: str) -> None:
        self.db.execute(
            """UPDATE threads SET
                 first_date = CASE WHEN first_date='' OR first_date > ? THEN ? ELSE first_date END,
                 last_date  = CASE WHEN last_date  < ? THEN ? ELSE last_date END
               WHERE id=?""",
            (sent_on, sent_on, sent_on, sent_on, thread_id),
        )

    def _update_thread_state(self, thread_id: int, message_id: int,
                             sent_on: str, is_sent: int, feats: dict,
                             hard_noise: bool = False) -> None:
        """Apply one appended message to its thread's persistent aggregate."""
        received = 0 if is_sent else 1
        addressed = feats["addressed_to_me"]
        inbound_deadline = feats["has_deadline"] if not is_sent else 0
        self.db.execute(
            """INSERT INTO thread_state
               (thread_id, first_message_id, first_sent_on,
                latest_message_id, latest_sent_on, message_count,
                sent_count, received_count, unread_received_count,
                addressed_to_me_count, deadline_count)
               VALUES (?,?,?,?,?,1,?,?,?,?,?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 first_message_id = CASE
                   WHEN excluded.first_sent_on < thread_state.first_sent_on
                     OR (excluded.first_sent_on = thread_state.first_sent_on
                         AND excluded.first_message_id < thread_state.first_message_id)
                   THEN excluded.first_message_id ELSE thread_state.first_message_id END,
                 first_sent_on = CASE
                   WHEN excluded.first_sent_on < thread_state.first_sent_on
                     OR (excluded.first_sent_on = thread_state.first_sent_on
                         AND excluded.first_message_id < thread_state.first_message_id)
                   THEN excluded.first_sent_on ELSE thread_state.first_sent_on END,
                 latest_message_id = CASE
                   WHEN excluded.latest_sent_on > thread_state.latest_sent_on
                     OR (excluded.latest_sent_on = thread_state.latest_sent_on
                         AND excluded.latest_message_id > thread_state.latest_message_id)
                   THEN excluded.latest_message_id ELSE thread_state.latest_message_id END,
                 latest_sent_on = CASE
                   WHEN excluded.latest_sent_on > thread_state.latest_sent_on
                     OR (excluded.latest_sent_on = thread_state.latest_sent_on
                         AND excluded.latest_message_id > thread_state.latest_message_id)
                   THEN excluded.latest_sent_on ELSE thread_state.latest_sent_on END,
                 message_count = thread_state.message_count + 1,
                 sent_count = thread_state.sent_count + excluded.sent_count,
                 received_count = thread_state.received_count + excluded.received_count,
                 unread_received_count = thread_state.unread_received_count
                                         + excluded.unread_received_count,
                 addressed_to_me_count = thread_state.addressed_to_me_count
                                          + excluded.addressed_to_me_count,
                 deadline_count = thread_state.deadline_count + excluded.deadline_count""",
            (thread_id, message_id, sent_on, message_id, sent_on,
             is_sent, received, received, addressed, inbound_deadline),
        )
        # 액션 상태기계 — 이 메시지가 최신이면 증분 전이, 역순 삽입(Outlook 이
        # 오래된 메일을 늦게 줌)이면 이 스레드만 재접기. 두 경로가 같은
        # fold_action 을 쓰므로 결과는 정의상 등가(드리프트 테스트가 가드).
        # hard 노이즈 메시지는 전이 대상이 아님 — 자동회신이 열린 요청의 source 를
        # 탈취하거나 시스템 '완료' 문구가 강등시키지 않게(리뷰 반영, 2026-07-17).
        row = self.db.execute(
            "SELECT latest_message_id, action_source_id, action_strength, "
            "action_kind, action_has_deadline, completion_after_action "
            "FROM thread_state WHERE thread_id=?", (thread_id,)).fetchone()
        if row["latest_message_id"] == message_id:
            if hard_noise:
                return
            msg = dict(feats)
            msg["id"] = message_id
            msg["is_sent"] = is_sent
            new = fold_action({k: row[k] for k in _ACTION_COLS}, msg)
            if any(new[k] != row[k] for k in _ACTION_COLS):
                self._write_action_state(thread_id, new)
        else:
            self._refold_thread_actions(thread_id)

    def _refold_thread_actions(self, thread_id: int) -> None:
        """스레드의 액션 상태를 시간순 전체 재계산 — 역순 삽입 보정.

        비용은 이 스레드 크기에 비례(전체 DB 재계산 아님). 증분 경로와
        동일하게 hard 노이즈 메시지는 건너뛴다."""
        state = dict(_EMPTY_ACTION)
        for m in self.db.execute(
                """SELECT m.id AS id, m.is_sent, m.sender_addr, m.subject, f.*
                   FROM messages m
                   JOIN message_features f ON f.message_id=m.id
                   WHERE m.thread_id=? ORDER BY m.sent_on, m.id""",
                (thread_id,)):
            if self._is_hard_noise(m["sender_addr"], m["subject"]):
                continue
            state = fold_action(state, m)
        self._write_action_state(thread_id, state)

    def _update_people(self, rec: MailRecord, is_sent: int) -> None:
        def upsert(addr: str, name: str, from_inc: int, to_inc: int) -> None:
            addr = addr.lower()
            if not addr or addr in self.my_addresses:
                return
            self.db.execute(
                """INSERT INTO people (addr, name, from_count, to_count, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(addr) DO UPDATE SET
                     name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                     from_count = from_count + excluded.from_count,
                     to_count   = to_count + excluded.to_count,
                     last_seen  = MAX(last_seen, excluded.last_seen)""",
                (addr, name, from_inc, to_inc, rec.sent_on, rec.sent_on),
            )

        if is_sent:
            for a in rec.to + rec.cc:
                upsert(a, "", 0, 1)
        else:
            upsert(rec.sender_addr, rec.sender_name, 1, 0)

    # ------------------------------------------------------------ queries

    def search(self, query: str, limit: int = 50) -> list[sqlite3.Row]:
        """DSL 질의 → 구조화 필터 + 단계적 FTS(phrase→AND→OR)→LIKE 폴백.

        각 행에 파생컬럼 snippet(⟪⟫ 강조)·tier 를 얹어 돌려준다. 완화 순서 =
        정밀→느슨: tier1 연속구 · tier2 FTS-AND · tier3 LIKE-AND(부분일치, 2자어
        포함 모두 포함) · tier4 FTS-OR(하나라도 — 유일한 '관련 낮음'). tier 를 1차
        정렬키, 같은 tier 안에서는 bm25(제목:본문=3:1)·최신순. id 로 중복 제거.
        """
        q = search_mod.parse_query(query)
        where, params = self._build_filters(q)

        if not q.has_text():
            if not q.has_filters():
                return []                                   # 빈 질의
            sql = ("SELECT m.*, '' AS snippet, 0 AS tier FROM messages m "
                   "LEFT JOIN threads t ON t.id = m.thread_id WHERE 1=1"
                   + where + " ORDER BY m.sent_on DESC LIMIT ?")
            return self.db.execute(sql, params + [limit]).fetchall()

        short_w, short_p = self._like_terms_sql(search_mod.terms_short(q))
        seen: set = set()
        out: list = []

        def collect(rows):
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                out.append(r)

        for tier in (1, 2):                                 # 연속구, FTS-AND
            match = search_mod.build_match(q, tier)
            if not match:
                continue
            collect(self._fts_tier(match, tier, where, params,
                                   short_w, short_p, limit))
            if len(out) >= limit:
                return out[:limit]

        if len(out) < limit:                                # tier3: LIKE-AND(부분일치)
            like_w, like_p = self._like_terms_sql(list(q.terms) + list(q.phrases))
            if like_w:
                sql = ("SELECT m.*, '' AS snippet, 3 AS tier FROM messages m "
                       "LEFT JOIN threads t ON t.id = m.thread_id WHERE 1=1"
                       + like_w + where + " ORDER BY m.sent_on DESC LIMIT ?")
                collect(self.db.execute(sql, like_p + params + [limit]).fetchall())

        if len(out) < limit:                                # tier4: FTS-OR(관련 낮음)
            match = search_mod.build_match(q, 3)            # build_match tier3 = OR
            if match:
                collect(self._fts_tier(match, 4, where, params, "", [], limit))
        return out[:limit]

    def _fts_tier(self, match, tier, where, params, short_w, short_p, limit):
        sql = (f"SELECT m.*, snippet(messages_fts, 1, '⟪', '⟫', '…', 12) AS snippet, "
               f"{int(tier)} AS tier, bm25(messages_fts, 3.0, 1.0) AS _score "
               "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
               "LEFT JOIN threads t ON t.id = m.thread_id "
               "WHERE messages_fts MATCH ?" + short_w + where +
               " ORDER BY _score, m.sent_on DESC LIMIT ?")
        return self.db.execute(sql, [match] + short_p + params + [limit]).fetchall()

    @staticmethod
    def _like_terms_sql(needles):
        """각 키워드를 (제목 OR 본문) LIKE 로 AND. (sql조각, params) 반환."""
        parts, params = [], []
        for t in needles:
            parts.append(" AND (m.subject LIKE ? OR m.new_content LIKE ?)")
            params += [f"%{t}%", f"%{t}%"]
        return "".join(parts), params

    def _resolve_addr(self, name: str) -> str | None:
        """사람 이름 → 대표 주소 (왕래 많은 순). to:/cc: 한글 이름 해석용.

        to_addrs·cc_addrs 에는 표시명이 없고 주소만 있어, 한글 이름은 people 로
        먼저 주소를 찾아야 매칭된다. 공백은 무시하고 비교.
        """
        ns = name.replace(" ", "")
        if not ns:
            return None
        row = self.db.execute(
            "SELECT addr FROM people WHERE REPLACE(name, ' ', '') LIKE ? "
            "ORDER BY (from_count + to_count) DESC LIMIT 1", (f"%{ns}%",),
        ).fetchone()
        return row["addr"] if row else None

    def _build_filters(self, q):
        """Query 의 구조화 조건 → (' AND …' SQL, params). 주소 LIKE 는 ASCII 라
        대소문자 무시(SQLite 기본). 사람 이름은 공백 무시 매칭."""
        conds: list = []
        params: list = []
        if q.from_:
            ors = []
            for v in q.from_:
                if "@" in v:
                    ors.append("m.sender_addr LIKE ?")
                    params.append(f"%{v}%")
                else:
                    ors.append("(REPLACE(m.sender_name, ' ', '') LIKE ? "
                               "OR m.sender_addr LIKE ?)")
                    params += [f"%{v.replace(' ', '')}%", f"%{v}%"]
            conds.append("(" + " OR ".join(ors) + ")")
        for vals, col in ((q.to, "m.to_addrs"), (q.cc, "m.cc_addrs")):
            if not vals:
                continue
            ors = []
            for v in vals:
                addr = v if "@" in v else self._resolve_addr(v)
                ors.append(f"{col} LIKE ?")
                params.append(f"%{addr or v}%")
            conds.append("(" + " OR ".join(ors) + ")")
        if q.after:
            conds.append("m.sent_on >= ?"); params.append(q.after)
        if q.before:
            conds.append("m.sent_on < ?"); params.append(q.before)
        if q.thread is not None:
            conds.append("m.thread_id = ?"); params.append(q.thread)
        fl = q.is_flags
        if "unread" in fl:
            conds.append("m.read_at = ''")
        if "read" in fl:
            conds.append("m.read_at != ''")
        if "sent" in fl:
            conds.append("m.is_sent = 1")
        if "received" in fl:
            conds.append("m.is_sent = 0")
        if "flagged" in fl:
            conds.append("COALESCE(t.flagged, 0) = 1")
        if q.has_attach:
            conds.append("m.attach_names != ''")
        for f in q.files:
            conds.append("m.attach_names LIKE ?"); params.append(f"%{f}%")
        return "".join(" AND " + c for c in conds), params

    def frequent_people(self, limit: int = 200) -> list[sqlite3.Row]:
        """왕래 많은 순 사람 목록 — 검색 상세의 이름 자동완성(datalist)용."""
        return self.db.execute(
            "SELECT name, addr FROM people WHERE name != '' "
            "ORDER BY (from_count + to_count) DESC, last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def messages_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        """id 목록으로 메일 조회(순서 무관) — AI 검색 심층읽기(iv-lite)용."""
        ids = [int(i) for i in ids]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        return self.db.execute(
            f"SELECT * FROM messages WHERE id IN ({ph})", ids
        ).fetchall()

    # ---------------------------------------------------- AI 검색 캐시 (Phase 2)

    def ai_search_get(self, q: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM ai_search WHERE q=?", (q,)
        ).fetchone()

    def ai_search_put(self, q: str, raw_q: str, dsl: str,
                      result_json: str, backend: str) -> None:
        self.db.execute(
            "INSERT INTO ai_search(q, raw_q, dsl, result_json, backend, created) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(q) DO UPDATE SET "
            "raw_q=excluded.raw_q, dsl=excluded.dsl, result_json=excluded.result_json, "
            "backend=excluded.backend, created=excluded.created",
            (q, raw_q, dsl, result_json, backend, datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    # ---------------------------------------------------- 질문하기(ask) 캐시

    def ask_basis(self) -> int:
        """캐시 기준선 — 메시지가 늘면 바뀌어 옛 답이 자동 무효화된다.

        **id 가 아니라 ingest_seq 를 본다**(2026-08-11). 번호가 날짜 기반이 된 뒤로
        MAX(id) 는 '가장 나중에 **발신된**' 메일이라, 백필(더 오래된 메일을 나중에
        수집)에서는 메시지가 늘어도 값이 그대로다 — 새 메일이 왔는데 옛 답이
        유효한 채로 남는다(실측). 도착 순서는 ingest_seq 만이 안다.
        """
        row = self.db.execute("SELECT MAX(ingest_seq) m FROM messages").fetchone()
        return int(row["m"] or 0)

    def count_after(self, basis: int) -> int:
        """기준선 이후 도착한 메일 수. **두 id 의 차가 아니라 실제 개수다** —
        번호가 날짜 기반이라 차이는 개수와 무관하다(id 는 신원이지 카운터가 아니다).
        """
        return int(self.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE ingest_seq > ?",
            (int(basis or 0),)).fetchone()["c"])

    def ask_get(self, key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT rowid AS id, * FROM ask_cache WHERE key=?", (key,)).fetchone()

    def ask_by_id(self, rid: int) -> sqlite3.Row | None:
        """저장된 답변을 그대로 다시 보기 — 새 메일이 와도 그때의 답을 보존."""
        return self.db.execute(
            "SELECT rowid AS id, * FROM ask_cache WHERE rowid=?", (int(rid),)).fetchone()

    def ask_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        """질문 이력(최신순) — 같은 질문은 가장 최근 것만."""
        return self.db.execute(
            """SELECT rowid AS id, question, result_json, created FROM ask_cache
               WHERE rowid IN (SELECT MAX(rowid) FROM ask_cache GROUP BY question)
               ORDER BY created DESC, rowid DESC LIMIT ?""", (int(limit),)).fetchall()

    def ask_all(self, limit: int = 500) -> list[sqlite3.Row]:
        """전체 문답 행(최신순) — 대화(parent 체인) 묶기용. 결과 JSON 에 parent_id."""
        return self.db.execute(
            "SELECT rowid AS id, key, question, result_json, created FROM ask_cache "
            "ORDER BY created DESC, rowid DESC LIMIT ?", (int(limit),)).fetchall()

    def ask_delete(self, ids: list[int]) -> int:
        """문답 행 삭제(대화 정리) — rowid 목록. 지운 행 수를 돌려준다."""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = self.db.execute(
            f"DELETE FROM ask_cache WHERE rowid IN ({marks})",
            [int(i) for i in ids])
        self.db.commit()
        return cur.rowcount

    def ask_put(self, key: str, question: str, result_json: str,
                backend: str) -> None:
        self.db.execute(
            "INSERT INTO ask_cache(key, question, result_json, backend, created) "
            "VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "question=excluded.question, result_json=excluded.result_json, "
            "backend=excluded.backend, created=excluded.created",
            (key, question, result_json, backend,
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def ai_search_recent(self, limit: int = 10) -> list[sqlite3.Row]:
        """최근 AI 검색 목록 — 재방문·재사용용."""
        return self.db.execute(
            "SELECT q, raw_q, dsl, created FROM ai_search "
            "ORDER BY created DESC LIMIT ?", (limit,),
        ).fetchall()

    def unanswered(self, days: int = 14, max_recipients: int = 50) -> list[sqlite3.Row]:
        """미답변 스레드: 마지막 메일이 수신이고 To 에 내가 있으며 내 답장이 없는 것.

        max_recipients 이상 수신자의 단체 발송은 개인 회신 의무가 약해 제외
        (기본 50 — 20~30명 실무 메일은 포함, 그룹/팀 전체 공지만 배제).

        Outlook 에서 사라진 메일(gone_at)도 제외한다 — 열리지도 않는 메일을
        '회신 필요'로 계속 띄우는 것은 목록의 신뢰를 깎는다.
        """
        rows = self.db.execute(
            """
            SELECT t.id AS thread_id, m.subject, m.sender_name, m.sender_addr,
                   m.sent_on, m.message_id, m.to_addrs,
                   CAST(julianday('now') - julianday(m.sent_on) AS INTEGER) AS days_old
            FROM threads t
            JOIN thread_state s ON s.thread_id=t.id
            JOIN messages m ON m.id=s.latest_message_id
            WHERE (t.hidden IS NULL OR t.hidden = 0)
              AND m.is_sent = 0
              AND m.gone_at IS NULL
              AND m.sent_on >= datetime('now', ?)
            ORDER BY m.sent_on ASC
            """,
            (f"-{days} days",),
        ).fetchall()
        result = []
        for row in rows:
            tos = [a for a in (row["to_addrs"] or "").split(";") if a]
            if len(tos) < max_recipients and set(tos) & self.my_addresses:
                result.append(row)
        return result

    # ------------------------------------- Outlook 에서 사라진 메일 (유령 표시)
    # 삭제·이동은 사람의 의도라 코드가 해석하지 않는다. 다만 "Outlook 에 더 이상
    # 없다"는 **사실**은 알려야 한다 — 안 그러면 열리지도 않는 메일이 계속
    # '회신 필요'로 뜬다. 판정에서만 빼고 검색·분석에는 남긴다(내용은 여전히
    # 사실이고, 지우는 것은 별개의 명시적 동작이어야 한다).

    def set_gone(self, message_id: int, gone: bool) -> None:
        """원문 열기 성공/실패의 부산물로 기록. 추가 COM 왕복이 없다.

        되돌아오면(지운 편지함에서 복구 등) 다음 열기 성공이 알아서 지운다.
        """
        self.db.execute(
            "UPDATE messages SET gone_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds") if gone else None,
             message_id))
        self.db.commit()

    def gone_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) n FROM messages WHERE gone_at IS NOT NULL"
        ).fetchone()["n"]

    def open_thread_tails(self) -> list[sqlite3.Row]:
        """열린 스레드별 최신 메시지와 수집 시 유지한 파생값."""
        return self.db.execute(
            """
            SELECT t.id AS thread_id, t.last_date, t.rolling_summary,
                   m.id AS last_id, m.is_sent AS last_is_sent,
                   m.sender_name, m.sender_addr, m.to_addrs, m.cc_addrs,
                   m.new_content, m.subject, m.sent_on,
                   CAST(julianday('now') - julianday(m.sent_on) AS INTEGER) AS days_old,
                   s.message_count AS msg_count, s.sent_count AS my_msg_count,
                   s.addressed_to_me_count, s.deadline_count,
                   f.has_deadline AS last_has_deadline,
                   f.has_decision AS last_has_decision,
                   f.has_request AS last_has_request,
                   f.has_question AS last_has_question
            FROM threads t
            JOIN thread_state s ON s.thread_id=t.id
            JOIN messages m ON m.id=s.latest_message_id
            JOIN message_features f ON f.message_id=m.id
            WHERE (t.hidden IS NULL OR t.hidden=0)
              AND m.gone_at IS NULL
            ORDER BY m.sent_on DESC
            """
        ).fetchall()

    def action_closed_by_me_on(self, date_iso: str) -> list[dict]:
        """해당 날짜 내 실질 발신이 '열려 있던 액션 슬롯'을 종결시킨 스레드.

        thread_state 는 현재값만 저장하므로 fold_action 재생으로 판정한다 —
        대상이 그날 발신이 있는 스레드뿐이라 비용은 해당 스레드 크기 합에 비례.
        이후 새 요청으로 다시 열렸어도 '그날 종결' 사실은 유지된다(데일리
        하루 요약의 '내 활동' 근거). 반환: [{"thread_id", "subject"}] 발신순.
        """
        tids = [r["thread_id"] for r in self.db.execute(
            """SELECT DISTINCT thread_id FROM messages WHERE is_sent=1
               AND sent_on >= ? AND sent_on < date(?, '+1 day')
               ORDER BY thread_id""", (date_iso, date_iso))]
        out: list[dict] = []
        for tid in tids:
            state = dict(_EMPTY_ACTION)
            subject = ""
            closed = False
            for m in self.db.execute(
                    """SELECT m.id AS id, m.is_sent, m.sent_on,
                              m.sender_addr, m.subject, f.*
                       FROM messages m
                       JOIN message_features f ON f.message_id=m.id
                       WHERE m.thread_id=? ORDER BY m.sent_on, m.id""",
                    (tid,)):
                if not subject:
                    subject = m["subject"]
                if self._is_hard_noise(m["sender_addr"], m["subject"]):
                    continue
                was_open = bool(state["action_source_id"])
                state = fold_action(state, m)
                if (m["is_sent"] and was_open
                        and not state["action_source_id"]
                        and m["sent_on"][:10] == date_iso):
                    closed = True
            if closed:
                out.append({"thread_id": tid, "subject": subject})
        return out

    # date(sent_on)=? 는 컬럼을 함수로 감싸 idx_messages_sent_on 을 못 써 전수
    # 스캔한다. sent_on 은 'YYYY-MM-DDTHH:MM:SS' ISO 라 date 비교는 [일, 다음날)
    # 범위와 문자열상 등가 — 결과 동일하되 인덱스 범위 스캔으로 바뀐다.
    # 상한 date(?, '+1 day') 는 상수(바인드값)라 행마다가 아니라 1회 평가.
    def sent_on_date(self, date_iso: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM messages WHERE is_sent=1
               AND sent_on >= ? AND sent_on < date(?, '+1 day')
               ORDER BY sent_on""",
            (date_iso, date_iso),
        ).fetchall()

    def received_on_date(self, date_iso: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM messages WHERE is_sent=0
               AND sent_on >= ? AND sent_on < date(?, '+1 day')
               ORDER BY sent_on""",
            (date_iso, date_iso),
        ).fetchall()

    def thread_messages(self, thread_id: int) -> list[sqlite3.Row]:
        """스레드 메시지 (표시용 HTML 은 message_html 조인 — 키명 body_html 유지)."""
        return self.db.execute(
            "SELECT m.*, COALESCE(h.html, '') AS body_html "
            "FROM messages m LEFT JOIN message_html h ON h.message_id = m.id "
            "WHERE m.thread_id=? ORDER BY m.sent_on",
            (thread_id,),
        ).fetchall()

    def quote_messages(self, thread_id: int,
                       sender_addr: str | None = None) -> list[sqlite3.Row]:
        """인용 검증용 경량 메시지 조회.

        표시용 thread_messages 와 달리 message_html(인라인 이미지 포함)을 읽지 않고
        인용 출처 판정에 필요한 열만 가져온다. sender_addr 를 주면 그 사람이 직접
        발신한 수신 메일만 반환한다(인물 도시에 전용).
        """
        where = "WHERE thread_id=?"
        args: list = [thread_id]
        if sender_addr is not None:
            where += " AND is_sent=0 AND sender_addr=?"
            args.append((sender_addr or "").strip().lower())
        return self.db.execute(
            "SELECT id, thread_id, sender_addr, sender_name, is_sent, sent_on, "
            f"subject, new_content FROM messages {where} ORDER BY sent_on, id",
            args).fetchall()

    def thread(self, thread_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,)
        ).fetchone()

    def top_senders(self, limit: int = 30) -> list[sqlite3.Row]:
        """수신량 많은 발신자 (people 테이블). 차단 후보 판단용.

        from_count=이 사람→나, to_count=나→이 사람. 일방(to_count=0)·다량이
        '신경 쓸 필요 없는' 후보. 내 주소는 people 에 안 들어가므로 자동 제외.
        """
        return self.db.execute(
            """SELECT addr, name, from_count, to_count, last_seen
               FROM people WHERE from_count > 0
               ORDER BY from_count DESC, to_count ASC LIMIT ?""",
            (limit,),
        ).fetchall()

    def threads_active_on(self, date_iso: str) -> list[int]:
        rows = self.db.execute(
            "SELECT DISTINCT thread_id FROM messages "
            "WHERE sent_on >= ? AND sent_on < date(?, '+1 day')",
            (date_iso, date_iso),
        ).fetchall()
        return [r["thread_id"] for r in rows]

    def threads_active_between(self, start_iso: str, end_iso: str) -> list[int]:
        """[start, end] (양끝 포함) 활동 스레드 — 요약 '마지막 실행 이후' 창용.
        date(sent_on)<=end 은 sent_on < (end+1일) 과 등가(인덱스 범위 스캔)."""
        rows = self.db.execute(
            "SELECT DISTINCT thread_id FROM messages "
            "WHERE sent_on >= ? AND sent_on < date(?, '+1 day')",
            (start_iso, end_iso),
        ).fetchall()
        return [r["thread_id"] for r in rows]

    def get_state(self, key: str) -> str | None:
        """sync_state kv 조회 (last_harvest·daily_ai:<date> 등 범용)."""
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO sync_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    # ----------------------------------------- 폴더별 '한 번 완주했는가'
    # last_sync 는 전역 sent_on 워터마크라 증분은 그보다 새 메일만 준다. 규칙이
    # 이미 하위 폴더로 옮겨 둔 메일은 전부 그보다 과거여서, 이 표가 없으면 수집
    # 범위를 넓혀도 영영 안 들어온다. sync --full 을 강요하지 않기 위한 장치다
    # (CLAUDE.md 5). 소스가 라벨의 의미를 정하고 store 는 행만 보관한다.

    def synced_folders(self) -> set:
        """처음부터 끝까지 한 번 읽은 폴더 라벨. 없거나 깨졌으면 빈 집합."""
        from .sources.outlook_com import FOLDER_STATE_KEY, parse_folder_state
        return parse_folder_state(self.get_state(FOLDER_STATE_KEY))

    def mark_synced_folders(self, labels, in_scope=None) -> None:
        """**ingest 가 성공으로 끝난 뒤에만** 부른다.

        중간에 죽으면 다음 sync 가 그 폴더를 다시 처음부터 읽는다 — 비싸지만,
        부분 백필을 완료로 적으면 그 폴더의 나머지 메일이 영구히 누락된다.

        in_scope(이번에 실제로 연 폴더)를 주면 **범위를 벗어난 폴더를 기록에서
        뺀다** — 껐다 켜는 사이의 메일이 누락되지 않게(merge_folder_state 참고).
        """
        if not labels and not in_scope:
            return
        from .sources.outlook_com import FOLDER_STATE_KEY, merge_folder_state
        self.set_state(FOLDER_STATE_KEY, merge_folder_state(
            self.get_state(FOLDER_STATE_KEY), labels,
            datetime.now().isoformat(timespec="seconds"), keep=in_scope))

    FOLDER_VIEW_KEY = "outlook_folder_view_v1"

    def folder_view(self) -> list:
        """마지막 수집이 본 폴더 목록 — 웹 설정 화면이 COM 없이 렌더하려고 쓴다.

        건너뛴 행의 kind 는 나중에 생긴 필드라 구 값에는 없다. **버리지 않고
        사유에서 추정해 채운다** — 키를 갈아 무시했더니 사용자 화면에서 폴더
        목록이 통째로 사라졌다(2026-08-10). 다음 수집이 정확한 값으로 덮는다.
        """
        try:
            rows = json.loads(self.get_state(self.FOLDER_VIEW_KEY) or "[]")
        except ValueError:
            return []
        from .sources.outlook_com import infer_skip_kind
        out = []
        for r in rows:
            if not (isinstance(r, dict) and r.get("label")):
                continue
            if not r.get("included") and not r.get("kind"):
                r = {**r, "kind": infer_skip_kind(r.get("reason") or "")}
            out.append(r)
        return out

    def set_folder_view(self, rows) -> None:
        self.set_state(self.FOLDER_VIEW_KEY,
                       json.dumps(list(rows or []), ensure_ascii=False))

    def message(self, ref: str) -> sqlite3.Row | None:
        """숫자면 rowid, 아니면 message_id 로 조회."""
        if ref.isdigit():
            return self.db.execute(
                "SELECT * FROM messages WHERE id=?", (int(ref),)
            ).fetchone()
        return self.db.execute(
            "SELECT * FROM messages WHERE message_id=?", (ref,)
        ).fetchone()

    def recent(self, limit: int = 30, today_only: bool = False) -> list[sqlite3.Row]:
        # date(sent_on)=date('now') 와 등가지만 컬럼을 함수로 안 감싸 인덱스 범위 스캔.
        # 양변 모두 date('now')(UTC) 기준이라 결과 동일.
        where = ("WHERE sent_on >= date('now') AND sent_on < date('now', '+1 day')"
                 if today_only else "")
        return self.db.execute(
            f"SELECT * FROM messages {where} ORDER BY sent_on DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_thread_read(self, thread_id: int) -> bool:
        """스레드의 수신 메일을 '읽음'으로(웹 열람 시). 새로 읽음 처리된 게
        있으면 True. 발신(내가 보낸) 메일은 대상 아님.

        **읽음 처리할 것이 없으면 쓰지 않는다.** UPDATE 는 0행이어도 쓰기
        트랜잭션을 열어, 이미 다 읽은 스레드를 다시 여는 것만으로 sync 의 청크
        커밋과 경합했다 — 화면이 수 초 멈추고 길면 'database is locked'
        (2026-08-15 실서버 추적에서 이 UPDATE 가 7초 대기하는 것을 확인).
        탐지는 인덱스를 타는 읽기 한 번이다.

        **정말 쓸 것이 있어도 화면을 세우지는 않는다.** 열람 표시는 다음 열람에
        다시 하면 그만인 부수 효과인데, sync 가 쉬지 않고 청크를 커밋하는 동안
        기본 대기(30초)로 들어가면 굶다가 'database is locked' 로 **요청 자체를
        죽인다**(2026-08-15 실측: 미읽음 스레드 첫 열람이 30.09초 뒤 예외).
        그래서 이 쓰기에만 짧은 대기를 걸고, 못 잡으면 넘긴다 — 조용히는 말고
        `skipped_read_marks` 로 세어 둔다(운영 중 빈도를 알 수 있게)."""
        if not self.db.execute(
                "SELECT 1 FROM messages WHERE thread_id=? AND is_sent=0 "
                "AND (read_at IS NULL OR read_at='') LIMIT 1",
                (thread_id,)).fetchone():
            return False
        self.db.execute(f"PRAGMA busy_timeout={self.READ_MARK_WAIT_MS}")
        try:
            cur = self.db.execute(
                "UPDATE messages SET read_at=? "
                "WHERE thread_id=? AND is_sent=0 AND (read_at IS NULL OR read_at='')",
                (datetime.now().isoformat(timespec="seconds"), thread_id),
            )
            if cur.rowcount > 0:
                self.db.execute(
                    "UPDATE thread_state SET unread_received_count=0 "
                    "WHERE thread_id=?", (thread_id,))
            self.db.commit()
            return cur.rowcount > 0
        except sqlite3.OperationalError as e:
            if "lock" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            self.db.rollback()
            self.skipped_read_marks += 1
            return False
        finally:
            self.db.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")

    # ---------------------------------------------- 리포트 '처리함' 표시
    @staticmethod
    def report_key(*parts) -> str:
        """항목 키 — 메일 id + 문장처럼 그 항목을 특정하는 조각들로 만든다."""
        blob = "\u0000".join(str(p) for p in parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]

    def mark_report_done(self, kind: str, key: str, thread_id: int = 0,
                         label: str = "") -> None:
        """리포트 항목을 접는다 — 메일 밖(회의·구두)에서 처리한 경우."""
        self.db.execute(
            "INSERT INTO report_done(kind, key_hash, thread_id, label, done_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(kind, key_hash) DO NOTHING",
            (kind, key, thread_id, label[:120],
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def unmark_report_done(self, kind: str, key: str) -> None:
        self.db.execute("DELETE FROM report_done WHERE kind=? AND key_hash=?",
                        (kind, key))
        self.db.commit()

    def report_done_keys(self, kind: str) -> set:
        return {r["key_hash"] for r in self.db.execute(
            "SELECT key_hash FROM report_done WHERE kind=?", (kind,))}

    def report_done_list(self, kind: str, limit: int = 30) -> list:
        return self.db.execute(
            "SELECT key_hash, thread_id, label, done_at FROM report_done "
            "WHERE kind=? ORDER BY done_at DESC LIMIT ?", (kind, limit)).fetchall()

    def dismiss_signal(self, thread_id: int, kind: str) -> bool:
        """열린 액션의 신호 수동 해제 — kind: 'action'(회신 필요·확인 후보 전체)
        | 'deadline'(⏰ 만). 현재 source 메시지에 걸리므로 새 요청이 오면 자동
        복귀한다. 열린 액션이 없으면 False."""
        if kind not in ("action", "deadline"):
            return False
        row = self.db.execute(
            "SELECT action_source_id FROM thread_state WHERE thread_id=?",
            (thread_id,)).fetchone()
        if not row or not row["action_source_id"]:
            return False
        src = row["action_source_id"]
        cur = self.db.execute(
            "SELECT source_id, dismiss_action, dismiss_deadline "
            "FROM action_overrides WHERE thread_id=?", (thread_id,)).fetchone()
        da = dd = 0
        if cur and cur["source_id"] == src:      # 같은 요청 건의 기존 해제와 병합
            da, dd = cur["dismiss_action"], cur["dismiss_deadline"]
        if kind == "action":
            da = 1
        else:
            dd = 1
        self.db.execute(
            "INSERT INTO action_overrides"
            "(thread_id, source_id, dismiss_action, dismiss_deadline) "
            "VALUES (?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
            "source_id=excluded.source_id, dismiss_action=excluded.dismiss_action, "
            "dismiss_deadline=excluded.dismiss_deadline",
            (thread_id, src, da, dd))
        self.db.commit()
        return True

    def restore_signal(self, thread_id: int) -> None:
        """수동 해제 철회 — 판정이 다시 그대로 보인다."""
        self.db.execute(
            "DELETE FROM action_overrides WHERE thread_id=?", (thread_id,))
        self.db.commit()

    def set_flag(self, thread_id: int, on: bool) -> None:
        """수동 플래그(중요 표시) 설정/해제."""
        self.db.execute(
            "UPDATE threads SET flagged=? WHERE id=?", (1 if on else 0, thread_id)
        )
        self.db.commit()

    def hide_thread(self, thread_id: int, on: bool) -> None:
        """숨김 설정/해제. 숨기면 추적(미답변·개입)·메일함·스레드 기본목록과
        **AI 프롬프트 재료**(hidden_thread_ids 참조)에서 제외.
        새 수신 메일이 오면 자동 해제된다(_insert) — 놓침 방지."""
        self.db.execute(
            "UPDATE threads SET hidden=? WHERE id=?", (1 if on else 0, thread_id)
        )
        self.db.commit()

    def hidden_thread_ids(self) -> frozenset:
        """숨긴 스레드 id 집합 — **AI 프롬프트 조립 전의 공통 거름망**.

        숨김은 목록에서만 빼는 표시 축이 아니라 "조용히 하라"는 뜻이다. 그런데
        2026-08-02 점검에서 숨긴 스레드의 원문이 롤링 요약·수확·분석·AI 검색·
        인물 요약 프롬프트에 그대로 실리는 것이 확인됐다 — AI 재료를 모으는
        경로는 조립 직전에 이 집합으로 거른다(각 소비처에 조건을 흩뿌리면
        다음 AI 지점을 추가할 때 또 샌다). 예외는 사용자가 그 스레드를 직접
        지목한 온디맨드 분석 하나뿐이다(명시 의도 우선, ask.allow_tids).
        인덱스 한 번 조회라 캐시하지 않는다."""
        return frozenset(r["id"] for r in self.db.execute(
            "SELECT id FROM threads WHERE hidden=1"))

    def correspondence(self, addr: str, limit: int = 100) -> list[sqlite3.Row]:
        """이 상대와 '주고받은' 메일 전부 (양방향, 최신순).

        - 그가 나에게 보낸 것: sender_addr = addr
        - 내가 그에게 보낸 것: is_sent=1 이고 To/Cc 에 addr 포함
        to_addrs/cc_addrs 는 소문자 ';' 연결이라 양끝을 ';' 로 감싸 토큰 정확 매치.
        """
        addr = (addr or "").lower()
        like = f"%;{addr};%"
        return self.db.execute(
            """SELECT * FROM messages
               WHERE sender_addr = ?
                  OR (is_sent = 1 AND (
                       (';' || to_addrs || ';') LIKE ?
                       OR (';' || cc_addrs || ';') LIKE ?))
               ORDER BY sent_on DESC, id DESC LIMIT ?""",
            (addr, like, like, limit),
        ).fetchall()

    def person_thread_ids(self, addr: str) -> set[int]:
        """이 주소가 참여한 스레드 id 집합(양방향). 이름 매칭 카드의 동명이인
        방지용 — 이름이 같아도 이 사람과 실제 오간 스레드로 교집합한다."""
        addr = (addr or "").lower()
        like = f"%;{addr};%"
        return {r["thread_id"] for r in self.db.execute(
            """SELECT DISTINCT thread_id FROM messages
               WHERE sender_addr = ?
                  OR (is_sent = 1 AND (
                       (';' || to_addrs || ';') LIKE ?
                       OR (';' || cc_addrs || ';') LIKE ?))""",
            (addr, like, like))}

    def person_window_counts(self, window_weeks: int = 26) -> list[dict]:
        """최근 window_weeks 주 창 안 addr별 (recv, sent, last_seen) 집계 —
        인물 랜딩 순위 재료. 창은 DB 최신 메일(asof) 기준 상대(결정론·테스트 안정).
        점수 공식은 report._intensity 로 분리 — 여기선 원자료만 만든다."""
        row = self.db.execute(
            "SELECT MAX(sent_on) m FROM messages WHERE sent_on != ''").fetchone()
        if not row or not row["m"]:
            return []
        since = self.db.execute(
            "SELECT date(?, ?)", (row["m"], f"-{window_weeks * 7} days")
        ).fetchone()[0]
        agg: dict[str, list] = {}   # addr -> [recv, sent, last_seen]
        for r in self.db.execute(
                "SELECT sender_addr, sent_on FROM messages "
                "WHERE is_sent=0 AND sent_on >= ?", (since,)):
            a = (r["sender_addr"] or "").lower()
            if not a or a in self.my_addresses:
                continue
            e = agg.setdefault(a, [0, 0, ""])
            e[0] += 1
            e[2] = max(e[2], r["sent_on"])
        for r in self.db.execute(
                "SELECT to_addrs, sent_on FROM messages "
                "WHERE is_sent=1 AND sent_on >= ?", (since,)):
            for a in (r["to_addrs"] or "").split(";"):
                a = a.strip().lower()
                if not a or a in self.my_addresses:
                    continue
                e = agg.setdefault(a, [0, 0, ""])
                e[1] += 1
                e[2] = max(e[2], r["sent_on"])
        names = {r["addr"]: r["name"] for r in
                 self.db.execute("SELECT addr, name FROM people") if r["name"]}
        return [{"addr": a, "name": names.get(a, ""),
                 "recv": v[0], "sent": v[1], "last_seen": v[2]}
                for a, v in agg.items()]

    def person_sent_texts(self, addr: str, limit: int = 300,
                          window_weeks: int = 26) -> list[str]:
        """이 사람이 최근 창에 보낸 정제 본문 — AI 도시에 어휘 재료용.

        본인이 직접 쓴 것만(is_sent=0 이고 발신자=이 addr). 인용된 남의 말·내 말은
        new_content 단계에서 이미 빠져 있고, 표시부에서 strip_preserved 로 한 번 더
        보존 인용을 걷는다. 최신순 limit 통(어휘 표본 상한), 기본 창은 26주.
        숨긴 스레드 제외는 person_thread_context 와 같은 이유(AI 전용 재료)."""
        addr = (addr or "").lower()
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return []
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        return [r["new_content"] or "" for r in self.db.execute(
            "SELECT new_content FROM messages "
            "WHERE is_sent=0 AND sender_addr=? AND sent_on >= ? "
            "AND thread_id NOT IN (SELECT id FROM threads WHERE hidden=1) "
            "ORDER BY sent_on DESC LIMIT ?", (addr, since, limit))]

    def person_word_basis(self, addr: str, window_weeks: int = 26) -> dict:
        """업무 어휘 대상 기준선 — DB 최신일과 창 안 대상 메일의 최신 ID·통수."""
        addr = (addr or "").strip().lower()
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return {"window_end": "", "since": "", "basis_message_id": 0,
                    "mail_count": 0}
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        row = self.db.execute(
            """SELECT COALESCE(MAX(id), 0), COUNT(*)
               FROM messages
               WHERE is_sent=0 AND sender_addr=? AND sent_on >= ?""",
            (addr, since),
        ).fetchone()
        return {
            "window_end": latest[:10],
            "since": since,
            "basis_message_id": int(row[0]),
            "mail_count": int(row[1]),
        }

    def people_word_rows(self, addrs, window_weeks: int = 26) -> list[sqlite3.Row]:
        """업무 어휘 대조 코퍼스.

        sync 백필이 준비됐으면 compact 문장 토큰만 읽고, 준비 전에는 결과 보존을
        위해 기존 최근 본문 경로를 사용한다.
        """
        normalized = sorted({str(a).strip().lower() for a in addrs if str(a).strip()})
        if not normalized:
            return []
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return []
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        marks = ",".join("?" * len(normalized))
        self._term_features_ready = self._term_features_are_current()
        if (self._term_features_ready
                and window_weeks <= self._word_window_weeks()):
            return self.db.execute(
                f"""SELECT m.id, m.thread_id, m.sender_addr, m.sent_on,
                           f.feature_json AS term_features
                    FROM messages m
                    JOIN message_term_features f ON f.message_id=m.id
                    WHERE m.is_sent=0 AND m.sender_addr IN ({marks})
                      AND m.sent_on >= ?""",
                [*normalized, since],
            ).fetchall()
        return self.db.execute(
            f"""SELECT id, thread_id, subject, sender_name, sender_addr,
                       sent_on, new_content
                FROM messages
                WHERE is_sent=0 AND sender_addr IN ({marks}) AND sent_on >= ?""",
            [*normalized, since],
        ).fetchall()

    def person_word_bag_rows(
            self, addr: str, window_weeks: int = 26) -> list[sqlite3.Row] | None:
        """대상 인물 compact bag. 집계 백필 전에는 None으로 폴백을 지시한다."""
        self._term_bags_ready = self._term_bags_are_current()
        if (not self._term_bags_ready
                or window_weeks != self._word_window_weeks()):
            return None
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return []
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        return self.db.execute(
            """SELECT m.id, m.thread_id, m.sender_addr, m.sent_on,
                      b.body_bag_json AS term_body_bag,
                      b.subject_bag_json AS term_subject_bag
               FROM messages m
               JOIN message_term_bags b ON b.message_id=m.id
               WHERE m.is_sent=0 AND m.sender_addr=? AND m.sent_on >= ?""",
            ((addr or "").strip().lower(), since),
        ).fetchall()

    def people_word_background(
            self, addrs, target_addr: str,
            window_weeks: int = 26, candidates: dict | None = None,
            corpus_fingerprint: str = "") -> dict | None:
        """대상 후보에 대한 대조군의 정확한 메일 DF.

        eligible 전체 DF를 후보별로 지연 캐시한 뒤 대상 DF를 뺀다. 본문은 rolling
        집계, 제목은 스레드 첫 메시지의 subject-body 차집합에서만 센다.
        candidates가 없으면 호출자가 전체 원문 경로로 폴백한다.
        """
        self._term_bags_ready = self._term_bags_are_current()
        if (not self._term_bags_ready or candidates is None
                or window_weeks != self._word_window_weeks()):
            return None
        target = (target_addr or "").strip().lower()
        normalized = sorted({
            str(a).strip().lower() for a in addrs
            if str(a).strip()
        })
        if not normalized or target not in normalized:
            return {"mail_count": 0, "term_df": Counter(),
                    "phrase_df": Counter()}
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return {"mail_count": 0, "term_df": Counter(),
                    "phrase_df": Counter()}
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        fingerprint = (
            corpus_fingerprint
            or self.people_word_corpus_fingerprint(normalized, window_weeks)
        )
        cache_key = (
            tuple(normalized), int(window_weeks), fingerprint,
            self._term_bag_version(),
        )
        cached = self._word_background_cache.get(cache_key)
        marks = ",".join("?" * len(normalized))
        if cached is None:
            if len(self._word_background_cache) >= 4:
                self._word_background_cache.pop(
                    next(iter(self._word_background_cache)))
            mail_count = self.db.execute(
                f"""SELECT COUNT(*) FROM messages
                    WHERE is_sent=0 AND sender_addr IN ({marks})
                      AND sent_on >= ?""",
                [*normalized, since],
            ).fetchone()[0]
            cached = {"mail_count": int(mail_count), "df": {}}
            self._word_background_cache[cache_key] = cached

        wanted = {
            ("term", str(term)) for term in candidates.get("terms") or ()
        }
        wanted.update(
            ("phrase", self._phrase_term(phrase))
            for phrase in candidates.get("phrases") or ())
        missing = wanted - set(cached["df"])
        if missing:
            self.db.execute(
                """CREATE TEMP TABLE IF NOT EXISTS word_term_candidates (
                     kind TEXT NOT NULL,
                     term TEXT NOT NULL,
                     PRIMARY KEY (kind, term)
                   ) WITHOUT ROWID""")
            self.db.execute("DELETE FROM word_term_candidates")
            self.db.executemany(
                "INSERT INTO word_term_candidates(kind, term) VALUES (?, ?)",
                sorted(missing))
            values = {key: 0 for key in missing}
            for row in self.db.execute(
                f"""SELECT d.kind, d.term, d.mail_df AS n
                    FROM word_term_candidates c
                    CROSS JOIN person_term_window d
                    WHERE d.kind=c.kind AND d.term=c.term
                      AND d.sender_addr IN ({marks})""",
                normalized,
            ):
                values[(row["kind"], row["term"])] += int(row["n"])

            # ranked는 현재 창 안 sender/thread 첫 메시지만 남긴다. delta 테이블은
            # subject-body 차집합이라 본문과 제목이 겹쳐도 메일 DF를 한 번만 센다.
            for row in self.db.execute(
                f"""WITH ranked AS (
                        SELECT m.id,
                               ROW_NUMBER() OVER (
                                 PARTITION BY m.sender_addr, m.thread_id
                                 ORDER BY m.sent_on, m.id
                               ) AS rn
                        FROM messages m
                        WHERE m.is_sent=0
                          AND m.sender_addr IN ({marks})
                          AND m.sent_on >= ?
                    )
                    SELECT sd.kind, sd.term
                    FROM ranked r
                    JOIN message_term_subject_delta sd
                      ON sd.message_id=r.id
                    JOIN word_term_candidates c
                      ON c.kind=sd.kind AND c.term=sd.term
                    WHERE r.rn=1""",
                [*normalized, since],
            ):
                values[(row["kind"], row["term"])] += 1
            cached["df"].update(values)

        term_df, phrase_df = Counter(), Counter()
        own_terms = Counter(candidates.get("term_df") or {})
        own_phrases = Counter(candidates.get("phrase_df") or {})
        for kind, encoded in wanted:
            total = int(cached["df"].get((kind, encoded), 0))
            if kind == "term":
                term_df[encoded] = max(0, total - own_terms[encoded])
            else:
                pair = tuple(encoded.split("\x1f", 1))
                if len(pair) == 2:
                    phrase_df[pair] = max(
                        0, total - own_phrases[pair])
        return {
            "mail_count": max(
                0, int(cached["mail_count"])
                - int(candidates.get("mail_count", 0))),
            "term_df": term_df,
            "phrase_df": phrase_df,
        }

    def people_word_corpus_fingerprint(
            self, addrs, window_weeks: int = 26) -> str:
        """현재 분석 창 대조 메일 집합의 안정적인 내용 지문.

        새 대조 메일과 창 밖으로 빠진 메일을 모두 감지한다. 날짜 자체가 아니라
        실제 집합이 바뀔 때만 최종 프로필 캐시를 무효화한다.
        """
        normalized = sorted({str(a).strip().lower() for a in addrs if str(a).strip()})
        if not normalized:
            return "-"
        latest = self.db.execute(
            "SELECT MAX(sent_on) FROM messages WHERE sent_on != ''").fetchone()[0]
        if not latest:
            return "-"
        since = self.db.execute(
            "SELECT date(?, ?)", (latest, f"-{window_weeks * 7} days")
        ).fetchone()[0]
        marks = ",".join("?" * len(normalized))
        digest = hashlib.sha256()
        count = 0
        for row in self.db.execute(
            f"""SELECT id
                FROM messages
                WHERE is_sent=0 AND sender_addr IN ({marks}) AND sent_on >= ?
                ORDER BY id""",
            [*normalized, since],
        ):
            digest.update(int(row["id"]).to_bytes(8, "big", signed=False))
            count += 1
        return f"{count}:{digest.hexdigest()}"

    def people_word_profile(self, addr: str, basis: dict, window_weeks: int,
                            feature_version: str) -> dict | None:
        """현재 기준선과 정확히 맞는 업무 어휘 파생 캐시를 읽는다."""
        row = self.db.execute(
            """SELECT profile_json FROM people_word_profiles
               WHERE addr=? AND basis_message_id=?
                 AND window_weeks=? AND feature_version=?""",
            ((addr or "").strip().lower(), basis["basis_message_id"],
             window_weeks, feature_version),
        ).fetchone()
        if not row or not row["profile_json"]:
            return None
        try:
            value = json.loads(row["profile_json"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def save_people_word_profile(self, addr: str, profile: dict, basis: dict,
                                 window_weeks: int,
                                 feature_version: str) -> None:
        """업무 어휘 파생 결과 저장. 기존 메일 원문은 캐시에 복제하지 않는다."""
        self.db.execute(
            """INSERT INTO people_word_profiles
               (addr, profile_json, basis_message_id, window_end, window_weeks,
                feature_version, updated)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(addr) DO UPDATE SET
                 profile_json=excluded.profile_json,
                 basis_message_id=excluded.basis_message_id,
                 window_end=excluded.window_end,
                 window_weeks=excluded.window_weeks,
                 feature_version=excluded.feature_version,
                 updated=excluded.updated""",
            ((addr or "").strip().lower(),
             json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
             basis["basis_message_id"], basis["window_end"], window_weeks,
             feature_version),
        )
        self.db.commit()

    def person_msg_count(self, addr: str, since_seq: int = 0) -> int:
        """이 사람 관련 메시지 수(양방향) — 도시에 증분 갱신 가드(basis)용.

        since_seq 를 주면 그 워터마크 **뒤에 도착한** 것만 센다(심층 분석 카드의
        '이후 새 메일 N통'). 도착 순서는 `ingest_seq` 만이 안다 — `id` 는 메일의
        시각에서 계산한 신원이라 백필분이 워터마크 아래에 꽂힌다(불변식 8).
        """
        addr = (addr or "").lower()
        like = f"%;{addr};%"
        sql = ("SELECT COUNT(*) FROM messages WHERE (sender_addr=? "
               "OR (is_sent=1 AND ((';'||to_addrs||';') LIKE ? "
               "OR (';'||cc_addrs||';') LIKE ?)))")
        args: list = [addr, like, like]
        if since_seq:
            sql += " AND ingest_seq > ?"
            args.append(int(since_seq))
        return self.db.execute(sql, args).fetchone()[0]

    def _my_like(self) -> tuple[str, list]:
        """내 주소 중 하나가 목록에 있는지 보는 SQL 조각 — (조건문, 인자)."""
        addrs = sorted(self.my_addresses)
        if not addrs:
            return "0", []
        return (" OR ".join(["(';'||{}||';') LIKE ?"] * len(addrs)),
                [f"%;{a};%" for a in addrs])

    def person_relation(self, addr: str) -> dict:
        """이 사람과 나의 관계 — **코드가 세는 값만**(프로필 AI 재료, 2026-08-18).

        프로필 재료를 '그 사람이 쓴 발췌'로만 주면 모델이 발췌를 옮겨 적는다.
        누가 누구에게 직접 걸었고 내가 얼마나 답했는지는 세면 나오는 사실이라
        코드가 세어 주고, 모델은 그 위에 해석만 얹는다(인용 검증이 필요 없는
        재료라 판단 문장의 근거로 쓸 수 있다).

        `to_me` 는 **받는 사람(To)** 에 내가 든 것만이다 — 참조로만 도는 관계와
        나에게 직접 거는 관계는 다른 관계이고, 그 차이가 프로필의 핵심이다.
        """
        addr = (addr or "").strip().lower()
        out = {"to_me": 0, "cc_only": 0, "from_me": 0, "threads": 0,
               "replied_threads": 0, "they_started": 0, "i_started": 0,
               "first": "", "last": ""}
        if not addr:
            return out
        to_cond, to_args = self._my_like()
        cc_cond, cc_args = self._my_like()
        row = self.db.execute(
            f"""SELECT
                 SUM(CASE WHEN {to_cond.format(*(['to_addrs'] * len(to_args)))}
                          THEN 1 ELSE 0 END) AS to_me,
                 SUM(CASE WHEN NOT ({to_cond.format(*(['to_addrs'] * len(to_args)))})
                           AND ({cc_cond.format(*(['cc_addrs'] * len(cc_args)))})
                          THEN 1 ELSE 0 END) AS cc_only,
                 MIN(sent_on) AS first, MAX(sent_on) AS last
               FROM messages WHERE is_sent=0 AND sender_addr=?""",
            [*to_args, *to_args, *cc_args, addr]).fetchone()
        out["to_me"] = int(row["to_me"] or 0)
        out["cc_only"] = int(row["cc_only"] or 0)
        out["first"] = (row["first"] or "")[:10]
        out["last"] = (row["last"] or "")[:10]
        like = f"%;{addr};%"
        out["from_me"] = int(self.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE is_sent=1 "
            "AND ((';'||to_addrs||';') LIKE ? OR (';'||cc_addrs||';') LIKE ?)",
            (like, like)).fetchone()["c"])
        tids = self.person_thread_ids(addr)
        out["threads"] = len(tids)
        if not tids:
            return out
        marks = ",".join("?" * len(tids))
        out["replied_threads"] = int(self.db.execute(
            f"SELECT COUNT(DISTINCT thread_id) c FROM messages "
            f"WHERE is_sent=1 AND thread_id IN ({marks})",
            list(tids)).fetchone()["c"])
        # 누가 먼저 거나 — 스레드의 첫 메일 발신자. 대화의 방향을 한 값으로 본다.
        for r in self.db.execute(
                f"""SELECT thread_id, sender_addr, is_sent FROM messages m
                    WHERE thread_id IN ({marks}) AND m.id = (
                      SELECT id FROM messages x WHERE x.thread_id = m.thread_id
                      ORDER BY x.sent_on, x.id LIMIT 1)""", list(tids)):
            if (r["sender_addr"] or "").lower() == addr:
                out["they_started"] += 1
            elif r["is_sent"]:
                out["i_started"] += 1
        return out

    def person_cohorts(self, addr: str, limit: int = 3,
                       min_threads: int = 2) -> list[dict]:
        """이 사람과 **자주 같은 스레드에 있는** 사람들 — 많은 순(결정론, AI 0콜).

        세는 단위는 메시지가 아니라 **스레드**다: 한 스레드에서 열 번 말한 사람이
        다섯 스레드에 한 번씩 나온 사람보다 가깝지는 않다. 스레드가 하나뿐이면
        우연이라 `min_threads` 로 자른다.

        말한 사람만 센다(수신인은 안 센다) — 전사 공지의 수신자 200명이 '함께
        도는 사람'이 되면 값이 0이다. 봇·자동발송은 인물 랭킹과 같은 기준으로
        빼고(`is_noise_sender_hard`), 숨긴 스레드는 통째로 뺀다.
        """
        addr = (addr or "").strip().lower()
        tids = self.person_thread_ids(addr) - self.hidden_thread_ids()
        if not addr or not tids:
            return []
        marks = ",".join("?" * len(tids))
        rows = self.db.execute(
            f"""SELECT sender_addr a, COUNT(DISTINCT thread_id) n
                FROM messages
                WHERE thread_id IN ({marks}) AND is_sent=0 AND sender_addr != ?
                GROUP BY a ORDER BY n DESC, a""", [*tids, addr]).fetchall()
        out = []
        for r in rows:
            if r["n"] < min_threads:
                break                      # 정렬돼 있으므로 여기서 끝
            if self._noise is not None and self._noise.is_noise_sender_hard(r["a"]):
                continue
            out.append({"addr": r["a"], "name": self.person_name(r["a"]) or r["a"],
                        "threads": int(r["n"])})
            if len(out) >= limit:
                break
        return out

    def person_thread_context(self, addr: str, limit: int = 8,
                              excerpts_per_thread: int = 2) -> list[dict]:
        """도시에 AI 재료 — 대상 인물이 직접 쓴 발췌 + 내 회신(문맥).

        롤링 요약은 문맥 전용이고 인용 근거는 sender_addr=addr 인 수신 메시지의
        신규 작성분(strip_preserved)으로 제한한다. 스레드당 최근 N통만 제공해
        프롬프트 크기를 바운드한다. 숨긴 스레드는 SQL 에서 뺀다 — 이 함수는
        AI 프롬프트 전용 재료라, 호출자가 거르기를 잊을 여지를 안 남긴다
        (2026-08-02: 숨긴 대화가 인물 요약에 실리던 구멍).

        **고르는 순서가 관계 순서다**(2026-08-18, 사용자 요구). 나를 받는
        사람(To)에 넣어 보낸 스레드와 내가 답장한 스레드가 먼저다 — 참조로만
        돌던 공지가 앞자리를 차지하면 프로필이 '그 사람이 쓴 문장 모음'이 된다.
        각 스레드에 내 회신 한 조각을 문맥으로 함께 싣는다(인용 근거는 아니다.
        관계는 한쪽 발화만 봐서는 안 보인다).
        """
        addr = (addr or "").strip().lower()
        to_cond, to_args = self._my_like()
        rows = self.db.execute(
            f"""SELECT t.id, t.rolling_summary, t.last_date,
                       MAX(CASE WHEN m.is_sent=0 AND m.sender_addr=?
                                 AND ({to_cond.format(*(['m.to_addrs'] * len(to_args)))})
                                THEN 1 ELSE 0 END) AS direct,
                       MAX(CASE WHEN m.is_sent=1 THEN 1 ELSE 0 END) AS mine
                FROM threads t JOIN messages m ON m.thread_id=t.id
                WHERE t.hidden=0 AND t.id IN (
                  SELECT thread_id FROM messages
                  WHERE is_sent=0 AND sender_addr=?)
                GROUP BY t.id
                ORDER BY (direct + mine) DESC, t.last_date DESC LIMIT ?""",
            [addr, *to_args, addr, limit]).fetchall()
        if not rows:
            return []
        tids = [r["id"] for r in rows]
        marks = ",".join("?" * len(tids))
        msgs = self.db.execute(
            f"""SELECT id, thread_id, subject, new_content, sent_on
                FROM messages
                WHERE thread_id IN ({marks}) AND is_sent=0 AND sender_addr=?
                ORDER BY sent_on DESC, id DESC""",
            [*tids, addr]).fetchall()
        replies = self.db.execute(
            f"""SELECT thread_id, new_content, sent_on FROM messages
                WHERE thread_id IN ({marks}) AND is_sent=1
                ORDER BY sent_on DESC, id DESC""", tids).fetchall()
        mine: dict[int, str] = {}
        for m in replies:
            text = strip_preserved(m["new_content"] or "").strip()
            if text:
                mine.setdefault(m["thread_id"], text)
        by_tid: dict[int, list[dict]] = {tid: [] for tid in tids}
        subjects: dict[int, str] = {}
        for m in msgs:
            tid = m["thread_id"]
            subjects.setdefault(tid, m["subject"] or "(제목 없음)")
            if len(by_tid[tid]) >= max(1, excerpts_per_thread):
                continue
            text = strip_preserved(m["new_content"] or "").strip()
            if text:
                by_tid[tid].append({"message_id": m["id"], "text": text})
        out = []
        for r in rows:
            excerpts = by_tid[r["id"]]
            if not excerpts:
                continue
            out.append({"thread_id": r["id"],
                        "subject": subjects.get(r["id"], "(제목 없음)"),
                        "summary": (r["rolling_summary"] or "").strip(),
                        "direct": bool(r["direct"]), "replied": bool(r["mine"]),
                        "my_reply": mine.get(r["id"], ""),
                        "excerpts": excerpts})
        return out

    def people_dossier(self, addr: str,
                       include_stale: bool = False) -> sqlite3.Row | None:
        where = "addr=?"
        args: list = [(addr or "").lower()]
        if not include_stale:
            where += " AND validator_version=?"
            args.append(DOSSIER_VALIDATOR_VERSION)
        return self.db.execute(
            f"SELECT * FROM people_dossier WHERE {where}", args).fetchone()

    def save_people_dossier(self, addr: str, dossier_md: str,
                            basis_msg_count: int,
                            validator_version: int = DOSSIER_VALIDATOR_VERSION) -> None:
        self.db.execute(
            """INSERT INTO people_dossier
               (addr, dossier_md, updated, basis_msg_count, validator_version)
               VALUES (?,?,datetime('now'),?,?)
               ON CONFLICT(addr) DO UPDATE SET
                 dossier_md=excluded.dossier_md, updated=excluded.updated,
                 basis_msg_count=excluded.basis_msg_count,
                 validator_version=excluded.validator_version""",
            ((addr or "").lower(), dossier_md or "", basis_msg_count,
             validator_version))
        self.db.commit()

    def mark_people_dossier_checked(
            self, addr: str, basis_msg_count: int,
            validator_version: int = DOSSIER_VALIDATOR_VERSION) -> None:
        """AI 호출 불필요/검증 0건을 처리 완료로 기록해 같은 입력 재호출을 막는다.

        현재 검증 버전의 유효 카드는 보존한다. 구버전 카드는 잘못된 발화자 근거를
        포함할 수 있으므로 내용·갱신일을 비우고 현재 버전의 빈 행으로 전환한다.
        """
        addr = (addr or "").lower()
        row = self.people_dossier(addr, include_stale=True)
        if row and row["validator_version"] == validator_version:
            self.db.execute(
                "UPDATE people_dossier SET basis_msg_count=? WHERE addr=?",
                (basis_msg_count, addr))
        else:
            self.db.execute(
                """INSERT INTO people_dossier
                   (addr, dossier_md, updated, basis_msg_count, validator_version)
                   VALUES (?,'','',?,?)
                   ON CONFLICT(addr) DO UPDATE SET
                     dossier_md='', updated='',
                     basis_msg_count=excluded.basis_msg_count,
                     validator_version=excluded.validator_version""",
                (addr, basis_msg_count, validator_version))
        self.db.commit()

    def dossier_roles(self) -> dict[str, str]:
        """addr → 한 줄(첫 불릿의 서술) — 랜딩 목록 표시용.

        슬롯 첫머리가 '한 줄'(이 사람이 나에게 어떤 상대인가)이라 목록에도 그
        문장이 간다(2026-08-18). 헤더는 건너뛰고 첫 불릿의 서술만 뽑는다.
        """
        out = {}
        for r in self.db.execute(
                """SELECT addr, dossier_md FROM people_dossier
                   WHERE dossier_md!='' AND validator_version=?""",
                (DOSSIER_VALIDATOR_VERSION,)):
            for ln in (r["dossier_md"] or "").splitlines():
                s = ln.strip()
                if not s or s.startswith("##"):
                    continue
                s = s.lstrip("-* ").strip()
                if s.startswith("[#"):
                    j = s.find("] ")
                    if j != -1:
                        s = s[j + 2:].strip()
                if s:
                    out[r["addr"]] = s if len(s) <= 60 else s[:59] + "…"
                    break
        return out

    def person_name(self, addr: str) -> str:
        """이 주소의 표시 이름(people 우선, 없으면 메일 발신명). 없으면 ''."""
        addr = (addr or "").lower()
        row = self.db.execute(
            "SELECT name FROM people WHERE addr=?", (addr,)
        ).fetchone()
        if row and row["name"]:
            return row["name"]
        row = self.db.execute(
            "SELECT sender_name FROM messages WHERE sender_addr=? AND sender_name!='' "
            "ORDER BY sent_on DESC LIMIT 1", (addr,)
        ).fetchone()
        return row["sender_name"] if row and row["sender_name"] else ""

    def display_names(self, addrs) -> dict:
        """주소 → 표시 이름. 이름이 없는 주소는 **키 자체가 없다**.

        화면 하나에 나오는 주소 전부를 한 질의로 가져온다 — person_name() 은
        주소당 2질의라 스레드 렌더(12통 × 수신 5명)에서 100질의를 넘긴다.
        people 하나만 보는 것은 우연이 아니다: _update_people 이 이름을 쓰는 곳이
        '나에게 메일을 보낸 사람' 뿐이라 messages.sender_name 을 또 뒤져도 새로
        나오는 이름이 없다.
        """
        want = sorted({(a or "").strip().lower() for a in addrs if (a or "").strip()})
        out = {}
        for i in range(0, len(want), 400):     # SQLite 변수 상한(999) 회피
            chunk = want[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.db.execute(
                    "SELECT addr, name FROM people "
                    f"WHERE name != '' AND addr IN ({marks})", chunk):
                out[r["addr"]] = r["name"]
        return out

    # -------------------------------------------------- 수확 신호

    def add_signal(self, date_iso: str, kind: str, who: str,
                   thread_id: int | None, signal: str, quote: str = "") -> None:
        """인물/프로젝트 신호 적재 — Phase 2 주간 증류의 재료.

        같은 날·같은 축·같은 스레드·**같은 대상**에서 같은 인용이 다시 오면 넣지
        않는다(2026-08-25). 종전에는 워터마크가 같은 메일을 두 번 안 보내 준다는
        전제로 무조건 INSERT 했는데, 플래그 스레드를 시간 앞머리 밖에서도 싣게
        되면서 그 전제가 깨졌다(distill._harvest_items).

        열쇠에 인용을 넣는 이유: 같은 메일을 다시 읽으면 서술(signal)은 조금씩
        달라져도 근거 문장은 같다. **who 를 함께 넣는 이유**: 한 문장에서 두
        사람의 신호가 나올 수 있어(「A 가 B 에게 …를 넘겼습니다」), who 를 빼면
        둘째 사람이 조용히 사라진다 — 중복을 막으려다 없는 것을 만드는 쪽이
        더 나쁘다. who 표기가 흔들리면 비슷한 줄이 하나 더 생길 뿐이다.
        인용이 없는 줄은 대조할 것이 없어 그대로 넣는다.
        """
        if quote:
            dup = self.db.execute(
                "SELECT 1 FROM distill_signals WHERE date=? AND kind=? "
                "AND who=? AND thread_id IS ? AND quote=?",
                (date_iso, kind, who or "", thread_id, quote)).fetchone()
            if dup:
                return
        self.db.execute(
            """INSERT INTO distill_signals
                 (date, kind, who, thread_id, signal, quote, created)
               VALUES (?,?,?,?,?,?,datetime('now'))""",
            (date_iso, kind, who or "", thread_id, signal or "", quote or ""))
        self.db.commit()

    def person_signals(self, addr: str, name: str = "",
                       limit: int = 20) -> list[sqlite3.Row]:
        """이 사람의 축적된 인물 신호(역할·담당 변경 등) — 도시에 '최근 변화'.

        수확이 distill_signals 에 쌓지만 읽는 곳이 없던 것을 여기서 처음 소비한다.
        동명이인 방지: 이 addr 참여 스레드로 교집합(+ 이름 매치 보조)."""
        tids = self.person_thread_ids(addr)
        if not tids:
            return []
        marks = ",".join("?" * len(tids))
        args = list(tids)
        name_cond = ""
        if name:
            name_cond = " AND who LIKE ?"
            args.append(f"%{name}%")
        args.append(limit)
        return self.db.execute(
            f"SELECT * FROM distill_signals WHERE kind='person' "
            f"AND thread_id IN ({marks}){name_cond} "
            f"ORDER BY date DESC, id DESC LIMIT ?", args).fetchall()

    _STRIP_MARK = "<div class='imgstrip'>"

    def maybe_prune_html(self, retain_days: int) -> tuple[int, int] | None:
        """sync 종료 훅 — 하루 1회만 실제 프룬. (마커 n, 삭제 n) 또는 None(스킵).

        retain_days <= 0 이면 기능 끔(임베드도 프룬도 안 함 — 현행 유지).
        건너뛴 날은 다음 실행이 경과일 기준으로 한 번에 처리(누락 없음).
        가드는 '같은 날 + 같은 설정값'일 때만 — 보존 기간을 바꾸면 그날이라도
        다음 sync 에서 즉시 반영된다 (PC 스모크 피드백, 2026-07-13).
        """
        # 재절단 백업 만료는 이미지 보존 설정과 무관하다 — retain_days=0
        # ('임베드 끔'은 지원 옵션)인 사용자가 백업을 영구 보유하던 버그.
        # **진행 중 트랜잭션이 있으면 건너뛴다**: 이 함수는 sync 의 finally 에서
        # 불리는데, ingest 의 롤백은 except Exception 이라 KeyboardInterrupt 를
        # 잡지 않는다. 여기서 commit 하면 남의 미완 청크가 함께 커밋돼 어휘
        # 파생 없는 메일이 영구히 남는다(2026-07-31 리뷰 실증: 150통 중단 시
        # 39통 누락). 다음 sync 가 만료를 이어받으므로 미루면 그만이다.
        if self.db.in_transaction:
            # 이 함수는 sync 의 finally 에서 불린다. ingest 의 롤백은
            # except Exception 이라 KeyboardInterrupt 를 잡지 않아, 중단 시
            # 미완 청크가 열린 채로 여기 온다 — 여기서 commit 하면 남의 미완
            # 작업이 함께 커밋돼 어휘 파생 없는 메일이 영구히 남는다
            # (2026-07-31 리뷰 실증: 150통 중단 시 39통 누락, 재수집으로도
            #  복구 불가). 프룬·만료는 다음 sync 가 이어받으면 그만이다.
            return None
        freed = self._expire_reclean_backup()
        self.db.commit()
        if retain_days <= 0:
            if freed:
                self.db.execute("PRAGMA incremental_vacuum").fetchall()
                self.db.commit()
            return None
        today = datetime.now().date().isoformat()
        stamp = f"{today}:{retain_days}"
        if self.get_state("last_image_prune") == stamp:
            return None
        n_mark, n_del = self._prune_html(retain_days)
        self.set_state("last_image_prune", stamp)
        if n_mark or n_del or freed:
            # 조각 회수 — 풀 VACUUM(배타 수십 초) 금지, auto_vacuum=INCREMENTAL 전제.
            # **fetchall 필수**: PRAGMA 는 결과를 소비해야 실제로 회수한다
            # (안 돌리면 한 페이지만 반환되고 끝난다 — 2026-07-31 실측:
            #  20.5MB→20.5MB vs 20.5MB→0.01MB).
            self.db.execute("PRAGMA incremental_vacuum").fetchall()
            self.db.commit()
        return n_mark, n_del

    def _expire_reclean_backup(self) -> int:
        """재절단 백업의 보존 창 — 오래된 행을 지운다.

        절단 오탐은 며칠 안에 눈에 띄고(diagnose 의 '절단 실패 의심'·본문 확인),
        무한 보존은 DB 를 부풀린다(3만통 기준 35.8MB 실측). 되돌릴 기회는
        RECLEAN_BACKUP_DAYS 동안 준다."""
        cutoff = (datetime.now()
                  - timedelta(days=RECLEAN_BACKUP_DAYS)).isoformat(
                      timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM reclean_backup WHERE created != '' AND created < ?",
            (cutoff,))
        return cur.rowcount or 0

    def _prune_html(self, retain_days: int) -> tuple[int, int]:
        """retain_days 경과 메일의 message_html 압축 — (마커 전환 n, 삭제 n)."""
        cutoff = (datetime.now() - timedelta(days=retain_days)).date().isoformat()
        n_mark = n_del = 0
        rows = self.db.execute(
            "SELECT h.message_id AS mid, h.html FROM message_html h "
            "JOIN messages m ON m.id = h.message_id "
            "WHERE substr(m.sent_on, 1, 10) < ?", (cutoff,)).fetchall()
        for r in rows:
            html = r["html"] or ""
            if html.startswith(self._STRIP_MARK):
                continue                      # 이미 마커 — 재프룬 금지
            # 임베드분 + 미임베드 cid 흔적(컷오프 게이트로 건너뛴 백필 메일)
            # 둘 다 '이미지 있었음' — 마커로 흔적을 남긴다
            n_img = (html.count("data:image/")
                     + html.count('data-blocked-src="cid:'))
            if n_img:
                marker = (f"{self._STRIP_MARK}🖼 이미지 {n_img}장 — "
                          f"보존 기간({retain_days}일) 경과, 원본은 Outlook에서"
                          "</div>")
                self.db.execute(
                    "UPDATE message_html SET html=? WHERE message_id=?",
                    (marker, r["mid"]))
                n_mark += 1
            else:
                self.db.execute(
                    "DELETE FROM message_html WHERE message_id=?", (r["mid"],))
                n_del += 1
        self.db.commit()
        return n_mark, n_del

    def save_summary(self, thread_id: int, summary: str, msg_count: int) -> None:
        self.db.execute(
            """UPDATE threads SET rolling_summary=?, summary_msg_count=?,
               summary_updated=datetime('now') WHERE id=?""",
            (summary, msg_count, thread_id),
        )
        self.db.commit()

    # ────────────────── 스레드 노트 색인 (2026-08-11)
    # 원본은 vault/notes/*.md — 여기는 검색·AI 문맥용 미러. 쓰기는 notes.reindex
    # 한 곳에서만 온다(파일 → DB 단방향).

    def note_row(self, thread_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM notes WHERE thread_id=?", (thread_id,)).fetchone()

    def notes_all(self) -> list[sqlite3.Row]:
        """AI 문맥 선정용 전체 목록 — 노트는 사람이 손으로 만드는 것이라 소량."""
        return self.db.execute(
            "SELECT n.*, t.norm_subject FROM notes n "
            "LEFT JOIN threads t ON t.id = n.thread_id "
            "ORDER BY n.mtime DESC").fetchall()

    def noted_thread_ids(self) -> frozenset:
        """노트가 있는 스레드 — 목록 배지용."""
        return frozenset(r["thread_id"] for r in self.db.execute(
            "SELECT thread_id FROM notes"))

    def index_note(self, thread_id: int, path: str, mtime: float,
                   content: str) -> None:
        self.db.execute(
            "INSERT INTO notes(thread_id, path, mtime, content) VALUES(?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET path=excluded.path, "
            "mtime=excluded.mtime, content=excluded.content",
            (thread_id, path, mtime, content))
        self.db.commit()

    def prune_notes(self, keep: set) -> int:
        """파일이 사라진 색인 제거 — 파일 삭제는 사람의 결정이고 색인은 따라간다."""
        gone = [r["thread_id"] for r in
                self.db.execute("SELECT thread_id FROM notes")
                if r["thread_id"] not in keep]
        for tid in gone:
            self.db.execute("DELETE FROM notes WHERE thread_id=?", (tid,))
        if gone:
            self.db.commit()
        return len(gone)

    def search_notes(self, query: str, limit: int = 10) -> list[dict]:
        """노트 본문 검색 — 검색 화면의 '내 노트' 절용.

        FTS 를 쓰지 않는 이유: 노트는 사람이 손으로 만드는 파일이라 많아야 수백
        건이고, trigram 토크나이저는 2자 검색어('캐시')를 아예 매칭하지 못한다
        — 메일 검색이 단계적 LIKE 폴백을 두는 이유가 그 한계다(2026-08-11).
        파이썬 부분일치(AND·대소문자 무시)가 더 단순하고 빈틈이 없다.
        메일 DSL(from:·after:)은 노트에 의미가 없어 텍스트 항만 본다. 숨긴
        스레드의 노트는 뺀다 — '숨김 = 목록·추적 제외' 계약을 노트도 따른다."""
        terms = [t.lower() for t in re.split(r"[^0-9A-Za-z가-힣]+", query or "")
                 if len(t) >= 2]
        if not terms:
            return []
        out: list[dict] = []
        for r in self.db.execute(
                "SELECT n.thread_id, n.path, n.content, "
                "t.norm_subject AS subject FROM notes n "
                "LEFT JOIN threads t ON t.id = n.thread_id "
                "WHERE (t.hidden IS NULL OR t.hidden=0) "
                "ORDER BY n.mtime DESC"):
            content = r["content"] or ""
            low = content.lower()
            if not all(t in low for t in terms):
                continue
            i = low.find(terms[0])
            j = i + len(terms[0])
            snip = " ".join((("…" if i > 30 else "") + content[max(0, i - 30):i]
                             + "⟪" + content[i:j] + "⟫"
                             + content[j:j + 60] + "…").split())
            out.append({"thread_id": r["thread_id"], "path": r["path"],
                        "subject": r["subject"], "snippet": snip})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------- 암묵지 (후보 큐 · md 색인)

    def add_knowledge_candidate(self, date_iso: str, title: str, body: str,
                                threads: str, quote: str,
                                source: str = "daily") -> int | None:
        """수확이 캐낸 암묵지 후보 적재. 같은 제목이 이미 살아 있으면(pending/
        saved) 중복으로 보고 None — 매일 도는 수확이 같은 지식을 다시 캘 수 있다.
        dismissed 와는 대조하지 않는다: 사람이 버린 것과 같은 제목이라도 새 근거로
        다시 올라오는 것은 막지 않는다(유보는 영구 거부가 아니다)."""
        dup = self.db.execute(
            "SELECT 1 FROM knowledge_candidates WHERE title=? "
            "AND status IN ('pending', 'saved')", (title,)).fetchone()
        if dup:
            return None
        cur = self.db.execute(
            "INSERT INTO knowledge_candidates"
            "(date, source, title, body, threads, quote, created) "
            "VALUES (?,?,?,?,?,?,?)",
            (date_iso, source, title, body, threads, quote,
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        return cur.lastrowid

    def knowledge_candidates(self, status: str = "pending",
                             date_iso: str = "") -> list[sqlite3.Row]:
        q = "SELECT * FROM knowledge_candidates WHERE status=?"
        args: list = [status]
        if date_iso:
            q += " AND date=?"
            args.append(date_iso)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    def knowledge_candidate(self, cid: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM knowledge_candidates WHERE id=?", (cid,)).fetchone()

    def set_knowledge_status(self, cid: int, status: str,
                             path: str = "") -> None:
        self.db.execute(
            "UPDATE knowledge_candidates SET status=?, path=? WHERE id=?",
            (status, path, cid))
        self.db.commit()

    def index_knowledge(self, path: str, title: str, threads: str,
                        mtime: float, content: str) -> None:
        self.db.execute(
            "INSERT INTO knowledge(path, title, threads, mtime, content) "
            "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "title=excluded.title, threads=excluded.threads, "
            "mtime=excluded.mtime, content=excluded.content",
            (path, title, threads, mtime, content))
        self.db.commit()

    def knowledge_row(self, path: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM knowledge WHERE path=?", (path,)).fetchone()

    def knowledge_all(self) -> list[sqlite3.Row]:
        """전체 목록(최신 수정 순) — 검색·ask 문맥·향후 지식 메뉴가 같이 쓴다."""
        return self.db.execute(
            "SELECT * FROM knowledge ORDER BY mtime DESC").fetchall()

    def prune_knowledge(self, keep: set) -> int:
        """파일이 사라진 색인 제거 — prune_notes 와 같은 계약."""
        gone = [r["path"] for r in self.db.execute("SELECT path FROM knowledge")
                if r["path"] not in keep]
        for p in gone:
            self.db.execute("DELETE FROM knowledge WHERE path=?", (p,))
        if gone:
            self.db.commit()
        return len(gone)

    def search_knowledge(self, query: str, limit: int = 10) -> list[dict]:
        """지식 본문 검색 — search_notes 와 같은 이유로 FTS 없이 부분일치.

        숨김 필터를 걸지 않는 이유: 지식은 스레드가 아니라 **파일이 원본**이고,
        사람이 승인해 만든 산출물이라 숨길 대상이 아니다(스레드 숨김은 목록
        소음 제어일 뿐, 그 대화에서 배운 지식까지 감추라는 뜻이 아니다)."""
        terms = [t.lower() for t in re.split(r"[^0-9A-Za-z가-힣]+", query or "")
                 if len(t) >= 2]
        if not terms:
            return []
        out: list[dict] = []
        for r in self.knowledge_all():
            hay = f"{r['title']}\n{r['content']}"
            low = hay.lower()
            if not all(t in low for t in terms):
                continue
            i = low.find(terms[0])
            j = i + len(terms[0])
            snip = " ".join((("…" if i > 30 else "") + hay[max(0, i - 30):i]
                             + "⟪" + hay[i:j] + "⟫"
                             + hay[j:j + 60] + "…").split())
            out.append({"path": r["path"], "title": r["title"],
                        "threads": r["threads"], "snippet": snip})
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        row = self.db.execute(
            """SELECT COUNT(*) AS msgs, SUM(raw_chars) AS raw,
                      SUM(LENGTH(new_content)) AS kept FROM messages"""
        ).fetchone()
        threads = self.db.execute("SELECT COUNT(*) AS n FROM threads").fetchone()
        people = self.db.execute("SELECT COUNT(*) AS n FROM people").fetchone()
        return {
            "messages": row["msgs"] or 0,
            "threads": threads["n"],
            "people": people["n"],
            "raw_chars": row["raw"] or 0,
            "kept_chars": row["kept"] or 0,
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "fts": self.fts_tokenizer,
        }
