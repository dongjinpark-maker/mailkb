"""지식 증류 계층 — 데일리 '수확(harvest)': 암묵지 후보·인물/프로젝트 신호.

설계(docs/ARCHITECTURE.md §6.6b): 데일리 AI 의 임무는 통찰 생산이 아니라 **수확**
— 오늘 메일에서 '축적할 사실'(암묵지 후보·인물/프로젝트 신호)을 구조화 추출해
쌓는다. 결정 원장 축은 2026-08-14 폐지 — 활용도가 낮아 사용자가 제거를 확정했고,
"무엇을/왜"는 암묵지 축이 흡수한다.

환각 가드: 모든 추출 항목에 원문 인용(quote)을 강제하고, 해당 스레드
new_content 에 부분일치(공백 무시)하지 않으면 그 항목을 버린다.
암묵지의 확정은 사람(회고 화면 [지식으로 저장]) — 여기서는 pending 으로만 적재.

AI 호출은 review.ai_run 재사용(테스트 mock 경로 통일을 위해 review 모듈 참조).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import review
from .clean import smart_truncate, strip_preserved
from .config import Config
from .store import DOSSIER_VALIDATOR_VERSION, Store

# ------------------------------------------------------------------ 프롬프트

# 수확 — 창의적 해석 금지, 명시된 사실만. 인용은 검증되므로 의역하면 버려진다.
HARVEST = """당신은 업무 메일에서 '축적할 사실'을 수확하는 추출기다. 아래는 {date} 에 활동한 업무 스레드들의 새 메일이다. 창의적 해석·추측 없이, 본문에 명시된 것만 추출하라.

출력 형식 (마크다운, 섹션 4개 고정 — 해당 없으면 그 섹션에 "- 없음" 한 줄):
## 오늘 델타
- 직전 수확 이후 달라진 것만 3~6줄, 각 줄 끝에 #스레드번호. 직전 델타에 이미 있는 내용 반복 금지.
## 인물 신호
- <이름> | <역할·담당·상태에 관한 새 사실 한 줄> | #<스레드번호> | 인용: "<원문 문장 그대로>"
## 프로젝트 신호
- #<스레드번호> | <사안 상태 변화: 이전 → 이후> | 인용: "<원문 문장 그대로>"
## 암묵지 후보
- 제목: <재사용 가능한 자기완결 한 문장 — "X 는 Y 로 한다/풀었다" 꼴> | 내용: <2~4문장. 왜 그렇게 하는지, 시도했다 안 된 것이 있으면 그것도> | #<스레드번호>[, #<스레드번호>] | 인용: "<근거가 명시된 원문 문장 그대로>"

규칙:
- '암묵지'는 **검색으로 알 수 있는 일반 지식을 제외**하라 — 이 조직·이 프로젝트 고유의 판단 기준, 제약, 우회로, 합의된 방식만. 확정된 합의("X 로 하기로 했다")도 그 이유가 함께 있으면 암묵지다. 완결된 일만이 아니다: 진행 중이어도 방식이 명시돼 있으면 캐라. 실패한 시도(무엇을 왜 기각했는지)는 그 자체로 지식이다.
- 인용은 반드시 원문에 있는 문장을 글자 그대로 옮겨라 (요약·의역 금지). 인용할 문장이 없으면 그 항목을 만들지 마라.
- 없는 스레드 번호를 만들지 마라. 억지로 채우지 마라 — 없으면 "- 없음".

{rules}[직전 델타 — 반복 금지]
{yesterday}

[스레드]
{items}
"""

# 수확 로그(harvest.jsonl) 품질 감사 지시문 — 첫 기록 시 <home>/logs/ 에 저장.
HARVEST_LOG_ANALYSIS = """# 데일리 수확 로그 분석 지시문

너는 mailkb 의 데일리 '수확'(암묵지 후보·신호 추출) 품질을 감사하는 검토자다.
같은 폴더의 `harvest.jsonl` 이 분석 대상이다.

## 데이터 형식
JSONL — 한 줄 = 하루치 수확 1회. 각 줄: date, backend, raw(모델 원문 출력),
saved_knowledge[](적재된 암묵지 후보: title/threads), n_person, n_project,
n_knowledge, dropped(인용 검증 실패로 버린 항목 수).

