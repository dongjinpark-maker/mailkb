"""주제 위키의 손발 — `/mail-wiki` 세션이 읽고 쓰는 동안 **찾기·세기·대조**를 맡는다.

왜 필요한가: 사용자가 등록한 주제(과제·사안)에 대해서만 메일을 재료로 페이지를
편찬·갱신하는 것이 `/mail-wiki` 다(계약: agent-guides/minerva-wiki.md). 처음엔
세션이 search/show/thread 로 전부 했는데, 데모 실측(2026-11-10)에서 새 메일 18통을
반영하는 데 명령 약 120회·17만 토큰이 들었다 — 대부분이 '주제에 속한 메일 찾기'와
'쓴 인용 다시 대조하기'였다. 둘 다 판단이 아니라 계산이라 여기로 옮겼다. 같은
실험이 명령 4회·9만 토큰으로 줄었고 페이지의 결정·경위는 같았다.

  scan    주제에 속한 메일을 찾아 확정/후보로 가르고, 반영 안 된 새 메일을 묶음으로 준다
  check   쓴 페이지를 원문과 대조한다(인용·날짜·근거·이전 줄 보존)
  status  주제마다 얼마나 쌓였는지 한 줄씩
  report  회사 내용 없는 수치 보고 — 밖에서 이 기능을 고치는 사람에게 보낼 재료
  leak    밖으로 보낼 글에 회사 내용이 섞였는지 대조

전부 AI 호출 0, DB 는 읽기만 한다. 파일을 쓰는 것은 `<vault>/wiki/.history/` 뿐이다
(check 가 통과한 판의 사본, 실행 기록 한 줄 — 둘 다 메일 내용이 아니다).

불변식과의 관계(CLAUDE.md):
  · 숨긴 스레드는 재료에서 뺀다 — scan 이 `store.hidden_thread_ids()` 로 거른다(3).
  · 인용 검증은 코드가 한다 — check 가 그 자리다(7). 보존 인용(mid-join)은 근거에서
    뺀다: 남이 주고받은 글이라 그대로 대조하면 A 의 문장을 B 의 말로 올리게 된다
    (distill._QuoteChecker 와 같은 이유).
  · id 는 카운터가 아니다 — '새 메일'은 발신 시각(`through`)으로 가른다(8).
  · 읽기 경로에서 DB 에 쓰지 않는다(9).
"""

from __future__ import annotations

import collections
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from .clean import strip_preserved, take_preserved
from .config import Config
from .store import Store

WIKI_VER = 1               # report 머리 줄에 싣는 도구 판 — 형식·판정 규칙을 바꾸면 올린다
MARKS = ("결정", "변경", "사실", "논의")
TAILS = ("결정", "변경", "해소")
DROP_TAIL = " → 제외됨(주제 밖)"
# 묶음 크기 — 가끔 돌리는 도구라 한 번에 수십~백 통이 쌓인다. 실험에서 한 번에 읽힌
# 최대가 18통이었고 그 위는 재 보지 않았다. 넉넉히 두 배로 잡고 나머지는 다음 묶음으로.
SCAN_LIMIT = 40
BODY_CAP = 6000            # 한 메일 본문 상한(자) — 넘으면 자르고 그 사실을 알린다
# 인용 길이(공백 제외). 하한이 distill(10)보다 낮은 까닭: 거기는 스레드 전체에서 찾지만
# 여기는 **지목된 메일 한 통**에서 찾으므로 짧아도 닻이 된다. 10 으로 두니 "리뷰
# 부탁드립니다." 같은 멀쩡한 한 문장이 걸렸다(2026-11-10 실험 페이지).
QUOTE_MIN, QUOTE_MAX = 6, 300
LEAK_WINDOW = 12           # 원문과 이만큼 이어서 겹치면 반출 의심
# 뒤집힘 신호 — 후보 스레드에 이 말이 새로 나왔으면 이유에 함께 싣는다(읽을지는 사용자 몫)
SIGNALS = ("변경", "취소", "보류", "연기", "확정", "최종", "철회")
RATINGS = ("쓸만함", "보통", "아님")
_FILES_NOT_PAGES = ("index.md", "log.md", "notes.md")
_TITLES = ("책임", "수석", "선임", "주임", "팀장", "사원", "대리", "과장", "차장", "부장",
           "님", "프로", "연구원", "매니저", "파트장", "그룹장")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def slug(topic: str) -> str:
    """주제 이름 → 파일 이름. 소문자, 공백은 '-', 경로 문자는 뺀다(한글은 그대로)."""
    s = re.sub(r"\s+", "-", (topic or "").strip().lower())
    return re.sub(r'[\\/:*?"<>|]', "", s) or "주제"


def wiki_dir(cfg: Config) -> Path:
    return cfg.vault / "wiki"


def page_paths(cfg: Config) -> list[Path]:
    w = wiki_dir(cfg)
    if not w.is_dir():
        return []
    return sorted(p for p in w.glob("*.md") if p.name not in _FILES_NOT_PAGES)


def resolve_page(cfg: Config, ref: str) -> Path:
    """주제 이름·파일 이름·경로 어느 것으로 불러도 페이지 파일을 찾는다."""
    p = Path(ref)
    if p.suffix == ".md" and p.exists():
        return p
    w = wiki_dir(cfg)
    for cand in (w / ref, w / f"{ref}.md", w / f"{slug(ref)}.md"):
        if cand.suffix == ".md" and cand.exists():
            return cand
    for path in page_paths(cfg):                       # topic: 머리말로도 찾는다
        if parse_page(path).meta.get("topic", "").strip().lower() == ref.strip().lower():
            return path
    raise SystemExit(f"주제 페이지 없음: {ref}  ({w} 에서 찾았다)")


