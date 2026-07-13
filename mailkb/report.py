"""통계 분석 — 시간축 신호 대시보드 (웹 /stats, 전폭 단일 페이지).

mailkb-lab report.py 이식(2026-07-10). 라이브 DB 를 조회 전용으로 읽어
선택 기간(2/4/8/16주) 안의 신호를 계산해 자족형 HTML 로 렌더한다.
외부 리소스(폰트/CDN) 요청 없음 — 사내망에서 그대로 열림.

신호:
  §1 증발한 내 요청   — 내가 물었는데 답 없이 7일+ 지난 것 (누적)
  §2 조용해진 사람    — 평소 교신량 대비 최근 2주 급감/무소식
  §3 내 응답 지연 추세 — 주별 내 응답 중앙값 시계열
  §4 야간·주말 발신    — 업무시간 외 메일 활동 비율 추세
  §5 자주 주고받는 상대 — 나 중심 방향 그래프 (기간 연동)

JS 는 인라인이 아니라 /report.js 로 서빙된다(웹 CSP: script-src 'self').
"""

from __future__ import annotations

import html
import json
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .clean import strip_preserved

# ------------------------------------------------------------------ 설정

# 검토 기간 선택지 (주) — 모든 섹션(관계도 포함)이 이 기간을 따른다
PERIODS = (2, 4, 8, 16)
DEFAULT_WEEKS = 4

# 요청 신호 — 좁게 잡는다: "확인했습니다"/"잘 부탁드립니다" 같은 종결 인사 오탐 방지.
# (review._REQUEST_RX 와 별개 정의 — 저쪽은 개입 큐용으로 더 넓다. 합치지 말 것)
REQUEST_RX = re.compile(
    r"[?？]|(?<!잘\s)(?<!잘)부탁드|요청드|바랍니다|주시기|회신\s*부탁")

NIGHT_FROM, NIGHT_TO = 20, 7   # 야간 기준 (20시~익일 7시)
EVAPORATE_MIN_DAYS = 7         # §1: 최소 경과일
QUIET_BASE_MIN = 3             # §2: 기준선 최소 통수
QUIET_DROP_RATIO = 0.3         # §2: 최근 비율이 기준선의 30% 이하면 신호


def clamp_weeks(raw) -> int:
    """기간 파라미터 검증 — 허용 목록 밖이면 기본값."""
    try:
        w = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WEEKS
    return w if w in PERIODS else DEFAULT_WEEKS


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fmt_h(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f}일"
    return f"{hours:.0f}h" if hours >= 10 else f"{hours:.1f}h"


def _wcw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


# ------------------------------------------------------------------ 데이터 적재

