"""주간 보고 — 내가 관여한 사안을 토픽으로 묶어 진행·이슈·향후로 서술한다.

재료는 **내가 직접 관련된 원문 메일**이다. 누적 요약·데일리 요약·이전 보고는
현재 사실·상태·중요도·후보 선별의 근거가 아니다:
  · 내 발신 메일        — '내가 한 일'의 1차 증거이자 유일한 인용처
  · 나를 지목한 수신     — 본문이 내 이름·호칭을 부른 것(mentions_me)
  · 직접 수신           — To 에 내 주소 + 수신인 수 <= direct_to (액션 판정기와 같은 정의;
                          저장된 addressed_to_me 비트는 CC 를 포함해 여기 쓰지 않는다)

중요도는 지목·답장 신호가 높다. **기계적 종결 여부는 점수에 넣지 않는다**(사용자
확정) — 마무리됐다는 사실은 상태로만 표기한다.

AI 계층(graceful, 실패해도 결정론 뼈대는 남는다):
  1 기간 내 원문을 소배치로 읽어 메시지 단위 근거 카드 생성
  2 검증된 카드로 토픽 묶기 → 최대 MAX_TOPICS 개
  3 토픽별 서술 — 기간 내 원문 + 기간 이전 **원문**으로 델타 작성
  4 누락 점검·총평·의미 검증
모든 서술은 메시지 번호와 인용을 달고, 코드의 원문 대조와 별도 의미 검증을 모두
통과한 것만 남는다. '내가 한 것'은 내 발신 메일에서만 인용을 허용한다.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from . import actions, features, promises, review
from .config import Config
from .clean import smart_truncate, strip_preserved
from .distill import _norm_ws
from .store import Store

WINDOW_WEEKS = 1          # 주간 보고 기본 창 — --weeks 로 조정
MAX_TOPICS = 5            # executive summary 형태 유지 (사용자 확정)
WEEKLY_TOP = 5            # 한 절에 본문으로 보여 줄 최대 건수 (나머지는 '외 N건')
EXEC_TOP = 5              # Executive Summary 항목 수 (사용자 확정: 일간 1 · 주간 5)
REST_TOP = 20             # '그 외' 목록 상한 — 넘으면 '외 N건'으로 밝힌다
TONE_SAMPLES = 2          # 문체 표본으로 붙일 내 보고성 메일 수
TONE_CHARS = 1200         # 표본 한 통의 상한
TONE_MIN_CHARS = 400      # 이보다 짧으면 문체가 안 드러난다
# 보고성 제목 — 문체 표본 선정은 결정론이다(AI 에게 고르게 하면 사실이 샌다)
_TONE_SUBJECT_RX = re.compile(r"보고|현황|정리|요약|주간|월간|회고")
MAX_CANDIDATES = 64       # 중요 상태 우선 후보 상한 — 나머지는 '그 외'에 남김
CARD_BATCH = 8            # 원문 근거 카드 1콜당 스레드 수
CARD_MESSAGES = 8         # 카드용 스레드당 최초·신호·최신 메시지 상한
# 카드는 **인용 근거를 뽑는 자리**라 이 상한이 가장 아팠다 — 700자면 업무 메일의
# 결론·기한이 대개 밖에 있었다. 앞뒤 분할(clean.smart_truncate)로 꼬리를 살리고
# 상한도 올린다. 콜이 배치당 1회씩 여러 번이라 무한정 올리지는 않는다(2026-08-03).
CARD_BODY_MAX = 1200
BODY_MAX = 2400           # 최종 서술용 메일 한 통 본문 상한
QUOTE_MAX = 300
MAX_AI_CALLS = ((MAX_CANDIDATES + CARD_BATCH - 1) // CARD_BATCH
                + MAX_TOPICS + 5)  # 카드+묶기+서술+누락+총평+검증+해석

WEEKLY_SYSTEM = review.MAIL_EVIDENCE_SYSTEM + """
주간 보고의 현재 사실·상태·중요도는 기간 내 원문 메일과 결정론 상태만으로 판단한다.
누적 요약·데일리 요약·이전 보고의 문장은 사실 근거가 아니며 인용하거나 그 내용만으로
항목을 추가·삭제·승격하지 않는다. 이전 보고는 표현 반복을 피하는 데만 참고한다."""


# ───────────────────────────────────────────────── 결정론 수집

def bounds(weeks: int = WINDOW_WEEKS, today: str | None = None) -> tuple[str, str]:
    """(시작일, 종료일) — 종료일 포함. weeks 는 1 이상."""
    end = today or date.today().isoformat()
    try:
        d = date.fromisoformat(end)
    except ValueError:
        d = date.today()
        end = d.isoformat()
    start = (d - timedelta(days=7 * max(1, int(weeks)) - 1)).isoformat()
    return start, end


def _direct_to_me(to_addrs: str, me: set, direct_to: int) -> bool:
    """직접 수신 — To 에 내 주소가 있고 수신인이 소수(액션 판정기와 같은 정의)."""
    tos = [a.strip().lower() for a in (to_addrs or "").split(";") if a.strip()]
    return bool(set(tos) & me) and len(tos) <= direct_to


def collect(store: Store, cfg: Config, start: str, end: str) -> list[dict]:
    """창 안에서 내가 관여한 스레드 + 관여도 점수(높은 순).

    점수 = 지목 5 · 내 발신에 온 답장 4 · 직접 수신 3 · 내 발신 2 · 결정 3 · 기한 2.
    숨김·노이즈는 제외. 종결 여부는 넣지 않는다.
    """
    me = set(store.my_addresses)
    hidden = store.hidden_thread_ids()
    rows = store.db.execute(
        """SELECT m.id, m.thread_id, m.subject, m.sender_name, m.sender_addr,
                  m.to_addrs, m.sent_on, m.is_sent,
                  COALESCE(f.mentions_me,0) mentions_me,
                  COALESCE(f.has_decision,0) has_decision,
                  COALESCE(f.has_deadline,0) has_deadline
           FROM messages m
           LEFT JOIN message_features f ON f.message_id=m.id
           WHERE m.sent_on >= ? AND m.sent_on < date(?, '+1 day')
           ORDER BY m.sent_on ASC, m.id ASC""", (start, end)).fetchall()

    th: dict[int, dict] = {}
    for r in rows:
        tid = r["thread_id"]
        if tid in hidden:
            continue
        if not r["is_sent"] and cfg.is_noise(r["sender_addr"] or ""):
            continue
        t = th.setdefault(tid, {
            "thread_id": tid, "subject": r["subject"] or "(제목 없음)",
            "sent": 0, "named": 0, "direct": 0, "replies": 0,
            "decision": 0, "deadline": 0, "people": {},
            "first": r["sent_on"], "last": r["sent_on"], "n": 0,
        })
        t["n"] += 1
        t["last"] = r["sent_on"]
        if r["is_sent"]:
            t["sent"] += 1
        else:
            if t["sent"]:                       # 내 발신 뒤에 온 수신 = 내 것에 대한 답장
                t["replies"] += 1
            if r["mentions_me"]:
                t["named"] += 1
            if _direct_to_me(r["to_addrs"] or "", me, cfg.direct_to):
                t["direct"] += 1
            who = (r["sender_name"] or r["sender_addr"] or "").strip()
            if who:
                t["people"][who] = t["people"].get(who, 0) + 1
        t["decision"] += int(r["has_decision"] or 0)
        t["deadline"] += int(r["has_deadline"] or 0)

    out = []
    for t in th.values():
        if not (t["sent"] or t["named"] or t["direct"]):
            continue                            # 내가 관여하지 않은 스레드는 제외
        t["score"] = (t["named"] * 5 + t["replies"] * 4 + t["direct"] * 3
                      + t["sent"] * 2 + min(t["decision"], 2) * 3
                      + min(t["deadline"], 2) * 2)
        out.append(t)
    out.sort(key=lambda x: (x["score"], x["last"]), reverse=True)
    return out


def _calendar(store: Store, cfg: Config, start: str, end: str,
              by_id: dict, promise_tids: set) -> list[dict]:
    """기간 내 기한 신호 → 날짜가 확정된 것만 날짜순으로.

    상대 표현("이번 주 금요일까지")은 그 메일의 발신일 기준으로 환산한다.
    환산되지 않으면 싣지 않는다 — 틀린 날짜를 캘린더에 박느니 비운다.
    """
    seen, out = set(), []
    dropped = store.report_done_keys("deadline")   # 웹에서 '처리함'으로 접은 기한
    d0 = date.fromisoformat(start)
    for i in range((date.fromisoformat(end) - d0).days + 1):
        day = (d0 + timedelta(days=i)).isoformat()
        for tid, subject, quote in review.deadline_signals(store, cfg, day):
            if tid in seen:
                continue
            if Store.report_key(tid, quote) in dropped:
                continue        # seen 에 넣기 **전**에 — 넣으면 그 스레드의 다른
            seen.add(tid)       # 기한까지 주간에서 영영 사라진다
            m = promises._WHEN_RX.search(quote)
            due = promises.resolve_when(m.group(0), date.fromisoformat(day)) if m else None
            if not due:
                continue
            t = by_id.get(tid) or {}
            # 정보성 공지(사무용품 마감 등) — 날짜는 맞지만 보고 대상은 아니다.
            # 빼지 않고 표시만 낮춘다(놓치면 곤란한 것이 섞여 있다).
            low = (t.get("state") == "마무리" and t.get("score", 0) < 10
                   and tid not in promise_tids)
            out.append({"due": due, "thread_id": tid, "subject": subject,
                        "quote": quote, "low": low})
    return sorted(out, key=lambda c: c["due"])


def _states(store: Store, cfg: Config, items: list[dict], end: str) -> None:
    """스레드별 상태를 코드가 확정 — AI 가 진척도를 지어내지 못하게. 제자리 갱신."""
    acts = actions.classify_threads(store, cfg)
    for t in items:
        a = acts.get(t["thread_id"])
        last_sent = store.db.execute(
            "SELECT is_sent, sent_on FROM messages WHERE thread_id=? "
            "ORDER BY sent_on DESC, id DESC LIMIT 1", (t["thread_id"],)).fetchone()
        mine_last = bool(last_sent and last_sent["is_sent"])
        wd = review._workdays_since(
            last_sent["sent_on"] if last_sent else "", end, cfg.holidays)
        if a and a.level == actions.REQUIRED:
            t["state"], t["state_note"] = "내 차례", "회신·결정 필요"
        elif mine_last and wd >= cfg.stall_workdays:
            t["state"], t["state_note"] = "막힘", f"내 발신 후 영업 {wd}일 무응답"
        elif mine_last:
            t["state"], t["state_note"] = "상대 대기", "내가 마지막으로 보냄"
        else:
            t["state"], t["state_note"] = "마무리", "열린 요청 없음"


def report_rounds(cfg: Config, before: str) -> list[str]:
    """`before` 이전에 저장된 보고서 종료일들 — 오래된 것부터.

    날짜가 아닌 stem 은 버린다. 금고에 섞인 다른 .md 를 차수로 세면 창이 엉뚱한
    곳으로 튄다(웹의 weekly_files 도 같은 가드를 쓴다)."""
    d = cfg.vault / "weekly"
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.md"):
        if f.stem >= before:
            continue
        try:
            date.fromisoformat(f.stem)
        except ValueError:
            continue
        out.append(f.stem)
    return sorted(out)


def _last_round(store: Store, cfg: Config, weeks: int, start: str,
                end: str) -> dict | None:
    """지난 차수에 한 내 약속이 **지금** 어떻게 됐나. 지난 차수가 없으면 None.

    창은 **저장된 보고서 두 개 사이**다 — 직전 보고의 종료일이 끝, 그 앞 보고의
    다음 날이 시작. 이 차수의 창(`start`)에서 되짚으면 안 된다: 주간 파일은
    생성한 날만 있어 간격이 불규칙해서, 07-25 에 낸 보고를 07-29 에 열면
    'weeks*7 일 전' 계산이 두 차수 전을 가리키고 그 사이 약속은 어느 창에도
    안 잡힌다(2026-08-01 적대 검토에서 실측). 앞 보고가 없으면 그때만 같은
    weeks 로 되짚는다.

    14일 컷(promises.extract)과 무관하게 그 차수 기간만 본다 — 컷을 적용하면
    2주 넘은 차수를 점검할 때 조용히 빈 절이 된다."""
    rounds = report_rounds(cfg, end)        # 이 차수 파일(stem == end)은 제외된다
    if not rounds:
        return None
    prev_end = rounds[-1]
    try:
        if len(rounds) >= 2:
            prev_start = (date.fromisoformat(rounds[-2])
                          + timedelta(days=1)).isoformat()
        else:                               # 첫 보고 앞은 알 수 없다 — 창으로 근사
            prev_start = bounds(weeks, prev_end)[0]
    except (ValueError, OverflowError):     # 상식 밖 날짜의 파일 — 점검을 건너뛴다
        return None
    if prev_start > prev_end:
        return None
    return promises.review_period(store, prev_start, prev_end, today=end)


def deterministic(store: Store, cfg: Config, weeks: int = WINDOW_WEEKS,
                  today: str | None = None, report_extras: bool = True) -> dict:
    """AI 없이 나오는 뼈대 — 창·통계·관여 스레드(상태 포함). AI 계층의 입력.

    report_extras=False 는 **보고서에만 쓰이는 계산**(내 약속·기한 캘린더·지난
    차수 점검)을 통째로 건너뛴다. 일간의 상태판(review._state_map)은 items 만
    쓰는데, 그 경로가 하루 두 번 돌면서 금고를 읽고 창의 날짜마다
    deadline_signals 를 훑고 있었다."""
    start, end = bounds(weeks, today)
    items = collect(store, cfg, start, end)
    _states(store, cfg, items, end)
    by_id = {t["thread_id"]: t for t in items}
    ptids = ({p["thread_id"] for p in promises.extract(store, today=end)}
             if report_extras else set())
    return {
        "start": start, "end": end, "weeks": weeks,
        "items": items,
        # 리포트가 '가벼운 건'을 거를 때 쓰는 재료 — 내 약속이 걸린 스레드
        "promise_tids": ptids,
        "calendar": (_calendar(store, cfg, start, end, by_id, ptids)
                     if report_extras else []),
        # 일간에서 '처리함'으로 접은 정체 스레드 — 주간 '막힘'도 같이 빠져야 한다
        "done_stalled": (store.report_done_keys("stalled") if report_extras
                         else set()),
        "last_round": (_last_round(store, cfg, weeks, start, end)
                       if report_extras else None),
        "stat": {
            "threads": len(items),
            "sent": sum(t["sent"] for t in items),
            "named": sum(t["named"] for t in items),
            "direct": sum(t["direct"] for t in items),
        },
    }


# ───────────────────────────────────────────────── AI 프롬프트

CARD = """기간 내 원문 메일에서 주간 보고 후보 사실을 추출한다.

