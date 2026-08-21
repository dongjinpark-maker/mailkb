"""통계 분석 — 시간축 신호 대시보드 (웹 /stats, 전폭 단일 페이지).

mailkb-lab report.py 이식(2026-07-10). 라이브 DB 를 조회 전용으로 읽어
신호를 계산해 자족형 HTML 로 렌더한다.
외부 리소스(폰트/CDN) 요청 없음 — 사내망에서 그대로 열림.

신호 (2026-08-02 정리 — 이 도구의 목적에 맞는 것만 남겼다):
  타일  응답 중앙값   — 나/상대 대비 (§1 의 '양'에 '속도'를 붙여 부하 진단 완성)
  §1 볼륨 추세       — 주별 발신/수신 통수 2계열 (얼마나 들어오나)
  §2 활동 히트맵     — 요일×시간 발신/수신 격자
  §3 받은 메일 구성  — 스팸/공지/업무(직접·참조) 비율 (그중 쓸 것은 얼마나)
  §4 왕복 많은 논의  — 발신 방향 전환이 잦은 스레드 (어디서 막히나)

2026-07-13 개편은 "Email Meter 등 표준 지표 이식"이었는데, 그 기준이 이 도구의
목적("Outlook 이 못 하는 것만")에서 나온 것이 아니라 남의 제품에서 온 것이었다.
빠진 것과 이유:
  · 자주 주고받는 상대 — 인물 화면이 흡수(같은 재료로 인물별 카드가 더 많이 준다)
  · 답 기다리는 내 발신 — 정규식 요청 판정. 2026-07-30 에 같은 이유로 전면
    폐기한 신호 노출의 잔존분이었다(실측 오탐: '별첨 참고 바랍니다'가 1위)
  · 야간·주말 비율 — 메일 지식이 아니라 자기 정량화
  · 검토 기간 선택(2/4/8/16W) — 섹션마다 필요한 창이 달라 하나로 강제할 수 없다
    (아래 상수 참조)

JS 는 인라인이 아니라 /report.js 로 서빙된다(웹 CSP: script-src 'self').
"""

from __future__ import annotations

import html
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .clean import strip_preserved

# ------------------------------------------------------------------ 설정

# 창은 **섹션마다 고정**이다 — 전역 기간 선택기(2/4/8/16W)를 없앴다(2026-08-02).
#
# 없앤 이유. ① 섹션마다 필요한 창이 다르다: 추세는 길어야 읽히고(코드가 스스로
# "6~8주부터 안정화"라고 경고한다) 왕복 목록은 짧아야 '회의 전환 후보'로 쓸모가
# 있다(4개월 전 핑퐁은 이미 끝난 논의다). 하나로 강제하면 어느 값을 골라도 절반은
# 틀린 창으로 본다. ② 그 선택기는 한동안 **고장난 채 돌아다녔는데**(축만 바뀌고
# 데이터셋은 전량, 2026-07-10 수정) 사용 중이 아니라 코드 검토로 발견됐다 —
# 쓰이지 않았다는 뜻이다. ③ 응답 지표만 늘 '최근 2주' 앵커라 선택과 무관했고,
# 그래서 같은 이름이 한 화면에서 다른 값을 냈다.
#
# 창을 없애는 대신 **각 섹션이 자기 창을 화면에 적는다** — 지금까지처럼 전역
# 선택기가 있는데 일부만 따르는 것보다 정직하다.
TREND_WEEKS = 12     # 추세·패턴(응답 중앙값·볼륨·히트맵) — 안정화 구간 위
RECENT_WEEKS = 4     # 지금 상태(인박스 구성·살아있는 논의)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fmt_h(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f}일"
    return f"{hours:.0f}h" if hours >= 10 else f"{hours:.1f}h"


# ------------------------------------------------------------------ 데이터 적재