def load(db, max_weeks: int, extra_me=frozenset()) -> dict | None:
    """라이브 DB(조회 전용 SELECT 만)에서 기간 내 신호 계산용 데이터 적재.

    메일이 없으면 None (웹은 빈 상태 페이지로 렌더).
    """
    all_msgs = [dict(r) for r in db.execute(
        """SELECT id, thread_id, sender_addr, sender_name, to_addrs, subject,
                  sent_on, is_sent, new_content
           FROM messages WHERE sent_on != '' ORDER BY sent_on, id"""
    )]
    if not all_msgs:
        return None

    # 내 주소 집합: is_sent=1 발신자 + 설정 주소(별칭 포함). 별칭 발신 메일이
    # is_sent=0 으로 들어와 있으면 발신으로 재분류 (§2~§5 오염 방지).
    # 창 밖 메일까지 포함해 파악 — 별칭 지식은 기간과 무관하게 온전해야 함.
    my_addrs = {(m["sender_addr"] or "").lower() for m in all_msgs if m["is_sent"]}
    my_addrs |= {a.lower() for a in extra_me if a}
    my_addrs.discard("")
    for m in all_msgs:
        if not m["is_sent"] and (m["sender_addr"] or "").lower() in my_addrs:
            m["is_sent"] = 1
    # 숨긴 스레드는 §1(증발한 요청)에서 제외 — 사용자가 신호를 끈 건.
    # (구 추적제외 폐지로 기준을 dismissed → hidden 으로 교체, 2026-07-12)
    hidden_ids = {r["id"] for r in
                  db.execute("SELECT id FROM threads WHERE hidden=1")}
    names = {r["addr"].lower(): r["name"] for r in
             db.execute("SELECT addr, name FROM people") if r["name"]}

    asof = _dt(all_msgs[-1]["sent_on"])
    data_first = _dt(all_msgs[0]["sent_on"])
    # 주 축: 데이터 시작 주 ~ asof 주, 최대 max_weeks — 검토 기간은 항상 제한됨
    last_ws = _week_start(asof.date())
    n_weeks = min(max_weeks,
                  (last_ws - _week_start(data_first.date())).days // 7 + 1)
    weeks = [last_ws - timedelta(weeks=n_weeks - 1 - i) for i in range(n_weeks)]
    widx = {w: i for i, w in enumerate(weeks)}
    window_start = weeks[0]      # 검토 기간 시작 주(월요일)

    # ★ 검토 기간 제한: 선택한 창(window_start~asof) 밖의 메일은 분석 대상에서
    #   제외한다. "모든 mail 이 아니라 제한된 기간" — 이게 빠지면 기간을 눌러도
    #   §1(증발) 등 창 무관 섹션이 그대로라 기간 전환이 데이터에 안 먹힌다.
    msgs = [m for m in all_msgs if _dt(m["sent_on"]).date() >= window_start]
    first = _dt(msgs[0]["sent_on"]) if msgs else asof

    def wk(dt: datetime) -> int | None:
        return widx.get(_week_start(dt.date()))

    threads: dict[int, list] = defaultdict(list)
    for m in msgs:
        threads[m["thread_id"]].append(m)

    # 내가 보낸 적 있는 주소 = 상호 교신자 (noreply/봇 자동 배제, 내 주소 제외)
    mutual: set[str] = set()
    for m in msgs:
        if m["is_sent"]:
            for a in (m["to_addrs"] or "").split(";"):
                a = a.strip().lower()
                if a and a not in my_addrs:
                    mutual.add(a)

    return {
        "msgs": msgs, "threads": threads, "hidden": hidden_ids,
        "names": names, "mutual": mutual, "my_addrs": my_addrs,
        "asof": asof, "first": first,
        "weeks": weeks, "wk": wk, "n_weeks": n_weeks,
    }


# ------------------------------------------------------------------ 신호 계산

def sig_evaporated(d: dict) -> list[dict]:
    """§1 스레드별 '마지막 발신이 요청인데 이후 수신 없음' + 7일 경과."""
    out = []
    for tid, ms in d["threads"].items():
        if tid in d["hidden"]:
            continue
        last_req = None
        for m in ms:
            if m["is_sent"]:
                # 보존 인용(mid-join FW 등) 속 과거 요청은 '내 요청'이 아님
                body = strip_preserved(m["new_content"] or "") + " " + (m["subject"] or "")
                if REQUEST_RX.search(body) and (m["to_addrs"] or "").strip():
                    last_req = m
            elif last_req is not None and m["sent_on"] > last_req["sent_on"]:
                last_req = None  # 답이 왔음
        if last_req is None:
            continue
        days = (d["asof"] - _dt(last_req["sent_on"])).days
        if days < EVAPORATE_MIN_DAYS:
            continue
        to = [a.strip().lower() for a in last_req["to_addrs"].split(";") if a.strip()]
        who = d["names"].get(to[0], to[0]) if to else "?"
        if len(to) > 1:
            who += f" 외 {len(to) - 1}"
        out.append({
            "thread_id": tid, "subject": last_req["subject"] or "(제목 없음)",
            "who": who, "sent": _dt(last_req["sent_on"]).strftime("%m/%d"),
            "days": days,
            "sev": ("critical" if days >= 21 else
                    "serious" if days >= 14 else "warning"),
        })
    out.sort(key=lambda x: -x["days"])
    return out


def sig_quiet(d: dict) -> list[dict]:
    """§2 상호 교신자 중 최근 2주 수신이 기준선 대비 급감한 사람."""
    if d["n_weeks"] < 3:
        return []
    per: dict[str, list[int]] = defaultdict(lambda: [0] * d["n_weeks"])
    disp: dict[str, str] = {}
    for m in d["msgs"]:
        if m["is_sent"]:
            continue
        a = (m["sender_addr"] or "").lower()
        if a not in d["mutual"]:
            continue
        i = d["wk"](_dt(m["sent_on"]))
        if i is not None:
            per[a][i] += 1
            disp[a] = m["sender_name"] or a
    out = []
    for a, series in per.items():
        base, recent = series[:-2], series[-2:]
        # 기준선: 그 사람의 첫 수신 주부터 계산 (신규 인물 오탐 방지)
        try:
            start = next(i for i, v in enumerate(base) if v)
        except StopIteration:
            continue
        eff = base[start:]
        if sum(eff) < QUIET_BASE_MIN:
            continue
        rate = sum(eff) / len(eff)                 # 주당 통수
        if sum(recent) <= rate * 2 * QUIET_DROP_RATIO:
            out.append({
                "addr": a, "name": disp[a], "series": series,
                "rate": rate, "recent": sum(recent),
                "base_weeks": len(eff),
            })
    out.sort(key=lambda x: -x["rate"])
    return out


def _reply_pairs(d: dict) -> list[tuple]:
    """(수신자 addr, 수신자 name, 응답 지연 h, 내 답장 주 idx) 목록."""
    pairs = []
    for ms in d["threads"].values():
        pending = []
        for m in ms:
            if m["is_sent"]:
                for p in pending:
                    delay = (_dt(m["sent_on"]) - _dt(p["sent_on"])).total_seconds() / 3600
                    i = d["wk"](_dt(m["sent_on"]))
                    if i is not None and 0 < delay < 24 * 30:
                        pairs.append(((p["sender_addr"] or "").lower(),
                                      p["sender_name"] or p["sender_addr"],
                                      delay, i))
                pending = []
            else:
                pending.append(m)
    return pairs


def sig_latency(d: dict, pairs: list) -> dict:
    """§3 주별 내 응답 중앙값 시계열 + 최근 2주 vs 이전 4주 델타."""
    by_week: dict[int, list[float]] = defaultdict(list)
    for _, _, delay, i in pairs:
        by_week[i].append(delay)
    series = [round(statistics.median(by_week[i]), 1) if i in by_week else None
              for i in range(d["n_weeks"])]
    recent = [x for i in range(max(0, d["n_weeks"] - 2), d["n_weeks"])
              for x in by_week.get(i, [])]
    prior = [x for i in range(max(0, d["n_weeks"] - 6), d["n_weeks"] - 2)
             for x in by_week.get(i, [])]
    return {
        "series": series,
        "overall": statistics.median([p[2] for p in pairs]) if pairs else None,
        "recent": statistics.median(recent) if recent else None,
        "prior": statistics.median(prior) if prior else None,
        "n": len(pairs),
    }


def sig_volume(d: dict, days: int) -> dict:
    """§5 최근 N일 발신 상대 / 수신 발신자 상위 — N 은 선택 기간을 따름."""
    cutoff = d["asof"] - timedelta(days=days)
    sent_c: Counter = Counter()
    recv_c: Counter = Counter()
    disp: dict[str, str] = {}
    for m in d["msgs"]:
        if _dt(m["sent_on"]) < cutoff:
            continue
        if m["is_sent"]:
            for a in (m["to_addrs"] or "").split(";"):
                a = a.strip().lower()
                if a and a not in d["my_addrs"]:   # 자기 자신(별칭 포함) 제외
                    sent_c[a] += 1
        else:
            a = (m["sender_addr"] or "").lower()
            if a and a not in d["my_addrs"]:
                recv_c[a] += 1
                if m["sender_name"]:
                    disp[a] = m["sender_name"]

    def nm(a: str) -> str:
        return d["names"].get(a) or disp.get(a) or a

    top = lambda c: [{"addr": a, "name": nm(a), "n": n}
                     for a, n in c.most_common(8)]
    union = sorted({*sent_c, *recv_c},
                   key=lambda a: -(sent_c[a] + recv_c[a]))
    rows = [{"name": nm(a), "addr": a, "sent": sent_c[a], "recv": recv_c[a]}
            for a in union]
    return {"sent": top(sent_c), "recv": top(recv_c), "rows": rows,
            "days": days}


def sig_offhours(d: dict) -> dict:
    """§4 주별 야간·주말 발신 비율."""
    tot = [0] * d["n_weeks"]
    off = [0] * d["n_weeks"]
    for m in d["msgs"]:
        if not m["is_sent"]:
            continue
        dt = _dt(m["sent_on"])
        i = d["wk"](dt)
        if i is None:
            continue
        tot[i] += 1
        if dt.hour >= NIGHT_FROM or dt.hour < NIGHT_TO or dt.weekday() >= 5:
            off[i] += 1
    series = [round(off[i] / tot[i] * 100, 1) if tot[i] else None
              for i in range(d["n_weeks"])]
    vals = [v for v in series if v is not None]
    return {"series": series, "tot": tot, "off": off,
            "recent": series[-1] if series and series[-1] is not None else None,
            "prior": (sum(vals[:-1]) / len(vals[:-1]) if len(vals) > 1 else None)}


# ------------------------------------------------------------------ SVG 헬퍼

def _nice_ticks(vmax: float, n: int = 3) -> list[float]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    step = next((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), mag)
    top = step * n
    while top < vmax:
        top += step
    ticks, v = [], 0.0
    while v <= top + 1e-9:
        ticks.append(round(v, 2))
        v += step
    return ticks


def svg_line(series: list, labels: list[str], unit: str, chart_id: str,
             area: bool = False) -> str:
    """단일 시리즈 라인 차트 (크로스헤어 툴팁, 끝점 직접 라벨)."""
    W, H, PL, PR, PT, PB = 640, 210, 44, 56, 14, 26
    pw, ph = W - PL - PR, H - PT - PB
    n = len(series)
    vals = [v for v in series if v is not None]
    if not vals or n < 2:
        return '<p class="empty">데이터가 아직 부족합니다.</p>'
    ticks = _nice_ticks(max(vals))
    top = ticks[-1]
    def X(i): return PL + pw * i / (n - 1)
    def Y(v): return PT + ph * (1 - v / top)

    g = []
    for t in ticks:  # 가로 그리드 (헤어라인)
        g.append(f'<line x1="{PL}" y1="{Y(t):.1f}" x2="{PL + pw}" y2="{Y(t):.1f}" '
                 f'class="grid"/>')
        g.append(f'<text x="{PL - 8}" y="{Y(t) + 4:.1f}" class="tick" '
                 f'text-anchor="end">{t:g}</text>')
    step = max(1, (n + 7) // 8)  # x 라벨 성김
    for i in range(0, n, step):
        g.append(f'<text x="{X(i):.1f}" y="{H - 8}" class="tick" '
                 f'text-anchor="middle">{labels[i]}</text>')

    # 결측 주는 선을 끊어서 표현
    runs, cur = [], []
    for i, v in enumerate(series):
        if v is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append((i, v))
    if cur:
        runs.append(cur)
    for run in runs:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in run)
        if area and len(run) > 1:
            g.append(f'<polygon class="wash" points="{X(run[0][0]):.1f},{Y(0):.1f} '
                     f'{pts} {X(run[-1][0]):.1f},{Y(0):.1f}"/>')
        if len(run) == 1:
            i, v = run[0]
            g.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="4" class="dot"/>')
        else:
            g.append(f'<polyline class="line" points="{pts}"/>')
    # 끝점: 서피스 링 + 도트 + 직접 라벨 (선택적 라벨 — 끝점만)
    li, lv = next((i, v) for i, v in reversed(list(enumerate(series)))
                  if v is not None)
    g.append(f'<circle cx="{X(li):.1f}" cy="{Y(lv):.1f}" r="6" class="ring"/>')
    g.append(f'<circle cx="{X(li):.1f}" cy="{Y(lv):.1f}" r="4" class="dot"/>')
    g.append(f'<text x="{X(li) + 9:.1f}" y="{Y(lv) + 4:.1f}" class="endlabel">'
             f'{lv:g}{unit}</text>')
    g.append(f'<line id="{chart_id}-x" class="xhair" x1="0" y1="{PT}" x2="0" '
             f'y2="{PT + ph}" visibility="hidden"/>')

    payload = html.escape(json.dumps(
        {"labels": labels, "series": series, "unit": unit,
         "pl": PL, "pr": PR, "w": W}, ensure_ascii=False), quote=True)
    return (f'<svg class="linechart" id="{chart_id}" viewBox="0 0 {W} {H}" '
            f'role="img" tabindex="0" data-chart="{payload}">'
            f'<rect x="{PL}" y="{PT}" width="{pw}" height="{ph}" fill="transparent"/>'
            + "".join(g) + "</svg>")


def svg_count_bars(items: list[dict], alt: bool = False, tip_word: str = "발신") -> str:
    """§5 통수 가로 막대 — 단일 시리즈, 값은 막대 끝 직접 라벨."""
    if not items:
        return '<p class="empty">해당 기간 데이터가 없습니다.</p>'
    W, PL, PR, ROW, PT = 460, 128, 44, 30, 6
    H = PT + ROW * len(items) + 10
    vmax = max(x["n"] for x in items) * 1.06
    pw = W - PL - PR
    def X(v): return PL + pw * v / vmax

    g = [f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT + ROW * len(items)}" '
         f'class="baseline"/>']
    for k, it in enumerate(items):
        y = PT + ROW * k + (ROW - 18) / 2
        bw = max(X(it["n"]) - PL, 3)
        name = it["name"] if _wcw(it["name"]) <= 14 else it["name"][:7] + "…"
        g.append(f'<text x="{PL - 9}" y="{y + 13}" class="rowlabel" '
                 f'text-anchor="end">{html.escape(name)}</text>')
        g.append(f'<path class="bar{" alt" if alt else ""}" '
                 f'd="M{PL},{y} h{bw - 4:.1f} a4,4 0 0 1 4,4 v10 '
                 f'a4,4 0 0 1 -4,4 h-{bw - 4:.1f} z"/>')
        g.append(f'<text x="{X(it["n"]) + 7:.1f}" y="{y + 13}" '
                 f'class="vallabel">{it["n"]}</text>')
        g.append(f'<rect class="bar-hit" x="0" y="{PT + ROW * k}" width="{W}" '
                 f'height="{ROW}" tabindex="0" data-tip="'
                 + html.escape(f'{it["name"]} · {tip_word} {it["n"]}통',
                               quote=True) + '"/>')
    return (f'<svg class="barchart" viewBox="0 0 {W} {H}" role="img">'
            + "".join(g) + "</svg>")


