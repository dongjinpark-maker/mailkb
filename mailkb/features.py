"""수집 시 한 번 계산하는 메시지 신호 — 문장 단위 게이팅.

정규식이 수집 쓰기 트랜잭션 안에서 돈다(store._insert) — 느려지면 sync 가 락을 쥔
채 멈춘다. 저장 비트는 액션 판정(actions.py)·개입 큐의 게이트라, 낡으면 조용한
신호 누락이 된다.

판정 원칙 (docs/PROPOSAL-actions.md):
- 문장 단위: 요청·기한은 같은 문장에 완료/과거 문맥이 있으면 무효
  ("검토 요청 건을 완료했습니다" 차단). '다시/재차/리마인드'는 재요청으로 살림.
- 순서 무관: 마지막 문장이 이기는 방식이 아니라 문장별 게이팅 후 OR —
  한국어 맺음말("잘 부탁드립니다")이 항상 마지막에 오는 구조에 견고.
- 인사·관용구("참고/양해 부탁드립니다")는 약한 요청조차 아님(PLEASANTRY).
"""

from __future__ import annotations

import re

from .clean import strip_preserved


# 규칙·파생 스키마를 바꾸면 올린다 → 기존 DB 1회 백필. clean.strip_preserved 도 포함.
FEATURE_VERSION = "2"

# 문장 분할: 줄 단위 → 문장부호 뒤 공백. 소수점(1.5)·날짜(7.10)는 뒤에 공백이
# 없어 안전. 한국어 개조식(불릿·줄바꿈 종결)은 줄 단위 분할이 흡수한다.
_SENT_SPLIT_RX = re.compile(r"(?<=[.!?？])\s+")


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        out.extend(p for p in _SENT_SPLIT_RX.split(line) if p.strip())
    return out


# "까지"는 시간어가 선행해야 기한 — "현재까지 진행중" 류 배제. 줄 전체를 감싸는
# lazy 래퍼([^\n]*?…[^\n]*)는 수만 자 본문에서 백트래킹 폭발 → 금지.
_TIME_WORD = (
    r"(?:오늘|금일|내일|명일|익일|모레|이번\s*주|금주|다음\s*주|차주|내주|주말|월말|"
    r"(?:월|화|수|목|금|토|일)요일|오전|오후|아침|저녁|자정|정오|"
    r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\d{1,2}:\d{2}|"
    r"\d{1,2}\s*[/.]\s*\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|"
    r"\d{1,2}\s*월\s*(?:말|초|중순)|\d{1,2}\s*일|EOD|COB)"
)
# 순수 '기한'만 — 구 버전의 요청 프록시(회신 부탁·주시면·확정해 주)는 요청
# 계층(STRONG/WEAK_REQUEST_RX)으로 분리했다. ⏰ 는 '기한이 걸린 열린 요청'이지
# ↩ 의 복제가 아니다.
DEADLINE_RX = re.compile(
    r"(?:" + _TIME_WORD
    + r"\s*(?:\([^\)\n]{0,8}\)\s*)?(?:초|말|중순)?\s*"
    r"(?:까지|내로|안에|중으로|전\s*까지|중(?=\s|$))"
    r"|기한|마감|데드라인|asap|가능한\s*(?:한\s*)?빨리"
    r"|빠른\s*(?:회신|답변|답장|처리|검토)"
    r"|by\s+(?:eod|cob|today|tomorrow|(?:this|next)\s+week"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day))",
    re.IGNORECASE,
)

# 요청 앵커드 — "승인 올리겠습니다" 류 서술이 안 잡히게 승인류 뒤에 부탁/요청이 와야.
DECISION_RX = re.compile(
    r"("
    r"(승인|결재|재가|컨펌|approve|confirm)\s*(을|를|해)?\s*"
    r"(부탁|요청|필요|바랍|주시|주세요|주실|해\s*주|가능|여부)"
    r"|검토\s*(부탁|요청)"
    r"|의견\s*(을|를)?\s*(주|부탁|달라|주세요|주시)"
    r"|결정\s*(을|를|이|해)?\s*(부탁|필요|해\s*주|주시)"
    r"|판단\s*(을|를|이)?\s*(부탁|필요|바랍)"
    r"|가부[^\n]{0,10}(회신|부탁|판단|결정|주)"
    r")"
)

# 강한 요청 — 행동 동사에 앵커된 명시 요청 + 청유 의문. 단독으로 REQUIRED 근거.
# '확인'은 제외(가장 과적재된 동사 — "확인했습니다"/"확인 바랍니다" 오탐원) → 약한 쪽.
_ACT_VERB = (r"(?:회신|답신|답변|답장|검토|의견|판단|결정|승인|재가|컨펌|처리|"
             r"확답|제출|송부|회람|참석\s*여부)")
