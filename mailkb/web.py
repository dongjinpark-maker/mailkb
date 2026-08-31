"""Minerva — mailkb 웹 UI (stdlib http.server 기반 로컬 앱, localhost 전용).

서비스 표시명은 Minerva, 코드/폴더/명령은 mailkb 그대로 (표시명만 분리).

브라우저가 한글·HTML 렌더를 담당 → curses(windows-curses)의 CJK 한계를 우회.
표시용 HTML 은 store 에 이미 정제되어 저장됨(clean.sanitize_html) + 여기 CSP 로 이중 방어.

화면: 분석(첫 화면 — 근거 달린 질의응답) · 메일함 · 스레드 · 인물 · 기억 · 통계.
      검색은 헤더 입력창, 설정은 헤더 ⚙ (2026-07-26 홈=분석 개편).
조작(POST): 분석 잡 생성 · 동기화 · 플래그/숨김/신호 해제 · 노트 · Outlook 열기 ·
      첨부 · 지식 저장/유보/외부 편집기 열기 · 설정 저장.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import (__version__, actions, config as cfgmod, promises, report,
               review, search as search_mod, terms, weekly)
from .clean import (PRESERVED_MARK, QFOLD_CLOSE, QFOLD_OPEN, add_dark_colors,
                    hide_image_signatures, parse_preserved, preserved_label,
                    qfold_open, retitle_qfold, smart_truncate, strip_preserved)
from .store import Store, image_cutoff_for

# 백그라운드 잡 상태 표준형 — 모든 잡(회고·검색·동기화·주간·분석·인물 요약·지식 저장)이
# 같은 모양을 쓰고 같은 대기 카드로 그려진다. 스트리밍 필드는 ai_run 이 흘리는
# 수신 상태이고(phase/recv/model/retry/tail/failed/last_ev), cancel 은 실행 중일
# 때만 threading.Event — 중지 버튼이 set 하면 스트리밍 루프가 0.5초 안에
# 프로세스를 죽이고 AICancelled 를 올린다. stream 은 그 즉시성이 성립하는
# 백엔드인지(_arm_job_backend 가 판정). AI 아닌 잡도 같은 형태를 쓰되 이벤트가
# 없어 슬롯이 비고, CSS `.waitslot:empty` 가 그 줄을 숨긴다.
_JOB_STREAM = {"phase": "", "recv": 0, "model": "", "retry": "", "tail": "",
               "failed": "", "fatal": False, "last_ev": 0.0, "stream": False,
               "cancel": None, "calls": 0,
               # 무수신 경고 기준(초). 0 = 미지정이고 _job_live_line 이 기본값을
               # 쓴다 — _arm_job_backend 가 백엔드를 보고 실제 값을 심는다.
               # 첫 이벤트까지의 '정상 침묵'이 백엔드마다 다르기 때문이다.
               "stall": 0,
               "started": 0.0}


# 진행 중 화면은 JS 꺼짐 폴백으로 meta refresh 자동 새로고침(전체 페이지 모드에서만).
# **새 백그라운드 잡을 만들면 여기 마커도 함께 넣는다** — 빠지면 JS-off 환경에서
# 그 화면만 영영 안 넘어간다(주간 보고·분석이 실제로 그랬다).
_RUNNING_MARKERS = ("data-review-running", "data-aisearch-running",
                    "data-sync-running", "data-weekly-running",
                    "data-ask-running", "data-dossier-running",
                    "data-kn-running", "data-diag-running",
                    "data-pdiag-running", "data-aitest-running")


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
_review_job = _new_job(msg="", step=0, total=3, date="", ai=False)
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

# 주간 보고 백그라운드 잡(단일) — 본문·머리글·누락으로 AI 최대 3콜
# (weekly.MAX_AI_CALLS). 요청 스레드에서 돌리면 단일 스레드 서버가 그동안 멈춘다.
# /weekly/status 폴링으로 진행("원문 읽고 토픽 쓰는 중…")을 흘린다.
_WEEKLY_ETA_PER_WEEK = (8, 13)    # 주당 하한·상한(분) — 아래 실측에서 나온 값


def _weekly_eta(weeks: int = 1) -> str:
    """주간 보고 소요 시간 안내 — **여기 한 곳에서만** 만든다.

    2026-07-29 에 대기 카드가 "1~3분", 진입 문구가 "2~5분" 이라 같은 작업을 두
    화면이 다르게 말했고, 그래서 시간 표기를 통째로 없앴었다. 실측으로 근거가
    생겨 되살리되 만드는 자리를 하나로 묶는다.

    실측(데모·sonnet, 3콜 재설계 뒤 2026-08-24):
      1주(36스레드) 458 · 538 · 623초   → 7.6~10.4분
      2주(50스레드) 873 · 1,555초       → 14.6~25.9분
    **같은 프롬프트가 2.2배까지 흔들린다**(2주 본문 콜 616초 대 1,363초, 느린
    쪽이 산출은 더 적었다). 그래서 값이 아니라 범위로 말한다.

    기간에 비례하는 것은 재료가 아니라 **산출량**이다 — 1주 41KB → 2주 48KB
    (+17%)인데 토픽 불릿은 46 → 65~76 이었고 시간은 1.4~2.5배가 됐다. 시간은
    출력량에 비례하므로(실측 25자/초) 주 수에 거의 비례한다.

    스레드 수와는 거의 무관하다 — 주 2,000통(777스레드 → 재료 예산이 147건
    선택)도 517초였다. 재료 예산이 프롬프트를 묶어 주기 때문이다.

    3주 이상은 미측정이다. 주당 값을 그대로 늘려 잡는다 — 틀리더라도 **과대
    추정 쪽이 안전하다**(짧게 말하면 멈춘 줄 알고 중지를 누른다)."""
    lo, hi = _WEEKLY_ETA_PER_WEEK
    w = max(1, int(weeks or 1))
    return f"보통 {lo * w}~{hi * w}분"
_weekly_job = _new_job(weeks=1, date="")
_weekly_lock = threading.Lock()

# 질문하기 백그라운드 잡(단일) — AI 최대 12콜(조사·답변·검증·조건부 보정). 진행 문구는
# ask 엔진이 내보내는 것("조사 2라운드 — 검색 1회 · 정독 3통")을 그대로 흘린다.
_ask_job = _new_job(question="", parent=None, person="", token="",
                    mail=None, thread=None, result=None)
_ask_lock = threading.Lock()

# 인물 요약(도시에) 백그라운드 잡(단일) — AI 1콜. 인물 화면 '요약 갱신' 버튼에서만
# 시작한다(2026-07-29 이전에는 일간 회고가 상위 6명을 배치로 갱신했다 — 하루 정리와
# 인물 카드 유지보수를 한 버튼에 묶지 않기 위해 분리).
_dossier_job = _new_job(addr="", name="", done_at=0.0)
_dossier_lock = threading.Lock()

# 지식 저장 백그라운드 잡 — 보강 AI 1콜(수십 초). 요청 스레드에서 돌리면
# 단일 스레드 서버가 통째로 멈춘다(실측: 20초 보강 동안 홈 GET 이 19초 대기,
# 2026-08-14 사용자 보고). 실시간 진행이 필요 없는 작업이라 스트림 없이 완료
# 메시지만 남기고, 회고 화면 폴링이 카드를 갈아 끼운다.
#
# **여기만 대기열이 있다**(2026-08-27). 후보는 회고 화면에 여러 개가 한꺼번에 뜨고
# 지식 탭은 날짜를 가리지 않고 전체 pending 을 보여 준다 — "여러 개를 처리해 둬라"가
# 자연스러운 화면인데 단일 슬롯이 두 번째 클릭을 거절했다. 다른 잡(현안 브리핑·인물
# 요약 등)은 "지금 이 화면을 보고 싶다"는 성격이라 줄을 세우지 않는다.
#   queue — 대기 중인 후보 id, 넣은 순서
#   done  — 이번 배수에서 끝난 결과 문구(화면이 끝난 것부터 쌓아 보여 준다)
#   msg   — 가장 최근 결과 하나(종전 필드 유지)
_kn_job = _new_job(cid=0, day="", msg="", queue=[], done=[])
_kn_lock = threading.Lock()

# 인물 진단 백그라운드 잡(단일, 2026-08-18) — 스레드 진단과 같은 모양을 사람
# 축으로. 기존 [대화 분석](조사 엔진)은 10분이 걸려 미팅 직전에 못 쓴다.
_pdiag_job = _new_job(addr="", msg="")
_pdiag_lock = threading.Lock()

# 스레드 진단 백그라운드 잡(단일) — 진단이 만들어지는 곳은 스레드 화면의 이
# 버튼 하나다(2026-08-15 에 회고에서 분리).
# AI 1콜이라 요청 스레드에서 돌리면 단일 스레드 서버가 그동안 멈춘다
# (지식 저장과 같은 판례).
_diag_job = _new_job(tid=0, msg="")
_diag_lock = threading.Lock()

# 설정 › AI 백엔드 상태의 [응답 시험] 잡(단일, 2026-08-19) — doctor 는 실행 파일이
# PATH 에 있는지까지만 본다. "그 CLI 가 이 모델을 실제로 부를 수 있는가"는 불러 봐야
# 알고(현안 브리핑의 opus 가 대표적), 웹만 쓰는 사용자는 터미널의 `mailkb diagnose`
# 를 돌리지 않는다. PATH 에 있는 백엔드마다 1콜이라 요청 스레드에서 돌리면 단일
# 스레드 서버가 그동안 멈춘다.
_aitest_job = _new_job(rows=None, at="")
_aitest_lock = threading.Lock()

# 원격 이미지(2026-08-15) — **서버는 받아오지 않는다.** 저장 HTML 에는
# data-blocked-src 로 URL 만 남아 있고, 사용자가 [위험을 감수하고 보기]를 누르면
# 그 화면에서만 src 를 되돌리고 CSP 의 img-src 를 풀어 **브라우저가 직접** 받는다.
#
# 서버 프록시를 짧게 갖고 있다가 걷어냈다: 사내망은 직접 나가는 길을 막아 프록시
# 경유가 필요했는데, 프록시를 거치면 목적지 IP 를 프록시가 해석해 SSRF 방어의
# 근거(접속한 실제 피어 IP 검사)가 무너지고 PAC·프록시 인증·사내 루트 인증서가
# 줄줄이 따라온다. 얻는 것은 뉴스레터 배너·로고 정도고(업무 그림은 첨부·cid 로
# 이미 보인다), 브라우저는 시스템 프록시를 써서 그냥 된다. 방어를 낮춰야만 되는
# 기능은 넣지 않는다 — 그래서 서버의 아웃바운드는 0 으로 돌아왔다(AI subprocess 뿐).
_IMG_MIN_PX = 5                      # 이보다 작게 선언된 이미지는 추적 픽셀로 본다
_IMG_TAG_RX = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_BLOCKED_RX = re.compile(r'data-blocked-src="(https?://[^"]+)"', re.IGNORECASE)


def _img_too_small(tag: str) -> bool:
    """선언 치수가 _IMG_MIN_PX 미만이면 추적 픽셀 — 눌러도 안 보여준다.

    한 변만 작아도 뺀다(1×400 스페이서도 볼 것이 없다). 치수를 아예 선언하지
    않은 이미지는 알 수 없으므로 보여준다 — 그것까지 빼면 정상 이미지가 사라진다.
    """
    for attr in ("width", "height"):
        m = re.search(rf'\b{attr}="(\d+)"', tag)
        if m and int(m.group(1)) < _IMG_MIN_PX:
            return True
    return False


def _remote_imgs(html: str) -> list:
    """되살릴 수 있는 원격 이미지 URL — 배너 개수와 src 복원이 같은 목록을 쓴다.

    추적 픽셀은 제외한다(클릭이 곧 수신 확인이 되면 차단의 목적이 무너진다).
    순서 보존 중복 제거."""
    out, seen = [], set()
    for m in _IMG_TAG_RX.finditer(html or ""):
        tag = m.group(0)
        um = _IMG_BLOCKED_RX.search(tag)
        if not um or _img_too_small(tag):
            continue
        url = _html.unescape(um.group(1))    # 저장은 _attr_esc — DOM 과 같은 복원
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def show_remote_images(html: str) -> str:
    """data-blocked-src → src (추적 픽셀은 그대로 둔다). `?images=1` 화면 전용.

    되돌린 HTML 은 저장하지 않는다 — 이 요청의 렌더에만 쓴다. 그래야 '보기'가
    한 화면의 선택으로 남고, 다음에 열면 다시 차단된 상태에서 시작한다."""
    def one(m):
        tag = m.group(0)
        if _img_too_small(tag) or not _IMG_BLOCKED_RX.search(tag):
            return tag
        return _IMG_BLOCKED_RX.sub(
            lambda u: 'src="%s"' % u.group(1), tag, count=1)
    return _IMG_TAG_RX.sub(one, html or "")


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
# [위험을 감수하고 보기] 화면에만 쓰는 완화본 — **img-src 하나만** 푼다.
# 스크립트·외부 CSS·fetch 는 그대로 막히고, 서버는 여전히 밖으로 안 나간다
# (이미지를 받는 것은 브라우저다 — 사내 프록시를 쓰므로 그냥 된다).
CSP_IMAGES = CSP.replace("img-src 'self' data:", "img-src 'self' data: https: http:")

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
/* AI 백엔드 상태(2026-08-19) — 백엔드를 고르는 표 바로 아래. 색은 신호를 거들
   뿐이고 글자가 신호를 나른다(있음·응답·실패·없음). 없음은 경고가 아니다 —
   opencode 는 안 깔린 것이 보통이고, 안 쓰면 무방하다. */
.aichk { margin: 8px 0 20px; font-size: 13.5px; }
.airow { display: flex; align-items: baseline; gap: 10px; padding: 5px 10px;
    margin: 3px 0; background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r-md); }
.airow.ok .aimark { color: var(--ok); }
.airow.warn .aimark { color: var(--warn); }
.airow.fail .aimark { color: var(--danger); }
.airow.none { opacity: .72; }
.airow .aimark { flex: 0 0 auto; font-weight: 600; min-width: 52px; }
.airow .ainame { flex: 0 0 auto; color: var(--ink-2); font-weight: 600;
    min-width: 64px; }
.airow .aibin { flex: 0 0 auto; color: var(--muted); font-family: var(--mono);
    font-size: 12.5px; min-width: 72px; }
.airow .airole { flex: 0 0 auto; color: var(--ink-2); font-size: 12.5px; }
.airow .aidetail { color: var(--muted); overflow-wrap: anywhere; }
.aifix { margin: -1px 0 6px 12px; padding-left: 10px; color: var(--ink-2);
    border-left: 2px solid var(--border); font-size: 13px; }
.aichk form { margin: 10px 0 0; display: flex; align-items: center; gap: 6px; }
/* 수집 폴더 목록 — 상태를 **왼쪽 고정 열**에 둔다.
   setrow(space-between)로 그렸더니 상태 글자가 라벨 길이만큼 밀려 행마다 다른
   자리에 섰다. 오른쪽 정렬로 옮겨도 시선이 지그재그가 되는 건 같다 — 이 목록에서
   가장 먼저 답해야 하는 질문이 "무엇이 수집되나"라 그 답은 **세로로 훑을 수
   있어야** 한다. 사유와 버튼은 같은 오른쪽 열을 써서 가장자리도 맞춘다. */
.folderrow { display: grid; grid-template-columns: max-content 1fr max-content;
    align-items: center; gap: 4px 12px; padding: 6px 10px; margin: 3px 0;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r-md); }
.folderrow .fstate { font-size: 12.5px; color: var(--ink-3); white-space: nowrap; }
.folderrow.on .fstate { color: var(--ink-2); }
.folderrow .fname { font-family: var(--mono); font-size: 13px;
    overflow-wrap: anywhere; }
.folderrow.off .fname { color: var(--ink-3); }
.folderrow .fwhy { font-size: 12.5px; color: var(--ink-3); text-align: right; }
.folderrow form { margin: 0; justify-self: end; }
.folderrow button { font-size: 12.5px; padding: 3px 10px; }
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
/* flex-wrap 을 쓰지 않는 이유: wrap 은 항목을 줄에 **배치한 뒤** 축소하므로,
   탭 묶음이 통째로 한 줄을 차지하고 ⓘ 가 혼자 아랫줄로 떨어진다(노트 탭이 늘면서
   좁은 폭에서 실제로 그랬다 — 2026-08-11 캡처). nowrap + 묶음의 min-width:0 이면
   묶음이 줄어들며 **안에서** 접히고 ⓘ 는 오른쪽 제자리에 남는다. */
.listtabs { display: flex; justify-content: space-between; align-items: baseline;
    gap: 10px; margin: 2px 0 8px; font-size: 13px; flex-wrap: nowrap; }
.listtabs .ltabs { min-width: 0; }
/* 접히는 지점은 탭 **사이**(' · ' 의 공백)뿐이라야 한다 — 라벨 안에서 끊기면
   '숨김 0' 이 '숨' / '김 0' 으로 갈라진다(실측). */
.listtabs .ltabs a, .listtabs .ltabs b { white-space: nowrap; }
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
/* 좌상단 '← …'(.uplink) — **왔던 화면으로 가는 실제 링크**다(2026-08-18).
   href="#" + 뒤로 핸들러였을 때는 링크 가로채기와 겹쳐 한 클릭에 두 번
   이동했고, 우클릭·새 탭도 안 됐다. 이름은 app.js 가 좌측 이력에서 채운다. */
.uplink { font-size: 13px; }
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
/* 근거 링크는 문장 뒤 작은 표식으로 — 줄 앞의 11자리 번호가 서술보다 먼저
   읽히면 카드가 '번호 목록'이 된다(2026-08-18). */
.dcard .dclaim .dref { color: var(--ink-3); font-size: 11.5px;
  text-decoration: none; }
.dcard .dclaim .dref:hover { color: var(--accent); }
/* 자주 같이 있는 사람 — 프로필의 결정론 각주. 이름은 그 사람 화면으로 간다. */
.dcard .cohort { color: var(--ink-2); font-size: 12.5px; margin: 10px 0 0;
  line-height: 1.7; }
.dcard h2 + .cohort { margin-top: 2px; }
.dcard .cohort .clbl { color: var(--ink-3); font-size: 12px; margin-right: 6px; }
.dcard .cohort a { color: var(--ink-2); text-decoration: none;
  border-bottom: 1px solid var(--border); }
.dcard .cohort a:hover { color: var(--accent); border-color: var(--accent); }
.prow .prole { color: var(--ink-3); font-size: 12.5px; font-weight: 400;
    margin-left: 6px; }
.dim { color: var(--ink-3); }
.analysis { background: var(--analysis-bg); border-radius: var(--r-md); padding: 12px 14px; margin: 10px 0; }
.analysis .sig { color: var(--warn); } .analysis pre { margin: 4px 0; white-space: pre-wrap; }
/* 요지 머리줄의 ⓘ — .sig 의 warn 색을 상속하지 않게 색을 명시. 툴팁은
   title 관례(.mgone 과 동일) — CSS 툴팁을 만들지 않는다(2026-08-11) */
.analysis .ihint { cursor: help; color: var(--ink-3); font-size: 12px; }
/* 현안 브리핑 슬롯(2026-08-15) — 라벨 열 + 본문 열. 확정이 먼저 온다.
   **`.analysis` 안에 가두지 않는다**(2026-08-18) — 인물 화면은 같은 마크업을
   `.dcard` 안에 그리는데 선택자가 스레드 쪽에만 걸려 있어 라벨 열이 통째로
   안 먹었다(줄바꿈 없는 한 덩어리로 보였다). 클래스 이름이 이 기능 전용이라
   전역으로 둬도 부딪히지 않는다. */
.dx { margin: 6px 0 2px; }
.dxrow { display: flex; gap: 10px; padding: 3px 0; align-items: baseline; }
.dxlead { margin: 2px 0 8px; line-height: 1.6; }
/* 카드 머리의 작은 액션(프로필 다시 만들기 등) — 기능이 자기 산출물 옆에
   있으면 버튼 줄이 필요 없다(2026-08-18). */
/* AI 진입은 화면 어디서나 **버튼**이다(2026-08-18) — 카드 머리 액션만 밑줄
   링크였던 것을 쟁점 분석 진입과 같은 `aibtn ghost compact` 로 맞췄다. */
.dcard h2 .cardact, .dcard .cardact { display: inline; margin: 0 0 0 8px; }
.dcard h2 { display: flex; align-items: center; flex-wrap: wrap; gap: 2px 0; }
.dcard .empty .cardact { margin: 0 4px; }
/* 낡음 배지 — 머리줄 자체가 warn 색이라 색만으로는 안 보인다. 칩으로 띄운다
   (경고 팔레트 토큰을 그대로 쓴다). 7일 이상일 때만 붙으므로 상시 노출이 아니다. */
.analysis .stale { background: var(--warn-bg); color: var(--warn);
  border: 1px solid var(--warn-border); border-radius: 999px;
  padding: 0 7px; font-size: 11.5px; margin-left: 4px; }
/* 현안 브리핑 접기 — 첫 문장만 남기고 나머지 슬롯을 접는다(2026-08-19).
   요약줄은 **무엇이 몇 개 숨었는지**를 말한다: '자세히'만 있으면 펼칠지 말지
   판단할 근거가 없다. */
.dxmore { margin: 2px 0 0; }
.dxmore > summary { cursor: pointer; color: var(--ink-3); font-size: 12.5px;
  padding: 3px 0; list-style: none; }
.dxmore > summary::-webkit-details-marker { display: none; }
.dxmore > summary::before { content: "⌄ "; }
.dxmore[open] > summary::before { content: "⌃ "; }
.dxmore > summary:hover { color: var(--accent); }
.dxrow.warn .dxkind { color: var(--warn); }
.dxkind { flex: 0 0 5.5em; color: var(--ink-3); font-size: 12px; }
.dxbody { flex: 1; }
/* 스레드 요약 버튼·대기 줄(2026-08-15) — 분석 블록 안쪽 마지막 줄.
   요약이 회고에서 빠져 여기서만 만들어지므로, 없을 때도 버튼은 보인다. */
.analysis .diagbar { display: flex; align-items: center; gap: 10px;
  margin-top: 10px; flex-wrap: wrap; }
/* 쟁점 분석 진입은 브리핑 버튼과 **같은 줄**이다(2026-08-18 사용자 보고).
   `.tmap` 은 원래 액션 바 아래 독립 줄이라 `margin-top:10px` 을 갖고 있었는데,
   `.diagbar` 안으로 들어온 뒤에도 그 여백이 남아 flex 아이템이 5px 내려가
   두 버튼의 윗변이 어긋났다(실측). 자리를 옮긴 요소는 옛 자리의 여백도 함께
   내려놓아야 한다. */
.analysis .diagbar .tmap { margin: 0; }
.analysis .diagbar .tmap form { margin: 0; }
.analysis .diagwait { display: flex; align-items: center; gap: 8px;
  margin-top: 10px; color: var(--ink-2); font-size: 13px; }
/* 내 노트(스레드 화면, 2026-08-11) — 분석 블록의 이웃. 본문은 _md_to_html 을
   그대로 쓰므로 안쪽 .daily 판(테두리·배경)만 끈다 — 노트는 카드가 아니다 */
details.mynote { background: var(--analysis-bg); border-radius: var(--r-md);
  padding: 10px 14px; margin: 10px 0; }
details.mynote > summary { cursor: pointer; font-weight: 600; color: var(--ink-2); }
/* 본문은 메일보다 한 단계 작게(2026-08-12 사용자 요청) — 노트는 읽는 글이
   아니라 곁에 두는 메모라, 스레드 본문과 같은 크기면 시선을 뺏는다. */
details.mynote .daily { background: none; border: 0; padding: 6px 0 0;
  font-size: 13.5px; }
details.mynote .daily h2 { font-size: 14px; margin: 12px 0 6px; }
details.mynote .daily li { line-height: 1.5; }
/* 인라인 노트 편집기(2026-08-11) — 한 줄 고치자고 외부 편집기를 콜드 스타트
   하지 않게. textarea 규칙이 이 저장소에 처음 생기는 자리라 폭·리사이즈까지
   여기서 정한다. 스킨 공통이라 _SKIN_CSS 가 아니라 여기에 둔다. */
details.mynote .noterow { display: flex; flex-wrap: wrap; gap: 8px;
  align-items: center; margin-top: 10px; }
details.mynote .noterow form { display: inline; margin: 0; }
a.btn { display: inline-block; }        /* a 는 세로 패딩이 안 먹는다 */
details.mynote textarea { width: 100%; box-sizing: border-box; min-height: 260px;
  margin-top: 6px; padding: 10px 12px; font-family: var(--mono);
  font-size: 12.5px; line-height: 1.6; color: var(--ink);
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--r-sm); resize: vertical; }
details.mynote textarea:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 1px; }
details.mynote .notehint { margin: 6px 0 0; font-size: 12.5px; color: var(--ink-3); }
details.mynote .noteconf { margin: 8px 0 0; padding: 8px 12px; font-size: 13px;
  color: var(--warn); background: var(--warn-bg);
  border: 1px solid var(--warn-border); border-radius: var(--r-sm); }
details.mynote .ihint { cursor: help; color: var(--ink-3); font-size: 12px; }
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
/* 메일 머리글은 **두 줄**이다(2026-08-11).
   1줄 = 이 메일의 신원(누가 · 누구에게 · 언제 · AI). '누가 → 누구에게'는 한 쌍이라
   갈라 놓지 않는다. 2줄 = 이 메일의 내용 표찰(제목 · 첨부).
   종전에는 한 줄에 발신·제목·수신·첨부를 다 밀어 넣어 굵고 말줄임 걸린 슬롯
   (.mh-who) 하나에서 셋이 서로를 밀어내 전부 사라졌다.
   보조줄이 .mhead **안**인 이유: 배경(.sent/.focusmsg)이 두 줄을 함께 덮어야 한다.
   flex-wrap 이 아니라 중첩 컨테이너인 이유: wrap 은 줄바꿈 지점을 폭이 정해서
   좁아지면 AI 버튼이 먼저 내려가 줄 구성이 뒤집힌다 — 줄은 폭과 무관해야 한다. */
.msg .mhead { background: var(--surface-3); padding: 6px 12px; font-size: 13px;
    color: var(--ink-2); display: flex; flex-direction: column;
    align-items: stretch;   /* baseline 이면 줄이 내용 폭으로 줄어 mh-when 이 오른쪽 끝에 못 닿는다 */
    gap: 1px; }
.msg .mhead.sent { background: var(--ok-bg); }
.msg .mhead .mh-r1 { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.msg .mhead .mh-who { font-weight: 700; color: var(--ink); overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }
/* 수신인은 발신자 바로 옆(1줄). **flex-basis 가 0 인 것이 이 줄의 핵심**이다.
   좁아질 때 "발신자보다 수신인이 먼저 줄어든다"를 shrink 계수로는 못 만든다 —
   flex 는 `shrink × 기본폭` 에 비례해 줄이는데 발신자("→ 김도현")가 수신인 줄보다
   4배쯤 짧아, 수신인이 대부분을 흡수하고도 발신자에 떨어지는 몇 px 이 발신자를
   통째로 무너뜨린다(400px 패널에서 수신인은 멀쩡한데 `→ 김…` 이 됐다).
   기본폭 0 + grow 로 두면 수신인은 **남는 자리만** 차지한다 — 자리가 없으면 0 이
   되어 사라지고 발신자는 그때까지 손대지 않는다. 진짜 우선순위가 된다.
   min-width:0 과 말줄임을 직접 갖는 것도 필수다: 보조줄에 있을 때 걸리던
   `.mh-r2 > span` 규칙이 1줄에서는 안 걸린다. */
.msg .mhead .mh-to { flex: 1 1 0; min-width: 0; overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis;
    color: var(--ink-3); font-size: 12px; }
/* margin-left:auto 를 쓰지 않는다 — auto 여백은 flex-grow 보다 **먼저** 여유 공간을
   가져가므로, 그걸 두면 위의 수신인이 늘 폭 0 이 된다. 대신 수신인이 늘어나면서
   날짜를 오른쪽 끝으로 민다(그래서 mh-to 는 조건 없이 항상 그린다). */
.msg .mhead .mh-when { flex: none; color: var(--ink-3); font-size: 12px; }
/* 보조줄 — 좁아질 때 **줄어드는 순서를 shrink 로 못 박는다**: 첨부(4) > 제목(1). */
.msg .mhead .mh-r2 { display: flex; align-items: baseline; gap: 10px; min-width: 0;
    font-size: 12px; line-height: 1.4; }
.msg .mhead .mh-r2 > span { min-width: 0; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; }
.msg .mhead .mh-subj { flex: 0 1 auto; font-weight: 600; color: var(--ink-2); }
/* 첨부는 정보량에 비해 과하게 굵었다(굵은 발신자 슬롯 안에 붙어 있었다) — 가장
   흐리게, 가장 먼저 줄어들게, 날짜와 같은 오른쪽 기둥으로. margin-left:auto 는
   폭이 모자라면 0 이 되어 자연히 왼쪽 흐름으로 돌아온다. */
/* 선택자를 `> .mh-att` 로 쓰는 이유: 위의 `.mh-r2 > span` 은 요소 선택자가 하나
   더 붙어 특이도가 높다(0,3,1). 클래스만 쓴 규칙(0,3,0)으로는 min-width 를 못
   이겨 첨부가 폭 0 으로 사라졌다(좁은 패널 실측 — 캡처로만 잡혔다). */
.msg .mhead .mh-r2 > .mh-att { flex: 0 4 auto; margin-left: auto;
    color: var(--ink-3); font-size: 11.5px;
    /* 가장 먼저 줄어들되 **0 이 되지는 않는다** — 통째로 사라지면 '첨부가 있다'는
       신호까지 없어진다. 말줄임은 앞부분을 남기므로 이 폭이면 📎 가 살아남는다. */
    min-width: 1.7em; }
/* 메일별 AI 분석 진입(머리글 오른쪽 끝) — 저장 분석이 있으면 보기 링크+다시 */
.msg .mhead .mh-ai { flex: none; display: inline-flex; align-items: center;
    gap: 6px; margin-left: 10px; }
.msg .mhead .mh-ai form { margin: 0; display: inline-flex; }
.msg .mhead .mh-ai .aibtn.compact { padding: 1px 8px; font-size: 11.5px; }
/* 서버가 먼저 끝났는데 창이 안 닫힌 경우 — 죽은 화면을 설명 없이 두지 않는다 */
.srvgone { position: fixed; inset: 0; display: flex; align-items: center;
    justify-content: center; background: var(--bg); color: var(--ink-2);
    font-size: 15px; z-index: 9999; }
/* 스레드 쟁점 분석 진입 — 액션 바 아래 한 줄(4통 이상 스레드에만 그려진다) */
.tmap { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    margin: 10px 0 0; }
.tmap form { margin: 0; display: inline-flex; }
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
/* 심화 경로 안내 — 엔진이 상한에 부딪힌 답에만. 경고가 아니라 안내라 보조색이다 */
.deephint { margin: 10px 0 4px; padding: 8px 12px; font-size: 13px;
    color: var(--ink-2); background: var(--surface-2);
    border: 1px solid var(--border); border-radius: var(--r-md); }
.deephint code { font-family: var(--mono); font-size: 12.5px; padding: 1px 5px;
    background: var(--surface); border: 1px solid var(--border-2);
    border-radius: 4px; overflow-wrap: anywhere; }
.deephint .copybtn { font-size: 12px; padding: 1px 7px; margin-left: 2px;
    cursor: pointer; background: var(--surface); color: var(--ink-2);
    border: 1px solid var(--border-2); border-radius: 4px; }
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
.askans { font-size: 15.5px; max-width: 100ch; margin-bottom: 4px;
    white-space: pre-line; }   /* 쟁점 분석의 단락 개행 유지 — 기존 답은 개행이 없어 동일 */
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
/* 쟁점 분석 — 쟁점 카드 + 상태 칩. 상태가 어휘(합의·해소·진행 중·보류·평행선)
   밖이면 코드가 비워 칩이 아예 안 뜬다 — 틀린 판정 칩보다 낫다 */
.issue { border: 1px solid var(--border); border-radius: var(--r-md);
    padding: 10px 13px; margin: 0 0 10px; background: var(--surface-2); }
.issue-h { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    font-weight: 600; font-size: 15px; }
.issue-n { font-size: 13.5px; color: var(--ink-2); margin: 3px 0 6px; }
.issue .askev { margin-top: 6px; }
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
/* 버튼 안의 비용 꼬리('· 수 분') — 채움/빈 배경 어느 쪽에서도 읽히게 색이 아니라
   **투명도**로 낮춘다(.dim 의 회색은 파란 배경에서 대비가 무너진다). */
.aibtn .cost { opacity: .75; font-weight: 400; }
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
/* 카드 안의 빈 자리 — 목록 화면의 20px 여백은 여기선 과하다. 폼을 품고 있으므로
   p 가 아니라 div 다(파서가 <p> 안의 <form> 을 만나면 <p> 를 닫아 버려 버튼이
   제 줄로 떨어진다 — 2026-08-18 실측). */
.dcard .empty { padding: 4px 0 2px; line-height: 1.6; }
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
/* Outlook 에 없는 메일 — 사실 통보지 경고가 아니라(사용자가 지운 것일 수 있다)
   흐린 테두리만 쓴다. 내용은 여기 그대로 있다는 뜻이기도 하다. */
.msg .mhead .mgone { flex: none; font-size: 11.5px; color: var(--ink-3);
    border: 1px solid var(--border-strong); border-radius: var(--r-sm);
    padding: 0 5px; white-space: nowrap; }
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
_URL_MD_RX = re.compile(r"https?://[^\s<>\"']+")   # escape 후 텍스트 안의 URL
_BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RX = re.compile(r"[-*]\s+(.*)")


def esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _row_get(row, name: str, default=None):
    """sqlite3.Row 는 없는 컬럼에 IndexError — 구 DB·축약 조회에도 견디게."""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return default


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
    # 외부 URL 도 클릭되게 — 지식 md 참조 절이 주 소비처(회고 본문에도 이득).
    # 문장 끝 구두점은 링크에서 뗀다("…참조: https://x/y." 의 마침표).
    t = _URL_MD_RX.sub(
        lambda m: (lambda u, tail: f"<a href='{u}'>{u}</a>{tail}")(
            m.group(0).rstrip(".,)]'"), m.group(0)[len(m.group(0).rstrip(".,)]'")):]),
        t)
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


def _visible_attach(names: str) -> list:
    """표시용 첨부 이름 목록 — 자동 명명된 인라인 이미지는 걸러낸다.

    문자열이 아니라 목록인 것은 렌더가 개수를 세고 구분자를 직접 넣기 때문이다
    (종전에는 ';' 로 이어 붙여 'a.xlsx;b.pdf' 가 한 덩어리로 나왔다).
    """
    return [n.strip() for n in (names or "").split(";")
            if n.strip() and not _NOISE_ATTACH_RX.match(n.strip())]


# ── 스레드 안 메일 머리글 보조줄 재료 (2026-08-11) ──────────────────────
# 머리글에 텍스트 슬롯이 하나뿐이라 제목·수신인·첨부가 서로를 밀어냈다.
# 자리를 하나 더 주면서, 그 자리에 들어갈 문구를 만드는 순수 함수들.

def _outof(items, unit: str, lead: int = 1) -> str:
    """'대표 외 N단위' — 수신·참조·첨부가 같은 문법을 쓴다.

    한 줄뿐이라 둘 이상을 나열하면 좁은 창에서 둘 다 잘린다. 하나만 온전히
    보이고 나머지는 수로 남는 편이 읽힌다 — 그래서 기본 lead 는 1 이다.
    lead=2 는 수신인 하나뿐이다: 내가 주 수신자가 아닐 때 원래 대표와 나를
    함께 보여야 해서(아래 _people_label). 이름 나열은 쉼표로 잇는다 — '·' 는
    format_recipients 가 수신/참조를 가르는 데 이미 쓴다.
    """
    if not items:
        return ""
    head = items[:lead]
    rest = len(items) - len(head)
    label = ", ".join(head)
    return label if not rest else f"{label} 외 {rest}{unit}"


_NAME_CAP = 18      # 대표 이름이 길어도 '외 N명'은 살아남아야 한다 — 그게 정보다
_TIP_MAX = 20       # 전사 공지 50명을 툴팁에 다 적으면 화면을 덮는다


def _people_label(addrs, names: dict, mine: set) -> tuple:
    """(머리글 라벨, 툴팁용 전체 항목).

    내 주소는 별칭이 여럿이어도 '나' 한 사람으로 접는다 — 내가 읽는 화면이라
    내가 명단에 있는지가 첫 번째 질문이다.

    다만 **나를 맨 앞으로 끌어올리지는 않는다**(2026-08-11 수정). 그렇게 하면
    'To: 이서연 선임; 나' 가 `수신 나 외 1명` 이 되어 내가 주 수신자인 것처럼
    읽힌다 — 실제로는 이서연 선임에게 간 메일이고 나는 곁다리다. 답장 의무가
    누구에게 있는지가 뒤집힌다. 그래서 내가 첫째가 아니면 원래 대표를 앞에 두고
    나를 그 뒤에 붙여 둘 다 보인다: `수신 이서연 선임, 나 외 1명`.
    (데모 282통 실측 — To 는 4%, CC 는 26% 에서 발동. 잘림 위험은 늘지 않는다:
     13자 초과가 108/282 로 종전과 같다.)
    """
    seen, disp, full = set(), [], []
    me_at = -1
    for a in addrs:
        a = (a or "").strip().lower()
        if not a or a in seen:
            continue
        seen.add(a)
        if a in mine:
            full.append(f"나 <{a}>")
            if me_at < 0:
                me_at = len(disp)
                disp.append("나")
            continue
        nm = names.get(a)
        disp.append(nm or a.split("@")[0] or a)
        full.append(f"{nm} <{a}>" if nm else a)
    lead = 1
    if me_at > 0:
        disp.insert(1, disp.pop(me_at))   # 대표 바로 뒤 — 대표를 밀어내지 않는다
        lead = 2
    for i in range(min(lead, len(disp))):     # 상한은 **보이는 이름 전부**에 건다
        if len(disp[i]) > _NAME_CAP:
            disp[i] = disp[i][:_NAME_CAP - 1] + "…"
    return _outof(disp, "명", lead), full


def _tip_join(full) -> str:
    if len(full) <= _TIP_MAX:
        return "; ".join(full)
    return "; ".join(full[:_TIP_MAX]) + f" … (총 {len(full)}명)"


def format_recipients(to_addrs: str, cc_addrs: str, names: dict,
                      mine: set) -> tuple:
    """';' 연결 주소 → (보조줄 라벨, 툴팁 전문).

    예: ('수신 나 외 3명 · 참조 김도현', '수신: 나 <me@…>; …\\n참조: …')

    구분자가 쉼표가 아니라 '·' 인 것은 쉼표가 이름 나열의 쉼표로 읽히기 때문이다.
    발신/수신 메일의 문구를 다르게 하지 않는다 — 발신 메일은 내 주소가 To 에
    없어 자연히 첫 수신인이 대표가 되고, 그게 곧 '내가 누구에게 보냈나'다.
    """
    to = [a for a in (to_addrs or "").split(";") if a.strip()]
    seen = {a.strip().lower() for a in to}
    # Outlook 이 같은 사람을 To 와 CC 양쪽에 넣는 일이 흔하다 — 한 번만 센다
    cc = [a for a in (cc_addrs or "").split(";")
          if a.strip() and a.strip().lower() not in seen]
    to_lab, to_full = _people_label(to, names, mine)
    cc_lab, cc_full = _people_label(cc, names, mine)
    parts, tips = [], []
    if to_lab:
        parts.append(f"수신 {to_lab}")
        tips.append("수신: " + _tip_join(to_full))
    if cc_lab:
        parts.append(f"참조 {cc_lab}")
        tips.append("참조: " + _tip_join(cc_full))
    if not parts:
        # 캘린더 항목·수집 결손 — 빈 줄보다 사실을 적는다
        return "수신 없음", ""
    return " · ".join(parts), "\n".join(tips)


def format_detail(store, cfg, thread_id: int) -> dict:
    """디테일 뷰 데이터: 상단 분석 + 하단 메일 타임라인.

    각 타임라인 항목은 표시용 html(정제됨)과 텍스트(html 없을 때 폴백)를 함께 준다.
    액션 판정(actions)은 계산만 하고 화면엔 내지 않는다(신호 노출 폐지,
    2026-07-30) — d["act"] 는 주간 보고와 같은 판정기를 쓰는 소비처 호환용.
    """
    t = store.thread(thread_id)
    msgs = store.thread_messages(thread_id)
    if not t or not msgs:
        return {"title": f"#{thread_id}", "analysis": ["(스레드 없음)"],
                "timeline": []}

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
    summary_meta = None
    # 요지 형식(확정/남은 것/분기점)이면 구조를 따로 준다 — analysis 는 plain
    # 텍스트 계약이라(CLI·프롬프트가 소비한다) 슬롯 구조를 여기 섞지 않는다.
    # 옛 산문 요약은 한 줄도 안 잡히므로 종전대로 그대로 나간다(2026-08-15).
    diag = review.parse_diagnosis(summ)
    if summ:
        analysis.append("")
        analysis.append("[현안 브리핑]" if diag else "[누적 요약]")
        if diag:
            for kind, body, quote in diag:
                analysis.append(f"· {kind} — {body}")
        else:
            analysis.extend(summ.splitlines())
        # 배지 재료(2026-08-11): analysis 는 plain 텍스트 계약이라(CLI·프롬프트가
        # 소비할 수 있다) UI 장식을 섞지 않고, 구조화 메타를 따로 준다 — HTML 은
        # render_thread 가 조립한다. fresh 는 이미 든 msgs 재사용(추가 질의 0).
        cnt = t["summary_msg_count"] or 0
        # cnt==0 인데 요약이 있으면 재절단 가드 리셋(store._reclean_quotes 가
        # summary_msg_count=0 으로 되돌린 상태) 직후다 — 그때 len(msgs) 를
        # '신규'로 내보내면 요약이 이미 반영한 메일까지 세는 거짓말이라 배지를
        # 숨긴다(fresh=0). 배지는 사실 서술일 때만 그린다(2026-08-11).
        # 마지막 메일 이후 경과일 — 진단은 **그 시점의 스냅샷**이라 오래된 스레드일수록
        # 메일 밖에서 해소됐을 확률이 높다(2026-08-18 회사 PC 실측: 기각 12/21 이
        # 전부 그 사유였다). 결정론 값이라 모델 협조에 기대지 않고, 사용자가
        # 카드를 3초 안에 판정하는 데 쓴다.
        gap = 0
        try:
            gap = (date.today()
                   - date.fromisoformat((msgs[-1]["sent_on"] or "")[:10])).days
        except ValueError:
            pass
        summary_meta = {
            "fresh": max(0, len(msgs) - cnt) if cnt > 0 else 0,
            "updated": _utc_to_local_stamp(
                _row_get(t, "summary_updated", "") or ""),
            "gap": max(0, gap),    # 마지막 메일 이후 경과일
            "diag": diag,          # [(슬롯, 서술, 근거)] — 빈 리스트면 옛 산문
        }

    # 수신·참조 표시 이름은 스레드에 **딱 한 번** 조회한다 — person_name() 은
    # 주소당 2질의라 12통 × 5명이면 화면 하나에 100질의가 넘는다.
    addrs = {a for m in msgs for col in ("to_addrs", "cc_addrs")
             for a in (m[col] or "").split(";") if a.strip()}
    names = store.display_names(addrs)
    mine = {a.lower() for a in (cfg.my_addresses or [])} or set(store.my_addresses)

    timeline: list[dict] = []
    for m in msgs:
        to_label, to_full = format_recipients(
            m["to_addrs"] or "", m["cc_addrs"] or "", names, mine)
        timeline.append({
            "id": m["id"],                # 검색·목록에서 이 메일로 스크롤(#msg-{id})
            "sent_on": _fmt_stamp(m["sent_on"]),
            "is_sent": bool(m["is_sent"]),
            "sender": m["sender_name"] or m["sender_addr"],
            "sender_addr": m["sender_addr"],
            # 제목은 **원문 그대로**다. 스레드 제목과 겹치는 부분을 걷어내던
            # 방식은 98% 의 메일에서 빈 문자열이 되어 제목이 아예 안 보였다
            # (2026-08-11 사용자 지적). 'RE:' 가 붙어 있는 것도 사실이다.
            "subject": m["subject"],
            "to_label": to_label,                        # 보조줄 '수신 … · 참조 …'
            "to_full": to_full,                          # 툴팁 전문(이름 <주소>)
            "attach": _visible_attach(m["attach_names"]),
            "gone": bool(_row_get(m, "gone_at")),
            "html": (m["body_html"] or "").strip(),
            "body": (m["new_content"] or "").splitlines(),
        })
    timeline.reverse()   # 최신 메일 먼저 (메일 클라이언트 관례)
    d = {"title": subject, "analysis": analysis, "timeline": timeline,
         "act": act}
    if summary_meta:
        # 요약이 있을 때만 키가 존재한다 — 키 부재가 '요약 없음'의 사양.
        d["summary_meta"] = summary_meta
    return d


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

# 기본 스킨은 카드형(2026-08-11 사용자 확정) — 미설정 사용자가 보는 얼굴.
# 설정에서 명시적으로 고른 값은 저장되므로 classic 을 고른 사람은 무영향.
# _skin_ok 의 불법값 폴백도 같은 상수다: 폴백이 classic 이면 '미설정 → bento,
# 오타 → classic' 으로 기본이 둘이 되어 설정 화면의 active 표시와 실제 화면이
# 어긋난다. classic 보호는 폴백이 아니라 CSS 게이팅([data-skin='bento'] 프리픽스)
# 이 지킨다. 이 블록이 _head 보다 위에 있는 이유: 시그니처 기본값은 def 시점에
# 평가된다.
SKINS = ("classic", "bento")
_DEFAULT_SKIN = "bento"


def _skin_ok(v: str) -> str:
    return v if v in SKINS else _DEFAULT_SKIN


def _head(title: str, refresh: int | None = None, extra_css: str = "",
          read_w: int | None = None, active: str | None = None,
          theme: str = "light", read_fs: int | None = None,
          skin: str = _DEFAULT_SKIN, appwin: bool = True) -> str:
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
        # **여기가 "모든 문서가 창 수명 프로토콜에 참여한다"를 구조적 사실로
        # 만드는 자리다.** 문서 선언이 이 패키지에서 여기 한 곳뿐이라, _head 가
        # 실으면 앞으로 생길 전폭 페이지도 자동으로 딸려 온다 — 규약이 아니라
        # 구조라서 예외를 둘 수 없다(2026-08-10 통계 페이지가 바로 그 예외였다).
        # defer: 파서를 막지 않고 <body> 가 생긴 뒤에 돈다.
        + ("<script src='/appwin.js' defer></script>" if appwin else "")
        + "</head><body>"
        f"<header class='top'><span class='brand'>Minerva</span>"
        f"{_nav_html(active)}</header>"
    )


def _page(title: str, inner: str, theme: str = "light",
          skin: str = _DEFAULT_SKIN) -> str:
    """단일 컬럼 페이지 — 차단 안내 등 셸이 필요 없는 특수 응답용.

    **창 수명 스크립트를 싣지 않는다(appwin=False).** 이 페이지는 교차 출처 POST
    거부(403) 한 곳에서만 쓰이고 그 HTML 은 **남의 오리진 탭에서 렌더된다** —
    태그를 달면 그 페이지가 창을 등록하고 beat 을 쳐서 **진짜 창을 닫아도 서버가
    안 죽는다.** POST /appwin 에 Origin 검사를 넣는 방식으로는 못 막는다:
    same_origin 은 Origin: null 을 통과시키고, Edge 앱 창이 no-referrer 아래서
    보내는 것이 바로 그 값이다.
    """
    return (_head(title, theme=theme, skin=skin, appwin=False)
            + f"<div id='right' style='flex:1;overflow-y:auto'>"
              f"<div class='inner'>{inner}</div></div></body></html>")


def _page_wide(title: str, inner: str, extra_css: str = "",
               script_src: str | None = None, active: str | None = None,
               theme: str = "light", skin: str = _DEFAULT_SKIN) -> str:
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
                      skin=cfg.opt("web", "skin", default=_DEFAULT_SKIN))


def _shell(title: str, left: str, right: str, refresh: int | None = None,
           read_w: int | None = None, theme: str = "light",
           read_fs: int | None = None, skin: str = _DEFAULT_SKIN) -> str:
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
# 벤토 스킨 — 기본이다(2026-08-11, 종전에는 고른 사람에게만).
#
# **classic 은 이 블록의 영향을 전혀 받지 않는다.** 모든 규칙이
# `<html data-skin='bento'>` 를 요구하므로, classic 을 고르면 이 블록이 없는 것과
# 같다.
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
/* 링크 있는 타일은 통째로 눌린다(2026-08-11) — 커서·리프트가 '눌린다'는 신호다.
   호버 리프트는 위 .dcard 와 같은 2px 문법, 줄이기 설정도 같은 방식으로 끈다. */
:root[data-skin='bento'] .btile[data-href] {
  cursor: pointer; transition: transform .14s ease, box-shadow .14s ease;
}
:root[data-skin='bento'] .btile[data-href]:hover {
  transform: translateY(-2px); box-shadow: var(--shadow-pop);
}
:root[data-skin='bento'] .btile[data-href]:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  :root[data-skin='bento'] .btile[data-href] { transition: none; }
  :root[data-skin='bento'] .btile[data-href]:hover { transform: none; }
}
/* 지식 타일의 최근 제목 미리보기 — 자르기는 폭을 아는 CSS 가 한다 */
:root[data-skin='bento'] .bdectitle {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  color: var(--ink-2);
}
"""


