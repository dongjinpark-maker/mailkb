"""소스 공통 데이터 모델.

모든 소스(Outlook COM, Fake, 향후 IMAP 등)는 MailRecord 를 yield 한다.
파이프라인의 나머지 전부는 이 모델만 알면 된다 — COM 은 여기 뒤로 격리된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass
class MailRecord:
    """소스 독립적인 메일 한 통."""

    message_id: str            # RFC Message-ID (영구 키)
    subject: str
    sender_name: str
    sender_addr: str
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    sent_on: str = ""          # ISO8601 "YYYY-MM-DDTHH:MM:SS"
    body_text: str = ""        # 평문/마크다운 본문 (검색·AI·신호용 — 인용 제거 대상)
    body_html: str = ""        # 원본 HTML (표시용 — store 가 정제해 저장; 없으면 "")
    inline_images: dict = field(default_factory=dict)
    # {cid: (mime, bytes)} — HTML 의 cid: 참조에 대응하는 인라인 이미지 바이트.
    # 소스가 바이트만 동봉하고 치환은 store 가 정제(인용 절단) '후'에 한다
    # (docs/PROPOSAL-images.md — 재인용 체인 중복 임베드 방지)
    entry_id: str = ""         # Outlook EntryID (불안정 — 폴더 이동 시 변경)
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    conversation_key: str = "" # 소스가 주는 스레드 힌트 (Outlook ConversationIndex 앞 22바이트 등)
    attachments: list[str] = field(default_factory=list)  # 파일명만; 내용은 hot 저장소(Outlook)에서 O(1) 조회
    folder: str = ""           # "inbox" | "sent" 등


class Source(Protocol):
    """메일 소스 인터페이스."""

    name: str

    def fetch(self, since_iso: str | None,
              image_cutoff: str | None = None) -> Iterator[MailRecord]:
        """since_iso 이후의 메일을 시간순으로 yield. None 이면 전체.

        image_cutoff: 이 날짜(YYYY-MM-DD) 이전 메일은 인라인 이미지 추출 생략
        (선택 — 어댑터가 지원하지 않으면 무시하고 store 게이트가 걸러준다)."""
        ...
