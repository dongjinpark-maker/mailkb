"""Minerva — mailkb 웹 UI (stdlib http.server 기반 로컬 앱, localhost 전용).

서비스 표시명은 Minerva, 코드/폴더/명령은 mailkb 그대로 (표시명만 분리).

브라우저가 한글·HTML 렌더를 담당 → curses(windows-curses)의 CJK 한계를 우회.
표시용 HTML 은 store 에 이미 정제되어 저장됨(clean.sanitize_html) + 여기 CSP 로 이중 방어.

화면: 홈(개입 큐 흡수) · 메일함 · 스레드 · 검색 · 데일리 · 통계 · 설정.
조작(POST): 동기화 · 플래그/숨김/추적 · 노트 · Outlook 열기 · 첨부 · 설정 저장.
"""

from __future__ import annotations

import html as _html
import re
import threading
import urllib.parse
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import __version__, config as cfgmod, report, review
from .clean import (PRESERVED_MARK, QFOLD_CLOSE, QFOLD_OPEN,
                    hide_image_signatures, strip_preserved)
from .store import Store, image_cutoff_for

# 데일리 생성 백그라운드 잡(단일) — 웹은 단일 스레드라 리뷰(수 초~수십 초)는 별 스레드로
# step: 진행 단계(1~total, 0=미상/비-AI) — 대기 화면 프로그레스 바 재료
_review_job = {"running": False, "msg": "", "step": 0, "total": 5}
_review_lock = threading.Lock()


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
  --bg:#fafafa; --surface:#ffffff; --surface-2:#f6f7f9; --surface-3:#eef1f4; --fold:#fbfbfc;
  --ink:#1a1a1a; --ink-2:#555555; --ink-3:#888888; --muted:#aaaaaa;
  --border:#e5e5e5; --border-2:#dddddd; --border-strong:#bbbbbb;
  --accent:#0b6bcb; --accent-strong:#0b4b8f; --sel-bg:#eef6ff; --hover-bg:#f0f6ff; --splitter:#cfe3f7;
  --sel-ring:rgba(11,107,203,.35);
  --danger:#c0392b; --danger-bg:#fdecea;
  --ok:#2c5a2c; --ok-bg:#e7f1e7; --ok-border:#b7d7b7; --toast-bg:#2c5a2c;
  --accent2:#e67e22;
  --warn:#8a6d00; --warn-bg:#fff8e1; --warn-border:#ffe082;
  --sent-bg:#eef6ec; --sent-border:#cfe3cf; --sent-ink:#3f6b3f;
  --analysis-bg:#f0f4f8; --code-bg:#f2f2f2;
}
:root[data-theme='dark'] {
  color-scheme: dark;
  --bg:#16181b; --surface:#212529; --surface-2:#1b1f22; --surface-3:#282d31; --fold:#1e2225;
  --ink:#e6e8ea; --ink-2:#b0b5ba; --ink-3:#868c92; --muted:#6b7178;
  --border:#333a40; --border-2:#3a4147; --border-strong:#4b535a;
  /* 강조 = 따뜻한 코랄 — 다크에서 파랑보다 눈이 편함(라이트는 파랑 유지) */
  --accent:#e8975a; --accent-strong:#f4b183; --sel-bg:#2e2317; --hover-bg:#35291b; --splitter:#5e472c;
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
body { font-family: system-ui, "Segoe UI", "Malgun Gothic", sans-serif;
       margin: 0; padding: 0; display: flex; flex-direction: column;
       line-height: 1.5; color: var(--ink); background: var(--bg); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { display: flex; align-items: baseline; gap: 14px; flex: none;
             border-bottom: 2px solid var(--border-2); padding: 12px 20px 8px; }
header.top .brand { font-weight: 700; font-size: 18px; }
header.top nav { flex: 1; display: flex; align-items: baseline; }
header.top nav a { margin-right: 12px; font-size: 14px; }
header.top nav a.gear { margin-left: auto; margin-right: 0; font-size: 17px;
    text-decoration: none; }
/* 현재 위치한 메뉴 — 밑줄로 표시 */
header.top nav a.active { text-decoration: underline; text-underline-offset: 5px;
    text-decoration-thickness: 2px; font-weight: 700; color: var(--accent-strong); }
/* 설정 페이지 */
.setlist { margin: 8px 0 20px; }
.setrow { display: flex; justify-content: space-between; align-items: center;
    gap: 10px; padding: 7px 10px; margin: 3px 0; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px; }
.setrow .mono { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
.setrow form { margin: 0; }
table.settbl { border-collapse: collapse; margin: 6px 0 20px; font-size: 13.5px; }
table.settbl th, table.settbl td { text-align: left; padding: 4px 14px 4px 0;
    vertical-align: top; }
table.settbl th { color: var(--ink-2); font-weight: 600; }
table.settbl input, table.settbl select { font-size: 13px; padding: 2px 4px; }
.setadd { display: flex; gap: 6px; margin-top: 4px; }
.setadd input[type=text] { flex: 1; font-size: 13px; padding: 4px 6px; }
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
/* 메일함·스레드 공통 필터 바: 탭(좌) + j/k(우) */
.listtabs { display: flex; justify-content: space-between; align-items: baseline;
    gap: 10px; margin: 2px 0 8px; font-size: 13px; flex-wrap: wrap; }
.listtabs .ltabs a { color: var(--accent); } .listtabs .ltabs b { color: var(--ink); }
.listtabs .kbdhint { flex: none; }
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
.mrow { display: block; padding: 7px 10px; margin: 3px 0; border-radius: 8px;
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
.more { text-align: center; padding: 10px 0 16px; color: var(--ink-3); font-size: 13px; }
#toast { position: fixed; bottom: 18px; right: 18px; z-index: 10;
         background: var(--toast-bg); color: #fff; padding: 10px 16px; border-radius: 8px;
         box-shadow: 0 2px 10px rgba(0,0,0,.25); font-size: 14px; }
h1 { font-size: 20px; } h2 { font-size: 17px; margin-top: 22px; }
.lens { display: block; padding: 12px 14px; margin: 8px 0; border: 1px solid var(--border);
        border-radius: 8px; background: var(--surface); }
.lens:hover { border-color: var(--accent); text-decoration: none; }
.lens .q { font-size: 16px; font-weight: 600; color: var(--ink); }
.lens .cnt { float: right; color: var(--ink-2); font-weight: 600; }
.badge-urgent { color: var(--danger); }
.cat { margin: 18px 0 6px; font-weight: 700; }
.item { padding: 7px 10px; margin: 4px 0; border-left: 3px solid var(--border-strong);
        background: var(--surface); border-radius: 0 6px 6px 0; }
.item.hot { border-left-color: var(--danger); }
.item.personal { border-left-color: var(--accent2); }
.item .who { color: var(--ink-2); } .item .day { color: var(--ink-3); font-size: 13px; }
.item .snip { color: var(--ink-2); font-size: 13px; display: block; margin-top: 2px; }
.star { color: var(--accent2); font-weight: 700; }
.dim { color: var(--ink-3); }
/* 홈 '지금 할 일' 배너 + 접이식 개입 + 컴팩트 렌즈 (개입 페이지 흡수) */
.banner { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 9px 12px; margin: 12px 0 6px; }
.banner .big { font-weight: 800; font-size: 16px; margin-right: 4px; }
details.catfold { margin: 8px 0 4px; background: var(--fold); border: 1px solid var(--border);
    border-radius: 6px; padding: 2px 10px; }
.lensrow { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px;
    margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 14px; }
.lensrow .lh { font-weight: 700; color: var(--ink-2); }
.lensrow a { color: var(--accent); text-decoration: none; }
.lensrow a b { color: var(--ink-2); }
.analysis { background: var(--analysis-bg); border-radius: 8px; padding: 12px 14px; margin: 10px 0; }
.analysis .sig { color: var(--warn); } .analysis pre { margin: 4px 0; white-space: pre-wrap; }
.msg { border: 1px solid var(--border); border-radius: 8px; margin: 12px 0; overflow: hidden; }
.msg .mhead { background: var(--surface-3); padding: 6px 12px; font-size: 13px; color: var(--ink-2);
    display: flex; align-items: baseline; gap: 10px; }
.msg .mhead.sent { background: var(--ok-bg); }
.msg .mhead .mh-who { font-weight: 700; color: var(--ink); overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }
.msg .mhead .mh-when { margin-left: auto; flex: none; color: var(--ink-3); font-size: 12px; }
.msg .mbody { padding: 12px 14px; }
.msg .mbody img { max-width: 100%; }
.msg .mbody img[data-blocked-src] { min-width: 8px; min-height: 8px;
    outline: 1px dashed var(--border-strong); }
.msg .mbody table { border-collapse: collapse; }
.msg .mbody td, .msg .mbody th { border: 1px solid var(--border-strong); padding: 4px 8px; }
.msg .mbody blockquote { border-left: 3px solid var(--border-2); margin: 4px 0; padding-left: 10px;
    color: var(--ink-2); }
.imgnote { background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 6px;
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
/* 이미지·인용 표식은 평탄화에서 제외(원래 테마 색 유지) */
:root[data-theme='dark'] .mailhtml img { background: transparent; }
:root[data-theme='dark'] .mailhtml blockquote { border-left-color: var(--border-2) !important;
    color: var(--ink-2) !important; }
:root[data-theme='dark'] .mailhtml details.qfold > summary { color: var(--ink-3) !important; }
/* 이미지 서명 숨김 표식 — 꼬리 로고·명함 카드를 대체한 한 줄 */
.sighide { display: inline-block; font-size: 12px; color: var(--ink-3);
    background: var(--surface-2); border: 1px dashed var(--border); border-radius: 6px;
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
.md-rich pre.md-code { background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px; overflow-x: auto; }
.md-rich pre.md-code code { background: none; padding: 0; }
.md-rich blockquote { border-left: 3px solid var(--border-2); margin: 6px 0; padding-left: 10px;
    color: var(--ink-2); }
.md-rich hr { border: none; border-top: 1px solid var(--border-2); margin: 12px 0; }
.md-rich del { color: var(--ink-3); }   /* 취소선(diff 삭제분) — 흐리게 */
.md-rich table.md-table { border-collapse: collapse; margin: 8px 0; }
.md-rich table.md-table th { background: var(--surface-3); }
.daily { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 4px 18px 16px; }
.daily h2 { font-size: 15px; margin: 20px 0 8px; padding-bottom: 5px;
    border-bottom: 1px solid var(--border); }
.daily ul { margin: 4px 0; padding-left: 20px; }
.daily ul ul { margin: 2px 0; }
.daily li { margin: 3px 0; line-height: 1.55; }
.daily p { margin: 6px 0; color: var(--ink-2); }
form.search { margin: 10px 0; }
form.search input[type=text] { padding: 7px 10px; width: 60%; font-size: 15px;
    border: 1px solid var(--border-strong); border-radius: 6px; }
form.search button { padding: 7px 14px; font-size: 15px; }
.empty { color: var(--ink-3); padding: 20px 0; }
.digest { padding: 4px 10px; margin: 3px 0; border-left: 2px solid var(--border);
    background: var(--surface); font-size: 14px; }
.flash { background: var(--ok-bg); border: 1px solid var(--ok-border); border-radius: 6px;
    padding: 8px 12px; margin: 0 0 12px; color: var(--ok); }
.actions { margin: 10px 0 16px; display: flex; flex-wrap: wrap; gap: 8px;
    align-items: center; }
.actions form { display: inline; margin: 0; }
button { padding: 6px 12px; font-size: 14px; border: 1px solid var(--border-strong);
    border-radius: 6px; background: var(--surface); color: var(--ink); cursor: pointer; }
button:hover { border-color: var(--accent); background: var(--hover-bg); }
button.danger:hover { border-color: var(--danger); background: var(--danger-bg); }
.refine { margin: 8px 0 16px; display: flex; gap: 8px; }
.refine input[type=text] { flex: 1; padding: 7px 10px; font-size: 14px;
    border: 1px solid var(--border-strong); border-radius: 6px; }
details { margin: 10px 0; }
summary { cursor: pointer; font-weight: 600; font-size: 15px; padding: 4px 0; color: var(--ink-2); }
input, select, textarea { background: var(--surface); color: var(--ink); }
/* 결정 원장: 스레드 상세 '결정 기록' 폼 + 검토 큐 버튼 */
.actions details.recdec { margin: 0; }
.actions details.recdec > summary { padding: 6px 12px; font-size: 14px;
    font-weight: 400; color: var(--ink); background: var(--surface);
    border: 1px solid var(--border-strong); border-radius: 6px; list-style: none; }
.actions details.recdec[open] > summary { border-color: var(--accent); }
/* 펼치면 라벨 '장기기억' → '✕ 닫기' (중복 라벨 제거, 접기 유지) */
.actions details.recdec .xcl { display: none; }
.actions details.recdec[open] .lbl { display: none; }
.actions details.recdec[open] .xcl { display: inline; color: var(--ink-3); }
.recdec form { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.recdec input[type=text] { padding: 6px 8px; font-size: 13px;
    border: 1px solid var(--border-strong); border-radius: 6px; }
.recdec input[name=title] { width: 320px; }
.recdec input[name=rationale] { width: 240px; }
.recdec input[name=decider] { width: 110px; }
.decbtns { display: flex; gap: 6px; margin-top: 5px; align-items: baseline;
    flex-wrap: wrap; }
.decbtns form { margin: 0; display: inline; }
.decbtns button { font-size: 12.5px; padding: 3px 10px; }
.decbtns details.decedit { margin: 0; }
.decbtns details.decedit > summary { font-size: 12.5px; font-weight: 400;
    padding: 3px 4px; }
.decedit form { display: flex; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.decedit input[type=text] { padding: 4px 6px; font-size: 13px;
    border: 1px solid var(--border-strong); border-radius: 6px; width: 260px; }
/* '오늘 메일 정리 중' 대기 화면 — 사서가 메일을 장부로 옮겨 적는 루프 (순수 CSS) */
.libscene { position: relative; height: 112px; max-width: 420px; margin: 16px 0 4px; }
.libscene .mailstack { position: absolute; left: 22px; bottom: 14px; }
.libscene .mailstack span { display: block; width: 46px; height: 13px; margin-top: 4px;
    background: var(--surface); border: 1.5px solid var(--border-strong);
    border-radius: 3px; }
.libscene .flymail { position: absolute; left: 28px; bottom: 44px; font-size: 20px;
    color: var(--accent); animation: rv-fly 2.4s ease-in-out infinite; }
@keyframes rv-fly {
  0%   { transform: translate(0, 6px) rotate(0deg); opacity: 0; }
  14%  { transform: translate(24px, -6px) rotate(6deg); opacity: 1; }
  58%  { transform: translate(158px, -34px) rotate(14deg); opacity: 1; }
  82%  { transform: translate(238px, 2px) rotate(3deg); opacity: 0; }
  100% { transform: translate(238px, 2px); opacity: 0; }
}
.libscene .book { position: absolute; right: 26px; bottom: 12px; display: flex; }
.libscene .page { width: 64px; height: 46px; background: var(--fold);
    border: 1.5px solid var(--border-strong); padding: 8px 9px 6px; }
.libscene .page.l { border-radius: 8px 2px 2px 8px; border-right-width: 0.75px; }
.libscene .page.r { border-radius: 2px 8px 8px 2px; border-left-width: 0.75px; }
.libscene .page i { display: block; height: 3px; margin: 5px 0; border-radius: 2px;
    background: var(--accent); opacity: .5; width: 0;
    animation: rv-write 7.2s linear infinite; }
/* 줄마다 지연을 음수로 어긋내 — 항상 어딘가에 '적히는 중'인 줄이 보이게 */
.libscene .page.l i:nth-child(1) { animation-delay: 0s; }
.libscene .page.l i:nth-child(2) { animation-delay: -6s; }
.libscene .page.l i:nth-child(3) { animation-delay: -4.8s; }
.libscene .page.r i:nth-child(1) { animation-delay: -3.6s; }
.libscene .page.r i:nth-child(2) { animation-delay: -2.4s; }
.libscene .page.r i:nth-child(3) { animation-delay: -1.2s; }
@keyframes rv-write {
  0% { width: 0; } 14% { width: 100%; } 92% { width: 100%; } 100% { width: 0; }
}
.libscene .quill { position: absolute; right: 46px; bottom: 46px; font-size: 18px;
    color: var(--ink-2); transform-origin: 20% 90%;
    animation: rv-quill 1.2s ease-in-out infinite; }
@keyframes rv-quill {
  0%, 100% { transform: rotate(-7deg) translateY(0); }
  50%      { transform: rotate(7deg) translateY(-3px); }
}
/* 이미지 보존 기간 경과 마커(프룬 산출) · 메일 내 중복 이미지 생략 표시 */
.imgstrip { background: var(--surface-3); border: 1px dashed var(--border-strong);
    border-radius: 6px; padding: 5px 10px; font-size: 12.5px; color: var(--ink-3);
    margin-bottom: 8px; }
/* mid-join 보존 인용 접힘 — HTML 층(store 저장분)과 텍스트 층(렌더 시 변환) 공용 */
.mbody details.qfold { margin: 10px 0 2px; }
.mbody details.qfold > summary { cursor: pointer; color: var(--ink-3);
    font-size: 12.5px; padding: 4px 0; border-top: 1px dashed var(--border-strong); }
.mbody details.qfold > .qbody { margin-top: 6px;
    border-left: 3px solid var(--border-2); padding-left: 10px; }
.imgnote-inline { display: inline-block; font-size: 12px; color: var(--muted);
    border: 1px dashed var(--border); border-radius: 4px; padding: 1px 6px; }
.rvbar { max-width: 420px; height: 8px; background: var(--surface-3);
    border-radius: 999px; overflow: hidden; margin: 8px 0 6px; }
.rvfill { height: 100%; background: var(--accent); border-radius: 999px;
    transition: width .6s ease; }
.rvfill.indet { width: 38%; animation: rv-indet 1.6s ease-in-out infinite; }
@keyframes rv-indet { 0% { margin-left: -38%; } 100% { margin-left: 100%; } }
@media (prefers-reduced-motion: reduce) {
  .libscene .flymail, .libscene .page i, .libscene .quill, .rvfill.indet {
    animation: none; }
  .libscene .page i { width: 100%; }
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


def _md_inline(text: str) -> str:
    """인라인 마크다운(굵게·#참조)만 HTML 로. escape 먼저."""
    t = esc(text)
    t = _BOLD_RX.sub(r"<strong>\1</strong>", t)
    return _linkify_refs(t)


def _md_to_html(md: str) -> str:
    """데일리 마크다운(앱·AI 생성)을 구조화된 HTML 로 — 다른 페이지와 톤 일치.

    지원: `##` 헤딩, 중첩 불릿(2칸 들여쓰기), `**굵게**`, `#123` 링크.
    맨 위 `#` 한 줄(날짜 제목)은 페이지 h1 과 중복이라 건너뛴다.
    """
    out: list[str] = []
    depth = 0

    def close_lists() -> None:
        nonlocal depth
        while depth > 0:
            out.append("</li></ul>")
            depth -= 1

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped:
            close_lists()
            continue
        if stripped.startswith("## "):
            close_lists()
            out.append(f"<h2>{_md_inline(stripped[3:].strip())}</h2>")
            continue
        if stripped.startswith("# "):
            close_lists()
            continue
        m = _BULLET_RX.match(stripped)
        if m:
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
            out.append(f"<li>{_md_inline(m.group(1))}")
            continue
        close_lists()
        out.append(f"<p>{_md_inline(stripped)}</p>")
    close_lists()
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

    def _row(cells: list[str], tag: str) -> str:
        parts = []
        for j in range(ncol):
            val = cells[j] if j < len(cells) else ""          # 부족한 셀은 빈칸
            al = aligns[j] if j < len(aligns) else ""
            sty = " style='text-align:%s'" % al if al else ""
            parts.append("<%s%s>%s</%s>"
                         % (tag, sty, _mail_md_inline(esc(val)), tag))
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


def build_home(store, cfg, today: str, daily_md: str | None) -> dict:
    """홈 렌더가 쓰는 키만. 결정 렌즈는 md 파싱 대신 원장(decisions)을 직접 조회."""
    missed = review.filtered_unanswered(store, cfg)
    intervention = review.intervention_queue(store, cfg, today, unanswered=missed)
    dc = store.decision_counts()
    return {
        "has_review": daily_md is not None,
        "missed": missed,
        "intervention": intervention,
        "digest": review.today_digest(store, cfg, today),
        "n_dec": dc.get("confirmed", 0),
        "n_dec_pending": dc.get("candidate", 0),
    }


# Outlook 이 본문 붙여넣기 이미지에 자동으로 붙이는 무의미한 첨부 이름 —
# 타임라인 헤더에 노출해도 정보가 없어 표시에서 제외한다 (DB·첨부 추출은 무관)
_NOISE_ATTACH_RX = re.compile(
    r"^(제목\s*없는\s*첨부\s*파일|untitled\s+attachment)", re.IGNORECASE)


def _visible_attach(names: str) -> str:
    """표시용 첨부 이름 — 자동 명명된 인라인 이미지는 걸러낸다."""
    kept = [n.strip() for n in (names or "").split(";")
            if n.strip() and not _NOISE_ATTACH_RX.match(n.strip())]
    return ";".join(kept)


def format_detail(store, thread_id: int) -> dict:
    """디테일 뷰 데이터: 상단 분석 + 하단 메일 타임라인.

    각 타임라인 항목은 표시용 html(정제됨)과 텍스트(html 없을 때 폴백)를 함께 준다.
    """
    t = store.thread(thread_id)
    msgs = store.thread_messages(thread_id)
    if not t or not msgs:
        return {"title": f"#{thread_id}", "analysis": ["(스레드 없음)"], "timeline": []}

    subject = msgs[0]["subject"]
    participants = sorted({m["sender_name"] or m["sender_addr"] for m in msgs})
    me = store.my_addresses
    last = msgs[-1]
    last_to = [a for a in last["to_addrs"].split(";") if a]

    signals: list[str] = []
    # ↩ = 답장 화살표 — 수동 플래그(⚐/⚑)와 글리프 충돌 회피. 개입 큐의
    # '내 응답 대기' 카테고리와 같은 용어(2026-07-12 정렬).
    if not last["is_sent"] and len(last_to) < 3 and (set(last_to) & me):
        signals.append("↩ 내 응답 대기")
    if any(review.DEADLINE_RX.search(strip_preserved(m["new_content"] or ""))
           for m in msgs if not m["is_sent"]):
        signals.append("⏰ 기한/요청")

    analysis = [
        f"제목: {subject}",
        f"기간: {msgs[0]['sent_on'][:10]} ~ {msgs[-1]['sent_on'][:10]}  ·  "
        f"{len(msgs)}통  ·  참여 {len(participants)}명",
        f"참여자: {', '.join(participants)}",
    ]
    if signals:
        analysis.append("신호: " + "  ".join(signals))
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
    return {"title": subject, "analysis": analysis, "timeline": timeline}


_NAV = ('<nav><a href="/">홈</a>'
        '<a href="/mail">메일함</a><a href="/threads">스레드</a>'
        '<a href="/search">검색</a><a href="/records">기록</a>'
        '<a href="/stats">통계</a>'
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


def _head(title: str, refresh: int | None = None, extra_css: str = "",
          read_w: int | None = None, active: str | None = None,
          theme: str = "light") -> str:
    meta_refresh = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    # extra_css 는 _CSS '앞'에 넣는다 — 겹치는 셀렉터(body·h1·header·* 등)는 뒤의
    # _CSS 가 이겨 상단 셸 타이포/헤더가 다른 페이지와 동일하게 유지되고,
    # extra_css 고유 규칙(통계 컴포넌트·CSS 변수)만 추가로 적용된다.
    extra = f"<style>{extra_css}</style>" if extra_css else ""
    # 읽기 창(#right) 너비는 설정값을 CSS 변수로 주입(미지정 시 CSS 기본 1200px).
    rw = f"<style>:root{{--read-w:{int(read_w)}px}}</style>" if read_w else ""
    # 테마는 <html data-theme> 로 — 다크는 :root[data-theme='dark'] 토큰 오버라이드.
    th = "dark" if theme == "dark" else "light"
    return (
        f"<!doctype html><html lang='ko' data-theme='{th}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{meta_refresh}"
        f"<title>{esc(title)} · Minerva</title>{extra}<style>{_CSS}</style>{rw}"
        "</head><body>"
        f"<header class='top'><span class='brand'>Minerva</span>"
        f"{_nav_html(active)}</header>"
    )


def _page(title: str, inner: str, theme: str = "light") -> str:
    """단일 컬럼 페이지 — 차단 안내 등 셸이 필요 없는 특수 응답용."""
    return (_head(title, theme=theme)
            + f"<div id='right' style='flex:1;overflow-y:auto'>"
              f"<div class='inner'>{inner}</div></div></body></html>")


def _page_wide(title: str, inner: str, extra_css: str = "",
               script_src: str | None = None, active: str | None = None,
               theme: str = "light") -> str:
    """상단 nav 셸 + 전폭 단일 컬럼 페이지 (좌/우 분할 없음).

    통계처럼 좌우 프레임이 필요 없는 화면용 — 헤더(Minerva·홈·메일함…)는 다른
    메뉴와 동일하고, 그 아래에 콘텐츠만 전폭으로 스크롤(#right 재사용)된다.
    링크·기간선택 폼은 순수 GET 이라 app.js 없이 일반 이동한다.
    """
    tip = "<div id='tip' role='status'></div>"
    scr = f"<script src='{esc(script_src)}'></script>" if script_src else ""
    return (
        _head(title, None, extra_css, active=active, theme=theme)
        + f"<main id='right'><div class='inner'>{inner}</div></main>"
        + tip + scr + "</body></html>"
    )


def render_stats_page(store, cfg, weeks: int) -> str:
    """통계 전폭 페이지 — 상단 셸은 다른 메뉴와 통일, 본문만 통계."""
    inner = report.render_stats(store, cfg, weeks)
    return _page_wide("통계 분석", inner, extra_css=report.CSS,
                      script_src="/report.js", active="/stats",
                      theme=cfg.opt("web", "theme", default="light"))


def _shell(title: str, left: str, right: str, refresh: int | None = None,
           read_w: int | None = None, theme: str = "light") -> str:
    """Outlook 유사 좌/우 분할 셸 (#14). 콘텐츠 갱신은 /app.js 가 fragment 로."""
    return (
        _head(title, refresh, read_w=read_w, theme=theme)
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
    if (path === "/" || path === "/mail" || path === "/threads" ||
        path === "/search" || path === "/records" || path === "/daily" ||
        path === "/settings") return "left";
    if (path === "/refine" || path === "/lens/intervene" || path === "/person") return "left";
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

  /* ---- 자동 동기화: 주기(분)를 서버에서 받아 백그라운드로 /autosync POST ---- */
  fetch("/syncmin").then(function (r) { return r.text(); }).then(function (s) {
    var min = parseInt(s, 10);
    if (!(min > 0)) return;                    /* 0=끔 */
    setInterval(function () {
      fetch("/autosync", { method: "POST",
        headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (t) {
          var n = parseInt(t, 10);
          if (!(n > 0)) return;                /* 새 메일 없으면 조용히 */
          toast("새 메일 " + n + "통");
          var p = location.pathname.replace(/\/+$/, "") || "/";
          if (p === "/") {                     /* 홈이면 '지금 할 일'만 조용히 갱신 */
            load(location.pathname + location.search, "left", false)
              .catch(function () {});
          }                                    /* 메일함은 스크롤 유지 — 토스트만 */
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

  /* ---- 패널 주입 ---- */
  /* ---- 왼쪽 프레임 자체 히스토리 ('← 뒤로' = 왼쪽의 이전 항목으로) ---- */
  var leftStack = [], leftCur = null, backNav = false;
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
    if (url !== null && url !== undefined) history.pushState({}, "", url);
    if (pane === "left" && url) noteLeft(url);    /* 왼쪽 프레임 히스토리 기록 */
    markSelected();
    markNav();
    hookReviewPolling(el);
    hookMore();
  }

  function markSelected() {
    var m = (location.pathname.match(/^\/thread\/(\d+)/) || [])[1];
    var links = left.getElementsByTagName("a");
    for (var i = 0; i < links.length; i++) {
      var row = links[i].closest(".item, .digest, .mrow");
      if (!row) continue;
      if (m && links[i].getAttribute("href") === "/thread/" + m) {
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
    if (path === "/daily") return "/records";   /* 구 데일리 경로 → 기록 메뉴 */
    var tops = ["/mail", "/threads", "/search", "/records", "/stats", "/settings"];
    for (var i = 0; i < tops.length; i++) {
      if (path === tops[i] || path.indexOf(tops[i] + "/") === 0) return tops[i];
    }
    return null;  /* /thread, /person 등 하위 화면은 직전 메뉴 유지 */
  }
  function markNav() {
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
     진행 중엔 프로그레스(rvfill 폭·#rv-stage 문구)만 패치해 대기 애니메이션
     (.libscene)이 끊기지 않게 하고, 완료 응답이 오면 전체 교체한다. */
  function hookReviewPolling(root) {
    if (!root.querySelector("[data-review-running]")) return;
    setTimeout(function () {
      var u = new URL("/review/status", location.origin);
      u.searchParams.set("frag", "1");
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
          var nf = tmp.querySelector(".rvfill"), of = right.querySelector(".rvfill");
          if (nf && of) { of.className = nf.className; of.style.width = nf.style.width; }
          var ns = tmp.querySelector("#rv-stage"), os = right.querySelector("#rv-stage");
          if (ns && os) os.textContent = ns.textContent;
          hookReviewPolling(right);                 /* 다음 폴링 예약 */
        })
        .catch(function () { hookReviewPolling(right); });
    }, 2000);
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
          fin.searchParams.delete("frag");
          fin.searchParams.delete("msg");
          var p = pane || paneFor(fin.pathname);
          var clean = fin.pathname + (fin.search || "");
          inject(p, html, push === false ? null : clean);
          if (msg) toast(msg);
        });
      });
  }

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
    var btns = form.querySelectorAll("button");
    function setDisabled(v) {
      for (var i = 0; i < btns.length; i++) btns[i].disabled = v;
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
        /* 303 을 따라온 경우만 주소 갱신 — 200 직접 응답(refine)은 유지 */
        inject(p, html, res.redirected ? fin.pathname + (fin.search || "") : null);
        if (msg) toast(msg);
        /* 스레드 상태 변경(플래그·숨김·추적)은 왼쪽 목록에도 즉시 반영 */
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

  /* ---- 브라우저 뒤로/앞으로 — pushState 짝 (없으면 뒤로가기가 죽은 버튼이 된다) */
  window.addEventListener("popstate", function () {
    var p = location.pathname + (location.search || "");
    load(p, null, false).catch(function () { location.reload(); });
  });

  /* ---- 키보드 네비게이션: j/k 로 이동하면 바로 열람(우측에 표시) ---- */
  function navRows() {
    return left ? Array.prototype.slice.call(
      left.querySelectorAll(".mrow, .item, .digest")) : [];
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
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable))
      return;                  /* 입력 중엔 개입 안 함 */
    var k = e.key, rows, i;
    if (k === "j") {
      rows = navRows(); if (!rows.length) return;
      e.preventDefault(); i = curIdx(rows);
      focusRow(rows, i < 0 ? 0 : Math.min(i + 1, rows.length - 1));
    } else if (k === "k") {
      rows = navRows(); if (!rows.length) return;
      e.preventDefault(); i = curIdx(rows);
      focusRow(rows, i < 0 ? rows.length - 1 : Math.max(i - 1, 0));
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
      picks[i].setAttribute("aria-checked", sel ? "true" : "false");
    }
    fetch("/settings/theme", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "fetch" },
      body: "theme=" + encodeURIComponent(val)
    }).catch(function () {});
  });

  leftCur = (paneFor(location.pathname) === "left")
    ? location.pathname + (location.search || "") : "/";
  markSelected();
  markNav();
  hookReviewPolling(right);
  hookMore();
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


def _run_review_job(cfg, ai: bool, today: str) -> None:
    from . import notes
    try:
        store = Store(cfg.db_path, cfg.my_addresses)
        try:
            det = review.deterministic(store, cfg, today)
            ai_text = note = None
            if ai:
                # graceful — AI 실패해도 결정론 리뷰는 반드시 저장 (#10)
                ai_text, note = review.run_ai_layer(
                    store, cfg, det, persist_date=today, progress=_job_progress)
            path = notes.write_daily(cfg, today, review.render(det, ai_text))
            if note:
                msg = f"완료: {path.name} · {note}"
            else:
                msg = f"완료: {path.name}" + (" · AI 분석 포함" if ai else "")
        finally:
            store.close()
    except Exception as e:   # AI 계층은 run_ai_layer 가 삼킴 — 여긴 비-AI 오류
        msg = f"실패: {e!r}"[:200]
    with _review_lock:
        _review_job.update(running=False, msg=msg)


def _start_review(cfg, ai: bool, today: str) -> bool:
    with _review_lock:
        if _review_job["running"]:
            return False
        _review_job.update(running=True, msg="준비 중…", step=0)
    threading.Thread(target=_run_review_job, args=(cfg, ai, today), daemon=True).start()
    return True


# 대기 화면 애니메이션 — 사서가 메일을 한 통씩 장부(장기기억 초안)로 옮겨 적는 루프.
# 순수 CSS(아래 _CSS .libscene)라 CSP 무관. app.js 폴링은 진행부(#rv-meta 내부)만
# 패치해 애니메이션이 끊기지 않는다. 장식용이므로 aria-hidden.
_LIB_SCENE = (
    "<div class='libscene' aria-hidden='true'>"
    "<div class='mailstack'><span></span><span></span><span></span></div>"
    "<div class='flymail'>✉</div>"
    "<div class='book'>"
    "<div class='page l'><i></i><i></i><i></i></div>"
    "<div class='page r'><i></i><i></i><i></i></div></div>"
    "<div class='quill'>✒</div></div>")


def render_review_status(store=None):
    """(inner_html, running) — running 이면 do_GET 이 자동 새로고침 붙임.

    완료 화면은 다음 동선(반영 대기 처리)으로 이어지게 장기기억 제안 링크를 단다."""
    with _review_lock:
        running, msg = _review_job["running"], _review_job["msg"]
        step, total = _review_job["step"], _review_job["total"]
    if running:
        if step:      # AI 단계 진행 중 — 채워지는 바 + "단계 i/N"
            pct = max(6, min(100, round(step * 100 / max(total, 1))))
            fill = f"<div class='rvfill' style='width:{pct}%'></div>"
            label = f"단계 {min(step, total)}/{total} · {esc(msg or '')}"
        else:         # 비-AI(빠름)·시작 직후 — 흐르는 바(indeterminate)
            fill = "<div class='rvfill indet'></div>"
            label = esc(msg or "준비 중…")
        # data-review-running: app.js 폴링 훅 마커 (전체 페이지는 meta refresh)
        return ("<div data-review-running='1' hidden></div>"
                "<h1>오늘 메일 정리 중…</h1>" + _LIB_SCENE +
                f"<div id='rv-meta'><div class='rvbar'>{fill}</div>"
                f"<p class='dim' id='rv-stage'>{label}</p></div>"
                "<p class='dim'>메일을 읽고 장기기억 초안을 준비합니다 — "
                "완료되면 자동 전환.</p>",
                True)
    body = f"<div class='flash'>{esc(msg or '대기 중')}</div>" if msg else ""
    links = ["<a href='/daily'>→ 오늘 데일리 보기</a>"]
    if store is not None:
        pend = store.decision_counts().get("candidate", 0)
        if pend:
            links.insert(0, f"<a href='/records?tab=decisions'>"
                            f"반영 대기 {pend}건 → 기록 › 장기기억</a>")
    return (f"<h1>오늘 메일 정리</h1>{body}"
            "<p>" + " · ".join(links) + " · <a href='/'>홈</a></p>", False)


# ─────────────────────────────────────────────────── 렌즈 렌더

def _digest_details(digest: dict) -> str:
    work = digest.get("work", [])
    excl = []
    if digest.get("n_notice"):
        excl.append(f"공지 {digest['n_notice']}")
    if digest.get("n_spam"):
        excl.append(f"노이즈 {digest['n_spam']}")
    meta = f" · 제외 {' · '.join(excl)}" if excl else ""
    if not work:
        return (f"<details><summary>오늘 메일 핵심 (0){esc(meta)}</summary>"
                "<p class='dim'>오늘 업무 메일 없음</p></details>")
    rows = []
    for it in work:
        arrow = "→ " if it["is_sent"] else ""
        core = it.get("ai_core") or it.get("lead") or ""
        who = (f" <span class='who'>· {esc(it['who'])}</span>"
               if it.get("who") else "")
        rows.append(
            f"<div class='digest'><a href='/thread/{it['thread_id']}'>"
            f"{esc(arrow + it['subject'])}</a>{who}"
            + (f" <span class='snip'>— {esc(core)}</span>" if core else "") + "</div>")
    return (f"<details open><summary>오늘 메일 핵심 ({len(work)}){esc(meta)}</summary>"
            + "\n".join(rows) + "</details>")


def _is_act(it: dict) -> bool:
    """홈 전면('지금 할 일')에 둘 항목: 내 결정 대기 + 나 지목(★) 응답.
    나머지 개입(응답 일반·정체·멈춤)은 '그 외 개입'으로 접는다."""
    return it["category"] == "decide" or (it["category"] == "respond" and it.get("personal"))


def render_home(store, cfg, today: str, refine_note: str | None = None) -> str:
    """홈 = 단일 대시보드. 구 개입 페이지를 흡수해 '지금 할 일'만 전면에 두고
    나머지 개입은 접는다(미니멀). refine_note 가 오면 그 큐로 AI 정리 재실행."""
    m = build_home(store, cfg, today, load_daily(cfg, today))
    base = m["intervention"]
    if refine_note is not None:
        queue = review.ai_refine_intervention(
            store, cfg, base, extra_context=refine_note or None, persist_date=today)
    else:
        queue = review.apply_saved_ai(base, store.load_intervention_ai(today))

    out = [f"<h1>Minerva · {esc(today)}</h1>",
           "<div class='actions'>"
           "<form method='post' action='/sync'><button>메일 동기화</button></form>"
           + _review_button_forms() + "</div>"]

    # ── 지금 할 일 (구 개입 흡수, 미니멀) ──
    if not queue:
        out.append("<p class='empty'>개입할 것 없음 — 큐 깨끗</p>")
    else:
        act = [it for it in queue if _is_act(it)]
        rest = [it for it in queue if not _is_act(it)]
        nd = sum(1 for it in act if it["category"] == "decide")
        nr = len(act) - nd
        sub = [s for s in (f"결정 {nd}" if nd else "", f"나 지목 응답 {nr}" if nr else "") if s]
        out.append("<div class='banner'><span class='big'>지금 할 일 "
                   f"{len(act)}</span>"
                   + (f" <span class='dim'>· {' · '.join(sub)}</span>" if sub else "")
                   + "</div>")
        if act:
            out.extend(_item_html(it) for it in act)
        else:
            out.append("<p class='dim'>지금 바로 할 일 없음 — 아래 <b>그 외 개입</b> 확인</p>")
        if rest:
            counts = " · ".join(f"{label.split()[0]} {c}"
                                for label, c in ((lb, sum(1 for it in rest if it["category"] == k))
                                                 for k, lb in review.CATEGORIES) if c)
            body = []
            for key, label in review.CATEGORIES:
                g = [it for it in rest if it["category"] == key]
                if not g:
                    continue
                body.append(f"<div class='cat'>{esc(label)} ({len(g)})</div>")
                body.extend(_item_html(it) for it in g)
            openattr = " open" if not act else ""
            out.append(f"<details class='catfold'{openattr}>"
                       f"<summary>그 외 개입 ({len(rest)}) <span class='dim'>· {counts}</span></summary>"
                       + "".join(body) + "</details>")
        # AI 정리 (구 개입 페이지에서 홈으로 이전)
        out.append("<form class='refine' method='post' action='/refine'>"
                   f"<input type='text' name='note' value='{esc(refine_note or '')}' "
                   "placeholder='AI 정리 — 추가 정보(선택): 예) ECN은 처리 중, 납기건 우선'>"
                   "<button>AI 정리</button></form>")

    # ── 오늘 메일 핵심 (접기) ──
    out.append(_digest_details(m.get("digest") or {}))

    # ── 장기기억 (컴팩트 한 줄) — 반영된 결정 수 + 반영 대기(제안) 배지 ──
    pend = (f" <span class='dim'>· 제안 {m['n_dec_pending']}</span>"
            if m["n_dec_pending"] else "")
    out.append("<div class='lensrow'><span class='lh'>장기기억</span>"
               f"<a href='/records?tab=decisions'>결정 <b>{m['n_dec']}</b>{pend}</a></div>")
    if not (m["n_dec"] or m["n_dec_pending"]) and not m["has_review"]:
        out.append("<p class='dim'>· 장기기억은 <b>오늘 메일 정리</b>가 제안을 올려 "
                   "채웁니다 (반영은 기록 › 장기기억에서)</p>")
    return "\n".join(out)


def _item_html(it: dict) -> str:
    cls = "item"
    if it["category"] == "decide" or it.get("ai_priority") == "상":
        cls += " hot"
    elif it.get("personal"):
        cls += " personal"
    star = "<span class='star'>★ </span>" if it.get("personal") else ""
    tag = f" {esc(it['tag'])}" if it.get("tag") else ""
    snip = (f"<span class='snip'>「{esc(it['snippet'])}」</span>"
            if it.get("snippet") else "")
    reason = ""
    if it.get("ai_reason"):
        act = f" · 제안: {esc(it['ai_action'])}" if it.get("ai_action") else ""
        reason = f"<span class='snip'>↳ {esc(it['ai_reason'])}{act}</span>"
    return (
        f"<div class='{cls}'>{star}"
        f"<a href='/thread/{it['thread_id']}'>{esc(it['subject'])}</a> "
        f"<span class='who'>· {esc(it['who'])}</span> "
        f"<span class='day'>— {esc(review.day_label(it))}{tag}</span>"
        f"{snip}{reason}</div>"
    )


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


def _more_html(path: str, offset: int) -> str:
    """추가 로딩 센티널 — app.js 가 IntersectionObserver 로 감지해 이어 붙인다.
    JS 꺼짐 폴백은 '더 보기' 링크(전체 페이지 이동). path 에 쿼리가 있으면 '&' 로 연결."""
    sep = "&" if "?" in path else "?"
    return (f"<div class='more' data-more='{path}{sep}offset={offset}'>"
            f"<a href='{path}{sep}offset={offset}'>더 보기</a></div>")


# 메일함·스레드 왼쪽 목록 공통 필터 (양쪽 통일). 순서 = 탭 순서.
# (추적제외 탭은 2026-07-12 폐지 — 숨김이 흡수, 새 수신 시 자동 해제.
#  응답 대기/기한·요청은 스레드 상세의 신호(↩/⏰)와 같은 판정을 필터로 노출.)
_LIST_FILTERS = [("", "전체"), ("unread", "미개봉"),
                 ("awaiting", "↩ 내 응답 대기"), ("deadline", "⏰ 기한/요청"),
                 ("flagged", "🚩 플래그"), ("hidden", "🙈 숨김")]


def _list_flt(qs) -> str:
    """쿼리스트링에서 활성 필터 하나 — 없으면 '' (전체)."""
    for key in ("unread", "awaiting", "deadline", "flagged", "hidden"):
        if (qs.get(key) or [""])[0] == "1":
            return key
    return ""


def _awaiting_thread_ids(store) -> set:
    """'↩ 내 응답 대기' 스레드 — 마지막 메일이 수신이고 To(3인 미만)에 내가 포함.

    스레드 상세의 ↩ 신호와 동일 판정. 숨김 스레드는 제외."""
    me = store.my_addresses
    out: set = set()
    for r in store.db.execute(
            """SELECT t.id, m.is_sent, m.to_addrs FROM threads t
               JOIN messages m ON m.id = (
                 SELECT id FROM messages WHERE thread_id=t.id
                 ORDER BY sent_on DESC, id DESC LIMIT 1)
               WHERE (t.hidden IS NULL OR t.hidden=0)"""):
        if r["is_sent"]:
            continue
        tos = [a for a in (r["to_addrs"] or "").split(";") if a]
        if len(tos) < 3 and set(tos) & me:
            out.add(r["id"])
    return out


def _deadline_thread_ids(store) -> set:
    """'⏰ 기한/요청' 스레드 — 수신 메일 본문에 기한/요청 신호(DEADLINE_RX).

    스레드 상세의 ⏰ 신호와 동일 판정. 숨김 스레드는 제외."""
    out: set = set()
    for r in store.db.execute(
            "SELECT m.thread_id, m.new_content FROM messages m "
            "JOIN threads t ON t.id=m.thread_id "
            "WHERE m.is_sent=0 AND (t.hidden IS NULL OR t.hidden=0)"):
        if r["thread_id"] in out:
            continue
        if review.DEADLINE_RX.search(strip_preserved(r["new_content"] or "")):
            out.add(r["thread_id"])
    return out


def _ids_cond(ids: set, col: str) -> str:
    """id 집합 → SQL IN 조건 (빈 집합이면 항상 거짓)."""
    if not ids:
        return f" AND {col} IN (-1)"
    return f" AND {col} IN ({','.join(str(int(i)) for i in ids)})"


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
    return ("<div class='listtabs'><span class='ltabs'>"
            + " · ".join(parts)
            + "</span><span class='kbdhint'>j/k 이동</span></div>")


def _noise_thread_ids(store, cfg) -> set:
    """'노이즈 스레드' = 비노이즈 수신 메일이 하나도 없는 스레드(수신 전부 노이즈).

    스레드 목록의 일반 탭에서 제외한다 — 메일함이 개별 노이즈 메시지를 빼는 것과
    같은 스코프. 숨김 탭에서는 제외하지 않아 숨긴 노이즈도 여기서 찾아 복구할 수 있다.
    발신만 있는 스레드(수신 0건)는 노이즈로 보지 않는다(내가 시작한 대화).
    """
    recv: set = set()       # 수신 메일이 있는 스레드
    real: set = set()       # 비노이즈 수신 메일이 하나라도 있는 스레드
    for r in store.db.execute(
            "SELECT thread_id, sender_addr, subject FROM messages WHERE is_sent=0"):
        recv.add(r["thread_id"])
        if not (cfg.is_noise(r["sender_addr"])
                or cfg.is_noise_subject_strong(r["subject"])):
            real.add(r["thread_id"])
    return recv - real


def render_mail(store, cfg, offset: int = 0, flt: str = "") -> str:
    """메일함 — 노이즈 제외 수신함, 최신순. 스레드 목록과 같은 필터 바(양쪽 통일).

    flt: '' 전체 | unread 미개봉 | awaiting 응답 대기 | deadline 기한/요청 |
    flagged 플래그 | hidden 숨김. 숨김은 전체/그 외 필터에서 빠지고 'hidden'
    탭에서만. offset>0 이면 조각만 반환.
    """
    # 신호 필터(응답 대기·기한/요청)는 스레드 단위 판정 — 소속 메일을 보여준다
    await_ids = _awaiting_thread_ids(store)
    dead_ids = _deadline_thread_ids(store)
    if flt == "hidden":
        tcond = "t.hidden=1"
    else:
        tcond = "(t.hidden IS NULL OR t.hidden=0)"
        if flt == "unread":
            tcond += " AND (m.read_at IS NULL OR m.read_at='')"
        elif flt == "awaiting":
            tcond += _ids_cond(await_ids, "m.thread_id")
        elif flt == "deadline":
            tcond += _ids_cond(dead_ids, "m.thread_id")
        elif flt == "flagged":
            tcond += " AND t.flagged=1"
    base = "/mail" + (f"?{flt}=1" if flt else "")
    raw = store.db.execute(
        "SELECT m.id, m.thread_id, m.subject, m.sender_name, m.sender_addr, "
        "m.sent_on, m.read_at, t.flagged "
        "FROM messages m JOIN threads t ON t.id=m.thread_id "
        f"WHERE m.is_sent=0 AND {tcond} "
        "ORDER BY m.sent_on DESC, m.id DESC LIMIT ? OFFSET ?",
        (_RAW_BATCH, offset)).fetchall()
    items: list[str] = []
    consumed = 0
    for r in raw:
        consumed += 1
        # 숨김 탭은 복구용 — 노이즈여도 보여준다(전체/미개봉/플래그 탭만 노이즈 제외).
        if flt != "hidden" and (cfg.is_noise(r["sender_addr"])
                                or cfg.is_noise_subject_strong(r["subject"])):
            continue
        badge = "🚩 " if r["flagged"] else ""
        cls = "mrow read" if r["read_at"] else "mrow"   # 읽음=제목 볼드 해제
        items.append(
            f"<a class='{cls}' href='/thread/{r['thread_id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(badge)}{esc(r['subject'])}</span>"
            f"<span class='mdate'>{esc(_fmt_when(r['sent_on']))}</span></span>"
            f"<span class='msubj'>{esc(r['sender_name'] or r['sender_addr'])}</span></a>")
        if len(items) >= _PAGE:
            break
    has_more = ((len(items) >= _PAGE and consumed < len(raw))
                or len(raw) == _RAW_BATCH)
    more = _more_html(base, offset + consumed) if has_more else ""
    if offset:
        return "".join(items) + more
    # 카운트 — 한 번의 스캔(노이즈 필터 반영). 숨김은 total 에서 빠지고 따로 센다.
    total = n_unread = n_await = n_dead = n_flag = n_hidden = 0
    for r in store.db.execute(
            "SELECT m.thread_id, m.sender_addr, m.subject, m.read_at, "
            "t.flagged, t.hidden "
            "FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.is_sent=0"):
        # 숨김은 노이즈 포함해 센다(복구용). 전체/미개봉/… 는 노이즈 제외.
        if r["hidden"]:
            n_hidden += 1
            continue
        if cfg.is_noise(r["sender_addr"]) or cfg.is_noise_subject_strong(r["subject"]):
            continue
        total += 1
        if not r["read_at"]:
            n_unread += 1
        if r["thread_id"] in await_ids:
            n_await += 1
        if r["thread_id"] in dead_ids:
            n_dead += 1
        if r["flagged"]:
            n_flag += 1
    counts = {"": total, "unread": n_unread, "awaiting": n_await,
              "deadline": n_dead, "flagged": n_flag, "hidden": n_hidden}
    body = "".join(items) or "<p class='empty'>수신 메일 없음</p>"
    return ("<h1>메일함</h1>"
            + _list_filter_bar("/mail", flt, counts)
            + f"<div class='mlist'>{body}{more}</div>")


def _thread_span_days(first: str, last: str) -> int:
    """스레드 논의 기간(첫 메일~마지막 메일, 달력일)."""
    try:
        return (date.fromisoformat((last or "")[:10])
                - date.fromisoformat((first or "")[:10])).days
    except ValueError:
        return 0


def render_threads(store, cfg, offset: int = 0, flt: str = "") -> str:
    """스레드 — 메일함과 같은 목록 UI: 제목 [N통] · 마지막 발신인 · 날짜.

    flt: '' 전체 | unread 미개봉 | awaiting 응답 대기 | deadline 기한/요청 |
    flagged 플래그 | hidden 숨김. 메일함과 같은 필터 바(양쪽 통일).
    숨김은 전체/그 외 필터에서 빠지고 숨김 탭에서만.
    """
    # 노이즈 스레드는 일반 탭에서 제외(메일함과 동일 스코프), 숨김 탭에선 유지(복구).
    noise_ids = _noise_thread_ids(store, cfg)
    ncsv = ",".join(str(int(i)) for i in noise_ids)
    nx = f" AND t.id NOT IN ({ncsv})" if noise_ids else ""    # alias t. (행 쿼리·unread)
    nxb = f" AND id NOT IN ({ncsv})" if noise_ids else ""     # bare id (agg: FROM threads)
    await_ids = _awaiting_thread_ids(store)
    dead_ids = _deadline_thread_ids(store)
    if flt == "hidden":
        cond = "WHERE t.hidden=1"
    elif flt == "awaiting":
        cond = ("WHERE (t.hidden IS NULL OR t.hidden=0)"
                + _ids_cond(await_ids, "t.id") + nx)
    elif flt == "deadline":
        cond = ("WHERE (t.hidden IS NULL OR t.hidden=0)"
                + _ids_cond(dead_ids, "t.id") + nx)
    elif flt == "flagged":
        cond = "WHERE t.flagged=1 AND (t.hidden IS NULL OR t.hidden=0)" + nx
    elif flt == "unread":
        cond = ("WHERE (t.hidden IS NULL OR t.hidden=0) AND EXISTS("
                "SELECT 1 FROM messages WHERE thread_id=t.id AND is_sent=0 "
                "AND (read_at IS NULL OR read_at=''))" + nx)
    else:
        cond = "WHERE (t.hidden IS NULL OR t.hidden=0)" + nx
    rows = store.db.execute(
        f"""SELECT t.id, t.flagged, t.hidden, t.first_date, t.last_date,
                  (SELECT subject FROM messages WHERE thread_id=t.id
                     ORDER BY sent_on, id LIMIT 1) AS subject,
                  (SELECT COUNT(*) FROM messages WHERE thread_id=t.id) AS n,
                  (SELECT sender_name FROM messages WHERE thread_id=t.id
                     ORDER BY sent_on DESC, id DESC LIMIT 1) AS last_name,
                  (SELECT sender_addr FROM messages WHERE thread_id=t.id
                     ORDER BY sent_on DESC, id DESC LIMIT 1) AS last_addr,
                  (SELECT sent_on FROM messages WHERE thread_id=t.id
                     ORDER BY sent_on DESC, id DESC LIMIT 1) AS last_on,
                  (SELECT COUNT(*) FROM messages WHERE thread_id=t.id
                     AND is_sent=0 AND (read_at IS NULL OR read_at='')) AS unread_n
           FROM threads t {cond} ORDER BY t.last_date DESC LIMIT ? OFFSET ?""",
        (_PAGE + 1, offset)).fetchall()
    has_more = len(rows) > _PAGE
    rows = rows[:_PAGE]
    items: list[str] = []
    for r in rows:
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
            f"<span class='msubj'>마지막: {esc(r['last_name'] or r['last_addr'] or '')}</span></a>")
    base = f"/threads?{flt}=1" if flt else "/threads"
    more = _more_html(base, offset + _PAGE) if has_more else ""
    if offset:
        return "".join(items) + more
    # total/미개봉/플래그는 노이즈 제외, 숨김(hid)은 노이즈 포함(복구용).
    agg = store.db.execute(
        "SELECT COALESCE(SUM(CASE WHEN (hidden IS NULL OR hidden=0)" + nxb + " THEN 1 ELSE 0 END),0) total, "
        "COALESCE(SUM(CASE WHEN flagged=1 AND (hidden IS NULL OR hidden=0)" + nxb + " THEN 1 ELSE 0 END),0) flag, "
        "COALESCE(SUM(CASE WHEN hidden=1 THEN 1 ELSE 0 END),0) hid FROM threads").fetchone()
    n_unread = store.db.execute(
        "SELECT COUNT(DISTINCT m.thread_id) c FROM messages m "
        "JOIN threads t ON t.id=m.thread_id WHERE m.is_sent=0 "
        "AND (m.read_at IS NULL OR m.read_at='') AND (t.hidden IS NULL OR t.hidden=0)" + nx
    ).fetchone()["c"]
    counts = {"": agg["total"], "unread": n_unread,
              "awaiting": len(await_ids - noise_ids),
              "deadline": len(dead_ids - noise_ids),
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
                     "<button class='iconbtn flag on' title='플래그 해제'>⚑</button></form>")
    else:
        forms.append(f"<form method='post' action='/thread/{tid}/flag'>"
                     "<button class='iconbtn flag' title='플래그'>⚐</button></form>")
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
    # 장기기억 수동 기록 — 사람이 쓰므로 즉시 반영 (기록 › 장기기억에 축적).
    # summary 라벨은 펼치면 '✕ 닫기'로 바뀜(CSS) — 라벨 중복 없이 접기 유지.
    forms.append(
        "<details class='recdec'><summary><span class='lbl'>장기기억</span>"
        "<span class='xcl'>✕ 닫기</span></summary>"
        f"<form method='post' action='/thread/{tid}/record-decision'>"
        "<input type='text' name='title' placeholder='기억할 내용 (필수)'>"
        "<input type='text' name='rationale' placeholder='근거 (선택)'>"
        f"<input type='text' name='decider' value='{esc(decider)}' "
        "placeholder='결정자'>"
        "<button>장기기억에 반영</button></form></details>")
    return f"<div class='actions'>{''.join(forms)}</div>"


def render_thread(store, tid: int) -> str:
    d = format_detail(store, tid)
    t = store.thread(tid)
    out = [f"<h1>{esc(d['title'])}</h1>"]
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
        cls = " class='sig'" if a.startswith(("신호", "[누적", "[롤링")) else ""
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
    for blk, (raw, qtail) in zip(d["timeline"], parts):
        sent = " sent" if blk["is_sent"] else ""
        arrow = "→" if blk["is_sent"] else ""
        att = f" 📎{esc(blk['attach'])}" if blk["attach"] else ""
        # 참여자(발신자) 이름 클릭 → 그 주소와 주고받은 메일 전체(왼쪽). 내 발신은 링크 없음.
        if not blk["is_sent"] and blk.get("sender_addr"):
            who = (f"<a href='/person?addr={_q(blk['sender_addr'])}' "
                   f"title='이 사람과 주고받은 메일'>{esc(blk['sender'])}</a>")
        else:
            who = esc(blk["sender"])
        out.append("<div class='msg'>")
        out.append(
            f"<div class='mhead{sent}'>"
            f"<span class='mh-who'>{arrow} {who}{att}</span>"
            f"<span class='mh-when'>{esc(blk['sent_on'])}</span></div>")
        out.append("<div class='mbody'>")
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
            out.append("<div class='mailhtml'>" + mail_html + "</div>")
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
            # 보존 인용을 HTML 층과 같은 접힘으로 — 서식 렌더(체인은 md 산출물)
            out.append(QFOLD_OPEN + "<div class='md-rich md-show'>"
                       + _mail_md_to_html(qtail) + "</div>" + QFOLD_CLOSE)
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
        return (f"<button type='button' class='themebtn{on}' role='radio' "
                f"aria-checked='{pressed}' data-set-theme='{esc(val)}'>"
                f"{icon}{esc(label)}</button>")
    out.append("<h2>화면 테마</h2>")
    out.append("<p class='dim'>즉시 적용되고 설정에 저장됩니다.</p>")
    out.append("<div class='themepick' role='radiogroup' aria-label='화면 테마'>"
               + _tbtn("light", "라이트", _SUN)
               + _tbtn("dark", "다크", _MOON) + "</div>")

    # ── 차단된 발신인 (편집 가능) ──
    out.append("<h2>차단된 발신인</h2>")
    out.append("<p class='dim'>이 패턴이 발신 주소에 포함되면 신호(개입·미답변·요약·디제스트)에서 "
               "제외됩니다. 실제 수신 차단은 Outlook 규칙으로 병행하세요.</p>")
    if cfg.blocked_senders:
        rows = "".join(
            "<div class='setrow'>"
            f"<span class='mono'>{esc(addr)}</span>"
            "<form method='post' action='/settings/unblock'>"
            f"<input type='hidden' name='addr' value='{esc(addr)}'>"
            "<button class='danger'>차단 해제</button></form></div>"
            for addr in cfg.blocked_senders)
        out.append(f"<div class='setlist'>{rows}</div>")
    else:
        out.append("<p class='empty'>차단된 발신인 없음</p>")

    # ── 판정 기준 (런타임 편집 → overrides.json 영구 저장) ──
    smd = cfg.opt("ai", "summary_max_days", default=1)
    def _num(name, val, note):
        return (f"<tr><th>{esc(note[0])}</th>"
                f"<td><input type='number' name='{name}' value='{esc(str(val))}' "
                f"min='{note[1]}' style='width:70px'></td>"
                f"<td class='dim'>{esc(note[2])}</td></tr>")
    backends = sorted(set(list(cfg.ai_backends) + ["internal", "sonnet", "haiku"]))
    def _sel(name, cur):
        opts = "".join(
            f"<option{' selected' if b == cur else ''}>{esc(b)}</option>"
            for b in backends)
        return (f"<td><select name='{name}'>{opts}</select></td>")
    num_rows = (
        _num("broadcast_to", cfg.broadcast_to,
             ("대량발송 제외선", 1, "수신인 이 수 이상이면 그룹공지로 보고 개입 큐 제외 (broadcast_to)"))
        + _num("direct_to", cfg.direct_to,
               ("직접수신 상한", 0, "수신인 이 수 이하면 '직접 온 메일'로 유지 (direct_to)"))
        + _num("stall_workdays", cfg.stall_workdays,
               ("응답 정체(영업일)", 1, "내가 보낸 메일 무응답이 이 영업일 넘으면 정체 (stall_workdays)"))
        + _num("stale_workdays", cfg.stale_workdays,
               ("스레드 정체(영업일)", 1, "열린 스레드 무활동이 이 영업일 넘으면 정체 (stale_workdays)"))
        + _num("summary_max_days", smd,
               ("요약 창(일)", 1, "매 실행 최근 이 일수까지 소급 요약 (summary_max_days)")))
    out.append("<h2>판정 기준</h2>")
    out.append("<form method='post' action='/settings/save'>"
               "<table class='settbl'>" + num_rows
               + "<tr><th>요약 백엔드</th>" + _sel("summary_backend", cfg.ai_summary_backend)
               + "<td class='dim'>요약/회고/디제스트</td></tr>"
               + "<tr><th>분류 백엔드</th>" + _sel("classify_backend", cfg.ai_classify_backend)
               + "<td class='dim'>개입 큐 '액션 필요?' 분류</td></tr>"
               + "</table><button>저장</button></form>")

    # ── 표시 설정 ──
    rw = cfg.opt("web", "reading_width", default=1200)
    sync_min = cfg.opt("web", "sync_interval_min", default=30)
    img_days = cfg.opt("web", "image_retain_days", default=60)
    out.append("<h2>표시 · 동기화</h2>")
    out.append(
        "<form method='post' action='/settings/save'>"
        "<table class='settbl'>"
        "<tr><th>읽기 창 너비(px)</th>"
        f"<td><input type='number' name='reading_width' value='{esc(str(rw))}' "
        "min='600' step='20' style='width:80px'></td>"
        "<td class='dim'>오른쪽 메일 확인창 본문 최대 폭 (기본 1200, 변경 후 새로고침 시 전체 반영)</td></tr>"
        "<tr><th>자동 동기화(분)</th>"
        f"<td><input type='number' name='sync_interval_min' value='{esc(str(sync_min))}' "
        "min='0' step='5' style='width:80px'></td>"
        "<td class='dim'>이 주기마다 백그라운드로 메일 수집 (기본 30 · 0=끔 · 새 메일 오면 알림)</td></tr>"
        "<tr><th>이미지 보존(일)</th>"
        f"<td><input type='number' name='image_retain_days' value='{esc(str(img_days))}' "
        "min='0' step='1' style='width:80px'></td>"
        "<td class='dim'>인라인 이미지·서식 HTML 보존 기간 (기본 60 · 0=임베드 끔). "
        "경과분은 텍스트로 압축 — 늘려도 지난 것은 sync --full 로만 복구</td></tr>"
        "</table><button>저장</button></form>")

    # ── 노이즈 규칙 (발신자·제목) ──
    out.append("<h2>노이즈 규칙</h2>")
    out.append("<p class='dim'>이 패턴에 걸리면 개입·미답변·요약에서 제외됩니다.</p>")
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
        f"<p class='dim'>Minerva (mailkb) v{esc(__version__)} · "
        "<a href='https://github.com/dongjinpark-maker/mailkb' "
        "target='_blank' rel='noopener noreferrer'>GitHub</a> · "
        "MIT © 2026 Dongjin Park</p>")
    out.append("</div>")
    return "\n".join(out)


_SETTINGS_INTS = [   # (폼 필드, 오버라이드 섹션, 키, 최소값)
    ("broadcast_to", "review", "broadcast_to", 1),
    ("direct_to", "review", "direct_to", 0),
    ("stall_workdays", "review", "stall_workdays", 1),
    ("stale_workdays", "review", "stale_workdays", 1),
    ("summary_max_days", "ai", "summary_max_days", 1),
    ("reading_width", "web", "reading_width", 600),
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
    for field_, key in [("summary_backend", "summary"), ("classify_backend", "classify")]:
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
                     "<button class='danger'>발신자 차단</button></form>")
    # 한 줄(같은 높이): ← 뒤로(왼쪽) · 이름(가운데) · 발신자 차단(오른쪽)
    out = ["<div class='personhead'>"
           "<a href='#' class='backlink'>← 뒤로</a>"
           f"<span class='ptitle'>{esc(name)}</span>"
           f"<span class='pright'>{block_ctl}</span></div>",
           f"<p class='dim'>전체 {len(rows)} (양방향) · "
           f"<span class='mono'>{esc(addr)}</span> · "
           "<span class='kbdhint'>→ 표시·배경색 = 내가 보낸 메일 · j/k 이동</span></p>"]
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
            f"<a class='{cls}' href='/thread/{r['thread_id']}'>"
            f"<span class='mtop'><span class='mfrom'>{esc(subj)}</span>"
            f"<span class='mdate'>{esc(_fmt_when(r['sent_on']))}</span></span>"
            f"<span class='msubj'>{esc(sub_who)}</span></a>")
    out.append(f"<div class='mlist'>{''.join(items)}</div>")
    return "\n".join(out)


def render_search(store, q: str) -> str:
    form = ("<form class='search' method='get' action='/search'>"
            f"<input type='text' name='q' value='{esc(q)}' autofocus "
            "placeholder='검색어'> <button>검색</button></form>")
    out = [f"<h1>검색</h1>", form]
    if q:
        rows = store.search(q, limit=50)
        out.append(f"<p class='dim'>{len(rows)}건</p>")
        for r in rows:
            arrow = "→" if r["is_sent"] else ""
            out.append(
                f"<div class='item'><a href='/thread/{r['thread_id']}'>"
                f"{esc(r['subject'])}</a> <span class='who'>· {arrow} "
                f"{esc(r['sender_name'])}</span> "
                f"<span class='day'>{esc(r['sent_on'][:16])}</span></div>")
        if not rows:
            out.append("<p class='empty'>결과 없음</p>")
    return "\n".join(out)


def _review_button_forms() -> str:
    # '오늘 메일 정리' = AI 5단계(요약·수확·디제스트·분류·주석) 포함 데일리 생성.
    # '기록만 남기기' = AI 없이 결정론 스냅샷만 (Phase 2 에서 축소 여부 판단).
    return ("<form method='post' action='/review'><input type='hidden' name='ai' value='1'>"
            "<button>오늘 메일 정리</button></form>"
            "<form method='post' action='/review'><button>기록만 남기기</button></form>")


def render_daily(cfg, day: str, today: str | None = None) -> str:
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
    out = [f"<h1>데일리 리뷰 · {esc(day)}</h1>", nav,
           "<div class='actions'>" + _review_button_forms() + "</div>"]
    if md is None:
        out.append("<p class='empty'>해당 날짜 리뷰 없음 — "
                   "<code>review [--ai] --date " + esc(day) + "</code></p>")
    else:
        out.append(_md_to_html(md))
    return "\n".join(out)


# ────────────────── 기록 (데일리·장기기억 — 주간/분기는 Phase 2)
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
            "<button>반영</button></form>"
            f"<form method='post' action='/decision/{did}/reject'>"
            "<button class='danger'>유보</button></form>"
            "<details class='decedit'><summary class='dim'>수정 후 반영</summary>"
            f"<form method='post' action='/decision/{did}/amend'>"
            f"<input type='text' name='title' value='{esc(r['title'])}'>"
            f"<input type='text' name='rationale' value='{esc(r['rationale'])}' "
            "placeholder='근거'>"
            "<button>반영</button></form></details>"
            "</div>")
    elif flip == "reject":
        ctl = ("<div class='decbtns'>"
               f"<form method='post' action='/decision/{r['id']}/reject'>"
               "<button class='danger' title='장기기억에서 빼서 유보로'>유보</button>"
               "</form></div>")
    elif flip == "confirm":
        ctl = ("<div class='decbtns'>"
               f"<form method='post' action='/decision/{r['id']}/confirm'>"
               "<button title='다시 장기기억에 반영'>반영</button></form></div>")
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
           "<p class='dim'>메일에서 건진 결정의 영구 기억 — '오늘 메일 정리'가 "
           "반영문 초안을 제안하고, 반영/유보는 사람이 정합니다. 반영된 것만 "
           "검색·회고 재료가 됩니다.</p>"]
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
               "placeholder='결정·근거·결정자 검색'> <button>검색</button></form>")
    rows = store.decisions(status=st, q=q)
    if rows:
        # 반영 목록엔 '유보'(빼기), 유보 목록엔 '반영'(복원) — 오클릭 상호 복구 가능
        flip = "reject" if st == "confirmed" else "confirm"
        out.extend(_decision_row(r, flip=flip) for r in rows)
    else:
        out.append("<p class='empty'>해당 항목 없음</p>")
    return "\n".join(out)


def render_records(store, cfg, qs, today: str) -> str:
    """기록 페이지 — 탭: 데일리 | 장기기억. (주간·분기 탭은 Phase 2 에서 추가)"""
    tab = (qs.get("tab") or ["daily"])[0]
    if tab not in ("daily", "decisions"):
        tab = "daily"
    counts = store.decision_counts()
    cand = counts.get("candidate", 0)
    dec_label = (f"장기기억 {counts.get('confirmed', 0)}"
                 + (f" · 제안 {cand}" if cand else ""))
    tabs = []
    for key, label in (("daily", "데일리"), ("decisions", dec_label)):
        if key == tab:
            tabs.append(f"<b>{esc(label)}</b>")
        else:
            tabs.append(f"<a href='/records?tab={key}'>{esc(label)}</a>")
    bar = ("<div class='listtabs'><span class='ltabs'>"
           + " · ".join(tabs) + "</span></div>")
    if tab == "decisions":
        return bar + render_decisions(store, qs)
    return bar + render_daily(cfg, (qs.get("date") or [today])[0], today)


# ─────────────────────────────────────────────────── 조작(POST) 동작

def perform_action(store, cfg, path: str, form: dict) -> str:
    """상태 변경 동작 실행 → 리다이렉트할 위치(?msg= 포함) 반환. 소켓 무관(테스트 대상).

    Outlook COM(open/outlook sync)은 Windows 서버 스레드에서 실행된다.
    """
    parts = path.strip("/").split("/")

    if path == "/sync":
        from .sources import get_source
        src = get_source(cfg.source)                 # outlook 이면 Windows COM
        retain = int(cfg.opt("web", "image_retain_days", default=60) or 0)
        cutoff = image_cutoff_for(retain)
        try:
            stats = store.ingest(src.fetch(store.last_sync(), image_cutoff=cutoff),
                                 image_cutoff=cutoff)
        finally:
            # 프룬은 COM 불필요 — 수집이 실패(Outlook 꺼짐 등)해도 실행
            store.maybe_prune_html(retain)
        return "/?msg=" + _q(f"동기화({src.name}): 신규 {stats.inserted} · 중복 {stats.skipped}")

    if path == "/settings/unblock":
        addr = (form.get("addr") or [""])[0].strip()
        if addr and cfgmod.remove_blocked(cfg, addr):
            return "/settings?msg=" + _q(f"차단 해제: {addr}")
        return "/settings?msg=" + _q("해제할 항목 없음")

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
    if path == "/":
        return "홈", render_home(store, cfg, today), 200, "left"
    if path == "/lens/intervene":     # 구 개입 페이지 → 홈으로 흡수(구 링크·북마크 호환)
        return "홈", render_home(store, cfg, today), 200, "left"
    if path == "/mail":
        return "메일함", render_mail(store, cfg, _offset(qs), _list_flt(qs)), 200, "left"
    if path == "/threads":
        return "스레드", render_threads(store, cfg, _offset(qs), _list_flt(qs)), 200, "left"
    if path == "/person":
        # 스레드에서 이름 클릭 → 주소별 메일(왼쪽 목록 프레임). 발신자 차단도 여기.
        return "주소별 메일", render_person(store, cfg, (qs.get("addr") or [""])[0]), 200, "left"
    if path == "/search":
        return "검색", render_search(store, (qs.get("q") or [""])[0].strip()), 200, "left"
    if path == "/settings":
        return "설정", render_settings(store, cfg), 200, "left"
    if path in ("/records", "/daily"):
        # 기록(데일리·결정 원장). /daily 는 구 메뉴 경로 — 북마크 호환 흡수.
        return "기록", render_records(store, cfg, qs, today), 200, "left"
    if path == "/review/status":
        inner, running = render_review_status(store)
        return "정리", inner, 200, "right"
    if path.startswith("/thread/"):
        try:
            tid = int(path.split("/")[2])
        except (IndexError, ValueError):
            return "404", "<p>잘못된 스레드</p>", 404, "right"
        store.mark_thread_read(tid)   # 열람 = 읽음 (다음 목록 렌더에 반영)
        return "스레드", render_thread(store, tid), 200, "right"
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
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _panes(self, store, inner: str, pane: str, today: str) -> tuple[str, str]:
        """요청된 패널 외의 기본 콘텐츠 — 좌측 기본은 홈, 우측은 읽기(상세) 패널.

        좌측은 상단 메뉴 콘텐츠(홈/메일함/스레드/검색/기록), 우측은
        스레드·메일 상세를 여는 읽기 영역. 데일리도 좌측 메뉴라 우측 기본은
        데일리가 아니라 안내 문구(이전엔 우측 기본이 데일리라 데일리 메뉴가
        좌우 중복이 됐음)."""
        if pane == "left":
            return inner, _READING_HINT
        return render_home(store, self.cfg, today), inner

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
        frag = (qs.get("frag") or [""])[0] == "1"
        today = date.today().isoformat()
        store = Store(self.cfg.db_path, self.cfg.my_addresses)
        try:
            if path == "/stats":
                # 통계 분석 — 좌/우 셸 대신 전폭 단일 컬럼이되, 상단 nav 셸은
                # 다른 메뉴와 동일(Minerva·홈·메일함…). 기간(2/4/8/16주) 선택은
                # GET 재요청으로 매번 재분석.
                weeks = report.clamp_weeks((qs.get("weeks") or [""])[0])
                self._send_html(render_stats_page(store, self.cfg, weeks))
                return
            title, inner, code, pane = route(store, self.cfg, path, qs, today)
            # 전체 페이지 모드의 리뷰 상태는 meta refresh 로 자동 새로고침 (JS 꺼짐 폴백)
            refresh = 2 if (path == "/review/status"
                            and "data-review-running" in inner and not frag) else None
            msg = (qs.get("msg") or [""])[0]
            if msg and not frag:
                # fragment 모드는 JS 토스트가 msg 를 표시 — flash 중복 방지
                inner = f"<div class='flash'>{esc(msg)}</div>" + inner
            if frag:
                body = inner
            else:
                left, right = self._panes(store, inner, pane, today)
                rw = self.cfg.opt("web", "reading_width", default=1200)
                theme = self.cfg.opt("web", "theme", default="light")
                body = _shell(title, left, right, refresh, read_w=rw, theme=theme)
        except Exception as e:  # 죽지 않게
            inner = f"<pre>{esc(repr(e))}</pre>"
            code = 500
            th = self.cfg.opt("web", "theme", default="light")
            body = inner if frag else _shell(
                "오류", "<p class='empty'>오류</p>", inner, theme=th)
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
        if path == "/autosync":           # app.js 백그라운드 주기 동기화 → 신규 통수만 반환
            n = 0
            store = Store(self.cfg.db_path, self.cfg.my_addresses)
            retain = int(self.cfg.opt("web", "image_retain_days",
                                      default=60) or 0)
            try:
                from .sources import get_source
                src = get_source(self.cfg.source)
                cutoff = image_cutoff_for(retain)
                n = store.ingest(src.fetch(store.last_sync(),
                                           image_cutoff=cutoff),
                                 image_cutoff=cutoff).inserted
            except Exception:             # 자동 동기화 실패는 조용히(다음 주기 재시도)
                n = 0
            try:
                store.maybe_prune_html(retain)   # 프룬은 COM 불필요 — 항상 시도
            except Exception:
                pass
            finally:
                store.close()
            body = str(n).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        today = date.today().isoformat()
        store = Store(self.cfg.db_path, self.cfg.my_addresses)
        try:
            if path in ("/refine", "/lens/intervene/refine"):  # AI 정리 = 홈 인라인 렌더(200 직접)
                note = (form.get("note") or [""])[0].strip()
                inner = render_home(store, self.cfg, today, refine_note=note)
                if self._is_fetch():
                    self._send_html(inner)             # fragment — 좌측에 주입됨
                else:
                    left, right = self._panes(store, inner, "left", today)
                    rw = self.cfg.opt("web", "reading_width", default=1200)
                    th = self.cfg.opt("web", "theme", default="light")
                    self._send_html(_shell("홈", left, right, read_w=rw, theme=th))
                return
            if path == "/review":                     # 데일리 생성(백그라운드)
                ai = bool((form.get("ai") or [""])[0])
                _start_review(self.cfg, ai, today)
                location = "/review/status"
            elif path in ("/settings/save", "/settings/noise"):
                # 오버라이드 파일 저장 후 cfg 재로드 → 즉시 반영(다음 요청부터)
                home = self.cfg.home
                location = (_save_settings(home, form) if path == "/settings/save"
                            else _save_noise(self.cfg, form))
                _Handler.cfg = cfgmod.load(home)
            else:
                location = perform_action(store, self.cfg, path, form)
        except Exception as e:
            location = "/?msg=" + _q(f"실패: {e!r}")
        finally:
            store.close()
        if self._is_fetch():
            location = _with_frag(location)           # fetch 는 fragment 를 따라감 (#16)
        self.send_response(303)
        self.send_header("Location", location)
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
        if _com:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
