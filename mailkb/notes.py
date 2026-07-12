"""지식 볼트 — 마크다운 파일.

vault/daily/YYYY-MM-DD.md  데일리 리뷰 (review 가 생성)
vault/notes/<slug>.md      큐레이션 노트 (note 명령이 템플릿 생성, 요지는 사람이 기입)

노트의 메일 참조는 Message-ID (영구 키). EntryID 는 open 편의용 보조.
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


def create_thread_note(cfg: Config, store: Store, thread_id: int) -> Path:
    """스레드 노트 템플릿. 이미 있으면 덮어쓰지 않는다 (사람 기록 보호)."""
    msgs = store.thread_messages(thread_id)
    if not msgs:
        raise SystemExit(f"스레드 #{thread_id} 없음")
    t = store.thread(thread_id)
    subject = msgs[0]["subject"]
    # thread_id 접미로 동일 제목의 다른 스레드끼리 파일명이 충돌하지 않게 함
    path = cfg.vault / "notes" / f"{_slug(subject)}-{thread_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path

    participants = sorted(
        {m["sender_name"] or m["sender_addr"] for m in msgs}
    )
    lines = [
        "---",
        f"thread: {thread_id}",
        f"subject: {subject}",
        f"period: {msgs[0]['sent_on'][:10]} ~ {msgs[-1]['sent_on'][:10]}",
        f"participants: {', '.join(participants)}",
        f"created: {date.today().isoformat()}",
        "---",
        "",
        f"# {subject}",
        "",
        "## 요지 (직접 기입)",
        "- ",
        "",
        "## 결정과 근거",
        "- ",
        "",
    ]
    if t and t["rolling_summary"]:
        lines += ["## AI 누적 요약 (참고)", "", t["rolling_summary"], ""]
    lines.append("## 메일 타임라인")
    for m in msgs:
        att = f" 📎{m['attach_names']}" if m["attach_names"] else ""
        lines.append(
            f"- {m['sent_on'][:16]} **{m['sender_name']}** — {m['subject']}{att}"
        )
        lines.append(f"  `{m['message_id']}`")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
