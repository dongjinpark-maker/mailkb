"""Outlook COM 소스 — 회사 PC(Windows + 클래식 Outlook + pywin32) 전용.

이 파일만 Windows 를 요구한다. import 는 생성 시점까지 지연되므로
Linux/WSL 에서 다른 소스로 개발·테스트하는 데 지장 없다.

회사 PC 최초 실행 점검은 `mailkb doctor` 가 대신한다(이 파일의 probe_outlook 이
그 재료를 만든다). 사람 눈이 필요한 것만 남는다 — Exchange 주소(X.500) → SMTP
변환 정상 여부, 증분 sync 속도(150통/일 기준).
"""

from __future__ import annotations

import email.parser
import email.utils
import heapq
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, NamedTuple
from urllib.parse import unquote

from ..clean import html_to_markdown
from .base import MailRecord

# MAPI 속성 (PropertyAccessor 용)
PR_TRANSPORT_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"

FOLDER_INBOX = 6
FOLDER_SENT = 5
FOLDER_DELETED = 3
FOLDER_JUNK = 23
OL_MAIL_ITEM = 0            # Folder.DefaultItemType — 메일 폴더만 훑는다

# 동시에 여는 폴더 커서 상한. heapq.merge 는 첫 소비 때 **모든** 반복자를
# 기동하므로(실측), N 개의 Restrict+Sort 뷰가 첫 레코드 전에 다 만들어진다.
DEFAULT_MAX_FOLDERS = 50

# sync_state 키 — 폴더별 '한 번 완주했는가' (A4 백필 장치). v1 은 형식 버전.
FOLDER_STATE_KEY = "outlook_folders_v1"

# 이름으로 거르는 기본 제외. 지운 편지함·정크는 받은 편지함의 **형제**라
# 구조상 이미 빠지지만, IMAP 계정은 INBOX.Trash 처럼 **아래** 붙는 경우가 있다.
_DEFAULT_EXCLUDE_NAMES = (
    "deleted items", "지운 편지함", "junk email", "junk e-mail", "정크 메일",
    "sync issues", "동기화 문제", "conversation history", "대화 기록",
    "rss feeds", "rss 피드",
)

# 인라인(cid) 이미지 수집 — docs/ARCHITECTURE.md §6.1.
# HTML 이 참조하는 cid 에 대응하는 '이미지' 첨부만 바이트로 동봉하고,
# 치환은 store 가 정제(인용 절단) 후에 한다.
_CID_SRC_RX = re.compile(r"src=[\"']cid:([^\"']+)", re.IGNORECASE)
_IMG_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
}


# ───────────────────────────────────────── 폴더 범위 (받은 편지함 하위 재귀)
# 규칙으로 수신 메일을 하위 폴더에 자동 분류하는 환경에서, 받은 편지함 직속만
# 훑으면 색인이 **조용히** 거의 빈 채로 남는다(2026-08-09 확산 검토). 정책 판단은
# 전부 plan_folders 한 곳에 모아 COM 밖으로 뺐다 — Linux 에서 전부 시험된다.


@dataclass(frozen=True)
class FolderCandidate:
    """COM 순회가 찾아낸 폴더 하나 — 정책 판단 **전**의 날 사실."""

    label: str                  # "inbox" | "inbox/프로젝트/NPX" | "sent"
    depth: int                  # inbox·sent = 0
    received: bool              # 받은 편지함 계열인가
    item_type: int = OL_MAIL_ITEM
    special: str = ""           # "deleted" | "junk" — CompareEntryIDs 로 확인된 것
    entry_id: str = ""          # 진단용 보조. **식별자가 아니다**(아래 참조)
    folder: object = None       # COM Folder. 순수 테스트에서는 None


@dataclass(frozen=True)
class FolderSpec:
    """실제로 열 폴더 하나 — 라벨과 '수신 폴더인가'를 따로 나른다.

    분리한 이유가 이 변경의 정확성 핵심이다. 하위 폴더는 라벨이 "inbox" 가
    아니면서 내용물은 수신 메일이라, 종전의 문자열 비교(`name == "inbox"`)로는
    시각 필드를 못 고른다. Sort 키 · _to_record 의 시각 · heapq 병합 키 셋이
    어긋나면 전역 시간순 입력 가정이 깨진다(docs/ARCHITECTURE.md §6).
    """

    label: str
    received: bool
    folder: object = None
    entry_id: str = ""
    known: bool = True          # False = 한 번도 끝까지 읽은 적 없음 → 백필 대상


