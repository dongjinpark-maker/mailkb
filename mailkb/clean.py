"""본문 정리: HTML→텍스트, 인용문/서명 제거.

토큰 깔때기의 ② 단계. 답장 체인은 이전 메일을 전부 재인용하므로
"이 메일에서 새로 쓰인 부분"만 남기는 것이 검색 품질과 AI 비용 양쪽의 핵심.

한국어 Outlook 환경의 인용 헤더 패턴을 우선 지원한다.
과잉 제거보다 과소 제거가 낫다는 원칙: 애매하면 남긴다.
"""

from __future__ import annotations

import colorsys
import re
from html.parser import HTMLParser


# ---------------------------------------------------------------- HTML → text

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "blockquote", "h1", "h2", "h3"}
# svg·math: 다이어그램/수식의 라벨 텍스트가 문맥 없이 본문에 섞이면 검색·AI 가
# "매출 3억" 같은 조각을 서사처럼 읽는다 — _DROP_TREE(표시)와 같은 취급으로 통째 스킵.
_SKIP_TAGS = {"style", "script", "head", "title", "svg", "math"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = p.text()
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------ HTML → 마크다운
#
# Outlook 의 item.Body 는 이미 서식이 날아간 평문이고, html_to_text 도 굵게/
# 기울임/링크/표를 버린다. vault 가 마크다운이므로 HTMLBody 를 마크다운으로
# 변환해 서식을 살린다. 완전한 렌더러가 아니라 업무 메일에 흔한 서식만 관대하게
# 변환한다(과소 변환 > 과잉 손실 — 실패하면 html_to_text 로 폴백).

_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ",
            "h4": "#### ", "h5": "##### ", "h6": "###### "}
_INLINE_MARK = {"b": "**", "strong": "**", "i": "*", "em": "*",
                "code": "`", "tt": "`",
                "s": "~~", "del": "~~", "strike": "~~"}
# 강조 마커를 붙일 수 있는 인라인 태그(구조 태그와 분리 — <div style=bold> 같은
# 블록에는 마커를 안 붙여 "\n\n**" 깨짐을 피한다). span/font 는 style 로만 판정.
_INLINE_STYLE_TAGS = {"b", "strong", "i", "em", "code", "tt",
                      "u", "span", "font", "mark", "small",
                      "s", "del", "strike"}
_STYLE_BOLD_RX = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)")
_STYLE_ITALIC_RX = re.compile(r"font-style\s*:\s*italic")
# 취소선 — Confluence 변경 알림의 삭제분(diff-html-removed)·취소 표기 구분
_STYLE_STRIKE_RX = re.compile(r"text-decoration[^;]*line-through")
# 숨김 서브트리 — 알림 메일의 프리헤더(미리보기 문구) 등이 본문으로 새는 것 방지
_HIDE_STYLE_RX = re.compile(
    r"display\s*:\s*none|mso-hide\s*:\s*all|visibility\s*:\s*hidden")
# 레이아웃(컨테이너) 표 식별자 — 알림 메일 셸의 관례적 id/class
_LAYOUT_TABLE_ID_RX = re.compile(r"wrapper|container|pattern|footer|header|email")


def _style_marks(attrs) -> list[str]:
    """style 속성에서 굵게/기울임/취소선을 읽어 마커로. (Word/Outlook 은 <b> 대신
    <span style='font-weight:bold'> 를 즐겨 쓴다.)"""
    style = ""
    for k, v in attrs:
        if k == "style" and v:
            style += ";" + v.lower()
    marks: list[str] = []
    if _STYLE_BOLD_RX.search(style):
        marks.append("**")
    if _STYLE_ITALIC_RX.search(style):
        marks.append("*")
    if _STYLE_STRIKE_RX.search(style):
        marks.append("~~")
    return marks


