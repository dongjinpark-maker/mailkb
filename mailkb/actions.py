"""스레드 액션 공통 판정기 — 홈·메일함·스레드 목록·상세가 같은 결과를 쓴다.

세 층의 최상단 (docs/PROPOSAL-actions.md):
  L1 message_features  본문 문장 게이팅 신호 (수집 시 1회, features.py)
  L2 thread_state      열린 요청 슬롯 상태기계 (수집 트랜잭션 증분, store.fold_action)
  L3 여기              열린 슬롯 × 수신 대상 × 노이즈 → REQUIRED/MAYBE/NONE

판정은 본문을 다시 읽지 않는다 — thread_state 와 신호 원본 메시지의 메타·저장
비트의 좁은 조인만. 근거 문장 표시(evidence)만 해당 메시지 1통을 읽는다.

원칙: 오탐(불필요한 알림)과 미탐(놓친 공)의 비용이 비대칭 — 미탐이 치명적이라
부정 규칙의 목적지는 NONE 이 아니라 MAYBE(접힌 확인 후보)다. 사용자가 훑어서
회수할 수 있는 실수만 허용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clean import strip_preserved
from .features import (DEADLINE_RX, DECISION_RX, STRONG_REQUEST_RX,
                       WEAK_REQUEST_RX, sentence_gate, split_sentences)

REQUIRED = "required"   # 명시적인 응답·결정·처리 필요
MAYBE = "maybe"         # 액션 가능성 있으나 모호 — 접힌 확인 후보
NONE = "none"           # 액션 불필요

# 판정 이유 코드 → 표시 문구. 모든 REQUIRED/MAYBE 는 이유를 갖는다(설명 가능성).
REASON_LABELS = {
    "hidden": "숨긴 스레드",
    "hard_noise": "확실한 노이즈 발신·제목",
    "strong_direct": "직접 수신 + 명시 요청",
    "strong_named": "이름 지목 + 명시 요청",
    "strong_participated": "내 참여 스레드 + 명시 요청",
    "question_direct": "직접 수신 + 질문",
    "subject_request": "제목의 요청 표지",
    "group_call": "담당자·전원 지목 + 명시 요청",
    "group_to": "그룹 수신 — 지목 없음",
    "cc_only": "참조(CC) 수신",
    "list_recipient": "주소에 내가 없음(배포 리스트 추정)",
    "weak_direct": "직접 수신 + 완곡한 부탁",
    "named_request": "이름 지목 + 요청 신호",
    "named_only": "이름 언급만",
    "weak_group": "그룹 수신 + 약한 신호",
    "weak_unaddressed": "주소·지목 없음 + 약한 신호",
    "completion_after": "요청 후 완료 통보 — 확인만",
    "broadcast": "대량 발송",
    "external": "외부 발신(허용 목록 외)",
}


@dataclass
class Action:
    level: str                          # required | maybe | none
    kind: str = ""                      # decide | respond
    source_id: int = 0                  # 신호 원본 메시지 id
    has_deadline: bool = False
    reasons: list = field(default_factory=list)
    named: bool = False                 # 본문이 내 이름·호칭을 지목
    participated: bool = False          # 내 발신이 있는 스레드
    # 표시용 원본 메타 (재조회 방지)
    sender_name: str = ""
    sender_addr: str = ""
    subject: str = ""
    sent_on: str = ""

    def reason_text(self) -> str:
        return " · ".join(REASON_LABELS.get(r, r) for r in self.reasons)


_QUERY = """
SELECT s.thread_id, s.action_source_id, s.action_strength, s.action_kind,
       s.action_has_deadline, s.completion_after_action, s.sent_count,
       t.hidden,
       m.sender_name, m.sender_addr, m.subject, m.to_addrs, m.cc_addrs,
       m.sent_on,
       f.has_question, f.has_request, f.mentions_me, f.mentions_group,
       f.subject_has_request