## 할 일
1. raw 를 읽고 형식 이탈(섹션 누락·라벨 불일치·의역 인용)을 지적하라.
2. dropped 가 많은 날의 raw 에서 왜 인용 검증에 실패했는지 패턴을 찾아라
   (의역, 여러 문장 합침, 말줄임 등).
3. saved_knowledge 중 '일반 지식'(검색으로 알 수 있는 것)이 섞였는지 판정하라.
4. HARVEST 프롬프트에 보탤 규칙 1~3줄과 few-shot 예시(실제 오추출 축약)를 제안하라.

## 출력 (형식 고정)
### 요약
- 총 실행 N회 · 암묵지 후보 X건 · 신호 Y건 · 드롭 Z건 · 형식이탈 W회
### 오추출/드롭 패턴
- 패턴명: 설명 + 해당 날짜
### 프롬프트 개선 제안
- ...
"""


# ------------------------------------------------------------------ 파서·검증

_TID_RX = re.compile(r"#(\d+)")
_QUOTE_RX = re.compile(r"인용\s*[:：]\s*[\"“]?(.*?)[\"”]?\s*$")
_KN_TITLE_RX = re.compile(r"제목\s*[:：]\s*([^|]+)")
_KN_BODY_RX = re.compile(r"내용\s*[:：]\s*([^|]+)")
_SEC_RX = re.compile(r"^##\s*(.+?)\s*$", re.M)
# 어제 데일리 md 에서 델타 절을 떼는 정규식. **모델 출력의 절 이름(`## 오늘 델타`)이
# 아니라 렌더된 파일의 절 이름**이어야 한다 — 데일리는 `## 오늘 확정·변경 (N건)` 으로
# 쓴다(review._render_changes). 옛 이름을 들고 있어서 `_recent_delta` 가 늘 '(없음)'을
# 돌려줬고, "이미 보고한 것 반복 금지" 재료가 한 번도 프롬프트에 실린 적이 없다
# (2026-08-06 발견). 옛 파일 호환을 위해 두 이름을 다 받는다.
_DELTA_SEC_RX = re.compile(r"## (?:오늘 확정·변경|오늘 델타)[^\n]*\n(.*?)(?=\n## |\Z)",
                           re.S)

_QUOTE_MIN = 10          # 공백 제거 후 최소 길이 — 이보다 짧은 인용은 앵커 불충분
_QUOTE_MAX = 300


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _sections(text: str) -> dict[str, list[str]]:
    """모델 출력 → {섹션명: [불릿 줄...]}. '없음' 줄은 버린다."""
    out: dict[str, list[str]] = {}
    parts = _SEC_RX.split(text or "")
    # parts = [프리앰블, 이름1, 본문1, 이름2, 본문2, ...]
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip()
        lines = []
        for raw in parts[i + 1].splitlines():
            ln = raw.strip()
            ln = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", ln)
            if not ln or ln in ("없음", "(없음)"):
                continue
            lines.append(ln)
        out[name] = lines
    return out


class _QuoteChecker:
    """스레드별 본문(공백 제거) 캐시 — 인용이 실제 단일 메시지에 있는지 검증.

    **보존 인용(mid-join)은 근거에서 뺀다**(2026-08-06). 그 블록은 남이 주고받은
    글을 옮겨 실은 것이라, 그대로 대조하면 A 가 쓴 문장을 B 의 결정으로 원장에
    올릴 수 있다 — 실측 예: 오태양 책임의 「sync_for_cpu 훅 호출은 UMD 책임입니다」가
    강미래 선임 메일 안에 있어, 결정자를 강미래로 붙여도 통과했다. 인물 요약 쪽
    (_PersonQuoteChecker)은 처음부터 이렇게 하고 있었고 여기만 빠져 있었다.
    """

    def __init__(self, store: Store, addr: str | None = None):
        self.store = store
        # None = 발신자 제한 없음. 빈 문자열은 '아무도 아님'이라 그대로 둔다
        # (인물 요약이 빈 주소로 오면 근거 0줄이 되는 종전 동작 유지).
        self.addr = None if addr is None else (addr or "").strip().lower()
        # tid -> [(정규화 본문, 발신자명, 발신자주소, 내가_보냄)]
        self.cache: dict[int, list[tuple]] = {}

    def _bodies(self, tid: int) -> list[tuple]:
        if tid not in self.cache:
            rows = []
            for m in self.store.quote_messages(tid, sender_addr=self.addr):
                body = strip_preserved(m["new_content"] or "").strip()
                if body:
                    rows.append((_norm_ws(body),
                                 (m["sender_name"] or "").strip(),
                                 (m["sender_addr"] or "").strip().lower(),
                                 bool(m["is_sent"])))
            self.cache[tid] = rows
        return self.cache[tid]

    def ok(self, tid: int, quote: str) -> bool:
        q = _norm_ws(quote)
        if not (self._QMIN <= len(q) <= self._QMAX):
            return False
        return any(q in b[0] for b in self._bodies(tid))

    _QMIN = _QUOTE_MIN
    _QMAX = 10_000   # 정규화 후 상한(사실상 무제한 — 원문 길이는 _QUOTE_MAX 로 제한)


class _PersonQuoteChecker(_QuoteChecker):
    """인물 도시에 전용 — 대상 인물이 **직접 쓴** 신규 본문만 인용 근거로 허용."""

    def __init__(self, store: Store, addr: str):
        super().__init__(store, addr)


def _norm_name(s: str) -> str:
    return "".join((s or "").split()).lower()


def _parse_line_common(line: str) -> tuple[int | None, str]:
    """줄에서 (스레드번호, 인용) 추출 — 없으면 (None, '')."""
    m = _TID_RX.search(line)
    tid = int(m.group(1)) if m else None
    qm = _QUOTE_RX.search(line)
    quote = (qm.group(1).strip() if qm else "")[:_QUOTE_MAX]
    return tid, quote


def parse_harvest(text: str) -> dict:
    """모델 출력을 구조화 — 검증 전 원시 파싱 (검증·적재는 harvest 가)."""
    sec = _sections(text)
    delta = sec.get("오늘 델타", [])[:8]

    person = []
    for ln in sec.get("인물 신호", []):
        tid, quote = _parse_line_common(ln)
        segs = [s.strip() for s in ln.split("|")]
        if tid is None or len(segs) < 2:
            continue
        person.append({"who": segs[0][:80], "signal": segs[1][:200],
                       "thread_id": tid, "quote": quote})

    project = []
    for ln in sec.get("프로젝트 신호", []):
        tid, quote = _parse_line_common(ln)
        if tid is None:
            continue
        segs = [s.strip() for s in ln.split("|")]
        sig = next((s for s in segs
                    if s and not s.startswith("#") and "인용" not in s[:3]), "")
        if not sig:
            continue
        project.append({"thread_id": tid, "signal": sig[:200], "quote": quote})

    knowledge = []
    for ln in sec.get("암묵지 후보", []):
        _tid, quote = _parse_line_common(ln)
        tm = _KN_TITLE_RX.search(ln)
        bm = _KN_BODY_RX.search(ln)
        # 암묵지는 스레드가 여럿일 수 있다 — 줄의 # 전부를 모은다
        tids = [int(m) for m in _TID_RX.findall(ln)]
        if not tm or not tids:
            continue
        knowledge.append({
            "title": tm.group(1).strip()[:200],
            "body": (bm.group(1).strip() if bm else "")[:1000],
            "thread_ids": tids,
            "quote": quote,
        })

    return {"delta": delta,
            "person": person, "project": project, "knowledge": knowledge}


# ------------------------------------------------------------------ 재료 조립

_CAP_THREADS = 40      # 수확 대상 업무 스레드 상한
_CAP_MSGS = 3          # 스레드당 하루치 메시지 상한 (창이 길면 일수만큼 늘림, 최대 8)
_CAP_BODY = 1000       # 메시지 본문 상한 (자)
_CAP_SUMM = 300        # 롤링 요약 상한 (자)
# 도시에 발췌 — 8스레드 × 2발췌라 한 콜에 다 실려도 작다(300→600 이면 9.6K자).
_CAP_EXCERPT = 600     # 인물 요약용 본인 발췌 상한 (자)


def _recent_delta(cfg: Config, date_iso: str) -> str:
    """가장 최근 데일리 md(최대 7일 소급)의 '오늘 델타' 섹션 — 반복 금지 재료.

    하루 이틀 건너뛴 경우 어제 파일이 없으므로, 있는 것 중 최신을 쓴다."""
    try:
        base = date.fromisoformat(date_iso)
    except ValueError:
        return "(없음)"
    for back in range(1, 8):
        path = Path(cfg.vault) / "daily" / f"{(base - timedelta(days=back)).isoformat()}.md"
        if not path.exists():
            continue
        try:
            m = _DELTA_SEC_RX.search(path.read_text(encoding="utf-8"))
        except OSError:
            return "(없음)"
        if m and m.group(1).strip():
            return m.group(1).strip()[:1500]
        return "(없음)"
    return "(없음)"


def _harvest_window(store: Store, cfg: Config, date_iso: str) -> tuple[str, str]:
    """수확 창 (start_day, last_ts) — 하루 이틀 건너뛰어도 다음 실행이 소급한다.

    - 마커 `last_harvest`(프롬프트에 실은 가장 최신 메시지의 타임스탬프) 이후의
      메시지만 재료 → 같은 날 재실행은 새 메일이 없으면 AI 콜 없이 끝난다.
    - 소급 상한 = ai.summary_max_days(기본 1일 — 오늘만, 요약과 공유). 건너뛴 날
      소급이 필요하면 config 에서 늘린다.
    - 과거 --date 백필(마커보다 과거 날짜)은 그 날짜 하루만 보고 마커는 안 움직인다.
    """
    n = max(1, int(cfg.opt("ai", "summary_max_days", default=1)))
    try:
        floor = (date.fromisoformat(date_iso) - timedelta(days=n - 1)).isoformat()
    except ValueError:
        floor = date_iso
    last_ts = store.get_state("last_harvest") or ""
    if last_ts and date_iso < last_ts[:10]:
        return date_iso, ""            # 백필 모드: 그 날 하루, 워터마크 미적용
    return floor, last_ts


def _harvest_items(store: Store, cfg: Config, start_day: str, end_day: str,
                   last_ts: str) -> tuple[str, str]:
    """창 안의 업무 스레드 블록 + 실은 메시지의 최대 타임스탬프(마커 전진용).

    창 안 메시지 중 last_ts 이후 것만 싣는다(재실행 시 중복 과금 방지)."""
    try:
        days = (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days + 1
    except ValueError:
        days = 1
    per_cap = min(8, _CAP_MSGS * max(1, days))
    picked = []                # (플래그, 마지막 활동, tid, 창 메시지, 제목, 요약)
    # 숨긴 스레드는 수확 재료에서 뺀다 — 안 거르면 숨긴 대화의 원문이 프롬프트에
    # 실린다(2026-08-02 점검). 숨긴 스레드만 신규면 blocks 가 비어 AI 콜 자체가
    # 없고 마커도 안 전진한다 — 다음 실행이 재확인할 뿐 비용은 0.
    deny = store.hidden_thread_ids()
    for tid in store.threads_active_between(start_day, end_day):
        if tid in deny:
            continue
        t = store.thread(tid)
        msgs = store.thread_messages(tid)
        if not t or not msgs:
            continue
        if review.thread_kind(cfg, msgs) != "work":
            continue
        win = [m for m in msgs
               if start_day <= (m["sent_on"] or "")[:10] <= end_day
               and (not last_ts or m["sent_on"] > last_ts)]
        if not win:
            continue
        picked.append((bool(t["flagged"]), win[-1]["sent_on"], tid, win[-per_cap:],
                       msgs[0]["subject"], t["rolling_summary"] or ""))
    # 플래그(🚩) 스레드 먼저, 그 안에서 최근 활동순 — 바쁜 날 상한(_CAP_THREADS)
    # 에서 사용자가 중요 표시한 건이 잘려나가지 않게. 순서만, 판정 무왜곡.
    picked.sort(key=lambda x: x[1], reverse=True)      # 2차 기준: 최근 활동
    picked.sort(key=lambda x: x[0], reverse=True)      # 1차 기준: 플래그(안정 정렬)
    blocks, max_ts = [], ""
    for _, _, tid, win, subject, summ in picked[:_CAP_THREADS]:
        # 요약은 split-join 평탄화로 개행이 사라져 표 구조가 이미 없다 —
        # smart_truncate 를 걸 이유도 효과도 없는 자리(본문 절단과 다르다)
        # **진단(파생물)을 재료로 넣지 않는다**(2026-08-16). 어제는 인용 꼬리만
        # 뺐는데, 서술도 그 스레드의 AI 산출이라 같은 고리다 — 수확이 그것을
        # 근거 삼으면 지난주 판단이 오늘 수확으로 되돌아온다. 주간 보고가
        # 이미 지키는 규칙("사실·상태·선별을 전부 원문에서 다시 한다")을
        # 수확에도 적용한다. 그 자리는 아래 창(원문)이 메운다.
        head = f"[#{tid}] {subject}"
        body = ""
        for m in win:
            who = "나" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
            when = m["sent_on"][5:10] + " " + m["sent_on"][11:16]
            body += (f"\n  ({when} {who}) "
                     + smart_truncate((m["new_content"] or "").strip(), _CAP_BODY))
            if m["sent_on"] > max_ts:
                max_ts = m["sent_on"]
        blocks.append(head + body)
    return "\n".join(blocks), max_ts


# ------------------------------------------------------------------ 수확 실행

def _log_harvest(cfg: Config, rec: dict) -> None:
    """<home>/logs/harvest.jsonl 누적 + 분석 지시문 1회 저장 (실패는 삼킴)."""
    try:
        d = cfg.home / "logs"
        d.mkdir(parents=True, exist_ok=True)
        analyze = d / "ANALYZE-harvest.md"
        if not analyze.exists():
            analyze.write_text(HARVEST_LOG_ANALYSIS, encoding="utf-8")
        with (d / "harvest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def harvest(store: Store, cfg: Config, det: dict,
            backend: str | None = None, on_event=None, cancel=None,
            on_error=None) -> dict | None:
    """데일리 수확 — 지난 수확 이후 업무 스레드에서 신호·암묵지 후보를 추출해
    적재. 소급 창은 ai.summary_max_days(기본 1일 — 오늘만)이라, 며칠 건너뛴 뒤
    소급하려면 그 값을 늘려야 한다(_window 참고).

    반환: {"delta": [...], "person", "project", "knowledge", "dropped"} —
    재료 없음(새 메일 없음 포함)/백엔드 미설정/호출 실패면 None
    (graceful, 데일리는 결정론 섹션만으로 살아남는다).
    """
    date_iso = det.get("date") or date.today().isoformat()
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return None
    start_day, last_ts = _harvest_window(store, cfg, date_iso)
    items, max_ts = _harvest_items(store, cfg, start_day, date_iso, last_ts)
    if not items:
        return None
    rules = cfg.ai_rules_text()
    rules_block = f"[사용자 지침 — 우선 적용]\n{rules}\n\n" if rules else ""
    period = date_iso if start_day >= date_iso else f"{start_day} ~ {date_iso}"
    prompt = HARVEST.format(date=period, rules=rules_block,
                            yesterday=_recent_delta(cfg, date_iso),
                            items=items)
    try:
        raw = review.ai_run(cmd, prompt, on_event=on_event, cancel=cancel)
    except review.AIError as e:
        review._notify_error(on_error, e)   # 삼키는 자리가 곧 보고 자리
        return None
    # 수확 성공 → 워터마크 전진(앞으로만 — 백필은 max() 가드가 자연 처리).
    # 저장 건수와 무관: 모델이 본 메일은 재실행 때 다시 과금하지 않는다.
    if max_ts:
        cur = store.get_state("last_harvest")
        store.set_state("last_harvest", max(cur, max_ts) if cur else max_ts)

    parsed = parse_harvest(raw)
    checker = _QuoteChecker(store)
    dropped = 0
    person_saved, project_saved = [], []
    for s in parsed["person"]:
        if not checker.ok(s["thread_id"], s["quote"]):
            dropped += 1
            continue
        store.add_signal(date_iso, "person", s["who"], s["thread_id"],
                         s["signal"], s["quote"])
        person_saved.append(s)
    for s in parsed["project"]:
        if not checker.ok(s["thread_id"], s["quote"]):
            dropped += 1
            continue
        store.add_signal(date_iso, "project", "", s["thread_id"],
                         s["signal"], s["quote"])
        project_saved.append(s)
    knowledge_saved = []
    for k in parsed["knowledge"]:
        # 인용은 참조 스레드 중 **어느 하나**의 본문에 있으면 된다(다중 참조).
        # 발신자 제한은 걸지 않는다 — 남이 알려준 노하우도 지식이다.
        if not any(checker.ok(t, k["quote"]) for t in k["thread_ids"]):
            dropped += 1
            continue
        kid = store.add_knowledge_candidate(
            date_iso, k["title"], k["body"],
            ";".join(str(t) for t in k["thread_ids"]), k["quote"])
        if kid:                            # None = 중복(살아 있는 같은 제목)
            knowledge_saved.append({**k, "id": kid})

    result = {"delta": parsed["delta"],
              "person": person_saved, "project": project_saved,
              "knowledge": knowledge_saved, "dropped": dropped}
    if cfg.opt("ai", "harvest_log", default=True):
        _log_harvest(cfg, {
            "date": date_iso, "backend": backend, "raw": raw[:8000],
            "n_person": len(person_saved), "n_project": len(project_saved),
            "n_knowledge": len(knowledge_saved),
            "saved_knowledge": [{"title": x["title"],
                                 "threads": x["thread_ids"]}
                                for x in knowledge_saved],
            "dropped": dropped,
        })
    return result


# ───────────────────────────────────────────── 인물 도시에 AI 요약 (v2)
# v1 결정론 카드 위에 얹는 AI 카드. 대상 인물 전용 근거 검증으로 발화자 오귀속을
# 차단 — 인용이 그 사람이 직접 쓴 신규 본문에 없으면 그 줄을 버린다. 백엔드
# 미설정·실패·근거 0줄이어도 v1 결정론 카드는 항상 살아남는다.
# 백엔드는 요약용(사내/로컬) — 회사 메일 발췌가 외부로 나가면 안 된다.

DOSSIER = """당신은 담당자(나)의 동료 한 사람에 대한 **짧은 인물 카드**를 쓴다.
읽는 사람은 이 사람과 매일 일한다 — **발췌를 옮겨 적으면 값이 0이다.**
여러 통을 겹쳐 봐야 보이는 것만 써라.

