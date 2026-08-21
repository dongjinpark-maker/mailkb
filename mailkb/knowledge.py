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
                   backend: str | None = None) -> Path:
    """후보 → md 파일. **여기가 파일이 생기는 유일한 지점**이다.

    보강(AI 1콜)을 저장 시점에 시도하고, 실패하면 수확본 그대로 저장한다 —
    AI 실패는 우아하게, 저장 자체는 늘 된다.
    """
    row = store.knowledge_candidate(cid)
    if row is None or row["status"] != "pending":
        raise ValueError(f"암묵지 후보 없음 또는 처리됨: #{cid}")
    tids = [int(t) for t in (row["threads"] or "").split(";") if t.strip()]
    body = (row["body"] or "").strip()
    try:
        cmd = cfg.ai_cmd(backend)
        enriched = review.ai_run(cmd, ENRICH.format(
            title=row["title"], body=body or "(내용 없음)",
            threads=_thread_material(store, tids)))
        enriched = enriched.strip()
        if enriched:
            body = enriched
    except (SystemExit, review.AIError):
        pass                              # 백엔드 미설정·실패 → 수확본 유지

    path = _unique_path(cfg, row["date"], row["title"])
    text = "\n".join([
        "---",
        f"created: {row['date']}",
        f"source: {row['source']}",
        f"threads: [{', '.join(str(t) for t in tids)}]",
        f"saved: {date.today().isoformat()}",
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
                          path.stat().st_mtime, body)
    return path


# ------------------------------------------------------------------ 색인

_TITLE_RX = re.compile(r"(?m)^#\s+(.+)$")
_FM_THREADS_RX = re.compile(r"(?m)^threads:\s*\[([^\]]*)\]")


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
            if row and abs(row["mtime"] - mtime) < 1e-6:
                continue
            try:
                title, threads, core = parse_file(
                    p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue                  # 잠긴/깨진 파일은 이번 회차만 건너뛴다
            store.index_knowledge(str(p), title or p.stem, threads, mtime, core)
            changed += 1
    return changed + store.prune_knowledge(seen)