STRONG_REQUEST_RX = re.compile(
    r"(?:" + _ACT_VERB + r"\s*(?:을|를|해|이|여부)?\s*"
    r"(?:부탁|요청|바랍|주시|주세요|주실|주십|해\s*주|요망)"
    r"|(?:알려|공유|전달|보내)\s*주(?:세요|시|실|십)"
    r"|의견\s*(?:을|를)?\s*(?:주|달라)"
    r"|가능\s*(?:할까요|한가요|하신지|하실지|할지|합니까|한지)"
    r"|[가-힣]\s*주(?:세요|실\s*수|시겠|시길|십사|시면)"
    r"|(?:please|pls|kindly)\s+(?:review|confirm|approve|advise|reply|respond"
    r"|check|share|send|update)"
    r"|(?:could|can|would)\s+you\b"
    r"|let\s+me\s+know"
    r")",
    re.IGNORECASE,
)

# 약한 요청 — 앵커 없는 부탁/요망. 단독으론 REQUIRED 가 못 되고 MAYBE 까지만.
WEAK_REQUEST_RX = re.compile(
    r"(?:부탁\s*(?:드립|드려|드리|합니|해\s*주)"
    r"|요청\s*(?:드립|드려|합니)"
    r"|확인\s*(?:을|를)?\s*(?:부탁|바랍|요망)"
    r"|필요합니다|필요하[여니]"
    r"|바랍니다"
    r"|는지\s*[^\n]{0,8}(?:확인|검토|부탁|회신|알려|판단)"
    r")"
)

# 인사·관용구 — 이 문장의 약한 요청은 요청이 아님("잘/참고/양해 부탁드립니다").
# 같은 문장의 강한 요청(행동 동사 앵커)은 영향 없음.
PLEASANTRY_RX = re.compile(
    r"(?:(?:잘|앞으로도|많은\s*관심|아무쪼록)\s*부탁"
    r"|(?:참고|참조|양해|협조|이해)\s*(?:부탁|바랍|해\s*주|요망|하시)"
    r")"
)

# 완료·과거 문맥 — 같은 문장의 요청·기한 신호를 무효화.
# '이상/문제 없으면 ~해 주세요'는 조건부 요청이지 완료 보고가 아님(부정 전방탐색).
COMPLETION_RX = re.compile(
    r"(?:(?:완료|처리|확인|반영|전달|송부|제출|배포|접수|회신|답변|공유|적용|조치)\s*"
    r"(?:했|하였|됐|되었|되어\s*있|드렸|드린)"
    r"|이상\s*없(?!으면|다면|으시)|문제\s*없(?!으면|다면|으시)|잘\s*받았"
    r"|(?:종료|취소|철회|마감)\s*(?:했|하였|됐|되었|됩니다|함)"
    r"|(?:has|have)\s+been\s+(?:completed|resolved|closed|done)"
    r"|\b(?:completed|resolved)\b"
    r")",
    re.IGNORECASE,
)

# 과거 요청 언급 — "요청하신/부탁드렸던/요청 건" 은 새 요청이 아니라 회고.
HISTORICAL_RX = re.compile(
    r"(?:(?:요청|부탁|문의|말씀)\s*(?:하신|하셨|드렸|드린|주신|주셨|받은|받았)"
    r"|요청\s*건"
    r"|지난\s*(?:번|주)\s*(?:요청|부탁|말씀)"
    r")"
)

# 재촉 표지 — 과거 언급이라도 "다시/재차/리마인드"면 살아있는 재요청.
REMIND_RX = re.compile(r"(?:다시|재차|한\s*번\s*더|리마인드|remind)", re.IGNORECASE)

# 명시적 철회 — 열린 요청을 해제하는 유일한 상대측 문구(완료 통보는 해제 아님).
# "기존 요청은 무시해 주세요" — '무시해 주세요'가 강한 요청('~해 주세요')으로
# 오인되지 않도록 철회 판정이 요청 판정보다 먼저 돈다(classify_message).
WITHDRAWAL_RX = re.compile(
    r"(?:(?:회신|답변|답장|대응|처리|확인|검토)\s*(?:은|는|이|가)?\s*"
    r"(?:불필요|필요\s*없|안\s*하셔도|생략)"
    r"|(?:요청|건)\s*(?:을|은|는)?\s*(?:취소|철회|취하|무시)"
    r"|무시하셔도|무시해\s*주(?:세요|시)|신경\s*(?:안\s*쓰|쓰지\s*않으)셔도"
    r"|no\s+action\s+(?:needed|required)"
    r")",
    re.IGNORECASE,
)

# 완곡 조건 — "가능하면/시간 되시면 ~해 주세요"는 강한 요청이 아니라 약한 요청.
HEDGE_RX = re.compile(
    r"(?:가능\s*하\s*(?:시\s*)?[다면]|가능하다면|시간\s*(?:이\s*)?되시면|"
    r"여유\s*(?:가\s*)?되시면|괜찮으시다면|if\s+possible)",
    re.IGNORECASE,
)

