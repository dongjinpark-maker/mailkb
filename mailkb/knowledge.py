"""암묵지 md — vault/knowledge/*.md 생성·색인.

수확(distill.harvest)이 캐낸 후보를 사람이 회고 화면에서 [지식으로 저장]하면
**그때** 파일이 생긴다 — 승인 전에는 파일이 없다(vault 가 초안으로 어지러워지지
않는다). 항목당 파일 하나. 검색으로 알 수 있는 일반 지식이 아니라 조직 고유의
노하우·제약·우회로가 대상이다(선별은 수확 프롬프트가, 확정은 사람이).

**파일이 원본**이다(notes 와 같은 계약). store.knowledge 는 검색·AI 문맥·향후
지식 메뉴용 색인(미러)이라 지워도 reindex 가 복구한다. 사람이 외부 편집기로
고치면 mtime 비교로 따라간다.

참조 절은 코드가 만든다: 메일 줄(스레드의 앵커 메일 + 검증된 인용)과 본문에서
정규식으로 추출한 외부 링크(wiki·jira 등). **AI 에게 링크를 뽑게 하지 않는다**
— URL 환각은 겉보기에 멀쩡해서 위험하고, 코드 추출은 본문에 있는 것만 나오므로
그 자체가 검증이다(CLAUDE.md 7 의 정신).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import review
from .clean import smart_truncate, strip_preserved
from .config import Config
from .notes import _slug
from .store import Store

_URL_RX = re.compile(r"https?://[^\s<>\")\]]+")
_URL_CAP = 5                 # 참조 절의 외부 링크 상한 — 넘치면 소음이다
_KN_DIR = "knowledge"

# 보강 — 사람이 '남길 가치가 있다'고 판단한 것에만 참조 스레드 전문을 읽는
# 큰 호출을 쓴다(수확은 롤링 요약만 본 2~4문장 초안이라 그대로 파일이 되면
# 빈약하다). 출력은 md 본문 그 자체 — 제목·참조는 코드가 감싼다.
ENRICH = """당신은 조직 지식 문서의 편집자다. 아래 초안은 업무 메일에서 캐낸 암묵지 한 건이고, 이어지는 것은 그 근거가 된 메일 스레드 원문이다.

초안을 **몇 달 뒤 맥락 없이 읽어도 재사용할 수 있는** 자기완결 문서 본문으로 다시 써라.

규칙:
- 마크다운 본문만 출력하라 (제목·frontmatter·참조 절 금지 — 코드가 붙인다).
- 4~10문장. 무엇을 어떻게 하는지, 왜 그렇게 하는지, 시도했다 안 된 것이 있으면 그것과 이유.
- 스레드 원문에 없는 사실을 만들지 마라. 일반론·검색으로 알 수 있는 배경 설명을 덧붙이지 마라.
- 사람 이름은 원문에 있는 그대로 쓴다.

[초안]
{title}

{body}