def load(db, max_weeks: int, extra_me=frozenset()) -> dict | None:
    """라이브 DB(조회 전용 SELECT 만)에서 기간 내 신호 계산용 데이터 적재.

    메일이 없으면 None (웹은 빈 상태 페이지로 렌더).
    """
    # 전 이력은 (1) 별칭 판정용 발신 주소 집합, (2) 기간 축(min/max sent_on)만
    # 필요하다. 이 둘은 메타 컬럼만으로 충분하므로 큰 new_content 는 읽지 않는다
    # — 대형 DB 에서 매 통계 로드마다 전 이력 본문을 훑던 비용을 없앤다.
    # 본문까지 필요한 것은 검토 창 안 메일뿐이라 아래에서 따로 로드한다(결과 동일).
    meta = db.execute(
        "SELECT sender_addr, is_sent, sent_on FROM messages "
        "WHERE sent_on != '' ORDER BY sent_on, id"
    ).fetchall()
    if not meta:
        return None

    # 내 주소 집합: is_sent=1 발신자 + 설정 주소(별칭 포함). 별칭 발신 메일이
    # is_sent=0 으로 들어와 있으면 발신으로 재분류 (전 절 오염 방지).
    # 창 밖 메일까지 포함해 파악 — 별칭 지식은 기간과 무관하게 온전해야 함.
    my_addrs = {(m["sender_addr"] or "").lower() for m in meta if m["is_sent"]}
    my_addrs |= {a.lower() for a in extra_me if a}
    my_addrs.discard("")
    # 숨긴 스레드 — **목록** 절(왕복 논의)만 이걸로 거른다. 볼륨·히트맵 같은
    # 집계는 전량 센다(메일은 실제로 왔으니까). 숨김은 "조용히 하라"이지
    # "없던 일로 하라"가 아니다.
    hidden_ids = {r["id"] for r in
                  db.execute("SELECT id FROM threads WHERE hidden=1")}
    names = {r["addr"].lower(): r["name"] for r in
             db.execute("SELECT addr, name FROM people") if r["name"]}

    asof = _dt(meta[-1]["sent_on"])
    data_first = _dt(meta[0]["sent_on"])
    # 주 축: 데이터 시작 주 ~ asof 주, 최대 max_weeks — 검토 기간은 항상 제한됨
    last_ws = _week_start(asof.date())
    n_weeks = min(max_weeks,
                  (last_ws - _week_start(data_first.date())).days // 7 + 1)
    weeks = [last_ws - timedelta(weeks=n_weeks - 1 - i) for i in range(n_weeks)]
    widx = {w: i for i, w in enumerate(weeks)}
    window_start = weeks[0]      # 검토 기간 시작 주(월요일)

    # ★ 검토 기간 제한: 창(window_start~asof) 안 메일만 본문까지 로드한다. 문자열
    #   비교 sent_on >= 'YYYY-MM-DD' 는 _dt(sent_on).date() >= window_start 과 등가.
    msgs = [dict(r) for r in db.execute(
        """SELECT id, thread_id, sender_addr, sender_name, to_addrs, subject,
                  sent_on, is_sent, new_content
           FROM messages WHERE sent_on >= ? ORDER BY sent_on, id""",
        (window_start.isoformat(),)
    )]
    for m in msgs:      # 별칭 발신 재분류 — 창 안 메일에 적용(원래도 창 밖은 버려짐)
        if not m["is_sent"] and (m["sender_addr"] or "").lower() in my_addrs:
            m["is_sent"] = 1
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

def sig_volume_trend(d: dict) -> dict:
    """§1 주별 발신/수신 통수 2계열 + 최근 주 vs 이전 평균 델타(타일용)."""
    n = d["n_weeks"]
    sent = [0] * n
    recv = [0] * n
    for m in d["msgs"]:
        i = d["wk"](_dt(m["sent_on"]))
        if i is None:
            continue
        (sent if m["is_sent"] else recv)[i] += 1

    def recent_prior(series):
        recent = series[-1] if series else None
        prior = (sum(series[:-1]) / len(series[:-1])) if len(series) > 1 else None
        return recent, prior

    sr, spr = recent_prior(sent)
    rr, rpr = recent_prior(recv)
    return {"sent": sent, "recv": recv,
            "sent_recent": sr, "sent_prior": spr,
            "recv_recent": rr, "recv_prior": rpr,
            "sent_total": sum(sent), "recv_total": sum(recv)}


WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")


def sig_heatmap(d: dict) -> dict:
    """§2 요일(월~일) × 시간(0~23) 발신/수신 통수 격자."""
    sent = [[0] * 24 for _ in range(7)]
    recv = [[0] * 24 for _ in range(7)]
    for m in d["msgs"]:
        dt = _dt(m["sent_on"])
        (sent if m["is_sent"] else recv)[dt.weekday()][dt.hour] += 1
    return {"sent": sent, "recv": recv,
            "sent_max": max((max(r) for r in sent), default=0),
            "recv_max": max((max(r) for r in recv), default=0)}


# 일간 회고에서 **AI 계층이 실제로 돌았음**을 뜻하는 절 머리. 파일이 있다는 것만으로는
# 안 된다 — 웹의 자동 생성(web._maybe_auto_review)은 ai=False 라 파일은 매일 생기지만
# 롤링 요약·수확은 돌지 않는다. 이 둘은 AI 를 돌렸을 때만 review.render 가 낸다.
# (그 자동 생성이 절을 **지우던** 것이 2026-08-06 결함 — 이 격자가 지난 날을 잃었다.
#  지금은 review.restore_ai_layer 가 보관분을 다시 얹어 판정이 흔들리지 않는다.)
_AI_DAILY_MARKS = ("## Executive Summary", "## AI 회고 분석")


def sig_memory(store, cfg, d: dict) -> dict:
    """§5 기억 커버리지 — **지식이 안 쌓인 구간**을 드러낸다.

    왜 통계에 두나: 수확의 소급 상한이 `ai.summary_max_days`(기본 1일)라
    **앱을 안 연 날의 메일은 수확에서 영구히 빠진다.** 그 대가가 어디에도
    보이지 않으면, 사용자는 자기 기억에 구멍이 몇 개인지 모른 채 상한을 올릴지
    말지 판단할 수 없다. 이 절은 허영 지표가 아니라 이미 있는 트레이드오프의
    계량기다(§4 받은 메일 구성이 노이즈 규칙을 점검하는 것과 같은 부류).

    날짜별 판정은 회고 파일의 AI 절 유무로 한다 — 파일 존재만 보면 거짓말이
    된다(자동 생성분은 ai=False).

    **'요약된 스레드 N/M' 은 2026-08-15 에 뺐다.** 누적 요약이 회고에서 빠져
    스레드 화면 버튼으로 옮겨지면서, 그 수는 '지식이 쌓였나'가 아니라 '내가
    요약 버튼을 몇 번 눌렀나'가 됐다. 애초에 저장된 지식(md)의 **대용물**이었고
    이제 진짜 지표가 있으니 대용물을 남길 이유가 없다.
    """
    daily = cfg.vault / "daily"
    asof = d["asof"].date()
    days = []
    for wi, w in enumerate(d["weeks"]):
        for wd in range(7):
            day = w + timedelta(days=wd)
            if day > asof:
                break
            on = False
            p = daily / f"{day.isoformat()}.md"
            try:
                on = any(mk in p.read_text(encoding="utf-8") for mk in _AI_DAILY_MARKS)
            except (OSError, UnicodeDecodeError):
                pass                       # 파일 없음·읽기 실패 = 안 쌓인 날
            days.append({"date": day, "on": on, "weekend": wd >= 5,
                         "wd": wd, "wi": wi})
    since = d["weeks"][0].isoformat()
    # 저장된 지식(암묵지 md) — 결정 원장은 2026-08-14 폐지, 지식이 그 자리다
    saved = store.db.execute(
        "SELECT COUNT(*) FROM knowledge_candidates WHERE status='saved' AND "
        "date >= ?", (since,)).fetchone()[0]
    on_n = sum(1 for x in days if x["on"])
    return {"days": days, "days_on": on_n, "days_total": len(days),
            "knowledge": saved, "any": bool(on_n or saved)}


def svg_coverage(days: list, n_weeks: int) -> str:
    """§5 날짜 격자 — 요일(행)×주(열). 채움 = 그날 지식이 쌓임.

    §2 히트맵과 같은 시각 문법을 쓴다(같은 셀 크기·툴팁). 농도가 아니라 2단계인
    것은 이 절의 질문이 '얼마나'가 아니라 '쌓였나 아닌가'이기 때문이다.

    **히트맵의 CSS 클래스를 물려 쓰면 안 된다.** `svg.heatmap` 의
    `width:100%;max-width:600px` 는 **24열(538px)** 짜리 히트맵에 맞춘 값이라,
    12열(286px)인 이 격자에 걸면 화면에서 2.1배로 늘어난다 — 뷰박스 셀 치수가
    같아도 칸이 두 배로 보인다(2026-08-02 실측). 그래서 전용 클래스를 쓰고
    뷰박스와 같은 값의 width/height 를 실어 **1:1 로 렌더**한다. viewBox 는
    남겨 좁은 화면에서 비율대로 줄어들게 한다.

    **셀에 tabindex 를 주지 않는다.** 히트맵은 값이 있는 칸만 탭 정지라 성기지만,
    여기서는 빈 칸도 정보라 전부 주면 최대 84번을 눌러야 격자를 빠져나간다
    (자체 점검에서 확인). 격자는 눈으로 보는 보조물이고, 같은 사실은 위 요약
    줄(지식이 쌓인 날 N/M)이 글자로 이미 말한다 — 그래서 svg 에 aria-label 을
    달아 읽히게 하고 키보드 동선은 비운다."""
    CW, CH, PL, PT = 21, 20, 30, 18
    W = PL + max(1, n_weeks) * CW + 4
    H = PT + 7 * CH + 4
    g = []
    for wd in range(7):
        y = PT + wd * CH
        g.append(f'<text x="{PL - 7}" y="{y + CH / 2 + 4:.1f}" class="hmtick" '
                 f'text-anchor="end">{WEEKDAY_KO[wd]}</text>')
    for x in days:
        cx = PL + x["wi"] * CW
        y = PT + x["wd"] * CH
        cls = "covcell on" if x["on"] else ("covcell weekend" if x["weekend"]
                                            else "covcell")
        tip = f'{x["date"].isoformat()} · ' + ("지식 쌓임" if x["on"] else "안 쌓임")
        g.append(f'<rect x="{cx}" y="{y}" width="{CW - 2}" height="{CH - 2}" '
                 f'rx="3" class="{cls}" data-tip="'
                 + html.escape(tip, quote=True) + '"/>')
    on_n = sum(1 for x in days if x["on"])
    label = f"날짜별 기억 커버리지 — {len(days)}일 중 {on_n}일 지식이 쌓임"
    return (f'<svg class="covgrid" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="{html.escape(label, quote=True)}">'
            + "".join(g) + "</svg>")


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
    """§3(타일) 주별 내 응답 중앙값 시계열 + 최근 2주 vs 이전 4주 델타."""
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


def _their_pairs(d: dict) -> list[tuple]:
    """(상대 addr, name, 상대 응답 지연 h, 주 idx) — 내 발신 뒤 첫 수신까지.

    _reply_pairs 의 거울 — 저쪽은 '내가 받고 답한 시간', 이쪽은 '내가 보내고
    받기까지'(상대가 나를 기다리게 한 게 아니라 내가 기다린 시간).
    주 idx 를 함께 담아 두 지표가 같은 형태(중앙값·주별 스파크라인)로 그려지게
    한다 — 나란히 놓고 대비하는 것이 이 지표의 유일한 쓸모라 형태가 같아야 한다."""
    pairs = []
    for ms in d["threads"].values():
        pending = []            # 답을 기다리는 내 발신
        for m in ms:
            if not m["is_sent"]:
                for p in pending:
                    delay = (_dt(m["sent_on"]) - _dt(p["sent_on"])).total_seconds() / 3600
                    i = d["wk"](_dt(m["sent_on"]))
                    if i is not None and 0 < delay < 24 * 30:
                        pairs.append(((m["sender_addr"] or "").lower(),
                                      m["sender_name"] or m["sender_addr"],
                                      delay, i))
                pending = []
            else:
                pending.append(m)
    return pairs


def sig_response(d: dict, mine: list, theirs: list) -> dict:
    """§3 응답 시간 요약 — 나/상대 중앙값과 표본 수."""
    med = lambda xs: statistics.median(xs) if xs else None
    return {
        "mine": med([p[2] for p in mine]), "mine_n": len(mine),
        "theirs": med([p[2] for p in theirs]), "theirs_n": len(theirs),
    }


def sig_inbox_mix(d: dict, cfg, since: str = "") -> dict:
    """§3 받은 메일 구성 — 상호배타 우선순위로 4구간 분류.

    스팸 > 공지(대량발송) > 업무·직접(To 에 나) > 업무·참조(그 외 — CC 등).
    '내가 직접 처리해야 할 것 vs 그냥 참조로 흘러온 것'의 비율을 보여준다.
    since 는 짧은 창(RECENT_WEEKS) — 이 절의 쓸모는 '노이즈 규칙을 고친 효과가
    지금 인박스에 났나'라 최근이어야 한다."""
    seg = {"spam": 0, "notice": 0, "direct": 0, "cc": 0}
    bcast = cfg.broadcast_to
    for m in d["msgs"]:
        if m["is_sent"]:
            continue
        if since and (m["sent_on"] or "") < since:
            continue
        subj = m["subject"] or ""
        if cfg.is_noise(m["sender_addr"]) or cfg.is_noise_subject_strong(subj):
            seg["spam"] += 1
            continue
        to = [a.strip().lower() for a in (m["to_addrs"] or "").split(";") if a.strip()]
        if len(to) >= bcast:
            seg["notice"] += 1
        elif set(to) & d["my_addrs"]:
            seg["direct"] += 1
        else:
            seg["cc"] += 1
    return {"seg": seg, "total": sum(seg.values())}


def sig_pingpong(d: dict, cfg, since: str = "") -> list[dict]:
    """§4 발신 방향 전환(왕복)이 잦은 스레드 — 메일로 안 끝나는 논의.

    '왕복' = 시간순에서 is_sent 가 바뀐 횟수(같은 사람 연속 발신은 1회로 안 셈).
    전원 자동발송(노이즈)·강한 제목 노이즈 스레드는 제외.
    since 는 짧은 창(RECENT_WEEKS) — '회의 전환 후보'라는 조언은 **살아 있는
    논의**에만 성립한다. 넉 달 전 핑퐁은 이미 끝났거나 잊힌 것이다.
    숨긴 스레드도 뺀다 — 이 절은 집계가 아니라 **목록**이라, 사용자가 조용히
    하라고 한 것이 제목째로 뜨면 안 된다(볼륨·히트맵 같은 집계는 전량 센다)."""
    out = []
    for tid, ms in d["threads"].items():
        if tid in d["hidden"]:
            continue
        full = sorted(ms, key=lambda m: m["sent_on"])
        if not full:
            continue
        # 제목·노이즈 판정은 **스레드 전체**의 첫 메일로 한다 — 창으로 자른 첫
        # 메일은 'RE: RE: …' 라 원 제목이 아니다. 왕복 카운트만 창 안에서 센다.
        seq = [m for m in full if (m["sent_on"] or "") >= since] if since else full
        if not seq:
            continue
        inbound = [m for m in seq if not m["is_sent"]]
        if inbound and all(cfg.is_noise(m["sender_addr"]) for m in inbound):
            continue
        if cfg.is_noise_subject_strong(full[0]["subject"] or ""):
            continue
        turns = sum(1 for a, b in zip(seq, seq[1:]) if a["is_sent"] != b["is_sent"])
        if turns < 2:
            continue
        parts = sorted({(m["sender_name"] or m["sender_addr"])
                        for m in inbound})
        who = parts[0] if parts else "?"
        if len(parts) > 1:
            who += f" 외 {len(parts) - 1}"
        out.append({"thread_id": tid, "subject": full[0]["subject"] or "(제목 없음)",
                    "turns": turns, "msgs": len(seq), "who": who})
    out.sort(key=lambda x: (-x["turns"], -x["msgs"]))
    return out[:8]


# ------------------------------------------------------------ 인물 도시에 (v1)
# 랜딩 순위 = 교류 강도. 데이터 수집(store.person_window_counts)과 점수 공식
# (_intensity)을 분리 — 정렬 기준을 바꾸려면 _intensity 한 곳만 고친다.

def _intensity(recv: int, sent: int, last_seen: str, today: str) -> float:
    """교류 강도 — v1: 수신 빈도 위주(사용자 지정). 최근성·상호성 가중 등으로
    튜닝하려면 이 순수 함수만 교체한다. last_seen/today 는 향후 최근성 가중용."""
    return recv * 1.0 + sent * 0.5


def rank_people(store, cfg, window_weeks: int = 26, limit: int = 50) -> list[dict]:
    """인물 랜딩 — 최근 window_weeks 주 교류 강도순. 봇·자동발송(ignore/blocked)만
    제외하고 외부 협력사는 남긴다(도시에 대상). 각 원소: addr·name·recv·sent·
    last_seen·score."""
    rows = store.person_window_counts(window_weeks)
    today = max((r["last_seen"] for r in rows), default="")
    out = []
    for r in rows:
        if cfg.is_noise_sender_hard(r["addr"]):
            continue
        score = _intensity(r["recv"], r["sent"], r["last_seen"], today)
        if score <= 0:
            continue
        out.append({**r, "score": score})
    out.sort(key=lambda x: (-x["score"], x["addr"]))
    return out[:limit]


def _addr_in_to(m: dict, addr: str) -> bool:
    """내 발신 메일 m 의 To 에 addr 가 있나 (load 는 cc_addrs 를 안 싣는다)."""
    return addr in {a.strip().lower()
                    for a in (m.get("to_addrs") or "").split(";") if a.strip()}


def person_metrics(store, cfg, addr: str, weeks: int = 26) -> dict | None:
    """단일 인물의 결정론 지표 — 관계 수치·진행중·미결(내 대기). 전원 대상
    report.load 를 한 번 로드해 이 addr 로 좁힌다(→ /stats 수치와 정의상 일치)."""
    extra_me = {a.lower() for a in getattr(cfg, "my_addresses", []) or []}
    extra_me |= {str(a).lower()
                 for a in (cfg.opt("report", "extra_me", default=[]) or [])}
    d = load(store.db, weeks, extra_me)
    if d is None:
        return None
    addr = addr.lower()
    nw = d["n_weeks"]
    recv_series = [0] * nw           # 주별 수신 (스파크라인용)
    sent_series = [0] * nw           # 주별 발신
    pthreads: set[int] = set()
    recv = sent = 0
    last = ""
    for tid, ms in d["threads"].items():
        here = False
        for m in ms:
            if (m["sender_addr"] or "").lower() == addr:
                here = True
                recv += 1
                last = max(last, m["sent_on"])
                wi = d["wk"](_dt(m["sent_on"]))
                if wi is not None:
                    recv_series[wi] += 1
            elif m["is_sent"] and _addr_in_to(m, addr):
                here = True
                sent += 1
                last = max(last, m["sent_on"])
                wi = d["wk"](_dt(m["sent_on"]))
                if wi is not None:
                    sent_series[wi] += 1
        if here:
            pthreads.add(tid)
    mine = [p[2] for p in _reply_pairs(d) if p[0] == addr]    # 내가 이 사람에게 답한 시간
    theirs = [p[2] for p in _their_pairs(d) if p[0] == addr]  # 이 사람이 내게 답한 시간
    return {
        "addr": addr, "recv": recv, "sent": sent, "last_seen": last,
        "my_median_h": statistics.median(mine) if mine else None,
        "their_median_h": statistics.median(theirs) if theirs else None,
        # 인물 카드의 '진행 중'. 창을 안 좁힌다 — 카드는 그 사람과의 관계 전체를
        # 보는 자리라 통계 §4('지금 뜨거운 논의')와 목적이 다르다.
        "pingpong": [pp for pp in sig_pingpong(d, cfg)
                     if pp["thread_id"] in pthreads],
        "recv_series": recv_series, "sent_series": sent_series,
        "asof": d["asof"], "weeks": d["n_weeks"],
    }


# ── 주요 어휘 (도시에 v1) — 본인 발신어의 가중 빈도. 형태소 분석기(mecab/konlpy)
# 없이 stdlib 로만: URL 제거 → 토큰 분리 → 말미 조사 제거 → 불용어 → 빈도.
# "형태소 분석"이 아니라 "빈도 뷰"라 이름('주요 어휘')으로 명확히 한다.
# 불용어 한/영 표준은 stopwords-iso(MIT, github.com/stopwords-iso)에서 발췌·정리.

# URL·이메일 — 본문의 http/https/www/도메인·주소를 통째로 제거(어휘 아님).
_URL_RX = re.compile(
    r"https?://\S+|www\.\S+|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 한글 2자+ 또는 영문 시작 3자+(EC·ED·DB·AI 같은 2자 약어는 노이즈라 제외;
# CVE·QAT·MPW·DDR·NPX-200·SoC 등 3자+ 도메인 약어·모델명은 보존).
_WORD_RX = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+.\-]{2,}")
# 말미 조사 — 긴 것 먼저(에서 > 에). 제거는 어간 2자+ 일 때만(결과→결 같은 과잉절단 방지).
_JOSA_RX = re.compile(
    r"(에서|으로|에게|한테|처럼|보다|마다|밖에|조차|라도|이라도|든지|까지|부터"
    r"|과의|와의|은|는|이|가|을|를|의|에|로|도|만|과|와|나)$")
# 동사·경어체 어미(첨부합니다→첨부) · 호칭(도현님→도현) — 조사와 같은 어간 가드.
_ENDING_RX = re.compile(
    r"(었습니다|았습니다|였습니다|하였습니다|했습니다|하겠습니다|겠습니다|드립니다"
    r"|드리겠습니다|바랍니다|주시기|주세요|하세요|드려요|해요|합니다|됩니다|입니다"
    r"|습니다)$")
_HONOR_RX = re.compile(r"(님께|께서|님|씨|군|양)$")

# 한국어 불용어 — stopwords-iso/ko 2자+ 발췌 + 업무메일 상투어·직함·서명·조사.
_STOP_KO = """
안녕하세요 안녕하십니까 감사합니다 감사드립니다 감사 수고하세요 수고 부탁드립니다
부탁드려요 부탁드리겠습니다 부탁 바랍니다 드립니다 드리겠습니다 드려요 올림 드림 인사
있습니다 없습니다 합니다 했습니다 하겠습니다 됩니다 되었습니다 같습니다 입니다
관련 관련하여 대한 대해 통해 통하여 위해 위한 경우 내용 아래 이번 다음 이전 오늘 내일
어제 저희 제가 그리고 하지만 또한 다만 이에 해당 그것 이것 여기 거기
첨부 참고 참조 회신 전달 공유 확인 답변 문의 말씀 안내 검토 요청 진행 예정 필요 처리 부분
팀장 수석 책임 선임 주임 사원 대리 과장 차장 부장 이사 상무 전무 대표 그룹 본부 사업부
개발팀 파트 센터 내선 직통 휴대폰 대표번호 사무실
에서 으로 에게 한테 까지 부터 처럼 마다 밖에 조차 라도 든지
가까스로 가령 각각 각자 각종 같다 같이 거의 게다가 겨우 결국 고로 과연 관하여 관한
그들 그때 그래 그래도 그래서 그러나 그러니 그러면 그런데 그럼 그저 근거 기타 나머지
남들 남짓 너희 다른 다섯 다소 다수 단지 당신 당장 대로 더구나 도착 동시 동안 두번
뒤이어 등등 따라 따위 때문 마저 마치 만약 만일 만큼 매번 모두 무렵 무슨 무엇 물론
바로 반대로 반드시 보다 불구 비교 비록 상대 설령 설마 설사 시각 시간 아니 아무 아울러
아홉 약간 양자 어느 어디 어때 어떤 어떻게 언제 얼마 여덟 여러분 여부 여섯 여전히
오로지 오직 오히려 외에 우리 우선 응당 의거 의해 이것 이곳 이때 이라면 이래 이런 이상
이어서 이외 이용 이유 이제 일곱 일단 일반 있다 자기 자신 잠깐 잠시 저것 저기 저희 전부
전자 정도 제외 조금 즉시 지금 진짜 차라리 첫번째 타인 하게 하고 하곤 하기 하나 하느니
하는 하더라도 하도록 하든지 하면 하면서 하물며 하여 하여금 하여야 한다면 한데 한마디
함께 해도 해요 했어요 향하여 향해서 혹시 혹은 혼자 훨씬
""".split()

# 영어 불용어 — stopwords-iso/en 기능어(3자+) + 웹/툴·첨부 확장자(도메인 아님).
_STOP_EN = """
the and for are was this that with from have has had not but you your our all any can
will would should could been were they them their then than out get got may more most
some such only also into over under about after before per via its just now how who
which when where why what does did might must shall her him his she said through during
between among against without within because while although however therefore moreover
furthermore these those each every both either neither other another same different few
several many much least enough too are aren isn don doesn didn couldn wouldn shouldn
http https www com org net html htm url uri link links click href
jira confluence wiki sharepoint teams zoom slack outlook gmail email mail mailto cid
png jpg jpeg gif svg pdf docx xlsx pptx doc xls ppt zip
""".split()

# 통합(영문은 소문자 저장 — 토큰도 소문자로 대조).
WORD_STOP = frozenset(_STOP_KO) | frozenset(w.lower() for w in _STOP_EN)


def _strip_josa(w: str) -> str:
    if not ("가" <= w[0] <= "힣"):
        return w
    m = _JOSA_RX.search(w)
    if not m:
        return w
    stem = w[:m.start()]
    return stem if len(stem) >= 2 else w


def _stem(w: str) -> str:
    """한글 토큰 → 어간. 어미·호칭·조사를 순서대로 벗기되 어간 2자+ 일 때만."""
    if "가" <= w[0] <= "힣":
        for rx in (_ENDING_RX, _HONOR_RX):
            m = rx.search(w)
            if m and len(w) - len(m.group(0)) >= 2:
                w = w[:m.start()]
    return _strip_josa(w)


def top_words(texts, limit: int = 25, extra_stop=(),
              min_count: int = 2) -> list[tuple[str, int]]:
    """본인 발신 본문 목록 → (단어, 빈도) 상위 limit. 1회성 단어(min_count 미만)는
    '주요' 어휘가 아니므로 제외. 반환은 빈도 내림차순."""
    stop = WORD_STOP | {s2 for s in extra_stop if (s2 := str(s).strip())} \
        | {s2.lower() for s in extra_stop if (s2 := str(s).strip())}
    c: Counter = Counter()
    for t in texts:
        body = _URL_RX.sub(" ", strip_preserved(t or ""))   # URL·이메일 먼저 제거
        for tok in _WORD_RX.findall(body):
            w = _stem(tok)
            if len(w) < 2 or w in stop or w.lower() in stop:
                continue
            c[w] += 1
    return [(w, n) for w, n in c.most_common() if n >= min_count][:limit]


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


def svg_trend(sent: list, recv: list, labels: list[str]) -> str:
    """§1 주별 발신/수신 2계열 라인 — 발신=강조, 수신=보조. 크로스헤어 툴팁."""
    W, H, PL, PR, PT, PB = 640, 220, 40, 52, 14, 26
    pw, ph = W - PL - PR, H - PT - PB
    n = len(labels)
    allv = [v for v in sent + recv if v is not None]
    if not allv or n < 2:
        return '<p class="empty">데이터가 아직 부족합니다.</p>'
    ticks = _nice_ticks(max(allv))
    top = ticks[-1] or 1
    def X(i): return PL + pw * i / (n - 1)
    def Y(v): return PT + ph * (1 - v / top)

    g = []
    for t in ticks:
        g.append(f'<line x1="{PL}" y1="{Y(t):.1f}" x2="{PL + pw}" y2="{Y(t):.1f}" class="grid"/>')
        g.append(f'<text x="{PL - 8}" y="{Y(t) + 4:.1f}" class="tick" text-anchor="end">{t:g}</text>')
    step = max(1, (n + 7) // 8)
    for i in range(0, n, step):
        g.append(f'<text x="{X(i):.1f}" y="{H - 8}" class="tick" text-anchor="middle">{labels[i]}</text>')

    for series, cls, dotcls in ((recv, "line alt", "dot alt"), (sent, "line", "dot")):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(series))
        g.append(f'<polyline class="{cls}" points="{pts}"/>')
        li = n - 1
        g.append(f'<circle cx="{X(li):.1f}" cy="{Y(series[li]):.1f}" r="6" class="ring"/>')
        g.append(f'<circle cx="{X(li):.1f}" cy="{Y(series[li]):.1f}" r="4" class="{dotcls}"/>')
    g.append(f'<line id="trend-x" class="xhair" x1="0" y1="{PT}" x2="0" y2="{PT + ph}" visibility="hidden"/>')

    payload = html.escape(json.dumps(
        {"labels": labels, "series": sent, "series2": recv,
         "unit": "통", "leg": "발신", "leg2": "수신",
         "pl": PL, "pr": PR, "w": W}, ensure_ascii=False), quote=True)
    return (f'<svg class="linechart" id="trend" viewBox="0 0 {W} {H}" '
            f'role="img" tabindex="0" data-chart="{payload}">'
            f'<rect x="{PL}" y="{PT}" width="{pw}" height="{ph}" fill="transparent"/>'
            + "".join(g) + "</svg>")


def svg_heatmap(grid: list, vmax: int, alt: bool = False) -> str:
    """§2 요일×시간 히트맵 — 단일 색 램프(통수=농도). 셀별 툴팁."""
    CW, CH, PL, PT = 21, 20, 30, 18
    W = PL + 24 * CW + 4
    H = PT + 7 * CH + 4
    fill = "var(--mark2)" if alt else "var(--mark)"
    g = []
    for h in range(0, 24, 3):
        g.append(f'<text x="{PL + h * CW + CW / 2:.1f}" y="{PT - 6}" '
                 f'class="hmtick" text-anchor="middle">{h}</text>')
    for wd in range(7):
        y = PT + wd * CH
        g.append(f'<text x="{PL - 7}" y="{y + CH / 2 + 4:.1f}" class="hmtick" '
                 f'text-anchor="end">{WEEKDAY_KO[wd]}</text>')
        for h in range(24):
            c = grid[wd][h]
            x = PL + h * CW
            op = (0.14 + 0.86 * (c / vmax)) if (c and vmax) else 0
            cell = (f'<rect x="{x}" y="{y}" width="{CW - 2}" height="{CH - 2}" '
                    f'rx="3" class="hmcell"')
            if op:
                cell += f' fill="{fill}" fill-opacity="{op:.2f}"'
            else:
                cell += ' fill="var(--node)"'
            if c:
                cell += (' tabindex="0" data-tip="'
                         + html.escape(f'{WEEKDAY_KO[wd]} {h}시 · {c}통', quote=True)
                         + '"')
            g.append(cell + "/>")
    return (f'<svg class="heatmap" viewBox="0 0 {W} {H}" role="img">'
            + "".join(g) + "</svg>")


_MIX_SEG = (("direct", "업무 · 직접(To)", "s-direct"),
            ("cc", "업무 · 참조(CC)", "s-cc"),
            ("notice", "공지 · 대량발송", "s-notice"),
            ("spam", "스팸 · 자동발송", "s-spam"))


def svg_mixbar(mix: dict) -> str:
    """§4 받은 메일 구성 — 단일 누적 가로 막대 + 범례(구간별 통수·%)."""
    total = mix["total"]
    if not total:
        return '<p class="empty">받은 메일이 없습니다.</p>'
    seg = mix["seg"]
    W, BH = 640, 30
    g = [f'<svg class="mixbar" viewBox="0 0 {W} {BH}" role="img" '
         f'preserveAspectRatio="none">']
    x = 0.0
    for key, _lbl, cls in _MIX_SEG:
        n = seg[key]
        if not n:
            continue
        w = W * n / total
        g.append(f'<rect class="{cls}" x="{x:.1f}" y="0" width="{max(w - 1.5, 0.5):.1f}" '
                 f'height="{BH}" tabindex="0" data-tip="'
                 + html.escape(f'{_lbl} · {n}통 ({n / total * 100:.0f}%)', quote=True)
                 + '"/>')
        x += w
    g.append("</svg>")
    legend = ['<div class="mixlegend">']
    for key, lbl, cls in _MIX_SEG:
        n = seg[key]
        legend.append(
            f'<span class="mitem"><span class="mkey {cls}"></span>'
            f'{lbl} <b>{n}</b> <span class="mpct">{n / total * 100:.0f}%</span></span>')
    legend.append("</div>")
    return "".join(g) + "".join(legend)


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
  /* 차트 전용(격자·축·노드·델타·공지 세그먼트) — 라이트 기본.
     심각도 칩(--st-crit/good/serious, --chip-*-ink)은 2026-08-02 삭제 —
     2026-07-13 개편이 '증발한 요청/조용해진 사람'을 지우며 마크업만 걷고
     CSS 를 남긴 잔재였다(3주간 사용 0). */
  --grid:#e8e8ec; --base:#c9c9cf; --node:#eaeef9;
  --good:#006300; --bad:#c22a2a;
  --st-warn:#fab219;
}
:root[data-theme='dark']{
  --grid:#2c3238; --base:#454c53; --node:#282d31;
  --good:#6cc46c; --bad:#e0705f;
  --st-warn:#e0b24a;
}
*{margin:0;padding:0;box-sizing:border-box}
body{-webkit-font-smoothing:antialiased}
header.hero{margin-bottom:20px}
h1{font-weight:800;letter-spacing:-.02em;text-wrap:balance}
.meta{color:var(--muted);font-size:13px;margin-top:6px}
.meta b{color:var(--ink2);font-weight:600}
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
.tile .n{font-size:12.5px;color:var(--muted);margin-top:2px}
/* 창 표기 — 절마다 창이 다르므로 제목 옆에 반드시 적는다(전역 선택기 폐지) */
.win{font-size:12px;font-weight:600;color:var(--muted);margin-left:8px;
  background:var(--plane);border-radius:999px;padding:3px 9px;
  vertical-align:2px;letter-spacing:0}