# 앱 창 수명 JS — /appwin.js 로 서빙하고 **_head 가 모든 문서에 싣는다**.
# 창이 곧 앱이다: 마지막 창을 닫으면 서버가 끝나고, 서버가 끝나면 창이 스스로
# 닫힌다. 서버가 창 프로세스를 추적하는 방법도 있지만 Edge 가 창을 이미 떠 있는
# 인스턴스에 넘기면 무력해진다(2026-08-09 실측) — 창이 열려 있는지는 페이지가
# 제일 정확히 안다.
#
# **_APP_JS 안에 두면 안 되는 이유(2026-08-10 실제 발생).** app.js 는 좌/우 셸이
# 있는 문서에서만 돈다(`if (!left || !right) return`). 통계(_page_wide)처럼 셸이
# 없는 전폭 문서는 app.js 를 아예 싣지도 않고, 실었어도 그 가드에 걸려 되돌아간다.
# 그래서 통계로 이동하면 나가는 문서의 bye 만 남고 새 문서는 등록을 못 해
# **창이 멀쩡히 열려 있는데 5~6초 뒤 서버가 죽었다**. 게다가 그 문서에는 beat 도
# 없어 serverGone 조차 못 돌아 창은 그대로 남았다 — 최악의 조합.
# 이 파일은 셸에 의존하지 않고 문서 하나당 하나씩 독립으로 돈다.
#
# 앱 모드 여부를 자산에 굽지 않는다 — no-cache 라 서버를 다시 띄워도 브라우저가
# 검증만 하고 옛 본문을 쓸 수 있다. 매번 GET /appwin 으로 묻는다.
_APPWIN_JS = r"""
(function () {
  "use strict";
  fetch("/appwin").then(function (r) { return r.text(); }).then(function (s) {
    if (s !== "1") return;                     /* 일반 서버 — 아무것도 안 한다 */
    var wid = String(Date.now()) + "-" + Math.random().toString(36).slice(2);
    function tell(ev) {
      /* open/beat 은 sendBeacon 이 아니라 fetch — **응답이 필요하다.**
         beat 이 연속 실패하는 것이 서버가 사라졌다는 유일한 신호다. */
      return fetch("/appwin", {
        method: "POST",
        headers: { "X-Requested-With": "fetch",
                   "Content-Type": "application/x-www-form-urlencoded" },
        body: "ev=" + ev + "&id=" + encodeURIComponent(wid),
      });
    }
    var gone = false;
    function serverGone() {
      if (gone) return;
      gone = true;
      try { window.close(); } catch (e) {}
      /* 창을 못 닫는 브라우저도 있다 — 죽은 화면을 그대로 두지 않는다 */
      setTimeout(function () {
        var d = document.createElement("div");
        d.className = "srvgone";
        d.textContent = "서버가 종료되었습니다 — 이 창을 닫으셔도 됩니다.";
        document.body.appendChild(d);
      }, 500);
    }
    tell("open").catch(function () {});
    var miss = 0;
    setInterval(function () {
      tell("beat").then(function () { miss = 0; })
        .catch(function () { miss += 1; if (miss >= 2) serverGone(); });
    }, 4000);   /* beat: 서버의 _APP_BEAT_SEC/_APP_BEAT_MISS 와 같아야 한다 */

    /* 문서를 떠나면 **무조건** bye — event.persisted 를 보지 않는다. bfcache 라고
       건너뛰면 그 id 가 영영 등록된 채 남아, 나중에 진짜로 창을 닫아도 집합이
       안 비어 서버가 영영 안 죽는다(bfcache 항목은 예고 없이 폐기되고 그때는
       아무 이벤트도 안 온다). unload/beforeunload 는 쓰지 않는다(신뢰 불가).
       ※ 지금은 모든 HTML 이 Cache-Control: no-store 라 크로미움이 bfcache 에
       넣지 않는다. no-store 를 걷어내게 되면 pageshow{persisted} 에서 다시
       등록하는 코드가 필요해진다 — 그때까지는 없는 편이 낫다. */
    window.addEventListener("pagehide", function () {
      try { navigator.sendBeacon("/appwin",
        "ev=bye&id=" + encodeURIComponent(wid)); } catch (e) {}
    });
  }).catch(function () {});
})();
"""

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
    if (path === "/people/diagnose/status") return "left";
    if (path === "/settings/status") return "left";
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

  /* ---- 창 크기 기억: **창을 처음 열 때만** 복원 + 리사이즈 시 저장 ----
     통계는 전폭 페이지라 app.js 가 없다 → 거기서 창을 조절해도 저장되지 않는데,
     돌아오는 순간 이 복원이 저장된 옛 크기로 되돌려 놨다(2026-08-19 사용자 보고).
     창 크기는 **사용자가 마지막에 만진 것이 정답**이라, 화면을 옮길 때마다
     되돌리지 않는다. 같은 창에서 한 번만 — sessionStorage 는 창을 닫으면
     비워지므로 '이 창에서 이미 복원했나'와 정확히 같은 뜻이다.
     (기업 정책으로 storage 가 막히면 복원을 건너뛴다 — 앱 창은 실행 인자
     `--window-size` 로 이미 저장 크기로 열리므로 잃는 것이 없다.) */
  var _wszDone = true;
  try { _wszDone = sessionStorage.getItem("mailkb.wsz") === "1"; } catch (e) {}
  if (!_wszDone) {
    try { sessionStorage.setItem("mailkb.wsz", "1"); } catch (e) {}
    fetch("/winsize").then(function (r) { return r.text(); }).then(function (s) {
      var p = (s || "").split(",");
      var w = parseInt(p[0], 10), h = parseInt(p[1], 10);
      if (w > 0 && h > 0 &&
          (Math.abs(window.outerWidth - w) > 20 || Math.abs(window.outerHeight - h) > 20)) {
        try { window.resizeTo(w, h); } catch (e) { /* 일반 탭은 차단 — 무시 */ }
      }
    }).catch(function () {});
  }
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

  /* 앱 창 수명은 여기 없다 — _APPWIN_JS(/appwin.js)로 옮겼다. app.js 는 좌/우
     셸이 있는 문서에서만 도는데(위의 !left || !right 가드) 창 수명은 **모든**
     문서가 지켜야 하기 때문이다. 2026-08-10 참고. */

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

  /* ---- 현안 브리핑 접기 — **스위치 하나만** 기억한다(스레드·인물 공통) ----
     슬롯을 다 채운 브리핑은 뷰포트의 266% 까지 간다(실측) — 첫 문장만 남기고
     나머지를 접는다. 스레드마다 기억하면 "왜 이것만 접혀 있지"가 되고 상태가
     스레드 수만큼 늘어난다. 기본은 접힘이고, 사용자가 펼치면 그대로 기억한다.
     **방금 만든 것은 접지 않는다** — 눌러서 얻은 결과가 접힌 채 나오면
     "안 만들어졌나"가 된다(2026-08-01 회고 절에서 겪은 판례). */
  var BRIEF_KEY = "mailkb.brief.open";
  var briefForceOpen = false;      /* 완료 주입 1회만 — 기억값은 안 건드린다 */
  var briefQuiet = false;          /* 우리가 연 것은 저장하지 않는다(인쇄 등) */
  function briefWantOpen() {
    try { return localStorage.getItem(BRIEF_KEY) === "1"; } catch (e) { return false; }
  }
  function applyBriefFold(root) {
    var list = (root || document).querySelectorAll("details.dxmore");
    if (!list.length) { briefForceOpen = false; return; }
    var open = briefForceOpen || briefWantOpen();
    briefQuiet = true;
    for (var i = 0; i < list.length; i++) list[i].open = open;
    briefQuiet = false;
    briefForceOpen = false;
  }
  /* toggle 은 버블링하지 않는다 — 캡처로 받는다 */
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (briefQuiet || !d || !d.classList || !d.classList.contains("dxmore")) return;
    try { localStorage.setItem(BRIEF_KEY, d.open ? "1" : "0"); } catch (err) {}
  }, true);
  /* 인쇄는 항상 펼침 — 접힌 내용이 종이에서 사라지면 안 된다. 찾기(Ctrl+F)는
     크로미움이 접힌 details 를 자동으로 펼친다(다른 브라우저는 보장 없음). */
  window.addEventListener("beforeprint", function () {
    briefQuiet = true;
    var l = document.querySelectorAll("details.dxmore");
    for (var i = 0; i < l.length; i++) l[i].open = true;
    briefQuiet = false;
  });

  /* ---- 패널 주입 + 좌우 한 쌍의 브라우저 이력 ---- */
  /* 좌측 이력 — URL 과 **그 화면의 이름**을 함께 쌓는다(2026-08-18). 인물 화면의
     '← …'가 왔던 곳을 그대로 가리키려면 이름이 필요하다. 인물 화면에 들어오는
     길은 여섯 가지(스레드 발신자·인물 목록·어휘 지도·자주 같이 있는 사람·분석
     추천·홈 타일)라 '← 인물' 고정은 그중 하나에서만 맞다. */
  var leftStack = [], leftCur = null, leftLbl = "", rightCur = null;
  var backNav = false;
  var restoringHistory = false;
  var appDepth = 0;
  var rightBlankHtml = (right.querySelector(".inner") || right).innerHTML;
  function historyState() {
    return { minerva: 1, leftUrl: leftCur || "", rightUrl: rightCur || "",
             depth: appDepth };
  }
  function screenLabel(url) {
    var p = new URL(url || "/", location.origin).pathname.replace(/\/+$/, "") || "/";
    var t = left.querySelector(".personhead .ptitle");
    if (t && (p === "/people" || p === "/person")) {
      var nm = (t.textContent || "").trim();
      if (nm) return nm.length > 12 ? nm.slice(0, 12) + "…" : nm;
    }
    if (p.indexOf("/search") === 0) return "검색";     /* 메뉴에 없다(헤더 검색창) */
    var target = navTarget(p);
    var a = target && document.querySelector(
      'header.top nav a[href="' + target + '"]');
    return (a && (a.textContent || "").trim()) || "뒤로";
  }
  /* 좌상단 '← …' — 좌측 이력이 있으면 **그 화면**을 가리키게 갈아 끼운다.
     이력이 없으면(새로고침·직접 진입) 서버가 렌더한 기본값을 그대로 둔다. */
  function paintUpLink() {
    var a = left && left.querySelector("a.uplink");
    if (!a || !leftStack.length) return;
    var top = leftStack[leftStack.length - 1];
    a.setAttribute("href", top.url);
    a.textContent = "← " + top.label;
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
    var lbl = screenLabel(u);                    /* 주입 직후라 DOM 은 새 화면이다 */
    if (backNav) { leftCur = u; leftLbl = lbl; return; }  /* 되짚기는 안 쌓음 */
    if (u !== leftCur) {
      if (leftCur) leftStack.push({ url: leftCur, label: leftLbl || "뒤로" });
      leftCur = u;
    }
    leftLbl = lbl;
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
    paintUpLink();
    applyBriefFold(el);
    hookReviewPolling(el);
    hookAiPolling(el);
    hookWeeklyPolling(el);
    hookAskPolling(el);
    hookDossierPolling(el);
    hookSyncPolling(el);
    hookKnPolling(el);
    hookDiagPolling(el);
    hookPdiagPolling(el);
    hookAitestPolling(el);
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

  /* ---- 지식 저장 잡 폴링 — 실시간 진행이 필요 없는 작업이라 단순하다:
     마커가 있는 동안 3초마다 묻고, 마커가 사라진 응답이 오면 통째로 교체. */
  /* ---- 스레드 요약 잡 폴링 (지식 저장과 같은 수법 — 마커 소멸 = 완료) ---- */
  /* ---- 현안 브리핑(인물) 잡 폴링 ----
     **인물 화면은 좌측 패널이다**(/people 라우트가 "left"). 스레드 쪽에서 훅을
     복사하며 right 로 두었더니 완료돼도 화면이 안 바뀌었다 — 마커는 좌측에
     있는데 우측만 보고 있었기 때문이다(2026-08-18 사용자 보고). */
  function hookPdiagPolling(root) {
    if (!root.querySelector("[data-pdiag-running]")) return;
    setTimeout(function () {
      var u = new URL("/people/diagnose/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!left.querySelector("[data-pdiag-running]")) return;
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-pdiag-running]")) {
            briefForceOpen = true;                  /* 방금 만든 것은 안 접는다 */
            inject("left", html, null);
            return;
          }
          hookPdiagPolling(left);
        })
        .catch(function () { hookPdiagPolling(left); });
    }, 3000);
  }

  /* ---- AI 백엔드 [응답 시험] 폴링 ----
     설정은 **좌측 패널**이다(paneFor). 백엔드마다 1콜이라 보통 수 초~수십 초. */
  /* ---- 심화 경로 안내의 [복사] — 127.0.0.1 은 secure context 라 clipboard 가 된다.
     실패해도 조용히 넘어간다: 명령은 어차피 선택 가능한 <code> 로 보인다. ---- */
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".copybtn");
    if (!b) return;
    ev.preventDefault();
    var txt = b.getAttribute("data-copy") || "";
    var done = function () { b.textContent = "복사됨"; };
    try {
      if (navigator.clipboard) navigator.clipboard.writeText(txt).then(done, function () {});
    } catch (e) { /* 안 되면 그대로 둔다 */ }
  });

  function hookAitestPolling(root) {
    if (!root.querySelector("[data-aitest-running]")) return;
    setTimeout(function () {
      var u = new URL("/settings/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!left.querySelector("[data-aitest-running]")) return;
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-aitest-running]")) {
            inject("left", html, null);
            return;
          }
          hookAitestPolling(left);
        })
        .catch(function () { hookAitestPolling(left); });
    }, 3000);
  }

  function hookDiagPolling(root) {
    if (!root.querySelector("[data-diag-running]")) return;
    setTimeout(function () {
      var u = new URL("/thread/diagnose/status", location.origin);
      u.searchParams.set("frag", "1");
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!right.querySelector("[data-diag-running]")) return; /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-diag-running]")) {
            briefForceOpen = true;                  /* 방금 만든 것은 안 접는다 */
            inject("right", html, null);            /* 완료 → 요약이 실린 화면 */
            return;
          }
          hookDiagPolling(right);                    /* 다음 폴링 예약 */
        })
        .catch(function () { hookDiagPolling(right); });
    }, 3000);
  }

  /* ---- 지식 저장 폴링. 대기열을 하나씩 비우므로 **진행 중에도 화면을 갱신한다**
     — 3건을 넣고 몇 분간 아무것도 안 변하면 멈춘 것으로 읽힌다. 다만 inject() 는
     scrollTop 을 0 으로 되돌리므로 3초마다 부르면 안 된다. [data-kn-cards]
     안만 갈아 끼운다(patchJob 의 #{p}-extra 와 같은 방식). id 가 아닌 이유는
     이 절이 두 패널에 동시에 뜰 수 있어서다(회고=우측 · 지식 탭=좌측). */
  function hookKnPolling(root) {
    var mk = root.querySelector("[data-kn-running]");
    if (!mk) return;
    var day = mk.getAttribute("data-kn-day") || "";
    setTimeout(function () {
      var u = new URL("/knowledge/status", location.origin);
      u.searchParams.set("frag", "1");
      if (day) u.searchParams.set("date", day);  /* 보고 있는 날짜로 되돌려 받는다 */
      u.searchParams.set("_", Date.now());   /* 메모리 캐시 우회 */
      fetch(u.toString(), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (!right.querySelector("[data-kn-running]")) return; /* 화면 전환됨 */
          var tmp = document.createElement("div");
          tmp.innerHTML = html;
          if (!tmp.querySelector("[data-kn-running]")) {
            inject("right", html, null);            /* 완료 → 결과 화면으로 교체 */
            return;
          }
          var nc = tmp.querySelector("[data-kn-cards]"),
              oc = right.querySelector("[data-kn-cards]");
          if (nc && oc && nc.innerHTML !== oc.innerHTML) oc.innerHTML = nc.innerHTML;
          hookKnPolling(right);                     /* 다음 폴링 예약 */
        })
        .catch(function () { hookKnPolling(right); });
    }, 3000);
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
    /* 목록 하단 **센티널**의 '더 보기'만 비켜선다 — 관찰자가 이어 붙이고, JS 가
       꺼졌을 때만 전체 페이지 폴백으로 쓴다. `.more` 는 벤토 타일의 '열기'가
       모양 때문에 같이 쓰는 클래스라, 통째로 제외하면 그 링크만 전체 페이지
       이동이 된다 — 같은 타일인데 누르는 자리에 따라 동작이 갈렸다(2026-08-18
       실측: 타일 본체는 SPA, '열기'는 문서 재로드). 선택자가 뜻하는 것을
       정확히 가리키게 한다. */
    if (a.closest(".more[data-more]")) return;
    /* 원격 이미지 보기/안전 보기 — **전체 페이지 이동이어야 한다.** 패널 주입은
       이미 받은 문서의 CSP 를 바꾸지 못해 이미지가 그대로 막힌다(조용히 안 됨). */
    if (a.classList && a.classList.contains("imgshow")) return;
    e.preventDefault();
    if (!noteLeaveOk()) return;     /* 편집 중인 노트를 두고 떠나는가 */
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
    /* '← …'(uplink)는 좌측 이력을 되짚는 이동이다 — 스택에서 빼고, 이 이동은
       다시 쌓지 않는다(왔다 갔다 해도 스택이 자라지 않게). 링크가 서버 기본값
       그대로면(이력 없음) 평범한 이동이라 아무것도 건드리지 않는다. */
    var up = !!(a.classList && a.classList.contains("uplink")
                && leftStack.length
                && leftStack[leftStack.length - 1].url === href);
    if (up) { leftStack.pop(); backNav = true; }
    load(href).then(upDone, function () { upDone(); location.href = href; });
    function upDone() { if (up) backNav = false; }
  });

  /* ---- 인라인 노트 편집기(2026-08-11) ----
     자동 저장은 하지 않는다(사용자 확정) — 대신 저장하지 않은 글을 남긴 채
     화면을 떠나려 하면 한 번 묻는다. dirty 판정은 defaultValue 비교라 서버가
     원본을 따로 실어 줄 필요가 없고, 충돌 복구로 값을 되살린 상태도 자동으로
     '미저장'이 된다. beforeunload 는 여기서도 쓰지 않는다(appwin.js 와 같은
     판단 — 신뢰할 수 없다). 그래서 창 닫기·브라우저 뒤로는 못 잡는다. */
  function noteLeaveOk() {
    var ta = document.querySelector("form.noteedit textarea[name='body']");
    if (!ta || ta.value === ta.defaultValue) return true;
    return window.confirm("저장하지 않은 노트가 있습니다 — 그대로 나갈까요?");
  }

  /* ---- 벤토 타일: 링크 있는 타일은 통째로 클릭(2026-08-11 사용자 확정) ----
     내부의 링크·버튼·폼·접기가 항상 우선이고(앵커 가로채기가 먼저 잡으면
     defaultPrevented 로도 걸러진다), 텍스트를 긁는 중이면 이동하지 않는다.
     이동은 위 앵커 가로채기와 같은 패턴 — load() 실패 시 전체 이동 폴백. */
  function tileGo(t) {
    var href = t.getAttribute("data-href") || "";
    if (href.charAt(0) !== "/" || href.slice(0, 2) === "//") return;
    if (!noteLeaveOk()) return;
    load(href).catch(function () { location.href = href; });
  }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 ||
        e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var t = e.target.closest ? e.target.closest(".btile[data-href]") : null;
    if (!t) return;
    if (e.target.closest("a, button, form, input, summary, details")) return;
    if (window.getSelection && String(window.getSelection())) return;
    tileGo(t);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var t = e.target;
    if (t && t.classList && t.classList.contains("btile")
        && t.getAttribute("data-href")) tileGo(t);
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
    /* 노트 편집기(2026-08-11): 비우고 저장 = 삭제라 한 번 묻는다(askdel 과
       같은 방식). 다른 폼을 누른 것이면 '편집 중 이탈'이므로 그쪽을 묻는다.
       stash 는 충돌로 되돌아왔을 때 되살릴 내 원고다 — 서버는 파일을 지키느라
       파일의 현재 내용을 그려 주므로, 안 챙기면 내가 친 글이 사라진다. */
    var noteForm = form.classList && form.classList.contains("noteedit");
    var stash = null;
    if (noteForm) {
      var nta = form.querySelector("textarea[name='body']");
      stash = nta ? nta.value : "";
      if (!stash.trim() && !window.confirm(
          "본문이 비었습니다 — 저장하면 노트 파일이 삭제됩니다. 계속할까요?")) return;
    } else if (!noteLeaveOk()) {
      return;
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
      /* 서브미터를 넘긴다 — 노트 편집기의 '저장'과 '외부 편집기'처럼 버튼으로
         갈리는 폼이 성립하려면 눌린 버튼의 name/value 가 실려야 한다
         (2026-08-11). e.submitter 를 모르는 구형 브라우저면 종전과 같다. */
      body: new URLSearchParams(new FormData(form, e.submitter)),
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
        /* 충돌로 되돌아왔으면 서버는 파일을 지키느라 '파일의 현재 내용'을
           그렸다 — 내가 친 원고를 되살린다. 사람이 쓴 글을 버리지 않는다. */
        if (stash !== null) {
          var re = document.querySelector(
            "form.noteedit[data-conflict='1'] textarea[name='body']");
          if (re) re.value = stash;
        }
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
            /\/thread\/\d+\/(flag|unflag|hide|unhide|note-save)$/.test(action)) {
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
      leftLbl = screenLabel(wantLeft);
      rightCur = wantRight;
      restoringHistory = false;
      markSelected();
      markNav();
      paintUpLink();
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

  /* '← 뒤로'(.backlink, href="#") 전용 핸들러는 없앴다(2026-08-18). 되짚기는
     이제 **실제 링크**(.uplink)라 위의 링크 가로채기 하나만 탄다 — 한 클릭에
     두 번 이동하던 구조가 사라지고, 우클릭·새 탭도 된다. 스택이 비었을 때
     history.back() 으로 앱 밖(=서버 종료)까지 나가던 길도 함께 없어졌다. */

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
  leftLbl = screenLabel(leftCur);   /* 스택은 비어 있다 — '← …'는 서버 기본값 */
  applyBriefFold(document);         /* 서버가 그린 첫 화면에도 기억값을 적용 */
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

# 서빙하는 JS 자산 전부. 여기 넣으면 라우팅·헤더가 자동으로 따라온다.
# C1 제어문자 회귀 테스트도 이 목록을 기준으로 돈다(자산이 늘 때 빠뜨리지 않게).
_JS_ASSETS = {
    "/appwin.js": lambda: _APPWIN_JS,     # 창 수명 — 모든 문서
    "/app.js": lambda: _APP_JS,           # SPA — 좌/우 셸 문서만
    "/report.js": lambda: report.REPORT_JS,   # 통계 전폭 페이지
}


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
            # 이번 실행이 쓴 콜 — 중지·부분 실패로 끝났어도 쓴 만큼은 보여준다.
            # (같은 줄이 일간 회고 화면에도 남는다 — 보관분, render_daily)
            spent = review.fmt_meter(det.get("ai_meter"))
            if spent:
                msg += " · " + spent
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
    # run_ai_layer 는 오늘·백필 모두 3단계(수확·디제스트·하루요약) — 진행 바 일치.
    # (누적 요약 갱신은 2026-08-15 에 스레드 화면 버튼으로 빠졌다.)
    cancel = _job_start(_review_job, _review_lock, msg="준비 중…", step=0,
                        total=3, date=day, ai=ai)
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
    """'그새 새 메일이 왔나'의 기준선 — 도착 순서(ingest_seq)를 본다.
    MAX(id)는 발신 시각 기준이라 백필에 반응하지 않는다(store.ask_basis 주석)."""
    row = store.db.execute(
        "SELECT COALESCE(MAX(ingest_seq), 0) FROM messages").fetchone()
    return int(row[0]) if row else 0


# ─────────────────────────────────────────────────── 메일 동기화(백그라운드)

def _do_sync(store, cfg) -> tuple:
    """동기화 1회(수집 + 이미지 프룬). (완료 msg, 신규 통수) 반환.
    수집 실패(Outlook 꺼짐 등)에도 프룬(COM 불필요)은 반드시 실행 — 기존 보장 유지.
    잡 래퍼와 테스트가 공유하는 순수 동작(소켓·스레드 무관)."""
    from .sources import folder_labels, get_source, remember_folder_plan
    # known_folders 를 넘기지 않으면 배경 동기화는 새 폴더를 영영 백필하지 않는다
    # (증분 필터가 워터마크 이후만 주므로) — CLI 와 같은 계약을 쓴다.
    src = get_source(cfg.source, cfg=cfg,        # outlook 이면 Windows COM
                     known_folders=store.synced_folders())
    retain = int(cfg.opt("web", "image_retain_days", default=60) or 0)
    cutoff = image_cutoff_for(retain)
    try:
        stats = store.ingest(src.fetch(store.last_sync(), image_cutoff=cutoff),
                             image_cutoff=cutoff)
    finally:
        store.maybe_prune_html(retain)
    store.mark_synced_folders(getattr(src, "drained_folders", None),
                              in_scope=folder_labels(src))
    remember_folder_plan(store, src)             # 설정 화면이 쓸 폴더 목록
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

    완료 화면은 다음 동선(암묵지 후보 저장/유보)으로 이어지게 링크를 단다."""
    with _review_lock:
        st = dict(_review_job)
        running, msg = st["running"], st["msg"]
        step, total = st["step"], st["total"]
        job_date = st.get("date") or ""
    if running:
        # 비-AI 자동 갱신(_maybe_auto_review)도 같은 슬롯을 쓴다 — 제목이 다르지
        # 않으면 사용자가 AI 회고가 도는 줄 착각한다(끊을 대상도 없다).
        ai_job = bool(st.get("ai"))
        # 지금까지의 콜 수 — 요약 단계는 스레드 수만큼 콜이 나가서 단계 표시만
        # 보면 오래 걸리는 이유가 안 보인다(비-AI 잡은 콜이 없으니 안 붙는다).
        calls = int(st.get("calls") or 0)
        call_s = f" · 호출 {calls}회" if calls else ""
        # data-review-running: app.js 폴링 훅 마커 (전체 페이지는 meta refresh)
        return ("<div data-review-running='1' hidden></div>"
                + _job_wait_card(
                    "rv",
                    "AI 회고 작성 중" if ai_job else "일간 회고 정리 중",
                    stage=(f"단계 {min(step, total)}/{total} · {msg or ''}{call_s}"
                           if step else (msg or "준비 중…") + call_s),
                    live=_job_live_line(st) if ai_job else "",
                    preview=_job_preview(st) if ai_job else "",
                    model=(st.get("model") or "") if ai_job else "",
                    step=step, total=total if step else 0,
                    hint=("메일을 읽고 회고·암묵지 초안을 준비합니다 — 완료되면 "
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
        pend = len(store.knowledge_candidates())
        if pend:
            day = job_date or ""
            href = f"/records?tab=daily&date={esc(day)}" if day else "/daily"
            links.insert(0, f"<a href='{href}'>"
                            f"암묵지 후보 {pend}건 → 일간 회고에서 저장/유보</a>")
    return (f"<h1>AI 회고</h1>{body}"
            "<p>" + " · ".join(links) + " · <a href='/'>홈</a></p>", False)


# ─────────────────────────────────────────────────── 렌즈 렌더





# 목록 페이지네이션(#5) — 초기엔 화면 한 판 분량만, 스크롤 시 추가 로딩
_PAGE = 30          # 한 번에 렌더하는 행 수
_RAW_BATCH = 400    # 노이즈 필터 전 원시 조회 상한 (메일함)


_WEEKDAY = "월화수목금토일"


def _fmt_stamp(iso: str) -> str:
    """메일 시각 — 'YYYY-MM-DD (요) HH:MM'. 목록용 _fmt_when 과 짝이다.

    ISO 의 'T' 를 그대로 내보내면 사람이 읽는 자리에 기계 표기가 남는다
    (2026-08-11 사용자 지적). 요일까지 넣는 것은 일정·회의 메일에서 날짜만으로는
    무슨 요일인지 안 잡히기 때문이다 — 그 판단이 스레드 안에서 자주 필요하다.
    표시 지점이 넷(스레드 머리글·검색 결과·분석 인용 둘)이라 규칙은 하나로 둔다.
    """
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        # 형식이 어긋난 값(수집 결손·외부 유입)이라도 'T' 는 남기지 않는다
        return iso[:16].replace("T", " ").rstrip()
    return f"{iso[:10]} ({_WEEKDAY[d.weekday()]}) {iso[11:16]}".rstrip()


def _utc_to_local_stamp(s: str, tz=None) -> str:
    """threads.summary_updated → 로컬 'YYYY-MM-DD (요) HH:MM' (_fmt_stamp 표기).

    이 컬럼만 sqlite `datetime('now')` = **UTC** 로 저장된다(store.save_summary).
    사람이 읽는 자리라 로컬로 돌린다. 같은 DB 라도 ask_cache.created 나
    messages.sent_on 은 로컬 시각이라 이 변환을 쓰면 9시간 어긋난다 — 컬럼마다
    저장 시각대가 다르다는 것을 잊지 말 것(2026-08-11). 빈 값(구 데이터)·형식
    오류는 '' 를 돌려주고 호출부가 툴팁 자체를 그리지 않는다. tz 는 테스트
    고정용(기본 None = 시스템 로컬).
    """
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc).astimezone(tz)
    except ValueError:
        return ""
    return f"{dt:%Y-%m-%d} ({_WEEKDAY[dt.weekday()]}) {dt:%H:%M}"


def _fmt_when(iso: str) -> str:
    """목록 날짜 — 오늘은 시:분, 올해는 M/D, 그 외 YYYY/M/D.

    _fmt_stamp 와 목적이 다르다: 이쪽은 한눈에 훑는 목록용 상대 표기다.
    """
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
# 노트 탭(2026-08-11): 목록의 📝 배지와 **같은 글리프**라야 "저 배지 붙은 것들"로
# 바로 읽힌다. 자리는 플래그 옆·숨김 앞 — 숨김은 복구용이라 끝에 둔다.
_LIST_FILTERS = [("", "전체"), ("unread", "미개봉"),
                 ("flagged", "🚩 플래그"), ("noted", "📝 노트"),
                 ("hidden", "🙈 숨김")]


def _list_flt(qs) -> str:
    """쿼리스트링에서 활성 필터 하나 — 없으면 '' (전체).
    구 북마크의 awaiting/deadline 키는 전체로 조용히 강등된다."""
    for key in ("unread", "flagged", "noted", "hidden"):
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
    "base": None, "max_seq": 0, "received": set(), "real": set(),
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
    """(noise_thread_ids, noise_msg_ids) — append-only high-water 증분 캐시.

    워터마크는 **id 가 아니라 ingest_seq** 다(2026-08-11). 번호가 날짜 기반이 된
    뒤로 "나중에 들어온 행은 항상 id 가 더 크다"가 깨졌다 — 백필(더 오래된 메일을
    나중에 수집)은 id 워터마크보다 **아래**에 행을 만들어 분류에서 조용히 빠졌다
    (실측: 7/10 수집 후 7/3 백필 시 `id>워터마크` 로 잡히는 새 행 0건, 실제 1건).
    ingest_seq 는 도착 순서라 append-only 가 다시 성립한다.
    """
    max_seq = store.db.execute(
        "SELECT COALESCE(MAX(ingest_seq), 0) FROM messages").fetchone()[0]
    base = (str(store.db_path), _noise_config_version(cfg))
    with _noise_cache_lock:
        rebuild = (_noise_cache["base"] != base
                   or max_seq < _noise_cache["max_seq"])
        if not rebuild and max_seq == _noise_cache["max_seq"]:
            return _noise_cache["thread_ids"], _noise_cache["msg_ids"]

        if rebuild:
            recv, real, nmsg = set(), set(), set()
            where, params = "", ()
        else:
            recv = set(_noise_cache["received"])
            real = set(_noise_cache["real"])
            nmsg = set(_noise_cache["msg_ids"])
            where, params = " AND ingest_seq>?", (_noise_cache["max_seq"],)

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
            base=base, max_seq=max_seq, received=recv, real=real,
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
    noted = _noted(store, cfg)               # 📝 배지 — 내 노트 있는 스레드
    if flt == "hidden":
        tcond = "t.hidden=1"
    else:
        tcond = "(t.hidden IS NULL OR t.hidden=0)"
        if flt == "unread":
            tcond += " AND (m.read_at IS NULL OR m.read_at='')"
        elif flt == "flagged":
            tcond += " AND t.flagged=1"
        elif flt == "noted":
            # 노트는 스레드 단위다 — 메일함 행(메일)에는 '이 메일의 스레드에
            # 노트가 있다'로 잇는다. 📝 배지가 이미 같은 규칙이다.
            tcond += " AND EXISTS (SELECT 1 FROM notes n WHERE n.thread_id=t.id)"
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
        badge = ("🚩 " if r["flagged"] else "") + (
            "📝 " if r["thread_id"] in noted else "")
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
          COALESCE(SUM(CASE WHEN {visible} AND {real} AND n.thread_id IS NOT NULL
            THEN 1 ELSE 0 END),0) noted,
          COALESCE(SUM(CASE WHEN t.hidden=1 THEN 1 ELSE 0 END),0) hidden
        FROM messages m JOIN threads t ON t.id=m.thread_id
        -- notes.thread_id 가 PK 라 LEFT JOIN 이 행을 늘리지 않는다. 집계 CASE 안에
        -- 서브쿼리를 넣으면 메일마다 한 번씩 도므로 조인으로 뺀다.
        LEFT JOIN notes n ON n.thread_id=t.id WHERE m.is_sent=0"""
    ).fetchone()
    counts = {"": agg["total"], "unread": agg["unread"],
              "flagged": agg["flagged"], "noted": agg["noted"],
              "hidden": agg["hidden"]}
    body = "".join(items) or (
        # 노트 탭은 처음엔 비어 있는 게 정상이다 — '수신 메일 없음'은 고장으로 읽힌다
        "<p class='empty'>아직 노트가 없습니다 — 스레드를 열고 [노트]에 적으면 "
        "여기 모입니다.</p>" if flt == "noted"
        else "<p class='empty'>수신 메일 없음</p>")
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
    noted = _noted(store, cfg)                # 📝 배지 — 내 노트 있는 스레드
    ncsv = ",".join(str(int(i)) for i in noise_ids)
    nx = f" AND t.id NOT IN ({ncsv})" if noise_ids else ""    # alias t. (행 쿼리·unread)
    nxb = f" AND id NOT IN ({ncsv})" if noise_ids else ""     # bare id (agg: FROM threads)
    if flt == "hidden":
        cond = "WHERE t.hidden=1"
    elif flt == "flagged":
        cond = "WHERE t.flagged=1 AND (t.hidden IS NULL OR t.hidden=0)" + nx
    elif flt == "noted":
        # 숨김·노이즈 규칙은 플래그와 **똑같이** 둔다 — 탭마다 다르면 어느 탭에
        # 뭐가 빠지는지 아무도 못 외운다. (숨긴 스레드의 노트는 검색으로 찾는다.)
        cond = ("WHERE t.id IN (SELECT thread_id FROM notes) "
                "AND (t.hidden IS NULL OR t.hidden=0)" + nx)
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
        if r["id"] in noted:
            marks += "📝"                     # 내 노트 있음(2026-08-11)
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
    # 노트는 별도 질의 — 위 집계의 SUM(CASE) 안에 서브쿼리를 넣으면 스레드마다
    # 한 번씩 돈다. notes.thread_id 가 PK 라 조인은 인덱스 조회다.
    n_noted = store.db.execute(
        "SELECT COUNT(*) c FROM threads t JOIN notes n ON n.thread_id=t.id "
        "WHERE (t.hidden IS NULL OR t.hidden=0)" + nx
    ).fetchone()["c"]
    # 응답대기·기한 뱃지는 리스트와 동일 집합이어야 한다: await/dead ∩ 비노이즈 ∩ 비숨김.
    counts = {"": agg["total"], "unread": n_unread,
              "flagged": agg["flag"], "noted": n_noted, "hidden": agg["hid"]}
    body = "".join(items) or (
        # 노트 탭은 처음엔 비어 있는 게 정상이다 — '스레드 없음'은 고장으로 읽힌다
        "<p class='empty'>아직 노트가 없습니다 — 스레드를 열고 [노트]에 적으면 "
        "여기 모입니다.</p>" if flt == "noted"
        else "<p class='empty'>스레드 없음</p>")
    return ("<h1>스레드</h1>"
            + _list_filter_bar("/threads", flt, counts)
            + f"<div class='mlist'>{body}{more}</div>")