class Page:
    """주제 페이지 한 장 — 머리말(등록 정보)과 절."""

    def __init__(self, path: Path):
        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
        front, body = (m.group(1), m.group(2)) if m else ("", text)
        self.meta: dict[str, str] = {}
        for line in front.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                self.meta[k.strip()] = v.strip()
        self.secs: dict[str, list[str]] = collections.OrderedDict()
        cur = None
        for line in body.splitlines():
            h = re.match(r"##\s+(.*)", line)
            if h:
                cur = h.group(1).strip()
                self.secs[cur] = []
            elif cur is not None:
                self.secs[cur].append(line)

    def list(self, key: str) -> list[str]:
        raw = self.meta.get(key, "").strip()
        return [x.strip() for x in raw.strip("[]").split(",") if x.strip()]

    def ids(self, key: str) -> list[int]:
        return [int(x) for x in self.list(key) if x.isdigit()]

    def bullets(self, sec: str) -> list[str]:
        return [l for l in self.secs.get(sec, []) if l.startswith("- ")]

    @property
    def topic(self) -> str:
        return self.meta.get("topic", self.path.stem)

    @property
    def names(self) -> list[str]:
        """주제 이름 + 별칭 — 메일을 찾는 열쇠."""
        return [x for x in [self.topic] + self.list("aliases") if x]

    @property
    def through(self) -> str:
        return self.meta.get("through", "")[:16]


def parse_page(path: Path) -> Page:
    return Page(path)


# ── 경위 줄 ────────────────────────────────────────────────────────────────
# 인용은 끝에서부터 잡는다(탐욕) — 원문에 따옴표나 '→'(예: "1% → 10%")가 들어 있어도
# 꼬리로 읽히지 않게. 꼬리는 닫는 따옴표 **뒤에** 정해진 꼴로만 온다.
_TAIL = r"(?: → \d\d-\d\d (?:%s) \[메일 #\d+\]|%s)" % ("|".join(TAILS), re.escape(DROP_TAIL))
LINE_RX = re.compile(
    r'^- (\d\d-\d\d) \*\*(%s)\*\* (.+?) — (.+?) \[메일 #(\d+)\] "(.+)"(%s*)$'
    % ("|".join(MARKS), _TAIL))
_TAIL_RX = re.compile(_TAIL)


def _line_parts(line: str):
    m = LINE_RX.match(line)
    return m.groups() if m else None


def _tails(line: str) -> list[str]:
    parts = _line_parts(line)
    return _TAIL_RX.findall(parts[6]) if parts else []


# ── 메일 읽기 ──────────────────────────────────────────────────────────────

class _Mail:
    """DB 조회 한 곳 — 재료에서 뺄 것(자동 알림·숨김·사라진 메일)을 여기서 거른다."""

    _COLS = ("id, thread_id, subject, sender_name, sender_addr, to_addrs, cc_addrs, "
             "sent_on, new_content, attach_names")

    def __init__(self, store: Store, cfg: Config):
        self.store, self.cfg = store, cfg
        self.hidden = store.hidden_thread_ids()
        self._threads: dict[int, list] = {}

    def human(self, m) -> bool:
        return not self.cfg.is_noise(m["sender_addr"] or "")

    def all_rows(self, tid: int) -> list:
        if tid not in self._threads:
            self._threads[tid] = self.store.db.execute(
                f"SELECT {self._COLS} FROM messages WHERE thread_id=? AND gone_at IS NULL "
                "ORDER BY sent_on, id", (tid,)).fetchall()
        return self._threads[tid]

    def msgs(self, tid: int) -> list:
        """사람이 쓴 메일만 — 페이지의 재료."""
        return [m for m in self.all_rows(tid) if self.human(m)]

    def title(self, tid: int) -> str:
        rows = self.all_rows(tid)
        subj = rows[0]["subject"] if rows else ""
        return re.sub(r"^((RE|FW|FWD|답장|전달)\s*:\s*)+", "", subj or "", flags=re.I).strip()

    def like(self, needle: str) -> list:
        """제목이나 본문에 needle 이 든 메일. LIKE 는 ASCII 만 대소문자를 무시한다 —
        한글은 대소문자가 없으니 그걸로 충분하다."""
        pat = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return self.store.db.execute(
            "SELECT id, thread_id, subject, sender_addr, new_content FROM messages "
            "WHERE gone_at IS NULL AND (subject LIKE ? ESCAPE '\\' "
            "OR new_content LIKE ? ESCAPE '\\')", (pat, pat)).fetchall()

    def human_thread_count(self) -> int:
        seen = set()
        for r in self.store.db.execute(
                "SELECT DISTINCT thread_id, sender_addr FROM messages WHERE gone_at IS NULL"):
            if not self.cfg.is_noise(r["sender_addr"] or ""):
                seen.add(r["thread_id"])
        return len(seen - self.hidden)

    def out(self, m) -> dict:
        """세션에 넘기는 메일 한 통. 보존 인용은 이해용으로만 따로 싣는다."""
        own = strip_preserved(m["new_content"] or "").strip()
        d = {"id": m["id"], "date": m["sent_on"][:16],
             "sender": m["sender_name"] or m["sender_addr"]}
        if len(own) > BODY_CAP:
            d["body"] = own[:BODY_CAP]
            d["body_truncated"] = f"본문 {len(own)}자 중 앞 {BODY_CAP}자만 — 인용은 보이는 부분에서만"
        else:
            d["body"] = own
        if m["attach_names"]:
            d["attachments_unread"] = m["attach_names"]
        pre = take_preserved(m["new_content"] or "")
        if pre:
            d["earlier_context_not_citable"] = pre[:BODY_CAP]
        return d

    def row(self, tid: int) -> dict:
        ms = self.msgs(tid)
        return {"thread": tid, "subject": self.title(tid), "messages": len(ms),
                "last": ms[-1]["sent_on"][:16] if ms else ""}


def _variants(names: list[str]) -> list[str]:
    """붙여 쓴 표기 — 'ALPHA-1' 을 등록했는데 CI 는 'alpha1' 이라 쓰는 식의 차이."""
    have = {n.lower() for n in names}
    out = []
    for n in names:
        for v in (n.replace("-", ""), n.replace(" ", ""), n.replace("-", " ")):
            if len(v) >= 3 and v.lower() not in have:
                have.add(v.lower())
                out.append(v)
    return out