[규칙]
- 스레드마다 이번 기간에 실제로 생긴 진행·이슈·다음 행동을 최대 4개 뽑는다.
- 기간 이전 원문은 변화 전 상태를 이해하는 참고일 뿐, fact의 근거나 인용이 아니다.
- fact마다 현재 기간 메일 하나의 mid와 그 본문에서 그대로 복사한 10자 이상 연속
  quote를 붙인다. 여러 메시지를 한 quote로 합치지 않는다.
- mine=true는 사용자가 직접 한 일이며 반드시 '나(발신)' 메시지여야 한다.
- 중요도는 high/normal/low 중 하나다. 내 차례·막힘·결정·기한은 빠뜨리지 않는다.
- 사실이 없는 스레드도 tid와 빈 facts를 반환한다.

[출력] JSON 객체 하나만:
{{"threads": [{{"tid": 12, "importance": "high",
  "facts": [{{"kind": "progress", "text": "...", "mid": 31,
              "quote": "...", "mine": false}}]}}]}}

[원문 묶음]
{source}
"""

GROUP = """검증된 원문 근거 카드에서 주간 보고의 '토픽'을 정한다.
아래 목록은 사용자가 이번 기간에 직접 관여한 스레드와 원문 대조를 통과한 사실이다.

[규칙]
- 제목이 달라도 같은 사안이면 한 토픽으로 묶어라(예: 타이밍 문제와 그 재작업).
- 보고 가치가 높은 순으로 최대 {max_topics}개만. 내 차례·막힘·결정·기한과 실제 변화,
  이어서 관여도(지목·답장·내 발신)를 우선한다.