def _open_external(path) -> bool:
    """파일을 로컬 기본 연결 프로그램으로 연다 — 노트(.md)의 외부 편집기 열기.

    localhost 전용 앱이 사용자 클릭으로 실행하는 것이라 'Outlook 열기'(COM)와
    같은 신뢰 수준이다. 실패해도 노트 파일은 이미 있으므로 False 만 돌려주고
    호출부가 경로를 알려준다 — 열기 실패가 생성 실패처럼 보이면 안 된다.

    **Windows 에서 os.startfile 을 쓰지 않는다**(2026-08-11 사용자 보고):
    ShellExecute 로 뜬 프로그램이 **Minerva 를 띄운 콘솔을 물려받아**, VS Code
    같은 Electron 편집기가 콜드 스타트하며 내는 자기 로그(shared storage 초기화
    · DeprecationWarning · Unknown channel …)를 우리 콘솔에 쏟았다. 노트는
    열렸지만 서버 콘솔이 남의 로그로 덮이면 정작 Minerva 메시지를 못 본다.
    explorer 를 한 단계 끼우면 편집기가 explorer 의 자식이 되어 콘솔이 끊긴다
    — 여는 방식은 그대로 '연결 프로그램'이고, 인자는 셸을 거치지 않아 경로의
    `&`·`^`·공백도 안전하다(`cmd /c start` 는 그 셸 파싱을 다시 들인다)."""
    p = str(path)
    import subprocess
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
             "stdin": subprocess.DEVNULL}
    try:
        if hasattr(os, "startfile"):                     # Windows
            # DETACHED_PROCESS: 콘솔을 아예 물려주지 않는다(위 docstring).
            # explorer 는 성공해도 종료 코드가 1 이라 반환값은 보지 않는다.
            subprocess.Popen(
                ["explorer.exe", p],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0), **quiet)
            return True
        subprocess.Popen(["xdg-open", p], start_new_session=True, **quiet)
        return True
    except OSError:
        return False


