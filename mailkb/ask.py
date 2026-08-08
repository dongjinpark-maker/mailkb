"""질문하기 — 저장된 메일에서 근거가 달린 답을 찾는다.

검색은 '그 메일'을 찾아주고 끝나지만 여기서는 '그래서 결론이 뭐였지'에 답한다.
계획을 한 번에 확정하지 않고 **본 뒤에 다음을 정하는 라운드 루프**를 돈다 —
사람이 검색하고, 훑고, 다시 검색하는 순서와 같다. 고정 계획은 사내 용어가
예상과 다를 때나 답이 체인을 따라가야 할 때 그냥 실패한다.

  라운드: 훑기(제목·발신·날짜·스니펫 — 토큰 거의 안 듦)
          → 판단(더 검색 / 이것들 정독 / 충분)
          → 정독(인용 제거 본문) → 반복
  상한: 라운드 MAX_ROUNDS · 정독 MAX_BODIES · 전체 콜 MAX_CALLS
        (모델이 도구를 자유롭게 호출하는 루프가 아니라 호스트가 실행·상한을 강제)

비용은 질문 난이도에 비례한다 — 쉬운 질문은 답변 검증을 포함해 3콜,
어려운 질문은 예산까지 쓴다.

답변은 세 상태다: 확인됨 / 상충함(날짜 다른 근거가 충돌) / 근거 부족.
모든 주장은 그 메일 본문에서 인용을 달아야 하고, **코드가 정독한 본문과 대조**해
통과한 것만 남는다(환각 차단). 검증 통과가 0이면 상태를 근거 부족으로 강등한다 —
강등만 하고 승격은 하지 않는다. 근거 부족일 때도 확인한 사실과 다음 확인처를 준다.
"""

from __future__ import annotations

import json
import re
from datetime import date

from . import review
from .clean import quote_context, smart_truncate
from .config import Config
from .distill import _norm_ws
from .store import Store

MAX_ROUNDS = 6            # 검색/정독 판단 라운드 상한
MAX_BODIES = 48           # 정독 누적 상한 — 선택 스레드 시간축 전개 공간 포함
MAX_CALLS = 12            # 조사+답변+검증+조건부 재작성의 전체 상한
ASK_FEATURE_VERSION = 3   # 선검색·스레드 전개·검증 후 재작성 도입
READ_PER_ROUND = 10       # 한 라운드에 모델이 고를 최대 통수
HITS_PER_QUERY = 24       # 질의당 훑을 최대 건수 — 정답 후보 재현율 우선
# 예산은 **한 콜에 실리는 입력 총량**으로 잡는다. 통당 상한으로는 총량을 못 묶기
# 때문이다 — 같은 3,000자 설정에서 3통이면 10K, 24통이면 75K 로 그냥 늘어난다
# (2026-08-03 실측). 사용자가 아는 값은 "내 백엔드 컨텍스트 창"이지 통당 자수가
# 아니므로, 손잡이를 창 크기 하나로 두고 통당 배분은 코드가 정한다.
#
# 토큰은 셀 수 없다(stdlib only · 백엔드는 CLI). 그래서 추정은 **토큰→자수 변환
# 한 곳에서만** 쓰고, 그 뒤 배분은 조립된 프롬프트의 실제 길이로 맞춘다(_fit).
# 비율이 틀려도 창의 70%나 130%를 쓸 뿐이고 넘기지는 않는다.
ASK_MAX_INPUT_TOKENS = 120_000   # 한 콜 입력 상한. 0 = 제한 없음
CHARS_PER_TOKEN = 1.0     # 보수적 — 한국어는 대략 1자당 0.7~1.5토큰이라 안전 쪽
_ROUND_SHARE = 0.25       # 라운드 콜 몫. 라운드는 '더 읽을까'만 정하면 된다
# 통당 예산 사다리 — 위에서부터 시도해 예산에 맞는 첫 값을 쓴다. 0 = 자르지 않음.
_BODY_LADDER = (0, 8000, 4000, 2000, 1200, 600, 300)
QUOTE_MIN, QUOTE_MAX = 10, 300
COUNTER_TERMS = ("변경", "취소", "최종", "보류", "재검토")
COUNTER_MAX_THREADS = 5
COUNTER_MAX_TERMS = 3
COUNTER_MAX_TIER = 3      # 반전 검색은 tier4(FTS-OR '관련 낮음')를 받지 않는다
COUNTER_OFF_THREAD = 2    # 안 본 스레드에서 새로 정독할 상한
THREAD_EXPAND_MAX = 12    # 선택 스레드별 최초·최신·상태 신호 정독 상한
ASK_MEMORY_TOP = 5        # 프롬프트에 실을 장기기억(승인 결정) 상한
# 인용 앞뒤로 원문에서 떼어 붙일 문맥(자). 인용만으로는 조건·전제·후속이 안 보여
# 사용자가 메일을 다시 열게 된다(2026-08-03 지적). 실측: 100 이면 조건까지 들어오고
# 250 은 과하다. 모델이 아니라 코드가 원문을 복사하므로 환각 위험이 0 이다.
QUOTE_CONTEXT = 120

ANALYSIS_SYSTEM = review.MAIL_EVIDENCE_SYSTEM + """
질문의 현재 답을 찾는 것이 목표다. 오래된 값보다 최신 변경·취소·최종 결정을 우선하고,
서로 다른 시점의 값은 현재 값과 변경 이력으로 분리한다. 답을 못 찾았을 때만 근거
부족으로 끝내며, 검색 후보에 있는 관련 스레드는 시간순으로 확인한다."""

_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "read", "answer"]},
        "queries": {"type": "array", "items": {"type": "string"}},
        "ids": {"type": "array", "items": {"type": "integer"}},
        "why": {"type": "string"},
    },
    "required": ["action"],
}
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["확인됨", "상충함", "근거 부족"]},
        "headline": {"type": "string"},
        "answer": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object"}},
        "conflicts": {"type": "array", "items": {"type": "object"}},
        "open": {"type": "array", "items": {"type": "object"}},
        "leads": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["state", "answer", "claims", "conflicts", "leads"],
}
_ROLES = ("결론", "근거", "배경")
_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "array", "items": {"type": "string"}},
        "answer_supported": {"type": "boolean"},
    },
    "required": ["supported", "answer_supported"],
}

_DSL = """- 사람:   from:이름|주소   to:이름|주소   cc:이름|주소
- 기간:   after:YYYY[-MM[-DD]]   before:…   on:…   (after=이후 포함, before=이전 배타)
- 상태:   is:unread  is:read  is:sent  is:received  is:flagged
- 첨부:   has:attachment   file:파일명일부
- 스레드: thread:번호
- 내용:   맨 키워드(공백 구분)  ·  "정확한 구"
- 한국어 trigram 은 3글자 미만 단어를 잘 못 잡는다. 3글자 이상 핵심어를 쓰고 한↔영을 함께."""

STEP = """당신은 저장된 업무 메일에서 질문의 답을 찾는 조사관이다. 다음 한 걸음만 정하라.

[질문] {question}
[오늘] {today}
{previous}{memory}{rules}
[검색 DSL]
{dsl}

[이미 실행한 질의]
{queries}

[찾은 메일 — 아직 본문 안 읽음]
{hits}

[정독한 본문]
{read}

[남은 예산] 라운드 {round}/{max_rounds} · 정독 {nread}/{max_bodies}

[규칙]
- 근거가 부족하면 action=search — 한 번에 2~3개 질의를 **서로 다른 각도**로 내라
  (전부 실행된다). 질문의 단어가 메일의 단어와 같다고 가정하지 마라: 동의어·사내
  용어·한↔영 표기를 바꿔 보고, 코드명은 하이픈/공백 변형(NPX-200↔NPX 200)을 함께
  시도하라. 본문 키워드가 안 잡히면 **사람 축**(from:담당자 + 짧은 키워드)이나
  **기간 축**(after:/before:)으로 틀어라. 같은 질의를 반복하지 마라.
- '변경·취소·보류·최종·재검토' 같은 **갱신 신호는 최소 한 번 따로 검색**하라.
  오래된 메일 하나로 현재 상태를 단정하는 것이 가장 흔한 오답이다.
- 유망한 후보가 보이면 action=read 로 메일 번호를 지정(최대 {read_per_round}개).
  **위 목록에 있는 번호만** 읽을 수 있다. 같은 스레드의 다른 메일이 필요하면
  먼저 `thread:번호` 로 검색해 목록에 올려라.
- 답할 수 있거나, 더 찾아도 없을 것 같으면 action=answer.

[출력] JSON 객체 하나만. 코드펜스·설명 금지:
{{"action": "search"|"read"|"answer", "queries": ["질의"], "ids": [번호], "why": "한 줄"}}
"""