def svg_ego_graph(vol: dict) -> str:
    """§5 방향 그래프 — 나(중심)와 상대들, 발신/수신 화살표 굵기 = 통수.

    상대 선정·통수는 vol["rows"](합집합의 실측 카운트)를 쓴다 — 상위 8
    발신/수신 목록(vol["sent"/"recv"])을 병합하면 목록 밖 방향이 0 으로
    보여 '주고받은 적이 있는데 선이 없는' 노드가 생겼다(2026-07-13).
    한 통이라도 있으면 가장 가는 선으로라도 연결하고, 선이 없는 방향은
    그 방향 교류가 정말 0 일 때뿐이다."""
    import math
    people = vol["rows"][:10]        # 이미 발신+수신 합계 내림차순
    if len(people) < 2:
        return ""
    W, H = 640, 520
    cx, cy, R = W / 2, H / 2, 190
    max_tot = max(p["sent"] + p["recv"] for p in people)
    max_edge = max(max(p["sent"], p["recv"]) for p in people)
    rc = 27  # 중심 노드

    defs = """<defs>
      <marker id="arr-out" markerUnits="strokeWidth" markerWidth="4.5" markerHeight="4"
        refX="3.4" refY="2" orient="auto"><path d="M0,0 L4.5,2 L0,4 z" fill="var(--mark)"/></marker>
      <marker id="arr-in" markerUnits="strokeWidth" markerWidth="4.5" markerHeight="4"
        refX="3.4" refY="2" orient="auto"><path d="M0,0 L4.5,2 L0,4 z" fill="var(--mark2)"/></marker>
    </defs>"""
    edges, nodes = [], []
    n = len(people)
    for k, p in enumerate(people):
        ang = -math.pi / 2 + 2 * math.pi * k / n
        ux, uy = math.cos(ang), math.sin(ang)
        px_, py_ = -uy, ux  # 수직 벡터 (양방향 곡선 분리용)
        nx, ny = cx + R * ux, cy + R * uy
        rn = 11 + 16 * math.sqrt((p["sent"] + p["recv"]) / max_tot)

        def arc(frm, to, gap_f, gap_t, side, width, cls, marker, tip):
            # 시작/끝을 노드 표면까지 당기고, side 방향으로 곡선을 띄운다
            fx = frm[0] + (ux if frm == (cx, cy) else -ux) * gap_f + px_ * side
            fy = frm[1] + (uy if frm == (cx, cy) else -uy) * gap_f + py_ * side
            tx = to[0] + (-ux if to != (cx, cy) else ux) * gap_t + px_ * side
            ty = to[1] + (-uy if to != (cx, cy) else uy) * gap_t + py_ * side
            mx = (fx + tx) / 2 + px_ * side * 2.2
            my = (fy + ty) / 2 + py_ * side * 2.2
            path = f"M{fx:.1f},{fy:.1f} Q{mx:.1f},{my:.1f} {tx:.1f},{ty:.1f}"
            e = (f'<path class="{cls}" d="{path}" stroke-width="{width:.1f}" '
                 f'marker-end="url(#{marker})"/>')
            hit = (f'<path class="edge-hit" d="{path}" '
                   f'stroke-width="{max(14, width + 10):.1f}" tabindex="0" '
                   f'data-tip="{html.escape(tip, quote=True)}"/>')
            return e + hit

        # 1통 = 최소 1px 에서 시작해 통수(√)에 비례 — 0통만 선 없음
        if p["sent"]:
            w = 1.0 + 7.0 * math.sqrt(p["sent"] / max_edge)
            edges.append(arc((cx, cy), (nx, ny), rc + 2, rn + 7, 7, w,
                             "edge out", "arr-out",
                             f'나 → {p["name"]} · 발신 {p["sent"]}통'))
        if p["recv"]:
            w = 1.0 + 7.0 * math.sqrt(p["recv"] / max_edge)
            edges.append(arc((nx, ny), (cx, cy), rn + 2, rc + 7, -7, w,
                             "edge in", "arr-in",
                             f'{p["name"]} → 나 · 수신 {p["recv"]}통'))

        name = p["name"] if _wcw(p["name"]) <= 14 else p["name"][:7] + "…"
        lx, ly = nx + ux * (rn + 13), ny + uy * (rn + 13)
        anchor = "start" if ux > .3 else "end" if ux < -.3 else "middle"
        if anchor == "middle":
            ly += 9 if uy > 0 else -4
        nodes.append(
            f'<circle class="node" cx="{nx:.1f}" cy="{ny:.1f}" r="{rn:.1f}" '
            f'tabindex="0" data-tip="'
            + html.escape(f'{p["name"]} · 발신 {p["sent"]}통 / 수신 {p["recv"]}통',
                          quote=True) + '"/>'
            f'<text class="nodelabel" x="{lx:.1f}" y="{ly + 4:.1f}" '
            f'text-anchor="{anchor}">{html.escape(name)}</text>')
    center = (f'<circle cx="{cx}" cy="{cy}" r="{rc}" class="menode"/>'
              f'<text x="{cx}" y="{cy + 5}" text-anchor="middle" '
              f'class="melabel">나</text>')
    return (f'<svg class="egograph" viewBox="0 0 {W} {H}" role="img">'
            + defs + "".join(edges) + "".join(nodes) + center + "</svg>")


