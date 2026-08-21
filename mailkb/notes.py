"""지식 볼트 — 마크다운 파일.

vault/daily/YYYY-MM-DD.md  데일리 리뷰 (review 가 생성)
vault/notes/<slug>.md      큐레이션 노트 (note 명령이 템플릿 생성, 요지는 사람이 기입)

노트의 메일 참조는 Message-ID (영구 키). EntryID 는 open 편의용 보조.

노트의 원본은 **파일**이다(2026-08-11 사용자 확정) — 사람이 외부 편집기로
고치는 대상이라 DB 로 옮기지 않는다("파일은 코드가 지우면 안 된다"와 같은
계약). 검색·AI 문맥용 색인은 store 의 notes 테이블에 **미러**로 두고,
mtime 비교(reindex)로 파일을 따라간다 — 색인은 지워져도 재색인으로 복구된다.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import Config
from .store import Store


def _slug(text: str) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", text).strip("-")
    return s[:60] or "untitled"


def write_daily(cfg: Config, date_iso: str, content: str) -> Path:
    path = cfg.vault / "daily" / f"{date_iso}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class NoThread(Exception):
    """그 번호의 스레드가 없다 — 호출부가 화면에 맞게 처리한다.

    종전에는 SystemExit 였는데, 이 앱의 웹은 COM 때문에 **단일 스레드**
    HTTPServer 라(ThreadingHTTPServer 아님) SystemExit 가 serve_forever 까지
    올라가 **서버 프로세스가 통째로 종료**됐다 — 없는 번호로 노트 POST 한 번이면
    앱이 꺼졌다(2026-08-11 점검에서 실측). CLI 는 이 예외를 SystemExit 로
    바꿔 종전 동작(메시지 + 종료 코드)을 유지한다."""


def _thread_msgs(store: Store, thread_id: int) -> list:
    """스레드 메일 목록 — 없으면 NoThread. 발생점을 여기 한 곳으로 모은다."""
    msgs = store.thread_messages(thread_id)
    if not msgs:
        raise NoThread(f"스레드 #{thread_id} 없음")
    return msgs


def _note_file(cfg: Config, thread_id: int, msgs: list) -> Path:
    """노트 파일 경로 — 스레드 번호 접미로 같은 제목의 다른 스레드와 안 겹친다.

    화면·문서와 같은 표기(`260714-001`)를 쓴다. 파일은 사람이 열어 보는 것이라
    여기만 다른 숫자가 뜨면 같은 스레드인지 대조가 안 된다.
    """
    path = (cfg.vault / "notes"
            / f"{_slug(msgs[0]['subject'])}-{thread_id}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _frontmatter(thread_id: int, msgs: list) -> str:
    """파일 머리의 meta — **화면에는 절대 안 보인다**(2026-08-11 사용자 요구).

    그래도 파일에 남기는 이유는 노트를 Obsidian 같은 외부 도구로 열었을 때
    어느 스레드의 기록인지 알 길이 이것뿐이기 때문이다."""
    participants = sorted({m["sender_name"] or m["sender_addr"] for m in msgs})
    return "\n".join([
        "---",
        f"thread: {thread_id}",
        f"subject: {msgs[0]['subject']}",
        f"period: {msgs[0]['sent_on'][:10]} ~ {msgs[-1]['sent_on'][:10]}",
        f"participants: {', '.join(participants)}",
        f"created: {date.today().isoformat()}",
        "---",
        "",
    ])


def note_template_body(store: Store, thread_id: int) -> str:
    """빈 노트의 사람 본문 초안 — 파일 생성과 웹 편집기 초안이 공유한다.

    AI 누적 요약 사본과 메일 타임라인은 넣지 않는다(2026-08-11 사용자 확정).
    둘 다 스레드 화면이 더 잘 보여주는 중복이었고, 무엇보다 **파일 본문 =
    화면에 보이는 것 = 색인·AI 에 실리는 것**이라는 등식을 깨뜨렸다.

    제목 줄(`# 제목`)도 같은 이유로 뺀다(2026-08-12 사용자 지적) — 편집 상자
    바로 위에 스레드 제목이 이미 크게 있어 한 줄이 통째로 중복이었다. 파일만
    따로 열었을 때는 frontmatter 의 subject 와 파일명이 그 역할을 한다."""
    _thread_msgs(store, thread_id)      # 없는 스레드면 여기서 NoThread
    return "\n".join([
        "## 요지",
        "- ",
        "",
        "## 결정과 근거",
        "- ",
        "",
    ])


def create_thread_note(cfg: Config, store: Store, thread_id: int) -> Path:
    """스레드 노트 템플릿 파일. 이미 있으면 덮어쓰지 않는다 (사람 기록 보호).

    **파일을 만드는** 진입점이다(CLI `note`, 웹의 외부 편집기 열기). 본문을
    고치는 것은 save_thread_note 쪽이다."""
    msgs = _thread_msgs(store, thread_id)
    path = _note_file(cfg, thread_id, msgs)
    if path.exists():
        return path
    path.write_text(_frontmatter(thread_id, msgs)
                    + note_template_body(store, thread_id), encoding="utf-8")
    return path


_NOTE_TID_RX = re.compile(r"-(\d+)\.md$")
# 사람 본문에서 제외하는 기계 생성 절. 2026-08-11 이후 새 노트에는 아예 안
# 들어가지만(note_template_body), 그 전에 만든 파일이 남아 있어 필터는 유지한다.
_MACHINE_HEADINGS = ("## AI 누적 요약", "## 메일 타임라인")


def find_thread_note(cfg: Config, thread_id: int) -> Path | None:
    """이 스레드의 노트 파일 — 없으면 None.

    글롭 `*-{tid}.md` 는 접미 일치라 tid 7 이 `…-17.md` 를 잡지 않는다('-' 가
    구분자). 같은 tid 파일이 여럿이면(제목 개명 후 수동 복사 등) 정렬 첫 번째 —
    reindex 와 같은 규칙이라 화면과 색인이 같은 파일을 본다."""
    d = cfg.vault / "notes"
    if not d.is_dir():
        return None
    hits = sorted(p for p in d.glob(f"*-{int(thread_id)}.md") if p.is_file())
    return hits[0] if hits else None


def note_body(text: str) -> str:
    """노트 파일 → 사람 본문. frontmatter 와 기계 생성 절을 뗀다.

    화면([내 노트])과 색인(notes 테이블)이 같은 본문을 쓴다 — 다르면 검색에
    걸린 문장이 화면에 없다."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    out, skipping = [], False
    for ln in text.splitlines():
        if ln.startswith("## "):
            skipping = any(ln.startswith(h) for h in _MACHINE_HEADINGS)
        if not skipping:
            out.append(ln)
    return "\n".join(out).strip()


