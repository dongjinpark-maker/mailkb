"""검색 질의어(DSL) 파서 + FTS 완화 tier 생성 — 순수(DB 비의존, stdlib만).

`store.Store.search` 가 이 파서 결과로 SQL 을 조립한다. 파싱·정규화·tier 규칙을
여기 순수 함수로 격리해, 나중에 "SQLite 검색 skill" 로 그대로 추출할 수 있게 한다.

지원 연산자 (나머지 토큰은 키워드):
    from: to: cc:            사람/주소 (이름은 공백 무시 매칭, @있으면 주소)
    after: before: on:       날짜 경계 (YYYY | YYYY-MM | YYYY-MM-DD)
    is:unread|read|sent|received|flagged
    has:attachment
    file:                    첨부 파일명
    thread:N
    "정확한 구"               연속 구(phrase)

trigram FTS 는 3글자 미만 토큰을 색인하지 못한다(한국어 2자어 '모델·평가·서빙'
등은 개별 MATCH 0건). 그래서 2자 이하 키워드는 FTS 가 아니라 LIKE 로 라우팅하고,
연속 2자어는 phrase tier("모델 평가")가 별도로 건진다. build_match 참고.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 알려진 연산자 키 — 이 외의 `key:val` 은 그냥 키워드로 취급(예: URL http://…)
_FIELD_KEYS = {"from", "to", "cc", "after", "before", "on",
               "is", "has", "file", "thread"}
_IS_VALUES = {"unread", "read", "sent", "received", "flagged"}

# `key:"quoted val"` | `key:val` | "quoted phrase" | bare
_TOKEN_RX = re.compile(r'''
    (?P<key>\w+):"(?P<kqval>[^"]*)"      # from:"강 미래"
  | (?P<key2>\w+):(?P<val>\S+)           # from:강미래  after:2026-06
  | "(?P<phrase>[^"]*)"                  # "정확한 구"
  | (?P<term>\S+)                        # 맨 키워드
''', re.VERBOSE)


@dataclass
class Query:
    terms: list = field(default_factory=list)      # 맨 키워드
    phrases: list = field(default_factory=list)    # "정확한 구"
    from_: list = field(default_factory=list)
    to: list = field(default_factory=list)
    cc: list = field(default_factory=list)
    after: str | None = None                        # sent_on >= after (ISO date)
    before: str | None = None                       # sent_on < before  (배타 상한)
    thread: int | None = None
    is_flags: set = field(default_factory=set)
    has_attach: bool = False
    files: list = field(default_factory=list)

    def has_text(self) -> bool:
        return bool(self.terms or self.phrases)

    def has_filters(self) -> bool:
        return bool(self.from_ or self.to or self.cc or self.after or self.before
                    or self.thread or self.is_flags or self.has_attach or self.files)


# ─────────────────────────────────────────────────────── 날짜 경계

def date_floor(s: str) -> str | None:
    """기간의 시작일(포함). '2026'→2026-01-01, '2026-06'→2026-06-01."""
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", s or "")
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2) or "01", m.group(3) or "01"
    if not ("01" <= mo <= "12") or not ("01" <= d <= "31"):
        return None
    return f"{y}-{mo}-{d}"


def date_ceil(s: str) -> str | None:
    """기간의 끝(배타 상한). '2026'→2027-01-01, '2026-06'→2026-07-01,
    '2026-06-15'→2026-06-16 (그 다음 날/달/해)."""
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", s or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    if d is not None:
        from datetime import date, timedelta
        try:
            nxt = date(y, int(mo), int(d)) + timedelta(days=1)
        except ValueError:
            return None
        return nxt.isoformat()
    if mo is not None:
        mm = int(mo)
        if not 1 <= mm <= 12:
            return None
        return f"{y + 1}-01-01" if mm == 12 else f"{y}-{mm + 1:02d}-01"
    return f"{y + 1}-01-01"


# ─────────────────────────────────────────────────────── 파서

def parse_query(text: str) -> Query:
    q = Query()
    for m in _TOKEN_RX.finditer(text or ""):
        key = m.group("key") or m.group("key2")
        if key:
            if key.lower() in _FIELD_KEYS:
                val = m.group("kqval")
                if val is None:
                    val = m.group("val")
                _apply_field(q, key.lower(), val)
            else:
                q.terms.append(m.group(0))       # 알 수 없는 key:val → 키워드(URL 등)
            continue
        ph = m.group("phrase")
        if ph is not None:
            ph = ph.strip()
            if ph:
                q.phrases.append(ph)
            continue
        term = m.group("term")
        if term:
            q.terms.append(term)
    return q


def _apply_field(q: Query, key: str, val: str) -> None:
    val = (val or "").strip()
    if not val and key not in ("has",):
        return
    if key == "from":
        q.from_.append(val)
    elif key == "to":
        q.to.append(val)
    elif key == "cc":
        q.cc.append(val)
    elif key == "after":
        d = date_floor(val)
        if d:
            q.after = d if q.after is None else max(q.after, d)
    elif key == "before":
        d = date_floor(val)
        if d:
            q.before = d if q.before is None else min(q.before, d)
    elif key == "on":
        lo, hi = date_floor(val), date_ceil(val)
        if lo:
            q.after = lo if q.after is None else max(q.after, lo)
        if hi:
            q.before = hi if q.before is None else min(q.before, hi)
    elif key == "is":
        v = val.lower()
        if v in _IS_VALUES:
            q.is_flags.add(v)
    elif key == "has":
        if val.lower() in ("attachment", "attach", "file", "") :
            q.has_attach = True
    elif key == "file":
        q.files.append(val)
    elif key == "thread":
        try:
            q.thread = int(val)
        except ValueError:
            pass


# ─────────────────────────────────────────────────────── FTS 매칭 문자열

def _fts_quote(s: str) -> str:
    """FTS5 문자열 리터럴 — 연산자 오해 방지를 위해 각 토큰을 큰따옴표로 감싼다."""
    return '"' + s.replace('"', '""') + '"'


def terms_fts(q: Query) -> list:
    """FTS 색인 가능한(≥3자) 키워드."""
    return [t for t in q.terms if len(t) >= 3]


def terms_short(q: Query) -> list:
    """trigram 이 못 잡는(<3자) 키워드 — LIKE 로 처리."""
    return [t for t in q.terms if 0 < len(t) < 3]


def build_match(q: Query, tier: int) -> str | None:
    """완화 tier 의 FTS5 MATCH 문자열. 없으면 None.

    tier 1 phrase : 구 + 맨 키워드 전체를 하나의 연속 구로  → 최고 정밀.
                    (2자어라도 붙어 있으면 여기서 잡힌다: "모델 평가"=5자 → 색인됨)
    tier 2 AND    : 구·≥3자어를 모두 포함(암묵 AND). 2자어는 store 가 LIKE 로 AND.
    tier 3 OR     : 구·≥3자어 중 하나라도            → '관련 낮음'
    """
    phrases = [_fts_quote(p) for p in q.phrases]
    t3 = terms_fts(q)
    if tier == 1:
        parts = list(phrases)
        if len(q.terms) >= 2:
            joined = " ".join(q.terms)       # 2자어 포함 전체를 연속 구로
            if len(joined) >= 3:
                parts.append(_fts_quote(joined))
        elif len(q.terms) == 1 and phrases and len(q.terms[0]) >= 3:
            parts.append(_fts_quote(q.terms[0]))
        # 구 없이 단어 하나뿐이면 tier2 와 동일 → None 으로 중복 방지
        return " AND ".join(parts) if parts else None
    if tier == 2:
        parts = phrases + [_fts_quote(t) for t in t3]
        return " ".join(parts) if parts else None       # 공백 = 암묵 AND
    if tier == 3:
        parts = phrases + [_fts_quote(t) for t in t3]
        return " OR ".join(parts) if len(parts) >= 2 else None
    return None