[스레드 원문]
{threads}
"""


def kn_dir(cfg: Config) -> Path:
    return cfg.vault / _KN_DIR


def _unique_path(cfg: Config, date_iso: str, title: str) -> Path:
    d = kn_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    base = f"{date_iso}-{_slug(title)}"
    path = d / f"{base}.md"
    n = 2
    while path.exists():
        path = d / f"{base}-{n}.md"
        n += 1
    return path


def _anchor_mail(store: Store, tids: list[int], quote: str):
    """인용이 실제로 들어 있는 메일 — 참조 줄과 링크 추출의 기준.

    없으면(보강 뒤 재검증 실패 등) 각 스레드의 첫 메일로 대체한다.
    """
    flat = re.sub(r"\s+", "", quote or "")
    rows = []
    for tid in tids:
        msgs = store.thread_messages(tid)
        if not msgs:
            continue
        hit = None
        if flat:
            for m in msgs:
                body = strip_preserved(m["new_content"] or "")
                if flat in re.sub(r"\s+", "", body):
                    hit = m
                    break
        rows.append(hit or msgs[0])
    return rows


def _links_from(mails) -> list[str]:
    """앵커 메일 본문의 URL — 중복 제거, 상한. 자동화 링크(ci 등) 필터는
    실사용에서 소음이 확인되면 config 제외 목록으로(확장 지점)."""
    out: list[str] = []
    for m in mails:
        for u in _URL_RX.findall(strip_preserved(m["new_content"] or "")):
            u = u.rstrip(".,;")
            if u not in out:
                out.append(u)
            if len(out) >= _URL_CAP:
                return out
    return out


def _references(store: Store, tids: list[int], quote: str) -> str:
    mails = _anchor_mail(store, tids, quote)
    lines = ["## 참조"]
    for m in mails:
        who = m["sender_name"] or m["sender_addr"] or "발신자 미상"
        day = (m["sent_on"] or "")[:10]
        q = f' — "{smart_truncate(quote, 120)}"' if quote else ""
        lines.append(f"- [#{m['thread_id']}] {day} {who}{q}")
        quote = ""              # 인용은 앵커 한 줄에만 — 반복하면 소음이다
    lines += [f"- {u}" for u in _links_from(mails)]
    return "\n".join(lines)


def _thread_material(store: Store, tids: list[int], cap: int = 6000) -> str:
    """보강 프롬프트용 스레드 원문 — 스레드당 상한을 두고 시간순."""
    out = []
    for tid in tids:
        for m in store.thread_messages(tid):
            who = m["sender_name"] or m["sender_addr"]
            body = smart_truncate(strip_preserved(m["new_content"] or ""), 800)
            out.append(f"[{(m['sent_on'] or '')[:16]} {who}] {body}")
    return smart_truncate("\n\n".join(out), cap)


def save_candidate(cfg: Config, store: Store, cid: int,
                   backend: str | None = None, cancel=None) -> Path:
    """후보 → md 파일. **여기가 파일이 생기는 유일한 지점**이다.

    보강(AI 1콜)을 저장 시점에 시도하고, 실패하면 수확본 그대로 저장한다 —
    AI 실패는 우아하게, 저장 자체는 늘 된다.

    보강 실패 여부는 `save_candidate.last_enriched` 에 남는다(호출 직후에만
    유효한 관측값). 종전에는 실패가 완전히 조용해서, 사용자는 "지식으로 저장:
    x.md" 성공 메시지만 보고 본문이 보강본인지 수확본인지 알 길이 없었다.

    cancel(threading.Event)이 서면 `review.AICancelled` 가 그대로 올라간다 —
    중지는 실패가 아니라 "지금은 저장하지 않겠다"이므로 수확본으로 저장하지
    않고, 파일도 안 만들고, 후보는 pending 으로 남는다(2026-10-25).
    """
    row = store.knowledge_candidate(cid)
    if row is None or row["status"] != "pending":
        raise ValueError(f"암묵지 후보 없음 또는 처리됨: #{cid}")
    tids = [int(t) for t in (row["threads"] or "").split(";") if t.strip()]
    body = (row["body"] or "").strip()
    save_candidate.last_enriched = True
    try:
        cmd = cfg.ai_cmd(backend)
        enriched = review.ai_run(cmd, ENRICH.format(
            title=row["title"], body=body or "(내용 없음)",
            threads=_thread_material(store, tids)),
            system_prompt=review.MAIL_TASK_SYSTEM, cancel=cancel)
        enriched = enriched.strip()
        if enriched:
            body = enriched
        else:
            save_candidate.last_enriched = False
    # AIAuthError(인증 만료·세션 한도)를 함께 잡는다 — AIError 의 하위가 아니라
    # 종전 except 를 통과했고, 그래서 **저장 자체가 안 됐다**(후보는 pending 으로
    # 남았다). 위 docstring 이 "저장 자체는 늘 된다"고 약속하는데 정작 사용자가
    # 실제로 만나는 실패(세션 한도)에서 그 약속이 깨져 있었다(2026-08-25 실행 확인).
    # 다른 자리에서 AIAuthError 를 올려 보내는 이유(백엔드가 통째로 죽었으니 중단)는
    # 여기엔 없다 — 이 콜은 저장의 곁가지지 저장의 조건이 아니다.
    except (SystemExit, review.AIError, review.AIAuthError):
        save_candidate.last_enriched = False   # 백엔드 미설정·실패 → 수확본 유지

    path = _unique_path(cfg, row["date"], row["title"])
    saved = date.today().isoformat()
    # 대체 관계는 **새 파일에만** 적는다 — 옛 파일은 사람 산출물이라 코드가 고치지
    # 않는다. 역방향(대체됨)은 색인이 조회 때 계산한다(superseded_stems).
    # 저장 사이에 대상 파일이 지워졌으면 관계도 버린다(없는 것을 가리키지 않게).
    sup = (row["supersedes"] or "").strip() if "supersedes" in row.keys() else ""
    if sup and not (kn_dir(cfg) / f"{sup}.md").is_file():
        sup = ""
    text = "\n".join([
        "---",
        f"created: {row['date']}",
        f"source: {row['source']}",
        f"threads: [{', '.join(str(t) for t in tids)}]",
        f"saved: {saved}",
        *([f"supersedes: {sup}.md"] if sup else []),
        "---",
        "",
        f"# {row['title']}",
        "",
        body or "(내용 없음)",
        "",
        _references(store, tids, row["quote"] or ""),
        "",
    ])
    path.write_text(text, encoding="utf-8")
    store.set_knowledge_status(cid, "saved", str(path))
    store.index_knowledge(str(path), row["title"], row["threads"] or "",
                          path.stat().st_mtime, body, saved=saved,
                          supersedes=sup, parse_ver=PARSE_VER)
    return path


# ------------------------------------------------------------------ 색인

_TITLE_RX = re.compile(r"(?m)^#\s+(.+)$")
_FM_THREADS_RX = re.compile(r"(?m)^threads:\s*\[([^\]]*)\]")
_FM_KEY_RX = re.compile(r"(?m)^(saved|created|supersedes):[ \t]*(.*?)[ \t]*$")
_DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")

# 색인이 frontmatter 에서 읽는 것이 늘면 올린다 — reindex 가 mtime 이 같아도 한 번
# 다시 읽는다. 1: saved·supersedes(2026-10-25).
PARSE_VER = 1


def stem_of(value: str) -> str:
    """'x.md'·'x'·경로 → 파일 이름(확장자 없음). 모델·사람이 어떤 꼴로 적어도
    같은 키가 되게 한다."""
    v = (value or "").strip().strip("`'\"").strip()
    return Path(v).stem if v else ""


def parse_meta(text: str) -> dict:
    """frontmatter 의 날짜·대체 관계 — {"saved": YYYY-MM-DD|"", "supersedes": 파일 이름|""}.

    모든 키가 선택적이다. 조사 세션이 vault/knowledge 에 직접 쓴 md 는 frontmatter
    가 없거나 일부만 있을 수 있다(agent-guides/minerva-researcher.md) — 그래도
    색인·목록이 깨지면 안 된다. saved 가 없으면 created 를 쓴다."""
    meta = {"saved": "", "supersedes": ""}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    found = {k: v for k, v in _FM_KEY_RX.findall(text[:end])}
    for key in ("saved", "created"):
        m = _DATE_RX.search(found.get(key, ""))
        if m:
            meta["saved"] = m.group(0)
            break
    meta["supersedes"] = stem_of(found.get("supersedes", ""))
    return meta


def superseded_stems(rows) -> dict:
    """{대체된 파일 이름: 대체한 색인 행} — 조회 때 계산한다(저장하지 않는다).

    대상이 실제 색인에 있을 때만 센다. 자기 자신을 가리키는 것은 무시한다.
    둘 이상이 같은 것을 대체하면 저장일이 늦은 쪽을 남긴다."""
    stems = {Path(r["path"]).stem for r in rows}
    out: dict = {}
    for r in rows:
        sup = (r["supersedes"] or "") if "supersedes" in r.keys() else ""
        if not sup or sup not in stems or sup == Path(r["path"]).stem:
            continue
        prev = out.get(sup)
        if prev is None or (r["saved"] or "") > (prev["saved"] or ""):
            out[sup] = r
    return out


def parse_file(text: str) -> tuple[str, str, str]:
    """(title, threads';'연결, 사람 본문). frontmatter 의 모르는 키는 무시한다
    — 나중에 tags: 등을 더해도 기존 파일과 코드가 안 깨진다(확장 지점)."""
    body = text
    threads = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[:end]
            m = _FM_THREADS_RX.search(fm)
            if m:
                threads = ";".join(
                    t.strip() for t in m.group(1).split(",") if t.strip())
            body = text[end + 4:]
    tm = _TITLE_RX.search(body)
    title = tm.group(1).strip() if tm else ""
    # 색인 본문 = 제목·참조 절을 뗀 알맹이 (검색 스니펫이 본문을 보게)
    core = body.split("## 참조")[0]
    if tm:
        core = core.replace(tm.group(0), "", 1)
    return title, threads, core.strip()


def reindex(cfg: Config, store: Store) -> int:
    """vault/knowledge/*.md ↔ knowledge 색인 동기화(mtime 증분).

    파일이 원본이므로 방향은 파일 → DB 한쪽뿐이다(notes.reindex 와 같은 계약).
    """
    d = kn_dir(cfg)
    seen: set[str] = set()
    changed = 0
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            if not p.is_file():
                continue
            seen.add(str(p))
            mtime = p.stat().st_mtime
            row = store.knowledge_row(str(p))
            # 파싱 버전이 같을 때만 건너뛴다 — 색인이 새 frontmatter 키를 읽게
            # 되면(PARSE_VER) 기존 행을 한 번만 다시 읽는다. 바뀐 행만 쓴다
            # (읽기 경로에서 조건 없이 쓰지 않는다 — CLAUDE.md 9).
            if (row and abs(row["mtime"] - mtime) < 1e-6
                    and (row["parse_ver"] or 0) == PARSE_VER):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                  # 잠긴/깨진 파일은 이번 회차만 건너뛴다
            title, threads, core = parse_file(text)
            meta = parse_meta(text)
            store.index_knowledge(str(p), title or p.stem, threads, mtime, core,
                                  saved=meta["saved"],
                                  supersedes=meta["supersedes"],
                                  parse_ver=PARSE_VER)
            changed += 1
    return changed + store.prune_knowledge(seen)


# ------------------------------------------------------------------ 수확 재료

KNOWN_TOP = 40               # 수확 프롬프트에 싣는 저장 지식 건수 상한(최근 저장순)
KNOWN_CHARS = 3_000          # 그 블록 전체 상한(자) — 수확 시간은 입력 크기에 비례한다


def known_block(store: Store) -> tuple[str, set]:
    """수확 프롬프트의 [이미 있는 지식] 블록과, 대체 대상으로 받아 줄 파일 이름 집합.

    왜 있나(2026-10-25): 수확이 기존 지식을 몰라 같은 노하우를 제목만 바꿔 다시
    캤고(중복 검사는 제목 완전 일치뿐), 결론이 뒤집혀도 옛 지식과 새 후보가 같은
    무게로 남았다. 목록을 보여 주고 (1) 같은 것은 내지 말고 (2) 결론을 바꾸면
    `대체: <파일 이름>` 을 달게 한다. 파일 이름은 코드가 이 집합과 대조한다.

    대기 후보는 파일이 없어 대체 대상이 될 수 없다 — 중복 방지용으로 제목만 싣는다.
    대체된 지식은 목록에서 뺀다(이미 끝난 결론을 또 대체하게 하지 않는다)."""
    rows = store.knowledge_all()
    gone = superseded_stems(rows)
    live = [r for r in rows if Path(r["path"]).stem not in gone]
    live.sort(key=lambda r: (r["saved"] or "", r["mtime"]), reverse=True)
    lines, ok, size = [], set(), 0
    for r in live[:KNOWN_TOP]:
        stem = Path(r["path"]).stem
        ln = f"- {stem}: {r['title']}"
        if size + len(ln) > KNOWN_CHARS:
            break
        lines.append(ln)
        ok.add(stem)
        size += len(ln) + 1
    pend = [f"- (대기) {r['title']}"
            for r in store.knowledge_candidates(status="pending")]
    for ln in pend:
        if size + len(ln) > KNOWN_CHARS:
            break
        lines.append(ln)
        size += len(ln) + 1
    if not lines:
        return "", ok
    return ("[이미 있는 지식 — 같은 내용이면 후보로 내지 마라. "
            "'파일 이름: 제목' 꼴이고 (대기)는 아직 저장 전이다]\n"
            + "\n".join(lines) + "\n\n"), ok

