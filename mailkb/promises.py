"""내 약속 추적과 기한 날짜 계산 — 리포트가 '내가 말해 놓고 안 한 것'을 짚는 재료.

왜 필요한가: 메일 클라이언트는 "제가 하겠습니다"를 기억해 주지 않는다. 이 도구는
원문을 들고 있으므로 **내가 직접 쓴 문장**에서 약속을 뽑아 인용과 함께 되짚을 수
있다. 남의 의도를 추측하는 게 아니라 내 문장을 인용하는 것이라, 예전 '지금 할 일'
큐(정규식으로 상대 의도를 추측하다 신뢰를 잃었다)와 성격이 다르다.

설계 원칙 — **모르겠으면 보고하지 않는다**(2026-08-01 사용자 확정):
  · 확정 어미만 인정하고 요청·조건·부정문은 뺀다
  · 그 뒤 내가 그 스레드에 한 통이라도 보냈으면 뺀다(이행 정황)
  · 기한은 실제 날짜로 환산되는 것만 쓴다("이번 주 중"처럼 안 잡히면 기한 없음)
  · 오래된 것은 뺀다(PROMISE_MAX_DAYS) — 대개 메일 밖에서 처리됐다
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .clean import strip_preserved

PROMISE_MAX_DAYS = 14      # 이보다 오래된 약속은 보고하지 않는다

# 내가 하겠다고 **확정한** 어미만. "검토해 보겠습니다"류 추측형은 넣지 않는다.
_PROMISE_RX = re.compile(
    r"(하겠습니다|드리겠습니다|보내겠습니다|공유하겠습니다|올리겠습니다|맡겠습니다"
    r"|잡겠습니다|정리하겠습니다|진행하겠습니다|반영하겠습니다|제출하겠습니다"
    r"|처리하겠습니다|전달하겠습니다|확인하겠습니다)")
# 남에게 하는 요청·조건절·부정 — 내 약속이 아니다
_NOT_PROMISE_RX = re.compile(
    r"부탁|주시면|주세요|바랍니다|해주시|주시기|어렵|힘들|못하|않겠|말씀해\s*주")

_WHEN_RX = re.compile(
    r"(오늘|내일|모레|다음\s*주\s*[월화수목금]요일|이번\s*주\s*[월화수목금]요일"
    r"|[월화수목금]요일|\d{1,2}\s*/\s*\d{1,2}|\d{1,2}월\s*\d{1,2}일)")
_WEEKDAY = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4}


def resolve_when(expr: str, base: date) -> date | None:
    """상대 표현을 실제 날짜로. 확정 못 하면 None — 그러면 기한을 붙이지 않는다.

    '이번 주 중'·'조만간'처럼 날짜가 안 잡히는 것을 억지로 추정하면 틀린 기한이
    리포트에 박힌다. 모르는 것은 말하지 않는 편이 낫다."""
    e = (expr or "").replace(" ", "")
    if "오늘" in e:
        return base
    if "내일" in e:
        return base + timedelta(days=1)
    if "모레" in e:
        return base + timedelta(days=2)
    m = re.search(r"(\d{1,2})/(\d{1,2})", e) or re.search(r"(\d{1,2})월(\d{1,2})일", e)
    if m:
        try:
            got = date(base.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        # 연말에 "1/5까지"라고 쓰면 내년 1월이다. 같은 해로 두면 이미 지난 날짜가
        # 되어 리포트에 '⚠ 지남'이 붙는다. 두 달 넘게 과거면 다음 해로 넘긴다
        # (과거 날짜를 기한으로 적는 경우도 있으므로 여유를 둔다).
        if (base - got).days > 60:
            try:
                got = got.replace(year=base.year + 1)
            except ValueError:          # 2/29
                return None
        return got
    for name, idx in _WEEKDAY.items():
        if name in e:
            ahead = (idx - base.weekday()) % 7
            if "다음주" in e:
                ahead += 7
            elif ahead == 0:
                ahead = 7          # 같은 요일이면 다음 주 그 요일
            return base + timedelta(days=ahead)
    return None


def _sentences(text: str):
    for s in re.split(r"(?<=[.!?다요])\s+", text or ""):
        s = " ".join(s.split())
        if s:
            yield s


def _scan(store, since: str, until: str | None, day: date) -> list[dict]:
    """[since, until] 사이 내 발신에서 약속을 뽑고 이행 정황을 붙인다.

    '처리함'(store.report_done)으로 접은 것은 marked_done=True 로 표시해 돌려준다 —
    지난 차수 점검에서는 '지킨 것'으로 세야 하기 때문이다."""
    done_keys = store.report_done_keys("promise")
    out = []
    sql = ("SELECT id, thread_id, sent_on, subject, new_content FROM messages "
           "WHERE is_sent=1 AND sent_on >= ?")
    args = [since]
    if until:
        sql += " AND sent_on < date(?, '+1 day')"
        args.append(until)
    rows = store.db.execute(sql + " ORDER BY sent_on", args).fetchall()
    for r in rows:
        # 보존 인용(mid-join, PRESERVED_MARK 아래)은 **상대가 쓴 글**이다. 안 떼면
        # 남의 확정 어미가 첫 매치가 되어 '내 약속'으로 보고된다(2026-08-01 실증).
        for s in _sentences(strip_preserved(r["new_content"] or "")):
            if not (8 <= len(s) <= 120) or not _PROMISE_RX.search(s):
                continue
            if _NOT_PROMISE_RX.search(s):
                continue
            key = store.report_key(r["id"], s)
            after = store.db.execute(
                "SELECT is_sent FROM messages WHERE thread_id=? AND id>?",
                (r["thread_id"], r["id"])).fetchall()
            sent_day = datetime.fromisoformat(r["sent_on"]).date()
            m = _WHEN_RX.search(s)
            out.append({
                "key": key, "thread_id": r["thread_id"], "message_id": r["id"],
                "subject": r["subject"] or "", "quote": s,
                "days": (day - sent_day).days,
                "due": resolve_when(m.group(0), sent_day) if m else None,
                "replies_after": sum(1 for a in after if not a["is_sent"]),
                # 이행 정황 — 그 뒤 내가 그 스레드에 보냈거나, 사용자가 접었거나
                "followed_up": any(a["is_sent"] for a in after),
                "marked_done": key in done_keys,
            })
            break                          # 메일당 한 건만 (가장 앞 문장)
    out.sort(key=lambda p: (p["due"] or date(9999, 1, 1), -p["days"]))
    return out


def extract(store, today: str | None = None, max_days: int = PROMISE_MAX_DAYS) -> list[dict]:
    """내가 한 약속 중 **후속이 없는 것**. 최신 기한/최근 순으로 돌려준다.

    '미이행'이라고 단정하지 않는다 — 다른 스레드나 메일 밖에서 처리했을 수 있다.
    리포트는 "그 뒤 내가 보낸 것 없음"이라는 사실만 적고, 사용자가 '처리함'으로
    접을 수 있게 한다(store.report_done).
    """
    day = date.fromisoformat(today) if today else date.today()
    floor = (day - timedelta(days=max_days)).isoformat()
    # 위쪽도 막는다 — 안 막으면 **지난 날짜의 리포트**에 그 뒤에 한 약속이 섞여
    # "-2일 전"처럼 음수 경과일이 찍힌다(2026-08-01 실기기 데이터에서 확인).
    return [p for p in _scan(store, floor, day.isoformat(), day)
            if not p["followed_up"] and not p["marked_done"]]


def review_period(store, start: str, end: str, today: str | None = None) -> dict:
    """그 기간에 한 약속이 **지금** 어떻게 됐나 — 지난 차수 점검용.

    kept  = 그 뒤 내가 그 스레드에 보냈거나(정황상 이행) 사용자가 '처리함'으로 접은 것
    open  = 아직 내 후속이 없는 것

    open 을 '안 지켰다'로 쓰지 않는다 — 다른 스레드나 메일 밖에서 처리했을 수 있다.
    14일 컷(extract)과 무관하게 **그 차수 기간**만 본다.
    """
    day = date.fromisoformat(today) if today else date.today()
    items = _scan(store, start, end, day)
    kept = [p for p in items if p["followed_up"] or p["marked_done"]]
    keys = {p["key"] for p in kept}
    return {"kept": kept, "start": start, "end": end,
            "open": [p for p in items if p["key"] not in keys]}
