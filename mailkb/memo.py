"""메모 — vault/memo/*.md. 메일에 묶이지 않는 내 기록과 할 일(2026-11-18).

스레드 노트는 스레드 하나에 붙고, 지식은 AI 가 캐낸 것을 사람이 승인해야 생긴다.
그 사이에 "메일을 보다가, 또는 일하다가 떠오른 것을 바로 적는 자리"가 없었다.
메모는 그 자리다 — 지식과 같은 쓰임(분석·현안 브리핑의 문맥)이되 쓰는 길이 가볍다.

**메모 한 건에 파일 하나, 파일의 글이 곧 메모다.** frontmatter 가 없다:

    [ ] 오태양 책임에게 전류 스파이크 로그 요청     첫 줄 = 제목
    기한: 2026-11-09                               `[ ]` 할 일 · `[x]` 끝남 · 없으면 참고
                                                   `기한:` 줄은 없어도 된다
    - 11-05 구두로 요청. 금요일까지 준다고 함.     본문은 길어져도 된다

종류·기한을 입력란으로 빼지 않고 글 안에 둔 이유: 편집 상자에 보인 것 = 파일 =
색인이라는 등식(notes 와 같은 계약)을 지키고, 외부 편집기에서도 똑같이 고칠 수
있게 하려는 것이다. 고친 날은 파일 mtime, 만든 날은 번호가 말한다.

**파일이 원본**이다. store.memos 는 목록·검색·AI 문맥용 미러라 지워도 reindex 가
복구한다. 웹은 외부 편집기와 같은 자격의 편집자 하나일 뿐이라, 저장 전에
base_mtime 으로 '내가 열어 본 그 파일이 맞는지' 확인하고 다르면 손대지 않는다.

**번호는 파일 이름 머리에 있다** — `26111099001-제목.md`. 메일과 같은 11자리
`#번호` 표기를 쓰되 메일이 안 쓰는 99번 칸에서 낸다(store.MEMO_SLOT): 번호만 보고
메모인지 갈리고 메일·스레드 번호와 겹치지 않는다. 번호가 DB 가 아니라 파일 이름에
있어서 DB 를 새로 만들어도 그대로다. 이름의 제목 부분은 탐색기에서 알아보라고
붙인 것이라 신원이 아니다 — 제목을 고쳐도 이름은 바꾸지 않는다(외부 편집기가
열어 둔 파일의 이름을 바꾸면 저장할 때 옛 이름 파일이 다시 생겨 둘로 갈린다).
손으로 만든 번호 없는 파일도 목록에 나오고 고칠 수 있다. 번호는 사람이
[번호 매기기]를 눌렀을 때만 붙인다(같은 이유 — 코드가 말없이 이름을 바꾸지 않는다).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .notes import _slug
from .store import SLOT_SPAN, Store, is_memo_id, memo_base, memo_day

_DIR = "memo"
# 색인이 파일에서 읽는 것이 늘면 올린다 — reindex 가 mtime 이 같아도 한 번 다시 읽는다.
PARSE_VER = 1
# 그날 마지막으로 낸 번호. 그날 맨 뒤 메모를 지우고 곧바로 새로 쓰면 파일만 봐서는
# 같은 번호가 다시 나온다 — 지운 메모를 가리키던 참조가 새 메모로 풀린다(오류가
# 아니라 오답). DB 를 새로 만든 당일에만 이 보호가 빠진다.
_LAST_KEY = "memo_last_no"

# 첫 줄 머리의 체크 — `- [ ]`(마크다운 할 일 목록 꼴)도 받는다.
_HEAD_RX = re.compile(r"^(\s*(?:[-*]\s+)?\[)( |x|X)(\])\s*(.*)$")
# 기한은 **그 줄이 기한뿐일 때만** 읽는다. 본문 문장 속의 "기한: …"(남의 말을
# 옮겨 적은 것)까지 잡으면 내 할 일의 기한이 조용히 바뀐다.
_DUE_RX = re.compile(
    r"(?m)^[ \t]*기한[ \t]*[:：][ \t]*(\d{4})-(\d{1,2})-(\d{1,2})[ \t]*$")
_NO_RX = re.compile(r"^(\d+)(?:-|$)")

KINDS = {"todo": "할 일", "ref": "참고", "done": "끝남"}


def memo_dir(cfg: Config) -> Path:
    return cfg.vault / _DIR


def norm(text: str) -> str:
    """저장할 글 — 줄 끝을 맞추고 앞의 빈 줄을 뗀다(첫 줄이 제목이라야 한다)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(ln.rstrip() for ln in t.split("\n")).strip("\n")
    return t + "\n" if t.strip() else ""