FROM thread_state s
JOIN threads t ON t.id = s.thread_id
JOIN messages m ON m.id = s.action_source_id
JOIN message_features f ON f.message_id = s.action_source_id
WHERE s.action_source_id > 0
"""


def evaluate(row, cfg, me: set) -> Action:
    """열린 액션 하나를 REQUIRED/MAYBE/NONE 으로. row 는 _QUERY 한 행.

    사다리(위가 먼저):
      1 숨김·확실한 노이즈(발신 hard/제목 강)          → NONE
      2 증거 강도 × 수신 대상 → 기본 레벨
          강한 요청: 직접 수신/이름 지목/내 참여       → REQUIRED
                     그룹 To·CC (지목 없음)            → MAYBE
                     주소에 내가 없음(리스트)          → MAYBE
          약한 신호: 질문 + 직접 수신                  → REQUIRED
                     제목 요청 표지 + 직접 수신        → REQUIRED
                     직접 수신/이름 언급               → MAYBE
                     그룹 + 담당자 지목·제목 표지      → MAYBE, 그 외 → NONE
      3 강등(모두 MAYBE 까지만 — NONE 으로 안 떨어뜨림):
          요청 이후 상대 완료 통보 / 대량 발송(지목 없음) / 외부 발신(비허용)
    수신인 수는 하드 제외가 아니라 3의 강등 요소로만 쓴다.
    """
    if row["hidden"]:
        return Action(NONE, reasons=["hidden"])
    if (cfg.is_noise_sender_hard(row["sender_addr"])
            or cfg.is_noise_subject_strong(row["subject"])):
        return Action(NONE, reasons=["hard_noise"])

    tos = [a for a in (row["to_addrs"] or "").split(";") if a]
    ccs = [a for a in (row["cc_addrs"] or "").split(";") if a]
    me_in_to = bool(set(tos) & me)
    me_in_cc = bool(set(ccs) & me)
    direct = me_in_to and len(tos) <= cfg.direct_to
    named = bool(row["mentions_me"])
    participated = row["sent_count"] >= 1
    strong = row["action_strength"] == "strong"
    reasons: list[str] = []

    if strong:
        if direct:
            level = REQUIRED
            reasons.append("strong_direct")
        elif named and (me_in_to or me_in_cc):
            level = REQUIRED
            reasons.append("strong_named")
        elif me_in_to and participated:
            level = REQUIRED
            reasons.append("strong_participated")
        elif me_in_to and row["mentions_group"]:
            # "각 담당자/전원은 회신" — 그룹이어도 전원을 명시 지목한 강한 요청.
            # 초대형 발송은 아래 broadcast 강등이 MAYBE 로 받친다.
            level = REQUIRED
            reasons.append("group_call")
        elif me_in_to:
            level = MAYBE
            reasons.append("group_to")
        elif me_in_cc:
            level = MAYBE
            reasons.append("cc_only")
        else:
            level = MAYBE
            reasons.append("list_recipient")
    else:
        if row["has_question"] and direct:
            level = REQUIRED
            reasons.append("question_direct")
        elif row["subject_has_request"] and direct:
            level = REQUIRED
            reasons.append("subject_request")
        elif named and (me_in_to or me_in_cc):
            # 이름 지목 — 요청 신호가 함께면 대규모 그룹이라도 REQUIRED(원 설계),
            # 지목'만'이면(FYI에 내 이름) 확인 후보까지.
            if row["has_request"] or row["has_question"]:
                level = REQUIRED
                reasons.append("named_request")
            else:
                level = MAYBE
                reasons.append("named_only")
        elif direct:
            level = MAYBE
            reasons.append("weak_direct")
        elif me_in_to or me_in_cc:
            # 약한 신호 + 그룹 수신 — 버리지 않고 확인 후보로(미탐 비대칭 원칙)
            level = MAYBE
            reasons.append("weak_group")
        else:
            return Action(NONE, reasons=["weak_unaddressed"])

    if row["completion_after_action"] and level == REQUIRED:
        level = MAYBE
        reasons.append("completion_after")
    elif row["completion_after_action"]:
        reasons.append("completion_after")
    if len(tos) + len(ccs) >= cfg.broadcast_to and not named:
        if level == REQUIRED:
            level = MAYBE
        reasons.append("broadcast")
    if cfg.is_noise_external(row["sender_addr"]) and not named:
        if level == REQUIRED:
            # 강한 증거는 확인 후보로 남긴다(협력사 누락 방지 — allowlist 등록 전)
            level = MAYBE
            reasons.append("external")
        else:
            # 외부 + 약신호 = 뉴스레터·광고("오늘까지 신청…") — 제외.
            # 진짜 협력사는 filters.external_allowlist 로 이 분기를 안 탄다.
            return Action(NONE, reasons=reasons + ["external"])

    return Action(
        level=level,
        kind=row["action_kind"] or "respond",
        source_id=row["action_source_id"],
        has_deadline=bool(row["action_has_deadline"]),
        reasons=reasons,
        named=named,
        participated=participated,
        sender_name=row["sender_name"] or "",
        sender_addr=row["sender_addr"] or "",
        subject=row["subject"] or "",
        sent_on=row["sent_on"] or "",
    )


def classify_threads(store, cfg) -> dict[int, Action]:
    """열린 액션이 있는 전 스레드의 판정 — {thread_id: Action}.

    NONE 도 이유와 함께 담는다(감사·상세 표시용). 열린 액션이 없는 스레드는
    키 자체가 없다(= NONE, 'no_open_action'). 판정은 요청 시점 라이브 —
    hidden 같은 휘발 필드가 캐시로 낡지 않는다."""
    me = store.my_addresses
    return {r["thread_id"]: evaluate(r, cfg, me)
            for r in store.db.execute(_QUERY)}


def evaluate_thread(store, cfg, thread_id: int) -> Action:
    """단일 스레드 판정 — 상세 화면·감사용. 열린 액션 없으면 NONE."""
    row = store.db.execute(
        _QUERY + " AND s.thread_id=?", (thread_id,)).fetchone()
    if row is None:
        return Action(NONE, reasons=["no_open_action"])
    return evaluate(row, cfg, store.my_addresses)


def evidence_from_body(body: str) -> str:
    """정제 본문에서 판정 근거 문장 1개 — 첫 매치 문장, 없으면 질문, 없으면 첫 줄.

    저장 비트와 같은 문장 게이트(features.sentence_gate)를 쓴다 — 게이트에 걸린
    "검토 요청 건을 완료했습니다" 류가 근거로 표시되는 불일치 방지.
    """
    live = [s for s in split_sentences(body) if not sentence_gate(s)[2]]
    for rx in (DECISION_RX, STRONG_REQUEST_RX, DEADLINE_RX, WEAK_REQUEST_RX):
        for s in live:
            if rx.search(s):
                return s.strip()
    for s in live:
        if "?" in s or "？" in s:
            return s.strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return lines[0] if lines else ""


def evidence_sentence(store, action: Action) -> str:
    """신호 원본 메시지에서 판정 근거 문장 1개 — 상세·큐 스니펫용.

    저장 비트는 게이트고 근거 문장은 표시 시 재실행으로 복원한다(추가 저장 없음).
    """
    if not action.source_id:
        return ""
    row = store.db.execute(
        "SELECT new_content FROM messages WHERE id=?",
        (action.source_id,)).fetchone()
    if not row:
        return ""
    return evidence_from_body(strip_preserved(row["new_content"] or ""))


def signal_sets(store, cfg) -> tuple[frozenset, frozenset, frozenset]:
    """(REQUIRED 스레드, MAYBE 스레드, ⏰ 기한 스레드) — 목록 탭·뱃지 공용.

    ⏰ = 열린 액션에 기한이 걸린 것(REQUIRED/MAYBE 모두) — 내 회신·철회로
    액션이 닫히면 함께 사라진다(과거 deadline_count 누적 아님)."""
    req, may, dl = set(), set(), set()
    for tid, a in classify_threads(store, cfg).items():
        if a.level == REQUIRED:
            req.add(tid)
        elif a.level == MAYBE:
            may.add(tid)
        if a.has_deadline and a.level != NONE:
            dl.add(tid)
    return frozenset(req), frozenset(may), frozenset(dl)