- 토픽명은 사안을 알아볼 수 있는 한국어 명사구 20자 이내. 메일 제목 복사가 아니어도 된다.
- 어느 토픽에도 안 들어가는 스레드는 그냥 빼라(억지로 묶지 말 것).
- 입력에 없는 스레드 번호를 만들지 마라.

[출력] JSON 객체 하나만. 코드펜스·설명 금지:
{{"topics": [{{"name": "토픽명", "threads": [번호, 번호]}}]}}

[스레드와 검증된 사실]
{threads}
"""

WRITE = """당신은 주간 업무 보고의 한 토픽을 쓰는 실무자다. 아래 근거 메일만 사용한다.

[토픽] {name}

[기간] {start} ~ {end}
{rules_user}
[규칙]
- **진행 사항**: 이 기간에 무엇이 움직였나. '기간 전 상태'와 비교해 **달라진 점**을 써라.
- **이슈**: 걸린 것·미결·리스크. 없으면 빈 배열.
- **향후 방향**: 다음에 필요한 것. **메일에 근거가 있을 때만** 쓰고, 없으면 빈 배열
  (추측 금지). 기한이 있으면 날짜를 함께.
- 각 항목은 한국어 한 문장(80자 내외)으로 간결하게.
- 모든 항목에 근거가 필요하다: mid(메일 번호) + tid(스레드 번호) +
  quote(그 메일 본문에서 **그대로 복사한** 연속된 구절 10자 이상).
  여러 메일의 문장을 하나의 quote 로 합치지 마라.
- **mine=true 는 '내가 한 것'을 뜻하며, quote 는 반드시 '내 발신' 메일에서만 가져와라.**
  남이 쓴 문장으로 내 성과를 서술하지 마라.
- '직전 보고'는 문장 반복을 줄이기 위한 참고일 뿐이다. 그 안의 사실·상태·중요도를
  채택하거나, 원문에 있는 항목을 직전 보고 때문에 삭제하지 마라.

[출력] JSON 객체 하나만. 코드펜스·설명 금지:
{{"progress": [{{"text": "...", "tid": 12, "mid": 31,
                 "quote": "...", "mine": false}}],
  "issues": [{{"text": "...", "tid": 12, "mid": 31, "quote": "..."}}],
  "next": [{{"text": "...", "tid": 12, "mid": 31, "quote": "..."}}]}}

[기간 전 원문 — 변화 전 상태 참고, 현재 항목의 인용 금지]
{before}

[직전 보고 — 표현 중복 회피 전용, 사실·상태·선별 근거로 사용 금지]
{previous}

[근거 메일]
{mails}
"""

OVERVIEW = """당신은 주간 업무 보고의 머리글(executive summary)을 쓴다.
읽는 사람은 상위 management 다 — 세부 경과가 아니라 **무엇이 중요하고 무엇이
걸려 있는지**가 먼저 와야 한다.
{rules_user}
[규칙]
- 아래 [토픽 서술]과 [결정론 상태]만 근거로 쓴다. 새 사실을 만들지 마라.
- 항목은 **최대 {top}건**, 중요한 것부터. 각 항목은 한국어 한 문장(80자 내외).
- 각 항목은 '무엇이 어떻게 됐고, 그래서 지금 무엇이 필요한가'가 드러나게 쓴다.
  경과 나열이 아니라 판단이 서는 문장이어야 한다.
- '지난 차수 대비'를 쓸 근거는 [결정론 상태] 뿐이다. 그 밖의 비교는 하지 마라.
- [문체 표본]은 **어조·문장 길이·용어**만 따르는 참고다. 거기 적힌 사실·수치·
  인명·일정을 이번 요약에 가져오지 마라.
- 쓸 것이 없으면 빈 배열을 내라. 채우려고 사소한 것을 올리지 마라.
- 이어서 보고 순서를 중요도순으로 정하라(토픽명 그대로).

[좋은 예 — 형태만 참고. 아래 사실을 가져다 쓰지 마라]
- "양자화가 QAT 로 확정돼 B0 일정의 최대 변수가 닫혔습니다 — 남은 것은 재학습
  데이터 8/5 수령입니다"        ← 무엇이 어떻게 됐고 + 그래서 무엇이 남았나
- "GDS 제출 8/20 이 다가오는데 타이밍 클로저 hold 위반이 유일한 걸림돌입니다"
- "협력사 NDA 2건이 이번 분기 만료인데 제 후속이 12일째 없습니다"
[나쁜 예] "양자화 관련 논의가 있었고 일정도 이야기됐습니다" ← 경과 나열, 판단 없음

[출력] JSON 객체 하나만:
{{"summary": ["한 문장", "한 문장"], "order": ["토픽명", "토픽명"]}}

[결정론 상태 — 코드가 센 사실. '지난 차수 대비'의 유일한 근거]
{board}

[문체 표본 — 어조·문장 길이·용어 참고 전용, 사실·상태 근거 사용 금지]
{tone}

[토픽 서술]
{topics}
"""

CHECK = """당신은 주간 보고의 누락을 점검한다.

아래 '이미 다룬 토픽' 밖에서, 보고에 넣지 않으면 곤란할 사안이 있는지만 보라.
조용하지만 중요한 것(기한 통보, 단발 결정, 요청 접수)을 특히 살펴라.
아래 검증된 사실에 없는 이유를 만들지 마라. 없으면 빈 배열.

[출력] JSON 객체 하나만:
{{"missed": [{{"tid": 12}}]}}

[이미 다룬 토픽]
{topics}

[다루지 않은 스레드와 검증된 사실]
{rest}
"""

VERIFY_REPORT = """주간 보고의 각 서술이 붙어 있는 원문 인용으로 의미상 뒷받침되는지
보수적으로 검증한다. 인용에 같은 단어가 있다는 이유만으로 통과시키지 말고, 시제·주체·
확정/예정/취소 상태가 서술과 맞아야 한다. summary는 통과한 서술들만 요약하는지 본다.

[보고 항목과 원문]
{claims}

[총평]
{summary}

[결정론 상태 — 코드가 센 사실. 총평이 이걸 근거로 쓰는 것은 허용]
{board}

[출력] JSON 객체 하나만:
{{"supported": ["r0", "r2"], "summary_supported": true}}
"""

_CARD_SCHEMA = {
    "type": "object",
    "properties": {"threads": {"type": "array", "items": {"type": "object"}}},
    "required": ["threads"],
}
_GROUP_SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {"type": "object"}}},
    "required": ["topics"],
}
_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "progress": {"type": "array", "items": {"type": "object"}},
        "issues": {"type": "array", "items": {"type": "object"}},
        "next": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["progress", "issues", "next"],
}
_OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": ["array", "string"], "items": {"type": "string"}},
        "order": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "order"],
}
_CHECK_SCHEMA = {
    "type": "object",
    "properties": {"missed": {"type": "array", "items": {"type": "object"}}},
    "required": ["missed"],
}
_VERIFY_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "array", "items": {"type": "string"}},
        "summary_supported": {"type": "boolean"},
    },
    "required": ["supported", "summary_supported"],
}

# 해석 층 — 사실 층과 분리된 유일한 비인용 단계. 인용 강제는 정확하지만 사실
# 나열에 수렴하므로, 검증을 통과한 서술만 재료로 '그래서 무엇을 주목하나'를
# 별도 라벨로 쓴다. 화면·마크다운에서 참고 의견임을 명시한다(초안은 AI, 확정은 사람).
INSIGHT = """당신은 주간 보고를 읽는 사람에게 '그래서 무엇을 주목해야 하나'를 말한다.