def spark(series: list, w: int = 100, h: int = 30) -> str:
    """스탯 타일/행 스파크라인 — 회색 선 + 마지막 점 강조."""
    pts = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(pts) < 2:
        return ""
    vmax = max(v for _, v in pts) or 1
    n = len(series)
    def X(i): return 4 + (w - 12) * i / (n - 1)
    def Y(v): return 4 + (h - 8) * (1 - v / vmax)
    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
    li, lv = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<polyline class="sparkline" points="{poly}"/>'
            f'<circle cx="{X(li):.1f}" cy="{Y(lv):.1f}" r="3" class="dot"/></svg>')


# ------------------------------------------------------------------ HTML 조립

CSS = """
:root{
  /* 통계도 앱과 같은 팔레트/테마를 공유 — 공용 토큰(_CSS)에 통계 이름을 연결.
     surface/ink/muted/border 는 _CSS :root 를 그대로 상속(같은 이름). */
  --plane:var(--bg); --brand:var(--accent); --mark:var(--accent); --mark2:var(--accent2);
  --ink2:var(--ink-2);
  /* 차트 전용(격자·축·노드·상태색) — 라이트 기본 */
  --grid:#e8e8ec; --base:#c9c9cf; --node:#eaeef9;
  --good:#006300; --bad:#c22a2a;
  --st-warn:#fab219; --st-serious:#ec835a; --st-crit:#d03b3b; --st-good:#0ca30c;
  --chip-warn-ink:#7a5200; --chip-serious-ink:#8a3413;
}
:root[data-theme='dark']{
  --grid:#2c3238; --base:#454c53; --node:#282d31;
  --good:#6cc46c; --bad:#e0705f;
  --st-warn:#e0b24a; --st-serious:#e08a5a; --st-crit:#e05a5a; --st-good:#4fb84f;
  --chip-warn-ink:#e6c87e; --chip-serious-ink:#eba887;
}
*{margin:0;padding:0;box-sizing:border-box}
/* 다크: 따뜻한 강조(코랄) 위 글자는 어둡게(흰색 대비 부족) */
html[data-theme='dark'] .popt.active{color:#16181b}
html[data-theme='dark'] .melabel{fill:#16181b}
body{-webkit-font-smoothing:antialiased}
header.hero{margin-bottom:20px}
.brandline{width:44px;height:5px;background:var(--brand);border-radius:3px;
  margin-bottom:16px}
h1{font-weight:800;letter-spacing:-.02em;text-wrap:balance}
.meta{color:var(--muted);font-size:13px;margin-top:6px}
.meta b{color:var(--ink2);font-weight:600}
.periods{display:flex;gap:8px;align-items:center;margin:16px 0 22px;flex-wrap:wrap}
.periods .plabel{font-size:13px;color:var(--muted);margin-right:4px}
.popt{display:inline-block;padding:6px 16px;border-radius:999px;
  font-size:13.5px;font-weight:600;color:var(--ink2);cursor:pointer;
  text-decoration:none;user-select:none;
  background:var(--surface);border:1px solid var(--border)}
.popt:hover{border-color:var(--mark);color:var(--mark);text-decoration:none}
.popt.active{background:var(--brand);color:#fff;border-color:var(--brand)}
.popt:focus-visible{outline:2px solid var(--mark);outline-offset:2px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  gap:14px;margin-bottom:26px}
.tile{background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:18px 20px 14px}
.tile .lbl{font-size:13px;color:var(--ink2);font-weight:600}
.tile .val{font-size:34px;font-weight:700;letter-spacing:-.01em;margin-top:2px}
.tile .val small{font-size:16px;font-weight:600;color:var(--ink2)}
.tile .delta{font-size:12.5px;font-weight:600;margin-top:2px}
.tile .delta.up-bad{color:var(--bad)} .tile .delta.down-good{color:var(--good)}
.tile .delta.flat{color:var(--muted)}
.tile .sub{font-size:12.5px;color:var(--muted);margin-top:2px}
.spark{display:block;width:100px;height:30px;margin-top:8px}
section.card{background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:24px 26px;margin-bottom:18px}
h2{font-size:19px;font-weight:700;letter-spacing:-.01em}
h2 .no{color:var(--brand);margin-right:8px;font-weight:800}
.desc{font-size:13.5px;color:var(--muted);margin:3px 0 16px}
svg.linechart,svg.barchart{width:100%;height:auto;display:block}
svg:focus{outline:2px solid var(--mark);outline-offset:3px;border-radius:8px}
.grid{stroke:var(--grid);stroke-width:1}
.baseline{stroke:var(--base);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.rowlabel{fill:var(--ink2);font-size:12.5px}
.vallabel{fill:var(--ink2);font-size:12px;font-weight:600;
  font-variant-numeric:tabular-nums}
.endlabel{fill:var(--ink2);font-size:12px;font-weight:700}
.line,.sparkline{fill:none;stroke-width:2;stroke-linejoin:round;
  stroke-linecap:round}
.line{stroke:var(--mark)} .sparkline{stroke:var(--base)}
.dot{fill:var(--mark)} .ring{fill:var(--surface)}
.wash{fill:var(--mark);fill-opacity:.1}
.bar{fill:var(--mark)} .bar.alt{fill:var(--mark2)}
svg.egograph{width:100%;height:auto;display:block;max-width:660px;margin:0 auto}
.edge{fill:none;stroke-linecap:round;pointer-events:none}
.edge.out{stroke:var(--mark)} .edge.in{stroke:var(--mark2)}
.edge-hit{fill:none;stroke:transparent}
.edge-hit:focus{outline:none}
.node{fill:var(--node);stroke:var(--base);stroke-width:1}
.node:hover,.node:focus-visible{stroke:var(--mark);stroke-width:2;outline:none}
.nodelabel{fill:var(--ink2);font-size:12px;font-weight:600}
.menode{fill:var(--brand)} .melabel{fill:#fff;font-size:14px;font-weight:800}
.legend{display:flex;gap:18px;align-items:center;font-size:12.5px;
  color:var(--ink2);margin-bottom:4px}
.legend .key{width:12px;height:12px;border-radius:3px;background:var(--mark);
  display:inline-block;margin-right:6px;vertical-align:-1px}
.legend .key.alt{background:var(--mark2)}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:720px){.duo{grid-template-columns:1fr}}
h3{font-size:14px;font-weight:700;color:var(--ink2);margin-bottom:10px;
  display:flex;align-items:center;gap:7px}
h3 .key{width:12px;height:12px;border-radius:3px;background:var(--mark);
  display:inline-block}
h3 .key.alt{background:var(--mark2)}
.bar-hit{fill:transparent;cursor:default}
.bar-hit:hover+*,.bar-hit:focus{outline:none}
.xhair{stroke:var(--base);stroke-width:1}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{color:var(--muted);font-weight:600;text-align:left;font-size:12.5px;
  padding:7px 10px;border-bottom:1px solid var(--grid)}
td{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.agebar{display:inline-block;height:8px;border-radius:0 4px 4px 0;
  background:var(--mark);vertical-align:middle}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;
  font-weight:700;padding:2px 9px;border-radius:999px;white-space:nowrap}
.chip::before{content:"";width:7px;height:7px;border-radius:50%;
  background:currentColor}
.chip.warn{color:var(--chip-warn-ink);background:color-mix(in srgb,var(--st-warn) 18%,var(--surface))}
.chip.serious{color:var(--chip-serious-ink);background:color-mix(in srgb,var(--st-serious) 18%,var(--surface))}
.chip.crit{color:var(--st-crit);background:color-mix(in srgb,var(--st-crit) 13%,var(--surface))}
.chip.ok{color:var(--st-good);background:color-mix(in srgb,var(--st-good) 13%,var(--surface))}
.empty{color:var(--muted);font-size:14px;padding:14px 0}
.empty.good{color:var(--good);font-weight:600}
details.tbl{margin-top:12px}
details.tbl summary{font-size:12.5px;color:var(--muted);cursor:pointer;
  user-select:none}
details.tbl summary:hover{color:var(--ink2)}
details.tbl table{margin-top:8px}
.note{font-size:12.5px;color:var(--muted);background:var(--plane);
  border-radius:10px;padding:10px 14px;margin-bottom:16px}
.qrow{display:grid;grid-template-columns:minmax(120px,1fr) 110px 1fr auto;
  gap:14px;align-items:center;padding:9px 0;border-bottom:1px solid var(--grid)}
.qrow:last-child{border-bottom:none}
.qrow .nm{font-weight:600;font-size:14px}
.qrow .st{font-size:12.5px;color:var(--muted)}
#tip{position:fixed;pointer-events:none;background:var(--ink);
  color:var(--surface);font-size:12.5px;padding:7px 11px;border-radius:9px;
  visibility:hidden;z-index:9;max-width:320px;line-height:1.45;
  box-shadow:0 4px 14px rgba(0,0,0,.18)}
#tip b{font-size:13.5px;font-variant-numeric:tabular-nums}
footer{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
@media (prefers-reduced-motion: no-preference){
  .bar-hit{transition:fill .12s}
}
.bar-hit:hover,.bar-hit:focus-visible{fill:color-mix(in srgb,var(--mark) 9%,transparent)}
"""

