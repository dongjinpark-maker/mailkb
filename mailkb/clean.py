"""본문 정리: HTML→텍스트, 인용문/서명 제거.

토큰 깔때기의 ② 단계. 답장 체인은 이전 메일을 전부 재인용하므로
"이 메일에서 새로 쓰인 부분"만 남기는 것이 검색 품질과 AI 비용 양쪽의 핵심.

한국어 Outlook 환경의 인용 헤더 패턴을 우선 지원한다.
과잉 제거보다 과소 제거가 낫다는 원칙: 애매하면 남긴다.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


# ---------------------------------------------------------------- HTML → text

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "blockquote", "h1", "h2", "h3"}
_SKIP_TAGS = {"style", "script", "head", "title"}


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
                "code": "`", "tt": "`"}
# 강조 마커를 붙일 수 있는 인라인 태그(구조 태그와 분리 — <div style=bold> 같은
# 블록에는 마커를 안 붙여 "\n\n**" 깨짐을 피한다). span/font 는 style 로만 판정.
_INLINE_STYLE_TAGS = {"b", "strong", "i", "em", "code", "tt",
                      "u", "span", "font", "mark", "small"}
_STYLE_BOLD_RX = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)")
_STYLE_ITALIC_RX = re.compile(r"font-style\s*:\s*italic")


def _style_marks(attrs) -> list[str]:
    """style 속성에서 굵게/기울임을 읽어 마커로. (Word/Outlook 은 <b> 대신
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
    return marks


class _MarkdownConverter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0
        self._list_stack: list[list] = []          # ['ul'] | ['ol', n]
        self._href: list[str] = []
        self._mark_stack: list[tuple] = []          # (sink, idx, marks)
        # 표 상태
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._rows: list[list[str]] | None = None
        self._row_is_header = False

    def _sink(self) -> list[str]:
        return self._cell if self._cell is not None else self._out

    def _emit(self, s: str) -> None:
        self._sink().append(s)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
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
            self._rows = []
        elif tag == "tr":
            self._row = []
            self._row_is_header = False
        elif tag in ("td", "th"):
            self._cell = []
            if tag == "th":
                self._row_is_header = True

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
        if tag in _INLINE_STYLE_TAGS:
            if self._mark_stack:
                sink, idx, marks = self._mark_stack.pop()
                inner = "".join(sink[idx + len(marks):])
                if marks and inner.strip() == "":
                    del sink[idx:idx + len(marks)]      # 빈 강조 제거
                else:
                    for mk in reversed(marks):
                        self._emit(mk)
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
            if self._cell is not None and self._row is not None:
                cell = re.sub(r"\s+", " ", "".join(self._cell)).strip().replace("|", r"\|")
                self._row.append(cell)
            self._cell = None
        elif tag == "tr":
            if self._row is not None and self._rows is not None:
                self._rows.append(self._row)
                if self._row_is_header:
                    self._rows.append(["---"] * len(self._row))
            self._row = None
        elif tag == "table":
            if self._rows:
                # GFM 유효성: 첫 행 뒤 구분행이 없으면(th 없는 Outlook 표 —
                # 붙여넣기 표의 전형) 삽입해 다운스트림 표 렌더가 인식하게 한다
                if (len(self._rows) < 2
                        or not all(c == "---" for c in self._rows[1])):
                    self._rows.insert(1, ["---"] * len(self._rows[0]))
                self._out.append("\n\n")
                for r in self._rows:
                    self._out.append("| " + " | ".join(r) + " |\n")
                self._out.append("\n")
            self._rows = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.replace("\xa0", " ").replace("​", "")
        if self._cell is not None:
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
                self.out.append(f"</{ev[1]}>")
                self._pop_open(ev[1])
        self._pend.clear()

    def _pop_open(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i] == tag:
                del self._open[i]
                return

    def _do_cut(self) -> None:
        """인용 라벨 확정 — 보류분 폐기, 열린 태그 닫기, 이후 전부 무시."""
        self._pend.clear()
        for tag in reversed(self._open):
            self.out.append(f"</{tag}>")
        self._open.clear()
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
        self._open.clear()
        self.out.append(QFOLD_OPEN)
        self._preserving = True
        self._pend = pend
        self._flush()                    # 구분선 등 보류분을 폴드 안으로

    def _hold(self, ev: tuple) -> None:
        self._pend.append(ev)
        if len(self._pend) > self._PEND_MAX:
            self._flush()

    def _pend_has_sep(self) -> bool:
        """보류분에 구분선(대시/언더바 전용 줄)이 있나 — Outlook 헤더 블록 앞
        '________________________________' 절단 판정용."""
        return any(ev[0] == "text" and _DASH_ONLY_RX.match(ev[1].strip())
                   for ev in self._pend)

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
        if tag == "img":
            src = _attr_get(attrs, "src").strip()
            if src.lower().startswith("data:image/"):
                parts.append(f' src="{_attr_esc(src)}"')
            elif src:
                # 원격 이미지 = 추적 픽셀 → 무력화(토글로 로드 가능하게 원본 보존)
                parts.append(f' data-blocked-src="{_attr_esc(src)}"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._cut:
            return
        if tag in _DROP_TREE:
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
        if self._cut or tag in _DROP_TREE or self._drop or tag not in _ALLOWED_TAGS:
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
            self.out.append(f"</{tag}>")
            self._pop_open(tag)

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
        elif s and _HDR_FIRST.match(s) and self._pend_has_sep():
            if not self._preserve:
                self._do_cut()
                return
            self._begin_preserve()
        esc = _data_esc(data)
        if s and len(s) >= 2 and _DASH_ONLY_RX.match(s):
            self._hold(("text", esc))
            return
        if self._pend:
            if not s:
                self._hold(("text", esc))   # 공백 청크는 보류 유지 (순서 보존)
                return
            self._flush()                    # 실제 내용 — 라벨이 아니었다
        self.out.append(esc)

    def close(self) -> None:
        super().close()
        if not self._cut:
            self._flush()   # 라벨 없이 끝남 — 보류분(서명 구분선 등) 보존
        if self._preserving:
            # 원본이 태그를 안 닫고 끝나도 폴드는 균형 있게 닫는다
            for tag in reversed(self._open):
                self.out.append(f"</{tag}>")
            self._open.clear()
            self.out.append(QFOLD_CLOSE)


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


# ---------------------------------------------------------- 인용문 절단 지점

# mid-join 보존 (docs/PROPOSAL-midjoin.md): 스레드의 첫 보유 메일은 인용 체인이
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


# 인용 시작 라벨 (구분선 패턴 공용)
_QUOTE_LABEL = (
    r"(?:Original\s+Message|원본\s*메[일시]지?|Forwarded\s+message|전달된\s*메[일시]지?)"
)

# sanitize_html 쪽 라벨 판정 (텍스트 청크 단위 — 태그로 쪼개진 경우는 _Sanitizer
# 보류 상태기계가 처리). 정크 클래스에 * 포함: 별표가 텍스트로 남은
# "--------- **Original Message** ---------" 단일 청크도 매칭.
_HTML_CUT_RX = re.compile(
    rf"^[\s\-—_=*]*{_QUOTE_LABEL}[\s\-—_=*]*$", re.IGNORECASE)
_DASH_ONLY_RX = re.compile(r"^[\s\-—_=*]+$")

# 한 줄로 인용 시작을 확정하는 패턴 (이 줄부터 끝까지 버림)
_CUT_LINE_PATTERNS = [
    # 대시 구분선형 — 대시·라벨 각 경계에 마크다운 강조([*_]) 허용.
    # html_to_markdown 이 <b>Original Message</b> 를 **Original Message** 로
    # 바꾸므로 "--------- **Original Message** ---------"(1차 관측 형태)와
    # "**-----원본 메시지-----**" 변형까지 커버한다.
    rf"^[*_]*\s*-{{2,}}\s*[*_]*\s*{_QUOTE_LABEL}\s*[*_]*\s*-{{2,}}",
    # 강조 전용형(대시 없음) — 강조 마커 필수 + 줄 전체 앵커(과잉 절단 방지)
    rf"^[*_]{{1,3}}\s*{_QUOTE_LABEL}\s*[*_]{{1,3}}\s*$",
    r"^On .{4,80} wrote:\s*$",                      # Gmail 영문
    r"^\d{4}[.년].{2,30}에?\s.{0,40}(님이|이\(가\))\s*(작성|썼습니다)",  # Gmail 국문
    r"^_{10,}\s*$",                                 # Outlook 구분선 (뒤에 From 블록)
]
_CUT_LINE_RES = [re.compile(p, re.IGNORECASE) for p in _CUT_LINE_PATTERNS]

# Outlook 답장 헤더 블록: "보낸 사람:"/"From:" 으로 시작해 2줄 안에
# 날짜/받는 사람 계열 필드가 이어지면 인용 시작으로 판정
_HDR_FIRST = re.compile(r"^\s*(보낸\s*사람|발신|From)\s*:", re.IGNORECASE)
_HDR_FOLLOW = re.compile(
    r"^\s*(보낸\s*날짜|날짜|받는\s*사람|수신|참조|제목|Sent|Date|To|Cc|Subject)\s*:",
    re.IGNORECASE,
)


def _find_cut(lines: list[str]) -> int:
    """인용이 시작되는 줄 인덱스. 없으면 len(lines)."""
    n = len(lines)
    for i, line in enumerate(lines):
        for rx in _CUT_LINE_RES:
            if rx.search(line.strip()):
                return i
        if _HDR_FIRST.match(line):
            # 뒤따르는 2줄 안에 헤더 필드가 하나라도 있으면 블록으로 인정
            follow = [l for l in lines[i + 1 : i + 3]]
            if any(_HDR_FOLLOW.match(l) for l in follow):
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
    """서명/고지 시작 지점부터 제거. 최소 2줄의 본문은 보존."""
    for i in range(2, len(lines)):
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
    lines = body_text.split("\n")
    cut = _find_cut(lines)
    head = [l for l in lines[:cut] if not l.lstrip().startswith(">")]
    head = _strip_signature(head)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(head)).strip()
    if preserve_quotes and cut < len(lines):
        tail = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[cut:])).strip()
        if tail:
            text = (text + "\n\n" if text else "") + PRESERVED_MARK + "\n" + tail
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
# — 잘려나간 재인용 체인 속 이미지는 임베드되지 않는다 (docs/PROPOSAL-images.md).

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
