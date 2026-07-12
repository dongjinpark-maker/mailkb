"""Outlook COM 소스 — 회사 PC(Windows + 클래식 Outlook + pywin32) 전용.

이 파일만 Windows 를 요구한다. import 는 생성 시점까지 지연되므로
Linux/WSL 에서 다른 소스로 개발·테스트하는 데 지장 없다.

검증 항목 (회사 PC에서 최초 실행 시):
  - 보안 팝업 발생 여부 (프로그래밍 방식 액세스 설정)
  - 증분 sync 속도 (150통/일 기준)
  - Exchange 주소(X.500) → SMTP 변환 정상 여부
"""

from __future__ import annotations

import email.parser
import email.utils
from datetime import datetime, timedelta, timezone
from typing import Iterator

from ..clean import html_to_markdown
from .base import MailRecord

# MAPI 속성 (PropertyAccessor 용)
PR_TRANSPORT_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"

FOLDER_INBOX = 6
FOLDER_SENT = 5


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


class OutlookComSource:
    name = "outlook"

    def __init__(self) -> None:
        import win32com.client  # Windows 전용 — 지연 import

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")

    # ------------------------------------------------------------- fetch

    def fetch(self, since_iso: str | None) -> Iterator[MailRecord]:
        for folder_const, folder_name in ((FOLDER_INBOX, "inbox"), (FOLDER_SENT, "sent")):
            folder = self._ns.GetDefaultFolder(folder_const)
            items = folder.Items
            items.Sort("[ReceivedTime]")
            if since_iso:
                # DASL 필터 — 로캘 무관, 단 날짜는 UTC 로 비교됨 → 변환 필수
                when = _dasl_utc(since_iso)
                items = items.Restrict(
                    f"@SQL=\"urn:schemas:httpmail:datereceived\" > '{when}'"
                )
            item = items.GetFirst()
            while item is not None:
                rec = self._to_record(item, folder_name)
                if rec is not None:
                    yield rec
                item = items.GetNext()

    def _to_record(self, item, folder_name: str) -> MailRecord | None:
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

        when = item.ReceivedTime if folder_name == "inbox" else item.SentOn
        to, cc = self._recipients(item)

        return MailRecord(
            message_id=message_id,
            subject=item.Subject or "",
            sender_name=item.SenderName or "",
            sender_addr=self._sender_smtp(item),
            to=to,
            cc=cc,
            sent_on=when.strftime("%Y-%m-%dT%H:%M:%S") if when else "",
            body_text=body,
            body_html=html,
            entry_id=item.EntryID,
            in_reply_to=(headers.get("In-Reply-To") or "").strip(),
            references=refs_raw.split(),
            conversation_key=getattr(item, "ConversationID", "") or "",
            attachments=[a.FileName for a in item.Attachments],
            folder=folder_name,
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
        for folder_const in (FOLDER_INBOX, FOLDER_SENT):
            items = self._ns.GetDefaultFolder(folder_const).Items
            found = items.Find(dasl)
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