# /report.js 로 서빙 — 웹 CSP(script-src 'self')가 인라인 스크립트를 막으므로
REPORT_JS = """
(function(){
  var tip = document.getElementById('tip');
  if(!tip) return;
  function showTip(html, x, y){
    tip.replaceChildren();
    html.forEach(function(part){
      if(part.b){ var b=document.createElement('b'); b.textContent=part.b; tip.appendChild(b); }
      else { tip.appendChild(document.createTextNode(part.t)); }
      tip.appendChild(document.createElement('br'));
    });
    tip.style.visibility='visible';
    var r = tip.getBoundingClientRect();
    var px = Math.min(x + 14, window.innerWidth - r.width - 10);
    var py = Math.max(y - r.height - 12, 8);
    tip.style.left = px + 'px'; tip.style.top = py + 'px';
  }
  function hideTip(){ tip.style.visibility='hidden'; }

  // 라인 차트: 크로스헤어 + 가장 가까운 X 로 스냅
  document.querySelectorAll('svg.linechart').forEach(function(svg){
    var cfg = JSON.parse(svg.getAttribute('data-chart'));
    var xh = document.getElementById(svg.id + '-x');
    var n = cfg.series.length, idx = n - 1;
    function xOf(i){ return cfg.pl + (cfg.w - cfg.pl - cfg.pr) * i / (n - 1); }
    function render(i, cx, cy){
      idx = i;
      xh.setAttribute('x1', xOf(i)); xh.setAttribute('x2', xOf(i));
      xh.setAttribute('visibility','visible');
      var v = cfg.series[i];
      showTip([{b:(v==null?'—':v + cfg.unit)},{t:cfg.labels[i] + ' 주'}], cx, cy);
    }
    svg.addEventListener('pointermove', function(e){
      var r = svg.getBoundingClientRect();
      var vx = (e.clientX - r.left) / r.width * cfg.w;
      var i = Math.round((vx - cfg.pl) / (cfg.w - cfg.pl - cfg.pr) * (n - 1));
      render(Math.max(0, Math.min(n - 1, i)), e.clientX, e.clientY);
    });
    svg.addEventListener('pointerleave', function(){
      xh.setAttribute('visibility','hidden'); hideTip();
    });
    svg.addEventListener('focus', function(){
      var r = svg.getBoundingClientRect();
      render(idx, r.left + r.width * idx / n, r.top + 40);
    });
    svg.addEventListener('blur', function(){
      xh.setAttribute('visibility','hidden'); hideTip();
    });
    svg.addEventListener('keydown', function(e){
      if(e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      var i = Math.max(0, Math.min(n - 1, idx + (e.key === 'ArrowRight' ? 1 : -1)));
      var r = svg.getBoundingClientRect();
      render(i, r.left + r.width * i / n, r.top + 40);
    });
  });

  // 막대·노드·엣지: data-tip 을 가진 모든 요소가 히트 타깃
  document.querySelectorAll('[data-tip]').forEach(function(el){
    function on(e){
      var p = e.touches ? e.touches[0] : e;
      showTip([{b: el.getAttribute('data-tip')}],
              p.clientX || 300, p.clientY || 200);
    }
    el.addEventListener('pointermove', on);
    el.addEventListener('focus', function(){
      var r = el.getBoundingClientRect();
      showTip([{b: el.getAttribute('data-tip')}], r.left + 60, r.top);
    });
    el.addEventListener('pointerleave', hideTip);
    el.addEventListener('blur', hideTip);
  });
})();
"""