ANSWER = """당신은 저장된 업무 메일만 근거로 질문에 답한다. 아래 본문 밖의 지식은 쓰지 않는다.

[질문] {question}
[오늘] {today}
[근거 시간축] {span}
{previous}{memory}{rules}
[정독한 본문]
{read}

[찾았지만 읽지 않은 목록 — leads 후보]
{hits}

[규칙]
- **headline** — 질문에 대한 답 한 줄(30자 내외). 답을 못 찾았으면 빈 문자열.
- **answer** — 경위 3~6문장. 결정에 붙은 **조건·기한·예외·수치를 반드시** 쓴다.
  이것이 빠지면 사용자가 메일을 다시 열게 된다. **최신 메일이 이전을 뒤집을 수
  있으니 날짜를 보라** — 현재 값과 변경 이력을 구분해 쓴다.
  answer 안에 메일 번호(#12)를 적지 마라 — claims 가 그 일을 한다.
- **claims** — 각 항목에 mid + quote + role.
    quote 는 그 메일 본문에서 **그대로 복사한 연속 구절**이고, 그 문장만 읽어도
    뜻이 서는 **완결 문장 1~2개**로 잡아라. 조각을 내면 사용자가 메일을 다시 연다.
    서로 다른 메일의 문장을 하나의 quote 로 합치지 마라.
    `…(중략 — N자)…`·`…(표 잘림 …)` 표시를 **가로질러 인용하지 마라** — 원문에는
    그런 연속 문자열이 없어 그 항목이 통째로 버려진다.
    text 는 라벨이 아니라 **문장**으로 쓴다("QAT 로 확정"이 아니라 "김민수 팀장이
    07-26 에 QAT 로 확정했고 고객 데이터 8/5 수령이 전제").
    role 은 결론(답 그 자체) · 근거(그 결론의 이유) · 배경(주변 사실) 중 하나.
- **open** — 메일에 **명시된** 열린 것(기한·요청·미결)만. 추론하지 마라. 없으면 [].
- **state** 는 셋 중 하나:
    확인됨    — 근거가 일관되고 뒤집는 메일이 없다
    상충함    — 날짜가 다른 근거가 서로 다른 값을 말한다 → conflicts 를 **2개 이상**
                채워라(1개면 상충이 아니다)
    근거 부족 — 답을 말하는 메일이 없다. **추측하지 마라.** answer 에 무엇이
                확인되지 않는지 쓰고, 확인된 주변 사실은 claims 에, 다음 확인처는
                leads 에 담아라.
- 본문 머리의 `이 스레드 N통 중 M통 열람` 은 **덜 봤다는 뜻**이다. 그 스레드의
  사실은 단정하지 말고 leads 로 넘겨라.
- 질문에 필요 없는 개인정보(주소 등)는 반복하지 마라.
{guide}
[출력] JSON 객체 하나만. 코드펜스·설명 금지:
{{"state": "확인됨|상충함|근거 부족",
  "headline": "한 줄 결론",
  "answer": "3~6문장",
  "claims":    [{{"text": "주장 문장", "mid": 12, "quote": "원문 인용", "role": "결론"}}],
  "conflicts": [{{"label": "먼저|나중", "value": "값", "mid": 12, "quote": "원문 인용"}}],
  "open":      [{{"text": "열린 것", "mid": 12, "quote": "원문 인용"}}],
  "leads":     [{{"tid": 34, "why": "여기를 보면 되는 이유"}}]}}
"""


VERIFY = """당신은 답변 작성자가 아니라 보수적인 근거 검증기다.
각 항목의 statement 가 quote 에서 직접 확인되는 경우만 supported 에 넣어라.

[판정 규칙]
- quote 에 같은 단어가 있다는 이유만으로 통과시키지 마라.
- 부정·조건·예정·제안·완료를 구분한다. 의미가 바뀌거나 중요한 조건이 빠지면 탈락이다.
- answer_supported 는 답변의 모든 사실 주장이 아래 인용들의 결합으로 직접 뒷받침될
  때만 true 다. 하나라도 추측·과장·근거 없는 최신성 판단이 있으면 false 다.
- 외부 지식과 상식은 쓰지 않는다.

[질문]
{question}

[검증할 답변]
{answer}

[검증할 항목]
{items}

[출력] JSON 객체 하나만. 코드펜스·설명 금지:
{{"supported": ["c0", "x0"], "answer_supported": true|false}}
"""

REPAIR = """검증을 통과한 메일 근거만 사용해 답변을 다시 쓴다.

[질문]
{question}

[상태]
{state}

[검증된 근거]
{evidence}

[규칙]
- 3~6문장, 결론부터 쓴다.
- 결정에 붙은 **조건·기한·예외·수치**는 빠뜨리지 마라 — 다만 근거의 text·quote
  안에 있는 것만 쓴다. 이것이 빠지면 사용자가 메일을 다시 열게 된다.
- 근거의 text·value·quote 밖의 사실이나 최신성 판단을 추가하지 않는다.
- 메일 번호(#12)는 적지 마라 — 근거 목록이 그 일을 한다.
- 상충함이면 날짜가 다른 값을 함께 제시하고 현재 값을 임의로 고르지 않는다.
- 근거 부족이면 확인되지 않은 결론과 확인된 주변 사실을 구분한다.

[출력] JSON 객체 하나만:
{{"answer": "..."}}
"""

_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


# ───────────────────────────────────────────────── 조사 루프

def _fmt_hits(hits: dict) -> str:
    if not hits:
        return "(없음)"
    out = []
    for h in list(hits.values())[:60]:
        snip = re.sub(r"\s+", " ", (h["snippet"] or ""))[:120]
        out.append(f"#{h['id']} [스레드 {h['thread_id']}] {h['sent_on'][:16]} "
                   f"{'나(발신)' if h['is_sent'] else h['sender']}: {h['subject']}"
                   + (f" — {snip}" if snip else ""))
    return "\n".join(out)


def _fmt_read(read: dict, limit: int = 0, totals: dict | None = None) -> str:
    """정독 본문 블록. limit=0 이면 자르지 않는다(전문).

    totals(스레드별 전체 통수)를 주면 **몇 통 중 몇 통을 봤는지** 적는다 —
    조각만 보고 있다는 사실을 모델이 알아야 "더 읽자"를 고를 수 있다. 요약을
    지어 넣는 것보다 정직하고 비용이 0 이다(2026-08-03)."""
    if not read:
        return "(아직 없음)"
    seen = _seen_counts(read)
    out = []
    for m in sorted(read.values(), key=lambda x: (x["sent_on"], x["id"])):
        who = "나(발신)" if m["is_sent"] else m["sender"]
        tid = m["thread_id"]
        part = ""
        if totals and totals.get(tid, 0) > seen.get(tid, 0):
            part = f" · 이 스레드 {totals[tid]}통 중 {seen[tid]}통 열람"
        body = smart_truncate(m["body"], limit) if limit else m["body"]
        out.append(f"[#{m['id']} · 스레드 {tid} · {m['sent_on'][:16]} · {who}{part}] "
                   f"{m['subject']}\n{body}")
    return "\n\n".join(out)


def _fit(build, budget: int) -> str:
    """예산(자)에 맞는 프롬프트 — 통당 예산을 낮춰 가며 **실제 길이를 재서** 고른다.

    토큰 추정은 budget 을 만들 때 한 번 쓰이고, 여기서는 조립 결과의 정확한
    문자 수만 본다. 그래서 비율이 다소 틀려도 창을 넘기지 않는다.
    사다리 바닥에서도 넘으면 그대로 보낸다 — 정독 통수를 줄이는 것은 조사
    품질을 깎는 일이라, 그 판단까지 여기서 하지 않는다(실패는 로그에 남는다).
    """
    out = build(_BODY_LADDER[0])
    if not budget or len(out) <= budget:
        return out
    for per in _BODY_LADDER[1:]:
        out = build(per)
        if len(out) <= budget:
            return out
    return out


def _evidence_span(read: dict) -> str:
    """근거의 시간 범위 한 줄 — "최신이 이전을 뒤집는다"를 적용할 재료.

    지금까지는 그 규칙을 지시만 하고 날짜는 24개 헤더에서 직접 찾게 했다."""
    if not read:
        return "(아직 없음)"
    days = sorted((m["sent_on"] or "")[:10] for m in read.values() if m["sent_on"])
    if not days:
        return "(날짜 없음)"
    span = days[0] if days[0] == days[-1] else f"{days[0]} ~ {days[-1]}"
    return f"{span} · {len(read)}통"


