"""사전 점검 — 수집 전에 "이 PC 에서 되는가" 를 30초 안에 답한다.

왜 있는가: 종전에는 첫 실패가 `sync` 도중에, 알아볼 수 없는 모습으로 왔다.
pywin32 없음은 raw ModuleNotFoundError, 보안 센터 차단은 16진수 com_error 였고,
둘 다 수십 분짜리 명령을 절반쯤 돌린 뒤에 나왔다. `diagnose` 는 **이미 수집된**
DB 를 읽는 사후 도구라 그 자리를 대신하지 못한다.

계약 셋:
  1. **AI 호출 0 · 네트워크 0.** 백엔드는 PATH 존재만 보고, 실제 응답 확인은
     `diagnose --backend` 의 몫이다(CLAUDE.md §2 의 '호출 0' 목록에 속한다).
  2. **설정도 DB 도 없는 상태(init 전)에서 돌아야 한다.** cfg=None 은 정상 입력.
  3. **아무것도 만들지 않는다.** DB 는 읽기 전용 URI 로만 열고 Store 를 쓰지
     않는다 — Store 는 파일을 만들고 마이그레이션을 돌린다.

이 파일은 COM 을 모른다(CLAUDE.md 1: pywin32 는 sources/outlook_com.py 안에서만).
Outlook 사실은 `probe_outlook` 이 만든 **평범한 dict** 로 받는다. 그 덕분에
Linux 테스트가 dict 를 손으로 지어 판정 로직 전체를 검증한다.
"""

from __future__ import annotations

import platform
import shutil
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import store
from .config import ROLE_LABEL

OK, WARN, FAIL, SKIP, INFO = "ok", "warn", "fail", "skip", "info"

# 신호등 — **cp949 에서 살아남는 문자만 쓴다.** ✓ ⚠ ✗ 와 이모지는 한국어
# Windows 콘솔에서 전부 '?' 로 치환된다(main 이 errors="replace"). 색도 못 쓰므로
# (ANSI 금지) 모양만으로는 구별이 약하다 — 그래서 한글 라벨을 함께 붙인다.
# INFO 와 SKIP 은 판정이 아니라 안내라 같은 표시를 쓴다. 상태를 둘로 나눠 둔 것은
# 테스트가 "확인 불가"와 "참고"를 구분하기 위해서다.
_MARK = {OK: "● 통과", WARN: "▲ 주의", FAIL: "■ 실패",
         SKIP: "○ 확인불가", INFO: "· 참고"}

SECTIONS = ("환경", "Outlook", "폴더 범위", "설정", "저장소", "AI")

# 맨 위에 뜨는 한눈 요약 — "무엇이 되고 무엇이 안 되나".
# 항목별 점검을 다 읽지 않아도 이 네 줄이면 판단이 선다. pywin32 가 없을 때
# '이 도구를 못 쓴다'가 아니라 **수집과 원문 열기만 막힌다**는 것도 여기서 보인다.
CAP_COLLECT = "메일 수집"
CAP_READ = "검색·회고·웹 UI"
CAP_OPEN = "Outlook 원문 열기"
CAP_AI = "AI 기능"


@dataclass(frozen=True)
class Check:
    """점검 한 줄. warn·fail 이면 remedy 가 비어 있으면 안 된다 — 처방 없는
    경고는 사용자를 막다른 길에 세운다(테스트가 이걸 강제한다)."""

    section: str
    name: str
    status: str
    detail: str = ""
    remedy: str = ""
    extra: tuple = field(default_factory=tuple)   # 들여쓴 보조 줄(폴더 목록 등)


def _env_facts(env=None) -> dict:
    """플랫폼 사실 — 테스트가 주입할 수 있게 한 곳에 모은다."""
    if env:
        return env
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "release": platform.release(),
        "windows": sys.platform == "win32",
        "encoding": (sys.stdout.encoding or "?").lower(),
        "year": str(date.today().year),
    }