def replace_body(raw: str, body: str) -> str:
    """frontmatter(meta)는 그대로 두고 그 아래를 사람 본문으로 통째 교체.

    화면 계약이 '텍스트 상자에 보인 것 = 파일의 본문'이라(meta 는 화면에 절대
    노출하지 않는다 — 2026-08-11 사용자 요구) 아래쪽을 통으로 바꾼다. 경계는
    note_body 와 **같은 규칙**(첫 `\\n---`)이라 읽기와 쓰기가 어긋나지 않는다.

    구 파일의 기계 절(AI 누적 요약·메일 타임라인)이 이때 함께 사라지는 것은
    의도다 — 원래 화면에도 색인에도 없던 사본이라 잃는 것이 없다."""
    head = ""
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            head = raw[:end + 4] + "\n\n"
    return head + body.strip() + "\n"


def save_thread_note(cfg: Config, store: Store, thread_id: int, body: str,
                     base_mtime: float | None = None) -> tuple[str, Path | None]:
    """스레드 노트의 **사람 본문**을 파일에 반영 — 웹 인라인 편집기의 저장.

    '파일이 원본'이라는 계약(모듈 머리말)은 그대로다. 웹은 외부 편집기와 같은
    자격의 편집자 하나일 뿐이라, 쓰기 전에 base_mtime 으로 '내가 열어 본 그
    파일이 맞는지' 확인하고 다르면 **손대지 않는다**(2026-08-11). 자동 저장을
    두지 않은 것과 같은 이유다 — 사람이 쓴 글을 코드가 말없이 지우지 않는다.

    본문이 비면 파일을 지운다: '비우고 저장 = 삭제'가 편집기 하단 안내와 같은
    규칙이고, 그래야 앱 안에서 노트를 정리할 길이 생긴다(사용자 확정).

    반환 (상태, 경로) — created · saved · deleted · noop · conflict
    """
    path = find_thread_note(cfg, thread_id)
    cur = 0.0
    if path is not None:
        try:
            cur = path.stat().st_mtime
        except OSError:
            path = None                       # 그새 사라졌다 = 새로 만드는 셈
    # 허용 오차는 reindex 와 같은 1e-6 (mtime 을 repr 로 왕복시키므로 정확히 같다)
    if base_mtime is not None and abs(cur - base_mtime) > 1e-6:
        return "conflict", path
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        if path is None:
            return "noop", None               # 만들 것도 지울 것도 없다
        path.unlink()
        reindex(cfg, store)                   # prune_notes 가 색인을 걷어낸다
        return "deleted", path
    if path is None:                          # 첫 저장에서만 파일이 생긴다
        msgs = _thread_msgs(store, thread_id)
        path = _note_file(cfg, thread_id, msgs)
        raw, created = _frontmatter(thread_id, msgs), True
    else:
        raw, created = path.read_text(encoding="utf-8"), False
        if note_body(raw) == text:
            return "saved", path              # 안 바뀌었으면 mtime 도 안 건드린다
    path.write_text(replace_body(raw, text), encoding="utf-8")
    # 색인은 reindex 에 맡기지 않고 **직접** 쓴다: reindex 는 mtime 이 같으면
    # 건너뛰는데, 같은 파일을 연달아 저장하면 파일시스템 시각 해상도 안에서
    # 두 번째 쓰기의 mtime 이 첫 번째와 같을 수 있어 색인이 옛 본문에 머문다
    # (2026-08-11 테스트가 실제로 잡았다). 방금 쓴 내용을 아는데 파일을 다시
    # 읽을 이유도 없다.
    store.index_note(thread_id, str(path), path.stat().st_mtime, text)
    return ("created" if created else "saved"), path


def reindex(cfg: Config, store: Store) -> int:
    """vault/notes/*.md ↔ notes 테이블 동기화(mtime 증분). 반환 = 변경 건수.

    파일이 원본이므로 방향은 파일 → DB 한쪽뿐이다. 사라진 파일의 색인은 지운다
    — 색인은 미러라 지워도 잃는 것이 없다(파일 삭제는 사람이 한 결정)."""
    d = cfg.vault / "notes"
    seen: set[int] = set()
    changed = 0
    if d.is_dir():
        for p in sorted(d.iterdir()):
            m = _NOTE_TID_RX.search(p.name)
            if not p.is_file() or not m:
                continue
            tid = int(m.group(1))
            if tid in seen:          # 같은 tid 둘 — find_thread_note 와 같은 첫 파일
                continue
            seen.add(tid)
            mtime = p.stat().st_mtime
            row = store.note_row(tid)
            if row and row["path"] == str(p) and abs(row["mtime"] - mtime) < 1e-6:
                continue
            try:
                body = note_body(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue             # 잠긴/깨진 파일은 이번 회차만 건너뛴다
            store.index_note(tid, str(p), mtime, body)
            changed += 1
    return changed + store.prune_notes(seen)