규칙 (한국어):
- 발췌 복창 금지. 한 통을 그대로 요약한 줄은 쓰지 마라.
- 성격·역량 평가 금지("꼼꼼하다", "일을 잘한다"). 관찰된 **일과 방식**만.
- 한 줄은 한 문장, 40~80자. 길면 아무도 안 읽는다.
- 슬롯은 아래 넷, 이 순서로. 해당 사실이 없으면 그 슬롯째 생략.
- `맡은 일`·`요즘 하는 일`만 `- [#번호] 서술 · 인용: "발췌 조각"` 형식이다.
  인용은 '대상 인물 직접 작성 발췌'에 그대로 있는 조각이어야 한다(코드가 원문과
  대조해 틀리면 그 줄을 버린다). 나머지 두 슬롯은 **인용 없이** 서술만 쓴다.
- 요약·신호·내 회신·기존 카드는 문맥 전용이다. 그 텍스트를 인용 근거로 쓰지 마라.

## 한 줄
- 이 사람이 나에게 어떤 상대인가 — 무엇을 맡은 누구이고 우리 사이에 주로 무엇이
  오가나. 한 문장. (인용 없음)
## 맡은 일
- 담당 영역·책임 (최대 2줄)
## 요즘 하는 일
- 지금 굴리고 있는 일 (최대 2줄). 이 두 줄만 봐도 **무슨 일을 하는 사람인지**
  알 수 있어야 한다.