class Skip(NamedTuple):
    """건너뛴 폴더 하나 — 이유와 **그 이유의 종류**.

    종류를 나눠 두는 것이 중요하다. 'structural'(지운 편지함·비메일 폴더)은
    설정을 바꿔도 안 변하는 사실이지만, 'setting'(제외 목록·하위 폴더 끔)은
    **설정의 거울**이라 사용자가 설정을 바꾸는 순간 낡는다. 화면이 둘을 같게
    다루면, 제외를 풀어도 지난 수집의 '제외 목록'이 남아 그 행이 버튼 없이
    갇힌다(2026-08-10 실제 발생).

    'capacity'(상한 초과)는 셋째 부류다 — 설정도 구조도 아니고 **그때 자리가
    없었다**는 사실이라, 설정을 바꿔도 낡지 않고 다음 수집에서 달라질 수 있다.
    화면은 버튼을 주되 참고로 사유를 함께 적는다.
    """

    label: str
    reason: str
    kind: str = "setting"   # "setting" | "structural" | "capacity"


SKIP_SUBFOLDERS_OFF = "하위 폴더 수집 꺼짐"
SKIP_EXCLUDED = "제외 목록"
_SKIP_SPECIAL = "{} 계열"
_SKIP_NONMAIL = "메일 폴더 아님(DefaultItemType={})"
_SKIP_CAP = "폴더 상한 {} 초과"


def infer_skip_kind(reason: str) -> str:
    """kind 없이 저장된 구 행의 종류 추정 — **읽기 전용 마이그레이션 전용**.

    새 코드는 Skip.kind 를 그대로 쓴다. 사용자에게 보이는 문자열로 판정하는 것은
    원래 피할 일이지만, 여기서는 이미 저장된 값을 되살리는 유일한 방법이고
    (버리면 폴더 목록이 통째로 사라진다 — 2026-08-10 에 실제로 그렇게 했다)
    그 문자열은 이 파일이 만든 것이라 안다.

    모르는 사유는 structural 로 본다 — 버튼을 안 주는 쪽이 안전하다. 화면은
    사용자의 현재 제외 설정을 이보다 먼저 보므로, 잘못 추정해도 되돌릴 수 있다.
    """
    r = (reason or "").strip()
    if r in (SKIP_SUBFOLDERS_OFF, SKIP_EXCLUDED):
        return "setting"
    if r.startswith(_SKIP_CAP.split("{", 1)[0]):
        return "capacity"
    return "structural"


@dataclass
class FolderPlan:
    """이번 수집에서 열 폴더와, 건너뛴 폴더의 **이유**."""

    specs: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # [Skip]
    subfolders_enabled: bool = True
    max_folders: int = DEFAULT_MAX_FOLDERS

    def unknown(self) -> list:
        """이번에 처음부터 끝까지 읽을 폴더 — 사용자에게 미리 알릴 대상."""
        return [s.label for s in self.specs if not s.known]

    def as_rows(self) -> list:
        """설정 화면·doctor 가 쓰는 평범한 dict 목록 (COM 객체 없음)."""
        rows = [{"label": s.label, "included": True, "reason": "",
                 "known": s.known} for s in self.specs]
        rows += [{"label": sk.label, "included": False, "reason": sk.reason,
                  "kind": sk.kind, "known": True} for sk in self.skipped]
        return rows

    def summary_line(self) -> str:
        """sync·doctor·설정 화면이 공유하는 한 줄 (조용한 실패 금지)."""
        subs = sum(1 for s in self.specs if "/" in s.label)
        head = f"받은편지함 + 하위 {subs}개" if subs else "받은편지함"
        if not self.subfolders_enabled:
            head += " (하위 폴더 수집 꺼짐)"
        tail = f" · 건너뜀 {len(self.skipped)}개" if self.skipped else ""
        return f"{head} · 보낸편지함 · 폴더 {len(self.specs)}개{tail}"


