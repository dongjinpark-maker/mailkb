"""SQLite 저장소 — 인덱스 계층.

원본은 Outlook(hot)에 있고, 여기는 메타 + new_content + FTS + 롤링 요약만.
연 200~300MB 수준. 백업은 파일 복사 한 번.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import search as search_mod
from .clean import (extract_new_content, inject_inline_images,
                    normalize_subject, sanitize_html)
from .features import FEATURE_VERSION, classify_message
from .sources.base import MailRecord

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
    -- 액션 상태기계 (docs/PROPOSAL-actions.md): 열린 요청 슬롯은 스레드당 1개.
    -- 내 실질 회신(is_trivial 아님)·명시적 철회만 닫는다. 상대의 완료 통보는
    -- 닫지 않고 completion_after_action 표시만(잘못 닫힘 = 조용히 놓친 공).
    action_source_id        INTEGER NOT NULL DEFAULT 0,   -- 0 = 열린 액션 없음
    action_strength         TEXT NOT NULL DEFAULT '',     -- 'strong' | 'weak'
    action_kind             TEXT NOT NULL DEFAULT '',     -- 'decide' | 'respond'
    action_has_deadline     INTEGER NOT NULL DEFAULT 0,
    completion_after_action INTEGER NOT NULL DEFAULT 0
);
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
    raw_chars    INTEGER DEFAULT 0,    -- 절감 측정용 원본 길이
    folder       TEXT DEFAULT ''
);
-- 표시용 HTML(이미지 임베드 포함)은 별도 테이블 — 큰 blob 이 messages 행에
-- 끼면 목록·카운트 전수 스캔이 오버플로 페이지를 건너 읽어 느려진다
-- (docs/PROPOSAL-images.md 3-B-2). 스레드 열람 때만 조인.
CREATE TABLE IF NOT EXISTS message_html (
    message_id INTEGER PRIMARY KEY,   -- messages.id
    html       TEXT DEFAULT ''        -- 정제·이미지 임베드된 HTML (프룬 대상)
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_sent_on ON messages(sent_on);
CREATE INDEX IF NOT EXISTS idx_messages_thread_date
    ON messages(thread_id, sent_on DESC, id DESC);

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

-- 신호 수동 해제 오버레이 (상세 화면 칩의 ✕) — 파생 테이블이 아니라
-- 백필(drop+재생성)·재접기에 살아남는다. source_id(해제 당시의 요청 메시지)에
-- 키가 걸려 있어 같은 스레드에 새 요청이 오면(action_source_id 변경) 자동으로
-- 무시된다 = 신호 자동 복귀. 숨김(스레드 전체)과 달리 이 요청 건만 끈다.
CREATE TABLE IF NOT EXISTS action_overrides (
    thread_id        INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    dismiss_action   INTEGER NOT NULL DEFAULT 0,   -- 회신 필요·확인 후보 해제(⏰ 포함)
    dismiss_deadline INTEGER NOT NULL DEFAULT 0    -- ⏰ 만 해제
);

CREATE TABLE IF NOT EXISTS intervention_ai (
    date       TEXT NOT NULL,      -- 오늘자로 저장 → 날짜 바뀌면 자동 무시
    thread_id  INTEGER NOT NULL,
    priority   TEXT DEFAULT '',    -- 상|중|하
    reason     TEXT DEFAULT '',
    action     TEXT DEFAULT '',
    flag       TEXT DEFAULT '',    -- 처리됨
    updated    TEXT DEFAULT '',
    PRIMARY KEY (date, thread_id)
);

-- 결정 원장 (지식 증류 Phase 1): 데일리 수확이 후보(candidate)로 적재,
-- 반영(confirmed)은 사람이 웹 '기억 › 장기기억' 반영 대기 큐에서. AI 는 제안만.
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    thread_id     INTEGER NOT NULL,
    decided_on    TEXT DEFAULT '',        -- 결정 시점 (YYYY-MM-DD)
    title         TEXT DEFAULT '',        -- 결정 한 줄
    rationale     TEXT DEFAULT '',        -- 근거
    decider       TEXT DEFAULT '',        -- 결정자 (이름/주소)
    quote         TEXT DEFAULT '',        -- 원문 인용 (환각 앵커 — 본문 부분일치 검증됨)
    status        TEXT DEFAULT 'candidate', -- candidate|confirmed|superseded|rejected
    superseded_by INTEGER,                -- 이 결정을 뒤집은 후속 결정 id (Phase 2)
    source        TEXT DEFAULT 'daily',   -- daily|weekly|manual
    created       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);

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

    전이 규칙 (docs/PROPOSAL-actions.md):
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
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        # incremental vacuum: 이미지 프룬이 지운 공간을 조각 단위로 회수 —
        # 풀 VACUUM(수십 초 배타 잠금)은 단일 스레드 웹 서버를 세우므로 금지.
        # 이 PRAGMA 는 새 DB(테이블 생성 전)에서만 효력 — clean start 전제.
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")  # 웹 백그라운드 쓰기 경합 대비
        # 성능 PRAGMA(결과 불변, 속도만) — synchronous=NORMAL: WAL 에서 표준 권장.
        # 앱 크래시엔 안전, OS 크래시/정전 시에만 마지막 트랜잭션 유실 가능한데
        # 이 DB 는 Outlook 에서 재수집 가능한 캐시(message_id UNIQUE 로 멱등)라
        # 안전하다. 커밋마다 fsync 제거 → sync·열람 쓰기 대폭 가속.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-16384")   # 16MB 페이지 캐시(기본 2MB)
        self.db.execute("PRAGMA temp_store=MEMORY")    # 정렬·임시 결과 RAM
        self.db.execute("PRAGMA mmap_size=268435456")  # 256MB 메모리맵 읽기
        self.db.executescript(_SCHEMA)
        # 파생 테이블(재구축 가능) — 버전 마이그레이션이 drop+재생성으로 스키마를 바꾼다.
        self.db.executescript(_FEATURES_DDL)
        self.db.executescript(_THREAD_STATE_DDL)
        # 일반 스키마 개편은 clean start 원칙. 목록용 파생 테이블만 원본 messages에서
        # 결과 불변으로 재생성할 수 있어 _ensure_derived_state가 버전별 1회 백필한다.
        try:
            self.db.execute(_FTS_TRIGRAM)
            self.fts_tokenizer = "trigram"
        except sqlite3.OperationalError:
            self.db.execute(_FTS_FALLBACK)
            self.fts_tokenizer = "unicode61"
        self.db.commit()
        self._ensure_derived_state()

    def close(self) -> None:
        self.db.close()

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

    def ingest(self, records, progress=None,
               image_cutoff: str | None = None) -> SyncStats:
        """MailRecord 스트림을 인덱싱. 시간순 입력을 가정.

        progress(stats) 가 주어지면 레코드마다 호출된다(CLI 라이브 카운터용).
        image_cutoff(YYYY-MM-DD): 이 날짜 이전 메일은 인라인 이미지를 임베드하지
        않는다(대량 백필에서 곧 프룬될 이미지 낭비 방지). None 이면 게이트 없음.
        """
        stats = SyncStats()
        max_seen = self.last_sync() or ""
        try:
            for rec in records:
                stats.fetched += 1
                if self._insert(rec, stats, image_cutoff):
                    stats.inserted += 1
                else:
                    stats.skipped += 1
                if rec.sent_on > max_seen:
                    max_seen = rec.sent_on
                if progress:
                    progress(stats)
            if max_seen:
                self.db.execute(
                    "INSERT INTO sync_state(key, value) VALUES('last_sync', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (max_seen,),
                )
            self.db.commit()
            return stats
        except Exception:
            self.db.rollback()
            raise

    def _insert(self, rec: MailRecord, stats: SyncStats,
                image_cutoff: str | None = None) -> bool:
        exists = self.db.execute(
            "SELECT 1 FROM messages WHERE message_id=?", (rec.message_id,)
        ).fetchone()
        if exists:
            return False

        thread_id, t_created = self._assign_thread(rec, stats)
        # mid-join 보존 (docs/PROPOSAL-midjoin.md): 새 스레드를 만든 메일 = 그
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
               (message_id, entry_id, thread_id, subject, sender_name, sender_addr,
                to_addrs, cc_addrs, sent_on, is_sent, attach_names, new_content,
                raw_chars, folder)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
        # 새 스레드
        cur = self.db.execute(
            """INSERT INTO threads (norm_subject, conversation_key, first_date, last_date)
               VALUES (?,?,?,?)""",
            (norm, rec.conversation_key, rec.sent_on, rec.sent_on),
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
        """sync_state kv 조회 (last_summary 등 범용)."""
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
        있으면 True. 발신(내가 보낸) 메일은 대상 아님."""
        cur = self.db.execute(
            "UPDATE messages SET read_at=? "
            "WHERE thread_id=? AND is_sent=0 AND (read_at IS NULL OR read_at='')",
            (datetime.now().isoformat(timespec="seconds"), thread_id),
        )
        if cur.rowcount > 0:
            self.db.execute(
                "UPDATE thread_state SET unread_received_count=0 WHERE thread_id=?",
                (thread_id,),
            )
        self.db.commit()
        return cur.rowcount > 0

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
        """숨김 설정/해제. 숨기면 추적(미답변·개입)·메일함·스레드 기본목록에서 제외.
        새 수신 메일이 오면 자동 해제된다(_insert) — 놓침 방지."""
        self.db.execute(
            "UPDATE threads SET hidden=? WHERE id=?", (1 if on else 0, thread_id)
        )
        self.db.commit()

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

    def person_window_counts(self, window_weeks: int = 13) -> list[dict]:
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

    def person_sent_texts(self, addr: str, limit: int = 300) -> list[str]:
        """이 사람이 보낸 메일의 정제 본문(신규 작성분) — 도시에 '주요 어휘'용.

        본인이 직접 쓴 것만(is_sent=0 이고 발신자=이 addr). 인용된 남의 말·내 말은
        new_content 단계에서 이미 빠져 있고, 표시부에서 strip_preserved 로 한 번 더
        보존 인용을 걷는다. 최신순 limit 통(어휘 표본 상한)."""
        addr = (addr or "").lower()
        return [r["new_content"] or "" for r in self.db.execute(
            "SELECT new_content FROM messages "
            "WHERE is_sent=0 AND LOWER(sender_addr)=? "
            "ORDER BY sent_on DESC LIMIT ?", (addr, limit))]

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

    def save_intervention_ai(self, date_iso: str, thread_id: int, priority: str,
                             reason: str, action: str, flag: str) -> None:
        """개입 큐 AI 정리 결과를 오늘자로 저장(스레드당 1건, upsert)."""
        self.db.execute(
            """INSERT INTO intervention_ai
                 (date, thread_id, priority, reason, action, flag, updated)
               VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(date, thread_id) DO UPDATE SET
                 priority=excluded.priority, reason=excluded.reason,
                 action=excluded.action, flag=excluded.flag, updated=excluded.updated""",
            (date_iso, thread_id, priority or "", reason or "", action or "", flag or ""),
        )
        self.db.commit()

    def load_intervention_ai(self, date_iso: str) -> dict:
        """오늘자 저장된 AI 정리 주석 {thread_id: {ai_priority/ai_reason/...}}."""
        rows = self.db.execute(
            "SELECT thread_id, priority, reason, action, flag "
            "FROM intervention_ai WHERE date=?", (date_iso,),
        ).fetchall()
        return {
            r["thread_id"]: {
                "ai_priority": r["priority"] or None,
                "ai_reason": r["reason"],
                "ai_action": r["action"],
                "ai_flag": r["flag"],
            } for r in rows
        }

    # -------------------------------------------------- 결정 원장 · 수확 신호

    @staticmethod
    def _norm_title(title: str) -> str:
        return " ".join((title or "").split()).lower()

    def add_decision(self, thread_id: int, decided_on: str, title: str,
                     rationale: str = "", decider: str = "", quote: str = "",
                     status: str = "candidate", source: str = "daily") -> int | None:
        """결정 적재. 같은 스레드에 같은 제목(공백·대소문자 무시)의 살아있는
        결정(candidate/confirmed)이 이미 있으면 중복으로 보고 None."""
        title = (title or "").strip()
        if not title:
            return None
        norm = self._norm_title(title)
        for r in self.db.execute(
                "SELECT title FROM decisions WHERE thread_id=? "
                "AND status IN ('candidate','confirmed')", (thread_id,)):
            if self._norm_title(r["title"]) == norm:
                return None
        cur = self.db.execute(
            """INSERT INTO decisions
                 (thread_id, decided_on, title, rationale, decider, quote,
                  status, source, created)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
            (thread_id, decided_on or "", title, rationale or "",
             decider or "", quote or "", status, source),
        )
        self.db.commit()
        return cur.lastrowid

    def decisions(self, status: str | None = None, q: str = "",
                  limit: int = 300) -> list[sqlite3.Row]:
        """원장 목록 — status 필터(없으면 전체), q 는 제목/근거/결정자 LIKE."""
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if q:
            like = f"%{q}%"
            cond.append("(title LIKE ? OR rationale LIKE ? OR decider LIKE ?)")
            args += [like, like, like]
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        args.append(limit)
        return self.db.execute(
            f"SELECT * FROM decisions {where} "
            "ORDER BY decided_on DESC, id DESC LIMIT ?", args).fetchall()

    def decision(self, did: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM decisions WHERE id=?", (did,)).fetchone()

    def decision_counts(self) -> dict:
        out = {"candidate": 0, "confirmed": 0, "rejected": 0, "superseded": 0}
        for r in self.db.execute(
                "SELECT status, COUNT(*) n FROM decisions GROUP BY status"):
            out[r["status"]] = r["n"]
        return out

    def set_decision_status(self, did: int, status: str,
                            title: str | None = None,
                            rationale: str | None = None) -> bool:
        """상태 변경(+선택적 제목/근거 수정 — '수정 후 확정'). 없으면 False."""
        if not self.decision(did):
            return False
        sets, args = ["status=?"], [status]
        if title is not None and title.strip():
            sets.append("title=?")
            args.append(title.strip())
        if rationale is not None:
            sets.append("rationale=?")
            args.append(rationale.strip())
        args.append(did)
        self.db.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", args)
        self.db.commit()
        return True

    def add_signal(self, date_iso: str, kind: str, who: str,
                   thread_id: int | None, signal: str, quote: str = "") -> None:
        """인물/프로젝트 신호 적재 — Phase 2 주간 증류의 재료."""
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

    def person_decisions(self, addr: str, name: str = "",
                         limit: int = 20) -> list[sqlite3.Row]:
        """이 사람이 결정자인 장기기억 항목 — 도시에 '관여한 결정'.

        decider 이름 매치를 이 addr 참여 스레드로 교집합(동명이인 방지).
        반려(rejected) 제외 — 살아있는 결정만."""
        if not name:
            return []
        tids = self.person_thread_ids(addr)
        if not tids:
            return []
        marks = ",".join("?" * len(tids))
        args = [f"%{name}%", *tids, limit]
        return self.db.execute(
            f"SELECT * FROM decisions WHERE decider LIKE ? "
            f"AND status IN ('candidate','confirmed') "
            f"AND thread_id IN ({marks}) "
            f"ORDER BY decided_on DESC, id DESC LIMIT ?", args).fetchall()

    # ------------------------------------------ 본문 HTML 수명주기 (이미지 프룬)
    # docs/PROPOSAL-images.md: retain_days 경과 시 표시용 HTML 을 텍스트 수준으로
    # 압축 — 이미지 있던 메일은 초경량 마커 한 줄, 없던 메일은 행 삭제.
    # 검색(FTS)·AI 재료는 new_content 라 무손실.

    _STRIP_MARK = "<div class='imgstrip'>"

    def maybe_prune_html(self, retain_days: int) -> tuple[int, int] | None:
        """sync 종료 훅 — 하루 1회만 실제 프룬. (마커 n, 삭제 n) 또는 None(스킵).

        retain_days <= 0 이면 기능 끔(임베드도 프룬도 안 함 — 현행 유지).
        건너뛴 날은 다음 실행이 경과일 기준으로 한 번에 처리(누락 없음).
        가드는 '같은 날 + 같은 설정값'일 때만 — 보존 기간을 바꾸면 그날이라도
        다음 sync 에서 즉시 반영된다 (PC 스모크 피드백, 2026-07-13).
        """
        if retain_days <= 0:
            return None
        today = datetime.now().date().isoformat()
        stamp = f"{today}:{retain_days}"
        if self.get_state("last_image_prune") == stamp:
            return None
        n_mark, n_del = self._prune_html(retain_days)
        self.set_state("last_image_prune", stamp)
        if n_mark or n_del:
            # 조각 회수 — 풀 VACUUM(배타 수십 초) 금지, auto_vacuum=INCREMENTAL 전제
            self.db.execute("PRAGMA incremental_vacuum")
            self.db.commit()
        return n_mark, n_del

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