def parse(text: str) -> dict:
    """글 → {"kind": todo|done|ref, "title": …, "due": YYYY-MM-DD|""}."""
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    m = _HEAD_RX.match(first)
    if m:
        kind = "done" if m.group(2) in "xX" else "todo"
        title = m.group(4).strip()
    else:
        kind, title = "ref", first.strip().lstrip("#").strip()
    due = ""
    dm = _DUE_RX.search(text or "")
    if dm:
        try:
            due = date(int(dm.group(1)), int(dm.group(2)),
                       int(dm.group(3))).isoformat()
        except ValueError:
            due = ""                       # 달력에 없는 날은 기한이 아니다
    return {"kind": kind, "title": title or "(제목 없음)", "due": due}


def snippet(text: str) -> str:
    """목록 둘째 줄 — 제목과 기한 줄 다음의 첫 글 줄."""
    seen_title = False
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        if not seen_title:
            seen_title = True
            continue
        if _DUE_RX.match(ln):
            continue
        return ln.strip().lstrip("-*").strip()
    return ""


def toggle_text(text: str) -> tuple[str, str]:
    """첫 줄의 `[ ]` ↔ `[x]` 를 뒤집는다 → (새 글, 새 종류).

    참고 메모(체크 없음)는 그대로 돌려준다 — 끝낼 것이 없다."""
    lines = (text or "").split("\n")
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        m = _HEAD_RX.match(ln)
        if not m:
            return text, "ref"
        mark = "x" if m.group(2) == " " else " "
        lines[i] = m.group(1) + mark + ln[m.end(2):]
        return "\n".join(lines), ("done" if mark == "x" else "todo")
    return text, "ref"


def compose(body: str, todo: bool = False, due: str = "") -> str:
    """새 메모 칸의 입력(글 + 할 일 체크 + 기한) → 파일에 쓸 글.

    체크와 기한을 글 안으로 옮긴다 — 저장된 뒤에는 글이 전부다. 사용자가 이미
    `[ ]` 나 `기한:` 줄을 직접 썼으면 겹쳐 붙이지 않는다."""
    text = norm(body)
    if not text:
        return ""
    lines = text.rstrip("\n").split("\n")
    if todo and not _HEAD_RX.match(lines[0]):
        lines[0] = "[ ] " + lines[0].lstrip()
    if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", (due or "").strip())
            and not _DUE_RX.search(text)):
        lines.insert(1, f"기한: {due.strip()}")
        if len(lines) > 2 and lines[2].strip():
            lines.insert(2, "")            # 기한 줄과 본문 사이 한 줄
    return "\n".join(lines) + "\n"


def no_of(name: str) -> int:
    """파일 이름 머리의 메모 번호 — 없거나 메모 번호 꼴이 아니면 0."""
    m = _NO_RX.match(Path(name).stem)
    return int(m.group(1)) if m and is_memo_id(m.group(1)) else 0


def _name_key(name: str) -> tuple:
    """같은 번호의 파일이 여럿일 때 번호의 임자를 고르는 순서 — 이름이 짧은 쪽.

    파일을 복사하면 사본의 이름이 길어진다(`… - 복사본.md`·`…(1).md`). 이름순만
    보면 `-사본.md` 가 `.md` 보다 앞서 사본이 번호를 가져간다."""
    return (len(name), name)