_notes_indexed = False    # 프로세스당 1회 전체 색인 — 목록 배지가 구 DB 에서도 뜨게


def _noted(store, cfg) -> frozenset:
    """노트 있는 스레드 집합(목록 📝 배지용).

    첫 호출에서 색인을 파일과 맞춘다 — 서버를 새로 켠 직후 목록부터 열어도
    이전에 만든 노트의 배지가 보이게. 이후에는 노트 버튼·스레드 화면·검색·
    질문 경로가 각자 갱신하므로 여기서는 DB 만 읽는다."""
    global _notes_indexed
    if not _notes_indexed:
        from . import notes as notes_mod
        notes_mod.reindex(cfg, store)
        _notes_indexed = True
    return store.noted_thread_ids()


def _actions_bar(tid: int, t, has_attach: bool, decider: str = "",
                 has_note: bool = False, editing: bool = False) -> str:
    flagged = bool(t["flagged"]) if t else False
    hidden = bool(t["hidden"]) if t else False
    forms: list[str] = []

    def _btn(action, label, cls="", title=""):
        tt = f" title='{esc(title)}'" if title else ""
        forms.append(f"<form method='post' action='/thread/{tid}/{action}'>"
                     f"<button class='{cls}'{tt}>{esc(label)}</button></form>")

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
    # 노트는 스레드당 md 파일 하나. 여기 버튼은 **노트가 없을 때만** 나온다 —
    # 있으면 [내 노트] 카드의 [편집]이 그 일을 맡으므로 중복이다(2026-08-11).
    # POST 폼이 아니라 GET 링크인 것은 의미와 맞춘 것이다: 눌러도 파일은 안
    # 생기고 편집 상자만 열린다(파일은 첫 저장에서 생긴다 — 사용자 확정).
    if not has_note and not editing:
        forms.append(f"<a class='btn' href='/thread/{tid}?note=edit'>"
                     "📝 노트 쓰기</a>")
    _btn("open", "Outlook 열기")
    if has_attach:
        _btn("attach", "첨부 추출")
    return f"<div class='actions'>{''.join(forms)}</div>"


_MAIL_SCOPE_RX = re.compile(r"@(\d+)~mail:(\d+)$")
_THREAD_SCOPE_RX = re.compile(r"@(\d+)~thread:(\d+)$")


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
        "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND ingest_seq > ?",
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


THREAD_MAP_MIN_MSGS = 4   # 2~3통엔 쟁점 구조가 성립하지 않는다 — 버튼 소음 방지


def _thread_analyses(store, tid: int) -> dict | None:
    """스레드의 저장된 쟁점 분석 {id, created, basis} — 최신 것 하나.

    키 형식 v3:질문@<basis>~thread:<tid> — _mail_analyses 와 같은 방식으로
    ask_cache 를 그대로 쓴다. LIKE '%~thread:%' 는 '%~mail:%' 와 문자열상
    서로 매치되지 않아 두 조회가 상대 것을 집어 오지 않는다."""
    out = None
    rows = store.db.execute(
        "SELECT rowid AS id, key, created FROM ask_cache "
        "WHERE key LIKE '%~thread:%' ORDER BY created, rowid")
    for r in rows:
        m = _THREAD_SCOPE_RX.search(r["key"])
        if m and int(m.group(2)) == int(tid):   # created 오름차순 → 마지막이 최신
            out = {"id": r["id"], "created": r["created"] or "",
                   "basis": int(m.group(1))}
    return out


_PERSON_SCOPE_RX = re.compile(r"@(\d+)~([^#]+?)(?:#\d+)?$")


def _person_analysis(store, addr: str) -> dict | None:
    """이 사람의 저장된 심층 분석 {id, created, basis, headline} — 최신 것 하나.

    키 형식 v3:질문@<basis>~<주소>. 스레드·메일 범위는 `thread:`·`mail:` 접두가
    붙어 서로 집어 오지 않는다(_thread_analyses 와 같은 관례). LIKE 는 후보만
    좁히고 판정은 정규식이 한다 — 주소의 `_` 가 LIKE 와일드카드이기 때문이다.
    """
    addr = (addr or "").strip().lower()
    if not addr:
        return None
    out = None
    rows = store.db.execute(
        "SELECT rowid AS id, key, created, result_json FROM ask_cache "
        "WHERE key LIKE ? ORDER BY created, rowid", (f"%~{addr}%",))
    for r in rows:                      # created 오름차순 → 마지막이 최신
        m = _PERSON_SCOPE_RX.search(r["key"] or "")
        if not m or m.group(2).strip().lower() != addr:
            continue
        try:
            head = str((json.loads(r["result_json"]) or {}).get("headline") or "")
        except (ValueError, TypeError):
            head = ""
        out = {"id": r["id"], "created": r["created"] or "",
               "basis": int(m.group(1)), "headline": head.strip()}
    return out


def _thread_map_controls(store, tid: int, hit: dict | None) -> str:
    """스레드 머리의 쟁점 분석 진입 — 없으면 버튼, 있으면 보기 링크+다시.

    낡음 문법은 메일 분석과 동일: 경과일 + 그 뒤 이 스레드에 도착한 새 메일 수."""
    if not hit:
        # **채움(색 반전)은 분석 페이지를 여는 큰 작업에만**(2026-08-18 사용자 확정).
        # 옆의 현안 브리핑(1콜·제자리)과 무게가 글자 없이 갈린다.
        return ("<span class='tmap dim'><form method='post' action='/ask/jobs'>"
                f"<input type='hidden' name='tid' value='{int(tid)}'>"
                "<button class='aibtn compact' "
                "title='쟁점별 입장과 현재 상태 — 조사 라운드까지 도는 큰 분석"
                "(최대 12콜, 수 분). 현안 브리핑으로 부족할 때 쓴다'>"
                # 비용은 **아직 없을 때만** 말한다(인물 빈 카드와 같은 관례,
                # 2026-08-19). 한 번 만들고 나면 꼬리는 사라지므로 자주 쓰는
                # 사람 화면에는 글자가 늘지 않는다. 툴팁은 마우스를 올려야
                # 보이고 터치·키보드에선 안 보여서 그 자리를 대신하지 못한다.
                "쟁점별 입장까지 보기 <span class='cost'>· 수 분</span>"
                "</button></form></span>")
    ago = _days_ago((hit["created"] or "")[:10], date.today().isoformat())
    label = f"쟁점 분석 보기 · {ago}" if ago else "쟁점 분석 보기"
    fresh = store.db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND ingest_seq > ?",
        (int(tid), hit["basis"])).fetchone()[0]
    stale = f" <span class='dim'>이후 새 메일 {fresh}통</span>" if fresh else ""
    return ("<div class='tmap'>"
            f"<a class='aibtn ghost compact' href='/ask?id={int(hit['id'])}'>"
            f"{esc(label)}</a>{stale}"
            "<form method='post' action='/ask/jobs'>"
            f"<input type='hidden' name='tid' value='{int(tid)}'>"
            "<input type='hidden' name='fresh' value='1'>"
            # '다시'만으로는 무엇을 다시 하는지 알 수 없다 — 바로 왼쪽이
            # '브리핑 갱신'이라 더 헷갈렸다(2026-08-18 사용자 보고).
            "<button class='aibtn compact' "
            "title='쟁점 분석을 새로 실행 — 조사 라운드(최대 12콜, 수 분)'>"
            "쟁점 분석 다시</button></form></div>")


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


def _note_card(store, cfg, tid: int, path, editing: bool,
               conflict: bool = False) -> str:
    """[내 노트] 카드 — 보기/편집 두 모습(2026-08-11 인라인 편집기).

    편집 모드의 본문은 **파일을 직접 읽는다**: 이 상자가 곧 덮어쓸 그 파일을
    보여야 하고, 색인은 잠긴 파일을 건너뛰었을 수 있다. 보기 모드는 종전대로
    색인(note_row)을 쓴다 — '검색에 걸린 문장이 화면에 있다'는 계약 유지.

    화면에는 사람 본문만 나온다(meta 는 절대 노출하지 않는다 — 사용자 요구).
    """
    from . import notes as notes_mod
    if path is None and not editing:
        return ""
    name = path.name if path is not None else "새 노트"
    head = f"<summary>내 노트 <span class='dim'>{esc(name)}</span>"

    if not editing:
        row = store.note_row(tid)
        body = (row["content"] if row else "") or ""
        try:            # 수정 시각 — 노트 mtime 은 파일시스템의 **로컬** epoch
            mt = path.stat().st_mtime      # 이라 UTC 용 _utc_to_local_stamp 를
        except OSError:                    # 쓰면 9시간 어긋난다(2026-08-11).
            mt = 0.0
        if mt:
            dt = datetime.fromtimestamp(mt)
            ago = _days_ago(dt.date().isoformat(), date.today().isoformat())
            tip = ("마지막 수정: " + _fmt_stamp(dt.isoformat())
                   + (f" · {ago}" if ago else ""))
            head += (f" <span class='ihint' title='{esc(tip)}' "
                     f"aria-label='{esc(tip)}'>ⓘ</span>")
        inner = (_md_to_html(body) if body.strip()
                 else "<p class='dim'>아직 비어 있습니다 — [편집]으로 "
                      "채워 주세요.</p>")
        return (f"<details class='mynote' open>{head}</summary>{inner}"
                "<div class='noterow'>"
                f"<a class='btn' href='/thread/{tid}?note=edit'>편집</a>"
                f"<form method='post' action='/thread/{tid}/note'>"
                "<button title='기본 연결 프로그램으로 엽니다'>"
                "외부 편집기 ✎</button></form></div></details>")

    # ── 편집 모드
    body, base = "", 0.0
    if path is not None:
        try:
            body = notes_mod.note_body(path.read_text(encoding="utf-8"))
            base = path.stat().st_mtime
        except OSError:
            path = None
    if path is None:                    # 파일은 첫 저장 때 생긴다 — 지금은 초안만
        try:
            body = notes_mod.note_template_body(store, tid)
        except notes_mod.NoThread:
            body = ""
    warn = ("<div class='noteconf'>다른 곳에서 이 노트가 바뀌었습니다 — "
            "덮어쓰지 않았습니다. 아래 글을 그대로 저장하면 그 변경을 "
            "대신합니다.</div>" if conflict else "")
    dc = " data-conflict='1'" if conflict else ""
    # base(파일 수정 시각)는 충돌 검사용이자 **우리 폼이라는 표식**이다:
    # parse_qs 가 빈 값을 키째로 버려서 body 만으로는 '빈 저장'과 '엉뚱한
    # POST'를 구별할 수 없다. repr 로 넘겨야 왕복이 정확하다(1e-6 오차 경계).
    return (f"<details class='mynote' open>{head}</summary>{warn}"
            f"<form class='noteedit' method='post'{dc} "
            f"action='/thread/{tid}/note-save'>"
            f"<input type='hidden' name='base' value='{base!r}'>"
            "<textarea name='body' rows='16' spellcheck='false' "
            f"placeholder='여기에 씁니다'>{esc(body)}</textarea>"
            "<p class='notehint'>마크다운을 쓸 수 있습니다 · "
            "비우고 저장하면 노트가 삭제됩니다</p>"
            "<div class='noterow'><button class='btn-primary'>저장</button>"
            f"<a class='btn' href='/thread/{tid}'>취소</a>"
            "<button name='ext' value='1' class='btn-quiet' "
            "title='저장하고 기본 연결 프로그램으로 엽니다'>외부 편집기 ✎"
            "</button></div></form></details>")


def render_thread(store, cfg, tid: int, qs=None) -> str:
    d = format_detail(store, cfg, tid)
    t = store.thread(tid)
    # 노트 색인을 파일과 맞춘다(2026-08-11) — 외부 편집기로 고치고 돌아와 새로
    # 고침하면 화면·검색·AI 가 같은 내용을 본다. 파일 몇십 개 stat 이라 싸다.
    from . import notes as notes_mod
    notes_mod.reindex(cfg, store)
    note_path = notes_mod.find_thread_note(cfg, tid)
    q = qs or {}
    editing = (q.get("note") or [""])[0] == "edit"
    conflict = (q.get("noteconflict") or [""])[0] == "1"
    # 원격 이미지를 이 화면에서만 되살린다(?images=1). 저장 HTML 은 그대로고,
    # 실제 로드는 **브라우저가 직접** 한다 — 서버는 밖으로 나가지 않는다.
    # do_GET 이 이 요청의 응답에만 CSP img-src 를 풀어 준다.
    show_images = (q.get("images") or [""])[0] == "1"
    # sticky 헤더(제목): 센티널이 화면을 벗어나면 app.js(hookThreadHead)가
    # .stuck 을 붙여 컴팩트(1줄 말줄임)로. 액션 바는 sticky 밖 — f/h 키가 대체.
    # (신호 칩 ↩/⏰/☑ 은 2026-07-30 제거 — 판정 정밀도가 낮아 신뢰를 깎았다.)
    out = ["<div class='sticksentinel'></div>",
           f"<div class='threadhead'><h1>{esc(d['title'])}</h1></div>"]
    if show_images:
        out.append("<div class='imgnote'>⚠ 원격 이미지를 불러왔습니다 — 이 화면에서만 "
                   "적용되며, 발신자에게 열람 사실과 쿠키가 전달될 수 있습니다 "
                   f"<a class='imgshow' href='/thread/{tid}'>안전 보기로</a></div>")
    if t:
        has_attach = any(blk["attach"] for blk in d["timeline"])
        # 결정자 기본값 = 최신 수신 메일 발신인 (타임라인은 최신 먼저)
        decider = next((blk["sender"] for blk in d["timeline"]
                        if not blk["is_sent"]), "")
        out.append(_actions_bar(tid, t, has_attach, decider=decider,
                                has_note=note_path is not None,
                                editing=editing))
        # 쟁점 분석은 스레드 머리에서 내려왔다(2026-08-16) — 아래 진단 줄에
        # 부가 링크로 붙는다. 한 스레드에 AI 버튼이 둘이면 사용자는 어느 쪽을
        # 눌러야 하는지 알 수 없고, 값이 큰 쪽(진단 1콜)이 12콜짜리와 같은
        # 무게로 놓인다.
    out.append("<div class='analysis'>")
    smeta = d.get("summary_meta")
    diag = (smeta or {}).get("diag") or []
    badged = False
    for a in d["analysis"]:
        if not a:
            continue
        # 요지 본문 줄(· 확정 — …)은 아래 카드가 그린다 — 여기선 건너뛴다
        if diag and a.startswith("· "):
            continue
        # "[롤링" 은 구버전 저장 노트 호환용 (표시 문구는 "누적 요약"으로 개명)
        cls = (" class='sig'" if a.startswith(("[누적", "[롤링", "[스레드 진단",
                                              "[현안"))
               else "")
        line = esc(a)
        if cls and smeta and not badged:
            # 배지는 머리줄 1회만 — 요약 본문 줄이 우연히 '[누적…'으로 시작해도
            # 두 번 그리지 않는다(2026-08-11). 문구는 쟁점 분석의 낡음 표시
            # (_thread_map_controls)와 같은 관용구를 쓴다.
            badged = True
            if smeta.get("gap", 0) >= 7:
                # 진단이 다룬 마지막 메일이 오래됐다 — 맞는 지적이어도 그 뒤
                # 회의·결정으로 해소됐을 수 있다는 것을 화면이 먼저 말한다
                line += (f" <span class='stale'>마지막 메일 "
                         f"{smeta['gap']}일 전</span>")
            if smeta["fresh"]:
                line += (f" <span class='dim'>이후 새 메일 "
                         f"{smeta['fresh']}통</span>")
            if smeta["updated"]:
                ago = _days_ago(smeta["updated"][:10], date.today().isoformat())
                tip = (("현안 브리핑" if diag else "누적 요약") + " 마지막 갱신: "
                       + smeta["updated"] + (f" · {ago}" if ago else ""))
                line += (f" <span class='ihint' title='{esc(tip)}' "
                         f"aria-label='{esc(tip)}'>ⓘ</span>")
        out.append(f"<div{cls}>{line}</div>")
        if cls and diag:
            out.append(_diagnosis_card(diag))
    # 진단은 이 버튼에서만 만들어진다(2026-08-16 — 요지를 흡수).
    if t:
        deeper = (_thread_map_controls(store, tid, _thread_analyses(store, tid))
                  if len(d["timeline"]) >= THREAD_MAP_MIN_MSGS else "")
        out.append(_diagnose_controls(tid, bool(smeta), deeper))
    out.append("</div>")
    # 내 노트(2026-08-11) — 파일에 쓴 것을 원래 스레드에서 다시 보고, 여기서
    # 바로 고친다. AI 요약(위 analysis)과 나란히 놓여 'AI 대 내 기록'이 한
    # 화면에 잡힌다.
    out.append(_note_card(store, cfg, tid, note_path, editing, conflict))
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
        # 참여자(발신자) 이름 클릭 → 그 사람 도시에(왼쪽). 내 발신은 링크 없음.
        if not blk["is_sent"] and blk.get("sender_addr"):
            who = (f"<a href='/people?addr={_q(blk['sender_addr'])}' "
                   f"title='이 사람 도시에'>{esc(blk['sender'])}</a>")
        else:
            who = esc(blk["sender"])
        # 머리글 조각 — 전부 메일 헤더에서 온 사용자 콘텐츠라 본문도 title 도 esc().
        # 제목 툴팁은 전문이 보이는데도 남긴다 — 좁은 폭에서 말줄임될 때 전문을
        # 볼 길이 그것뿐이다(실측: 창 1100px 이상이면 잘리는 제목이 없다).
        subj = (f"<span class='mh-subj' title='제목: {esc(blk['subject'])}'>"
                f"{esc(blk['subject'])}</span>") if blk["subject"] else ""
        tip = f" title='{esc(blk['to_full'])}'" if blk["to_full"] else ""
        to = f"<span class='mh-to'{tip}>{esc(blk['to_label'])}</span>"
        att = (f"<span class='mh-att' title='첨부 {len(blk['attach'])}개: "
               f"{esc('; '.join(blk['attach']))}'>"
               f"📎 {esc(_outof(blk['attach'], '개'))}</span>") if blk["attach"] else ""
        out.append(f"<div class='msg' id='msg-{blk['id']}'>")
        out.append(
            f"<div class='mhead{sent}'>"
            "<div class='mh-r1'>"
            # '누가 → 누구에게'는 한 쌍이라 붙여 둔다. mgone 은 메일의 상태 배지라
            # 그 쌍을 가르지 않고 날짜 쪽 메타 정보에 붙인다.
            f"<span class='mh-who'>{arrow} {who}</span>{to}"
            # Outlook 에 없는 메일 — 내용은 여기 남아 있지만 원문 열기는 안 된다.
            # 알려 주지 않으면 사용자는 [원문] 을 누르고 나서야 안다.
            + ("<span class='mgone' title='Outlook 에서 지웠거나 수집 범위 밖 "
               "폴더로 옮긴 메일입니다. 내용은 여기 남아 있고 미답변 판정에서는 "
               "빠집니다'>Outlook에 없음</span>" if blk.get("gone") else "")
            + f"<span class='mh-when'>{esc(blk['sent_on'])}</span>"
            f"{_mail_ai_controls(store, blk['id'], analyses.get(blk['id']), tid)}"
            "</div>"
            # 제목이 원문 그대로가 된 뒤로는 사실상 늘 그려진다. 조건은 제목이
            # 빈 메일(캘린더 항목·수집 결손) 방어로 남긴다 — 빈 div 를 내면
            # gap 만큼 줄이 뜬다.
            + (f"<div class='mh-r2'>{subj}{att}</div>" if (subj or att) else "")
            + "</div>")
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
            if "data-blocked-src" in blk["html"] and not show_images:
                # 되살릴 수 있는 원격 이미지(추적 픽셀 제외)가 있을 때만 링크를
                # 단다 — 픽셀뿐인 메일에서 누르면 클릭이 곧 수신 확인이 된다.
                # cid 추출 실패만 있는 메일도 종전 문구 그대로(되살릴 게 없다).
                n_remote = len(_remote_imgs(blk["html"]))
                if n_remote:
                    out.append(
                        "<div class='imgnote'>🚫 원격 이미지 "
                        f"{n_remote}장 차단됨(추적 방지) "
                        f"<a class='imgshow' href='/thread/{tid}?images=1'>"
                        "⚠ 위험을 감수하고 보기</a></div>")
                else:
                    out.append(
                        "<div class='imgnote'>🚫 일부 이미지를 표시할 수 없습니다"
                        "(원격 차단 또는 추출 실패) — 원문은 Outlook에서</div>")
            # 꼬리 이미지 서명(임베드 PNG·height≤210·본문 뒤)은 "Signature 숨김"
            # 한 줄로 대체 — 공간만 먹는 로고·명함 카드 제거(clean.hide_image_signatures).
            mail_html = hide_image_signatures(blk["html"])
            if show_images:                  # 이 요청의 렌더에만 — 저장분은 그대로
                mail_html = show_remote_images(mail_html)
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


