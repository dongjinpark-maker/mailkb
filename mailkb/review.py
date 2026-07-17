"""하루 끝 회고.

결정론 계층(SQL + 규칙)이 먼저고, AI 는 그 위의 판단만 맡는다:
  결정론: 오늘 보낸 것 / 미답변 / 기한 신호  ← AI 없이 항상 동작
  AI:     롤링 요약 갱신 + 결정·누락·side effect 분석 ← --ai 일 때만
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import date, timedelta

from . import actions, features
from . import search as search_mod
from .clean import strip_preserved
from .config import Config
from .features import DECISION_RX, DEADLINE_RX, REQUEST_RX, is_trivial_msg
from .store import Store

# Compatibility aliases for callers and tests using review's historical names.
_DECISION_RX = DECISION_RX
_REQUEST_RX = REQUEST_RX
_is_trivial_msg = is_trivial_msg


def _line_at(text: str, pos: int) -> str:
    """text[pos] 가 속한 한 줄 (매치 스니펫용)."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start : end if end != -1 else len(text)].strip()


# '무의미 한 줄' 판정은 features.is_trivial_msg 로 이관(L2 상태기계와 공유,
# 2026-07-17) — 위 별칭 _is_trivial_msg 가 종전 이름을 유지한다.


def _subject_noise(cfg: Config, subject: str, *, i_replied: bool, n_to: int) -> bool:
    """제목 노이즈 2단계 판정.

    강한 노이즈(시스템 알림/설문 등)는 참여 무관 무조건 노이즈.
    약한 노이즈(주간보고 등)는 내가 답장하지 않았고 수신 3인 이상 대량일 때만.
    """
    if cfg.is_noise_subject_strong(subject or ""):
        return True
    return (cfg.is_noise_subject_weak(subject or "")
            and not i_replied and n_to >= 3)

# 개입 큐 카테고리 (우선순위 순 — 스레드는 최상위 1곳에만)
CATEGORIES = [
    ("decide", "🔴 결정 필요"),
    ("respond", "🟠 회신 필요"),
    ("stalled_mine", "🟡 내가 넘긴 공(정체)"),
    ("stalled_thread", "⚪ 멈춘 주요 스레드"),
]


def day_label(item: dict) -> str:
    """항목의 경과 표기. 정체 카테고리는 영업일, 그 외는 달력 D+."""
    if item["category"].startswith("stalled"):
        return f"영업 {item['days']}d"
    return f"D+{item['days']}"