## 일하는 방식
- 여러 통에서 되풀이되는 패턴 (최대 2줄, 인용 없음). 어디까지 정해서 오나,
  무엇을 먼저 묻나, 결정을 누구에게 넘기나, 먼저 메일을 보내는 쪽인가.
  아래 [나와의 관계] 수치가 뒷받침하면 그 수치를 근거로 말하라.

[나와의 관계 — 코드가 센 값이다. 인용 검증이 필요 없는 사실]
{relation}

[참여 스레드 — 앞쪽이 나와 직접 주고받은 것]
{threads}

[인물 신호]
{signals}

[주요 어휘] {words}

[기존 카드 — 새 재료로 갱신, 없으면 새로 작성]
{prev}

위 형식대로 카드만 출력하라:"""

# 슬롯 계약(2026-08-18). 진단(review._DIAG_*)과 같은 모양이다 — **사실 슬롯만
# 인용을 검증하고 판단 슬롯은 개수·길이만 본다.** 모든 줄에 인용을 요구하던
# 종전 계약이 "프로필이 발췌가 된다"의 원인이었다: 검증을 통과하는 가장 쉬운
# 길이 발췌를 옮겨 적는 것이라, 모델이 그 길로 간다.
_DOSSIER_SECS = ("한 줄", "맡은 일", "요즘 하는 일", "일하는 방식")
_DOSSIER_VERIFIED = ("맡은 일", "요즘 하는 일")
_DOSSIER_CAPS = {"한 줄": 1, "맡은 일": 2, "요즘 하는 일": 2, "일하는 방식": 2}
_DOSSIER_LINE = 200                  # 한 줄 상한(자) — 카드는 화면 한 눈이다
_CAP_MY_REPLY = 300                  # 내 회신은 문맥이라 짧게


def _relation_block(rel: dict) -> str:
    """관계를 **수치로** 넘긴다 — 모델이 세지 않게(세면 틀린다), 그리고 판단
    슬롯이 근거로 쓸 수 있게. 참조로만 도는 관계와 나에게 직접 거는 관계는
    다른 관계이고, 그 차이가 프로필의 절반이다."""
    if not rel or not (rel["to_me"] or rel["cc_only"] or rel["from_me"]):
        return "(집계 없음)"
    out = [f"- 나를 받는 사람(To)에 넣어 보낸 메일 {rel['to_me']}통 · "
           f"참조(Cc)로만 온 것 {rel['cc_only']}통",
           f"- 내가 그 사람에게 보낸 메일 {rel['from_me']}통 · "
           f"함께 있는 스레드 {rel['threads']}개 중 {rel['replied_threads']}개에 내가 답했다",
           f"- 먼저 메일을 보내는 쪽: 그 사람 {rel['they_started']} / 나 {rel['i_started']}"]
    if rel["first"] and rel["last"]:
        out.append(f"- 교신 기간 {rel['first']} ~ {rel['last']}")
    return "\n".join(out)


def _dossier_materials(store: Store, cfg: Config, addr: str,
                       name: str) -> dict | None:
    ctx = store.person_thread_context(addr, limit=8)
    if not ctx:
        return None
    threads = "\n".join(
        f"[#{c['thread_id']}] {c['subject']}"
        + ("  (나에게 직접)" if c.get("direct") else "")
        + ("  (내가 답함)" if c.get("replied") else "")
        + "\n  대상 인물 직접 작성 발췌:\n"
        + "\n".join(
            f"  - [메시지 {e['message_id']}] "
            f"{smart_truncate(e['text'] or '', _CAP_EXCERPT).replace(chr(10), ' ')}"
            for e in c["excerpts"])
        + (f"\n  내 회신(문맥 전용): "
           f"{smart_truncate(c['my_reply'], _CAP_MY_REPLY).replace(chr(10), ' ')}"
           if c.get("my_reply") else "")
        for c in ctx) or "(없음)"
    # 신호도 숨긴 스레드 것은 뺀다 — 제목 한 줄이라도 숨긴 대화의 내용이다
    deny = store.hidden_thread_ids()
    sigs = [s for s in store.person_signals(addr, name)
            if s["thread_id"] not in deny]
    signals = "\n".join(f"[#{s['thread_id']}] {s['signal']}"
                        for s in sigs[:6]) or "(없음)"
    from . import report                       # 지연 임포트(순환 방지)
    texts = [t for t in store.person_sent_texts(addr, limit=200) if t.strip()]
    words = ", ".join(w for w, _ in report.top_words(texts, limit=12)) or "(부족)"
    return {"threads": threads, "signals": signals, "words": words,
            "relation": _relation_block(store.person_relation(addr))}


def _sanitize_dossier(raw: str, checker: "_QuoteChecker") -> str:
    """슬롯 계약대로 정리 — **사실 슬롯만 인용을 원문과 대조**하고, 판단 슬롯은
    개수·길이만 본다(2026-08-18).

    판단 문장에 인용을 요구하지 않는 이유는 진단에서와 같다: 여러 통을 겹쳐야
    보이는 문장은 원문에 그대로 있을 수 없어, 강제하면 모델이 **발췌를 옮겨
    적는 쪽으로 도망친다**. 그것이 프로필이 발췌 모음이 된 원인이었다.
    통과한 줄은 인용 꼬리를 떼고 서술 + #스레드참조만 남긴다(참조는 링크로).
    """
    sec = _sections(raw)
    out: list[str] = []
    for name in _DOSSIER_SECS:
        kept = []
        for ln in sec.get(name, []):
            if name in _DOSSIER_VERIFIED:
                tid, quote = _parse_line_common(ln)
                if tid is None or not checker.ok(tid, quote):
                    continue
            claim = smart_truncate(
                _QUOTE_RX.sub("", ln).rstrip(" ·-").strip(), _DOSSIER_LINE)
            if claim:
                kept.append(f"- {claim}")
            if len(kept) >= _DOSSIER_CAPS[name]:
                break
        if kept:
            out.append(f"## {name}")
            out.extend(kept)
    return "\n".join(out)


@dataclass(frozen=True)
class DossierResult:
    """도시에 1명 생성 결과 — 실패와 검증 0건을 분리해 재시도 정책을 정한다."""

    status: str                          # ok | empty | error | no_material | no_backend
    md: str = ""


def _gen_dossier(store: Store, cfg: Config, cmd, addr: str,
                 name: str, prev_md: str,
                 on_event=None, cancel=None) -> DossierResult:
    materials = _dossier_materials(store, cfg, addr, name)
    if materials is None:
        return DossierResult("no_material")
    prompt = DOSSIER.format(prev=(prev_md or "(없음)"), **materials)
    try:
        raw = review.ai_run(cmd, prompt, on_event=on_event, cancel=cancel)
    except review.AIError:                # AICancelled 는 상위 타입이 아니라 통과
        return DossierResult("error")
    md = _sanitize_dossier(raw, _PersonQuoteChecker(store, addr))
    return DossierResult("ok", md) if md else DossierResult("empty")


def refresh_person_dossier(store: Store, cfg: Config, addr: str,
                           name: str = "", backend: str | None = None,
                           on_event=None, cancel=None) -> DossierResult:
    """인물 1명의 AI 요약을 다시 만든다 — 사용자가 인물 화면에서 누를 때만.

    2026-07-29 이전에는 일간 회고가 돌 때마다 상위 15명 중 낡은 6명을 배치로
    갱신했다. 하루 정리와 인물 카드 유지보수는 성격이 다른 일인데 한 버튼의
    비용·시간에 묶여 있었고, 정작 요약이 필요한 순간은 회고할 때가 아니라 그
    사람 화면을 열 때다. 대신 요약이 낡을 수 있으므로 화면이 '며칠 전 기준 ·
    새 메일 N통 미반영'을 함께 보여준다(web.render_dossier).

    상태별 처리는 배치 시절 그대로다 — ok 는 저장, empty/no_material 은 basis 만
    전진(카드는 비운다), error 는 전진시키지 않는다. 버튼은 사용자가 누른 것이라
    basis 를 보고 콜을 건너뛰지 않는다 — 전진의 실효는 화면의 '새 메일 N통 미반영'
    표시뿐이다.
    AICancelled 는 잡지 않고 올린다(취소는 실패가 아니다)."""
    addr = (addr or "").strip().lower()
    cnt = store.person_msg_count(addr) if addr else 0
    if not cnt:
        # 배치 시절엔 후보가 rank_people 에서만 왔지만 이제 임의 주소가 들어올
        # 수 있다. 교신이 없는 주소에 basis 행을 만들면 people_dossier(재수집으로
        # 복구 안 되는 표)에 쓰레기가 쌓인다 — 재료도 없으니 그냥 돌려보낸다.
        return DossierResult("no_material")
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return DossierResult("no_backend")
    row = store.people_dossier(addr, include_stale=True)
    current = bool(row and row["validator_version"] == DOSSIER_VALIDATOR_VERSION)
    prev = row["dossier_md"] if current else ""   # 구버전 내용은 재료로 안 쓴다
    who = name or store.person_name(addr) or addr
    result = _gen_dossier(store, cfg, cmd, addr, who, prev, on_event, cancel)
    if result.status == "ok":
        store.save_people_dossier(addr, result.md, cnt, DOSSIER_VALIDATOR_VERSION)
    elif result.status in ("empty", "no_material"):
        # 모델 호출 성공 후 전부 검증 탈락했거나 인용 재료가 없으면 같은 입력은
        # 처리 완료. 현재 버전의 기존 카드는 보존하고 구버전 내용은 비운다.
        store.mark_people_dossier_checked(addr, cnt, DOSSIER_VALIDATOR_VERSION)
    return result
