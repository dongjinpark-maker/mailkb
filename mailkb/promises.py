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


def _first_promise(text: str) -> str | None:
    """본문에서 확정 어미 약속 문장 하나(가장 앞) — 내 약속·받을 것이 같은 규칙을 쓴다.

    보존 인용(mid-join, PRESERVED_MARK 아래)은 **다른 사람이 쓴 글**이라 뗀다 —
    안 떼면 남의 확정 어미가 첫 매치가 된다(2026-08-01 실증)."""
    for s in _sentences(strip_preserved(text or "")):
        if not (8 <= len(s) <= 120) or not _PROMISE_RX.search(s):
            continue
        if _NOT_PROMISE_RX.search(s):
            continue
        return s
    return None


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
        # 보존 인용은 상대가 쓴 글이다 — _first_promise 가 떼고 본다.
        s = _first_promise(r["new_content"])   # 메일당 한 건만 (가장 앞 문장)
        if s is None:
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


# ------------------------------------------------------------------ 받을 것
# 상대가 나에게 한 약속(2026-10-25). 내 약속의 거울이다 — 같은 확정 어미 규칙을
# **나를 To 에 넣은 상대 메일**에 그대로 쓴다. 2026-07-30 에 폐기한 '답 기다리는
# 내 발신'과 성격이 다르다: 그것은 내 메일이 요청인지를 정규식으로 **추측**했고
# ("별첨 참고 바랍니다"가 1위), 이것은 상대가 직접 쓴 확정 문장을 **인용**한다.

def _scan_owed(store, since: str, until: str | None, day: date,
               direct_to: int, is_noise=None) -> list[dict]:
    """[since, until] 사이 나를 To 에 넣은 수신 메일에서 상대의 확정 약속을 뽑는다.

    - To 가 direct_to 명 이하일 때만 — 단체 메일의 "공유하겠습니다"는 나에게 한
      약속이 아니다(주간 `_direct_to_me` 와 같은 정의).
    - 노이즈 발신(시스템 알림 등)과 숨긴 스레드는 뺀다 — 숨김은 "조용히 하라"다.
    - **해제**: 그 뒤 **같은 사람**이 그 스레드에 다시 보냈으면 뺀다(보냈거나
      진행을 알렸다는 정황). 내 회신이나 다른 사람의 메일로는 풀리지 않는다 —
      내가 재촉한 것은 상대가 지킨 것이 아니다. '처리함'으로 접은 것도 뺀다."""
    me = {a.lower() for a in store.my_addresses}
    hidden = store.hidden_thread_ids()
    done_keys = store.report_done_keys("owed")
    sql = ("SELECT id, thread_id, sent_on, subject, new_content, sender_addr, "
           "sender_name, to_addrs FROM messages WHERE is_sent=0 AND sent_on >= ?")
    args = [since]
    if until:
        sql += " AND sent_on < date(?, '+1 day')"
        args.append(until)
    out = []
    for r in store.db.execute(sql + " ORDER BY sent_on", args).fetchall():
        if r["thread_id"] in hidden:
            continue
        sender = (r["sender_addr"] or "").lower()
        if not sender or (is_noise and is_noise(sender)):
            continue
        tos = [a.strip().lower() for a in (r["to_addrs"] or "").split(";")
               if a.strip()]
        if not (set(tos) & me) or len(tos) > direct_to:
            continue
        s = _first_promise(r["new_content"])
        if s is None:
            continue
        key = store.report_key(r["id"], s)
        after = store.db.execute(
            "SELECT sender_addr FROM messages WHERE thread_id=? AND id>?",
            (r["thread_id"], r["id"])).fetchall()
        sent_day = datetime.fromisoformat(r["sent_on"]).date()
        m = _WHEN_RX.search(s)
        out.append({
            "key": key, "thread_id": r["thread_id"], "message_id": r["id"],
            "subject": r["subject"] or "", "quote": s,
            "who": r["sender_name"] or r["sender_addr"] or "",
            "addr": sender, "sent_on": r["sent_on"],
            "days": (day - sent_day).days,
            "due": resolve_when(m.group(0), sent_day) if m else None,
            "followed_up": any((a["sender_addr"] or "").lower() == sender
                               for a in after),
            "marked_done": key in done_keys,
        })
    out.sort(key=lambda p: (p["due"] or date(9999, 1, 1), -p["days"]))
    return out


def extract_owed(store, today: str | None = None,
                 max_days: int = PROMISE_MAX_DAYS, direct_to: int = 4,
                 is_noise=None) -> list[dict]:
    """상대가 나에게 한 약속 중 **그 뒤 그 사람에게서 소식이 없는 것**.

    '안 지켰다'고 단정하지 않는다 — 메일 밖이나 다른 스레드로 왔을 수 있다.
    사실("그 뒤 이 사람이 이 스레드에 보낸 것 없음")만 적고 '처리함'으로 접게 한다.
    """
    day = date.fromisoformat(today) if today else date.today()
    floor = (day - timedelta(days=max_days)).isoformat()
    return [p for p in _scan_owed(store, floor, day.isoformat(), day,
                                  direct_to, is_noise)
            if not p["followed_up"] and not p["marked_done"]]