class _MarkdownConverter(HTMLParser):
    """HTML → 마크다운. 알림형 메일(Confluence/JIRA 류)의 중첩 레이아웃 표를
    견디도록 설계됐다 — 표 상태는 스택으로(중첩 표 무손실), 레이아웃 표는
    컨테이너로 투명화(셀 내용을 블록으로 방출해 개행 보존)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0
        self._hidden = 0                    # display:none 서브트리 깊이 (프리헤더)
        self._list_stack: list[list] = []          # ['ul'] | ['ol', n]
        self._href: list[str] = []
        self._mark_stack: list[tuple] = []          # (sink, idx, marks)
        # 표 상태 — 데이터 표는 스택(_tstack)으로 중첩을 견디고, 레이아웃 표는
        # 모드 스택(_tmode)에 True 로 쌓여 셀 처리를 건너뛴다
        self._tstack: list[dict] = []
        self._tmode: list[bool] = []        # top = 현재(가장 안쪽) 표, True=투명
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._rows: list[list[str]] | None = None
        self._row_is_header = False
        # <pre> 원문 보관 — result() 의 공백 정리에서 코드 들여쓰기를 보호
        self._in_pre = False
        self._pre_parts: list[str] = []
        self._pre_store: list[str] = []
        # 데이터 표 셀 안의 <pre> 인덱스 — 멀티라인 펜스는 표 행(한 줄)을 깨므로
        # 인라인(줄=<br>)으로 복원한다. 표 밖은 종전대로 펜스.
        self._pre_inline: set[int] = set()

    def _sink(self) -> list[str]:
        return self._cell if self._cell is not None else self._out

    def _emit(self, s: str) -> None:
        self._sink().append(s)

    @staticmethod
    def _is_layout_table(attrs) -> bool:
        """레이아웃(컨테이너) 표 판정 — 이메일 조판 관례 기반.

        오판해도 손실은 없다: 투명 오판이면 블록 텍스트로 강등(개행 보존),
        데이터 오판이면 파이프 표로 렌더될 뿐이다."""
        a = {k.lower(): (v or "").lower() for k, v in attrs}
        if a.get("role") == "presentation":
            return True
        ident = a.get("id", "") + " " + a.get("class", "")
        if "confluencetable" in ident:      # Confluence 본문 데이터 표
            return False
        if _LAYOUT_TABLE_ID_RX.search(ident):
            return True
        # 이메일 레이아웃 표의 전형: border/cellpadding/cellspacing 전부 0
        return (a.get("border", "0") in ("", "0")
                and a.get("cellpadding") == "0"
                and a.get("cellspacing") == "0")

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if self._hidden:                    # 숨김 서브트리 — 깊이만 추적
            if tag not in _VOID_HTML:
                self._hidden += 1
            return
        style = next((v for k, v in attrs if k == "style" and v), "")
        if style and _HIDE_STYLE_RX.search(style.lower()):
            if tag not in _VOID_HTML:
                self._hidden = 1
            return
        if tag in _INLINE_STYLE_TAGS:
            marks = ([_INLINE_MARK[tag]] if tag in _INLINE_MARK else []) + _style_marks(attrs)
            sink = self._sink()
            idx = len(sink)
            sink.extend(marks)
            self._mark_stack.append((sink, idx, marks))
            return
        if tag == "br":
            self._emit("\n")
        elif tag == "p":
            self._emit("\n\n")
        elif tag == "div":
            self._emit("\n")
        elif tag in _HEADING:
            self._emit("\n\n" + _HEADING[tag])
        elif tag == "pre":
            # 코드 블록 — 원문 그대로 보관했다가 result() 에서 펜스로 복원
            self._in_pre = True
            self._pre_parts = []
        elif tag == "input":
            # 작업 목록 체크박스 (Confluence 할 일 등) — 상태를 글리프로 보존
            a = {k.lower(): v for k, v in attrs}
            if (a.get("type") or "").lower() == "checkbox":
                self._emit("☑ " if "checked" in a else "☐ ")
        elif tag == "img":
            # 콘텐츠 이미지의 대체텍스트는 AI/검색 재료 — 아바타(작음)·추적
            # 픽셀(1px)은 width 로 거른다 (width 미지정 = 콘텐츠로 간주)
            a = {k.lower(): (v or "") for k, v in attrs}
            alt = a.get("alt", "").strip()
            try:
                w = int(a.get("width") or 9999)
            except ValueError:
                w = 9999
            if alt and w >= 48:
                self._emit(f"[그림: {alt}]")
        elif tag == "a":
            href = ""
            for k, v in attrs:
                if k == "href" and v:
                    href = v.strip()
            self._href.append(href)
            if href:
                self._emit("[")
        elif tag == "ul":
            self._list_stack.append(["ul"])
        elif tag == "ol":
            self._list_stack.append(["ol", 0])
        elif tag == "li":
            depth = max(0, len(self._list_stack) - 1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                self._list_stack[-1][1] += 1
                marker = f"{self._list_stack[-1][1]}. "
            else:
                marker = "- "
            self._emit("\n" + "  " * depth + marker)
        elif tag == "blockquote":
            self._emit("\n\n> ")
        elif tag == "hr":
            # 구분선 보존 — 섹션 나눔의 가독성 신호 (렌더러가 <hr> 로 복원.
            # 서명 절단 패턴은 정확히 '--' 라 '---' 는 안전)
            self._emit("\n\n---\n\n")
        elif tag == "table":
            if self._is_layout_table(attrs):
                self._tmode.append(True)    # 투명 — 셀 내용이 블록으로 흐른다
                self._emit("\n")
            else:
                self._tmode.append(False)
                # 바깥 표 상태를 저장하고 새로 시작 — 중첩 표 무손실
                self._tstack.append(dict(rows=self._rows, row=self._row,
                                         cell=self._cell,
                                         hdr=self._row_is_header))
                self._rows, self._row, self._cell = [], None, None
                self._row_is_header = False
        elif tag == "tr":
            if self._tmode and not self._tmode[-1]:
                self._row = []
                self._row_is_header = False
        elif tag in ("td", "th"):
            if self._tmode and not self._tmode[-1]:
                self._cell = []
                if tag == "th":
                    self._row_is_header = True
            else:
                self._emit("\n")            # 투명 표의 셀 = 블록 경계

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/> 등 자기완결 태그 — 인라인 강조 태그가 자기완결이면 무시(빈 강조)
        if tag in _INLINE_STYLE_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if self._hidden:
            if tag not in _VOID_HTML:
                self._hidden -= 1
            return
        if tag in _INLINE_STYLE_TAGS:
            if self._mark_stack:
                sink, idx, marks = self._mark_stack.pop()
                inner = "".join(sink[idx + len(marks):])
                if marks and inner.strip() == "":
                    del sink[idx:idx + len(marks)]      # 빈 강조 제거
                elif marks:
                    # 가장자리 공백을 마커 밖으로 — "<b>aaa </b>" 를 그대로
                    # 옮긴 "**aaa **" 는 유효한 마크다운이 아니라(CommonMark)
                    # 렌더러가 못 살린다. "**aaa** " 로 재배치.
                    lead = inner[:len(inner) - len(inner.lstrip())]
                    trail = inner[len(inner.rstrip()):]
                    del sink[idx:]
                    sink.append(lead)
                    sink.extend(marks)
                    sink.append(inner.strip())
                    sink.extend(reversed(marks))
                    sink.append(trail)
            return
        if tag == "pre":
            raw = "".join(self._pre_parts).strip("\n")
            self._in_pre = False
            if raw.strip():
                idx = len(self._pre_store)
                self._pre_store.append(raw)
                if self._cell is not None:        # 데이터 표 셀 안 — 한 줄 유지(인라인)
                    self._pre_inline.add(idx)
                    self._emit("\x01%d\x01" % idx)              # 주변 개행 없이
                else:
                    self._emit("\n\n\x01%d\x01\n\n" % idx)      # 표 밖 — 블록 펜스
            return
        if tag == "p" or tag in _HEADING:
            self._emit("\n\n")
        elif tag == "a":
            href = self._href.pop() if self._href else ""
            if href:
                self._emit(f"]({href})")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._emit("\n")
        elif tag in ("td", "th"):
            if self._tmode and not self._tmode[-1]:
                if self._cell is not None and self._row is not None:
                    # 셀 안 블록(문단/목록 항목) 경계를 ' · ' 로 보존
                    parts = [re.sub(r"\s+", " ", p).strip()
                             for p in "".join(self._cell).split("\n")]
                    cell = " · ".join(p for p in parts if p)
                    self._row.append(cell.replace("|", r"\|"))
                self._cell = None
            else:
                self._emit("\n")
        elif tag == "tr":
            if self._tmode and not self._tmode[-1]:
                if self._row is not None and self._rows is not None:
                    self._rows.append(self._row)
                    if self._row_is_header:
                        self._rows.append(["---"] * len(self._row))
                self._row = None
            else:
                self._emit("\n")
        elif tag == "table":
            if not self._tmode:
                return                      # 짝 안 맞는 </table>
            if self._tmode.pop():           # 투명 표 — 닫기만
                self._emit("\n")
                return
            rows, self._rows = self._rows, None
            st = self._tstack.pop()         # 바깥 표 상태 복원
            self._rows, self._row = st["rows"], st["row"]
            self._cell, self._row_is_header = st["cell"], st["hdr"]
            if not rows:
                return
            # GFM 유효성: 첫 행 뒤 구분행이 없으면(th 없는 Outlook 표 —
            # 붙여넣기 표의 전형) 삽입해 다운스트림 표 렌더가 인식하게 한다
            if (len(rows) < 2 or not all(c == "---" for c in rows[1])):
                rows.insert(1, ["---"] * len(rows[0]))
            if self._cell is not None:
                # 데이터 표 속 데이터 표(희귀) — 바깥 셀에 평탄화(무손실)
                flat = " ; ".join(" / ".join(r) for r in rows
                                  if not all(c == "---" for c in r))
                self._cell.append(flat)
            else:
                self._out.append("\n\n")
                for r in rows:
                    self._out.append("| " + " | ".join(r) + " |\n")
                self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip or self._hidden:
            return
        if self._in_pre:
            self._pre_parts.append(data)    # 코드 원문 — 공백 정리 없이 보관
            return
        text = data.replace("\xa0", " ").replace("​", "")
        if self._cell is not None:
            # 텍스트 노드의 개행(소스 프리티프린트)은 공백으로 — 셀 안 블록
            # 경계는 p/br/div/li 핸들러가 방출한 '\n' 만이다 (td 닫힘서 ' · ')
            text = re.sub(r"\s+", " ", text)
            if text:
                self._emit(text)
            return
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        self._emit(text)

    def result(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 목록 항목 사이 빈 줄 제거 — 소스 들여쓰기 개행이 항목을 쪼개
        # 렌더러가 목록을 분리 인식하는 것을 방지 (연속 항목만 병합)
        item = r"[ ]*(?:-|\d+\.)\s"
        for _ in range(4):
            new = re.sub(rf"(?m)^({item}[^\n]*)\n\n(?={item})", r"\1\n", text)
            if new == text:
                break
            text = new
        # <pre> 복원 — 표 밖은 코드펜스, 표 셀 안은 인라인(줄=<br>, 코드=백틱)으로
        # 해서 표 행이 한 줄로 유지되게 한다(멀티라인 펜스는 GFM 표 셀에 못 담음).
        def _restore_pre(m):
            idx = int(m.group(1))
            raw = self._pre_store[idx]
            if idx in self._pre_inline:
                segs = []
                for ln in raw.split("\n"):
                    ln = ln.replace("|", "\\|")           # 표 구분자 보호
                    segs.append("`" + ln + "`" if ln.strip() and "`" not in ln else ln)
                return "<br>".join(segs)
            return "```\n" + raw + "\n```"
        text = re.sub(r"\x01(\d+)\x01", _restore_pre, text)
        return text.strip()


def html_to_markdown(html: str) -> str:
    """HTML 메일 본문 → 마크다운. bold/italic/링크/표/목록/제목 보존.

    파싱이 실패하면 서식 없는 html_to_text 로 폴백해 텍스트만이라도 살린다.
    """
    if not html:
        return ""
    p = _MarkdownConverter()
    try:
        p.feed(html)
        p.close()
        out = p.result()
    except Exception:
        return html_to_text(html)
    return out or html_to_text(html)


# ------------------------------------------------------------ HTML 정제 (표시용)
#
# 이메일 HTML 은 적대적이다(추적 픽셀·스크립트·외부 CSS). 웹 UI 에서 렌더하기 전에
# 허용목록으로 정제한다: script/iframe/style/form/on* 제거, 원격 이미지 무력화,
# javascript: 링크 차단. 서버의 CSP 헤더와 이중 방어. 안전 우선(애매하면 제거).

_ALLOWED_TAGS = {
    "p", "div", "span", "br", "hr", "b", "strong", "i", "em", "u", "s",
    "strike", "del", "ins", "mark", "a", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "colgroup", "col", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "code", "img", "sub", "sup", "small", "big", "font", "center",
    "abbr", "cite", "q",
}
# 태그 + 자식 내용까지 통째로 버릴 것
_DROP_TREE = {
    "script", "style", "head", "title", "meta", "link", "iframe", "object",
    "embed", "noscript", "form", "input", "button", "select", "textarea",
    "svg", "math", "base", "applet",
}
_VOID = {"br", "hr", "img", "col"}
# HTML void(빈) 요소 전체 — 닫는 태그가 없다. _DROP_TREE 의 void(meta·link·base·
# input·embed)를 드롭 카운터로 세면 안 닫혀 이후 본문이 통째로 드롭되므로 제외한다.
_VOID_HTML = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}
_ATTR_ALLOW = {
    "*": {"title", "dir", "lang"},
    "a": {"name"},                       # href 는 별도 검증, target/rel 강제
    "img": {"alt", "width", "height"},   # src 는 별도(원격 차단)
    "td": {"colspan", "rowspan", "align", "valign"},
    "th": {"colspan", "rowspan", "align", "valign"},
    "table": {"border", "cellpadding", "cellspacing", "align", "width", "bgcolor"},
    "col": {"span", "width"},
    "font": {"color", "face", "size"},
    "ol": {"start", "type"},
    "div": {"align"},
    "p": {"align"},
}
_STYLE_BAD_RX = re.compile(
    r"url\s*\(|expression|javascript:|@import|behavior|-moz-binding|position\s*:")
_URL_OK_RX = re.compile(r"^(https?:|mailto:|tel:|#|/|\./|\.\./)", re.IGNORECASE)


def _attr_esc(v: str) -> str:
    return (v.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _data_esc(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_style(v: str) -> str:
    keep = []
    for decl in (v or "").split(";"):
        if decl.strip() and not _STYLE_BAD_RX.search(decl.lower()):
            keep.append(decl.strip())
    return "; ".join(keep)


def _safe_url(v: str) -> str | None:
    v = (v or "").strip()
    if not v or not _URL_OK_RX.match(v):
        return None
    return v


def _attr_get(attrs, key: str) -> str:
    for k, val in attrs:
        if k.lower() == key:
            return val or ""
    return ""


class _Sanitizer(HTMLParser):
    """허용목록 정제 + 인용 라벨 절단.

    인용 라벨("Original Message"/"원본 메시지" 류)을 만나면 그 지점부터 전부
    버리고, 이미 방출한 열린 태그를 역순으로 닫아 균형을 맞춘다. 라벨이 여러
    태그로 쪼개진 경우(예: "-----" 조각 span 뒤에 라벨 span)를 잡기 위해
    대시 전용 텍스트는 즉시 방출하지 않고 보류(_pend)했다가 — 라벨이 오면
    보류분을 폐기하고 절단, 실제 내용이 오면 보류분을 그대로 방출한다
    (라벨 없는 서명 구분선 "-----" 보존).
    """

    _PEND_MAX = 16  # 보류 이벤트 상한 — 병리적 문서에서 무한 보류 방지

    def __init__(self, preserve_quotes: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop = 0
        self._cut = False
        self._open: list[str] = []      # 방출된 열린 태그 (절단 시 닫기 균형용)
        self._pend: list[tuple] = []    # 보류 이벤트 (대시 조각 이후)
        # mid-join 보존 모드: 인용 라벨에서 버리는 대신 접힘(details)으로 감싸
        # 계속 정제한다 — 스레드 첫 보유 메일의 인용 체인은 DB 에 없는 유일본
        self._preserve = preserve_quotes
        self._preserving = False        # 폴드가 열렸나 (첫 라벨 이후)
        self._sep = False               # 인용 경계 태그(<hr>·border-top)를 봤나
        self._swallow: list[str] = []   # 폴드 진입에서 미리 닫은 태그(짝을 삼킨다)
        self._cand: dict | None = None  # 헤더 후보 판정 중(확정 전까지 전부 보류)

    def _flush(self) -> None:
        """보류분을 정상 방출 (라벨이 아니었음)."""
        for ev in self._pend:
            kind = ev[0]
            if kind in ("text", "raw"):
                self.out.append(ev[1])
            elif kind == "open":
                self.out.append(ev[2])
                self._open.append(ev[1])
            else:  # close
                self._emit_close(ev[1])
        self._pend.clear()

    def _emit_close(self, tag: str) -> None:
        """닫는 태그 방출 — 단, 폴드 진입에서 이미 닫아 둔 짝은 삼킨다.

        안 삼키면 폴드 안에 짝 없는 </div> 가 남고, 브라우저가 그걸 조상 div
        로 올려 닫으면서 <details> 를 스택에서 팝한다 → 인용이 접힘 밖으로
        새고 .mailhtml 스타일도 빠진다(2026-07-31 리뷰 실증)."""
        if tag not in self._open:
            # 열린 적 없는 닫는 태그 — 우리가 폴드 진입에서 닫았거나(_swallow)
            # 원문이 원래 짝이 안 맞거나. 어느 쪽이든 방출하면 브라우저가
            # 조상까지 닫으며 <details> 를 팝해 인용이 접힘 밖으로 샌다.
            if tag in self._swallow:
                self._swallow.remove(tag)
            return
        self.out.append(f"</{tag}>")
        self._pop_open(tag)

    def _pop_open(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i] == tag:
                del self._open[i]
                return

    # 꼬리의 빈 껍데기 한 겹 — 뒤에 닫는 태그만 남았을 때만 지운다(중첩은 반복).
    _EMPTY_TAIL_RX = re.compile(
        r"\s*<(div|p|span|font|blockquote)[^>]*>\s*</\1>\s*"
        r"(?=(?:</(?:div|p|span|font|blockquote)>\s*)*$)", re.IGNORECASE)
    _TRAIL_HR_RX = re.compile(r"\s*<hr\s*/?>\s*"
                              r"(?=(?:</(?:div|p|span|font|blockquote)>\s*)*$)",
                              re.IGNORECASE)

    def _trim_trailing(self) -> None:
        """절단 자리에 남은 경계 태그·빈 컨테이너를 걷어낸다.

        경계 태그(<hr>·테두리 div)는 인용 '앞'에 있으므로 뒤만 버리면 그대로
        남는다 — 본문 끝에 아무것도 없는 구분선이 그어져 "아래 뭔가 있는데
        안 보인다"로 읽힌다(2026-07-31 리뷰: 데모 196건 중 고아 hr 31건)."""
        joined = "".join(self.out)
        for _ in range(12):              # 중첩 빈 컨테이너를 겹겹이 벗긴다
            trimmed = self._TRAIL_HR_RX.sub("", self._EMPTY_TAIL_RX.sub("", joined))
            if trimmed == joined:
                break
            joined = trimmed
        self.out = [joined]

    def _do_cut(self) -> None:
        """인용 라벨 확정 — 보류분 폐기, 열린 태그 닫기, 이후 전부 무시."""
        self._pend.clear()
        for tag in reversed(self._open):
            self.out.append(f"</{tag}>")
        self._open.clear()
        self._trim_trailing()        # 닫은 **뒤에** 정리해야 빈 컨테이너가 남지 않는다
        self._cut = True

    def _begin_preserve(self) -> None:
        """보존 모드의 절단 지점 — 버리는 대신 접힘 컨테이너를 연다.

        열린 태그를 닫아 균형을 맞춘 뒤 폴드를 열고, 보류분(대시 구분선)은
        폴드 안 첫 내용으로 이월한다. 두 번째 이후 라벨(체인 속 중첩 인용)은
        no-op — 폴드는 메일당 하나, 중첩 라벨은 내용으로 흐른다.
        """
        if self._preserving:
            return
        pend, self._pend = self._pend, []
        for tag in reversed(self._open):
            self.out.append(f"</{tag}>")
        # 강제로 닫은 수만큼, 원문에서 나중에 도착할 대응 닫는 태그를 삼킨다.
        # 안 삼키면 폴드 안에 짝 없는 </div> 가 남아 브라우저가 <details> 를
        # 스택에서 팝해 인용이 접힘 밖으로 새고 .mailhtml 스타일도 빠진다
        # (2026-07-31 리뷰 실증).
        self._swallow = list(self._open)   # 나중에 올 대응 닫는 태그를 삼킨다
        self._open.clear()
        self.out.append(QFOLD_OPEN)
        self._preserving = True
        self._pend = pend
        self._flush()                    # 구분선 등 보류분을 폴드 안으로

    def _hold(self, ev: tuple) -> None:
        self._pend.append(ev)
        # 후보 판정 중에는 상한으로 흘려보내지 않는다 — 흘리면 되돌릴 수 없다.
        # 보류량은 문서 크기로만 묶인다(내용 유실 없음, 메모리 O(문서)).
        if self._cand is None and len(self._pend) > self._PEND_MAX:
            self._flush()

    def _pend_has_sep(self) -> bool:
        """구분선을 봤나 — Outlook 헤더 블록 앞의 경계.

        텍스트 '________' 뿐 아니라 **태그로만 오는 구분선**도 센다: 클래식
        Outlook 은 `<div style='border-top:solid #E1E1E1 1.0pt'>`, OWA·모바일은
        `<hr>`, Mac 은 전용 컨테이너 클래스라 텍스트 청크가 아예 없다 — 그래서
        실기기 답장이 HTML 경로에서 하나도 안 잘렸다(2026-07-31 리뷰 실증:
        데모 282통 중 67통에 인용 체인 잔존)."""
        return self._sep or any(
            ev[0] == "text" and _DASH_ONLY_RX.match(ev[1].strip())
            for ev in self._pend)

    def _mark_sep(self, tag: str, attrs) -> None:
        """인용 경계 태그면 표시 — <hr> · border-top 스타일 · 클라이언트 전용 컨테이너."""
        if tag == "hr":
            self._sep = True
            return
        blob = " ".join(f"{k}={v}" for k, v in (attrs or []) if v).lower()
        if "border-top" in blob.replace(" ", "") or _QUOTE_CONTAINER_RX.search(blob):
            self._sep = True

    def _attrs(self, tag: str, attrs) -> str:
        allow = _ATTR_ALLOW.get("*", set()) | _ATTR_ALLOW.get(tag, set())
        parts: list[str] = []
        for k, v in attrs:
            k = k.lower()
            if k.startswith("on") or k in ("target", "rel", "src", "href"):
                continue
            if k == "style":
                sv = _safe_style(v or "")
                if sv:
                    parts.append(f' style="{_attr_esc(sv)}"')
                continue
            if k in allow:
                parts.append(f' {k}="{_attr_esc(v or "")}"')
        if tag == "a":
            href = _safe_url(_attr_get(attrs, "href"))
            if href:
                parts.append(f' href="{_attr_esc(href)}"')
            parts.append(' target="_blank" rel="noopener noreferrer"')
        if tag == "table":
            # 작성자가 "이 표는 조판용"이라고 선언한 것만 남긴다 — 우리 판정을
            # 저장하는 게 아니라 원문의 선언을 지우지 않는 것이라 오탐 개념이
            # 없다. 값은 우리가 정한다(임의 ARIA role 이 우리 접근성 트리에
            # 섞이지 않게). 화면에서는 CSS 가 이걸 보고 테두리를 뺀다.
            if _attr_get(attrs, "role").strip().lower() in ("presentation", "none"):
                parts.append(' role="presentation"')
        if tag == "img":
            src = _attr_get(attrs, "src").strip()
            if src.lower().startswith("data:image/"):
                parts.append(f' src="{_attr_esc(src)}"')
            elif src:
                # 원격 이미지 = 추적 픽셀 → 무력화. 원본 URL 은 보존한다 —
                # 스레드 화면의 [위험을 감수하고 보기]가 그 화면에서만 src 로
                # 되돌리고, 로드는 브라우저가 한다(web.show_remote_images).
                parts.append(f' data-blocked-src="{_attr_esc(src)}"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._cut:
            return
        self._mark_sep(tag, attrs)
        if tag in _DROP_TREE:
            # svg 는 사용자가 볼 다이어그램일 수 있다 — 무흔적 삭제는 "이미지가
            # 안 나온다"는 문의로 돌아온다(2026-08-15 실사용 보고). 중복 cid 생략과
            # 같은 한 줄 흔적을 남긴다(script/style 은 쓰레기라 종전대로 무흔적).
            if tag == "svg" and not self._drop and not self._cut:
                self.out.append("<span class='imgnote-inline'>"
                                "🖼 다이어그램 생략(SVG) — 원문은 Outlook에서</span>")
            # void(빈) 태그는 닫는 태그가 없다 → 카운터를 세면 안 내려가 이후 본문이
            # 전부 드롭된다(<meta>/<link> 하나로 body_html 이 통째로 비던 버그 수정).
            if tag not in _VOID_HTML:
                self._drop += 1
            return
        if self._drop or tag not in _ALLOWED_TAGS:
            return
        close = " /" if tag in _VOID else ""
        rendered = f"<{tag}{self._attrs(tag, attrs)}{close}>"
        if tag in _VOID:
            if self._pend:
                self._hold(("raw", rendered))
            else:
                self.out.append(rendered)
            return
        if self._pend:
            self._hold(("open", tag, rendered))
        else:
            self.out.append(rendered)
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if self._cut:
            return
        self._mark_sep(tag, attrs)       # <hr /> 형태
        if tag in _DROP_TREE or self._drop or tag not in _ALLOWED_TAGS:
            return
        rendered = f"<{tag}{self._attrs(tag, attrs)} />"
        if self._pend:
            self._hold(("raw", rendered))
        else:
            self.out.append(rendered)

    def handle_endtag(self, tag: str) -> None:
        if self._cut:
            return
        if tag in _DROP_TREE:
            # 시작 태그와 대칭: void(닫는 태그 없는) 드롭 태그는 카운터를 안 건드린다.
            # 안 그러면 드롭 서브트리 속 stray </meta></link> 가 드롭을 조기 종료해 유출.
            if tag not in _VOID_HTML and self._drop:
                self._drop -= 1
            return
        if self._drop or tag not in _ALLOWED_TAGS or tag in _VOID:
            return
        if self._pend:
            self._hold(("close", tag))
        else:
            self._emit_close(tag)

    def handle_data(self, data: str) -> None:
        if self._cut or self._drop:
            return
        # convert_charrefs=True 가 &nbsp; 를 \xa0 로 만들므로 판정 전에 정규화
        s = data.replace("\xa0", " ").strip()
        if s and _HTML_CUT_RX.match(s) and (
                self._pend or sum(1 for ch in s if ch in "-—_=*") >= 2):
            # 라벨 단독 청크는 대시 동반 또는 보류(대시 조각 선행) 시에만 절단.
            # 보존 모드면 폴드를 열고 라벨부터 내용으로 계속 방출한다.
            if not self._preserve:
                self._do_cut()
                return
            self._begin_preserve()
        # 라벨 없는 한국어 Outlook 답장: "________" 구분선 뒤 "보낸 사람:" 헤더.
        # 구분선을 보류(_pend)한 상태에서 헤더 시작 청크가 오면 인용 시작으로 절단.
        # (텍스트 경로 _find_cut 의 _HDR_FIRST 판정을 HTML 경로에 맞춰 재현)
        #
        # **약한 FIRST(De/Van/Da/Von)는 여기서 제외한다.** 이 경로엔 2줄 FOLLOW
        # 게이트가 없어 라벨 하나로 뒤를 전부 버리는데, 저장된 HTML 은 재절단
        # 대상도 백업 대상도 아니라 오탐이 곧 영구 손실이다(2026-07-31 리뷰
        # 실증: "Da: 부산항" 한 줄에 견적 본문 전량 폐기). 해당 언어 메일도
        # AI 입력이 되는 new_content 는 텍스트 경로에서 정상 절단된다 —
        # 여기서 잃는 것은 화면의 '인용 접기' 뿐이다.
        elif (s and self._cand is None        # **이미 후보면 다시 열지 않는다**
                and _HDR_FIRST.match(s) and self._pend_has_sep()):
            # 후보 상태로 들어간다 — 여기서 바로 자르면 라벨 한 줄이 뒤를 전부
            # 버리는데(공문 "발신:" 한 줄에 본문 전량 폐기 실증) 저장된 HTML 은
            # 재절단 대상도 백업 대상도 아니라 오탐이 곧 영구 손실이다.
            # 후보를 겹쳐 열면 만료 카운터가 0 으로 되돌아 창이 무한 연장되고,
            # 그 사이 보류된 정상 본문이 뒤늦은 절단에 통째로 딸려 갔다(S1).
            self._cand = {"hits": 0, "chunks": 0}
        if self._cand is not None and s:
            self._cand["chunks"] += 1
            if _HDR_FOLLOW.match(s):
                self._cand["hits"] += 1
                # 텍스트 경로와 같은 증거(필드 3개) — 되돌릴 수 없는 경로가
                # 더 공격적이면 안 된다(2026-07-31 리뷰).
                if self._cand["hits"] >= 3:      # 확정 — 진짜 헤더 블록
                    self._cand = None
                    if not self._preserve:
                        self._do_cut()
                        return
                    self._begin_preserve()
            elif self._cand["chunks"] > 14:      # 헤더가 아니었다 — 되돌린다
                self._cand = None
                self._flush()
        esc = _data_esc(data)
        if self._cand is not None:
            self._hold(("text", esc))        # 후보 판정 전까지는 전부 보류
            return
        if s and len(s) >= 2 and _DASH_ONLY_RX.match(s):
            self._hold(("text", esc))
            return
        if self._pend:
            if not s:
                self._hold(("text", esc))   # 공백 청크는 보류 유지 (순서 보존)
                return
            self._flush()                    # 실제 내용 — 라벨이 아니었다
        if s:
            self._sep = False                # 내용이 나왔다 — 구분선 신호 소멸
        self.out.append(esc)

    def close(self) -> None:
        super().close()
        if self._cand is not None:       # 확정 못 한 후보 = 헤더가 아니었다
            self._cand = None
        if not self._cut:
            self._flush()   # 라벨 없이 끝남 — 보류분(서명 구분선 등) 보존
        if self._preserving:
            # 원본이 태그를 안 닫고 끝나도 폴드는 균형 있게 닫는다
            for tag in reversed(self._open):
                self.out.append(f"</{tag}>")
            self._open.clear()
            self.out.append(QFOLD_CLOSE)


# ─────────────────────────────────────── 다크 모드 색 보정 (2026-07-26)
# 메일은 흰 배경을 전제로 색을 고른다. 본문이 놓이는 다크 배경은 body 의
# --bg(#16181b — #right·.msg 엔 배경 선언이 없다)인데, 거기 그대로 얹으면 Outlook
# 기본 강조색이 전부 WCAG AA(4.5:1) 미달이라(빨강 2.75:1 · 파랑 3.45:1 · 보라
# 2.22:1) 예전엔 .mailhtml * 를 통째로 평탄화했다 — 읽히기는 하나 작성자가 준
# 강조가 전부 사라진다.
#
# 여기서는 색상(H)은 두고 명도(L)만 끌어올린 값을 --dk 로 **함께** 실어 보내고
# 어느 쪽을 쓸지는 CSS 가 고른다. 서버가 한쪽만 구워 보내면 안 되는 이유:
# 테마 토글이 클라이언트에서 즉시 일어나(data-theme 교체) 다시 받아오지 않는다.
#
# 저장이 아니라 **렌더 시점**에 붙인다 — 저장 시점에 구우면 이미 저장된 메일은
# 재수집해야 색이 살아나고, 그건 이 프로젝트가 금지하는 재수집 강요다.
_DK_LO, _DK_HI = 0.70, 0.92     # 명도 하한/상한 — 하한 0.70 이면 흔한 강조색이 5:1 이상
_DK_SMAX = 0.75                 # 채도 상한 — 다크에서 형광색처럼 튀지 않게
_DK_ACHROMA = 0.08              # 이 아래는 무채색(검정·회색) → 본문색에 맡긴다

# Word/Outlook 이 실제로 뱉는 이름 색만. 못 알아보는 이름은 그냥 건너뛴다.
_DK_NAMED = {
    "red": "#ff0000", "blue": "#0000ff", "green": "#008000", "purple": "#800080",
    "orange": "#ffa500", "navy": "#000080", "maroon": "#800000", "teal": "#008080",
    "olive": "#808000", "fuchsia": "#ff00ff", "aqua": "#00ffff", "lime": "#00ff00",
    "silver": "#c0c0c0", "gray": "#808080", "grey": "#808080",
    "black": "#000000", "white": "#ffffff",
}
_DK_RGB_RX = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", re.I)
# style 안의 color 선언 — background-color 등에 걸리지 않게 앞 경계를 본다
_DK_DECL_RX = re.compile(
    r"(?i)(?<![-\w])color\s*:\s*(#[0-9a-f]{3,8}|rgba?\([^)]*\)|[a-z]+)")
# 인라인 !important — CSS 캐스케이드에서 인라인의 !important 는 시트의
# !important 를 항상 이기므로, 남겨 두면 다크 평탄화도 --dk 규칙도 진다
_DK_IMPORTANT_RX = re.compile(r"(?i)\s*!\s*important\b")
_DK_STYLE_RX = re.compile(r'(?i)style="([^"]*)"')
_DK_FONT_RX = re.compile(r"(?i)<font\b([^>]*)>")
_DK_ATTR_RX = re.compile(r'(?i)\b(color|style)\s*=\s*"([^"]*)"')


def _dk_rgb(v: str):
    v = (v or "").strip().lower()
    v = _DK_NAMED.get(v, v)
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8):
            try:
                return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
        return None
    m = _DK_RGB_RX.match(v)
    if m:
        try:
            return tuple(min(255, max(0, round(float(x)))) for x in m.groups())
        except ValueError:
            return None
    return None