def _render_folder_scope(store, cfg) -> str:
    """수집 폴더 — 하위 재귀 켬/끔, 상한, 제외 목록.

    폴더 목록은 **마지막 수집이 본 것**을 저장해 둔 것이다(store.folder_view).
    설정 화면을 열 때마다 Outlook COM 을 부르지 않으려는 것 — 페이지 로드가
    사서함 순회에 묶이면 안 되고, Windows 밖에서는 아예 불가능하다.
    """
    on = bool(cfg.opt("sources", "include_subfolders", default=True))
    cap = cfg.opt("sources", "max_folders", default=50)
    excl = [str(x) for x in (cfg.opt("sources", "exclude_folders", default=[]) or [])]
    out = ["<h2>수집 폴더</h2>",
           "<p class='dim'>Outlook 규칙이 수신 메일을 하위 폴더로 자동 분류한다면 "
           "하위 폴더 수집이 켜져 있어야 합니다 — 꺼져 있으면 그 메일은 색인에 "
           "들어오지 않습니다. 지운 편지함·정크 메일은 항상 제외됩니다.</p>",
           # 끄는 쪽의 결과를 미리 말해 둔다 — 이미 들어온 메일이 남는다는 것이
           # 좋아 보이지만, 그 스레드만 갱신이 멈춰 '정체'로 오판될 수 있다.
           "<p class='dim'>폴더를 빼도 <b>이미 수집한 메일은 남습니다</b>. 다만 그 "
           "스레드는 새 메일이 안 들어와 멈춘 것처럼 보일 수 있습니다. 다시 넣으면 "
           "빠진 기간을 한 번에 메웁니다(그 한 번은 오래 걸립니다).</p>",
           "<form method='post' action='/settings/save'>"
           "<table class='settbl'>"
           "<tr><th>하위 폴더 수집</th><td>"
           # 체크박스는 꺼져 있으면 전송되지 않는다 — 앞의 hidden 이 '끔'을 나른다
           "<input type='hidden' name='include_subfolders' value='0'>"
           "<label><input type='checkbox' name='include_subfolders' value='1'"
           + (" checked" if on else "") + "> 받은 편지함 하위까지</label></td>"
           "<td class='dim'>기본 켬</td></tr>"
           "<tr><th>폴더 상한</th>"
           f"<td><input type='number' name='max_folders' value='{esc(str(cap))}' "
           "min='0' step='5' style='width:80px'></td>"
           "<td class='dim'>동시에 여는 폴더 수 (기본 50 · 0=무제한)</td></tr>"
           "</table><button class='btn-primary'>수집 폴더 저장</button></form>"]

    rows = store.folder_view()
    if rows:
        out.append("<p class='dim'>마지막 수집이 본 폴더입니다. 바꾼 내용은 다음 "
                   "동기화부터 적용됩니다.</p>")
        for r in rows:
            label = str(r.get("label") or "")
            if label in ("inbox", "sent"):
                continue                   # 루트는 끌 수 없다(끄면 앱이 무의미)
            why = str(r.get("reason") or "")
            # **설정으로 정해지는 것은 지금 설정을 보고, 저장된 이유는 쓰지
            # 않는다.** 저장된 이유는 지난 수집 시점의 설정을 비춘 것이라 사용자가
            # 방금 바꾼 것과 어긋난다 — 종전에는 제외를 풀어도 그때의 '제외 목록'이
            # 남아 그 행이 버튼 없이 갇혔다(2026-08-10). 설정과 무관하게 참인
            # 것(structural)만 저장분을 믿는다.
            # 사용자가 지금 빼 둔 것을 **가장 먼저** 본다. 이 순서라야 저장분의
            # 종류를 잘못 읽어도(구 값 추정 등) 사용자가 되돌릴 수 있다 —
            # structural 을 먼저 보면 그 행이 다시 버튼 없이 갇힌다.
            if label in excl:              # 사용자가 뺀 것 — 되돌릴 수 있어야 한다
                state, why, ctl = "off", "", ("folder-include", "포함")
            elif str(r.get("kind") or "") == "structural":
                state, ctl = "off", None
            elif not on and "/" in label:  # 하위 폴더 수집이 꺼져 있다
                state, why, ctl = "off", "하위 폴더 수집 꺼짐", None
            else:
                state, ctl = "on", ("folder-exclude", "제외")
                # 'capacity'(그때 자리가 없었다)만 참고로 남긴다. 설정의 거울인
                # 사유는 지금 설정이 이미 뒤집었으므로 적으면 거짓이 된다.
                why = why if str(r.get("kind") or "") == "capacity" else ""
            # 오른쪽 열은 '할 수 있는 일'이 있으면 버튼, 없으면 '못 하는 이유'.
            # 버튼이 있는데 참고 사유도 있으면 사유는 라벨 옆으로 보낸다.
            note = ""
            if ctl:
                right = (f"<form method='post' action='/settings/{ctl[0]}'>"
                         f"<input type='hidden' name='label' value='{esc(label)}'>"
                         f"<button>{ctl[1]}</button></form>")
                note = f" <span class='fwhy'>{esc(why)}</span>" if why else ""
            else:
                right = f"<span class='fwhy'>{esc(why)}</span>" if why else ""
            out.append(
                f"<div class='folderrow {state}'>"
                f"<span class='fstate'>{'● 수집' if state == 'on' else '○ 제외'}"
                f"</span><span class='fname'>{esc(label)}{note}</span>"
                f"{right}</div>")
    else:
        out.append("<p class='empty'>아직 수집한 적이 없어 폴더 목록이 없습니다 — "
                   "한 번 동기화하면 여기에 나옵니다.</p>")
    # 목록에 없는 폴더도 직접 넣을 수 있어야 한다(수집 전, 또는 새로 만든 폴더)
    out.append("<form method='post' action='/settings/folder-exclude' "
               "class='setadd'><input type='text' name='label' "
               "placeholder='제외할 폴더 이름 또는 inbox/보관'> "
               "<button>제외 추가</button></form>")
    # 위 목록에 이미 '○ 제외'로 보이는 것은 다시 적지 않는다. 여기 남는 것은
    # **아직 본 적 없는 폴더**(수집 전에 미리 넣었거나, 이름이 바뀌었거나,
    # 사라진 폴더)뿐이라 그게 곧 "왜 목록에 없지"의 답이 된다.
    seen = {str(r.get("label") or "") for r in rows}
    ghost = [x for x in excl if x not in seen]
    if ghost:
        out.append("<p class='dim'>목록에 없는 제외 항목 — 아직 수집에서 본 적 "
                   "없는 폴더입니다: "
                   + " · ".join(f"<span class='mono'>{esc(x)}</span>"
                                for x in ghost)
                   + "</p>")
    return "\n".join(out)


# 상태 넷 — **'안 된다'와 '느리다'를 가른다.** 사내 게이트웨이를 거치는 CLI 는
# 한 단어 답에도 30초를 넘길 수 있는데, 그걸 '실패'로 찍으면 고장으로 읽힌다.
# 상한은 백엔드마다 다르다(review.aitest_timeout) — opencode 는 콜드 스타트만
# ~20초라 claude 기준 30초로 재면 멀쩡한 백엔드가 늘 '무응답'이 된다. 값을 여기
# 두면 CLI `diagnose` 와 갈라지므로 엔진 한 곳에서만 정한다.
_AITEST_TIMEOUT = review.AITEST_TIMEOUT   # 점검 1콜의 상한(엔진 기본 300s 와 별개)
_AI_MARK = {"have": ("●", "있음", "ok"), "ok": ("●", "응답", "ok"),
            "slow": ("▲", "무응답", "warn"), "fail": ("■", "실패", "fail"),
            "none": ("·", "없음", "none"),
            # 래퍼 명령 — 런처는 있는데 그 안은 못 본다. 경고가 아니라 미지(未知)라
            # 흐린 색을 쓴다(없음과 같은 계열). '확인 필요'가 아니라 '확인 안 됨'인
            # 이유: 잘 쓰고 있는 사람도 재시작마다 이 줄을 본다(시험 결과가
            # 인메모리다). 사실만 말하고 시키지 않는다.
            "wrap": ("○", "확인 안 됨", "none"),
            # 응답은 왔는데 설정이 안 먹었다 — '응답'으로 적으면 조용한 실패가
            # 된다. 고장은 아니므로 warn(▲) 이고 처방을 함께 단다.
            "setup": ("▲", "설정 안 먹음", "warn")}
_AI_FIX = {
    "setup": "부른 것은 대답했지만 지정한 설정이 적용되지 않았습니다. 그대로 두면 "
             "토큰이 몇 배로 나가고 메일 본문에 도구가 열립니다 — "
             "docs/OPENCODE-WINDOWS.md §1.1 의 자리와 파일 이름을 확인하세요.",
    "fail": "이 AI 가 응답하지 않습니다. 로그인이 풀렸거나 이 모델을 쓸 권한이 "
            "없을 수 있습니다. 위 표에서 다른 AI 로 바꿀 수 있습니다.",
    "slow": "원래 느린 AI 일 수 있으니 한 번 더 눌러 보세요. 계속 이러면 "
            "터미널에서 직접 실행해 로그인·프록시를 확인하세요 — 모델이 "
            "없는 것과는 다른 증상입니다.",
}

# 이름 넷만 보고는 고를 수 없어서 성격을 적는다 — 다만 **선택지 안이 아니라
# 표 위 범례에** 적는다. `<select>` 는 가장 긴 선택지에 맞춰 폭이 정해지므로
# 설명을 넣으면 2열이 8칸 → 45칸으로 부풀고, 같은 표를 쓰는 숫자 입력 여섯 줄까지
# 그 폭에 끌려가 설명 열을 밀어낸다(2026-08-26 실측). 범례는 colspan 한 줄이라
# 열 폭을 건드리지 않는다.
_BACKEND_NOTE = {
    "sonnet": "기본",
    "opus": "가장 똑똑함(느리고 비쌈)",
    "haiku": "가장 빠르고 쌈",
}
_BACKEND_ORDER = ("sonnet", "opus", "haiku", "internal")   # 기본에서 시작


def _backend_legend(cfg, names) -> str:
    """선택지 성격 한 줄 — 내장 아닌 이름은 실제로 부르는 실행 파일로 말한다."""
    parts = []
    for n in names:
        note = _BACKEND_NOTE.get(n)
        if not note:
            try:
                note = f"직접 지정({cfg.ai_cmd(n)[0]})"
            except SystemExit:               # 선언도 내장도 없는 이름
                continue
        parts.append(f"<b>{esc(n)}</b> {esc(note)}")
    return " · ".join(parts)


def _ai_roles_by_backend(cfg) -> dict[str, list[str]]:
    """백엔드 이름 → 그것을 부르는 역할 라벨들.

    해석은 `config.backend_for` 한 곳이다 — 여기서 규칙을 다시 만들면 화면이
    엔진과 갈라진다(2026-08-19 doctor 가 그랬다).
    """
    out: dict[str, list[str]] = {}
    for role in cfg._ROLES:
        name = cfg.backend_for(role)
        if name:
            out.setdefault(name, []).append(cfgmod.ROLE_LABEL.get(role, role))
    return out


def _ai_backends(cfg) -> list[dict]:
    """이 PC 의 AI 백엔드 목록 — 내장 넷(claude 모델 셋 + opencode) + config 선언분.

    **역할이 쓰는 것만 보면 안 된다.** 백엔드를 바꾸려는 사람이 알고 싶은 것은
    "지금 무엇을 고를 수 있나"이고, 그건 안 쓰는 모델까지 물어봐야 안다
    (haiku 를 지금 아무 역할도 안 쓰지만, 쓸 수 있는지는 알아야 고른다).
    """
    import shutil

    roles = _ai_roles_by_backend(cfg)
    names = ["sonnet", "haiku", "opus", "internal"]
    names += [n for n in sorted(cfg.ai_backends) if n not in names]
    rows = []
    for name in names:
        try:
            cmd = cfg.ai_cmd(name)
        except SystemExit:                       # 선언도 내장도 없는 이름
            continue
        # cmd[0] 은 래퍼면 런처다(`wsl.exe`). 그것만 보이면 어느 백엔드든
        # 똑같이 `wsl.exe` 로 뜨고, which(런처) 성공을 **백엔드가 있다**는 뜻으로
        # 말하게 된다 — opencode 를 지워도 `● 있음` 이었다(2026-08-30).
        launcher = Path(str(cmd[0])).name
        prog = review.backend_program(cmd)
        wrapped = bool(prog) and prog.lower() != Path(launcher).stem.lower()
        # 래퍼가 **아닐 때는 cmd[0] 을 그대로** 둔다 — 경로로 선언한 백엔드까지
        # 표시가 바뀔 이유가 없다(이 변경의 대상은 래퍼뿐이다).
        rows.append({"name": name, "cmd": cmd,
                     "binary": f"{prog} ({launcher})" if wrapped else cmd[0],
                     "where": shutil.which(cmd[0]), "wrapped": wrapped,
                     "roles": roles.get(name, [])})
    return rows


def _run_aitest_job(cfg) -> None:
    """AI 응답 시험 워커 — PATH 에 있는 백엔드마다 짧은 호출 1회.

    없는 것은 부르지 않는다(호출이 아니라 사실이다 — '없음'으로 적는다).
    """
    rows = {}
    for b in _ai_backends(cfg):
        if not b["where"]:
            rows[b["name"]] = ("none", "이 PC 에 설치돼 있지 않습니다")
            continue
        limit = review.aitest_timeout(b["cmd"])
        # '돌긴 도는데 설정이 안 먹은' 상태는 예외로 안 온다 — 성공 경로의
        # stderr 에만 있다. 그것을 잡으려고 시험이 존재한다(§1.1).
        # **opencode 에만 on_event 를 넘긴다.** on_event 가 있으면 ai_run 이
        # 스트리밍 경로로 가는데, claude 까지 그렇게 하면 점검 콜이 종전 블로킹
        # 에서 stream-json 으로 바뀐다 — 잘 쓰던 사람에게 없던 위험을 새로 만든다.
        # 이 신호(setup 경고)를 내는 백엔드는 지금 opencode 뿐이라 대가가 없다.
        notes: list = []

        def _note(info: dict, _n=notes) -> None:
            if info.get("ev") == "notice":
                _n.append(str(info.get("text") or ""))

        watch = _note if review._is_opencode_cmd(b["cmd"]) else None
        try:
            out = review.ai_run(b["cmd"], "한 단어로만 답하라. 정상이면 OK.",
                                timeout=limit, retries=0, on_event=watch)
            line = (out.splitlines() or [""])[0][:60]
            # 응답했지만 설정이 안 먹었으면 '응답'으로 넘기지 않는다 — 그게
            # 조용한 실패의 정의다. 색은 warn(▲)이고 처방은 아래 _AI_FIX.
            rows[b["name"]] = (("setup", notes[0]) if notes
                               else ("ok", line))
        except review.AITimeout:    # 안 되는 것이 아니라 늦는 것 — 갈라 적는다
            rows[b["name"]] = ("slow", f"{limit}초 안에 응답 없음")
        except Exception as e:      # AIError·AIAuthError·OSError 전부 한 줄로
            rows[b["name"]] = ("fail",
                               " ".join(str(e).split())[:160] or type(e).__name__)
    with _aitest_lock:
        _aitest_job.update(running=False, rows=rows, at=time.strftime("%H:%M"))


