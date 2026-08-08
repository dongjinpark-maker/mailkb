"""Minerva — mailkb 웹 UI (stdlib http.server 기반 로컬 앱, localhost 전용).

서비스 표시명은 Minerva, 코드/폴더/명령은 mailkb 그대로 (표시명만 분리).

브라우저가 한글·HTML 렌더를 담당 → curses(windows-curses)의 CJK 한계를 우회.
표시용 HTML 은 store 에 이미 정제되어 저장됨(clean.sanitize_html) + 여기 CSP 로 이중 방어.

화면: 분석(첫 화면 — 근거 달린 질의응답) · 메일함 · 스레드 · 인물 · 기억 · 통계.
      검색은 헤더 입력창, 설정은 헤더 ⚙ (2026-07-26 홈=분석 개편).
조작(POST): 분석 잡 생성 · 동기화 · 플래그/숨김/신호 해제 · 노트 · Outlook 열기 ·
      첨부 · 장기기억 반영/유보 · 설정 저장.
"""

from __future__ import annotations

import html as _html
import json
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import (__version__, actions, config as cfgmod, promises, report,
               review, search as search_mod, terms, weekly)
from .clean import (PRESERVED_MARK, QFOLD_CLOSE, QFOLD_OPEN, add_dark_colors,
                    hide_image_signatures, parse_preserved, preserved_label,
                    qfold_open, retitle_qfold, strip_preserved)
from .store import Store, image_cutoff_for

# 백그라운드 잡 상태 표준형 — 모든 잡(회고·검색·동기화·주간·분석·인물 요약)이
# 같은 모양을 쓰고 같은 대기 카드로 그려진다. 스트리밍 필드는 ai_run 이 흘리는
# 수신 상태이고(phase/recv/model/retry/tail/failed/last_ev), cancel 은 실행 중일
# 때만 threading.Event — 중지 버튼이 set 하면 스트리밍 루프가 0.5초 안에
# 프로세스를 죽이고 AICancelled 를 올린다. stream 은 그 즉시성이 성립하는
# 백엔드인지(_arm_job_backend 가 판정). AI 아닌 잡도 같은 형태를 쓰되 이벤트가
# 없어 슬롯이 비고, CSS `.waitslot:empty` 가 그 줄을 숨긴다.
_JOB_STREAM = {"phase": "", "recv": 0, "model": "", "retry": "", "tail": "",
               "failed": "", "fatal": False, "last_ev": 0.0, "stream": False,
               "cancel": None,
               "started": 0.0}


# 진행 중 화면은 JS 꺼짐 폴백으로 meta refresh 자동 새로고침(전체 페이지 모드에서만).
# **새 백그라운드 잡을 만들면 여기 마커도 함께 넣는다** — 빠지면 JS-off 환경에서
# 그 화면만 영영 안 넘어간다(주간 보고·분석이 실제로 그랬다).
_RUNNING_MARKERS = ("data-review-running", "data-aisearch-running",
                    "data-sync-running", "data-weekly-running",
                    "data-ask-running", "data-dossier-running")


def _new_job(**extra) -> dict:
    """잡 상태 dict 생성 — 공통 필드 + 잡 고유 필드."""
    job = {"running": False, "stage": "", "error": ""}
    job.update(_JOB_STREAM)
    job.update(extra)
    return job


def _job_start(job: dict, lock, **fields):
    """단일 슬롯 획득 → cancel Event 반환. 이미 실행 중이면 None.

    None 을 받은 호출부는 **사용자에게 시작되지 않았음을 알린다** — 조용히
    남의 잡 화면으로 보내면 자기 요청이 도는 줄 착각한다."""
    cancel = threading.Event()
    with lock:
        if job["running"]:
            return None
        job.update(_JOB_STREAM)        # 이전 실행의 수신 상태를 지운다
        job.update(running=True, error="", cancel=cancel,
                   started=time.time(), **fields)
    return cancel


# 데일리 생성 백그라운드 잡(단일) — 웹은 단일 스레드라 리뷰(수 초~수십 초)는 별 스레드로
# step: 진행 단계(1~total, 0=미상/비-AI) — 대기 화면 프로그레스 바 재료
_review_job = _new_job(msg="", step=0, total=4, date="", ai=False)
_review_lock = threading.Lock()

# 결정론 데일리 리뷰 자동 갱신(lazy-on-view) — 버튼 없이 오늘치를 배경 재생성한다.
# {날짜: 마지막 생성 기준선(MAX rowid)} — 새 메일로 rowid 가 늘면 다시 생성.
# 인메모리(재시작 시 리셋 → 그날 첫 조회에 1회 재생성, 무해). 결정론이라 비용 작음.
_auto_review_basis: dict = {}
_auto_lock = threading.Lock()

# AI 검색 백그라운드 잡(단일) — 번역·본문심사에 수십 초 걸려 요청 스레드에서 돌리면
# 서버(단일 스레드)가 그동안 멈춘다. 별 스레드로 돌리고 /aisearch/status 폴링으로
# 단계(방법 7)·잠정 결과(방법 8)를 흘린다. prelim=엔진 1차 후보, result=최종.
_aisearch_job = _new_job(query="", fresh=False, result=None, prelim=None)
_aisearch_lock = threading.Lock()

# 메일 동기화 백그라운드 잡(단일) — Outlook COM 수집은 수 초~수십 초. 요청 스레드에서
# 돌리면 그동안 UI 전체가 멈춘다(리뷰·AI검색은 이미 백그라운드인데 sync 만 인라인이었음).
# 별 스레드에서 자체 CoInitialize 로 COM 을 열고, /sync/status 폴링으로 완료를 알린다.
_sync_job = _new_job(msg="", n=0)
_sync_lock = threading.Lock()

# 주간 보고 백그라운드 잡(단일) — 원문 카드·토픽 서술·근거 검증으로 AI 최대 17콜.
# 요청 스레드에서 돌리면 단일 스레드 서버가 그동안 멈춘다. /weekly/status 폴링으로
# 진행("토픽 3/5 서술 중…")을 흘린다 — weekly 엔진이 내보내는 메시지 그대로.
_weekly_job = _new_job(weeks=1, date="")
_weekly_lock = threading.Lock()

# 질문하기 백그라운드 잡(단일) — AI 최대 12콜(조사·답변·검증·조건부 보정). 진행 문구는
# ask 엔진이 내보내는 것("조사 2라운드 — 검색 1회 · 정독 3통")을 그대로 흘린다.
_ask_job = _new_job(question="", parent=None, person="", token="",
                    mail=None, result=None)
_ask_lock = threading.Lock()

# 인물 요약(도시에) 백그라운드 잡(단일) — AI 1콜. 인물 화면 '요약 갱신' 버튼에서만
# 시작한다(2026-07-29 이전에는 일간 회고가 상위 6명을 배치로 갱신했다 — 하루 정리와
# 인물 카드 유지보수를 한 버튼에 묶지 않기 위해 분리).
_dossier_job = _new_job(addr="", name="", done_at=0.0)
_dossier_lock = threading.Lock()


def _q(s: str) -> str:
    return urllib.parse.quote(str(s)[:200])


# localhost 계열은 전부 동일 로컬로 취급 — 브라우저가 127.0.0.1 로 열고 폼이
# localhost 로 가는(또는 반대) 조합을 차단하지 않는다 (#17).
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def same_origin(origin: str | None, host: str | None) -> bool:
    """드라이브-바이 POST 차단 판정.

    - Origin 헤더 없음 → 허용 (구형 클라이언트/도구)
    - 리터럴 "null" → 허용. Referrer-Policy: no-referrer 나 Edge 앱 모드에서
      Origin 이 null 로 오는데, 로컬 1인 도구라 이를 막으면 정상 사용이 깨진다.
      (샌드박스 iframe 발 CSRF 방어 일부 양보 — 의도된 트레이드오프)
    - 그 외에는 host:port 일치, 또는 양쪽 다 로컬 호스트명(포트 일치)이면 허용.
    """
    if not origin:
        return True
    if origin.strip().lower() == "null":
        return True
    try:
        o = urllib.parse.urlsplit(origin)
        h = urllib.parse.urlsplit("//" + (host or ""))
        oh, op = (o.hostname or "").lower(), o.port
        hh, hp = (h.hostname or "").lower(), h.port
    except ValueError:          # 비정상 포트 등 — 닫힘(fail-closed)
        return False
    if (oh, op) == (hh, hp):
        return True
    return oh in _LOCAL_HOSTS and hh in _LOCAL_HOSTS and op == hp


def _blocked_html(host: str) -> str:
    """차단 안내 — 기술 용어 대신 왜/무엇을 설명 (#18)."""
    url = f"http://{host}/" if host else "Minerva 주소"
    return (
        "<h1>요청을 보낸 곳을 확인할 수 없습니다</h1>"
        "<p>이 요청은 Minerva 화면이 아닌 다른 페이지(또는 다른 주소로 연 "
        "Minerva)에서 왔습니다. 안전을 위해 처리하지 않았습니다.</p>"
        f"<p>브라우저에서 <a href='{esc(url)}'>{esc(url)}</a> 를 직접 열어 "
        "그 화면의 버튼으로 다시 시도하세요.</p>"
    )

# 이메일 HTML 이 정제를 뚫어도 원격 로드/스크립트를 막는 최후 방어선.
# 원격 이미지(추적 픽셀) 차단: img-src 'self' data: — 외부 http 이미지는 로드 안 됨.
# script-src/connect-src 'self': 앱 JS(/app.js)와 fetch 만 허용 — 인라인 스크립트는
# 여전히 차단되므로 메일 HTML 방어는 그대로다 ('unsafe-inline' 아님).
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; "
       "font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

_CSS = """
:root {
  color-scheme: light; --left-w: 380px;
  --bg:#fafafa; --surface:#ffffff; --surface-2:#f6f7f9; --surface-3:#eef1f4;
  --ink:#1a1a1a; --ink-2:#555555; --ink-3:#888888; --muted:#aaaaaa;
  --border:#e5e5e5; --border-2:#dddddd; --border-strong:#bbbbbb;
  --accent:#0b6bcb; --accent-strong:#0b4b8f; --accent-fg:#ffffff;
  --sel-bg:#eef6ff; --hover-bg:#f0f6ff; --splitter:#cfe3f7;
  --sel-ring:rgba(11,107,203,.35);
  --danger:#c0392b; --danger-bg:#fdecea;
  --ok:#2c5a2c; --ok-bg:#e7f1e7; --ok-border:#b7d7b7; --toast-bg:#2c5a2c;
  --accent2:#e67e22;
  --warn:#8a6d00; --warn-bg:#fff8e1; --warn-border:#ffe082;
  --sent-bg:#eef6ec; --sent-border:#cfe3cf; --sent-ink:#3f6b3f;
  --analysis-bg:#f0f4f8; --code-bg:#f2f2f2;
  /* 모양·서체 — 스킨이 갈아끼우는 축(색과 별개). classic 값은 지금 그대로다.
     여기 없는 미세 반경(2·4·5px 등)은 디테일이라 스킨 대상이 아니다. */
  --r-sm:6px; --r-md:8px; --r-lg:10px;
  --sans:system-ui, "Segoe UI", "Malgun Gothic", sans-serif;
  --mono:ui-monospace, Consolas, monospace;
  --shadow-card:none;        /* classic 은 카드에 그림자를 쓰지 않는다 */
  --shadow-pop:0 2px 10px rgba(0,0,0,.14);
}
:root[data-theme='dark'] {
  color-scheme: dark;
  --bg:#16181b; --surface:#212529; --surface-2:#1b1f22; --surface-3:#282d31;
  --ink:#f3f5f7; --ink-2:#ccd1d6; --ink-3:#a4aab1; --muted:#6b7178;
  --border:#333a40; --border-2:#3a4147; --border-strong:#4b535a;
  /* 강조 = 따뜻한 코랄 — 다크에서 파랑보다 눈이 편함(라이트는 파랑 유지) */
  --accent:#e8975a; --accent-strong:#f4b183; --accent-fg:#16181b;
  --sel-bg:#2e2317; --hover-bg:#35291b; --splitter:#5e472c;
  --sel-ring:rgba(232,151,90,.40);
  --danger:#e0705f; --danger-bg:#3a2320;
  --ok:#8ccf8c; --ok-bg:#1f2e1d; --ok-border:#35502f; --toast-bg:#2e6b2e;
  --accent2:#e6b356;   /* 별·개인 마커 = 골드 — 코랄 링크와 구분 */
  --warn:#d9bd62; --warn-bg:#332f13; --warn-border:#5a521f;
  --sent-bg:#1f2e1d; --sent-border:#35502f; --sent-ink:#9bd09b;
  --analysis-bg:#1c2733; --code-bg:#191d21;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { font-family: var(--sans);
       margin: 0; padding: 0; display: flex; flex-direction: column;
       line-height: 1.5; color: var(--ink); background: var(--bg); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
/* 공통 명령 버튼. 컴포넌트별 규칙보다 먼저 두어 hover가 전용 배경을 덮지 않는다. */
button, .btn { padding: 6px 12px; font: inherit; font-size: 14px;
    border: 1px solid var(--border-strong); border-radius: var(--r-sm);
    background: var(--surface); color: var(--ink); cursor: pointer;
    text-decoration: none; }
button:hover, .btn:hover { border-color: var(--accent); background: var(--hover-bg);
    color: var(--ink); text-decoration: none; }
button:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent);
    outline-offset: 2px; }
button:disabled, .btn[aria-disabled='true'] { opacity: .52; cursor: not-allowed;
    filter: none; }
.btn-primary { color: var(--accent-fg); background: var(--accent);
    border-color: var(--accent); font-weight: 600; }
.btn-primary:hover { color: var(--accent-fg); background: var(--accent-strong);
    border-color: var(--accent-strong); }
.btn-quiet { color: var(--accent); background: transparent; border-color: transparent; }
.btn-quiet:hover { color: var(--accent-strong); background: var(--surface-3);
    border-color: transparent; }
button.danger, .btn-danger { color: var(--danger); background: var(--surface);
    border-color: var(--danger); }
button.danger:hover, .btn-danger:hover { color: var(--danger);
    border-color: var(--danger); background: var(--danger-bg); }
.btn-caution { color: var(--warn); background: var(--surface);
    border-color: var(--warn-border); }
.btn-caution:hover { color: var(--warn); background: var(--warn-bg);
    border-color: var(--warn); }
header.top { display: flex; align-items: center; gap: 14px; flex: none;
             border-bottom: 2px solid var(--border-2); padding: 12px 20px 8px; }
header.top .brand { font-weight: 700; font-size: 18px; }
header.top nav { flex: 1; display: flex; align-items: center; }
header.top nav a { margin-right: 12px; font-size: 14px; }
header.top nav .navsearch { margin: 0 0 0 18px; display: flex; }
header.top nav .navsearch input { width: 230px; max-width: 32vw; padding: 5px 12px;
    font-size: 13px; border: 1px solid var(--border-strong); border-radius: 999px;
    background: var(--surface); color: var(--ink); }
header.top nav .navsearch input:focus { outline: none; border-color: var(--accent);
    width: 300px; }
header.top nav .navsync { margin-left: auto; display: flex; align-self: center; }
header.top nav .navsync button, header.top nav .navback, header.top nav a.gear {
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 36px; border: 0; background: transparent;
    cursor: pointer; font: inherit; font-size: 16px; color: var(--ink-2); padding: 0;
    border-radius: var(--r-sm); }
header.top nav .navsync button:hover, header.top nav .navback:hover,
header.top nav a.gear:hover {
    background: var(--surface-3); color: var(--accent); }
header.top nav .navback { margin-left: 6px; align-self: center; }
header.top nav a.gear { margin-left: 4px; margin-right: 0; font-size: 17px; }
/* 현재 위치한 메뉴 — 밑줄로 표시 */
header.top nav a.active { text-decoration: underline; text-underline-offset: 5px;
    text-decoration-thickness: 2px; font-weight: 700; color: var(--accent-strong); }
/* 설정 페이지 */
.setlist { margin: 8px 0 20px; }
.setrow { display: flex; justify-content: space-between; align-items: center;
    gap: 10px; padding: 7px 10px; margin: 3px 0; background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--r-md); }
.setrow .mono { font-family: var(--mono); font-size: 13px; }
.setrow form { margin: 0; }
table.settbl { border-collapse: collapse; margin: 6px 0 20px; font-size: 13.5px; }
table.settbl th, table.settbl td { text-align: left; padding: 4px 14px 4px 0;
    vertical-align: top; }
table.settbl th { color: var(--ink-2); font-weight: 600; }
table.settbl input, table.settbl select { font-size: 13px; padding: 2px 4px; }
.setadd { display: flex; gap: 6px; margin-top: 4px; }
.setadd input[type=text] { flex: 1; font-size: 13px; padding: 4px 6px; }
.setrow .setlabel { display: flex; flex-direction: column; gap: 1px; color: var(--ink-2); }
.setrow .setsub { color: var(--muted); font-size: 12px; font-weight: 400; }
.setrow .setval { color: var(--ink-2); font-size: 13px; text-align: right; }
.setrow .setval a { color: var(--accent); }
.settings h3 { font-size: 14px; margin: 14px 0 4px; color: var(--ink-2); }
/* 화면 테마 — Android 스타일 세그먼트 토글 (해/달 아이콘) */
.themepick { display: inline-flex; gap: 4px; margin: 8px 0 20px; padding: 4px;
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px; }
.themebtn { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    padding: 7px 16px; border: 0; border-radius: 999px; background: transparent;
    color: var(--ink-2); font-size: 13.5px; font-weight: 600; line-height: 1;
    transition: background .16s, color .16s, box-shadow .16s; }
.themebtn svg { width: 16px; height: 16px; flex: none; }
.themebtn:hover { color: var(--ink); }
.themebtn.active { background: var(--surface); color: var(--accent-strong);
    box-shadow: 0 1px 3px rgba(0,0,0,.16); }
.themebtn.active svg { color: var(--accent); }
.themebtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* Outlook 유사 좌/우 분할 — 패널별 독립 스크롤 (#14) */
#layout { flex: 1; display: flex; min-height: 0; }
#left { width: var(--left-w); min-width: 240px; max-width: 70vw; flex: none;
        overflow-y: auto; border-right: 1px solid var(--border-2); background: var(--surface-2); }
#left .inner { padding: 12px 16px 48px; }
#splitter { width: 6px; flex: none; cursor: col-resize; }
#splitter:hover, #splitter.drag { background: var(--splitter); }
#right { flex: 1; min-width: 0; overflow-y: auto; }
#right .inner { max-width: var(--read-w, 1200px); padding: 12px 20px 60px; }
.selected { outline: 2px solid var(--sel-ring); background: var(--sel-bg); }
.kbd { outline: 2px solid var(--accent); outline-offset: -1px; }
.kbdhint { color: var(--muted); font-size: 12px; }
/* 메일함·스레드 공통 필터 바: 탭(좌) */
.listtabs { display: flex; justify-content: space-between; align-items: baseline;
    gap: 10px; margin: 2px 0 8px; font-size: 13px; flex-wrap: wrap; }
.listtabs .ltabs a { color: var(--accent); } .listtabs .ltabs b { color: var(--ink); }
/* 필터 바 오른쪽 (i) 키보드 도움말 — 순수 CSS 호버/포커스 팝오버 */
.kbdhelp { position: relative; color: var(--ink-3); cursor: help; flex: none;
    font-size: 14px; line-height: 1; outline: none; }
.kbdhelp:hover, .kbdhelp:focus { color: var(--accent); }
.kbdpop { display: none; position: absolute; right: 0; top: 130%; z-index: 20;
    white-space: nowrap; background: var(--surface); color: var(--ink-2);
    border: 1px solid var(--border); border-radius: var(--r-md); padding: 8px 11px;
    font-size: 12.5px; line-height: 1.7; box-shadow: var(--shadow-pop);
    text-align: left; font-weight: 400; }
.kbdpop b { color: var(--ink); }
.kbdhelp:hover .kbdpop, .kbdhelp:focus .kbdpop, .kbdhelp:focus-within .kbdpop {
    display: block; }
.backlink { font-size: 13px; }
/* 플래그 버튼: 다른 버튼과 같은 박스(패딩·높이), 글리프만 조금 크게 */
button.iconbtn { font-size: 15px; padding: 6px 12px; }
button.flag { color: var(--muted); }               /* ⚐ 미표시(색 없음) */
button.flag.on { color: var(--danger); }            /* ⚑ 플래그(색 있음) */
/* 주소별 화면 헤더: 뒤로(좌) · 이름(가운데) · 발신자 차단(우) — 같은 높이 */
.personhead { display: flex; align-items: center; gap: 10px; margin: 2px 0 6px; }
.personhead .ptitle { flex: 1; text-align: center; font-weight: 700; font-size: 18px; }
.personhead .pright { flex: none; }
.personhead form { margin: 0; }
/* 내가 보낸 메일: 배경 구별 (메일함·주소별 메일 공통) */
.mrow.sent { background: var(--sent-bg); border-color: var(--sent-border); }
.mrow.sent .mfrom { font-weight: 400; color: var(--sent-ink); }
/* 메일 클라이언트식 목록 행 (메일함·스레드) */
/* 목록 날짜 그룹 헤더(오늘/어제/이번 주…) — 비 sticky(커서 outline·팝오버와 무간섭) */
.dghead { margin: 12px 2px 2px; font-size: 12px; font-weight: 600; color: var(--ink-3); }
.mrow { display: block; padding: 7px 10px; margin: 3px 0; border-radius: var(--r-md);
        background: var(--surface); border: 1px solid var(--border); color: var(--ink); }
.mrow:hover { border-color: var(--accent); text-decoration: none; }
.mtop { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.mfrom { font-weight: 600; font-size: 13.5px; overflow: hidden;
         white-space: nowrap; text-overflow: ellipsis; }
/* 읽은 메일(메일함)은 제목 볼드 해제 — 일반 메일 클라이언트 관례. 읽으면 자동. */
.mrow.read .mfrom { font-weight: 400; color: var(--ink-2); }
.mcnt { color: var(--ink-3); font-weight: 400; font-size: 12px; }
.mcnt.hot { color: var(--danger); font-weight: 700; }   /* 5일+ 논의 또는 3통+ */
.mdate { color: var(--ink-3); font-size: 12px; flex: none; }
.msubj { display: block; font-size: 13px; color: var(--ink-2); margin-top: 1px;
         overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
/* 관계 배지 — 할 말이 있을 때만 붙으므로 작고 조용하게. 상태 표식이 아니라 사실. */
.rbadge { display: inline-block; margin-left: 6px; font-size: 11px; line-height: 1.5;
          padding: 0 5px; border-radius: 3px; border: 1px solid var(--border);
          color: var(--ink-3); vertical-align: 1px; }
.rbadge.first { color: var(--accent2); border-color: var(--accent2); }
.more { text-align: center; padding: 10px 0 16px; color: var(--ink-3); font-size: 13px; }
#toast { position: fixed; bottom: 18px; right: 18px; z-index: 10;
         background: var(--toast-bg); color: #fff; padding: 10px 16px; border-radius: var(--r-md);
         box-shadow: 0 2px 10px rgba(0,0,0,.25); font-size: 14px; }
h1 { font-size: 20px; } h2 { font-size: 17px; margin-top: 22px; }
.cat { margin: 18px 0 6px; font-weight: 700; }
.item { padding: 7px 10px; margin: 4px 0; border-left: 3px solid var(--border-strong);
        background: var(--surface); border-radius: 0 6px 6px 0; }
.item.hot { border-left-color: var(--danger); }
.item.personal { border-left-color: var(--accent2); }
.item .who { color: var(--ink-2); } .item .day { color: var(--ink-3); font-size: 13px; }
.item .snip { color: var(--ink-2); font-size: 13px; display: block; margin-top: 2px; }
.star { color: var(--accent2); font-weight: 700; }
/* 인물 도시에 — 랜딩 목록 + 도시에 카드 */
.plist { display: flex; flex-direction: column; margin-top: 8px; }
.prow { display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; padding: 8px 10px; border-bottom: 1px solid var(--border);
    text-decoration: none; color: var(--ink); }
.prow:hover { background: var(--hover-bg); }
.prow .pnm { font-weight: 600; }
.prow .pmeta { color: var(--ink-3); font-size: 13px; display: flex; gap: 10px;
    align-items: baseline; white-space: nowrap; }
.prow .pago { color: var(--muted); }
.dcard { background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r-md); padding: 4px 16px 12px; margin: 12px 0; }
.dcard h2 { font-size: 15px; margin: 12px 0 6px;
    border-bottom: 1px solid var(--border); padding-bottom: 5px; }
.dcard ul { margin: 6px 0; padding-left: 18px; }
.dcard li { margin: 4px 0; line-height: 1.5; }
.dcard p { color: var(--ink-2); line-height: 1.6; }
.wordmap { padding: 2px 0; }
.wmgroup { display: grid; grid-template-columns: minmax(90px, 145px) 1fr;
    gap: 10px; align-items: start; padding: 8px 0;
    border-bottom: 1px solid var(--border); }
.wmgroup:last-of-type { border-bottom: 0; }
.wmlabel { color: var(--ink-2); font-size: 12.5px; font-weight: 600;
    overflow-wrap: anywhere; }
.wmterms { display: flex; flex-wrap: wrap; gap: 5px 6px; min-width: 0; }
.wmterm { display: inline-flex; align-items: baseline; gap: 4px;
    padding: 2px 7px; border: 1px solid var(--border); border-radius: 4px;
    background: var(--surface-2); color: var(--ink); font-size: 12.5px;
    line-height: 1.4; white-space: nowrap; }
.wmterm:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.wmterm .wmn { color: var(--ink-3); font-size: 11px; font-variant-numeric: tabular-nums; }
.wmrise { color: var(--accent-strong); }
.wmmentions { display: flex; flex-wrap: wrap; gap: 5px 10px; }
.wmmentions a { font-size: 12.5px; }
@media (max-width: 560px) {
    .wmgroup { grid-template-columns: 1fr; gap: 5px; }
}
.dcap { color: var(--ink-3); font-size: 12px; margin: 6px 0 0; }
/* 관계 수치 시각화 — 자족적(팔레트 토큰만, report.CSS 불필요) */
.relbal { display: flex; align-items: center; gap: 10px; margin: 10px 0 2px; }
.relbal .rlbl { font-size: 13px; color: var(--ink-2); white-space: nowrap;
    font-variant-numeric: tabular-nums; display: inline-flex; align-items: center; gap: 5px; }
.relbal .sw { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.relbal .sw.recv { background: var(--accent); }
.relbal .sw.sent { background: var(--accent2); }
.relbar { flex: 1; display: flex; height: 9px; border-radius: 999px; overflow: hidden;
    background: var(--surface-3); min-width: 60px; }
.relseg { height: 100%; }
.relseg.recv { background: var(--accent); }
.relseg.sent { background: var(--accent2); }
.rsblock { margin: 12px 0 2px; }
.rsrow { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
.rsrow .rsname { width: 46px; font-size: 12.5px; color: var(--ink-2); flex: none; }
.rstrack { flex: 1; height: 8px; border-radius: 999px; background: var(--surface-3);
    overflow: hidden; }
.rsfill { height: 100%; border-radius: 999px; background: var(--accent); }
.rsrow .rsval { width: 44px; font-size: 12.5px; color: var(--ink-2); text-align: right;
    flex: none; font-variant-numeric: tabular-nums; }
.relfoot { display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin-top: 12px; }
.relfoot .sparkwrap { display: inline-flex; align-items: center; gap: 8px; }
.relfoot .sparklbl { font-size: 12px; color: var(--ink-3); }
svg.rspark { display: block; height: 22px; width: 120px; }
.relfoot .rlast { font-size: 13px; color: var(--ink-3); white-space: nowrap;
    font-variant-numeric: tabular-nums; }
.dcard.aidoss { background: var(--sel-bg); border-color: var(--splitter);
    border-left: 3px solid var(--accent); }
.dcard.aidoss .aitag { font-size: 11px; font-weight: 600; color: var(--accent);
    border: 1px solid var(--splitter); border-radius: 4px; padding: 0 5px;
    vertical-align: 2px; }
.dcard .dsec { font-weight: 700; color: var(--ink-2); margin: 10px 0 2px;
    font-size: 12px; letter-spacing: .02em; }
.dcard .dsec:first-of-type { margin-top: 4px; }
.dcard .dclaim { color: var(--ink); line-height: 1.55; margin: 2px 0; }
.prow .prole { color: var(--ink-3); font-size: 12.5px; font-weight: 400;
    margin-left: 6px; }
.dim { color: var(--ink-3); }
.analysis { background: var(--analysis-bg); border-radius: var(--r-md); padding: 12px 14px; margin: 10px 0; }
.analysis .sig { color: var(--warn); } .analysis pre { margin: 4px 0; white-space: pre-wrap; }
/* 스레드 상세 sticky 헤더 — 제목이 스크롤을 따라온다. hookThreadHead 가
   센티널 이탈 시 .stuck(컴팩트 1줄 말줄임). 배경 필수 — 비치면 타임라인과 겹침. */
.threadhead { position: sticky; top: 0; z-index: 5; background: var(--bg);
    padding: 2px 0 4px; }
.threadhead.stuck { border-bottom: 1px solid var(--border); }
.threadhead.stuck h1 { font-size: 15px; margin: 6px 0 4px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.msg { border: 1px solid var(--border); border-radius: var(--r-md); margin: 12px 0; overflow: hidden;
    scroll-margin-top: 72px;  /* stuck 헤더 높이 — focusMsg·n/p 스크롤이 안 가리게 */
    transition: box-shadow .5s ease; }
/* 검색·목록에서 연 메일을 잠깐 강조(2.8s 후 JS 가 클래스 제거 → 트랜지션으로 페이드) */
.msg.focusmsg { box-shadow: 0 0 0 2px var(--accent); }
.msg.focusmsg .mhead { background: var(--sel-bg); }
.msg .mhead { background: var(--surface-3); padding: 6px 12px; font-size: 13px; color: var(--ink-2);
    display: flex; align-items: baseline; gap: 10px; }
.msg .mhead.sent { background: var(--ok-bg); }
.msg .mhead .mh-who { font-weight: 700; color: var(--ink); overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }
.msg .mhead .mh-when { margin-left: auto; flex: none; color: var(--ink-3); font-size: 12px; }
/* 메일별 AI 분석 진입(머리글 오른쪽 끝) — 저장 분석이 있으면 보기 링크+다시 */
.msg .mhead .mh-ai { flex: none; display: inline-flex; align-items: center;
    gap: 6px; margin-left: 10px; }
.msg .mhead .mh-ai form { margin: 0; display: inline-flex; }
.msg .mhead .mh-ai .aibtn.compact { padding: 1px 8px; font-size: 11.5px; }
/* 본문 글자 크기 — 설정(web.reading_font)이 --read-fs 로 주입. 미설정 시 폴백이
   현행(본문 16px 상속·pre 13px 모노)과 동일해 시각 변화 없음. 크롬(mhead·칩)은 고정. */
.msg .mbody { padding: 12px 14px; font-size: var(--read-fs, 16px); }
.msg .mbody pre { font-size: var(--read-fs, 13px); }
/* 메일 원본 HTML 은 인라인 font-size(pt)가 상속을 이긴다 — zoom 으로 블록째 비례
   확대(제목·본문 위계 유지). 16px 고정은 이중 확대(상속 확대 × zoom) 방지. */
.mailhtml { font-size: 16px; zoom: var(--read-zoom, 1); }
.msg .mbody img { max-width: 100%; }
.msg .mbody img[data-blocked-src] { min-width: 8px; min-height: 8px;
    outline: 1px dashed var(--border-strong); }
.msg .mbody table { border-collapse: collapse; }
.msg .mbody td, .msg .mbody th { border: 1px solid var(--border-strong); padding: 4px 8px; }
/* 조판용 표(스페이서)는 테두리를 그리지 않는다 — 알림 메일(Confluence 류)은 내용
   없는 표로 레이아웃을 잡는데, 위 규칙이 그 빈 칸까지 테두리 박스로 그렸다.
   판정을 cellpadding/cellspacing/border 로만 하는 이유: 작성자가 다는 role·class·id
   는 정제가 지운다(clean._ATTR_ALLOW) — 파이썬 쪽 판정 clean._is_layout_table 의
   세 갈래 중 CSS 가 표현할 수 있는 한 갈래다. role 은 정제가 보존하기 시작한
   메일(신규 수집분)에서만 잡힌다.
   `> * >` 의 * 는 브라우저가 넣는 <tbody> 자리다 — `table > tr` 로 쓰면 영원히
   매치되지 않는다(저장 문자열엔 tbody 가 없지만 DOM 엔 항상 있다).
   자식 결합자인 이유: 한 겹만 짚어야 레이아웃 표 **안에 든** 진짜 데이터 표의
   셀이 테두리를 지킨다(실측 구조: zeros > td > zeros > td > border="1").
   특이도 (0,5,3) > 기본 규칙 (0,2,1) 이라 순서에 의존하지 않는다. */
.msg .mbody table[role="presentation"] > * > tr > td,
.msg .mbody table[role="presentation"] > * > tr > th,
.msg .mbody table[cellpadding="0"][cellspacing="0"]:not([border]) > * > tr > td,
.msg .mbody table[cellpadding="0"][cellspacing="0"]:not([border]) > * > tr > th,
.msg .mbody table[cellpadding="0"][cellspacing="0"][border="0"] > * > tr > td,
.msg .mbody table[cellpadding="0"][cellspacing="0"][border="0"] > * > tr > th {
    border: 0; padding: 0; }
.msg .mbody blockquote { border-left: 3px solid var(--border-2); margin: 4px 0; padding-left: 10px;
    color: var(--ink-2); }
.imgnote { background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: var(--r-sm);
    padding: 4px 10px; font-size: 12px; color: var(--warn); margin-bottom: 8px; }
/* 다크 모드 메일 가독성 (2026-07-14): 메일 원본 HTML(.mailhtml)은 흰 배경 전제의
   인라인 색(검은 글씨·흰 블록·파란 링크)을 담고 있어 다크에서 안 보인다. 다크에서만
   그 색을 테마 색으로 평탄화 — 라이트는 원본 그대로 둔다. 의미 색상(빨간 배지 등)도
   무채색이 되지만 '안 보이는 것보다 읽히는 게 낫다'는 선택. 우리 콘텐츠(.md-rich·
   배너·데일리)는 .mailhtml 밖이라 무영향. */
:root[data-theme='dark'] .mailhtml,
:root[data-theme='dark'] .mailhtml * {
    color: var(--ink) !important;
    background-color: transparent !important;
    border-color: var(--border-strong) !important;
}
:root[data-theme='dark'] .mailhtml a { color: var(--accent) !important;
    text-decoration: underline; }
/* 작성자 강조색 되살리기 (2026-07-26) — clean.add_dark_colors 가 심어 둔 --dk.
   ① 색을 지닌 요소의 자손은 상속시킨다. 안 하면 <span color=red><b>긴급</b></span>
      에서 <b> 가 위 평탄화에 걸려 혼자 흰색이 된다(실제로 겪음). 링크는 제외 —
      작성자가 링크에 색을 줬어도 링크는 링크색이 낫다.
   ② 그다음 자기 색이 있는 요소를 그 색으로. 둘 다 (0,4,1) 로 특이도가 같아
      순서로 결정된다 — 자기 --dk 가 있으면 ②가 이기고, 없으면 ①의 상속이 남는다.
   라이트 테마에는 --dk 를 읽는 규칙이 없어 선언만 남고 무해하다. */
:root[data-theme='dark'] .mailhtml [style*="--dk"] :not(a) { color: inherit !important; }
:root[data-theme='dark'] .mailhtml :not(a)[style*="--dk"] { color: var(--dk) !important; }
/* 이미지·인용 표식은 평탄화에서 제외(원래 테마 색 유지) */
:root[data-theme='dark'] .mailhtml img { background: transparent; }
:root[data-theme='dark'] .mailhtml blockquote { border-left-color: var(--border-2) !important;
    color: var(--ink-2) !important; }
:root[data-theme='dark'] .mailhtml details.qfold > summary { color: var(--ink-3) !important; }
/* 이미지 서명 숨김 표식 — 꼬리 로고·명함 카드를 대체한 한 줄 */
.sighide { display: inline-block; font-size: 12px; color: var(--ink-3);
    background: var(--surface-2); border: 1px dashed var(--border); border-radius: var(--r-sm);
    padding: 3px 10px; margin: 6px 0; }
.sighide::before { content: "✂ "; }
:root[data-theme='dark'] .mailhtml .sighide { color: var(--ink-3) !important;
    background: var(--surface-2) !important; border-color: var(--border) !important; }
/* HTML 없는 본문(#21, 2026-07-13 반전): 기본 서식(md-rich), 버튼 누르면 저장
   텍스트(md-raw). 실사용(COM)에서 HTML 없는 본문 = 프룬/변환 산출물이라 raw 는
   원문이 아니다 — 서식이 원 의도에 가깝고, 텍스트는 검증용 토글로.
   .md-show 는 토글 무관 상시 서식(mid-join 접힘 내용 등). */
.md-toggle { font-size: 12px; padding: 2px 12px; margin: 0 0 10px; cursor: pointer;
    color: var(--accent); background: var(--surface); border: 1px solid var(--border-strong); border-radius: 12px; }
.md-toggle:hover { background: var(--hover-bg); border-color: var(--accent); }
.md-raw { display: none; }
.md-rich { display: block; }
.mthread.md-on .md-raw { display: block; }
.mthread.md-on .md-rich:not(.md-show) { display: none; }
.md-rich > :first-child { margin-top: 0; }
.md-rich h3, .md-rich h4, .md-rich h5, .md-rich h6 { margin: 12px 0 4px; font-size: 15px; }
.md-rich p { margin: 6px 0; }
.md-rich ul, .md-rich ol { margin: 4px 0; padding-left: 22px; }
.md-rich li { margin: 2px 0; }
.md-rich code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; font-size: 90%; }
.md-rich pre.md-code { background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--r-sm);
    padding: 10px 12px; overflow-x: auto; }
.md-rich pre.md-code code { background: none; padding: 0; }
.md-rich blockquote { border-left: 3px solid var(--border-2); margin: 6px 0; padding-left: 10px;
    color: var(--ink-2); }
.md-rich hr { border: none; border-top: 1px solid var(--border-2); margin: 12px 0; }
.md-rich del { color: var(--ink-3); }   /* 취소선(diff 삭제분) — 흐리게 */
.md-rich table.md-table { border-collapse: collapse; margin: 8px 0; }
.md-rich table.md-table th { background: var(--surface-3); }
.daily { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-md);
    padding: 4px 18px 16px; }
.daily h2 { font-size: 15px; margin: 20px 0 8px; padding-bottom: 5px;
    border-bottom: 1px solid var(--border); }
.daily ul { margin: 4px 0; padding-left: 20px; }
.daily ul ul { margin: 2px 0; }
.daily li { margin: 3px 0; line-height: 1.55; }
.daily p { margin: 6px 0; color: var(--ink-2); }
/* 데일리 재구성(하루 요약 카드·긴급도 칩·참고 접힘) */
.daily .dsum { background: var(--sel-bg); border: 1px solid var(--splitter);
    border-radius: var(--r-md); padding: 10px 14px; margin: 12px 0 4px;
    font-size: 14.5px; line-height: 1.7; }
.daily .dsum p { color: var(--ink); margin: 4px 0; }
.daily .pri { display: inline-block; font-size: 11px; font-weight: 700;
    border-radius: 4px; padding: 0 5px; line-height: 17px; vertical-align: 1px; }
.daily .pri.hi { color: #fff; background: #c0392b; }
.daily .pri.mid { color: var(--warn); background: var(--warn-bg);
    border: 1px solid var(--warn-border); line-height: 15px; }
.daily .pri.lo { color: var(--ink-3); background: var(--surface-3); }
.daily .star { color: var(--accent2); }
.daily .ddl { color: var(--warn); font-weight: 600; }
.daily .warnmark { color: var(--warn); }
.daily .snip { color: var(--ink-3); }
.daily .cont { color: var(--ink-3); font-size: 13px; }
.daily details.dref { margin-top: 18px; border-top: 1px solid var(--border);
    padding-top: 8px; }
.daily details.dref summary { cursor: pointer; color: var(--ink-3);
    font-weight: 600; font-size: 14px; }
.daily details.dref[open] summary { margin-bottom: 4px; }
form.search { margin: 10px 0; }
form.search input[type=text] { padding: 7px 10px; width: 60%; font-size: 15px;
    border: 1px solid var(--border-strong); border-radius: var(--r-sm); }
form.search button { padding: 7px 14px; font-size: 15px; }
.shint { color: var(--ink-3); font-size: 12.5px; margin: 2px 0 8px; }
.shint code { background: var(--surface); padding: 1px 5px; border-radius: 4px; }
details.adv { margin: 4px 0 12px; }
details.adv summary { cursor: pointer; color: var(--ink-2); font-size: 13px;
    width: max-content; }
details.adv .advbody { display: flex; flex-wrap: wrap; gap: 10px 14px;
    align-items: center; padding: 10px 2px 2px; font-size: 13px; }
details.adv label { color: var(--ink-2); }
details.adv input[type=text], details.adv select { padding: 4px 7px; font-size: 13px;
    border: 1px solid var(--border-strong); border-radius: 5px; }
.facets { margin: 8px 0 4px; display: flex; flex-wrap: wrap; gap: 6px; }
.facet { font-size: 12.5px; padding: 3px 9px; border: 1px solid var(--border);
    border-radius: 999px; color: var(--ink-2); background: var(--surface);
    text-decoration: none; }
.facet:hover { border-color: var(--border-strong); }
.facet b { color: var(--ink-3); font-weight: 600; margin-left: 3px; }
.snip { color: var(--ink-2); font-size: 13px; margin: 1px 0 0 2px; line-height: 1.5; }
.snip mark { background: rgba(232, 151, 90, .28); color: inherit;
    padding: 0 1px; border-radius: 2px; }
/* 검색으로 들어왔을 때 본문에서 그 낱말 — 결과 목록의 스니펫 강조와 같은 색이라야
   "이것 때문에 걸렸구나"가 이어진다. Range 등록이라 본문 마크업은 그대로다. */
::highlight(kw) { background: rgba(232, 151, 90, .40); color: inherit; }
.lowrel { color: var(--ink-3); font-size: 12px; margin: 14px 0 4px;
    border-top: 1px dashed var(--border); padding-top: 8px; }
/* 질문하기(ask) — 상태 배지는 의미색: 확인=녹 / 상충=호박 / 근거 부족=중립(오류 아님) */
.askstate { display: flex; align-items: center; gap: 9px; margin: 10px 0 12px;
    flex-wrap: wrap; }
.askbadge { font-size: 12.5px; font-weight: 700; padding: 3px 10px;
    border-radius: 999px; border: 1px solid transparent; }
.askbadge.ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok-border); }
.askbadge.warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn-border); }
.askbadge.thin { background: var(--surface-3); color: var(--ink-2); border-color: var(--border-2); }
/* 대화형 레이아웃 — 우측(#right)에 대화록 + 하단 고정 입력 */
.chat { display: flex; flex-direction: column; gap: 18px; padding-bottom: 12px; }
.chatq { display: flex; justify-content: flex-end; }
.chatq .bubble { background: var(--accent); color: #fff; border-radius: 14px 14px 4px 14px;
    padding: 9px 14px; max-width: 80%; font-size: 15px; line-height: 1.45;
    white-space: pre-wrap; }
.chata { max-width: 100%; }
.chatintro { padding: 8px 2px; }
.chatintro h2 { font-size: 20px; margin: 0 0 8px; }
/* 랜딩 '이어서 볼 것' — 홈(=분석)의 미니 대시보드(대화 없을 때만 보임) */
.chatnext { margin-top: 18px; padding: 10px 14px; background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--r-lg); font-size: 13.5px; }
.chatnext .nexthd { font-size: 11.5px; letter-spacing: .06em; font-weight: 700;
    color: var(--ink-3); text-transform: uppercase; margin-bottom: 6px; }
.chatnext .nextrow { padding: 2px 0; }
.chatnext a { text-decoration: none; }
.chatnext a:hover { text-decoration: underline; }
.chatbar { position: sticky; bottom: 0; display: flex; gap: 8px; padding: 12px 0 4px;
    margin-top: 8px; background: linear-gradient(transparent, var(--bg) 22%); }
.chatbar input[type=text] { flex: 1; font: inherit; font-size: 15px; padding: 10px 14px;
    border: 1px solid var(--border-strong); border-radius: 22px;
    background: var(--surface); color: var(--ink); }
.chatbar input[type=text]:focus { outline: 2px solid var(--sel-ring); outline-offset: 0; }
.chatbar button { font: inherit; font-size: 14px; padding: 0 18px; border: 0;
    border-radius: 22px; background: var(--accent); color: var(--accent-fg);
    cursor: pointer; font-weight: 600; }
.chatbar button:hover { background: var(--accent-strong); color: var(--accent-fg); }
.asklisthd { margin-bottom: 8px; }
/* 그 답이 못 본 메일 수 — 상태 배지보다 약하게. 판정을 뒤집는 게 아니라
   '다시 조사할지' 판단할 재료라 보조 색으로 둔다 */
.askstale { color: var(--ink-3); }
/* 분석 기준선 footer — 카드가 아니라 조용한 상태줄. 떠 있지 않고 목록 뒤에 붙어
   함께 스크롤한다(고정하면 마지막 대화를 가린다). 라이트·다크 모두 보조 텍스트 대비 */
.askbasis { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border);
    color: var(--ink-3); font-size: 12px; line-height: 1.55; }
.askbasis a { color: var(--ink-3); text-decoration: underline;
    text-underline-offset: 2px; }
.askbasis a:hover { color: var(--accent); }
.chatredo { font-size: 12.5px; margin: 10px 0 0; }
.asklaunch { display: inline; margin: 0; }
/* 대화 행 삭제 ✕ — hover 에만 노출(목록 소음 방지), 날짜와 안 겹치게 우측 여백 */
.askconv { position: relative; }
.askconv > .mrow { padding-right: 40px; }
.askdel { position: absolute; top: 50%; right: 4px; transform: translateY(-50%); margin: 0; }
.askdel button { border: 0; background: transparent; color: var(--ink-3); cursor: pointer;
    font-size: 13px; line-height: 1; padding: 0; width: 32px; height: 32px;
    border-radius: var(--r-sm); opacity: 0; }
.askconv:hover .askdel button, .askdel button:focus-visible { opacity: 1; }
.askdel button:hover { background: var(--danger-bg); color: var(--danger); }
@media (hover:none) { .askdel button { opacity: 1; } }
/* 이 답이 무엇을 근거로 했나 — 사이드바 기준선(.askbasis)과 다른 것이라
   이름을 나눈다. 모델이 아니라 코드가 만든다(자기가 뭘 안 봤는지는 코드만 안다). */
.askreach { font-size: 12px; color: var(--ink-3); margin-top: 14px; }
.askscope { font-size: 12.5px; color: var(--ink-3); margin-top: 12px; }
.askscope summary { cursor: pointer; color: var(--ink-2); }
.askscope ul { margin: 6px 0 0; padding-left: 18px; }
.askscope code { font-family: var(--mono); font-size: 12px;
    background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
/* 답변 문단 폭 — 일반 챗봇(ChatGPT·Claude ~768px) 수준. 100ch ≈ 775px,
   한글 ~48자/줄(가독 권장 상단). 근거 블록은 원래 제한 없음. */
.askans { font-size: 15.5px; max-width: 100ch; margin-bottom: 4px; }
.askref { font-size: 11.5px; text-decoration: none; color: var(--accent);
    background: var(--sel-bg); border: 1px solid var(--border-2); border-radius: 5px;
    padding: 1px 5px; margin-left: 3px; white-space: nowrap; }
.askref:hover { border-color: var(--accent); }
h3.asksec { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--ink-3); margin: 18px 0 8px; font-weight: 700; }
.askev { display: flex; flex-direction: column; gap: 10px; }
.askitem { border-left: 3px solid var(--border-2); padding: 2px 0 2px 12px; }
.askq { font-size: 14px; margin-top: 3px; }
/* 인용 부호(U+201C/U+201D)는 CSS 이스케이프라 백슬래시를 두 번 쓴다.
   한 번만 쓰면 파이썬이 8진 이스케이프로 먹어 제어문자 U+0081 + 문자 C 가 되고
   브라우저엔 두부가 뜬다. 이 파일의 CSS 는 raw 문자열이 아니다. */
.askq::before { content: "\\201C"; color: var(--ink-3); }
.askq::after { content: "\\201D"; color: var(--ink-3); }
/* 한 줄 결론 — 답을 읽지 않고도 스캔되게. 근거 있는 claim 이 있을 때만 나온다. */
.askhead { font-size: 18px; font-weight: 700; letter-spacing: -.01em;
    line-height: 1.45; margin: 2px 0 8px; max-width: 100ch; }
/* 인용 + 원문 앞뒤 문맥 — 문맥은 흐리게 해서 모델이 지목한 근거와 구분한다.
   문맥이 붙으면 여는/닫는 인용부호는 안 쓴다(범위가 인용이 아니라 발췌 전체다). */
.askq.ctx::before, .askq.ctx::after { content: none; }
.askq .qctx { color: var(--ink-3); }
.askq .qhit { color: var(--ink); background: var(--sel-bg); border-radius: 3px;
    padding: 0 2px; }
.askclash { display: flex; gap: 12px; flex-wrap: wrap; }
.askside { flex: 1 1 240px; border: 1px solid var(--border-2); border-radius: var(--r-md);
    padding: 11px 13px; background: var(--surface-2); }
.askside.win { border-color: var(--warn-border); background: var(--warn-bg); }
.asklabel { font-size: 11.5px; letter-spacing: .04em; color: var(--ink-3);
    font-weight: 700; margin-bottom: 4px; }
.askval { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
/* AI 검색 */
.aibtn { display: inline-flex; align-items: center; gap: 6px; margin: 0;
    padding: 7px 14px; font-size: 13.5px; font-weight: 600; text-decoration: none;
    color: var(--accent-fg); background: var(--accent); border: 1px solid var(--accent);
    border-radius: 999px;
    font-family: inherit; cursor: pointer; }
.aibtn::before { content: "\\2726"; font-size: 14px; line-height: 1; }
.aibtn:hover { color: var(--accent-fg); background: var(--accent-strong);
    border-color: var(--accent-strong); filter: none; text-decoration: none; }
.aibtn.ghost { background: var(--surface); color: var(--accent);
    border: 1px solid var(--border-strong); font-weight: 500; }
.aibtn.ghost:hover { background: var(--hover-bg); color: var(--accent-strong);
    border-color: var(--accent); }
.aibtn.compact { padding: 3px 9px; font-size: 12.5px; }
.askrow { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.aiq { color: var(--ink-3); font-weight: 400; font-size: 16px; }
.aidsl { margin: 6px 0; font-size: 13px; color: var(--ink-2); display: flex;
    flex-wrap: wrap; align-items: center; gap: 8px; }
.aidsl code { background: var(--surface); padding: 2px 8px; border-radius: var(--r-sm);
    border: 1px solid var(--border); color: var(--ink); }
.aiedit { font-size: 12.5px; color: var(--accent); text-decoration: none; }
.aiexp { color: var(--ink-3); font-size: 12.5px; margin: 4px 0; }
.ainote { color: var(--ink-3); font-size: 13px; margin: 2px 0 10px; }
.aihead { margin: 12px 0 6px; font-weight: 600; }
.aicards { list-style: none; margin: 0; padding: 0; counter-reset: ai; }
.aicards .aicard { counter-increment: ai; position: relative; padding: 10px 12px 10px 40px;
    margin: 6px 0; background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r-lg); }
.aicards .aicard::before { content: counter(ai); position: absolute; left: 12px; top: 11px;
    width: 20px; height: 20px; border-radius: 50%; background: var(--accent);
    color: var(--accent-fg);
    font-size: 12px; font-weight: 700; display: flex; align-items: center;
    justify-content: center; }
.aicard .aititle { font-weight: 600; text-decoration: none; color: var(--accent); }
.aicard .aimeta { color: var(--ink-3); font-size: 12.5px; margin-top: 2px; }
.aicard .aireason { color: var(--ink-2); font-size: 13px; margin: 5px 0 0;
    line-height: 1.5; }
.aiothers { margin: 12px 0; }
.aiothers summary { cursor: pointer; color: var(--ink-2); font-size: 13px;
    width: max-content; }
.aiothers .aicard::before { background: var(--muted); }
.aifoot { color: var(--ink-3); font-size: 12px; margin-top: 16px;
    border-top: 1px solid var(--border); padding-top: 8px; }
.aifoot a, .aiothers a { color: var(--accent); }
.aifail { background: rgba(232,151,90,.14); border: 1px solid var(--border);
    color: var(--ink-2); font-size: 13px; padding: 8px 12px; border-radius: var(--r-md);
    margin: 6px 0 12px; }
.waitcard .spin { width: 22px; height: 22px; flex: none;
    border: 3px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: aispin .8s linear infinite; }
.aiwaitmsg { font-size: 15px; font-weight: 600; color: var(--ink); }
.aiwaitsub { font-size: 13px; color: var(--ink-3); margin-top: 5px;
    line-height: 1.55; max-width: 460px; }
.aiwaittime { font-size: 12.5px; color: var(--muted); margin-top: 9px;
    font-variant-numeric: tabular-nums; }
@keyframes aispin { to { transform: rotate(360deg); } }
.aiprelim { margin: 14px 0 6px; font-size: 12.5px; }
.aicards.prelim { opacity: .62; }        /* 잠정 결과 — 본문 확정 전이라 흐리게 */
.empty { color: var(--ink-3); padding: 20px 0; }
.digest { padding: 4px 10px; margin: 3px 0; border-left: 2px solid var(--border);
    background: var(--surface); font-size: 14px; }
.flash { background: var(--ok-bg); border: 1px solid var(--ok-border); border-radius: var(--r-sm);
    padding: 8px 12px; margin: 0 0 12px; color: var(--ok); }
.actions { margin: 10px 0 16px; display: flex; flex-wrap: wrap; gap: 8px;
    align-items: center; }
.actions form { display: inline; margin: 0; }
details { margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; font-size: 15px; padding: 4px 0; color: var(--ink-2); }
input, select, textarea { background: var(--surface); color: var(--ink); }
/* 결정 원장: 스레드 상세 '결정 기록' 폼 + 검토 큐 버튼 */
.actions details.recdec { margin: 0; }
.actions details.recdec > summary { padding: 6px 12px; font-size: 14px;
    font-weight: 400; color: var(--ink); background: var(--surface);
    border: 1px solid var(--border-strong); border-radius: var(--r-sm); list-style: none; }
.actions details.recdec[open] > summary { border-color: var(--accent); }
/* 펼치면 라벨 '장기기억' → '✕ 닫기' (중복 라벨 제거, 접기 유지) */
.actions details.recdec .xcl { display: none; }
.actions details.recdec[open] .lbl { display: none; }
.actions details.recdec[open] .xcl { display: inline; color: var(--ink-3); }
.recdec form { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.recdec input[type=text] { padding: 6px 8px; font-size: 13px;
    border: 1px solid var(--border-strong); border-radius: var(--r-sm); }
.recdec input[name=title] { width: 320px; }
.recdec input[name=rationale] { width: 240px; }
.recdec input[name=decider] { width: 110px; }
/* 리포트 본문의 구분선·블록인용 — 마크다운 `---` 과 `> ` 가 예전엔 리터럴로
   찍혔다(주간 머리의 AI 인증 만료 안내가 `&gt; ⚠ …` 로 보였다). */
.daily hr { border: 0; border-top: 1px solid var(--border); margin: 18px 0; }
.daily blockquote { margin: 10px 0; padding: 8px 12px; border-left: 3px solid var(--warn);
    background: var(--surface-2); border-radius: 0 6px 6px 0; }
.daily blockquote p { margin: 0; }
/* 리포트 항목 '처리함' — 항목 줄 끝에 얹히는 작은 버튼. 평소엔 흐리게 두고
   그 줄에 마우스를 올렸을 때만 또렷해진다(읽는 데 방해되지 않게). */
.donebtn { display: inline; margin-left: 8px; }
.donebtn button { font-size: 11.5px; padding: 1px 8px; background: var(--surface); }
/* 리포트 본문의 버튼만 평소 흐리게. opacity 를 쓰면 테두리와 :focus-visible 링까지
   흐려지고 되돌리기 버튼(details.doneundo)도 같이 안 보인다 — 색으로만 낮춘다. */
.daily .donebtn button { color: var(--ink-3); border-color: var(--border); }
.daily li:hover > .donebtn button, .daily .donebtn button:hover,
.daily .donebtn button:focus-visible { color: var(--ink-2); }
details.doneundo { margin: 18px 0 4px; }
details.doneundo > summary { cursor: pointer; font-size: 12.5px; }
details.doneundo ul { margin: 6px 0 0; padding-left: 18px; }
details.doneundo li { font-size: 12.5px; margin: 3px 0; }
/* 스킨이 카드에 깊이를 줄 수 있게 하는 고리 하나. classic 은 --shadow-card:none
   이라 무해하다 — box-shadow 는 상속되지 않고 초깃값이 none 이라, 선언만 늘고
   그려지는 결과는 지금과 같다. .msg.focusmsg 의 포커스 링은 특이도가 높아 이긴다. */
.dcard, .msg, .analysis, .daily .dsum, .chatnext, .aicards .aicard,
.waitcard, .askside, .aifail { box-shadow: var(--shadow-card); }
.decbtns { display: flex; gap: 6px; margin-top: 5px; align-items: baseline;
    flex-wrap: wrap; }
.decbtns form { margin: 0; display: inline; }
.decbtns button { font-size: 12.5px; padding: 3px 10px; }
.decbtns details.decedit { margin: 0; }
.decbtns details.decedit > summary { font-size: 12.5px; font-weight: 400;
    padding: 3px 4px; }
.decedit form { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.decedit input[type=text] { padding: 4px 6px; font-size: 13px;
    border: 1px solid var(--border-strong); border-radius: var(--r-sm); width: 260px; }
/* 이미지 보존 기간 경과 마커(프룬 산출) · 메일 내 중복 이미지 생략 표시 */
.imgstrip { background: var(--surface-3); border: 1px dashed var(--border-strong);
    border-radius: var(--r-sm); padding: 5px 10px; font-size: 12.5px; color: var(--ink-3);
    margin-bottom: 8px; }
/* mid-join 보존 인용 접힘 — HTML 층(store 저장분)과 텍스트 층(렌더 시 변환) 공용 */
.mbody details.qfold { margin: 10px 0 2px; }
.mbody details.qfold > summary { cursor: pointer; color: var(--ink-3);
    font-size: 12.5px; padding: 4px 0; border-top: 1px dashed var(--border-strong); }
.mbody details.qfold > .qbody { margin-top: 6px;
    border-left: 3px solid var(--border-2); padding-left: 10px; }
/* 인용에서 복원한 대화 턴 — 메시지가 아니므로(DB 행 없음) 흐리게, 링크 없이. */
.qturn + .qturn { margin-top: 12px; padding-top: 10px; border-top: 1px dotted var(--border-2); }
.qturn .qturn-h { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
    margin-bottom: 3px; }
.qturn .qw { font-size: 12.5px; font-weight: 600; color: var(--ink-2); }
.qturn .qd { font-size: 11.5px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.qturn .qsrc { font-size: 11px; color: var(--ink-3); border: 1px solid var(--border);
    border-radius: 3px; padding: 0 4px; }
.qturn .md-rich { color: var(--ink-3); }
.imgnote-inline { display: inline-block; font-size: 12px; color: var(--muted);
    border: 1px dashed var(--border); border-radius: 4px; padding: 1px 6px; }
/* 선택 검색 — 고른 자리 바로 아래 뜨는 버튼. 문서 좌표(absolute)라 패널
   스크롤에는 따라가지 않으므로 스크롤 시 감춘다(JS). */
.selfind { position: absolute; z-index: 20; font-size: 12.5px; padding: 4px 10px;
    border: 1px solid var(--accent); border-radius: var(--r-sm);
    background: var(--surface); color: var(--accent); cursor: pointer;
    box-shadow: 0 2px 8px rgba(0,0,0,.18); white-space: nowrap; }
.selfind:hover, .selfind:focus-visible { background: var(--accent); color: #fff; }
/* 검색 결과 머리 — 무엇을 골라 찾았는지 */
.selq { color: var(--ink-2); font-size: 13px; margin: 6px 0 2px; }
.selq b { color: var(--ink); font-weight: 600; }
/* 좁힐 말 후보 — 코드가 고르지 않고 내놓기만 한다(_narrow_chips) */
.narrow { color: var(--ink-3); font-size: 12.5px; margin: 2px 0 6px;
    display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.nchip { display: inline-block; font-size: 12.5px; padding: 1px 9px;
    border: 1px solid var(--border-strong); border-radius: 999px;
    background: var(--surface-2); color: var(--ink); text-decoration: none; }
.nchip:hover, .nchip:focus-visible { border-color: var(--accent); color: var(--accent); }
/* 작성 중 초안(2c) — 검증 전 텍스트임을 시각적으로도 구분(흐림+기울임) */
.draft { opacity: .55; font-style: italic; }
.waitslot:empty { display: none; }
/* AI 대기 카드 — 스피너·진행바·수신 줄·초안·중지를 한 덩어리로. 빈 슬롯은
   위 :empty 로 숨어 카드가 자연 축소된다(모델 배지는 빈 알약 껍데기 방지). */
.waitcard { background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r-lg); padding: 14px 16px; margin: 10px 0; max-width: 560px; }
.waithead { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.waitcard .rvbar { max-width: none; margin: 10px 0 8px; }
.waitdraft { margin: 10px 0 2px; border-left: 3px solid var(--border-2);
    background: var(--surface-3); border-radius: 0 8px 8px 0; padding: 8px 12px;
    font-size: 13px; color: var(--ink-2); line-height: 1.55; }
.waitmeta { display: flex; justify-content: space-between; align-items: center;
    gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.waitmeta form { margin: 0; }
.rvbar { max-width: 420px; height: 8px; background: var(--surface-3);
    border-radius: 999px; overflow: hidden; margin: 8px 0 6px; }
.rvfill { height: 100%; background: var(--accent); border-radius: 999px;
    transition: width .6s ease; }
.rvfill.indet { width: 38%; animation: rv-indet 1.6s ease-in-out infinite; }
@keyframes rv-indet { 0% { margin-left: -38%; } 100% { margin-left: 100%; } }
@media (prefers-reduced-motion: reduce) {
  .rvfill.indet, .waitcard .spin { animation: none; }
}
"""

_REF_RX = re.compile(r"#(\d+)")
_BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RX = re.compile(r"[-*]\s+(.*)")


def esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _linkify_refs(text: str) -> str:
    """이미 escape 된 텍스트의 #123 을 스레드 링크로."""
    return _REF_RX.sub(r'<a href="/thread/\1">#\1</a>', text)


_PRI_RX = re.compile(r"\[(상|중|하)\]")            # 개입 항목 긴급도 → 색 칩
_PRI_CLASS = {"상": "hi", "중": "mid", "하": "lo"}
_SNIP_HL_RX = re.compile(r"「(.+?)」")


# 리포트 항목의 '처리함' 표식(review.done_mark).
#
# **esc 를 지난 형태로 잡으면 안 된다**(2026-08-01 적대 검토). esc 는 `<`→`&lt;` 로
# 바꾸는데 그게 정확히 그 정규식이 찾는 모양이라, 메일 제목에 `<!--done:…-->` 를
# 심으면 이스케이프가 방어가 되지 않고 **가짜 버튼**이 그려졌다. 제목·인용은
# 발신자가 정하는 값이고 stalled 키는 발신자가 계산할 수 있어, 남의 항목을 접게
# 만드는 것도 가능했다.
#
# 그래서 **원문 줄 끝에 붙은 것 하나만** 신뢰한다(review.done_mark 의 계약과 같다).
# 그 밖의 표식 모양은 본문에서 통째로 지운다 — 화면에 글자로도 남기지 않는다.
_DONE_TAIL_RX = re.compile(r"<!--done:([a-z]+):([0-9a-f]{6,40})-->\s*$")
_DONE_RAW_RX = re.compile(r"<!--done:([a-z]+):([0-9a-f]{6,40})-->")


def _done_button(kind: str, key: str, back: str, label: str, tid: int) -> str:
    """메일 밖(회의·구두)에서 처리한 항목을 접는 버튼.

    한 번 누르면 **다음 리포트 판단에서도** 빠진다(결정론 단계에서 제외).
    label·tid 를 같이 넘기는 것은 되돌리기 목록에 무엇을 접었는지 남기기 위해서다
    — 키만 저장하면 나중에 그 해시가 무엇이었는지 아무도 모른다."""
    return ("<form class='donebtn' method='post' action='/report/done'>"
            f"<input type='hidden' name='kind' value='{esc(kind)}'>"
            f"<input type='hidden' name='key' value='{esc(key)}'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            f"<input type='hidden' name='tid' value='{tid}'>"
            f"<input type='hidden' name='label' value='{esc(label[:120])}'>"
            "<button title='메일 밖에서 처리했음 — 저장된 리포트에서도 빠집니다'>"
            "처리함</button></form>")


def _md_inline(text: str, back: str = "") -> str:
    """인라인 마크다운(굵게·#참조) + 데일리 장식(긴급도 칩·★·⏰·⚠·「근거」).
    escape 먼저. 데일리 전용 — 메일 본문은 _mail_md_* 별도 경로."""
    mark = _DONE_TAIL_RX.search(text)          # 줄 끝의 진짜 표식 하나만
    if _DONE_RAW_RX.search(text):
        text = _DONE_RAW_RX.sub("", text)      # 주입분 포함 전부 본문에서 제거
    t = esc(text)
    t = _BOLD_RX.sub(r"<strong>\1</strong>", t)
    t = _linkify_refs(t)
    t = _PRI_RX.sub(
        lambda m: f"<span class='pri {_PRI_CLASS[m.group(1)]}'>{m.group(1)}</span>", t)
    t = _SNIP_HL_RX.sub(r"<span class='snip'>「\1」</span>", t)
    t = t.replace("★", "<span class='star'>★</span>")
    t = t.replace("⏰기한", "<span class='ddl'>⏰기한</span>")
    t = t.replace("⚠", "<span class='warnmark'>⚠</span>")
    if mark is None:
        return t
    # 되돌리기 목록에 남길 이름 — 표식을 뺀 원문 줄에서 뽑는다(HTML 이전 형태)
    label = re.sub(r"\s+", " ", text.replace("**", "")).strip().lstrip("-# ").strip()
    ref = re.search(r"#(\d+)", label)
    return t + _done_button(mark.group(1), mark.group(2), back, label,
                            int(ref.group(1)) if ref else 0)


_COUNT_RX = re.compile(r"\((\d+)건\)")
_MORE_RX = re.compile(r"^[-*]\s*…\s*외\s*\d+건")


def _apply_done(md: str, done: set) -> str:
    """접힌 항목의 줄을 빼고 그 절 머리의 `(N건)` 을 남은 수로 고친다.

    저장된 리포트 파일은 다시 만들어지지 않으므로 **화면에서** 세야 한다.
    안 고치면 '(3건)' 아래 1건만 보인다(2026-08-01 사용자 확정). 남은 항목이
    0이면 절을 통째로 뺀다 — 머리만 남아 빈 목록을 광고하는 꼴이 되기 때문이다.

    렌더 전에 마크다운 단계에서 처리한다. 그래야 목록 구조(ul/li 짝)를 만들지
    않고도 정확히 빠진다."""
    lines = (md or "").splitlines()
    drop = [False] * len(lines)

    # 1) 접힌 불릿과 그 아래 딸린 줄(인용·중첩)을 표시
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        mark = (_DONE_TAIL_RX.search(lines[i])
                if _BULLET_RX.match(stripped) else None)
        if not (mark and f"{mark.group(1)}:{mark.group(2)}" in done):
            i += 1
            continue
        indent = len(lines[i]) - len(stripped)
        drop[i] = True
        j = i + 1
        while j < len(lines) and lines[j].strip():
            if len(lines[j]) - len(lines[j].lstrip()) <= indent:
                break
            drop[j] = True
            j += 1
        i = j
    if not any(drop):
        return md

    # 2) 절 머리마다 남은/접힌 항목을 센다. 머리는 `## … (N건)` 또는
    #    `(N건)` 을 단 불릿(참고 › 오래 멈춘 스레드, 지난 차수 점검 › 아직)이다.
    owner: dict[int, int] = {}          # 자식 들여쓰기 → 머리 줄 번호
    heads: dict[int, dict] = {}
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if stripped.startswith("#"):
            owner.clear()
            if stripped.startswith("## ") and _COUNT_RX.search(stripped):
                owner[0] = idx
                heads[idx] = {"kept": 0, "dropped": 0, "members": []}
            continue
        if not _BULLET_RX.match(stripped):
            o = owner.get(indent - 2)   # 항목의 연속 줄
            if o is not None:
                heads[o]["members"].append(idx)
            continue
        o = owner.get(indent)
        if o is not None:
            heads[o]["members"].append(idx)
            if not _MORE_RX.match(stripped):
                heads[o]["dropped" if drop[idx] else "kept"] += 1
        if _COUNT_RX.search(stripped):  # 이 불릿이 아래 항목들의 머리다
            owner[indent + 2] = idx
            heads[idx] = {"kept": 0, "dropped": 0, "members": []}
        else:
            owner.pop(indent + 2, None)

    # 3) 남은 게 없으면 절째로, 있으면 숫자만 고친다
    for idx, h in heads.items():
        if not h["dropped"]:
            continue
        if h["kept"]:
            lines[idx] = _COUNT_RX.sub(
                lambda m: f"({max(0, int(m.group(1)) - h['dropped'])}건)",
                lines[idx], count=1)
        else:
            drop[idx] = True
            for k in h["members"]:
                drop[k] = True
    return "\n".join(ln for k, ln in enumerate(lines) if not drop[k])


def _md_to_html(md: str, back: str = "", done: set | None = None,
                cards: bool = False) -> str:
    """데일리 마크다운(앱·AI 생성)을 구조화된 HTML 로 — 다른 페이지와 톤 일치.

    지원: `##` 헤딩, 중첩 불릿(2칸 들여쓰기), `**굵게**`, `#123` 링크.
    맨 위 `#` 한 줄(날짜 제목)은 페이지 h1 과 중복이라 건너뛴다.
    구조 규칙(2026-07-17 재구성): 첫 `##` 이전의 문단들 = 하루 요약 → 카드
    (.dsum), `## 참고` = 접힘(details). 옛 형식 데일리는 둘 다 해당 없음 →
    기존과 동일하게 렌더된다.

    cards=True 면 `##` 절마다 <section class='rcard'> 로 감싼다 — 벤토 스킨에서만
    켠다. **읽는 데이터도 계산도 그대로**고 감싸는 방식만 바뀐다(AI 호출이나 새
    질의가 끼면 그건 스킨이 아니라 다른 기능이다 — 2026-08-01 사용자 지적).

    done 은 '처리함'으로 접힌 `종류:키` 집합이다. 그 항목의 줄과 이어지는 연속
    줄을 **화면에서도** 뺀다 — 저장된 리포트 파일은 그대로라, 안 빼면 버튼을
    눌러도 그 자리에 남아 아무 일도 안 일어난 것처럼 보인다. 개수 표기를 다시
    세고 빈 절을 지우는 것까지 _apply_done 이 마크다운 단계에서 한다.
    """
    if done:
        md = _apply_done(md, done)
    out: list[str] = []
    depth = 0
    seen_h2 = False
    in_ref = False          # '참고' details 내부
    title_seen = False      # 맨 위 `#` 한 줄(날짜 제목)만 건너뛴다
    in_card = False         # cards=True 일 때 열려 있는 절 카드
    quote: list[str] = []   # 연속된 '> ' 줄
    lead: list[str] = []    # 첫 ## 이전 문단(하루 요약)

    def close_lists() -> None:
        nonlocal depth
        while depth > 0:
            out.append("</li></ul>")
            depth -= 1

    def close_card() -> None:
        nonlocal in_card
        if in_card:
            out.append("</section>")
            in_card = False

    def open_card() -> None:
        nonlocal in_card
        if cards:
            out.append("<section class='rcard'>")
            in_card = True

    def flush_lead() -> None:
        nonlocal lead
        if lead:
            out.append("<div class='dsum'>" + "\n".join(lead) + "</div>")
            lead = []

    def flush_quote() -> None:
        """'> ' 줄 묶음 → blockquote. 주간 머리의 AI 인증 만료 안내가 여기 걸린다 —
        예전에는 `&gt;` 가 본문에 그대로 찍혀 깨진 줄처럼 보였다."""
        nonlocal quote
        if not quote:
            return
        html = ("<blockquote>"
                + "".join(f"<p>{_md_inline(q, back)}</p>" for q in quote)
                + "</blockquote>")
        (lead if not seen_h2 else out).append(html)
        quote = []

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped == ">" or stripped.startswith("> "):
            quote.append(stripped[1:].strip())
            continue
        flush_quote()
        if not stripped:
            close_lists()
            continue
        if stripped.startswith("## "):
            close_lists()
            flush_lead()
            seen_h2 = True
            title = stripped[3:].strip()
            if in_ref:
                out.append("</details>")
                in_ref = False
            close_card()
            if title == "참고":
                # '참고' 접힘은 그 자체가 카드 역할을 한다 — 이중으로 감싸지 않는다
                out.append("<details class='dref'><summary>참고</summary>")
                in_ref = True
                continue
            open_card()
            out.append(f"<h2>{_md_inline(title, back)}</h2>")
            continue
        if stripped.startswith("# "):
            close_lists()
            if not title_seen:
                title_seen = True     # 맨 위 날짜 제목 — 페이지 h1 과 중복
                flush_lead()
                continue
            # 그 뒤의 `#` 는 내용이다. 일간의 `# AI 회고 분석` 이 여기 걸린다 —
            # 예전에는 제목을 버리고 '참고' 접힘도 안 닫아서, 사용자가 버튼을 눌러
            # 얻은 AI 분석이 접힌 참고 안쪽에 머리 없이 들어가 있었다.
            flush_lead()
            if in_ref:
                out.append("</details>")
                in_ref = False
            close_card()
            seen_h2 = True
            open_card()
            out.append(f"<h2>{_md_inline(stripped[2:].strip(), back)}</h2>")
            continue
        if stripped in ("---", "***", "___"):
            close_lists()
            flush_lead()
            close_card()          # 꼬리말(조사 범위)은 카드 밖이다
            out.append("<hr>")
            continue
        m = _BULLET_RX.match(stripped)
        if m:
            flush_lead()
            indent = len(line) - len(stripped)
            level = indent // 2 + 1
            if level > depth:
                while depth < level:
                    out.append("<ul>")
                    depth += 1
            else:
                while depth > level:
                    out.append("</li></ul>")
                    depth -= 1
                out.append("</li>")
            out.append(f"<li>{_md_inline(m.group(1), back)}")
            continue
        # 들여쓴 비-불릿 줄(↳ 사유·「근거」)은 열린 항목의 연속 줄 — 목록을
        # 끊지 않는다 (lazy continuation).
        if depth > 0 and line != stripped:
            out.append(f"<br><span class='cont'>{_md_inline(stripped, back)}</span>")
            continue
        close_lists()
        if not seen_h2:
            lead.append(f"<p>{_md_inline(stripped, back)}</p>")
            continue
        out.append(f"<p>{_md_inline(stripped, back)}</p>")
    flush_quote()
    close_lists()
    flush_lead()
    close_card()
    if in_ref:
        out.append("</details>")
    return "<div class='daily'>" + "\n".join(out) + "</div>"


# ─────────────── 메일 본문 마크다운 (text-only 메일, 토글 렌더 #21)
# 위 _md_to_html 는 데일리 리포트 전용(첫 # 스킵·#123 링크)이라 메일엔 부적합.
# 여기 것은 일반 마크다운 부분집합을 안전 변환: escape 먼저 → 화이트리스트 태그만.
# 밑줄(_)형 강조는 snake_case 오탐이 커서 미지원 — 별표(*)형만.
_MAIL_LIST_RX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_MAIL_ITEM_RX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_MAIL_ORD_RX = re.compile(r"^\s*\d+[.)]\s+")
_MAIL_HEAD_RX = re.compile(r"^(#{1,6})\s+(.*)$")
_MAIL_HR_RX = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MAIL_CODE_RX = re.compile(r"`([^`]+)`")
# 링크 라벨은 한 겹의 대괄호 허용 — "[공지] 제목" 링크가 "[[공지] 제목](url)" 로
# 변환되는 게 정상(CommonMark 균형 괄호)이라 렌더러가 받아줘야 한다
_MAIL_LINK_RX = re.compile(r"\[((?:[^\[\]\n]|\[[^\[\]\n]*\])+)\]\(([^)\s]+)\)")
# 굵게/취소선은 안쪽 가장자리 공백 허용("**aaa **" — 구버전 변환 저장분) —
# 공백은 태그 밖으로 재배치해 살린다. 기울임(*)은 수식·글롭 오탐 위험이 커서 엄격 유지.
_MAIL_STRONG_RX = re.compile(r"\*\*(\s*)([^*\n]*[^*\s\n])(\s*)\*\*")
_MAIL_EM_RX = re.compile(r"(?<![*\w])\*(\S(?:.*?\S)?)\*(?![*\w])")
_MAIL_DEL_RX = re.compile(r"~~(\s*)([^~\n]*[^~\s\n])(\s*)~~")   # 취소선 (diff 삭제분 등)
# GFM 표 구분행: `|---|:--:|--:|` (2열 이상). `---` 단독 수평선과 안 겹치게 파이프 필수.
_MAIL_TDELIM_RX = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
# 파이프 행: `| a | b |` — 구분행 없는 표(구버전 html_to_markdown 저장분) 인식용
_MAIL_PIPE_ROW_RX = re.compile(r"^\s*\|.*\|\s*$")
_MAIL_MD_SIGNAL_RX = re.compile(
    r"(?m)^\s*(?:#{1,6}\s+\S|[-*+]\s+\S|\d+[.)]\s+\S|>\s+\S|```)"
    r"|\*\*\S|`[^`]+`|\[(?:[^\[\]\n]|\[[^\[\]\n]*\])+\]\([^)\s]+\)"
    r"|^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$"      # 표 구분행
    r"|^\s*\|[^\n]*\|\s*\n\s*\|[^\n]*\|"                   # 구분행 없는 파이프 표 2행+
)


def _looks_like_markdown(text: str) -> bool:
    """text-only 메일이 마크다운 서식을 담고 있어 보이면 True → 토글 버튼 제공."""
    return bool(text and _MAIL_MD_SIGNAL_RX.search(text))


def _mail_md_inline(s: str) -> str:
    """이미 escape 된 한 줄에 인라인 마크다운 적용(코드/링크/굵게/기울임)."""
    codes: list[str] = []

    def _stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    s = _MAIL_CODE_RX.sub(_stash, s)          # 코드 스팬 먼저 보호(다른 변환서 제외)

    def _link(m):
        label, url = m.group(1), m.group(2)
        if url.lower().startswith(("http://", "https://", "mailto:")):
            return ('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                    % (url, label))
        return m.group(0)                     # 미지원 스킴은 원문 유지

    s = _MAIL_LINK_RX.sub(_link, s)
    s = _MAIL_STRONG_RX.sub(
        lambda m: "%s<strong>%s</strong>%s" % (m.group(1), m.group(2), m.group(3)), s)
    s = _MAIL_EM_RX.sub(lambda m: "<em>%s</em>" % m.group(1), s)
    s = _MAIL_DEL_RX.sub(
        lambda m: "%s<del>%s</del>%s" % (m.group(1), m.group(2), m.group(3)), s)
    s = re.sub(r"\x00(\d+)\x00",
               lambda m: "<code>%s</code>" % codes[int(m.group(1))], s)
    return s


def _split_table_row(line: str) -> list[str]:
    """GFM 표 한 행을 셀 리스트로. 바깥 파이프 제거, `\\|` 이스케이프 처리."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _cell_align(delim_cell: str) -> str:
    """구분행 셀(`:--`, `:-:`, `--:`)에서 정렬을 뽑는다."""
    c = delim_cell.strip()
    left, right = c.startswith(":"), c.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return ""


def _render_table(heads: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    ncol = len(heads)

    def _cell(val: str) -> str:
        # 표 셀의 <br>(GFM 셀 줄바꿈 관례 — 셀 안 <pre> 를 인라인화한 결과)는 실제
        # 줄바꿈으로 살리고, 나머지는 escape 후 인라인 마크다운 적용.
        return "<br>".join(_mail_md_inline(esc(seg)) for seg in val.split("<br>"))

    def _row(cells: list[str], tag: str) -> str:
        parts = []
        for j in range(ncol):
            val = cells[j] if j < len(cells) else ""          # 부족한 셀은 빈칸
            al = aligns[j] if j < len(aligns) else ""
            sty = " style='text-align:%s'" % al if al else ""
            parts.append("<%s%s>%s</%s>" % (tag, sty, _cell(val), tag))
        return "<tr>" + "".join(parts) + "</tr>"

    thead = "<thead>" + _row(heads, "th") + "</thead>"
    tbody = "<tbody>" + "".join(_row(r, "td") for r in rows) + "</tbody>"
    return "<table class='md-table'>" + thead + tbody + "</table>"


def _split_preserved(raw: str) -> tuple[str, str]:
    """new_content 를 (신규 작성분, 보존 인용) 으로 분할 — PRESERVED_MARK 기준.

    마커가 없으면 (원문, "") — mid-join 첫 보유 메일에만 마커가 있다."""
    if PRESERVED_MARK not in (raw or ""):
        return raw, ""
    head, _sep, tail = raw.partition(PRESERVED_MARK)
    return head.rstrip(), tail.strip()


def _mail_md_to_html(text: str) -> str:
    """text-only 메일 본문 마크다운을 안전 HTML 로. escape 먼저 → 화이트리스트만.

    지원: 헤딩(#~######→h3~h6), 불릿/번호 목록, 인용(>), 코드펜스(```),
    수평선, 인라인 코드/링크/굵게/기울임. 원문(<pre>)과 토글로 전환.
    """
    lines = (text or "").split("\n")
    out: list[str] = []
    para: list[str] = []

    def _flush():
        if para:
            out.append("<p>" + "<br>".join(_mail_md_inline(esc(x)) for x in para)
                       + "</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):                    # 코드 펜스
            _flush()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1                                         # 닫는 펜스 소비
            out.append("<pre class='md-code'><code>"
                       + esc("\n".join(code)) + "</code></pre>")
            continue
        if _MAIL_HR_RX.match(line):                        # 수평선
            _flush()
            out.append("<hr>")
            i += 1
            continue
        h = _MAIL_HEAD_RX.match(line)                      # 헤딩(페이지 톤과 충돌 없게 강등)
        if h:
            _flush()
            lvl = min(len(h.group(1)) + 2, 6)
            out.append("<h%d>%s</h%d>"
                       % (lvl, _mail_md_inline(esc(h.group(2).strip())), lvl))
            i += 1
            continue
        if stripped.startswith(">"):                       # 인용(재귀)
            _flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + _mail_md_to_html("\n".join(quote))
                       + "</blockquote>")
            continue
        if _MAIL_LIST_RX.match(line):                      # 목록(ul/ol)
            _flush()
            ordered = bool(_MAIL_ORD_RX.match(line))
            items = []
            while i < n and _MAIL_LIST_RX.match(lines[i]):
                items.append("<li>"
                             + _mail_md_inline(esc(_MAIL_ITEM_RX.match(lines[i]).group(1)))
                             + "</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag))
            continue
        if "|" in line and i + 1 < n and _MAIL_TDELIM_RX.match(lines[i + 1]):  # GFM 표
            _flush()
            heads = _split_table_row(line)
            aligns = [_cell_align(c) for c in _split_table_row(lines[i + 1])]
            i += 2
            rows = []
            while (i < n and lines[i].strip() and "|" in lines[i]
                   and not lines[i].strip().startswith("```")):
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_render_table(heads, aligns, rows))
            continue
        if (_MAIL_PIPE_ROW_RX.match(line) and i + 1 < n
                and _MAIL_PIPE_ROW_RX.match(lines[i + 1])
                and not _MAIL_TDELIM_RX.match(lines[i + 1])):
            # 구분행 없는 파이프 표 (th 없는 Outlook 표의 구버전 변환 저장분) —
            # 첫 행을 헤더로 렌더
            _flush()
            heads = _split_table_row(line)
            i += 1
            rows = []
            while i < n and _MAIL_PIPE_ROW_RX.match(lines[i]):
                if not _MAIL_TDELIM_RX.match(lines[i]):
                    rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_render_table(heads, [], rows))
            continue
        if not stripped:                                   # 빈 줄 → 문단 종료
            _flush()
            i += 1
            continue
        para.append(line)
        i += 1
    _flush()
    return "\n".join(out)


# ─────────────────────────────── 뷰모델 (순수 — 렌더러와 분리, 구 model.py 병합)
# 렌즈·홈·디테일 데이터 구성. HTML 을 만들지 않는 순수 로직이라 단위 테스트 대상.

def load_daily(cfg, today: str) -> str | None:
    """오늘자 데일리 리뷰 마크다운을 읽는다. 없으면 None."""
    path = Path(cfg.vault) / "daily" / f"{today}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None




# Outlook 이 본문 붙여넣기 이미지에 자동으로 붙이는 무의미한 첨부 이름 —
# 타임라인 헤더에 노출해도 정보가 없어 표시에서 제외한다 (DB·첨부 추출은 무관)
_NOISE_ATTACH_RX = re.compile(
    r"^(제목\s*없는\s*첨부\s*파일|untitled\s+attachment)", re.IGNORECASE)


def _visible_attach(names: str) -> str:
    """표시용 첨부 이름 — 자동 명명된 인라인 이미지는 걸러낸다."""
    kept = [n.strip() for n in (names or "").split(";")
            if n.strip() and not _NOISE_ATTACH_RX.match(n.strip())]
    return ";".join(kept)


def format_detail(store, cfg, thread_id: int) -> dict:
    """디테일 뷰 데이터: 상단 분석 + 하단 메일 타임라인.

    각 타임라인 항목은 표시용 html(정제됨)과 텍스트(html 없을 때 폴백)를 함께 준다.
    액션 판정(actions)은 계산만 하고 화면엔 내지 않는다(신호 노출 폐지,
    2026-07-30) — d["act"] 는 주간 보고와 같은 판정기를 쓰는 소비처 호환용.
    """
    t = store.thread(thread_id)
    msgs = store.thread_messages(thread_id)
    if not t or not msgs:
        return {"title": f"#{thread_id}", "analysis": ["(스레드 없음)"], "timeline": []}

    subject = msgs[0]["subject"]
    participants = sorted({m["sender_name"] or m["sender_addr"] for m in msgs})

    act = actions.evaluate_thread(store, cfg, thread_id)

    analysis = [
        f"제목: {subject}",
        f"기간: {msgs[0]['sent_on'][:10]} ~ {msgs[-1]['sent_on'][:10]}  ·  "
        f"{len(msgs)}통  ·  참여 {len(participants)}명",
        f"참여자: {', '.join(participants)}",
    ]
    # 누적 요약: 있으면 표시, 없으면 아무것도 안 보임(빈 안내문 제거 — 요약이 없을 땐
    # 안 보이는 게 자연스러움)
    summ = review.strip_summary_header(t["rolling_summary"])  # 기존 '갱신된 요약' 머리말 제거
    if summ:
        analysis.append("")
        analysis.append("[누적 요약]")
        analysis.extend(summ.splitlines())

    timeline: list[dict] = []
    for m in msgs:
        arrow = "→" if m["is_sent"] else " "
        vis_att = _visible_attach(m["attach_names"])
        att = f"  📎{vis_att}" if vis_att else ""
        timeline.append({
            "id": m["id"],                # 검색·목록에서 이 메일로 스크롤(#msg-{id})
            "sent_on": m["sent_on"][:16],
            "is_sent": bool(m["is_sent"]),
            "sender": m["sender_name"] or m["sender_addr"],
            "sender_addr": m["sender_addr"],
            "to": m["to_addrs"],
            "attach": vis_att,
            "head": f"{m['sent_on'][:16]} {arrow} {m['sender_name']}{att}",
            "html": (m["body_html"] or "").strip(),
            "body": (m["new_content"] or "").splitlines(),
        })
    timeline.reverse()   # 최신 메일 먼저 (메일 클라이언트 관례)
    return {"title": subject, "analysis": analysis, "timeline": timeline,
            "act": act}


# 검색은 nav 링크가 아니라 헤더 상시 검색창으로 승격(2026-07-15) — 어느 화면에서든
# 바로 검색. 값은 app.js syncNavSearch 가 /search 일 때 URL 의 q 로 채운다.
# 첫 항목 '분석'(href=/) = 첫 화면. 위치명(홈) 대신 기능명 — 다른 메뉴와 층위 일치.
# /ask* 도 같은 밑줄로 매핑(navTarget).
# 수동 동기화는 전역 동작이라 우측 아이콘(↻) — 자동 동기화의 보조 수단.
_NAV = ('<nav><a href="/">분석</a>'
        '<a href="/mail">메일함</a><a href="/threads">스레드</a>'
        '<a href="/people">인물</a><a href="/records">기억</a>'
        '<a href="/stats">통계</a>'
        # 뒤로 — 앱 모드(--app)엔 브라우저 뒤로 버튼이 없다. history.back() 만
        # 호출하면 기존 popstate 핸들러가 그 URL 을 해당 패널(좌/우)로 복원한다.
        "<button type='button' class='navback' title='뒤로' aria-label='뒤로'>←</button>"
        "<form class='navsearch' method='get' action='/search' role='search'>"
        "<input name='q' placeholder='🔍 검색' aria-label='검색' autocomplete='off'>"
        "</form>"
        "<form class='navsync' method='post' action='/sync'>"
        "<button title='메일 동기화' aria-label='메일 동기화'>↻</button></form>"
        '<a href="/settings" class="gear" title="설정" aria-label="설정">⚙</a></nav>')

# 우측(읽기) 패널의 기본 안내 — 좌측에서 항목을 열기 전까지 표시.
_READING_HINT = ("<p class='empty'>왼쪽에서 스레드나 메일을 선택하면 "
                 "여기에서 원문이 열립니다.</p>")


def _nav_html(active: str | None = None) -> str:
    """상단 nav — active 경로의 링크에 class='active'(밑줄) 부여.

    app.js 없는 전폭 페이지(통계)는 서버가 직접 활성 메뉴를 표시해야 한다.
    좌우 셸 페이지는 app.js markNav 가 이동마다 갱신하므로 active 생략 가능.
    """
    if not active:
        return _NAV
    # 정확 매칭만 치환(gear 등 다른 속성 없는 최상위 링크). 1회만.
    return _NAV.replace(f'<a href="{active}">',
                        f'<a href="{active}" class="active">', 1)


_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#22262b'/>"
    "<text x='16' y='23' text-anchor='middle'"
    " font-family='Segoe UI,system-ui,sans-serif' font-size='21' font-weight='700'"
    " fill='#e8975a'>M</text></svg>"
)


def _head(title: str, refresh: int | None = None, extra_css: str = "",
          read_w: int | None = None, active: str | None = None,
          theme: str = "light", read_fs: int | None = None,
          skin: str = "classic") -> str:
    # **noscript 안에 둔다.** 진행 화면 자동 새로고침은 JS 꺼짐 폴백인데, 밖에 두면
    # JS 가 켜져 있어도 전체 페이지가 2초마다 리로드된다 — 그때마다 CSS 기본값
    # (--left-w: 380px)로 그려졌다가 app.js 가 저장 폭을 다시 적용해 좌/우 분리선이
    # 떨리고, 창 크기 복원(resizeTo)까지 매번 재실행된다(2026-07-29 실기기 증상).
    # JS 가 있으면 폴링이 같은 일을 더 가볍게 한다.
    meta_refresh = (f"<noscript><meta http-equiv='refresh' content='{refresh}'>"
                    "</noscript>" if refresh else "")
    # extra_css 는 _CSS '앞'에 넣는다 — 겹치는 셀렉터(body·h1·header·* 등)는 뒤의
    # _CSS 가 이겨 상단 셸 타이포/헤더가 다른 페이지와 동일하게 유지되고,
    # extra_css 고유 규칙(통계 컴포넌트·CSS 변수)만 추가로 적용된다.
    extra = f"<style>{extra_css}</style>" if extra_css else ""
    # 읽기 창(#right) 너비는 설정값을 CSS 변수로 주입(미지정 시 CSS 기본 1200px).
    rw = f"<style>:root{{--read-w:{int(read_w)}px}}</style>" if read_w else ""
    # 본문 글자 크기(web.reading_font)도 같은 방식 — 별도 <style>(rw 문자열 불변).
    # --read-zoom: 메일 원본 HTML 은 인라인 font-size(pt)가 상속을 이겨 변수가 안
    # 먹는다 → .mailhtml 을 zoom 으로 블록째 비례 확대(기준 16px 대비 배율).
    rf = (f"<style>:root{{--read-fs:{int(read_fs)}px;"
          f"--read-zoom:{int(read_fs) / 16:.4g}}}</style>") if read_fs else ""
    # 테마는 <html data-theme> 로 — 다크는 :root[data-theme='dark'] 토큰 오버라이드.
    # 스킨(모양·밀도)은 밝기와 **별개 축**이라 data-skin 으로 따로 싣는다.
    th = "dark" if theme == "dark" else "light"
    sk = _skin_ok(skin)
    return (
        f"<!doctype html><html lang='ko' data-theme='{th}' data-skin='{sk}'>"
        "<head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{meta_refresh}"
        f"<title>{esc(title)} · Minerva</title>"
        "<link rel='icon' type='image/svg+xml' href='/favicon.svg'>"
        f"{extra}<style>{_CSS}{_SKIN_CSS}</style>{rw}{rf}"
        "</head><body>"
        f"<header class='top'><span class='brand'>Minerva</span>"
        f"{_nav_html(active)}</header>"
    )


def _page(title: str, inner: str, theme: str = "light",
          skin: str = "classic") -> str:
    """단일 컬럼 페이지 — 차단 안내 등 셸이 필요 없는 특수 응답용."""
    return (_head(title, theme=theme, skin=skin)
            + f"<div id='right' style='flex:1;overflow-y:auto'>"
              f"<div class='inner'>{inner}</div></div></body></html>")


def _page_wide(title: str, inner: str, extra_css: str = "",
               script_src: str | None = None, active: str | None = None,
               theme: str = "light", skin: str = "classic") -> str:
    """상단 nav 셸 + 전폭 단일 컬럼 페이지 (좌/우 분할 없음).

    통계처럼 좌우 프레임이 필요 없는 화면용 — 헤더(Minerva·홈·메일함…)는 다른
    메뉴와 동일하고, 그 아래에 콘텐츠만 전폭으로 스크롤(#right 재사용)된다.
    링크·기간선택 폼은 순수 GET 이라 app.js 없이 일반 이동한다.
    """
    tip = "<div id='tip' role='status'></div>"
    scr = f"<script src='{esc(script_src)}'></script>" if script_src else ""
    return (
        _head(title, None, extra_css, active=active, theme=theme, skin=skin)
        + f"<main id='right'><div class='inner'>{inner}</div></main>"
        + tip + scr + "</body></html>"
    )


def render_stats_page(store, cfg) -> str:
    """통계 전폭 페이지 — 상단 셸은 다른 메뉴와 통일, 본문만 통계."""
    inner = report.render_stats(store, cfg)
    return _page_wide("통계 분석", inner, extra_css=report.CSS,
                      script_src="/report.js", active="/stats",
                      theme=cfg.opt("web", "theme", default="light"),
                      skin=cfg.opt("web", "skin", default="classic"))


def _shell(title: str, left: str, right: str, refresh: int | None = None,
           read_w: int | None = None, theme: str = "light",
           read_fs: int | None = None, skin: str = "classic") -> str:
    """Outlook 유사 좌/우 분할 셸 (#14). 콘텐츠 갱신은 /app.js 가 fragment 로."""
    return (
        _head(title, refresh, read_w=read_w, theme=theme, read_fs=read_fs,
              skin=skin)
        + "<div id='layout'>"
        + f"<aside id='left'><div class='inner'>{left}</div></aside>"
        + "<div id='splitter' title='드래그로 폭 조절'></div>"
        + f"<main id='right'><div class='inner'>{right}</div></main>"
        + "</div><div id='toast' hidden></div>"
        + "<script src='/app.js'></script></body></html>"
    )


def _with_frag(location: str) -> str:
    """303 Location 에 frag=1 부가 — fetch 가 따라가서 fragment 를 받게 (#16)."""
    return location + ("&frag=1" if "?" in location else "?frag=1")


# ─────────────────────────────────────────────────────────────────────
# 벤토 스킨 — 설정 › 화면 스킨에서 고른 사람에게만 적용된다.
#
# **classic 은 이 블록의 영향을 전혀 받지 않는다.** 모든 규칙이
# `<html data-skin='bento'>` 를 요구하므로, 고르지 않으면 존재하지 않는 것과 같다.
#
# 원칙: **토큰 오버라이드가 본체**다. 아래 선택자 규칙은 토큰으로 표현할 수 없는
# 것만 최소로 둔다(출처를 서체로 가르는 2개 + 카드 호버 1개). 여기에 규칙을 계속
# 더하고 싶어지면 그건 스킨이 아니라 레이아웃 변경이라는 신호다 — 그때는 스킨이
# 아니라 별도 렌더러로 간다.
#
# 특이도 주의: 다크 토큰은 `:root[data-theme='dark']`(0,2,0) 이라 스킨의 라이트
# 블록과 같은 값이다. 순서로 이기게 두면 '다크+벤토'에서 라이트 색이 덮어쓴다 →
# 라이트 블록에 :not([data-theme='dark']) 를 붙여 0,3,0 으로 올린다.
_SKIN_CSS = """
:root[data-skin='bento'] {
  /* 모양·깊이는 밝기와 무관 — 두 테마가 공유한다 */
  --r-sm:8px; --r-md:12px; --r-lg:14px;
  --shadow-card:0 1px 2px rgba(19,26,33,.05), 0 8px 24px -14px rgba(19,26,33,.30);
  --shadow-pop:0 2px 6px rgba(19,26,33,.08), 0 18px 40px -18px rgba(19,26,33,.40);
}
:root[data-skin='bento']:not([data-theme='dark']) {
  /* 바탕을 한 단 낮춰 흰 카드가 '떠 있게' 만든다 — 벤토의 핵심 감각 */
  --bg:#eef2f7; --surface:#ffffff; --surface-2:#f6f8fb; --surface-3:#e7edf4;
  --border:#e3e9f0; --border-2:#d3dbe4; --border-strong:#b8c3ce;
  --ink:#131a21; --ink-2:#4a5561; --ink-3:#77838f; --muted:#9aa5b1;
}
:root[data-skin='bento'][data-theme='dark'] {
  --bg:#15181c; --surface:#1e2328; --surface-2:#232930; --surface-3:#191d22;
  --border:#2e353c; --border-2:#3a424a; --border-strong:#4d565f;
  --shadow-card:0 1px 2px rgba(0,0,0,.40), 0 10px 28px -16px rgba(0,0,0,.80);
  --shadow-pop:0 2px 6px rgba(0,0,0,.45), 0 22px 46px -20px rgba(0,0,0,.90);
}
/* 코드가 센 사실(스레드 번호)은 모노로 — 사람·AI 가 쓴 문장과 눈으로 갈린다.
   이 앱의 원칙("결정론은 코드가, 문장은 AI가")을 화면 문법으로 옮긴 것이다. */
:root[data-skin='bento'] .daily a[href^="/thread/"] {
  font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: .9em;
}
/* 원문 인용은 왼쪽 괘선을 세워 문장에서 떼어 놓는다 */
:root[data-skin='bento'] .daily .snip {
  display: block; border-left: 2px solid var(--border-2);
  padding-left: 9px; margin-left: 0;
}
/* 벤토 홈 — 있는 것을 다시 배치한 격자. 6열 위에 타일이 서로 다른 폭으로 앉는다. */
:root[data-skin='bento'] .bhome {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; margin: 14px 0 4px;
}
:root[data-skin='bento'] .btile {
  grid-column: span 12; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-lg); padding: 12px 15px 14px; box-shadow: var(--shadow-card);
  min-width: 0; overflow: hidden;
}
:root[data-skin='bento'] .btile.s8 { grid-column: span 8; }
:root[data-skin='bento'] .btile.s6 { grid-column: span 6; }
:root[data-skin='bento'] .btile.s4 { grid-column: span 4; }
/* 숫자 한 줄짜리 칸은 진짜 작게 — 큰 칸과 대비가 나야 격자로 읽힌다 */
:root[data-skin='bento'] .btile.mini { padding: 10px 13px 11px; }
:root[data-skin='bento'] .btile.ai { border-color: var(--accent2); }
@media (max-width: 900px) {
  :root[data-skin='bento'] .btile.s8 { grid-column: span 12; }
  :root[data-skin='bento'] .btile.s4 { grid-column: span 6; }
}
@media (max-width: 620px) {
  :root[data-skin='bento'] .btile.s4, :root[data-skin='bento'] .btile.s6 {
    grid-column: span 12; }
}
:root[data-skin='bento'] .bth {
  display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px;
}
:root[data-skin='bento'] .bth .lab {
  font-size: 11.5px; font-weight: 700; letter-spacing: .08em; color: var(--ink-3);
}
:root[data-skin='bento'] .bth .cnt {
  font-family: var(--mono); font-size: 11.5px; color: var(--muted);
}
:root[data-skin='bento'] .bth .more { margin-left: auto; font-size: 12px; }
:root[data-skin='bento'] .btile .daily { padding: 0; }
:root[data-skin='bento'] .btile ul { padding-left: 16px; margin: 2px 0; }
:root[data-skin='bento'] .btile li { font-size: 13px; margin: 2px 0; }
:root[data-skin='bento'] .bsaid .daily p { color: var(--ink); font-size: 14px; }
:root[data-skin='bento'] .bnum {
  font-size: 26px; font-weight: 700; letter-spacing: -.02em; line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
:root[data-skin='bento'] .bnum.sm { font-size: 17px; font-family: var(--mono); }
:root[data-skin='bento'] .brow { font-size: 13.5px; margin: 3px 0; }
/* 리포트를 문서가 아니라 카드 묶음으로 — 절마다 독립된 판이 되어 훑기 쉬워진다.
   바깥 .daily 는 판 역할을 내려놓고 배경이 된다(카드가 떠 보이게). */
:root[data-skin='bento'] .daily {
  background: none; border: 0; padding: 0;
}
:root[data-skin='bento'] .daily .rcard {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-lg); padding: 2px 18px 16px; margin: 12px 0;
  box-shadow: var(--shadow-card);
}
:root[data-skin='bento'] .daily .rcard > h2 { border-bottom: 0; margin-bottom: 4px; }
:root[data-skin='bento'] .daily details.dref {
  background: var(--surface); border: 1px solid var(--border); border-top: 1px solid var(--border);
  border-radius: var(--r-lg); padding: 12px 18px; margin-top: 12px;
  box-shadow: var(--shadow-card);
}
:root[data-skin='bento'] .daily hr { margin: 18px 0 10px; }
/* 카드가 얹혀 있다는 감각 — 2px 만. 대시보드가 들썩이면 읽기가 방해된다 */
:root[data-skin='bento'] .dcard, :root[data-skin='bento'] .aicards .aicard {
  transition: transform .14s ease, box-shadow .14s ease;
}
:root[data-skin='bento'] .dcard:hover,
:root[data-skin='bento'] .aicards .aicard:hover {
  transform: translateY(-2px); box-shadow: var(--shadow-pop);
}
@media (prefers-reduced-motion: reduce) {
  :root[data-skin='bento'] .dcard, :root[data-skin='bento'] .aicards .aicard {
    transition: none; }
  :root[data-skin='bento'] .dcard:hover,
  :root[data-skin='bento'] .aicards .aicard:hover { transform: none; }
}
"""

SKINS = ("classic", "bento")


def _skin_ok(v: str) -> str:
    return v if v in SKINS else "classic"


# 앱 JS — /app.js 로 서빙 (CSP script-src 'self' 하에서만 실행됨).
# 책임: 링크/폼 fetch 가로채기(#16), 스플리터+localStorage(#15), 토스트, 리뷰 폴링.
_APP_JS = r"""
(function () {
  "use strict";
  var left = document.getElementById("left");
  var right = document.getElementById("right");
  var splitter = document.getElementById("splitter");
  if (!left || !right) return;

  function paneFor(path) {
    path = path.replace(/\/+$/, "") || "/";
    /* 홈(/)=분석 대화록 — 우측. 좌측은 목록/메뉴 성격 페이지들 */
    if (path === "/mail" || path === "/threads" ||
        path === "/search" || path === "/records" || path === "/daily" ||
        path === "/settings") return "left";
    if (path === "/lens/intervene" || path === "/person" ||
        path === "/people" || path === "/ask/list") return "left";
    /* 주간 보고는 기억(좌측) 소속 — POST /weekly 의 303 이 여기로 온다.
       우측에 넣으면 hookWeeklyPolling(좌측 기준)이 못 봐 대기 화면이 멈춘다 */
    if (path === "/weekly/status") return "left";
    if (path === "/people/dossier/status") return "left";
    return "right";
  }
  function paneEl(p) { return p === "left" ? left : right; }

  /* ---- #15 좌측 폭: 드래그 + localStorage 복원 ---- */
  var KEY = "mailkb.leftw";
  function applyW(w) {
    var max = Math.floor(window.innerWidth * 0.7);
    w = Math.max(240, Math.min(w || 380, max));
    document.documentElement.style.setProperty("--left-w", w + "px");
    return w;
  }
  try {
    var saved = parseInt(localStorage.getItem(KEY), 10);
    if (!isNaN(saved)) applyW(saved);
  } catch (e) { /* 기업 정책으로 localStorage 차단 시 무시 */ }
  if (splitter) {
    var drag = null;
    splitter.addEventListener("pointerdown", function (e) {
      drag = { x: e.clientX, w: left.getBoundingClientRect().width };
      splitter.classList.add("drag");
      splitter.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    splitter.addEventListener("pointermove", function (e) {
      if (drag) applyW(Math.round(drag.w + e.clientX - drag.x));
    });
    splitter.addEventListener("pointerup", function (e) {
      if (!drag) return;
      var w = applyW(Math.round(drag.w + e.clientX - drag.x));
      drag = null;
      splitter.classList.remove("drag");
      try { localStorage.setItem(KEY, String(w)); } catch (err) {}
    });
  }

  /* ---- 창 크기 기억: 로드 시 기억된 크기로 복원(resizeTo) + 리사이즈 시 저장 ---- */
  fetch("/winsize").then(function (r) { return r.text(); }).then(function (s) {
    var p = (s || "").split(",");
    var w = parseInt(p[0], 10), h = parseInt(p[1], 10);
    if (w > 0 && h > 0 &&
        (Math.abs(window.outerWidth - w) > 20 || Math.abs(window.outerHeight - h) > 20)) {
      try { window.resizeTo(w, h); } catch (e) { /* 일반 탭은 차단 — 무시 */ }
    }
  }).catch(function () {});
  var _wszT;
  window.addEventListener("resize", function () {
    clearTimeout(_wszT);
    _wszT = setTimeout(function () {
      fetch("/winsize", {
        method: "POST",
        headers: { "X-Requested-With": "fetch",
                   "Content-Type": "application/x-www-form-urlencoded" },
        body: "w=" + window.outerWidth + "&h=" + window.outerHeight,
      }).catch(function () {});
    }, 600);
  });
  window.addEventListener("pagehide", function () {   /* 닫힐 때 최종 크기 확보 */
    try { navigator.sendBeacon("/winsize",
      "w=" + window.outerWidth + "&h=" + window.outerHeight); } catch (e) {}
  });

  /* ---- 표시 최신화: DB 변경(새 메일)을 토큰으로 감지해 현재 목록/홈을 조용히 다시 그림.
     autosync 든 스케줄러 등 외부 sync 든 DB 만 바뀌면 반영한다(수집 주기와 분리). ---- */
  var lastTok = null, listDirty = false;
  function refreshDisplay() {
    /* 우측에서 스레드를 열면 location 은 /thread/N 이지만 왼쪽은 메일함·스레드
       목록 그대로다. 주소창이 아니라 실제 왼쪽 패널 상태로 갱신 대상을 정한다. */
    var cur = new URL(leftCur || "/mail", location.origin);
    var p = cur.pathname.replace(/\/+$/, "") || "/";
    var target = cur.pathname + (cur.search || "");
    if (p === "/mail" || p === "/threads") {
      if (!left || left.scrollTop >= 150) {
        listDirty = true;                       /* 깊이 읽는 동안은 미루되 변경을 버리지 않음 */
        return;
      }
      var sc = left.scrollTop;
      listDirty = false;
      load(target, "left", false)
        .then(function () { left.scrollTop = sc; })
        .catch(function () { listDirty = true; });
    }
  }
  function checkFresh() {
    return fetch("/latest?_=" + Date.now()).then(function (r) { return r.text(); })
      .then(function (tok) {
      if (lastTok === null) { lastTok = tok; return; }   /* 첫 호출 = 기준선 */
      if (tok !== lastTok) { lastTok = tok; refreshDisplay(); return; }
      /* 직전 목록 fetch 실패 등으로 dirty 가 남았으면 같은 토큰이어도 재시도. */
      if (listDirty && left && left.scrollTop < 150) refreshDisplay();
    }).catch(function () {});
  }
  checkFresh();                                /* 기준선 */
  setInterval(checkFresh, 60000);              /* 60초마다 표시 최신화(가벼운 DB 조회) */
  left.addEventListener("scroll", function () {
    if (listDirty && left.scrollTop < 150) refreshDisplay();
  });

  /* ---- 자동 동기화(Outlook 수집): 주기(분)마다 /autosync. 새 메일이면 토스트 + 즉시 최신화 ---- */
  fetch("/syncmin").then(function (r) { return r.text(); }).then(function (s) {
    var min = parseInt(s, 10);
    if (!(min > 0)) return;                    /* 0=끔 */
    setInterval(function () {
      fetch("/autosync", { method: "POST", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (t) {
          /* 백그라운드 잡 시작됨 → 완료를 감시해 '새 메일 N통' 토스트(서버 안 멈춤) */
          if (t === "started") watchSyncToast();
        }).catch(function () {});
    }, min * 60000);
  }).catch(function () {});

  /* ---- 토스트 ---- */
  var toastTimer = null;
  function toast(msg) {
    var t = document.getElementById("toast");
    if (!t || !msg) return;
    t.textContent = msg;            /* textContent — 서버 msg 도 신뢰하지 않음 */
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4000);
  }

  /* ---- 패널 주입 + 좌우 한 쌍의 브라우저 이력 ---- */
  var leftStack = [], leftCur = null, rightCur = null, backNav = false;
  var restoringHistory = false;
  var appDepth = 0;
  var rightBlankHtml = (right.querySelector(".inner") || right).innerHTML;
  function historyState() {
    return { minerva: 1, leftUrl: leftCur || "", rightUrl: rightCur || "",
             depth: appDepth };
  }
  function rememberPane(pane, url) {
    if (pane === "left") noteLeft(url);
    else rightCur = url;
  }
  function replacePaneUrl(pane, url, address) {
    rememberPane(pane, url);
    history.replaceState(historyState(), "", address || (location.pathname + location.search));
  }
  function noteLeft(u) {
    if (!u) return;
    if (backNav) { leftCur = u; return; }        /* 뒤로 이동은 스택에 안 쌓음 */
    if (u !== leftCur) { if (leftCur) leftStack.push(leftCur); leftCur = u; }
  }
  function leftBack() {
    if (!leftStack.length) return false;
    var u = leftStack.pop();
    backNav = true;
    load(u, "left", true).then(clr, clr);
    function clr() { backNav = false; }
    return true;
  }

  function inject(pane, html, url) {
    var host = paneEl(pane);
    var el = host.querySelector(".inner") || host;
    el.innerHTML = html;
    host.scrollTop = 0;
    if (pane === "right") msgCurId = null;        /* 새 내용 — n/p 커서 리셋 */
    if (url !== null && url !== undefined) {
      rememberPane(pane, url);
      appDepth += 1;
      history.pushState(historyState(), "", url);
    }
    markSelected();
    markNav();
    hookReviewPolling(el);
    hookAiPolling(el);
    hookWeeklyPolling(el);
    hookAskPolling(el);
    hookDossierPolling(el);
    hookSyncPolling(el);
    hookMore();
    hookThreadHead();
    chatBottom(el);                               /* 대화록은 최신(맨 아래)으로 */
  }

  /* 대화(#right .chat)는 채팅처럼 맨 아래(최신 답)로 스크롤하고 입력창에 포커스 */
  function chatBottom(el) {
    if (!el.querySelector(".chatbar")) return;
    right.scrollTop = right.scrollHeight;
    var ae = document.activeElement;             /* 딴 데(좌측 검색 등) 입력 중이면 뺏지 않음 */
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
    var inp = el.querySelector(".chatbar input[type=text]");
    if (inp && !inp.value) try { inp.focus(); } catch (e) {}
  }

  /* ---- 스레드 sticky 헤더: 센티널이 위로 벗어나면 컴팩트(.stuck) ---- */
  var _thObs = null;
  function hookThreadHead() {
    if (_thObs) { _thObs.disconnect(); _thObs = null; }
    var sen = right.querySelector(".sticksentinel");
    var head = right.querySelector(".threadhead");
    if (!sen || !head || !window.IntersectionObserver) return;
    _thObs = new IntersectionObserver(function (entries) {
      head.classList.toggle("stuck", !entries[0].isIntersecting);
    }, { root: right, threshold: 0 });
    _thObs.observe(sen);
  }

  /* ---- 선택 검색: 읽다가 고른 말을 그대로 검색으로 ----------------------
     결과는 **좌측**(검색은 좌측 패널 라우트)이라 읽던 스레드가 우측에 남는다.
     질의는 다듬지 않는다 — 용어 선택은 이미 연속구(tier1)로 정확하고, 긴 선택이
     불러오는 느슨한 결과는 검색 화면의 '— 관련 낮음 —' 구분선이 표시한다.
     읽고 있던 그 메일은 exclude 로 뺀다(안 빼면 1등이 자기 자신이다). */
  var selBtn = null, selQ = "", selMid = "";
  var SEL_MAX = 200;            /* 원문 그대로 보내는 상한 — FTS 질의 폭주 방지 */

  function hideSelBtn() {
    if (selBtn) { selBtn.remove(); selBtn = null; }
    selQ = ""; selMid = "";
  }

  function runSelSearch() {
    if (!selQ) return;
    var url = "/search?sel=1&q=" + encodeURIComponent(selQ)
      + (selMid ? "&exclude=" + encodeURIComponent(selMid) : "");
    hideSelBtn();
    load(url, "left").catch(function () {});
  }

  function showSelBtn() {
    var sel = window.getSelection && window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return hideSelBtn();
    var text = (sel.toString() || "").replace(/\s+/g, " ").trim();
    var node = sel.anchorNode;
    var host = node && (node.nodeType === 1 ? node : node.parentNode);
    var body = host && host.closest && host.closest("#right .mbody");
    if (text.length < 2 || !body) return hideSelBtn();
    var msg = body.closest(".msg");
    selMid = msg && msg.id.indexOf("msg-") === 0 ? msg.id.slice(4) : "";
    selQ = text.slice(0, SEL_MAX);
    var rect = sel.getRangeAt(0).getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height)) return hideSelBtn();
    if (!selBtn) {
      selBtn = document.createElement("button");
      selBtn.type = "button";
      selBtn.className = "selfind";
      selBtn.textContent = "이 말 찾기";
      /* 포커스를 훔치면 선택이 풀리는 브라우저가 있다 — mousedown 을 막는다 */
      selBtn.addEventListener("mousedown", function (e) { e.preventDefault(); });
      selBtn.addEventListener("click", runSelSearch);
      document.body.appendChild(selBtn);
    }
    selBtn.style.top = (rect.bottom + window.scrollY + 6) + "px";
    selBtn.style.left = (rect.left + window.scrollX) + "px";
  }

  document.addEventListener("selectionchange", function () {
    /* 드래그 중에는 붙였다 뗐다 하지 않는다 — 손을 뗄 때 한 번만 판단 */
    if (selBtn) showSelBtn();
  });
  document.addEventListener("mouseup", function (e) {
    if (selBtn && e.target === selBtn) return;
    setTimeout(showSelBtn, 0);              /* 선택 확정 후에 읽는다 */
  });
  right.addEventListener("scroll", hideSelBtn);

  /* ---- 검색으로 들어온 낱말 강조 --------------------------------------
     본문은 **정제된 메일 원본 HTML** 이라 <mark> 로 감싸면 마크업을 건드리고
     텍스트 노드가 쪼개진다(선택 검색이 고른 문자열이 경계에서 잘린다). 그래서
     CSS Custom Highlight API 로 Range 만 등록한다 — DOM 무변형, 되돌릴 코드 없음.
     못 쓰는 브라우저에서는 조용히 넘어간다(스크롤·메일 테두리는 그대로 동작). */
  var HL_NAME = "kw", HL_MAX = 200, hlOn = false;

  function hlOk() { return !!(window.CSS && CSS.highlights && window.Highlight); }

  function clearHl() {
    if (hlOk()) CSS.highlights.delete(HL_NAME);
    hlOn = false;
  }

  function applyHl(text) {
    clearHl();
    if (!text || !hlOk()) return;
    var words = [], raw = text.split(/\s+/), i;
    for (i = 0; i < raw.length; i++)
      if (raw[i].length >= 2) words.push(raw[i].toLowerCase());
    var root = right.querySelector(".mthread");
    if (!words.length || !root) return;
    /* 접힌 인용 안에 일치가 있으면 **먼저** 편다 — 닫힌 채로는 칠해도 안 보이고,
       스크롤(focusMsg) 뒤에 열면 높이가 바뀌어 위치가 어긋난다. */
    var folds = root.querySelectorAll("details.qfold");
    for (i = 0; i < folds.length; i++) {
      var ft = (folds[i].textContent || "").toLowerCase();
      for (var j = 0; j < words.length; j++)
        if (ft.indexOf(words[j]) > -1) { folds[i].open = true; break; }
    }
    var ranges = [], walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), node;
    while (ranges.length < HL_MAX && (node = walker.nextNode())) {
      var low = node.nodeValue.toLowerCase();
      for (var k = 0; k < words.length && ranges.length < HL_MAX; k++) {
        var at = low.indexOf(words[k]);
        while (at > -1 && ranges.length < HL_MAX) {
          var r = document.createRange();
          r.setStart(node, at);
          r.setEnd(node, at + words[k].length);
          ranges.push(r);
          at = low.indexOf(words[k], at + words[k].length);
        }
      }
    }
    if (!ranges.length) return;
    var h = new Highlight();
    for (i = 0; i < ranges.length; i++) h.add(ranges[i]);
    CSS.highlights.set(HL_NAME, h);
    hlOn = true;
  }

  /* 검색·목록에서 연 메일로 스크롤 + 잠깐 강조. inject 가 방금 scrollTop=0 으로
     맨 위(역순이라 최신)로 리셋하므로 그 뒤에 부른다. id 는 메시지 id(정수). */
  function focusMsg(pane, id) {
    if (!id || !/^[0-9]+$/.test("" + id)) return;
    var host = paneEl(pane), el = document.getElementById("msg-" + id);
    if (!el || (host && !host.contains(el))) return;
    if (pane === "right") msgCurId = el.id;      /* n/p 커서를 여기서 이어가게 */
    el.classList.add("focusmsg");
    if (el.scrollIntoView) el.scrollIntoView({ block: "start" });
    setTimeout(function () { el.classList.remove("focusmsg"); }, 2800);
  }

  function markSelected() {
    var m = (location.pathname.match(/^\/thread\/(\d+)/) || [])[1];
    /* 질문 이력은 /ask?id=N 이 곧 선택 항목 (스레드와 달리 id 가 쿼리에 있다) */
    var askId = location.pathname.indexOf("/ask") === 0
      ? new URLSearchParams(location.search).get("id") : null;
    var links = left.getElementsByTagName("a");
    for (var i = 0; i < links.length; i++) {
      var row = links[i].closest(".item, .digest, .mrow");
      if (!row) continue;
      var href = links[i].getAttribute("href") || "";
      if (askId && href === "/ask?id=" + askId) {
        row.classList.add("selected");
        continue;
      }
      /* href 에 ?focus=… 가 붙을 수 있으므로 경로만 비교(쿼리 무시) */
      var hp = href.split("?")[0];
      if (m && hp === "/thread/" + m) {
        row.classList.add("selected");
        if (row.classList.contains("mrow")) row.classList.add("read");  /* 열람=읽음: 목록 볼드 즉시 해제 */
      } else {
        row.classList.remove("selected");
      }
    }
  }

  /* ---- 상단 nav: 현재 위치한 최상위 메뉴에 밑줄(active) ---- */
  function navTarget(path) {
    path = (path || "/").replace(/\/+$/, "") || "/";
    if (path === "/") return "/";
    if (path.indexOf("/ask") === 0) return "/";  /* 홈=분석 — /ask* 도 홈 밑줄 */
    if (path === "/daily") return "/records";   /* 구 데일리 경로 → 기억 메뉴 */
    var tops = ["/mail", "/threads", "/people", "/search", "/records",
                "/stats", "/settings"];
    for (var i = 0; i < tops.length; i++) {
      if (path === tops[i] || path.indexOf(tops[i] + "/") === 0) return tops[i];
    }
    return null;  /* /thread, /person 등 하위 화면은 직전 메뉴 유지 */
  }
  /* 헤더 검색창 = '새 검색' 런처 — 이동하면 비운다(현재 질의는 /search 페이지의
     검색창이 담고 편집한다). 결과는 URL(/search?q=…)에 있어 '뒤로'로 복원된다. */
  function syncNavSearch() {
    var inp = document.querySelector("header.top .navsearch input");
    if (!inp || inp === document.activeElement) return;   /* 입력 중이면 건드리지 않음 */
    inp.value = "";
  }

  function markNav() {
    syncNavSearch();
    var target = navTarget(location.pathname);
    if (!target) return;
    var nav = document.querySelector("header.top nav");
    if (!nav) return;
    var links = nav.getElementsByTagName("a");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("href") === target) links[i].classList.add("active");
      else links[i].classList.remove("active");
    }
  }

  /* ---- 목록 추가 로딩(#5): 센티널이 보이면 다음 배치를 이어 붙임 */
  function hookMore() {
    var m = left.querySelector(".more[data-more]");
    if (!m || m._hooked) return;
    m._hooked = true;
    if (!window.IntersectionObserver) return;  /* 폴백: '더 보기' 링크 */
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        io.disconnect();
        var u = new URL(m.getAttribute("data-more"), location.origin);
        u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
        fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
          .then(function (r) { return r.text(); })
          .then(function (html) {
            m.insertAdjacentHTML("beforebegin", html);
            m.remove();
            markSelected();
            hookMore();               /* 새 센티널에 재장착 */
          })
          .catch(function () { m.remove(); });
        return;
      }
    }, { root: left, rootMargin: "240px" });
    io.observe(m);
  }

  /* ---- 리뷰 백그라운드 잡 폴링 (setTimeout 체인 — 중첩 방지) ----
     진행 중엔 카드 슬롯만 패치해 스피너·경과가 끊기지 않게 하고,
     완료 응답이 오면 전체 교체한다. */
  function hookReviewPolling(root) {
    /* 소유 패널(우측)이 비었을 때만 t0 를 지운다 — inject() 는 폴링 훅 6종을
       주입된 패널로 모두 부르므로, 남의 패널을 갱신했다고 경과가 되감기면 안 된다 */
    if (!root.querySelector("[data-review-running]")) {
      if (!right.querySelector("[data-review-running]")) jobT0.rv = 0;
      return;
    }
    jobElapsed("rv", "#rv-elapsed", right);
    setTimeout(function () {
      var u = new URL("/review/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!right.querySelector("[data-review-running]")) return; /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-review-running]")) {
            inject("right", html, null);            /* 완료 → 결과 화면으로 교체 */
            return;
          }
          patchJob(tmp, right, "rv");
          hookReviewPolling(right);                 /* 다음 폴링 예약 */
        })
        .catch(function () { hookReviewPolling(right); });
    }, 2000);
  }

  /* ---- AI 검색 백그라운드 잡 폴링 (좌측 패널) ----
     진행 중엔 카드 슬롯(단계 바·문구·잠정 결과)만 패치해 스피너·경과가
     끊기지 않게 하고, 완료/에러 응답이 오면 전체 교체한다. */
  function hookAiPolling(root) {
    if (!root.querySelector("[data-aisearch-running]")) {
      if (!left.querySelector("[data-aisearch-running]")) jobT0.ai = 0;
      return;
    }
    jobElapsed("ai", "#ai-elapsed");
    setTimeout(function () {
      var u = new URL("/aisearch/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!left.querySelector("[data-aisearch-running]")) return; /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-aisearch-running]")) {
            inject("left", html, null);             /* 완료·에러 → 결과로 교체 */
            return;
          }
          patchJob(tmp, left, "ai");
          hookAiPolling(left);                        /* 다음 폴링 예약 */
        })
        .catch(function () { hookAiPolling(left); });
    }, 1500);
  }

  /* ---- 폴링 fragment 의 텍스트만 제자리 패치 — 전체 교체는 스크롤·포커스를 깬다 */
  function patchText(tmp, host, sel) {
    var n = tmp.querySelector(sel), o = host.querySelector(sel);
    if (n && o) o.textContent = n.textContent;
  }

  /* ---- 대기 카드 공통 패치 — 잡이 달라도 슬롯 규약이 같다(#{prefix}-*).
     텍스트 4종 + 진행바(클래스·폭) + HTML 슬롯(검색 잠정 결과). 카드를 통째로
     갈아끼우면 스피너 애니메이션과 경과 카운터가 매 폴링마다 되감긴다. */
  function patchJob(tmp, host, p) {
    patchText(tmp, host, "#" + p + "-stage");
    patchText(tmp, host, "#" + p + "-live");     /* 수신·재시도·실패·무수신 */
    patchText(tmp, host, "#" + p + "-preview");  /* 작성 중 초안(검증 전) */
    patchText(tmp, host, "#" + p + "-model");    /* 실모델 배지 — 잡 끝까지 유지 */
    var nf = tmp.querySelector("#" + p + "-fill"),
        of = host.querySelector("#" + p + "-fill");
    if (nf && of) { of.className = nf.className; of.style.width = nf.style.width; }
    var ne = tmp.querySelector("#" + p + "-extra"),
        oe = host.querySelector("#" + p + "-extra");
    if (ne && oe && ne.innerHTML !== oe.innerHTML) oe.innerHTML = ne.innerHTML;
  }

  /* ---- 잡 경과 초 — AI CLI 는 호출당 수십 초라, 숫자가 없으면 멈춘 걸로 보인다 */
  var jobT0 = {};
  function jobElapsed(key, sel, host) {
    var el = (host || left).querySelector(sel);
    if (!el) return;
    /* 잡 시작 시각(data-since)이 있으면 그것으로 센다 — 패널이 다시 그려지거나
       페이지가 리로드돼도 경과가 0 으로 되돌아가지 않는다(2026-07-29: 조사
       중간에 '0초 경과'로 되감기던 증상). 없으면 클라이언트 기준선으로 폴백. */
    var since = parseInt(el.getAttribute("data-since"), 10);
    if (since > 0) {
      el.textContent = Math.max(0, Math.round(Date.now() / 1000 - since));
      return;
    }
    if (!jobT0[key]) jobT0[key] = Date.now();
    el.textContent = Math.round((Date.now() - jobT0[key]) / 1000);
  }

  /* ---- 질문하기 잡 폴링 (우측) — 조사 진행 문구만 갈아끼우고, 끝나면 답변으로 */
  function hookAskPolling(root) {
    /* 답변은 우측(읽기) 패널 — 좌측은 질문 이력이 그대로 남는다 */
    var running = root.querySelector("[data-ask-running]");
    if (!running) {
      if (!right.querySelector("[data-ask-running]")) jobT0.ask = 0;
      return;
    }
    jobElapsed("ask", "#ask-elapsed", right);
    setTimeout(function () {
      var u = new URL("/ask/status", location.origin);
      var token = running.getAttribute("data-ask-job");
      if (token) u.searchParams.set("job", token);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!right.querySelector("[data-ask-running]")) return;  /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-ask-running]")) {
            inject("right", html, null);               /* 완료·에러 → 답변으로 교체 */
            var done = tmp.querySelector("[data-ask-result-id]");
            if (done) {
              var rid = done.getAttribute("data-ask-result-id");
              if (/^[0-9]+$/.test(rid || ""))
                replacePaneUrl("right", "/ask?id=" + rid, "/ask?id=" + rid);
            }
            load("/ask/list", "left", false).then(function () {
              replacePaneUrl("left", "/ask");
            }).catch(function () {});                /* 대화 목록에 새 대화 반영 */
            return;
          }
          patchJob(tmp, right, "ask");
          if (right.scrollHeight - right.scrollTop - right.clientHeight < 80)
            right.scrollTop = right.scrollHeight;      /* 바닥 근처면 계속 따라감 */
          hookAskPolling(right);                       /* 다음 폴링 예약 */
        })
        .catch(function () { hookAskPolling(right); });
    }, 1500);
  }

  /* ---- 주간 보고 잡 폴링 (좌측) — 진행 문구만 갈아끼우고, 끝나면 보고로 교체 */
  function hookWeeklyPolling(root) {
    if (!root.querySelector("[data-weekly-running]")) {
      if (!left.querySelector("[data-weekly-running]")) jobT0.weekly = 0;
      return;
    }
    jobElapsed("weekly", "#wk-elapsed");
    setTimeout(function () {
      var u = new URL("/weekly/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!left.querySelector("[data-weekly-running]")) return;  /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-weekly-running]")) {
            inject("left", html, null);               /* 완료·에러 → 보고로 교체 */
            return;
          }
          patchJob(tmp, left, "wk");
          hookWeeklyPolling(left);                    /* 다음 폴링 예약 */
        })
        .catch(function () { hookWeeklyPolling(left); });
    }, 1500);
  }

  /* ---- 인물 요약 잡 폴링 (좌측) — 끝나면 인물 화면 전체로 교체 */
  function hookDossierPolling(root) {
    var running = root.querySelector("[data-dossier-running]");
    if (!running) {
      if (!left.querySelector("[data-dossier-running]")) jobT0.dz = 0;
      return;
    }
    jobElapsed("dz", "#dz-elapsed");
    setTimeout(function () {
      var u = new URL("/people/dossier/status", location.origin);
      var addr = running.getAttribute("data-dossier-addr");
      if (addr) u.searchParams.set("addr", addr);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!left.querySelector("[data-dossier-running]")) return;  /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-dossier-running]")) {
            inject("left", html, null);              /* 완료 → 요약 카드로 교체 */
            return;
          }
          patchJob(tmp, left, "dz");
          hookDossierPolling(left);                  /* 다음 폴링 예약 */
        })
        .catch(function () { hookDossierPolling(left); });
    }, 1500);
  }

  /* ---- 동기화 백그라운드 잡 폴링 (수동 '메일 동기화' — 우측 대기화면) ----
     완료되면 결과 화면으로 교체하고 '신규 N · 중복 M' 토스트도 띄운다. */
  function hookSyncPolling(root) {
    if (!root.querySelector("[data-sync-running]")) {
      if (!right.querySelector("[data-sync-running]")) jobT0.sy = 0;
      return;
    }
    jobElapsed("sy", "#sy-elapsed", right);   /* 카드에 경과 슬롯이 있으면 돌려야 한다 */
    setTimeout(function () {
      var u = new URL("/sync/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 — 같은 URL 반복 폴링 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!right.querySelector("[data-sync-running]")) return; /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-sync-running]")) {
            inject("right", html, null);            /* 완료 → 결과 화면 */
            var m = tmp.querySelector("[data-sync-msg]");
            if (m && m.getAttribute("data-sync-msg")) {
              toast(m.getAttribute("data-sync-msg")); checkFresh();
            }
            return;
          }
          hookSyncPolling(right);
        })
        .catch(function () { hookSyncPolling(right); });
    }, 2000);
  }

  /* ---- 자동 동기화 완료 감시(무화면) — 잡이 끝나면 '새 메일 N통' 토스트 ---- */
  function watchSyncToast() {
    var u = new URL("/sync/status", location.origin);
    u.searchParams.set("frag", "1");
    u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 */
    fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var tmp = document.createElement("div");
        tmp.innerHTML = html;
        if (tmp.querySelector("[data-sync-running]")) {
          setTimeout(watchSyncToast, 3000); return;   /* 아직 진행 중 */
        }
        /* 자동 주기 동기화는 '신규>0' 일 때만 토스트 — 매 주기 '신규 0' 알림 방지(구 동작). */
        var m = tmp.querySelector("[data-sync-msg]");
        var n = m ? (parseInt(m.getAttribute("data-sync-n"), 10) || 0) : 0;
        if (n > 0) { toast("새 메일 " + n + "통"); checkFresh(); }
      })
      .catch(function () {});
  }

  /* ---- fragment 로드 ---- */
  function load(url, pane, push) {
    var u = new URL(url, location.origin);
    u.searchParams.set("frag", "1");
    return fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) {
        return res.text().then(function (html) {
          var fin = new URL(res.url, location.origin);
          var msg = fin.searchParams.get("msg");
          var focus = fin.searchParams.get("focus");   /* 검색·목록에서 이 메일로 스크롤 */
          fin.searchParams.delete("frag");
          fin.searchParams.delete("msg");
          var p = pane || paneFor(fin.pathname);
          var clean = fin.pathname + (fin.search || "");
          inject(p, html, push === false ? null : clean);
          if (msg) toast(msg);
          /* 강조 먼저(접힌 인용을 펴 높이를 확정) → 그다음 스크롤 */
          if (p === "right") applyHl(fin.searchParams.get("hl"));
          if (focus) focusMsg(p, focus);
          /* 홈(/)·분석(/ask)을 우측에 열면 좌측엔 대화 이력 — 메뉴 클릭은 우측만
             갱신하므로 목록이 아직 없을 때 함께 채운다(F5 는 서버 _panes 담당).
             이미 목록이면 유지(스크롤 보존) — /ask/list 자체는 p=left 라 재귀 없음 */
          if (p === "right"
              && (fin.pathname.indexOf("/ask") === 0 || fin.pathname === "/")
              && !restoringHistory
              && !left.querySelector(".asklisthd"))
            load("/ask/list", "left", false)
              .then(function () {
                noteLeft("/ask");                       /* 좌측 실제 상태도 이력 스냅숏에 반영 */
                history.replaceState(historyState(), "", location.pathname + location.search);
              })
              .catch(function () {});
        });
      });
  }

  /* ---- nav 뒤로(←): history.back() — popstate 가 좌우 패널을 함께 복원.
     앱 이력이 없으면(depth=0) 외부로 나가지 않고 no-op ---- */
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest(".navback") && appDepth > 0) history.back();
  });

  /* ---- 링크 가로채기: 내부 링크는 해당 패널만 갱신 ---- */
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 ||
        e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest("a") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (href.charAt(0) !== "/" || href.slice(0, 2) === "//") return;
    if (href === "/stats" || href.slice(0, 7) === "/stats?") return; /* 통계 = 전폭 페이지, 일반 이동 */
    if (a.closest(".more")) return; /* '더 보기'는 관찰자/전체 페이지 폴백이 처리 */
    e.preventDefault();
    if (a.classList && a.classList.contains("mrow")) a.classList.add("read");  /* 낙관적: 클릭 즉시 볼드 해제 */
    if (a.classList && a.classList.contains("aibtn")
        && href.slice(0, 8) === "/search?" && href.indexOf("ai=1") > -1) {
      /* AI 검색만 — 즉시 진행 표시(서버 대기화면이 곧 대체). 같은 aibtn 스타일의
         /ask 링크(＋신규 분석·브리핑)는 우측 패널행이라 좌측을 덮으면 안 됨 */
      var li = left.querySelector(".inner") || left;
      /* 서버 대기 화면과 같은 카드 골격 — 곧 대체되므로 모양이 튀면 안 된다 */
      li.innerHTML =
        "<div data-aisearch-running='1' hidden></div>"
        + "<div class='waitcard'><div class='waithead'><div class='spin'></div>"
        + "<div class='aiwaitmsg'>AI가 찾고 있어요</div></div>"
        + "<div class='rvbar'><div class='rvfill' id='ai-fill' style='width:33%'>"
        + "</div></div>"
        + "<p class='aiwaitsub waitslot' id='ai-stage'>"
        + "질문을 검색식으로 번역하는 중 · 단계 1/3</p>"
        + "<div class='waitmeta'><span class='aiwaittime'>"
        + "<span id='ai-elapsed'>0</span>초 경과 · "
        + "본문까지 읽어 후보를 확정합니다</span></div></div>";
      /* 경과 소유자는 jobElapsed 하나 — 타이머를 따로 두면 t0 가 갈려 첫 폴링에
         숫자가 뒤로 점프한다 */
      jobT0.ai = Date.now();
    }
    load(href).catch(function () { location.href = href; });
  });

  /* ---- #21 마크다운 토글: 기본 서식 ↔ 저장 텍스트 show/hide (서버가 둘 다 실어줌) */
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".md-toggle") : null;
    if (!b) return;
    var box = b.closest(".mthread");
    if (!box) return;
    e.preventDefault();
    var on = box.classList.toggle("md-on");
    b.textContent = on ? "서식 보기" : "텍스트 보기";
  });

  /* ---- #16 폼 가로채기: 전체 화면이 좌측으로 리셋되지 않게 ---- */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.getAttribute) return;
    var action = form.getAttribute("action") || location.pathname;
    if (action.charAt(0) !== "/") return;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    e.preventDefault();
    /* 분석 대화 삭제 — 문답 전체가 지워지므로 한 번 확인. 열려 있던 대화를
       지우면 우측도 비운다(주소의 ?id= 는 inject 가 갈아끼우기 전에 읽는다) */
    var delAsk = form.classList && form.classList.contains("askdel");
    if (delAsk && !window.confirm("이 대화(문답 전체)를 삭제할까요?")) return;
    var delOpen = false;
    if (delAsk) {
      var di = form.querySelector("input[name='id']");
      delOpen = !!di && new URLSearchParams(location.search).get("id") === di.value;
    }
    var btns = form.querySelectorAll("button");
    function setDisabled(v) {
      for (var i = 0; i < btns.length; i++) {
        btns[i].disabled = v;
        if (v) btns[i].setAttribute("aria-busy", "true");
        else btns[i].removeAttribute("aria-busy");
      }
    }
    setDisabled(true);                       /* 이중 제출 방지 */
    var done = function () { setDisabled(false); };
    if (method === "get") {
      var q = new URLSearchParams(new FormData(form)).toString();
      load(action + (q ? "?" + q : "")).then(done, function () {
        done(); location.href = action + (q ? "?" + q : "");
      });
      return;
    }
    fetch(action, {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      headers: { "X-Requested-With": "fetch" },
    }).then(function (res) {
      return res.text().then(function (html) {
        if (res.status === 403) {            /* 차단 안내는 우측에 그대로 표시 */
          inject("right", html, null);
          return;
        }
        var fin = new URL(res.url, location.origin);
        var msg = fin.searchParams.get("msg");
        fin.searchParams.delete("frag");
        fin.searchParams.delete("msg");
        var p = paneFor(fin.pathname);
        /* 303 을 따라온 경우만 주소 갱신 — 직접 200 응답이면 현재 주소 유지 */
        inject(p, html, res.redirected ? fin.pathname + (fin.search || "") : null);
        if (msg) toast(msg);
        if (delOpen) load("/ask", "right", false)
          .then(function () { replacePaneUrl("right", "/ask"); })
          .catch(function () {});  /* 지운 대화록 비움 */
        /* 본문 글자 크기 — 저장 즉시 CSS 변수 갱신(새로고침 불요).
           서버 클램프(최소 12)와 같은 규칙으로 미러링한다 */
        if (action === "/settings/save") {
          var ff = form.querySelector("input[name='reading_font']");
          var fv = ff ? parseInt(ff.value, 10) : 0;
          if (fv > 0) {
            fv = Math.max(12, fv);
            var rt = document.documentElement;
            rt.style.setProperty("--read-fs", fv + "px");
            rt.style.setProperty("--read-zoom", String(fv / 16));
          }
        }
        /* 스레드 상태 변경(플래그·숨김·신호 해제/복원)은 왼쪽(홈·메일함·
           스레드 목록)에도 즉시 반영 — 큐·탭 카운트·확인 후보가 같이 갱신된다 */
        if (leftCur &&
            /\/thread\/\d+\/(flag|unflag|hide|unhide)$/.test(action)) {
          var sc = left.scrollTop;
          load(leftCur, "left", false)
            .then(function () { left.scrollTop = sc; })   /* 스크롤 유지 */
            .catch(function () {});
        }
      });
    }).then(done, function (err) {
      done();
      /* 네트워크 실패(TypeError)만 네이티브 폴백 — 서버는 아직 처리 전이다.
         주입 단계 오류에서 재제출하면 동작이 중복 실행될 수 있어 제외. */
      if (err && err.name === "TypeError") form.submit();
    });
  }, true);

  /* ---- 브라우저 뒤로/앞으로 — 당시 좌·우 패널을 함께 복원 ---- */
  window.addEventListener("popstate", function (e) {
    var st = e.state;
    if (!st || st.minerva !== 1) {
      var p = location.pathname + (location.search || "");
      load(p, null, false).catch(function () { location.reload(); });
      return;
    }
    appDepth = Math.max(0, parseInt(st.depth, 10) || 0);
    var wantLeft = st.leftUrl || "";
    var wantRight = st.rightUrl || "";
    var jobs = [];
    restoringHistory = true;
    if (wantLeft && wantLeft !== leftCur) jobs.push(load(wantLeft, "left", false));
    if (wantRight !== rightCur) {
      if (wantRight) jobs.push(load(wantRight, "right", false));
      else inject("right", rightBlankHtml, null);
    }
    Promise.all(jobs).then(function () {
      leftCur = wantLeft;
      rightCur = wantRight;
      restoringHistory = false;
      markSelected();
      markNav();
    }).catch(function () { restoringHistory = false; location.reload(); });
  });

  /* ---- 키보드 네비게이션: j/k 로 이동하면 바로 열람(우측에 표시) ---- */
  function navRows() {
    /* 보이는 행만 — 접힌 <details>(확인 후보 폴드) 안 행이 들어오면 j/k 커서가
       화면 밖에서 돌고 f/h 가 안 보이는 행에 동작한다. offsetParent 는
       display:none(접힘 포함) 조상 아래에서 null. */
    if (!left) return [];
    return Array.prototype.slice.call(
      left.querySelectorAll(".mrow, .item, .digest"))
      .filter(function (r) { return r.offsetParent !== null; });
  }
  function curIdx(rows) {
    var i;
    for (i = 0; i < rows.length; i++)
      if (rows[i].classList.contains("kbd")) return i;
    /* 키보드 커서가 없으면 '지금 열린(마우스로 클릭한) 항목'을 기준으로 */
    for (i = 0; i < rows.length; i++)
      if (rows[i].classList.contains("selected")) return i;
    return -1;
  }
  function focusRow(rows, i) {          /* 이동 = 커서 이동 + 즉시 열람 */
    for (var k = 0; k < rows.length; k++) rows[k].classList.remove("kbd");
    if (i < 0 || i >= rows.length) return;
    var el = rows[i];
    el.classList.add("kbd");
    if (el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    var a = (el.matches && el.matches("a")) ? el
      : (el.querySelector ? el.querySelector("a[href^='/']") : null);
    if (a) a.click();                   /* 위임 클릭 핸들러가 load + 낙관적 읽음까지 처리 */
  }
  /* ---- 목록 단축키(f 플래그 · h 숨김): 서버가 상태 토글, 결과 토큰으로
     상태명 토스트 + 좌측 목록 갱신 후 커서 복원(살아있으면 머무름, 사라졌으면 다음 행) ---- */
  function tokenToast(tok) {
    return { "flag:on": "🚩 플래그", "flag:off": "플래그 해제",
             "hide:on": "🙈 숨김", "hide:off": "숨김 해제",
             }[tok] || "";
  }
  function restoreKbd(tid, i) {
    var rows = navRows(); if (!rows.length) return;
    var el = left ? left.querySelector("a[href='/thread/" + tid + "']") : null;
    var row = el && el.closest ? el.closest(".mrow, .item, .digest") : null;
    var j = (row && rows.indexOf(row) >= 0) ? rows.indexOf(row)   /* 남았으면 그 행 */
          : Math.max(0, Math.min(i, rows.length - 1));            /* 사라졌으면 다음(끝이면 이전) */
    for (var a = 0; a < rows.length; a++) rows[a].classList.remove("kbd");
    rows[j].classList.add("kbd");
    if (rows[j].scrollIntoView) rows[j].scrollIntoView({ block: "nearest" });
  }
  function openTid() {
    /* 우측 상세에 열린 스레드 id. 주소가 1순위, 좌측 목록으로 이동해 주소가
       바뀐 뒤에도 우측에 상세가 남아 있으면 그 조작 폼(action)에서 읽는다. */
    var m = location.pathname.match(/^\/thread\/(\d+)/);
    if (m) return m[1];
    var f = right.querySelector("form[action^='/thread/']");
    m = f && f.getAttribute("action").match(/^\/thread\/(\d+)/);
    return m ? m[1] : null;
  }
  function toggleRow(kind) {
    /* 대상 = 우측에 열린 스레드(보고 있는 것) 1순위 — 우측 안에서 다른 스레드로
       이동해 목록 커서와 어긋나도 화면의 메일에 동작한다. 없으면 커서 행. */
    var rows = navRows(), i = curIdx(rows), tid = openTid();
    if (!tid && i >= 0) {
      var row = rows[i];                              /* .mrow 는 행 자체가 <a> (focusRow 와 동일 처리) */
      var a = (row.matches && row.matches("a[href^='/thread/']")) ? row
            : (row.querySelector ? row.querySelector("a[href^='/thread/']") : null);
      var m = a && a.getAttribute("href").match(/\/thread\/(\d+)/);
      if (m) tid = m[1];
    }
    if (!tid) return false;
    fetch("/thread/" + tid + "/" + kind + "-toggle",
          { method: "POST", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.text(); })
      .then(function (tok) {
        toast(tokenToast(tok));
        if (openTid() === tid) {          /* 우측 상세 동기화 — 플래그·숨김 */
          var rsc = right.scrollTop;
          load("/thread/" + tid, "right", false)   /* 주소는 그대로(push 없음) */
            .then(function () { right.scrollTop = rsc; })   /* 읽던 위치 유지 */
            .catch(function () {});
        }
        var cur = leftCur || (location.pathname + (location.search || ""));
        load(cur, "left", false)
          .then(function () { if (i >= 0) restoreKbd(tid, i); })
          .catch(function () {});
      }).catch(function () {});
    return true;
  }
  /* ---- 읽기 창(#right) 스크롤: 문서는 height:100% 라 안 움직인다 — 패널을 직접 민다 */
  function paneAtBottom() {
    return right.scrollTop + right.clientHeight >= right.scrollHeight - 4;
  }
  function paneScroll(frac) {          /* frac: +아래 / -위 (창 높이 비율) */
    right.scrollBy({ top: Math.round(right.clientHeight * frac), behavior: "smooth" });
  }
  var msgCurId = null;                /* n/p 커서 — 우측 내용이 갈리면 리셋 */
  function msgNav(dir) {
    /* 스레드 타임라인(최신 먼저)에서 n=아래(과거)/p=위(최신) 메시지로 이동.
       현재 위치는 '커서 상태'로 기억한다 — 스크롤 위치 추정은 짧은 스레드
       (전체가 한 화면, 스크롤 없음)에서 메시지 top 이 앵커를 못 넘어 영영
       같은 메시지만 맴돌았다(#56·57). 커서가 없으면 화면 위치로 1회 추정.
       목록 화면은 no-op. 더 갈 곳이 없으면 창 끝까지 스크롤 — 먹통 방지. */
    var msgs = right.querySelectorAll(".mthread .msg");
    if (!msgs.length) return false;
    var cur = -1, i;
    for (i = 0; i < msgs.length; i++)
      if (msgs[i].id === msgCurId) { cur = i; break; }
    if (cur < 0) {                    /* 첫 사용 — 앵커 위로 지나간 마지막 메시지 */
      var th = right.querySelector(".threadhead");
      var anchor = right.getBoundingClientRect().top
                 + Math.max(th ? th.offsetHeight : 0, 72) + 6;  /* scroll-margin 만큼 여유 */
      for (i = 0; i < msgs.length; i++)
        if (msgs[i].getBoundingClientRect().top <= anchor) cur = i; else break;
    }
    var j = Math.max(0, Math.min(cur + dir, msgs.length - 1));
    if (j !== cur) focusMsg("right", msgs[j].id.slice(4));
    else right.scrollTo({ top: dir > 0 ? right.scrollHeight : 0, behavior: "smooth" });
    return true;
  }
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (e.key === "Escape") {  /* 입력란 탈출('/' 의 짝) — 그 외 Esc 는 무동작 */
      if (selBtn) { hideSelBtn(); e.preventDefault(); return; }   /* 선택 버튼 먼저 닫는다 */
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) {
        t.blur(); e.preventDefault();
      } else if (hlOn) {
        clearHl(); e.preventDefault();   /* 입력란 탈출이 먼저 — 강조 끄기는 맨 뒤 */
      }
      return;
    }
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable))
      return;                  /* 입력 중엔 개입 안 함 */
    /* 선택 검색 버튼이 떠 있는 동안은 그쪽이 키를 갖는다. 이 가드가 없으면
       본문을 골라 놓고 Space 를 누르는 순간 읽기 창이 넘어간다(선택은 포커스가
       아니라 위의 입력란 가드에 안 걸린다). */
    if (selBtn) {
      if (e.key === "Enter") { e.preventDefault(); runSelSearch(); return; }
      if (e.key === " " || e.key === "Spacebar") { hideSelBtn(); return; }
    }
    var k = e.key, rows, i;
    if (k === " " || k === "Spacebar") {
      /* 읽기 창 페이지 스크롤(메일 클라이언트 관례). 바닥이면 다음 메일로.
         스페이스로 '활성화'되는 것(버튼·summary)에 포커스가 있을 때만 양보한다.
         <a> 는 스페이스로 활성화되지 않으므로 제외 — 목록 행이 <a> 라, 여기에
         양보하면 기본 동작이 왼쪽 패널을 넘겨버린다(관측된 버그). */
      if (t && t.closest && t.closest("button, summary, [role='button']")) return;
      e.preventDefault();
      if (e.shiftKey) { if (right.scrollTop <= 0) msgNav(-1); else paneScroll(-0.9); }
      else if (paneAtBottom()) msgNav(1);
      else paneScroll(0.9);
      return;
    }
    if (k === "/") {           /* '/' → 헤더 검색창 포커스 (검색이 1순위 동작) */
      var s = document.querySelector("header.top .navsearch input");
      if (s) { e.preventDefault(); s.focus(); s.select(); }
      return;
    }
    if (k === "j") {
      rows = navRows(); if (!rows.length) return;
      e.preventDefault(); i = curIdx(rows);
      focusRow(rows, i < 0 ? 0 : Math.min(i + 1, rows.length - 1));
    } else if (k === "k") {
      rows = navRows(); if (!rows.length) return;
      e.preventDefault(); i = curIdx(rows);
      focusRow(rows, i < 0 ? rows.length - 1 : Math.max(i - 1, 0));
    } else if (k === "f") {        /* 플래그 토글 */
      if (toggleRow("flag")) e.preventDefault();
    } else if (k === "h") {        /* 숨김 토글 */
      if (toggleRow("hide")) e.preventDefault();
    } else if (k === "n") {        /* 스레드 안 다음(과거) 메일 */
      if (msgNav(1)) e.preventDefault();
    } else if (k === "p") {        /* 스레드 안 이전(최신) 메일 */
      if (msgNav(-1)) e.preventDefault();
    }
  });

  /* 마우스로 연 항목을 키보드 커서(kbd)에 동기화 — j/k 기준 = 마지막 클릭 항목.
     focusRow 의 합성 클릭(isTrusted=false)은 무시해 키보드 커서를 덮지 않는다. */
  document.addEventListener("click", function (e) {
    if (!e.isTrusted || !left) return;
    var row = e.target.closest ? e.target.closest(".mrow, .item, .digest") : null;
    if (!row || !left.contains(row)) return;
    var rows = navRows();
    for (var i = 0; i < rows.length; i++) rows[i].classList.remove("kbd");
    row.classList.add("kbd");
  });

  /* ---- '← 뒤로' = 왼쪽 프레임의 이전 항목으로 (없으면 브라우저 히스토리) ---- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".backlink") : null;
    if (!b) return;
    e.preventDefault();
    if (!leftBack()) history.back();
  });

  /* ---- 화면 테마(라이트/다크): 즉시 <html data-theme> 적용 + 서버에 영구화 ---- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-set-theme]") : null;
    if (!b) return;
    e.preventDefault();
    var val = b.getAttribute("data-set-theme") === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", val);
    var picks = document.getElementsByClassName("themebtn");
    for (var i = 0; i < picks.length; i++) {
      var sel = picks[i].getAttribute("data-set-theme") === val;
      picks[i].classList.toggle("active", sel);
      picks[i].setAttribute("aria-pressed", sel ? "true" : "false");
    }
    fetch("/settings/theme", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "fetch" },
      body: "theme=" + encodeURIComponent(val)
    }).catch(function () {});
  });

  /* ---- 화면 스킨(클래식/벤토): 즉시 <html data-skin> 적용 + 서버에 영구화 ---- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-set-skin]") : null;
    if (!b) return;
    e.preventDefault();
    var val = b.getAttribute("data-set-skin") === "bento" ? "bento" : "classic";
    document.documentElement.setAttribute("data-skin", val);
    var picks = document.querySelectorAll("[data-set-skin]");
    for (var i = 0; i < picks.length; i++) {
      var sel = picks[i].getAttribute("data-set-skin") === val;
      picks[i].classList.toggle("active", sel);
      picks[i].setAttribute("aria-pressed", sel ? "true" : "false");
    }
    fetch("/settings/skin", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "fetch" },
      body: "skin=" + encodeURIComponent(val)
    }).catch(function () {});
  });

  var initialUrl = location.pathname + (location.search || "");
  var initialPane = paneFor(location.pathname);
  leftCur = (initialPane === "left")
    ? initialUrl
    : (/^\/thread\/\d+/.test(location.pathname) ? "/threads" : "/ask");
    /* 우측 페이지의 좌측 기본은 분석 이력(서버 _panes 와 동일) — 홈(/)=분석 */
  rightCur = initialPane === "right" ? initialUrl : "";
  history.replaceState(historyState(), "", initialUrl);
  markSelected();
  markNav();
  hookReviewPolling(right);
  hookAiPolling(left);
  hookWeeklyPolling(left);
  hookDossierPolling(left);
  hookAskPolling(right);        /* 조사 대기화면은 우측(대화록) — F5 복원도 우측을 본다 */
  hookSyncPolling(right);
  hookMore();
  hookThreadHead();
  /* 전체 로드로 /thread/…?focus=N 을 열었을 때(새로고침·직접 URL)도 그 메일로 스크롤 */
  applyHl(new URLSearchParams(location.search).get("hl"));
  focusMsg("right", new URLSearchParams(location.search).get("focus"));
})();
"""


# ─────────────────────────────────────────────────── 데일리 생성(백그라운드)

def _job_progress(msg: str) -> None:
    """단계 진행을 상태줄에 반영 — /review/status 폴링이 실시간 표시(#13).
    각 단계 시작마다 step 을 올려 프로그레스 바(step/total)를 채운다."""
    with _review_lock:
        if _review_job["running"]:
            _review_job["msg"] = msg
            if msg == "완료":
                _review_job["step"] = _review_job["total"]
            else:
                _review_job["step"] += 1


def _run_review_job(cfg, ai: bool, day: str, cancel=None) -> None:
    from . import notes
    if ai:
        _arm_job_backend(_review_job, _review_lock, cfg, cfg.ai_summary_backend)
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            det = review.deterministic(store, cfg, day)
            ai_text = note = None
            if ai:
                # graceful — AI 실패해도 결정론 리뷰는 반드시 저장 (#10)
                try:
                    ai_text, note = review.run_ai_layer(
                        store, cfg, det, persist_date=day,
                        progress=_job_progress,
                        on_event=_job_stream_event(_review_job, _review_lock),
                        cancel=cancel)
                except review.AICancelled:
                    # 중지해도 데일리는 쓴다 — 이미 돌아간 요약·수확은 DB 에
                    # 남았고, 파일을 안 쓰면 그 작업이 화면에서 증발한다.
                    note = "(중지됨 — 결정론 회고만)"
            path = notes.write_daily(cfg, day, review.render(det, ai_text, store))
            if note:
                msg = f"완료: {path.name} · {note}"
            else:
                msg = f"완료: {path.name}" + (" · AI 분석 포함" if ai else "")
        finally:
            store.close()
    except review.AIAuthError as e:   # 인증 만료가 다른 경로로 샜을 때도 안내 문구
        msg = str(e).splitlines()[0][:200]
    except Exception as e:   # AI 계층은 run_ai_layer 가 삼킴 — 여긴 비-AI 오류
        msg = "회고를 만들지 못했습니다 — " + (" ".join(str(e).split())[:120]
                                            or type(e).__name__)
    with _review_lock:
        _review_job.update(running=False, msg=msg)


def _start_review(cfg, ai: bool, day: str) -> bool:
    # run_ai_layer 는 오늘·백필 모두 4단계(요약·수확·디제스트·하루요약) — 진행 바 일치.
    cancel = _job_start(_review_job, _review_lock, msg="준비 중…", step=0,
                        total=4, date=day, ai=ai)
    if cancel is None:
        return False
    threading.Thread(target=_run_review_job, args=(cfg, ai, day, cancel),
                     daemon=True).start()
    return True


def _maybe_auto_review(cfg, today: str, max_rowid: int) -> None:
    """오늘 데일리 리뷰(결정론)를 lazy-on-view 로 배경 재생성한다. 버튼 없이,
    페이지를 막지 않고(논블로킹) 조용히 — 완료되면 /latest 토큰(데일리 mtime 포함)이
    바뀌어 app.js 가 홈을 in-place 로 다시 그린다(사용성 무해).

    재생성 조건: 오늘치 파일이 없거나, 마지막 생성 기준선(MAX rowid) 이후 새 메일이
    들어온 경우. 같은 기준선으로 이미 만들었으면 no-op(매 조회·60초 폴링에도 안전).
    AI 계층은 넣지 않는다 — 비싸므로 일간 회고의 'AI 회고' 버튼에만."""
    exists = (Path(cfg.vault) / "daily" / f"{today}.md").exists()
    with _auto_lock:
        if exists and _auto_review_basis.get(today) == max_rowid:
            return                       # 이 기준선으로 이미 생성됨
    if _start_review(cfg, ai=False, day=today):   # 진행 중 잡 있으면 False → 다음 조회에 재시도
        with _auto_lock:
            _auto_review_basis[today] = max_rowid


def _max_rowid(store) -> int:
    row = store.db.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
    return int(row[0]) if row else 0


# ─────────────────────────────────────────────────── 메일 동기화(백그라운드)

def _do_sync(store, cfg) -> tuple:
    """동기화 1회(수집 + 이미지 프룬). (완료 msg, 신규 통수) 반환.
    수집 실패(Outlook 꺼짐 등)에도 프룬(COM 불필요)은 반드시 실행 — 기존 보장 유지.
    잡 래퍼와 테스트가 공유하는 순수 동작(소켓·스레드 무관)."""
    from .sources import get_source
    src = get_source(cfg.source)                 # outlook 이면 Windows COM
    retain = int(cfg.opt("web", "image_retain_days", default=60) or 0)
    cutoff = image_cutoff_for(retain)
    try:
        stats = store.ingest(src.fetch(store.last_sync(), image_cutoff=cutoff),
                             image_cutoff=cutoff)
    finally:
        store.maybe_prune_html(retain)
    return (f"동기화({src.name}): 신규 {stats.inserted} · 중복 {stats.skipped}",
            stats.inserted)


def _run_sync_job(cfg) -> None:
    """백그라운드 동기화 잡. COM 은 스레드마다 초기화 필요 → 여기서 CoInitialize."""
    com = False
    try:
        import pythoncom      # Windows(pywin32)에서만
        pythoncom.CoInitialize()
        com = True
    except Exception:
        com = False
    msg, n = "", 0
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            msg, n = _do_sync(store, cfg)
        finally:
            store.close()
    except Exception as e:      # 수집 실패는 조용히 기록(다음 주기 재시도)
        msg = "동기화 실패 — " + (" ".join(str(e).split())[:120]
                                or type(e).__name__)
    finally:
        if com:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
    with _sync_lock:
        _sync_job.update(running=False, msg=msg, n=n)


def _start_sync(cfg) -> bool:
    """동기화 잡 시작(단일 슬롯). 이미 실행 중이면 False(로컬 단일 사용자)."""
    if _job_start(_sync_job, _sync_lock, msg="", n=0) is None:
        return False
    threading.Thread(target=_run_sync_job, args=(cfg,), daemon=True).start()
    return True


def render_sync_status(store=None) -> tuple:
    """(inner, running) — 동기화 진행/완료. 완료 msg 는 data-sync-msg 로 실어
    app.js 폴링이 토스트로도 띄운다(수동·자동 공통)."""
    with _sync_lock:
        running, msg, n = _sync_job["running"], _sync_job["msg"], _sync_job["n"]
    if running:
        # 동기화는 AI 가 아니라 끊을 대상도 수신 표시도 없다 — 같은 카드 껍데기를
        # 쓰되 AI 슬롯은 비고(:empty 로 숨음) 중지 버튼도 생략된다.
        return ("<div data-sync-running='1' hidden></div>"
                "<h1>메일 동기화</h1>"
                + _job_wait_card(
                    "sy", "Outlook 에서 새 메일을 가져오는 중",
                    stage="받은편지함·보낸편지함을 훑고 색인합니다.",
                    hint="완료되면 자동으로 알려드려요.",
                    started=_sync_job.get("started") or 0.0), True)
    # data-sync-n: autosync 감시(watchSyncToast)가 신규>0 일 때만 토스트하도록 통수도 실음
    # (수동 /sync 결과 화면은 전체 msg 표시, 자동 주기 토스트는 신규 있을 때만 — 구 동작 유지).
    marker = (f"<div data-sync-msg='{esc(msg)}' data-sync-n='{n}' hidden></div>"
              if msg else "")
    body = f"<div class='flash'>{esc(msg or '동기화 완료')}</div>" if msg else ""
    return (marker + "<h1>메일 동기화</h1>" + body +
            "<p><a href='/'>→ 홈</a> · <a href='/mail'>메일함 보기</a></p>", False)


def render_review_status(store=None):
    """(inner_html, running) — running 이면 do_GET 이 자동 새로고침 붙임.

    완료 화면은 다음 동선(반영 대기 처리)으로 이어지게 장기기억 제안 링크를 단다."""
    with _review_lock:
        st = dict(_review_job)
        running, msg = st["running"], st["msg"]
        step, total = st["step"], st["total"]
        job_date = st.get("date") or ""
    if running:
        # 비-AI 자동 갱신(_maybe_auto_review)도 같은 슬롯을 쓴다 — 제목이 다르지
        # 않으면 사용자가 AI 회고가 도는 줄 착각한다(끊을 대상도 없다).
        ai_job = bool(st.get("ai"))
        # data-review-running: app.js 폴링 훅 마커 (전체 페이지는 meta refresh)
        return ("<div data-review-running='1' hidden></div>"
                + _job_wait_card(
                    "rv",
                    "AI 회고 작성 중" if ai_job else "일간 회고 정리 중",
                    stage=(f"단계 {min(step, total)}/{total} · {msg or ''}"
                           if step else (msg or "준비 중…")),
                    live=_job_live_line(st) if ai_job else "",
                    preview=_job_preview(st) if ai_job else "",
                    model=(st.get("model") or "") if ai_job else "",
                    step=step, total=total if step else 0,
                    hint=("메일을 읽고 장기기억 초안을 준비합니다 — 완료되면 "
                          "자동 전환. " + _cancel_hint(st.get("stream", False)))
                         if ai_job else
                         "새 메일을 반영해 오늘 회고를 다시 만듭니다.",
                    cancel_action="/review/cancel" if ai_job else "",
                    started=st.get("started") or 0.0),
                True)
    body = f"<div class='flash'>{esc(msg or '대기 중')}</div>" if msg else ""
    # 완료 후 복귀는 실행한 그 날짜의 데일리로(과거 백필 포함)
    if job_date:
        links = [f"<a href='/records?tab=daily&date={esc(job_date)}'>"
                 f"→ {esc(job_date)} 일간 회고 보기</a>"]
    else:
        links = ["<a href='/daily'>→ 오늘 일간 회고 보기</a>"]
    if store is not None:
        pend = store.decision_counts().get("candidate", 0)
        if pend:
            links.insert(0, f"<a href='/records?tab=decisions'>"
                            f"반영 대기 {pend}건 → 기억 › 장기기억</a>")
    return (f"<h1>AI 회고</h1>{body}"
            "<p>" + " · ".join(links) + " · <a href='/'>홈</a></p>", False)


# ─────────────────────────────────────────────────── 렌즈 렌더





# 목록 페이지네이션(#5) — 초기엔 화면 한 판 분량만, 스크롤 시 추가 로딩
_PAGE = 30          # 한 번에 렌더하는 행 수
_RAW_BATCH = 400    # 노이즈 필터 전 원시 조회 상한 (메일함)


def _fmt_when(iso: str) -> str:
    """목록 날짜 — 오늘은 시:분, 올해는 M/D, 그 외 YYYY/M/D."""
    if not iso:
        return ""
    today = date.today()
    if iso[:10] == today.isoformat():
        return iso[11:16]
    y, m, dd = iso[:4], iso[5:7], iso[8:10]
    if y == str(today.year):
        return f"{int(m)}/{int(dd)}"
    return f"{y}/{int(m)}/{int(dd)}"


def _date_group(iso: str, today) -> str:
    """목록 날짜 그룹 키 — ASCII(더 보기 URL 로 다음 배치에 넘김). 최신순에서 단조:
    t(오늘) → y(어제) → w(이번 주·월요일 시작) → lw(지난 주) → m(이번 달) → YYYY-MM.
    '어제' 판정이 주(週) 판정보다 앞이라 월요일의 어제(지난주 일요일)도 역행 없음."""
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    if d >= today:                      # 미래 타임스탬프(시계 어긋남)도 오늘로 흡수
        return "t"
    if (today - d).days == 1:
        return "y"
    monday = today - timedelta(days=today.weekday())
    if d >= monday:
        return "w"
    if d >= monday - timedelta(days=7):
        return "lw"
    if (d.year, d.month) == (today.year, today.month):
        return "m"
    return f"{d.year:04d}-{d.month:02d}"


_GROUP_LABELS = {"t": "오늘", "y": "어제", "w": "이번 주",
                 "lw": "지난 주", "m": "이번 달"}


def _group_label(key: str) -> str:
    """그룹 키 → 표시 라벨. 월 키(YYYY-MM)는 'YYYY년 M월'."""
    if key in _GROUP_LABELS:
        return _GROUP_LABELS[key]
    y, m = key.split("-")
    return f"{int(y)}년 {int(m)}월"


def _more_html(path: str, offset: int, group: str = "") -> str:
    """추가 로딩 센티널 — app.js 가 IntersectionObserver 로 감지해 이어 붙인다.
    JS 꺼짐 폴백은 '더 보기' 링크(전체 페이지 이동). path 에 쿼리가 있으면 '&' 로 연결.
    group = 이 배치 마지막 행의 날짜 그룹 키 — 다음 배치가 이어받아 같은 그룹이
    경계에서 헤더를 중복 방출하지 않는다(offset 뒤에 부가 — 접두 단정 테스트 보존)."""
    sep = "&" if "?" in path else "?"
    g = f"&g={group}" if group else ""
    return (f"<div class='more' data-more='{path}{sep}offset={offset}{g}'>"
            f"<a href='{path}{sep}offset={offset}{g}'>더 보기</a></div>")


# 메일함·스레드 왼쪽 목록 공통 필터 (양쪽 통일). 순서 = 탭 순서.
# (추적제외 탭은 2026-07-12 폐지 — 숨김이 흡수, 새 수신 시 자동 해제.
#  회신 필요/기한 탭은 2026-07-30 제거 — 정규식 판정의 정밀도가 낮아 상시
#  노출이 도구 신뢰를 깎았다. 판정 엔진(actions)은 주간 보고 재료로만 남고,
#  '이 메일이 뭘 요구하나'는 메일 분석(온디맨드 AI)이 답한다.)
_LIST_FILTERS = [("", "전체"), ("unread", "미개봉"),
                 ("flagged", "🚩 플래그"), ("hidden", "🙈 숨김")]


def _list_flt(qs) -> str:
    """쿼리스트링에서 활성 필터 하나 — 없으면 '' (전체).
    구 북마크의 awaiting/deadline 키는 전체로 조용히 강등된다."""
    for key in ("unread", "flagged", "hidden"):
        if (qs.get(key) or [""])[0] == "1":
            return key
    return ""


def _list_filter_bar(base: str, active: str, counts: dict) -> str:
    """메일함·스레드 공통 필터 바: 탭(왼쪽) + 'j/k 이동'(오른쪽 끝)."""
    parts = []
    for key, label in _LIST_FILTERS:
        n = counts.get(key)
        lbl = f"{label} {n}" if n is not None else label
        if key == active:
            parts.append(f"<b>{esc(lbl)}</b>")
        else:
            href = base + (f"?{key}=1" if key else "")
            parts.append(f"<a href='{href}'>{esc(lbl)}</a>")
    # 오른쪽 끝 (i) — 키보드 단축키 안내(순수 CSS 호버/포커스 팝오버, CSP 준수).
    help = ("<span class='kbdhelp' tabindex='0' aria-label='키보드 단축키'>ⓘ"
            "<span class='kbdpop'>"
            "<b>키보드</b><br>j / k 목록 이동<br>Space 본문 넘기기(끝이면 다음 메일)<br>"
            "n / p 스레드 안 메일 이동<br>"
            "f 플래그<br>h 숨김<br>/ 검색<br>Esc 검색창 빠져나오기</span></span>")
    return ("<div class='listtabs'><span class='ltabs'>"
            + " · ".join(parts)
            + "</span>" + help + "</div>")


# 노이즈는 설정 의존이라 DB에 고정하지 않는다. 설정이 같으면 새 rowid 이후만 분류하고,
# 프로세스 시작·설정 변경 때만 좁은 메타데이터를 한 번 전수 스캔한다.
_noise_cache = {
    "base": None, "max_id": 0, "received": set(), "real": set(),
    "thread_ids": frozenset(), "msg_ids": frozenset(),
}
_noise_cache_lock = threading.Lock()


def _noise_config_version(cfg) -> tuple:
    """is_noise / is_noise_subject_strong 이 의존하는 설정 표면 전체(지문 재료).
    새 노이즈 입력을 추가하면 여기에도 넣어야 캐시가 stale 되지 않는다."""
    return (tuple(cfg.ignore_senders), tuple(cfg.blocked_senders),
            tuple(cfg.internal_domains), tuple(cfg.subject_noise_strong),
            tuple(cfg.opt("filters", "external_allowlist", default=None) or []))


def _noise_sets(store, cfg) -> tuple:
    """(noise_thread_ids, noise_msg_ids) — append-only high-water 증분 캐시."""
    max_id = store.db.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
    base = (str(store.db_path), _noise_config_version(cfg))
    with _noise_cache_lock:
        rebuild = (_noise_cache["base"] != base
                   or max_id < _noise_cache["max_id"])
        if not rebuild and max_id == _noise_cache["max_id"]:
            return _noise_cache["thread_ids"], _noise_cache["msg_ids"]

        if rebuild:
            recv, real, nmsg = set(), set(), set()
            where, params = "", ()
        else:
            recv = set(_noise_cache["received"])
            real = set(_noise_cache["real"])
            nmsg = set(_noise_cache["msg_ids"])
            where, params = " AND id>?", (_noise_cache["max_id"],)

        for r in store.db.execute(
                "SELECT id, thread_id, sender_addr, subject FROM messages "
                "WHERE is_sent=0" + where, params):
            recv.add(r["thread_id"])
            if (cfg.is_noise(r["sender_addr"])
                    or cfg.is_noise_subject_strong(r["subject"])):
                nmsg.add(r["id"])
            else:
                real.add(r["thread_id"])
        tset = frozenset(recv - real)
        mset = frozenset(nmsg)
        _noise_cache.update(
            base=base, max_id=max_id, received=recv, real=real,
            thread_ids=tset, msg_ids=mset,
        )
        return tset, mset


def _noise_thread_ids(store, cfg) -> frozenset:
    """'노이즈 스레드' = 비노이즈 수신 메일이 하나도 없는 스레드(수신 전부 노이즈).

    스레드 목록의 일반 탭에서 제외 — 숨김 탭에선 유지(복구). 발신만 있는 스레드
    (수신 0건)는 노이즈로 보지 않는다(내가 시작한 대화). 캐시는 `_noise_sets` 참고.
    """
    return _noise_sets(store, cfg)[0]


def render_mail(store, cfg, offset: int = 0, flt: str = "", g: str = "") -> str:
    """메일함 — 노이즈 제외 수신함, 최신순. 스레드 목록과 같은 필터 바(양쪽 통일).

    flt: '' 전체 | unread 미개봉 | flagged 플래그 | hidden 숨김.
    숨김은 전체/그 외 필터에서 빠지고 'hidden' 탭에서만. offset>0 이면 조각만 반환. g = 직전 배치 마지막 날짜 그룹 키
    (더 보기 URL 로 전달 — 경계 헤더 중복 방지, 비교에만 쓰고 출력 안 함).
    """
    _, noise_msg = _noise_sets(store, cfg)   # 메시지 단위 노이즈(캐시) — is_noise 재계산 제거
    if flt == "hidden":
        tcond = "t.hidden=1"
    else:
        tcond = "(t.hidden IS NULL OR t.hidden=0)"
        if flt == "unread":
            tcond += " AND (m.read_at IS NULL OR m.read_at='')"
        elif flt == "flagged":
            tcond += " AND t.flagged=1"
    base = "/mail" + (f"?{flt}=1" if flt else "")
    # people 조인 하나로 관계 배지 재료까지 같이 — 행당 추가 조회 없음(N+1 방지).
    raw = store.db.execute(
        "SELECT m.id, m.thread_id, m.subject, m.sender_name, m.sender_addr, "
        "m.sent_on, m.read_at, t.flagged, "
        "p.from_count AS p_from, p.to_count AS p_to, p.first_seen AS p_first "
        "FROM messages m JOIN threads t ON t.id=m.thread_id "
        "LEFT JOIN people p ON p.addr = m.sender_addr "
        f"WHERE m.is_sent=0 AND {tcond} "
        "ORDER BY m.sent_on DESC, m.id DESC LIMIT ? OFFSET ?",
        (_RAW_BATCH, offset)).fetchall()
    items: list[str] = []
    consumed = 0
    nrows = 0                # 실제 행 수 — 그룹 헤더는 페이지 크기에 안 세게
    last_g = g               # 직전 배치에서 이어받은 그룹 키
    today_d = date.today()
    for r in raw:
        consumed += 1
        # 숨김 탭은 복구용 — 노이즈여도 보여준다(전체/미개봉/플래그 탭만 노이즈 제외).
        if flt != "hidden" and r["id"] in noise_msg:
            continue
        gk = _date_group(r["sent_on"], today_d)
        if gk and gk != last_g:          # emit 직전 비교 — 스킵 행은 키 갱신 없음
            items.append(f"<div class='dghead'>{_group_label(gk)}</div>")
            last_g = gk
        badge = "🚩 " if r["flagged"] else ""
        cls = "mrow read" if r["read_at"] else "mrow"   # 읽음=제목 볼드 해제
        items.append(
            f"<a class='{cls}' href='/thread/{r['thread_id']}?focus={r['id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(badge)}{esc(r['subject'])}</span>"
            f"<span class='mdate'>{esc(_fmt_when(r['sent_on']))}</span></span>"
            f"<span class='msubj'>{esc(r['sender_name'] or r['sender_addr'])}"
            f"{_relation_badge(r)}</span></a>")
        nrows += 1
        if nrows >= _PAGE:
            break
    has_more = ((nrows >= _PAGE and consumed < len(raw))
                or len(raw) == _RAW_BATCH)
    more = _more_html(base, offset + consumed, last_g) if has_more else ""
    if offset:
        return "".join(items) + more
    # 탭 카운트는 SQLite 한 번의 집계로 계산한다. id 문자열은 내부 정수만 사용한다.
    def _in_expr(ids, col, *, negate=False):
        if not ids:
            return "1" if negate else "0"
        op = "NOT IN" if negate else "IN"
        return f"{col} {op} ({','.join(str(int(i)) for i in ids)})"

    real = _in_expr(noise_msg, "m.id", negate=True)
    visible = "(t.hidden IS NULL OR t.hidden=0)"
    agg = store.db.execute(
        f"""SELECT
          COALESCE(SUM(CASE WHEN {visible} AND {real} THEN 1 ELSE 0 END),0) total,
          COALESCE(SUM(CASE WHEN {visible} AND {real}
            AND (m.read_at IS NULL OR m.read_at='') THEN 1 ELSE 0 END),0) unread,
          COALESCE(SUM(CASE WHEN {visible} AND {real} AND t.flagged=1
            THEN 1 ELSE 0 END),0) flagged,
          COALESCE(SUM(CASE WHEN t.hidden=1 THEN 1 ELSE 0 END),0) hidden
        FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.is_sent=0"""
    ).fetchone()
    counts = {"": agg["total"], "unread": agg["unread"],
              "flagged": agg["flagged"], "hidden": agg["hidden"]}
    body = "".join(items) or "<p class='empty'>수신 메일 없음</p>"
    return ("<h1>메일함</h1>"
            + _list_filter_bar("/mail", flt, counts)
            + f"<div class='mlist'>{body}{more}</div>")


# 관계 배지 — 발신자와 나의 왕래 기록에서 나오는 **사실**만 쓴다(2026-08-06).
# 왜: '읽을지 말지'의 1차 판단이 화면에 없었다. 노이즈 규칙은 발신 주소·제목
# 문자열이라 설치마다 손으로 채워야 하는데, `people` 의 왕래 수는 사용자 행동
# 기록이라 **설정 없이 자동으로 개인화된다**(실측: 오늘 받은 16통 중 5통이
# 답한 적 없는 상대이고 그게 정확히 자동발송이었다).
# 규칙 둘 — ① 할 말이 없으면 아무것도 쓰지 않는다(모든 줄에 붙으면 소음이다).
# ② **표시만 한다.** 정렬·필터·자동 숨김에 쓰지 않는다 — 쓰는 순간 "내가 처음
# 답하는 사람"이 화면에서 사라진다.
_ONEWAY_MIN = 3          # 이만큼 받고도 한 번도 안 보냈을 때만 '답한 적 없음'


def _relation_badge(row) -> str:
    """발신자 관계 배지 — 없으면 ''.

    `첫 메일`은 people.first_seen 과 그 메일의 시각을 맞춰 판정한다. from_count 는
    **지금까지의 누계**라 과거 행에 그대로 쓰면 거짓이 된다(옛 메일이 전부
    '첫 메일'이 아니게 되는 이유)."""
    keys = row.keys()
    if "p_from" not in keys:
        return ""
    frm = row["p_from"] or 0
    to = row["p_to"] or 0
    out = []
    if row["p_first"] and row["p_first"] == row["sent_on"]:
        out.append("<span class='rbadge first' title='이 사람에게 받은 첫 메일'>첫 메일</span>")
    if to == 0 and frm >= _ONEWAY_MIN:
        out.append(f"<span class='rbadge oneway' title='{frm}통 받는 동안 한 번도 "
                   "보낸 적이 없다'>↩ 0</span>")
    return "".join(out)


def _thread_span_days(first: str, last: str) -> int:
    """스레드 논의 기간(첫 메일~마지막 메일, 달력일)."""
    try:
        return (date.fromisoformat((last or "")[:10])
                - date.fromisoformat((first or "")[:10])).days
    except ValueError:
        return 0


def render_threads(store, cfg, offset: int = 0, flt: str = "", g: str = "") -> str:
    """스레드 — 메일함과 같은 목록 UI: 제목 [N통] · 마지막 발신인 · 날짜.

    flt: '' 전체 | unread 미개봉 | flagged 플래그 | hidden 숨김.
    메일함과 같은 필터 바(양쪽 통일). 숨김은 전체/그 외 필터에서 빠지고
    숨김 탭에서만.
    """
    # 노이즈 스레드는 일반 탭에서 제외(메일함과 동일 스코프), 숨김 탭에선 유지(복구).
    noise_ids = _noise_thread_ids(store, cfg)
    ncsv = ",".join(str(int(i)) for i in noise_ids)
    nx = f" AND t.id NOT IN ({ncsv})" if noise_ids else ""    # alias t. (행 쿼리·unread)
    nxb = f" AND id NOT IN ({ncsv})" if noise_ids else ""     # bare id (agg: FROM threads)
    if flt == "hidden":
        cond = "WHERE t.hidden=1"
    elif flt == "flagged":
        cond = "WHERE t.flagged=1 AND (t.hidden IS NULL OR t.hidden=0)" + nx
    elif flt == "unread":
        cond = ("WHERE (t.hidden IS NULL OR t.hidden=0) "
                "AND s.unread_received_count>0" + nx)
    else:
        cond = "WHERE (t.hidden IS NULL OR t.hidden=0)" + nx
    rows = store.db.execute(
        f"""SELECT t.id, t.flagged, t.hidden, t.first_date, t.last_date,
                  first.subject, s.message_count AS n,
                  last.sender_name AS last_name, last.sender_addr AS last_addr,
                  last.sent_on AS last_on, last.sent_on AS sent_on,
                  s.unread_received_count AS unread_n,
                  p.from_count AS p_from, p.to_count AS p_to, p.first_seen AS p_first
           FROM threads t
           JOIN thread_state s ON s.thread_id=t.id
           JOIN messages first ON first.id=s.first_message_id
           JOIN messages last ON last.id=s.latest_message_id
           LEFT JOIN people p ON p.addr = last.sender_addr
           {cond} ORDER BY t.last_date DESC LIMIT ? OFFSET ?""",
        (_PAGE + 1, offset)).fetchall()
    has_more = len(rows) > _PAGE
    rows = rows[:_PAGE]
    items: list[str] = []
    last_g = g               # 직전 배치에서 이어받은 그룹 키
    today_d = date.today()
    for r in rows:
        gk = _date_group(r["last_on"] or "", today_d)
        if gk and gk != last_g:
            items.append(f"<div class='dghead'>{_group_label(gk)}</div>")
            last_g = gk
        marks = ("🚩" if r["flagged"] else "") + ("🙈" if r["hidden"] else "")
        if r["last_addr"] and cfg.is_blocked(r["last_addr"]):
            marks += "⛔"
        badge = f"{marks} " if marks else ""
        hot = (r["n"] >= 3
               or _thread_span_days(r["first_date"], r["last_date"]) >= 5)
        cnt_cls = "mcnt hot" if hot else "mcnt"
        # 메일함과 동일 규칙: 안 읽은 수신 메일이 있으면 볼드(안 읽음), 다 읽었으면 해제
        rcls = "mrow" if r["unread_n"] else "mrow read"
        items.append(
            f"<a class='{rcls}' href='/thread/{r['id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(badge)}{esc(r['subject'])}"
            f" <span class='{cnt_cls}'>[{r['n']}통]</span></span>"
            f"<span class='mdate'>{esc(_fmt_when(r['last_on'] or ''))}</span></span>"
            f"<span class='msubj'>마지막: {esc(r['last_name'] or r['last_addr'] or '')}"
            f"{_relation_badge(r)}</span></a>")
    base = f"/threads?{flt}=1" if flt else "/threads"
    more = _more_html(base, offset + _PAGE, last_g) if has_more else ""
    if offset:
        return "".join(items) + more
    # total/미개봉/플래그는 노이즈 제외, 숨김(hid)은 노이즈 포함(복구용).
    agg = store.db.execute(
        "SELECT COALESCE(SUM(CASE WHEN (hidden IS NULL OR hidden=0)" + nxb + " THEN 1 ELSE 0 END),0) total, "
        "COALESCE(SUM(CASE WHEN flagged=1 AND (hidden IS NULL OR hidden=0)" + nxb + " THEN 1 ELSE 0 END),0) flag, "
        "COALESCE(SUM(CASE WHEN hidden=1 THEN 1 ELSE 0 END),0) hid FROM threads").fetchone()
    n_unread = store.db.execute(
        "SELECT COUNT(*) c FROM threads t JOIN thread_state s ON s.thread_id=t.id "
        "WHERE s.unread_received_count>0 AND (t.hidden IS NULL OR t.hidden=0)" + nx
    ).fetchone()["c"]
    # 응답대기·기한 뱃지는 리스트와 동일 집합이어야 한다: await/dead ∩ 비노이즈 ∩ 비숨김.
    counts = {"": agg["total"], "unread": n_unread,
              "flagged": agg["flag"], "hidden": agg["hid"]}
    body = "".join(items) or "<p class='empty'>스레드 없음</p>"
    return ("<h1>스레드</h1>"
            + _list_filter_bar("/threads", flt, counts)
            + f"<div class='mlist'>{body}{more}</div>")


def _actions_bar(tid: int, t, has_attach: bool, decider: str = "") -> str:
    flagged = bool(t["flagged"]) if t else False
    hidden = bool(t["hidden"]) if t else False
    forms: list[str] = []

    def _btn(action, label, cls=""):
        forms.append(f"<form method='post' action='/thread/{tid}/{action}'>"
                     f"<button class='{cls}'>{esc(label)}</button></form>")

    # 플래그: 아이콘으로 유/무 (⚐ 없음 / ⚑ 색 있음)
    if flagged:
        forms.append(f"<form method='post' action='/thread/{tid}/unflag'>"
                     "<button class='iconbtn flag on' title='플래그 해제' "
                     "aria-label='플래그 해제' aria-pressed='true'>⚑</button></form>")
    else:
        forms.append(f"<form method='post' action='/thread/{tid}/flag'>"
                     "<button class='iconbtn flag' title='플래그' "
                     "aria-label='플래그' aria-pressed='false'>⚐</button></form>")
    # 숨기기: 목록·추적에서 제외, 새 수신 메일이 오면 자동 해제 (숨김 탭에서 복구).
    # 구 '추적 제외'는 2026-07-12 폐지 — 숨기기가 흡수.
    if hidden:
        _btn("unhide", "🙈 숨김 해제")
    else:
        _btn("hide", "숨기기")
    # 노트/열기/첨부 (발신자 차단은 주소별 보기 페이지로 이동 — 이름 클릭)
    _btn("note", "노트 생성")
    _btn("open", "Outlook 열기")
    if has_attach:
        _btn("attach", "첨부 추출")
    # 장기기억 수동 기록 — 사람이 쓰므로 즉시 반영 (기억 › 장기기억에 축적).
    # summary 라벨은 펼치면 '✕ 닫기'로 바뀜(CSS) — 라벨 중복 없이 접기 유지.
    forms.append(
        "<details class='recdec'><summary><span class='lbl'>장기기억</span>"
        "<span class='xcl'>✕ 닫기</span></summary>"
        f"<form method='post' action='/thread/{tid}/record-decision'>"
        "<input type='text' name='title' placeholder='기억할 내용 (필수)'>"
        "<input type='text' name='rationale' placeholder='근거 (선택)'>"
        f"<input type='text' name='decider' value='{esc(decider)}' "
        "placeholder='결정자'>"
        "<button class='btn-primary'>장기기억에 반영</button></form></details>")
    return f"<div class='actions'>{''.join(forms)}</div>"


_MAIL_SCOPE_RX = re.compile(r"@(\d+)~mail:(\d+)$")


def _mail_analyses(store, mids: set[int]) -> dict[int, dict]:
    """메일 id → 저장된 분석 {id, created, basis}. 같은 메일은 최신 것만.

    키 형식 v3:질문@<basis>~mail:<mid> 에서 접미로 찾는다 — 분석 이력과 별도
    테이블을 만들지 않고 ask_cache 를 그대로 쓰기 위한 조회다. basis(당시
    MAX rowid)는 '이후 새 메일 N통' 낡음 표시의 기준이 된다."""
    if not mids:
        return {}
    out: dict[int, dict] = {}
    rows = store.db.execute(
        "SELECT rowid AS id, key, created FROM ask_cache "
        "WHERE key LIKE '%~mail:%' ORDER BY created, rowid")
    for r in rows:
        m = _MAIL_SCOPE_RX.search(r["key"])
        if not m:
            continue
        mid = int(m.group(2))
        if mid in mids:                      # created 오름차순 → 마지막이 최신
            out[mid] = {"id": r["id"], "created": r["created"] or "",
                        "basis": int(m.group(1))}
    return out


def _mail_ai_controls(store, mid: int, hit: dict | None,
                      thread_id: int | None = None) -> str:
    """메일 머리글의 분석 진입 — 없으면 '분석' 버튼, 있으면 보기 링크+다시.

    분석 결과는 ask_cache 영구 저장이라 여기서 다시 여는 것은 공짜다. 낡음은
    인물 요약과 같은 문법: 경과일 + 그 뒤 도착한 새 메일 수(전역 basis 기준)."""
    if not hit:
        return ("<form class='mh-ai' method='post' action='/ask/jobs'>"
                f"<input type='hidden' name='mid' value='{int(mid)}'>"
                "<button class='aibtn ghost compact' "
                "title='이 메일의 의미·맥락·필요한 액션을 AI 로 정리'>"
                "분석</button></form>")
    ago = _days_ago((hit["created"] or "")[:10], date.today().isoformat())
    label = f"분석 보기 · {ago}" if ago else "분석 보기"
    # '이후 새 메일'은 **이 스레드** 기준 — 전역으로 세면 무관한 메일 유입에도
    # 숫자가 늘어 낡음 표시가 거짓말이 된다. basis(분석 당시 전역 MAX rowid)보다
    # 큰 id 가 이 스레드에 있으면 그것이 곧 분석 후 도착한 메일이다.
    fresh = store.db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND rowid > ?",
        (thread_id or -1, hit["basis"])).fetchone()[0]
    stale = f" <span class='dim'>이후 새 메일 {fresh}통</span>" if fresh else ""
    # div — span(phrasing) 안의 form 은 HTML 사양 위반(구 _signal_chips 주석의
    # 규칙 그대로). CSS .mh-ai 가 inline-flex 라 배치는 동일하다.
    return ("<div class='mh-ai'>"
            f"<a class='aibtn ghost compact' href='/ask?id={int(hit['id'])}'>"
            f"{esc(label)}</a>{stale}"
            "<form method='post' action='/ask/jobs'>"
            f"<input type='hidden' name='mid' value='{int(mid)}'>"
            "<input type='hidden' name='fresh' value='1'>"
            "<button class='aibtn ghost compact' title='새 맥락으로 다시 분석'>"
            "다시</button></form></div>")


def _preserved_turns_html(turns: list[dict]) -> str:
    """보존 인용을 대화 턴으로 그린다 — 각 턴은 **메시지가 아니다.**

    내가 수신자가 아니었던 대화라 DB 에 행이 없다: 링크도 앵커도 검색 결과도
    없다. 그래서 링크를 주지 않고 '인용에서'라고 출처를 밝힌다 — 눌러도 갈 곳이
    없는 것을 눌러 보게 만들지 않는다. 목록의 [N통]도 그대로 둔다(메시지 수는
    안 늘었다)."""
    out = []
    for t in turns:
        who = esc(t["who"] or t["addr"] or "발신자 미상")
        when = esc(t["when"] or "")
        out.append("<div class='qturn'><div class='qturn-h'>"
                   f"<span class='qw'>{who}</span>"
                   + (f"<span class='qd'>{when}</span>" if when else "")
                   + "<span class='qsrc'>인용에서</span></div>"
                   "<div class='md-rich md-show'>"
                   + _mail_md_to_html(t["body"]) + "</div></div>")
    return "".join(out)


def render_thread(store, cfg, tid: int) -> str:
    d = format_detail(store, cfg, tid)
    t = store.thread(tid)
    # sticky 헤더(제목): 센티널이 화면을 벗어나면 app.js(hookThreadHead)가
    # .stuck 을 붙여 컴팩트(1줄 말줄임)로. 액션 바는 sticky 밖 — f/h 키가 대체.
    # (신호 칩 ↩/⏰/☑ 은 2026-07-30 제거 — 판정 정밀도가 낮아 신뢰를 깎았다.)
    out = ["<div class='sticksentinel'></div>",
           f"<div class='threadhead'><h1>{esc(d['title'])}</h1></div>"]
    if t:
        has_attach = any(blk["attach"] for blk in d["timeline"])
        # 결정자 기본값 = 최신 수신 메일 발신인 (타임라인은 최신 먼저)
        decider = next((blk["sender"] for blk in d["timeline"]
                        if not blk["is_sent"]), "")
        out.append(_actions_bar(tid, t, has_attach, decider=decider))
    out.append("<div class='analysis'>")
    for a in d["analysis"]:
        if not a:
            continue
        # "[롤링" 은 구버전 저장 노트 호환용 (표시 문구는 "누적 요약"으로 개명)
        cls = " class='sig'" if a.startswith(("[누적", "[롤링")) else ""
        out.append(f"<div{cls}>{esc(a)}</div>")
    out.append("</div>")
    # text-only 메일 중 마크다운으로 보이는 게 하나라도 있으면 스레드당 토글 버튼 1개.
    # 프룬 마커(이미지 보존 기간 경과)는 HTML 로 취급하지 않음 — 텍스트와 함께 표시
    def _is_strip_marker(h):
        return (h or "").startswith("<div class='imgstrip'>")

    raws = ["" if (blk["html"] and not _is_strip_marker(blk["html"]))
            else "\n".join(blk["body"]) for blk in d["timeline"]]
    # 보존 인용(mid-join) 분할 — 텍스트 표시 경로(프룬 후 포함)에서도 HTML 층과
    # 같은 접힘 경험을 재현한다 (저장 증가 없음 — 렌더 시 마커를 폴드로 변환)
    parts = [_split_preserved(r) for r in raws]
    # HTML 없는 본문(프룬·텍스트)이 마크다운으로 보이면 스레드당 토글 1개 —
    # 기본은 서식, 버튼은 저장 텍스트(변환 산출물)를 보여준다 (검증용)
    any_md = any(h and _looks_like_markdown(h) for h, _t in parts)
    out.append("<div class='mthread'>")
    if any_md:
        out.append("<button type='button' class='md-toggle'>텍스트 보기</button>")
    # 메일별 저장 분석(ask_cache) — 있으면 머리글에 '분석 보기', 없으면 '분석'
    analyses = _mail_analyses(store, {blk["id"] for blk in d["timeline"]})
    for blk, (raw, qtail) in zip(d["timeline"], parts):
        sent = " sent" if blk["is_sent"] else ""
        arrow = "→" if blk["is_sent"] else ""
        att = f" 📎{esc(blk['attach'])}" if blk["attach"] else ""
        # 참여자(발신자) 이름 클릭 → 그 사람 도시에(왼쪽). 내 발신은 링크 없음.
        if not blk["is_sent"] and blk.get("sender_addr"):
            who = (f"<a href='/people?addr={_q(blk['sender_addr'])}' "
                   f"title='이 사람 도시에'>{esc(blk['sender'])}</a>")
        else:
            who = esc(blk["sender"])
        out.append(f"<div class='msg' id='msg-{blk['id']}'>")
        out.append(
            f"<div class='mhead{sent}'>"
            f"<span class='mh-who'>{arrow} {who}{att}</span>"
            f"<span class='mh-when'>{esc(blk['sent_on'])}</span>"
            f"{_mail_ai_controls(store, blk['id'], analyses.get(blk['id']), tid)}"
            "</div>")
        out.append("<div class='mbody'>")
        # 보존 인용(mid-join)을 대화 턴으로 읽어 둔다 — 접힘 머리줄과 펼침 내용이
        # 여기서 나온다. blk["body"] 는 HTML 메일에도 있는 new_content 라 두 층이
        # 같은 재료를 쓴다. 못 읽으면 turns=[] → 종전 화면 그대로(폴백).
        turns = parse_preserved("\n".join(blk["body"]))
        qlabel = preserved_label(turns)
        is_marker = bool(blk["html"]) and _is_strip_marker(blk["html"])
        if is_marker:
            out.append(blk["html"])          # 프룬 배너 (마커는 프룬이 만든 고정 형식)
        if blk["html"] and not is_marker:
            if "data-blocked-src" in blk["html"]:
                out.append("<div class='imgnote'>🚫 일부 이미지를 표시할 수 없습니다"
                           "(원격 차단 또는 추출 실패) — 원문은 Outlook에서</div>")
            # 꼬리 이미지 서명(임베드 PNG·height≤210·본문 뒤)은 "Signature 숨김"
            # 한 줄로 대체 — 공간만 먹는 로고·명함 카드 제거(clean.hide_image_signatures).
            mail_html = hide_image_signatures(blk["html"])
            # 메일 원본 HTML — 흰 배경 전제의 인라인 색을 담고 있어 다크에서
            # 검은 글씨·흰 블록·파란 링크로 깨진다. .mailhtml 로 감싸 다크 모드
            # CSS 가 그 색만 테마 색으로 평탄화한다(우리 콘텐츠엔 영향 없음).
            # 단 작성자가 준 강조색까지 죽으므로, 다크용 대체색(--dk)을 함께
            # 실어 보내 CSS 가 고르게 한다 — 테마 토글이 클라이언트에서 즉시
            # 일어나므로 서버가 한쪽만 구워 보내면 토글 순간 어긋난다.
            # 접힘 머리줄은 저장 HTML 안에 구워져 있다 — 렌더 시점에 갈아 끼운다
            # (재수집 강요 금지). 못 읽었으면 qlabel='' 이라 원문 그대로 지난다.
            out.append("<div class='mailhtml'>"
                       + retitle_qfold(add_dark_colors(mail_html), qlabel)
                       + "</div>")
        elif raw and _looks_like_markdown(raw):
            # HTML 없는 본문(프룬 마커·행 삭제·텍스트 메일 공통) — 서식 기본.
            # 저장 텍스트는 변환 산출물이라 raw 가 원문이 아니다; 텍스트는
            # 토글(md-raw)로 실어 문법 리터럴 검증용으로만 쓴다.
            out.append("<div class='md-rich'>" + _mail_md_to_html(raw) + "</div>")
            out.append("<pre class='md-raw' style='white-space:pre-wrap'>"
                       + esc(raw) + "</pre>")
        elif raw.strip():
            out.append("<pre style='white-space:pre-wrap'>" + esc(raw) + "</pre>")
        elif not qtail:
            # 이미지-전용 메일 등 텍스트가 비면(빈 본문 가드) 안내만
            out.append("<p class='dim'>본문 없음 — Outlook에서 확인</p>")
        if qtail:
            # 보존 인용을 HTML 층과 같은 접힘으로 — 서식 렌더(체인은 md 산출물).
            # 턴을 읽었으면 대화로, 못 읽었으면 종전처럼 통째로.
            inner = (_preserved_turns_html(turns) if turns else
                     "<div class='md-rich md-show'>" + _mail_md_to_html(qtail) + "</div>")
            out.append(qfold_open(qlabel) + inner + QFOLD_CLOSE)
        out.append("</div></div>")
    out.append("</div>")   # .mthread
    return "\n".join(out)


def render_settings(store, cfg) -> str:
    """설정 페이지 — 차단 발신인·판정 기준·노이즈 규칙을 런타임 편집.

    바뀐 값은 overrides.json 에 저장돼 config.toml 위에 병합된다(영구·재시작 유지)."""
    out = ["<div class='settings'>", "<h1>설정</h1>"]

    # ── 화면 테마 (라이트/다크) — 세그먼트 토글 ──
    cur_theme = cfg.opt("web", "theme", default="light")
    _SUN = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            "stroke-width='2' stroke-linecap='round' aria-hidden='true'>"
            "<circle cx='12' cy='12' r='4.2'/><path d='M12 2.5v2.4M12 19.1v2.4"
            "M4.2 4.2l1.7 1.7M18.1 18.1l1.7 1.7M2.5 12h2.4M19.1 12h2.4"
            "M4.2 19.8l1.7-1.7M18.1 5.9l1.7-1.7'/></svg>")
    _MOON = ("<svg viewBox='0 0 24 24' fill='currentColor' aria-hidden='true'>"
             "<path d='M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a7 7 0 1 0 11 11z'/></svg>")
    def _tbtn(val, label, icon):
        on = " active" if cur_theme == val else ""
        pressed = "true" if cur_theme == val else "false"
        return (f"<button type='button' class='themebtn{on}' "
                f"aria-pressed='{pressed}' data-set-theme='{esc(val)}'>"
                f"{icon}{esc(label)}</button>")
    out.append("<h2>화면 테마</h2>")
    out.append("<p class='dim'>즉시 적용되고 설정에 저장됩니다.</p>")
    out.append("<div class='themepick' role='group' aria-label='화면 테마'>"
               + _tbtn("light", "라이트", _SUN)
               + _tbtn("dark", "다크", _MOON) + "</div>")

    # ── 화면 스킨 (모양·밀도) — 밝기와 별개 축이라 토글을 따로 둔다 ──
    cur_skin = _skin_ok(cfg.opt("web", "skin", default="classic"))
    _ROWS = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' "
             "stroke-width='2' stroke-linecap='round' aria-hidden='true'>"
             "<path d='M4 7h16M4 12h16M4 17h10'/></svg>")
    _GRID = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' "
             "stroke-width='2' aria-hidden='true'>"
             "<rect x='3.5' y='3.5' width='8' height='8' rx='2'/>"
             "<rect x='14' y='3.5' width='6.5' height='4.5' rx='1.6'/>"
             "<rect x='14' y='10.5' width='6.5' height='10' rx='1.6'/>"
             "<rect x='3.5' y='14' width='8' height='6.5' rx='2'/></svg>")
    def _sbtn(val, label, icon):
        on = " active" if cur_skin == val else ""
        return (f"<button type='button' class='themebtn{on}' "
                f"aria-pressed='{'true' if cur_skin == val else 'false'}' "
                f"data-set-skin='{esc(val)}'>{icon}{esc(label)}</button>")
    out.append("<h2>화면 스킨</h2>")
    out.append("<p class='dim'>카드형은 홈과 일간·주간 보고를 카드 격자로 "
               "배치합니다. 메일 읽는 화면은 같습니다.</p>")
    out.append("<div class='themepick' role='group' aria-label='화면 스킨'>"
               + _sbtn("classic", "클래식", _ROWS)
               # 저장값은 'bento' 그대로 — 화면 표기만 바꾼다(값을 바꾸면 이미
               # 고른 설정이 조용히 풀린다). 장기기억의 '반영/유보'와 같은 방식.
               + _sbtn("bento", "카드형", _GRID) + "</div>")

    # ── 판정 기준 (런타임 편집 → overrides.json 영구 저장) ──
    from . import ask as ask_mod           # 지연 import (web ↔ ask 순환 방지)
    smd = cfg.opt("ai", "summary_max_days", default=1)
    def _num(name, val, note):
        return (f"<tr><th>{esc(note[0])}</th>"
                f"<td><input type='number' name='{name}' value='{esc(str(val))}' "
                f"min='{note[1]}' style='width:70px'></td>"
                f"<td class='dim'>{esc(note[2])}</td></tr>")
    backends = sorted(set(list(cfg.ai_backends) + list(cfgmod._BUILTIN_BACKENDS)))
    def _sel(name, cur):
        opts = "".join(
            f"<option{' selected' if b == cur else ''}>{esc(b)}</option>"
            for b in backends)
        return (f"<td><select name='{name}'>{opts}</select></td>")
    num_rows = (
        _num("broadcast_to", cfg.broadcast_to,
             ("대량발송 제외선", 1, "수신인이 이 수 이상이면 그룹 공지로 본다"))
        + _num("direct_to", cfg.direct_to,
               ("직접수신 상한", 0, "수신인이 이 수 이하면 '직접 온 메일'"))
        + _num("stall_workdays", cfg.stall_workdays,
               ("응답 정체(영업일)", 1, "내 발신 무응답이 이만큼 넘으면 정체"))
        + _num("stale_workdays", cfg.stale_workdays,
               ("스레드 정체(영업일)", 1, "열린 스레드 무활동이 이만큼 넘으면 정체"))
        + _num("summary_max_days", smd,
               ("요약 창(일)", 1, "매 실행 이 일수까지 소급 요약"))
        + _num("ask_max_input_tokens",
               cfg.opt("ai", "ask_max_input_tokens",
                       default=ask_mod.ASK_MAX_INPUT_TOKENS),
               ("분석 입력 상한(토큰)", 0,
                "한 콜에 싣는 최대 입력 — 백엔드 창 크기 (0=제한 없음)")))
    out.append("<h2>판정 기준</h2>")
    out.append("<form method='post' action='/settings/save'>"
               "<table class='settbl'>" + num_rows
               + "<tr><th>요약 백엔드</th>" + _sel("summary_backend", cfg.ai_summary_backend)
               + "<td class='dim'>요약 · 회고</td></tr>"
               + "<tr><th>AI 검색 백엔드</th>" + _sel("search_backend", cfg.ai_search_backend)
               + "<td class='dim'>흐릿한 기억으로 찾기</td></tr>"
               + "<tr><th>분석 백엔드</th>" + _sel("ask_backend", cfg.ai_ask_backend)
               + "<td class='dim'>질문 조사·답변 (한 질문 최대 12콜)</td></tr>"
               + "<tr><th>주간 백엔드</th>"
               + _sel("weekly_backend",
                      cfg.opt("ai", "weekly", default=None)
                      or cfg.ai_summary_backend)
               + f"<td class='dim'>주간 보고 (최대 {weekly.MAX_AI_CALLS}콜 · "
                 "미설정 시 요약 백엔드)</td></tr>"
               + "</table><button class='btn-primary'>판정 기준 저장</button></form>")

    # ── 표시 설정 ──
    rw = cfg.opt("web", "reading_width", default=1200)
    rf = cfg.opt("web", "reading_font", default=0)
    # 미설정이면 value 는 빈 값 — _save_settings 가 빈 필드를 건너뛰므로 다른
    # 항목 저장 시 원치 않는 reading_font 오버라이드가 기록되지 않는다.
    rf_val = esc(str(rf)) if rf else ""
    sync_min = cfg.opt("web", "sync_interval_min", default=30)
    img_days = cfg.opt("web", "image_retain_days", default=60)
    out.append("<h2>표시 · 동기화</h2>")
    out.append(
        "<form method='post' action='/settings/save'>"
        "<table class='settbl'>"
        "<tr><th>읽기 창 너비(px)</th>"
        f"<td><input type='number' name='reading_width' value='{esc(str(rw))}' "
        "min='600' step='20' style='width:80px'></td>"
        "<td class='dim'>읽기 창 본문 최대 폭 (기본 1200)</td></tr>"
        "<tr><th>본문 글자 크기(px)</th>"
        f"<td><input type='number' name='reading_font' value='{rf_val}' "
        "placeholder='16' min='12' step='1' style='width:80px'></td>"
        "<td class='dim'>메일 본문 크기 (기본 16)</td></tr>"
        "<tr><th>자동 동기화(분)</th>"
        f"<td><input type='number' name='sync_interval_min' value='{esc(str(sync_min))}' "
        "min='0' step='5' style='width:80px'></td>"
        "<td class='dim'>백그라운드 수집 주기 (기본 30 · 0=끔)</td></tr>"
        "<tr><th>이미지 보존(일)</th>"
        f"<td><input type='number' name='image_retain_days' value='{esc(str(img_days))}' "
        "min='0' step='1' style='width:80px'></td>"
        "<td class='dim'>이미지·서식 HTML 보존 (기본 60 · 0=임베드 끔) · "
        "경과분은 압축되고 <code>sync --full</code> 로만 복구</td></tr>"
        "</table><button class='btn-primary'>표시 설정 저장</button></form>")

    # ── 차단된 발신인 (편집 가능) ──
    out.append("<h2>차단된 발신인</h2>")
    out.append("<p class='dim'>이 패턴이 발신 주소에 있으면 목록·요약·보고에서 "
               "뺍니다. 수신 차단 자체는 Outlook 규칙으로.</p>")
    if cfg.blocked_senders:
        rows = "".join(
            "<div class='setrow'>"
            f"<span class='mono'>{esc(addr)}</span>"
            "<form method='post' action='/settings/unblock'>"
            f"<input type='hidden' name='addr' value='{esc(addr)}'>"
            "<button>차단 해제</button></form></div>"
            for addr in cfg.blocked_senders)
        out.append(f"<div class='setlist'>{rows}</div>")
    else:
        out.append("<p class='empty'>차단된 발신인 없음</p>")

    # ── 노이즈 규칙 (발신자·제목) ──
    out.append("<h2>노이즈 규칙</h2>")
    out.append("<p class='dim'>이 패턴에 걸리면 목록·요약·보고에서 뺍니다.</p>")
    noise_lists = [
        ("ignore_senders", "발신자 포함 문자열", cfg.ignore_senders),
        ("subject_noise_strong", "제목 강한 노이즈 (무조건 제외)", cfg.subject_noise_strong),
        ("subject_noise_weak", "제목 약한 노이즈 (미참여+대량일 때만)", cfg.subject_noise_weak),
    ]
    for key, label, items in noise_lists:
        out.append(f"<h3>{esc(label)}</h3>")
        rows = "".join(
            "<div class='setrow'>"
            f"<span class='mono'>{esc(p)}</span>"
            "<form method='post' action='/settings/noise'>"
            f"<input type='hidden' name='op' value='remove'>"
            f"<input type='hidden' name='list' value='{key}'>"
            f"<input type='hidden' name='pattern' value='{esc(p)}'>"
            "<button class='danger'>삭제</button></form></div>"
            for p in items)
        add = ("<form method='post' action='/settings/noise' class='setadd'>"
               f"<input type='hidden' name='op' value='add'>"
               f"<input type='hidden' name='list' value='{key}'>"
               "<input type='text' name='pattern' placeholder='패턴 추가'>"
               "<button>추가</button></form>")
        out.append(f"<div class='setlist'>{rows or ''}{add}</div>")

    # ── 정보 (About) ──
    out.append("<h2>정보</h2>")
    out.append(
        "<div class='setlist'>"
        "<div class='setrow'>"
        "<span class='setlabel'>버전</span>"
        f"<span class='setval'>Minerva v{esc(__version__)}</span></div>"
        "<div class='setrow'>"
        "<span class='setlabel'>라이선스</span>"
        "<span class='setval'>MIT © 2026 Dongjin Park · "
        "<a href='https://github.com/dongjinpark-maker/mailkb' "
        "target='_blank' rel='noopener noreferrer'>GitHub</a></span></div>"
        "<div class='setrow'>"
        "<span class='setlabel'>최신 코드로 업데이트"
        "<span class='setsub'>받은 뒤 창을 닫았다 다시 열면 적용됩니다</span></span>"
        "<form method='post' action='/settings/update'>"
        "<button type='submit'>업데이트</button></form></div>"
        "</div>")
    out.append("</div>")
    return "\n".join(out)


def _git_update() -> str:
    """설정의 '최신으로 업데이트' — 코드 폴더에서 git pull --ff-only 후 결과 경로.
    고정 명령·고정 cwd(리포)라 인젝션 없음. 적용은 창을 닫았다 다시 열 때(새 서버)."""
    import shutil
    import subprocess
    repo = Path(__file__).resolve().parent.parent
    git = shutil.which("git")
    if not git:
        return "/settings?msg=" + _q("git 을 찾지 못했습니다 — 수동으로 git pull 하세요")
    try:
        r = subprocess.run([git, "pull", "--ff-only"], cwd=str(repo),
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return "/settings?msg=" + _q(f"업데이트 실패: {e}")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return "/settings?msg=" + _q("업데이트 실패: " + (out[:120] or "git pull 오류"))
    if "up to date" in out.lower():
        return "/settings?msg=" + _q("이미 최신입니다")
    return "/settings?msg=" + _q("업데이트 완료 — 창을 닫았다 다시 열면 적용됩니다")


_SETTINGS_INTS = [   # (폼 필드, 오버라이드 섹션, 키, 최소값)
    ("broadcast_to", "review", "broadcast_to", 1),
    ("direct_to", "review", "direct_to", 0),
    ("stall_workdays", "review", "stall_workdays", 1),
    ("stale_workdays", "review", "stale_workdays", 1),
    ("summary_max_days", "ai", "summary_max_days", 1),
    ("ask_max_input_tokens", "ai", "ask_max_input_tokens", 0),   # 0=제한 없음
    ("reading_width", "web", "reading_width", 600),
    ("reading_font", "web", "reading_font", 12),
    ("sync_interval_min", "web", "sync_interval_min", 0),   # 0=자동 동기화 끔
    ("image_retain_days", "web", "image_retain_days", 0),   # 0=이미지 임베드 끔
]
_NOISE_LISTS = {"ignore_senders", "subject_noise_strong", "subject_noise_weak"}


def _save_settings(home, form: dict) -> str:
    """판정 기준 폼 → overrides.json. 파싱 안 되는 값은 건너뛴다."""
    n = 0
    for field_, sec, key, lo in _SETTINGS_INTS:
        v = (form.get(field_) or [""])[0].strip()
        if not v:
            continue
        try:
            cfgmod.set_override(home, sec, key, max(lo, int(v)))
            n += 1
        except ValueError:
            pass
    for field_, key in [("summary_backend", "summary"),
                        ("search_backend", "search"), ("ask_backend", "ask"),
                        ("weekly_backend", "weekly")]:
        v = (form.get(field_) or [""])[0].strip()
        if v:
            cfgmod.set_override(home, "ai", key, v)
            n += 1
    return "/settings?msg=" + _q(f"설정 저장: {n}개 항목")


def _save_noise(cfg, form: dict) -> str:
    """노이즈 규칙 add/remove → 현재 리스트를 갱신해 overrides.json 에 저장."""
    op = (form.get("op") or [""])[0]
    lst = (form.get("list") or [""])[0]
    pat = (form.get("pattern") or [""])[0].strip().lower()
    if lst not in _NOISE_LISTS or not pat:
        return "/settings?msg=" + _q("잘못된 노이즈 입력")
    cur = list(getattr(cfg, lst))
    if op == "add":
        if pat not in cur:
            cur.append(pat)
    elif op == "remove":
        cur = [p for p in cur if p != pat]
    else:
        return "/settings?msg=" + _q("잘못된 동작")
    cfgmod.set_override(cfg.home, "filters", lst, cur)
    return "/settings?msg=" + _q("노이즈 규칙 갱신")


def render_person(store, cfg, addr: str) -> str:
    """이 주소와 주고받은 메일 전체(양방향) — 메일함과 같은 목록 UI(왼쪽 프레임).

    내가 그에게 보낸 것(배경색 구별)과 그가 나에게 보낸 것을 최신순으로.
    발신자 차단 버튼이 여기 있다(스레드에서 이름 클릭 → 이 페이지에서 차단).
    """
    addr = (addr or "").strip().lower()
    if not addr:
        return "<h1>주소별 메일</h1><p class='empty'>주소가 없습니다</p>"
    rows = store.correspondence(addr, limit=200)
    name = store.person_name(addr) or addr
    blocked = cfg.is_blocked(addr)
    if blocked:
        block_ctl = ("<span class='dim'>⛔ 차단됨 (해제는 "
                     "<a href='/settings'>설정</a>)</span>")
    else:
        block_ctl = (f"<form method='post' action='/block' style='margin:0'>"
                     f"<input type='hidden' name='addr' value='{esc(addr)}'>"
                     "<button class='btn-caution'>발신자 차단</button></form>")
    # 한 줄(같은 높이): ← 뒤로(왼쪽) · 이름(가운데) · 발신자 차단(오른쪽)
    out = ["<div class='personhead'>"
           "<a href='#' class='backlink'>← 뒤로</a>"
           f"<span class='ptitle'>{esc(name)}</span>"
           f"<span class='pright'>{block_ctl}</span></div>",
           f"<p class='dim'>전체 {len(rows)} (양방향) · "
           f"<span class='mono'>{esc(addr)}</span> · "
           "<span class='kbdhint'>→ 표시·배경색 = 내가 보낸 메일</span></p>"]
    if not rows:
        out.append("<p class='empty'>주고받은 메일 없음</p>")
        return "\n".join(out)
    items = []
    for r in rows:
        if r["is_sent"]:
            cls = "mrow sent"
            sub_who = f"→ {name}"                       # 내가 이 사람에게 보냄
            subj = r["subject"] or "(제목 없음)"
        else:
            cls = "mrow read" if r["read_at"] else "mrow"
            sub_who = r["sender_name"] or r["sender_addr"]
            subj = r["subject"] or "(제목 없음)"
        items.append(
            f"<a class='{cls}' href='/thread/{r['thread_id']}?focus={r['id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(subj)}</span>"
            f"<span class='mdate'>{esc(_fmt_when(r['sent_on']))}</span></span>"
            f"<span class='msubj'>{esc(sub_who)}</span></a>")
    out.append(f"<div class='mlist'>{''.join(items)}</div>")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────── 인물 도시에 (v1)
# 이미 추출된 메일·액션·결정·신호를 사람 중심으로 재조립한 결정론 화면.
# 정체성 기준 = 이메일 주소(동명이인 자동 분리). 이름 매칭 카드(결정·변화)는
# store 가 참여 스레드로 교집합해 동명이인 오염을 막는다. 모든 항목에 근거 링크.

def _days_ago(iso: str, ref: str) -> str:
    """last_seen → 'N일 전' 상대 표기(기준 ref 대비)."""
    if not iso:
        return ""
    try:
        d0 = date.fromisoformat(iso[:10])
        d1 = date.fromisoformat((ref or iso)[:10])
    except ValueError:
        return ""
    n = (d1 - d0).days
    if n <= 0:
        return "오늘"
    if n == 1:
        return "어제"
    if n < 14:
        return f"{n}일 전"
    if n < 60:
        return f"{n // 7}주 전"
    return f"{n // 30}개월 전"


def render_people_page(store, cfg) -> str:
    """'인물' 랜딩 — 최근 N개월 교류 강도순 목록. 순위는 report.rank_people
    (계산 분리), 미결 배지는 액션 큐를 발신자별 집계."""
    win = int(cfg.opt("dossier", "window_weeks", default=26) or 26)
    ranked = report.rank_people(store, cfg, window_weeks=win)
    out = ["<h1>인물</h1>",
           f"<p class='dim'>최근 {max(1, win // 4)}개월 · 교류 강도순 · "
           f"{len(ranked)}명</p>"]
    if not ranked:
        out.append("<p class='empty'>교류 기록이 없습니다 — 먼저 동기화하세요.</p>")
        return "\n".join(out)
    # 동명(표시 이름 충돌) → 도메인 접미사로 구분
    name_counts: dict[str, int] = {}
    for r in ranked:
        nm = r["name"] or r["addr"]
        name_counts[nm] = name_counts.get(nm, 0) + 1
    now = max((r["last_seen"] for r in ranked), default="")
    roles = store.dossier_roles()          # addr → 캐시된 역할 한 줄(있으면)
    rows = []
    for r in ranked:
        nm = r["name"] or r["addr"]
        label = esc(nm)
        if name_counts.get(nm, 0) > 1:
            label += f" <span class='dim'>({esc(r['addr'].split('@')[-1])})</span>"
        role = roles.get(r["addr"])
        if role:
            label += f"<span class='prole'>{esc(role)}</span>"
        rows.append(
            f"<a class='prow' href='/people?addr={_q(r['addr'])}'>"
            f"<span class='pnm'>{label}</span>"
            f"<span class='pmeta'>↓{r['recv']} ↑{r['sent']}"
            f"<span class='pago'>{esc(_days_ago(r['last_seen'], now))}</span>"
            f"</span></a>")
    out.append(f"<div class='plist'>{''.join(rows)}</div>")
    return "\n".join(out)


def _wordmap_chip(item, css: str = "") -> str:
    """특징어 칩 — 메일 단위 지지도와 최신 근거 스레드를 함께 표시."""
    support = int(item.get("support", 0))
    evidence = item.get("evidence") or []
    title = f"{support}통에서 확인"
    if "lift" in item:
        title += f" · 다른 인물 대비 {float(item['lift']):+.1f}"
    if "ratio" in item:
        title += f" · 최근 지지율 {float(item['ratio']):.1f}배"
    body = (f"{esc(item.get('term', ''))}"
            f"<span class='wmn'>{support}</span>")
    classes = f"wmterm {css}".strip()
    if evidence:
        return (f"<a class='{classes}' href='/thread/{int(evidence[0])}' "
                f"title='{esc(title)}'>{body}</a>")
    return f"<span class='{classes}' title='{esc(title)}'>{body}</span>"


def _wordmap_html(profile: dict, months: int = 6) -> str:
    """업무 어휘 지도 — 구문·공기어 군집·특징어·상승어·사람 언급."""
    rows = []
    phrases = profile.get("phrases") or []
    if phrases:
        rows.append("<div class='wmgroup'><div class='wmlabel'>반복 구문</div>"
                    f"<div class='wmterms'>{''.join(_wordmap_chip(x) for x in phrases)}</div>"
                    "</div>")
    for cluster in profile.get("clusters") or []:
        chips = "".join(_wordmap_chip(x) for x in cluster.get("terms") or [])
        if chips:
            rows.append(
                f"<div class='wmgroup'><div class='wmlabel'>{esc(cluster.get('label', '연관어'))}"
                f"</div><div class='wmterms'>{chips}</div></div>")
    singles = profile.get("terms") or []
    if singles:
        rows.append("<div class='wmgroup'><div class='wmlabel'>특징어</div>"
                    f"<div class='wmterms'>{''.join(_wordmap_chip(x) for x in singles)}</div>"
                    "</div>")
    rising = profile.get("rising") or []
    if rising:
        rows.append("<div class='wmgroup'><div class='wmlabel'>최근 상승</div>"
                    f"<div class='wmterms'>{''.join(_wordmap_chip(x, 'wmrise') for x in rising)}"
                    "</div></div>")
    mentions = profile.get("mentions") or []
    if mentions:
        links = "".join(
            f"<a href='/people?addr={_q(x['addr'])}'>{esc(x['name'])} "
            f"<span class='dim'>{int(x['support'])}통</span></a>"
            for x in mentions)
        rows.append("<div class='wmgroup'><div class='wmlabel'>함께 언급</div>"
                    f"<div class='wmmentions'>{links}</div></div>")
    return (f"<div class='wordmap'>{''.join(rows)}</div>"
            f"<p class='dcap'>최근 {months}개월 · 발신 {int(profile.get('mail_count', 0))}통"
            " · 메일 단위 출현 · 다른 인물 대비 · 표현을 누르면 근거 스레드로 이동</p>")


def _spark_svg(series, w: int = 120, h: int = 22) -> str:
    """주별 총량 스파크라인 — 자족적 인라인 SVG polyline(팔레트 토큰 스트로크).
    마지막 점 강조. 값이 3주 미만이거나 전부 0이면 빈 문자열."""
    s = [max(0, int(v)) for v in (series or [])]
    if len(s) < 3 or sum(s) == 0:
        return ""
    pad = 2.0
    n = len(s)
    mx = max(s) or 1
    def xs(i): return pad + i * (w - 2 * pad) / (n - 1)
    def ys(v): return h - pad - (v / mx) * (h - 2 * pad)
    pts = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(s))
    lx, ly = xs(n - 1), ys(s[-1])
    return (f"<svg class='rspark' viewBox='0 0 {w} {h}' "
            f"role='img' aria-label='주별 교신 추세'>"
            f"<polyline points='{pts}' fill='none' stroke='var(--accent)' "
            "stroke-width='1.6' stroke-linejoin='round' stroke-linecap='round'/>"
            f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='2.1' fill='var(--accent)'/></svg>")


def _relmetrics_html(m, months: int = 3) -> str:
    """관계 수치 카드 본문 — ① 주고받기 균형 막대 ② 회신 속도 비교 ③ 주별 교신
    스파크 + 최근 접촉. 재료 없는 요소는 생략(graceful). 관찰 가능한 사실만."""
    recv, sent = m["recv"], m["sent"]
    parts = []

    # ① 주고받기 균형 막대 — 받은/보낸 비율. 둘 다 0이면 생략.
    if recv + sent > 0:
        parts.append(f"<p class='dcap' style='margin:8px 0 0'>최근 {months}개월</p>")
        segs = ""
        if recv:
            segs += f"<div class='relseg recv' style='flex:{recv}'></div>"
        if sent:
            segs += f"<div class='relseg sent' style='flex:{sent}'></div>"
        parts.append(
            "<div class='relbal'>"
            f"<span class='rlbl'><span class='sw recv'></span>받은 {recv}</span>"
            f"<div class='relbar'>{segs}</div>"
            f"<span class='rlbl'><span class='sw sent'></span>보낸 {sent}</span></div>")

    # ② 회신 속도 비교 — 공통 스케일 막대. 둘 다 None 이면 블록 생략.
    their, mine = m["their_median_h"], m["my_median_h"]
    if their is not None or mine is not None:
        mx = max(v for v in (their, mine) if v is not None) or 1
        rows = []
        if their is not None:
            rows.append(
                f"<div class='rsrow'><span class='rsname'>이 사람</span>"
                f"<div class='rstrack'><div class='rsfill' style='width:{their / mx * 100:.0f}%'>"
                f"</div></div><span class='rsval'>{report._fmt_h(their)}</span></div>")
        if mine is not None:
            rows.append(
                f"<div class='rsrow'><span class='rsname'>나</span>"
                f"<div class='rstrack'><div class='rsfill' style='width:{mine / mx * 100:.0f}%'>"
                f"</div></div><span class='rsval'>{report._fmt_h(mine)}</span></div>")
        cap = "회신까지 걸린 시간(중앙값) · 막대 길수록 느림"
        if their is None:
            cap = "회신까지 걸린 시간(중앙값) · 이 사람 회신 표본 없음"
        elif mine is None:
            cap = "회신까지 걸린 시간(중앙값) · 내 회신 표본 없음"
        parts.append(f"<div class='rsblock'>{''.join(rows)}"
                     f"<p class='dcap'>{cap}</p></div>")

    # ③ 주별 교신 스파크 + 최근 접촉 (한 줄)
    series = [a + b for a, b in zip(m["recv_series"], m["sent_series"])]
    spark = _spark_svg(series)
    last = ""
    if m["last_seen"]:
        last = ("최근 접촉 " + _days_ago(m["last_seen"], m["asof"].isoformat()))
    foot = ""
    if spark:
        foot += f"<span class='sparkwrap'><span class='sparklbl'>주별 교신</span>{spark}</span>"
    if last:
        foot += f"<span class='rlast'>{last}</span>"
    if foot:
        parts.append(f"<div class='relfoot'>{foot}</div>")

    return "".join(parts)


def _dossier_ai_card(dz, unreflected: int = 0, today: str = "") -> str:
    """AI 요약 카드 — 캐시된 dossier_md(## 섹션 + '- [#N] 서술') 렌더. 각 줄에
    근거 스레드 링크, 하단에 갱신일·추정 안내. 근거 검증은 생성 시(distill) 완료.

    갱신이 자동이 아니게 된 뒤로는 **얼마나 낡았는지**가 카드의 신뢰도 정보다 —
    푸터에 경과일과 반영되지 않은 새 메일 수를 함께 싣는다."""
    body = []
    for raw in (dz["dossier_md"] or "").splitlines():
        s = raw.strip()
        if s.startswith("## "):
            body.append(f"<div class='dsec'>{esc(s[3:].strip())}</div>")
        elif s.startswith("- "):
            body.append(f"<div class='dclaim'>{_md_inline(s[2:].strip())}</div>")
    upd = (dz["updated"] or "")[:10]
    cap = esc(upd) + " 갱신"
    ago = _days_ago(upd, today or date.today().isoformat())
    if ago and ago != "오늘":
        cap += f"({esc(ago)})"
    if unreflected > 0:
        cap += f" · 새 메일 {unreflected}통 미반영"
    return ("<div class='dcard aidoss'><h2>요약 <span class='aitag'>AI 추정</span></h2>"
            + "".join(body)
            + f"<p class='dcap'>{cap} · 근거는 #스레드로 확인</p></div>")


def render_dossier(store, cfg, addr: str) -> str:
    """단일 인물 도시에 — AI 요약(있으면 맨 위) + 결정론 카드. 빈 카드는 생략."""
    addr = (addr or "").strip().lower()
    if not addr:
        return "<h1>인물</h1><p class='empty'>주소가 없습니다</p>"
    name = store.person_name(addr) or addr
    win = int(cfg.opt("dossier", "window_weeks", default=26) or 26)
    m = report.person_metrics(store, cfg, addr, weeks=win)
    out = ["<div class='personhead'><a href='/people' class='backlink'>← 인물</a>"
           f"<span class='ptitle'>{esc(name)}</span>"
           f"<span class='pright'><a href='/person?addr={_q(addr)}'>"
           "전체 왕래 메일 →</a></span></div>",
           f"<p class='dim'><span class='mono'>{esc(addr)}</span></p>",
           ]

    # AI 요약(v2) — 결정론 카드 위에 얹는다. 캐시 없으면 생략(graceful).
    dz = store.people_dossier(addr)
    has_ai = bool(dz and (dz["dossier_md"] or "").strip())
    stale_row = store.people_dossier(addr, include_stale=True) if not dz else None
    cnt = store.person_msg_count(addr)
    basis = dz["basis_msg_count"] if dz else 0
    unreflected = max(0, cnt - basis) if dz else 0

    # 버튼 둘 — 하는 일이 다르다. 요약 갱신은 저장되는 카드를 다시 만들고,
    # 대화 분석은 그 사람에 대한 질문에 답한다(대화록으로 이동).
    label = ("요약 갱신" if has_ai
             else ("요약 다시 만들기" if stale_row else "AI 요약 만들기"))
    fresh = " <span class='dim'>새 메일 없음 — 갱신해도 내용이 거의 같습니다</span>"
    out.append(
        "<div class='actions'>"
        "<form method='post' action='/people/dossier'>"
        f"<input type='hidden' name='addr' value='{esc(addr)}'>"
        f"<button class='aibtn'>{label}</button></form>"
        # 대화 분석(브리핑) — 분석 엔진에 '이 사람과의 왕래' 범위만 고정.
        # 이름은 상단 '분석' 메뉴와 일관되게(진입하면 분석 대화록으로 열린다).
        "<form class='asklaunch' method='post' action='/ask/jobs'>"
        f"<input type='hidden' name='person' value='{esc(addr)}'>"
        "<button class='aibtn ghost'>대화 분석</button></form></div>"
        "<p class='dim dosshint'>요약 = 이 사람 카드(저장됨) · "
        "대화 분석 = 질문에 답(대화록으로)"
        + (fresh if has_ai and not unreflected else "") + "</p>")

    with _dossier_lock:
        dj = dict(_dossier_job)
    if dj["running"] and dj["addr"] == addr:
        # 갱신 중에도 직전 요약은 지우지 않는다 — 아직 유효한 정보다.
        out.append(_dossier_wait_html(dj))
    elif (dj["addr"] == addr and dj["stage"] in _DOSSIER_NOTE
            and time.time() - (dj.get("done_at") or 0) < _NOTE_TTL):
        # 실패(stage="error")면 잡이 남긴 구체적 사유를 우선한다 — 고정 문구
        # "잠시 후 다시 눌러 주세요"는 SSO 만료에선 정반대 조언이다. 나머지
        # 상태는 사유 필드를 읽지 않는다(직전 실행 잔재를 섞지 않기 위해).
        note = ((dj.get("error") or "").strip() if dj["stage"] == "error"
                else "") or _DOSSIER_NOTE[dj["stage"]]
        out.append(f"<div class='aifail'>{esc(note)}</div>")

    if has_ai:
        out.append(_dossier_ai_card(dz, unreflected))
    elif stale_row is not None:
        # 검증 규약이 바뀌면 옛 카드는 숨겨진다. 배치 갱신이 없어진 뒤로는
        # 이 안내가 유일한 복구 동선이다.
        out.append("<div class='dcard'><h2>요약</h2><p class='empty'>"
                   "요약 형식이 바뀌어 다시 만들어야 합니다 — 위 "
                   "<b>요약 다시 만들기</b>를 눌러 주세요.</p></div>")

    cards: list[tuple[str, str]] = []

    # 1. 관계 수치 — 시각화(균형 막대·회신 비교·주별 스파크). 재료 있을 때만.
    if m:
        viz = _relmetrics_html(m, months=max(1, win // 4))
        if viz:
            cards.append(("관계 수치", viz))

    # 2. 진행 중 (왕복 잦은 스레드)
    if m and m["pingpong"]:
        lis = "".join(
            f"<li>{esc(pp['subject'])} <span class='dim'>· {pp['turns']}왕복</span> "
            f"<a href='/thread/{pp['thread_id']}'>#{pp['thread_id']}</a></li>"
            for pp in m["pingpong"][:6])
        cards.append(("진행 중", f"<ul>{lis}</ul>"))

    # 3. 이 사람에게 한 내 약속 — 미팅 전에 가장 먼저 알아야 할 것.
    # promises 는 내가 직접 쓴 확정 어미만 뽑으므로(정규식 요청 판정과 다르다)
    # 그 사람 참여 스레드로 좁히기만 하면 새 계산이 없다.
    ptids = store.person_thread_ids(addr)
    mine = [x for x in promises.extract(store) if x["thread_id"] in ptids]
    if mine:
        lis = "".join(
            f"<li>{esc(x['quote'])} "
            + (f"<span class='dim'>· {x['days']}일 전</span> " if x["days"] else "")
            + (f"<span class='warn'>⚠ 기한 {x['due'].isoformat()}</span> "
               if x.get("due") else "")
            + f"<a href='/thread/{x['thread_id']}'>#{x['thread_id']}</a></li>"
            for x in mine[:6])
        cards.append(("이 사람에게 한 내 약속", f"<ul>{lis}</ul>"))

    # 4. 관여한 결정 (결정자=이 사람, 참여 스레드 교집합)
    decs = store.person_decisions(addr, name)
    if decs:
        lis = "".join(
            f"<li>{esc(d['title'])} "
            f"<a href='/thread/{d['thread_id']}'>#{d['thread_id']}</a></li>"
            for d in decs[:8])
        cards.append(("관여한 결정", f"<ul>{lis}</ul>"))

    # 5. 최근 변화 (축적된 인물 신호 — distill_signals 첫 소비처)
    sigs = store.person_signals(addr, name)
    if sigs:
        lis = "".join(
            f"<li>{esc(s['signal'])} "
            f"<a href='/thread/{s['thread_id']}'>#{s['thread_id']}</a> "
            f"<span class='dim'>· {esc(s['date'])}</span></li>"
            for s in sigs[:8])
        cards.append(("최근 변화", f"<ul>{lis}</ul>"))

    # 6. 업무 어휘 지도 — 최근 창 안 메일 단위 지지도·대조 점수·공기어 군집.
    # 하드 노이즈는 제외하고, compact 대상 bag + 현재 창 rolling DF를 읽는다.
    min_mails = int(cfg.opt("dossier", "wordcloud_min_mails", default=8) or 8)
    top_n = int(cfg.opt("dossier", "wordcloud_top", default=25) or 25)
    basis = (store.person_word_basis(addr, win)
             if not cfg.is_noise_sender_hard(addr) else None)
    if basis and basis["mail_count"] >= min_mails:
        extra = ([n for n in cfg.my_names if n]
                 + [a.split("@")[0] for a in cfg.my_addresses]
                 + list(cfg.opt("dossier", "word_stop_extra", default=[]) or []))
        ranked = report.rank_people(store, cfg, window_weeks=win, limit=50)
        eligible = {r["addr"].lower() for r in ranked}
        eligible.add(addr)
        names = store.word_people_names()
        names[addr] = name
        corpus_fingerprint = store.people_word_corpus_fingerprint(
            eligible, win)
        version = terms.profile_signature(
            extra, eligible, top_n, name, people_names=names,
            corpus_fingerprint=corpus_fingerprint)
        profile = store.people_word_profile(addr, basis, win, version)
        if profile is None:
            rows = store.person_word_bag_rows(addr, win)
            background = None
            if rows is not None:
                candidates = terms.background_candidates(
                    rows, addr, names=names, extra_stop=extra)
                background = store.people_word_background(
                    eligible, addr, win, candidates=candidates,
                    corpus_fingerprint=corpus_fingerprint)
            if rows is None or background is None:
                rows = store.people_word_rows(eligible, win)
                background = None
            profile = terms.analyze(
                rows, addr, names=names, extra_stop=extra, top_n=top_n,
                background=background)
            store.save_people_word_profile(addr, profile, basis, win, version)
        if any(profile.get(k) for k in
               ("clusters", "terms", "phrases", "rising", "mentions")):
            cards.append(("업무 어휘 지도",
                          _wordmap_html(profile, months=max(1, win // 4))))

    if not cards and not has_ai:
        out.append("<p class='empty'>아직 이 사람에 대한 도시에 재료가 없습니다.</p>")
    for title, body in cards:
        out.append(f"<div class='dcard'><h2>{esc(title)}</h2>{body}</div>")
    return "\n".join(out)


_SEARCH_HINT = ("<p class='shint'><code>from:</code> <code>to:</code> "
                "<code>after:2026-06</code> <code>before:</code> "
                "<code>has:attachment</code> "
                "<code>is:sent</code> · <code>\"정확한 구\"</code></p>")


def _period_tokens(period: str, today: str) -> list:
    """상세 폼의 기간 선택 → DSL 날짜 토큰. today = 'YYYY-MM-DD'."""
    try:
        d = date.fromisoformat(today)
    except (ValueError, TypeError):
        return []
    ym = today[:7]
    if period == "thismonth":
        return [f"after:{ym}"]
    if period == "lastmonth":
        prev = (d.replace(day=1) - timedelta(days=1)).isoformat()[:7]
        return [f"after:{prev}", f"before:{ym}"]
    if period == "3months":
        m = d.replace(day=1)
        for _ in range(3):
            m = (m - timedelta(days=1)).replace(day=1)
        return [f"after:{m.isoformat()[:7]}"]
    if period == "thisyear":
        return [f"after:{today[:4]}"]
    return []


# 칩에서 뺄 서술형 어미 — 어휘 지도용 불용어(report.WORD_STOP)는 이걸 안 거른다.
_CHIP_TAIL_RX = re.compile(
    r"(습니다|합니다|됩니다|입니다|드립니다|하겠|으로는|으로|에서|에게|부터|까지"
    r"|하고|하는|한다|이다|같습|보입|필요합)$")


def _narrow_chips(raw: str, rows: list, exclude) -> str:
    """선택 검색이 정밀 결과를 못 냈을 때 '좁힐 말' 후보 — **코드가 고르지 않는다.**

    2026-08-07 실측(코퍼스 문장 150개): 임의의 문장을 고르면 93%가 정밀 결과
    (tier 1~3)를 못 낸다. 그런데 그중 **97%는 선택 안의 어느 한 단어로는** 정밀
    결과가 나온다 — 정보가 없는 게 아니라 어느 단어인지 코드가 모르는 것이다.
    상위 3개를 AND 로 묶어 자동 정제해 봤더니 정밀 회복은 6~10%인데 0건이
    30~64%였다. 소음 대신 아무것도 안 남는다.

    그래서 고르지 말고 **보여 준다**. 칩이 하나 빗나가도 안 누르면 그만이지만,
    자동 정제가 빗나가면 결과가 통째로 사라지고 사용자는 이유를 못 본다(폐기한
    '지금 할 일' 큐와 같은 부류의 실수다). 3개면 94%가 덮인다.

    정렬은 **긴 말 순**이다 — 드문 말 순은 3개 기준 75%로 떨어진다. 드문 말은
    변별력이 있어서가 아니라 토큰화 조각(`그대`·`두고`)이 드물어서 올라온다.
    서술형 어미는 뺀다(`좋습니다`·`관점으로`가 길이만으로 칩에 올라왔다) —
    커버리지 손실 없이(92%→93%) 조각이 0이 된다.
    """
    if not raw or any(r["tier"] in (1, 2, 3) for r in rows):
        return ""
    feat = terms.extract_features(raw)
    surface = {k: s for k, s, _n in feat.get("body_surfaces") or []}
    low = raw.strip().lower()
    seen, cands = set(), []
    for tok in (t for s in feat["body"] for t in s):
        if len(tok) < 2 or tok in seen or tok == low:
            continue
        seen.add(tok)
        cands.append(tok)
    kept = [t for t in cands if not _CHIP_TAIL_RX.search(t)] or cands
    top = sorted(kept, key=lambda t: (-len(t), t))[:3]
    if not top:
        return ""
    ex = f"&exclude={exclude}" if exclude is not None else ""
    chips = "".join(
        f"<a class='nchip' href='/search?sel=1&q={_q(surface.get(t, t))}{ex}'>"
        f"{esc(surface.get(t, t))}</a>" for t in top)
    return ("<p class='narrow'>고른 문장과 똑같이 일치하는 메일이 없습니다 — "
            f"한 단어로 좁혀 보세요 {chips}</p>")


def _int_or_none(v: str):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _search_effective(qs, today: str) -> tuple:
    """(raw_q, effective_q) — 검색창 q 에 상세 폼(f_*) 을 DSL 토큰으로 병합.

    상세 폼은 별도 쿼리 경로가 아니라 '검색식을 만들어 검색창에 써넣는' 빌더다
    (단일 진실원). 병합된 effective 를 다시 검색창 value 로 보여 편집 가능하게 한다.
    """
    raw = (qs.get("q") or [""])[0].strip()
    extra = []
    ff = (qs.get("f_from") or [""])[0].strip()
    if ff:
        extra.append(f'from:"{ff}"' if " " in ff else f"from:{ff}")
    extra += _period_tokens((qs.get("f_period") or [""])[0], today)
    if (qs.get("f_has") or [""])[0] == "1":
        extra.append("has:attachment")
    d = (qs.get("f_dir") or [""])[0]
    if d == "sent":
        extra.append("is:sent")
    elif d == "received":
        extra.append("is:received")
    effective = (raw + " " + " ".join(extra)).strip() if extra else raw
    return raw, effective


def _snip_html(snippet: str) -> str:
    """snippet() 의 ⟪⟫ 표시를 <mark> 로. HTML 은 먼저 escape."""
    return esc(snippet).replace("⟪", "<mark>").replace("⟫", "</mark>")


def _hl_terms(effective: str) -> str:
    """검색 결과에서 스레드로 넘길 '칠할 말' — DSL 필터는 뺀 본문 낱말만.

    `from:강미래 after:2026-06 리포트` 로 찾았으면 `리포트` 만 칠해야 한다.
    파서가 이미 필터와 낱말을 갈라 두므로(`search.parse_query`) 여기서 다시
    쪼개지 않는다 — 질의 문법의 진실원은 한 곳이다."""
    q = search_mod.parse_query(effective or "")
    return " ".join(q.phrases + q.terms).strip()


def _search_facets(rows, effective: str) -> str:
    """결과에서 상위 발신자를 뽑아 좁히기 칩으로. 클릭 시 effective 에 from: 추가."""
    counts: dict = {}
    for r in rows:
        nm = r["sender_name"] or r["sender_addr"]
        if nm:
            counts[nm] = counts.get(nm, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    if len(top) < 2:
        return ""
    chips = []
    for nm, c in top:
        tok = f'from:"{nm}"' if " " in nm else f"from:{nm}"
        href = "/search?q=" + urllib.parse.quote(f"{effective} {tok}".strip())
        chips.append(f"<a class='facet' href='{esc(href)}'>{esc(nm)}<b>{c}</b></a>")
    return "<div class='facets'>" + "".join(chips) + "</div>"


def _ai_card(it: dict) -> str:
    """AI 추천 결과 한 건 — 제목(링크)·발신·날짜·한 줄 이유."""
    arrow = "→ " if it.get("is_sent") else ""
    reason = (it.get("reason") or "").strip()
    rhtml = f"<p class='aireason'>{esc(reason)}</p>" if reason else ""
    return (
        f"<li class='aicard'><a class='aititle' href='/thread/{it['thread_id']}?focus={it['id']}'>"
        f"{esc(it['subject'] or '(제목 없음)')}</a>"
        f"<div class='aimeta'>{arrow}{esc(it.get('sender') or '')} · "
        f"{esc(it.get('date') or '')}</div>{rhtml}</li>")


# ─────────────────────────────────────────────────── AI 검색(백그라운드)

# 단계 → (표시문구, 프로그레스 단계). prelim 은 검색 직후 잠정 결과를 흘리는
# 신호라 단계는 search 와 같은 2로 둔다(본문심사에서 3으로 오른다).
_AISEARCH_STAGE = {
    "translate": ("질문을 검색식으로 번역하는 중", 1),
    "search":    ("검색식으로 메일을 훑는 중", 2),
    "prelim":    ("1차 후보를 추리는 중 — 아래 잠정 결과", 2),
    "judge":     ("후보 본문을 읽고 확정하는 중", 3),
}
_AISEARCH_TOTAL = 3


def _aisearch_progress(stage: str, payload=None) -> None:
    """review.ai_search 의 진행 콜백 — 잡 상태에 단계·잠정 결과를 실어 폴링에 노출."""
    with _aisearch_lock:
        if not _aisearch_job["running"]:
            return
        _aisearch_job["stage"] = stage
        if stage == "prelim" and payload:
            _aisearch_job["prelim"] = payload


def _run_aisearch_job(cfg, query: str, today: str, use_cache: bool,
                      cancel=None) -> None:
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            res = review.ai_search(store, cfg, query, today,
                                   use_cache=use_cache,
                                   progress=_aisearch_progress, cancel=cancel)
        finally:
            store.close()
        with _aisearch_lock:
            _aisearch_job.update(running=False, stage="done", result=res)
    except review.AICancelled:                   # 중지 → 일반 검색 결과로
        with _aisearch_lock:
            _aisearch_job.update(running=False, stage="cancelled")
    except (review.AIError, review.AIAuthError) as e:  # CLI 부재·인증 만료·타임아웃 → 일반검색 폴백
        with _aisearch_lock:
            _aisearch_job.update(running=False, stage="error", error=str(e)[:120])
    except Exception as e:                        # 방어적 — 잡이 조용히 죽지 않게
        with _aisearch_lock:
            _aisearch_job.update(
                running=False, stage="error",
                error=("AI 검색에 실패했습니다 — "
                       + (" ".join(str(e).split())[:100] or type(e).__name__)))


def _start_aisearch(cfg, query: str, today: str, use_cache: bool) -> bool:
    """AI 검색 잡 시작(단일 슬롯). 시작했으면 True, 이미 실행 중이면 False."""
    cancel = _job_start(_aisearch_job, _aisearch_lock, stage="translate",
                        query=query, fresh=not use_cache, result=None,
                        prelim=None)
    if cancel is None:
        return False
    threading.Thread(target=_run_aisearch_job,
                     args=(cfg, query, today, use_cache, cancel),
                     daemon=True).start()
    return True


def _aisearch_wait_html(st: dict) -> str:
    """진행 중 대기 화면 — 공용 대기 카드(prefix ai) + 잠정 결과(#ai-extra).

    잠정 결과는 카드가 아니라 extra 슬롯이다: 마크업이 있는 목록이라 텍스트
    패치로는 못 갈아끼우고(app.js patchJob 이 innerHTML 로 처리), 카드 폭에
    눌리면 읽기 어렵다."""
    stage = st.get("stage") or "translate"
    label, step = _AISEARCH_STAGE.get(stage, ("준비 중", 1))
    prelim = st.get("prelim") or {}
    pit = prelim.get("items") or []
    extra = ""
    if pit:
        extra = ("<p class='dim aiprelim'>잠정 결과 — 본문 확인 전, 검색기가 먼저 "
                 "추린 후보입니다. 확정되면 자동으로 정리됩니다.</p>"
                 "<ol class='aicards prelim'>"
                 + "".join(_ai_card(it) for it in pit) + "</ol>")
    return ("<div data-aisearch-running='1' hidden></div>"
            f"<h1>AI 검색 <span class='aiq'>· {esc(st.get('query', ''))}</span></h1>"
            + _job_wait_card(
                "ai", "AI가 찾고 있어요",
                stage=f"{label} · 단계 {step}/{_AISEARCH_TOTAL}",
                live=_job_live_line(st), preview=_job_preview(st),
                model=st.get("model") or "",
                step=step, total=_AISEARCH_TOTAL,
                hint="본문까지 읽어 후보를 확정합니다. "
                     + _cancel_hint(st.get("stream", False)),
                cancel_action="/aisearch/cancel", extra=extra,
                started=st.get("started") or 0.0))


def render_aisearch_status(store, cfg, today: str) -> tuple:
    """(inner, running) — 폴링·전체페이지 공용. 완료면 결과, 에러면 일반검색 폴백."""
    with _aisearch_lock:
        st = dict(_aisearch_job)
    if st["running"]:
        return _aisearch_wait_html(st), True
    if st["stage"] == "cancelled":
        q = st.get("query", "")
        return ("<div class='aifail'>AI 검색을 중지했습니다 — 일반 검색 결과입니다."
                "</div>" + render_search(store, cfg, {"q": [q]}, today), False)
    if st["stage"] == "error":
        q = st.get("query", "")
        retry = "/search?ai=1&fresh=1&q=" + urllib.parse.quote(q)
        banner = ("<div class='aifail'>AI 검색을 쓸 수 없습니다 — "
                  f"{esc(st.get('error', ''))}. 일반 검색 결과를 보여드립니다. "
                  f"<a class='aibtn ghost compact' href='{esc(retry)}'>다시 시도</a></div>")
        return banner + render_search(store, cfg, {"q": [q]}, today), False
    if st["result"]:
        return render_aisearch(st["result"]), False
    return ("<h1>AI 검색</h1><p class='empty'>진행 중인 AI 검색이 없습니다. "
            "검색 화면에서 <b>AI로 다시 찾기</b>를 눌러 주세요.</p>", False)


def render_aisearch(result: dict) -> str:
    """AI 검색 결과 화면 — 해석 DSL(편집)·추천 카드·그 외 후보·근거 표시."""
    raw = result.get("query", "")
    dsl = result.get("dsl", "")
    items = result.get("items", [])
    others = result.get("others", [])
    dsl_href = "/search?q=" + urllib.parse.quote(dsl or raw)
    fresh_href = "/search?ai=1&fresh=1&q=" + urllib.parse.quote(raw)
    out = [f"<h1>AI 검색 <span class='aiq'>· {esc(raw)}</span></h1>"]
    if dsl:
        out.append(
            "<div class='aidsl'>AI 해석 <code>" + esc(dsl) + "</code>"
            f"<a class='aiedit' href='{esc(dsl_href)}'>편집·일반검색</a></div>")
    exps = result.get("expansions") or []
    if exps:
        out.append("<p class='aiexp'>확장 검색어: " + esc(", ".join(exps[:8])) + "</p>")
    if result.get("note"):
        out.append(f"<p class='ainote'>{esc(result['note'])}</p>")
    if items:
        out.append("<p class='dim aihead'>이게 찾으시는 것 같아요</p>")
        out.append("<ol class='aicards'>"
                   + "".join(_ai_card(it) for it in items) + "</ol>")
    else:
        out.append(
            "<p class='empty'>정확히 맞는 메일을 찾지 못했습니다. "
            f"<a href='{esc(dsl_href)}'>일반 검색으로 보기</a> · "
            f"<a class='aibtn ghost compact' href='{esc(fresh_href)}'>"
            "다르게 다시 찾기</a></p>")
    if others:
        out.append(
            f"<details class='aiothers'><summary>그 외 후보 {len(others)}건</summary>"
            "<ol class='aicards'>" + "".join(_ai_card(it) for it in others)
            + "</ol></details>")
    cache = " · 캐시됨" if result.get("from_cache") else ""
    cost = result.get("cost") or {}
    parts = []
    secs = cost.get("seconds")
    if secs:
        parts.append(f"{secs / 60:.1f}분" if secs >= 60 else f"{secs:.0f}초")
    if cost.get("calls"):
        tok = int(cost.get("in", 0)) + int(cost.get("out", 0))
        parts.append(f"${cost.get('usd', 0):.3f}")
        parts.append(f"{tok:,}토큰·{cost['calls']}회")
    extra = (" · " + " · ".join(parts)) if parts else ""
    out.append(
        "<p class='aifoot'>후보 " + str(result.get("candidate_count", 0))
        + f"개 검토 · {esc(result.get('backend', ''))}{extra}{cache} · "
        f"<a href='{esc(dsl_href)}'>일반 검색 결과 보기</a> · "
        f"<a class='aibtn ghost compact' href='{esc(fresh_href)}'>새로 찾기</a></p>")
    return "\n".join(out)


def render_search(store, cfg, qs, today: str) -> str:
    raw, effective = _search_effective(qs, today)
    # AI 검색 모드(?ai=1) — 흐릿한 자연어를 번역·본문심사로. 명시적 클릭에서만.
    # 캐시 히트는 즉시(무과금·무대기), 미스는 백그라운드 잡+폴링(서버 안 멈춤, 방법 7·8).
    if (qs.get("ai") or [""])[0] == "1" and raw:
        fresh = (qs.get("fresh") or [""])[0] == "1"   # 캐시 우회 재실행('새로 찾기')
        if not fresh:
            cached = store.ai_search_get(review._normalize_q(raw))
            if cached and cached["result_json"]:
                try:
                    res = json.loads(cached["result_json"])
                    res["from_cache"] = True
                    return render_aisearch(res)
                except ValueError:
                    pass
        # 같은 질의로 방금 실패한(에러) 잡이 있으면 폴백을 그대로 보여준다 — JS-off
        # meta refresh 가 실패한 CLI 를 2초마다 재시도하는 무한 루프를 막는다.
        # ('다르게 다시 찾기'(fresh=1)로는 언제든 재시도 가능.)
        if not fresh:
            with _aisearch_lock:
                j = dict(_aisearch_job)
            if (not j["running"] and j["stage"] == "error"
                    and review._normalize_q(j.get("query", "")) == review._normalize_q(raw)):
                return render_aisearch_status(store, cfg, today)[0]
        started = _start_aisearch(cfg, raw, today, use_cache=not fresh)
        inner = render_aisearch_status(store, cfg, today)[0]
        if not started:
            # 슬롯을 남의 질의가 잡고 있으면 그 질의의 대기 화면이 나온다 —
            # 말하지 않으면 자기 검색이 도는 줄 안다(_job_start 계약).
            with _aisearch_lock:
                busy = _aisearch_job.get("query") or ""
            if review._normalize_q(busy) != review._normalize_q(raw):
                inner = (f"<div class='aifail'>다른 AI 검색(<b>{esc(busy)}</b>)이 "
                         "진행 중입니다 — 이 검색은 시작되지 않았습니다. "
                         "끝난 뒤 다시 눌러 주세요.</div>" + inner)
        return inner
    # /search 페이지는 편집 가능한 검색창 + 상세 빌더를 직접 둔다(질의 다듬기용).
    # 헤더 검색창은 '새 검색' 런처(입력하면 새로 시작). 결과가 없을 땐 상세를 펼쳐
    # 필터 옵션이 처음부터 보이게 한다.
    rows = store.search(effective, limit=50) if effective else []
    # 선택 검색(본문에서 드래그해 온 질의) — 읽고 있던 그 메일은 결과에서 뺀다.
    # 안 빼면 **1등이 자기 자신**이라 첫 줄이 늘 방금 읽던 문장이 된다(실측).
    ex = _int_or_none((qs.get("exclude") or [""])[0])
    if ex is not None:
        rows = [r for r in rows if r["id"] != ex]
    box = ("<form class='search' method='get' action='/search'>"
           f"<input type='text' name='q' value='{esc(effective)}' autofocus "
           "placeholder='검색 — 예: from:강미래 after:2026-06 리포트'> "
           "<button class='btn-primary'>검색</button></form>")
    ppl = store.frequent_people(200)
    opts = "".join(f"<option value='{esc(p['name'])}'>{esc(p['addr'])}</option>"
                   for p in ppl)
    adv_open = "" if rows else " open"       # 결과 없으면(첫 검색·0건) 상세를 펼쳐 노출
    adv = (
        f"<details class='adv'{adv_open}><summary>상세 검색</summary>"
        "<form class='advbody' method='get' action='/search'>"
        f"<input type='hidden' name='q' value='{esc(raw)}'>"
        "<label>사람 <input type='text' name='f_from' list='ppl' "
        "placeholder='이름/주소'></label>"
        "<label>기간 <select name='f_period'>"
        "<option value=''>전체</option><option value='thismonth'>이번달</option>"
        "<option value='lastmonth'>지난달</option><option value='3months'>최근3개월</option>"
        "<option value='thisyear'>올해</option></select></label>"
        "<label>방향 <select name='f_dir'>"
        "<option value=''>전체</option><option value='received'>받은</option>"
        "<option value='sent'>보낸</option></select></label>"
        "<label><input type='checkbox' name='f_has' value='1'> 첨부</label>"
        "<button>적용</button></form>"
        f"<datalist id='ppl'>{opts}</datalist></details>")
    out = ["<h1>검색</h1>", box, _SEARCH_HINT, adv]
    if (qs.get("sel") or [""])[0] == "1" and raw:
        # 무엇으로 찾았는지 밝힌다 — 질의는 손대지 않는다. 정밀 결과가 없을 때만
        # 좁힐 말을 **후보로** 내고 고르는 것은 사람이 한다(_narrow_chips).
        out.append(f"<p class='selq'>본문에서 고른 말 <b>「{esc(raw)}」</b></p>")
        out.append(_narrow_chips(raw, rows, ex))
    if effective:
        # 흐릿한 기억이면 AI 검색으로 — 명시적 클릭에서만(과금).
        # (답이 궁금하면 상단 '분석' 메뉴 — 검색 경유 진입점은 중복이라 제거)
        if raw:
            enc = urllib.parse.quote(raw)
            out.append("<div class='askrow'>"
                       f"<a class='aibtn' href='/search?ai=1&q={enc}'>"
                       "AI로 다시 찾기</a></div>")
        out.append(f"<p class='dim'>{len(rows)}건</p>")
        out.append(_search_facets(rows, effective))
        # 찾은 말을 스레드까지 들고 간다 — 어느 메일인지는 focus 가, 무엇 때문에
        # 걸렸는지는 hl 이 알려준다(클라이언트가 본문에서 그 말을 칠한다).
        hl = _hl_terms(effective)
        hlq = f"&hl={_q(hl)}" if hl else ""
        low = False
        for r in rows:
            if r["tier"] == 4 and not low:            # tier4 = FTS-OR(하나라도)만 느슨
                out.append("<p class='lowrel'>— 관련 낮음 —</p>")
                low = True
            arrow = "→" if r["is_sent"] else ""
            snip = (f"<div class='snip'>{_snip_html(r['snippet'])}</div>"
                    if r["snippet"] else "")
            out.append(
                f"<div class='item'><a href='/thread/{r['thread_id']}?focus={r['id']}{hlq}'>"
                f"{esc(r['subject'])}</a> <span class='who'>· {arrow} "
                f"{esc(r['sender_name'])}</span> "
                f"<span class='day'>{esc(r['sent_on'][:16])}</span>{snip}</div>")
        if not rows:
            out.append("<p class='empty'>결과 없음</p>")
    return "\n".join(out)


def _review_button_forms(day: str | None = None) -> str:
    # 결정론 데일리 리뷰는 이제 버튼 없이 lazy-on-view 로 자동 생성(_maybe_auto_review).
    # 남은 버튼은 'AI 회고' 하나 — 보고 있는 날짜의 리뷰에 AI 계층을 얹는다.
    # 과거 날짜면 run_ai_layer 가 그 날짜 작업(요약·수확·디제스트·하루 요약)만 실행.
    dt = f"<input type='hidden' name='date' value='{esc(day)}'>" if day else ""
    return ("<form method='post' action='/review'><input type='hidden' name='ai' value='1'>"
            f"{dt}<button class='aibtn ghost'>AI 회고</button></form>")


_DONE_KINDS = (("promise", "내 약속"), ("stalled", "오래 멈춘 스레드"),
               ("deadline", "기한"))
# 접기 목록의 상한. 기본 30 이면 한 종류를 31건 넘게 접었을 때 오래된 것이
# 화면에서 계속 빠지면서 **되돌릴 방법이 없어진다**(필터는 무제한이었다).
DONE_FOLD_LIMIT = 200


def _cards(cfg) -> bool:
    """리포트를 절 카드로 그릴지 — 벤토 스킨에서만."""
    return _skin_ok(cfg.opt("web", "skin", default="classic")) == "bento"


def _done_set(store) -> set:
    """'처리함'으로 접힌 `종류:키` 집합 — 이미 저장된 리포트에서 그 줄을 빼는 데 쓴다."""
    if store is None:
        return set()
    return {f"{kind}:{key}" for kind, _ in _DONE_KINDS
            for key in store.report_done_keys(kind)}


def _done_fold(store, back: str) -> str:
    """'처리함'으로 접은 항목 — 되돌리기.

    접는 것은 되돌릴 수 있어야 한다. 접힌 채로 잊히면 '사라졌다'와 구별되지 않고,
    잘못 눌렀을 때 되살릴 방법이 없다. 리포트 맨 아래 접힘으로 둔다."""
    if store is None:
        return ""
    rows = []
    for kind, label in _DONE_KINDS:
        for r in store.report_done_list(kind, DONE_FOLD_LIMIT):
            # 저장된 label 에 이미 [#스레드] 가 들어 있다 — 여기서 또 붙이지 않는다
            rows.append(
                "<li><span class='dim'>" + esc(label) + "</span> "
                + esc(r["label"] or (f"[#{r['thread_id']}]" if r["thread_id"]
                                     else "(내용 없음)"))
                + f" <span class='dim'>· {esc(r['done_at'][:10])}</span>"
                "<form class='donebtn' method='post' action='/report/undo'>"
                f"<input type='hidden' name='kind' value='{esc(kind)}'>"
                f"<input type='hidden' name='key' value='{esc(r['key_hash'])}'>"
                f"<input type='hidden' name='back' value='{esc(back)}'>"
                "<button>되돌리기</button></form></li>")
    if not rows:
        return ""
    return ("<details class='doneundo'><summary class='dim'>"
            f"처리함으로 접은 항목 ({len(rows)})</summary>"
            "<ul>" + "".join(rows) + "</ul></details>")


def render_daily(cfg, day: str, today: str | None = None, store=None) -> str:
    md = load_daily(cfg, day)
    # 날짜 이동 ◀ ▶ — 미래로는 오늘까지만
    nav = ""
    try:
        d = date.fromisoformat(day)
        prev_d = (d - timedelta(days=1)).isoformat()
        next_d = (d + timedelta(days=1)).isoformat()
        parts = [f"<a href='/records?tab=daily&date={prev_d}'>◀ {prev_d}</a>"]
        if today is None or next_d <= today:
            parts.append(f"<a href='/records?tab=daily&date={next_d}'>{next_d} ▶</a>")
        nav = "<p class='dim'>" + " · ".join(parts) + "</p>"
    except ValueError:
        pass
    out = [f"<h1>일간 회고 · {esc(day)}</h1>", nav,
           "<div class='actions'>" + _review_button_forms(day) + "</div>"]
    if md is None:
        out.append("<p class='empty'>해당 날짜에 저장된 리뷰가 없습니다.</p>")
    else:
        back = f"/records?tab=daily&date={_q(day)}"
        out.append(_md_to_html(md, back, _done_set(store), cards=_cards(cfg)))
        out.append(_done_fold(store, back))
    return "\n".join(out)


# ────────────────── 기억 (데일리·장기기억 — 주간/분기는 Phase 2)
# 메뉴명(2026-07-17 사용자 확정): 기억(구 기록) — '기록'은 앱의 모든 것(메일·스레드·
# 노트)에 해당해 변별력이 없었다. 하루 단위 기억(데일리) ↔ 영구 기억(장기기억)으로
# 아래 용어 체계와 한 묶음. 경로 /records 는 유지(URL↔표시명 불일치는 무해).
# 용어(2026-07-12 사용자 확정): 장기기억(구 결정 원장) · 반영 대기(구 검토 대기) ·
# 반영/유보(구 확정/반려). DB status 값(candidate/confirmed/rejected)은 그대로 —
# 화면 용어만. AI 는 '반영문 초안'(자기완결 한 문장)을 제안하고 사람이 반영한다.

def _decision_row(r, review_mode: bool = False, flip: str = "") -> str:
    """장기기억 항목 한 건.

    review_mode(반영 대기)면 반영/유보/수정 버튼을, flip 이면 상태 전환 버튼
    하나를 붙인다 — 'reject'(반영 목록에서 유보로) / 'confirm'(유보에서 복원).
    """
    who = f" <span class='who'>· {esc(r['decider'])}</span>" if r["decider"] else ""
    day = f" <span class='day'>{esc(r['decided_on'])}</span>" if r["decided_on"] else ""
    why = (f"<span class='snip'>근거: {esc(r['rationale'])}</span>"
           if r["rationale"] else "")
    quote = ""
    if r["quote"]:
        quote = ("<details><summary class='dim'>원문 인용</summary>"
                 f"<p class='dim'>「{esc(r['quote'])}」</p></details>")
    ctl = ""
    if review_mode:
        did = r["id"]
        ctl = (
            "<div class='decbtns'>"
            f"<form method='post' action='/decision/{did}/confirm'>"
            "<button class='btn-primary'>반영</button></form>"
            f"<form method='post' action='/decision/{did}/reject'>"
            "<button class='btn-caution'>유보</button></form>"
            "<details class='decedit'><summary class='dim'>수정 후 반영</summary>"
            f"<form method='post' action='/decision/{did}/amend'>"
            f"<input type='text' name='title' value='{esc(r['title'])}'>"
            f"<input type='text' name='rationale' value='{esc(r['rationale'])}' "
            "placeholder='근거'>"
            "<button class='btn-primary'>반영</button></form></details>"
            "</div>")
    elif flip == "reject":
        ctl = ("<div class='decbtns'>"
               f"<form method='post' action='/decision/{r['id']}/reject'>"
               "<button class='btn-caution' title='장기기억에서 빼서 유보로'>유보</button>"
               "</form></div>")
    elif flip == "confirm":
        ctl = ("<div class='decbtns'>"
               f"<form method='post' action='/decision/{r['id']}/confirm'>"
               "<button class='btn-primary' title='다시 장기기억에 반영'>반영</button>"
               "</form></div>")
    return (f"<div class='item'>"
            f"<a href='/thread/{r['thread_id']}'>#{r['thread_id']}</a> "
            f"<b>{esc(r['title'])}</b>{who}{day}{why}{quote}{ctl}</div>")


def render_decisions(store, qs) -> str:
    """장기기억 — 반영 대기(AI 초안 제안, 사람이 반영) + 반영된 결정 목록."""
    st = (qs.get("st") or ["confirmed"])[0]
    if st not in ("confirmed", "rejected"):
        st = "confirmed"
    q = (qs.get("q") or [""])[0].strip()
    counts = store.decision_counts()
    out = ["<h1>장기기억</h1>",
           "<p class='dim'>메일에서 건진 결정의 영구 기억 — 'AI 회고'가 "
           "반영문 초안을 제안하고, 반영/유보는 사람이 정합니다. 반영된 결정은 "
           "분석(질문·브리핑)의 장기기억 문맥으로 주입됩니다.</p>"]
    cands = store.decisions(status="candidate")
    if cands:
        out.append(f"<h2>반영 대기 ({len(cands)})</h2>")
        out.extend(_decision_row(r, review_mode=True) for r in cands)
    # 반영/유보 필터 + 검색
    tabs = []
    for key, label in (("confirmed", f"반영 {counts.get('confirmed', 0)}"),
                       ("rejected", f"유보 {counts.get('rejected', 0)}")):
        if key == st:
            tabs.append(f"<b>{esc(label)}</b>")
        else:
            tabs.append(f"<a href='/records?tab=decisions&st={key}'>{esc(label)}</a>")
    out.append("<div class='listtabs'><span class='ltabs'>"
               + " · ".join(tabs) + "</span></div>")
    out.append("<form class='search' method='get' action='/records'>"
               "<input type='hidden' name='tab' value='decisions'>"
               f"<input type='hidden' name='st' value='{esc(st)}'>"
               f"<input type='text' name='q' value='{esc(q)}' "
               "placeholder='결정·근거·결정자 검색'> "
               "<button class='btn-primary'>검색</button></form>")
    rows = store.decisions(status=st, q=q)
    if rows:
        # 반영 목록엔 '유보'(빼기), 유보 목록엔 '반영'(복원) — 오클릭 상호 복구 가능
        flip = "reject" if st == "confirmed" else "confirm"
        out.extend(_decision_row(r, flip=flip) for r in rows)
    else:
        out.append("<p class='empty'>해당 항목 없음</p>")
    return "\n".join(out)


_STALL_SECS = 30      # 이 시간 무수신이면 '응답 대기' 경고로 바꾼다 (2d)


def _job_stream_event(job: dict, lock):
    """ai_run 스트리밍 이벤트 → 잡 상태 반영 콜백(주간 보고·질문하기 공용).

    model 이벤트는 콜 하나의 시작 신호라 수신 카운터를 리셋한다 — 이전 콜의
    수신량이 다음 콜에 이월되면 '받고 있다'는 신호가 거짓이 된다. 단 tail
    (초안 미리보기 재료)은 잡 시작에만 리셋한다: thinking 이 긴 모델은 다음
    콜 사고 중에 미리보기가 사라져 화면이 다시 '죽은 척'하기 때문이다."""
    def on_event(info: dict) -> None:
        with lock:
            if not job["running"]:
                return
            job["last_ev"] = time.time()
            ev = info.get("ev")
            if ev == "model":
                job.update(model=_plain(str(info.get("model") or "")),
                           phase="", recv=0, retry="", failed="")
            elif ev == "phase":
                job["phase"] = str(info.get("phase") or "")
                job["retry"] = ""
            elif ev == "delta":
                job["recv"] += int(info.get("bytes") or 0)
                text = info.get("text") or ""
                if text:
                    job["tail"] = (job["tail"] + text)[-800:]
            elif ev == "retry":
                job["retry"] = (f"호출 실패 — 재시도 {info.get('attempt')}"
                                f"/{info.get('total')} ({info.get('wait')}초 뒤)")
                job["failed"] = ""     # 새 시도가 도는 중 — 직전 실패 안내는 낡음
                job["fatal"] = False
            elif ev == "failed":       # 재시도 소진 — 파이프라인은 계속될 수 있다
                job["failed"] = str(info.get("error") or "")[:160]
                # fatal=True(인증 만료 등)는 이어지지 않는다 — 문구가 달라야
                # 한다. 안내 옆에 "이어서 진행"이 붙으면 서로를 부정한다.
                job["fatal"] = bool(info.get("fatal"))
                job["retry"] = ""
    return on_event


# 제어문자 제거 — 백엔드 stderr 에는 ANSI 색상 escape(\x1b[31m…)나 바이너리
# 쓰레기가 섞일 수 있고, esc() 는 <>&\'" 만 막지 제어문자는 그대로 통과시킨다.
# 이 저장소는 C1 제어문자가 화면에 두부(□)로 뜬 전력이 있다(2026-07-26).
_CTRL_RX = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _plain(text: str) -> str:
    """화면에 싣기 전 마지막 관문 — 제어문자를 지운다."""
    return _CTRL_RX.sub("", text or "")


def _job_live_line(st: dict) -> str:
    """수신 상황 한 줄(평문 — esc 는 렌더 쪽).

    우선순위: 재시도 > 직전 실패 > 무수신 > 단계. 모델은 여기 없다 — 전용
    배지(#wk-model/#ask-model)가 잡 끝까지 유지한다. 스트리밍 이벤트가 아직
    없으면 빈 문자열 — claude CLI 외 백엔드는 이벤트가 안 오므로 이 줄 없이
    기존 stage 표시만 남는다(관측이 안 되는 걸 아는 척 안 함)."""
    # 중지 안내는 스트리밍 이벤트 유무보다 먼저다 — 이벤트가 0건인 백엔드가
    # 바로 '눌러도 안 멈추는 것처럼 보이는' 경우이기 때문이다.
    ev = st.get("cancel")
    if ev is not None and ev.is_set():
        return ("중지하는 중…" if st.get("stream")
                else "중지 요청됨 — 진행 중인 호출이 끝나면 멈춥니다")
    if not st.get("last_ev"):
        return ""
    if st.get("retry"):
        return _plain(st["retry"])
    if st.get("failed"):
        if st.get("fatal"):                # 인증 만료 등 — 여기서 멈춘다
            return _plain(f"중단됨 — {st['failed']}")
        # weekly 는 실패한 콜을 삼키고 다음 콜로 넘어간다 — 그 사실을 화면에
        # 남기지 않으면 실패 구간 내내 '살아 있는데 조용한' 화면이 된다.
        return _plain(f"직전 호출 실패 — 이어서 진행 ({st['failed']})")
    idle = int(time.time() - st["last_ev"])
    if idle >= _STALL_SECS:
        return f"응답 대기 — {idle}초째 무수신"
    recv = st.get("recv") or 0
    # 수신 0 은 정보가 아니다 — 사고 구간은 모델·백엔드에 따라 내용이 아예 안 오고
    # (실기기 관찰: opus 경로에서 항상 0), 작성 구간도 전환 직후엔 0이다.
    # 숫자는 실제로 받았을 때만 싣는다. 단위는 송신 줄과 같은 자를 쓴다.
    tail = f" · 수신 {review.fmt_bytes(recv)}" if recv else ""
    if st.get("phase") == "writing":
        return "작성 중" + tail
    if st.get("phase") == "thinking":
        return "모델 사고 중" + tail
    return "모델 응답 대기 중"


# 응답은 JSON 스트림이라 원문 꼬리를 그대로 보이면 중괄호·키가 태반이다.
# 서술 값 키의 마지막 문자열 값만 골라 보여준다. 닫는 따옴표를 요구하지 않아
# 아직 쓰는 중인 문자열도 잡힌다.
_PREVIEW_RE = re.compile(r'"(?:text|answer|summary|why|name)"\s*:\s*"((?:[^"\\]|\\.)*)')


def _preview_text(tail: str) -> str:
    """작성 중 텍스트 꼬리 → 사람이 읽을 조각(끝 120자). 못 찾으면 빈 문자열."""
    matches = _PREVIEW_RE.findall(tail or "")
    if not matches:
        return ""
    frag = (matches[-1].replace("\\n", " ").replace('\\"', '"')
            .replace("\\\\", "\\"))
    return re.sub(r"\s+", " ", _plain(frag)).strip()[-120:]


def _job_preview(st: dict) -> str:
    """초안 미리보기 문구 — phase 무관, tail 만 있으면 보여준다(sticky)."""
    frag = _preview_text(st.get("tail") or "")
    return f"작성 중 초안(검증 전) — …{frag}" if frag else ""


def _arm_job_backend(job: dict, lock, cfg, backend_name) -> None:
    """잡이 쓸 백엔드를 보고 (1) 무수신 워치독을 무장하고 (2) 스트리밍 여부를 심는다.

    워치독은 claude 백엔드만 — 스트리밍 이벤트가 애초에 없는 백엔드(opencode
    등)에 잡 시작 시각을 심으면 정상 진행 중에도 '무수신' 오탐이 난다.
    job["stream"] 은 중지 버튼의 안내 문구가 갈라지는 근거다(_cancel_hint):
    스트리밍이면 진행 중 호출을 즉시 끊지만, 블로킹 경로는 콜 경계에서만 멈춘다."""
    try:
        cmd = cfg.ai_cmd(backend_name)
    except (SystemExit, Exception):
        # 백엔드 미설정(SystemExit — Exception 하위가 아니라 따로 적어야 한다)
        # 이든 설정 파손이든, **표시용 판정이 잡을 죽여선 안 된다** — 잡 스레드가
        # 여기서 죽으면 running=True 인 채 슬롯이 영구 점유돼 서버를 다시 띄울
        # 때까지 그 기능이 막힌다.
        return
    stream = review._is_claude_cmd(cmd)
    with lock:
        if job["running"]:
            job["stream"] = stream
            if stream:
                job["last_ev"] = time.time()


def _cancel_hint(stream: bool) -> str:
    """중지 버튼이 실제로 무엇을 하는지 — 거짓 기대를 만들지 않는다."""
    return ("중지하면 진행 중인 호출을 즉시 끊습니다." if stream
            else "중지는 진행 중인 AI 호출이 끝난 뒤에 적용됩니다.")


def _job_wait_card(prefix: str, title: str, *, stage: str = "",
                   live: str = "", preview: str = "", model: str = "",
                   hint: str = "", step: int = 0, total: int = 0,
                   cancel_action: str = "", cancel_extra: str = "",
                   extra: str = "", started: float = 0.0) -> str:
    """백그라운드 잡 공용 대기 카드 — 모든 잡이 같은 껍데기를 쓴다.

    prefix 는 잡 식별자(rv·ai·wk·ask·dz·sy)이자 id 접두다. id 붙은 텍스트
    슬롯({p}-model/stage/live/preview/elapsed)은 app.js patchText 가
    textContent 만 갈아끼우므로 **자식 요소 없는 순수 텍스트 노드**여야 한다.
    빈 슬롯은 CSS `.waitslot:empty` 가 숨겨 카드가 자연 축소된다 — 관측되지
    않는 것(스트리밍 없는 백엔드의 수신·모델)을 아는 척하지 않는 계약.

    step/total 이 오면 결정론 진행바(폭 = step/total)를, 없으면 인디터미닛
    막대를 그린다. cancel_action 이 비면 중지 버튼째 생략한다 — 끊을 대상이
    없는 잡(동기화·비AI 자동 회고)에 죽은 버튼을 두지 않기 위해서다.
    extra 는 **이스케이프하지 않는 HTML 슬롯**(AI 검색 잠정 결과)이라 호출부가
    이미 esc 한 마크업만 넘긴다. 카드 폭(560px)에 눌리지 않게 카드 밖 형제로
    둔다. 러닝 마커(data-*-running)는 잡별 속성이 달라 호출부가 카드 밖에 둔다.
    """
    if total > 0:
        pct = max(4, min(100, round(step * 100 / total)))
        bar = f"<div class='rvfill' id='{prefix}-fill' style='width:{pct}%'></div>"
    else:
        bar = f"<div class='rvfill indet' id='{prefix}-fill'></div>"
    cancel = ""
    if cancel_action:
        cancel = (f"<form method='post' action='{cancel_action}'>{cancel_extra}"
                  "<button class='aibtn ghost compact'>중지</button></form>")
    return (
        "<div class='waitcard'>"
        "<div class='waithead'><div class='spin'></div>"
        f"<div class='aiwaitmsg'>{esc(title)}</div>"
        f"<span class='askbadge thin waitslot' id='{prefix}-model'>"
        f"{esc(model)}</span></div>"
        f"<div class='rvbar'>{bar}</div>"
        f"<p class='aiwaitsub waitslot' id='{prefix}-stage'>{esc(stage)}</p>"
        f"<p class='aiwaitsub waitslot' id='{prefix}-live'>{esc(live)}</p>"
        f"<blockquote class='waitdraft draft waitslot' id='{prefix}-preview'>"
        f"{esc(preview)}</blockquote>"
        "<div class='waitmeta'>"
        f"<span class='aiwaittime'><span id='{prefix}-elapsed'"
        f"{f' data-since=\'{int(started)}\'' if started else ''}>0</span>초 경과"
        f" · {esc(hint)}</span>{cancel}"
        "</div></div>"
        f"<div id='{prefix}-extra'>{extra}</div>")


def _weekly_progress(msg: str) -> None:
    """weekly.run_ai_layer 진행 콜백 — 잡 상태에 실어 폴링에 노출."""
    with _weekly_lock:
        if _weekly_job["running"]:
            _weekly_job["stage"] = msg


def _run_weekly_job(cfg, weeks: int, cancel) -> None:
    from . import weekly as weekly_mod
    _arm_job_backend(_weekly_job, _weekly_lock, cfg,
                     cfg.opt("ai", "weekly", default=None)
                     or cfg.ai_summary_backend)   # weekly.run_ai_layer 와 동일식
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            content, det = weekly_mod.generate(
                store, cfg, weeks=weeks, ai=True, progress=_weekly_progress,
                on_event=_job_stream_event(_weekly_job, _weekly_lock),
                cancel=cancel)
            # AI 가 중단됐고(인증 만료 등) 그 기간 보고서가 이미 있으면 덮지
            # 않는다 — AI 서술이 든 보고를 뼈대로 갈아 끼우는 건 손실이다.
            kept = bool(det.get("ai_error")) and weekly_mod.report_path(
                cfg, det).exists()
            if not kept:
                weekly_mod.write(cfg, det, content)
            end, ai_err = det["end"], det.get("ai_error") or ""
        finally:
            store.close()
        with _weekly_lock:
            _weekly_job.update(
                running=False, stage="error" if ai_err else "done", date=end,
                error=(ai_err + (" — 기존 보고서를 유지했습니다" if kept else ""))
                if ai_err else "")
    except review.AICancelled:                   # 중지는 실패가 아니다 — 조용히 접는다
        with _weekly_lock:
            _weekly_job.update(running=False, stage="cancelled")
    except review.AIAuthError as e:              # generate 가 삼키므로 보통은
        # 도달하지 않는다 — 다른 경로로 샜을 때의 방어선(repr 노출 방지).
        with _weekly_lock:
            _weekly_job.update(running=False, stage="error",
                               error=str(e).splitlines()[0][:160])
    except Exception as e:                       # 잡이 조용히 죽지 않게
        # 화면이 이 문자열을 그대로 보여준다 — repr 대신 사람이 읽을 문장
        with _weekly_lock:
            _weekly_job.update(
                running=False, stage="error",
                error=("주간 보고를 만들지 못했습니다 — "
                       + (" ".join(str(e).split())[:100] or type(e).__name__)))


def _start_weekly(cfg, weeks: int) -> bool:
    cancel = _job_start(_weekly_job, _weekly_lock, stage="준비 중…",
                        weeks=weeks, date="")
    if cancel is None:
        return False
    threading.Thread(target=_run_weekly_job, args=(cfg, weeks, cancel),
                     daemon=True).start()
    return True


def weekly_files(cfg) -> list[str]:
    """저장된 주간 보고 날짜(최신순).

    날짜가 아닌 stem 은 버린다 — 금고에 섞인 `메모.md` 같은 파일은 문자열 정렬에서
    날짜보다 뒤라 '최신 차수'가 돼 화면 기본값을 차지했다(2026-08-01 적대 검토).
    weekly.report_rounds 도 같은 가드를 쓴다."""
    d = cfg.vault / "weekly"
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.md"):
        try:
            date.fromisoformat(p.stem)
        except ValueError:
            continue
        out.append(p.stem)
    return sorted(out, reverse=True)


def render_weekly(cfg, qs, store=None) -> str:
    """기억 › 주간 — 저장된 보고 렌더 + 기간 선택·생성 버튼 + 지난 보고 목록."""
    from . import weekly as weekly_mod

    files = weekly_files(cfg)
    want = (qs.get("date") or [""])[0]
    cur = want if want in files else (files[0] if files else "")
    try:
        weeks = max(1, min(12, int((qs.get("weeks") or [
            str(weekly_mod.WINDOW_WEEKS)])[0])))
    except ValueError:
        weeks = weekly_mod.WINDOW_WEEKS

    opts = "".join(
        f"<option value='{w}'{' selected' if w == weeks else ''}>{w}주</option>"
        for w in (1, 2, 4))
    out = ["<h1>주간 보고</h1>",
           "<div class='actions'>"
           "<form method='post' action='/weekly'>"
           f"<select name='weeks'>{opts}</select> "
           "<button class='aibtn'>보고 만들기</button></form></div>",
           "<p class='dim'>내가 관여한 사안(내 발신·나 지목·직접 수신)을 토픽으로 묶어 "
           "진행·이슈·향후로 정리합니다. 기간 내 원문을 소배치로 읽고 근거를 재검증합니다. "
           f"AI 최대 {weekly_mod.MAX_AI_CALLS}콜.</p>"]

    if cur:
        # 인접 차수 이동 — 일간(render_daily)은 날짜 산술로 ◀▶ 를 만들지만 주간은
        # 생성한 날만 파일이 있어 간격이 불규칙하다. 목록의 앞뒤 원소를 가리킨다.
        i = files.index(cur)
        nav = []
        if i + 1 < len(files):                       # files 는 최신순
            nav.append(f"<a href='/records?tab=weekly&date={_q(files[i+1])}'>"
                       f"◀ {esc(files[i+1])}</a>")
        nav.append(f"<b>{esc(cur)}</b>")
        if i > 0:
            nav.append(f"<a href='/records?tab=weekly&date={_q(files[i-1])}'>"
                       f"{esc(files[i-1])} ▶</a>")
        out.append("<p class='dim'>" + " · ".join(nav) + "</p>")   # 일간과 같은 스타일
        path = cfg.vault / "weekly" / f"{cur}.md"
        try:
            back = f"/records?tab=weekly&date={_q(cur)}"
            out.append(_md_to_html(path.read_text(encoding="utf-8"), back,
                                   _done_set(store), cards=_cards(cfg)))
            out.append(_done_fold(store, back))
        except OSError:
            out.append("<p class='empty'>보고 파일을 읽지 못했습니다.</p>")
    else:
        out.append("<p class='empty'>저장된 주간 보고가 없습니다. "
                   "위에서 기간을 고르고 <b>보고 만들기</b>를 눌러 주세요.</p>")

    others = [d for d in files if d != cur][:24]
    if others:
        links = " · ".join(
            f"<a href='/records?tab=weekly&date={_q(d)}'>{esc(d)}</a>"
            for d in others)
        out.append(f"<p class='dim'>지난 보고: {links}</p>")
    return "\n".join(out)


def render_weekly_status(cfg, store=None) -> tuple:
    """(inner, running) — 폴링·전체페이지 공용. 완료면 그 보고를, 에러면 안내.

    store 는 '처리함' 반영에 쓴다. 여기가 **보고 만들기 직후 랜딩 화면**이라,
    안 넘기면 접은 항목이 그대로 보이고 되돌리기 접기도 없다."""
    with _weekly_lock:
        st = dict(_weekly_job)
    if st["running"]:
        return ("<div data-weekly-running='1' hidden></div>"
                "<h1>주간 보고</h1>"
                + _job_wait_card(
                    "wk", "AI가 주간 보고를 쓰는 중",
                    stage=st.get("stage") or "준비 중…",
                    live=_job_live_line(st), preview=_job_preview(st),
                    model=st.get("model") or "",
                    hint="토픽을 묶고 사안별로 진행·이슈·향후를 씁니다 — "
                         "완료되면 자동 전환. "
                         + _cancel_hint(st.get("stream", False)),
                    cancel_action="/weekly/cancel",
                    started=st.get("started") or 0.0), True)
    if st["stage"] == "cancelled":
        return ("<h1>주간 보고</h1>"
                "<div class='aifail'>중지했습니다 — 보고는 만들어지지 않았습니다."
                "</div>" + render_weekly(cfg, {}, store), False)
    if st["stage"] == "error":
        return ("<h1>주간 보고</h1>"
                f"<div class='aifail'>보고를 만들지 못했습니다 — {esc(st.get('error', ''))}"
                "</div>" + render_weekly(cfg, {}, store), False)
    if st["stage"] == "done" and st.get("date"):
        return render_weekly(cfg, {"date": [st["date"]]}, store), False
    return render_weekly(cfg, {}, store), False


def _ask_progress(msg: str) -> None:
    with _ask_lock:
        if _ask_job["running"]:
            _ask_job["stage"] = msg


def _run_ask_job(cfg, question: str, parent_id, person: str = "",
                 cancel=None, mail_id: int | None = None,
                 use_cache: bool = True) -> None:
    from . import ask as ask_mod
    _arm_job_backend(_ask_job, _ask_lock, cfg, cfg.ai_ask_backend)
    on_event = _job_stream_event(_ask_job, _ask_lock)
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            # use_cache=False('다시 조사/다시 분석') 는 엔진 캐시까지 뚫어야
            # 한다 — 웹 캐시만 건너뛰면 같은 키(basis 불변)로 엔진이 즉시
            # 히트해 fresh 가 무동작이 된다(2026-07-30 리뷰 지적).
            if mail_id:                      # 메일 분석 — 그 메일+스레드에서 출발
                res = ask_mod.analyze_mail(store, cfg, mail_id,
                                           use_cache=use_cache,
                                           progress=_ask_progress,
                                           on_event=on_event, cancel=cancel)
            elif person:                     # 인물 브리핑 — 같은 엔진, 범위만 고정
                res = ask_mod.brief(store, cfg, person,
                                    name=store.person_name(person) or "",
                                    use_cache=use_cache,
                                    progress=_ask_progress,
                                    on_event=on_event, cancel=cancel)
            else:
                res = ask_mod.ask(store, cfg, question, use_cache=use_cache,
                                  progress=_ask_progress, parent_id=parent_id,
                                  on_event=on_event, cancel=cancel)
        finally:
            store.close()
        with _ask_lock:
            _ask_job.update(running=False, stage="done", result=res)
    except review.AICancelled:                   # 중지 — 검색 폴백도 띄우지 않는다
        with _ask_lock:
            _ask_job.update(running=False, stage="cancelled")
    except (review.AIError, review.AIAuthError,
            SystemExit) as e:                    # CLI 부재·인증 만료·설정 오류 → 검색 폴백
        with _ask_lock:
            _ask_job.update(running=False, stage="error", error=str(e)[:140])
    except Exception as e:
        with _ask_lock:
            _ask_job.update(
                running=False, stage="error",
                error=("조사에 실패했습니다 — "
                       + (" ".join(str(e).split())[:100] or type(e).__name__)))


def _start_ask(cfg, question: str, parent_id, person: str = "",
               mail_id: int | None = None, use_cache: bool = True) -> str | None:
    token = secrets.token_urlsafe(12)
    cancel = _job_start(_ask_job, _ask_lock, stage="조사 준비 중…",
                        question=question, parent=parent_id, person=person,
                        mail=mail_id, token=token, result=None)
    if cancel is None:
        return None
    threading.Thread(target=_run_ask_job,
                     args=(cfg, question, parent_id, person, cancel, mail_id,
                              use_cache),
                     daemon=True).start()
    return token


# 요약 갱신이 끝났는데 카드가 안 바뀌는 경우들 — 조용히 넘어가면 사용자는
# 버튼이 고장 난 줄 안다. 상태별로 무엇이 일어났는지 한 줄로 말한다.
_NOTE_TTL = 300       # 잡 결과 안내는 5분만 — 다음 방문까지 남으면 유령 배너다

_DOSSIER_NOTE = {
    "empty": "요약을 만들지 못했습니다 — 근거로 쓸 수 있는 인용이 없었습니다.",
    "no_material": "이 사람이 직접 쓴 본문이 없어 요약할 재료가 없습니다.",
    "no_backend": "AI 백엔드가 설정되지 않았습니다 — config.toml 의 [ai] 확인.",
    "error": "AI 호출에 실패했습니다 — 잠시 후 다시 눌러 주세요.",
    "cancelled": "중지했습니다 — 기존 요약은 그대로입니다.",
}


def _dossier_progress(msg: str) -> None:
    with _dossier_lock:
        if _dossier_job["running"]:
            _dossier_job["stage"] = msg


def _dossier_wait_html(st: dict) -> str:
    """인물 요약 대기 — 마커 + 공용 대기 카드(prefix dz)."""
    return (f"<div data-dossier-running='1' "
            f"data-dossier-addr='{esc(st.get('addr') or '')}' hidden></div>"
            + _job_wait_card(
                "dz", "AI가 인물 요약을 쓰는 중",
                stage=st.get("stage") or "재료를 모으는 중…",
                live=_job_live_line(st), preview=_job_preview(st),
                model=st.get("model") or "",
                hint="이 사람이 직접 쓴 본문에서 인용을 뽑아 검증합니다. "
                     + _cancel_hint(st.get("stream", False)),
                cancel_action="/people/dossier/cancel",
                cancel_extra=f"<input type='hidden' name='addr' "
                             f"value='{esc(st.get('addr') or '')}'>",
                started=st.get("started") or 0.0))


def _run_dossier_job(cfg, addr: str, name: str, cancel) -> None:
    from . import distill
    _arm_job_backend(_dossier_job, _dossier_lock, cfg, cfg.ai_summary_backend)
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            _dossier_progress("본문 읽고 인용 뽑는 중…")
            res = distill.refresh_person_dossier(
                store, cfg, addr, name=name,
                # 배치 시절과 같은 요약 백엔드 — 안 넘기면 [ai] default 로
                # 나가고, 위 _arm_job_backend 가 심은 stream 판정과도 어긋나
                # '무수신' 오탐과 '즉시 중지' 거짓 안내가 생긴다.
                backend=cfg.ai_summary_backend,
                on_event=_job_stream_event(_dossier_job, _dossier_lock),
                cancel=cancel)
        finally:
            store.close()
        with _dossier_lock:
            _dossier_job.update(running=False, stage=res.status,
                                done_at=time.time())
    except review.AICancelled:
        with _dossier_lock:
            _dossier_job.update(running=False, stage="cancelled",
                                done_at=time.time())
    except review.AIAuthError as e:               # 인증 만료 — 문구를 그대로 보인다
        with _dossier_lock:
            _dossier_job.update(running=False, stage="error",
                                error=str(e).splitlines()[0][:160],
                                done_at=time.time())
    except Exception as e:                        # 잡이 조용히 죽지 않게
        # 화면(render_dossier)이 이 문자열을 그대로 보여주므로 repr 을 넣지
        # 않는다 — "OperationalError('database is locked')" 는 사용자에게
        # 아무 의미가 없다. 사람이 읽을 문장 + 원인 한 줄만.
        with _dossier_lock:
            _dossier_job.update(
                running=False, stage="error", done_at=time.time(),
                error=("AI 요약을 만들지 못했습니다 — "
                       + (" ".join(str(e).split())[:100] or type(e).__name__)))


def _start_dossier(cfg, addr: str, name: str = "") -> bool:
    cancel = _job_start(_dossier_job, _dossier_lock, stage="준비 중…",
                        addr=addr, name=name)
    if cancel is None:
        return False
    threading.Thread(target=_run_dossier_job, args=(cfg, addr, name, cancel),
                     daemon=True).start()
    return True


def render_dossier_status(store, cfg, addr: str) -> tuple:
    """(inner, running) — 진행 중엔 마커+카드만(가볍게), 끝나면 인물 화면 전체.

    폴링마다 인물 화면을 다시 그리면 관계 수치·업무 어휘 지도가 1.5초마다
    재계산된다. 그 비용을 진행 중에는 내지 않는다."""
    with _dossier_lock:
        st = dict(_dossier_job)
    if st["running"] and st["addr"] == (addr or "").strip().lower():
        return _dossier_wait_html(st), True
    return render_dossier(store, cfg, addr), False


_ASK_BADGE = {"확인됨": "ok", "상충함": "warn", "근거 부족": "thin"}


def _ask_ref(mid: int, tid: int) -> str:
    """근거 칩 — 그 메일로 바로 내려간다(스레드 열고 해당 메일에 포커스)."""
    return f"<a class='askref' href='/thread/{tid}?focus={mid}'>#{mid}</a>"


def _ask_quote(c: dict) -> str:
    """인용 + 원문에서 떼어 온 앞뒤 문맥. 문맥은 흐리게 — 무엇이 모델이 지목한
    근거인지 계속 구분된다. 옛 답(context 없음)은 종전과 똑같이 그려진다."""
    q = esc(c.get("quote", ""))
    ctx = c.get("context") or {}
    pre, post = esc(ctx.get("pre", "")), esc(ctx.get("post", ""))
    if not (pre or post):
        return f"<div class='askq'>{q}</div>"
    sep = "" if (post and post[0] in ",.;:)]}") else " "
    return (f"<div class='askq ctx'>"
            + (f"<span class='qctx'>…{pre} </span>" if pre else "")
            + f"<span class='qhit'>{q}</span>"
            + (f"<span class='qctx'>{sep}{post}…</span>" if post else "")
            + "</div>")


def _ask_answer_body(res: dict) -> str:
    """어시스턴트 답 본문 — 상태·답변·근거·상충·다음 확인처·조사 범위(대화 말풍선 안)."""
    st = res.get("state") or ""
    scope_data = res.get("scope") or {}
    legacy = (bool(res.get("cached")) and st in ("확인됨", "상충함")
              and not scope_data.get("semantic_checked"))
    shown_state = "검증 전 답변" if legacy else st
    badge_class = "thin" if legacy else _ASK_BADGE.get(st, "thin")
    out = []
    note = ""
    if res.get("stale"):
        note = f"<span class='dim'> · 이후 새 메일 {res['stale']}통</span>"
    out.append(f"<div class='askstate'><span class='askbadge "
               f"{badge_class}'>{esc(shown_state)}</span>{note}</div>")
    if res.get("headline"):
        out.append(f"<div class='askhead'>{esc(res['headline'])}</div>")
    if res.get("answer"):
        out.append(f"<div class='askans'>{esc(res['answer'])}</div>")

    if res.get("conflicts"):
        conflicts = sorted(
            res["conflicts"],
            key=lambda c: (c.get("sent_on") or "", c.get("mid") or 0),
        )
        out.append("<h3 class='asksec'>부딪히는 근거</h3><div class='askclash'>")
        for i, c in enumerate(conflicts):
            latest = i == len(conflicts) - 1
            win = " win" if latest else ""
            label = esc(c.get("label", ""))
            if latest:
                label += " · 최신 근거"
            out.append(
                f"<div class='askside{win}'>"
                f"<div class='asklabel'>{label} · "
                f"{esc((c.get('sent_on') or '')[:10])}</div>"
                f"<div class='askval'>{esc(c.get('value', ''))}</div>"
                f"<div class='askq'>{esc(c.get('quote', ''))}</div>"
                f"<div class='dim'>{esc(c.get('sender', ''))} "
                f"{_ask_ref(c['mid'], c['thread_id'])}</div></div>")
        out.append("</div>")

    if res.get("claims"):
        # 결론 근거를 먼저 — 무엇이 답의 뿌리이고 무엇이 배경인지 갈라 보인다.
        # role 이 없는 옛 답은 전부 '배경' 으로 떨어져 종전과 같은 한 덩어리가 된다.
        groups = [("결론", "근거 — 결론"), ("근거", "근거 — 이유"), ("배경", "배경")]
        if st == "근거 부족":
            groups = [("결론", "확인한 것"), ("근거", "확인한 것"), ("배경", "확인한 것")]
        done = set()
        for role, title in groups:
            items = [c for c in res["claims"]
                     if (c.get("role") or "배경") == role]
            if not items:
                continue
            if title not in done:
                out.append(f"<h3 class='asksec'>{title}</h3>")
                done.add(title)
            out.append("<div class='askev'>")
            for c in items:
                out.append(f"<div class='askitem'><div>{esc(c.get('text', ''))}</div>"
                           + _ask_quote(c)
                           + f"<div class='dim'>{esc(c.get('sender', ''))} · "
                             f"{esc((c.get('sent_on') or '')[:16])} · "
                             f"{esc(c.get('subject', ''))} "
                             f"{_ask_ref(c['mid'], c['thread_id'])}</div></div>")
            out.append("</div>")

    if res.get("open"):
        out.append("<h3 class='asksec'>열린 것</h3><div class='askev'>")
        for o in res["open"]:
            out.append(f"<div class='askitem'><div>{esc(o.get('text', ''))}</div>"
                       + _ask_quote(o)
                       + f"<div class='dim'>{esc(o.get('sender', ''))} · "
                         f"{esc((o.get('sent_on') or '')[:16])} "
                         f"{_ask_ref(o['mid'], o['thread_id'])}</div></div>")
        out.append("</div>")

    if res.get("leads"):
        out.append("<h3 class='asksec'>여기부터 보면 됩니다</h3><div class='askev'>")
        for ld in res["leads"]:
            out.append(
                f"<div class='askitem'><a href='/thread/{ld['thread_id']}'>"
                f"{esc(ld.get('subject', '') or '(제목 없음)')}</a>"
                f"<div class='dim'>{esc(ld.get('why', ''))}</div></div>")
        out.append("</div>")

    s = scope_data
    # '기준' 줄 — 어디까지 보고 답했나. **모델에게 묻지 않는다**(자기가 뭘 안
    # 봤는지는 코드만 정확히 안다). 옛 답은 span/partial 이 없어 안 나온다.
    basis = [x for x in (s.get("span") or "", f"{s.get('read', 0)}통 정독") if x]
    if s.get("partial"):
        basis.append("일부만 본 스레드 " + " · ".join(s["partial"][:3]))
    out.append(f"<div class='askreach'>기준 · {esc(' · '.join(basis))}</div>")

    scope = ["<details class='askscope'><summary>조사 범위 · "
             f"AI {s.get('calls', 0)}콜</summary><ul>"]
    for q in (s.get("queries") or []):
        scope.append(f"<li>검색 <code>{esc(q)}</code></li>")
    if s.get("counter_checked"):
        scope.append(
            f"<li>변경·취소·최종 근거 확인 {s.get('counter_count', 0)}회</li>")
    if s.get("semantic_checked"):
        scope.append("<li>주장-인용 의미 검증 완료</li>")
    elif res.get("claims") or res.get("conflicts"):
        # 검증기가 응답을 못 준 답(또는 이 검증 도입 전 답) — 줄을 빼버리면 '해당
        # 없음' 으로 읽힌다. 어떤 보증까지 받았는지 명시한다.
        scope.append("<li>주장-인용 의미 검증 안 됨 — 인용 대조만 통과</li>")
    scope.append(f"<li>훑음 {s.get('hits', 0)}건 · 정독 {s.get('read', 0)}통"
                 + (f" · 근거 검증 탈락 {s['dropped']}" if s.get("dropped") else "")
                 + f" · {esc(s.get('backend', ''))}</li>")
    scope.append("</ul></details>")
    out.append("".join(scope))
    return "\n".join(out)


def _ask_turn(res: dict) -> str:
    """대화 한 턴 — 내 질문(말풍선) + 어시스턴트 답. 브리핑은 질문 대신 대상 표기."""
    who = res.get("person") or {}
    if who.get("addr"):
        q = (f"{esc(who.get('name') or who['addr'])} 브리핑 · 최근 "
             f"{int(who.get('months') or 3)}개월")
    else:
        q = esc(res.get("question", ""))
    return (f"<div class='chatq'><div class='bubble'>{q}</div></div>"
            f"<div class='chata'>{_ask_answer_body(res)}</div>")


def _ask_input(follow_id=None, placeholder="메일에 대해 물어보세요") -> str:
    """하단 고정 입력 — follow_id 가 있으면 그 대화에 이어 묻는다."""
    hidden = (f"<input type='hidden' name='follow' value='{int(follow_id)}'>"
              if follow_id else "")
    # 조사 중에는 autofocus 를 빼야 JS-off meta refresh(2초)가 포커스를 계속
    # 빼앗지 않는다. JS-on 에서는 inject 후 hookFocus 가 알아서 포커스를 준다.
    focus = "" if placeholder == "조사 중…" else "autofocus "
    return ("<form class='chatbar' method='post' action='/ask/jobs'>"
            f"{hidden}<input type='text' name='q' required autocomplete='off' "
            f"{focus}placeholder='{esc(placeholder)}'>"
            "<button class='btn-primary'>보내기</button></form>")


def _ask_one_turn(res: dict) -> dict:
    """문답 하나를 1턴 대화록 형태로 — transcript 실패·캐시 미기록의 안전망."""
    return {"turns": [res], "latest_id": res.get("id")}


def render_ask_thread(store, cfg, tr: dict, pending=None, job_token: str = "",
                      live: str = "", preview: str = "",
                      stage: str = "조사 준비 중…", model: str = "",
                      stream: bool = False, started: float = 0.0) -> str:
    """우측 — 한 대화의 대화록(말풍선) + 하단 고정 입력. pending 은 조사 중인 새 질문.

    stage/live/preview/model 은 대기 카드 내용 — 값은 잡 상태를 아는
    render_ask_status 만 채우고, 폴링(app.js)이 textContent 로 갈아끼운다."""
    turns = "\n".join(_ask_turn(t) for t in tr["turns"])
    wait = ""
    if pending is not None:
        wait = (f"<div class='chatq'><div class='bubble'>{esc(pending)}</div></div>"
                f"<div class='chata'><div data-ask-running='1' "
                f"data-ask-job='{esc(job_token)}' hidden></div>"
                + _job_wait_card(
                    "ask", "AI가 메일을 조사하는 중", stage=stage, live=live,
                    preview=preview, model=model,
                    hint="메일을 찾아 읽고 근거를 대조합니다. "
                         + _cancel_hint(stream),
                    cancel_action="/ask/cancel",
                    cancel_extra=f"<input type='hidden' name='job' "
                                 f"value='{esc(job_token)}'>",
                    started=started)
                + "</div>")
    running = pending is not None
    redo = ""
    if not running and tr["turns"]:              # 마지막 답을 새로 조사(캐시 무시)
        last = tr["turns"][-1]
        who = last.get("person") or {}
        hidden = "<input type='hidden' name='fresh' value='1'>"
        if who.get("addr"):
            hidden += (f"<input type='hidden' name='person' "
                       f"value='{esc(who['addr'])}'>")
        else:
            hidden += (f"<input type='hidden' name='q' "
                       f"value='{esc(last.get('question', ''))}'>")
            if last.get("parent_id"):
                hidden += (f"<input type='hidden' name='follow' "
                           f"value='{int(last['parent_id'])}'>")
        redo = ("<form class='dim chatredo' method='post' action='/ask/jobs'>"
                f"{hidden}<button class='aibtn ghost compact'>다시 조사</button>"
                " — 마지막 질문을 캐시 없이 새로 조사합니다</form>")
    return (f"<div class='chat'>{turns}{wait}</div>{redo}"
            + _ask_input(None if running else tr.get("latest_id"),
                         "조사 중…" if running else "이어서 묻기"))


def _ask_basis_footer(store, cfg) -> str:
    """좌측 하단 기준선 — 질문하기 전에 알아야 할 두 가지.

    (1) 어느 시점까지의 메일이 반영됐나 (2) 새 분석이 어느 백엔드를 쓰나.
    답의 신뢰도·비용·개인정보 전달 경로를 가늠하는 재료라 탐색 기능이 아니다.
    '새 분석'이라고 못박는 이유: 저장된 옛 답은 그때의 백엔드로 만들어졌고
    그 진실은 각 답의 scope.backend 에 남아 있다.
    """
    if cfg is None:
        return ""
    info = store.basis_info()
    with _sync_lock:
        syncing = _sync_job["running"]
    if syncing:
        when = "동기화 중"
    elif info["checked_at"]:
        # 오늘이면 시:분, 지난 날이면 날짜까지 — '언제까지 확인됐나'가 요점
        stamp = info["checked_at"]
        today = date.today().isoformat()
        when = f"동기화 {esc(stamp[11:16])}" if stamp[:10] == today \
            else f"동기화 {esc(stamp[5:10])}"
    else:
        when = "동기화 기록 없음"
    return ("<div class='askbasis'>"
            f"<div>메일 {info['messages']:,}통 · {when}</div>"
            "<div>새 분석 · <a href='/settings' title='분석 백엔드 설정'>"
            f"{esc(cfg.ai_ask_backend)}</a></div></div>")


def render_ask_list(store, limit: int = 40, cfg=None) -> str:
    """좌측 — 대화 목록. 하나가 '질문→추가질문' 한 덩어리(ChatGPT 사이드바처럼).

    맨 아래에 기준선 footer(메일 통수·동기화 시각·새 분석 백엔드). 떠 있지 않고
    목록 뒤에 붙어 함께 스크롤한다 — 마지막 대화를 가리지 않게."""
    from . import ask as ask_mod

    convs = ask_mod.conversations(store, limit)
    out = ["<div class='asklisthd'><a class='aibtn' href='/ask'>신규 분석</a></div>"]
    if not convs:
        out.append("<p class='empty'>아직 대화가 없습니다.</p>")
        out.append(_ask_basis_footer(store, cfg))
        return "\n".join(out)
    items = []
    for c in convs:
        badge = {"확인됨": "✔", "상충함": "⚠"}.get(c.get("state", ""), "·")
        turns = f" · {c['turns']}턴" if c["turns"] > 1 else ""
        # 그 답이 못 본 메일 수 — 없으면 줄이지 않는다(0을 쓰면 모든 행이 시끄럽다).
        # 대화록의 '이후 새 메일 N통'과 같은 재료를 좁은 목록용으로 줄인 표현.
        stale = (f"<span class='askstale'> · 이후 {c['stale']}통</span>"
                 if c.get("stale") else "")
        items.append(
            # 래퍼 + 절대배치 ✕ — 앵커가 .mrow 를 유지해야 j/k·선택 표시가 그대로다
            "<div class='askconv'>"
            f"<a class='mrow read' href='/ask?id={c['id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(c['title'])}</span>"
            f"<span class='mdate'>{esc((c.get('last') or '')[5:16])}</span></span>"
            f"<span class='msubj'>{badge} {esc(c.get('state', ''))}{turns}"
            f"{stale}</span></a>"
            "<form class='askdel' method='post' action='/ask/delete'>"
            f"<input type='hidden' name='id' value='{c['id']}'>"
            "<button class='danger' title='대화 삭제' aria-label='대화 삭제'>✕</button>"
            "</form></div>")
    out.append(f"<div class='mlist'>{''.join(items)}</div>")
    out.append(_ask_basis_footer(store, cfg))
    return "\n".join(out)


def _ask_next_up(store, cfg) -> str:
    """랜딩 상태줄 '이어서 볼 것' — 홈(=분석)의 미니 대시보드.

    대화가 없을 때만 보이는 자리라 소음이 없다. 구 홈 대시보드에서 살아남은
    항목: 장기기억 한 줄 + 최근 주간 보고 + 자주 왕래 인물(→ 인물 분석 동선)."""
    rows = []
    dc = store.decision_counts()
    pend = (f" · 제안 {dc.get('candidate', 0)}" if dc.get("candidate") else "")
    rows.append(f"<a href='/records?tab=decisions'>🧠 장기기억 — 결정 "
                f"<b>{dc.get('confirmed', 0)}</b>{pend}</a>")
    wf = weekly_files(cfg)
    rows.append(f"<a href='/records?tab=weekly'>📅 주간 보고 — "
                + (f"{esc(wf[0])}" if wf else "아직 없음") + "</a>")
    ppl = store.frequent_people(3)
    if ppl:
        chips = " · ".join(
            f"<a href='/people?addr={_q(p['addr'])}'>{esc(p['name'] or p['addr'])}</a>"
            for p in ppl)
        rows.append(f"<span>👤 자주 왕래 — {chips}</span>")
    return ("<div class='chatnext'><div class='nexthd'>이어서 볼 것</div>"
            + "".join(f"<div class='nextrow'>{r}</div>" for r in rows) + "</div>")


_HOME_TILE_MAX = 3        # 홈 타일은 훑는 자리 — 항목을 짧게 끊는다
_HOME_COUNT_RX = re.compile(r"\((\d+)건\)")


def _md_sections(md: str) -> dict:
    """저장된 리포트를 '절 제목 → 절 마크다운' 으로 쪼갠다.

    파일 하나를 문자열로 나누는 것뿐이다 — 새 질의도 AI 호출도 없다.
    (홈이 리포트를 **다시 만들지 않는다**는 것이 이 화면의 계약이다.)"""
    out, title, buf = {}, None, []
    for ln in (md or "").splitlines():
        if ln.startswith("## "):
            if title:
                out[title] = "\n".join(buf).strip()
            title, buf = ln[3:].strip(), []
        elif title is not None:
            buf.append(ln)
    if title:
        out[title] = "\n".join(buf).strip()
    return out


def _tile_body(body: str, done: set, limit: int = _HOME_TILE_MAX) -> str:
    """절 마크다운 → 타일 본문. 접힌 항목을 빼고 상위 몇 건만 남긴다.

    '처리함' 버튼은 달지 않는다 — 누르면 홈에서 화면이 튀고, 접는 자리는
    리포트 화면이다. 대신 접힌 항목은 여기서도 안 보인다(같은 집합을 쓴다)."""
    if done:
        body = _apply_done(body, done)
    lines, kept = [], 0
    for ln in body.splitlines():
        if ln.lstrip().startswith(("- ", "* ")) and len(ln) - len(ln.lstrip()) == 0:
            if kept >= limit:
                break
            kept += 1
        lines.append(ln)
    return _md_to_html(review.strip_done_marks("\n".join(lines)))


def _bento_home(store, cfg, today: str) -> str:
    """벤토 홈 — **이미 있는 것을 다시 배치**한 격자.

    재료는 두 가지뿐이다: 지금 랜딩이 이미 읽는 값(장기기억 카운트·최근 주간
    보고·자주 왕래)과 **저장된 오늘 회고 파일**. 새로 계산하지 않고, 회고가
    없으면 그 타일을 그리지 않는다. AI 가 쓴 절(Executive Summary)은 사용자가
    AI 회고를 돌렸을 때만 파일에 있으므로, 있으면 보여주고 없으면 비운다 —
    여기서 새로 부르지 않는다(2026-08-01 사용자 확정)."""
    sec = _md_sections(load_daily(cfg, today) or "")
    done = _done_set(store)
    big, small = [], []          # 내용 타일 / 숫자 한 줄짜리 타일

    def tile(dest, body, label, cnt="", link="", cls=""):
        head = (f"<div class='bth'><span class='lab'>{esc(label)}</span>"
                + (f"<span class='cnt'>{esc(cnt)}</span>" if cnt else "")
                + (f"<a class='more' href='{link}'>열기</a>" if link else "")
                + "</div>")
        dest.append((cls, head + body))

    def find(prefix):
        for k, v in sec.items():
            if k.startswith(prefix):
                m = _HOME_COUNT_RX.search(k)
                return v, (m.group(0) if m else "")
        return None, ""

    daily_link = f"/records?tab=daily&date={_q(today)}"
    body, _c = find("Executive Summary")
    if body:
        tile(big, f"<div class='bsaid'>{_tile_body(body, done, 5)}</div>",
             "오늘의 요약", link=daily_link, cls="ai")
    body, cnt = find("내 약속")
    if body:
        tile(big, _tile_body(body, done), "내 약속", cnt, daily_link)
    body, cnt = find("변화")
    if body:
        tile(big, _tile_body(body, done), "변화 — 어제 이후", cnt, daily_link)

    dc = store.decision_counts()
    pend = dc.get("candidate", 0)
    tile(small, f"<div class='bnum'>{dc.get('confirmed', 0)}</div>"
         + (f"<div class='dim'>반영 대기 {pend}건</div>" if pend else ""),
         "장기기억", link="/records?tab=decisions")

    wf = weekly_files(cfg)
    tile(small, f"<div class='bnum sm'>{esc(wf[0]) if wf else '아직 없음'}</div>",
         "최근 주간 보고", link="/records?tab=weekly")

    ppl = store.frequent_people(3)
    if ppl:
        tile(small, "".join(
            f"<div class='brow'><a href='/people?addr={_q(p['addr'])}'>"
            f"{esc(p['name'] or p['addr'])}</a></div>" for p in ppl),
            "자주 왕래", link="/people")

    # 칸 크기를 내용 수에 맞춘다 — **전부 같은 크기면 격자가 아니라 표다.**
    # 12열 위에서 줄이 딱 떨어지는 조합만 쓴다(들쭉날쭉하면 깨진 것처럼 보인다).
    _BIG_SPANS = {1: (12,), 2: (8, 4), 3: (12, 6, 6)}
    spans = _BIG_SPANS.get(len(big), (12,) * len(big))
    out = []
    for (cls, html), sp in zip(big, spans):
        out.append(f"<div class='btile s{sp} {cls}'>{html}</div>")
    sspan = 4 if len(small) == 3 else (6 if len(small) == 2 else 12)
    for cls, html in small:
        out.append(f"<div class='btile mini s{sspan} {cls}'>{html}</div>")
    return "<div class='bhome'>" + "".join(out) + "</div>" if out else ""


def _ask_landing(store, cfg, today: str | None = None) -> str:
    """새 대화(홈 첫 화면) — 인트로 + 하단 입력 + 이어서 볼 것.

    벤토 스킨에서는 '이어서 볼 것' 줄 대신 격자(_bento_home)를 깐다.
    클래식은 지금 그대로다."""
    if _skin_ok(cfg.opt("web", "skin", default="classic")) == "bento":
        grid = _bento_home(store, cfg, today or date.today().isoformat())
        return ("<div class='chat'><div class='chatintro'>"
                "<h2>무엇이 궁금하세요?</h2>"
                "<p class='dim'>저장된 메일에서 <b>근거가 달린 답</b>을 찾습니다. "
                "인용을 원문과 대조해 통과한 것만 남기고, 답이 없으면 없다고 합니다."
                "</p></div>" + grid + "</div>"
                + _ask_input(None, "메일에 대해 물어보세요"))
    return ("<div class='chat'><div class='chatintro'>"
            "<h2>무엇이 궁금하세요?</h2>"
            "<p class='dim'>저장된 메일에서 <b>근거가 달린 답</b>을 찾습니다. "
            "찾은 메일을 읽고 인용을 원문과 대조해, 통과한 것만 답에 남깁니다. "
            "답이 없으면 없다고 답합니다.</p>"
            "<p class='dim'>예) NPX-200 양자화 최종 결정 뭐였지? · "
            "MPW 일정 언제로 확정됐어? · 인물 페이지에서 <b>대화 분석</b>으로 "
            "브리핑도 됩니다.</p>"
            "</div>" + _ask_next_up(store, cfg) + "</div>"
            + _ask_input(None, "메일에 대해 물어보세요"))


def render_ask(store, cfg, qs, today: str | None = None) -> str:
    """질문하기 GET 화면 — 저장된 대화·캐시만 열고, 새 잡은 POST만 허용한다."""
    from . import ask as ask_mod

    rid = (qs.get("id") or [""])[0]
    if rid.isdigit():                            # 저장된 대화 열기
        tr = ask_mod.transcript(store, int(rid))
        if tr:
            return render_ask_thread(store, cfg, tr)
        return "<div class='chat'><p class='empty'>대화를 찾지 못했습니다.</p></div>"

    # 구 GET 링크 호환: 저장된 답은 열 수 있지만 새 AI 잡은 GET 렌더에서 시작하지 않는다.
    person = (qs.get("person") or [""])[0].strip().lower()
    if person:
        name = store.person_name(person) or person
        q = f"{name} · 최근 {ask_mod.BRIEF_MONTHS}개월 브리핑 — 내가 알아야 할 것"
        hit = ask_mod.cached(store, q, None, person)
        if hit:
            tr = ask_mod.transcript(store, hit["id"])
            return render_ask_thread(store, cfg, tr or _ask_one_turn(hit))
        return _ask_landing(store, cfg, today)

    q = (qs.get("q") or [""])[0].strip()
    parent = (qs.get("follow") or [""])[0]
    parent_id = int(parent) if parent.isdigit() else None

    if not q:                                    # 새 대화 진입
        return _ask_landing(store, cfg, today)

    hit = ask_mod.cached(store, q, parent_id)
    if hit:                                      # 옛 북마크도 저장된 대화까지만 연다
        tr = ask_mod.transcript(store, hit["id"])
        return render_ask_thread(store, cfg, tr or _ask_one_turn(hit))
    return _ask_landing(store, cfg, today)


def _ask_fallback(store, cfg, q: str, err: str, person: str = "",
                  mail_id: int | None = None) -> str:
    """AI 불가 — 일반 검색 결과라도 보여준다(#10). POST 재시도 폼 유지.

    메일 분석 실패면 재시도가 **mid 로** 다시 제출돼야 한다 — 질문 문자열로
    재제출하면 seed·scope 없는 일반 질문이 되어, 성공해도 스레드 머리글의
    '분석 보기'와 영영 연결되지 않는다(이중 이력). 검색어도 자동 생성 질문이
    아니라 그 메일 제목을 쓴다."""
    if mail_id:
        target = f"<input type='hidden' name='mid' value='{int(mail_id)}'>"
        m = store.message(str(mail_id))
        search_q = (m["subject"] or "").strip() if m else q
    elif person:
        target = f"<input type='hidden' name='person' value='{esc(person)}'>"
        search_q = q
    else:
        target = f"<input type='hidden' name='q' value='{esc(q)}'>"
        search_q = q
    retry = ("<form class='asklaunch' method='post' action='/ask/jobs'>"
             "<input type='hidden' name='fresh' value='1'>"
             f"{target}"
             "<button class='aibtn ghost compact'>다시 시도</button></form>")
    banner = (f"<div class='aifail'>질문에 답할 수 없습니다 — {esc(err)}. "
              "일반 검색 결과를 보여드립니다. "
              f"{retry}</div>")
    return banner + render_search(store, cfg, {"q": [search_q]},
                                  date.today().isoformat())


def render_ask_status(store, cfg, token: str = "") -> tuple:
    """(inner, running) — 폴링·전체페이지 공용. 진행 중이면 대화록 + 조사 중 말풍선."""
    with _ask_lock:
        st = dict(_ask_job)
    if token and token != st.get("token"):
        return ("<div class='chat'><p class='empty'>이 분석 작업을 찾지 못했습니다.</p>"
                "</div>", False)
    if st["running"]:
        from . import ask as ask_mod
        # 이어 묻기면 지금까지의 대화 위에, 새 대화면 빈 대화 위에 조사 중 말풍선을 얹는다
        pid = st.get("parent")
        tr = ask_mod.transcript(store, int(pid)) if pid else None
        if tr is None:
            tr = {"turns": [], "latest_id": None}
        inner = render_ask_thread(
            store, cfg, tr, pending=st.get("question", ""),
            job_token=st.get("token", ""),
            live=_job_live_line(st), preview=_job_preview(st),
            stage=st.get("stage") or "조사 준비 중…",
            model=st.get("model") or "", stream=st.get("stream", False),
            started=st.get("started") or 0.0,
        )
        return inner, True
    if st["stage"] == "cancelled":
        return ("<div class='chat'><p class='empty'>조사를 중지했습니다 — "
                "답변은 저장되지 않았습니다.</p></div>"
                + _ask_input(None, "질문하기"), False)
    if st["stage"] == "error":
        return _ask_fallback(store, cfg, st.get("question", ""),
                             st.get("error", ""), st.get("person", ""),
                             st.get("mail")), False
    if st.get("result"):
        from . import ask as ask_mod
        res = st["result"]
        tr = ask_mod.transcript(store, res["id"]) if res.get("id") else None
        # 캐시 기록 실패로 id 가 없어도 방금 결과는 보여준다 — 답이 증발하면 안 됨
        marker = (f"<div data-ask-result-id='{int(res['id'])}' hidden></div>"
                  if res.get("id") else "")
        return marker + render_ask_thread(store, cfg, tr or _ask_one_turn(res)), False
    return render_ask(store, cfg, {}), False


def render_records(store, cfg, qs, today: str) -> str:
    """기억 페이지 — 탭: 일간 회고 | 주간 보고 | 장기기억."""
    tab = (qs.get("tab") or ["daily"])[0]
    if tab not in ("daily", "weekly", "decisions"):
        tab = "daily"
    counts = store.decision_counts()
    cand = counts.get("candidate", 0)
    dec_label = (f"장기기억 {counts.get('confirmed', 0)}"
                 + (f" · 제안 {cand}" if cand else ""))
    tabs = []
    for key, label in (("daily", "일간 회고"), ("weekly", "주간 보고"),
                       ("decisions", dec_label)):
        if key == tab:
            tabs.append(f"<b>{esc(label)}</b>")
        else:
            tabs.append(f"<a href='/records?tab={key}'>{esc(label)}</a>")
    bar = ("<div class='listtabs'><span class='ltabs'>"
           + " · ".join(tabs) + "</span></div>")
    if tab == "decisions":
        return bar + render_decisions(store, qs)
    if tab == "weekly":
        with _weekly_lock:
            running = _weekly_job["running"]
        if running:                      # 생성 중이면 대기 화면(폴링이 전환)
            return bar + render_weekly_status(cfg, store)[0]
        return bar + render_weekly(cfg, qs, store)
    day = (qs.get("date") or [today])[0]
    if day == today:                     # 오늘 데일리 조회 시에도 배경 자동 생성
        _maybe_auto_review(cfg, today, _max_rowid(store))
    return bar + render_daily(cfg, day, today, store)


# ─────────────────────────────────────────────────── 조작(POST) 동작

def _toggle_thread(store, cfg, tid: int, kind: str) -> str:
    """목록 단축키(f/h)용 상태 토글 — DB 현재값을 뒤집고 결과 토큰을 돌려준다.
    버튼(flag/unflag·hide/unhide)과 달리 클라이언트가 상태를 몰라도
    되게 서버가 판정한다. 토큰(예: 'flag:on')을 app.js 가 상태명 토스트로 매핑."""
    if kind == "flag":
        cur = store.thread(tid)
        on = not (cur and cur["flagged"])
        store.set_flag(tid, on)
        return "flag:on" if on else "flag:off"
    if kind == "hide":
        cur = store.thread(tid)
        on = not (cur and cur["hidden"])
        store.hide_thread(tid, on)
        return "hide:on" if on else "hide:off"
    return ""


def _submit_ask_job(store, cfg, form: dict) -> str:
    """POST /ask/jobs — 질문은 본문에서 받고 주소에는 불투명 잡 토큰만 남긴다."""
    from . import ask as ask_mod

    person = (form.get("person") or [""])[0].strip().lower()
    mid_raw = (form.get("mid") or [""])[0].strip()
    mail_id = int(mid_raw) if mid_raw.isdigit() else None
    fresh = (form.get("fresh") or [""])[0] == "1"
    parent = (form.get("follow") or [""])[0]
    parent_id = int(parent) if parent.isdigit() else None
    if mail_id:
        m = store.message(str(mail_id))
        if not m:
            return "/mail?msg=" + _q(f"메일 #{mail_id} 을 찾을 수 없습니다")
        question = ask_mod.mail_question(mail_id, m["subject"] or "")
        scope = f"mail:{mail_id}"
        parent_id = None
    elif person:
        name = store.person_name(person) or person
        question = (f"{name} · 최근 {ask_mod.BRIEF_MONTHS}개월 브리핑 "
                    "— 내가 알아야 할 것")
        scope = person
        parent_id = None
    else:
        question = (form.get("q") or [""])[0].strip()
        scope = ""
    if not question:
        return "/?msg=" + _q("질문을 입력해 주세요")

    if not fresh:
        hit = ask_mod.cached(store, question, parent_id, scope)
        if hit:
            return f"/ask?id={int(hit['id'])}"

    token = _start_ask(cfg, question, parent_id, person, mail_id,
                       use_cache=not fresh)
    if token is None:
        # 단일 슬롯 — 이 질문은 시작되지 않았다. 진행 화면으로 합류시키되 거기엔
        # 남의 질문이 떠 있으므로, 왜 그런지와 내 질문이 안 걸렸음을 반드시 알린다.
        with _ask_lock:
            running = dict(_ask_job)
        busy = (running.get("question") or "").strip()
        short = busy if len(busy) <= 30 else busy[:30] + "…"
        note = (f"'{short}' 조사 중 — 이 질문은 시작되지 않았습니다"
                if busy else "다른 분석이 진행 중입니다 — 이 질문은 시작되지 않았습니다")
        token = running.get("token") or ""
        if not token:
            return "/?msg=" + _q(note)
        return f"/ask/status?job={_q(token)}&msg=" + _q(note)
    return "/ask/status?job=" + _q(token)


def _report_back(raw: str) -> str:
    """리포트 화면으로 돌아갈 주소를 **다시 만든다** — 받은 문자열은 쓰지 않는다.

    탭과 날짜만 뽑아 서버가 조립한다. 사용자 입력이 그대로 Location 헤더로 가면
    `/records\r\nX-Injected: …` 로 응답을 통째로 위조할 수 있다(http.server 는
    헤더 값을 검증하지 않는다 — 2026-08-01 적대 검토에서 실증)."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw or "").query)
    except ValueError:
        q = {}
    tab = (q.get("tab") or [""])[0]
    day = (q.get("date") or [""])[0]
    out = "/records?tab=" + (tab if tab in ("daily", "weekly") else "daily")
    return out + (f"&date={day}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "")


def perform_action(store, cfg, path: str, form: dict) -> str:
    """상태 변경 동작 실행 → 리다이렉트할 위치(?msg= 포함) 반환. 소켓 무관(테스트 대상).

    Outlook COM(open/outlook sync)은 Windows 서버 스레드에서 실행된다.
    """
    parts = path.strip("/").split("/")

    # /sync 는 백그라운드 잡으로 이관(do_POST 에서 _start_sync → /sync/status).
    # UI 프리즈 방지. 실제 동작은 _do_sync(수집+프룬) — 잡·테스트 공용.

    if path == "/settings/unblock":
        addr = (form.get("addr") or [""])[0].strip()
        if addr and cfgmod.remove_blocked(cfg, addr):
            return "/settings?msg=" + _q(f"차단 해제: {addr}")
        return "/settings?msg=" + _q("해제할 항목 없음")

    if path in ("/report/done", "/report/undo"):
        # 리포트 항목 접기/되돌리기 — 메일 밖(회의·구두)에서 처리한 것.
        kind = (form.get("kind") or [""])[0].strip()
        key = (form.get("key") or [""])[0].strip()
        back = _report_back((form.get("back") or [""])[0])
        if kind not in dict(_DONE_KINDS) or not re.fullmatch(r"[0-9a-f]{6,40}", key):
            return back
        if path == "/report/done":
            tid = (form.get("tid") or ["0"])[0].strip()
            store.mark_report_done(kind, key, int(tid) if tid.isdigit() else 0,
                                   (form.get("label") or [""])[0].strip())
        else:
            store.unmark_report_done(kind, key)
        return back

    if path == "/ask/jobs":                           # 분석 잡 생성(GET 렌더와 분리)
        return _submit_ask_job(store, cfg, form)

    if path == "/ask/delete":                         # 분석 대화 삭제(뿌리+이어묻기 전체)
        from . import ask as ask_mod
        rid = (form.get("id") or [""])[0].strip()
        if not rid.isdigit():
            return "/ask/list?msg=" + _q("삭제할 대화 없음")
        n = store.ask_delete(ask_mod.conversation_ids(store, int(rid)))
        return "/ask/list?msg=" + _q(f"대화 삭제 — {n}문답" if n else "이미 삭제됨")

    if path == "/block":                              # 주소별 보기 페이지의 발신자 차단
        addr = (form.get("addr") or [""])[0].strip().lower()
        if not addr:
            return "/?msg=" + _q("차단할 주소 없음")
        p = "/person?addr=" + _q(addr) + "&msg="
        if cfgmod.add_blocked(cfg, addr):
            return p + _q(f"차단: {addr} · Outlook 규칙에도 추가하세요")
        return p + _q(f"이미 차단됨: {addr}")

    if len(parts) == 3 and parts[0] == "decision":
        # 장기기억 반영 대기 — 반영은 사람(휴먼 인 더 루프). AI 는 초안 제안만.
        # (내부 액션명 confirm/reject 은 유지 — 화면 용어만 반영/유보)
        back = "/records?tab=decisions"
        try:
            did = int(parts[1])
        except ValueError:
            return back + "&msg=" + _q("잘못된 항목")
        action = parts[2]
        if action == "confirm":
            ok = store.set_decision_status(did, "confirmed")
            return back + "&msg=" + _q("장기기억에 반영" if ok else "항목 없음")
        if action == "reject":
            ok = store.set_decision_status(did, "rejected")
            return back + "&msg=" + _q("유보됨" if ok else "항목 없음")
        if action == "amend":
            title = (form.get("title") or [""])[0].strip()
            rationale = (form.get("rationale") or [""])[0]
            if not title:
                return back + "&msg=" + _q("반영할 내용이 비었습니다")
            ok = store.set_decision_status(did, "confirmed",
                                           title=title, rationale=rationale)
            return back + "&msg=" + _q("수정 후 반영" if ok else "항목 없음")

    if len(parts) == 3 and parts[0] == "thread":
        try:
            tid = int(parts[1])
        except ValueError:
            return "/?msg=" + _q("잘못된 스레드")
        action, back = parts[2], f"/thread/{tid}"
        if action == "record-decision":
            # 수동 기록 — 사람이 직접 쓰는 것이라 즉시 반영(confirmed, 인용 불요)
            title = (form.get("title") or [""])[0].strip()
            if not title:
                return back + "?msg=" + _q("반영할 내용이 비었습니다")
            msgs = store.thread_messages(tid)
            decided = msgs[-1]["sent_on"][:10] if msgs else ""
            did = store.add_decision(
                tid, decided, title,
                rationale=(form.get("rationale") or [""])[0].strip(),
                decider=(form.get("decider") or [""])[0].strip(),
                status="confirmed", source="manual")
            return back + "?msg=" + _q(
                "장기기억에 반영됨" if did else "이미 장기기억에 있음")
        if action == "flag":
            store.set_flag(tid, True)
            return back + "?msg=" + _q("플래그 표시")
        if action == "unflag":
            store.set_flag(tid, False)
            return back + "?msg=" + _q("플래그 해제")
        if action == "hide":
            # 숨김: 목록·추적에서 제외, 새 수신 메일이 오면 자동 해제
            store.hide_thread(tid, True)
            return back + "?msg=" + _q(
                "숨김 — 목록·추적에서 제외, 새 메일 오면 자동 해제 (숨김 탭에서 복구)")
        if action == "unhide":
            store.hide_thread(tid, False)
            return back + "?msg=" + _q("숨김 해제")
        if action == "note":
            from . import notes
            p = notes.create_thread_note(cfg, store, tid)
            return back + "?msg=" + _q(f"노트 생성: {p.name}")
        if action == "open":                          # Windows COM
            from .sources import get_source
            msgs = store.thread_messages(tid)
            if not msgs:
                return back + "?msg=" + _q("메일 없음")
            m = msgs[-1]
            ok = get_source("outlook").open_in_outlook(m["entry_id"], m["message_id"])
            return back + "?msg=" + _q("Outlook에서 열림" if ok else "Outlook에서 못 찾음")
        if action == "attach":                         # Windows COM
            from .sources import get_source
            dest = cfg.vault / "notes" / f"attachments-{tid}"
            dest.mkdir(parents=True, exist_ok=True)
            src = get_source("outlook")
            used, saved = set(), []
            for m in store.thread_messages(tid):
                if m["attach_names"]:
                    saved += src.save_attachments(
                        m["entry_id"], str(dest), m["message_id"], used=used)
            if saved:
                return back + "?msg=" + _q(f"첨부 {len(saved)}개 저장: {dest}")
            return back + "?msg=" + _q("추출할 첨부 없음")

    return "/?msg=" + _q("알 수 없는 동작")


# ─────────────────────────────────────────────────── 라우팅 (모듈 함수 — 테스트 대상)

def _offset(qs) -> int:
    try:
        return max(0, int((qs.get("offset") or ["0"])[0]))
    except ValueError:
        return 0


def route(store, cfg, path, qs, today):
    """(title, inner, code, pane) — pane 은 셸의 좌/우 배치 (#14).

    left: 목록/메뉴 성격(홈·메일함·스레드·검색·데일리·설정),
    right: 상세 성격(스레드·렌즈·리뷰 상태).
    """
    if path in ("/", "/lens/intervene"):
        # 홈 = 분석(대화). 구 대시보드(지금 할 일·개입·오늘 핵심)는 제거(2026-07-26)
        # — 개입 신호 노출은 2026-07-30 제거됐고(판정 엔진은 주간 보고 재료로 유지), 장기기억·주간·인물은 랜딩 상태줄로.
        # 데일리 리뷰 lazy 자동 생성 트리거는 홈 진입에 그대로 둔다(앱 열면 생성).
        _maybe_auto_review(cfg, today, _max_rowid(store))
        return "분석", render_ask(store, cfg, qs, today), 200, "right"
    if path == "/mail":
        return ("메일함", render_mail(store, cfg, _offset(qs), _list_flt(qs),
                                   (qs.get("g") or [""])[0]), 200, "left")
    if path == "/threads":
        return ("스레드", render_threads(store, cfg, _offset(qs), _list_flt(qs),
                                      (qs.get("g") or [""])[0]), 200, "left")
    if path == "/people":
        # 인물 도시에. addr 있으면 그 사람 도시에, 없으면 랜딩 목록.
        addr = (qs.get("addr") or [""])[0]
        inner = (render_dossier(store, cfg, addr) if addr
                 else render_people_page(store, cfg))
        return "인물", inner, 200, "left"
    if path == "/people/dossier/status":
        inner, running = render_dossier_status(
            store, cfg, (qs.get("addr") or [""])[0])
        return "인물", inner, 200, "left"
    if path == "/person":
        # 도시에의 '전체 왕래 메일 →'로 도달. 발신자 차단도 여기.
        return "주소별 메일", render_person(store, cfg, (qs.get("addr") or [""])[0]), 200, "left"
    if path == "/search":
        return "검색", render_search(store, cfg, qs, today), 200, "left"
    if path == "/settings":
        return "설정", render_settings(store, cfg), 200, "left"
    if path in ("/records", "/daily"):
        # 기억(데일리·장기기억). /daily 는 구 메뉴 경로 — 북마크 호환 흡수.
        # 경로 /records 는 표시명 개편(기록→기억, 2026-07-17) 후에도 유지 — URL 과
        # 표시명이 어긋나도 동선엔 영향이 없고, 옛 북마크 호환 분기만 늘어난다.
        return "기억", render_records(store, cfg, qs, today), 200, "left"
    if path == "/review/status":
        inner, running = render_review_status(store)
        return "정리", inner, 200, "right"
    if path == "/aisearch/status":
        inner, running = render_aisearch_status(store, cfg, today)
        return "AI 검색", inner, 200, "left"
    if path == "/weekly/status":
        inner, running = render_weekly_status(cfg, store)
        return "주간 보고", inner, 200, "left"
    # 질문하기 = 좌(이력 목록) / 우(질문·답변) — 메일함과 같은 감각.
    # 답변은 긴 읽을거리라 읽기 패널(--read-w)이 맞고, app.js paneFor 와도 일치한다.
    if path == "/ask":
        return "분석", render_ask(store, cfg, qs, today), 200, "right"
    if path == "/ask/list":
        # 좌측 대화 목록 fragment — 폴링 완료 후 app.js 가 좌측만 새로 그릴 때.
        # (/ask?frag=1 은 우측 콘텐츠를 반환하므로 좌측 갱신엔 못 쓴다)
        return "분석", render_ask_list(store, cfg=cfg), 200, "left"
    if path == "/ask/status":
        token = (qs.get("job") or [""])[0]
        inner, running = render_ask_status(store, cfg, token)
        return "분석", inner, 200, "right"
    if path == "/sync/status":
        inner, running = render_sync_status(store)
        return "동기화", inner, 200, "right"
    if path.startswith("/thread/"):
        try:
            tid = int(path.split("/")[2])
        except (IndexError, ValueError):
            return "404", "<p>잘못된 스레드</p>", 404, "right"
        store.mark_thread_read(tid)   # 열람 = 읽음 (다음 목록 렌더에 반영)
        return "스레드", render_thread(store, cfg, tid), 200, "right"
    return "404", "<p class='empty'>없는 페이지</p>", 404, "right"


# ─────────────────────────────────────────────────── HTTP 핸들러

class _Handler(BaseHTTPRequestHandler):
    cfg = None  # serve() 가 주입
    # 단일 스레드 서버 보호: 브라우저(Edge/Chrome)는 요청 없이 미리 여는
    # 투기적 연결을 만드는데, 그 빈 소켓의 요청 대기에 서버가 잡히면 다음
    # 클릭이 그동안 멈춘다. 로컬은 요청 전송이 즉각적이므로 3초면 충분.
    timeout = 3

    def log_message(self, *a):  # 조용히
        pass

    def _send_html(self, html_str: str, code: int = 200) -> None:
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 진행 중 화면은 1.5초마다 같은 URL 을 다시 부른다. 캐시 지시가 없으면
        # 브라우저(Chrome/Edge)가 메모리 캐시에서 같은 응답을 돌려줘 수신량·단계가
        # 첫 값에 굳는다(2026-07-29 실기기 증상). 로컬 1인 도구라 HTML 캐시로
        # 얻을 것이 없으므로 전부 no-store.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _panes(self, store, inner: str, pane: str, today: str,
               path: str = "") -> tuple[str, str]:
        """요청된 패널 외의 기본 콘텐츠 — 좌측 기본은 분석 이력, 우측은 읽기 패널.

        좌측은 상단 메뉴 콘텐츠(메일함/스레드/검색/기억 등), 우측은 스레드·메일
        상세를 여는 읽기 영역. 홈(/)=분석이므로 우측 콘텐츠(대화록·랜딩)의 좌측
        기본은 분석 대화 이력이다.

        예외 — `/thread/N` 전체 로드(F5·북마크)의 좌측은 스레드 목록. app.js 의
        `leftCur` 부트스트랩과 짝을 맞출 것(어긋나면 표시 최신화가 왼쪽을 딴
        화면으로 바꿔치기)."""
        if pane == "left":
            return inner, _READING_HINT
        if path.startswith("/thread/"):
            return render_threads(store, self.cfg), inner
        # 홈(=분석)·/ask*·상태 페이지의 좌측 기본 = 분석 대화 이력
        return render_ask_list(store, cfg=self.cfg), inner

    def _is_fetch(self) -> bool:
        return self.headers.get("X-Requested-With") == "fetch"

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(u.query)
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/favicon.svg":
            body = _FAVICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/app.js", "/report.js"):
            js = _APP_JS if path == "/app.js" else report.REPORT_JS
            body = js.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/winsize":            # 기억된 창 크기 → app.js 가 resizeTo 로 복원
            body = _win_size_arg(self.cfg.opt("web", "window_size",
                                              default="2000,1200")).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/syncmin":            # 자동 동기화 주기(분) → app.js setInterval
            body = str(_sync_interval_min(self.cfg)).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/latest":             # 표시 최신화 토큰 — DB 변경(새 메일) 감지용(가벼움)
            # MAX(rowid): messages 는 삭제 없이 append-only 라 새 메일이면 반드시 증가.
            # + 오늘 데일리 파일 mtime: 배경 자동 리뷰가 재생성하면 토큰이 바뀌어
            #   app.js 가 홈을 in-place 로 다시 그린다(결정론 리뷰 자동 반영).
            st = Store(self.cfg.db_path, self.cfg.my_addresses, self.cfg.my_names, noise=self.cfg)
            try:
                row = st.db.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
            finally:
                st.close()
            daily = Path(self.cfg.vault) / "daily" / f"{date.today().isoformat()}.md"
            mt = int(daily.stat().st_mtime) if daily.exists() else 0
            body = f"{row[0]}:{mt}".encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        frag = (qs.get("frag") or [""])[0] == "1"
        today = date.today().isoformat()
        store = Store(self.cfg.db_path, self.cfg.my_addresses, self.cfg.my_names, noise=self.cfg)
        try:
            if path == "/stats":
                # 통계 분석 — 좌/우 셸 대신 전폭 단일 컬럼이되, 상단 nav 셸은
                # 다른 메뉴와 동일(Minerva·홈·메일함…). 검토 기간 선택은 없앴다
                # (2026-08-02) — 창은 절마다 고정이라 ?weeks= 는 무시한다.
                # 구 북마크가 404 나지 않도록 파라미터는 조용히 버린다.
                self._send_html(render_stats_page(store, self.cfg))
                return
            title, inner, code, pane = route(store, self.cfg, path, qs, today)
            # 전체 페이지 모드의 진행 화면(리뷰·AI검색)은 meta refresh 로 자동 새로고침
            # (JS 꺼짐 폴백). AI 검색 대기는 /search?ai=1 자체가 이 마커를 실어 온다.
            refresh = 2 if (not frag and any(
                m in inner for m in _RUNNING_MARKERS)) else None
            msg = (qs.get("msg") or [""])[0]
            if msg and not frag:
                # fragment 모드는 JS 토스트가 msg 를 표시 — flash 중복 방지
                inner = f"<div class='flash'>{esc(msg)}</div>" + inner
            if frag:
                body = inner
            else:
                left, right = self._panes(store, inner, pane, today, path)
                rw = self.cfg.opt("web", "reading_width", default=1200)
                rf = self.cfg.opt("web", "reading_font", default=0)  # 0=미설정(주입 없음)
                theme = self.cfg.opt("web", "theme", default="light")
                body = _shell(title, left, right, refresh, read_w=rw,
                              theme=theme, read_fs=rf,
                              skin=self.cfg.opt("web", "skin", default="classic"))
        except Exception:  # 죽지 않게 — 상세는 콘솔(개발용), 화면엔 친절한 안내
            import traceback
            traceback.print_exc()
            code = 500
            th = self.cfg.opt("web", "theme", default="light")
            sk = self.cfg.opt("web", "skin", default="classic")
            msg = ("<p class='empty'>문제가 발생해 이 화면을 열지 못했습니다.<br>"
                   "잠시 후 다시 시도하거나 창을 닫았다 다시 열어 주세요.</p>")
            body = msg if frag else _shell(
                "오류", msg, "<p class='empty'>오류</p>", theme=th, skin=sk)
        finally:
            store.close()
        self._send_html(body, code)

    def do_POST(self):
        host = self.headers.get("Host", "")
        if not same_origin(self.headers.get("Origin"), host):
            blocked = _blocked_html(host)
            th = self.cfg.opt("web", "theme", default="light")
            self._send_html(blocked if self._is_fetch()
                            else _page("차단", blocked, theme=th), 403)
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        form = urllib.parse.parse_qs(raw)
        if path == "/winsize":            # 창 크기 기억 → 다음 실행 --window-size (DB 불필요)
            try:
                w = int(float((form.get("w") or ["0"])[0]))
                h = int(float((form.get("h") or ["0"])[0]))
            except (ValueError, TypeError):
                w = h = 0
            if w >= 400 and h >= 300:
                cfgmod.set_override(self.cfg.home, "web", "window_size",
                                    _win_size_arg("%d,%d" % (w, h)))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/settings/theme":     # 라이트/다크 저장 (app.js 가 즉시 적용, 여기선 영구화)
            val = "dark" if (form.get("theme") or ["light"])[0] == "dark" else "light"
            cfgmod.set_override(self.cfg.home, "web", "theme", val)
            _Handler.cfg = cfgmod.load(self.cfg.home)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/settings/skin":      # 화면 스킨(모양·밀도) — 테마와 별개 축
            val = _skin_ok((form.get("skin") or ["classic"])[0])
            cfgmod.set_override(self.cfg.home, "web", "skin", val)
            _Handler.cfg = cfgmod.load(self.cfg.home)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/autosync":           # app.js 주기 동기화 → 백그라운드 잡 시작(논블로킹)
            # 서빙 스레드를 막지 않게 잡만 띄우고 즉시 응답. 완료·신규 통수는 app.js 가
            # /sync/status 폴링으로 받아 '새 메일 N통' 토스트를 띄운다(수집+프룬은 잡 안).
            started = _start_sync(self.cfg)
            body = b"started" if started else b"busy"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        today = date.today().isoformat()
        store = Store(self.cfg.db_path, self.cfg.my_addresses, self.cfg.my_names, noise=self.cfg)
        try:
            tog = re.match(r"^/thread/(\d+)/(flag|hide)-toggle$", path)
            if tog:                                   # 목록 단축키(f/h) — 상태 토큰 200
                token = _toggle_thread(store, self.cfg, int(tog.group(1)), tog.group(2))
                body = token.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return                                # finally 가 store.close()
            if path == "/review":                     # 데일리 생성(백그라운드)
                ai = bool((form.get("ai") or [""])[0])
                # 데일리 페이지가 실어 준 날짜 — 그 날짜의 리뷰를 (재)생성한다.
                # 검증 실패·미래 날짜는 오늘로(필드 없는 구 진입점 호환 포함).
                d = (form.get("date") or [""])[0].strip()
                try:
                    d = min(date.fromisoformat(d).isoformat(), today)
                except ValueError:
                    d = today
                if not _start_review(self.cfg, ai, d):
                    # 홈 진입마다 도는 결정론 자동 갱신이 슬롯을 먼저 잡을 수
                    # 있다. 조용히 남의 잡 화면으로 보내면 사용자는 자기 요청이
                    # 도는 줄 안다 — 시작되지 않았음을 말한다.
                    location = ("/review/status?msg="
                                + _q("다른 회고 작업이 진행 중입니다 — "
                                     "이 요청은 시작되지 않았습니다"))
                else:
                    location = "/review/status"
            elif path == "/sync":                     # 메일 동기화(백그라운드)
                _start_sync(self.cfg)
                location = "/sync/status"
            elif path == "/weekly":                   # 주간 보고 생성(백그라운드)
                try:
                    weeks = max(1, min(12, int((form.get("weeks") or ["1"])[0])))
                except ValueError:
                    weeks = 1
                if not _start_weekly(self.cfg, weeks):
                    location = ("/weekly/status?msg="
                                + _q("다른 주간 보고가 진행 중입니다 — "
                                     "이 요청은 시작되지 않았습니다"))
                else:
                    location = "/weekly/status"
            elif path == "/weekly/cancel":            # 중지 — Event 만 켠다.
                # 실제 종료는 스트리밍 루프가 프로세스를 죽이며 수행(0.5초 주기)
                with _weekly_lock:
                    ev = _weekly_job.get("cancel")
                    if _weekly_job["running"] and ev is not None:
                        ev.set()
                location = "/weekly/status"
            elif path == "/aisearch/cancel":
                with _aisearch_lock:
                    ev = _aisearch_job.get("cancel")
                    q0 = _aisearch_job.get("query") or ""
                    if _aisearch_job["running"] and ev is not None:
                        ev.set()
                location = "/search?q=" + _q(q0)   # 일반 검색 결과로 내려앉는다
            elif path == "/review/cancel":
                with _review_lock:
                    ev = _review_job.get("cancel")
                    if _review_job["running"] and ev is not None:
                        ev.set()
                location = "/review/status"
            elif path == "/people/dossier":          # 인물 요약 갱신(백그라운드)
                who = (form.get("addr") or [""])[0].strip().lower()
                if not who:                          # 주소 없는 요청은 슬롯도 안 잡는다
                    location = "/people?msg=" + _q("인물을 먼저 선택해 주세요")
                elif not _start_dossier(self.cfg, who,
                                        store.person_name(who) or ""):
                    location = (f"/people?addr={_q(who)}&msg="
                                + _q("다른 인물 요약 작업이 진행 중입니다 — "
                                     "이 요청은 시작되지 않았습니다"))
                else:
                    location = f"/people/dossier/status?addr={_q(who)}"
            elif path == "/people/dossier/cancel":
                who = (form.get("addr") or [""])[0].strip().lower()
                with _dossier_lock:
                    ev = _dossier_job.get("cancel")
                    if _dossier_job["running"] and ev is not None:
                        ev.set()
                location = f"/people?addr={_q(who)}"
            elif path == "/ask/cancel":
                with _ask_lock:
                    ev = _ask_job.get("cancel")
                    if _ask_job["running"] and ev is not None:
                        ev.set()
                    token = _ask_job.get("token", "")
                location = "/ask/status?job=" + _q(token)
            elif path == "/settings/update":
                location = _git_update()          # git pull — 적용은 창 닫았다 재실행
            elif path in ("/settings/save", "/settings/noise"):
                # 오버라이드 파일 저장 후 cfg 재로드 → 즉시 반영(다음 요청부터)
                home = self.cfg.home
                location = (_save_settings(home, form) if path == "/settings/save"
                            else _save_noise(self.cfg, form))
                _Handler.cfg = cfgmod.load(home)
            else:
                location = perform_action(store, self.cfg, path, form)
        except Exception as e:
            location = "/?msg=" + _q(
            "실패 — " + (" ".join(str(e).split())[:120] or type(e).__name__))
        finally:
            store.close()
        if self._is_fetch():
            location = _with_frag(location)           # fetch 는 fragment 를 따라감 (#16)
        self.send_response(303)
        # CR/LF 가 남아 있으면 응답 헤더가 위조된다. 각 경로가 _q 로 인코딩하는 것이
        # 원칙이지만, 한 곳이라도 새면 응답 전체가 넘어가므로 여기서 한 번 더 막는다.
        self.send_header("Location", location.replace("\r", "").replace("\n", ""))
        self.send_header("Content-Length", "0")
        self.end_headers()


def _find_msedge() -> str | None:
    """msedge.exe 탐색: PATH → 표준 설치 경로 (#19)."""
    import os
    import shutil
    from pathlib import Path
    exe = shutil.which("msedge")
    if exe:
        return exe
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cand = (Path(base) / "Microsoft" / "Edge" / "Application"
                    / "msedge.exe")
            if cand.is_file():
                return str(cand)
    return None


def _win_size_arg(raw) -> str:
    """'W,H' 를 안전 정규화 — 정수 2개, 합리 범위로 클램프. 파싱 실패 시 2000,1200.
    (--window-size 인자로 쓰이므로 신뢰 못 할 값을 그대로 넣지 않는다.)"""
    try:
        w, h = (int(float(x)) for x in str(raw).split(",")[:2])
    except (ValueError, TypeError):
        return "2000,1200"
    return "%d,%d" % (max(600, min(w, 6000)), max(400, min(h, 4000)))


def _sync_interval_min(cfg) -> int:
    """자동 동기화 주기(분). 0=끔. 기본 30, 합리 범위로 클램프(과도 폴링 방지)."""
    try:
        v = int(cfg.opt("web", "sync_interval_min", default=30))
    except (ValueError, TypeError):
        return 30
    if v <= 0:
        return 0
    return max(1, min(v, 1440))


def _open_ui(url: str, app_mode: bool, window_size: str = "2000,1200") -> None:
    """UI 열기 — 앱 모드(Edge --app)는 Windows 전용, 실패 시 기본 브라우저 폴백.
    window_size 는 마지막으로 기억된 창 크기(없으면 2000,1200)."""
    import sys as _sys
    if app_mode and _sys.platform == "win32":
        exe = _find_msedge()
        if exe:
            try:
                import subprocess
                subprocess.Popen([exe, f"--app={url}",
                                  f"--window-size={_win_size_arg(window_size)}"])
                return
            except OSError:
                pass
        print("Edge 를 찾지 못해 기본 브라우저로 엽니다", file=_sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def serve(cfg, port: int = 8765,
          open_browser: bool = False, app_mode: bool = False) -> None:
    _Handler.cfg = cfg
    host = "127.0.0.1"          # 루프백 고정 — 원격 바인딩 미지원(로컬 1인 도구 전제)
    # 단일 스레드 HTTPServer: Outlook COM 은 스레드마다 초기화가 필요한데, 요청을
    # 이 서빙 스레드에서 처리하므로 여기서 CoInitialize 한 번이면 open/sync 가 동작한다
    # (ThreadingHTTPServer 면 요청 스레드마다 CoInitialize 필요 → 복잡·에러). sqlite
    # 스레드 이슈도 동시 해소. 로컬 1인 도구라 단일 스레드로 충분.
    try:
        import pythoncom  # Windows(pywin32)에서만 존재
        pythoncom.CoInitialize()
        _com = True
    except Exception:
        _com = False
    httpd = HTTPServer((host, port), _Handler)
    # keep-alive 연결: 요청마다 Store 를 열고 닫는데, 그 연결이 '마지막 연결'이면
    # close 시 WAL 체크포인트가 돈다(측정상 요청당 ~1ms 추가·대형 DB 에서 증가).
    # 서버 수명 동안 idle 읽기 연결 하나를 상시 열어두면 요청 close 가 마지막이
    # 아니게 되어 체크포인트 폭주를 막는다(결과 불변 — 그냥 유휴 연결).
    import sys as _sysmod
    try:
        _keepalive = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
    except Exception:
        _keepalive = None
    if _keepalive is not None and _keepalive.recleaned:
        # 절단 규칙 승격의 소급 재절단은 보통 **웹 서버**가 치른다(런처가 먼저
        # 뜬다). 조용히 몇 초 멈추면 원인을 알 길이 없어 한 줄 남긴다.
        # try 밖에 둔다 — 안에 있으면 이 print 의 실수 하나가 except 에 먹혀
        # keepalive 연결까지 조용히 사라진다(자체 검증에서 실제로 겪었다).
        print(f"인용 재절단 {_keepalive.recleaned}건 — 절단 규칙 갱신 소급 적용",
              file=_sysmod.stderr, flush=True)
    import os
    pidfile = cfg.home / "minerva.pid"          # 런처가 재시작 때 옛 서버를 찾아 종료
    try:
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pidfile = None
    url = f"http://{host}:{port}/"
    print(f"Minerva 웹 UI: {url}  (Ctrl-C 로 종료)")
    if open_browser or app_mode:
        _open_ui(url, app_mode, cfg.opt("web", "window_size", default="2000,1200"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        httpd.server_close()
        if _keepalive is not None:
            try:
                _keepalive.close()
            except Exception:
                pass
        if pidfile is not None:
            try:
                if pidfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pidfile.unlink()            # 우리 것일 때만 삭제(새 서버가 덮어썼으면 보존)
            except OSError:
                pass
        if _com:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