def dark_variant(v: str) -> str | None:
    """작성자 색 → 다크 배경에서 읽히는 색. 무채색이면 None(본문색에 맡김)."""
    rgb = _dk_rgb(v)
    if not rgb:
        return None
    r, g, b = [c / 255 for c in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if s < _DK_ACHROMA:
        return None
    nl = min(_DK_HI, max(_DK_LO, 1.0 - l if l < 0.5 else l))
    return "#%02x%02x%02x" % tuple(
        round(c * 255) for c in colorsys.hls_to_rgb(h, nl, min(s, _DK_SMAX)))


def _dk_font_to_style(html: str) -> str:
    """<font color=X> 의 색을 style 로 옮긴다 — --dk 주입은 다음 단계가 한 번에.
    style 에 이미 color 가 있으면 그쪽이 이기므로 건드리지 않는다."""
    def one(m):
        attrs = m.group(1)
        found = {k.lower(): v for k, v in _DK_ATTR_RX.findall(attrs)}
        col = found.get("color", "").strip()
        style = found.get("style", "")
        if not col or _DK_DECL_RX.search(style):
            return m.group(0)
        merged = (style.rstrip("; ") + "; " if style.strip() else "") + f"color:{col}"
        if "style" in found:
            return "<font" + _DK_ATTR_RX.sub(
                lambda a: (f'style="{merged}"' if a.group(1).lower() == "style"
                           else a.group(0)), attrs) + ">"
        return f'<font{attrs} style="{merged}">'
    return _DK_FONT_RX.sub(one, html)


def add_dark_colors(html: str) -> str:
    """style 의 color 마다 다크용 --dk 를 덧붙이고, 인라인 !important 를 걷어낸다.

    !important 제거 이유(2026-07-27 실측): 인라인 스타일의 !important 는 시트의
    !important 를 항상 이기므로 `color:black!important`(Confluence·뉴스레터형
    표 템플릿에 흔함)가 다크 평탄화를 뚫고 다크 배경 위 순수 검정으로 남는다.
    메일 색과 경쟁하는 시트 규칙은 다크 모드에만 있어(라이트는 원본 그대로
    두는 설계) 제거해도 라이트 표시는 바뀌지 않는다.

    이미 --dk 가 있으면 건너뛴다 — 두 번 돌려도 같은 결과(멱등)."""
    if not html:
        return ""
    low = html.lower()
    if "color" not in low and "important" not in low:
        return html

    def one_style(m):
        sv = _DK_IMPORTANT_RX.sub("", m.group(1))
        if "--dk" in sv:
            return m.group(0)
        c = _DK_DECL_RX.search(sv)
        dk = dark_variant(c.group(1)) if c else None
        if dk:
            return f'style="{sv.rstrip("; ")}; --dk:{dk}"'
        return m.group(0) if sv == m.group(1) else f'style="{sv}"'

    return _DK_STYLE_RX.sub(one_style, _dk_font_to_style(html))


def sanitize_html(html: str, preserve_quotes: bool = False) -> str:
    """이메일 HTML → 웹 UI 표시용 안전 HTML (허용목록·원격이미지 차단).

    preserve_quotes: 인용 라벨에서 절단하는 대신 details 접힘(QFOLD)으로 감싸
    보존 — 스레드의 첫 보유 메일(mid-join)은 인용 체인이 유일본이다.
    """
    if not html:
        return ""
    p = _Sanitizer(preserve_quotes=preserve_quotes)
    try:
        p.feed(html)
        p.close()
    except Exception:
        return "<pre>" + _data_esc(html_to_text(html)) + "</pre>"
    return "".join(p.out).strip()


# ------------------------------------------------- 이미지 서명 숨김 (표시 시점)
# 본문 꼬리의 '이미지 블록 서명'(로고·명함 카드)을 "Signature 숨김" 한 줄로
# 대체한다. 아주 좁은 AND 조건만 — 콘텐츠 이미지(차트·파형)와 확실히 구분:
#   ① data:image/png;base64 로 임베드된 이미지 (원격/차단 이미지는 대상 아님)
#   ② 선언된 height ≤ 210px (height 속성 또는 style:height — 미선언은 대상 아님)
#   ③ 본문 뒤쪽 경계 — 그 뒤로 실질 텍스트/콘텐츠 이미지가 없음(테두리 table
#      로 감싸였으면 그 table 째 제거). 앞에 실질 본문이 있을 때만(이미지 단독
#      메일은 손대지 않음 — 접으면 볼 게 없어진다).
_SIG_PNG_PREFIX = "data:image/png;base64,"
_SIG_MAX_H = 210
_SIG_NOTE = "<div class='sighide'>Signature 숨김</div>"
_STYLE_HEIGHT_RX = re.compile(r"height\s*:\s*(\d+)")


class _SigImageHider(HTMLParser):
    """정제된 HTML 을 faithful 재직렬화하며, 꼬리의 서명 이미지 블록을 찾는다.

    '실질 콘텐츠'(비공백 텍스트 또는 서명 아닌 이미지)를 만날 때마다 절단
    후보 지점을 그 뒤로 옮긴다. 끝까지 갔을 때 마지막 실질 콘텐츠 이후가
    서명 이미지뿐이면, 그 지점에서 잘라 열린 태그를 닫고 노트를 붙인다
    (_Sanitizer 의 인용 절단과 같은 기법)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []       # 현재 열린 태그
        self.cut_len: int | None = None  # 마지막 실질 콘텐츠 직후의 out 길이
        self.cut_stack: list[str] = []   # 그 시점의 열린 태그 스냅샷
        self.has_content = False
        self.tail_sig = False            # 마지막 실질 콘텐츠 이후 서명 이미지 존재

    def _ser_attrs(self, attrs) -> str:
        parts = []
        for k, v in attrs:
            if v is None:
                parts.append(f" {k}")
            else:
                parts.append(f' {k}="{_attr_esc(v)}"')
        return "".join(parts)

    def _mark_content(self) -> None:
        self.has_content = True
        self.cut_len = len(self.out)
        self.cut_stack = list(self.stack)
        self.tail_sig = False

    def _is_sig_img(self, attrs) -> bool:
        a = {k.lower(): (v or "") for k, v in attrs}
        if not a.get("src", "").startswith(_SIG_PNG_PREFIX):
            return False
        h = None
        if a.get("height", "").strip().isdigit():
            h = int(a["height"].strip())
        else:
            m = _STYLE_HEIGHT_RX.search(a.get("style", ""))
            if m:
                h = int(m.group(1))
        return h is not None and h <= _SIG_MAX_H

    def _img(self, tag, attrs, void: str) -> None:
        self.out.append(f"<{tag}{self._ser_attrs(attrs)}{void}>")
        if self._is_sig_img(attrs):
            self.tail_sig = True
        else:
            self._mark_content()          # 콘텐츠 이미지 = 블로킹

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "img":
            self._img(tag, attrs, "")
            return
        self.out.append(f"<{tag}{self._ser_attrs(attrs)}>")
        if tag not in _VOID_HTML:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs) -> None:
        if tag == "img":
            self._img(tag, attrs, " /")
            return
        self.out.append(f"<{tag}{self._ser_attrs(attrs)} />")

    def handle_endtag(self, tag) -> None:
        self.out.append(f"</{tag}>")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i]
                break

    def handle_data(self, data) -> None:
        self.out.append(_data_esc(data))
        if data.replace("\xa0", " ").strip():
            self._mark_content()

    def result(self) -> str | None:
        if not (self.has_content and self.tail_sig and self.cut_len is not None):
            return None
        kept = self.out[:self.cut_len]
        closes = [f"</{t}>" for t in reversed(self.cut_stack)]
        return "".join(kept) + "".join(closes) + _SIG_NOTE


def hide_image_signatures(html: str) -> str:
    """꼬리 이미지 서명(임베드 PNG·height≤210)을 "Signature 숨김" 으로 대체.

    조건 미충족이면 입력을 그대로 반환(무변경). mid-join 인용 접기(qfold)가
    있는 메일은 안전하게 건너뛴다(접힘 구조를 절단하지 않도록)."""
    if (not html or _SIG_PNG_PREFIX not in html
            or "class='qfold'" in html or 'class="qfold"' in html):
        return html
    p = _SigImageHider()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return html
    return p.result() or html


# ---------------------------------------------------------- 인용문 절단 지점

# mid-join 보존 (docs/ARCHITECTURE.md §6.1): 스레드의 첫 보유 메일은 인용 체인이
# 내 사서함에 없는 유일본이라 절단하지 않고 남긴다. 두 층의 경계 표식 —
#  - 텍스트(new_content): PRESERVED_MARK 한 줄. FTS·AI·검색은 전문을 보고,
#    신호 정규식 등 '신규 작성분'만 봐야 하는 소비자는 strip_preserved() 사용.
#  - HTML(message_html): QFOLD(details 접힘, 기본 닫힘). 프룬되면 웹 렌더러가
#    PRESERVED_MARK 를 같은 접힘으로 재현한다 (저장 증가 없이 렌더링만).
PRESERVED_MARK = "--- 이전 대화 (인용 보존) ---"
QFOLD_OPEN = ("<details class='qfold'><summary>이전 대화 (인용 보존)</summary>"
              "<div class='qbody'>")
QFOLD_CLOSE = "</div></details>"


def strip_preserved(text: str) -> str:
    """보존 인용 블록을 뗀 '신규 작성분'만 반환 — 신호/요청 판정용."""
    i = (text or "").find(PRESERVED_MARK)
    return text if i < 0 else text[:i].rstrip()


def take_preserved(text: str) -> str:
    """보존 인용 블록만 반환 — 없으면 ''. strip_preserved 의 짝."""
    i = (text or "").find(PRESERVED_MARK)
    return "" if i < 0 else text[i + len(PRESERVED_MARK):].strip()


# ── 보존 인용을 '대화 턴'으로 (2026-08-06) ────────────────────────────────
# 왜: mid-join 스레드는 **DB 메시지 수보다 실제 대화가 길다.** 내가 수신자가
# 아니었던 앞부분이 메시지 행 없이 인용 안에만 있기 때문이다(실측: 스레드 3통,
# 대화 5턴). 그런데 화면은 그것을 '이전 대화 (인용 보존)' 한 줄로 접어, 몇 턴이
# 누구 사이에 오갔는지도 보이지 않는다 — 이 도구는 그 앞부분을 갖고 있으면서
# 사용자에게 안 보여 주고 있었다.
#
# 파싱은 헤더가 이미 구조라서 가능하다: 수집 때 HTML→마크다운 변환이
# `**보낸 사람:** / **보낸 날짜:** / **제목:**` 를 남긴다(텍스트 원본·영문
# Outlook 형식도 같이 받는다). **실패하면 빈 리스트**를 돌려 호출측이 지금
# 화면을 그대로 쓰게 한다 — 파싱 실패가 사용자에게 보이지 않아야 한다.
def _hdr_rx(*labels: str) -> re.Pattern:
    """`**보낸 사람:** 값` / `From: 값` 둘 다 받는다 — 굵게 표시가 콜론을 감싼다."""
    alt = "|".join(labels)
    return re.compile(rf"^\*{{0,2}}\s*(?:{alt})\s*\*{{0,2}}\s*:\s*\*{{0,2}}\s*(.+)$")


_PRES_HDR = {
    "who": _hdr_rx(r"보낸\s*사람", "발신자", "From"),
    "when": _hdr_rx(r"보낸\s*날짜", r"보낸\s*시간", "Sent", "Date"),
    "subject": _hdr_rx("제목", "Subject"),
    "to": _hdr_rx(r"받는\s*사람", "수신자", "To", "Cc", "참조"),
}
_PRES_ADDR_RX = re.compile(r"<([^<>@\s]+@[^<>\s]+)>|\[mailto:([^\]\s]+)\]")
# 2026년 08월 01일 Saturday PM 02:00 / 2026-08-01 14:00 / 2026/8/1 오후 2:00
_PRES_DATE_RX = re.compile(r"(\d{4})\s*[년\-/.]\s*(\d{1,2})\s*[월\-/.]\s*(\d{1,2})")
# 오전/오후 표시는 앞(한국어·Outlook 한글)에도 뒤(영문 `2:00 PM`)에도 온다.
_PRES_TIME_RX = re.compile(r"(AM|PM|오전|오후)?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?",
                           re.IGNORECASE)


def _pres_when(raw: str) -> str:
    """보낸 날짜 문자열 → 'YYYY-MM-DD HH:MM' (시각 못 읽으면 날짜만, 아니면 '')."""
    d = _PRES_DATE_RX.search(raw or "")
    if not d:
        return ""
    out = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
    t = _PRES_TIME_RX.search(raw[d.end():])
    if not t:
        return out
    hh, mm = int(t.group(2)), int(t.group(3))
    ap = (t.group(1) or t.group(4) or "").upper()
    if ap in ("PM", "오후") and hh < 12:
        hh += 12
    elif ap in ("AM", "오전") and hh == 12:
        hh = 0
    return f"{out} {hh:02d}:{mm:02d}" if 0 <= hh < 24 else out


def parse_preserved(text: str) -> list[dict]:
    """보존 인용 블록을 대화 턴 목록으로 — 오래된 것부터.

    각 턴은 {"who", "addr", "when", "subject", "body"}. 인용 블록이 없거나
    헤더를 못 찾으면 **빈 리스트**(호출측은 원문 표시로 폴백한다).
    """
    block = take_preserved(text)
    if not block:
        return []
    lines = block.splitlines()
    starts = [i for i, ln in enumerate(lines) if _PRES_HDR["who"].match(ln.strip())]
    if not starts:
        return []
    turns = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        seg = lines[i:end]
        got = {"who": "", "addr": "", "when": "", "subject": ""}
        body_at = 0
        for j, ln in enumerate(seg):
            s = ln.strip()
            if not s:                       # 빈 줄 = 헤더 끝
                if got["who"]:
                    body_at = j + 1
                    break
                continue
            hit = False
            for key, rx in _PRES_HDR.items():
                m = rx.match(s)
                if not m:
                    continue
                hit = True
                if key == "to":
                    break                   # 수신자는 턴 표시에 안 쓴다
                val = m.group(1).strip().strip("*").strip()
                if key == "who":
                    a = _PRES_ADDR_RX.search(val)
                    got["addr"] = (a.group(1) or a.group(2) or "").lower() if a else ""
                    got["who"] = _PRES_ADDR_RX.sub("", val).strip().strip("\"'<>") or got["addr"]
                elif key == "when":
                    got["when"] = _pres_when(val)
                else:
                    got["subject"] = val
                break
            if not hit and got["who"]:      # 헤더가 아닌 줄 = 본문 시작
                body_at = j
                break
        body = _strip_signature([ln for ln in seg[body_at:]])
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        turns.append({**got, "body": "\n".join(body).strip()})
    # 인용은 보통 최신이 위다. 시각을 읽었으면 그걸로, 아니면 역순으로 뒤집는다.
    if all(t["when"] for t in turns):
        turns.sort(key=lambda t: t["when"])
    else:
        turns.reverse()
    return turns


def preserved_label(turns: list[dict]) -> str:
    """접힘 머리줄 — '앞선 대화 2턴 — 강미래 → 오태양 · 07-31 ~ 08-01'.

    턴을 못 읽었으면 '' (호출측이 기존 문구를 쓴다)."""
    if not turns:
        return ""
    who: list[str] = []
    for t in turns:
        name = (t["who"] or "").strip()
        if name and (not who or who[-1] != name):
            who.append(name)
    flow = " → ".join(who[:3]) + (" 외" if len(who) > 3 else "")
    days = [t["when"][5:10] for t in turns if t["when"]]
    span = ""
    if days:
        span = f" · {days[0]}" + (f" ~ {days[-1]}" if days[-1] != days[0] else "")
    head = f"앞선 대화 {len(turns)}턴"
    return head + (f" — {flow}" if flow else "") + span


def qfold_open(label: str = "") -> str:
    """인용 접힘 여는 태그 — label 이 있으면 그 문구를 머리줄로 쓴다."""
    if not label:
        return QFOLD_OPEN
    return (f"<details class='qfold'><summary>{_data_esc(label)}</summary>"
            "<div class='qbody'>")


_QFOLD_SUMMARY_RX = re.compile(
    r"(<details[^>]*class=['\"]qfold['\"][^>]*>\s*<summary[^>]*>)(.*?)(</summary>)",
    re.S | re.I)


def retitle_qfold(html: str, label: str) -> str:
    """저장된 HTML 의 인용 접힘 머리줄을 교체 — **렌더 시점**에만 손댄다.

    접힘은 수집 때 `message_html` 에 구워져 있어서 상수를 고쳐도 기존 메일은
    안 바뀐다. 재수집을 강요하지 않는 것이 이 저장소의 불변식이라(#5), 저장분은
    두고 그릴 때 갈아 끼운다 — `hide_image_signatures`·`add_dark_colors` 와 같은 자리다.
    """
    if not label or not html or "qfold" not in html:
        return html
    return _QFOLD_SUMMARY_RX.sub(
        lambda m: m.group(1) + _data_esc(label) + m.group(3), html, count=1)


# 문장 경계 — 종결부호 뒤 공백 · 한국어 종결어미 뒤 줄바꿈 · 빈 줄.
# 한국어 업무 메일은 마침표를 자주 생략하므로 '다/요 + 개행'을 함께 본다.
_SENT_BOUND = re.compile(r"(?<=[.!?])\s+|(?<=[다요])\s*\n|\n\s*\n")


def quote_context(body: str, quote: str, pad: int = 120):
    """인용의 **원문 앞뒤**를 떼어 준다 — `(pre, quote, post)` 또는 None.

    왜: 인용이 조각이면 조건·전제·후속이 안 보여 사용자가 메일을 다시 연다
    (2026-08-03 사용자 지적). 그런데 인용은 이미 원문의 연속 부분 문자열임이
    검증된 값이라(ask._quote_ok), **위치를 찾아 앞뒤를 그대로 복사**하면 된다 —
    모델이 문장을 더 만들지 않으므로 환각 위험이 0이다.

    대조는 공백을 전부 지운 형태로 하므로(모델이 줄바꿈을 흘린다) 정규화
    인덱스 → 원문 인덱스 대응표를 만들어 위치를 되찾는다.

    확장은 **문장 경계로 다듬되 방향이 중요하다** — 앞은 pad 안의 *가장 이른*
    경계부터, 뒤는 *가장 늦은* 경계까지다. 반대로 하면(가장 가까운 경계) 9자만
    늘어나 아무 쓸모가 없다(프로토타입에서 확인).
    """
    body, quote = body or "", quote or ""
    if not body or not quote:
        return None
    idx, buf = [], []
    for i, ch in enumerate(body):
        if not ch.isspace():
            buf.append(ch)
            idx.append(i)
    p = "".join(buf).find(re.sub(r"\s+", "", quote))
    if p < 0 or not idx:
        return None
    s, e = idx[p], idx[p + len(re.sub(r"\s+", "", quote)) - 1] + 1
    # 인용 끝의 종결부호는 인용에 붙인다 — 「…합니다」 뒤에 마침표만 따로 뜨는 것 방지
    while e < len(body) and body[e] in ".!?":
        e += 1
    if pad > 0:
        m = _SENT_BOUND.search(body, max(0, s - pad), s)
        ls = m.end() if m else max(0, s - pad)
        cap = min(len(body), e + pad)
        # start() > e 로 거른다 — 위에서 인용에 붙인 종결부호가 바로 뒤 경계로
        # 잡혀 꼬리가 0자가 되는 것을 막는다(실측에서 확인).
        ms = [x for x in _SENT_BOUND.finditer(body, e, cap) if x.start() > e]
        le = (ms[-1].start() + (1 if body[ms[-1].start()] in ".!?" else 0)
              if ms else cap)
    else:
        ls = le = None
    return (body[ls:s].strip() if ls is not None else "",
            body[s:e],
            body[e:le].strip() if le is not None else "")


def _is_table_sep(line: str) -> bool:
    # 구분행(| --- | --- |)은 '남은 행 수'에 세지 않는다 — 데이터가 아니다
    return bool(line) and set(line) <= set("|- ") and "---" in line


# 본문 절단의 머리 비중 — 나머지는 꼬리에 준다. 업무 메일은 앞에 인사·배경,
# **뒤에 결론과 요청**이 온다. 앞에서만 자르면 정작 필요한 쪽이 날아간다:
# 통당 2,200자 메일 16통을 실측했더니 결론 문장이 살아남은 것이 0통이었다
# (2026-08-03). 같은 예산으로 앞뒤를 나눠 담으면 16/16 이 살아난다 — 비용 0.
_HEAD_RATIO = 0.6


def _cut_head(text: str, limit: int, mark: bool = True) -> str:
    """앞에서 limit 자 — 마크다운 표를 반쪽으로 자르지 않는다.

    맹목 슬라이스는 절단점이 표 중간이면 남은 행이 사라졌다는 표시 없이
    **온전해 보이는 반쪽 표**를 만든다. 모델은 잘린 견적표를 전체로 믿고
    "3건"이라 종합한다(2026-08-02 점검에서 확인). 절단점이 표 안이면 마지막
    완결 행으로 물러나고 남은 행 수를 명시한다.

    표 행 판정은 '줄 시작 |' 하나로 충분하다: _MarkdownParser 가 셀 내 파이프를
    \\| 로 이스케이프하고, 셀 안 <pre> 는 <br> 인라인으로, 중첩 표는 바깥 셀에
    평탄화해 **행 = 물리 한 줄**을 보장한다(result() 참조).
    """
    if len(text) <= limit:
        return text
    ls = text.rfind("\n", 0, limit) + 1
    if not text.startswith("|", ls):
        return text[:limit]                 # 표 밖 — 단순 슬라이스
    bs = ls
    while bs > 0:
        prev = text.rfind("\n", 0, bs - 1) + 1
        if not text.startswith("|", prev):
            break
        bs = prev
    be = ls
    while be < len(text):
        nl = text.find("\n", be)
        nl = len(text) if nl < 0 else nl + 1
        if not text.startswith("|", be):
            break
        be = nl
    rows = text[bs:be].splitlines()
    ends, pos = [], bs
    for i, row in enumerate(rows):
        pos += len(row)
        ends.append((pos, i))
        pos += 1
    for pos, i in reversed(ends):
        left = sum(1 for r in rows[i + 1:] if not _is_table_sep(r))
        if left == 0:
            continue
        tag = f"\n…(표 잘림 — 이하 {left}행 생략)" if mark else ""
        if pos + len(tag) <= limit:
            return text[:pos] + tag
    total = sum(1 for r in rows if not _is_table_sep(r))
    head = text[:bs].rstrip()
    tag = f"…(표 생략 — {total}행)" if mark else ""
    if len(head) + len(tag) + 1 > limit:
        head = head[:max(0, limit - len(tag) - 1)]
    return (head + "\n" + tag) if (head and tag) else (head or tag)


def _cut_tail(text: str, limit: int) -> str:
    """뒤에서 limit 자 — 줄(표 행) 경계에서 시작한다.

    꼬리를 문장 중간에서 시작하면 첫 줄이 조각나고, 표 안에서 시작하면 머리행
    없는 행 뭉치가 된다. 경계로 **앞당기지 않고 뒤로 미뤄** 반쪽을 안 만든다."""
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    nl = text.find("\n", cut)
    return text[cut:] if nl < 0 else text[nl + 1:]


def smart_truncate(text: str, limit: int) -> str:
    """프롬프트용 본문 절단 — **앞뒤를 나눠 담고** 표를 반쪽으로 자르지 않는다.

    한도를 넘으면 앞 60% + 중략 표시 + 뒤 40%. 뒤를 남기는 이유는 업무 메일의
    결론·요청이 끝에 오기 때문이고(_HEAD_RATIO 주석의 실측), 중략을 **표시하는**
    이유는 모델이 조각을 전문으로 믿지 않게 하기 위해서다 — 종전 산문 절단은
    아무 표시 없이 끊겨 모델이 잘린 줄 몰랐다.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    # 마커 길이가 생략 자수(자릿수)에 달려 순환한다 — 꼬리를 줄이며 몇 번이면
    # 수렴한다. 수렴 못 하거나(아주 작은 한도) 겹치면 머리만 남긴다.
    room = max(0, limit - 24)
    head = _cut_head(text, int(room * _HEAD_RATIO), mark=False)
    tail = _cut_tail(text, room - len(head)) if room > len(head) else ""
    if not (head and tail):        # 나눠 담을 자리가 없다 — 마커만 남기지 않는다
        return _cut_head(text, limit)
    for _ in range(3):
        gap = len(text) - len(head) - len(tail)
        if gap <= 0:                       # 경계 보정으로 겹쳤다
            break
        mark = f"\n…(중략 — {gap:,}자)…\n"
        over = len(head) + len(mark) + len(tail) - limit
        if over <= 0:
            return head + mark + tail
        if len(tail) < over:
            break
        tail = _cut_tail(tail, len(tail) - over)
    return _cut_head(text, limit)


# 인용 시작 라벨 (구분선 패턴 공용)
# 절단 로직의 버전. 라벨·패턴을 넓히면 올린다 — Store 열 때 이 값이 저장값과
# 다르면 기존 new_content 에 재절단을 1회 소급한다(원문은 저장 안 하지만 절단
# 실패분은 인용 체인이 new_content 안에 그대로 남아 있어 가능. 재수집 불필요).
CLEAN_VERSION = 6

# 다국어 라벨(2026-07-31): 해외 파트너 스레드(프랑스어 Outlook)에서 절단 실패로
# 매 답장의 전체 체인이 '신규'로 남아 요약 입력이 반복 부풀었다(실사례 22KB×3).
# 라벨은 클라이언트가 찍는 고정 어휘라 오탐 여지가 작고, 헤더 블록 판정은
# FIRST+FOLLOW 2줄 게이트가 지킨다.
_QUOTE_LABEL = (
    r"(?:Original\s+Message|원본\s*메[일시]지?|Forwarded\s+message|전달된\s*메[일시]지?"
    r"|Message\s+d['\u2019]origine|Message\s+transf[ée]r[ée]"          # fr
    r"|Ursprüngliche\s+Nachricht|Weitergeleitete\s+Nachricht"          # de
    r"|Mensaje\s+original|Mensaje\s+reenviado"                         # es
    r"|Messaggio\s+originale|Messaggio\s+inoltrato"                    # it
    r"|Oorspronkelijk\s+bericht|Doorgestuurd\s+bericht"                # nl
    r"|元のメッセージ|転送されたメッセージ|原始邮件|原始郵件|转发邮件)"       # ja·zh
)

# sanitize_html 쪽 라벨 판정 (텍스트 청크 단위 — 태그로 쪼개진 경우는 _Sanitizer
# 보류 상태기계가 처리). 정크 클래스에 * 포함: 별표가 텍스트로 남은
# "--------- **Original Message** ---------" 단일 청크도 매칭.
_HTML_CUT_RX = re.compile(
    rf"^[\s\-—_=*]*{_QUOTE_LABEL}[\s\-—_=*]*$", re.IGNORECASE)
_DASH_ONLY_RX = re.compile(r"^[\s\-—_=*]+$")
# 클라이언트가 인용 블록에 붙이는 표식 — 이것들도 '구분선'으로 친다
_QUOTE_CONTAINER_RX = re.compile(
    r"divrplyfwdmsg|mail-editor-reference-message-container|gmail_quote"
    r"|moz-cite-prefix|appleoriginalcontents|yahoo_quoted", re.IGNORECASE)

# 한 줄로 인용 시작을 확정하는 패턴 (이 줄부터 끝까지 버림)
_CUT_LINE_PATTERNS = [
    # 대시 구분선형 — 대시·라벨 각 경계에 마크다운 강조([*_]) 허용.
    # html_to_markdown 이 <b>Original Message</b> 를 **Original Message** 로
    # 바꾸므로 "--------- **Original Message** ---------"(1차 관측 형태)와
    # "**-----원본 메시지-----**" 변형까지 커버한다.
    rf"^[*_]*\s*-{{2,}}\s*[*_]*\s*{_QUOTE_LABEL}\s*[*_]*\s*-{{2,}}",
    # 강조 전용형(대시 없음) — 강조 마커 필수 + 줄 전체 앵커(과잉 절단 방지)
    rf"^[*_]{{1,3}}\s*{_QUOTE_LABEL}\s*[*_]{{1,3}}\s*$",
    r"^_{10,}\s*$",                                 # Outlook 구분선 (뒤에 From 블록)
]

# 귀속줄(Gmail·Apple Mail·Thunderbird) — "누가 언제 썼다:" 한 줄로 인용이 시작된다.
# **주소 흔적이 있을 때만** 인정한다: 이 문형은 평범한 서술문과 겹쳐서
# ("2026년 7월 기준 자료는 김도현 님이 작성했습니다.", "On the attached spec
# page 3, the reviewer wrote:") 정상 본문이 잘리던 것을 실측했다(2026-07-31 리뷰).
# 길이 상한이 넉넉한 이유: html_to_markdown 이 주소를 [addr](mailto:addr) 로
# 두 배 늘려 옛 80자 예산을 넘겨 진짜 Gmail 인용이 안 잘렸다(같은 리뷰).
_ATTRIB_PATTERNS = [
    r"^On .{4,200} wrote\s*:\s*$",                              # en
    r"^\d{4}[.년\-].{2,160}(님이|이\(가\))?\s*(작성|썼습니다|쓴\s*글)\s*[:：]\s*$",
    r"^Le .{4,200} a écrit\s*:\s*$",                            # fr
    r"^Am .{4,200} schrieb .{0,80}:\s*$",                        # de
    r"^El .{4,200} escribió\s*:\s*$",                           # es
    r"^Il .{4,200} ha scritto\s*:\s*$",                         # it
    r"^Op .{4,200} schreef .{0,80}:\s*$",                        # nl
]
_ATTRIB_RES = [re.compile(p, re.IGNORECASE) for p in _ATTRIB_PATTERNS]
_CUT_LINE_RES = [re.compile(p, re.IGNORECASE) for p in _CUT_LINE_PATTERNS]

# Outlook 답장 헤더 블록 판정(_find_cut 참조): From 계열 라벨로 시작하고,
# 앞이 빈 줄·구분선이며, 뒤따르는 헤더 필드가 3개 이상이면 인용 시작이다.
# 라벨 종류로 기준을 나누지 않는다 — 짧은 외래 라벨(De/Von/Da/Van)만 걸러도
# 클라이언트별 필드 순서를 놓치고, 정작 오탐은 필드 수로 갈렸다(2026-07-31).
# 콜론은 반각·전각([:：]) 모두 — 일본어권 Outlook 은 전각을 쓴다. 프랑스어
# "De :"의 콜론 앞 공백은 NBSP(U+00A0)일 수 있는데 \s 가 매치한다.
# 라벨 앞의 마크다운 강조를 허용한다 — 실제 Outlook 은 헤더 라벨을 <b> 로
# 찍고 html_to_markdown 이 "**De :**" 로 바꾸므로, 줄머리 앵커만으로는 게이트가
# 아예 발화하지 않았다(2026-07-31 리뷰: 한국어 "**보낸 사람:**" 도 동일).
_EMPH = r"[*_]{0,3}\s*"          # 라벨 앞 강조 마커
_EMPH_END = r"\s*[*_]{0,3}\s*"   # 라벨과 콜론 사이 (f-string 안에 {0,3} 을 쓰면
                                 #  치환 필드로 먹혀 정규식이 깨진다 — 상수로 뺀다)
_HDR_FIRST = re.compile(
    rf"^\s*{_EMPH}(보낸\s*사람|From"
    rf"|De|Von|Da|Van|发件人|寄件者|差出人){_EMPH_END}[:：]", re.IGNORECASE)
# 발신인 줄의 주소 흔적 — "보낸 사람: 김도현 <a@b.com>" 처럼 주소가 있으면
# 진짜 답장 헤더일 확률이 매우 높다(문서 머리·표에는 주소가 없다).
# 원자 그룹(?>…)+길이 상한 — 앞의 문자 클래스가 줄 끝까지 삼켰다 되돌아오며
# O(n²) 가 됐다(200KB 한 줄에서 무응답. base64 data URL 이 실제로 그런 줄을
# 만든다). 원자 그룹은 백트래킹을 막고, 상한은 주소 문법상 넉넉하다.
_ADDR_HINT = re.compile(
    r"(?>[\w.+-]{1,64})@(?>[\w-]{1,63})\.(?>[\w.-]{2,63})"
    r"|<[^>\s]{1,128}@[^>\s]{1,128}>|\(mailto:")
_HDR_FOLLOW = re.compile(
    rf"^\s*{_EMPH}("
    r"보낸\s*날짜|날짜|받는\s*사람|수신|참조|제목|Sent|Date|To|Cc|Subject"
    r"|Envoyé|À|A|Objet"                             # fr
    r"|Gesendet|Datum|An|Betreff"                    # de
    r"|Enviado|Enviada|Fecha|Para|Asunto"            # es·pt
    r"|Inviato|Data|Oggetto"                         # it
    r"|Verzonden|Aan|Onderwerp"                      # nl
    r"|宛先|件名|送信日時|收件人|抄送|主题|发送时间"
    rf"){_EMPH_END}[:：]", re.IGNORECASE)


def _find_cut(lines: list[str]) -> int:
    """인용이 시작되는 줄 인덱스. 없으면 len(lines)."""
    n = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        for rx in _CUT_LINE_RES:
            if rx.search(s):
                return i
        # 주소가 있거나, 날짜처럼 숫자가 여럿인 짧은 줄일 때만 인용 시작으로
        # 본다 — "On the attached spec page 3, the reviewer wrote:" 같은 서술문을
        # 거르면서 표시 이름만 쓰는 Thunderbird 형태는 놓치지 않는다.
        if (any(rx.search(s) for rx in _ATTRIB_RES)
                and (_ADDR_HINT.search(s)
                     or (len(s) <= 140 and sum(c.isdigit() for c in s) >= 4))):
            return i
        if _HDR_FIRST.match(line):
            # 헤더 블록은 (1) 앞이 빈 줄·구분선이고 (2) 뒤 12줄(빈 줄 제외)
            # 안에 헤더 필드가 3개 이상이다. 실제 답장 헤더는 날짜·수신·제목이
            # 함께 오는데(어느 클라이언트든 3개 이상) 정상 본문에 우연히
            # 겹치는 라벨은 1~2개뿐이다.
            #  (1) 은 양식 안내·구간표·번역표가 문장 바로 밑에 라벨을 잇는 것과
            #      진짜 인용 헤더를 가른다.
            #  (2) 는 주소 유무로 낮추지 않는다 — 계정 요청 양식·반송 로그·
            #      피싱 주의 공지처럼 본문에 주소가 있는 정상 문서가 잘렸다
            #      (2026-07-31 리뷰 실증).
            prev = lines[i - 1].strip() if i else ""
            if prev and not _DASH_ONLY_RX.match(prev):
                continue
            nxt = [l for l in lines[i + 1 : i + 14] if l.strip()][:12]
            if sum(1 for l in nxt if _HDR_FOLLOW.match(l)) >= 3:
                return i
    return n


# ------------------------------------------------------------------- 서명 등

_SIG_PATTERNS = [
    re.compile(r"^--\s*$"),                          # 표준 서명 구분자
    re.compile(r"^={10,}\s*$"),
    re.compile(r"^※\s*(이|본)\s*(전자\s*)?메일은"),   # 법적 고지 시작
    re.compile(r"^(이|본)\s*(전자\s*)?메일은\s.{0,20}(기밀|비밀|보안)"),
    re.compile(r"^Confidentiality Notice", re.IGNORECASE),
]


def _strip_signature(lines: list[str]) -> list[str]:
    """서명/고지 시작 지점부터 제거. 본문 최소 1줄은 보존.

    (구현이 range(2,…) 라 서명이 둘째 줄에 오면 못 지웠다 — 짧은 답장
    "확인했습니다.\n--\n김도현 / 누리소프트" 가 실제 그 형태다.)"""
    for i in range(1, len(lines)):
        s = lines[i].strip()
        if any(rx.match(s) for rx in _SIG_PATTERNS):
            return lines[:i]
    return lines


def extract_new_content(body_text: str, preserve_quotes: bool = False) -> str:
    """메일 본문에서 '새로 쓰인 부분'만 추출.

    1) 인용 시작 지점에서 절단
    2) `>` 인용 줄 제거
    3) 꼬리 서명/법적 고지 제거

    preserve_quotes(mid-join 첫 보유 메일): 절단 지점 이후를 버리지 않고
    PRESERVED_MARK 아래에 원문 그대로 잇는다 (`>` 줄·서명도 보존 — 체인 속
    서명은 기록의 일부고, 서명 절단 패턴이 체인 전체를 지울 수 있다). 캡 없음.
    """
    # 보존 블록(mid-join 유일본)은 재투입에서도 손대지 않는다 — 그러지 않으면
    # extract_new_content 를 두 번 태우는 순간 체인이 증발한다(소급 재절단이
    # 반복 적용되는 구조라 이 고정점 성질이 안전망이다).
    keep_tail = ""
    if not preserve_quotes and PRESERVED_MARK in body_text:
        head, _, tail = body_text.partition(PRESERVED_MARK)
        body_text, keep_tail = head, PRESERVED_MARK + tail
    lines = body_text.split("\n")
    cut = _find_cut(lines)
    head = [l for l in lines[:cut] if not l.lstrip().startswith(">")]
    head = _strip_signature(head)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(head)).strip()
    if preserve_quotes and cut < len(lines):
        tail = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[cut:])).strip()
        if tail:
            text = (text + "\n\n" if text else "") + PRESERVED_MARK + "\n" + tail
    if keep_tail:
        text = (text + "\n\n" if text else "") + keep_tail
    return text