# 전원·담당자 지목 — 그룹메일에서 "각 담당자는 회신" 류의 집단 행동 요구.
GROUP_CALL_RX = re.compile(
    r"(?:각\s*담당자|담당자\s*분들|담당자\s*별|각자|각\s*팀|팀\s*별|전원|"
    r"모든\s*분|해당\s*되시는\s*분)"
)

# 제목의 요청 표지 — [검토 요청]·"…요청의 건" 류. 본문 약신호의 보조 승격 근거.
SUBJECT_REQUEST_RX = re.compile(
    r"(?:\[[^\]\n]{0,14}(?:요청|승인|결재|회신|확인)[^\]\n]{0,6}\]"
    r"|(?:요청|승인|결재)\s*(?:의?\s*건)?\s*$"
    r")"
)

# '무의미 한 줄' 메시지 — 수신인 추가(++)·FYI·단순 전달/공유. 내 발신이 이거면
# '실질 회신'이 아니라 액션을 해소하지 않는다. 보수적 판정: '++이름'은 짧을 때만,
# 관용구는 본문 전체가 그 문구일 때만. (구 review._is_trivial_msg 이관)
_TRIVIAL_PLUS_MAX = 30      # "++홍길동 수석" 급 한 줄 상한
_TRIVIAL_PHRASES = {
    "fyi", "f.y.i", "fyi입니다",
    "참고", "참고하세요", "참고 하세요", "참고바랍니다", "참고 바랍니다",
    "참고부탁드립니다", "참고 부탁드립니다", "참고요",
    "공유", "공유합니다", "공유드립니다", "공유 드립니다",
    "전달", "전달합니다", "전달드립니다", "전달 드립니다",
    "본문 참고", "하기 참고", "아래 참고", "상기 참고",
    "수신인 추가", "수신자 추가", "수신 추가", "참조 추가",
}


def is_trivial_msg(text: str) -> bool:
    """실질 내용 없는 메시지인가 — 빈 본문, '++이름'(수신인 추가), FYI/전달 한 줄."""
    s = " ".join((text or "").split())
    if not s:
        return True
    if (s.startswith("++") or s.startswith("+ ")) and len(s) <= _TRIVIAL_PLUS_MAX:
        return True
    core = s.rstrip(".!~^:;)").strip().lower()
    return core in _TRIVIAL_PHRASES


# 하위 호환 — 구 이름(review 가 별칭으로 재수출). 구 REQUEST_RX 의 후신은
# 강/약 분리라 정확한 등가는 없다; 약한 쪽이 가장 가깝다.
REQUEST_RX = WEAK_REQUEST_RX


def classify_message(content: str, subject: str = "",
                     names: tuple | list = ()) -> dict:
    """메시지 한 통의 저장 신호 — message_features 한 행 값.

    보존 인용(mid-join)을 뺀 신규 작성분만 문장 단위로 판정한다. names(내 이름·
    호칭)는 설정 의존 — 바뀌면 store._derived_version 이 백필을 트리거한다.
    """
    body = strip_preserved(content or "")
    strong = weak = question = deadline = decision = 0
    completion = withdrawal = 0
    for s in split_sentences(body):
        if WITHDRAWAL_RX.search(s):
            withdrawal = 1
            continue                      # 철회 문장 자체는 요청이 아님
        comp = COMPLETION_RX.search(s) is not None
        hist = HISTORICAL_RX.search(s) is not None
        if comp:
            completion = 1
        if (comp or hist) and not REMIND_RX.search(s):
            continue                      # 완료·과거 문맥 문장은 신호 무효
        if STRONG_REQUEST_RX.search(s):
            if HEDGE_RX.search(s):
                weak = 1              # "가능하면 검토해 주시면" — 완곡 → 약한 요청
            else:
                strong = 1
        elif WEAK_REQUEST_RX.search(s) and not PLEASANTRY_RX.search(s):
            weak = 1
        if DECISION_RX.search(s):
            decision = 1
        if DEADLINE_RX.search(s):
            deadline = 1
        if "?" in s or "？" in s:
            question = 1
    low = body.lower()
    mentioned = int(any(n and len(n) >= 2 and n.lower() in low for n in names))
    return {
        "has_deadline": deadline,
        "has_decision": decision,
        "has_request": int(strong or weak),
        "has_strong_request": strong,
        "has_weak_request": weak,
        "has_question": question,
        "has_completion": completion,
        "has_withdrawal": withdrawal,
        "mentions_me": mentioned,
        "mentions_group": int(bool(GROUP_CALL_RX.search(body))),
        "is_trivial": int(is_trivial_msg(content)),
        "subject_has_request": int(bool(SUBJECT_REQUEST_RX.search(subject or ""))),
    }


def classify_content(content: str) -> tuple[int, int, int, int]:
    """(기한, 결정, 요청, 질문) — 구 4-튜플 API (테스트·외부 호환용)."""
    f = classify_message(content)
    return (f["has_deadline"], f["has_decision"], f["has_request"],
            f["has_question"])