def _seen_counts(read: dict) -> dict:
    """정독한 스레드별 열람 통수 — 부분 열람 표기와 '기준' 줄이 공유한다."""
    seen: dict[int, int] = {}
    for m in read.values():
        seen[m["thread_id"]] = seen.get(m["thread_id"], 0) + 1
    return seen


def _thread_totals(store: Store, read: dict) -> dict:
    """정독한 스레드들의 전체 통수 — '조각을 보고 있다'를 알리는 재료."""
    tids = {m["thread_id"] for m in read.values()}
    if not tids:
        return {}
    marks = ",".join("?" * len(tids))
    return {r["thread_id"]: r["n"] for r in store.db.execute(
        f"SELECT thread_id, COUNT(*) n FROM messages "
        f"WHERE thread_id IN ({marks}) GROUP BY thread_id", list(tids))}


def _search(store: Store, cfg: Config, query: str, hits: dict,
            max_tier: int = 4, deny: frozenset = frozenset()) -> int:
    """질의 실행 → 훑기 목록에 추가(본문 없이 메타+스니펫). 새로 추가된 건수.

    max_tier 는 관련도 하한 — store.search 의 tier(1 연속구 · 2 FTS-AND ·
    3 LIKE-AND · 4 FTS-OR '관련 낮음') 중 그보다 느슨한 등급을 버린다. 기본 4 는
    전량 수용이라 모델이 직접 고른 질의는 지금까지와 똑같이 동작한다.
    deny 는 숨긴 스레드 집합(ask 가 1회 계산) — 프롬프트에 실리기 전에 거른다.
    """
    added = 0
    try:
        rows = store.search(query, HITS_PER_QUERY)
    except Exception:                      # 파서·FTS 오류는 그 질의만 건너뜀
        return 0
    for r in rows:
        if r["id"] in hits:
            continue
        if r["thread_id"] in deny:
            continue
        if (r["tier"] if "tier" in r.keys() else 0) > max_tier:
            continue
        if not r["is_sent"] and cfg.is_noise(r["sender_addr"] or ""):
            continue
        hits[r["id"]] = {
            "id": r["id"], "thread_id": r["thread_id"], "subject": r["subject"] or "",
            "sender": r["sender_name"] or r["sender_addr"] or "",
            "sent_on": r["sent_on"] or "", "is_sent": bool(r["is_sent"]),
            "snippet": r["snippet"] if "snippet" in r.keys() else "",
        }
        added += 1
    return added


def _seed(store: Store, cfg: Config, ids: list[int], hits: dict,
          include_noise: bool = False, deny: frozenset = frozenset()) -> int:
    """검색 없이 훑기 목록을 채운다(본문은 아직 안 읽음) — 범위가 정해진 조사용.

    include_noise: 사용자가 대상을 직접 지목한 조사(메일 분석)에서는 노이즈
    필터를 끈다 — 안 그러면 노이즈 발신자의 메일을 분석할 때 정작 그 메일이
    조사 목록에서 빠진다(실측: 훑기 0통으로 시작하는 빈 분석)."""
    n = 0
    for m in store.messages_by_ids(ids[:SEED_MAX]):
        if m["id"] in hits:
            continue
        if m["thread_id"] in deny:
            continue
        if (not include_noise and not m["is_sent"]
                and cfg.is_noise(m["sender_addr"] or "")):
            continue
        hits[m["id"]] = {
            "id": m["id"], "thread_id": m["thread_id"], "subject": m["subject"] or "",
            "sender": m["sender_name"] or m["sender_addr"] or "",
            "sent_on": m["sent_on"] or "", "is_sent": bool(m["is_sent"]),
            "snippet": re.sub(r"\s+", " ", (m["new_content"] or ""))[:120],
        }
        n += 1
    return n


def _read(store: Store, ids: list[int], hits: dict, read: dict,
          limit: int = READ_PER_ROUND, deny: frozenset = frozenset()) -> int:
    """지정 메일 정독 — 인용 제거 본문을 read 에 적재. 새로 읽은 통수.

    deny 검사는 여기가 **최종 방어선**이다 — 이어 묻기의 부모 답변 승계
    (read_ids: 숨기기 전에 캐시된 목록일 수 있다)도 이 함수를 지나므로,
    여기서 걸러야 과거 캐시 경유로 숨긴 본문이 되살아나지 않는다."""
    want = [i for i in ids if i not in read][:limit]
    if not want:
        return 0
    n = 0
    rows = sorted(store.messages_by_ids(want),
                  key=lambda m: ((m["sent_on"] or ""), m["id"]))
    for m in rows:
        if m["thread_id"] in deny:
            continue
        body = (m["new_content"] or "").strip()
        if not body:
            continue
        read[m["id"]] = {
            "id": m["id"], "thread_id": m["thread_id"], "subject": m["subject"] or "",
            "sender": m["sender_name"] or m["sender_addr"] or "",
            "sent_on": m["sent_on"] or "", "is_sent": bool(m["is_sent"]), "body": body,
        }
        hits.pop(m["id"], None)            # 읽은 것은 훑기 목록에서 빼 중복 노출 방지
        n += 1
    return n


def _thread_evidence_ids(store: Store, tids: list[int], read: dict,
                         allowed_ids: set[int] | None = None,
                         deny: frozenset = frozenset()) -> list[int]:
    """선택 스레드의 시간축을 펼칠 정독 id.

    최신 메시지만 읽으면 최초 제안과 변경 전 값을 잃고, 전부 읽으면 긴 스레드 하나가
    예산을 독점한다. 스레드별로 최초 1통·최신 6통·결정/기한/철회/완료 신호를 우선해
    상한 안에서 합친다.

    deny 필수 — 이 함수는 thread_id 로 messages 를 직접 SELECT 해 노이즈 필터도
    우회하는 유일한 경로다. 여기서 안 거르면 숨긴 스레드가 통째로 실린다.
    """
    out: list[int] = []
    for tid in tids:
        if tid in deny:
            continue
        rows = store.db.execute(
            """SELECT m.id, m.sent_on,
                      COALESCE(f.has_decision,0) has_decision,
                      COALESCE(f.has_deadline,0) has_deadline,
                      COALESCE(f.has_withdrawal,0) has_withdrawal,
                      COALESCE(f.has_completion,0) has_completion
               FROM messages m
               LEFT JOIN message_features f ON f.message_id=m.id
               WHERE m.thread_id=?
               ORDER BY m.sent_on ASC, m.id ASC""", (tid,)).fetchall()
        if allowed_ids is not None:
            rows = [r for r in rows if r["id"] in allowed_ids]
        if not rows:
            continue
        chosen = {rows[0]["id"]}
        chosen.update(r["id"] for r in rows[-6:])
        for r in reversed(rows):
            if (r["has_decision"] or r["has_deadline"]
                    or r["has_withdrawal"] or r["has_completion"]):
                chosen.add(r["id"])
            if len(chosen) >= THREAD_EXPAND_MAX:
                break
        order = {r["id"]: i for i, r in enumerate(rows)}
        keep = sorted(chosen, key=order.get)
        out.extend(mid for mid in keep if mid not in read)
    return out