아래 검증된 서술과 결정론 상태만 재료로 쓴다. 재료에 없는 사건·수치·이름을
만들지 마라. 개별 서술의 반복은 해석이 아니다 — 서술들을 가로질러 보이는 것만
말하라: 겹치는 원인, 커지는 리스크, 다음 주에 결판나는 것, 멈춰 있는 것.

- 2~4개. 각 항목은 한두 문장, 어떤 토픽에서 나온 판단인지 topic 에 적는다.
- 확실하지 않으면 '~로 보인다'로 쓰되, 재료로 뒷받침되지 않는 추측은 버려라.
- 쓸 만한 해석이 없으면 빈 배열 — 억지로 채우지 마라.

[출력] JSON 객체 하나만:
{{"insights": [{{"topic": "토픽명", "text": "해석 한두 문장"}}]}}

[검증된 서술]
{brief}

[짚어둘 것]
{missed}

[결정론 상태]
{stats}
"""

_INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {"insights": {"type": "array", "items": {"type": "object"}}},
    "required": ["insights"],
}


# ───────────────────────────────────────────────── AI 계층

class _MessageQuoteChecker:
    """최종 항목을 스레드가 아닌 한 메시지의 원문에 고정한다."""

    def __init__(self, store: Store, start: str, end: str):
        self.store = store
        self.start = start
        try:
            self.end_exclusive = (
                date.fromisoformat(end) + timedelta(days=1)).isoformat()
        except ValueError:
            self.end_exclusive = f"{end}T~"
        self.cache: dict[int, object] = {}

    def _row(self, mid: int):
        if mid not in self.cache:
            self.cache[mid] = self.store.message(str(mid))
        return self.cache[mid]

    def resolve(self, mid, tid: int, quote: str, mine: bool):
        q = _norm_ws(quote)
        if not (10 <= len(q) <= QUOTE_MAX):
            return None
        try:
            given = int(mid)
        except (TypeError, ValueError):
            given = 0
        candidates = [self._row(given)] if given else list(
            self.store.quote_messages(tid))
        matched = []
        for row in candidates:
            if not row or int(row["thread_id"]) != tid:
                continue
            sent_on = row["sent_on"] or ""
            if not (sent_on >= self.start and sent_on < self.end_exclusive):
                continue
            if mine and not row["is_sent"]:
                continue
            if q in _norm_ws(row["new_content"] or ""):
                matched.append(row)
        if given:
            return matched[0] if matched else None
        # mid가 빠진 구조화 응답은 인용 출처가 한 메시지로 유일할 때만 복구한다.
        return matched[0] if len(matched) == 1 else None


def _candidate_items(items: list[dict]) -> list[dict]:
    """열린 일·결정·기한을 먼저 보존한 뒤 관여도와 최신순으로 자른다."""
    def key(t):
        protected = (t["state"] in ("내 차례", "막힘")
                     or bool(t["decision"]) or bool(t["deadline"]))
        return (int(protected), t["score"], t["last"])

    return sorted(items, key=key, reverse=True)[:MAX_CANDIDATES]


def _period_rows(store: Store, tid: int, start: str, end: str,
                 limit: int | None = None) -> list:
    rows = list(store.db.execute(
        """SELECT m.id, m.thread_id, m.subject, m.sender_name, m.sender_addr,
                  m.sent_on, m.is_sent, m.new_content,
                  COALESCE(f.has_request,0) has_request,
                  COALESCE(f.has_decision,0) has_decision,
                  COALESCE(f.has_deadline,0) has_deadline,
                  COALESCE(f.has_withdrawal,0) has_withdrawal,
                  COALESCE(f.has_completion,0) has_completion
           FROM messages m
           LEFT JOIN message_features f ON f.message_id=m.id
           WHERE m.thread_id=? AND m.sent_on >= ?
             AND m.sent_on < date(?, '+1 day')
           ORDER BY m.sent_on ASC, m.id ASC""", (tid, start, end)))
    if limit is None or len(rows) <= limit:
        return rows

    chosen = {0}
    chosen.update(range(max(0, len(rows) - 4), len(rows)))
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if (r["has_request"] or r["has_decision"] or r["has_deadline"]
                or r["has_withdrawal"] or r["has_completion"]):
            chosen.add(i)
        if len(chosen) >= limit:
            break
    keep = sorted(chosen)
    if len(keep) > limit:
        keep = [0] + keep[-(limit - 1):]
    return [rows[i] for i in keep]


def _format_rows(rows: list, body_max: int) -> str:
    out = []
    for m in rows:
        body = (m["new_content"] or "").strip()
        if not body:
            continue
        who = "나(발신)" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
        out.append(
            f"[mid={m['id']} · thread=#{m['thread_id']} · {m['sent_on'][:16]} · {who}] "
            f"{m['subject']}\n{smart_truncate(body, body_max)}")
    return "\n\n".join(out)


def _topic_mails(store: Store, tids: list[int], start: str, end: str) -> str:
    """최종 서술 입력 — 기간 내 원문. 누적·데일리 요약은 읽지 않는다."""
    chunks = []
    for tid in tids:
        text = _format_rows(_period_rows(store, tid, start, end), BODY_MAX)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def _before_state(store: Store, tids: list[int], start: str) -> str:
    """변화 전 상태는 현재 누적 요약이 아니라 기간 이전 원문 끝부분으로 복원한다."""
    out = []
    for tid in tids:
        rows = list(store.db.execute(
            """SELECT id, thread_id, subject, sender_name, sender_addr,
                      sent_on, is_sent, new_content
               FROM messages WHERE thread_id=? AND sent_on < ?
               ORDER BY sent_on DESC, id DESC LIMIT 3""", (tid, start)))
        rows.reverse()
        text = _format_rows(rows, 600)
        out.append(text if text else f"[thread=#{tid}] 이 기간에 시작된 사안")
    return "\n\n".join(out) or "(이전 원문 없음)"


def _card_source(store: Store, items: list[dict], start: str, end: str) -> str:
    out = []
    for t in items:
        current = _format_rows(
            _period_rows(store, t["thread_id"], start, end, CARD_MESSAGES),
            CARD_BODY_MAX,
        ) or "(본문 없음)"
        out.append(
            f"=== thread=#{t['thread_id']} · {t['subject']} · 상태 {t['state']}"
            f"({t['state_note']}) · 결정 {t['decision']} · 기한 {t['deadline']} ===\n"
            f"[기간 이전 원문 — 참고]\n"
            f"{_before_state(store, [t['thread_id']], start)}\n"
            f"[현재 기간 원문 — fact 인용 대상]\n{current}")
    return "\n\n".join(out)


def _fallback_fact(store: Store, t: dict, start: str, end: str) -> list[dict]:
    """카드 콜 실패/빈 응답 때도 원문 한 문장으로 후보를 잃지 않는다."""
    rows = _period_rows(store, t["thread_id"], start, end, CARD_MESSAGES)
    for row in reversed(rows):
        for sentence in features.split_sentences((row["new_content"] or "").strip()):
            quote = sentence.strip()
            if len(_norm_ws(quote)) < 10:
                continue
            if row["has_deadline"]:
                kind = "next"
            elif row["has_request"] and not row["is_sent"]:
                kind = "issue"
            else:
                kind = "progress"
            return [{"kind": kind, "text": quote[:160], "mid": row["id"],
                     "tid": t["thread_id"], "quote": quote[:QUOTE_MAX],
                     "mine": bool(row["is_sent"])}]
    return []


def _thread_lines(items: list[dict], cards: dict[int, list[dict]]) -> str:
    """토픽 묶기 입력 — 결정론 상태와 원문 검증을 통과한 카드만."""
    out = []
    for t in items:
        who = ", ".join(sorted(t["people"], key=lambda k: -t["people"][k])[:3])
        head = (
            f"#{t['thread_id']} {t['subject']} · {t['first'][:10]}~{t['last'][:10]} "
            f"· 상태 {t['state']}({t['state_note']}) · 내발신 {t['sent']} "
            f"지목 {t['named']} 직접 {t['direct']} 답장 {t['replies']} "
            f"결정 {t['decision']} 기한 {t['deadline']}"
            + (f" · {who}" if who else ""))
        facts = cards.get(t["thread_id"]) or []
        lines = [
            f"    - {f['kind']}: {f['text']} "
            f"「{f['quote']}」 (mid={f['mid']})"
            for f in facts
        ]
        out.append(head + (("\n" + "\n".join(lines)) if lines else ""))
    return "\n".join(out)


def previous_report(cfg: Config, start: str) -> str:
    """직전 주간보고 발췌 — 사실 입력이 아니라 표현 중복 회피용 참고."""
    d = cfg.vault / "weekly"
    if not d.exists():
        return "(없음)"
    files = sorted(p for p in d.glob("*.md") if p.stem < start)
    if not files:
        return "(없음)"
    try:                        # 표식은 프롬프트에 잡음이다 — 떼고 넣는다
        return review.strip_done_marks(files[-1].read_text(encoding="utf-8"))[:2500]
    except OSError:
        return "(없음)"


def tone_samples(store: Store) -> str:
    """내가 쓴 보고성 메일 — **문체만** 참고한다(어조·문장 길이·용어).

    매 차수를 백지에서 쓰다 보니 톤이 들쭉날쭉했다. 선정은 결정론이다 —
    고르는 일까지 AI 에게 맡기면 '무엇이 중요한가'가 표본 쪽으로 샌다.
    사실 오염은 세 겹으로 막는다: 프롬프트 라벨 · 규칙문 · 총평 검증
    (summary_supported 는 통과한 서술만 요약하는지 본다)."""
    # 최근 400통 안에서만 찾는다 — 몇 년 전 문체를 끌어오지 않기 위해서고, 여기서
    # 못 찾으면 표본 없이(= 지금까지와 같이) 쓴다. 본문은 제목으로 걸러낸 뒤에
    # 꺼낸다 — 한 번에 고르면 수천 통의 본문을 정렬 버퍼에 올린다.
    rows = store.db.execute(
        "SELECT id, thread_id, subject FROM messages WHERE is_sent=1 "
        "ORDER BY sent_on DESC LIMIT 400").fetchall()
    # 숨긴 스레드의 내 발신도 표본에서 뺀다 — 문체 참고라도 본문 1200자가
    # 프롬프트에 통째로 실린다. 숨긴 대화의 내용이 새는 경로다(2026-08-02).
    deny = store.hidden_thread_ids()
    out = []
    for r in rows:
        if r["thread_id"] in deny:
            continue
        if not _TONE_SUBJECT_RX.search(r["subject"] or ""):
            continue
        body = store.db.execute(
            "SELECT new_content FROM messages WHERE id=?", (r["id"],)).fetchone()
        # 보존 인용은 상대가 쓴 글이다 — 떼고 나서 길이를 잰다. 안 떼면 '내가 쓴
        # 400자'가 아니라 '인용이 붙어 400자를 넘긴 메일'을 고르게 되고, 표본으로
        # 학습할 문체가 남의 문체가 된다(2026-08-01 실증).
        text = strip_preserved((body["new_content"] or "") if body else "")
        if len(text) < TONE_MIN_CHARS:      # 내가 쓴 분량이 짧으면 문체가 안 드러난다
            continue
        out.append(f"--- {r['subject']}\n{smart_truncate(text, TONE_CHARS)}")
        if len(out) >= TONE_SAMPLES:
            break
    return "\n\n".join(out) if out else "(없음)"


def board_facts(det: dict) -> str:
    """총평이 '지난 차수 대비'를 쓸 수 있는 **유일한** 사실 근거 — 코드가 센 값.

    이전 보고의 문장은 계속 넣지 않는다(사실 오염 위험이 그대로다). 대신 결정론이
    센 카운트와 지난 차수 약속 점검 결과만 준다."""
    st = det["stat"]
    lines = [f"- 이번 기간({det['start']} ~ {det['end']}): 스레드 {st['threads']}건 "
             f"· 내 발신 {st['sent']}통 · 나 지목 {st['named']} · 직접 수신 {st['direct']}"]
    board = {s: len([t for t in det["items"] if t["state"] == s])
             for s in ("내 차례", "막힘", "상대 대기", "마무리")}
    lines.append("- 상태판: " + " · ".join(f"{k} {v}건" for k, v in board.items()))
    if det.get("calendar"):
        lines.append(f"- 확정 기한 {len(det['calendar'])}건")
    lr = det.get("last_round")
    if lr and (lr["kept"] or lr["open"]):
        # '처리함'으로 접은 것을 kept 에 합쳐 세므로 "내 후속 있음"이라 단정하면
        # 코드가 모르는 것을 사실로 말하게 된다(모듈의 '모르면 보고하지 않는다'와 충돌).
        lines.append(f"- 지난 차수({lr['start']} ~ {lr['end']}) 내 약속 "
                     f"{len(lr['kept']) + len(lr['open'])}건 중 "
                     f"후속 또는 처리함 {len(lr['kept'])} · 아직 없음 {len(lr['open'])}")
    return "\n".join(lines)


def _ai(cfg: Config, cmd: list[str], prompt: str, meter: dict,
        schema=None, progress=None, label: str = "",
        on_event=None, cancel=None) -> dict | None:
    """AI 1콜 → JSON 객체. 실패는 None(호출부가 graceful 하게 계속).

    progress+label 이 오면 호출 직전 '콜 n/N · 송신 …' 를 상태 문구에 싣는다 —
    opus·effort high 로 콜 하나가 수 분까지 가는 동안 대기 화면이 멈춘 것처럼
    보이지 않게. 표시는 기존 배관(#wk-stage 1.5초 폴링 + #wk-elapsed 경과초)을
    그대로 타므로 서버·JS 변경이 없다."""
    meter["calls"] += 1
    if progress and label:
        # 송신/수신은 같은 자(review.fmt_bytes)를 쓴다 — 한 카드 안에서 위 줄은
        # KB, 아래 줄은 '자' 로 갈려 보이던 것을 맞춘 것(2026-07-29).
        size = review.fmt_bytes(len(prompt.encode("utf-8")))
        progress(f"{label} · 콜 {meter['calls']}/{MAX_AI_CALLS} · 송신 {size}")
    try:
        raw = review.ai_run(
            cmd, prompt, timeout=240, retries=1,
            system_prompt=WEEKLY_SYSTEM, json_schema=schema, effort="high",
            # effort_flag 는 run_ai_layer 가 meter 에 실어 보낸다 — 7개 호출
            # 지점에 인자를 각각 꿰는 대신, 한 실행에서 불변인 값을 이미 모든
            # 지점을 지나는 meter 에 태운 것(선언 없는 백엔드는 None = 무방출).
            effort_flag=meter.get("effort_flag"),
            on_event=on_event, cancel=cancel,
        )
    except review.AIError:
        return None
    return review._parse_json_obj(raw)


def _mine(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes")


def _brief(written: list[dict]) -> str:
    """토픽 서술을 텍스트로 — 총평(검증 전)과 해석(검증 후)이 같은 형태를 쓴다."""
    return "\n".join(
        f"## {w['name']}\n"
        + "\n".join(f"- {kind}: {it['text']}"
                    for kind, key in (("진행", "progress"), ("이슈", "issues"),
                                      ("향후", "next"))
                    for it in w[key])
        for w in written)


def _exec_lines(v) -> list[str]:
    """OVERVIEW 의 summary 를 항목 리스트로. 배열이 정본이고 문자열도 받는다 —
    백엔드가 CLI 라 형태가 흔들린다(줄바꿈 나열로 돌려주는 모델이 있다).
    렌더가 '- ' 를 붙이므로 앞머리 글머리표는 여기서 뗀다."""
    if isinstance(v, str):
        v = v.splitlines()
    out = []
    for s in v if isinstance(v, list) else []:
        if not isinstance(s, (str, int, float)) or isinstance(s, bool):
            continue                # 중첩 리스트·None 이 repr 로 새면 화면에 찍힌다
        s = re.sub(r"^\s*[-*\u2022]\s*", "", str(s)).strip()
        if s:
            out.append(s[:200])
    return out[:EXEC_TOP]


def _keep(rows, checker: _MessageQuoteChecker, allow: set[int]) -> list[dict]:
    """메시지 단위 인용 검증. mid 누락은 유일한 원문일 때만 복구한다."""
    out = []
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:400]
        quote = str(it.get("quote") or "").strip()[:QUOTE_MAX]
        try:
            tid = int(it.get("tid"))
        except (TypeError, ValueError):
            continue
        if not text or tid not in allow:
            continue
        mine = _mine(it.get("mine"))
        row = checker.resolve(it.get("mid"), tid, quote, mine)
        if not row:
            continue
        out.append({"text": text, "tid": tid, "mid": int(row["id"]),
                    "quote": quote, "mine": mine})
    return out


def _cards(store: Store, cfg: Config, cmd: list[str], candidates: list[dict],
           start: str, end: str, meter: dict, progress=None,
           on_event=None, cancel=None) -> dict[int, list[dict]]:
    checker = _MessageQuoteChecker(store, start, end)
    out: dict[int, list[dict]] = {}
    total = (len(candidates) + CARD_BATCH - 1) // CARD_BATCH
    for pos in range(0, len(candidates), CARD_BATCH):
        batch = candidates[pos:pos + CARD_BATCH]
        res = _ai(cfg, cmd, CARD.format(
            source=_card_source(store, batch, start, end)),
            meter, _CARD_SCHEMA, progress,
            f"원문 근거 {pos // CARD_BATCH + 1}/{total} 묶음 읽는 중",
            on_event, cancel) or {}
        raw_by_tid: dict[int, list] = {}
        for entry in res.get("threads") or []:
            if not isinstance(entry, dict):
                continue
            try:
                tid = int(entry.get("tid"))
            except (TypeError, ValueError):
                continue
            raw_by_tid[tid] = entry.get("facts") or []
        for t in batch:
            tid = t["thread_id"]
            raw = raw_by_tid.get(tid) or []
            facts = _keep(raw, checker, {tid})
            clean = []
            for fact in facts[:4]:
                source = next((
                    x for x in raw if isinstance(x, dict)
                    and str(x.get("quote") or "").strip()[:QUOTE_MAX] == fact["quote"]
                ), {})
                kind = str(source.get("kind") or "progress")
                fact["kind"] = kind if kind in ("progress", "issue", "next") else "progress"
                clean.append(fact)
            out[tid] = clean or _fallback_fact(store, t, start, end)
    return out


def _best_fact(facts: list[dict]) -> dict | None:
    order = {"issue": 0, "next": 1, "progress": 2}
    return min(facts, key=lambda f: order.get(f.get("kind"), 3)) if facts else None


def run_ai_layer(store: Store, cfg: Config, det: dict,
                 backend: str | None = None, progress=None,
                 on_event=None, cancel=None) -> dict | None:
    """원문 카드 → 토픽 → 서술 → 누락 → 총평 → 의미 검증."""
    items = det["items"]
    if not items:
        return None
    bk_name = backend or cfg.backend_for("weekly")
    try:
        cmd = cfg.ai_cmd(bk_name)
    except SystemExit:
        return None

    # 실모델 포착 — 백엔드 '이름'(opus 등)은 움직이는 별칭이라 보고서에는
    # 스트리밍 init 이벤트가 알려준 실제 모델 ID 를 남긴다(비스트리밍이면 빈 값).
    seen_model = {"v": ""}
    if on_event is not None:
        _outer_event = on_event

        def on_event(info):                # noqa: F811 — 의도된 래핑
            if info.get("ev") == "model" and info.get("model"):
                seen_model["v"] = str(info["model"])
            _outer_event(info)

    meter = {"calls": 0, "effort_flag": cfg.ai_effort_flag(bk_name)}
    by_id = {t["thread_id"]: t for t in items}
    candidates = _candidate_items(items)
    candidate_ids = {t["thread_id"] for t in candidates}
    cards = _cards(
        store, cfg, cmd, candidates, det["start"], det["end"], meter, progress,
        on_event, cancel)

    grouped = _ai(cfg, cmd, GROUP.format(
        max_topics=MAX_TOPICS, threads=_thread_lines(candidates, cards)),
        meter, _GROUP_SCHEMA, progress, "토픽 묶는 중", on_event, cancel)
    topics_in = (grouped or {}).get("topics") or []
    topics: list[dict] = []
    claimed: set[int] = set()
    for t in topics_in[:MAX_TOPICS]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()[:60]
        tids = []
        for x in t.get("threads") or []:
            if not isinstance(x, (int, str)) or not str(x).isdigit():
                continue
            tid = int(x)
            if tid in candidate_ids and tid not in claimed:
                tids.append(tid)
        if name and tids:
            topics.append({"name": name, "tids": tids})
            claimed.update(tids)
    if not topics:
        topics = [{"name": t["subject"][:60], "tids": [t["thread_id"]]}
                  for t in candidates[:MAX_TOPICS]]

    checker = _MessageQuoteChecker(store, det["start"], det["end"])
    prev = previous_report(cfg, det["start"])
    # 사용자 지침(ai-rules.md)은 서술·총평에만 넣는다 — 추출(CARD)·누락 점검
    # (CHECK)·검증(VERIFY_REPORT)에 넣으면 지침이 의역을 유발해 인용 검증
    # 탈락률만 올린다. 라벨이 '사실 근거 아님'을 명시하는 이유는 이 모듈의
    # 원칙(누적 요약·이전 보고를 사실 근거로 안 쓴다)과 한 몸이 되기 위해서다.
    rules = cfg.ai_rules_text()
    rules_user = (f"\n[사용자 지침 — 표현·우선순위 지시. 사실 근거가 아니며"
                  f" 인용 대상이 아니다]\n{rules}\n" if rules else "")
    written, dropped = [], 0
    for i, tp in enumerate(topics, 1):
        res = _ai(cfg, cmd, WRITE.format(
            name=tp["name"], start=det["start"], end=det["end"],
            rules_user=rules_user,
            before=_before_state(store, tp["tids"], det["start"]),
            previous=prev,
            mails=_topic_mails(store, tp["tids"], det["start"], det["end"])),
            meter, _WRITE_SCHEMA, progress, f"토픽 {i}/{len(topics)} 서술 중",
            on_event, cancel)
        if not res:
            continue
        allow = set(tp["tids"])
        sec = {k: _keep(res.get(k), checker, allow)
               for k in ("progress", "issues", "next")}
        raw_n = sum(len(res.get(k) or []) for k in ("progress", "issues", "next"))
        dropped += raw_n - sum(len(v) for v in sec.values())
        if any(sec.values()):
            written.append({"name": tp["name"], "tids": tp["tids"], **sec})
    if not written:
        return None

    board = board_facts(det)
    ov = _ai(
        cfg, cmd, OVERVIEW.format(topics=_brief(written), top=EXEC_TOP,
                                  rules_user=rules_user,
                                  board=board, tone=tone_samples(store)),
        meter, _OVERVIEW_SCHEMA, progress, "총평 정리 중",
        on_event, cancel) or {}
    summary = _exec_lines(ov.get("summary"))
    # 빈 결과의 이유를 갈라 둔다 — 호출 실패를 '특이사항 없음'이라 말하면 거짓이다
    summary_state = "ok" if summary else ("none" if ov else "failed")
    order = [str(x) for x in (ov.get("order") or [])]
    if order:
        rank = {n: i for i, n in enumerate(order)}
        written.sort(key=lambda w: rank.get(w["name"], len(rank)))

    covered = {tid for w in written for tid in w["tids"]}
    candidate_rest = [t for t in candidates if t["thread_id"] not in covered]
    missed = []
    if candidate_rest:
        chk = _ai(cfg, cmd, CHECK.format(
            topics=", ".join(w["name"] for w in written),
            rest=_thread_lines(candidate_rest, cards)),
            meter, _CHECK_SCHEMA, progress, "누락 점검 중",
            on_event, cancel) or {}
        selected = []
        for m in chk.get("missed") or []:
            if not isinstance(m, dict):
                continue
            try:
                tid = int(m.get("tid"))
            except (TypeError, ValueError):
                continue
            if tid in candidate_ids and tid not in covered and tid not in selected:
                selected.append(tid)
        # 모델이 놓쳐도 내 차례·막힘·결정·기한은 조용히 사라지지 않는다.
        for t in candidate_rest:
            if (t["state"] in ("내 차례", "막힘")
                    or t["decision"] or t["deadline"]):
                if t["thread_id"] not in selected:
                    selected.append(t["thread_id"])
        for tid in selected[:5]:
            fact = _best_fact(cards.get(tid) or [])
            if fact:
                missed.append({
                    "tid": tid, "mid": fact["mid"], "why": fact["text"],
                    "quote": fact["quote"], "mine": fact["mine"],
                    "subject": by_id[tid]["subject"],
                })

    claims = []
    refs: dict[str, dict] = {}
    for w in written:
        for key in ("progress", "issues", "next"):
            for it in w[key]:
                rid = f"r{len(claims)}"
                refs[rid] = it
                claims.append({
                    "id": rid, "text": it["text"], "mid": it["mid"],
                    "quote": it["quote"],
                })
    for it in missed:
        rid = f"r{len(claims)}"
        refs[rid] = it
        claims.append({
            "id": rid, "text": it["why"], "mid": it["mid"],
            "quote": it["quote"],
        })
    verified = _ai(
        cfg, cmd, VERIFY_REPORT.format(
            claims=json.dumps(claims, ensure_ascii=False),
            summary="\n".join(f"- {s}" for s in summary) or "(없음)", board=board),
        meter, _VERIFY_REPORT_SCHEMA, progress, "보고 근거 검증 중",
        on_event, cancel)
    semantic_checked = bool(verified)
    if verified:
        supported = {str(x) for x in verified.get("supported") or []}
        for w in written:
            for key in ("progress", "issues", "next"):
                before_n = len(w[key])
                w[key] = [it for it in w[key]
                          if next((rid for rid, ref in refs.items() if ref is it), "")
                          in supported]
                dropped += before_n - len(w[key])
        written = [w for w in written if any(
            w[k] for k in ("progress", "issues", "next"))]
        missed = [it for it in missed
                  if next((rid for rid, ref in refs.items() if ref is it), "")
                  in supported]
        if not verified.get("summary_supported"):
            summary = []
            summary_state = "unverified"
    if not written:
        return None

    # 해석 층 — 검증을 통과한 서술만 재료로 쓰는 비인용 단계. 실패하면 생략
    # (사실 층은 불변). 상태 카운트는 결정론 값이라 해석의 닻이 된다.
    n_state: dict[str, int] = {}
    for t in candidates:
        n_state[t["state"]] = n_state.get(t["state"], 0) + 1
    stats = " · ".join(f"{k} {v}건" for k, v in sorted(n_state.items()))
    ins = _ai(cfg, cmd, INSIGHT.format(
        brief=_brief(written),
        missed="\n".join(f"- {m['subject']} — {m['why']}" for m in missed)
               or "(없음)",
        stats=stats or "(없음)"),
        meter, _INSIGHT_SCHEMA, progress, "해석 정리 중",
        on_event, cancel) or {}
    insights = []
    for it in (ins.get("insights") or [])[:4]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:300]
        if text:
            insights.append({"topic": str(it.get("topic") or "").strip()[:60],
                             "text": text})

    accounted = {tid for w in written for tid in w["tids"]}
    accounted.update(m["tid"] for m in missed)
    rest = [t for t in items if t["thread_id"] not in accounted]
    if progress:
        progress("완료")
    return {"summary": summary, "summary_state": summary_state,
            "topics": written, "missed": missed,
            "insights": insights,
            "rest": rest, "calls": meter["calls"], "dropped": dropped,
            "semantic_checked": semantic_checked,
            "candidate_count": len(candidates),
            "model": seen_model["v"]}


# ───────────────────────────────────────────────── 렌더

def _bucket(det: dict, state: str) -> list[dict]:
    """상태별 스레드 — 중요한 것부터. 가벼운 건과 '처리함'으로 접은 것은 뺀다.

    접기는 일간의 '오래 멈춘 스레드'와 키를 공유한다 — 한쪽에서 접었는데 다른
    쪽에 남으면 "한 번 처리하면 다음 판단에 안 잡힌다"는 계약이 깨진다."""
    ptids = det.get("promise_tids") or set()
    done = det.get("done_stalled") or set()
    out = [t for t in det["items"] if t["state"] == state
           and review._worth_reporting(t, ptids)
           and not (state == "막힘"
                    and review.stalled_key(t["thread_id"]) in done)]
    return sorted(out, key=lambda t: -t.get("score", 0))


def _render_bucket(out: list[str], title: str, items: list[dict], fmt) -> None:
    out.append(f"## {title} ({len(items)}건)")
    for t in items[:WEEKLY_TOP]:
        out.append(fmt(t))
    if len(items) > WEEKLY_TOP:
        out.append(f"- … 외 {len(items) - WEEKLY_TOP}건 (중요도 낮은 순)")
    out.append("")


def _render_calendar(out: list[str], det: dict) -> None:
    """기한 — 날짜가 확정된 것만, 날짜순. 정보성 공지는 표시로 낮춘다.

    상대 표현이 실제 날짜로 환산되지 않으면 싣지 않는다(틀린 기한을 박느니
    비운다). 중요도가 낮아도 빼지는 않는다 — 놓치면 곤란한 것이 섞여 있다."""
    cal = det.get("calendar") or []
    if not cal:
        return
    out.append(f"## 기한 ({len(cal)}건)")
    for c in cal:
        tag = " · 중요도 낮음" if c["low"] else ""
        out.append(f"- **{c['due']:%m/%d}** [#{c['thread_id']}] {c['subject']}{tag}"
                   + review.done_mark("deadline",
                                      Store.report_key(c["thread_id"], c["quote"])))
        out.append(f"  「{c['quote']}」")
    out.append("")


def _render_last_round(out: list[str], det: dict) -> None:
    """지난 차수에 내가 한 약속이 그 뒤 어떻게 됐나.

    **'안 지켰다'고 쓰지 않는다.** 아는 사실은 "그 뒤 내가 그 스레드에 보낸 것이
    없다" 뿐이고, 다른 스레드나 메일 밖에서 처리했을 수 있다(예전 '지금 할 일'이
    추측으로 신뢰를 잃은 전례). 웹의 '처리함'으로 접은 것은 후속으로 센다."""
    lr = det.get("last_round")
    if not lr or not (lr["kept"] or lr["open"]):
        return                    # 지난 차수가 없거나, 그때 약속이 없었다
    today = date.fromisoformat(det["end"])
    out.append(f"## 지난 차수 점검 ({lr['start']} ~ {lr['end']})")
    if lr["kept"]:
        head = " · ".join(f"[#{p['thread_id']}] {_short(p['subject'])}"
                          for p in lr["kept"][:WEEKLY_TOP])
        more = (f" 외 {len(lr['kept']) - WEEKLY_TOP}건"
                if len(lr["kept"]) > WEEKLY_TOP else "")
        out.append(f"- 후속 있음 ({len(lr['kept'])}건): {head}{more}")
    if lr["open"]:
        out.append(f"- 아직 내 후속 없음 ({len(lr['open'])}건)")
        for p in lr["open"][:WEEKLY_TOP]:
            due = ""
            if p["due"]:
                due = f" · 기한 {p['due']:%m/%d}" + (" 지남" if p["due"] < today else "")
            out.append(f"  - [#{p['thread_id']}] {_short(p['subject'])}{due}"
                       + review.done_mark("promise", p.get("key", "")))
            out.append(f"    「{p['quote']}」")
        if len(lr["open"]) > WEEKLY_TOP:
            out.append(f"  - … 외 {len(lr['open']) - WEEKLY_TOP}건")
    out.append("")


def _short(s: str, n: int = 40) -> str:
    """제목 줄임 — 한 줄에 여러 건을 가운뎃점으로 늘어놓는 자리용."""
    s = (s or "").strip() or "(제목 없음)"
    return s if len(s) <= n else s[:n - 1] + "…"


def render(det: dict, ai: dict | None) -> str:
    """주간 보고 마크다운. AI 없으면 결정론 뼈대만(#10 — 도구는 항상 산다).

    2026-08-01 재구성: 토픽별 서술 앞에 **상태판**(내 차례·막힘·기한)을 세운다.
    읽고 나서 무엇을 해야 할지가 남아야 하는데, 진행 항목이 같은 무게로 나열되면
    그것이 안 보였다."""
    st = det["stat"]
    out = [f"# {det['start']} ~ {det['end']} 주간 보고", ""]
    if det.get("ai_error"):
        # 인증 만료 등으로 AI 보강이 중단됨 — 로그 없이도 원인이 보이게 머리에
        out += [f"> {det['ai_error']} (AI 보강 없이 뼈대만)", ""]
    # Executive Summary — 상위 management 보고 톤. 대상 선정은 결정론, 문장만 AI.
    # AI 가 없으면 비운다(결정론 흉내는 읽는 값이 없다 — 2026-08-01 사용자 확정).
    # AI 를 안 돌렸으면 절 자체를 내지 않는다. 돌렸는데 비었으면 그 이유를 말한다
    # (일간과 같은 문구 표 — review.EXEC_EMPTY).
    head = (ai or {}).get("summary") or []
    state = (ai or {}).get("summary_state") or ""
    if head or state:
        out += ["## Executive Summary"]
        out += ([f"- {s}" for s in head[:EXEC_TOP]]
                or [review.EXEC_EMPTY.get(state, review.EXEC_EMPTY["none"])])
        out += [""]
    _render_bucket(out, "내 차례", _bucket(det, "내 차례"),
                   lambda t: f"- [#{t['thread_id']}] {t['subject']} — "
                             f"{max(t['people'], key=t['people'].get) if t['people'] else '?'}")
    _render_bucket(out, "막힘", _bucket(det, "막힘"),
                   lambda t: f"- [#{t['thread_id']}] {t['subject']} — {t['state_note']}"
                             + review.done_mark("stalled",
                                                review.stalled_key(t["thread_id"])))
    _render_calendar(out, det)
    _render_last_round(out, det)

    if ai and ai.get("topics"):
        for i, w in enumerate(ai["topics"], 1):
            refs = " ".join(f"[#{t}]" for t in w["tids"])
            out.append(f"## {i}. {w['name']} {refs}")
            for label, key in (("진행", "progress"), ("이슈", "issues"),
                               ("향후", "next")):
                for it in w[key]:
                    mark = "*" if it["mine"] else ""
                    out.append(f"- **{label}** {mark}{it['text']} "
                               f"「{it['quote']}」 "
                               f"[메일 #{it['mid']} · 스레드 #{it['tid']}]")
            out.append("")
        if ai.get("missed"):
            out.append("## 짚어둘 것")
            for m in ai["missed"]:
                out.append(
                    f"- {m['subject']} — {m['why']} 「{m['quote']}」 "
                    f"[메일 #{m['mid']} · 스레드 #{m['tid']}]")
            out.append("")
        if ai.get("insights"):
            out.append("## 해석")
            out.append("_검증된 사실에서 도출한 AI 해석 — 원문 인용이 없는 "
                       "참고 의견입니다._")
            for it in ai["insights"]:
                tp = f"**[{it['topic']}]** " if it["topic"] else ""
                out.append(f"- {tp}{it['text']}")
            out.append("")
        # 상태판에 이미 있는 것은 '그 외'에서 뺀다 — 비-AI 경로는 원래 그랬는데
        # AI 경로만 토픽 커버리지로 rest 를 만들어 같은 스레드가 두 번 나왔다.
        board = ({t["thread_id"] for t in _bucket(det, "내 차례")}
                 | {t["thread_id"] for t in _bucket(det, "막힘")})
        rest = [t for t in (ai.get("rest") or [])
                if t.get("thread_id") not in board]
    else:
        # AI 가 없으면 토픽 서술이 없다. 상태판(내 차례·막힘·기한)이 이미 위에
        # 있으므로 여기서 전량을 다시 나열하지 않는다 — 중복이고, 개수 제한
        # 원칙과도 어긋난다(2026-08-01 재구성). 상태판에 안 걸린 것만 남긴다.
        shown = {t["thread_id"] for t in _bucket(det, "내 차례")}
        shown |= {t["thread_id"] for t in _bucket(det, "막힘")}
        rest = [t for t in det["items"] if t["thread_id"] not in shown]

    if rest:
        out.append(f"## 그 외 ({len(rest)})")
        for t in rest[:REST_TOP]:
            out.append(f"- {t['subject']} · {t['state']} "
                       f"· {t['last'][:10]} [#{t['thread_id']}]")
        if len(rest) > REST_TOP:            # 말없이 자르지 않는다
            out.append(f"- … 외 {len(rest) - REST_TOP}건")
        out.append("")

    out.append("---")
    scope = (f"조사 범위: 스레드 {st['threads']} · 내 발신 {st['sent']} "
             f"· 나 지목 {st['named']} · 직접 수신 {st['direct']}")
    if ai:
        scope += f" · AI {ai['calls']}콜"
        if ai.get("model"):
            scope += f" · 모델 {ai['model']}"
        scope += f" · 원문 후보 {ai.get('candidate_count', st['threads'])}"
        if ai.get("dropped"):
            scope += f" · 인용 검증 탈락 {ai['dropped']}"
        if not ai.get("semantic_checked"):
            scope += " · 의미 검증 미완료"
    out.append(scope)
    out.append("별표(*)는 내 발신 메일에서 인용한 '내가 한 것'을 뜻합니다.")
    return "\n".join(out)


def report_path(cfg: Config, det: dict) -> Path:
    """이 기간 보고서의 저장 경로 — write 와 같은 규칙(호출부가 존재 여부를
    먼저 볼 수 있게 분리했다: AI 가 중단됐는데 기존 보고가 있으면 덮지 않는다)."""
    return cfg.vault / "weekly" / f"{det['end']}.md"


def write(cfg: Config, det: dict, content: str) -> Path:
    """vault/weekly/<종료일>.md 로 저장."""
    path = report_path(cfg, det)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def generate(store: Store, cfg: Config, weeks: int = WINDOW_WEEKS,
             ai: bool = False, backend: str | None = None,
             today: str | None = None, progress=None,
             on_event=None, cancel=None) -> tuple[str, dict]:
    """주간 보고 생성 — (마크다운, det). AI 실패는 삼키고 뼈대만 반환한다.
    AICancelled 는 삼키지 않는다 — 취소는 실패가 아니라 사용자의 결정이다."""
    det = deterministic(store, cfg, weeks, today)
    try:
        layer = (run_ai_layer(store, cfg, det, backend, progress,
                              on_event=on_event, cancel=cancel) if ai else None)
    except review.AIAuthError as e:
        # 인증 만료 — AI 보강만 접고 결정론 뼈대는 그대로 낸다. 보고서 머리에
        # 안내를 실어 로그를 열지 않아도 원인이 보이게 한다.
        layer = None
        det["ai_error"] = str(e).splitlines()[0]
    det["ai"] = layer
    return render(det, layer), det