def _delta_html(recent, prior, unit: str, up_is_bad: bool, vs: str) -> str:
    if recent is None or prior is None or prior == 0:
        return '<div class="delta flat">비교 기준선 축적 중</div>'
    pct = (recent - prior) / prior * 100
    if abs(pct) < 3:
        return f'<div class="delta flat">→ 보합 (vs {vs})</div>'
    arrow = "▲" if pct > 0 else "▼"
    cls = ("up-bad" if up_is_bad else "down-good") if pct > 0 else \
          ("down-good" if up_is_bad else "up-bad")
    return (f'<div class="delta {cls}">{arrow} {abs(pct):.0f}% vs {vs}</div>')


def _tbl(headers: list[str], rows: list[list[str]], num_cols: set[int]) -> str:
    # 주의: f-string 표현식 안에 백슬래시를 쓰지 말 것 — Python 3.12 미만 SyntaxError
    NUM = ' class="num"'
    h = "".join(f"<th{NUM if i in num_cols else ''}>{x}</th>"
                for i, x in enumerate(headers))
    b = "".join(
        "<tr>" + "".join(
            f"<td{NUM if i in num_cols else ''}>{c}</td>"
            for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'


def _period_bar(weeks: int) -> str:
    """검토 기간 선택 — 각 기간이 곧 링크. 누르면 그 자리에서 최신 데이터로 재분석.

    별도 [분석] 버튼 없이 기간(2/4/8/16W)을 누르면 /stats?weeks=N 로 이동하며
    재분석된다. 통계 페이지는 app.js 를 싣지 않으므로 일반 링크 이동으로 동작.
    """
    opts = "".join(
        f'<a class="popt{" active" if w == weeks else ""}" '
        f'href="/stats?weeks={w}">{w}W</a>'
        for w in PERIODS)
    return (f'<div class="periods">'
            f'<span class="plabel">검토 기간</span>{opts}'
            f'<span class="plabel">— 기간을 누르면 최신 데이터로 다시 분석</span></div>')


def _stats_inner(weeks: int, inner: str, meta: str = "") -> str:
    """통계 콘텐츠 조각(제목·기간선택·섹션·푸터)만 반환.

    상단 nav 셸(Minerva·홈·개입…)과 보조 리소스(#tip, /report.js, report.CSS)는
    web 쪽 래퍼(web.render_stats_page → _page_wide)가 씌워 다른 메뉴와 통일한다.
    """
    return f"""<header class="hero">
<h1>통계 분석 <span style="color:var(--brand)">{weeks}주</span></h1>
{meta}
</header>
{_period_bar(weeks)}
{inner}
<footer>Minerva · 통계 분석 — 조회 전용, DB 무변경</footer>"""


def render_stats(store, cfg, weeks: int) -> str:
    """웹 /stats 콘텐츠 — 전폭 단일 컬럼(좌/우 셸 미사용). nav 셸은 web 이 씌운다."""
    weeks = clamp_weeks(weeks)
    extra_me = set(a.lower() for a in getattr(cfg, "my_addresses", []) or [])
    # 별칭이 더 있으면 config.toml [report] extra_me 로 추가 (config.py 무수정)
    extra_me |= {str(a).lower() for a in (cfg.opt("report", "extra_me", default=[]) or [])}
    d = load(store.db, weeks, extra_me)
    if d is None:
        return _stats_inner(weeks, '<p class="empty">메일이 없습니다 — 먼저 동기화하세요.</p>')
    return _stats_inner(weeks, _body(d, weeks), _meta_line(d))


def _meta_line(d: dict) -> str:
    period = f"{d['weeks'][0].strftime('%Y.%m.%d')} – {d['asof'].strftime('%Y.%m.%d')}"
    return (f'<div class="meta">기간 <b>{period}</b> ({d["n_weeks"]}주)'
            f' · 메일 {len(d["msgs"])}건'
            f' · 생성 {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>')


def _body(d: dict, weeks: int) -> str:
    labels = [f"{w.month}/{w.day}" for w in d["weeks"]]
    evap = sig_evaporated(d)
    quiet = sig_quiet(d)
    pairs = _reply_pairs(d)
    lat = sig_latency(d, pairs)
    offh = sig_offhours(d)
    vol = sig_volume(d, days=weeks * 7)   # §5 도 선택 기간을 따른다
    accumulating = d["n_weeks"] < 6

    # ---- KPI 타일
    tiles = []
    tiles.append(f"""<div class="tile"><div class="lbl">증발한 내 요청</div>
      <div class="val">{len(evap)}<small>건</small></div>
      <div class="sub">{"답 없이 7일+ 경과" + ("" if not evap else f" · 최장 {evap[0]['days']}일")}</div></div>""")
    tiles.append(f"""<div class="tile"><div class="lbl">조용해진 사람</div>
      <div class="val">{len(quiet)}<small>명</small></div>
      <div class="sub">기준선 대비 최근 2주 급감</div></div>""")
    lat_val = _fmt_h(lat["recent"]) if lat["recent"] is not None else "—"
    tiles.append(f"""<div class="tile"><div class="lbl">내 응답 중앙값 (최근 2주)</div>
      <div class="val">{lat_val}</div>
      {_delta_html(lat["recent"], lat["prior"], "h", True, "이전 4주")}
      {spark(lat["series"])}</div>""")
    off_val = f'{offh["recent"]:g}<small>%</small>' if offh["recent"] is not None else "—"
    tiles.append(f"""<div class="tile"><div class="lbl">야간·주말 발신 비율</div>
      <div class="val">{off_val}</div>
      {_delta_html(offh["recent"], offh["prior"], "%", True, "이전 평균")}
      {spark(offh["series"])}</div>""")

    # ---- §1 증발한 요청
    if evap:
        maxd = evap[0]["days"]
        shown, rest = evap[:20], max(0, len(evap) - 20)
        rows = []
        for e in shown:
            chip = {"warning": ("warn", "1주+"), "serious": ("serious", "2주+"),
                    "critical": ("crit", "3주+")}[e["sev"]]
            bar = f'<span class="agebar" style="width:{max(8, e["days"] / maxd * 110):.0f}px"></span>'
            rows.append([
                html.escape(e["subject"]), html.escape(e["who"]), e["sent"],
                f'{bar} <b style="font-variant-numeric:tabular-nums">{e["days"]}일</b>',
                f'<span class="chip {chip[0]}">{chip[1]}</span>'])
        sec1_body = _tbl(["제목", "받는 사람", "보낸 날", "경과", "상태"], rows, set())
        if rest:
            sec1_body += f'<p class="empty">… 외 {rest}건 (오래된 순 상위 20건 표시)</p>'
    else:
        sec1_body = '<p class="empty good">✓ 답을 기다리며 죽어 있는 요청이 없습니다.</p>'
    sec1 = f"""<section class="card"><h2><span class="no">1</span>증발한 내 요청</h2>
      <p class="desc">내가 질문·요청을 보냈는데 그 후 스레드에 수신이 없는 것 — 하루 단위로는 안 보이는 누적 목록.</p>
      {sec1_body}</section>"""

    # ---- §2 조용해진 사람
    if quiet:
        qrows = "".join(f"""<div class="qrow">
          <span class="nm">{html.escape(q["name"])}</span>
          {spark([float(v) for v in q["series"]])}
          <span class="st">평소 주 {q["rate"]:.1f}통 ({q["base_weeks"]}주 기준) → 최근 2주 {q["recent"]}통</span>
          <span class="chip {"crit" if q["recent"] == 0 else "warn"}">{"무소식" if q["recent"] == 0 else "급감"}</span>
          </div>""" for q in quiet)
        tblq = _tbl(["이름", "주소", "기준선(주당)", "최근 2주"],
                    [[html.escape(q["name"]), html.escape(q["addr"]),
                      f'{q["rate"]:.1f}', str(q["recent"])] for q in quiet],
                    {2, 3})
        sec2_body = qrows + f'<details class="tbl"><summary>표로 보기</summary>{tblq}</details>'
    else:
        sec2_body = ('<p class="empty">기준선을 세울 데이터가 부족하거나, 급감한 사람이 없습니다.</p>'
                     if accumulating else
                     '<p class="empty good">✓ 평소 교신 패턴에서 이탈한 사람이 없습니다.</p>')
    sec2 = f"""<section class="card"><h2><span class="no">2</span>조용해진 사람</h2>
      <p class="desc">서로 주고받던 사람 중 평소 수신량 대비 최근 2주가 급감한 경우 — 관계 단위의 끊긴 흐름.</p>
      {sec2_body}</section>"""

    # ---- §3 응답 지연 추세
    tbl3 = _tbl(["주", "내 응답 중앙값(h)"],
                [[labels[i], "—" if v is None else f"{v:g}"]
                 for i, v in enumerate(lat["series"])], {1})
    sec3 = f"""<section class="card"><h2><span class="no">3</span>내 응답 지연 추세</h2>
      <p class="desc">받은 메일에 내가 답하기까지 걸린 시간의 주별 중앙값 — 우상향이면 과부하의 선행지표. (표본 {lat["n"]}건)</p>
      {svg_line(lat["series"], labels, "h", "lat", area=False)}
      <details class="tbl"><summary>표로 보기</summary>{tbl3}</details></section>"""

    # ---- §4 야간·주말
    tbl4 = _tbl(["주", "발신", "야간·주말", "비율(%)"],
                [[labels[i], str(offh["tot"][i]), str(offh["off"][i]),
                  "—" if offh["series"][i] is None else f'{offh["series"][i]:g}']
                 for i in range(d["n_weeks"])], {1, 2, 3})
    sec4 = f"""<section class="card"><h2><span class="no">4</span>야간·주말 발신 비율</h2>
      <p class="desc">내 발신 중 {NIGHT_FROM}시 이후·{NIGHT_TO}시 이전·주말 비율 — 생활 패턴 경보.</p>
      {svg_line(offh["series"], labels, "%", "offh", area=True)}
      <details class="tbl"><summary>표로 보기</summary>{tbl4}</details></section>"""

    # ---- §5 자주 주고받는 상대 (선택 기간)
    tbl5 = _tbl(["이름", "주소", "내 발신", "수신"],
                [[html.escape(r["name"]), html.escape(r["addr"]),
                  str(r["sent"]), str(r["recv"])] for r in vol["rows"]],
                {2, 3})
    graph = svg_ego_graph(vol)
    duo = f"""<div class="duo">
        <div><h3><span class="key"></span>내가 자주 보낸 상대</h3>
          {svg_count_bars(vol["sent"], alt=False, tip_word="내 발신")}</div>
        <div><h3><span class="key alt"></span>내게 자주 보낸 사람</h3>
          {svg_count_bars(vol["recv"], alt=True, tip_word="수신")}</div>
      </div>"""
    if graph:
        vis5 = (f'<div class="legend"><span><span class="key"></span>나 → 상대 (발신)</span>'
                f'<span><span class="key alt"></span>상대 → 나 (수신)</span>'
                f'<span style="color:var(--muted)">원 크기 = 총 교신량 · 선 굵기 = 통수</span></div>'
                + graph
                + f'<details class="tbl"><summary>막대 그래프로 보기</summary>{duo}</details>')
    else:
        vis5 = duo
    sec5 = f"""<section class="card"><h2><span class="no">5</span>자주 주고받는 상대</h2>
      <p class="desc">최근 {vol["days"]}일 기준 — 나를 중심으로 한 방향 그래프. 두 방향 굵기의 비대칭이 관계의 방향성을 보여줍니다.</p>
      {vis5}
      <details class="tbl"><summary>표로 보기 (전체)</summary>{tbl5}</details></section>"""

    note = (f'<div class="note">데이터 축적 {d["n_weeks"]}주차 — 기준선 통계(§2, 델타)는 '
            f'6~8주부터 안정화됩니다. 짧은 기간(2W/4W) 선택 시에도 기준선 신호는 약해집니다.</div>'
            if accumulating else "")

    return note + f'<div class="kpis">{"".join(tiles)}</div>' + sec1 + sec2 + sec3 + sec4 + sec5