def owner_no(cfg: Config, path: Path) -> int:
    """이 파일이 가질 번호. 같은 번호의 파일이 둘이면(파일을 복사했을 때) 한
    파일만 번호를 갖고(_name_key) 나머지는 '번호 없음'이 된다 — 한 번호가 두
    메모를 가리키면 참조가 어느 쪽인지 알 수 없다."""
    no = no_of(path.name)
    if not no:
        return 0
    same = sorted((p.name for p in memo_dir(cfg).glob(f"{no}*.md")
                   if p.is_file() and no_of(p.name) == no), key=_name_key)
    return no if (not same or same[0] == path.name) else 0


def path_of(cfg: Config, name: str) -> Path | None:
    """파일 이름 → vault/memo 안의 실재 파일. 경로가 섞였으면 None.

    웹이 넘겨받는 값이라 폴더를 벗어나는 이름을 여기서 막는다."""
    name = (name or "").strip()
    if (not name or "/" in name or "\\" in name or name.startswith(".")
            or not name.endswith(".md")):
        return None
    p = memo_dir(cfg) / name
    return p if p.is_file() else None


def find(cfg: Config, no: int) -> Path | None:
    """번호 → 파일. 파일시스템을 본다(색인이 낡았어도 방금 만든 것을 찾는다)."""
    if not is_memo_id(no):
        return None
    d = memo_dir(cfg)
    if not d.is_dir():
        return None
    hits = sorted((p for p in d.glob(f"{int(no)}*.md")
                   if p.is_file() and no_of(p.name) == int(no)),
                  key=lambda p: _name_key(p.name))
    return hits[0] if hits else None


def read(path: Path) -> str:
    """메모 파일 읽기 — 메모장이 붙이는 BOM 을 뗀다(첫 줄의 `[ ]` 가 안 읽힌다)."""
    return path.read_text(encoding="utf-8-sig")


def _index(cfg: Config, store: Store, path: Path, text: str,
           mtime: float | None = None) -> None:
    meta = parse(text)
    store.index_memo(str(path), owner_no(cfg, path), meta["kind"],
                     meta["title"], meta["due"],
                     path.stat().st_mtime if mtime is None else mtime,
                     text, PARSE_VER)


def next_no(cfg: Config, store: Store, today: str | None = None) -> int:
    """오늘 낼 다음 메모 번호 — 날짜 + 99번 칸 + 그날 순번. 다 썼으면 0.

    순번은 '그날 파일 중 가장 큰 번호 + 1'이다. 번호가 파일 이름에 있으므로 DB 를
    새로 만들어도 이어진다. 지운 번호의 재사용은 _LAST_KEY 가 막는다."""
    base = memo_base(today or date.today().isoformat())
    if not base:
        return 0
    top = base
    d = memo_dir(cfg)
    if d.is_dir():
        for p in d.glob("*.md"):
            n = no_of(p.name)
            if base < n < base + SLOT_SPAN:
                top = max(top, n)
    try:
        last = int(store.get_state(_LAST_KEY) or 0)
    except ValueError:
        last = 0
    if base < last < base + SLOT_SPAN:
        top = max(top, last)
    return top + 1 if top + 1 < base + SLOT_SPAN else 0


def create(cfg: Config, store: Store, body: str, todo: bool = False,
           due: str = "", today: str | None = None) -> tuple[str, Path | None]:
    """새 메모 파일 → (상태, 경로). created · empty · full

    full 은 그날 번호(999건)를 다 쓴 것이다 — 조용히 다른 번호를 내지 않는다."""
    text = compose(body, todo, due)
    if not text:
        return "empty", None
    no = next_no(cfg, store, today)
    if not no:
        return "full", None
    d = memo_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{no}-{_slug(parse(text)['title'])}.md"
    path.write_text(text, encoding="utf-8")
    store.set_state(_LAST_KEY, str(no))
    _index(cfg, store, path, text)
    return "created", path


