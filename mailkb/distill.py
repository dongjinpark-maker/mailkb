"""지식 증류 계층 — Phase 1: 데일리 '수확(harvest)'과 결정 원장 적재.

설계(docs/PROPOSAL-distill.md): 데일리 AI 의 임무는 통찰 생산이 아니라 **수확** —
오늘 메일에서 '축적할 사실'(결정 후보·인물/프로젝트 신호)을 구조화 추출해
원장(SQLite)에 쌓는다. 통찰(추세·리스크)은 Phase 2 주간 Opus 증류가 맡는다.

환각 가드: 모든 추출 항목에 원문 인용(quote)을 강제하고, 해당 스레드
new_content 에 부분일치(공백 무시)하지 않으면 그 항목을 버린다.
반영은 사람(웹 '기억 › 장기기억' 반영 대기 큐) — 여기서는 candidate 로만 적재.

AI 호출은 review.ai_run 재사용(테스트 mock 경로 통일을 위해 review 모듈 참조).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from . import review
from .config import Config
from .store import Store

# ------------------------------------------------------------------ 프롬프트

# 수확 — 창의적 해석 금지, 명시된 사실만. 인용은 검증되므로 의역하면 버려진다.
HARVEST = """당신은 업무 메일에서 '축적할 사실'을 수확하는 추출기다. 아래는 {date} 에 활동한 업무 스레드들의 새 메일이다. 창의적 해석·추측 없이, 본문에 명시된 것만 추출하라.

출력 형식 (마크다운, 섹션 4개 고정 — 해당 없으면 그 섹션에 "- 없음" 한 줄):
## 오늘 델타
- 직전 수확 이후 달라진 것만 3~6줄, 각 줄 끝에 #스레드번호. 직전 델타에 이미 있는 내용 반복 금지.
## 결정 후보
- 결정: <장기기억에 남길 자기완결 한 문장 — 무엇을 어떻게 하기로 했는지, 몇 달 뒤 맥락 없이 읽어도 이해되게> | 근거: <왜> | 결정자: <이름> | #<스레드번호> | 인용: "<결정이 명시된 원문 문장 그대로>"
## 인물 신호
- <이름> | <역할·담당·상태에 관한 새 사실 한 줄> | #<스레드번호> | 인용: "<원문 문장 그대로>"
## 프로젝트 신호
- #<스레드번호> | <사안 상태 변화: 이전 → 이후> | 인용: "<원문 문장 그대로>"

규칙:
- '결정'은 이미 확정 선언된 것만 (제안·논의 중·요청·예정은 결정이 아님).
- 인용은 반드시 원문에 있는 문장을 글자 그대로 옮겨라 (요약·의역 금지). 인용할 문장이 없으면 그 항목을 만들지 마라.
- 없는 스레드 번호를 만들지 마라. 억지로 채우지 마라 — 없으면 "- 없음".

{rules}[직전 델타 — 반복 금지]
{yesterday}

[스레드]
{items}
"""

# 수확 로그(harvest.jsonl) 품질 감사 지시문 — 첫 기록 시 <home>/logs/ 에 저장.
HARVEST_LOG_ANALYSIS = """# 데일리 수확 로그 분석 지시문

너는 mailkb 의 데일리 '수확'(결정 후보·신호 추출) 품질을 감사하는 검토자다.
같은 폴더의 `harvest.jsonl` 이 분석 대상이다.

## 데이터 형식
JSONL — 한 줄 = 하루치 수확 1회. 각 줄: date, backend, raw(모델 원문 출력),
saved_decisions[](적재된 후보: thread_id/title/decider), n_person, n_project,
dropped(인용 검증 실패로 버린 항목 수).

## 할 일
1. raw 를 읽고 형식 이탈(섹션 누락·라벨 불일치·의역 인용)을 지적하라.
2. dropped 가 많은 날의 raw 에서 왜 인용 검증에 실패했는지 패턴을 찾아라
   (의역, 여러 문장 합침, 말줄임 등).
3. saved_decisions 중 '결정이 아닌 것'(제안/요청/예정)이 섞였는지 판정하라.
4. HARVEST 프롬프트에 보탤 규칙 1~3줄과 few-shot 예시(실제 오추출 축약)를 제안하라.