def _alias_scan(mail: _Mail, names: list[str], skip=frozenset()) -> tuple[list[dict], int]:
    """별칭이 걸린 사람 스레드를 확정/후보로 가른다. 반환 (행들, 자동 알림 건수).

    이름 검색만으로는 양쪽으로 틀린다(데모 실측: 이름이 걸린 62통 중 37통이 자동
    알림). 그래서 자동 알림은 세기만 하고, 사람 스레드는 '얼마나 이 주제의 것인가'로
    가른다 — 제목에 있으면 확정, 본문 절반 이상이면 확정, 스치기만 하면 후보.
    """
    low = [n.lower() for n in names if n]
    per: dict[int, dict] = {}
    noise = 0
    seen = set()
    for n in low:
        for r in mail.like(n):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            if not mail.human(r):
                noise += 1
                continue
            t = per.setdefault(r["thread_id"], {"own": 0, "pre": 0})
            own = strip_preserved(r["new_content"] or "").lower()
            if any(a in own for a in low):
                t["own"] += 1
            elif any(a in take_preserved(r["new_content"] or "").lower() for a in low):
                t["pre"] += 1
    rows = []
    for tid, hit in per.items():
        if tid in mail.hidden or tid in skip:
            continue
        n = len(mail.msgs(tid))
        if not n:
            continue
        in_title = any(a in mail.title(tid).lower() for a in low)
        if in_title:
            verdict, why = "확정", "제목에 별칭"
        elif n >= 2 and hit["own"] * 2 >= n:
            verdict, why = "확정", f"별칭이 {n}통 중 {hit['own']}통"
        elif hit["own"]:
            verdict, why = "후보", f"별칭이 {n}통 중 {hit['own']}통(제목에는 없음)"
        elif hit["pre"]:
            verdict, why = "후보", "별칭이 보존된 이전 대화 안에만 있음"
        else:
            continue                       # 답장 제목에만 걸린 경우는 in_title 이 이미 잡았다
        rows.append({**mail.row(tid), "verdict": verdict, "why": why})
    return sorted(rows, key=lambda r: r["last"]), noise


def _term_hits(mail: _Mail, terms: list[str]) -> dict[str, set[int]]:
    hits: dict[str, set[int]] = {}
    for t in terms:
        hits[t] = {r["thread_id"] for r in mail.like(t)
                   if mail.human(r) and r["thread_id"] not in mail.hidden}
    return hits