def save(cfg: Config, store: Store, name: str, body: str,
         base_mtime: float | None = None,
         toggle: bool = False) -> tuple[str, Path | None]:
    """메모 글을 파일에 반영 → (상태, 경로). saved · same · conflict · empty · gone

    notes.save_thread_note 와 같은 계약: base_mtime 이 지금 파일과 다르면 **손대지
    않는다**(자동 저장을 두지 않은 것과 같은 이유 — 사람이 쓴 글을 코드가 말없이
    지우지 않는다). 빈 글은 저장하지 않는다 — 노트는 '비우고 저장 = 삭제'지만
    메모에는 [삭제]가 따로 있어, 실수로 비운 저장이 파일을 지우게 두지 않는다.

    toggle 이면 저장하면서 첫 줄의 체크를 뒤집는다(편집기의 [끝냄]·[다시 열기])."""
    path = path_of(cfg, name)
    if path is None:
        return "gone", None
    try:
        cur = path.stat().st_mtime
    except OSError:
        return "gone", None
    if base_mtime is not None and abs(cur - base_mtime) > 1e-6:
        return "conflict", path
    text = norm(body)
    if not text:
        return "empty", path
    if toggle:
        text = norm(toggle_text(text)[0])
    if read(path) == text:
        return "same", path                # 안 바뀌었으면 mtime 도 안 건드린다
    path.write_text(text, encoding="utf-8")
    # 색인은 직접 쓴다 — reindex 는 mtime 이 같으면 건너뛰는데, 연달아 저장하면
    # 파일시스템 시각 해상도 안에서 mtime 이 같을 수 있다(notes 와 같은 판례).
    _index(cfg, store, path, text)
    return "saved", path


def toggle(cfg: Config, store: Store, name: str) -> tuple[str, Path | None]:
    """목록의 체크·회고의 [끝냄] — 파일 첫 줄의 `[ ]` ↔ `[x]` 만 바꾼다.

    → (상태, 경로). done · todo · ref(끝낼 것이 없는 참고 메모) · gone"""
    path = path_of(cfg, name)
    if path is None:
        return "gone", None
    try:
        old = read(path)
    except (OSError, UnicodeDecodeError):
        return "gone", None
    new, kind = toggle_text(old)
    if kind == "ref" or new == old:
        return "ref", path
    path.write_text(new, encoding="utf-8")
    _index(cfg, store, path, new)
    return kind, path


def delete(cfg: Config, store: Store, name: str) -> tuple[str, Path | None]:
    """메모 파일을 지운다 → (deleted|gone, 경로). 되돌리기는 없다(화면이 한 번 묻는다)."""
    path = path_of(cfg, name)
    if path is None:
        return "gone", None
    try:
        path.unlink()
    except OSError:
        return "gone", None
    reindex(cfg, store)                    # prune_memos 가 색인을 걷어낸다
    return "deleted", path


def assign_no(cfg: Config, store: Store, name: str,
              today: str | None = None) -> tuple[str, Path | None]:
    """번호 없는 파일에 번호를 붙인다(이름 앞에) → (상태, 새 경로).

    numbered · has(이미 번호가 있다) · full · gone

    번호의 날짜는 **번호를 낸 날**이다. 손으로 만든 파일은 만든 날을 믿을 만하게
    알 길이 없다(mtime 은 고친 날이고, 복사하면 생성 시각도 바뀐다)."""
    path = path_of(cfg, name)
    if path is None:
        return "gone", None
    if owner_no(cfg, path):
        return "has", path
    no = next_no(cfg, store, today)
    if not no:
        return "full", path
    # 겹친 번호로 '번호 없음'이 된 파일은 이름에 옛 번호가 붙어 있다 — 그것을 떼고 붙인다.
    stem = path.stem
    old = no_of(path.name)
    if old:
        stem = stem[len(str(old)):].lstrip("-")
    new = path.with_name(f"{no}-{stem or 'untitled'}.md")
    try:
        path.rename(new)
    except OSError:
        return "gone", path
    store.set_state(_LAST_KEY, str(no))
    reindex(cfg, store)
    return "numbered", new