## 출력 (형식 고정)
### 요약
- 총 실행 N회 · 적재 후보 X건 · 신호 Y건 · 드롭 Z건 · 형식이탈 W회
### 오추출/드롭 패턴
- 패턴명: 설명 + 해당 날짜
### 프롬프트 개선 제안
- ...
"""


# ------------------------------------------------------------------ 파서·검증

_TID_RX = re.compile(r"#(\d+)")
_QUOTE_RX = re.compile(r"인용\s*[:：]\s*[\"“]?(.*?)[\"”]?\s*$")
_DEC_TITLE_RX = re.compile(r"결정\s*[:：]\s*([^|]+)")
_DEC_WHY_RX = re.compile(r"근거\s*[:：]\s*([^|]+)")
_DEC_WHO_RX = re.compile(r"결정자\s*[:：]\s*([^|]+)")
_SEC_RX = re.compile(r"^##\s*(.+?)\s*$", re.M)
_DELTA_SEC_RX = re.compile(r"## 오늘 델타\s*\n(.*?)(?=\n## |\Z)", re.S)

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
    """스레드별 본문(공백 제거) 캐시 — 인용이 실제 원문에 있는지 검증."""

    def __init__(self, store: Store):
        self.store = store
        self.cache: dict[int, str] = {}

    def ok(self, tid: int, quote: str) -> bool:
        q = _norm_ws(quote)
        if not (self._QMIN <= len(q) <= self._QMAX):
            return False
        if tid not in self.cache:
            msgs = self.store.thread_messages(tid)
            if not msgs:
                self.cache[tid] = ""
            else:
                self.cache[tid] = _norm_ws(
                    "".join(m["new_content"] or "" for m in msgs))
        return q in self.cache[tid]

    _QMIN = _QUOTE_MIN
    _QMAX = 10_000   # 정규화 후 상한(사실상 무제한 — 원문 길이는 _QUOTE_MAX 로 제한)


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

    decisions = []
    for ln in sec.get("결정 후보", []):
        tid, quote = _parse_line_common(ln)
        tm = _DEC_TITLE_RX.search(ln)
        if tid is None or not tm:
            continue
        why = _DEC_WHY_RX.search(ln)
        who = _DEC_WHO_RX.search(ln)
        decisions.append({
            "thread_id": tid,
            "title": tm.group(1).strip()[:200],
            "rationale": (why.group(1).strip() if why else "")[:300],
            "decider": (who.group(1).strip() if who else "")[:80],
            "quote": quote,
        })

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

    return {"delta": delta, "decisions": decisions,
            "person": person, "project": project}


# ------------------------------------------------------------------ 재료 조립

_CAP_THREADS = 40      # 수확 대상 업무 스레드 상한
_CAP_MSGS = 3          # 스레드당 하루치 메시지 상한 (창이 길면 일수만큼 늘림, 최대 8)
_CAP_BODY = 1000       # 메시지 본문 상한 (자)
_CAP_SUMM = 300        # 롤링 요약 상한 (자)


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
    for tid in store.threads_active_between(start_day, end_day):
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
        summ = " ".join(summ.split())[:_CAP_SUMM]
        head = f"[#{tid}] {subject}"
        if summ:
            head += f"\n  (요약: {summ})"
        body = ""
        for m in win:
            who = "나" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
            when = m["sent_on"][5:10] + " " + m["sent_on"][11:16]
            body += (f"\n  ({when} {who}) "
                     + (m["new_content"] or "").strip()[:_CAP_BODY])
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
            backend: str | None = None) -> dict | None:
    """데일리 수확 — 지난 수확 이후(최대 3일 소급) 업무 스레드에서 결정 후보·신호
    를 추출해 원장 적재. 하루 이틀 건너뛰어도 다음 실행이 창으로 소급한다.

    반환: {"delta": [...], "decisions": [적재된 후보], "person", "project",
    "dropped"} — 재료 없음(새 메일 없음 포함)/백엔드 미설정/호출 실패면 None
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
        raw = review.ai_run(cmd, prompt)
    except review.AIError:
        return None
    # 수확 성공 → 워터마크 전진(앞으로만 — 백필은 max() 가드가 자연 처리).
    # 저장 건수와 무관: 모델이 본 메일은 재실행 때 다시 과금하지 않는다.
    if max_ts:
        cur = store.get_state("last_harvest")
        store.set_state("last_harvest", max(cur, max_ts) if cur else max_ts)

    parsed = parse_harvest(raw)
    checker = _QuoteChecker(store)
    dropped = 0
    saved_dec = []
    for d in parsed["decisions"]:
        if not checker.ok(d["thread_id"], d["quote"]):
            dropped += 1
            continue
        did = store.add_decision(
            d["thread_id"], date_iso, d["title"], rationale=d["rationale"],
            decider=d["decider"], quote=d["quote"],
            status="candidate", source="daily")
        if did:                            # None = 중복(이미 원장에 있음)
            saved_dec.append({**d, "id": did})
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

    result = {"delta": parsed["delta"], "decisions": saved_dec,
              "person": person_saved, "project": project_saved,
              "dropped": dropped}
    if cfg.opt("ai", "harvest_log", default=True):
        _log_harvest(cfg, {
            "date": date_iso, "backend": backend, "raw": raw[:8000],
            "saved_decisions": [{"thread_id": x["thread_id"], "title": x["title"],
                                 "decider": x["decider"]} for x in saved_dec],
            "n_person": len(person_saved), "n_project": len(project_saved),
            "dropped": dropped,
        })
    return result