# ------------------------------------------------------------------ 제목 정규화

_SUBJECT_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|회신|답장|전달|답신)\s*[:：]\s*|\[\s*(re|fw|fwd)\s*\]\s*)+",
    re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """RE:/FW:/회신:/전달: 접두어를 제거한 스레드 매칭용 제목."""
    s = _SUBJECT_PREFIX.sub("", subject or "")
    return re.sub(r"\s+", " ", s).strip().lower()


# ------------------------------------------------ 인라인 이미지 주입 (cid → data:)
# sanitize_html 은 cid: 를 원격 이미지처럼 data-blocked-src 로 무력화해 둔다.
# 여기서 정제(인용 절단) '후'에 살아남은 cid 참조에만 바이트를 base64 로 주입
# — 잘려나간 재인용 체인 속 이미지는 임베드되지 않는다 (docs/ARCHITECTURE.md §6.1).

_CID_IMG_RX = re.compile(
    r"<img\b[^>]*\bdata-blocked-src=\"cid:([^\"]+)\"[^>]*>", re.IGNORECASE)


def _norm_cid(cid: str) -> str:
    """Content-ID 정규화 — 꺾쇠(<>)·공백·URL 이스케이프·대소문자 차이 흡수."""
    from urllib.parse import unquote
    return unquote(cid or "").strip().strip("<>").lower()


