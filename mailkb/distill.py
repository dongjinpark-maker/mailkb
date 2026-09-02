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

# 재료 예산 — **건수가 아니라 자수로 자른다**(2026-08-25). 종전의 상한 40은
# 배치 시절의 잔재였고, 실측에서 두 가지가 드러났다.
#  ① 하루 100통이면 업무 스레드가 63개다. 상한 40에서 23개가 말없이 빠지는데,
#     재료 자체는 42KB뿐이라 잘라야 할 이유가 없었다(예산 압박 0).
#  ② 반대로 창이 3일이면 재료가 122KB까지 가고, 그 콜은 기본 타임아웃(300초)
#     경계에 걸터앉는다 — 관측 시도 7회 중 5회 초과, 재시도 3회가 모두 실패해
#     수확이 통째로 사라진 실행이 2회 중 1회였다. 건수 상한은 이걸 못 막는다.
# 값의 근거 — 3일 창 무거운 코퍼스에서 예산만 바꿔 단독 실측(2026-08-25):
#   47,206자 → 248초 · 62,406자 → 415초 · 77,604자 → 508초 (출력은 셋 다 7.3K자)
# **출력량이 같은데 시간이 입력에 비례한다.** 주간 보고에서 얻은 "시간 = 출력량 ÷
# 생성속도"가 여기선 안 통한다 — 수확은 모든 메일을 훑어 추출하는 일이라 읽는
# 시간이 지배한다. 그래서 예산은 출력이 아니라 입력으로 잡아야 한다.
# 45,000 을 고른 이유: 300초 기본 타임아웃 안에 드는 가장 큰 측정점이다. 하루
# 100통 · 통당 1,000자가 약 46KB 이므로 바쁜 하루가 한두 콜에 들어간다.
HARVEST_BUDGET = 45_000

# 수확 콜은 기본 타임아웃(300초)·재시도(2회)를 쓰지 않는다.
#
# 같은 크기 재료의 실측이 145 · 248 · 415 · 508 · ~500초로 흩어진다 — **변동이
# 3배 이상이고 크기로 설명되지 않는다.** 여기서 짧은 타임아웃 + 재시도는 손해다:
# 46,336자 실행이 600초에 걸려 죽고 재시도가 501초에 성공해 **총 1,103초**가
# 걸렸다. 600초를 통째로 버린 것이다. 이 콜의 타임아웃은 '고장'이 아니라 '느림'을
# 뜻하므로, 같은 프롬프트를 다시 던져도 빨라질 이유가 없다.
#
# 그래서 한 번에 넉넉히 기다리고(900초) 재시도는 1회만 둔다(진짜 일시적 실패용).
# 타임아웃이 나도 잃는 것은 '오늘 회고의 수확 절'뿐이다 — 워터마크는 성공했을
# 때만 전진하므로 **다음 실행이 같은 재료를 그대로 이어받는다**(영구 손실 아님).
HARVEST_TIMEOUT = 900
HARVEST_RETRIES = 1

# 플래그(🚩) 스레드 몫 — 시간 앞머리 **밖에** 있는 플래그 메일을 이만큼 더 싣는다.
# 시간 절단은 "T 이전 전부"라 정직하지만, 예산이 무는 날엔 사용자가 중요 표시한
# 스레드의 오후 메일이 다음 실행으로 밀린다. 그 스레드는 다음 날 늦게 보라고
# 표시한 것이 아니다. 앞머리를 먼저 채우고 남은 자리에 플래그만 얹으므로
# 워터마크는 그대로 앞머리 끝이다 — 얹힌 메일은 다음 실행이 다시 싣지만
# (중복 과금), 신호는 인용으로 중복이 막히고(store.add_signal) 암묵지는 제목으로
# 막힌다. 플래그가 없거나 다 앞머리 안에 있으면 이 값은 아무 일도 하지 않는다.
HARVEST_FLAG_EXTRA = 9_000
_CAP_BODY = 1000       # 메시지 본문 상한 (자)
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
    # 예산에 밀려 두고 온 날이 있으면 창이 그 앞으로 닫히지 않는다(2026-08-25).
    # 워터마크를 정직하게 만들어도 창이 앞질러 가면 같은 자리에서 다시 잃는다 —
    # 미룬 것은 '아직 안 본 것'이지 '건너뛴 날'이 아니다. 빚이 없으면 이 값은
    # 비어 있고 종전과 바이트 단위로 같게 동작한다.
    owed = store.get_state("harvest_owed_from") or ""
    if owed and owed < floor:
        floor = owed
    return floor, last_ts