def _term_scan(mail: _Mail, terms: list[str], skip: set[int]) -> tuple[list[dict], list[str], dict]:
    """고유 낱말 겹침으로 이름 없는 관련 스레드를 후보로 올린다.

    2개 이상 겹치거나, 1개라도 제목에 있으면 후보. 바깥 스레드 여러 곳에 걸리는
    낱말은 고유하지 않은 것이라 버린다(안 버리면 후보가 쏟아진다).
    반환 (후보 행, 버린 낱말, 낱말별 바깥 스레드 수).
    """
    if not terms:
        return [], [], {}
    hits = _term_hits(mail, terms)
    limit = max(8, mail.human_thread_count() // 8)
    outside = {t: len(hits[t] - skip) for t in terms}
    common = [t for t in terms if outside[t] > limit]
    use = [t for t in terms if t not in common]
    per: dict[int, list[str]] = collections.defaultdict(list)
    for t in use:
        for tid in hits[t] - skip:
            per[tid].append(t)
    rows = []
    for tid, got in per.items():
        title = mail.title(tid).lower()
        in_title = [t for t in got if t.lower() in title]
        if len(got) >= 2 or in_title:
            why = f"고유 낱말 {len(got)}개 겹침: {', '.join(got)}"
            if in_title:
                why += f" (제목에: {', '.join(in_title)})"
            rows.append({**mail.row(tid), "why": why})
    return sorted(rows, key=lambda r: r["last"]), common, outside


def _signals(mail: _Mail, tid: int, after: str) -> list[str]:
    body = " ".join(strip_preserved(m["new_content"] or "") for m in mail.msgs(tid)
                    if m["sent_on"][:16] > after)
    return [s for s in SIGNALS if s in body]


def _cut(dates: list[str], limit: int) -> tuple[str, int]:
    """오래된 것부터 limit 통에서 끊는다. 같은 분(分)에 온 메일은 한 묶음에 넣는다 —
    through 가 분 단위라, 갈라 놓으면 뒤쪽이 다음 scan 에서 '이미 반영됨'으로 빠진다."""
    dates = sorted(dates)
    if len(dates) <= limit:
        return (dates[-1] if dates else ""), 0
    edge = dates[limit - 1]
    kept = sum(1 for d in dates if d <= edge)
    return edge, len(dates) - kept


def _deadline_passed(line: str, today: date) -> bool:
    """`- 기한: MM-DD …` 줄의 날짜가 지났는데 '지남' 이 없는가."""
    m = re.match(r"- 기한:\s*(\d\d)-(\d\d)\b", line)
    if not m or "지남" in line:
        return False
    try:
        d = date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return False
    if d - today > timedelta(days=183):        # 1월에 보는 12월 기한은 작년 것
        d = d.replace(year=today.year - 1)
    return d < today


def _log_op(cfg: Config, rec: dict) -> None:
    """실행 기록 한 줄 — 시각·명령·건수뿐, 메일 내용은 남기지 않는다. report 가 작업
    소요 시간과 검증 통과율을 여기서 센다(세션은 자기 토큰·시간을 스스로 알지 못한다)."""
    try:
        h = wiki_dir(cfg) / ".history"
        h.mkdir(parents=True, exist_ok=True)
        with open(h / "ops.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **rec},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass            # 기록은 부가 기능이다 — 못 써도 scan·check 결과는 그대로 낸다


# ── scan ──────────────────────────────────────────────────────────────────

def scan_topic(store: Store, cfg: Config, topic: str, aliases: list[str],
               limit: int = SCAN_LIMIT, today: date | None = None) -> dict:
    """새 주제의 재료 — 확정 스레드의 메일(오래된 것부터 limit 통)과 후보."""
    today = today or date.today()
    path = wiki_dir(cfg) / f"{slug(topic)}.md"
    if path.exists():
        return {"error": f"이미 있는 주제다: {path.name} — update 를 쓴다"}
    mail = _Mail(store, cfg)
    names = [topic] + [a for a in aliases if a]
    found = [v for v in _variants(names) if mail.like(v)]
    rows, noise = _alias_scan(mail, names + found)
    conf = [r for r in rows if r["verdict"] == "확정"]
    edge, remaining = _cut([m["sent_on"][:16] for r in conf for m in mail.msgs(r["thread"])], limit)
    delivered = []
    n = 0
    for r in conf:
        ms = [mail.out(m) for m in mail.msgs(r["thread"]) if m["sent_on"][:16] <= edge]
        if ms:
            delivered.append({**r, "messages_all": ms})
            n += len(ms)
    out = {
        "today": today.isoformat(), "topic": topic, "page_file": path.name,
        "aliases_given": names[1:], "alias_variants_found": found,
        "confirmed_threads": delivered,
        "candidates": [r for r in rows if r["verdict"] == "후보"],
        "noise_excluded": noise, "through": edge,
        "batch": {"delivered": n, "remaining": remaining, "limit": limit},
    }
    if remaining:
        out["batch"]["note"] = ("나머지는 페이지를 쓰고 check 를 통과시킨 뒤 "
                                "`scan --page` 로 이어서 받는다(update 와 같은 절차)")
    _log_op(cfg, {"cmd": "scan-topic", "page": path.stem, "delivered": n, "remaining": remaining})
    return out


def scan_page(store: Store, cfg: Config, path: Path, limit: int = SCAN_LIMIT,
              today: date | None = None) -> dict:
    """갱신 재료 — `through` 뒤에 온 메일, 새로 걸린 스레드, 후보, 지난 기한."""
    today = today or date.today()
    page = parse_page(path)
    mail = _Mail(store, cfg)
    threads, cands, excl = page.ids("threads"), page.ids("candidates"), page.ids("excluded")
    through = page.through
    hidden_now = [t for t in threads if t in mail.hidden]
    live = [t for t in threads if t not in mail.hidden]

    rows, noise = _alias_scan(mail, page.names, skip=set(threads) | set(excl))
    new_conf = [r for r in rows if r["verdict"] == "확정"]
    alias_c = [r for r in rows if r["verdict"] == "후보"]

    # 한 묶음의 끝 — 소속 스레드의 새 메일과 새 확정 스레드의 through 뒤 메일을 한 줄로 세운다
    pool = [m["sent_on"][:16] for t in live for m in mail.msgs(t) if m["sent_on"][:16] > through]
    pool += [m["sent_on"][:16] for r in new_conf for m in mail.msgs(r["thread"])
             if m["sent_on"][:16] > through]
    edge, remaining = _cut(pool, limit)
    edge = edge or through
    new_msgs = sorted(
        ({"thread": t, **mail.out(m)} for t in live for m in mail.msgs(t)
         if through < m["sent_on"][:16] <= edge), key=lambda m: m["date"])
    conf_out = []
    for r in new_conf:
        # 새로 걸린 스레드는 through 이전 메일도 처음 보는 것이라 함께 준다(끝은 같은 edge)
        ms = [mail.out(m) for m in mail.msgs(r["thread"]) if m["sent_on"][:16] <= edge]
        if ms:
            conf_out.append({**r, "messages_all": ms})

    skip = set(threads) | set(excl) | {r["thread"] for r in rows}
    term_c, common, outside = _term_scan(mail, page.list("terms"), skip)
    all_c = alias_c + term_c
    for r in all_c:
        r["already_candidate"] = r["thread"] in cands
        sg = _signals(mail, r["thread"], through)
        if sg:
            r["change_signals"] = sg
    gone = [mail.row(c) for c in cands
            if c not in {r["thread"] for r in all_c} and c not in excl
            and c not in {r["thread"] for r in new_conf}]
    overdue = [l for l in page.bullets("열린 것") if _deadline_passed(l, today)]
    delivered = len(new_msgs) + sum(len(r["messages_all"]) for r in conf_out)
    fresh_c = [r for r in all_c if not r["already_candidate"]]
    out = {
        "today": today.isoformat(), "topic": page.topic, "page_file": page.path.name,
        "through": through,
        "nothing_new": not (delivered or conf_out or fresh_c or overdue or hidden_now),
        "new_messages": new_msgs,
        "new_confirmed_threads": conf_out,
        "candidates": all_c,
        "candidates_no_longer_matching": gone,
        "deadlines_passed_unmarked": overdue,
        "terms_too_common_ignored": common,
        "terms_hits_outside": outside,
        "threads_table": [mail.row(t) for t in live],
        "threads_hidden_by_user": hidden_now,
        "noise_excluded": noise,
        "through_after_update": edge,
        "batch": {"delivered": delivered, "remaining": remaining, "limit": limit},
    }
    if remaining:
        out["batch"]["note"] = "이 묶음을 반영하고 check 를 통과시킨 뒤 scan 을 다시 돌려 다음 묶음을 받는다"
    _log_op(cfg, {"cmd": "scan-page", "page": page.path.stem, "delivered": delivered,
                  "remaining": remaining})
    return out


def scan_thread(store: Store, cfg: Config, path: Path, tid: int,
                today: date | None = None) -> dict:
    """한 스레드의 메일 — add(넣기)와 drop(빼기)이 쓴다."""
    today = today or date.today()
    page = parse_page(path)
    mail = _Mail(store, cfg)
    ms = mail.msgs(tid)
    where = ("threads" if tid in page.ids("threads") else
             "candidates" if tid in page.ids("candidates") else
             "excluded" if tid in page.ids("excluded") else "없음")
    _, noise = _alias_scan(mail, page.names)
    table = [mail.row(t) for t in page.ids("threads") if t not in mail.hidden]
    if tid not in page.ids("threads"):
        table.append(mail.row(tid))
    out = {
        "today": today.isoformat(), "topic": page.topic, "thread": mail.row(tid),
        "now_in_page": where, "hidden": tid in mail.hidden,
        "messages": [] if tid in mail.hidden else [mail.out(m) for m in ms],
        "threads_table_if_added": table, "noise_excluded": noise,
        "through": page.through,
    }
    if not ms:
        out["error"] = f"스레드 #{tid} 에 사람이 쓴 메일이 없다(없는 번호이거나 자동 알림뿐)"
    if tid in mail.hidden:
        out["error"] = f"스레드 #{tid} 는 사용자가 숨겼다 — 재료로 쓰지 않는다"
    _log_op(cfg, {"cmd": "scan-thread", "page": page.path.stem, "delivered": len(out["messages"]),
                  "remaining": 0})
    return out


# ── check ─────────────────────────────────────────────────────────────────

_CATEGORY = (
    ("인용", "인용 불일치"), ("현재 상태", "근거 표시"), ("열린 것", "근거 표시"),
    ("이전 경위", "이전 줄 변경"), ("경위 줄 형식", "형식"), ("머리말에", "형식"),
    ("절 없음", "형식"), ("through", "머리말·표 불일치"), ("표 ≠", "머리말·표 불일치"),
    ("발신일", "날짜 불일치"), ("없는 메일", "없는 번호"), ("꼬리의", "없는 번호"),
    ("스레드가 threads", "소속 밖 메일"), ("숨긴", "숨긴 스레드"),
)


def _category(err: str) -> str:
    for key, name in _CATEGORY:
        if key in err:
            return name
    return "기타"


def check(store: Store, cfg: Config, path: Path, today: date | None = None) -> dict:
    """쓴 페이지를 원문과 대조한다. 통과하면 그 판을 `.history/` 에 보관해 다음 check 가
    '이전 경위 줄이 그대로인가'를 볼 수 있게 한다.

    보증하는 것: 쓴 것이 원문에 있다 · 옛 기록을 건드리지 않았다 · 현재 상태가 결정·사실
    줄에 기대고 있다. 보증하지 **않는** 것: 요지가 인용과 맞는가, 표시가 옳은가, 빠뜨린
    메일이 없는가 — 그건 읽는 사람의 몫이라 계약 문서에 그렇게 적어 둔다.
    """
    today = today or date.today()
    page = parse_page(path)
    mail = _Mail(store, cfg)
    errs: list[str] = []
    warns: list[str] = []
    for k in ("topic", "aliases", "terms", "threads", "candidates", "excluded",
              "created", "updated", "through"):
        if k not in page.meta:
            errs.append(f"머리말에 {k} 없음")
    for s in ("현재 상태", "열린 것", "경위", "기술", "사람", "스레드"):
        if s not in page.secs:
            errs.append(f"절 없음: {s}")
    threads = set(page.ids("threads"))
    for t in threads & mail.hidden:
        errs.append(f"숨긴 스레드가 threads 에 있음: {t} — excluded 로 옮긴다")

    lines = page.bullets("경위")
    marks_of: dict[int, set] = collections.defaultdict(set)
    count = collections.Counter()
    for l in lines:
        parts = _line_parts(l)
        if not parts:
            errs.append(f"경위 줄 형식: {l[:90]}")
            continue
        day, mark, _gist, _who, mid, quote, tail = parts
        mid = int(mid)
        marks_of[mid].add(mark)
        count[mark] += 1
        row = store.db.execute(
            "SELECT thread_id, sent_on, new_content FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            errs.append(f"없는 메일 #{mid}: {l[:60]}")
            continue
        dropped = DROP_TAIL in tail
        if row["thread_id"] not in threads and not dropped:
            errs.append(f"메일 #{mid} 의 스레드가 threads 에 없음: {l[:60]}")
        if row["sent_on"][5:10] != day:
            errs.append(f"메일 #{mid} 발신일 {row['sent_on'][5:10]} ≠ 줄 머리 {day}")
        q = _norm(quote)
        bare = len(re.sub(r"\s", "", q))
        own = _norm(strip_preserved(row["new_content"] or ""))
        if not QUOTE_MIN <= bare <= QUOTE_MAX:
            errs.append(f"인용 길이 {bare}자(공백 제외 {QUOTE_MIN}~{QUOTE_MAX}자) #{mid}: {quote[:40]}")
        elif q not in own:
            where = ("보존된 이전 대화 안에만 있음(근거로 못 쓴다)"
                     if q in _norm(row["new_content"] or "") else "원문에 없음")
            errs.append(f"인용 {where} #{mid}: {quote[:50]}")
        for tmid in re.findall(r"\[메일 #(\d+)\]", tail):
            if not store.db.execute("SELECT 1 FROM messages WHERE id=?", (int(tmid),)).fetchone():
                errs.append(f"꼬리의 메일 #{tmid} 없음")

    for l in page.bullets("현재 상태"):
        if "아직 확정된 것이 없다" in l:
            continue
        kind = "결정" if l.startswith("- 결정:") else "사실" if l.startswith("- 사실:") else None
        if not kind:
            errs.append(f"현재 상태 줄 머리가 결정:/사실: 아님: {l[:60]}")
            continue
        ids = [int(x) for x in re.findall(r"\[메일 #(\d+)\]", l)]
        need = {"결정", "변경"} if kind == "결정" else {"사실", "결정", "변경"}
        if not ids:
            errs.append(f"현재 상태 줄에 근거 없음: {l[:60]}")
        elif not any(marks_of.get(i, set()) & need for i in ids):
            errs.append(f"현재 상태 '{kind}' 줄의 근거가 경위의 {'/'.join(sorted(need))} 줄이 아님: {l[:70]}")
    for l in page.bullets("열린 것"):
        if not re.search(r"\[메일 #\d+\]", l):
            errs.append(f"열린 것 줄에 근거 없음: {l[:60]}")
        if _deadline_passed(l, today):
            warns.append(f"기한이 지났는데 '지남' 표시 없음: {l[:60]}")

    def table(name: str) -> set[int]:
        return {int(x) for x in re.findall(r"^\|\s*(\d+)\s*\|", "\n".join(page.secs.get(name, [])), re.M)}
    if table("스레드") != threads:
        errs.append("스레드 표 ≠ 머리말 threads")
    if table("확인할 스레드") != set(page.ids("candidates")):
        errs.append("확인할 스레드 표 ≠ 머리말 candidates")
    latest = max([m["sent_on"][:16] for t in threads for m in mail.msgs(t)] + [""])
    if page.through > latest:
        errs.append(f"through {page.through} 가 소속 스레드의 마지막 메일 {latest} 보다 늦다")
    elif page.through < latest:
        # 묶음으로 나눠 반영하는 중이면 정상이다 — 오류가 아니라 알림
        n = sum(1 for t in threads for m in mail.msgs(t) if m["sent_on"][:16] > page.through)
        warns.append(f"through 뒤에 반영 안 된 메일 {n}통 — scan 으로 다음 묶음을 받는다")

    hist = page.path.parent / ".history" / page.path.name
    kept = None
    if hist.exists():
        old = parse_page(hist).bullets("경위")
        kept = 0
        # 옛 줄은 그대로이거나, 끝에 꼬리만 더 붙은 꼴이어야 한다
        more = re.compile(r"(?:%s)+" % _TAIL)
        for o in old:
            if any(n == o or (n.startswith(o) and more.fullmatch(n[len(o):])) for n in lines):
                kept += 1
            else:
                errs.append(f"이전 경위 줄이 바뀌거나 사라짐: {o[:80]}")
    res = {"ok": not errs, "errors": errs, "warnings": warns, "lines": len(lines),
           "marks": {k: count.get(k, 0) for k in MARKS}, "previous_lines_kept": kept}
    _log_op(cfg, {"cmd": "check", "page": page.path.stem, "ok": not errs,
                  "categories": sorted({_category(e) for e in errs})})
    if not errs:
        try:
            hist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(page.path, hist)
            res["snapshot"] = "통과 — 이 판을 .history 에 보관했다(다음 check 가 이것과 비교한다)"
        except OSError as e:
            res["snapshot"] = f"통과했지만 사본을 못 남겼다: {e}"
    return res


# ── status ────────────────────────────────────────────────────────────────

def status(store: Store, cfg: Config, today: date | None = None) -> list[dict]:
    """주제마다 한 줄 — 돌릴지 말지 정하는 재료(AI 를 부르기 전에 본다)."""
    today = today or date.today()
    mail = _Mail(store, cfg)
    out = []
    for path in page_paths(cfg):
        page = parse_page(path)
        threads, cands, excl = page.ids("threads"), page.ids("candidates"), page.ids("excluded")
        live = [t for t in threads if t not in mail.hidden]
        through = page.through
        pending = sum(1 for t in live for m in mail.msgs(t) if m["sent_on"][:16] > through)
        have = sum(1 for t in live for m in mail.msgs(t) if m["sent_on"][:16] <= through)
        rows, _ = _alias_scan(mail, page.names, skip=set(threads) | set(excl))
        new_conf = [r for r in rows if r["verdict"] == "확정"]
        skip = set(threads) | set(excl) | {r["thread"] for r in rows}
        term_c, _, _ = _term_scan(mail, page.list("terms"), skip)
        fresh = [r for r in rows if r["verdict"] == "후보" and r["thread"] not in cands]
        fresh += [r for r in term_c if r["thread"] not in cands]
        pending += sum(sum(1 for m in mail.msgs(r["thread"])) for r in new_conf)
        overdue = sum(1 for l in page.bullets("열린 것") if _deadline_passed(l, today))
        try:
            age = (today - date.fromisoformat(through[:10])).days
        except ValueError:
            age = None
        todo = ("새 메일이 반영된 것보다 많다 — 묶음 여러 번" if pending > max(have, SCAN_LIMIT)
                else "update" if pending or new_conf or fresh or overdue
                else "판정할 후보 있음" if cands else "최신")
        out.append({"topic": page.topic, "file": path.name, "through": through,
                    "days_since": age, "new_messages": pending,
                    "new_confirmed_threads": len(new_conf), "new_candidates": len(fresh),
                    "candidates_waiting": len(cands), "deadlines_passed_unmarked": overdue,
                    "threads": len(live), "todo": todo})
    return out


def status_text(rows: list[dict]) -> str:
    if not rows:
        return "주제 페이지가 없다 — `/mail-wiki new <주제>` 로 만든다."
    out = []
    for r in rows:
        out.append(
            f"{r['topic']}  [{r['todo']}]\n"
            f"    기준 {r['through'].replace('T', ' ')}"
            + (f" ({r['days_since']}일 전)" if r["days_since"] is not None else "")
            + f" · 스레드 {r['threads']}\n"
            f"    새 메일 {r['new_messages']} · 새 확정 스레드 {r['new_confirmed_threads']}"
            f" · 새 후보 {r['new_candidates']} · 판정 대기 후보 {r['candidates_waiting']}"
            f" · 표시 안 된 지난 기한 {r['deadlines_passed_unmarked']}")
    return "\n".join(out)


# ── report ────────────────────────────────────────────────────────────────

def _ops(cfg: Config) -> list[dict]:
    p = wiki_dir(cfg) / ".history" / "ops.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _people(mail: _Mail, tid: int) -> set[str]:
    who = set()
    for m in mail.msgs(tid):
        who.add((m["sender_addr"] or "").lower())
        for col in ("to_addrs", "cc_addrs"):
            who.update(a.strip().lower() for a in (m[col] or "").split(";") if a.strip())
    return who - {""}


def _words(s: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]+|[가-힣]{2,}", s or "")}


def report(store: Store, cfg: Config, today: date | None = None) -> str:
    """회사 내용 없는 수치 보고. 주제는 T1, T2 로만 나오고 제목·이름·낱말·번호는 없다.

    이 기능을 고치는 사람은 회사 밖에 있어 메일을 볼 수 없다. 그래서 '무엇이 안
    되는가'를 **세어서** 내보낸다 — 특히 scan 이 못 올려 사용자가 직접 넣은 스레드는
    왜 놓쳤는지 알 수 있게 그 스레드의 수치 특징(낱말 겹침·참여자 겹침·날짜 거리)을
    함께 낸다. 내용을 안 봐도 찾기 규칙을 설계할 수 있어야 하기 때문이다.
    """
    today = today or date.today()
    mail = _Mail(store, cfg)
    pages = page_paths(cfg)
    n_msg = store.db.execute("SELECT COUNT(*) n FROM messages WHERE gone_at IS NULL").fetchone()["n"]
    n_noise = sum(r["n"] for r in store.db.execute(
        "SELECT sender_addr, COUNT(*) n FROM messages WHERE gone_at IS NULL GROUP BY sender_addr")
        if cfg.is_noise(r["sender_addr"] or ""))
    out = ["mail-wiki 자가 평가 — 수치 (메일 내용·이름·주제어 없음)",
           f"생성 {today.isoformat()} · 도구 판 {WIKI_VER}",
           f"[규모] 메일 {n_msg} · 사람이 쓴 스레드 {mail.human_thread_count()}"
           f" · 자동 알림 {round(100 * n_noise / max(n_msg, 1))}% · 숨긴 스레드 {len(mail.hidden)}"
           f" · 주제 {len(pages)}"]
    stems = {}
    for i, path in enumerate(pages, 1):
        page = parse_page(path)
        stems[path.stem] = f"T{i}"
        threads, cands, excl = page.ids("threads"), page.ids("candidates"), page.ids("excluded")
        live = [t for t in threads if t not in mail.hidden]
        rows, noise = _alias_scan(mail, page.names)
        auto = {r["thread"] for r in rows if r["verdict"] == "확정"}
        alias_c = {r["thread"] for r in rows if r["verdict"] == "후보"}
        terms = page.list("terms")
        term_rows, common, outside = _term_scan(mail, terms, skip=auto | alias_c)
        term_c = {r["thread"] for r in term_rows}
        manual = [t for t in live if t not in auto | alias_c | term_c]
        kyung = page.bullets("경위")
        marks = collections.Counter(p[1] for p in map(_line_parts, kyung) if p)
        bad = sum(1 for l in kyung if not _line_parts(l))
        tails = sum(len(_tails(l)) for l in kyung)
        cur, opn = page.bullets("현재 상태"), page.bullets("열린 것")
        kinds = collections.Counter(
            (re.match(r"- ([^\s:]+):", l).group(1) if re.match(r"- ([^\s:]+):", l) else "기타")
            for l in opn)
        through = page.through
        n_in = sum(len(mail.msgs(t)) for t in live)
        pending = sum(1 for t in live for m in mail.msgs(t) if m["sent_on"][:16] > through)
        try:
            age = (today - date.fromisoformat(through[:10])).days
        except ValueError:
            age = "?"
        rating = page.meta.get("rating", "").strip()
        rating = rating if rating in RATINGS else ("없음" if not rating else "기타")
        dead = sum(1 for t in terms if outside.get(t, 0) == 0)
        out += [
            "",
            f"[주제 T{i}] 별칭 {len(page.names) - 1} · 고유 낱말 {len(terms)}"
            f"(바깥에서 한 번도 안 걸림 {dead} · 너무 흔해 버림 {len(common)})"
            f" · 페이지 {round(path.stat().st_size / 1024, 1)}KB · 사용자 평가 {rating}",
            f"  소속 스레드 {len(live)} = 별칭으로 자동 확정 {len(set(live) & auto)}"
            f" + 별칭 후보에서 넣음 {len(set(live) & alias_c)}"
            f" + 낱말 후보에서 넣음 {len(set(live) & term_c)}"
            f" + scan 이 못 올려 직접 넣음 {len(manual)}",
            f"  후보 대기 {len(cands)} (별칭 스침 {len(set(cands) & alias_c)}"
            f" · 낱말 겹침 {len(set(cands) & term_c)}"
            f" · 지금은 안 걸림 {len(set(cands) - alias_c - term_c - auto)})"
            f" · 제외 {len(excl)} (자동 확정이었을 것 {len(set(excl) & auto)}"
            f" · 후보였을 것 {len(set(excl) & (alias_c | term_c))})",
            f"  자동 알림으로 제외한 메일 {noise} · 소속 메일 {n_in}통 · 숨겨져 빠진 소속 스레드 "
            f"{len(threads) - len(live)}",
            f"  경위 {len(kyung)}줄 (" + " · ".join(f"{k} {marks.get(k, 0)}" for k in MARKS)
            + f") · 형식 어긋남 {bad} · 꼬리 {tails} · 메일당 {round(len(kyung) / max(n_in, 1), 2)}줄",
            f"  현재 상태 {len(cur)}줄 (결정 {sum(l.startswith('- 결정:') and '아직' not in l for l in cur)}"
            f" · 사실 {sum(l.startswith('- 사실:') for l in cur)}) · 열린 것 {len(opn)}줄 ("
            + (" · ".join(f"{k} {v}" for k, v in kinds.items()) or "없음")
            + f") · 지남 표시 {sum('지남' in l for l in opn)}"
            f" · 표시 안 된 지난 기한 {sum(_deadline_passed(l, today) for l in opn)}",
            f"  반영 기준 뒤 경과 {age}일 · 아직 반영 안 된 새 메일 {pending}통",
        ]
        # 놓친 스레드의 생김새 — 내용 없이 수만. 찾기 규칙을 고칠 유일한 단서다.
        others_title = set()
        for t in live:
            others_title |= _words(mail.title(t))
        for k, t in enumerate(manual, 1):
            rest = [x for x in live if x != t]
            mine = _people(mail, t)
            theirs = set().union(*[_people(mail, x) for x in rest]) if rest else set()
            text = " ".join((m["subject"] or "") + " " + (m["new_content"] or "")
                            for m in mail.msgs(t)).lower()
            pre_only = (any(a.lower() in " ".join(take_preserved(m["new_content"] or "")
                                                 for m in mail.msgs(t)).lower() for a in page.names)
                        and not any(a.lower() in " ".join(strip_preserved(m["new_content"] or "")
                                                          for m in mail.msgs(t)).lower()
                                    for a in page.names))
            days = [date.fromisoformat(m["sent_on"][:10]) for m in mail.msgs(t)]
            odays = [date.fromisoformat(m["sent_on"][:10]) for x in rest for m in mail.msgs(x)]
            gap = min((abs((a - b).days) for a in days for b in odays), default="?")
            title_rest = set()
            for x in rest:
                title_rest |= _words(mail.title(x))
            out.append(
                f"  직접 넣은 스레드 {k}: 메일 {len(mail.msgs(t))}통"
                f" · 고유 낱말 겹침 {sum(1 for w in terms if w.lower() in text)}개"
                f" · 별칭이 보존 인용 안에만 {'예' if pre_only else '아니오'}"
                f" · 참여자 {len(mine)}명 중 다른 소속 스레드와 겹침 {len(mine & theirs)}명"
                f" · 가장 가까운 소속 메일과 {gap}일 차"
                f" · 제목 낱말 겹침 {len(_words(mail.title(t)) & title_rest)}개")

    # 작업 기록 — 종류·모델만
    logp = wiki_dir(cfg) / "log.md"
    if logp.exists():
        text = logp.read_text(encoding="utf-8")
        ops = collections.Counter(m.group(1) for m in re.finditer(
            r"^- \d{4}-\d\d-\d\d (new|update|add|drop|feedback)\b", text, re.M))
        models = collections.Counter(
            m.group(1).strip() for m in re.finditer(r"· 모델 ([A-Za-z0-9 ._:\-/]{1,40})\s*$", text, re.M))
        out += ["", "[작업] " + (" · ".join(f"{k} {v}" for k, v in ops.items()) or "없음")
                + f" · 변경 없음 {text.count('변경 없음')}"
                + " · 모델: " + (", ".join(f"{k}({v})" for k, v in models.items()) or "기록 없음")]
    recs = _ops(cfg)
    if recs:
        scans = [r for r in recs if r.get("cmd", "").startswith("scan")]
        checks = [r for r in recs if r.get("cmd") == "check"]
        big = [r["delivered"] for r in scans if r.get("delivered")]
        cat = collections.Counter(c for r in checks for c in r.get("categories", []))
        # 한 작업 = 같은 페이지에서 통과할 때까지의 check 묶음. 첫 시도 통과율을 센다.
        groups, cur_page, cur_n, first_ok = 0, None, 0, 0
        durations = []
        last_scan: dict[str, str] = {}
        for r in recs:
            if r.get("cmd", "").startswith("scan"):
                last_scan[r.get("page", "")] = r["ts"]
            elif r.get("cmd") == "check":
                key = r.get("page", "")
                cur_n = cur_n + 1 if key == cur_page else 1
                cur_page = key
                if r.get("ok"):
                    groups += 1
                    first_ok += cur_n == 1
                    cur_page, cur_n = None, 0
                    if key in last_scan:
                        try:
                            d = (datetime.fromisoformat(r["ts"])
                                 - datetime.fromisoformat(last_scan.pop(key))).total_seconds() / 60
                            durations.append(d)
                        except ValueError:
                            pass
        durations.sort()
        by_day = collections.Counter(r["ts"][:10] for r in checks)
        fail_day = collections.Counter(r["ts"][:10] for r in checks if not r.get("ok"))
        out += [
            f"[묶음] scan {len(scans)}회 · 메일을 준 scan {len(big)}회"
            + (f" · 묶음당 평균 {round(sum(big) / len(big), 1)}통 · 최대 {max(big)}통" if big else "")
            + f" · 남은 것이 있던 scan {sum(1 for r in scans if r.get('remaining'))}회",
            f"[검증] check {len(checks)}회 · 실패 {sum(not r.get('ok') for r in checks)}회"
            f" · 통과까지 간 작업 {groups}건 중 첫 시도 통과 {first_ok}건 · 실패 유형: "
            + (" · ".join(f"{k} {v}" for k, v in cat.items()) or "없음"),
            "[소요] scan 에서 check 통과까지 "
            + (f"{len(durations)}건 · 중앙값 {round(durations[len(durations) // 2], 1)}분"
               f" · 최대 {round(durations[-1], 1)}분" if durations else "기록 없음"),
            "[회차] " + " · ".join(f"{d} check {n}(실패 {fail_day.get(d, 0)})"
                                 for d, n in sorted(by_day.items())[-8:]),
        ]
    else:
        out += ["[검증] 실행 기록 없음"]
    notes = wiki_dir(cfg) / "notes.md"
    if notes.exists():
        ls = [l for l in notes.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        out += ["", f"[작업 메모 {len(ls)}건 — 세션이 남긴 메모 원문. 그대로 내보내지 말고 "
                    "feedback 절차로 다시 쓴 뒤 leak 을 통과시킨다]"] + ls
    return "\n".join(out)


# ── leak ──────────────────────────────────────────────────────────────────

def leak(store: Store, cfg: Config, text: str, window: int = LEAK_WINDOW) -> dict:
    """밖으로 보낼 글에 회사 내용이 섞였는지 대조한다.

    잡는 것: 메일 주소·도메인, 사람 이름, 주제어·별칭·고유 낱말, 메일 원문(제목 포함)과
    window 자 이상 이어서 겹치는 구간, 번호로 보이는 긴 숫자.
    못 잡는 것: **뜻만 옮긴 문장** — 그래서 통과해도 보내기 전에 사람이 읽는다.
    이 함수는 아무것도 전송하지 않는다.
    """
    low = text.lower()
    found: list[dict] = []

    def add(kind: str, what: str) -> None:
        if {"kind": kind, "what": what} not in found:
            found.append({"kind": kind, "what": what})

    for m in set(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text)):
        add("메일 주소", m)
    names, domains = set(), set()
    for r in store.db.execute("SELECT DISTINCT sender_name, sender_addr FROM messages"):
        addr = (r["sender_addr"] or "").lower()
        if "@" in addr:
            domains.add(addr.split("@", 1)[1])
        if cfg.is_noise(addr):
            continue
        n = r["sender_name"] or ""
        for t in _TITLES:
            n = n.replace(t, " ")
        names.update(tok for tok in re.split(r"[\s,/()]+", n) if len(tok) >= 2)
    for n in getattr(cfg, "my_names", None) or []:
        names.update(tok for tok in str(n).split() if len(tok) >= 2)
    for d in sorted(domains):
        if d and d in low:
            add("도메인", d)
    for n in sorted(names):
        if n.lower() in low:
            add("사람 이름", n)
    for path in page_paths(cfg):
        page = parse_page(path)
        for s in page.names + page.list("terms"):
            if len(s) >= 2 and s.lower() in low:
                add("주제어·별칭·낱말", s)

    # 원문 겹침 — 글의 window 자 조각을 집합으로 두고 메일을 한 번 훑는다. 반대로(글
    # 조각마다 전 메일을 찾기) 하면 메일 수만 배가 든다.
    t = _norm(text)
    grams = {t[i:i + window] for i in range(len(t) - window + 1)}
    hit = set()
    if grams:
        for r in store.db.execute("SELECT subject, new_content FROM messages"):
            body = _norm((r["subject"] or "") + " \n " + (r["new_content"] or ""))
            for i in range(len(body) - window + 1):
                g = body[i:i + window]
                if g in grams:
                    hit.add(g)
    i = 0
    while i + window <= len(t):
        if t[i:i + window] in hit:
            j = i + window
            while j < len(t) and t[j - window + 1:j + 1] in hit:
                j += 1
            add(f"메일 원문과 {window}자 이상 겹침", t[i:j])
            i = j
        else:
            i += 1
    for m in set(re.findall(r"\d{7,}", text)):
        add("긴 숫자(메일·스레드 번호일 수 있음)", m)
    return {"ok": not found, "found": found,
            "note": "ok 여도 보내기 전에 사람이 읽는다 — 뜻만 옮긴 문장은 이 검사가 못 잡는다. "
                    "이 명령은 아무것도 전송하지 않는다."}