def _counter_search(store: Store, cfg: Config, question: str, queries: list[str],
                    hits: dict, read: dict,
                    allowed_ids: set[int] | None = None,
                    deny: frozenset = frozenset()) -> list[str]:
    """최종 답변 전 갱신·반전 근거를 호스트가 직접 찾고 정독한다.

    모델이 곧바로 answer 를 택해도 이미 본 관련 스레드는 끝까지 펼친다. 모델 질의에
    갱신 단어가 없으면 같은 검색 축에 변경·취소·최종 신호를 붙여 별도로 확인한다.

    앵커에 단어를 덧붙이면 AND 매치가 되레 깨져 store.search 가 tier4(FTS-OR)로
    폴백한다 — 질문이 구체적일수록 무관한 메일이 쏟아지는 뒤집힌 특성이다. 그래서
    관련도 하한(COUNTER_MAX_TIER)을 걸고, 안 본 스레드의 정독은 따로 상한을 둔다.
    이 검색의 본래 이득은 '이미 본 스레드의 나중 메일'이라 그쪽은 제한하지 않는다.
    """
    counter: list[str] = []
    # 시간축 전개는 모델이 실제로 정독한 스레드가 기준이다. 아직 아무것도 읽지
    # 않은 경우에만 상위 훑기 후보를 사용해, 무관한 후보 4개가 본문 예산을
    # 잠식하지 않게 한다.
    evidence_pool = list(read.values()) or list(hits.values())
    seen_tids = {int(m.get("thread_id") or 0) for m in evidence_pool}
    seen_tids.discard(0)
    tids = []
    for m in evidence_pool:
        tid = int(m.get("thread_id") or 0)
        if tid and tid not in tids:
            tids.append(tid)
        if len(tids) >= COUNTER_MAX_THREADS:
            break

    forced = [f"thread:{tid}" for tid in tids]
    if not any(any(term in q for term in COUNTER_TERMS) for q in queries):
        anchor = (queries[-1] if queries else question).strip()
        forced.extend(f"{anchor} {term}" for term in COUNTER_TERMS[:COUNTER_MAX_TERMS])

    new_ids: list[int] = []
    for query in forced:
        if not query or query in queries or query in counter:
            continue
        before = set(hits)
        _search(store, cfg, query, hits, max_tier=COUNTER_MAX_TIER, deny=deny)
        if allowed_ids is not None:
            for mid in set(hits) - before:
                if mid not in allowed_ids:
                    hits.pop(mid, None)
        counter.append(query)
        new_ids.extend(i for i in hits if i not in before and i not in read)
        for mid in read:
            hits.pop(mid, None)

    # 이미 훑기 목록에 있던 미정독 메일도 포함해야 한다. 종전에는 새 검색으로
    # 추가된 id만 읽어, 최초 검색 5위에 있던 최종 결정 메일이 끝까지 후보로만
    # 남는 실패가 있었다.
    same = _thread_evidence_ids(store, tids, read, allowed_ids, deny=deny)
    off = []
    for i in new_ids:
        tid = int((hits.get(i) or {}).get("thread_id") or 0)
        if tid not in seen_tids:
            off.append(i)
    want = list(dict.fromkeys(same + off[:COUNTER_OFF_THREAD]))

    room = max(0, MAX_BODIES - len(read))
    if room and want:
        _read(store, want, hits, read, limit=room, deny=deny)
    return counter


def _quote_ok(read: dict, mid: int, quote: str) -> bool:
    """인용 검증 — **정독한 그 메일 본문**과 대조(메시지 단위, 스레드 단위보다 엄격)."""
    q = _norm_ws(quote)
    if not (QUOTE_MIN <= len(q)):
        return False
    m = read.get(mid)
    return bool(m) and q in _norm_ws(m["body"])


def _verify(rows, read: dict, keys=("text",), pad: int = QUOTE_CONTEXT) -> list[dict]:
    """인용이 통과한 항목만. mid·quote 없거나 대조 실패면 버린다.

    통과한 항목에는 **원문에서 떼어 온 앞뒤 문맥**을 붙인다(context) — 인용
    조각만으로는 조건·전제가 안 보여 사용자가 메일을 다시 열기 때문이다.
    read[mid]["body"] 는 절단 전 원문이라 그대로 쓸 수 있다."""
    out = []
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        try:
            mid = int(it.get("mid"))
        except (TypeError, ValueError):
            continue
        quote = str(it.get("quote") or "").strip()[:QUOTE_MAX]
        if not _quote_ok(read, mid, quote):
            continue
        item = {"mid": mid, "quote": quote, "thread_id": read[mid]["thread_id"],
                "sent_on": read[mid]["sent_on"], "sender": read[mid]["sender"],
                "subject": read[mid]["subject"]}
        if it.get("role"):                 # claims 만 갖는다 — 있으면 실어 보낸다
            item["role"] = str(it["role"]).strip()
        ctx = quote_context(read[mid]["body"], quote, pad) if pad else None
        if ctx:
            # 문맥은 **표시용**이라 줄바꿈을 접는다 — 원문 개행이 그대로 오면
            # CLI 들여쓰기가 깨지고 웹에서도 문단이 어긋난다. 인용 자체는
            # 검증을 통과한 값이므로 손대지 않는다.
            flat = lambda t: " ".join((t or "").split())
            item["context"] = {"pre": flat(ctx[0]), "post": flat(ctx[2])}
            # 인용도 같이 접는다 — 대조는 공백을 무시하므로(_quote_ok) 접어도
            # 검증 가능성이 그대로다. 종결부호를 흡수한 형태로 맞춰 둔다.
            item["quote"] = flat(ctx[1])
        ok = True
        for k in keys:
            v = str(it.get(k) or "").strip()
            if not v:
                ok = False
                break
            item[k] = v[:400]
        if ok:
            out.append(item)
    return out


def _semantic_verify(cmd: list[str], question: str, answer: str,
                     claims: list[dict], conflicts: list[dict],
                     effort_flag: str | None = None,
                     on_event=None, cancel=None) -> tuple:
    """별도 AI 판정으로 주장-인용 함의와 답변 전체의 근거 충족 여부를 확인한다.

    (claims, conflicts, answer_supported, checked). checked 는 판정이 실제로
    나왔는지다 — **검증기 고장은 '거부' 와 다르게 다룬다**. 호출 실패·응답 파손이면
    인용 대조를 이미 통과한 근거를 그대로 남기고 checked=False 로 알린다(그 근거는
    v2 이전의 보증을 그대로 만족한다). answer_supported 는 False 라 호출부가 모델
    자유 서술 대신 _safe_answer 로 갈아끼우므로 보수성은 유지된다.
    """
    items = []
    for i, claim in enumerate(claims):
        items.append({"id": f"c{i}", "statement": claim["text"],
                      "quote": claim["quote"], "sent_on": claim["sent_on"]})
    for i, conflict in enumerate(conflicts):
        items.append({"id": f"x{i}", "statement": conflict["value"],
                      "quote": conflict["quote"], "sent_on": conflict["sent_on"]})
    if not items:
        return [], [], False, False

    try:
        verdict = review._parse_json_obj(review.ai_run(
            cmd, VERIFY.format(
                question=question,
                answer=answer,
                items=json.dumps(items, ensure_ascii=False),
            ),
            timeout=240, retries=1,
            system_prompt=ANALYSIS_SYSTEM, json_schema=_VERIFY_SCHEMA,
            effort="high", effort_flag=effort_flag,
            on_event=on_event, cancel=cancel,
        ))
    except review.AIError:                 # 검증기 고장 — 조사 8콜을 통째로 버리지 않는다
        return claims, conflicts, False, False
    if not verdict:                        # 응답 파손도 '전량 거부' 로 읽지 않는다
        return claims, conflicts, False, False
    valid_ids = {item["id"] for item in items}
    supported = {
        str(i) for i in (verdict.get("supported") or [])
        if str(i) in valid_ids
    }
    kept_claims = [c for i, c in enumerate(claims) if f"c{i}" in supported]
    kept_conflicts = [c for i, c in enumerate(conflicts) if f"x{i}" in supported]
    return (kept_claims, kept_conflicts,
            verdict.get("answer_supported") is True, True)


def _safe_answer(state: str, claims: list[dict], conflicts: list[dict]) -> str:
    """검증되지 않은 자유 서술 대신 통과한 구조화 근거만으로 답을 만든다."""
    if state == "상충함" and conflicts:
        vals = "; ".join(
            f"{c['sent_on'][:10]} {c['value']}" for c in conflicts
        )
        return f"메일에 서로 다른 근거가 있습니다: {vals}."
    if claims:
        facts = " · ".join(c["text"] for c in claims)
        if state == "근거 부족":
            return f"질문의 결론은 확인하지 못했습니다. 확인된 주변 사실: {facts}."
        return f"메일에서 확인된 내용: {facts}."
    return "저장된 메일에서 질문에 답할 수 있는 근거를 확인하지 못했습니다."


def _repair_answer(cmd: list[str], question: str, state: str,
                   claims: list[dict], conflicts: list[dict],
                   effort_flag: str | None = None,
                   on_event=None, cancel=None) -> tuple[str | None, int]:
    """검증된 근거로 자연스러운 답을 재작성하고 같은 검증기를 한 번 더 통과시킨다."""
    evidence = json.dumps(
        [{"text": c["text"], "date": c["sent_on"][:10], "quote": c["quote"]}
         for c in claims]
        + [{"text": c["value"], "date": c["sent_on"][:10], "quote": c["quote"]}
           for c in conflicts],
        ensure_ascii=False,
    )
    try:
        data = review._parse_json_obj(review.ai_run(
            cmd, REPAIR.format(question=question, state=state, evidence=evidence),
            timeout=240, retries=1, system_prompt=ANALYSIS_SYSTEM,
            json_schema=_REPAIR_SCHEMA, effort="high", effort_flag=effort_flag,
            on_event=on_event, cancel=cancel,
        )) or {}
    except review.AIError:
        return None, 1
    answer = str(data.get("answer") or "").strip()[:2000]
    if not answer:
        return None, 1
    _, _, supported, checked = _semantic_verify(
        cmd, question, answer, claims, conflicts, effort_flag=effort_flag,
        on_event=on_event, cancel=cancel)
    return (answer if checked and supported else None), 2