def _harvest_items(store: Store, cfg: Config, start_day: str, end_day: str,
                   last_ts: str) -> tuple:
    """(블록, 워터마크, 미룬 메일 수) — **시간을 잘라** 예산만큼 싣는다.

    창 안 메시지 중 last_ts 이후 것만 싣는다(재실행 시 중복 과금 방지).

    ── 왜 시간으로 자르나 (2026-08-25) ─────────────────────────────────────
    종전에는 플래그·최근활동순으로 **스레드를** 고르고 건수 상한(40)에서 잘랐고,
    워터마크는 실은 것의 최대 시각으로 전진했다. 잘린 스레드의 메일은 전부 그보다
    과거라 다음 실행의 `sent_on > last_ts` 필터에서 통째로 사라졌다 — **영구
    누락이고 재실행해도 복구되지 않았다.** 결정론 재현: 후보 16 · 상한 8 이면
    8개 스레드 48통이 증발했고, 하루 100통이면 63개 중 23개다.

    고치는 길로 두 가지를 먼저 시도했고 둘 다 시뮬레이션이 반증했다.
      ① 워터마크 클램프 + 밀린 스레드 우선: 밀린 목록이 '이번에 안 실린 전부'라
         이미 실은 것까지 되돌아와 두 집합이 번갈아 실리며 진동했다(7회에 5/16).
      ② 스레드를 시간순으로 담기: 스레드끼리 시간이 겹쳐, 미룬 스레드의 첫 메일이
         늘 이른 시각이라 워터마크가 18분씩만 나아갔다 — 300통 커버에 860통을
         다시 읽었다.
    원인은 같다. **스레드 단위로 자르면서 시간 하나로 진도를 적으려 한 것**이다.

    그래서 자르는 축을 시간으로 바꾼다. 창 안 메일을 시각순으로 늘어놓고 예산까지
    담으면, 실은 것은 "T 이전 전부"이고 워터마크 = T 가 정확하다. 남은 것은 손대지
    않은 채 다음 실행이 T 부터 이어받는다. 스레드는 담긴 메일만으로 묶어 보여준다
    — 한 스레드가 반만 실릴 수 있지만, 그건 창이 원래 하던 일과 같다.

    스레드 안에서 최근 N통만 남기던 규칙(옛 per_cap)과 건수 상한(옛 _CAP_THREADS)
    은 뺐다. 둘 다 오래된 쪽을 말없이 버리는 통로였고, 총량은 예산이 잡는다.
    플래그(🚩)는 몫으로 남긴다 — 앞머리를 채운 뒤 남은 자리(HARVEST_FLAG_EXTRA)에
    플래그 스레드의 앞머리 밖 메일을 얹는다. 워터마크는 앞머리 끝 그대로라
    진도가 안 흔들리고, 얹은 메일은 다음 실행이 다시 싣는다(중복 과금) —
    그 중복이 기록을 겹치지 않게 store.add_signal 이 (날짜·축·대상·스레드·인용)
    을 열쇠로 거른다.
    """
    rows, subject_of, flagged = [], {}, set()   # flagged: 표시된 메일이 있는 스레드
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
        subject_of[tid] = msgs[0]["subject"]
        # 플래그는 **메일**에 붙는다(2026-09-02). 스레드 집합은 정렬용으로만 쓴다 —
        # 표시된 메일이 든 스레드를 프롬프트 앞으로 보내되, 곁다리로 얹는 것은
        # 그 스레드 전부가 아니라 표시된 메일뿐이다(종전에는 14통짜리에서 한 통을
        # 표시하면 14통이 다 실렸다).
        if any(m["flagged"] for m in msgs):
            flagged.add(tid)
        for m in msgs:
            when = m["sent_on"] or ""
            if start_day <= when[:10] <= end_day and (not last_ts or when > last_ts):
                rows.append((when, tid, m))
    if not rows:
        return "", "", 0
    rows.sort(key=lambda r: (r[0], r[1]))

    take, used = [], 0
    for r in rows:
        size = len(r[2]["new_content"] or "")
        if take and used + size > HARVEST_BUDGET:
            break
        take.append(r)
        used += size
    # 같은 시각의 메일은 함께 싣는다 — 워터마크가 T 인데 T 짜리 메일이 남아 있으면
    # 그것이 곧 누락이다(예산을 조금 넘겨도 잃는 것보다 낫다).
    mark = take[-1][0]
    while len(take) < len(rows) and rows[len(take)][0] == mark:
        take.append(rows[len(take)])
    rest = rows[len(take):]

    # 앞머리 **밖의 플래그 메일**을 남은 몫만큼 얹는다. 워터마크는 아래에서
    # 앞머리 끝으로 잡히므로(rest 의 최소 시각 앞), 얹은 것이 진도를 앞지르지
    # 않는다 — 다음 실행이 다시 싣지만 잃지는 않는다.
    extra, spent = [], 0
    for r in rest:
        if not r[2]["flagged"]:          # 표시된 **그 메일**만 얹는다
            continue
        size = len(r[2]["new_content"] or "")
        if spent + size > HARVEST_FLAG_EXTRA:
            break
        extra.append(r)
        spent += size

    # 스레드로 묶어 보여준다 — **플래그가 앞**, 그 안에서 첫 실린 메일 시각순.
    by_tid: dict = {}
    for when, tid, m in take + extra:
        by_tid.setdefault(tid, []).append(m)
    for msgs in by_tid.values():
        msgs.sort(key=lambda m: m["sent_on"] or "")
    blocks = []
    for tid, msgs in sorted(by_tid.items(),
                            key=lambda kv: (kv[0] not in flagged,
                                            kv[1][0]["sent_on"])):
        # **진단(파생물)을 재료로 넣지 않는다**(2026-08-16). 어제는 인용 꼬리만
        # 뺐는데, 서술도 그 스레드의 AI 산출이라 같은 고리다 — 수확이 그것을
        # 근거 삼으면 지난주 판단이 오늘 수확으로 되돌아온다. 주간 보고가
        # 이미 지키는 규칙("사실·상태·선별을 전부 원문에서 다시 한다")을
        # 수확에도 적용한다. 그 자리는 아래 창(원문)이 메운다.
        body = ""
        for m in msgs:
            who = "나" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
            when = m["sent_on"][5:10] + " " + m["sent_on"][11:16]
            # 🚩 는 스레드 머리가 아니라 **그 메일 줄**에 붙는다 — 스레드에 붙이면
            # 어느 통이 중요한지 모델도 모른다.
            body += (f"\n  ({when} {who}){' 🚩' if m['flagged'] else ''} "
                     + smart_truncate((m["new_content"] or "").strip(), _CAP_BODY))
        blocks.append(f"[#{tid}] " + subject_of.get(tid, "") + body)
    # 워터마크는 **앞머리 끝**이다 — 얹은 플래그 메일은 rest 를 앞지르지 않는다.
    mark = take[-1][0]
    if rest:
        safe = [r[0] for r in take + extra if r[0] < rest[0][0]]
        mark = max(safe) if safe else mark
    # 미룬 통수는 '아직 안 본 것' — 이번에 얹은 플래그 메일은 다음에 다시 오므로
    # 여기서 빼지 않는다(진도는 워터마크가 말한다).
    return "\n".join(blocks), mark, len(rest)


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

    반환: {"delta": [...], "person", "project", "knowledge", "dropped",
    "deferred"(예산에 밀려 다음 실행으로 넘긴 스레드 수)} —
    재료 없음(새 메일 없음 포함)/백엔드 미설정/호출 실패면 None
    (graceful, 데일리는 결정론 섹션만으로 살아남는다).
    """
    date_iso = det.get("date") or date.today().isoformat()
    try:
        cmd = cfg.ai_cmd(backend)
    except SystemExit:
        return None
    start_day, last_ts = _harvest_window(store, cfg, date_iso)
    items, max_ts, deferred = _harvest_items(store, cfg, start_day,
                                             date_iso, last_ts)
    if not items:
        return None
    rules = cfg.ai_rules_text()
    rules_block = f"[사용자 지침 — 우선 적용]\n{rules}\n\n" if rules else ""
    period = date_iso if start_day >= date_iso else f"{start_day} ~ {date_iso}"
    prompt = HARVEST.format(date=period, rules=rules_block,
                            yesterday=_recent_delta(cfg, date_iso),
                            items=items)
    try:
        raw = review.ai_run(cmd, prompt, timeout=HARVEST_TIMEOUT,
                            retries=HARVEST_RETRIES,
                            on_event=on_event, cancel=cancel)
    except review.AIError as e:
        review._notify_error(on_error, e)   # 삼키는 자리가 곧 보고 자리
        return None
    # 수확 성공 → 워터마크 전진(앞으로만 — 백필은 max() 가드가 자연 처리).
    # 저장 건수와 무관: 모델이 본 메일은 재실행 때 다시 과금하지 않는다.
    # max_ts 는 이미 잘린 메일 앞에서 멈춘 값이다(_harvest_items 참고) —
    # 빈 문자열이면 전진하지 않는다.
    if max_ts:
        cur = store.get_state("last_harvest")
        store.set_state("last_harvest", max(cur, max_ts) if cur else max_ts)
    # 남은 빚 — 있으면 다음 실행의 창이 여기까지 열린다(_harvest_window).
    store.set_state("harvest_owed_from", start_day if deferred else "")


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
              "knowledge": knowledge_saved, "dropped": dropped,
              "deferred": deferred}
    if cfg.opt("ai", "harvest_log", default=True):
        _log_harvest(cfg, {
            "date": date_iso, "backend": backend, "raw": raw[:8000],
            "n_person": len(person_saved), "n_project": len(project_saved),
            "n_knowledge": len(knowledge_saved),
            # 예산에 밀려 이번에 못 실은 스레드 수 — 로그만으로 "왜 이 스레드가
            # 수확에 없나"에 답할 수 있어야 한다. 종전엔 이 숫자가 없어서
            # 절단이 조용했다.
            "n_deferred": deferred,
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