def _ai_status_html(cfg) -> str:
    """설정 › AI 백엔드 상태 — **런타임에 필요한 것만**.

    설치 시점 판정(Python·tomllib·Outlook COM·config.toml·DB 유무)은 넣지 않는다.
    이 화면이 보인다는 것 자체가 웹이 떴다는 뜻이라 그 판정들은 스스로 답이 돼
    있다(2026-08-20 사용자 지적 — "이미 web 이 떴는데 억지로 만들 필요는 없다").

    웹이 떠 있어도 알 수 없는 것은 하나다 — **어느 모델이 실제로 대답하는가.**
    `doctor` 는 실행 파일이 PATH 에 있는지까지만 보고, claude CLI 는 모델마다
    권한이 다를 수 있다(현안 브리핑의 opus 가 대표적). opencode 는 안 깔린 것이
    보통이라 **경고가 아니라 '없음'** 으로 적는다 — 안 쓰면 무방하다.
    """
    with _aitest_lock:
        aj = dict(_aitest_job)
    tested = aj.get("rows") or {}
    rows = _ai_backends(cfg)

    out = ["<h2>이 PC 에서 쓸 수 있는 AI</h2>", "<div class='aichk'>"]
    for b in rows:
        got = tested.get(b["name"])
        key = got[0] if got else (
            "wrap" if b.get("wrapped") and b["where"]
            else "have" if b["where"] else "none")
        mark, word, cls = _AI_MARK[key]
        detail = got[1] if got else (
            "[응답 시험]으로 확인하세요" if key == "wrap"
            else "" if b["where"] else "이 PC 에 설치돼 있지 않습니다")
        role_s = " · ".join(b["roles"]) or "쓰는 기능 없음"
        out.append(
            f"<div class='airow {cls}'><span class='aimark'>{mark} {word}</span>"
            f"<span class='ainame'>{esc(b['name'])}</span>"
            f"<span class='aibin'>{esc(b['binary'])}</span>"
            f"<span class='airole'>{esc(role_s)}</span>"
            f"<span class='aidetail'>{esc(detail)}</span></div>")
        if key in _AI_FIX:
            out.append(f"<div class='aifix'>→ {esc(_AI_FIX[key])}</div>")
    if aj["running"]:
        out.append("<div data-aitest-running='1' class='airow'>"
                   "<span class='spin'></span> AI 에 물어보는 중…"
                   " <span class='dim'>완료되면 자동 전환</span>"
                   "</div>")
    else:
        out.append("<form method='post' action='/settings/aitest'>"
                   "<button class='aibtn ghost compact'>응답 시험</button>"
                   + (f" <span class='dim'>{esc(aj['at'])} 기준 · 다시 물어보기</span>"
                      if tested else
                      " <span class='dim'>설치된 AI 마다 한 번씩 실제로 "
                      "불러 봅니다</span>")
                   + "</form>")
    out.append("</div>")
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
    cur_skin = _skin_ok(cfg.opt("web", "skin", default=_DEFAULT_SKIN))
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
               # 고른 설정이 조용히 풀린다). 암묵지 후보의 '저장/유보'와 같은 방식.
               + _sbtn("bento", "카드형", _GRID) + "</div>")

    # ── 판정 기준 (런타임 편집 → overrides.json 영구 저장) ──
    from . import ask as ask_mod           # 지연 import (web ↔ ask 순환 방지)
    smd = cfg.opt("ai", "summary_max_days", default=1)
    def _num(name, val, note):
        return (f"<tr><th>{esc(note[0])}</th>"
                f"<td><input type='number' name='{name}' value='{esc(str(val))}' "
                f"min='{note[1]}' style='width:70px'></td>"
                f"<td class='dim'>{esc(note[2])}</td></tr>")
    known = set(list(cfg.ai_backends) + list(cfgmod._BUILTIN_BACKENDS))
    backends = [b for b in _BACKEND_ORDER if b in known]
    backends += sorted(known - set(backends))
    def _sel(name, cur):
        # value 는 백엔드 이름, 표시는 설명 붙은 글자다. 둘을 붙여 두면 설명이
        # 그대로 백엔드 이름으로 저장돼 설정이 깨진다.
        opts = "".join(
            f"<option value='{esc(b)}'{' selected' if b == cur else ''}>"
            f"{esc(b)}</option>"
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
               ("분석이 한 번에 읽는 양", 0,
                "고른 AI 가 감당하는 크기에 맞춥니다. 모르면 그대로 "
                "(0=제한 없음)")))
    out.append("<h2>판정 기준</h2>")
    out.append("<form method='post' action='/settings/save'>"
               "<table class='settbl'>" + num_rows
               + "<tr><td colspan='3' class='dim'><b>어떤 AI 가 할까</b> — "
                 "기능마다 다른 모델을 쓸 수 있습니다. 잘 모르겠으면 그대로 "
                 "두세요.<br>" + _backend_legend(cfg, backends) + "</td></tr>"
               + "<tr><th>일일 회고·요약</th>"
               + _sel("summary_backend", cfg.ai_summary_backend)
               + f"<td class='dim'>하루 회고 · 스레드 요약 · 인물 카드 · "
                 f"{review.DAILY_ETA}</td></tr>"
               + "<tr><th>AI 검색</th>" + _sel("search_backend", cfg.ai_search_backend)
               + "<td class='dim'>흐릿한 기억으로 찾기</td></tr>"
               + "<tr><th>분석</th>" + _sel("ask_backend", cfg.ai_ask_backend)
               + "<td class='dim'>질문에 근거를 달아 답합니다 · 보통 수 분</td></tr>"
               + "<tr><th>현안 브리핑</th>"
               + _sel("diagnose_backend", cfg.ai_diagnose_backend)
               + "<td class='dim'>스레드·인물에서 [현안 브리핑]을 누를 때 · "
                 "보통 1분 안</td></tr>"
               + "<tr><th>주간 보고</th>"
               + _sel("weekly_backend", cfg.backend_for("weekly"))
               + f"<td class='dim'>{_weekly_eta(1)}</td></tr>"
               + "</table><button class='btn-primary'>판정 기준 저장</button></form>")
    out.append(_ai_status_html(cfg))

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

    out.append(_render_folder_scope(store, cfg))

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
    ("max_folders", "sources", "max_folders", 0),           # 0=폴더 수 무제한
]
_NOISE_LISTS = {"ignore_senders", "subject_noise_strong", "subject_noise_weak"}
# 체크박스 필드 — 꺼져 있으면 브라우저가 아무것도 안 보내므로 폼이 hidden '0' 을
# 앞세운다. 그래서 값을 **마지막 것**으로 읽어야 체크가 이긴다.
_SETTINGS_BOOLS = [("include_subfolders", "sources", "include_subfolders")]


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
    for field_, sec, key in _SETTINGS_BOOLS:
        vals = form.get(field_)
        if not vals:
            continue          # 이 폼이 아예 다루지 않는 항목 — 건드리지 않는다
        cfgmod.set_override(home, sec, key, vals[-1].strip() == "1")
        n += 1
    for field_, key in [("summary_backend", "summary"),
                        ("search_backend", "search"), ("ask_backend", "ask"),
                        ("diagnose_backend", "diagnose"),
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


def _save_folder_exclude(cfg, form: dict, exclude: bool) -> str:
    """수집 폴더 제외 목록 add/remove → [sources] exclude_folders."""
    label = (form.get("label") or [""])[0].strip()
    if not label:
        return "/settings?msg=" + _q("폴더 이름이 비었습니다")
    if label in ("inbox", "sent"):
        # 이 둘을 빼면 앱이 볼 메일이 없어진다 — 실수로 막다른 길에 들어가지 않게
        return "/settings?msg=" + _q("받은편지함·보낸편지함은 제외할 수 없습니다")
    cur = [str(x) for x in (cfg.opt("sources", "exclude_folders", default=[]) or [])]
    if exclude:
        if label not in cur:
            cur.append(label)
    else:
        cur = [x for x in cur if x != label]
    cfgmod.set_override(cfg.home, "sources", "exclude_folders", cur)
    return "/settings?msg=" + _q(
        f"{label} {'제외' if exclude else '포함'} — 다음 동기화부터 적용")


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
    # '← …'는 실제 링크다(2026-08-18) — 기본값은 그 사람 화면이고, 좌측 이력이
    # 있으면 app.js 가 왔던 화면으로 갈아 끼운다(paintUpLink).
    out = ["<div class='personhead'>"
           f"<a href='/people?addr={_q(addr)}' class='uplink'>"
           f"← {esc(name[:12])}</a>"
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
# 이미 추출된 메일·액션·신호를 사람 중심으로 재조립한 결정론 화면.
# 정체성 기준 = 이메일 주소(동명이인 자동 분리). 이름 매칭 카드(최근 변화)는
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


_DCLAIM_REF_RX = re.compile(r"^\[#(\d+)\]\s*(.+)$")


def _cohort_line(store, addr: str) -> str:
    """'자주 같이 있는 사람' 한 줄 — 프로필 카드 각주(2026-08-18, 사용자 요청).

    사람을 아는 데는 그 사람이 **누구와 같이 도는지**가 크게 들어간다. 결정론
    값이라 AI 카드가 없어도 보인다(프로필을 아직 안 만든 사람에게 더 쓸모 있다).
    이름을 누르면 그 사람 화면으로 — 인물 사이를 건너다닐 수 있게 된다.
    """
    co = store.person_cohorts(addr)
    if not co:
        return ""
    links = " · ".join(
        f"<a href='/people?addr={_q(c['addr'])}' "
        f"title='함께 있는 스레드 {c['threads']}개'>{esc(c['name'])}</a>"
        for c in co)
    return f"<p class='cohort'><span class='clbl'>자주 같이 있는 사람</span>{links}</p>"


def _dossier_ai_card(dz, unreflected: int = 0, today: str = "",
                     addr: str = "", cohort: str = "") -> str:
    """AI 요약 카드 — 캐시된 dossier_md(## 섹션 + '- [#N] 서술') 렌더. 각 줄에
    근거 스레드 링크, 하단에 갱신일·추정 안내. 근거 검증은 생성 시(distill) 완료.

    갱신이 자동이 아니게 된 뒤로는 **얼마나 낡았는지**가 카드의 신뢰도 정보다 —
    푸터에 경과일과 반영되지 않은 새 메일 수를 함께 싣는다."""
    # '한 줄'(이 사람이 나에게 어떤 상대인가)은 라벨 없이 **리드 문단**으로 —
    # 슬롯 행에 넣으면 카드의 결론이 목록의 한 항목처럼 묻힌다(2026-08-18).
    body, lead, in_lead = [], "", False
    for raw in (dz["dossier_md"] or "").splitlines():
        s = raw.strip()
        if s.startswith("## "):
            name = s[3:].strip()
            in_lead = name == "한 줄"
            if not in_lead:
                body.append(f"<div class='dsec'>{esc(name)}</div>")
        elif s.startswith("- "):
            if in_lead:
                lead = lead or _md_inline(s[2:].strip())
            else:
                # 근거 번호는 **문장 뒤로**(2026-08-18) — 11자리 스레드 번호가
                # 줄 앞에 있으면 시선을 먼저 가져가 서술이 안 읽힌다. 링크는
                # 남기되 작게 뒤에 붙인다.
                claim, ref = s[2:].strip(), ""
                m = _DCLAIM_REF_RX.match(claim)
                if m:
                    claim, tid = m.group(2), m.group(1)
                    ref = (f" <a class='dref' href='/thread/{tid}' "
                           f"title='근거 스레드 #{tid}'>#</a>")
                body.append(f"<div class='dclaim'>{_md_inline(claim)}{ref}</div>")
    if lead:
        body.insert(0, f"<p class='dxlead'>{lead}</p>")
    upd = (dz["updated"] or "")[:10]
    cap = esc(upd) + " 갱신"
    ago = _days_ago(upd, today or date.today().isoformat())
    if ago and ago != "오늘":
        cap += f"({esc(ago)})"
    if unreflected > 0:
        cap += f" · 새 메일 {unreflected}통 미반영"
    else:
        # 버튼 줄을 없애며 사라졌던 안내를 카드 각주로 옮겼다(2026-08-18) —
        # '지금 눌러도 같은 내용'은 갱신 직전에 필요한 정보다.
        cap += " · 새 메일 없음(갱신해도 내용이 거의 같습니다)"
    # 갱신 버튼은 이 카드 머리에 둔다(2026-08-18) — 기능은 자기 산출물 옆에.
    redo = ("<form class='cardact' method='post' action='/people/dossier'>"
            f"<input type='hidden' name='addr' value='{esc(addr)}'>"
            "<button class='aibtn ghost compact'>다시 만들기</button></form>") if addr else ""
    return ("<div class='dcard aidoss'><h2>프로필 <span class='aitag'>AI 추정</span>"
            + redo + "</h2>"
            + "".join(body)
            + cohort + f"<p class='dcap'>{cap}</p></div>")


def render_dossier(store, cfg, addr: str) -> str:
    """단일 인물 도시에 — AI 요약(있으면 맨 위) + 결정론 카드. 빈 카드는 생략."""
    addr = (addr or "").strip().lower()
    if not addr:
        return "<h1>인물</h1><p class='empty'>주소가 없습니다</p>"
    name = store.person_name(addr) or addr
    win = int(cfg.opt("dossier", "window_weeks", default=26) or 26)
    m = report.person_metrics(store, cfg, addr, weeks=win)
    out = ["<div class='personhead'><a href='/people' class='uplink'>← 인물</a>"
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

    # AI 산출 셋을 **같은 모양의 카드**로 세운다(2026-08-18). 규칙은 하나다 —
    # **제목 아래에는 그 제목의 결과가 있다.** 종전에는 위에 버튼 줄이 있고
    # 심층 분석은 링크뿐이라 결과가 다른 화면에만 있었다: 화면만 봐서는 그
    # 기능이 무엇을 주는지 알 수 없었다. 만드는 버튼은 각 카드 안으로 들어갔고
    # (빈 카드에는 무엇을 얻는지 한 줄), 그래서 설명 문단도 버튼 줄도 없다.
    # 순서는 값이 싼 것부터: 현안 브리핑(1콜) → 심층 분석(수 분) → 프로필.
    out.append(_person_diagnosis_html(store, addr))
    out.append(_person_deep_card(store, addr))

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

    cohort = _cohort_line(store, addr)      # 결정론 — 프로필 유무와 무관하게 붙는다
    if has_ai:
        out.append(_dossier_ai_card(dz, unreflected, '', addr, cohort))
    else:
        # 프로필이 없거나(첫 방문) 형식이 바뀌어 숨겨졌을 때 — 만들 수 있는
        # 자리를 카드 자리에 그대로 둔다(버튼 줄에 두면 셋이 다시 늘어선다).
        why = ("프로필 형식이 바뀌어 다시 만들어야 합니다"
               if stale_row is not None else "아직 프로필이 없습니다")
        # AI 산출이 없을 때는 **결정론 정보가 먼저**다(2026-08-18) — '자주 같이
        # 있는 사람'은 AI 와 무관하게 늘 있는 사실이라, 안내 줄 뒤에 두면 실제
        # 내용이 "아직 없습니다" 아래로 밀린다.
        out.append(
            "<div class='dcard'><h2>프로필</h2>" + cohort
            + f"<div class='empty'>{why} — "
            "<form class='cardact' method='post' action='/people/dossier'>"
            f"<input type='hidden' name='addr' value='{esc(addr)}'>"
            "<button class='aibtn ghost compact'>만들기</button></form>"
            " <span class='dim'>AI 1콜</span></div></div>")

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
    # 내 노트도 함께 찾는다(2026-08-11) — 파일이 바뀌었을 수 있어 색인부터 갱신.
    nrows, krows = [], []
    if effective:
        from . import knowledge as knowledge_mod
        from . import notes as notes_mod
        notes_mod.reindex(cfg, store)
        nrows = store.search_notes(effective)
        knowledge_mod.reindex(cfg, store)
        krows = store.search_knowledge(effective)
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
                f"<span class='day'>{esc(_fmt_stamp(r['sent_on']))}</span>{snip}</div>")
        if nrows:
            # 내 노트 — 메일과 분모가 달라(사람이 쓴 파일) 섞지 않고 절을 나눈다
            out.append(f"<h2>내 노트 ({len(nrows)})</h2>")
            for n in nrows:
                snip = (f"<div class='snip'>{_snip_html(n['snippet'])}</div>"
                        if n["snippet"] else "")
                label = n["subject"] or f"#{n['thread_id']}"
                out.append(
                    f"<div class='item'>📝 <a href='/thread/{n['thread_id']}'>"
                    f"{esc(label)}</a>{snip}</div>")
        if krows:
            # 지식 — 사람이 승인해 vault/knowledge 에 남긴 암묵지(파일이 원본)
            out.append(f"<h2>지식 ({len(krows)})</h2>")
            for k in krows:
                snip = (f"<div class='snip'>{_snip_html(k['snippet'])}</div>"
                        if k["snippet"] else "")
                tid = next((t for t in (k["threads"] or "").split(";") if t), "")
                tlink = (f" <a href='/thread/{tid}'>#{tid}</a>" if tid else "")
                doc = f"/records?tab=knowledge&doc={_q(k['path'])}"
                out.append(f"<div class='item'>📄 <a href='{doc}'>"
                           f"{esc(k['title'])}</a>{tlink}{snip}</div>")
        if not rows and not nrows and not krows:
            out.append("<p class='empty'>결과 없음</p>")
    return "\n".join(out)


def _review_button_forms(day: str | None = None) -> str:
    # 결정론 데일리 리뷰는 이제 버튼 없이 lazy-on-view 로 자동 생성(_maybe_auto_review).
    # 남은 버튼은 'AI 회고' 하나 — 보고 있는 날짜의 리뷰에 AI 계층을 얹는다.
    # 과거 날짜면 run_ai_layer 가 그 날짜 작업(요약·수확·디제스트·하루 요약)만 실행.
    dt = f"<input type='hidden' name='date' value='{esc(day)}'>" if day else ""
    return ("<form method='post' action='/review'><input type='hidden' name='ai' value='1'>"
            f"{dt}<button class='aibtn ghost'>AI 회고</button>"
            f" <span class='dim'>· AI {review.DAILY_AI_CALLS}콜 · "
            f"{review.DAILY_ETA}</span></form>")


_DONE_KINDS = (("promise", "내 약속"), ("stalled", "오래 멈춘 스레드"),
               ("deadline", "기한"), ("shift", "변화"))
# 접기 목록의 상한. 기본 30 이면 한 종류를 31건 넘게 접었을 때 오래된 것이
# 화면에서 계속 빠지면서 **되돌릴 방법이 없어진다**(필터는 무제한이었다).
DONE_FOLD_LIMIT = 200


def _cards(cfg) -> bool:
    """리포트를 절 카드로 그릴지 — 벤토 스킨에서만."""
    return _skin_ok(cfg.opt("web", "skin", default=_DEFAULT_SKIN)) == "bento"


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
    if store is not None:
        out.append(_knowledge_cards(store, day))
    if md is None:
        out.append("<p class='empty'>해당 날짜에 저장된 리뷰가 없습니다.</p>")
    else:
        back = f"/records?tab=daily&date={_q(day)}"
        out.append(_md_to_html(md, back, _done_set(store), cards=_cards(cfg)))
        out.append(_done_fold(store, back))
    # 이 회고를 만드는 데 든 AI 콜 — 보관분(마지막 AI 실행)이라 완료 flash 가
    # 사라진 뒤에도, 다른 날과 비교할 때도 남아 있다. 결정론 회고·옛 회고·
    # 계측이 없는 백엔드에는 아무 줄도 안 붙는다.
    if store is not None:
        spent = review.fmt_meter(
            (review.load_ai_layer(store, day) or {}).get("meter"))
        if spent:
            out.append(f"<p class='dim'>{esc(spent)}</p>")
    return "\n".join(out)


def _kn_claim(cid: int, day: str) -> tuple[str, int]:
    """슬롯을 잡거나 줄을 선다 — ("start"|"queued"|"dup", 앞에 몇 건).

    한 락 안에서 판단해야 두 요청이 동시에 와도 한쪽만 "start" 를 받는다.
    `_job_start` 를 쓰지 않는 이유는 그것이 **줄을 못 세우기** 때문이다(다른 잡
    7종은 그 동작이 맞으므로 건드리지 않는다)."""
    with _kn_lock:
        if _kn_job["running"]:
            if cid == _kn_job["cid"] or cid in _kn_job["queue"]:
                return "dup", 0
            _kn_job["queue"].append(cid)
            return "queued", len(_kn_job["queue"])
        _kn_job.update(_JOB_STREAM)     # 이전 배수의 수신 상태를 지운다
        _kn_job.update(running=True, error="", cancel=threading.Event(),
                       started=time.time(), cid=cid, day=day,
                       msg="", queue=[], done=[])
    return "start", 0


def _kn_save_one(cfg, store, cid: int) -> str:
    """후보 하나를 저장하고 사람이 읽을 결과 한 줄을 돌려준다.

    보강 실패의 우아한 처리는 save_candidate 안에 있다(수확본으로 저장) —
    여기 오는 예외는 비-AI 오류다. **어떤 예외도 밖으로 내보내지 않는다**:
    한 건이 실패했다고 뒤에 줄 선 것까지 버리면 안 된다."""
    from . import knowledge as knowledge_mod
    try:
        path = knowledge_mod.save_candidate(cfg, store, cid)
        msg = f"지식으로 저장: {path.name}"
        # 보강이 실패해도 저장은 된다 — 다만 조용히 두지 않는다. 본문이
        # 수확본 그대로라는 사실은 사용자가 알아야 다시 눌러 볼 수 있다.
        if not getattr(knowledge_mod.save_candidate, "last_enriched", True):
            msg += " · AI 보강 실패(수확본 그대로 — 다시 저장하면 재시도)"
        return msg
    except ValueError as e:            # 후보 없음·이미 처리됨(대기 중 유보 포함)
        return str(e)
    except Exception as e:
        return ("지식을 저장하지 못했습니다 — "
                + (" ".join(str(e).split())[:120] or type(e).__name__))


def _run_kn_job(cfg, cid: int) -> None:
    """지식 저장 워커 — 요청 스레드 밖. 자기 Store 연결을 연다(sqlite 스레드 규칙).

    **대기열을 끝까지 비운다.** Store 는 배수 전체에 하나만 연다 — 항목마다 다시
    열면 수십 건에서 연결 여닫기가 쌓인다."""
    store = None
    with _kn_lock:
        mine = _kn_job["started"]      # 이 배수의 신분증 — finally 가 남의 배수를
    try:                               # 끝내지 않게 한다(아래 참조)
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        while True:
            msg = _kn_save_one(cfg, store, cid)
            with _kn_lock:
                _kn_job["done"].append(msg)
                _kn_job["msg"] = msg
                if not _kn_job["queue"]:
                    _kn_job.update(running=False)
                    return
                cid = _kn_job["queue"].pop(0)
                _kn_job.update(cid=cid)
            # 폴링 라우트가 day 로 화면을 다시 그린다 — DB 조회는 락 밖에서.
            row = store.knowledge_candidate(cid)
            if row is not None:
                with _kn_lock:
                    _kn_job.update(day=row["date"])
    finally:
        if store is not None:
            store.close()
        # 정상 종료는 위 루프가 이미 running=False 로 내려놨다. 여기 걸리는 것은
        # 예외로 빠져나온 경우뿐 — 슬롯을 풀고 **대기열도 비운다**. 안 비우면
        # 마커 없는 '대기 중' 카드가 남아(마커는 running 일 때만 붙는다) 폴링도
        # 안 돌고 저장 버튼도 없어 그 후보를 영영 못 만진다.
        # started 대조는 그 사이에 시작된 **다음 배수**를 끝내 버리지 않기 위한 것.
        with _kn_lock:
            if _kn_job["running"] and _kn_job["started"] == mine:
                left = len(_kn_job["queue"])
                _kn_job.update(running=False, queue=[])
                if left:
                    _kn_job["done"].append(
                        f"저장이 중단됐습니다 — 대기 중이던 {left}건은 "
                        "다시 눌러 주세요")


def _run_diag_job(cfg, tid: int) -> None:
    """스레드 진단 워커 — 요청 스레드 밖. 자기 Store 연결을 연다(sqlite 규칙)."""
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            got = review.diagnose_thread(store, cfg, tid)
            dropped = getattr(review.diagnose_thread, "last_dropped", 0)
            msg = ("현안 브리핑 갱신됨"
                   + (f" · 근거 검증 탈락 {dropped}줄" if dropped else "")
                   if got else "현안 브리핑을 만들지 못했습니다 — 잠시 후 다시")
        finally:
            store.close()
    except review.AIError as e:
        msg = "현안 브리핑 실패 — " + " ".join(str(e).split())[:120]
    except SystemExit as e:                 # 백엔드 미설정 — 조용히 끝내지 않는다
        msg = f"AI 백엔드가 설정되지 않았습니다: {e}"
    except Exception as e:
        msg = ("현안 브리핑을 만들지 못했습니다 — "
               + (" ".join(str(e).split())[:120] or type(e).__name__))
    with _diag_lock:
        _diag_job.update(running=False, msg=msg)


def _diagnose_controls(tid: int, has_diagnosis: bool, deeper: str = "") -> str:
    """스레드 진단 버튼(또는 진행 카드) + 더 깊이 볼 링크.

    deeper 는 쟁점 분석 진입이다(2026-08-16 강등) — 같은 재료로 다른 골격을
    내는 기능이 스레드 머리에 나란히 있으면 사용자가 어느 쪽을 눌러야 하는지
    모른다. 순서를 화면이 말해 준다: 진단이 1차(1콜), 쟁점별 입장이 필요하면
    그때 2차(최대 12콜).
    """
    with _diag_lock:
        sj = dict(_diag_job)
    if sj["running"] and sj["tid"] == tid:
        return ("<div data-diag-running='1' class='diagwait'>"
                "<span class='spin'></span> 스레드를 분석하는 중…"
                " <span class='dim'>AI 1콜 · 완료되면 자동 전환</span></div>")
    done = (sj["msg"] if (not sj["running"] and sj["tid"] == tid and sj["msg"])
            else "")
    note = f" <span class='dim'>{esc(done)}</span>" if done else ""
    return (f"<div class='diagbar'><form method='post' "
            f"action='/thread/{tid}/diagnose'>"
            f"<button class='aibtn ghost compact'>"
            f"{'브리핑 갱신' if has_diagnosis else '현안 브리핑'}</button></form>"
            f"{note}{deeper}</div>")


def _run_pdiag_job(cfg, addr: str, name: str) -> None:
    """인물 진단 워커 — 요청 스레드 밖(자기 Store 연결)."""
    try:
        store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
        try:
            got = review.diagnose_person(store, cfg, addr, name)
            dropped = getattr(review.diagnose_person, "last_dropped", 0)
            msg = (("현안 브리핑 갱신됨"
                    + (f" · 근거 검증 탈락 {dropped}줄" if dropped else ""))
                   if got else "현안 브리핑을 만들지 못했습니다 — 교신 기록이 없습니다")
        finally:
            store.close()
    except review.AIError as e:
        msg = "현안 브리핑 실패 — " + " ".join(str(e).split())[:120]
    except SystemExit as e:
        msg = f"AI 백엔드가 설정되지 않았습니다: {e}"
    except Exception as e:
        msg = ("현안 브리핑을 만들지 못했습니다 — "
               + (" ".join(str(e).split())[:120] or type(e).__name__))
    with _pdiag_lock:
        _pdiag_job.update(running=False, msg=msg)


def _person_diagnosis_html(store, addr: str) -> str:
    """인물 현안 브리핑 카드 — 스레드와 같은 슬롯·같은 렌더를 쓴다.

    **산출이 없어도 카드 자리를 그린다**(2026-08-18). 빈 자리에 제목과 "무엇을
    얻는지" 한 줄이 있어야 사용자가 누를지 말지 정할 수 있다 — 종전처럼 빈
    문자열을 돌려주면 화면 위쪽 버튼 하나가 그 설명을 떠맡아야 했다.
    """
    def act(label: str) -> str:
        return ("<form class='cardact' method='post' action='/people/diagnose'>"
                f"<input type='hidden' name='addr' value='{esc(addr)}'>"
                f"<button class='aibtn ghost compact'>{label}</button></form>")

    with _pdiag_lock:
        pj = dict(_pdiag_job)
    if pj["running"] and pj["addr"] == addr:
        return ("<div data-pdiag-running='1' class='dcard diagwait'>"
                "<span class='spin'></span> 이 사람과의 일을 분석하는 중…"
                " <span class='dim'>AI 1콜 · 완료되면 자동 전환</span></div>")
    day, text = review.load_person_diagnosis(store, addr)
    items = review.parse_diagnosis(text) if text else []
    if not items:
        return ("<div class='dcard'><h2>현안 브리핑</h2>"
                "<div class='empty'>이 사람과 지금 걸린 것 · 먼저 할 일 — "
                + act("만들기") + " <span class='dim'>AI 1콜</span></div></div>")
    ago = _days_ago(day, date.today().isoformat())
    head = ("<h2>현안 브리핑 <span class='aitag'>AI 추정</span>"
            + act("다시 만들기") + "</h2>")
    cap = (f"<p class='dcap'>{esc(day)}{' · ' + esc(ago) if ago else ''}</p>"
           if day else "")
    return ("<div class='dcard aidoss'>" + head + _diagnosis_card(items)
            + cap + "</div>")


def _person_deep_card(store, addr: str) -> str:
    """심층 분석 카드 — 마지막 결과의 한 줄 결론 + 전문 링크.

    종전에는 브리핑 아래 '더 깊이 파기' 링크 하나였고 산출은 분석 대화록에만
    있었다. **제목 아래에 그 제목의 결과가 없으면 화면이 스스로를 설명하지
    못한다** — 그래서 인물 화면이 저장된 최신 분석을 직접 집어 온다. 전문은
    링크로 보낸다(카드는 한 줄 결론까지만 — 셋을 나란히 세우려면 짧아야 한다).
    """
    def act(label: str, fresh: bool = False) -> str:
        return ("<form class='cardact' method='post' action='/ask/jobs'>"
                f"<input type='hidden' name='person' value='{esc(addr)}'>"
                + ("<input type='hidden' name='fresh' value='1'>"
                   if fresh else "")
                # 분석 페이지를 여는 진입이라 채움이다(스레드 쟁점 분석과 같은 규칙).
                + f"<button class='aibtn compact'>{label}</button></form>")

    hit = _person_analysis(store, addr)
    if not hit:
        return ("<div class='dcard'><h2>심층 분석</h2>"
                "<div class='empty'>쟁점별 입장과 경위까지 조사 라운드로 훑습니다 — "
                + act("분석하기")
                + " <span class='dim'>수 분 · 최대 12콜</span></div></div>")
    day = (hit["created"] or "")[:10]
    ago = _days_ago(day, date.today().isoformat())
    # 낡음 문법은 쟁점 분석(_thread_map_controls)과 같다 — 경과일 + 그 뒤 이
    # 사람과 오간 새 메일 수. 분석 자체는 그 시점 재료로 만든 것이다.
    new = store.person_msg_count(addr, since_seq=hit["basis"])
    cap = (esc(day) + (f" · {esc(ago)}" if ago else "")
           + (f" · 이후 새 메일 {new}통" if new else ""))
    body = (f"<div class='dx'><p class='dxlead'>{esc(hit['headline'])}</p></div>"
            if hit["headline"] else
            "<p class='dim'>한 줄 결론이 없는 분석입니다 — 전문에서 확인하세요.</p>")
    return ("<div class='dcard aidoss'><h2>심층 분석 "
            "<span class='aitag'>AI 추정</span>"
            f"<a class='aibtn ghost compact cardact' href='/ask?id={int(hit['id'])}'>"
            "전문 보기 →</a>"
            + act("다시", fresh=True) + "</h2>"
            + body + f"<p class='dcap'>{cap}</p></div>")


def _diagnosis_card(diag: list) -> str:
    """스레드 진단 — `정리` 한 문단 + 나머지 슬롯(2026-08-16).

    **정리는 슬롯 행이 아니라 문단으로 그린다** — 라벨 열에 밀어 넣으면 두세
    문장이 좁은 칸에서 접혀 읽기가 나빠진다. 나머지는 라벨+본문 두 열.
    문제·배경의 근거 인용은 ⓘ 툴팁 관례(.ihint) — 전문을 깔면 카드가 길어진다.
    """
    lead = [b for k, b, _ in diag if k == "정리"]
    rows = []
    for kind, body, quote in diag:
        if kind == "정리":
            continue
        tip = ""
        if quote:
            t = f"근거: {quote}"
            tip = (f" <span class='ihint' title='{esc(t)}' "
                   f"aria-label='{esc(t)}'>ⓘ</span>")
        cls = " warn" if kind == "문제" else ""
        rows.append(f"<div class='dxrow{cls}'><span class='dxkind'>{esc(kind)}</span>"
                    f"<span class='dxbody'>{esc(body)}{tip}</span></div>")
    head = f"<p class='dxlead'>{esc(lead[0])}</p>" if lead else ""
    # **첫 문장은 늘 보이고 나머지 슬롯은 접는다**(2026-08-19 사용자 확정).
    # 슬롯 상한을 다 채우면 카드가 뷰포트의 266% 까지 간다(실측) — 그런 날에도
    # 스레드 본문이 2,200px 아래로 밀리지 않게. 접을 것이 2줄 이하면 접지 않는다:
    # 접기 컨트롤이 내용보다 크면 손해다. 펼침 상태는 app.js 가 **스위치 하나**로
    # 기억하고(스레드·인물 공통), 방금 만든 직후에는 기억값과 무관하게 펼친다.
    if len(rows) <= 2:
        return "<div class='dx'>" + head + "".join(rows) + "</div>"
    kinds: dict[str, int] = {}
    for kind, _b, _q in diag:
        if kind != "정리":
            kinds[kind] = kinds.get(kind, 0) + 1
    parts = [f"{k} {n}" for k, n in list(kinds.items())[:3]]
    rest = sum(list(kinds.values())[3:])
    if rest:
        parts.append(f"그 외 {rest}줄")
    return ("<div class='dx'>" + head
            + "<details class='dxmore'><summary>자세히 — "
            + esc(" · ".join(parts)) + "</summary>"
            + "".join(rows) + "</details></div>")


def _knowledge_cards(store, day: str, back: str | None = None) -> str:
    """암묵지 후보 카드 — day 지정 시 그날 수확분, ""(빈)이면 전체 pending.

    승인 전에는 파일이 없다(AI 는 초안, 확정은 사람). 저장 시점에 참조 스레드
    전문을 읽는 보강 호출이 한 번 돌므로 버튼 라벨에 그 사실을 담는다.
    전체 모드는 지식 탭이 쓴다 — 후보가 그날 회고에만 붙어 있으면 회고를 안
    열어본 날의 후보가 묻힌다(날짜 라벨을 함께 단다).
    """
    rows = store.knowledge_candidates(status="pending", date_iso=day)
    with _kn_lock:
        kj = dict(_kn_job)
        # dict() 는 얕은 복사라 queue/done 이 **워커와 같은 리스트**다. 그대로
        # 쓰면 `in` 을 통과한 직후 워커가 pop 해 `.index()` 가 ValueError 를
        # 던진다(렌더 500). 락 안에서 떠 온다.
        kj["queue"] = list(_kn_job["queue"])
        kj["done"] = list(_kn_job["done"])
    kj_here = (kj["running"] or kj["done"]) and (not day or kj["day"] == day)
    if not rows and not kj_here:
        return ""
    back = back or f"/records?tab=daily&date={_q(day)}"
    # 마커는 이 화면이 폴링·meta refresh 를 걸지 정한다. day 를 함께 실어 폴링이
    # **보고 있는 날짜**를 되돌려 받게 한다 — 배수가 다른 날짜로 넘어가도 화면이
    # 남의 날로 갈아 끼워지지 않는다.
    mark = (f" data-kn-running='1' data-kn-day='{esc(day)}'"
            if kj["running"] else "")
    out = ["<div data-kn-cards>",
           f"<h2>🔧 암묵지 후보 ({len(rows)})</h2>" if rows
           else "<h2>🔧 암묵지 후보</h2>"]
    if rows:
        out.append("<p class='dim'>수확이 캐낸 조직 노하우 — 저장하면 "
                   "vault/knowledge/ 에 md 로 남고 검색·분석 문맥에 실립니다.</p>")
    if kj_here and kj["done"]:
        # 끝난 것부터 쌓아 보여 준다 — 배수 도중에도 진척이 보여야 한다
        for m in kj["done"]:
            out.append(f"<p class='dim'>✅ {esc(m)}</p>")
    for r in rows:
        if kj["running"] and kj["cid"] == r["id"]:
            # 저장 중 — 버튼 대신 대기 카드(마커가 폴링·meta refresh 를 건다)
            out.append(
                f"<div class='item waitcard'{mark}>"
                f"⏳ <b>{esc(r['title'])}</b>"
                "<div class='snip'>보강해서 저장하는 중 — 참조 스레드 전문을 "
                "읽는 AI 1콜이라 수십 초 걸릴 수 있습니다. 다른 화면을 봐도 "
                "됩니다(완료되면 여기 반영).</div></div>")
            continue
        if kj["running"] and r["id"] in kj["queue"]:
            # 줄 서 있음 — 저장 버튼은 빼고 [유보]만 남긴다. 유보가 곧 대기
            # 취소다(status 가 dismissed 가 되면 워커가 차례에 건너뛴다).
            # 처리 중인 항목이 다른 날짜면 이 화면엔 마커가 없으므로 여기도 단다.
            ahead = kj["queue"].index(r["id"]) + 1
            out.append(
                f"<div class='item waitcard'{mark}>"
                f"⏳ <b>{esc(r['title'])}</b>"
                f"<div class='snip'>대기 중 — 앞에 {ahead}건. 순서가 오면 "
                "저장합니다.</div>"
                "<div class='actions'>"
                f"<form method='post' action='/knowledge/{r['id']}/dismiss'>"
                f"<input type='hidden' name='back' value='{esc(back)}'>"
                "<button>유보</button></form></div></div>")
            continue
        tids = [t for t in (r["threads"] or "").split(";") if t]
        refs = " ".join(f"<a href='/thread/{t}'>#{t}</a>" for t in tids)
        quote = (f"<div class='snip'>「{esc(smart_truncate(r['quote'], 120))}」"
                 f" · {refs}</div>" if r["quote"] else
                 f"<div class='snip'>{refs}</div>")
        date_lbl = ("" if day else
                    f" <span class='day'>{esc(r['date'])} 수확</span>")
        out.append(
            f"<div class='item'>🔧 <b>{esc(r['title'])}</b>{date_lbl}"
            f"<div class='snip'>{esc(r['body'])}</div>{quote}"
            "<div class='actions'>"
            f"<form method='post' action='/knowledge/{r['id']}/save'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            "<button class='btn-primary'>지식으로 저장 (AI 보강)</button></form>"
            f"<form method='post' action='/knowledge/{r['id']}/dismiss'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            "<button>유보</button></form></div></div>")
    out.append("</div>")
    return "\n".join(out)


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
            if ev == "call":
                # 진행 중 화면의 '지금까지 몇 콜' — 잡 단위 누계라 model 이벤트의
                # 콜 단위 리셋(recv)과 달리 잡이 끝날 때까지 안 지운다.
                job["calls"] = int(job.get("calls") or 0) + 1
                # 콜 단위 리셋은 원래 model 이벤트가 했는데 **opencode 는 모델
                # 이름을 안 흘린다** — 여기서도 리셋하지 않으면 이전 콜의 수신량이
                # 다음 콜로 이월돼 '받고 있다'는 신호가 거짓이 된다. retry/failed 는
                # 건드리지 않는다(다음 콜이 도는 동안에도 직전 실패는 화면에 남아야
                # 한다). tail 은 잡 시작에만 지운다 — model 이벤트와 같은 이유.
                job["phase"], job["recv"] = "", 0
            elif ev == "model":
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



_STALL_SECS = 30      # 이 시간 무수신이면 '응답 대기' 경고로 바꾼다 (2d)
# opencode 는 첫 이벤트가 늦다 — 프로세스 콜드 스타트 + 모델 대기 뒤에야
# step_start 가 온다(2026-08-30 실측: 20~46초). claude 는 system/init 이 거의
# 즉시 오므로 30초로 충분하지만, 같은 값을 쓰면 여기선 **정상 구간마다** 경고가
# 떠 배경음이 된다. '평소보다 긴 침묵'이라는 뜻을 지키려면 기준이 백엔드마다
# 달라야 한다.
_STALL_SECS_OPENCODE = 90
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
    # **워치독은 스트리밍 백엔드에만.** `last_ev` 는 스트리밍 이벤트뿐 아니라
    # 콜 시작(`ev:call`)으로도 찍히는데, 그건 백엔드와 무관하게 온다 — 그래서
    # 이 게이트가 없으면 이벤트가 애초에 0건인 백엔드가 정상 진행 중에 30초 뒤
    # '무수신'으로 뜬다(_arm_job_backend 가 막으려던 그 오탐을 `call` 이 뒷문으로
    # 되살렸다). opencode 는 콜 하나가 60초를 넘기는 일이 흔해 그게 기본 표시가
    # 됐다(2026-08-30 실측).
    if st.get("stream") and idle >= (st.get("stall") or _STALL_SECS):
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
    if st.get("phase") == "tool":
        # opencode 전용 — claude 는 --tools "" 로 애초에 툴이 없다. 메일 분석
        # 프롬프트는 툴이 필요 없으니 이게 뜨면 **메일 본문이 툴을 유발했다**는
        # 뜻이다. 조용히 넘기지 않는다.
        return "도구 사용 중" + tail
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

    워치독은 스트리밍 백엔드(claude·opencode)만 — 이벤트가 애초에 없는 백엔드에
    잡 시작 시각을 심으면 정상 진행 중에도 '무수신' 오탐이 난다.
    job["stream"] 은 중지 버튼의 안내 문구가 갈라지는 근거이자(_cancel_hint):
    스트리밍이면 진행 중 호출을 즉시 끊지만 블로킹 경로는 콜 경계에서만 멈춘다 —
    _job_live_line 의 무수신 워치독 게이트이기도 하다."""
    try:
        cmd = cfg.ai_cmd(backend_name)
    except (SystemExit, Exception):
        # 백엔드 미설정(SystemExit — Exception 하위가 아니라 따로 적어야 한다)
        # 이든 설정 파손이든, **표시용 판정이 잡을 죽여선 안 된다** — 잡 스레드가
        # 여기서 죽으면 running=True 인 채 슬롯이 영구 점유돼 서버를 다시 띄울
        # 때까지 그 기능이 막힌다.
        return
    oc = review._is_opencode_cmd(cmd)
    stream = review._is_claude_cmd(cmd) or oc
    with lock:
        if job["running"]:
            job["stream"] = stream
            # 무수신 기준도 백엔드가 정한다 — 첫 이벤트까지의 정상 침묵이 다르다
            job["stall"] = _STALL_SECS_OPENCODE if oc else _STALL_SECS
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


# 주간 md 의 첫 줄 — `# 2026-08-04 ~ 2026-08-31 주간 보고`(weekly.py 가 고정 형식으로
# 쓴다). 파일 이름은 **만든 날**이라 대상 기간이 아니다 — 4주 보고를 `2026-08-31` 로만
# 보이면 사용자가 그 한 주짜리로 읽는다(2026-08-31 확인). 화면이 틀린 말을 하진 않지만
# 틀리게 읽도록 둔다.
_WEEKLY_SPAN_RX = re.compile(
    r"^#\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")


def _weekly_span(md: str) -> str:
    """주간 md → '2026-08-04 ~ 2026-08-31 (4주)'. 형식이 다르면 빈 문자열.

    주수는 날짜에서 센다 — 파일에 안 적혀 있고, 적으면 두 곳이 갈린다."""
    m = _WEEKLY_SPAN_RX.match((md or "").lstrip().splitlines()[0]
                              if (md or "").strip() else "")
    if not m:
        return ""
    a, b = m.group(1), m.group(2)
    try:
        days = (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
    except ValueError:
        return f"{a} ~ {b}"
    weeks = max(1, round(days / 7))
    return f"{a} ~ {b} ({weeks}주)"


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
           "진행·이슈·향후로 정리합니다. 기간 내 원문을 한 번에 읽고 인용을 코드가 "
           "대조합니다. "
           f"AI {weekly_mod.MAX_AI_CALLS}콜 · {weeks}주 기준 <b>{_weekly_eta(weeks)}</b> "
           "걸립니다 — 배경에서 도니 다른 화면을 봐도 됩니다.</p>"]

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
            md = path.read_text(encoding="utf-8")
            # 이동 줄은 '어느 보고'(파일 날짜), 부제는 '무엇을 담았나'(대상 기간).
            # 한 줄에 섞으면 앞뒤 링크와 어휘가 갈린다.
            span = _weekly_span(md)
            if span:
                out.append(f"<p class='dim'>{esc(span)}</p>")
            out.append(_md_to_html(md, back,
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
                    hint="원문을 읽어 토픽별 진행·이슈·향후를 씁니다 — "
                         f"{_weekly_eta(st.get('weeks') or 1)} 걸립니다"
                         "(멈춘 것이 아닙니다). "
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
                 use_cache: bool = True,
                 thread_id: int | None = None) -> None:
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
            elif thread_id:                  # 쟁점 분석 — 스레드를 심고 범위 잠금
                res = ask_mod.map_thread(store, cfg, thread_id,
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
               mail_id: int | None = None, use_cache: bool = True,
               thread_id: int | None = None) -> str | None:
    token = secrets.token_urlsafe(12)
    cancel = _job_start(_ask_job, _ask_lock, stage="조사 준비 중…",
                        question=question, parent=parent_id, person=person,
                        mail=mail_id, thread=thread_id, token=token, result=None)
    if cancel is None:
        return None
    threading.Thread(target=_run_ask_job,
                     args=(cfg, question, parent_id, person, cancel, mail_id,
                              use_cache, thread_id),
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


def _ask_claim_html(c: dict) -> str:
    """근거 한 항목 — 주장 문장 + 인용(문맥) + 출처. 쟁점 카드와 role 그룹이
    같은 모양을 쓴다."""
    return (f"<div class='askitem'><div>{esc(c.get('text', ''))}</div>"
            + _ask_quote(c)
            + f"<div class='dim'>{esc(c.get('sender', ''))} · "
              f"{esc(_fmt_stamp(c.get('sent_on') or ''))} · "
              f"{esc(c.get('subject', ''))} "
              f"{_ask_ref(c['mid'], c['thread_id'])}</div></div>")


# 쟁점 상태 칩 색 — 정리된 것(합의·해소)=ok, 안 정리된 것(보류·평행선)=warn,
# 진행 중·빈 값=중립. 상태 어휘는 ask._ISSUE_STATES 가 강제한다.
_ISSUE_CHIP = {"합의": "ok", "해소": "ok", "보류": "warn", "평행선": "warn"}


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

    # 쟁점 분석 — 골격은 issues[] 가 담는다(answer 는 재작성·폴백으로 갈릴 수
    # 있어 구조를 싣지 않는다). 쟁점에 연결된 근거는 카드 안에 그리고, 아래
    # role 그룹에서는 뺀다(중복 방지). issues 없는 답은 이 블록이 통째로 없다.
    linked: set = set()
    if res.get("issues"):
        claims_all = res.get("claims") or []
        out.append("<h3 class='asksec'>쟁점</h3>")
        for i, it in enumerate(res["issues"]):
            status = it.get("status") or ""
            chip = ""
            if status:
                cls = _ISSUE_CHIP.get(status, "")
                chip = (f"<span class='ichip{' ' + cls if cls else ''}'>"
                        f"{esc(status)}</span>")
            out.append("<div class='issue'><div class='issue-h'>"
                       f"{esc(it.get('title', ''))}{chip}</div>")
            if it.get("note"):
                out.append(f"<div class='issue-n'>{esc(it['note'])}</div>")
            ev = [j for j, c in enumerate(claims_all)
                  if c.get("issue") == i + 1]
            if ev:
                out.append("<div class='askev'>")
                for j in ev:
                    linked.add(j)
                    out.append(_ask_claim_html(claims_all[j]))
                out.append("</div>")
            out.append("</div>")

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
            items = [c for j, c in enumerate(res["claims"])
                     if j not in linked and (c.get("role") or "배경") == role]
            if not items:
                continue
            if title not in done:
                out.append(f"<h3 class='asksec'>{title}</h3>")
                done.add(title)
            out.append("<div class='askev'>")
            for c in items:
                out.append(_ask_claim_html(c))
            out.append("</div>")

    if res.get("open"):
        out.append("<h3 class='asksec'>열린 것</h3><div class='askev'>")
        for o in res["open"]:
            out.append(f"<div class='askitem'><div>{esc(o.get('text', ''))}</div>"
                       + _ask_quote(o)
                       + f"<div class='dim'>{esc(o.get('sender', ''))} · "
                         f"{esc(_fmt_stamp(o.get('sent_on') or ''))} "
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
                 # 재작성본에서 코드가 버린 문장 — 답이 짧아진 이유를 화면에 둔다
                 + (f" · 근거 밖 문장 {s['trimmed']}개 제외"
                    if s.get("trimmed") else "")
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


def _deep_hint(last: dict, turns: int = 1) -> str:
    """엔진 상한에 부딪힌 답에만 붙는 한 줄 — 심화 경로 안내.

    **상시로 두지 않는다.** 좋은 답(확인됨·근거 여럿) 뒤에 "더 깊은 건 다른 데서"를
    붙이면 방금 그 답의 값을 스스로 깎고, 매번 보이면 배경음이 된다(`AI 추정` 칩과
    낡음 배지를 같은 이유로 기각한 전례). 조건은 둘 — 엔진이 스스로 `근거 부족`
    이라 했거나, 한 대화에서 이어 묻기가 3턴을 넘었는데도 아직 묻고 있을 때.

    문구에 전제를 숨기지 않는다(터미널·저장소 폴더·Claude Code). 질문을 그대로
    박아 복사 한 번으로 옮겨 가게 한다 — 브라우저를 보던 사람이 다시 타이핑하지
    않도록.
    """
    if not last:
        return ""
    st = (last.get("state") or "").strip()
    # 3턴 넘게 물었어도 **마지막 답이 확인됨이면 붙이지 않는다** — 질문이 닫힌
    # 것이라, 그 뒤에 "더 깊게"를 붙이면 방금 얻은 답의 값을 깎는다(이 함수의 전제).
    if st != "근거 부족" and not (turns >= 3 and st != "확인됨"):
        return ""
    who = last.get("person") or {}
    if who.get("addr"):
        # 인물 브리핑에는 question 이 비어 있다 — 주소만 넘기면 스킬이 '질문'을
        # 못 받는다. 엔진이 실제로 물은 그 문장을 그대로 쓴다(brief_question).
        from . import ask as ask_mod
        q = ask_mod.brief_question(who.get("name") or who["addr"],
                                   int(who.get("months") or ask_mod.BRIEF_MONTHS))
    else:
        q = (last.get("question") or "").strip()
    # 셸/슬래시 명령에 그대로 붙여 넣는 한 줄이다 — 줄바꿈이 섞이면 뒤가 잘린다
    q = " ".join(q.split())
    if not q:
        return ""
    cmd = f"/mail-research {q}"
    # '저장소 폴더'라고 하면 data/(메일 저장소)로 읽는다 — 이 앱에서 '저장소'는
    # 메일 저장소를 가리키는 말이다(저장소 통계·doctor [저장소]). 코드 폴더의
    # **실제 경로**를 찍어 그 혼동을 아예 없앤다(git 업데이트가 쓰는 그 경로다).
    where = Path(__file__).resolve().parent.parent
    return ("<div class='deephint'>"
            "엔진은 정해진 라운드 안에서 한 번에 조사하고 끝납니다. "
            "<b>Claude Code 로는 각도를 바꿔 가며 더 파고들 수 있습니다</b> — "
            f"코드 폴더(<code>{esc(str(where))}</code>)에서 "
            f"<code>{esc(cmd)}</code> "
            "<button type='button' class='copybtn' "
            f"data-copy='{esc(cmd)}'>복사</button></div>")


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
        redo = (_deep_hint(last, len(tr.get('turns') or []))
                + "<form class='dim chatredo' method='post' action='/ask/jobs'>"
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
            "<div>새 분석 · <a href='/settings' title='분석을 어떤 AI 가 할지 고릅니다'>"
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

    대화가 없을 때만 보이는 자리라 소음이 없다. 항목: 지식 한 줄 + 최근 주간
    보고 + 자주 왕래 인물(→ 인물 분석 동선)."""
    rows = []
    kn = store.knowledge_all()
    pend = len(store.knowledge_candidates())
    rows.append("<a href='/records?tab=knowledge'>🧠 지식 — "
                f"<b>{len(kn)}</b>건"
                + (f" · 후보 {pend}" if pend else "") + "</a>")
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


def _tile_body(body: str, limit: int = _HOME_TILE_MAX) -> str:
    """절 마크다운 → 타일 본문. 상위 몇 건만 남긴다(접기는 호출부가 md 전체에
    _apply_done 을 먼저 적용한다 — 타일 라벨의 (N건)도 거기서 다시 센다).

    '처리함' 버튼을 홈에도 단다(2026-08-11 사용자 확정 — "접는 자리는 리포트
    화면"이라던 2026-08-01 결정을 뒤집음). back='/' 로 POST 후 홈으로 그대로
    돌아오고, 홈은 어차피 /latest 토큰으로 in-place 재렌더되는 화면이라
    '화면이 튄다'는 전제가 더는 성립하지 않는다. 표식을 떼지 않고
    _md_to_html(back='/') 에 넘겨 리포트와 **같은 버튼·같은 키**를 쓴다."""
    lines, kept = [], 0
    for ln in body.splitlines():
        if ln.lstrip().startswith(("- ", "* ")) and len(ln) - len(ln.lstrip()) == 0:
            if kept >= limit:
                break
            kept += 1
        lines.append(ln)
    return _md_to_html("\n".join(lines), back="/")


def _bento_home(store, cfg, today: str) -> str:
    """벤토 홈 — **이미 있는 것을 다시 배치**한 격자.

    재료는 두 가지뿐이다: 지금 랜딩이 이미 읽는 값(지식 카운트·최근 주간
    보고·자주 왕래)과 **저장된 오늘 회고 파일**. 새로 계산하지 않고, 회고가
    없으면 그 타일을 그리지 않는다. AI 가 쓴 절(Executive Summary)은 사용자가
    AI 회고를 돌렸을 때만 파일에 있으므로, 있으면 보여주고 없으면 비운다 —
    여기서 새로 부르지 않는다(2026-08-01 사용자 확정). 회고가 아직 없으면 빈
    격자 대신 안내 타일 하나를 세운다(2026-08-11) — 홈 GET 이 이미
    _maybe_auto_review 를 부르므로 '만드는 중'은 사실이고, 완성되면 /latest
    재렌더가 실제 타일로 바꿔 준다."""
    md = load_daily(cfg, today) or ""
    done = _done_set(store)
    if done:
        # 접기를 절 분해 **전에** md 전체에 적용한다 — 절 제목의 (N건) 재계산과
        # 빈 절 소멸(_apply_done)이 타일 라벨에도 반영되게. 안 하면 홈에서 접은
        # 직후 라벨 숫자가 그대로라 버튼이 안 먹은 것처럼 보인다.
        md = _apply_done(md, done)
    sec = _md_sections(md)
    big, small = [], []          # 내용 타일 / 숫자 한 줄짜리 타일

    def tile(dest, body, label, cnt="", link="", cls=""):
        # 링크가 있으면 타일 통째로 클릭된다(2026-08-11 사용자 확정) — data-href
        # 는 app.js 위임 리스너가 줍는다. <a> 로 감싸지 않는 이유: 본문에 스레드
        # 링크·처리함 폼이 들어와 인터랙티브 요소 중첩(invalid HTML)이 되기
        # 때문. '열기' 앵커는 JS 꺼짐 폴백으로 남기되 tabindex=-1 — 탭 스톱을
        # 타일당 하나로 모은다.
        head = (f"<div class='bth'><span class='lab'>{esc(label)}</span>"
                + (f"<span class='cnt'>{esc(cnt)}</span>" if cnt else "")
                + (f"<a class='more' href='{link}' tabindex='-1'>열기</a>"
                   if link else "")
                + "</div>")
        attrs = (f" data-href='{esc(link)}' tabindex='0' role='link'"
                 f" aria-label='{esc(label)} 열기'" if link else "")
        dest.append((cls, attrs, head + body))

    def find(prefix):
        for k, v in sec.items():
            if k.startswith(prefix):
                m = _HOME_COUNT_RX.search(k)
                return v, (m.group(0) if m else "")
        return None, ""

    daily_link = f"/records?tab=daily&date={_q(today)}"
    body, _c = find("Executive Summary")
    if body:
        tile(big, f"<div class='bsaid'>{_tile_body(body, 5)}</div>",
             "오늘의 요약", link=daily_link, cls="ai")
    body, cnt = find("내 약속")
    if body:
        tile(big, _tile_body(body), "내 약속", cnt, daily_link)
    body, cnt = find("변화")
    if body:
        tile(big, _tile_body(body), "변화 — 어제 이후", cnt, daily_link)
    if not big:
        # 회고가 아직 없는 날 — 숫자 타일 셋만 남으면 홈이 빈 것처럼 보인다.
        # 라벨은 '오늘의 회고'로: '오늘의 요약'(AI 절 타일)과 문자열이 겹치면
        # 회고 없는 날을 검사하는 기존 테스트와 충돌한다.
        tile(big, "<div class='dim'>오늘 회고를 만드는 중입니다 — 완성되면 "
             "여기에 채워집니다. 지난 회고는 기록에서 볼 수 있어요.</div>",
             "오늘의 회고", link=daily_link, cls="empty")

    kn = store.knowledge_all()
    pend = len(store.knowledge_candidates())
    # 최근 지식 미리보기 — 숫자만으로는 무엇이 쌓였는지 알 수 없다.
    # 제목 자르기는 CSS ellipsis 가 한다(폭이 반응형이라 파이썬이 모른다).
    tile(small, f"<div class='bnum'>{len(kn)}</div>"
         + (f"<div class='dim'>후보 {pend}건</div>" if pend else "")
         + "".join(f"<div class='brow bdectitle'>{esc(r['title'])}</div>"
                   for r in kn[:3]),
         "지식", link="/records?tab=knowledge")

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
    for (cls, attrs, html), sp in zip(big, spans):
        out.append(f"<div class='btile s{sp} {cls}'{attrs}>{html}</div>")
    sspan = 4 if len(small) == 3 else (6 if len(small) == 2 else 12)
    for cls, attrs, html in small:
        out.append(f"<div class='btile mini s{sspan} {cls}'{attrs}>{html}</div>")
    return "<div class='bhome'>" + "".join(out) + "</div>" if out else ""


# 첫 화면의 심화 경로 한 줄 — **두 스킨에 같은 문장**. 기본 스킨이 벤토라
# 클래식에만 넣으면 대부분의 사용자가 못 본다(2026-08-21).
_DEEP_LINE = ("<p class='dim'>더 깊게 파야 하면 Claude Code 로 — mailkb 코드 "
              "폴더에서 <code>/mail-research &lt;질문&gt;</code></p>")


def _ask_landing(store, cfg, today: str | None = None) -> str:
    """새 대화(홈 첫 화면) — 인트로 + 하단 입력 + 이어서 볼 것.

    벤토 스킨에서는 '이어서 볼 것' 줄 대신 격자(_bento_home)를 깐다.
    클래식은 지금 그대로다."""
    if _skin_ok(cfg.opt("web", "skin", default=_DEFAULT_SKIN)) == "bento":
        grid = _bento_home(store, cfg, today or date.today().isoformat())
        return ("<div class='chat'><div class='chatintro'>"
                "<h2>무엇이 궁금하세요?</h2>"
                "<p class='dim'>저장된 메일에서 <b>근거가 달린 답</b>을 찾습니다. "
                "인용을 원문과 대조해 통과한 것만 남기고, 답이 없으면 없다고 합니다."
                "</p>" + _DEEP_LINE + "</div>" + grid + "</div>"
                + _ask_input(None, "메일에 대해 물어보세요"))
    return ("<div class='chat'><div class='chatintro'>"
            "<h2>무엇이 궁금하세요?</h2>"
            "<p class='dim'>저장된 메일에서 <b>근거가 달린 답</b>을 찾습니다. "
            "찾은 메일을 읽고 인용을 원문과 대조해, 통과한 것만 답에 남깁니다. "
            "답이 없으면 없다고 답합니다.</p>"
            "<p class='dim'>예) NPX-200 양자화 최종 결정 뭐였지? · "
            "MPW 일정 언제로 확정됐어? · 인물 페이지에서 <b>심층 분석</b>으로 "
            "브리핑도 됩니다.</p>"
            + _DEEP_LINE +
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
        q = ask_mod.brief_question(name)         # 캐시 조회 키 — 조립은 한 곳에서만
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
                  mail_id: int | None = None,
                  thread_id: int | None = None) -> str:
    """AI 불가 — 일반 검색 결과라도 보여준다(#10). POST 재시도 폼 유지.

    메일 분석 실패면 재시도가 **mid 로** 다시 제출돼야 한다 — 질문 문자열로
    재제출하면 seed·scope 없는 일반 질문이 되어, 성공해도 스레드 머리글의
    '분석 보기'와 영영 연결되지 않는다(이중 이력). 검색어도 자동 생성 질문이
    아니라 그 메일 제목을 쓴다. 쟁점 분석(tid)도 같은 이유로 tid 재제출."""
    if mail_id:
        target = f"<input type='hidden' name='mid' value='{int(mail_id)}'>"
        m = store.message(str(mail_id))
        search_q = (m["subject"] or "").strip() if m else q
    elif thread_id:
        target = f"<input type='hidden' name='tid' value='{int(thread_id)}'>"
        row = store.db.execute(
            "SELECT subject FROM messages WHERE thread_id=? "
            "ORDER BY sent_on, id LIMIT 1", (int(thread_id),)).fetchone()
        search_q = (row["subject"] or "").strip() if row else q
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
                             st.get("mail"), st.get("thread")), False
    if st.get("result"):
        from . import ask as ask_mod
        res = st["result"]
        tr = ask_mod.transcript(store, res["id"]) if res.get("id") else None
        # 캐시 기록 실패로 id 가 없어도 방금 결과는 보여준다 — 답이 증발하면 안 됨
        marker = (f"<div data-ask-result-id='{int(res['id'])}' hidden></div>"
                  if res.get("id") else "")
        return marker + render_ask_thread(store, cfg, tr or _ask_one_turn(res)), False
    return render_ask(store, cfg, {}), False


def _kn_doc_ok(cfg, path_str: str) -> Path | None:
    """지식 파일 경로 검증 — vault/knowledge 아래의 실재 .md 만 통과."""
    from . import knowledge as knowledge_mod
    try:
        p = Path(path_str).resolve()
        root = knowledge_mod.kn_dir(cfg).resolve()
        if p.is_relative_to(root) and p.suffix == ".md" and p.is_file():
            return p
    except OSError:
        pass
    return None


def render_knowledge_page(store, cfg, qs) -> str:
    """기억 › 지식 — 대기 후보(전 날짜) + 저장된 지식 목록/본문/검색.

    렌더 진입마다 reindex(mtime 증분이라 싸다) — 파일이 원본이므로 외부
    편집기로 고친 것이 목록·검색에 바로 따라온다."""
    from . import knowledge as knowledge_mod
    knowledge_mod.reindex(cfg, store)
    doc = (qs.get("doc") or [""])[0]
    if doc:
        return _render_knowledge_doc(store, cfg, doc)
    q = (qs.get("q") or [""])[0].strip()
    out = ["<h1>지식</h1>",
           "<form method='get' action='/records' class='actions'>"
           "<input type='hidden' name='tab' value='knowledge'>"
           f"<input type='search' name='q' value='{esc(q)}' "
           "placeholder='지식 검색' aria-label='지식 검색'>"
           "<button>검색</button></form>"]
    if q:
        hits = store.search_knowledge(q, limit=20)
        out.append(f"<h2>검색 결과 ({len(hits)})</h2>")
        if not hits:
            out.append("<p class='empty'>일치하는 지식이 없습니다.</p>")
        for h in hits:
            link = f"/records?tab=knowledge&doc={_q(h['path'])}"
            snip = (f"<div class='snip'>{_snip_html(h['snippet'])}</div>"
                    if h.get("snippet") else "")
            out.append(f"<div class='item'>📄 <a href='{link}'>"
                       f"{esc(h['title'])}</a>{snip}</div>")
        out.append("<p class='dim'><a href='/records?tab=knowledge'>"
                   "← 전체 목록</a></p>")
        return "\n".join(out)
    out.append(_knowledge_cards(store, "", back="/records?tab=knowledge"))
    rows = store.knowledge_all()
    out.append(f"<h2>저장된 지식 ({len(rows)})</h2>")
    if not rows:
        out.append("<p class='empty'>아직 없습니다 — AI 회고가 캐낸 후보를 "
                   "[지식으로 저장]하면 여기 쌓입니다.</p>")
    for r in rows:
        tids = [t for t in (r["threads"] or "").split(";") if t]
        refs = " ".join(f"<a href='/thread/{t}'>#{t}</a>" for t in tids)
        day = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d")
        snip = smart_truncate(" ".join((r["content"] or "").split()), 110)
        link = f"/records?tab=knowledge&doc={_q(r['path'])}"
        out.append(
            f"<div class='item'>📄 <a href='{link}'>{esc(r['title'])}</a> "
            f"<span class='day'>{esc(day)}</span>"
            + (f" <span class='who'>· {refs}</span>" if refs else "")
            + (f"<div class='snip'>{esc(snip)}</div>" if snip else "")
            + "</div>")
    return "\n".join(out)


def _render_knowledge_doc(store, cfg, path_str: str) -> str:
    """지식 본문 — md 렌더 + [외부 편집기로 열기](파일이 원본이라 편집은 밖에서)."""
    from . import knowledge as knowledge_mod
    back = "<p class='dim'><a href='/records?tab=knowledge'>← 지식 목록</a></p>"
    p = _kn_doc_ok(cfg, path_str)
    row = store.knowledge_row(path_str)
    if p is None or row is None:
        return back + "<p class='empty'>지식 파일이 없습니다 — 옮겨졌거나 지워졌으면 목록에서 다시 여세요.</p>"
    text = p.read_text(encoding="utf-8")
    title, _threads, _core = knowledge_mod.parse_file(text)
    body = text
    if text.startswith("---"):                       # frontmatter 는 본문에서 뗀다
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
    out = [back, f"<h1>📄 {esc(title or p.stem)}</h1>",
           "<div class='actions'>"
           "<form method='post' action='/knowledge/open'>"
           f"<input type='hidden' name='path' value='{esc(str(p))}'>"
           f"<input type='hidden' name='back' "
           f"value='/records?tab=knowledge&doc={esc(_q(str(p)))}'>"
           "<button>외부 편집기로 열기</button></form></div>",
           _md_to_html(body, back="/records?tab=knowledge"),
           f"<p class='dim'>{esc(str(p))}</p>"]
    return "\n".join(out)


def render_records(store, cfg, qs, today: str) -> str:
    """기억 페이지 — 탭: 일간 회고 | 주간 보고 | 지식. (장기기억 탭은 2026-08-14
    폐지 — 활용도가 낮아 사용자가 제거를 확정했고, 영구 기억은 vault/knowledge
    의 암묵지 md 가 맡는다. 구 북마크 ?tab=decisions 는 일간으로 강등.)"""
    tab = (qs.get("tab") or ["daily"])[0]
    if tab not in ("daily", "weekly", "knowledge"):
        tab = "daily"
    tabs = []
    for key, label in (("daily", "일간 회고"), ("weekly", "주간 보고"),
                       ("knowledge", "지식")):
        if key == tab:
            tabs.append(f"<b>{esc(label)}</b>")
        else:
            tabs.append(f"<a href='/records?tab={key}'>{esc(label)}</a>")
    bar = ("<div class='listtabs'><span class='ltabs'>"
           + " · ".join(tabs) + "</span></div>")
    if tab == "knowledge":
        return bar + render_knowledge_page(store, cfg, qs)
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
    tid_raw = (form.get("tid") or [""])[0].strip()
    thread_id = int(tid_raw) if tid_raw.isdigit() else None
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
    elif thread_id:
        row = store.db.execute(
            "SELECT subject FROM messages WHERE thread_id=? "
            "ORDER BY sent_on, id LIMIT 1", (thread_id,)).fetchone()
        if not row:
            return "/threads?msg=" + _q(f"스레드 #{thread_id} 을 찾을 수 없습니다")
        question = ask_mod.thread_question(thread_id, row["subject"] or "")
        scope = f"thread:{thread_id}"
        parent_id = None
    elif person:
        name = store.person_name(person) or person
        question = ask_mod.brief_question(name)   # 캐시 키 재료 — 한 곳에서만
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
                       use_cache=not fresh, thread_id=thread_id)
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
    헤더 값을 검증하지 않는다 — 2026-08-01 적대 검토에서 실증).

    예외 하나(2026-08-11): 홈 타일의 '처리함'은 back='/' 로 온다 — 리터럴 한
    글자와의 동등 비교 뒤 **서버 상수**를 반환하므로 사용자 문자열이 헤더에
    실릴 경로가 없다(`/\r\n…`·`//evil`·`/?x` 는 아래 재조립으로 떨어진다)."""
    if (raw or "") == "/":
        return "/"
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

    if path == "/people/diagnose":
        # 인물 진단 — 1콜이지만 수십 초라 잡으로 넘긴다(요청 스레드가 막히면
        # 단일 스레드 서버 전체가 멈춘다). 인물 화면 폴링이 카드를 갈아 끼운다.
        addr = (form.get("addr") or [""])[0].strip().lower()
        back = "/people?addr=" + _q(addr)
        if not addr:
            return "/people?msg=" + _q("주소가 없습니다")
        name = store.person_name(addr)
        if _job_start(_pdiag_job, _pdiag_lock, addr=addr, msg="") is None:
            return back + "&msg=" + _q("다른 인물 브리핑이 진행 중입니다 — "
                                       "이 요청은 시작되지 않았습니다")
        threading.Thread(target=_run_pdiag_job, args=(cfg, addr, name),
                         daemon=True).start()
        return back + "&msg=" + _q("분석하는 중 — 완료되면 화면에 반영됩니다")

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

    if len(parts) == 2 and parts[0] == "knowledge" and parts[1] == "open":
        # 지식 md 를 OS 기본 편집기로 — 파일이 원본이라 편집은 밖에서 한다.
        back = (form.get("back") or ["/records?tab=knowledge"])[0]
        sep = "&" if "?" in back else "?"
        target = _kn_doc_ok(cfg, (form.get("path") or [""])[0])
        if target is None:
            return back + sep + "msg=" + _q("지식 파일이 아닙니다")
        if _open_external(target):                    # 노트와 같은 함수(콘솔 분리)
            return back + sep + "msg=" + _q(f"외부 편집기로 열기: {target.name}")
        return back + sep + "msg=" + _q(
            f"편집기 자동 열기 실패, 직접 여세요: {target}")

    if len(parts) == 3 and parts[0] == "knowledge":
        # 암묵지 후보 — 저장을 눌러야 md 가 생긴다(승인 전에는 파일이 없다).
        back = (form.get("back") or ["/records?tab=daily"])[0]
        sep = "&" if "?" in back else "?"
        try:
            kid = int(parts[1])
        except ValueError:
            return back + sep + "msg=" + _q("잘못된 항목")
        action = parts[2]
        if action == "save":
            # 보강 AI 1콜은 수십 초 — 단일 스레드 서버라 여기서 돌리면 저장이
            # 끝날 때까지 모든 화면이 멈춘다(실측 20초). 잡으로 넘기고 즉시
            # 돌아간다; 회고 화면 폴링이 대기 카드 → 완료로 갈아 끼운다.
            row = store.knowledge_candidate(kid)
            if row is None or row["status"] != "pending":
                return back + sep + "msg=" + _q(f"암묵지 후보 없음 또는 처리됨: #{kid}")
            # 진행 중이면 거절하지 않고 줄을 세운다 — 후보는 한 화면에 여럿이
            # 뜨고, 하나 누르고 수십 초 기다렸다 다시 누르는 것이 원래 불편이다.
            how, ahead = _kn_claim(kid, row["date"])
            if how == "dup":
                return back + sep + "msg=" + _q("이미 대기 중입니다")
            if how == "queued":
                return back + sep + "msg=" + _q(
                    f"대기열에 넣었습니다 — 앞에 {ahead}건")
            threading.Thread(target=_run_kn_job, args=(cfg, kid),
                             daemon=True).start()
            return back + sep + "msg=" + _q("보강해서 저장하는 중 — 완료되면 카드에 반영됩니다")
        if action == "dismiss":
            store.set_knowledge_status(kid, "dismissed")
            return back + sep + "msg=" + _q("유보됨")

    if len(parts) == 3 and parts[0] == "thread":
        try:
            tid = int(parts[1])
        except ValueError:
            return "/?msg=" + _q("잘못된 스레드")
        action, back = parts[2], f"/thread/{tid}"
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
        if action == "diagnose":
            # 스레드 진단은 여기서만 만들어진다(요지를 흡수, 2026-08-16).
            # AI 1콜이라 잡으로 넘기고 즉시 돌아간다 — 스레드 화면 폴링이
            # 진행 카드를 결과로 갈아 끼운다.
            if store.thread(tid) is None:
                return "/?msg=" + _q(f"스레드 없음: #{tid}")
            if _job_start(_diag_job, _diag_lock, tid=tid, msg="") is None:
                return back + "?msg=" + _q("다른 스레드 분석이 진행 중입니다 — "
                                           "이 요청은 시작되지 않았습니다")
            threading.Thread(target=_run_diag_job, args=(cfg, tid),
                             daemon=True).start()
            return back + "?msg=" + _q("분석하는 중 — 완료되면 화면에 반영됩니다")
        if action == "note":
            # 없으면 만들고, 외부 편집기(기본 연결 프로그램)로 연다(2026-08-11).
            # 즉시 색인해 배지·[내 노트]·검색이 새 파일을 바로 본다.
            from . import notes
            existed = notes.find_thread_note(cfg, tid)
            try:
                p = existed or notes.create_thread_note(cfg, store, tid)
            except notes.NoThread as e:
                # 없는 번호를 화면 오류로 — 예외를 그대로 올리면 단일 스레드
                # 서버가 통째로 죽는다(2026-08-11 실측).
                return back + "?msg=" + _q(str(e))
            notes.reindex(cfg, store)
            verb = "노트 열림" if existed else "노트 생성"
            if _open_external(p):
                return back + "?msg=" + _q(f"{verb}: {p.name} — 외부 편집기")
            return back + "?msg=" + _q(
                f"{verb}: {p.name} — 편집기 자동 열기 실패, 직접 여세요: {p}")
        if action == "note-save":
            # 인라인 편집기 저장(2026-08-11). 삭제를 별도 액션으로 두지 않는
            # 것은 '/thread/N/x' 세 토막 규칙 안에서 액션을 늘리지 않으려는
            # 것이고, '비우고 저장 = 삭제'가 편집기 안내와 같은 규칙이라서다.
            from . import notes
            # parse_qs 는 빈 값을 키째로 버린다 — body 는 사라질 수 있어도
            # base 는 파일이 없을 때조차 '0.0' 이라 항상 실린다. 없으면 우리
            # 폼이 아니다(엉뚱한 POST 한 번에 노트가 지워지는 길을 막는다).
            base = (form.get("base") or [""])[0].strip()
            try:
                base_mtime = float(base)
            except ValueError:
                return back + "?msg=" + _q(
                    "잘못된 요청 — 노트를 건드리지 않았습니다")
            try:
                st, p = notes.save_thread_note(
                    cfg, store, tid, (form.get("body") or [""])[0], base_mtime)
            except notes.NoThread as e:
                return back + "?msg=" + _q(str(e))
            if st == "conflict":
                # 덮어쓰지 않고 편집 모드로 되돌린다 — 내가 친 원고는 app.js 가
                # 되살린다(msg 로는 본문을 실어 올 수 없다).
                return (back + "?note=edit&noteconflict=1&msg="
                        + _q("다른 곳에서 노트가 바뀌었습니다 — "
                             "덮어쓰지 않았습니다"))
            if st == "noop":
                return back + "?msg=" + _q("빈 노트는 만들지 않았습니다")
            if st == "deleted":
                return back + "?msg=" + _q(f"노트 삭제: {p.name}")
            msg = "노트 저장" if st == "saved" else f"노트 생성: {p.name}"
            if (form.get("ext") or [""])[0]:      # 저장하고 외부 편집기로
                if _open_external(p):
                    return back + "?msg=" + _q(f"{msg} — 외부 편집기")
                return back + "?msg=" + _q(f"{msg} — 편집기 열기 실패: {p}")
            return back + "?msg=" + _q(msg)
        if action == "open":                          # Windows COM
            from .sources import get_source
            msgs = store.thread_messages(tid)
            if not msgs:
                return back + "?msg=" + _q("메일 없음")
            m = msgs[-1]
            ok = get_source("outlook", cfg=cfg).open_in_outlook(
                m["entry_id"], m["message_id"])
            # 열기 결과는 "Outlook 에 아직 있나"에 대한 공짜 답 — 여기서 남겨
            # 두면 유령 메일이 미답변·개입 판정에서 빠진다(추가 COM 왕복 0).
            store.set_gone(m["id"], not ok)
            return back + "?msg=" + _q(
                "Outlook에서 열림" if ok else
                "Outlook에서 못 찾음 — 'Outlook에 없음'으로 표시하고 "
                "미답변 판정에서 뺍니다")
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
        # — 개입 신호 노출은 2026-07-30 제거됐고(판정 엔진은 주간 보고 재료로 유지), 지식·주간·인물은 랜딩 상태줄로.
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
        # 기억(데일리·주간). /daily 는 구 메뉴 경로 — 북마크 호환 흡수.
        # 경로 /records 는 표시명 개편(기록→기억, 2026-07-17) 후에도 유지 — URL 과
        # 표시명이 어긋나도 동선엔 영향이 없고, 옛 북마크 호환 분기만 늘어난다.
        return "기억", render_records(store, cfg, qs, today), 200, "left"
    if path == "/review/status":
        inner, running = render_review_status(store)
        return "정리", inner, 200, "right"
    if path == "/knowledge/status":
        # 지식 저장 잡 폴링 — 회고 우측 패널을 잡의 날짜로 재렌더해 돌려준다.
        # 진행 중이면 대기 카드(마커 포함)가 그대로 있고, 완료면 카드가 결과
        # 한 줄로 바뀐 화면이 온다(app.js 는 마커 소멸을 완료 신호로 쓴다).
        with _kn_lock:
            day = (qs.get("date") or [_kn_job["day"] or today])[0]
        return ("기억", render_records(
            store, cfg, {"tab": ["daily"], "date": [day]}, today), 200, "right")
    if path == "/settings/status":
        # AI 응답 시험 잡 폴링 — 설정 화면을 다시 그려 돌려준다(마커 소멸 = 완료).
        return ("설정", render_settings(store, cfg), 200, "left")
    if path == "/people/diagnose/status":
        # 인물 진단 잡 폴링 — 잡이 붙은 인물 화면을 다시 그려 돌려준다.
        with _pdiag_lock:
            addr = _pdiag_job["addr"]
        return ("인물", render_dossier(store, cfg, addr), 200, "left")
    if path == "/thread/diagnose/status":
        # 스레드 진단 잡 폴링 — 잡이 붙은 스레드 화면을 다시 그려 돌려준다.
        # 진행 중이면 마커가 남아 있고, 끝나면 요약이 실린 화면이 온다.
        with _diag_lock:
            tid = _diag_job["tid"]
        return ("스레드", render_thread(store, cfg, int(tid or 0)), 200, "right")
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
        # qs 를 통째로 넘긴다(?note=edit 등) — render_search·render_records 와
        # 같은 관례. focus·hl 은 종전대로 app.js 가 처리한다.
        return "스레드", render_thread(store, cfg, tid, qs), 200, "right"
    return "404", "<p class='empty'>없는 페이지</p>", 404, "right"


# ─────────────────────────────────────────────────── HTTP 핸들러

class _Handler(BaseHTTPRequestHandler):
    cfg = None  # serve() 가 주입
    app_mode = False   # serve --app 일 때만 True — 페이지가 창 수명 관리를 켠다
    # 단일 스레드 서버 보호: 브라우저(Edge/Chrome)는 요청 없이 미리 여는
    # 투기적 연결을 만드는데, 그 빈 소켓의 요청 대기에 서버가 잡히면 다음
    # 클릭이 그동안 멈춘다. 로컬은 요청 전송이 즉각적이므로 3초면 충분.
    timeout = 3

    def log_message(self, *a):  # 조용히
        pass

    def _send_html(self, html_str: str, code: int = 200,
                   csp: str = "") -> None:
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 진행 중 화면은 1.5초마다 같은 URL 을 다시 부른다. 캐시 지시가 없으면
        # 브라우저(Chrome/Edge)가 메모리 캐시에서 같은 응답을 돌려줘 수신량·단계가
        # 첫 값에 굳는다(2026-07-29 실기기 증상). 로컬 1인 도구라 HTML 캐시로
        # 얻을 것이 없으므로 전부 no-store.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp or CSP)
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
        if path in _JS_ASSETS:
            body = _JS_ASSETS[path]().encode("utf-8")
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
        if path == "/appwin":             # 앱 창 수명 관리를 켤지 → app.js 가 묻는다
            body = b"1" if _Handler.app_mode else b"0"
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
                              skin=self.cfg.opt("web", "skin", default=_DEFAULT_SKIN))
        except Exception:  # 죽지 않게 — 상세는 콘솔(개발용), 화면엔 친절한 안내
            import traceback
            traceback.print_exc()
            code = 500
            th = self.cfg.opt("web", "theme", default="light")
            sk = self.cfg.opt("web", "skin", default=_DEFAULT_SKIN)
            msg = ("<p class='empty'>문제가 발생해 이 화면을 열지 못했습니다.<br>"
                   "잠시 후 다시 시도하거나 창을 닫았다 다시 열어 주세요.</p>")
            body = msg if frag else _shell(
                "오류", msg, "<p class='empty'>오류</p>", theme=th, skin=sk)
        finally:
            store.close()
        # 원격 이미지를 되살린 화면만 완화본. frag(패널 주입)은 문서의 CSP 를
        # 못 바꾸므로 대상이 아니다 — 그래서 그 링크는 app.js 가 가로채지 않는다.
        relaxed = (not frag and path.startswith("/thread/")
                   and (qs.get("images") or [""])[0] == "1")
        self._send_html(body, code, csp=CSP_IMAGES if relaxed else "")

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
        if path == "/appwin":             # 앱 창 등록/하트비트/닫힘 (appwin.js)
            _app_win_event((form.get("ev") or [""])[0],
                           (form.get("id") or [""])[0])
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
            val = _skin_ok((form.get("skin") or [_DEFAULT_SKIN])[0])
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
        # 사용자 조작 쓰기의 잠금 대기를 묶는다. 서버가 단일 스레드라 이 요청이
        # 기본 30초를 기다리면 **그동안 모든 화면이 함께 멈춘다** — sync 가 쉬지
        # 않고 청크를 커밋하는 동안 플래그 한 번이 30초 뒤 실패하는 것을 실측했다
        # (2026-08-15). 짧게 끊고 "다시 눌러 주세요"로 돌려보내는 편이 정직하다.
        store.db.execute(f"PRAGMA busy_timeout={Store.UI_WRITE_WAIT_MS}")
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
                    # 인물 화면으로 돌려보낸다 — 대기 카드·마커가 거기서도
                    # 그려져 폴링이 이어진다(현안 브리핑과 같은 관례).
                    # 상태 엔드포인트를 주소로 남기면 **이력에 '장소가 아닌
                    # URL'이 쌓여** 뒤로가기·좌측 스택이 그 자리로 돌아온다.
                    location = f"/people?addr={_q(who)}"
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
            elif path == "/settings/aitest":
                # AI 백엔드 상태의 [응답 시험] — PATH 에 있는 백엔드마다 1콜.
                if _job_start(_aitest_job, _aitest_lock,
                              rows=None, at="") is None:
                    location = "/settings?msg=" + _q("이미 시험 중입니다")
                else:
                    threading.Thread(target=_run_aitest_job,
                                     args=(self.cfg,), daemon=True).start()
                    location = "/settings?msg=" + _q("AI 백엔드에 물어보는 중…")
            elif path == "/settings/update":
                location = _git_update()          # git pull — 적용은 창 닫았다 재실행
            elif path in ("/settings/save", "/settings/noise",
                          "/settings/folder-exclude", "/settings/folder-include"):
                # 오버라이드 파일 저장 후 cfg 재로드 → 즉시 반영(다음 요청부터)
                home = self.cfg.home
                if path == "/settings/save":
                    location = _save_settings(home, form)
                elif path == "/settings/noise":
                    location = _save_noise(self.cfg, form)
                else:
                    location = _save_folder_exclude(
                        self.cfg, form, path.endswith("exclude"))
                _Handler.cfg = cfgmod.load(home)
            else:
                location = perform_action(store, self.cfg, path, form)
        except sqlite3.OperationalError as e:
            # 잠금 대기는 UI_WRITE_WAIT_MS 에서 끊긴다(아래 PRAGMA). 여기 오는 건
            # "sync 가 계속 쓰고 있다"는 뜻이라 SQLite 원문 대신 할 일을 말한다.
            if "lock" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            location = "/?msg=" + _q("동기화 중이라 지금은 저장하지 못했습니다 — "
                                     "잠시 후 다시 눌러 주세요")
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
        loc = location.replace("\r", "").replace("\n", "")
        if not loc.isascii():
            # 헤더는 latin-1 인코딩이라 한글이 새면 UnicodeEncodeError 로 요청이
            # 통째로 죽는다 — 실제로 다른 헤더에서 겪었다(2026-08-15). 이미
            # 인코딩된 %XX 는 건드리지 않게 '%' 를 safe 에 둔다.
            loc = urllib.parse.quote(loc, safe="/?:&=%#+,;@[]!$'()*~-._")
        self.send_header("Location", loc)
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
    window_size 는 마지막으로 기억된 창 크기(없으면 2000,1200).

    창의 수명은 여기서 추적하지 않는다 — 프로세스 핸들로 잡으려 했지만 Edge 가
    창을 이미 떠 있는 인스턴스에 넘겨 우리 프로세스가 즉시 끝나므로(전용 프로필을
    줘도 그렇다) 성립하지 않았다. 창이 열려 있는지는 페이지가 알린다(_watch_app_pages)."""
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