def _lead_line(content: str) -> str:
    """스니펫용 첫 '의미 있는' 줄 — 짧은 호칭/인사말 줄은 건너뛴다."""
    for ln in (content or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= 8 and (ln.endswith(("님,", "님", ",")) or ln.startswith("안녕")):
            continue
        return ln
    stripped = (content or "").strip().splitlines()
    return stripped[0].strip() if stripped else ""


def _mentions_me(content: str, names) -> bool:
    """본문이 나를 명시적으로 언급하는가 (이름/호칭 부분 매치, 2자 이상)."""
    c = (content or "").lower()
    return any(n.lower() in c for n in names if n and len(n) >= 2)


def _workdays_since(sent_on_iso: str, today_iso: str, holidays=()) -> int:
    """sent_on 다음 날부터 today 까지의 영업일 수 (주말·holidays 제외).

    같은 날/미래면 0. 금요일 발신 → 다음 월요일이 today 면 1.
    """
    try:
        d0 = date.fromisoformat(sent_on_iso[:10])
        d1 = date.fromisoformat(today_iso[:10])
    except (ValueError, TypeError):
        return 0
    if d1 <= d0:
        return 0
    hol = set(holidays or ())
    n, cur = 0, d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() >= 5:  # 토(5)·일(6)
            continue
        if cur.isoformat() in hol:
            continue
        n += 1
    return n


def thread_kind(cfg: Config, msgs) -> str:
    """오늘 활동 스레드 분류: 'spam'(수신 전원 노이즈) | 'notice'(대량발송·내 참여X) | 'work'.

    update_rolling_summaries 의 스킵 조건과 동일한 기준 — AI 요약에서 빠지는 것이
    곧 스팸/공지다. 나머지가 '업무'.
    """
    inbound = [m for m in msgs if not m["is_sent"]]
    if inbound and all(cfg.is_noise(m["sender_addr"]) for m in inbound):
        return "spam"
    subj = msgs[0]["subject"] if msgs else ""
    if cfg.is_noise_subject_strong(subj):
        return "spam"
    mine = any(m["is_sent"] for m in msgs)
    to_counts = [len([a for a in (m["to_addrs"] or "").split(";") if a])
                 for m in msgs]
    all_broadcast = bool(msgs) and all(n >= cfg.broadcast_to for n in to_counts)
    if all_broadcast and not mine:
        return "notice"
    if (not mine and cfg.is_noise_subject_weak(subj)
            and max(to_counts, default=0) >= 3):
        return "notice"
    return "work"


def today_digest(store: Store, cfg: Config, date_iso: str) -> dict:
    """오늘 활동한 '업무' 스레드별 핵심 한 줄(결정론: 첫 의미 줄) + 분류 카운트.

    공지(대량발송)·스팸은 개수만 세고 목록에서 뺀다. review --ai 는 ai_digest 로
    lead 를 진짜 한 줄 요약(ai_core)으로 덮어쓴다.
    """
    work: list[dict] = []
    n_spam = n_notice = 0
    for tid in store.threads_active_on(date_iso):
        msgs = store.thread_messages(tid)
        if not msgs:
            continue
        kind = thread_kind(cfg, msgs)
        if kind == "spam":
            n_spam += 1
            continue
        if kind == "notice":
            n_notice += 1
            continue
        last = msgs[-1]
        # 발신인 = 스레드 상대방. 마지막이 내 답장(→)이면 내 이름 대신
        # 직전 수신자(내가 답한 사람)를 보여준다.
        inbound = [m for m in msgs if not m["is_sent"]]
        origin = inbound[-1] if (last["is_sent"] and inbound) else last
        work.append({
            "thread_id": tid,
            "subject": msgs[0]["subject"],
            "is_sent": bool(last["is_sent"]),
            "who": origin["sender_name"] or origin["sender_addr"],
            "lead": _lead_line(strip_preserved(last["new_content"] or "")),
            "ai_core": "",
            "last_on": last["sent_on"],
        })
    work.sort(key=lambda x: x["last_on"], reverse=True)
    return {"work": work, "n_spam": n_spam, "n_notice": n_notice}


def deadline_signals(store: Store, cfg: Config, date_iso: str) -> list[tuple[str, str]]:
    """오늘 수신 메일에서 기한 문장 추출 (규칙 기반 — 요청 프록시 분리 후 순수 기한).

    노이즈 발신과 대량 발송(To 3인 이상 — 전사 공지의 "금일 18시까지" 류)은
    개인 액션 신호가 아니므로 제외.
    """
    signals = []
    for m in store.received_on_date(date_iso):
        if cfg.is_noise(m["sender_addr"]):
            continue
        if cfg.is_noise_subject_strong(m["subject"]):
            continue  # Invitation 류의 "…까지" 오염 차단
        if len([a for a in m["to_addrs"].split(";") if a]) >= 3:
            continue
        # 보존 인용(mid-join)은 신호 대상이 아님 — 신규 작성분만, 문장 게이팅
        # 적용("금요일까지 완료했습니다" 같은 완료 문맥 기한 제외 — L1 과 동일 기준)
        body = strip_preserved(m["new_content"])
        for s in features.split_sentences(body):
            if DEADLINE_RX.search(s) and not features.sentence_gate(s)[2]:
                signals.append((m["subject"], s.strip()))
                break
    return signals


def filtered_unanswered(store: Store, cfg: Config) -> list:
    """노이즈(발신자·강한 제목) 걸러낸 미답변 목록 — 개입 큐·데일리·웹 홈·CLI 공용.

    제목 필터는 강한 노이즈만 — unanswered 행에는 my_msg_count 가 없어
    약한(미참여+대량) 판정이 불가하다. 약한 필터는 개입 큐/디제스트에서만.
    """
    return [r for r in store.unanswered(max_recipients=cfg.broadcast_to)
            if not cfg.is_noise(r["sender_addr"])
            and not cfg.is_noise_subject_strong(r["subject"])]


def _days_between(sent_on_iso: str, today_iso: str) -> int:
    """달력일 경과 — 액션 원본 메일 기준 D+ 표기."""
    try:
        return max(0, (date.fromisoformat(today_iso[:10])
                       - date.fromisoformat(sent_on_iso[:10])).days)
    except (ValueError, TypeError):
        return 0


def intervention_queue(
    store: Store,
    cfg: Config,
    date_iso: str | None = None,
    unanswered: list | None = None,
    return_candidates: bool = False,
) -> list[dict]:
    """'무엇에 개입해야 하나' 결정론 액션 큐.

    decide/respond 는 공통 판정기(actions.classify_threads)의 REQUIRED —
    웹 ↩ 탭·스레드 상세와 정의상 같은 집합이다(홈·웹 불일치 제거, 2026-07-17).
    본문 정규식 재실행 없음: 저장 신호(L1)·액션 상태(L2)의 좁은 조인으로 판정하고,
    스니펫·근거 문장만 신호 원본 메시지에서 복원한다.

      decide         결정 필요 (REQUIRED · 결정 요청)
      respond        회신 필요 (REQUIRED · 그 외 요청/질문)
      stalled_mine   내가 넘긴 공 (내가 마지막·영업 stall_workdays 넘게 무응답·요청 포함)
      stalled_thread 멈춘 스레드 (열림·2통+·내 참여·영업 stale_workdays 넘게 무활동)

    return_candidates=True 면 (items, candidates) 반환. candidates 는 판정기의
    MAYBE(확인 후보) — 홈 접힌 목록으로 노출되고 AI(haiku) 분류가 실제 액션을
    다시 건져 FN(놓침)을 줄이는 후보 풀이다. unanswered 인자는 구 시그니처
    호환용으로 받되 더는 판정에 쓰지 않는다.
    """
    d = date_iso or date.today().isoformat()
    stall, stale = cfg.stall_workdays, cfg.stale_workdays
    bcast, holidays = cfg.broadcast_to, cfg.holidays
    me_names = [n for n in cfg.my_names if n] + [
        a.split("@")[0] for a in cfg.my_addresses if a]

    acts = actions.classify_threads(store, cfg)
    # 신호 원본 본문(스니펫·근거용) — REQUIRED/MAYBE 만 배치 조회
    src_ids = [a.source_id for a in acts.values()
               if a.level != actions.NONE and a.source_id]
    src = {m["id"]: m for m in store.messages_by_ids(src_ids)}

    tails = store.open_thread_tails()
    workdays: dict[int, int] = {}
    stale_ids: list[int] = []
    for t in tails:
        tid = t["thread_id"]
        wd = _workdays_since(t["sent_on"], d, holidays)
        workdays[tid] = wd
        n_to = len([a for a in (t["to_addrs"] or "").split(";") if a])
        if (t["msg_count"] >= 2 and wd >= stale and n_to < bcast
                and t["last_is_sent"] == 0):
            stale_ids.append(tid)

    # 멈춘 스레드 후보의 수신 발신자를 한 번에 읽어 N개의 thread_messages()를 없앤다.
    inbound_seen: set[int] = set()
    inbound_real: set[int] = set()
    for start in range(0, len(stale_ids), 800):
        chunk = stale_ids[start:start + 800]
        marks = ",".join("?" for _ in chunk)
        for row in store.db.execute(
                f"SELECT thread_id, sender_addr FROM messages "
                f"WHERE is_sent=0 AND thread_id IN ({marks})", chunk):
            tid = row["thread_id"]
            inbound_seen.add(tid)
            if not cfg.is_noise(row["sender_addr"]):
                inbound_real.add(tid)

    items: list[dict] = []
    candidates: list[dict] = []
    for t in tails:
        # (숨김 스레드는 open_thread_tails 가 이미 제외한다)
        tid = t["thread_id"]
        to = [a for a in (t["to_addrs"] or "").split(";") if a]
        a = acts.get(tid)
        wd = workdays[tid]

        # 수동 해제(상세 칩 ✕)한 요청 건은 정체 카테고리로도 재등장하지 않는다
        # — "이 건은 됐어"를 존중. 새 요청이 오면 해제가 풀리며 함께 복귀.
        if a and "user_dismissed" in a.reasons:
            continue
        if a and a.level == actions.REQUIRED:
            m = src.get(a.source_id)
            content = strip_preserved(m["new_content"] or "") if m else ""
            items.append({
                "category": "decide" if a.kind == "decide" else "respond",
                "thread_id": tid,
                "subject": t["subject"],
                "who": a.sender_name or a.sender_addr,
                "days": _days_between(a.sent_on, d),
                "snippet": (actions.evidence_from_body(content)
                            or _lead_line(content))[:120],
                "tag": "⏰" if a.has_deadline else "",
                # 나 지목·내 참여·직접 수신(decide 포함) — ★ 정렬 우선
                "personal": bool(a.named or a.participated
                                 or a.kind == "decide"),
                "reason": a.reason_text(),
            })
            continue
        if a and a.level == actions.MAYBE:
            m = src.get(a.source_id)
            content = strip_preserved(m["new_content"] or "") if m else ""
            candidates.append({
                "thread_id": tid,
                "subject": t["subject"],
                "who": a.sender_name or a.sender_addr,
                "snippet": _lead_line(content)[:120],
                "days": _days_between(a.sent_on, d),
                "content": content[:400],
                "tag": "⏰" if a.has_deadline else "",
                "reason": a.reason_text(),
            })
            continue

        # ── 정체 2종 (종전 논리 — 저장 신호로 본문 재스캔 없이) ──
        if _subject_noise(cfg, t["subject"],
                          i_replied=t["my_msg_count"] >= 1, n_to=len(to)):
            continue
        broadcast = len(to) >= bcast
        content = strip_preserved(t["new_content"] or "")
        signal = bool(t["last_has_decision"] or t["last_has_deadline"]
                      or t["last_has_question"] or t["last_has_request"])
        cat = who = snippet = None
        personal = False
        days = t["days_old"]
        if t["last_is_sent"] == 1 and wd >= stall and not broadcast and signal:
            cat, who, days = "stalled_mine", (to[0] if to else "?"), wd
            snippet = actions.evidence_from_body(content) or _lead_line(content)
            personal = True
        elif (t["msg_count"] >= 2 and wd >= stale and not broadcast
              and t["last_is_sent"] == 0):
            # 마지막이 수신일 때만 '멈춘 스레드' — 내가 마지막에 마무리한 스레드
            # (참석합니다/정상 진행 중 등)는 정체가 아니므로 제외.
            participates = bool(t["my_msg_count"] or t["addressed_to_me_count"])
            all_noise = tid in inbound_seen and tid not in inbound_real
            if participates and not all_noise:
                cat, who, days = "stalled_thread", t["sender_name"] or t["sender_addr"], wd
                snippet = _lead_line(content)
                personal = bool(t["my_msg_count"]) or _mentions_me(content, me_names)
        if not cat:
            continue
        items.append({
            "category": cat,
            "thread_id": tid,
            "subject": t["subject"],
            "who": who,
            "days": days,
            "snippet": (snippet or "")[:120],
            "tag": "⏰" if t["last_has_deadline"] else "",
            "personal": personal,
        })

    # 오래 방치된 항목은 큐에서 내림(기본 21일 초과 — 더는 '지금 할 일'이 아님).
    # 스레드 자체는 목록·↩ 필터에 그대로 남고, 웹 항목의 ✕(숨기기)로 명시 제거도 가능.
    # review.queue_max_days 로 조정 (0 = 상한 없음).
    max_days = int(cfg.opt("review", "queue_max_days", default=21) or 0)
    if max_days > 0:
        items = [it for it in items if it["days"] <= max_days]
    order = {k: i for i, (k, _) in enumerate(CATEGORIES)}
    # 카테고리 우선순위 → 나를 지목한 것(personal) 먼저 → 오래된 것 먼저
    items.sort(key=lambda it: (order.get(it["category"], 99),
                               0 if it.get("personal") else 1, -it["days"]))
    if return_candidates:
        return items, candidates
    return items


def deterministic(store: Store, cfg: Config, date_iso: str | None = None) -> dict:
    d = date_iso or date.today().isoformat()
    unanswered = filtered_unanswered(store, cfg)
    intervention, candidates = intervention_queue(
        store, cfg, d, unanswered=unanswered, return_candidates=True)
    # 데일리 '미답변/후속 필요'도 개입 큐와 같은 기준으로 — 요청 없는 정보/FYI·
    # 종결 메일은 빼고, 개입 큐에 남은(실제 액션이 걸린) 미답변만 노출한다.
    actionable_ids = {it["thread_id"] for it in intervention}
    unanswered_actionable = [r for r in unanswered
                             if r["thread_id"] in actionable_ids]
    return {
        "date": d,
        "sent": list(store.sent_on_date(d)),
        "received_count": len(store.received_on_date(d)),
        "unanswered": unanswered_actionable,
        "deadlines": deadline_signals(store, cfg, d),
        "intervention": intervention,
        "intervention_candidates": candidates,   # AI(haiku) 분류 후보 (FN 축소)
        "digest": today_digest(store, cfg, d),
    }


# ------------------------------------------------------------------- AI 계층
# AI 어댑터 — subprocess 호출만 (구 ai.py 병합, 2026-07-10 구조 재편).
# SDK, API 키, HTTP 클라이언트 없음. 인증·프록시·모델 관리는 opencode/claude
# CLI 쪽에 무임승차한다. subprocess/shutil 은 stdlib 라 상시 로드 비용 무시 가능.


class AIError(RuntimeError):
    pass


def _ai_resolve(cmd: list[str]) -> list[str]:
    """cmd[0] 을 PATH 에서 절대경로로 해석.

    Windows 에서 npm 설치 CLI(opencode/claude)는 .cmd 셔틀 파일인데,
    shell 없는 CreateProcess 는 확장자를 해석하지 못해 이름만으로는
    FileNotFoundError 가 난다. shutil.which 는 PATHEXT 를 존중한다.
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        raise FileNotFoundError(cmd[0])
    return [exe] + cmd[1:]


def _ai_run_once(cmd: list[str], prompt: str, timeout: int) -> str:
    """단발 호출. transient 실패는 AIError 로, 설정 문제는 FileNotFoundError 로."""
    try:
        proc = subprocess.run(
            _ai_resolve(cmd),
            input=prompt,
            capture_output=True,
            # Windows 기본 인코딩(cp949)은 메일 본문의 이모지 등에서 죽는다
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        # 명령 자체가 없음 — 재시도해도 소용없으므로 그대로 전파(루프 밖에서 처리)
        raise
    except subprocess.TimeoutExpired:
        raise AIError(f"AI 호출 시간 초과 ({timeout}s): {' '.join(cmd)}")
    if proc.returncode != 0:
        raise AIError(
            f"AI 호출 실패 (exit {proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()[:500]}"
        )
    out = proc.stdout.strip()
    if not out:
        raise AIError("AI 응답이 비어 있음")
    return out


def ai_run(cmd: list[str], prompt: str, timeout: int = 300, retries: int = 2) -> str:
    """프롬프트를 stdin 으로 전달하고 stdout 을 돌려받는다.

    일시적 실패(타임아웃·비정상 종료·빈 응답)는 지수 백오프로 재시도한다.
    명령을 찾을 수 없는 경우(설정/PATH 문제)는 재시도 없이 즉시 실패시킨다.
    """
    try:
        last: AIError | None = None
        for attempt in range(retries + 1):
            try:
                return _ai_run_once(cmd, prompt, timeout)
            except AIError as e:
                last = e
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))  # 2s, 4s
        assert last is not None
        raise last
    except FileNotFoundError:
        raise AIError(f"명령을 찾을 수 없음: {cmd[0]} — 설치/PATH 확인")


# ------------------------------------------------------------------ 프롬프트

SUMMARY_UPDATE = """당신은 업무 메일 스레드의 요약을 관리한다. 아래 기존 요약에 새 메일 내용을 반영해 갱신하라.

규칙:
- 5줄 이내, 한국어
- 결정된 사항과 그 근거를 최우선으로 보존
- 미해결 질문/요청과 담당자를 명시
- 날짜가 중요하면 유지
- 제목·머리말('갱신된 요약' 등)·군더더기 없이 요약 본문만

[기존 요약]
{existing}

[새 메일들]
{new_messages}

위를 반영한 요약 본문만 출력하라."""

THREAD_DIGEST = """다음은 오늘 활동이 있었던 '업무' 메일 스레드들의 요약이다(공지·스팸은 이미 제외됨).
각 스레드의 핵심을 딱 한 줄로 압축하라 (한국어).

규칙:
- 스레드당 정확히 한 줄: `#번호: 핵심`
- 핵심 = 지금 무엇이 관건인지 / 내가 알아야 할 결론·요청. 인사말·군더더기 금지, 30자 내외.
- 없는 번호를 만들지 마라.

[스레드]
{items}

각 스레드 한 줄씩만 출력:"""

INTERVENTION_REFINE = """당신은 담당자의 '개입 필요' 업무 큐를 다듬는다. 아래는 기계가 분류한 후보 항목들(스레드 요약 포함)과 담당자가 준 추가 정보다.

각 항목을 판단하라 (한국어):
- 긴급도: 상 / 중 / 하
- 사유: 왜 지금 개입이 필요한지 한 줄
- 제안: 다음 행동 한 줄 (누구에게 무엇을)
- 이미 처리됐거나 개입 불필요로 보이면 '상태:처리됨' 을 덧붙여라

출력 형식 — 항목당 정확히 한 줄, 긴급도 높은 순으로:
- [#스레드번호] 긴급도:상|중|하 · 사유: … · 제안: … · (필요시)상태:처리됨

규칙: 추가 정보가 특정 스레드를 언급하면 그 판단을 우선 반영하라. 후보에 없는 번호는 만들지 마라. 군더더기 없이.

{rules}[담당자 추가 정보]
{context}

[후보 항목]
{items}
"""

# 개입 큐 '분류' — 지금 내 액션(회신·결정·처리)이 필요한지 이지선다. 값싼 haiku 용.
# 결정론 규칙이 못 가르는 경계(종결/FYI/답변 vs 실제 요청)를 의도로 판정해
# FP(오탐)·FN(놓침)을 동시에 줄인다. 판정 근거는 원문만 — 롤링 요약은 참조하지
# 않는다(2026-07-12: 요약의 누락·왜곡이 판정에 번지는 것 차단, 대신 직전 대화
# 원문을 넣는다. haiku 입력 토큰은 싸다).
CLASSIFY_INTERVENTION = """당신은 담당자의 받은 메일이 '지금 내 액션이 필요한지'를 분류한다.

기준:
- 필요 = 내가 회신·결정·처리해야 끝나는 것 (질문/요청, 결정·판단 요청, 내 확인이 남은 완료 보고).
- 불필요 = 정보 공유(FYI)·단순 통보/공지, 종결 인사("확인했습니다"·"감사합니다"·"참석합니다"),
  내 질문에 이미 도착한 답변, 상대가 알아서 진행하겠다는 것.
- '직전 대화'가 있으면 흐름을 근거로 판단하라 — 내가 먼저 요청한 것에 온 완료 보고인지,
  상대가 새로 요구하는 것인지, 이미 끝난 대화의 종결 인사인지.

확실치 않으면 `불명`. 억지로 필요/불필요를 고르지 말고 불명으로 남겨라(보수적으로 처리됨).
출력: 항목당 정확히 한 줄 `#번호: 필요` 또는 `#번호: 불필요` 또는 `#번호: 불명`.
설명·군더더기·없는 번호 금지.

[예시 입력]
[#901] 자료공유
  마지막 메일: 지난주 세미나 자료 공유드립니다. 참고 바랍니다.
[#902] 일정확정
  직전 대화:
    (07-01 나) 킥오프 후보일로 7/10 또는 7/11 제안드립니다.
  마지막 메일: 일정 확정했습니다. 세부안 회신 부탁드립니다.
[#903] 검토완료
  직전 대화:
    (07-02 나) 첨부 문서 검토 부탁드립니다.
  마지막 메일: 확인했습니다. 이상 없습니다.
[#904] 리뷰
  직전 대화:
    (07-03 나) feature 브랜치 리뷰 요청드립니다.
  마지막 메일: 요청하신 브랜치 리뷰 완료했습니다. 코멘트 확인 바랍니다.
[예시 출력]
#901: 불필요
#902: 필요
#903: 불필요
#904: 필요

[분류할 항목]
{items}
"""

# PC 쪽 claude 가 나중에 classify.jsonl 을 읽어 정확도를 분석하고 프롬프트를
# 개선하도록 만드는 지시문. 첫 로그 기록 시 <home>/logs/ANALYZE.md 로 자동 저장 →
# 사용자가 PC에서 `claude -p < ANALYZE.md` 에 로그를 함께 물려 실행하면 된다.
CLASSIFY_LOG_ANALYSIS = """# 개입 분류 로그 분석 지시문

너는 mailkb 의 '개입 필요' 분류기(haiku) 품질을 감사하는 검토자다.
같은 폴더의 `classify.jsonl` 이 분석 대상 데이터다.

## 데이터 형식
JSONL — 한 줄이 '하루치 분류 1회 실행'이다. 각 줄:
- date: 실행일, backend: 사용 모델, raw: 모델 원문 출력(형식 이탈 점검용)
- items[]: 이번 실행에서 분류한 메일들. 각 항목:
  - thread_id, subject
  - det:     결정론이 넣은 자리. "respond"=회신 필요 큐에 있던 것,
             "candidate"=결정론이 약한 신호라 뺐던 놓침 후보
  - verdict: 분류기 판정 — "필요"/"불필요"/"불명"/null(파싱 실패)
  - action:  최종 처리 — respond의 "dropped"(제외=FP주장)·"kept"(유지),
             candidate의 "promoted"(승격=FN회수 주장)·"dropped"(그대로 뺌)
  - history, body: 분류기에게 실제로 준 근거(직전 대화 원문 + 마지막 메일 본문)

## 할 일
1. 각 item 을 summary+body 만 보고 너 스스로 필요/불필요를 독립 판정하라
   (기준: 필요=내가 회신·결정·처리해야 끝남 / 불필요=FYI·통보·종결인사·이미 온 답변).
2. 분류기 verdict 와 대조해 두 오류를 뽑아라:
   - 거짓 양성(FP): 실제 불필요인데 큐에 남거나 승격된 것.
   - 거짓 음성(FN): 실제 필요인데 dropped 된 것(특히 det=candidate 인데 미승격,
     또는 respond 인데 불필요로 dropped).
3. 오류의 '체계적 패턴'을 찾아라 — 예: 완곡한 요청("~해주시면 감사")을 놓침,
   종결 인사를 요청으로 오탐, 내 확인이 남은 완료보고를 불필요 처리 등.
4. raw 를 훑어 형식 이탈(없는 번호·설명 첨부·verdict=null 다발)이 있으면 지적하라.

## 출력 (이 형식 고정, 군더더기 없이)
### 요약
- 총 판정 N건 · 추정 FP X건 · 추정 FN Y건 · 불명 Z건 · 형식이탈 W건
### FP 목록
- #thread (subject): 분류기=…, 내판정=필요, 근거 한 줄
### FN 목록
- #thread (subject): 분류기=…, 내판정=필요인데 dropped, 근거 한 줄
### 체계적 패턴
- 패턴명: 설명 + 해당 #thread 들
### 프롬프트 개선 제안
- CLASSIFY_INTERVENTION few-shot 에 추가할 예시(오분류였던 실제 항목을 축약해
  `[#…] 제목 / 요약 / 본문 → 정답판정` 형태로 3~6개)
- 기준 문구에 보탤 규칙 1~3줄
"""

# 요약 로그(summary.jsonl) 를 PC 쪽 claude 가 읽어 요약 품질을 평가·개선하도록.
# 첫 기록 시 <home>/logs/ANALYZE-summary.md 로 저장.
SUMMARY_LOG_ANALYSIS = """# 롤링 요약 로그 분석 지시문

너는 mailkb 의 스레드 '누적 요약'(haiku 아님, sonnet) 품질을 감사하는 검토자다.
같은 폴더의 `summary.jsonl` 이 분석 대상이다.

## 데이터 형식
JSONL — 한 줄 = '하루치 요약 실행 1회'. 각 줄: date, backend, items[].
items 각 항목(실제 생성된 요약만, 재사용·스킵은 제외):
- thread_id, subject
- msg_count: 스레드 총 메시지 수, new_msgs: 이번에 새로 반영한 메시지 수
- in_chars: 입력(신규 메시지) 길이, out_chars: 생성된 요약 길이
- summary: 생성된 요약 본문

## 요약이 하는 일 (평가 기준)
이 요약은 (1) 사람이 스레드 맥락을 빨리 파악하고, (2) 개입 분류기(haiku)에 스레드
히스토리 근거로 들어간다. 따라서 좋은 요약은:
- 미결 사항(결정·회신 필요), 요청·기한, 최근 상태를 보존한다.
- 사실에 충실(환각·과장 없음). 원문에 없는 단정 금지.
- 간결(불필요한 인사·수사 제거). out_chars 가 in_chars 에 육박하면 압축 실패.

## 할 일
1. 각 summary 를 읽고 위 기준으로 A(양호)/B(보완)/C(불량) 등급.
2. 문제 유형을 뽑아라 — 예: 미결/기한 누락, 환각, 너무 장황(압축비 나쁨),
   최신 메시지 반영 안 됨, 결론 없이 나열만.
3. 압축비(out_chars/in_chars)가 비정상(너무 크거나 1줄로 뭉갬)인 항목 표시.

## 출력 (형식 고정)
### 요약
- 총 N건 · A a건 / B b건 / C c건 · 평균 압축비 r
### 보완/불량 목록
- #thread (subject): 등급 · 문제유형 · 한 줄 근거
### 체계적 패턴
- 패턴명: 설명 + 해당 #thread 들
### 프롬프트 개선 제안
- SUMMARY_UPDATE 지시문에 보탤/고칠 규칙 1~3줄 (미결·기한 보존, 길이 상한 등)
"""


_SUMMARY_HEADER_RX = re.compile(
    r"^\s*(?:"
    r"(?:#{1,6}\s*)?\*{2,3}\s*갱신\s*된?\s*요약\s*\*{2,3}\s*[:：]?\s*"   # **갱신된 요약** (인라인 허용)
    r"|(?:#{1,6}\s*)?갱신\s*된?\s*요약\s*[:：]\s*"                        # 갱신된 요약: (인라인 허용)
    r"|(?:#{1,6}\s*)?갱신\s*된?\s*요약\s*(?:\n+|$)"                       # 갱신된 요약 (단독 줄만)
    r")")


def strip_summary_header(text: str) -> str:
    """모델이 붙이는 '**갱신된 요약**' 류 머리말 제거 — 저장·표시 양쪽 정리.
    기존에 머리말이 박힌 요약도 표시 시점에 걸러진다."""
    return _SUMMARY_HEADER_RX.sub("", (text or "").lstrip(), count=1).strip()


def update_rolling_summaries(
    store: Store, cfg: Config, thread_ids: list[int], backend: str | None,
    date_iso: str | None = None,
) -> dict[int, str]:
    """활동 스레드의 롤링 요약을 증분 갱신. 비용은 신규 내용에만 비례.

    실제로 생성한(재사용 아닌) 요약은 <home>/logs/summary.jsonl 에 누적한다
    — 추후 요약 품질 분석 재료(b). 재사용·스킵은 실행이 아니므로 로그 제외."""
    cmd = cfg.ai_cmd(backend)
    result: dict[int, str] = {}
    log_items: list[dict] = []
    # 짧은 스레드는 요약 스킵 — 한두 통은 원문이 곧 요약이라 콜 낭비. 단, 통수는
    # 대리 지표일 뿐이라 **실질 본문이 충분히 길면(기본 1000자+) 통수가 적어도
    # 요약 대상**(장문 기획안·정리 보고 1통 등). 카운트·글자수 모두 '++수신인
    # 추가'·FYI 류 무의미 메시지(_is_trivial_msg)는 제외. '의미' 판정은 아래
    # 노이즈/공지/제목 필터가 담당. ai.summary_min_msgs(1=문턱 해제)·
    # summary_min_chars(0=내용 우회로 끔)로 조정.
    min_msgs = max(1, int(cfg.opt("ai", "summary_min_msgs", default=3)))
    min_chars = max(0, int(cfg.opt("ai", "summary_min_chars", default=1000)))
    attempts = successes = consec = 0
    for tid in thread_ids:
        t = store.thread(tid)
        msgs = store.thread_messages(tid)
        if not t or not msgs:
            continue
        # 플래그(🚩) 스레드는 길이 문턱 면제 — "중요 표시한 건 짧아도 기억해라".
        # (노이즈/공지/제목 필터는 그대로 적용 — 아래에서 동일하게 거른다)
        if not t["flagged"]:
            subs = [m for m in msgs if not _is_trivial_msg(m["new_content"])]
            sub_chars = sum(len(m["new_content"] or "") for m in subs)
            if len(subs) < min_msgs and not (min_chars and sub_chars >= min_chars):
                continue
        # 수신 메일이 있고 그 발신이 전원 노이즈면 스킵.
        # 발신 전용 스레드(수신 0건 — 내가 통보한 결정 등)는 노이즈가 아니므로
        # 스킵하지 않는다 (all([])==True 로 잘못 걸리던 버그 수정).
        inbound = [m for m in msgs if not m["is_sent"]]
        if inbound and all(cfg.is_noise(m["sender_addr"]) for m in inbound):
            continue
        # 전부 대량 발송(To 3+)이고 내 참여가 없는 스레드(전사 공지류)도 스킵
        if not any(m["is_sent"] for m in msgs) and all(
            len([a for a in m["to_addrs"].split(";") if a]) >= 3 for m in msgs
        ):
            continue
        # 제목 노이즈 — thread_kind(spam/notice)와 동일 기준으로 요약 스킵
        if _subject_noise(
            cfg, msgs[0]["subject"],
            i_replied=any(m["is_sent"] for m in msgs),
            n_to=max((len([a for a in (m["to_addrs"] or "").split(";") if a])
                      for m in msgs), default=0),
        ):
            continue
        if t["summary_msg_count"] >= len(msgs):
            result[tid] = t["rolling_summary"]
            continue
        new_msgs = msgs[t["summary_msg_count"]:]
        # 신규분이 전부 무의미(++·FYI)면 AI 콜 없이 기존 요약 재사용.
        # 마커(summary_msg_count)는 안 전진 — 다음 실질 메시지가 오면
        # 이들까지 신규 blob 에 함께 들어가 한 번에 반영된다.
        if all(_is_trivial_msg(m["new_content"]) for m in new_msgs):
            result[tid] = t["rolling_summary"]
            continue
        blob = "\n---\n".join(
            f"[{m['sent_on'][:16]}] {m['sender_name']} → {m['to_addrs']}\n"
            f"제목: {m['subject']}\n{m['new_content']}"
            for m in new_msgs
        )
        prompt = SUMMARY_UPDATE.format(
            existing=t["rolling_summary"] or "(없음 — 새 스레드)",
            new_messages=blob,
        )
        attempts += 1
        try:
            summary = strip_summary_header(ai_run(cmd, prompt))
        except AIError as e:
            # 이 스레드만 실패 → 건너뛴다. summary_msg_count 가드가 남아 있으므로
            # 다음 활동(새 메시지)이나 창 안에 다시 들면 자동 재요약된다.
            # 단발 실패가 마커를 묶어 요약 창을 3일로 되감던 문제 수정 —
            # 연속 실패는 백엔드 다운/행으로 보고 예외를 올려 마커를 묶는다(아래·②).
            consec += 1
            if consec >= 2:
                raise AIError("요약 백엔드 연속 실패 — 점검 필요: "
                              + str(e).splitlines()[0][:80]) from e
            continue
        consec = 0
        successes += 1
        store.save_summary(tid, summary, len(msgs))
        result[tid] = summary
        log_items.append({
            "thread_id": tid, "subject": msgs[0]["subject"],
            "msg_count": len(msgs), "new_msgs": len(new_msgs),
            "in_chars": len(blob), "out_chars": len(summary),
            "summary": summary,
        })
    # ② 시도분이 전부 실패(성공 0)면 백엔드 문제로 보고 마커 전진을 막는다:
    #    ai_analysis 로 예외를 올려 set_state 를 건너뛰게 → 다음 실행이 같은 창 재시도.
    #    (단발 실패는 여기 안 걸림 → 마커 전진 → 2회차가 3일 소급 반복하지 않음.)
    if attempts and not successes:
        raise AIError(f"요약 생성 실패 ({attempts}건 시도, 성공 0) — 백엔드 점검 필요")
    if log_items and cfg.opt("ai", "summary_log", default=True):
        _log_summary(cfg, date_iso or date.today().isoformat(),
                     backend or cfg.ai_default, log_items)
    return result


def _summary_window(store: Store, cfg: Config, review_date: str) -> tuple[str, str]:
    """요약 대상 날짜 창 (start, end) — 첫/재실행 구분 없이 한 공식.

    start = max(마지막 실행일, 리뷰날짜 − (summary_max_days−1)), 단 리뷰날짜 초과 금지.
    - 매일 돌리면 마지막 실행일 ≈ 리뷰날짜라 사실상 '마지막 실행 이후'와 동일.
    - 오래 비워도(또는 첫 실행) 최대 summary_max_days(기본 1 — 오늘만)일 소급.
      건너뛴 날 소급이 필요하면 config 에서 2~3 으로 (비용 상한 트레이드오프).
      → 3일 넘게 비운 구간의 가장 오래된 날은 요약에서 빠질 수 있음(의도된 트레이드오프).
    이미 요약된 스레드는 증분 가드로 재호출 없이 스킵되므로 소급은 값싸다.
    """
    n = max(1, int(cfg.opt("ai", "summary_max_days", default=1)))
    floor = (date.fromisoformat(review_date) - timedelta(days=n - 1)).isoformat()
    last = store.get_state("last_summary")
    base = max(last, floor) if last else floor
    return min(base, review_date), review_date


def refresh_summaries(
    store: Store, cfg: Config, review_date: str, backend: str | None
) -> dict[int, str]:
    """요약 창의 활동 스레드 롤링 요약 갱신 + last_summary 마커 전진.

    (구 ai_analysis 의 요약 파트 — 회고 분석 콜은 데일리 '수확'(distill.harvest)
    으로 대체됨, 2026-07-12 Phase 1.)
    요약 계층이 예외 없이 끝난 뒤에만 마커 전진(실패 시 다음 실행이 같은 창 재시도).
    마커는 앞으로만 이동 — 과거 --date 백필이 창을 되감지 않게.
    """
    start, end = _summary_window(store, cfg, review_date)
    thread_ids = store.threads_active_between(start, end)
    summaries = update_rolling_summaries(
        store, cfg, thread_ids, backend, date_iso=review_date)
    last = store.get_state("last_summary")
    store.set_state("last_summary", max(last, review_date) if last else review_date)
    return summaries


def _rules_block(cfg: Config) -> str:
    """<home>/ai-rules.md 사용자 지침 — 판단형 프롬프트(분석·개입 정리)에만 주입."""
    rules = cfg.ai_rules_text()
    return f"[사용자 지침 — 우선 적용]\n{rules}\n\n" if rules else ""


_DIGEST_LINE_RX = re.compile(r"^\s*[-*]?\s*\[?#(\d+)\]?\s*[:：]\s*(.+)$")


def ai_digest(store: Store, cfg: Config, digest: dict,
              backend: str | None = None) -> dict:
    """업무 스레드 핵심을 AI 한 줄 요약으로 채운다(배치 1콜).

    캐시된 롤링 요약(없으면 결정론 lead)만 넣어 토큰을 바운드한다. 백엔드 미설정·
    실패 시 결정론 lead 를 그대로 둔다(graceful). digest 를 제자리 갱신 후 반환.
    """
    work = digest.get("work", [])
    if not work:
        return digest
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return digest
    lines = []
    for it in work:
        t = store.thread(it["thread_id"])
        ctx = (t["rolling_summary"] if t and t["rolling_summary"] else it["lead"]) or ""
        lines.append(f"[#{it['thread_id']}] {it['subject']}: {ctx.replace(chr(10), ' ')[:200]}")
    try:
        out = ai_run(cmd, THREAD_DIGEST.format(items="\n".join(lines)))
    except AIError:
        return digest
    cores: dict[int, str] = {}
    for raw in out.splitlines():
        m = _DIGEST_LINE_RX.match(raw)
        if m:
            cores[int(m.group(1))] = m.group(2).strip()
    for it in work:
        if it["thread_id"] in cores:
            it["ai_core"] = cores[it["thread_id"]]
    return digest


_REFINE_PRIORITY_RX = re.compile(r"긴급도\s*[:：]\s*(상|중|하)")


def _split_reason_action(line: str) -> tuple[str, str]:
    """refine 한 줄에서 사유/제안 추출 (· 구분, 관대 파싱)."""
    reason = action = ""
    for seg in re.split(r"[·|]", line):
        s = seg.strip(" -*")
        if s.startswith("사유"):
            reason = s.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif s.startswith("제안"):
            action = s.split("：", 1)[-1].split(":", 1)[-1].strip()
    return reason, action


_PRIO_ORDER = {"상": 0, "중": 1, "하": 2, None: 3}


def _sort_refined(queue: list[dict]) -> list[dict]:
    """AI 주석 반영 정렬: 처리됨 맨 아래 → 긴급도 상>중>하 → 카테고리 → 오래된 순."""
    order = {k: i for i, (k, _) in enumerate(CATEGORIES)}
    return sorted(queue, key=lambda it: (
        1 if it.get("ai_flag") else 0,
        _PRIO_ORDER.get(it.get("ai_priority"), 3),
        order.get(it["category"], 99),
        -it["days"],
    ))


def apply_saved_ai(queue: list[dict], ann: dict) -> list[dict]:
    """저장된(오늘자) AI 주석을 결정론 큐에 병합·정렬. 주석 없으면 원본 그대로."""
    if not ann:
        return queue
    for it in queue:
        a = ann.get(it["thread_id"])
        if a:
            it.update(a)
    return _sort_refined(queue)


def ai_refine_intervention(
    store: Store,
    cfg: Config,
    queue: list[dict],
    extra_context: str | None = None,
    backend: str | None = None,
    persist_date: str | None = None,
) -> list[dict]:
    """개입 큐를 AI 로 다듬는다 — 추가 정보로 재분류·우선순위·사유/제안 주석.

    결정론 큐가 근거(후보 풀). AI 는 항목에 ai_priority/ai_reason/ai_action/ai_flag
    를 붙이고 재정렬만 하며, 큐에 없는 스레드는 만들지 않는다(모르는 #id 무시).
    persist_date 를 주면 그 날짜로 결과를 저장(웹/act 가 새로고침해도 유지).
    백엔드 미설정·AI 실패 시 결정론 큐를 그대로 돌려준다(graceful).
    """
    if not queue:
        return queue
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return queue  # 백엔드 설정 없음 → 결정론 그대로

    cat_label = dict(CATEGORIES)
    blob_lines = []
    for it in queue:
        t = store.thread(it["thread_id"])
        summ = (t["rolling_summary"] if t and t["rolling_summary"] else it["snippet"]) or ""
        summ = summ.replace("\n", " ")[:200]
        blob_lines.append(
            f"- [#{it['thread_id']}] ({cat_label.get(it['category'], it['category'])}) "
            f"{it['who']}: {it['subject']} — {day_label(it)}\n  요약: {summ}"
        )
    prompt = INTERVENTION_REFINE.format(
        context=(extra_context or "(없음)"),
        items="\n".join(blob_lines),
        rules=_rules_block(cfg),
    )
    try:
        out = ai_run(cmd, prompt)
    except AIError:
        return queue  # 호출 실패 → 결정론 그대로

    ann: dict[int, dict] = {}
    for raw in out.splitlines():
        ids = re.findall(r"#(\d+)", raw)
        if not ids:
            continue
        tid = int(ids[0])
        pr = _REFINE_PRIORITY_RX.search(raw)
        reason, action = _split_reason_action(raw)
        ann[tid] = {
            "ai_priority": pr.group(1) if pr else None,
            "ai_reason": reason,
            "ai_action": action,
            "ai_flag": "처리됨" if "처리됨" in raw else "",
        }
    for it in queue:
        if it["thread_id"] in ann:
            it.update(ann[it["thread_id"]])
    if persist_date:
        for tid, a in ann.items():
            store.save_intervention_ai(
                persist_date, tid, a["ai_priority"] or "",
                a["ai_reason"], a["ai_action"], a["ai_flag"])

    return _sort_refined(queue)


_CLASSIFY_RX = re.compile(r"#(\d+)\s*[:：]?\s*(필요|불필요|불명)")
_CLS_BODY_CAP = 2000    # 분류에 넣을 마지막 수신 메일 본문 상한
_CLS_HIST_MSGS = 4      # 직전 대화 맥락으로 넣을 메시지 수 (마지막 메일 제외)
_CLS_HIST_CAP = 500     # 맥락 메시지당 본문 상한


def _classify_context(store: Store, tids: set[int]) -> dict[int, dict]:
    """분류 대상 스레드의 '직전 대화 + 마지막 메일 본문' 조회.

    스니펫(120~200자)만으론 요청/FYI/종결이 구분 안 되는 게 오분류의 주원인.
    롤링 요약은 참조하지 않는다(2026-07-12) — 요약의 누락·왜곡이 판정에 번지는
    것을 차단하고, 대신 원문 입력을 늘린다(직전 K통 — haiku 입력 토큰은 싸다)."""
    ctx: dict[int, dict] = {}
    for tid in tids:
        msgs = store.thread_messages(tid)
        if not msgs:
            continue
        hist_lines = []
        for m in msgs[-1 - _CLS_HIST_MSGS:-1]:
            who = "나" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
            body = " ".join((m["new_content"] or "").split())[:_CLS_HIST_CAP]
            if body:
                hist_lines.append(f"({m['sent_on'][5:10]} {who}) {body}")
        ctx[tid] = {
            "body": (msgs[-1]["new_content"] or "").strip()[:_CLS_BODY_CAP],
            "history": "\n".join(hist_lines),
        }
    return ctx


def _classify_lines(ambiguous: list[dict], ctx: dict[int, dict]) -> list[dict]:
    """프롬프트 항목 블록 + 로그용 (직전 대화/본문) 을 함께 만든다."""
    out = []
    for it in ambiguous:
        c = ctx.get(it["thread_id"], {})
        body = c.get("body") or (it.get("content") or it.get("snippet") or "").strip()
        body = body[:_CLS_BODY_CAP] or "(본문 없음)"
        out.append({
            "thread_id": it["thread_id"], "subject": it["subject"],
            "body": body, "history": c.get("history") or "",
        })
    return out


def _append_log(cfg: Config, fname: str, analyze_name: str,
                analyze_text: str, rec: dict) -> None:
    """<home>/logs/<fname> 에 JSONL 한 줄 추가 + 분석 지시문 1회 저장.
    실패해도 호출측 로직엔 영향 없게 전부 삼킨다(graceful)."""
    try:
        d = cfg.home / "logs"
        d.mkdir(parents=True, exist_ok=True)
        analyze = d / analyze_name
        if not analyze.exists():
            analyze.write_text(analyze_text, encoding="utf-8")
        with (d / fname).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _log_classify(cfg: Config, date_iso: str, backend: str | None,
                  raw: str, items: list[dict]) -> None:
    """분류 판정을 JSONL 로 누적 — 나중에 PC쪽 claude 가 정확도 분석/프롬프트
    개선에 쓴다(b)."""
    _append_log(cfg, "classify.jsonl", "ANALYZE-classify.md", CLASSIFY_LOG_ANALYSIS,
                {"date": date_iso, "backend": backend, "n": len(items),
                 "raw": raw, "items": items})


def _log_summary(cfg: Config, date_iso: str, backend: str | None,
                 items: list[dict]) -> None:
    """생성한 롤링 요약을 JSONL 로 누적 — 추후 요약 품질 분석 재료(b)."""
    _append_log(cfg, "summary.jsonl", "ANALYZE-summary.md", SUMMARY_LOG_ANALYSIS,
                {"date": date_iso, "backend": backend, "n": len(items),
                 "items": items})


def ai_classify_intervention(
    store: Store,
    cfg: Config,
    queue: list[dict],
    candidates: list[dict],
    backend: str | None = None,
    date_iso: str | None = None,
) -> list[dict]:
    """개입 큐의 경계 항목을 AI(haiku)로 '액션 필요?' 분류.

    - 대상 = respond 항목(오탐 후보) + candidates(놓침 후보). 고신뢰 카테고리
      (decide·stalled_*)는 건드리지 않는다.
    - '불필요'로 판정된 respond 는 제외(FP↓), '필요'로 판정된 candidate 는 승격(FN↓).
    - 명시 판정 없거나 '불명'인 respond 는 보수적으로 유지(놓침 방지).
    - 판정 근거는 원문만 — '직전 대화(_CLS_HIST_MSGS통) + 마지막 메일 본문'.
      롤링 요약은 참조하지 않는다(요약 왜곡이 판정에 번지지 않게, 2026-07-12).
    - 판정 결과는 <home>/logs/classify.jsonl 에 누적(추후 정확도 분석용).
    - 분류 백엔드 미설정(SystemExit)·호출 실패(AIError) 시 결정론 큐를 그대로 반환
      — AI 없어도 기본은 동작한다.
    """
    ambiguous = [it for it in queue if it["category"] == "respond"] + list(candidates)
    if not ambiguous:
        return queue
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return queue

    ctx = _classify_context(store, {it["thread_id"] for it in ambiguous})
    fields = _classify_lines(ambiguous, ctx)
    blocks = []
    for fd in fields:
        hist = ""
        if fd["history"]:
            indented = "\n".join("    " + ln for ln in fd["history"].splitlines())
            hist = f"  직전 대화:\n{indented}\n"
        blocks.append(
            f"[#{fd['thread_id']}] {fd['subject']}\n"
            + hist
            + f"  마지막 메일: {fd['body'].replace(chr(10), ' ')}"
        )
    try:
        out = ai_run(cmd, CLASSIFY_INTERVENTION.format(items="\n".join(blocks)))
    except AIError:
        return queue

    verdict: dict[int, bool] = {}     # True=필요 / False=불필요 (불명·미판정은 미설정)
    labels: dict[int, str] = {}
    for raw in out.splitlines():
        m = _CLASSIFY_RX.search(raw)
        if m:
            tid, lab = int(m.group(1)), m.group(2)
            labels[tid] = lab
            if lab == "필요":
                verdict[tid] = True
            elif lab == "불필요":
                verdict[tid] = False

    result = [it for it in queue if it["category"] != "respond"]
    kept = set()
    for it in queue:
        if it["category"] != "respond":
            continue
        if verdict.get(it["thread_id"]) is False:   # 명시적 불필요만 제외
            continue
        if verdict.get(it["thread_id"]) is True:
            it["ai_kept"] = True
        result.append(it)
        kept.add(it["thread_id"])
    for c in candidates:
        if verdict.get(c["thread_id"]) is True and c["thread_id"] not in kept:
            result.append({
                "category": "respond", "thread_id": c["thread_id"],
                "subject": c["subject"], "who": c["who"], "days": c["days"],
                "snippet": c["snippet"], "tag": c.get("tag", ""),
                "personal": False, "ai_promoted": True,
            })
            kept.add(c["thread_id"])

    # 판정 로그 (b) — det(결정론 배치)·verdict·최종 action 을 함께 남긴다.
    if cfg.opt("ai", "classify_log", default=True):
        det_of = {it["thread_id"]: "respond" for it in queue
                  if it["category"] == "respond"}
        for c in candidates:
            det_of.setdefault(c["thread_id"], "candidate")
        log_items = []
        for fd in fields:
            tid = fd["thread_id"]
            det = det_of.get(tid, "?")
            v = verdict.get(tid)
            if det == "respond":
                action = "dropped" if v is False else "kept"
            else:
                action = "promoted" if (v is True and tid in kept) else "dropped"
            log_items.append({
                "thread_id": tid, "subject": fd["subject"], "det": det,
                "verdict": labels.get(tid), "action": action,
                "history": fd["history"], "body": fd["body"],
            })
        _log_classify(cfg, date_iso or date.today().isoformat(),
                      backend, out, log_items)

    order = {k: i for i, (k, _) in enumerate(CATEGORIES)}
    result.sort(key=lambda it: (order.get(it["category"], 99),
                                0 if it.get("personal") else 1, -it["days"]))
    return result


def run_ai_layer(
    store: Store,
    cfg: Config,
    det: dict,
    backend: str | None = None,
    persist_date: str | None = None,
    progress=None,
) -> tuple[str | None, str | None]:
    """AI 계층(요약 갱신→수확→디제스트→개입 분류→정리)을 graceful 하게 실행.

    반환 (ai_text, error_note) — ai_text 는 수확 전환(Phase 1) 이후 항상 None
    (수확 결과는 det["harvest"] 로 전달, render 가 데일리 md 에 씀).
    det 는 제자리 갱신된다. 백엔드 미설정·호출 실패여도 예외를 밖으로 내지
    않는다 — 결정론 리뷰는 항상 살아남는다(#10). progress(msg)는 단계 표시용.
    """
    # 작업별 백엔드 라우팅: 요약/수확/디제스트 = sonnet(품질), 개입 분류/정제 = haiku(비용).
    #  - --backend 를 명시하면 요약 계열은 그것을 우선 사용.
    #  - sonnet/haiku 는 config 에 [ai.backends.*] 가 없어도 내장 기본값으로 해결(ai_cmd)
    #    → PC config 무수정으로 이 라우팅이 동작. 진짜 미해결이면 graceful(결정론만).
    summary_backend = backend or cfg.ai_summary_backend
    classify_backend = cfg.ai_classify_backend
    ai_text = note = None
    try:
        if progress:
            progress("누적 요약 갱신 중…")
        refresh_summaries(store, cfg, det["date"], summary_backend)
    except AIError as e:
        note = "(AI 요약 실패 — 결정론 리뷰만) " + str(e).splitlines()[0][:120]
    except SystemExit as e:
        note = f"(AI 요약 백엔드 미설정 — 결정론 리뷰만: {e})"
    # 수확(결정 후보·신호 추출 → 원장 적재) — 자체 graceful (실패 시 None)
    if progress:
        progress("결정·신호 수확 중…")
    from . import distill   # 지연 임포트 — distill 이 review 를 임포트(순환 방지)
    det["harvest"] = distill.harvest(store, cfg, det, backend=summary_backend)
    # 아래 셋은 자체적으로 graceful (미설정·실패 시 결정론 결과 유지)
    if progress:
        progress("오늘 메일 핵심 요약 중…")
    det["digest"] = ai_digest(store, cfg, det["digest"], backend=summary_backend)
    # 경계 항목 분류(액션 필요?) — FP·FN 동시 축소. haiku.
    if progress:
        progress("개입 큐 AI 분류 중…")
    det["intervention"] = ai_classify_intervention(
        store, cfg, det["intervention"],
        det.get("intervention_candidates", []), backend=classify_backend,
        date_iso=persist_date or det.get("date"))
    # 우선순위·사유·제안 주석 (재분류 아님) — 역시 haiku.
    if progress:
        progress("개입 큐 우선순위 정리 중…")
    det["intervention"] = ai_refine_intervention(
        store, cfg, det["intervention"], backend=classify_backend,
        persist_date=persist_date)
    if progress:
        progress("완료")
    return ai_text, note


# ------------------------------------------------------------------ 렌더링

def render_intervention(lines: list[str], queue: list[dict]) -> None:
    """개입 큐 → 마크다운 줄 목록 (데일리 리뷰 본문·CLI act 공용)."""
    lines.append(f"## 개입 필요 ({len(queue)}건)")
    if not queue:
        lines.append("- 없음")
        lines.append("")
        return
    grouped: dict[str, list[dict]] = {}
    for it in queue:
        grouped.setdefault(it["category"], []).append(it)
    for key, label in CATEGORIES:
        group = grouped.get(key, [])
        if not group:
            continue
        star = sum(1 for it in group if it.get("personal"))
        extra = f" · ★나 지목 {star}" if star and key == "respond" else ""
        lines.append(f"- **{label}** ({len(group)}){extra}")
        for it in group:
            tag = f" {it['tag']}" if it.get("tag") else ""
            mark = "★ " if it.get("personal") else ""
            snip = f"  「{it['snippet']}」" if it.get("snippet") else ""
            lines.append(
                f"  - {mark}[#{it['thread_id']}] {it['who']}: {it['subject']}"
                f" — {day_label(it)}{tag}{snip}"
            )
            if it.get("ai_reason"):
                pr = f"[{it['ai_priority']}] " if it.get("ai_priority") else ""
                act = f" · 제안: {it['ai_action']}" if it.get("ai_action") else ""
                flag = f" · {it['ai_flag']}" if it.get("ai_flag") else ""
                lines.append(f"    ↳ {pr}{it['ai_reason']}{act}{flag}")
    lines.append("")


def _render_digest(lines: list[str], digest: dict) -> None:
    work = digest.get("work", [])
    lines.append(f"## 오늘 메일 핵심 ({len(work)})")
    excluded = []
    if digest.get("n_notice"):
        excluded.append(f"공지 {digest['n_notice']}")
    if digest.get("n_spam"):
        excluded.append(f"노이즈 {digest['n_spam']}")
    if excluded:
        lines.append(f"- (분류 제외: {' · '.join(excluded)})")
    if not work:
        lines.append("- 없음")
        lines.append("")
        return
    for it in work:
        arrow = "→ " if it["is_sent"] else ""
        core = it["ai_core"] or it["lead"] or "(내용 없음)"
        who = f" ({it['who']})" if it.get("who") else ""
        lines.append(f"- [#{it['thread_id']}] {arrow}{it['subject']}{who} — {core}")
    lines.append("")


def _render_harvest(lines: list[str], h: dict | None) -> None:
    """수확 결과(Phase 1) — 델타·결정 후보·신호. 수확 없으면(비-AI 등) 통째 생략."""
    if not h:
        return
    lines.append("## 오늘 델타")
    for d in h.get("delta") or []:
        lines.append(f"- {d}")
    if not h.get("delta"):
        lines.append("- 없음")
    lines.append("")
    dec = h.get("decisions") or []
    lines.append(f"## 장기기억 제안 ({len(dec)}건 — 웹 '기록 › 장기기억'에서 반영/유보)")
    for d in dec:
        who = f" ({d['decider']})" if d.get("decider") else ""
        why = f" — {d['rationale']}" if d.get("rationale") else ""
        lines.append(f"- [#{d['thread_id']}] {d['title']}{who}{why}")
    if not dec:
        lines.append("- 없음")
    lines.append("")
    if h.get("person"):
        lines.append("## 인물 신호")
        for s in h["person"]:
            lines.append(f"- {s['who']} — {s['signal']} (#{s['thread_id']})")
        lines.append("")
    if h.get("project"):
        lines.append("## 프로젝트 신호")
        for s in h["project"]:
            lines.append(f"- [#{s['thread_id']}] {s['signal']}")
        lines.append("")


def render(det: dict, ai_text: str | None = None) -> str:
    lines = [f"# {det['date']} 데일리 리뷰", ""]

    _render_harvest(lines, det.get("harvest"))
    _render_digest(lines, det.get("digest") or {})

    lines.append(f"## 내가 보낸 것 ({len(det['sent'])}건)")
    for m in det["sent"]:
        lines.append(f"- {m['sent_on'][11:16]} {m['subject']} → {m['to_addrs']}")
    if not det["sent"]:
        lines.append("- 없음")
    lines.append("")

    render_intervention(lines, det.get("intervention", []))

    lines.append(f"## 미답변 / 후속 필요 ({len(det['unanswered'])}건)")
    for r in det["unanswered"]:
        warn = " ⚠" if r["days_old"] >= 2 else ""
        lines.append(
            f"- [#{r['thread_id']}] {r['sender_name']}: {r['subject']}"
            f" — D+{r['days_old']}{warn}"
        )
    if not det["unanswered"]:
        lines.append("- 없음")
    lines.append("")

    if det["deadlines"]:
        lines.append("## 기한 신호 (규칙 추출)")
        for subj, s in det["deadlines"]:
            lines.append(f"- {subj}: 「{s}」")
        lines.append("")

    lines.append(f"수신 {det['received_count']}건 처리됨.")

    if ai_text:
        lines += ["", "---", "", "# AI 회고 분석", "", ai_text]

    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════ AI 검색 (Phase 2)
# 흐릿한 자연어 한 줄 → (1)DSL 번역 → (2)엔진 검색 → (3)스니펫 재순위+자기교정
# → (4)상위 5건 본문 심층읽기 확정. 목표는 '찾던 그 메일'을 상위로 올려 알아보게
# 하는 것 — 답 합성(지식검색)은 범위 밖. AI 는 DSL 만 출력하고 파서가 정화한다.
# docs/PROPOSAL-search.md 참고. Stage 1: 번역만.

def _parse_json_obj(text: str) -> dict | None:
    """모델 출력에서 첫 JSON 객체를 관대하게 추출(코드펜스·앞뒤 잡음 허용)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):                      # ```json … ``` 펜스 제거
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _ai_search_run(cfg: Config, prompt: str, backend: str, timeout: int,
                   meter: dict | None = None) -> str:
    """AI 검색 전용 실행. `claude -p` 백엔드면 --output-format json 으로 실제
    비용·토큰을 받아 meter(usd/in/out/calls)에 누적하고 result 텍스트만 돌려준다.
    비-claude 백엔드나 JSON 파싱 실패는 일반 ai_run(평문)으로 자연 폴백."""
    cmd = cfg.ai_cmd(backend)
    if any("claude" in str(c) for c in cmd):
        raw = ai_run(cmd + ["--output-format", "json"], prompt,
                     timeout=timeout, retries=1)
        data = _parse_json_obj(raw)
        if isinstance(data, dict) and "result" in data:
            if meter is not None:
                c = data.get("total_cost_usd")
                if isinstance(c, (int, float)):
                    meter["usd"] += float(c)
                u = data.get("usage") or {}
                # 실제 청구 기준 입력 토큰 = 신규 + 캐시생성 + 캐시읽기 (claude -p 는
                # 매 호출 CC 시스템 컨텍스트를 캐시로 실어 이게 비용의 대부분).
                meter["in"] += (int(u.get("input_tokens") or 0)
                                + int(u.get("cache_creation_input_tokens") or 0)
                                + int(u.get("cache_read_input_tokens") or 0))
                meter["out"] += int(u.get("output_tokens") or 0)
                meter["calls"] += 1
            return str(data.get("result") or "")
        return raw                              # JSON 아니면 평문으로 취급
    return ai_run(cmd, prompt, timeout=timeout, retries=1)


AISEARCH_TRANSLATE = """당신은 사내 업무 메일 검색기의 '질의 번역기'다. 사용자의 흐릿한 자연어 요청을 검색 DSL 로 바꾼다.

[검색 DSL 문법]
- 사람:   from:이름|주소   to:이름|주소   cc:이름|주소
- 기간:   after:YYYY[-MM[-DD]]   before:…   on:…   (after=이후 포함, before=이전 배타)
- 상태:   is:unread  is:read  is:sent  is:received  is:flagged
- 첨부:   has:attachment   file:파일명일부
- 스레드: thread:번호
- 내용:   맨 키워드(공백 구분)  ·  "정확한 구"
- 한국어 trigram 검색은 3글자 미만 단어를 잘 못 잡는다. 되도록 3글자 이상 핵심어를 쓰고
  한↔영·유의어를 함께 확장하라 (예: 마감 → 마감 일정 deadline).

[규칙]
- 오늘 날짜: {today}. '지난달'·'최근' 같은 상대표현은 이 기준으로 계산.
- 사람 언급 → from:/to:,  시점 → after:/before:,  첨부 언급 → has:attachment.
- dsl: 가장 정확할 것으로 보이는 1차 질의.
- fallback_dsl: 1차가 너무 좁아 결과가 없을 때 쓸 더 느슨한 질의(키워드 줄이거나 기간 넓힘). 없으면 "".
- 확신 없는 제약은 넣지 말 것(억지 추측 금지). 원시 SQL 금지 — 위 DSL 만.

[출력] JSON 객체 하나만. 코드펜스·다른 말 금지:
{{"dsl": "...", "fallback_dsl": "...", "expansions": ["..."], "note": "한 줄 해석 근거"}}

[사용자 요청]
{query}
"""


def ai_translate_query(cfg: Config, query: str, today: str,
                       backend: str | None = None, meter: dict | None = None) -> dict:
    """자연어 → 검색 DSL. {dsl, fallback_dsl, expansions, note} 반환.

    AI 출력이 JSON 이 아니거나 dsl 이 비면 원문을 키워드로 쓰는 안전 폴백.
    반환 dsl 은 search.parse_query 로 파싱 가능한 문자열이며, 실제 정화는
    store.search 의 파서가 한다(AI 가 낸 문자열을 SQL 로 직접 쓰지 않는다).
    """
    prompt = AISEARCH_TRANSLATE.format(today=today, query=query.strip())
    out = _ai_search_run(cfg, prompt, backend or cfg.ai_search_backend, 120, meter)
    data = _parse_json_obj(out) or {}
    dsl = (data.get("dsl") or "").strip()
    # dsl 이 비었거나 파싱해도 아무 의미(텍스트·필터)도 없으면 원문 키워드로 폴백
    parsed = search_mod.parse_query(dsl) if dsl else None
    if parsed is None or not (parsed.has_text() or parsed.has_filters()):
        dsl = query.strip()
    fallback = (data.get("fallback_dsl") or "").strip()
    exps = data.get("expansions")
    return {
        "dsl": dsl,
        "fallback_dsl": fallback,
        "expansions": [str(e) for e in exps] if isinstance(exps, list) else [],
        "note": (data.get("note") or "").strip(),
    }


def _cand(r) -> dict:
    """검색 결과 행 → 랭킹·표시용 후보 dict (본문 제외 — 스니펫만)."""
    return {
        "id": r["id"], "thread_id": r["thread_id"],
        "subject": r["subject"] or "",
        "sender": r["sender_name"] or r["sender_addr"] or "",
        "date": (r["sent_on"] or "")[:16], "is_sent": bool(r["is_sent"]),
        "snippet": (r["snippet"] or "").replace("\n", " ").strip()[:160],
        "tier": r["tier"],
    }


AISEARCH_CONFIRM = """당신은 메일 검색 심사관이다. 아래는 엔진이 1차로 추린 후보들의 **본문**이다.
사용자가 찾는 바로 그 메일이 이 중 어느 것인지 본문까지 읽고 순위를 매겨 확정한다.

[규칙]
- 여러 후보 중 실제로 찾는 메일에 부합하는 것만 남긴다(match=true). 본문을 보니
  무관하면 match=false(=목록에서 뺀다).
- 부합 정도가 높은 순으로 정렬.
- reason 은 한국어 한 줄 — 본문의 어떤 대목이 근거인지 구체적으로.
- 답을 지어내지 말 것. 본문에 근거가 없으면 match=false.

[출력] JSON 객체 하나만. 코드펜스·다른 말 금지:
{{"ranked": [{{"id": 정수, "reason": "...", "match": true}}]}}

[사용자가 찾는 것]
{query}

[후보 본문]
{bodies}
"""


def ai_confirm_top(cfg: Config, query: str, items: list,
                   backend: str | None = None, meter: dict | None = None) -> dict:
    """상위 후보의 본문까지 읽어 확정·재정렬(iv-lite). items=[{id,...,body}]."""
    if not items:
        return {"ranked": []}
    blocks = [
        f'### id={it["id"]} · {it["date"]} · {it["sender"]}\n제목: {it["subject"]}\n'
        f'본문:\n{it.get("body") or "(본문 없음)"}'
        for it in items
    ]
    prompt = AISEARCH_CONFIRM.format(query=query.strip(), bodies="\n\n".join(blocks))
    out = _ai_search_run(cfg, prompt, backend or cfg.ai_search_backend, 180, meter)
    data = _parse_json_obj(out) or {}
    ids = {it["id"] for it in items}
    ranked = data.get("ranked") if isinstance(data.get("ranked"), list) else []
    clean, seen = [], set()
    for r in ranked:
        if not isinstance(r, dict):
            continue
        try:
            rid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if rid not in ids or rid in seen:
            continue
        seen.add(rid)
        clean.append({"id": rid, "reason": str(r.get("reason") or "").strip(),
                      "match": bool(r.get("match", True))})
    return {"ranked": clean}


def _normalize_q(q: str) -> str:
    """캐시 키 — 소문자·공백 정리."""
    return " ".join((q or "").lower().split())


# 본문까지 읽어 심사할 엔진 상위 후보 수. 재순위(스니펫)와 확정(본문)을 한 콜로
# 합치면서(방법 1) 심층읽기 대상을 5→12로 넓혔다 — 정독 범위가 늘어 품질은 강화,
# AI 호출은 3→2회로 감소. reason 은 이 최종 심사에서만 생성한다(방법 4).
AISEARCH_JUDGE_POOL = 12


def _judge_bodies(store: Store, cfg: Config, query: str, rows: list,
                  bk: str, meter: dict) -> tuple:
    """엔진 상위 후보의 본문을 한 번에 읽어 순위+확정. (ordered[], pool_size) 반환.

    ordered = 부합 판정된 후보만, 심사 순서대로. 각 원소 = _cand + reason.
    """
    pool = [_cand(r) for r in rows[:AISEARCH_JUDGE_POOL]]
    if not pool:
        return [], 0
    bodies = {m["id"]: m for m in store.messages_by_ids([c["id"] for c in pool])}
    judge_in = []
    for c in pool:
        m = bodies.get(c["id"])
        body = strip_preserved(m["new_content"] or "")[:1100] if m else ""
        judge_in.append({**c, "body": body})
    conf = ai_confirm_top(cfg, query, judge_in, bk, meter)
    by_id = {c["id"]: c for c in pool}
    ordered = [dict(by_id[r["id"]], reason=r["reason"])
               for r in conf["ranked"] if r["id"] in by_id and r["match"]]
    return ordered, len(pool)


def ai_search(store: Store, cfg: Config, query: str, today: str,
              backend: str | None = None, top: int = 8,
              use_cache: bool = True, progress=None) -> dict:
    """AI 검색 오케스트레이터 — 번역→검색→본문심사(+자기교정). AI 호출 보통 2회.

    번역으로 DSL 을 얻고 엔진으로 후보를 좁힌 뒤, 상위 후보를 **본문까지** 한 콜에
    읽어 순위·확정·이유를 한 번에 낸다(재순위+확정 통합, 방법 1·4).

    progress(stage, payload) — 선택 콜백. stage: 'translate' | 'search' | 'prelim'
    (payload=엔진 잠정 결과) | 'judge' | 'done'(payload=최종 결과). 백그라운드
    실행 시 단계 스트리밍·점진 결과(방법 7·8)에 쓴다. 예외는 삼켜 파이프라인 보호.

    반환(렌더·캐시용): {query, dsl, note, expansions, items[], others[],
    candidate_count, backend, cost, from_cache}. items 각 원소는 _cand + reason.
    """
    def _emit(stage, payload=None):
        if progress:
            try:
                progress(stage, payload)
            except Exception:
                pass

    norm = _normalize_q(query)
    if use_cache:
        cached = store.ai_search_get(norm)
        if cached and cached["result_json"]:
            try:
                res = json.loads(cached["result_json"])
                res["from_cache"] = True
                _emit("done", res)
                return res
            except ValueError:
                pass

    bk = backend or cfg.ai_search_backend
    meter = {"usd": 0.0, "in": 0, "out": 0, "calls": 0}      # 실제 비용·토큰 누적
    t0 = time.time()
    _emit("translate")
    tr = ai_translate_query(cfg, query, today, bk, meter)
    dsl = tr["dsl"]
    _emit("search")
    rows = store.search(dsl, limit=30)
    if len(rows) < 3 and tr["fallback_dsl"]:                 # 엔진만 완화(AI 추가호출 없음)
        seen = {r["id"] for r in rows}
        rows += [r for r in store.search(tr["fallback_dsl"], limit=30)
                 if r["id"] not in seen]

    # 점진 결과(방법 8): 본문 심사 전, 엔진 스니펫 순위를 잠정 결과로 먼저 흘린다.
    prelim = [dict(_cand(r), reason="") for r in rows[:top]]
    _emit("prelim", {
        "query": query, "dsl": dsl, "note": tr.get("note", ""),
        "expansions": tr.get("expansions", []), "items": prelim, "others": [],
        "candidate_count": min(len(rows), AISEARCH_JUDGE_POOL),
        "backend": bk, "preliminary": True,
    })

    _emit("judge")
    ordered, ncand = _judge_bodies(store, cfg, query, rows, bk, meter)

    # 자기교정: 확정 결과가 하나도 없으면 재번역·재검색·재심사 1회
    if not ordered:
        hint = query + "  (직전 검색 결과가 부실했다. 다른 핵심어·유의어로 더 넓게)"
        tr2 = ai_translate_query(cfg, hint, today, bk, meter)
        if tr2["dsl"] and tr2["dsl"] != dsl:
            rows2 = store.search(tr2["dsl"], limit=30)
            if rows2:
                dsl, tr = tr2["dsl"], {**tr, "note": tr2["note"] or tr["note"]}
                _emit("judge")
                ordered, ncand = _judge_bodies(store, cfg, query, rows2, bk, meter)

    meter["seconds"] = round(time.time() - t0, 1)           # 실제 소요 시간
    result = {
        "query": query, "dsl": dsl, "note": tr.get("note", ""),
        "expansions": tr.get("expansions", []),
        "items": ordered[:top], "others": ordered[top:top + 10],
        "candidate_count": ncand, "backend": bk, "cost": meter,
        "from_cache": False,
    }
    # 캐시는 항상 갱신 — '새로 찾기'(use_cache=False)로 재실행한 결과도 저장해
    # 다음 조회부터 최신 결과가 나오게 한다(읽기만 use_cache 로 우회).
    store.ai_search_put(norm, query, dsl,
                        json.dumps(result, ensure_ascii=False), bk)
    _emit("done", result)
    return result
