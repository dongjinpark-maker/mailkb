"""SQLite 저장소 — 인덱스 계층.

원본은 Outlook(hot)에 있고, 여기는 메타 + new_content + FTS + 롤링 요약만.
연 200~300MB 수준. 백업은 파일 복사 한 번.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .clean import extract_new_content, normalize_subject, sanitize_html
from .sources.base import MailRecord

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
    body_html    TEXT DEFAULT '',      -- 정제된 표시용 HTML (웹 UI; 없으면 '')
    read_at      TEXT DEFAULT '',      -- 웹에서 스레드 열람 시각 (빈값=미읽음)
    raw_chars    INTEGER DEFAULT 0,    -- 절감 측정용 원본 길이
    folder       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_sent_on ON messages(sent_on);

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
-- 확정(confirmed)은 사람이 웹 '기록 › 결정' 검토 큐에서. AI 는 제안만.
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


class Store:
    def __init__(self, db_path: Path, my_addresses: list[str]):
        self.db_path = db_path
        self.my_addresses = {a.lower() for a in my_addresses}
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")  # 웹 백그라운드 쓰기 경합 대비
        self.db.executescript(_SCHEMA)
        self._migrate()
        try:
            self.db.execute(_FTS_TRIGRAM)
            self.fts_tokenizer = "trigram"
        except sqlite3.OperationalError:
            self.db.execute(_FTS_FALLBACK)
            self.fts_tokenizer = "unicode61"
        self.db.commit()

    def _migrate(self) -> None:
        """기존 DB 스키마 진화 — 없는 컬럼만 안전하게 추가(기존 데이터 무손상)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "body_html" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN body_html TEXT DEFAULT ''")
        if "read_at" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN read_at TEXT DEFAULT ''")
        tcols = {r["name"] for r in self.db.execute("PRAGMA table_info(threads)")}
        if "flagged" not in tcols:
            self.db.execute("ALTER TABLE threads ADD COLUMN flagged INTEGER DEFAULT 0")
        if "hidden" not in tcols:
            self.db.execute("ALTER TABLE threads ADD COLUMN hidden INTEGER DEFAULT 0")
        # 추적제외(dismissed) 폐지(2026-07-12) — 기존 데이터는 숨김으로 이관.
        # 숨김이 자동 해제(새 수신 시)까지 흡수했으므로 의미 손실 없음.
        self.db.execute(
            "UPDATE threads SET hidden=1, status='open' WHERE status='dismissed'")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- sync

    def last_sync(self) -> str | None:
        row = self.db.execute(
            "SELECT value FROM sync_state WHERE key='last_sync'"
        ).fetchone()
        return row["value"] if row else None

    def ingest(self, records, progress=None) -> SyncStats:
        """MailRecord 스트림을 인덱싱. 시간순 입력을 가정.

        progress(stats) 가 주어지면 레코드마다 호출된다(CLI 라이브 카운터용).
        """
        stats = SyncStats()
        max_seen = self.last_sync() or ""
        for rec in records:
            stats.fetched += 1
            if self._insert(rec, stats):
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

    def _insert(self, rec: MailRecord, stats: SyncStats) -> bool:
        exists = self.db.execute(
            "SELECT 1 FROM messages WHERE message_id=?", (rec.message_id,)
        ).fetchone()
        if exists:
            return False

        new_content = extract_new_content(rec.body_text)
        body_html = sanitize_html(rec.body_html) if rec.body_html else ""
        stats.raw_chars += len(rec.body_text)
        stats.kept_chars += len(new_content)
        is_sent = int(rec.sender_addr.lower() in self.my_addresses)
        thread_id = self._assign_thread(rec, stats)

        cur = self.db.execute(
            """INSERT INTO messages
               (message_id, entry_id, thread_id, subject, sender_name, sender_addr,
                to_addrs, cc_addrs, sent_on, is_sent, attach_names, new_content,
                body_html, raw_chars, folder)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.message_id, rec.entry_id, thread_id, rec.subject,
                rec.sender_name, rec.sender_addr.lower(),
                ";".join(a.lower() for a in rec.to),
                ";".join(a.lower() for a in rec.cc),
                rec.sent_on, is_sent, ";".join(rec.attachments),
                new_content, body_html, len(rec.body_text), rec.folder,
            ),
        )
        self.db.execute(_FTS_SYNC, (cur.lastrowid, rec.subject, new_content))
        self._touch_thread(thread_id, rec.sent_on)
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

    def _assign_thread(self, rec: MailRecord, stats: SyncStats) -> int:
        # 1순위: References/In-Reply-To 가 가리키는 기존 메시지의 스레드
        refs = list(rec.references)
        if rec.in_reply_to:
            refs.append(rec.in_reply_to)
        for ref in refs:
            row = self.db.execute(
                "SELECT thread_id FROM messages WHERE message_id=?", (ref,)
            ).fetchone()
            if row:
                return row["thread_id"]
        # 2순위: 소스가 준 대화 키 (Outlook ConversationIndex 루트)
        if rec.conversation_key:
            row = self.db.execute(
                "SELECT id FROM threads WHERE conversation_key=?",
                (rec.conversation_key,),
            ).fetchone()
            if row:
                return row["id"]
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
                return row["id"]
        # 새 스레드
        cur = self.db.execute(
            """INSERT INTO threads (norm_subject, conversation_key, first_date, last_date)
               VALUES (?,?,?,?)""",
            (norm, rec.conversation_key, rec.sent_on, rec.sent_on),
        )
        stats.new_threads += 1
        return cur.lastrowid

    def _touch_thread(self, thread_id: int, sent_on: str) -> None:
        self.db.execute(
            """UPDATE threads SET
                 first_date = CASE WHEN first_date='' OR first_date > ? THEN ? ELSE first_date END,
                 last_date  = CASE WHEN last_date  < ? THEN ? ELSE last_date END
               WHERE id=?""",
            (sent_on, sent_on, sent_on, sent_on, thread_id),
        )

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

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        # trigram 은 3글자 미만 질의 불가 — LIKE 로 폴백
        if self.fts_tokenizer == "trigram" and len(query) < 3:
            like = f"%{query}%"
            return self.db.execute(
                """SELECT m.* FROM messages m
                   WHERE m.new_content LIKE ? OR m.subject LIKE ?
                   ORDER BY m.sent_on DESC LIMIT ?""",
                (like, like, limit),
            ).fetchall()
        return self.db.execute(
            """SELECT m.* FROM messages_fts f
               JOIN messages m ON m.id = f.rowid
               WHERE messages_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (f'"{query}"', limit),
        ).fetchall()

    def unanswered(self, days: int = 14, max_recipients: int = 50) -> list[sqlite3.Row]:
        """미답변 스레드: 마지막 메일이 수신이고 To 에 내가 있으며 내 답장이 없는 것.

        max_recipients 이상 수신자의 단체 발송은 개인 회신 의무가 약해 제외
        (기본 50 — 20~30명 실무 메일은 포함, 그룹/팀 전체 공지만 배제).
        """
        rows = self.db.execute(
            """
            SELECT t.id AS thread_id, m.subject, m.sender_name, m.sender_addr,
                   m.sent_on, m.message_id,
                   CAST(julianday('now') - julianday(m.sent_on) AS INTEGER) AS days_old
            FROM threads t
            JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE thread_id = t.id
                ORDER BY sent_on DESC, id DESC LIMIT 1
            )
            WHERE (t.hidden IS NULL OR t.hidden = 0)
              AND m.is_sent = 0
              AND m.sent_on >= datetime('now', ?)
            ORDER BY m.sent_on ASC
            """,
            (f"-{days} days",),
        ).fetchall()
        return [r for r in rows if self._last_to_me(r["thread_id"], max_recipients)]

    def _last_to_me(self, thread_id: int, max_recipients: int = 50) -> bool:
        """마지막 메일이 '나에게 향한' 것인지.

        To 에 내가 있어야 하고, 수신자 max_recipients 이상의 단체 발송(전사·팀
        공지류)은 회신 의무가 약하므로 제외 — 필요하면 스레드에 직접 회신하거나
        note 로 승격하면 된다.
        """
        row = self.db.execute(
            """SELECT to_addrs FROM messages WHERE thread_id=?
               ORDER BY sent_on DESC, id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if not row:
            return False
        tos = [a for a in row["to_addrs"].split(";") if a]
        if len(tos) >= max_recipients:
            return False
        return bool(set(tos) & self.my_addresses)

    def open_thread_tails(self) -> list[sqlite3.Row]:
        """열린 스레드별 '최신 메시지 1건' + 파생값 (개입 큐용).

        unanswered() 의 상관 서브쿼리 패턴 재사용. 동분(同分) 메일에서 '마지막'
        이 흔들리지 않도록 sent_on 동률 시 id DESC 로 확정.
        """
        return self.db.execute(
            """
            SELECT t.id AS thread_id, t.last_date, t.rolling_summary,
                   m.id AS last_id, m.is_sent AS last_is_sent,
                   m.sender_name, m.sender_addr, m.to_addrs, m.cc_addrs,
                   m.new_content, m.subject, m.sent_on,
                   CAST(julianday('now') - julianday(m.sent_on) AS INTEGER) AS days_old,
                   (SELECT COUNT(*) FROM messages WHERE thread_id=t.id) AS msg_count,
                   (SELECT COUNT(*) FROM messages WHERE thread_id=t.id AND is_sent=1)
                       AS my_msg_count
            FROM threads t
            JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE thread_id=t.id
                ORDER BY sent_on DESC, id DESC LIMIT 1
            )
            WHERE (t.hidden IS NULL OR t.hidden=0)
            ORDER BY m.sent_on DESC
            """
        ).fetchall()

    def sent_on_date(self, date_iso: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM messages WHERE is_sent=1 AND date(sent_on)=?
               ORDER BY sent_on""",
            (date_iso,),
        ).fetchall()

    def received_on_date(self, date_iso: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM messages WHERE is_sent=0 AND date(sent_on)=?
               ORDER BY sent_on""",
            (date_iso,),
        ).fetchall()

    def thread_messages(self, thread_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY sent_on",
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
            "SELECT DISTINCT thread_id FROM messages WHERE date(sent_on)=?",
            (date_iso,),
        ).fetchall()
        return [r["thread_id"] for r in rows]

    def threads_active_between(self, start_iso: str, end_iso: str) -> list[int]:
        """[start, end] (양끝 포함) 활동 스레드 — 요약 '마지막 실행 이후' 창용."""
        rows = self.db.execute(
            "SELECT DISTINCT thread_id FROM messages "
            "WHERE date(sent_on) >= ? AND date(sent_on) <= ?",
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
        where = "WHERE date(sent_on)=date('now')" if today_only else ""
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
        self.db.commit()
        return cur.rowcount > 0

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