def _note(msg: str) -> None:
    """서버 콘솔 한 줄 — 앱 창 추적처럼 **조용히 실패하면 안 되는** 상태를 알린다.
    URL 안내와 같은 stream(stdout) 이라 순서가 뒤섞이지 않는다."""
    print(msg, flush=True)


# ── 앱 창 수명(페이지가 직접 알리는 경로) ───────────────────────────────
# 창 프로세스를 붙잡아 두는 방법을 먼저 만들었지만 Edge 가 창을 이미 떠 있는
# 인스턴스에 넘기면 우리가 띄운 프로세스가 0.0초 만에 끝나 무력했다(2026-08-09
# Windows 실측 — 전용 프로필을 줘도 그렇다. 그래서 통째로 걷어냈다).
# 창이 열려 있는지는 **페이지가 제일 정확히 안다**:
# 열릴 때 등록하고 닫힐 때 알린다(pagehide + sendBeacon — 창 크기 기억이 이미
# 같은 경로로 동작 중이다). 등록된 창이 하나도 없어야 서버를 내린다.
_APP_WINS: set = set()          # 열려 있는 앱 창 id
_APP_WIN_LOCK = threading.Lock()
_APP_WIN_EMPTY_AT = 0.0         # 마지막 창이 닫힌 시각(0=열린 창이 있음)
_APP_WIN_GRACE = 5.0            # 새로고침은 이 안에 새 id 로 다시 등록된다
_APP_WIN_TICK = 1.0             # 감시 주기
# app.js 의 beat 주기·실패 허용치와 **같아야 한다**(테스트가 두 값을 묶어 둔다).
# 여기 값은 사용자에게 알리는 소요 시간 계산에 쓴다.
_APP_BEAT_SEC = 4.0
_APP_BEAT_MISS = 2
# 실측(2026-08-09, WSL): beat 1회 = 서버 CPU 0.10ms · 왕복 0.23ms → 창 하나가
# 하루 종일 떠 있어도 서버 CPU 2.2초(0.002%). 기존 /latest 60초 폴링이 회당
# 0.80ms(DB 를 연다)라 그쪽이 더 비싸다. 창이 백그라운드면 브라우저가 타이머를
# 분당 1회로 조이므로 실제 비용은 이보다 낮다.