def cache_key(store: Store, question: str, parent_id: int | None = None,
              scope: str = "") -> str:
    """캐시 키 — 분석 버전 + 질문 + 기준선(MAX rowid). 새 메일이면 자연 무효화된다.
    추가 질문은 부모까지, 인물 브리핑은 대상 주소까지 넣어 서로 안 섞이게."""
    key = f"v{ASK_FEATURE_VERSION}:{_norm_ws(question or '')[:200]}@{store.ask_basis()}"
    if scope:
        key += f"~{scope}"
    return key + (f"#{int(parent_id)}" if parent_id else "")


def cached(store: Store, question: str, parent_id: int | None = None,
           scope: str = "") -> dict | None:
    """저장된 답이 있으면 그대로 — AI 호출 없이(웹이 즉시 렌더할 때 쓴다)."""
    row = store.ask_get(cache_key(store, question, parent_id, scope))
    if not row:
        return None
    try:
        res = json.loads(row["result_json"])
    except (ValueError, TypeError):
        return None
    res["cached"] = True
    res["id"] = row["id"]
    return res


def _prev_block(parent: dict | None) -> str:
    """추가 질문용 — 이전 문답을 프롬프트에 실어 같은 조사를 반복하지 않게."""
    if not parent:
        return ""
    ev = "\n".join(f"  - {c['text']} 「{c['quote']}」 [#{c['mid']}]"
                   for c in (parent.get("claims") or [])[:8])
    return (f"\n[이전 질문] {parent.get('question', '')}\n"
            f"[이전 답변({parent.get('state', '')})] {parent.get('answer', '')}\n"
            + (f"[이전 근거]\n{ev}\n" if ev else "")
            + "이번 질문은 위 답변에 이어지는 **추가 질문**이다. 이미 확인된 것은 다시\n"
              "찾지 말고, 새로 물은 부분을 채우는 데 집중하라.\n")