.tile .lbl .win{margin-left:6px;padding:2px 7px;vertical-align:1px}
/* §1 최근 주 수치 — 없앤 볼륨 타일이 갖고 있던 값을 여기로 옮겼다 */
.nowline{display:flex;gap:20px;flex-wrap:wrap;margin:10px 0 14px}
.nowitem{display:flex;align-items:baseline;gap:8px;font-size:14.5px}
.nowitem .delta{font-size:12.5px;font-weight:600}
.nowitem .delta.flat{color:var(--muted)}
.spark{display:block;width:100px;height:30px;margin-top:8px}
section.card{background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:24px 26px;margin-bottom:18px}
h2{font-size:19px;font-weight:700;letter-spacing:-.01em}
h2 .no{color:var(--brand);margin-right:8px;font-weight:800}
.desc{font-size:13.5px;color:var(--muted);margin:3px 0 16px}
svg.linechart{width:100%;height:auto;display:block}
svg:focus{outline:2px solid var(--mark);outline-offset:3px;border-radius:8px}
.grid{stroke:var(--grid);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.line,.sparkline{fill:none;stroke-width:2;stroke-linejoin:round;
  stroke-linecap:round}
.line{stroke:var(--mark)} .line.alt{stroke:var(--mark2)} .sparkline{stroke:var(--base)}
.dot{fill:var(--mark)} .dot.alt{fill:var(--mark2)} .ring{fill:var(--surface)}
/* §2 히트맵 */
svg.heatmap{width:100%;height:auto;display:block;max-width:600px}
/* §5 커버리지 격자 — 위 규칙을 물려 쓰면 12열(286px)이 600px 로 늘어나 칸이
   히트맵의 2.1배가 된다. width/height 속성으로 1:1 렌더하고 좁은 화면에서만
   줄인다(height:auto 가 없으면 폭만 줄고 높이가 남는다). */
svg.covgrid{display:block;max-width:100%;height:auto}
.hmtick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.hmcell{stroke:var(--surface);stroke-width:1}
.hmcell[tabindex]{cursor:default}
.hmcell[tabindex]:hover,.hmcell:focus{stroke:var(--ink2);stroke-width:1.5;outline:none}
/* §4 받은 메일 구성 누적 막대 */
svg.mixbar{width:100%;height:30px;display:block;border-radius:7px;overflow:hidden}
.s-direct{fill:var(--mark)} .s-cc{fill:var(--mark2)}
.s-notice{fill:var(--st-warn)} .s-spam{fill:var(--base)}
.mixbar rect{cursor:default}
.mixlegend{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:14px;font-size:13px;color:var(--ink2)}
.mitem{display:inline-flex;align-items:center;gap:6px}
.mitem b{font-variant-numeric:tabular-nums} .mpct{color:var(--muted);font-size:12px}
.mkey{width:12px;height:12px;border-radius:3px;display:inline-block}
.mkey.s-direct{background:var(--mark)} .mkey.s-cc{background:var(--mark2)}
.mkey.s-notice{background:var(--st-warn)} .mkey.s-spam{background:var(--base)}
.legend{display:flex;gap:18px;align-items:center;font-size:12.5px;
  color:var(--ink2);margin-bottom:4px}
.legend .key{width:12px;height:12px;border-radius:3px;background:var(--mark);
  display:inline-block;margin-right:6px;vertical-align:-1px}
.legend .key.alt{background:var(--mark2)}
h3{font-size:14px;font-weight:700;color:var(--ink2);margin-bottom:10px;
  display:flex;align-items:center;gap:7px}
h3 .key{width:12px;height:12px;border-radius:3px;background:var(--mark);
  display:inline-block}
h3 .key.alt{background:var(--mark2)}
.xhair{stroke:var(--base);stroke-width:1}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{color:var(--muted);font-weight:600;text-align:left;font-size:12.5px;
  padding:7px 10px;border-bottom:1px solid var(--grid)}
td{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.empty{color:var(--muted);font-size:14px;padding:14px 0}
details.tbl{margin-top:12px}
details.tbl summary{font-size:12.5px;color:var(--muted);cursor:pointer;
  user-select:none}
details.tbl summary:hover{color:var(--ink2)}
details.tbl table{margin-top:8px}
.note{font-size:12.5px;color:var(--muted);background:var(--plane);
  border-radius:10px;padding:10px 14px;margin-bottom:16px}
td a,.hero a{color:var(--mark);text-decoration:none}
td a:hover{text-decoration:underline}
.duo3{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:720px){.duo3{grid-template-columns:1fr}}
.hmwrap h3{justify-content:flex-start}
/* §5 기억 커버리지 — 농도가 아니라 2단계(쌓임/안 쌓임). 질문이 '얼마나'가
   아니라 '쌓였나'라서다. 주말은 테두리로만 구분(빈 칸이어도 문제가 아니다) */
.covcell{fill:var(--node)}
.covcell.on{fill:var(--mark)}
.covcell.weekend{fill:none;stroke:var(--node);stroke-width:1}
.covcell:focus-visible{outline:2px solid var(--mark);outline-offset:1px}
.memline{display:flex;gap:22px;flex-wrap:wrap;margin:10px 0 14px;font-size:14.5px}
.memitem .dim{color:var(--muted);font-size:13px}
.memhint{font-size:12.5px;color:var(--muted);margin-top:12px;line-height:1.6}
.memhint code{background:var(--plane);border-radius:5px;padding:1px 5px;
  font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--ink);
  color:var(--surface);font-size:12.5px;padding:7px 11px;border-radius:9px;
  visibility:hidden;z-index:9;max-width:320px;line-height:1.45;
  box-shadow:0 4px 14px rgba(0,0,0,.18)}
#tip b{font-size:13.5px;font-variant-numeric:tabular-nums}
"""

# /report.js 로 서빙 — 웹 CSP(script-src 'self')가 인라인 스크립트를 막으므로
REPORT_JS = """
(function(){
  /* nav 뒤로(←) — 통계는 전폭 페이지(app.js 미로드)라 여기서도 배선.
     tip 조기 return 보다 먼저 등록해야 한다. */
  document.addEventListener('click', function(e){
    if(e.target.closest && e.target.closest('.navback')) history.back();
  });
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
      var v = cfg.series[i], rows = [];
      if(cfg.series2){                 // 2계열(볼륨 추세): 발신·수신 함께
        var v2 = cfg.series2[i];
        rows = [{b:(cfg.leg + ' ' + (v==null?'—':v + cfg.unit))},
                {b:(cfg.leg2 + ' ' + (v2==null?'—':v2 + cfg.unit))},
                {t:cfg.labels[i] + ' 주'}];
      } else {
        rows = [{b:(v==null?'—':v + cfg.unit)},{t:cfg.labels[i] + ' 주'}];
      }
      showTip(rows, cx, cy);
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


def _delta_html(recent, prior, unit: str, up_is_bad, vs: str) -> str:
    """up_is_bad=True/False 면 방향에 좋음(초록)/나쁨(빨강) 색, None 이면 중립.
    (볼륨은 증가가 좋다/나쁘다 판단이 없어 None — 방향만 표시)."""
    if recent is None or prior is None or prior == 0:
        return '<div class="delta flat">비교 기준선 축적 중</div>'
    pct = (recent - prior) / prior * 100
    if abs(pct) < 3:
        return f'<div class="delta flat">→ 보합 (vs {vs})</div>'
    arrow = "▲" if pct > 0 else "▼"
    if up_is_bad is None:
        cls = "flat"
    else:
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


def _stats_inner(inner: str, meta: str = "") -> str:
    """통계 콘텐츠 조각(제목·섹션)만 반환.

    상단 nav 셸(Minerva·홈·메일함…)과 보조 리소스(#tip, /report.js, report.CSS)는
    web 쪽 래퍼(web.render_stats_page → _page_wide)가 씌워 다른 메뉴와 통일한다.
    검토 기간 선택 바는 없앴다 — 창은 섹션마다 고정이고 각자 화면에 적는다
    (모듈 상단 TREND_WEEKS/RECENT_WEEKS 주석 참조).
    """
    return f"""<header class="hero">
<h1>통계 분석</h1>
{meta}
</header>
{inner}
"""


def render_stats(store, cfg) -> str:
    """웹 /stats 콘텐츠 — 전폭 단일 컬럼(좌/우 셸 미사용). nav 셸은 web 이 씌운다."""
    extra_me = set(a.lower() for a in getattr(cfg, "my_addresses", []) or [])
    # 별칭이 더 있으면 config.toml [report] extra_me 로 추가 (config.py 무수정)
    extra_me |= {str(a).lower() for a in (cfg.opt("report", "extra_me", default=[]) or [])}
    # 가장 긴 창으로 **한 번만** 읽고, 짧은 창을 쓰는 절은 여기서 잘라 쓴다.
    # 기간 선택기 시절에는 누를 때마다 전량 재적재였다 — 지금이 더 싸다.
    d = load(store.db, TREND_WEEKS, extra_me)
    if d is None:
        return _stats_inner('<p class="empty">메일이 없습니다 — 먼저 동기화하세요.</p>')
    return _stats_inner(_body(d, cfg, store), _meta_line(d))


def _meta_line(d: dict) -> str:
    period = f"{d['weeks'][0].strftime('%Y.%m.%d')} – {d['asof'].strftime('%Y.%m.%d')}"
    return (f'<div class="meta">기간 <b>{period}</b> ({d["n_weeks"]}주)'
            f' · 메일 {len(d["msgs"])}건'
            f' · 생성 {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>')


def _recent_since(d: dict) -> str:
    """RECENT_WEEKS 창의 시작 타임스탬프 — 짧은 창을 쓰는 절의 컷오프."""
    return (d["asof"] - timedelta(weeks=RECENT_WEEKS)).isoformat()


def _win(label: str) -> str:
    """절 제목 옆 창 표기 — 절마다 창이 다르므로 화면에 적는 것이 계약이다."""
    return f'<span class="win">{label}</span>'


def _body(d: dict, cfg, store) -> str:
    labels = [f"{w.month}/{w.day}" for w in d["weeks"]]
    trend = sig_volume_trend(d)
    heat = sig_heatmap(d)
    my_pairs = _reply_pairs(d)
    their_pairs = _their_pairs(d)
    resp = sig_response(d, my_pairs, their_pairs)
    since = _recent_since(d)
    mix = sig_inbox_mix(d, cfg, since=since)
    ping = sig_pingpong(d, cfg, since=since)
    tw, rw = TREND_WEEKS, RECENT_WEEKS

    # ---- 타일: 응답 중앙값 나/상대. **둘은 짝이다** — 내 27시간이 느린 건지는
    # 상대 값이 있어야 판단된다. 하나만 두면 남은 하나가 해석 불가가 된다.
    def _resp_tile(lbl, med, n, pairs):
        lat = sig_latency(d, pairs)
        val = _fmt_h(med) if med is not None else "—"
        return (f'<div class="tile"><div class="lbl">{lbl} <span class="win">최근 {tw}주</span></div>'
                f'<div class="val">{val}</div>'
                f'<div class="n">표본 {n}건</div>'
                f'{_delta_html(lat["recent"], lat["prior"], "h", True, "이전 4주")}'
                f'{spark(lat["series"])}</div>')
    tiles = [
        _resp_tile("내 응답 중앙값", resp["mine"], resp["mine_n"], my_pairs),
        _resp_tile("상대 응답 중앙값", resp["theirs"], resp["theirs_n"], their_pairs),
    ]

    # ---- §1 볼륨 추세 (발신/수신 2계열) — 타일에 있던 최근 주 수치·델타를 흡수.
    # 스파크라인 두 개로는 두 계열이 각자 축을 써서 '벌어짐'이 안 보인다.
    tbl1 = _tbl(["주", "발신", "수신"],
                [[labels[i], str(trend["sent"][i]), str(trend["recv"][i])]
                 for i in range(d["n_weeks"])], {1, 2})
    def _now_line(lbl, recent, prior):
        val = f"{recent}통" if recent is not None else "—"
        return (f'<span class="nowitem"><b>{lbl} {val}</b>'
                f'{_delta_html(recent, prior, "통", None, "이전 평균")}</span>')
    sec1 = f"""<section class="card"><h2><span class="no">1</span>볼륨 추세
        {_win(f"최근 {tw}주")}</h2>
      <p class="desc">주별 내 발신·수신 통수 — 두 선의 벌어짐이 곧 부하의 방향(발신 급증 = 내가 밀어내는 중, 수신 급증 = 밀려오는 중).</p>
      <div class="nowline">
        {_now_line("최근 주 발신", trend["sent_recent"], trend["sent_prior"])}
        {_now_line("수신", trend["recv_recent"], trend["recv_prior"])}
      </div>
      <div class="legend"><span><span class="key"></span>발신</span>
        <span><span class="key alt"></span>수신</span></div>
      {svg_trend(trend["sent"], trend["recv"], labels)}
      <details class="tbl"><summary>표로 보기</summary>{tbl1}</details></section>"""

    # ---- §2 활동 히트맵 (요일×시간)
    sec2 = f"""<section class="card"><h2><span class="no">2</span>활동 히트맵
        {_win(f"최근 {tw}주")}</h2>
      <p class="desc">요일×시간대별 메일 통수 — 색이 진할수록 많음. 언제 몰리는지·야간과 주말 셀이 한눈에 보입니다.</p>
      <div class="duo3">
        <div class="hmwrap"><h3><span class="key"></span>내 발신</h3>
          {svg_heatmap(heat["sent"], heat["sent_max"], alt=False)}</div>
        <div class="hmwrap"><h3><span class="key alt"></span>수신</h3>
          {svg_heatmap(heat["recv"], heat["recv_max"], alt=True)}</div>
      </div></section>"""

    # ---- §3 받은 메일 구성 (짧은 창 — 노이즈 규칙을 고친 효과를 봐야 한다)
    sec3 = f"""<section class="card"><h2><span class="no">3</span>받은 메일 구성
        {_win(f"최근 {rw}주")}</h2>
      <p class="desc">받은 메일 {mix["total"]}건의 구성 — '직접(To)'은 내가 처리해야 할 것, '참조(CC)'·공지·스팸은 신호 대 소음.</p>
      {svg_mixbar(mix)}</section>"""

    # ---- §4 왕복 많은 논의 (짧은 창 — 살아 있는 논의여야 조언이 성립)
    if ping:
        prows = [[
            f'<a href="/thread/{p["thread_id"]}">{html.escape(p["subject"])}</a>',
            html.escape(p["who"]), p["turns"], p["msgs"]]
            for p in ping]
        sec4_body = _tbl(["제목", "상대", "왕복", "통수"], prows, {2, 3})
    else:
        sec4_body = '<p class="empty">왕복이 잦은 스레드가 없습니다.</p>'
    sec4 = f"""<section class="card"><h2><span class="no">4</span>왕복 많은 논의
        {_win(f"최근 {rw}주")}</h2>
      <p class="desc">발신 방향이 여러 번 바뀐 스레드 — 메일로 결론이 안 나는 논의는 회의 전환 후보입니다.</p>
      {sec4_body}</section>"""

    # ---- §5 기억 커버리지 — 지식이 안 쌓인 구간
    mem = sig_memory(store, cfg, d)
    if mem["any"]:
        sec5_body = f"""<div class="memline">
        <span class="memitem"><b>지식이 쌓인 날 {mem["days_on"]}</b>
          <span class="dim">/ {mem["days_total"]}일</span></span>
        <span class="memitem"><b>저장된 지식 {mem["knowledge"]}건</b></span>
      </div>
      {svg_coverage(mem["days"], d["n_weeks"])}
      <p class="memhint">빈 칸의 메일은 수확에서 <b>영구히</b> 빠집니다 —
        소급 상한이 <code>ai.summary_max_days</code>(기본 1일)라서입니다.
        구멍이 잦으면 그 값을 2~3 으로 올리거나, 회고를 자주 돌립니다.</p>"""
    else:
        # 실패와 미실행을 가르는 이 저장소의 관례(review.EXEC_EMPTY)를 따른다 —
        # AI 를 한 번도 안 돌린 사람에게 0/84 격자를 들이대는 것은 잔소리다.
        sec5_body = ('<p class="empty">AI 회고를 아직 돌리지 않아 이 기간의 '
                     '지식이 비어 있습니다 — 기억 › 일간 회고에서 <b>AI 회고</b>를 '
                     '누르면 그날부터 쌓입니다.</p>')
    sec5 = f"""<section class="card"><h2><span class="no">5</span>기억 커버리지
        {_win(f"최근 {tw}주")}</h2>
      <p class="desc">그날 회고에서 AI 계층(수확·하루 요약)이 실제로 돌았는지 — 채운 칸이 지식이 쌓인 날입니다. 파일이 있는 것만으로는 세지 않습니다(자동 생성분은 AI 를 부르지 않습니다).</p>
      {sec5_body}</section>"""

    note = (f'<div class="note">데이터 축적 {d["n_weeks"]}주차 — 추세·델타는 6~8주부터 안정화됩니다.</div>'
            if d["n_weeks"] < 6 else "")

    return (note + f'<div class="kpis">{"".join(tiles)}</div>'
            + sec1 + sec2 + sec3 + sec4 + sec5)