def _app_close_sec() -> int:
    """창을 닫고 서버가 끝나기까지 걸리는 최대 시간(초)."""
    return int(_APP_WIN_GRACE + _APP_WIN_TICK)


def _app_quit_sec() -> int:
    """서버가 끝나고 창이 닫히기까지 걸리는 최대 시간(초)."""
    return int(_APP_BEAT_SEC * _APP_BEAT_MISS)


def _app_win_reset() -> None:
    """창 등록 상태를 비운다 — 서버 시작 시(전역이라 앞선 서버 기록이 샌다)."""
    global _APP_WIN_EMPTY_AT
    with _APP_WIN_LOCK:
        _APP_WINS.clear()
        _APP_WIN_EMPTY_AT = 0.0


def _app_win_event(ev: str, wid: str) -> None:
    """앱 창 이벤트 — open/beat 는 등록, bye 는 해제."""
    global _APP_WIN_EMPTY_AT
    wid = (wid or "").strip()[:64]
    if not wid:
        return
    with _APP_WIN_LOCK:
        if ev == "bye":
            _APP_WINS.discard(wid)
            # 마지막 창이 닫혔다 — 유예 후에도 비어 있으면 서버를 내린다.
            # 새로고침·이동이면 곧 새 id 가 들어와 이 시각이 지워진다.
            _APP_WIN_EMPTY_AT = time.monotonic() if not _APP_WINS else 0.0
        else:
            _APP_WINS.add(wid)
            _APP_WIN_EMPTY_AT = 0.0


def _app_win_open() -> int:
    """등록된(열려 있는) 앱 창 수 — 0 은 '없다'와 '아직 등록 안 됐다' 둘 다다."""
    with _APP_WIN_LOCK:
        return len(_APP_WINS)


def _app_win_closed_for(now: float | None = None) -> float | None:
    """열린 창이 하나도 없으면 '닫힌 지 몇 초'인지, 아니면 None."""
    with _APP_WIN_LOCK:
        if _APP_WINS or not _APP_WIN_EMPTY_AT:
            return None
        return (now if now is not None else time.monotonic()) - _APP_WIN_EMPTY_AT


def _watch_app_pages(httpd, stop, tick: float = _APP_WIN_TICK,
                     grace: float = _APP_WIN_GRACE) -> bool:
    """앱 창이 전부 닫히면 서버를 정지시킨다(정지시켰으면 True).

    한 번도 등록되지 않았으면(옛 캐시 JS·JS 꺼짐) 영영 조건이 성립하지 않는다 —
    모르면 안 내리는 쪽으로 실패한다. 등록은 _APPWIN_JS 가 **모든 문서**에서
    하므로, 여기 도달했다면 정말로 창이 없는 것이다(2026-08-10 이전에는 통계
    페이지가 등록을 빠뜨려 그 전제가 깨져 있었다)."""
    while not stop.wait(tick):
        gone = _app_win_closed_for()
        if gone is not None and gone >= grace:
            _note(f"앱 창이 모두 닫혔습니다({gone:.1f}초 전) — 서버를 종료합니다")
            try:
                httpd.shutdown()
            except Exception:
                return False
            return True
    return False


def serve(cfg, port: int = 8765,
          open_browser: bool = False, app_mode: bool = False) -> None:
    _Handler.cfg = cfg
    # 클래스 속성이라 **반드시 매번 덮어쓴다** — 앞선 app 서버가 남긴 True 가
    # 그대로 살아 일반 서버의 페이지까지 창 수명 관리를 켜면 안 된다.
    _Handler.app_mode = bool(app_mode)
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
        _open_ui(url, app_mode,
                 cfg.opt("web", "window_size", default="2000,1200"))
    stopping = threading.Event()
    if app_mode:
        # 창 등록은 이 서버의 것만 센다 — 모듈 전역이라 같은 프로세스에서 앞선
        # 서버가 남긴 '마지막 창 닫힘' 기록이 살아 있으면 새 서버가 즉시 내려간다
        # (실사용은 프로세스가 매번 새로 뜨지만, 상태를 지우는 쪽이 맞다).
        _app_win_reset()
        _note(f"앱 창 모드 — 창을 닫으면 최대 {_app_close_sec()}초 뒤 서버가 끝납니다")
        threading.Thread(target=_watch_app_pages, args=(httpd, stopping),
                         daemon=True).start()
    interrupted = False
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stopping.set()
        if interrupted:
            # 한 줄로 끝낸다 — 창은 페이지가 스스로 닫으므로(beat 실패 감지)
            # 열린 창이 있을 때만 그 시간을 덧붙인다.
            tail = (f" — 앱 창은 최대 {_app_quit_sec()}초 안에 닫힙니다"
                    if app_mode and _app_win_open() else "")
            _note("종료" + tail)
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