def inject_inline_images(html: str, images: dict) -> tuple[str, int, int]:
    """정제된 HTML 의 cid 차단 마크에 인라인 이미지를 주입.

    images: {cid: (mime, bytes)}. 반환 (html, 임베드 수, 실패 수).
    - 같은 cid 의 재등장(메일 내 중복)은 1회만 임베드, 이후는 생략 표시
      — 무제한 정책에서 중복이 용량을 배수로 키우는 것 방지.
    - 매칭 실패(cid 는 있는데 바이트 없음)는 차단 마크 유지 → 웹 안내 배너.
    """
    if not html or not images:
        # 실패 수 = 남아 있는 cid 차단 마크 수 (images 비어도 집계)
        return html, 0, len(_CID_IMG_RX.findall(html or ""))
    import base64 as _b64
    norm = {_norm_cid(k): v for k, v in images.items()}
    b64_cache: dict[str, str] = {}
    embedded: set[str] = set()
    failed = 0

    def _repl(m):
        nonlocal failed
        raw_cid = m.group(1)
        cid = _norm_cid(raw_cid)
        item = norm.get(cid)
        if not item:
            failed += 1
            return m.group(0)                    # 추출 실패 — 차단 마크 유지
        if cid in embedded:                      # 메일 내 중복 — 생략 표시
            return "<span class='imgnote-inline'>🖼 (중복 이미지 생략)</span>"
        embedded.add(cid)
        mime, data = item
        if cid not in b64_cache:
            b64_cache[cid] = _b64.b64encode(data).decode("ascii")
        return m.group(0).replace(
            f'data-blocked-src="cid:{raw_cid}"',
            f'src="data:{mime};base64,{b64_cache[cid]}"', 1)

    out = _CID_IMG_RX.sub(_repl, html)
    return out, len(embedded), failed