def _memory_block(store: Store, question: str, deny: frozenset,
                  rows=None) -> str:
    """승인된 장기기억 중 질문과 관련된 것 — 문맥 전용 블록('' 이면 생략).

    사람이 반영을 눌러 승인한 결정 원장은 이 앱에서 가장 비싸게 만들어지는
    자산인데, 종전에는 어떤 AI 도 다시 읽지 않았다(2026-08-02 점검). 여기서
    조사 시작 전에 실어 주면 모델이 확정 사실을 알고 검색하고, 원문과 원장이
    어긋나면 상충을 짚을 수 있다.

    선정은 결정론(AI 콜 0)이다: 질문을 2자+ 토큰으로 쪼개 제목·근거·결정자와
    겹침 수로 순위, 동점은 최신 우선, 겹침 0이면 블록을 내지 않는다 — 무관한
    결정을 실으면 문맥이 아니라 소음이다. rows 를 주면(인물 브리핑 —
    person_decisions) 어휘 매칭 없이 최신순으로 쓴다: 인물 범위가 이미
    관련성 필터다. 어느 쪽이든 confirmed 만, 숨긴 스레드 것은 제외.

    '인용 금지' 라벨은 보조 장치다 — 강제는 코드가 한다(_quote_ok 는 정독
    본문만 통과시키므로 원장 문장을 인용해도 검증에서 떨어진다).
    """
    if rows is None:
        rows = store.decisions(status="confirmed")
        toks = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", (question or "").lower())
                if len(t) >= 2}
        scored = []
        for r in rows:
            if r["thread_id"] in deny:
                continue
            hay = f"{r['title']} {r['rationale']} {r['decider']}".lower()
            n = sum(1 for t in toks if t in hay)
            if n:
                scored.append((n, r["decided_on"] or "", r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        picked = [r for _, _, r in scored[:ASK_MEMORY_TOP]]
    else:
        picked = [r for r in rows
                  if r["status"] == "confirmed" and r["thread_id"] not in deny
                  ][:ASK_MEMORY_TOP]
    if not picked:
        return ""
    lines = "\n".join(
        f"- {r['decided_on'] or '?'} {r['title']}"
        + (f" (결정자 {r['decider']})" if r["decider"] else "")
        + f" [#{r['thread_id']}]"
        for r in picked)
    return ("\n[장기기억 — 사람이 승인한 결정. 문맥 전용, 인용 금지 — 모든 인용"
            " 근거는 정독 본문에서만]\n" + lines + "\n")


def ask(store: Store, cfg: Config, question: str, backend: str | None = None,
        use_cache: bool = True, progress=None, today: str | None = None,
        parent_id: int | None = None, seed_ids: list | None = None,
        scope_key: str = "", guide: str = "", lock_scope: bool = True,
        seed_noise: bool = False, allow_tids: set | None = None,
        memory_rows=None, on_event=None, cancel=None) -> dict:
    """질문 → {state, answer, claims, conflicts, leads, scope}. AIError 는 올린다.

    parent_id 를 주면 그 답변에 이어지는 **추가 질문**이 된다 — 이전 문답과 그때
    정독한 본문을 물려받아 시작하므로 같은 검색을 반복하지 않는다.
    seed_ids 는 조사 범위를 결정론으로 미리 채운다(인물 브리핑 — 검색 DSL 에 OR 가
    없어 from:/to: 조합을 모델에 맡기는 것보다 확실하다). lock_scope=False 면
    seed 는 출발점일 뿐 반전 검색이 범위 밖으로 확장할 수 있다(메일 분석 —
    그 메일에서 시작해 관련 스레드로 넓혀야 한다). guide 는 답변 형식 지시.
    호출부(CLI·웹)가 AIError 를 잡아 일반 검색으로 폴백한다(#10).
    on_event/cancel 은 ai_run 스트리밍 계약 그대로(진행 이벤트·취소).
    AICancelled 는 잡지 않고 올린다 — 취소는 폴백 대상이 아니다.

    숨긴 스레드는 조사 재료에서 제외된다(검색·seed·정독·스레드 전개 전부).
    allow_tids 는 그 예외 — 사용자가 **직접 지목한** 스레드(메일 분석)는 숨김을
    무시하고 조사한다. 명시 의도가 '조용히'보다 우선하기 때문이다.
    """
    q = (question or "").strip()
    if not q:
        raise review.AIError("질문이 비어 있습니다")
    day = today or date.today().isoformat()
    name = backend or cfg.ai_ask_backend   # CLI --backend 는 이번 실행만 덮어쓴다
    cmd = cfg.ai_cmd(name)                 # 미설정이면 SystemExit — 호출부가 처리
    eflag = cfg.ai_effort_flag(name)       # 선언된 백엔드만 --effort 류를 받는다
    # 한 콜 입력 상한(자) — 사용자는 토큰으로 말하고 코드가 자수로 바꾼다.
    max_tok = max(0, int(cfg.opt("ai", "ask_max_input_tokens",
                                 default=ASK_MAX_INPUT_TOKENS)))
    cpt = float(cfg.opt("ai", "chars_per_token", default=CHARS_PER_TOKEN)) or 1.0
    budget = int(max_tok * cpt)
    step_budget = int(budget * _ROUND_SHARE)

    # 실모델 포착 — 스트리밍 init 이벤트가 알려주는 실제 모델 ID 를 scope 에
    # 남긴다. 백엔드 '이름'(opus 등)은 움직이는 별칭이라 몇 달 뒤 같은 이름이
    # 다른 모델을 가리킬 수 있다 — 기록은 실값으로.
    seen_model = {"v": ""}
    if on_event is not None:
        _outer_event = on_event

        def on_event(info):                # noqa: F811 — 의도된 래핑
            if info.get("ev") == "model" and info.get("model"):
                seen_model["v"] = str(info["model"])
            _outer_event(info)

    parent = None
    if parent_id:
        row = store.ask_by_id(parent_id)
        if row:
            try:
                parent = json.loads(row["result_json"])
            except (ValueError, TypeError):
                parent = None
    prev = _prev_block(parent)

    key = cache_key(store, q, parent_id, scope_key)
    if use_cache:
        hit = cached(store, q, parent_id, scope_key)
        if hit:
            return hit

    # 숨긴 스레드 거름망 — 질의당 1회 계산해 모든 수집 함수에 전달한다.
    # allow_tids(직접 지목)만 예외로 뺀다.
    deny = store.hidden_thread_ids()
    if allow_tids:
        deny = deny - {int(t) for t in allow_tids}

    # 프롬프트 상단의 고정 문맥 — 장기기억(승인 결정) + 사용자 지침(ai-rules.md).
    # 둘 다 조사·답변 콜에만 싣는다. 검증(VERIFY)·재작성(REPAIR)에는 넣지 않는다:
    # 검증기의 "외부 지식과 상식은 쓰지 않는다" 계약을 문맥 주입이 흔들면 안 된다.
    memory = _memory_block(store, q, deny, rows=memory_rows)
    rules = cfg.ai_rules_text()
    rules_block = f"\n[사용자 지침 — 우선 적용]\n{rules}\n" if rules else ""

    hits: dict[int, dict] = {}
    read: dict[int, dict] = {}
    queries: list[str] = []
    calls = 0
    if parent:                             # 이전 조사 승계 — 본문 재정독·질의 중복 방지
        queries = list(parent.get("scope", {}).get("queries") or [])
        inherited = [int(i) for i in
                     (parent.get("scope", {}).get("read_ids") or [])][:MAX_BODIES]
        if inherited:
            _read(store, inherited, hits, read, limit=MAX_BODIES, deny=deny)
    if seed_ids:                           # 범위 고정(인물 브리핑) — 훑기 목록을 미리 채움
        _seed(store, cfg, [int(i) for i in seed_ids], hits,
              include_noise=seed_noise, deny=deny)
    elif q:
        # 모델의 첫 행동 전에 원 질문으로 넓게 훑는다. 표현이 충분히 구체적인 질문은
        # 첫 콜부터 실제 후보를 보고 read를 고를 수 있다. 결과 0이면 기록하지 않는다.
        seed_query = re.sub(r"[?？!！]+", " ", q).strip()
        if seed_query and _search(store, cfg, seed_query, hits, deny=deny):
            queries.append(seed_query)

    for rnd in range(1, MAX_ROUNDS + 1):
        if calls >= MAX_CALLS - 1:         # 답변 콜 1회분은 남긴다
            break
        totals = _thread_totals(store, read)
        step_prompt = _fit(lambda per: STEP.format(
            question=q, today=day, dsl=_DSL, previous=prev,
            memory=memory, rules=rules_block,
            queries="\n".join(f"- {x}" for x in queries) or "(없음)",
            hits=_fmt_hits(hits),
            read=_fmt_read(read, per, totals),
            round=rnd, max_rounds=MAX_ROUNDS,
            nread=len(read), max_bodies=MAX_BODIES,
            read_per_round=READ_PER_ROUND), step_budget)
        if progress:
            # 콜 하나가 수 분까지 갈 수 있어 콜 번호·입력 크기를 함께 싣는다 —
            # 경과초(#ask-elapsed)는 클라이언트가 세므로 여기선 정적 정보만.
            progress(f"조사 {rnd}라운드 — 검색 {len(queries)}회 · 정독 {len(read)}통"
                     f" · 콜 {calls + 1}/{MAX_CALLS}"
                     f" · 송신 {review.fmt_bytes(len(step_prompt.encode('utf-8')))}")
        step = review._parse_json_obj(review.ai_run(
            cmd, step_prompt,
            timeout=240, retries=1, system_prompt=ANALYSIS_SYSTEM,
            json_schema=_STEP_SCHEMA, effort="high", effort_flag=eflag,
            on_event=on_event, cancel=cancel)) or {}
        calls += 1
        action = str(step.get("action") or "").strip()

        if action == "search":
            fresh = [str(x).strip() for x in (step.get("queries") or [])
                     if str(x).strip() and str(x).strip() not in queries]
            if not fresh:
                break                      # 새 질의가 없으면 더 볼 게 없다
            for query in fresh[:3]:
                queries.append(query)
                _search(store, cfg, query, hits, deny=deny)
            continue
        if action == "read":
            ids = [int(x) for x in (step.get("ids") or [])
                   if str(x).isdigit() and int(x) in hits]
            if not ids or len(read) >= MAX_BODIES:
                break
            if _read(store, ids, hits, read, deny=deny) == 0:
                break
            continue
        break                              # answer 또는 알 수 없는 값 → 답변 단계로

    if progress:
        progress("변경·취소 근거 확인 중…")
    fixed_scope = ({int(i) for i in seed_ids}
                   if seed_ids and lock_scope else None)
    counter_queries = _counter_search(
        store, cfg, q, queries, hits, read, allowed_ids=fixed_scope, deny=deny)

    totals = _thread_totals(store, read)
    answer_prompt = _fit(lambda per: ANSWER.format(
        question=q, today=day, span=_evidence_span(read), previous=prev,
        memory=memory, rules=rules_block, guide=guide, hits=_fmt_hits(hits),
        read=_fmt_read(read, per, totals)), budget)
    if progress:
        progress(f"답변 작성 중 · 콜 {calls + 1}/{MAX_CALLS} · 송신 "
                 f"{review.fmt_bytes(len(answer_prompt.encode('utf-8')))}")
    res = review._parse_json_obj(review.ai_run(
        cmd, answer_prompt,
        timeout=240, retries=1, system_prompt=ANALYSIS_SYSTEM,
        json_schema=_ANSWER_SCHEMA, effort="high", effort_flag=eflag,
        on_event=on_event, cancel=cancel)) or {}
    calls += 1

    answer = str(res.get("answer") or "").strip()[:2000]
    headline = str(res.get("headline") or "").strip()[:120]
    pad = max(0, int(cfg.opt("ai", "quote_context_chars", default=QUOTE_CONTEXT)))
    claims = _verify(res.get("claims"), read, pad=pad)
    conflicts = _verify(res.get("conflicts"), read, keys=("label", "value"), pad=pad)
    # 열린 것 — claims 와 같은 인용 검증을 탄다(추론만으로 올라오지 못하게)
    open_items = _verify(res.get("open"), read, pad=pad)
    for c in claims:                       # role 관용 파싱 — 없거나 이상하면 배경
        c["role"] = c.get("role") if c.get("role") in _ROLES else "배경"
    conflicts.sort(key=lambda c: (c.get("sent_on") or "", c.get("mid") or 0))
    raw_n = len(res.get("claims") or []) + len(res.get("conflicts") or [])
    exact_n = len(claims) + len(conflicts)
    if exact_n:
        if progress:
            progress("주장과 인용 대조 중…")
        claims, conflicts, answer_supported, semantic_checked = _semantic_verify(
            cmd, q, answer, claims, conflicts, effort_flag=eflag,
            on_event=on_event, cancel=cancel,
        )
        calls += 1                         # 실패해도 백엔드는 불렀다 — 비용 표시는 정직하게
    else:
        answer_supported = semantic_checked = False
    dropped = raw_n - len(claims) - len(conflicts)

    leads = []
    known = {h["thread_id"] for h in hits.values()} | {
        m["thread_id"] for m in read.values()}
    for ld in (res.get("leads") or [])[:5]:
        try:
            tid = int(ld.get("tid"))
        except (TypeError, ValueError):
            continue
        if tid not in known:
            continue
        # 제목은 messages 에 있다(threads 테이블엔 subject 컬럼이 없음) — 첫 메일 기준
        row = store.db.execute(
            "SELECT subject FROM messages WHERE thread_id=? "
            "ORDER BY sent_on ASC, id ASC LIMIT 1", (tid,)).fetchone()
        leads.append({"thread_id": tid, "why": str(ld.get("why") or "").strip()[:120],
                      "subject": (row["subject"] if row else "") or ""})

    # 상태는 코드가 확정 — 강등만 한다(승격 없음).
    raw_state = str(res.get("state") or "").strip()
    state = raw_state
    if not claims and not conflicts:
        state = "근거 부족"
    elif state == "확인됨" and not claims:
        state = "근거 부족"
    elif state == "상충함" and len(conflicts) < 2:
        state = "확인됨" if claims else "근거 부족"
    elif state not in ("확인됨", "상충함", "근거 부족"):
        state = "근거 부족"
    needs_rewrite = (state != raw_state or (
        not answer_supported and (exact_n or state != "근거 부족")))
    if needs_rewrite:
        repaired = None
        if semantic_checked and (claims or conflicts) and calls + 2 <= MAX_CALLS:
            repaired, repair_calls = _repair_answer(
                cmd, q, state, claims, conflicts, effort_flag=eflag,
                on_event=on_event, cancel=cancel)
            calls += repair_calls
        answer = repaired or _safe_answer(state, claims, conflicts)

    # 근거 없는 결론은 내지 않는다 — headline 은 검증 통과 claim 이 있을 때만.
    # 상태가 근거 부족으로 강등됐으면 그 한 줄도 성립하지 않는다.
    if not claims or state == "근거 부족":
        headline = ""

    out = {
        "question": q, "state": state,
        "headline": headline,
        "answer": answer,
        "claims": claims, "conflicts": conflicts, "leads": leads,
        "open": open_items,
        "parent_id": int(parent_id) if parent_id else None,
        "parent_question": (parent or {}).get("question") or "",
        "scope": {"queries": queries, "counter_queries": counter_queries,
                  "counter_count": len(counter_queries) + sum(
                      1 for query in queries
                      if any(term in query for term in COUNTER_TERMS)),
                  "counter_checked": bool(counter_queries or any(
                      any(term in query for term in COUNTER_TERMS)
                      for query in queries)),
                  "hits": len(hits) + len(read),
                  "read": len(read), "calls": calls, "dropped": dropped,
                  "span": _evidence_span(read),
                  # 덜 본 스레드 — 모델이 아니라 코드만 정확히 아는 사실이라
                  # 여기서 세어 '기준' 줄에 싣는다
                  "partial": sorted(
                      f"#{t} {totals[t]}통 중 {n}통"
                      for t, n in _seen_counts(read).items()
                      if totals.get(t, 0) > n),
                  "semantic_checked": semantic_checked,
                  "backend": name,
                  "model": seen_model["v"],   # 스트리밍 아닐 땐 빈 문자열
                  # 추가 질문이 물려받을 정독 목록 — 같은 본문을 다시 안 읽는다
                  "read_ids": sorted(read.keys())},
        "cached": False,
    }
    try:
        store.ask_put(key, q, json.dumps(out, ensure_ascii=False), name)
        row = store.ask_get(key)
        if row:
            out["id"] = row["id"]          # 추가 질문·이력 링크가 가리킬 식별자
    except Exception:                      # 캐시 실패가 답변을 막지 않는다
        pass
    return out


BRIEF_MONTHS = 3
SEED_MAX = 60             # 브리핑 훑기 목록 상한(정독은 MAX_BODIES 가 제한)

_BRIEF_GUIDE = """- 이 답변은 **인물 브리핑**이다. 다음 순서로 쓰라:
  ① 지금 걸려 있는 것(내 회신·결정을 기다리는 것) ② 진행 중인 일 ③ 이 사람의 일하는 방식.
  ①이 없으면 없다고 쓰라 — 지어내지 마라."""


def person_message_ids(store: Store, cfg: Config, addr: str,
                       months: int = BRIEF_MONTHS,
                       today: str | None = None) -> list[int]:
    """그 사람과 주고받은 메일 id(최신순) — 그가 보낸 것 + 내가 그에게 보낸 것.

    검색 DSL 에 OR 가 없어 from:/to: 를 한 질의로 못 묶는다. 브리핑은 범위가
    처음부터 정해져 있으므로 검색 대신 여기서 결정론으로 모은다."""
    a = (addr or "").strip().lower()
    if not a:
        return []
    end = today or date.today().isoformat()
    since = store.db.execute(
        "SELECT date(?, ?)", (end, f"-{int(max(1, months)) * 30} days")
    ).fetchone()[0]
    like = f"%{a}%"
    rows = store.db.execute(
        """SELECT id FROM messages
           WHERE sent_on >= ? AND sent_on < date(?, '+1 day')
             AND ((is_sent=0 AND lower(sender_addr)=?)
                  OR (is_sent=1 AND (lower(to_addrs) LIKE ? OR lower(cc_addrs) LIKE ?)))
           ORDER BY sent_on DESC, id DESC LIMIT ?""",
        (since, end, a, like, like, SEED_MAX)).fetchall()
    return [r["id"] for r in rows]


def brief(store: Store, cfg: Config, addr: str, name: str = "",
          months: int = BRIEF_MONTHS, backend: str | None = None,
          use_cache: bool = True, progress=None,
          today: str | None = None, on_event=None, cancel=None) -> dict:
    """인물 브리핑 — 같은 조사 엔진에 인물 범위만 고정. 미팅 전 한 화면용."""
    who = (name or addr or "").strip()
    ids = person_message_ids(store, cfg, addr, months, today)
    if not ids:
        raise review.AIError(f"{who}와(과) 최근 {months}개월 교신 기록이 없습니다")
    q = f"{who} · 최근 {months}개월 브리핑 — 내가 알아야 할 것"
    # 장기기억은 어휘 매칭 대신 인물 범위로 고른다(도시에와 같은 함수) —
    # 브리핑 질문 문구에는 매칭할 어휘가 없다.
    res = ask(store, cfg, q, backend=backend, use_cache=use_cache,
              progress=progress, today=today, seed_ids=ids,
              scope_key=(addr or "").strip().lower(), guide=_BRIEF_GUIDE,
              memory_rows=store.person_decisions(addr, name),
              on_event=on_event, cancel=cancel)
    res["person"] = {"addr": addr, "name": name, "months": months}
    return res


def mail_question(mid: int, subject: str) -> str:
    """메일 분석의 자동 질문 — 캐시 키의 재료라 **여기 한 곳**에서만 만든다.
    (웹 제출과 엔진이 각자 만들면 한쪽만 고쳐도 캐시가 갈라져 이중 이력이 된다.)"""
    subj = (subject or "(제목 없음)").strip()[:60]
    return f"메일 #{int(mid)} ({subj}) — 이 메일의 의미와 필요한 액션"


_MAIL_GUIDE = """- 이 답변은 **메일 하나의 분석**이다. 다음 순서로 쓰라:
  ① 이 메일이 말하는 것 ② 맥락 — 무엇에 대한 답이고 어떤 경위에서 나왔나
  ③ 나에게 요구되는 것/해야 할 액션(없으면 없다고) ④ 관련해 봐야 할 스레드.
  대상 메일 밖의 사실은 맥락 설명에만 쓰고 반드시 인용을 달라."""


def analyze_mail(store: Store, cfg: Config, mid: int,
                 backend: str | None = None, use_cache: bool = True,
                 progress=None, today: str | None = None,
                 on_event=None, cancel=None) -> dict:
    """메일 1통 분석 — 같은 조사 엔진에 '그 메일 + 소속 스레드'를 심는다.

    인물 브리핑과 달리 범위를 잠그지 않는다(lock_scope=False): 대상 메일이
    가리키는 다른 스레드·발신자·코드명을 라운드 루프가 검색으로 따라간다.
    결과는 ask_cache 에 영구 저장(scope=mail:<id>) — 분석 이력에 나타나고
    이어 묻기·삭제가 그대로 된다."""
    m = store.message(str(int(mid)))
    if not m:
        raise review.AIError(f"메일 #{mid} 을 찾을 수 없습니다")
    # 대상 메일을 맨 앞에 — SEED_MAX(60통) 절단에도 대상은 반드시 남는다.
    seed = [int(mid)] + [r["id"] for r in store.db.execute(
        "SELECT id FROM messages WHERE thread_id=? AND id != ? "
        "ORDER BY sent_on, id", (m["thread_id"], int(mid)))]
    subj = (m["subject"] or "(제목 없음)").strip()[:60]
    q = mail_question(mid, m["subject"] or "")
    # 대상 스레드는 숨김이어도 조사한다(allow_tids) — 사용자가 그 메일의 [분석]을
    # 직접 눌렀다는 것이 곧 명시 의도다. 확장 검색이 잡는 **다른** 숨김 스레드는
    # 여전히 제외된다.
    res = ask(store, cfg, q, backend=backend, use_cache=use_cache,
              progress=progress, today=today, seed_ids=seed,
              scope_key=f"mail:{int(mid)}", guide=_MAIL_GUIDE,
              lock_scope=False, seed_noise=True,
              allow_tids={int(m["thread_id"])},
              on_event=on_event, cancel=cancel)
    res["mail"] = {"mid": int(mid), "thread_id": m["thread_id"],
                   "subject": subj}
    return res


def history(store: Store, limit: int = 20) -> list[dict]:
    """질문 이력(최신순) — 다시 열어볼 수 있게 id·상태·시각을 함께."""
    out = []
    for row in store.ask_recent(limit):
        try:
            res = json.loads(row["result_json"])
        except (ValueError, TypeError):
            continue
        out.append({
            "id": row["id"], "question": row["question"] or res.get("question", ""),
            "state": res.get("state", ""), "created": row["created"] or "",
            "claims": len(res.get("claims") or []),
            "parent_id": res.get("parent_id"),
        })
    return out


_BASIS_RX = re.compile(r"@(\d+)(?=[~#]|$)")


def basis_of(key: str) -> int | None:
    """캐시 키에서 기준선(그 답이 본 마지막 메일 rowid)을 뽑는다.

    키는 `v2:질문@기준선[~범위][#부모]`인데 **질문과 범위 둘 다 '@'를 품을 수 있다**
    — 인물 브리핑의 범위가 이메일 주소다. 그래서 예전처럼 split('@')[-1] 하면
    'nurisoft.co.kr'을 집어 인물 브리핑의 낡음이 늘 0으로 삼켜졌다.
    기준선만이 '@숫자' 뒤에 ~ 나 # 나 끝이 오므로 그 패턴의 마지막 매치를 쓴다."""
    hits = _BASIS_RX.findall(key or "")
    return int(hits[-1]) if hits else None


def _stale_of(store: Store, key: str) -> int:
    """그 답 이후 들어온 메일 수 — 낡았는지 사용자가 판단할 재료. 못 읽으면 0."""
    basis = basis_of(key)
    return max(0, store.ask_basis() - basis) if basis is not None else 0


def _load_all(store: Store, limit: int = 500) -> dict:
    """{id: 문답 dict} — parent_id 로 대화를 잇기 위한 원자료.

    key 도 함께 실어 둔다 — 목록이 대화별 낡음을 계산할 때 행마다 다시 조회하지
    않기 위해서다(대화 40개면 40번의 ask_by_id 가 된다)."""
    by_id: dict[int, dict] = {}
    for row in store.ask_all(limit):
        try:
            res = json.loads(row["result_json"])
        except (ValueError, TypeError):
            continue
        res["id"] = row["id"]
        res["created"] = row["created"] or ""
        res["key"] = (row["key"] if "key" in row.keys() else "") or ""
        by_id[row["id"]] = res
    return by_id


def _root_of(by_id: dict, i: int) -> int:
    """이 문답이 속한 대화의 뿌리(첫 질문) id — parent 체인을 거슬러 오른다."""
    seen = set()
    while i not in seen:
        seen.add(i)
        p = by_id.get(i, {}).get("parent_id")
        if not p or p not in by_id:
            break
        i = p
    return i


def conversations(store: Store, limit: int = 40) -> list[dict]:
    """대화 목록(최근 활동순) — 하나가 '질문→추가질문' 한 덩어리.

    ChatGPT/Claude 의 왼쪽 대화 목록에 대응. 뿌리 질문을 제목으로, 마지막
    답변 상태·주고받은 횟수·마지막 시각을 함께 준다.

    stale = 마지막 답 이후 들어온 메일 수. 목록에서 석 달 전 '확인됨'과 오늘
    '확인됨'이 똑같아 보이면 낡은 결론을 그대로 믿게 된다 — 이 도구가 막으려는
    바로 그 일이다. ask_basis() 는 한 번만 부르고 대화마다 뺄셈만 한다."""
    by_id = _load_all(store)
    groups: dict[int, list[dict]] = {}
    for i in by_id:
        groups.setdefault(_root_of(by_id, i), []).append(by_id[i])
    convs = []
    basis_now = store.ask_basis()
    for rid, members in groups.items():
        members.sort(key=lambda m: (m["created"], m["id"]))
        last = members[-1]
        seen = basis_of(last.get("key", ""))
        convs.append({
            "id": rid, "title": by_id[rid].get("question", ""),
            "stale": max(0, basis_now - seen) if seen is not None else 0,
            "turns": len(members), "last": last["created"],
            "state": last.get("state", ""),
        })
    convs.sort(key=lambda c: (c["last"], c["id"]), reverse=True)
    return convs[:limit]


def transcript(store: Store, member_id: int) -> dict | None:
    """한 대화의 전체 문답 — 뿌리부터 시간순. member_id 는 대화 내 아무 id.

    반환 {root, title, turns: [문답 dict…], latest_id, stale}. 각 문답은 load()
    와 같은 형태(그때의 답 보존)이고, 이어 묻기는 latest_id 를 parent 로 쓴다."""
    by_id = _load_all(store)
    if member_id not in by_id:
        return None
    root = _root_of(by_id, member_id)
    turns = sorted((m for i, m in by_id.items() if _root_of(by_id, i) == root),
                   key=lambda m: (m["created"], m["id"]))
    for m in turns:                          # 각 답의 낡음 정도(그 뒤 새 메일 수)
        m["stale"] = _stale_of(store, m.get("key", ""))
        m["cached"] = True
    return {"root": root, "title": by_id[root].get("question", ""),
            "turns": turns, "latest_id": turns[-1]["id"],
            "stale": turns[-1].get("stale", 0)}


def conversation_ids(store: Store, member_id: int) -> list[int]:
    """이 문답이 속한 대화의 전체 rowid — 삭제(정리)용. member_id 는 아무 턴."""
    by_id = _load_all(store)
    if member_id not in by_id:                   # 500행 한도 밖·깨진 행 — 단건이라도
        return [int(member_id)] if store.ask_by_id(member_id) else []
    root = _root_of(by_id, member_id)
    return [i for i in by_id if _root_of(by_id, i) == root]


def load(store: Store, rid: int) -> dict | None:
    """저장된 답변 그대로 열기 — 새 메일이 와도 그때의 답을 보존한다."""
    row = store.ask_by_id(rid)
    if not row:
        return None
    try:
        res = json.loads(row["result_json"])
    except (ValueError, TypeError):
        return None
    res["id"] = row["id"]
    res["cached"] = True
    res["created"] = row["created"] or ""
    res["stale"] = _stale_of(store, row["key"] or "")
    return res


# ───────────────────────────────────────────────── 렌더(CLI)

_MARK = {"확인됨": "✔", "상충함": "⚠", "근거 부족": "·"}


_ROLE_ORDER = {"결론": 0, "근거": 1, "배경": 2}


def _by_role(claims: list[dict]) -> list[dict]:
    """결론 → 근거 → 배경 순. role 이 없는 옛 답은 순서가 그대로 유지된다."""
    return sorted(claims, key=lambda c: _ROLE_ORDER.get(c.get("role"), 2))


def _quote_line(c: dict) -> str:
    """인용 + 원문 앞뒤 문맥(있으면). 문맥은 「」 밖에 둬 근거와 구분된다."""
    ctx = c.get("context") or {}
    pre, post = ctx.get("pre", ""), ctx.get("post", "")
    body = f"「{c['quote']}」"
    if pre:
        body = f"…{pre} " + body
    if post:
        # 쉼표·마침표로 이어지면 앞 공백을 넣지 않는다 (「…」 , 처럼 뜨는 것 방지)
        body += ("" if post[0] in ",.;:)]}" else " ") + f"{post}…"
    return body


def render_text(res: dict) -> str:
    """CLI 출력 — 상태·답변·근거·조사 범위."""
    s = res["scope"]
    head = f"{_MARK.get(res['state'], '')} {res['state']}"
    if res.get("cached"):
        head += "  (저장된 답변"
        head += f" · 이후 새 메일 {res['stale']}통)" if res.get("stale") else ")"
    out = [head, ""]
    if res.get("parent_question"):
        out += [f"↳ 추가 질문 · 원 질문: {res['parent_question']}", ""]
    if res.get("headline"):
        out += [res["headline"], ""]
    if res["answer"]:
        out += [res["answer"], ""]

    if res["conflicts"]:
        out.append("부딪히는 근거")
        for c in res["conflicts"]:
            out.append(f"  [{c['label']}] {c['value']} · {c['sent_on'][:10]} "
                       f"{c['sender']} #{c['mid']}")
            out.append(f"      「{c['quote']}」")
        out.append("")
    if res["claims"]:
        out.append("근거")
        for c in _by_role(res["claims"]):
            out.append(f"  · {c['text']}")
            out.append("      " + _quote_line(c))
            out.append(f"      — {c['sender']} {c['sent_on'][:16]} #{c['mid']} "
                       f"(스레드 {c['thread_id']})")
        out.append("")
    if res.get("open"):
        out.append("열린 것")
        for o in res["open"]:
            out.append(f"  · {o['text']}")
            out.append("      " + _quote_line(o) + f" — #{o['mid']}")
        out.append("")
    if res["leads"]:
        out.append("여기부터 보면 됩니다")
        for ld in res["leads"]:
            out.append(f"  · {ld['subject']} — {ld['why']} (스레드 {ld['thread_id']})")
        out.append("")

    out.append("조사 범위")
    for query in s["queries"]:
        out.append(f"  검색: {query}")
    if s.get("span"):
        out.append(f"  근거 {s['span']}")
    for part in (s.get("partial") or [])[:3]:
        out.append(f"  일부만 본 스레드 {part}")
    line = (f"  훑음 {s['hits']}건 · 정독 {s['read']}통 · AI {s['calls']}콜"
            f" · {s['backend']}")
    if s["dropped"]:
        line += f" · 인용 검증 탈락 {s['dropped']}"
    out.append(line)
    if res.get("id"):
        out += ["", f"이어서 묻기:  mailkb ask \"추가 질문\" --follow {res['id']}"]
    return "\n".join(out)