def _cols(s: str) -> int:
    """터미널 칸 수 — 한글은 두 칸이라 len() 으로 맞추면 열이 어긋난다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, cols: int) -> str:
    return s + " " * max(0, cols - _cols(s))


def _fmt_n(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "?"


# ─────────────────────────────────────────────────────────────── 절별 점검


def _check_env(facts: dict) -> list:
    out = []
    py = facts.get("python", "?")
    major_minor = tuple(int(x) for x in py.split(".")[:2] if x.isdigit())
    if major_minor and major_minor < (3, 11):
        out.append(Check("환경", "Python", FAIL, f"{py} — 3.11 이상이 필요합니다",
                         "tomllib 를 씁니다. python.org 에서 3.11+ 를 설치하고 "
                         "'Add to PATH' 를 켜세요"))
    else:
        out.append(Check("환경", "Python", OK, f"{py} · tomllib 사용 가능"))
    osname = f"{facts.get('system', '?')} {facts.get('release', '')}".strip()
    enc = facts.get("encoding", "?")
    if facts.get("windows"):
        out.append(Check("환경", "OS", OK, f"{osname} · 콘솔 인코딩 {enc}"))
    else:
        out.append(Check(
            "환경", "OS", INFO, f"{osname} · 콘솔 인코딩 {enc}",
            "Outlook COM 은 Windows 전용입니다 — 이 환경에서는 데모만 가능합니다"))
    return out


def _check_outlook(ol, facts: dict) -> list:
    """Outlook 절. ol=None 은 실패가 아니라 '확인 불가'다."""
    if not facts.get("windows"):
        return [Check("Outlook", "COM", SKIP, "확인 불가 — Windows 가 아닙니다",
                      "데모: mailkb --home ./demo init 그리고 "
                      "mailkb --home ./demo sync --source fake --full")]
    if not ol or not ol.get("available"):
        err = (ol or {}).get("error", "")
        if (ol or {}).get("pywin32_missing"):
            return [Check("Outlook", "pywin32", FAIL, "설치되어 있지 않습니다",
                          "pip install pywin32 "
                          "(프록시 뒤라면 --proxy http://<프록시>:<포트>)")]
        return [Check(
            "Outlook", "COM", FAIL, err or "Outlook 개체를 만들지 못했습니다",
            "Outlook 을 실행한 뒤 다시 시도하세요. 실행 파일이 outlook.exe 면 "
            "클래식, olk.exe 면 새 Outlook 이고 새 Outlook 에는 COM 이 없습니다 — "
            "제목 표시줄의 '새 Outlook' 토글을 끄면 클래식으로 돌아갑니다")]

    out = []
    ver = ol.get("version") or "?"
    run = " · 이미 실행 중" if ol.get("running") else ""
    pw = ol.get("pywin32")
    out.append(Check("Outlook", "COM 연결", OK,
                     f"Outlook {ver} (클래식){run}"
                     + (f" · pywin32 {pw}" if pw else "")))

    accts = ol.get("accounts") or []
    store_name = (ol.get("store") or {}).get("name") or ""
    if accts:
        out.append(Check("Outlook", "MAPI 계정", OK,
                         f"{len(accts)}개 · " + " · ".join(accts[:3])
                         + (f" · 기본 저장소 {store_name}" if store_name else "")))
    else:
        out.append(Check("Outlook", "MAPI 계정", FAIL, "계정을 찾지 못했습니다",
                         "Outlook 에 메일 계정이 설정되어 있는지 확인하세요"))

    guard = ol.get("guard") or {}
    probe, pol = guard.get("probe"), guard.get("policy")
    pol_line = ("정책 레지스트리 값 없음 — 백신 등록 상태를 따르는 기본 동작"
                if pol is None else f"정책 값 {pol} ({guard.get('policy_src', '')})")
    if probe == "ok":
        out.append(Check("Outlook", "프로그래밍 방식 액세스", OK,
                         "통과 (주소 속성 1건 읽기 성공)", extra=(pol_line,)))
    elif probe == "blocked":
        out.append(Check(
            "Outlook", "프로그래밍 방식 액세스", FAIL,
            "차단되었습니다 — 수집이 발신자 주소를 못 읽습니다",
            "파일 › 옵션 › 보안 센터 › 보안 센터 설정 › 프로그래밍 방식 액세스. "
            "회사 정책으로 잠겨 있으면 IT 에 예외를 요청하세요",
            extra=(pol_line,)))
    elif probe == "empty":
        out.append(Check("Outlook", "프로그래밍 방식 액세스", WARN,
                         "받은 편지함이 비어 있어 확인하지 못했습니다",
                         "메일이 한 통이라도 있어야 판정할 수 있습니다",
                         extra=(pol_line,)))
    else:
        out.append(Check("Outlook", "프로그래밍 방식 액세스", WARN,
                         guard.get("error") or "확인하지 못했습니다",
                         "sync 가 발신자 주소에서 막히면 보안 센터를 보세요",
                         extra=(pol_line,)))
    return out


def _check_folders(ol, cfg, facts: dict) -> list:
    scope = (ol or {}).get("scope") or {}
    if cfg is not None and not scope:
        scope = {
            "subfolders": bool(cfg.opt("sources", "include_subfolders",
                                       default=True)),
            "max_folders": cfg.opt("sources", "max_folders", default=50),
            "exclude": [str(x) for x in
                        (cfg.opt("sources", "exclude_folders", default=[]) or [])],
        }
    head = ("하위 폴더 수집 "
            + ("켜짐" if scope.get("subfolders", True) else "꺼짐")
            + f" · 상한 {scope.get('max_folders', 50)}"
            + f" · 추가 제외 {len(scope.get('exclude') or [])}개")

    rows = (ol or {}).get("folders")
    if not rows:
        why = ("Outlook 이 없어 확인하지 못했습니다" if not facts.get("windows")
               else "폴더 목록을 읽지 못했습니다")
        return [Check("폴더 범위", "폴더 목록", SKIP, f"— {why} · 설정값만: {head}")]

    inc = [r for r in rows if r.get("included")]
    exc = [r for r in rows if not r.get("included")]
    total = sum(int(r.get("count") or 0) for r in inc)
    lines = tuple(
        f"{str(r.get('label', '')):32.32} {_fmt_n(r.get('count')):>8}"
        + ("   최초" if not r.get("known", True) else "")
        for r in inc[:20])
    out = [Check("폴더 범위", "스캔 대상", OK,
                 f"{len(inc)}개 · {_fmt_n(total)}통 · {head}", extra=lines)]
    if exc:
        out.append(Check("폴더 범위", "제외", INFO, f"{len(exc)}개",
                         extra=tuple(f"{r.get('label')} — {r.get('reason')}"
                                     for r in exc[:8])))
    fresh = [r for r in inc if not r.get("known", True)]
    if fresh:
        out.append(Check(
            "폴더 범위", "최초 수집", WARN,
            f"{len(fresh)}개 — 다음 sync 가 이 폴더들을 처음부터 읽습니다"
            " (수 분~수십 분)",
            "좁게 먼저 확인하려면 mailkb sync --since <YYYY-MM-DD>"))
    # 하위 폴더가 꺼져 있는데 서브트리에 메일이 더 많다 = '왜 색인이 비었나'의 답
    if not scope.get("subfolders", True):
        hidden = sum(int(r.get("count") or 0) for r in exc
                     if "하위 폴더 수집 꺼짐" in str(r.get("reason", "")))
        root = sum(int(r.get("count") or 0) for r in inc)
        if hidden > root:
            out.append(Check(
                "폴더 범위", "하위 폴더", WARN,
                f"수집에서 빠진 하위 폴더에 {_fmt_n(hidden)}통이 있습니다 "
                f"(수집 대상 {_fmt_n(root)}통보다 많습니다)",
                "규칙으로 분류하고 계신 것 같습니다 — 설정 › 수집 폴더에서 "
                "하위 폴더 수집을 켜세요"))
    return out


def _check_config(cfg, home: Path, ol, facts: dict) -> list:
    cfg_path = Path(home) / "config.toml"
    if cfg is None or not cfg_path.exists():
        return [Check("설정", "config.toml", WARN, f"없음 — {cfg_path}",
                      "먼저 mailkb init")]
    out = [Check("설정", "config.toml", OK, str(cfg_path))]
    addrs = [a for a in (cfg.my_addresses or []) if a]
    if not addrs:
        out.append(Check(
            "설정", "my_addresses", FAIL, "비어 있습니다",
            "내 발신을 판정하지 못해 회고·미답변·내 약속이 통째로 빕니다. "
            "수집할 때 쓰이는 값이라 나중에 채워도 안 살아납니다 — "
            "지금 넣고 sync 하세요. 별칭 주소가 있으면 함께 나열하세요"))
    else:
        accts = {a.lower() for a in ((ol or {}).get("accounts") or [])}
        mine = {a.lower() for a in addrs}
        if accts and not (accts & mine):
            out.append(Check(
                "설정", "my_addresses", WARN,
                f"{len(addrs)}개 · Outlook 계정({' · '.join(sorted(accts))})과 "
                "겹치지 않습니다",
                "내 발신이 수신으로 오분류됩니다. 계정 주소를 넣으세요"))
        else:
            out.append(Check("설정", "my_addresses", OK,
                             f"{len(addrs)}개 · my_names {len(cfg.my_names or [])}개"
                             f" · source={cfg.source}"))
    # 공휴일 목록은 음력 때문에 매년 손으로 갱신해야 한다. 떨어져도 조용히
    # 틀리는 종류라(연휴 직후 정체 오탐) 여기서 소리를 낸다.
    years = {str(h)[:4] for h in (cfg.holidays or [])}
    this_year = facts.get("year") or str(date.today().year)
    if not cfg.holidays:
        out.append(Check("설정", "공휴일", WARN, "목록이 비어 있습니다",
                         "영업일 계산이 주말만 뺍니다 — 연휴 직후 멀쩡한 스레드가 "
                         "'멈춤'으로 뜹니다. config.toml [review] holidays 참고"))
    elif this_year not in years:
        out.append(Check(
            "설정", "공휴일", WARN,
            f"{this_year}년이 목록에 없습니다 (있는 해: {' · '.join(sorted(years))})",
            "정체·기한 판정이 영업일 기준이라 연휴 직후 오탐이 납니다. "
            "config.toml [review] holidays 에 올해를 추가하세요"))
    if cfg.internal_domains and not (
            cfg.opt("filters", "external_allowlist", default=[]) or []):
        out.append(Check(
            "설정", "external_allowlist", WARN,
            "internal_domains 는 켜져 있고 허용 목록이 비었습니다",
            "협력사·고객사 메일이 미답변·기한 판정에서 조용히 빠집니다. "
            "config.toml [filters] external_allowlist 에 도메인을 넣으세요"))
    return out


def _check_numbering(span) -> list:
    """메일 번호가 날짜 기반인지 — 옛 DB 를 새 코드로 열면 **오류 없이** 섞인다.

    옛 id(1..N)와 새 id(YYMMDD*100000+…)는 충돌하지 않아 아무 일도 안 일어나는데,
    코퍼스의 일부만 날짜 기반이면 vault 참조 보존이라는 목적이 조용히 무효가 된다.
    이 저장소는 조용한 실패를 금지하므로 한 줄로 알린다.
    """
    lo, hi = (span or (None, None))
    if not lo:
        return []                              # 메일이 없으면 판정할 것도 없다
    if lo >= store.DAY_SPAN:
        return [Check("저장소", "메일 번호", OK,
                      f"날짜 기반 ({lo} ~ {hi})")]
    return [Check("저장소", "메일 번호", WARN,
                  f"옛 번호가 섞여 있습니다 ({lo} ~ {hi})",
                  "새 홈에 다시 수집해야 vault(주간 보고·노트)의 참조가 "
                  "재수집에도 보존됩니다")]


def _check_store(cfg, home: Path) -> list:
    """DB 는 **읽기 전용**으로만 연다 — doctor 는 아무것도 만들지 않는다."""
    db = Path(cfg.db_path) if cfg is not None else Path(home) / "db.sqlite"
    if not db.exists():
        return [Check("저장소", "db.sqlite", WARN, "없음 — 아직 한 번도 수집하지 않았습니다",
                      "회사 PC: mailkb sync · "
                      "개발/데모: mailkb --home ./demo sync --source fake --full")]
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            thr = con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            span = con.execute("SELECT MIN(id), MAX(id) FROM messages").fetchone()
            row = con.execute("SELECT key, value FROM sync_state "
                              "WHERE key IN ('last_sync','last_sync_checked_at')"
                              ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return [Check("저장소", "db.sqlite", FAIL,
                      " ".join(str(e).split())[:120],
                      "파일이 손상되었을 수 있습니다 — 백업에서 되돌리세요")]
    state = dict(row)
    mb = db.stat().st_size / (1024 * 1024)
    out = [Check("저장소", "db.sqlite", OK,
                 f"{mb:.1f}MB · 메시지 {_fmt_n(msgs)} · 스레드 {_fmt_n(thr)}")]
    out += _check_numbering(span)
    bits = []
    if state.get("last_sync_checked_at"):
        bits.append("실행 " + state["last_sync_checked_at"][:16])
    if state.get("last_sync"):
        bits.append("워터마크 " + state["last_sync"][:16])
    if bits:
        out.append(Check("저장소", "마지막 수집", OK, " · ".join(bits)))
    return out


def _check_ai(cfg, which) -> list:
    if cfg is None:
        return [Check("AI", "백엔드", INFO, "확인할 설정이 없습니다 (init 전)",
                      extra=("내장 기본값은 sonnet=claude · internal=opencode",))]
    seen, out = {}, []
    # 역할 해석은 config 가 한다 — 여기서 다시 만들면 실제 호출과 갈라진다.
    # 종전에는 미설정 역할을 전부 ai_default 로 봐서 claude 만 있는 PC 에
    # "internal (ask·weekly) opencode 없음" 이라는 거짓 경고가 났고, 정작
    # 현안 브리핑이 쓰는 diagnose(기본 opus)는 검사하지도 않았다(2026-08-19).
    for role in cfg._ROLES:
        name = cfg.backend_for(role)
        if name:
            seen.setdefault(str(name), []).append(role)
    if not seen:
        return [Check("AI", "백엔드", INFO, "설정된 백엔드가 없습니다",
                      extra=("AI 없이도 수집·검색·회고·웹 UI 는 전부 동작합니다",))]
    found = []
    for name, roles in seen.items():
        try:
            cmd = cfg.ai_cmd(name)
        except Exception:
            cmd = None
        binary = (cmd or [None])[0]
        where = which(binary) if binary else None
        label = f"{name} ({'·'.join(ROLE_LABEL.get(r, r) for r in roles)})"
        if where:
            found.append(name)
            out.append(Check("AI", label, OK, f"{binary} → {where}"))
        else:
            out.append(Check(
                "AI", label, WARN, f"{binary or '명령 미설정'} — PATH 에 없습니다",
                "AI 없이도 수집·검색·회고·웹 UI 는 전부 동작합니다. "
                "쓰려면 CLI 를 설치하고 PATH 에 등록하세요"))
    # 안내할 백엔드는 **PATH 에 실제로 있는 것**을 고른다 — 없는 것을 시험해
    # 보라고 하면 곧바로 두 번째 실패를 준다.
    # 인자 없는 `diagnose` 는 역할이 쓰는 백엔드마다 1회씩 시험한다 — 전부
    # PATH 에 있으면 그대로 권하고, 빠진 것이 있으면 **있는 것 하나**로 좁혀
    # 권한다(없는 것을 시험해 보라고 하면 곧바로 두 번째 실패를 준다).
    if found and len(found) == len(seen):
        tip = "실제 호출 확인: mailkb diagnose  (역할이 쓰는 백엔드마다 1회)"
    else:
        tip = f"실제 호출 확인: mailkb diagnose --backend {(found or sorted(seen))[0]}"
    out.append(Check("AI", "응답 시험", INFO, "이 명령은 하지 않습니다",
                     extra=(tip,)))
    return out


# ─────────────────────────────────────────────────────────────── 조립·출력


def capabilities(checks, facts: dict) -> list:
    """항목별 점검 → "무엇이 되고 무엇이 안 되나" 네 줄.

    항목을 다 읽게 하지 않으려는 것이다. 특히 pywin32 가 없을 때 종전 출력은
    '실패' 한 줄이라 **이 도구를 아예 못 쓴다**고 읽히는데, 사실은 수집과 원문
    열기만 막히고 이미 모은 것으로 검색·회고·웹 UI 는 그대로 된다.
    """
    by = {}
    for c in checks:
        by.setdefault((c.section, c.name), c)

    def st(section, name):
        c = by.get((section, name))
        return c.status if c else None

    def worst(*pairs):
        got = [st(*p) for p in pairs]
        for level in (FAIL, WARN, SKIP):
            if level in got:
                return level
        return OK if any(g == OK for g in got) else SKIP

    out = []
    if not facts.get("windows"):
        out.append(Check("요약", CAP_COLLECT, SKIP, "Windows 가 아닙니다",
                         "데모 코퍼스로는 전부 볼 수 있습니다 — "
                         "mailkb --home ./demo sync --source fake --full"))
        out.append(Check("요약", CAP_OPEN, SKIP, "Windows 가 아닙니다",
                         "Outlook 원문 열기는 회사 PC 에서만 됩니다"))
    else:
        com = worst(("Outlook", "COM 연결"), ("Outlook", "COM"),
                    ("Outlook", "pywin32"))
        guard = st("Outlook", "프로그래밍 방식 액세스")
        addr = st("설정", "my_addresses")
        collect = FAIL if FAIL in (com, guard, addr) else (
            WARN if WARN in (com, guard, addr) else OK)
        detail = {FAIL: "막혀 있습니다", WARN: "될 수도 있습니다", OK: "됩니다"}[collect]
        out.append(Check("요약", CAP_COLLECT, collect, detail,
                         "" if collect == OK else "아래 [Outlook]·[설정] 절을 보세요"))
        out.append(Check("요약", CAP_OPEN, com if com != OK else OK,
                         "됩니다" if com == OK else "막혀 있습니다",
                         "" if com == OK else "아래 [Outlook] 절을 보세요"))

    db = st("저장소", "db.sqlite")
    out.append(Check(
        "요약", CAP_READ, OK if db == OK else WARN,
        "됩니다 (이미 모은 메일로)" if db == OK else "아직 수집한 메일이 없습니다",
        "" if db == OK else "한 번 수집하면 됩니다 — 수집이 막혀 있어도 "
                            "이 기능들은 Outlook 을 쓰지 않습니다"))

    ai = [c for c in checks if c.section == "AI" and c.status in (OK, WARN)]
    ai_ok = any(c.status == OK for c in ai)
    out.append(Check(
        "요약", CAP_AI, OK if ai_ok else WARN,
        "됩니다" if ai_ok else "쓸 수 있는 백엔드가 없습니다",
        "" if ai_ok else "AI 없이도 수집·검색·회고·웹 UI 는 전부 동작합니다"))
    return out


def run(cfg, home, outlook=None, *, which=shutil.which, env=None) -> list:
    """점검 결과만 만든다 — 출력·종료코드는 호출자 몫. AI 호출 0 · 네트워크 0.

    cfg=None(init 전)과 outlook=None(Windows 아님·pywin32 없음)은 **정상 입력**
    이며 실패가 아니다. 테스트가 Linux 에서 돈다.
    """
    facts = _env_facts(env)
    home = Path(home)
    checks = list(_check_env(facts))
    checks += _check_outlook(outlook, facts)
    checks += _check_folders(outlook, cfg, facts)
    checks += _check_config(cfg, home, outlook, facts)
    checks += _check_store(cfg, home)
    checks += _check_ai(cfg, which)
    return capabilities(checks, facts) + checks


def render(checks, header: str = "") -> str:
    """신호등 요약 + 절별 항목. 처방은 그 줄 바로 아래 '→'.

    쓰는 문자는 **cp949 에서 살아남는 것만**이다(_MARK 주석 참고). ANSI 색도
    쓰지 않는다 — 그래서 기호가 아니라 한글 라벨이 실제 신호를 나른다.
    """
    out = ["mailkb doctor  " + "=" * 32]
    if header:
        out.append(header)
    caps = [c for c in checks if c.section == "요약"]
    if caps:
        out.append("")
        mw = max(_cols(_MARK.get(c.status, "")) for c in caps)
        nw = max(_cols(c.name) for c in caps)
        for c in caps:
            line = (f"  {_pad(_MARK.get(c.status, ''), mw)}  "
                    f"{_pad(c.name, nw)}  {c.detail}")
            out.append(line.rstrip())
            if c.remedy:
                out.append(f"      → {c.remedy}")
    for sec in SECTIONS:
        rows = [c for c in checks if c.section == sec]
        if not rows:
            continue
        out.append(f"\n[{sec}]")
        mw = max(_cols(_MARK.get(c.status, "")) for c in rows)
        for c in rows:
            mark = _pad(_MARK.get(c.status, " "), mw)
            detail = f" {c.detail}" if c.detail else ""
            out.append(f"  {mark}  {c.name}{detail}")
            for line in c.extra:
                out.append(f"      {line}")
            if c.remedy:
                out.append(f"      → {c.remedy}")
    body = [c for c in checks if c.section != "요약"]
    n = {k: sum(1 for c in body if c.status == k) for k in (OK, WARN, FAIL)}
    out.append(f"\n통과 {n[OK]} · 주의 {n[WARN]} · 실패 {n[FAIL]}"
               "   (AI 호출 0 · 네트워크 0)")
    return "\n".join(out)


def exit_code(checks) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