def reindex(cfg: Config, store: Store) -> int:
    """vault/memo/*.md ↔ memos 색인 동기화(mtime 증분). 반환 = 변경 건수.

    파일이 원본이므로 방향은 파일 → DB 한쪽뿐이다. **바뀐 것이 없으면 한 줄도
    쓰지 않는다** — 목록·첫 화면·회고가 그릴 때마다 부르는 읽기 경로다(CLAUDE.md 9).
    지우기를 먼저 한다: 사라진 파일의 번호를 새 파일이 이어받을 수 있다."""
    d = memo_dir(cfg)
    files = sorted(p for p in d.glob("*.md") if p.is_file()) if d.is_dir() else []
    changed = store.prune_memos({str(p) for p in files})
    have = {r["path"]: r for r in store.memo_rows()}
    owner: dict[int, str] = {}             # 번호 → 그 번호의 임자(owner_no 와 같은 규칙)
    for p in sorted(files, key=lambda p: _name_key(p.name)):
        n = no_of(p.name)
        if n and n not in owner:
            owner[n] = p.name
    for p in files:
        n = no_of(p.name)
        no = n if n and owner.get(n) == p.name else 0
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        row = have.get(str(p))
        if (row is not None and abs(row["mtime"] - mtime) < 1e-6
                and (row["parse_ver"] or 0) == PARSE_VER and row["no"] == no):
            continue
        try:
            text = read(p)
        except (OSError, UnicodeDecodeError):
            continue                       # 잠긴/깨진 파일은 이번 회차만 건너뛴다
        meta = parse(text)
        store.index_memo(str(p), no, meta["kind"], meta["title"], meta["due"],
                         mtime, text, PARSE_VER)
        changed += 1
    return changed


# ------------------------------------------------------------------ 목록

GROUPS = ("기한 지남", "이번 주", "그 뒤", "기한 없음")


def due_group(due: str, today: str) -> str:
    """할 일을 기한으로 묶는 이름. '이번 주'는 이번 일요일까지다."""
    if not due:
        return GROUPS[3]
    if due < today:
        return GROUPS[0]
    try:
        t = date.fromisoformat(today)
    except ValueError:
        return GROUPS[2]
    sunday = t + timedelta(days=6 - t.weekday())
    return GROUPS[1] if due <= sunday.isoformat() else GROUPS[2]


def days_between(a: str, b: str) -> int:
    """b - a (일). 날짜가 아니면 0."""
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except ValueError:
        return 0


def todos(rows, today: str) -> list:
    """열린 할 일 — 기한 있는 것을 이른 순으로(지난 것이 맨 앞), 기한 없는 것은
    최근에 고친 순으로 뒤에."""
    todo = [r for r in rows if r["kind"] == "todo"]
    with_due = sorted((r for r in todo if r["due"]),
                      key=lambda r: (r["due"], -r["mtime"]))
    no_due = sorted((r for r in todo if not r["due"]),
                    key=lambda r: -r["mtime"])
    return with_due + no_due


def counts(rows, today: str) -> dict:
    """탭·타일에 쓰는 건수 — {"todo", "ref", "done", "late"}."""
    out = {"todo": 0, "ref": 0, "done": 0, "late": 0}
    for r in rows:
        out[r["kind"] if r["kind"] in out else "ref"] += 1
        if r["kind"] == "todo" and r["due"] and r["due"] < today:
            out["late"] += 1
    return out


def made_day(row) -> str:
    """만든 날 — 번호가 말한다. 번호 없는 파일은 ''."""
    return memo_day(row["no"]) if row["no"] else ""