def _norm_label(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _attr(obj, name: str, default=None):
    """COM 속성 안전 접근 — 스토어 종류에 따라 없거나 던지는 속성이 있다."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def plan_folders(cands, *, include_subfolders: bool = True,
                 exclude_names=(), max_folders: int = DEFAULT_MAX_FOLDERS,
                 known=None) -> FolderPlan:
    """후보 폴더 → 열 폴더 + 건너뛴 이유. **정책 전부가 여기 있고 COM 이 없다.**

    불변식: `len(specs) + len(skipped) == len(cands)` — 어떤 폴더도 이유 없이
    사라지지 않는다. 이 저장소는 조용한 실패를 금지한다.

    순서는 루트(inbox·sent) 먼저, 그다음 (depth, label). 상한은 루트를 안 센다 —
    상한이 낮아도 종전 동작(두 기본 폴더)은 반드시 남는다.

    known: `None` 은 '백필 판단 안 함'(전부 아는 폴더로 친다 — open/attach 나
    명시적 --full/--since 경로). **집합이면 빈 집합도 실제 상태**로 읽는다 —
    여기서 None 과 빈 집합을 같게 다루면 업그레이드 직후(상태 파일 없음 +
    워터마크 있음) 하위 폴더가 영영 백필되지 않는다.
    """
    excl = {_norm_label(x) for x in (*_DEFAULT_EXCLUDE_NAMES, *exclude_names)}
    excl.discard("")
    roots = [c for c in cands if c.depth == 0]
    subs = sorted((c for c in cands if c.depth > 0),
                  key=lambda c: (c.depth, c.label))
    specs, skipped = [], []

    def _take(c: FolderCandidate, always_known: bool = False) -> None:
        specs.append(FolderSpec(
            label=c.label, received=c.received, folder=c.folder,
            entry_id=c.entry_id,
            known=True if (known is None or always_known) else c.label in known))

    for c in roots:
        # 루트는 무조건 열고, **상태 파일이 없어도 아는 폴더로 친다**. 구버전이
        # 늘 훑던 폴더라 백필할 것이 없는데, 여기서 '처음 본다'고 판정하면
        # 업그레이드 첫 sync 가 사서함 전체를 다시 읽는다(규칙 5 위반).
        _take(c, always_known=True)
    for c in subs:
        name = c.label.rsplit("/", 1)[-1]
        if not include_subfolders:
            skipped.append(Skip(c.label, SKIP_SUBFOLDERS_OFF))
        elif c.special:
            what = "지운 편지함" if c.special == "deleted" else "정크 메일"
            skipped.append(Skip(c.label, _SKIP_SPECIAL.format(what),
                                "structural"))
        elif c.item_type != OL_MAIL_ITEM:
            # 최적화가 아니라 필수 — 일정·연락처 폴더에 Sort("[ReceivedTime]")
            # 을 걸면 예외가 난다.
            skipped.append(Skip(
                c.label, _SKIP_NONMAIL.format(c.item_type), "structural"))
        elif _norm_label(name) in excl or _norm_label(c.label) in excl:
            skipped.append(Skip(c.label, SKIP_EXCLUDED))
        elif max_folders and len(specs) - len(roots) >= max_folders:
            skipped.append(Skip(c.label, _SKIP_CAP.format(max_folders),
                                "capacity"))
        else:
            _take(c)
    return FolderPlan(specs=specs, skipped=skipped,
                      subfolders_enabled=include_subfolders,
                      max_folders=max_folders)


def parse_folder_state(raw) -> set:
    """sync_state 값 → 완주한 폴더 라벨 집합.

    없거나 깨졌으면 **빈 집합**이다. 보수적인 쪽이 안전하다 — 최악이 '한 번 더
    전부 읽고 전부 중복으로 건너뜀'이고, 반대 방향의 실수는 메일 누락이다.
    """
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, dict) or data.get("v") != 1:
        return set()
    done = data.get("done")
    return {str(x) for x in done} if isinstance(done, list) else set()


def merge_folder_state(raw, drained, now_iso: str = "", keep=None) -> str:
    """기존 상태 + 이번에 완주한 폴더 → 정렬된 JSON. 멱등.

    keep(이번에 실제로 연 폴더)을 주면 **범위를 벗어난 폴더를 기록에서 뺀다.**
    이게 없으면 이런 구멍이 난다: 하위 폴더 수집을 껐다가 한 달 뒤 다시 켜면
    그 폴더는 여전히 '아는 폴더'라 증분으로 열리는데, 그동안 전역 워터마크는
    받은편지함 때문에 전진해 있어 **꺼져 있던 한 달치가 영영 안 들어온다.**
    기록에서 빼면 다시 켤 때 한 번 전체를 읽고(대부분 중복) 그 구간을 메운다.

    keep 이 비었으면 지우지 않는다 — 폴더 순회 실패로 계획이 텅 빈 것을
    '전부 범위 밖'으로 읽으면 다음 sync 가 사서함 전체를 다시 읽는다.
    """
    done = parse_folder_state(raw)
    if keep:
        done &= {str(x) for x in keep}
    done |= {str(x) for x in (drained or [])}
    out = {"v": 1, "done": sorted(done)}
    if now_iso:
        out["at"] = now_iso
    return json.dumps(out, ensure_ascii=False)


def _norm_cid(cid: str) -> str:
    return unquote(cid or "").strip().strip("<>").lower()


def _collect_inline_images(attachments, html: str) -> tuple[dict, int]:
    """HTMLBody 의 cid: 참조에 대응하는 이미지 첨부 바이트 수집.

    attachments: COM Attachment 유사 객체 iterable (FileName,
    PropertyAccessor.GetProperty, SaveAsFile) — 순수 로직이라 WSL 에서
    모의 객체로 테스트 가능. 반환 ({cid: (mime, bytes)}, 실패 수).
    항목 단위 실패는 삼키고 계속 — 매칭 실패한 cid 는 store 정제 후
    차단 마크로 남아 웹에서 '추출 실패' 안내가 뜬다(graceful).
    """
    wanted = {_norm_cid(c) for c in _CID_SRC_RX.findall(html or "")}
    if not wanted:
        return {}, 0
    out: dict = {}
    failed = 0
    for a in attachments:
        try:
            cid_raw = a.PropertyAccessor.GetProperty(PR_ATTACH_CONTENT_ID) or ""
        except Exception:
            cid_raw = ""
        cid = _norm_cid(cid_raw)
        if not cid or cid not in wanted or cid in out:
            continue
        ext = (a.FileName or "").rpartition(".")[2].lower()
        mime = _IMG_MIME.get(ext)
        if not mime:
            failed += 1                     # cid 참조인데 이미지 확장자가 아님
            continue
        fd, tmp = tempfile.mkstemp(prefix="mailkb_cid_")
        os.close(fd)
        try:
            a.SaveAsFile(tmp)
            with open(tmp, "rb") as f:
                data = f.read()
            if data:
                out[cid] = (mime, data)
            else:
                failed += 1
        except Exception:
            failed += 1                     # SaveAsFile 실패 등 — 항목만 포기
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out, failed


def _dasl_utc(since_iso: str, overlap_minutes: int = 30) -> str:
    """DASL 필터용 날짜 문자열 — 로컬 naive ISO 를 UTC 로 변환.

    DASL(@SQL) 날짜 비교는 UTC 기준이다(MS 문서). 저장된 sent_on 은
    로컬 시각이라 그대로 넣으면 필터가 KST 기준 9시간 미래로 밀려
    그 사이 도착 메일이 증분에서 누락된다. overlap 은 시계 오차·경계
    안전망 — 겹쳐 읽은 메일은 message_id UNIQUE 가 걸러낸다.
    """
    local = datetime.fromisoformat(since_iso).astimezone()  # naive → 로컬
    utc = local.astimezone(timezone.utc) - timedelta(minutes=overlap_minutes)
    return utc.strftime("%Y-%m-%d %H:%M")


def _unique_filename(name: str, used: set) -> str:
    """dest 디렉토리 내 동명 첨부 충돌 방지 — 중복이면 stem-1, stem-2 …"""
    name = name or "attachment"
    if name not in used:
        used.add(name)
        return name
    base, dot, ext = name.rpartition(".")
    stem, suffix = (base, "." + ext) if dot else (name, "")
    i = 1
    while f"{stem}-{i}{suffix}" in used:
        i += 1
    out = f"{stem}-{i}{suffix}"
    used.add(out)
    return out


def _guard_policy() -> tuple:
    """프로그래밍 방식 액세스 정책값을 레지스트리에서 읽는다 — 즉시, 팝업 없음.

    (값, 출처) 또는 (None, ""). winreg 는 Windows 전용이라 **지연 import** 한다
    (이 모듈은 Linux 에서도 import 되어야 한다).
    """
    try:
        import winreg
    except Exception:
        return (None, "")
    names = ("ObjectModelGuard", "AdminSecurityMode")
    for root, rname in ((getattr(winreg, "HKEY_CURRENT_USER", None), "HKCU"),
                        (getattr(winreg, "HKEY_LOCAL_MACHINE", None), "HKLM")):
        for ver in ("16.0", "15.0"):
            path = (f"Software\\Policies\\Microsoft\\Office\\{ver}"
                    "\\Outlook\\Security")
            try:
                with winreg.OpenKey(root, path) as k:
                    for n in names:
                        try:
                            return (winreg.QueryValueEx(k, n)[0],
                                    f"{rname}\\...\\{ver}\\{n}")
                        except OSError:
                            continue
            except OSError:
                continue
    return (None, "")


def probe_outlook(cfg=None, known: set | None = None,
                  item_probe: bool = True) -> dict:
    """doctor 용 **읽기 전용** COM 프로브 — 던지지 않고 dict 에 담아 준다.

    win32com import 는 이 파일 밖으로 못 나간다(CLAUDE.md 1). 그래서 판정 로직은
    doctor.py 에 있고 여기는 사실 수집만 한다. 반환이 순수 dict 라서 Linux
    테스트가 같은 모양을 손으로 지어 doctor.run 전체를 검증할 수 있다.

    폴더 목록은 **sync 와 같은 경로**(folder_plan)로 만든다 — 미리보기와 실제
    수집 범위가 어긋나면 "왜 색인이 비었나"에 두 가지 답이 나온다.
    """
    out = {"available": False, "error": "", "running": False}
    try:
        import win32com.client            # noqa: F401 — 존재 확인
    except ImportError as e:
        out["pywin32_missing"] = True
        out["error"] = " ".join(str(e).split())[:200]
        return out
    try:
        from importlib.metadata import version
        out["pywin32"] = version("pywin32")
    except Exception:
        pass
    try:
        import win32com.client
        # GetActiveObject 가 되면 이미 떠 있는 것 — Dispatch 는 없으면 띄운다
        try:
            win32com.client.GetActiveObject("Outlook.Application")
            out["running"] = True
        except Exception:
            pass
        src = OutlookComSource(cfg=cfg, known_folders=known)
    except Exception as e:
        out["error"] = " ".join(str(e).split())[:200]
        return out

    out["available"] = True
    out["version"] = str(_attr(src._app, "Version", "") or "")
    try:
        out["accounts"] = [str(_attr(a, "SmtpAddress", "") or _attr(a, "DisplayName", ""))
                           for a in src._ns.Accounts]
    except Exception:
        out["accounts"] = []
    try:
        st = src._ns.GetDefaultFolder(FOLDER_INBOX).Store
        out["store"] = {"name": str(_attr(st, "DisplayName", "") or "")}
    except Exception:
        out["store"] = {}

    pol, where = _guard_policy()
    guard = {"policy": pol, "policy_src": where, "probe": "skip", "error": ""}
    if item_probe:
        # 개체 모델 가드가 실제로 막는 것은 폴더·통수가 아니라 **주소 속성**이다.
        # 수집이 메일마다 부르는 그 속성을 여기서 한 번 읽어 미리 걸린다.
        try:
            first = src._ns.GetDefaultFolder(FOLDER_INBOX).Items.GetFirst()
            if first is None:
                guard["probe"] = "empty"
            else:
                _ = first.SenderEmailAddress
                guard["probe"] = "ok"
        except Exception as e:
            msg = " ".join(str(e).split())[:200]
            guard["probe"] = "blocked" if _looks_blocked(msg) else "error"
            guard["error"] = msg
    out["guard"] = guard

    try:
        plan = src.folder_plan()
        rows = plan.as_rows()
        counts = {}
        for s in plan.specs:
            items = _attr(s.folder, "Items", None)
            counts[s.label] = _attr(items, "Count", None) if items else None
        for r in rows:
            if r["included"]:
                r["count"] = counts.get(r["label"])
        out["folders"] = rows
        out["scope"] = {
            "subfolders": plan.subfolders_enabled,
            "max_folders": plan.max_folders,
            "exclude": ([str(x) for x in
                         (cfg.opt("sources", "exclude_folders", default=[]) or [])]
                        if cfg is not None else []),
        }
    except Exception as e:
        out["folders"] = []
        out["folders_error"] = " ".join(str(e).split())[:200]
    return out


def _looks_blocked(msg: str) -> bool:
    """가드 차단인가, 다른 COM 오류인가 — 문구·HRESULT 로 가른다."""
    low = (msg or "").lower()
    return any(k in low for k in (
        "0x80080057", "-2147221231", "operation aborted", "작업이 취소",
        "denied", "거부", "보안", "security", "programmatic"))


class OutlookComSource:
    name = "outlook"

    def __init__(self, cfg=None, known_folders: set | None = None) -> None:
        import win32com.client  # Windows 전용 — 지연 import

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")
        self._cfg = cfg
        # None = '전부 아는 폴더'(백필 없음) — open/attach 와 명시적
        # --full/--since 경로가 이걸 쓴다. 루트를 항상 아는 것으로 치는 규칙은
        # 여기가 아니라 plan_folders 안에 있다(정책은 한 곳에).
        self._known = None if known_folders is None else set(known_folders)
        self._plan: FolderPlan | None = None
        self.drained_folders: list = []      # 이번에 끝까지 읽은 폴더 라벨

    # ------------------------------------------------------------- fetch

    def _scope_opts(self) -> dict:
        """[sources] 설정 — 없으면 기본(하위 재귀 켬)."""
        cfg = self._cfg
        if cfg is None:
            return {}
        excl = cfg.opt("sources", "exclude_folders", default=[]) or []
        return {
            "include_subfolders": bool(
                cfg.opt("sources", "include_subfolders", default=True)),
            "exclude_names": tuple(str(x) for x in excl),
            "max_folders": int(
                cfg.opt("sources", "max_folders", default=DEFAULT_MAX_FOLDERS)
                or 0),
        }

    def folder_plan(self) -> FolderPlan:
        """이번 실행에서 열 폴더 — 메모이즈. sync·doctor·_find 가 공유한다.

        공유하는 것이 요점이다. doctor 의 미리보기와 sync 의 실제 범위가
        어긋날 수 없어야 "왜 색인이 비었나"에 같은 답이 나온다.
        """
        if self._plan is None:
            self._plan = plan_folders(self._candidates(), known=self._known,
                                      **self._scope_opts())
        return self._plan

    def _special_ids(self) -> dict:
        """지운 편지함·정크의 EntryID — 스토어 종류에 따라 없을 수 있다."""
        out = {}
        for key, const in (("deleted", FOLDER_DELETED), ("junk", FOLDER_JUNK)):
            try:
                out[key] = self._ns.GetDefaultFolder(const).EntryID
            except Exception:
                pass          # 없는 스토어가 있다 — 여기서 죽으면 스캔 전체가 죽는다
        return out

    def _same_folder(self, a: str, b: str) -> bool:
        """MAPI 는 같은 객체에 short-term/long-term 두 EntryID 를 줄 수 있어
        문자열 비교로는 부족하다 — CompareEntryIDs 가 정답이고 == 는 폴백."""
        if not a or not b:
            return False
        try:
            return bool(self._ns.CompareEntryIDs(a, b))
        except Exception:
            return a == b

    def _candidates(self) -> list:
        """받은 편지함 서브트리 BFS + 보낸 편지함. **정책은 안 본다**(plan_folders 몫).

        보낸 편지함은 하위를 훑지 않는다 — 수신 메일을 규칙으로 분류하는 것이
        이 변경의 동기이고, 보낸 편지함 분류는 드물다.
        """
        inbox = self._ns.GetDefaultFolder(FOLDER_INBOX)
        sent = self._ns.GetDefaultFolder(FOLDER_SENT)
        special = self._special_ids()
        out = [FolderCandidate("inbox", 0, True, OL_MAIL_ITEM, "",
                               _attr(inbox, "EntryID", ""), inbox)]
        queue = [(inbox, "inbox", 0)]
        while queue:
            parent, plabel, depth = queue.pop(0)
            try:
                children = list(parent.Folders)
            except Exception:
                continue
            for f in children:
                # 라벨의 '/' 는 경로 구분자라 폴더명 안의 '/' 는 치환한다
                name = str(_attr(f, "Name", "") or "?").replace("/", "／")
                label = f"{plabel}/{name}"
                eid = _attr(f, "EntryID", "")
                kind = next((k for k, v in special.items()
                             if self._same_folder(eid, v)), "")
                out.append(FolderCandidate(
                    label, depth + 1, True,
                    int(_attr(f, "DefaultItemType", OL_MAIL_ITEM) or 0),
                    kind, eid, f))
                if not kind:
                    queue.append((f, label, depth + 1))
        out.append(FolderCandidate("sent", 0, False, OL_MAIL_ITEM, "",
                                   _attr(sent, "EntryID", ""), sent))
        return out

    def fetch(self, since_iso: str | None,
              image_cutoff: str | None = None) -> Iterator[MailRecord]:
        """계획된 폴더들을 sent_on 기준 병합해 전역 시간순으로 yield.

        폴더 순차 순회(구현 초기)는 폴더 안에서만 시간순이라, 내가 시작한
        스레드의 백필에서 상대 답장(Inbox)이 내 원 메일(Sent)보다 먼저 들어와
        store 의 '시간순 입력' 가정과 mid-join 판정(새 스레드 생성 = 첫 보유분)
        을 깨뜨렸다. 각 폴더가 이미 시간 정렬이므로 병합은 heapq.merge 로 lazy.
        하위 폴더가 붙어 스트림이 N 개로 늘어도 같은 계약이다.

        image_cutoff(YYYY-MM-DD): 이 날짜 이전 메일은 인라인 이미지 추출을
        건너뛴다 — 대량 백필에서 곧 프룬될 이미지에 COM 왕복을 쓰지 않는다."""
        streams = [self._folder_stream(spec, since_iso, image_cutoff)
                   for spec in self.folder_plan().specs]
        yield from heapq.merge(*streams, key=lambda r: r.sent_on)

    def _folder_stream(self, spec: FolderSpec, since_iso: str | None,
                       image_cutoff: str | None) -> Iterator[MailRecord]:
        items = spec.folder.Items
        # 처음 보는 폴더는 Restrict 를 걸지 않는다. 규칙이 이미 분류해 둔 메일은
        # 전부 워터마크보다 **과거**라, 증분 필터를 걸면 범위만 넓히고 아무것도
        # 못 가져온다. 한 번 완주하면 상태에 남고 다음부터 증분에 합류한다.
        restrict = since_iso if spec.known else None
        if restrict:
            # DASL 필터 — 로캘 무관, 단 날짜는 UTC 로 비교됨 → 변환 필수.
            # Restrict 결과 컬렉션은 정렬을 승계하지 않을 수 있어 Sort 는 뒤에.
            when = _dasl_utc(restrict)
            items = items.Restrict(
                f"@SQL=\"urn:schemas:httpmail:datereceived\" > '{when}'"
            )
        # 병합 키(rec.sent_on)와 같은 필드로 정렬 — 라벨이 아니라 received 를 본다
        items.Sort("[ReceivedTime]" if spec.received else "[SentOn]")
        item = items.GetFirst()
        while item is not None:
            rec = self._to_record(item, spec, image_cutoff)
            if rec is not None:
                yield rec
            item = items.GetNext()
        if restrict is None:
            # 완주한 '무제한 읽기'만 기록한다. 소비자가 중간에 버린 제너레이터는
            # GeneratorExit 로 끊겨 여기 못 온다 — 부분 백필이 완료로 남지 않는다.
            self.drained_folders.append(spec.label)

    def _to_record(self, item, spec: FolderSpec,
                   image_cutoff: str | None = None) -> MailRecord | None:
        if getattr(item, "Class", None) != 43:  # olMail 만 (회의요청 등 제외)
            return None

        headers = self._headers(item)
        message_id = (headers.get("Message-ID") or "").strip()
        if not message_id:
            message_id = self._prop(item, PR_INTERNET_MESSAGE_ID) or f"<entry:{item.EntryID}>"

        refs_raw = headers.get("References", "") or ""
        # HTMLBody: 표시용은 원본 그대로(store 가 정제해 저장), 검색/AI용 텍스트는
        # 마크다운으로 변환(서식 보존). item.Body 는 서식 없는 평문이라 폴백.
        html = getattr(item, "HTMLBody", "") or ""
        body = html_to_markdown(html) if html.strip() else ""
        if not body.strip():
            body = item.Body or ""

        when = item.ReceivedTime if spec.received else item.SentOn
        when_iso = when.strftime("%Y-%m-%dT%H:%M:%S") if when else ""
        to, cc = self._recipients(item)

        # 인라인 이미지 수집 — cid 참조가 있고 컷오프 안쪽 메일만 (COM 왕복 절약)
        inline: dict = {}
        if html and "cid:" in html.lower() and not (
                image_cutoff and when_iso and when_iso[:10] < image_cutoff):
            inline, _ = _collect_inline_images(item.Attachments, html)

        return MailRecord(
            message_id=message_id,
            subject=item.Subject or "",
            sender_name=item.SenderName or "",
            sender_addr=self._sender_smtp(item),
            to=to,
            cc=cc,
            sent_on=when_iso,
            body_text=body,
            body_html=html,
            inline_images=inline,
            entry_id=item.EntryID,
            in_reply_to=(headers.get("In-Reply-To") or "").strip(),
            references=refs_raw.split(),
            conversation_key=getattr(item, "ConversationID", "") or "",
            attachments=[a.FileName for a in item.Attachments],
            folder=spec.label,
        )

    # ----------------------------------------------------------- helpers

    def _prop(self, item, prop: str) -> str:
        try:
            return item.PropertyAccessor.GetProperty(prop) or ""
        except Exception:
            return ""

    def _headers(self, item) -> dict:
        """인터넷 헤더 파싱. 보낸편지함 항목은 transport 헤더가 없을 수 있음."""
        raw = self._prop(item, PR_TRANSPORT_HEADERS)
        if not raw:
            return {}
        msg = email.parser.HeaderParser().parsestr(raw)
        return dict(msg.items())

    def _sender_smtp(self, item) -> str:
        """Exchange X.500 주소를 SMTP 로 변환."""
        try:
            if item.SenderEmailType == "EX":
                exu = item.Sender.GetExchangeUser()
                if exu:
                    return exu.PrimarySmtpAddress
            return item.SenderEmailAddress or ""
        except Exception:
            return getattr(item, "SenderEmailAddress", "") or ""

    def _recipients(self, item) -> tuple[list[str], list[str]]:
        to: list[str] = []
        cc: list[str] = []
        for r in item.Recipients:
            addr = self._recipient_smtp(r)
            if not addr:
                continue
            (to if r.Type == 1 else cc).append(addr)  # 1=To, 2=CC, 3=BCC
        return to, cc

    def _recipient_smtp(self, recipient) -> str:
        try:
            ae = recipient.AddressEntry
            if ae.Type == "EX":
                exu = ae.GetExchangeUser()
                if exu:
                    return exu.PrimarySmtpAddress
            return recipient.Address or ""
        except Exception:
            return ""

    # -------------------------------------------- hot 저장소 O(1) 접근

    def get_item(self, entry_id: str, message_id: str = ""):
        """EntryID 로 O(1) 조회. 실패(폴더 이동 등) 시 Message-ID 로 재검색."""
        try:
            return self._ns.GetItemFromID(entry_id)
        except Exception:
            if message_id:
                return self._find_by_message_id(message_id)
            raise

    def _find_by_message_id(self, message_id: str):
        dasl = (
            f"@SQL=\"http://schemas.microsoft.com/mapi/proptag/0x1035001E\""
            f" = '{message_id}'"
        )
        # 수집 계획 전체를 훑는다(메모이즈되어 재순회 비용 없음). 종전에는 기본
        # 폴더 둘만 봐서, 규칙으로 하위 폴더에 분류된 메일은 EntryID 가 바뀌는
        # 순간(폴더 이동) open·attach 가 원리적으로 실패했다.
        for spec in self.folder_plan().specs:
            try:
                found = spec.folder.Items.Find(dasl)
            except Exception:
                continue
            if found is not None:
                return found
        return None

    def open_in_outlook(self, entry_id: str, message_id: str = "") -> bool:
        item = self.get_item(entry_id, message_id)
        if item is None:
            return False
        item.Display()
        return True

    def save_attachments(self, entry_id: str, dest_dir: str, message_id: str = "",
                         used: set | None = None) -> list[str]:
        """큐레이션 시 첨부를 vault 옆으로 추출 (Cold 계층).

        used 를 넘기면 여러 메일에 걸친 동명 첨부도 서로 덮어쓰지 않는다.
        """
        import os

        item = self.get_item(entry_id, message_id)
        if item is None:
            return []
        if used is None:
            used = set()
        saved = []
        for a in item.Attachments:
            fname = _unique_filename(a.FileName, used)
            path = os.path.join(dest_dir, fname)
            a.SaveAsFile(path)
            saved.append(path)
        return saved
