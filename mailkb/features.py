"""Stable message features computed once during ingestion."""

from __future__ import annotations

import re

from .clean import strip_preserved


# Bump when the derived schema or a rule changes so local rows rebuild once.
FEATURE_VERSION = "1"

_TIME_WORD = (
    r"(?:오늘|금일|내일|명일|익일|모레|이번\s*주|금주|다음\s*주|차주|내주|주말|월말|"
    r"(?:월|화|수|목|금|토|일)요일|오전|오후|아침|저녁|자정|정오|"
    r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\d{1,2}:\d{2}|"
    r"\d{1,2}\s*[/.]\s*\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*일|EOD|COB)"
)
DEADLINE_RX = re.compile(
    r"(?:" + _TIME_WORD + r"\s*(?:\([^\)\n]{0,8}\)\s*)?까지"
    r"|기한|마감|회신\s*(?:부탁|요청|바랍)|확정해\s*주|주시면)",
    re.IGNORECASE,
)

DECISION_RX = re.compile(
    r"("
    r"(승인|결재|재가|컨펌|approve|confirm)\s*(을|를|해)?\s*"
    r"(부탁|요청|필요|바랍|주시|주세요|주실|해\s*주|가능|여부)"
    r"|검토\s*(부탁|요청)"
    r"|의견\s*(을|를)?\s*(주|부탁|달라|주세요|주시)"
    r"|결정\s*(을|를|이|해)?\s*(부탁|필요|해\s*주|주시)"
    r"|판단\s*(을|를|이)?\s*(부탁|필요|바랍)"
    r"|가부[^\n]{0,10}(회신|부탁|판단|결정|주)"
    r")"
)

REQUEST_RX = re.compile(
    r"("
    r"(회신|답변|답장|검토|의견|판단|결정|승인|재가|컨펌|처리|확답)"
    r"\s*(을|를|해|이|여부)?\s*(부탁|요청|바랍|주시|주세요|주실|해\s*주|가능|필요|달)"
    r"|부탁\s*(드립|드려|합니|해\s*주)"
    r"|요청\s*(드립|드려|합니)"
    r"|필요합니다|필요하[여니]"
    r"|가능(할까요|한가요|하신지|하실|할지|합니까|한지)"
    r"|(알려|공유|전달|보내|검토)\s*주(세요|시|실|십)"
    r"|는지\s*[^\n]{0,8}(확인|검토|부탁|회신|알려|판단)"
    r"|해\s*주(세요|실\s*수|시겠|시길)"
    r")"
)


def classify_content(content: str) -> tuple[int, int, int, int]:
    """Return deadline/decision/request/question flags for authored content."""
    body = strip_preserved(content or "")
    return (
        int(bool(DEADLINE_RX.search(body))),
        int(bool(DECISION_RX.search(body))),
        int(bool(REQUEST_RX.search(body))),
        int("?" in body),
    )
