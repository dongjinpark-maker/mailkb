"""핵심 로직 단위 테스트: 인용 제거, 스레딩, 미답변 판정."""

import json
from datetime import date, timedelta
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote as urllib_unquote

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailkb import actions, distill, notes, review, web
from mailkb import search as search_mod
from mailkb.features import classify_message
from mailkb.clean import (
    PRESERVED_MARK,
    extract_new_content,
    hide_image_signatures,
    html_to_markdown,
    html_to_text,
    normalize_subject,
    sanitize_html,
    strip_preserved,
)
from mailkb.config import Config
from mailkb.sources.base import MailRecord
from mailkb.store import Store

ME = "me@corp.example"


class TestClean(unittest.TestCase):
    def test_korean_outlook_quote_block(self):
        body = (
            "새 내용입니다.\n확인 부탁드립니다.\n\n"
            "________________________________\n"
            "보낸 사람: 김민수 <kim@corp.example>\n"
            "보낸 날짜: 2026년 7월 3일 금요일 오후 2:00\n"
            "받는 사람: 김도현\n제목: RE: 검토\n\n이전 내용 전체..."
        )
        out = extract_new_content(body)
        self.assertIn("새 내용입니다", out)
        self.assertNotIn("이전 내용", out)
        self.assertNotIn("보낸 사람", out)

    def test_original_message_marker_korean(self):
        body = "회신입니다.\n\n-----원본 메시지-----\nFrom: x\n원래 내용"
        out = extract_new_content(body)
        self.assertEqual(out, "회신입니다.")

    def test_markdown_bold_quote_separator(self):
        # html_to_markdown 변환 후 1차 관측 형태: 대시 밖·라벨만 굵게
        body = ("회신입니다.\n\n--------- **Original Message** ---------\n"
                "From: x\n원래 내용")
        self.assertEqual(extract_new_content(body), "회신입니다.")

    def test_markdown_bold_quote_separator_variants(self):
        for sep in ["**-----Original Message-----**",   # 전체를 굵게
                    "**Original Message**",              # 대시 없는 강조 전용
                    "*-----원본 메시지-----*",
                    "--------- **전달된 메시지** ---------"]:
            body = f"회신입니다.\n\n{sep}\nFrom: x\n원래 내용"
            self.assertEqual(extract_new_content(body), "회신입니다.", msg=sep)

    def test_bare_label_sentence_not_cut(self):
        # 대시도 강조도 없는 맨몸 라벨 문장은 절단하지 않는다 (오탐 방지)
        body = "Original Message 항목을 참고해 주세요.\n다음 줄 내용입니다."
        out = extract_new_content(body)
        self.assertIn("다음 줄 내용입니다", out)

    def test_gt_quoted_lines_removed(self):
        body = "동의합니다.\n> 원래 제안\n> 상세 내용\n감사합니다."
        out = extract_new_content(body)
        self.assertNotIn("원래 제안", out)
        self.assertIn("동의합니다", out)

    def test_signature_and_disclaimer_stripped(self):
        body = (
            "본문 첫 줄.\n본문 둘째 줄.\n\n--\n홍길동 책임\n"
            "※ 본 메일은 기밀 정보를 포함할 수 있습니다."
        )
        out = extract_new_content(body)
        self.assertIn("본문 둘째 줄", out)
        self.assertNotIn("홍길동", out)
        self.assertNotIn("기밀", out)

    def test_short_mail_not_over_stripped(self):
        body = "네, 알겠습니다."
        self.assertEqual(extract_new_content(body), "네, 알겠습니다.")

    def test_inline_mark_edge_space_moved_out(self):
        # "<b>aaa </b>" → "**aaa** " — 마커 안 가장자리 공백은 무효 마크다운이라
        # 밖으로 재배치 (렌더러·외부 md 도구가 살릴 수 있게)
        self.assertEqual(html_to_markdown("<p><b>aaa </b>다음</p>"), "**aaa** 다음")
        self.assertEqual(html_to_markdown("<p>앞<b> aaa</b></p>"), "앞 **aaa**")
        out = html_to_markdown(
            "<p><span style='text-decoration: line-through;'>취소 </span>유지</p>")
        self.assertEqual(out, "~~취소~~ 유지")

    def test_html_to_text(self):
        html = "<html><style>p{color:red}</style><body><p>안녕하세요&nbsp;팀</p><br><div>둘째 줄</div></body></html>"
        out = html_to_text(html)
        self.assertIn("안녕하세요 팀", out)
        self.assertIn("둘째 줄", out)
        self.assertNotIn("color", out)

    def test_html_to_markdown_inline(self):
        html = '<p>이건 <b>중요</b>하고 <i>선택</i>이며 '\
               '<a href="https://x.nurisoft.co.kr/42">문서</a> 참고</p>'
        out = html_to_markdown(html)
        self.assertIn("**중요**", out)
        self.assertIn("*선택*", out)
        self.assertIn("[문서](https://x.nurisoft.co.kr/42)", out)

    def test_html_to_markdown_style_bold(self):
        # Word/Outlook 은 <b> 대신 span style 로 굵게를 준다
        html = '<p>납기 <span style="font-weight:bold">7월 10일</span> 확정</p>'
        self.assertIn("**7월 10일**", html_to_markdown(html))

    def test_html_to_markdown_table(self):
        html = "<table><tr><th>항목</th><th>납기</th></tr>"\
               "<tr><td>A건</td><td>7/10</td></tr></table>"
        out = html_to_markdown(html)
        self.assertIn("| 항목 | 납기 |", out)
        self.assertIn("| --- | --- |", out)
        self.assertIn("| A건 | 7/10 |", out)

    def test_html_to_markdown_list(self):
        out = html_to_markdown("<ul><li>첫째</li><li>둘째</li></ul>")
        self.assertIn("- 첫째", out)
        self.assertIn("- 둘째", out)

    def test_html_to_markdown_no_empty_emphasis(self):
        # 빈 style span 이 **** 같은 찌꺼기를 남기면 안 되고, 인접 굵게도 안 깨짐
        html = '<p>정상 <span style="font-weight:bold"></span>텍스트 '\
               '<b>가</b> 사이 <b>나</b></p>'
        out = html_to_markdown(html)
        self.assertNotIn("****", out)
        self.assertIn("**가**", out)
        self.assertIn("**나**", out)

    def test_html_to_markdown_quote_strip_still_works(self):
        # 마크다운으로 바꿔도 Outlook 답장 헤더 인용 제거가 동작해야 함
        reply = "<div>새 내용.</div><div>확인 부탁.</div>"\
                "<div>________________________________</div>"\
                "<div>보낸 사람: 김민수</div><div>보낸 날짜: 2026년 7월 3일</div>"\
                "<div>받는 사람: 김도현</div><div>이전 인용 전체...</div>"
        out = extract_new_content(html_to_markdown(reply))
        self.assertIn("새 내용", out)
        self.assertNotIn("이전 인용", out)
        self.assertNotIn("보낸 사람", out)

    def test_normalize_subject(self):
        self.assertEqual(normalize_subject("RE: RE: 검토 요청"), "검토 요청")
        self.assertEqual(normalize_subject("회신: 전달: 검토 요청"), "검토 요청")
        self.assertEqual(normalize_subject("[RE] 검토 요청"), "검토 요청")


class TestMarkdownNotificationMail(unittest.TestCase):
    """알림형 메일(Confluence 류) 변환 — 중첩/레이아웃 표, 숨김, 코드, 체크박스,
    취소선, 셀 내 블록, 이미지 alt, 목록 정리 (2026-07-13 개선 1~9)."""

    def test_nested_table_content_not_lost(self):
        # 레이아웃 표 안의 본문(제목·문단·데이터 표)이 소실되지 않는다
        html = ("<table role='presentation'><tr><td>"
                "<h2>결정 사항</h2><p>프리즈는 7/21 입니다.</p>"
                "<table border='1'><tr><th>항목</th><th>기한</th></tr>"
                "<tr><td>ECO</td><td>7/18</td></tr></table>"
                "</td></tr></table>")
        out = html_to_markdown(html)
        self.assertIn("## 결정 사항", out)
        self.assertIn("프리즈는 7/21 입니다.", out)
        self.assertIn("| ECO | 7/18 |", out)

    def test_layout_table_transparent_keeps_newlines(self):
        # role=presentation/전부-0 표는 컨테이너 — 문단 경계(개행) 보존, 파이프 없음
        for attrs in ("role='presentation'",
                      "border='0' cellpadding='0' cellspacing='0'"):
            html = (f"<table {attrs}><tr><td><p>첫 문단.</p><p>둘째 문단.</p>"
                    "</td></tr></table>")
            out = html_to_markdown(html)
            self.assertNotIn("|", out, msg=attrs)
            self.assertIn("첫 문단.\n\n둘째 문단.", out, msg=attrs)

    def test_plain_data_table_still_pipes(self):
        # 속성 없는 표(붙여넣기 표의 전형)는 종전대로 데이터 표
        out = html_to_markdown("<table><tr><td>a</td><td>b</td></tr></table>")
        self.assertIn("| a | b |", out)

    def test_hidden_preheader_skipped(self):
        html = ("<span style='display:none; max-height:0px;'>미리보기 문구</span>"
                "<p>실제 본문</p>")
        out = html_to_markdown(html)
        self.assertNotIn("미리보기", out)
        self.assertIn("실제 본문", out)

    def test_pre_becomes_fence_with_indent(self):
        html = "<div><pre>  if x:\n      run()</pre></div>"
        out = html_to_markdown(html)
        self.assertIn("```\n  if x:\n      run()\n```", out)

    def test_checkbox_state_glyphs(self):
        html = ("<ul><li><input type='checkbox' checked> 회귀 통과</li>"
                "<li><input type='checkbox'> 실보드 검증</li></ul>")
        out = html_to_markdown(html)
        self.assertIn("☑ 회귀 통과", out)
        self.assertIn("☐ 실보드 검증", out)

    def test_strikethrough_tag_and_style(self):
        out = html_to_markdown("<p><s>7/18 예정</s> 7/17 로 변경</p>")
        self.assertIn("~~7/18 예정~~", out)
        out = html_to_markdown(
            "<p><span style='text-decoration: line-through;'>구 문장</span>"
            " 새 문장</p>")
        self.assertIn("~~구 문장~~", out)

    def test_cell_multi_block_separator(self):
        # 셀 안 다중 문단은 ' · ' 로 경계 보존, 텍스트 노드의 소스 개행은 공백
        html = ("<table border='1'><tr><td><p>flags 추가.</p><p>소스 호환.</p>"
                "</td><td>줄1\n줄2</td></tr></table>")
        out = html_to_markdown(html)
        self.assertIn("| flags 추가. · 소스 호환. | 줄1 줄2 |", out)

    def test_img_alt_content_only(self):
        # 콘텐츠 이미지(큰 것/width 미지정)만 alt 방출 — 아바타·추적픽셀 제외
        out = html_to_markdown(
            "<p><img src='x' alt='레이턴시 차트' width='480'>"
            "<img src='y' alt='김민수' width='32'>"
            "<img src='z' alt='추적' width='1'>"
            "<img src='w' alt='첨부 다이어그램'></p>")
        self.assertIn("[그림: 레이턴시 차트]", out)
        self.assertIn("[그림: 첨부 다이어그램]", out)
        self.assertNotIn("김민수", out)
        self.assertNotIn("추적", out)

    def test_list_items_not_split_by_source_newlines(self):
        html = "<ul>\n  <li>항목 하나</li>\n\n  <li>항목 둘</li>\n</ul>"
        out = html_to_markdown(html)
        self.assertIn("- 항목 하나\n- 항목 둘", out)

    def test_notification_mail_end_to_end(self):
        # 셸(중첩 레이아웃) + 발췌 본문 — 본문은 살고 프리헤더는 죽는다
        html = ("<span style='display:none'>프리헤더</span>"
                "<table role='presentation'><tr><td>"
                "<table border='0' cellpadding='0' cellspacing='0'><tr><td>"
                "<p><b>김민수</b>님이 수정했습니다</p>"
                "<h2>회의 결과</h2><p>납기는 유지합니다.</p>"
                "</td></tr></table></td></tr></table>")
        nc = extract_new_content(html_to_markdown(html))
        self.assertIn("## 회의 결과", nc)
        self.assertIn("납기는 유지합니다.", nc)
        self.assertNotIn("프리헤더", nc)
        self.assertNotIn("|", nc)

    def test_web_renders_del(self):
        out = web._mail_md_to_html("~~지운 문장~~ 새 문장")
        self.assertIn("<del>지운 문장</del>", out)


class TestHideImageSignatures(unittest.TestCase):
    """꼬리 이미지 서명 숨김 — 임베드 PNG·height≤210·본문 뒤 (2026-07-14)."""

    PNG = "data:image/png;base64,iVBORw0KGgoAAAANS"

    def _img(self, h=120, style="", src=None):
        src = src or self.PNG
        hattr = f" height='{h}'" if h is not None else ""
        st = f" style='{style}'" if style else ""
        return f"<img src='{src}'{hattr}{st}>"

    def test_tail_signature_replaced(self):
        html = f"<p>본문입니다. 확인 부탁드립니다.</p>{self._img(120)}"
        out = hide_image_signatures(html)
        self.assertIn("Signature 숨김", out)
        self.assertNotIn("data:image/png", out)
        self.assertIn("본문입니다", out)

    def test_signature_in_bordered_table_removed_whole(self):
        html = ("<p>회신드립니다.</p>"
                "<table border='1'><tr><td>" + self._img(90) + "</td></tr></table>")
        out = hide_image_signatures(html)
        self.assertIn("Signature 숨김", out)
        self.assertNotIn("<table", out)          # 테두리 table 째 제거
        self.assertNotIn("data:image/png", out)

    def test_tall_image_kept(self):
        # height > 210 = 콘텐츠 이미지(차트 등) → 유지
        html = f"<p>파형 공유합니다.</p>{self._img(400)}"
        out = hide_image_signatures(html)
        self.assertNotIn("Signature 숨김", out)
        self.assertIn("data:image/png", out)

    def test_undeclared_height_kept(self):
        # height 미선언 → ≤210 확인 불가 → 대상 아님(보수적)
        html = f"<p>본문.</p>{self._img(h=None)}"
        self.assertNotIn("Signature 숨김", hide_image_signatures(html))

    def test_style_height_detected(self):
        html = f"<p>본문 텍스트입니다.</p>{self._img(h=None, style='height:180px')}"
        out = hide_image_signatures(html)
        self.assertIn("Signature 숨김", out)

    def test_image_only_mail_kept(self):
        # 앞에 실질 본문 없음 → 접지 않음(볼 게 없어짐)
        html = self._img(100)
        out = hide_image_signatures(html)
        self.assertNotIn("Signature 숨김", out)
        self.assertIn("data:image/png", out)

    def test_content_image_then_text_kept(self):
        # 이미지 뒤에 실질 텍스트 → 꼬리 아님 → 유지
        html = f"<p>스크린샷:</p>{self._img(100)}<p>위 화면을 확인해 주세요.</p>"
        out = hide_image_signatures(html)
        self.assertNotIn("Signature 숨김", out)

    def test_remote_image_not_matched(self):
        # 임베드(data:)만 대상 — 원격/차단 이미지는 제외
        html = "<p>본문.</p><img data-blocked-src='https://x/logo.png' height='80'>"
        self.assertNotIn("Signature 숨김", hide_image_signatures(html))

    def test_non_png_embedded_not_matched(self):
        html = ("<p>본문.</p><img src='data:image/jpeg;base64,/9j/4AAQ' height='80'>")
        self.assertNotIn("Signature 숨김", hide_image_signatures(html))

    def test_multiple_stacked_signatures_one_note(self):
        html = f"<p>감사합니다.</p>{self._img(80)}{self._img(60)}"
        out = hide_image_signatures(html)
        self.assertEqual(out.count("Signature 숨김"), 1)
        self.assertNotIn("data:image/png", out)

    def test_fast_path_identity(self):
        # 임베드 PNG 없으면 입력 그대로(무변경)
        html = "<p>그냥 텍스트 메일</p>"
        self.assertEqual(hide_image_signatures(html), html)

    def test_qfold_mail_skipped(self):
        # mid-join 인용 접기가 있으면 안전하게 건너뜀
        html = (f"<p>본문.</p><details class='qfold'><summary>이전</summary>"
                f"<div class='qbody'>{self._img(80)}</div></details>")
        self.assertEqual(hide_image_signatures(html), html)


class TestSanitizeHtml(unittest.TestCase):
    def test_strips_script_and_iframe(self):
        out = sanitize_html("<p>안녕<script>alert(1)</script><iframe src=x></iframe>끝</p>")
        self.assertNotIn("script", out)
        self.assertNotIn("iframe", out)
        self.assertIn("안녕", out)
        self.assertIn("끝", out)

    def test_removes_event_handlers(self):
        out = sanitize_html('<p onclick="steal()">클릭</p>')
        self.assertNotIn("onclick", out)
        self.assertIn("클릭", out)

    def test_blocks_javascript_href(self):
        out = sanitize_html('<a href="javascript:evil()">x</a>'
                            '<a href="https://s.nurisoft.co.kr">o</a>')
        self.assertNotIn("javascript:", out)
        self.assertIn("https://s.nurisoft.co.kr", out)

    def test_blocks_remote_image_keeps_data_uri(self):
        out = sanitize_html('<img src="http://track.evil/p.gif">'
                            '<img src="data:image/png;base64,AAAA">')
        self.assertIn("data-blocked-src", out)          # 원격 = 추적 픽셀 차단
        self.assertNotIn(' src="http://track', out)     # 활성 src 로는 안 나감
        self.assertIn("data:image/png", out)            # data URI 는 허용

    def test_sanitizes_style_but_keeps_formatting(self):
        out = sanitize_html('<p style="font-weight:bold;background:url(http://x)">굵게</p>')
        self.assertIn("font-weight:bold", out)
        self.assertNotIn("url(", out)

    def test_preserves_table_and_link(self):
        out = sanitize_html("<table><tr><td>A</td></tr></table>"
                            '<a href="https://s.com">문서</a>')
        self.assertIn("<table>", out)
        self.assertIn("<td>A</td>", out)
        self.assertIn('href="https://s.com"', out)

    def test_void_droptag_does_not_swallow_body(self):
        # <meta>/<link>(닫는 태그 없는 void)를 드롭 카운터로 세어 이후 본문이 통째로
        # 사라지던 버그 회귀 — 실제 HTML 메일은 head 에 <meta charset> 이 거의 항상 있다.
        out = sanitize_html(
            '<html><head><meta charset="utf-8"><title>t</title></head>'
            "<body><table><tr><td>내용</td></tr></table></body></html>")
        self.assertIn("<table>", out)
        self.assertIn("내용", out)
        # 최상위 void 드롭태그 뒤 본문도 보존
        self.assertIn("본문", sanitize_html('<meta charset="utf-8"><p>본문</p>'))
        self.assertIn("본문", sanitize_html('<link rel="x" href="y"><p>본문</p>'))

    def test_void_endtag_does_not_leak_dropped_subtree(self):
        # 시작/종료 대칭: 드롭 서브트리 속 stray </link></base> 가 드롭을 조기
        # 종료해 내용을 흘리면 안 됨 (void 시작만 안 세던 비대칭 회귀 방지).
        self.assertNotIn("LEAK", sanitize_html(
            "<object>y</link>LEAK1</object><p>ok</p>"))
        self.assertNotIn("LEAK", sanitize_html(
            "<noscript>z</base>LEAK2</noscript><p>ok</p>"))
        self.assertIn("ok", sanitize_html("<object>y</link>x</object><p>ok</p>"))

    def test_droptree_still_removes_script_style_after_fix(self):
        # 수정 후에도 script/style/head 는 자식까지 제거되어야 한다(void 아님)
        self.assertNotIn("alert", sanitize_html("<script>alert(1)</script><p>ok</p>"))
        self.assertNotIn("color", sanitize_html("<style>.x{color:red}</style><p>ok</p>"))
        keep = sanitize_html(
            "<head><style>a{}</style><title>t</title></head><body><p>본문</p></body>")
        self.assertIn("본문", keep)
        self.assertNotIn("title", keep)

    # ---- 인용 라벨 절단 (#2/#3)

    def test_quote_cut_single_node(self):
        out = sanitize_html("<div>회신입니다</div>"
                            "<div>-----원본 메시지-----</div><div>이전 내용</div>")
        self.assertIn("회신입니다", out)
        self.assertNotIn("원본 메시지", out)
        self.assertNotIn("이전 내용", out)

    def test_quote_cut_primary_form_balances_tags(self):
        # 1차 관측 형태: 대시 텍스트 + <b>라벨</b> + 대시 텍스트
        out = sanitize_html(
            "<div>본문</div>"
            "<div>--------- <b>Original Message</b> ---------</div>"
            "<p>이전 <b>내용</b></p>")
        self.assertIn("본문", out)
        self.assertNotIn("이전", out)
        self.assertNotIn("Original Message", out)
        for t in ("div", "b", "p"):
            self.assertEqual(out.count(f"<{t}>"), out.count(f"</{t}>"), msg=t)

    def test_quote_cut_split_fragments(self):
        # 대시/라벨/대시가 각각 별도 태그 (#3)
        out = sanitize_html(
            "<p>본문입니다</p><div>---------</div>"
            "<div>Original Message</div><div>---------</div><div>이전 내용</div>")
        self.assertIn("본문입니다", out)
        self.assertNotIn("이전 내용", out)

    def test_quote_cut_asterisk_text_chunk(self):
        # 별표가 텍스트로 남은 "--------- **Original Message** ---------"
        out = sanitize_html(
            "<div>본문</div>"
            "<div>--------- **Original Message** ---------</div><div>이전</div>")
        self.assertIn("본문", out)
        self.assertNotIn("이전", out)

    def test_quote_cut_outlook_underscore_header(self):
        # 라벨 없는 한국어 Outlook 답장: "____" 구분선 뒤 "보낸 사람:" 헤더 블록.
        # 텍스트 경로(new_content)만 잘리고 HTML 경로는 안 잘리던 회귀 가드.
        out = sanitize_html(
            "<p>회신 본문입니다.</p>"
            "<p>________________________________<br>"
            "보낸 사람: 김민수 &lt;kim@corp.example&gt;<br>"
            "보낸 날짜: 2026년 7월 3일<br>"
            "받는 사람: 나<br>"
            "제목: RE: 테스트</p>"
            "<p>이전 인용 본문입니다.</p>")
        self.assertIn("회신 본문입니다", out)
        self.assertNotIn("보낸 사람", out)
        self.assertNotIn("이전 인용 본문", out)
        self.assertEqual(out.count("<p>"), out.count("</p>"))

    def test_underscore_rule_without_header_preserved(self):
        # 구분선만 있고 뒤에 헤더가 아니면 과잉 절단 금지 (본문 보존)
        out = sanitize_html(
            "<p>본문 위</p>"
            "<p>________________________________<br>본문 아래 계속</p>")
        self.assertIn("본문 아래 계속", out)

    def test_signature_dashes_preserved(self):
        # 라벨이 안 오면 보류분 flush — 서명 구분선 보존
        out = sanitize_html("<div>본문</div><div>-----</div><div>홍길동 드림</div>")
        self.assertIn("-----", out)
        self.assertIn("홍길동 드림", out)

    def test_bare_label_sentence_not_cut_html(self):
        out = sanitize_html("<p>Original Message 항목을 참고하세요</p><p>다음 내용</p>")
        self.assertIn("다음 내용", out)

    def test_pend_overflow_flushes(self):
        # 대시 조각 폭주 시 보류 상한(16) 넘으면 강제 방출
        out = sanitize_html("<div>본문</div>" + "<div>--</div>" * 20 + "<div>끝</div>")
        self.assertIn("끝", out)
        self.assertIn("--", out)


class TestNoiseFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(
            home=Path("."),
            ignore_senders=["noreply", "jira@"],
            internal_domains=["corp.example"],
        )

    def test_system_senders_are_noise(self):
        self.assertTrue(self.cfg.is_noise("noreply-hr@corp.example"))
        self.assertTrue(self.cfg.is_noise("jira@corp.example"))

    def test_external_spam_is_noise(self):
        self.assertTrue(self.cfg.is_noise("promo@shopdeals.example"))

    def test_internal_colleague_not_noise(self):
        self.assertFalse(self.cfg.is_noise("minsu.kim@corp.example"))
        self.assertFalse(self.cfg.is_noise("kim@dev.corp.example"))  # 하위 도메인

    def test_no_internal_domains_allows_external(self):
        cfg = Config(home=Path("."), ignore_senders=["noreply"])
        self.assertFalse(cfg.is_noise("partner@vendor.example"))


class TestBlocklist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME],
                          internal_domains=["corp.example"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_noise_includes_blocked(self):
        self.cfg.blocked_senders = ["annoying@corp.example"]
        self.assertTrue(self.cfg.is_noise("annoying@corp.example"))
        self.assertTrue(self.cfg.is_blocked("ANNOYING@corp.example"))  # 소문자 매치
        self.assertFalse(self.cfg.is_noise("kim@corp.example"))

    def test_add_and_remove_blocked_roundtrip(self):
        from mailkb import config as cfgmod
        self.assertTrue(cfgmod.add_blocked(self.cfg, "Spam@Vendor.example"))
        self.assertFalse(cfgmod.add_blocked(self.cfg, "spam@vendor.example"))  # 중복
        self.assertIn("spam@vendor.example", self.cfg.blocked_senders)
        self.assertTrue(self.cfg.is_noise("spam@vendor.example"))
        # 파일에서 다시 읽어도 반영
        self.assertIn("spam@vendor.example", cfgmod._load_blocklist(self.home))
        # 해제
        self.assertTrue(cfgmod.remove_blocked(self.cfg, "spam@vendor.example"))
        self.assertNotIn("spam@vendor.example", self.cfg.blocked_senders)
        self.assertFalse(cfgmod.remove_blocked(self.cfg, "nope@x.example"))


def _rec(mid, sender, to, subject, when, body="본문", reply_to="", is_me=False):
    return MailRecord(
        message_id=f"<{mid}@t>",
        subject=subject,
        sender_name=sender.split("@")[0],
        sender_addr=sender,
        to=to,
        sent_on=when,
        body_text=body,
        in_reply_to=f"<{reply_to}@t>" if reply_to else "",
        references=[f"<{reply_to}@t>"] if reply_to else [],
    )


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_threading_by_references(self):
        self.store.ingest([
            _rec("a1", "kim@c", [ME], "일정 협의", "2026-07-01T09:00:00"),
            _rec("a2", ME, ["kim@c"], "RE: 일정 협의", "2026-07-01T10:00:00", reply_to="a1"),
            _rec("b1", "lee@c", [ME], "다른 건", "2026-07-01T11:00:00"),
        ])
        s = self.store.stats()
        self.assertEqual(s["messages"], 3)
        self.assertEqual(s["threads"], 2)

    def test_threading_by_subject_fallback(self):
        # References 없이 제목만으로 스레드 병합 (30일 창)
        self.store.ingest([
            _rec("c1", "kim@c", [ME], "발주 문의", "2026-07-01T09:00:00"),
            _rec("c2", "kim@c", [ME], "RE: 발주 문의", "2026-07-02T09:00:00"),
        ])
        self.assertEqual(self.store.stats()["threads"], 1)

    def test_threads_last_date_index(self):
        # 스레드 목록 정렬용 인덱스 존재 + 플래너가 실제 사용(전수 스캔+임시정렬 회피)
        idx = {r["name"] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_threads_last_date", idx)
        plan = " ".join(r[3] for r in self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM threads "
            "WHERE (hidden IS NULL OR hidden=0) ORDER BY last_date DESC LIMIT 50"))
        self.assertIn("idx_threads_last_date", plan)
        self.assertNotIn("TEMP B-TREE", plan)      # 임시 정렬 없음

    def test_derived_state_incremental_out_of_order_and_read(self):
        self.store.ingest([
            _rec("ds2", "kim@c", [ME], "상태", "2026-07-02T09:00:00",
                 body="내일까지 회신 부탁드립니다."),
            _rec("ds1", "kim@c", [ME], "RE: 상태", "2026-07-01T09:00:00",
                 reply_to="ds2"),
            _rec("ds3", ME, ["kim@c"], "RE: 상태", "2026-07-03T09:00:00",
                 reply_to="ds2"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<ds2@t>'").fetchone()[0]
        state = self.store.db.execute(
            "SELECT * FROM thread_state WHERE thread_id=?", (tid,)).fetchone()
        first_mid = self.store.db.execute(
            "SELECT message_id FROM messages WHERE id=?",
            (state["first_message_id"],)).fetchone()[0]
        latest_mid = self.store.db.execute(
            "SELECT message_id FROM messages WHERE id=?",
            (state["latest_message_id"],)).fetchone()[0]
        self.assertEqual((first_mid, latest_mid), ("<ds1@t>", "<ds3@t>"))
        self.assertEqual(state["message_count"], 3)
        self.assertEqual((state["received_count"], state["sent_count"]), (2, 1))
        self.assertEqual(state["deadline_count"], 1)
        self.assertEqual(state["unread_received_count"], 2)
        self.store.mark_thread_read(tid)
        unread = self.store.db.execute(
            "SELECT unread_received_count FROM thread_state WHERE thread_id=?",
            (tid,)).fetchone()[0]
        self.assertEqual(unread, 0)
        indexes = {r["name"] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_messages_thread_date", indexes)

    def test_derived_state_backfills_existing_messages(self):
        self.store.ingest([
            _rec("bf1", "kim@c", [ME], "백필", "2026-07-01T09:00:00",
                 body="금요일까지 검토 부탁드립니다."),
        ])
        path = self.store.db_path
        self.store.db.execute("DELETE FROM message_features")
        self.store.db.execute("DELETE FROM thread_state")
        self.store.db.execute("DELETE FROM sync_state WHERE key='derived_version'")
        self.store.db.commit()
        self.store.close()
        self.store = Store(path, [ME])
        feature = self.store.db.execute(
            "SELECT has_deadline, has_decision FROM message_features").fetchone()
        state = self.store.db.execute(
            "SELECT message_count, deadline_count FROM thread_state").fetchone()
        self.assertEqual(tuple(feature), (1, 1))
        self.assertEqual(tuple(state), (1, 1))

    def test_ingest_failure_rolls_back_message_and_derived_state(self):
        with mock.patch("mailkb.store.classify_message",
                        side_effect=RuntimeError("feature failure")):
            with self.assertRaises(RuntimeError):
                self.store.ingest([
                    _rec("rb1", "kim@c", [ME], "롤백", "2026-07-01T09:00:00")])
        for table in ("messages", "threads", "message_features", "thread_state"):
            count = self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, msg=table)

    def test_date_range_queries_exact_boundaries(self):
        # date(sent_on)=? → 범위 재작성이 [일, 다음날) 경계에서 정확히 등가인지
        self.store.ingest([
            _rec("p0", "kim@c", [ME], "전날 자정직전", "2026-07-14T23:59:59"),
            _rec("p1", "kim@c", [ME], "당일 자정", "2026-07-15T00:00:00"),
            _rec("p2", "lee@c", [ME], "당일 낮", "2026-07-15T13:30:00"),
            _rec("p3", "kim@c", [ME], "당일 자정직전", "2026-07-15T23:59:59"),
            _rec("p4", "kim@c", [ME], "다음날 자정", "2026-07-16T00:00:00"),
            _rec("s1", ME, ["kim@c"], "내가 보낸 당일", "2026-07-15T09:00:00"),
        ])
        recv = self.store.received_on_date("2026-07-15")
        self.assertEqual([r["subject"] for r in recv],
                         ["당일 자정", "당일 낮", "당일 자정직전"])   # p1,p2,p3 정렬순
        sent = self.store.sent_on_date("2026-07-15")
        self.assertEqual([r["subject"] for r in sent], ["내가 보낸 당일"])
        # threads_active_on: 당일 활동 스레드(p1,p2,p3,s1) — 전날 p0·다음날 p4 제외
        on = set(self.store.threads_active_on("2026-07-15"))
        self.assertEqual(len(on), 4)
        # between [14,15] 양끝 포함: p0 포함, p4(16일) 제외
        btw = set(self.store.threads_active_between("2026-07-14", "2026-07-15"))
        self.assertEqual(len(btw), 5)

    def test_hidden_thread_unhides_on_new_inbound(self):
        # 숨긴 스레드에 새 수신 메일이 오면 자동 숨김 해제 (구 추적제외의 복귀 흡수)
        self.store.ingest([_rec("d1", "kim@c", [ME], "질문 있습니다", "2026-07-01T09:00:00")])
        tid = self.store.message("1")["thread_id"]
        self.store.hide_thread(tid, True)
        self.assertEqual(self.store.unanswered(days=3650), [])
        # 같은 스레드(references)로 새 수신 메일 도착
        self.store.ingest([
            _rec("d2", "kim@c", [ME], "RE: 질문 있습니다", "2026-07-02T09:00:00", reply_to="d1"),
        ])
        self.assertEqual(self.store.thread(tid)["hidden"], 0)
        subjects = [r["subject"] for r in self.store.unanswered(days=3650)]
        self.assertIn("RE: 질문 있습니다", subjects)

    def test_hidden_stays_hidden_on_my_reply(self):
        # 내가 보낸 답장(is_sent=1)은 숨김 해제 트리거가 아님
        self.store.ingest([_rec("e1", "kim@c", [ME], "확인 요청", "2026-07-01T09:00:00")])
        tid = self.store.message("1")["thread_id"]
        self.store.hide_thread(tid, True)
        self.store.ingest([
            _rec("e2", ME, ["kim@c"], "RE: 확인 요청", "2026-07-02T09:00:00", reply_to="e1"),
        ])
        self.assertEqual(self.store.thread(tid)["hidden"], 1)
        self.assertEqual(self.store.unanswered(days=3650), [])

    def test_unanswered_detection(self):
        self.store.ingest([
            # 스레드 1: 내가 마지막 답장 → 미답변 아님
            _rec("d1", "kim@c", [ME], "완료 건", "2026-07-03T09:00:00"),
            _rec("d2", ME, ["kim@c"], "RE: 완료 건", "2026-07-03T10:00:00", reply_to="d1"),
            # 스레드 2: 수신이 마지막, To=나 → 미답변
            _rec("e1", "lee@c", [ME], "대기 건", "2026-07-03T11:00:00"),
            # 스레드 3: 수신이 마지막이지만 To 에 내가 없음(참조만) → 제외
            _rec("f1", "choi@c", ["kim@c"], "참조 건", "2026-07-03T12:00:00"),
        ])
        un = self.store.unanswered(days=3650)
        subjects = [r["subject"] for r in un]
        self.assertIn("대기 건", subjects)
        self.assertNotIn("완료 건", subjects)
        self.assertNotIn("참조 건", subjects)

    def test_is_sent_flag(self):
        self.store.ingest([_rec("g1", ME, ["kim@c"], "발신", "2026-07-03T09:00:00")])
        m = self.store.message("1")
        self.assertEqual(m["is_sent"], 1)

    def test_dedup_by_message_id(self):
        recs = [_rec("h1", "kim@c", [ME], "중복", "2026-07-03T09:00:00")]
        self.store.ingest(recs)
        stats = self.store.ingest(recs)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(self.store.stats()["messages"], 1)

    def test_search_korean(self):
        self.store.ingest([
            _rec("i1", "kim@c", [ME], "부품 수급", "2026-07-03T09:00:00",
                 body="MCU 납기가 지연되고 있습니다."),
        ])
        rows = self.store.search("납기가 지연")
        self.assertEqual(len(rows), 1)

    def test_top_senders_ranks_by_volume(self):
        self.store.ingest([
            _rec("p1", "kim@c", [ME], "a", "2026-07-01T09:00:00"),
            _rec("p2", "kim@c", [ME], "b", "2026-07-01T10:00:00"),
            _rec("p3", ME, ["kim@c"], "c", "2026-07-01T11:00:00"),   # 내가 kim 에게
            _rec("p4", "lee@c", [ME], "d", "2026-07-01T12:00:00"),
        ])
        rows = self.store.top_senders()
        self.assertEqual(rows[0]["addr"], "kim@c")     # from_count 최다
        self.assertEqual(rows[0]["from_count"], 2)
        self.assertEqual(rows[0]["to_count"], 1)       # 내가 kim 에게 1회
        addrs = [r["addr"] for r in rows]
        self.assertNotIn(ME, addrs)                    # 내 주소는 people 에 없음


class TestMidJoinPreserve(unittest.TestCase):
    """mid-join 인용 보존 — 스레드 첫 보유 메일의 인용 체인은 유일본이라
    절단하지 않는다 (텍스트=PRESERVED_MARK, HTML=qfold 접힘). 기존 스레드
    합류분은 종전대로 절단. docs/PROPOSAL-midjoin.md."""

    _KQ = ("________________________________\n"
           "보낸 사람: 강미래 <kang@corp.example>\n"
           "제목: 원 건\n받는 사람: 오태양\n\n"
           "원 논의 내용입니다.\n> 더 이전 인용\n--\n강미래 선임")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------- clean 계층
    def test_extract_preserve_marker_and_tail(self):
        body = "합류 안내드립니다.\n> 인라인 인용\n\n" + self._KQ
        out = extract_new_content(body, preserve_quotes=True)
        self.assertIn("합류 안내드립니다", out)
        self.assertNotIn("인라인 인용", out)          # 신규분의 > 줄은 종전대로 제거
        self.assertIn(PRESERVED_MARK, out)
        tail = out.split(PRESERVED_MARK)[1]
        self.assertIn("원 논의 내용입니다", tail)
        self.assertIn("> 더 이전 인용", tail)          # 보존부 > 줄 유지
        self.assertIn("강미래 선임", tail)             # 보존부 서명 유지 (체인의 일부)
        # 기본(비보존)은 종전 동작 그대로
        cut = extract_new_content(body)
        self.assertNotIn(PRESERVED_MARK, cut)
        self.assertNotIn("원 논의 내용", cut)

    def test_extract_preserve_noop_without_quotes(self):
        self.assertEqual(extract_new_content("네, 알겠습니다.", preserve_quotes=True),
                         "네, 알겠습니다.")

    def test_extract_preserve_quote_only_body(self):
        out = extract_new_content(self._KQ, preserve_quotes=True)
        self.assertTrue(out.startswith(PRESERVED_MARK))

    def test_strip_preserved(self):
        out = extract_new_content("본문.\n\n" + self._KQ, preserve_quotes=True)
        self.assertEqual(strip_preserved(out), "본문.")
        self.assertEqual(strip_preserved("마커 없는 본문"), "마커 없는 본문")

    def test_sanitize_preserve_fold_split_label(self):
        html = ("<p>회신입니다</p>"
                "<div>--------- </div><div><b>Original Message</b></div>"
                "<div> ---------</div><p>From: 김도현</p><p>이전 인용 내용</p>")
        out = sanitize_html(html, preserve_quotes=True)
        self.assertIn("details class='qfold'", out)
        self.assertIn("이전 인용 내용", out)
        self.assertTrue(out.endswith("</details>"))    # 폴드 닫힘 균형
        self.assertNotIn("이전 인용 내용", sanitize_html(html))  # 기본은 절단

    def test_sanitize_preserve_korean_header(self):
        html = ("<p>본문입니다</p><div>________________________________</div>"
                "<div>보낸 사람: 김민수</div><div>이전 본문 텍스트</div>")
        out = sanitize_html(html, preserve_quotes=True)
        self.assertIn("qfold", out)
        self.assertIn("보낸 사람", out)
        self.assertIn("이전 본문 텍스트", out)

    def test_sanitize_preserve_single_fold_for_nested_labels(self):
        html = ("<p>본문</p><p>-----원본 메시지-----</p><p>중간 인용</p>"
                "<p>-----원본 메시지-----</p><p>더 깊은 인용</p>")
        out = sanitize_html(html, preserve_quotes=True)
        self.assertEqual(out.count("<details"), 1)     # 폴드는 메일당 하나
        self.assertIn("중간 인용", out)
        self.assertIn("더 깊은 인용", out)

    # ---------------------------------------------------------- store 계층
    def test_first_holding_preserves_then_replies_cut(self):
        # References 가 미보유 메일(ghost)을 가리킴 — 새 스레드 = 내 첫 보유분
        self.store.ingest([_rec("j1", "kang@c", [ME], "RE: 원 건",
                                "2026-07-01T09:00:00",
                                body="합류 안내드립니다.\n\n" + self._KQ,
                                reply_to="ghost")])
        m = self.store.message("1")
        self.assertIn(PRESERVED_MARK, m["new_content"])
        self.assertIn("원 논의 내용", m["new_content"])
        # 후속 답장은 기존 스레드 합류 — 종전대로 절단 (중복 제거)
        self.store.ingest([_rec("j2", "lee@c", [ME], "RE: 원 건",
                                "2026-07-01T10:00:00",
                                body="후속 답변입니다.\n\n" + self._KQ,
                                reply_to="j1")])
        m2 = self.store.message("2")
        self.assertNotIn(PRESERVED_MARK, m2["new_content"])
        self.assertNotIn("원 논의 내용", m2["new_content"])
        self.assertEqual(self.store.stats()["threads"], 1)

    def test_preserved_text_is_searchable(self):
        self.store.ingest([_rec("j1", "kang@c", [ME], "RE: 원 건",
                                "2026-07-01T09:00:00",
                                body="합류 안내드립니다.\n\n" + self._KQ,
                                reply_to="ghost")])
        self.assertEqual(len(self.store.search("원 논의 내용")), 1)

    def test_html_fold_stored_and_prune_parity(self):
        body = "본문입니다.\n\n-----원본 메시지-----\nFrom: 강미래\n이전 본문 텍스트"
        html = ("<p>본문입니다.</p><p>-----원본 메시지-----</p>"
                "<p>From: 강미래</p><p>이전 본문 텍스트</p>")
        self.store.ingest([MailRecord(
            message_id="<h1@t>", subject="신규 건", sender_name="kang",
            sender_addr="kang@c", to=[ME], sent_on="2026-01-01T09:00:00",
            body_text=body, body_html=html)])
        tid = self.store.message("1")["thread_id"]
        row = self.store.thread_messages(tid)[0]
        self.assertIn("qfold", row["body_html"])       # HTML 층 접힘 저장
        self.assertIn("이전 본문 텍스트", row["body_html"])
        # 프룬(이미지 없음 → 행 삭제) 후에도 텍스트 층 마커로 접힘 재현
        self.store._prune_html(30)
        row = self.store.thread_messages(tid)[0]
        self.assertFalse(row["body_html"])
        out = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("qfold", out)
        self.assertIn("이전 본문 텍스트", out)

    # ------------------------------------------------------- 신호·fetch 계층
    def test_preserved_quote_not_deadline_signal(self):
        cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])
        self.store.ingest([_rec("d1", "park@c", [ME], "일정 공유",
                                "2026-07-02T09:00:00",
                                body="공유드립니다.\n\n" + self._KQ.replace(
                                    "원 논의 내용입니다.",
                                    "7월 21일까지 회신 부탁드립니다."))])
        self.assertEqual(review.deadline_signals(self.store, cfg, "2026-07-02"), [])
        # 신규 작성분의 기한은 여전히 잡힌다
        self.store.ingest([_rec("d2", "park@c", [ME], "다른 건",
                                "2026-07-02T10:00:00",
                                body="7월 21일까지 회신 부탁드립니다.")])
        self.assertEqual(
            len(review.deadline_signals(self.store, cfg, "2026-07-02")), 1)

    def test_outlook_fetch_merges_folders_chronologically(self):
        # COM 없이 병합 로직만 — inbox·sent 가 각자 시간순일 때 전역 시간순 yield
        from mailkb.sources.outlook_com import OutlookComSource
        inbox = [_rec(f"i{d}", "kim@c", [ME], "s", f"2026-07-0{d}T09:00:00")
                 for d in (1, 3, 5)]
        sent = [_rec(f"s{d}", ME, ["kim@c"], "s", f"2026-07-0{d}T10:00:00")
                for d in (2, 4)]

        class _Stub:
            def _folder_stream(self, fc, name, since, cutoff):
                return iter(inbox if name == "inbox" else sent)

        got = [r.sent_on for r in OutlookComSource.fetch(_Stub(), None)]
        self.assertEqual(len(got), 5)
        self.assertEqual(got, sorted(got))


class TestInlineImages(unittest.TestCase):
    """인라인 이미지 수명주기 — 주입(정제 후)·컷오프 게이트·프룬 마커·렌더."""

    PNG = ("image/png", b"\x89PNG-fake-bytes-0123456789")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          internal_domains=["corp.example"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _img_rec(self, mid, when, cids=("a@x",), dup=False):
        imgs = "".join(f'<img src="cid:{c}">' for c in cids)
        if dup:
            imgs += f'<img src="cid:{cids[0]}">'
        return MailRecord(
            message_id=f"<{mid}@t>", subject=f"이미지건 {mid}",
            sender_name="kim", sender_addr="kim@corp.example", to=[ME],
            sent_on=when, body_text="파형 공유드립니다.",
            body_html=f"<p>파형 공유</p>{imgs}",
            inline_images={c: self.PNG for c in cids})

    def _html(self, mid):
        r = self.store.db.execute(
            "SELECT h.html FROM message_html h JOIN messages m ON m.id=h.message_id "
            "WHERE m.message_id=?", (f"<{mid}@t>",)).fetchone()
        return r["html"] if r else None

    def test_inject_after_sanitize_with_dedup_and_fail(self):
        from mailkb.clean import inject_inline_images, sanitize_html
        html = sanitize_html(
            '<p>공유</p><img src="cid:W1@X"><img src="cid:W1@X"><img src="cid:none@x">')
        out, n, fail = inject_inline_images(html, {"<w1@x>": self.PNG})
        self.assertEqual((n, fail), (1, 1))         # 정규화 매칭 · 실패 집계
        self.assertEqual(out.count("data:image/png;base64,"), 1)
        self.assertIn("중복 이미지 생략", out)       # 메일 내 중복 1회만
        self.assertIn('data-blocked-src="cid:none@x"', out)  # 실패 → 차단 마크 유지

    def test_ingest_embeds_and_counts(self):
        stats = self.store.ingest([self._img_rec("n1", "2026-07-10T09:00:00",
                                                 cids=("a@x", "b@x"), dup=True)])
        self.assertEqual((stats.img_embedded, stats.img_failed), (2, 0))
        html = self._html("n1")
        self.assertEqual(html.count("data:image/"), 2)
        self.assertIn("중복 이미지 생략", html)

    def test_ingest_cutoff_gate_skips_old(self):
        stats = self.store.ingest(
            [self._img_rec("o1", "2026-05-01T09:00:00")],
            image_cutoff="2026-06-01")
        self.assertEqual(stats.img_embedded, 0)      # 컷오프 이전 — 임베드 스킵
        self.assertIn('data-blocked-src="cid:', self._html("o1"))

    def test_prune_marker_delete_and_marker_survives(self):
        old_day = (date.today() - timedelta(days=20)).isoformat()
        self.store.ingest([
            self._img_rec("img", f"{old_day}T09:00:00"),          # 이미지 → 마커
            MailRecord(message_id="<txt@t>", subject="서식만",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on=f"{old_day}T10:00:00",
                       body_text="표 있는 본문", body_html="<p><b>표</b> 본문</p>"),
            self._img_rec("new", "%sT09:00:00" % date.today().isoformat()),
        ])
        res = self.store.maybe_prune_html(14)
        self.assertEqual(res, (1, 1))                # 마커 1 · 삭제 1
        self.assertIn("이미지 1장", self._html("img"))
        self.assertIn("보존 기간(14일)", self._html("img"))
        self.assertIsNone(self._html("txt"))         # 서식 HTML 회수
        self.assertIn("data:image/", self._html("new"))  # 최근은 유지
        # 하루 1회 가드 — 같은 날 '같은 설정' 재호출은 None
        self.assertIsNone(self.store.maybe_prune_html(14))
        # 같은 날이라도 보존 기간을 바꾸면 즉시 재실행 (설정 변경 반영)
        self.assertIsNotNone(self.store.maybe_prune_html(10))
        # 마커는 다음날 프룬에서도 보존 (재프룬 금지)
        self.store.set_state("last_image_prune", "2000-01-01")
        self.assertEqual(self.store.maybe_prune_html(14), (0, 0))
        self.assertIn("이미지 1장", self._html("img"))

    def test_web_sync_failure_still_prunes(self):
        # Outlook 꺼짐 등 수집 실패에도 프룬(COM 불필요)은 실행된다 (_do_sync 보장)
        from mailkb import web
        old_day = (date.today() - timedelta(days=20)).isoformat()
        self.store.ingest([self._img_rec("img", f"{old_day}T09:00:00")])
        self.cfg.raw = {"web": {"image_retain_days": 14}}
        self.cfg.source = "fake"
        with mock.patch("mailkb.sources.fake.FakeSource.fetch",
                        side_effect=RuntimeError("COM down")):
            try:
                web._do_sync(self.store, self.cfg)
            except RuntimeError:
                pass                                  # 수집 실패는 잡이 안내
        self.assertIn("이미지 1장", self._html("img"))  # 프룬은 됐음

    def test_td_only_table_gets_delimiter_and_renders(self):
        # Outlook 표 전형(th 없음): 변환기가 구분행 삽입 + 렌더러 표 렌더
        from mailkb.clean import html_to_markdown
        from mailkb.web import _looks_like_markdown, _mail_md_to_html
        md = html_to_markdown("<table><tr><td>단계</td><td>기한</td></tr>"
                              "<tr><td>GDS</td><td>8/20</td></tr></table>")
        self.assertIn("| --- | --- |", md)
        self.assertTrue(_looks_like_markdown(md))
        self.assertIn("<td>GDS</td>", _mail_md_to_html(md))
        # 구버전 저장분(구분행 없음)도 렌더러가 표로 인식 (재수집 불필요)
        legacy = "| 단계 | 기한 |\n| GDS | 8/20 |"
        self.assertTrue(_looks_like_markdown(legacy))
        out = _mail_md_to_html(legacy)
        self.assertIn("<table class='md-table'>", out)
        self.assertIn("<td>GDS</td>", out)

    def test_hr_preserved_through_pipeline(self):
        # <hr> → '---' → 렌더 <hr> — 섹션 구분 가독성 보존, 서명 절단('--')과 무충돌
        from mailkb.clean import extract_new_content, html_to_markdown
        from mailkb.web import _mail_md_to_html
        md = html_to_markdown("<p>결정 사항</p><hr><p>참고 사항</p>")
        self.assertIn("---", md)
        kept = extract_new_content(md)
        self.assertIn("참고 사항", kept)             # 절단 안 됨
        self.assertIn("<hr>", _mail_md_to_html(kept))

    def test_pruned_markdown_table_renders_formatted(self):
        # 프룬된 메일의 텍스트(마크다운 표)는 서식 기본 렌더 + 텍스트 토글
        from mailkb import web
        old_day = (date.today() - timedelta(days=20)).isoformat()
        self.store.ingest([MailRecord(
            message_id="<tbl@t>", subject="일정표", sender_name="kim",
            sender_addr="kim@corp.example", to=[ME],
            sent_on=f"{old_day}T09:00:00",
            body_text="일정 공유\n\n| 단계 | 기한 |\n|---|---|\n| GDS | 8/20 |",
            body_html='<p>일정 공유</p><img src="cid:x@y"><table><tr><td>GDS</td></tr></table>',
            inline_images={"x@y": self.PNG})])
        self.store.maybe_prune_html(14)
        tid = self.store.message("1")["thread_id"]
        out = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("class='imgstrip'", out)            # 마커
        self.assertIn("class='md-rich'", out)             # 서식 기본 표시 (CSS 기본값)
        self.assertIn("<table class='md-table'>", out)    # 표 렌더
        self.assertIn("<td>GDS</td>", out)
        self.assertIn("md-toggle", out)                   # 저장 텍스트 검증 토글 (2026-07-13)

    def test_prune_disabled_when_zero(self):
        self.assertIsNone(self.store.maybe_prune_html(0))
        # 컷오프 sentinel: retain 0 → 전부 게이트
        from mailkb.store import image_cutoff_for
        self.assertEqual(image_cutoff_for(0), "9999-12-31")

    def test_render_marker_banner_with_text(self):
        from mailkb import web
        old_day = (date.today() - timedelta(days=20)).isoformat()
        self.store.ingest([self._img_rec("img", f"{old_day}T09:00:00")])
        self.store.maybe_prune_html(14)
        tid = self.store.message("1")["thread_id"]
        out = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("class='imgstrip'", out)        # 마커 배너
        self.assertIn("파형 공유드립니다", out)        # 텍스트 본문 함께
        self.assertNotIn("md-toggle", out)             # 마커는 html 취급 안 함


class TestComInlineCollect(unittest.TestCase):
    """outlook_com._collect_inline_images — 모의 COM 객체로 순수 로직 검증
    (PC 스모크 전에 매칭·MIME·실패 경로를 WSL 에서 보장)."""

    class _PA:
        def __init__(self, cid, raise_=False):
            self._cid, self._raise = cid, raise_

        def GetProperty(self, prop):
            if self._raise:
                raise RuntimeError("no property")
            return self._cid

    class _Att:
        def __init__(self, cid, fname, data=b"IMGDATA", pa_raise=False,
                     save_raise=False):
            self.FileName = fname
            self.PropertyAccessor = TestComInlineCollect._PA(cid, pa_raise)
            self._data, self._save_raise = data, save_raise

        def SaveAsFile(self, path):
            if self._save_raise:
                raise OSError("save failed")
            with open(path, "wb") as f:
                f.write(self._data)

    def test_collect_matching_and_failures(self):
        from mailkb.sources.outlook_com import _collect_inline_images
        html = ('<img src="cid:Wave1@X"><img src="cid:doc1@x">'
                '<img src="cid:broken@x"><img src="cid:gone@x">')
        atts = [
            self._Att("<wave1@x>", "wave.PNG"),            # 매칭(꺾쇠·대소문자)
            self._Att("doc1@x", "report.docx"),            # 이미지 아님 → 실패 집계
            self._Att("broken@x", "b.png", save_raise=True),  # 저장 실패 → 집계
            self._Att("", "noise.png", pa_raise=True),     # ContentID 없음 → 무시
            self._Att("unref@x", "unref.png"),             # HTML 미참조 → 무시
        ]
        out, failed = _collect_inline_images(atts, html)
        self.assertEqual(list(out), ["wave1@x"])
        self.assertEqual(out["wave1@x"], ("image/png", b"IMGDATA"))
        self.assertEqual(failed, 2)                        # docx + 저장 실패

    def test_collect_no_cid_short_circuit(self):
        from mailkb.sources.outlook_com import _collect_inline_images
        called = []

        class _Boom:
            @property
            def PropertyAccessor(self):
                called.append(1)
                raise AssertionError("cid 없으면 첨부를 건드리지 않아야")
        out, failed = _collect_inline_images([_Boom()], "<p>이미지 없음</p>")
        self.assertEqual((out, failed, called), ({}, 0, []))

    def test_collect_end_to_end_with_store(self):
        # 모의 첨부 → MailRecord.inline_images → store 주입까지 전체 경로
        from mailkb.sources.outlook_com import _collect_inline_images
        html = '<p>도면</p><img src="cid:fp1@x">'
        out, _ = _collect_inline_images([self._Att("<FP1@x>", "f.png")], html)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        stats = store.ingest([MailRecord(
            message_id="<c1@t>", subject="도면", sender_name="kim",
            sender_addr="kim@corp.example", to=[ME],
            sent_on="2026-07-10T09:00:00", body_text="도면 공유",
            body_html=html, inline_images=out)])
        self.assertEqual(stats.img_embedded, 1)
        r = store.db.execute("SELECT html FROM message_html").fetchone()
        self.assertIn("data:image/png;base64,", r["html"])


class TestRollingSummarySkip(unittest.TestCase):
    """review.update_rolling_summaries 의 스킵 로직 (AI 호출은 스텁으로 대체)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(
            home=Path(self.tmp.name),
            my_addresses=[ME],
            ignore_senders=["noreply"],
            internal_domains=["corp.example"],
            ai_backends={"internal": {"cmd": ["dummy"]}},
            # 이 클래스는 1통 스레드로 스킵 '사유'를, 3일 창으로 소급을 검증
            raw={"ai": {"summary_min_msgs": 1, "summary_max_days": 3}},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _tid(self, subject):
        row = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject=? LIMIT 1", (subject,)
        ).fetchone()
        return row["thread_id"]

    def test_send_only_thread_is_summarized(self):
        # 발신 전용(수신 0건) 스레드는 스킵되지 않아야 함 (버그 수정 회귀)
        self.store.ingest([
            MailRecord(message_id="<s1@t>", subject="A안 확정 통보",
                       sender_name="me", sender_addr=ME, to=["kim@corp.example"],
                       sent_on="2026-07-04T09:00:00",
                       body_text="최종적으로 A안으로 확정합니다."),
            # 대조군: 수신이 전부 noreply → 스킵되어야 함
            MailRecord(message_id="<n1@t>", subject="자동 알림",
                       sender_name="sys", sender_addr="noreply@corp.example",
                       to=[ME], sent_on="2026-07-04T09:10:00",
                       body_text="자동 발송 알림입니다."),
            # 정상 수신 스레드 → 요약되어야 함
            MailRecord(message_id="<r1@t>", subject="검토 요청",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:20:00",
                       body_text="검토 부탁드립니다."),
        ])
        send_only = self._tid("A안 확정 통보")
        noise = self._tid("자동 알림")
        normal = self._tid("검토 요청")

        with mock.patch("mailkb.review.ai_run", return_value="요약됨") as m:
            result = review.update_rolling_summaries(
                self.store, self.cfg, [send_only, noise, normal], backend=None
            )

        self.assertIn(send_only, result)   # 발신 전용도 요약됨 (수정 핵심)
        self.assertIn(normal, result)
        self.assertNotIn(noise, result)    # 노이즈 수신은 여전히 스킵
        # AI 는 요약 대상 2건에 대해서만 호출
        self.assertEqual(m.call_count, 2)

    def test_strip_summary_header(self):
        s = review.strip_summary_header
        self.assertEqual(s("**갱신된 요약**\n\n납기 확정."), "납기 확정.")
        self.assertEqual(s("**갱신된 요약**  납기 확정."), "납기 확정.")   # 인라인 볼드
        self.assertEqual(s("갱신된 요약: 납기 확정."), "납기 확정.")
        self.assertEqual(s("## 갱신된 요약\n내용"), "내용")
        self.assertEqual(s("납기 확정."), "납기 확정.")                    # 머리말 없음
        # 문장 속 '갱신된 요약'은 오탐하지 않음
        self.assertEqual(s("갱신된 요약본을 첨부합니다."), "갱신된 요약본을 첨부합니다.")

    def test_summary_generation_strips_header(self):
        # 생성 시 머리말이 붙어 와도 저장 전에 제거된다
        self.store.ingest([
            MailRecord(message_id="<h1@t>", subject="헤더건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="검토 부탁드립니다."),
        ])
        tid = self._tid("헤더건")
        with mock.patch("mailkb.review.ai_run",
                        return_value="**갱신된 요약**\n\n검토 요청 대기."):
            review.update_rolling_summaries(
                self.store, self.cfg, [tid], backend="internal")
        self.assertEqual(self.store.thread(tid)["rolling_summary"], "검토 요청 대기.")

    def test_summary_log_written(self):
        # 생성된 요약이 <home>/logs/summary.jsonl 로 누적된다 (b: 품질 분석 재료)
        self.store.ingest([
            MailRecord(message_id="<r1@t>", subject="검토 요청",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:20:00",
                       body_text="검토 부탁드립니다."),
        ])
        tid = self._tid("검토 요청")
        with mock.patch("mailkb.review.ai_run", return_value="스레드 요약본"):
            review.update_rolling_summaries(
                self.store, self.cfg, [tid], backend="internal",
                date_iso="2026-07-04")
        logf = Path(self.tmp.name) / "logs" / "summary.jsonl"
        self.assertTrue(logf.exists())
        self.assertTrue((logf.parent / "ANALYZE-summary.md").exists())
        rec = json.loads(logf.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["date"], "2026-07-04")
        item = rec["items"][0]
        self.assertEqual(item["thread_id"], tid)
        self.assertEqual(item["summary"], "스레드 요약본")
        self.assertEqual(item["new_msgs"], 1)

    def test_summary_log_skips_when_reused(self):
        # 재사용(신규 없음)만 있으면 실행이 아니므로 로그 미기록
        self.store.ingest([
            MailRecord(message_id="<r1@t>", subject="이미요약",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:20:00",
                       body_text="검토 부탁드립니다."),
        ])
        tid = self._tid("이미요약")
        self.store.save_summary(tid, "기존요약", 1)   # 이미 최신까지 요약됨
        with mock.patch("mailkb.review.ai_run", return_value="X") as m:
            review.update_rolling_summaries(
                self.store, self.cfg, [tid], backend="internal",
                date_iso="2026-07-04")
        m.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / "logs" / "summary.jsonl").exists())

    # --- 요약 대상 날짜 창 (마지막 실행 이후) ---

    def test_summary_window_first_run_is_max_days(self):
        # 마커 없음(첫 실행) — 이 클래스 설정(summary_max_days=3)으로 3일 창
        self.assertEqual(review._summary_window(self.store, self.cfg, "2026-07-20"),
                         ("2026-07-18", "2026-07-20"))

    def test_summary_window_max_days_config(self):
        # 기본값(미설정) = 1 → 오늘만 (2026-07-13 기본 3→1)
        self.cfg.raw = {}
        self.assertEqual(review._summary_window(self.store, self.cfg, "2026-07-20"),
                         ("2026-07-20", "2026-07-20"))

    def test_trivial_msg_detection(self):
        for s in ("++김철수 책임", "+ 박수석", "FYI", "fyi.", "참고하세요",
                  "전달드립니다", "공유합니다.", "수신인 추가", ""):
            self.assertTrue(review._is_trivial_msg(s), msg=s)
        for s in ("참고로 B안이 좋겠습니다", "확인했습니다",
                  "++김철수 책임. 일정 관련해 아래와 같이 정리했으니 검토 부탁드립니다. "
                  "세부 항목은 첨부 참조."):
            self.assertFalse(review._is_trivial_msg(s), msg=s)

    def test_summary_min_msgs_ignores_trivial(self):
        # 실질 2통 + '++' 1통 = 실질 2 < 3 → 스킵. 실질 3통째가 오면 요약.
        cfg3 = Config(home=Path(self.tmp.name), my_addresses=[ME],
                      internal_domains=["corp.example"],
                      ai_backends={"internal": {"cmd": ["dummy"]}})
        self.store.ingest([
            _rec("t1", "kim@corp.example", [ME], "티건", "2026-07-19T09:00:00",
                 body="검토 부탁드립니다."),
            _rec("t2", ME, ["kim@corp.example"], "티건", "2026-07-19T10:00:00",
                 body="확인 후 회신드리겠습니다.", reply_to="t1"),
            _rec("t3", "kim@corp.example", [ME], "티건", "2026-07-19T11:00:00",
                 body="++박수석", reply_to="t2"),
        ])
        tid = self._tid("티건")
        with mock.patch.object(review, "ai_run", return_value="요약본") as run:
            res = review.update_rolling_summaries(self.store, cfg3, [tid], "internal")
        self.assertNotIn(tid, res)
        run.assert_not_called()
        self.store.ingest([
            _rec("t4", "kim@corp.example", [ME], "티건", "2026-07-19T12:00:00",
                 body="일정 확정되어 회신드립니다. 세부 자료 첨부합니다.",
                 reply_to="t3"),
        ])
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res2 = review.update_rolling_summaries(self.store, cfg3, [tid], "internal")
        self.assertEqual(res2.get(tid), "요약본")

    def test_summary_skips_when_new_msgs_all_trivial(self):
        # 이미 요약된 스레드에 '++'만 새로 붙으면 AI 콜 없이 재사용, 마커 불변
        self.store.ingest([
            _rec("v1", "kim@corp.example", [ME], "재사용건", "2026-07-19T09:00:00",
                 body="검토 부탁드립니다.")])
        tid = self._tid("재사용건")
        with mock.patch.object(review, "ai_run", return_value="첫요약"):
            review.update_rolling_summaries(self.store, self.cfg, [tid], "internal")
        self.store.ingest([
            _rec("v2", "kim@corp.example", [ME], "재사용건", "2026-07-19T10:00:00",
                 body="++박수석", reply_to="v1")])
        with mock.patch.object(review, "ai_run", return_value="새요약") as run:
            res = review.update_rolling_summaries(self.store, self.cfg, [tid], "internal")
        run.assert_not_called()
        self.assertEqual(res[tid], "첫요약")
        self.assertEqual(self.store.thread(tid)["summary_msg_count"], 1)  # 마커 불변

    def test_summary_short_thread_with_rich_content_included(self):
        # 통수가 적어도 실질 본문이 충분(기본 1000자+)하면 요약 대상 (장문 1통)
        cfg3 = Config(home=Path(self.tmp.name), my_addresses=[ME],
                      internal_domains=["corp.example"],
                      ai_backends={"internal": {"cmd": ["dummy"]}})
        long_body = "제안 배경과 세부 일정 정리입니다. " + ("상세 항목 설명 " * 140)
        self.store.ingest([_rec("rich1", "kim@corp.example", [ME], "기획안",
                                "2026-07-19T09:00:00", body=long_body)])
        tid = self._tid("기획안")
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res = review.update_rolling_summaries(self.store, cfg3, [tid], "internal")
        self.assertEqual(res.get(tid), "요약본")
        # summary_min_chars=0 → 내용 우회로 끔: 장문 1통도 통수 문턱에 걸림
        cfg0 = Config(home=Path(self.tmp.name), my_addresses=[ME],
                      internal_domains=["corp.example"],
                      ai_backends={"internal": {"cmd": ["dummy"]}},
                      raw={"ai": {"summary_min_chars": 0}})
        self.store.ingest([_rec("rich2", "kim@corp.example", [ME], "기획안2",
                                "2026-07-19T10:00:00", body=long_body)])
        tid2 = self._tid("기획안2")
        with mock.patch.object(review, "ai_run", return_value="요약본") as run:
            res2 = review.update_rolling_summaries(self.store, cfg0, [tid2], "internal")
        self.assertNotIn(tid2, res2)
        run.assert_not_called()

    def test_summary_flagged_bypasses_threshold(self):
        # 플래그 스레드는 1통·단문이어도 요약 (길이 문턱 면제)
        cfg3 = Config(home=Path(self.tmp.name), my_addresses=[ME],
                      internal_domains=["corp.example"],
                      ai_backends={"internal": {"cmd": ["dummy"]}})
        self.store.ingest([_rec("fl1", "kim@corp.example", [ME], "플래그건",
                                "2026-07-19T09:00:00", body="짧은 확인 요청입니다.")])
        tid = self._tid("플래그건")
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res = review.update_rolling_summaries(self.store, cfg3, [tid], "internal")
        self.assertNotIn(tid, res)                      # 문턱에 걸림
        self.store.set_flag(tid, True)
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res2 = review.update_rolling_summaries(self.store, cfg3, [tid], "internal")
        self.assertEqual(res2.get(tid), "요약본")        # 플래그 → 면제

    def test_summary_min_msgs_threshold(self):
        # 기본(3통 미만 스킵): 한두 통은 원문이 곧 요약이라 콜 낭비 — 문턱으로 차단
        cfg_default = Config(home=Path(self.tmp.name), my_addresses=[ME],
                             internal_domains=["corp.example"],
                             ai_backends={"internal": {"cmd": ["dummy"]}})
        self.store.ingest([
            _rec("s1", "kim@corp.example", [ME], "짧은건", "2026-07-19T09:00:00"),
            _rec("l1", "kim@corp.example", [ME], "긴건", "2026-07-19T10:00:00"),
            _rec("l2", ME, ["kim@corp.example"], "긴건",
                 "2026-07-19T11:00:00", reply_to="l1"),
            _rec("l3", "kim@corp.example", [ME], "긴건",
                 "2026-07-19T12:00:00", reply_to="l2"),
        ])
        short_t, long_t = self._tid("짧은건"), self._tid("긴건")
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res = review.update_rolling_summaries(
                self.store, cfg_default, [short_t, long_t], "internal")
        self.assertNotIn(short_t, res)              # 1통 → 스킵
        self.assertEqual(res.get(long_t), "요약본")  # 3통 → 요약
        # ai.summary_min_msgs=1 이면 기존 동작 (이 클래스 self.cfg 가 그 설정)
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            res2 = review.update_rolling_summaries(
                self.store, self.cfg, [short_t], "internal")
        self.assertEqual(res2.get(short_t), "요약본")

    def test_summary_window_since_last_within_cap(self):
        # 마지막 실행이 상한 안(3일 내)이면 그 이후부터
        self.store.set_state("last_summary", "2026-07-19")
        self.assertEqual(review._summary_window(self.store, self.cfg, "2026-07-20"),
                         ("2026-07-19", "2026-07-20"))

    def test_summary_window_caps_long_gap_at_max_days(self):
        # 오래 비워도 최근 3일만 (07-10 마커여도 07-18~07-20)
        self.store.set_state("last_summary", "2026-07-10")
        self.assertEqual(review._summary_window(self.store, self.cfg, "2026-07-20"),
                         ("2026-07-18", "2026-07-20"))

    def test_summary_window_backfill_does_not_rewind(self):
        # 과거 --date 백필은 그날만, 마커는 안 되감김
        self.store.set_state("last_summary", "2026-07-20")
        self.assertEqual(review._summary_window(self.store, self.cfg, "2026-07-18"),
                         ("2026-07-18", "2026-07-18"))

    def test_refresh_summaries_advances_marker_and_covers_skipped_day(self):
        # 마지막 실행 07-17, 건너뛴 07-18 활동을 07-20 실행이 소급 요약 + 마커 전진
        self.store.set_state("last_summary", "2026-07-17")
        self.store.ingest([
            MailRecord(message_id="<s@t>", subject="건너뛴날", sender_name="kim",
                       sender_addr="kim@corp.example", to=[ME],
                       sent_on="2026-07-18T09:00:00", body_text="검토 부탁드립니다."),
        ])
        tid = self._tid("건너뛴날")
        with mock.patch.object(review, "ai_run", return_value="요약본"):
            review.refresh_summaries(self.store, self.cfg, "2026-07-20", "internal")
        self.assertEqual(self.store.thread(tid)["rolling_summary"], "요약본")  # 소급 요약됨
        self.assertEqual(self.store.get_state("last_summary"), "2026-07-20")   # 마커 전진

    def test_refresh_summaries_marker_not_advanced_on_failure(self):
        # 요약 호출 실패(AIError) 시 마커 미전진 → 다음 실행이 같은 창 재시도
        self.store.set_state("last_summary", "2026-07-17")
        self.store.ingest([
            MailRecord(message_id="<f@t>", subject="실패건", sender_name="kim",
                       sender_addr="kim@corp.example", to=[ME],
                       sent_on="2026-07-18T09:00:00", body_text="검토 부탁드립니다."),
        ])
        with mock.patch.object(review, "ai_run", side_effect=review.AIError("x")):
            with self.assertRaises(review.AIError):
                review.refresh_summaries(self.store, self.cfg, "2026-07-20", "internal")
        self.assertEqual(self.store.get_state("last_summary"), "2026-07-17")  # 그대로

    def test_refresh_summaries_isolated_failure_advances_marker(self):
        # 여러 스레드 중 하나만 실패(단발)해도 나머지 성공 → 마커 전진.
        # 실패 스레드는 건너뛰고(가드로 다음 활동 때 재요약) 2회차가 3일 소급 반복하지 않게 함.
        self.store.set_state("last_summary", "2026-07-17")
        self.store.ingest([
            MailRecord(message_id="<ok@t>", subject="성공건", sender_name="kim",
                       sender_addr="kim@corp.example", to=[ME],
                       sent_on="2026-07-19T09:00:00", body_text="검토 부탁드립니다."),
            MailRecord(message_id="<bad@t>", subject="실패건", sender_name="lee",
                       sender_addr="lee@corp.example", to=[ME],
                       sent_on="2026-07-18T09:00:00", body_text="확인 요망 드립니다."),
        ])

        def flaky(cmd, prompt, *a, **k):
            if "확인 요망" in prompt:      # 실패 스레드의 SUMMARY_UPDATE 만 실패
                raise review.AIError("boom")
            return "요약본"

        with mock.patch.object(review, "ai_run", side_effect=flaky):
            review.refresh_summaries(self.store, self.cfg, "2026-07-20", "internal")
        self.assertEqual(self.store.get_state("last_summary"), "2026-07-20")   # 전진
        self.assertEqual(
            self.store.thread(self._tid("성공건"))["rolling_summary"], "요약본")
        self.assertFalse(
            self.store.thread(self._tid("실패건"))["rolling_summary"])  # 미요약(건너뜀)

    def test_refresh_summaries_marker_pinned_when_all_fail(self):
        # 시도분 전부 실패(백엔드 다운) → 마커 미전진 → 다음 실행이 같은 창 재시도
        self.store.set_state("last_summary", "2026-07-17")
        self.store.ingest([
            MailRecord(message_id="<a@t>", subject="A건", sender_name="kim",
                       sender_addr="kim@corp.example", to=[ME],
                       sent_on="2026-07-18T09:00:00", body_text="검토 부탁드립니다."),
            MailRecord(message_id="<b@t>", subject="B건", sender_name="lee",
                       sender_addr="lee@corp.example", to=[ME],
                       sent_on="2026-07-19T09:00:00", body_text="확인 요망 드립니다."),
        ])
        with mock.patch.object(review, "ai_run", side_effect=review.AIError("down")):
            with self.assertRaises(review.AIError):
                review.refresh_summaries(self.store, self.cfg, "2026-07-20", "internal")
        self.assertEqual(self.store.get_state("last_summary"), "2026-07-17")  # 그대로


class TestViewModel(unittest.TestCase):
    """웹 뷰모델 순수 로직 (HTML 미생성 — 구 model.py 병합분)."""

    def test_parse_harvest_sections_and_fields(self):
        out = (
            "## 오늘 델타\n- ECN 승인 완료 #34\n- 납기 회신 대기 #45\n"
            "## 결정 후보\n"
            "- 결정: B안 채택 | 근거: 비용 절감 | 결정자: 김민수 | #34 | "
            "인용: \"B안으로 확정하겠습니다\"\n"
            "- 결정 형식 아님 (스레드 번호 없음)\n"
            "## 인물 신호\n- 김민수 | ECN 담당 이관 | #34 | 인용: \"제가 ECN을 이어받습니다\"\n"
            "## 프로젝트 신호\n- #45 | 승인 대기 → 승인 완료 | 인용: \"승인 완료되었습니다\"\n"
        )
        p = distill.parse_harvest(out)
        self.assertEqual(len(p["delta"]), 2)
        self.assertEqual(len(p["decisions"]), 1)
        d = p["decisions"][0]
        self.assertEqual((d["thread_id"], d["title"], d["decider"]),
                         (34, "B안 채택", "김민수"))
        self.assertEqual(d["quote"], "B안으로 확정하겠습니다")
        self.assertEqual(p["person"][0]["who"], "김민수")
        self.assertEqual(p["project"][0]["thread_id"], 45)
        self.assertEqual(p["project"][0]["signal"], "승인 대기 → 승인 완료")

    def test_parse_harvest_empty_sections(self):
        p = distill.parse_harvest("## 오늘 델타\n- 없음\n## 결정 후보\n- 없음\n")
        self.assertEqual(p, {"delta": [], "decisions": [], "person": [],
                             "project": []})

    def test_build_home_missed_matches_unanswered(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        cfg = Config(home=Path(tmp.name), my_addresses=[ME],
                     ignore_senders=["noreply"], internal_domains=["corp.example"])
        store.ingest([
            MailRecord(message_id="<a@t>", subject="검토 요청",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00", body_text="확인 부탁"),
            # 스팸(외부) → 미답변에서 제외되어야
            MailRecord(message_id="<b@t>", subject="특가",
                       sender_name="ad", sender_addr="promo@spam.example",
                       to=[ME], sent_on="2026-07-04T09:10:00", body_text="세일"),
        ])
        home = web.build_home(store, cfg, "2026-07-04", None)
        self.assertEqual(len(home["missed"]), 1)  # 스팸 제외
        self.assertFalse(home["has_review"])
        self.assertEqual(home["n_dec"], 0)         # 결정 렌즈 = 원장 직접 조회
        self.assertEqual(home["n_dec_pending"], 0)

    def test_format_detail(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        store.ingest([
            MailRecord(message_id="<x@t>", subject="일정 협의",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="회신 부탁드립니다. 내일까지 확정 필요."),
        ])
        tid = store.message("1")["thread_id"]
        cfg = Config(home=Path(tmp.name), my_addresses=[ME])
        d = web.format_detail(store, cfg, tid)
        self.assertEqual(d["title"], "일정 협의")
        self.assertEqual(len(d["timeline"]), 1)
        # 신호는 분석 줄이 아니라 칩(d["act"]) — 판정 근거만 분석 줄에 남는다
        self.assertEqual(d["act"].level, "required")
        self.assertTrue(d["act"].has_deadline)
        joined = "\n".join(d["analysis"])
        self.assertIn("판정:", joined)
        # 요약이 없으면 "[누적 요약]" 자체가 안 보임(빈 안내문 제거)
        self.assertNotIn("[누적 요약]", joined)

    def test_format_detail_summary_only_when_present(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        store.ingest([
            MailRecord(message_id="<s@t>", subject="요약건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="본문입니다."),
        ])
        tid = store.message("1")["thread_id"]
        cfg = Config(home=Path(tmp.name), my_addresses=[ME])
        # 요약 없을 때: 헤더 없음
        self.assertNotIn("[누적 요약]", "\n".join(web.format_detail(store, cfg, tid)["analysis"]))
        # 요약 있을 때: 헤더 + 내용 표시 (#21: "롤링" 아님)
        store.save_summary(tid, "핵심: 일정 확정 대기.", 1)
        joined = "\n".join(web.format_detail(store, cfg, tid)["analysis"])
        self.assertIn("[누적 요약]", joined)
        self.assertIn("핵심: 일정 확정 대기.", joined)
        self.assertNotIn("[롤링 요약]", joined)

    def test_format_detail_includes_html(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        store.ingest([
            MailRecord(message_id="<h@t>", subject="서식건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="굵게 확인", body_html="<p>굵게 <b>확인</b></p>"),
        ])
        tid = store.message("1")["thread_id"]
        cfg = Config(home=Path(tmp.name), my_addresses=[ME])
        d = web.format_detail(store, cfg, tid)
        self.assertIn("<b>확인</b>", d["timeline"][0]["html"])


class TestDecisionLedger(unittest.TestCase):
    """결정 원장 CRUD + 데일리 수확(distill.harvest) 적재·인용 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "t.sqlite", [ME])
        self.cfg = Config(home=self.home, my_addresses=[ME],
                          internal_domains=["corp.example"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}},
                          raw={"ai": {"summary_max_days": 3}})
        self.store.ingest([
            _rec("d1", "kim@corp.example", [ME], "ECN 결정",
                 "2026-07-20T09:00:00",
                 body="논의 끝에 B안으로 확정하겠습니다. 비용 절감이 근거입니다."),
        ])
        self.tid = self.store.message("1")["thread_id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_dedup(self):
        did = self.store.add_decision(self.tid, "2026-07-20", "B안 채택",
                                      decider="김민수")
        self.assertIsNotNone(did)
        # 같은 스레드 + 같은 제목(공백·대소문자 무시) → 중복 None
        self.assertIsNone(
            self.store.add_decision(self.tid, "2026-07-21", " b안  채택 "))
        # 반려하면(살아있는 결정 아님) 같은 제목 재적재 가능
        self.store.set_decision_status(did, "rejected")
        self.assertIsNotNone(
            self.store.add_decision(self.tid, "2026-07-21", "B안 채택"))

    def test_status_counts_and_search(self):
        a = self.store.add_decision(self.tid, "2026-07-20", "B안 채택",
                                    rationale="비용")
        b = self.store.add_decision(self.tid, "2026-07-20", "납기 연기")
        self.store.set_decision_status(a, "confirmed")
        c = self.store.decision_counts()
        self.assertEqual((c["confirmed"], c["candidate"]), (1, 1))
        rows = self.store.decisions(status="confirmed", q="비용")
        self.assertEqual([r["id"] for r in rows], [a])
        # 수정 후 확정
        self.store.set_decision_status(b, "confirmed",
                                       title="납기 1주 연기", rationale="자재")
        row = self.store.decision(b)
        self.assertEqual((row["title"], row["status"]),
                         ("납기 1주 연기", "confirmed"))

    def test_harvest_saves_validated_and_drops_fake_quote(self):
        raw = (
            "## 오늘 델타\n- ECN B안 확정 #%(t)d\n"
            "## 결정 후보\n"
            "- 결정: B안 채택 | 근거: 비용 절감 | 결정자: 김민수 | #%(t)d | "
            "인용: \"B안으로 확정하겠습니다\"\n"
            "- 결정: 가짜 결정 | 근거: x | 결정자: 이수 | #%(t)d | "
            "인용: \"원문에 존재하지 않는 문장입니다\"\n"
            "## 인물 신호\n"
            "- 김민수 | ECN 결정 주도 | #%(t)d | "
            "인용: \"논의 끝에 B안으로 확정하겠습니다\"\n"
            "## 프로젝트 신호\n- 없음\n"
        ) % {"t": self.tid}
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        with mock.patch.object(review, "ai_run", return_value=raw):
            res = distill.harvest(self.store, self.cfg, det, backend="internal")
        self.assertEqual(len(res["decisions"]), 1)       # 인용 검증 통과분만
        self.assertEqual(res["decisions"][0]["title"], "B안 채택")
        self.assertEqual(res["dropped"], 1)              # 가짜 인용은 폐기
        self.assertEqual(len(res["person"]), 1)
        cands = self.store.decisions(status="candidate")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["source"], "daily")
        self.assertEqual(cands[0]["quote"], "B안으로 확정하겠습니다")
        n_sig = self.store.db.execute(
            "SELECT COUNT(*) n FROM distill_signals WHERE kind='person'"
        ).fetchone()["n"]
        self.assertEqual(n_sig, 1)
        self.assertTrue((self.home / "logs" / "harvest.jsonl").exists())
        # 같은 날 재실행 — 워터마크 이후 새 메일 없음 → AI 콜 없이 None (중복 과금·적재 없음)
        with mock.patch.object(review, "ai_run", return_value=raw) as run2:
            res2 = distill.harvest(self.store, self.cfg, det, backend="internal")
        self.assertIsNone(res2)
        run2.assert_not_called()
        self.assertEqual(len(self.store.decisions(status="candidate")), 1)

    def test_harvest_flagged_thread_first_in_prompt(self):
        # 플래그 스레드가 더 오래된 활동이어도 프롬프트 블록 맨 앞에 온다
        self.store.ingest([
            _rec("fh1", "lee@corp.example", [ME], "플래그건",
                 "2026-07-20T08:00:00", body="중요 사안 초기 논의입니다."),
        ])
        ftid = self.store.db.execute(
            "SELECT thread_id t FROM messages WHERE subject='플래그건'"
        ).fetchone()["t"]
        self.store.set_flag(ftid, True)
        items, _ = distill._harvest_items(
            self.store, self.cfg, "2026-07-20", "2026-07-20", "")
        # ECN 결정(09:00, 더 최근) < 플래그건(08:00) 이지만 플래그가 먼저
        self.assertLess(items.index(f"[#{ftid}]"), items.index(f"[#{self.tid}]"))

    def test_harvest_window_covers_skipped_days(self):
        # 하루 이틀 건너뛰어도 창(최대 3일 소급)이 건너뛴 날의 결정까지 수확한다
        self.store.ingest([
            _rec("d0", "lee@corp.example", [ME], "납기 협의",
                 "2026-07-18T10:00:00",
                 body="협의 결과 납기를 7월 말로 연기 확정합니다."),
        ])
        tid2 = self.store.db.execute(
            "SELECT thread_id t FROM messages WHERE subject='납기 협의'"
        ).fetchone()["t"]
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        raw = ("## 오늘 델타\n- 납기 연기 확정 #%(t)d\n## 결정 후보\n"
               "- 결정: 납기 7월 말 연기 | 근거: 협의 | 결정자: 이 | #%(t)d | "
               "인용: \"납기를 7월 말로 연기 확정합니다\"\n"
               "## 인물 신호\n- 없음\n## 프로젝트 신호\n- 없음\n") % {"t": tid2}
        with mock.patch.object(review, "ai_run", return_value=raw) as run:
            res = distill.harvest(self.store, self.cfg, det, backend="internal")
        prompt = run.call_args[0][1]
        self.assertIn("2026-07-18 ~ 2026-07-20", prompt)   # 창 표기
        self.assertIn("납기를 7월 말로 연기", prompt)       # 건너뛴 날 원문 포함
        self.assertEqual(len(res["decisions"]), 1)          # 소급 결정 적재
        # 워터마크 = 프롬프트에 실은 가장 최신 메시지 타임스탬프
        self.assertEqual(self.store.get_state("last_harvest"),
                         "2026-07-20T09:00:00")

    def test_harvest_backfill_past_date_keeps_marker(self):
        # 마커보다 과거 날짜 백필: 그 날 하루만 보고 마커는 안 되감김
        self.store.set_state("last_harvest", "2026-07-21T08:00:00")
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        with mock.patch.object(review, "ai_run",
                               return_value="## 오늘 델타\n- 없음\n") as run:
            res = distill.harvest(self.store, self.cfg, det, backend="internal")
        self.assertIsNotNone(res)
        prompt = run.call_args[0][1]
        self.assertIn("B안으로 확정하겠습니다", prompt)     # 그 날(07-20) 메일 포함
        self.assertEqual(self.store.get_state("last_harvest"),
                         "2026-07-21T08:00:00")             # 마커 그대로

    def test_harvest_graceful_without_backend_or_material(self):
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        self.assertIsNone(
            distill.harvest(self.store, self.cfg, det, backend="ghost"))
        # 재료 없음(다른 날짜) → AI 호출 전에 None
        det2 = review.deterministic(self.store, self.cfg, "2026-07-25")
        self.assertIsNone(
            distill.harvest(self.store, self.cfg, det2, backend="internal"))

    def test_render_includes_harvest_sections(self):
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        det["harvest"] = {
            "delta": [f"ECN 확정 #{self.tid}"],
            "decisions": [{"thread_id": self.tid, "title": "B안 채택",
                           "decider": "김민수", "rationale": "비용"}],
            "person": [{"who": "김민수", "signal": "ECN 담당",
                        "thread_id": self.tid}],
            "project": [{"thread_id": self.tid, "signal": "대기 → 완료"}],
            "dropped": 0,
        }
        md = review.render(det)
        self.assertIn("## 오늘 델타", md)
        self.assertIn("## 장기기억 제안 (1건", md)
        self.assertIn(f"[#{self.tid}] B안 채택 (김민수) — 비용", md)
        self.assertIn("## 인물 신호", md)
        self.assertIn("## 프로젝트 신호", md)
        # 수확 없으면(비-AI 데일리) 섹션 자체가 없음
        det.pop("harvest")
        self.assertNotIn("오늘 델타", review.render(det))


class TestDecisionRegex(unittest.TestCase):
    def test_matches_requests(self):
        for s in ["재시험 여부 판단 부탁드립니다.", "설비 가부 회신 부탁드립니다.",
                  "인터페이스 검토 부탁드립니다.", "의견 주세요.", "승인 부탁드립니다."]:
            self.assertRegex(s, review._DECISION_RX, msg=s)

    def test_non_requests_not_matched(self):
        # 요청이 아닌 서술은 매칭되면 안 됨 (오탐 가드)
        for s in ["팀장님 승인 올리겠습니다.", "컨펌했습니다.", "검토 완료했습니다.",
                  "결재 상신 완료."]:
            self.assertIsNone(review._DECISION_RX.search(s), msg=s)


class TestDeadlineRegex(unittest.TestCase):
    def test_kkaji_needs_time_word(self):
        # "까지"는 날짜/시각/상대시점 선행 시에만 기한
        for s in ["내일까지 회신 주세요", "6/29까지 제출 바랍니다", "6.29까지",
                  "17:00까지 부탁드립니다", "이번 주 금요일까지 확정",
                  "6/29(월)까지 회신", "6월 29일까지", "오후 5시까지", "EOD까지"]:
            self.assertRegex(s, review.DEADLINE_RX, msg=s)

    def test_range_usage_not_deadline(self):
        # 범위·부사 용법의 "까지"는 기한이 아니다
        for s in ["현재까지 진행중입니다", "지금까지의 결과를 공유합니다",
                  "여기까지 확인했습니다", "그때까지의 이력입니다"]:
            self.assertIsNone(review.DEADLINE_RX.search(s), msg=s)

    def test_other_keywords_still_match(self):
        for s in ["제출 기한은 다음과 같습니다", "마감 임박", "ASAP 처리",
                  "차주 중 확정", "다음 주 초까지"]:
            self.assertRegex(s, review.DEADLINE_RX, msg=s)

    def test_request_proxies_removed_from_deadline(self):
        # '회신 부탁' 류는 요청 계층(STRONG_REQUEST_RX)의 일 — ⏰(기한)은 순수 기한만.
        # 구 혼합 설계에서는 모든 회신 부탁이 ⏰ 를 켜 ↩ 와 사실상 중복이었다.
        from mailkb.features import STRONG_REQUEST_RX
        for s in ["회신 부탁드립니다", "자료 주시면 감사하겠습니다"]:
            self.assertIsNone(review.DEADLINE_RX.search(s), msg=s)
            self.assertRegex(s, STRONG_REQUEST_RX, msg=s)

    def test_no_backtracking_on_long_line(self):
        # 수만 자 단일 줄(무매치)에서 폭발하지 않아야 한다 (#5)
        blob = "가나다라마바사 " * 8000  # ~64,000자, 개행 없음
        t0 = time.monotonic()
        self.assertIsNone(review.DEADLINE_RX.search(blob))
        self.assertIsNone(review._DECISION_RX.search(blob))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_line_at_extracts_matched_line(self):
        text = "첫 줄입니다\n중간: 내일까지 회신 부탁드립니다\n마지막 줄"
        m = review.DEADLINE_RX.search(text)
        self.assertEqual(review._line_at(text, m.start()),
                         "중간: 내일까지 회신 부탁드립니다")
        # 개행 없는 단일 줄
        one = "6/29까지 제출"
        m2 = review.DEADLINE_RX.search(one)
        self.assertEqual(review._line_at(one, m2.start()), one)


class TestWorkdays(unittest.TestCase):
    def test_weekend_skipped(self):
        self.assertEqual(review._workdays_since("2026-07-10", "2026-07-13"), 1)  # 금→월
        self.assertEqual(review._workdays_since("2026-07-13", "2026-07-15"), 2)  # 월→수
        self.assertEqual(review._workdays_since("2026-07-13", "2026-07-13"), 0)  # 같은 날
        self.assertEqual(review._workdays_since("2026-07-20", "2026-07-13"), 0)  # 미래

    def test_holiday_excluded(self):
        self.assertEqual(
            review._workdays_since("2026-07-13", "2026-07-15", holidays={"2026-07-14"}), 1)


class TestIntervention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME], ["김도현"])
        self.cfg = Config(
            home=Path(self.tmp.name), my_addresses=[ME], my_names=["김도현"],
            ignore_senders=["noreply"], internal_domains=["corp.example"],
            ai_default="internal", ai_backends={"internal": {"cmd": ["echo"]}},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _r(self, mid, sender, to, subject, when, body, cc=None, reply_to=""):
        return MailRecord(
            message_id=f"<{mid}@t>", subject=subject,
            sender_name=sender.split("@")[0], sender_addr=sender,
            to=to, cc=cc or [], sent_on=when, body_text=body,
            in_reply_to=f"<{reply_to}@t>" if reply_to else "",
            references=[f"<{reply_to}@t>"] if reply_to else [],
        )

    def _tid(self, subject):
        row = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject=? LIMIT 1", (subject,)
        ).fetchone()
        return row["thread_id"] if row else None

    def test_decide_excludes_broadcast_and_noise(self):
        self.store.ingest([
            self._r("dec", "kim@corp.example",
                    [ME, "jung@corp.example", "lee@corp.example"],
                    "결정건", "2026-07-19T09:00:00", "가부 회신 부탁드립니다."),
            self._r("bc", "bora@corp.example",
                    [ME] + [f"e{i}@corp.example" for i in range(60)],
                    "전사공지", "2026-07-19T09:10:00", "검토 부탁드립니다."),
            self._r("noi", "noreply@corp.example", [ME],
                    "자동알림", "2026-07-19T09:20:00", "승인 부탁드립니다."),
        ])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual([it["category"] for it in q], ["decide"])
        self.assertEqual(q[0]["thread_id"], self._tid("결정건"))

    def test_dedup_decision_over_respond(self):
        self.store.ingest([self._r(
            "q1", "lee@corp.example", [ME], "검토요청",
            "2026-07-19T09:00:00", "판단 부탁드립니다.")])
        tid = self._tid("검토요청")
        un = [{"thread_id": tid, "days_old": 1, "sender_addr": "lee@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["category"], "decide")  # 결정 키워드 → respond 아닌 decide

    def test_stalled_mine_workday_gate(self):
        self.store.ingest([
            self._r("s1", "oh@corp.example", [ME], "성적서 확인",
                    "2026-07-14T09:00:00", "확인 부탁"),
            self._r("s2", ME, ["oh@corp.example"], "RE: 성적서 확인",
                    "2026-07-15T09:00:00", "3번 항목 다시 검토 부탁드립니다.",
                    reply_to="s1"),
        ])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["category"], "stalled_mine")
        self.assertEqual(q[0]["days"], 3)  # 영업일 Wed→Mon

    def test_stalled_thread_cc_only(self):
        # CC 로 온 질문은 이제 '확인 후보'(MAYBE)가 흡수 — 멈춘 스레드로 중복
        # 노출하지 않는다 (2026-07-17 액션 판정기 통합).
        self.store.ingest([
            self._r("t1", "jung@corp.example", [ME, "kim@corp.example"],
                    "표준 논의", "2026-07-13T09:00:00", "방향 논의 필요"),
            self._r("t2", "kim@corp.example",
                    ["jung@corp.example", "yoon@corp.example"], "RE: 표준 논의",
                    "2026-07-14T09:00:00", "어떻게 진행할까요?",
                    cc=[ME], reply_to="t1"),
        ])
        q, cand = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=[],
            return_candidates=True)
        self.assertEqual(q, [])
        tid = self._tid("표준 논의")
        self.assertIn(tid, {c["thread_id"] for c in cand})

    def test_mentions_me_helper(self):
        self.assertTrue(review._mentions_me("김도현님 확인 부탁", ["김도현"]))
        self.assertFalse(review._mentions_me("다른 내용", ["김도현"]))
        self.assertFalse(review._mentions_me("아무개", ["x"]))  # 1자 후보 무시

    def test_respond_drops_group_fyi(self):
        # 수신 다수(>direct_to)·요청/이름/참여 없음 = 그룹 FYI → 개입 큐 제외
        self.store.ingest([self._r(
            "fyi", "bora@corp.example",
            [ME] + [f"g{i}@corp.example" for i in range(8)],
            "부서 공지", "2026-07-19T09:00:00", "지난주 자료 공유드립니다.")])
        tid = self._tid("부서 공지")
        un = [{"thread_id": tid, "days_old": 1, "sender_addr": "bora@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual(q, [])

    def test_respond_keeps_name_mention(self):
        # 대규모 그룹메일이라도 내 이름을 명시하면 유지 + personal
        self.store.ingest([self._r(
            "men", "bora@corp.example",
            [ME] + [f"g{i}@corp.example" for i in range(8)],
            "부서 공지2", "2026-07-19T09:00:00", "김도현님 이 건 확인 바랍니다.")])
        tid = self._tid("부서 공지2")
        un = [{"thread_id": tid, "days_old": 1, "sender_addr": "bora@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual([it["category"] for it in q], ["respond"])
        self.assertTrue(q[0]["personal"])

    def test_respond_keeps_my_participation_with_request(self):
        # 내 참여 스레드라도(정밀도 우선) 상대 회신에 '실제 요청'이 있어야 유지.
        self.store.ingest([
            self._r("p1", ME,
                    ["oh@corp.example"] + [f"g{i}@corp.example" for i in range(8)],
                    "협의건", "2026-07-18T09:00:00", "의견 정리해봤습니다."),
            self._r("p2", "oh@corp.example",
                    [ME] + [f"g{i}@corp.example" for i in range(8)],
                    "RE: 협의건", "2026-07-19T09:00:00",
                    "잘 봤습니다. 세부안 회신 부탁드립니다.", reply_to="p1"),
        ])
        tid = self._tid("협의건")
        un = [{"thread_id": tid, "days_old": 1, "sender_addr": "oh@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual([it["category"] for it in q], ["respond"])
        self.assertTrue(q[0]["personal"])

    def test_respond_drops_participation_closer(self):
        # 내 참여 스레드라도 상대 회신이 '종결 인사'(요청 없음)면 응답 불필요 → 제외
        self.store.ingest([
            self._r("c1", ME, ["oh@corp.example"],
                    "확인건", "2026-07-18T09:00:00", "의견 정리해봤습니다."),
            self._r("c2", "oh@corp.example", [ME],
                    "RE: 확인건", "2026-07-19T09:00:00",
                    "잘 봤습니다. 이상 없습니다.", reply_to="c1"),
        ])
        tid = self._tid("확인건")
        un = [{"thread_id": tid, "days_old": 1, "sender_addr": "oh@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual(q, [])

    def test_respond_personal_sorted_first(self):
        # 둘 다 응답 대기(요청 있음). 나 지목(personal)이 먼저 정렬.
        self.store.ingest([
            self._r("f", "bora@corp.example", [ME],
                    "직접요청", "2026-07-19T09:00:00", "회신 부탁드립니다."),
            self._r("m", "bora@corp.example", [ME],
                    "지목건", "2026-07-19T09:05:00", "김도현님 확인 바랍니다."),
        ])
        tf, tm = self._tid("직접요청"), self._tid("지목건")
        un = [{"thread_id": tf, "days_old": 1, "sender_addr": "bora@corp.example"},
              {"thread_id": tm, "days_old": 1, "sender_addr": "bora@corp.example"}]
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual([it["category"] for it in q], ["respond", "respond"])
        self.assertEqual(q[0]["thread_id"], tm)   # personal(나 지목) 먼저
        self.assertTrue(q[0]["personal"])
        self.assertFalse(q[1]["personal"])

    def _un1(self, subject, sender="kim@corp.example"):
        tid = self._tid(subject)
        return tid, [{"thread_id": tid, "days_old": 1, "sender_addr": sender}]

    def test_candidates_collected_for_borderline(self):
        # 요청 약한 경계 항목(종결/FYI)은 결정론에서 빠지되 candidates 로 남는다
        self.store.ingest([self._r("c", "kim@corp.example", [ME], "리뷰완료",
            "2026-07-19T09:00:00", "요청하신 리뷰 완료했습니다. 코멘트 확인 바랍니다.")])
        tid, un = self._un1("리뷰완료")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        self.assertEqual(queue, [])                              # 결정론 제외
        self.assertEqual([c["thread_id"] for c in cands], [tid])  # 후보로 보존

    def test_ai_classify_promotes_candidate(self):
        # AI(haiku)가 후보를 '필요'로 판정 → 큐 승격 (FN↓)
        self.store.ingest([self._r("c", "kim@corp.example", [ME], "리뷰완료",
            "2026-07-19T09:00:00", "요청하신 리뷰 완료했습니다. 코멘트 확인 바랍니다.")])
        tid, un = self._un1("리뷰완료")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 필요"):
            out = review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        self.assertEqual([it["thread_id"] for it in out], [tid])
        self.assertEqual(out[0]["category"], "respond")
        self.assertTrue(out[0]["ai_promoted"])

    def test_ai_classify_demotes_respond(self):
        # AI가 respond 항목을 '불필요'로 판정 → 제외 (FP↓)
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "요청건",
            "2026-07-19T09:00:00", "회신 부탁드립니다.")])
        tid, un = self._un1("요청건")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        self.assertEqual([it["category"] for it in queue], ["respond"])
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 불필요"):
            out = review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        self.assertEqual(out, [])

    def test_ai_classify_graceful_without_backend(self):
        # 분류 백엔드 미설정 → 결정론 큐 그대로 (AI 없어도 기본 동작)
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "요청건",
            "2026-07-19T09:00:00", "회신 부탁드립니다.")])
        _, un = self._un1("요청건")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        out = review.ai_classify_intervention(
            self.store, self.cfg, queue, cands, backend="nope")  # 없는 백엔드
        self.assertEqual(out, queue)

    def test_ai_classify_leaves_decide_untouched(self):
        # 고신뢰 카테고리(decide)는 분류 대상 아님 — AI 응답 무관하게 유지
        self.store.ingest([self._r("d", "kim@corp.example", [ME], "승인건",
            "2026-07-19T09:00:00", "가부 회신 부탁드립니다.")])
        tid, un = self._un1("승인건")
        queue = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=un)
        self.assertEqual([it["category"] for it in queue], ["decide"])
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 불필요") as run:
            out = review.ai_classify_intervention(
                self.store, self.cfg, queue, [], backend="internal")
        self.assertEqual([it["category"] for it in out], ["decide"])  # 그대로
        run.assert_not_called()   # 모호 항목 없음 → AI 호출조차 안 함

    def test_ai_classify_unclear_keeps_conservative(self):
        # '불명' 판정 → respond 유지, candidate 미승격 (억지 판정 대신 보수적 처리)
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "요청건",
            "2026-07-19T09:00:00", "회신 부탁드립니다.")])
        tid, un = self._un1("요청건")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 불명"):
            out = review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        self.assertEqual([it["thread_id"] for it in out], [tid])  # 유지됨
        self.assertNotIn("ai_kept", out[0])                       # 명시 필요 아님

    def test_ai_classify_widens_context(self):
        # 스니펫(120자)이 아니라 마지막 메일 본문 전체가 프롬프트에 들어간다
        tail = "끝문장확인용XYZ"
        body = "회신 부탁드립니다. " + ("가나다라마바사아자차 " * 40) + tail
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "긴본문",
            "2026-07-19T09:00:00", body)])
        tid, un = self._un1("긴본문")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 필요") as run:
            review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        prompt = run.call_args[0][1]
        self.assertIn("마지막 메일:", prompt)   # 블록 형식
        self.assertIn(tail, prompt)         # 200자 넘어간 꼬리까지 포함(스니펫 아님)

    def test_ai_classify_history_not_summary(self):
        # 판정 근거 = 직전 대화 원문 (롤링 요약은 프롬프트에 안 들어감)
        self.store.ingest([
            self._r("h1", ME, ["kim@corp.example"], "리뷰건",
                    "2026-07-18T09:00:00", "브랜치 리뷰 요청드립니다."),
            self._r("h2", "kim@corp.example", [ME], "리뷰건",
                    "2026-07-19T09:00:00", "리뷰 완료했습니다. 확인 바랍니다.",
                    reply_to="h1"),
        ])
        tid, un = self._un1("리뷰건")
        self.store.save_summary(tid, "요약왜곡문장ABC", 2)   # 요약은 미참조여야
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 필요") as run:
            review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        prompt = run.call_args[0][1]
        self.assertIn("직전 대화:", prompt)
        self.assertIn("브랜치 리뷰 요청드립니다", prompt)   # 내 이전 발신 원문
        self.assertIn("(07-18 나)", prompt)                # 방향·날짜 표기
        self.assertNotIn("요약왜곡문장ABC", prompt)         # 요약 미참조

    def test_ai_classify_writes_log(self):
        # 판정 결과가 <home>/logs/classify.jsonl 로 누적된다 (b: 추후 정확도 분석용)
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "요청건",
            "2026-07-19T09:00:00", "회신 부탁드립니다.")])
        tid, un = self._un1("요청건")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 불필요"):
            review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal",
                date_iso="2026-07-20")
        logf = Path(self.tmp.name) / "logs" / "classify.jsonl"
        self.assertTrue(logf.exists())
        self.assertTrue((logf.parent / "ANALYZE-classify.md").exists())
        rec = json.loads(logf.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["date"], "2026-07-20")
        item = next(i for i in rec["items"] if i["thread_id"] == tid)
        self.assertEqual((item["det"], item["verdict"], item["action"]),
                         ("respond", "불필요", "dropped"))

    def test_ai_classify_log_can_be_disabled(self):
        # ai.classify_log=false → 로그 미기록 (cfg.opt 로 config.py 수정 없이 제어)
        self.cfg.raw = {"ai": {"classify_log": False}}
        self.store.ingest([self._r("r", "kim@corp.example", [ME], "요청건",
            "2026-07-19T09:00:00", "회신 부탁드립니다.")])
        tid, un = self._un1("요청건")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        with mock.patch.object(review, "ai_run", return_value=f"#{tid}: 불필요"):
            review.ai_classify_intervention(
                self.store, self.cfg, queue, cands, backend="internal")
        self.assertFalse((Path(self.tmp.name) / "logs" / "classify.jsonl").exists())

    def test_ai_refine_persists_and_reloads(self):
        self.store.ingest([
            self._r("pa", "kim@corp.example", [ME], "A건", "2026-07-19T09:00:00", "본문A"),
            self._r("pb", "lee@corp.example", [ME], "B건", "2026-07-19T09:10:00", "본문B"),
        ])
        ta, tb = self._tid("A건"), self._tid("B건")
        queue = [
            {"category": "respond", "thread_id": ta, "subject": "A건",
             "who": "kim", "days": 1, "snippet": "", "tag": "", "personal": False},
            {"category": "respond", "thread_id": tb, "subject": "B건",
             "who": "lee", "days": 1, "snippet": "", "tag": "", "personal": False},
        ]
        out = (f"- [#{tb}] 긴급도:상 · 사유: 급함 · 제안: 즉시 회신\n"
               f"- [#{ta}] 긴급도:하 · 사유: 여유")
        with mock.patch.object(review, "ai_run", return_value=out):
            review.ai_refine_intervention(
                self.store, self.cfg, queue, persist_date="2026-07-20")
        ann = self.store.load_intervention_ai("2026-07-20")
        self.assertEqual(ann[tb]["ai_priority"], "상")
        self.assertEqual(ann[tb]["ai_action"], "즉시 회신")
        self.assertEqual(ann[ta]["ai_priority"], "하")
        self.assertEqual(self.store.load_intervention_ai("2026-07-21"), {})  # 다른 날짜엔 없음
        # 저장분을 새 결정론 큐에 병합·정렬(상 먼저)
        fresh = [
            {"category": "respond", "thread_id": ta, "subject": "A건",
             "who": "kim", "days": 1, "personal": False},
            {"category": "respond", "thread_id": tb, "subject": "B건",
             "who": "lee", "days": 1, "personal": False},
        ]
        merged = review.apply_saved_ai(fresh, ann)
        self.assertEqual(merged[0]["thread_id"], tb)
        self.assertEqual(merged[0]["ai_priority"], "상")

    # ---- #7 제목 기반 노이즈 2단계 필터

    def test_config_opt_generic_lookup(self):
        # 새 설정 키는 config.py 수정 없이 cfg.opt 로 읽는다 (단일 파일 업데이트 운용)
        cfg = Config(home=Path("."), raw={"review": {"knob": 7, "nested": {"x": 1}}})
        self.assertEqual(cfg.opt("review", "knob"), 7)                # 존재
        self.assertEqual(cfg.opt("review", "nested", "x"), 1)         # 중첩
        self.assertEqual(cfg.opt("review", "없는키", default=3), 3)   # 부재 → 기본값
        self.assertIsNone(cfg.opt("없는섹션", "k"))                    # raw 미제공 경로
        self.assertEqual(Config(home=Path(".")).opt("a", default="d"), "d")  # raw 자체 없음

    def test_config_defaults_apply_without_keys(self):
        # config.toml 에 키가 없어도(생성자 kwargs 생략) 기본값 적용
        cfg = Config(home=Path("."))
        self.assertTrue(cfg.is_noise_subject_strong("[nflow] 결재 알림"))
        self.assertTrue(cfg.is_noise_subject_strong("Meeting Invitation: 주간회의"))
        self.assertTrue(cfg.is_noise_subject_strong("[자동회신] 부재중입니다"))
        self.assertTrue(cfg.is_noise_subject_weak("2026-W28 주간보고"))
        self.assertTrue(cfg.is_noise_subject_weak("[회의록] 7/9 품질회의"))
        self.assertFalse(cfg.is_noise_subject_strong("설계 검토 요청"))
        self.assertFalse(cfg.is_noise_subject_weak("설계 검토 요청"))

    def test_queue_max_days_cap(self):
        # 21일(기본) 초과 방치 항목은 큐에서 내림 — 0 이면 상한 해제
        old = (date.today() - timedelta(days=30)).isoformat()
        self.store.ingest([self._r(
            "cap", "kim@corp.example", [ME], "오래된 승인 요청",
            f"{old}T09:00:00", "가부 회신 부탁드립니다.")])
        q = review.intervention_queue(self.store, self.cfg, date.today().isoformat(),
                                      unanswered=[])
        self.assertNotIn("오래된 승인 요청", [it["subject"] for it in q])
        self.cfg.raw = {"review": {"queue_max_days": 0}}
        q2 = review.intervention_queue(self.store, self.cfg, date.today().isoformat(),
                                       unanswered=[])
        self.assertIn("오래된 승인 요청", [it["subject"] for it in q2])
        self.cfg.raw = {}

    def test_queue_drops_strong_noise_even_with_decision(self):
        self.store.ingest([self._r(
            "kx", "kim@corp.example", [ME], "[nwork] 결재 요청",
            "2026-07-19T09:00:00", "승인 부탁드립니다.")])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual(q, [])

    def test_queue_drops_weak_noise_mass_unreplied(self):
        self.store.ingest([self._r(
            "wr", "kim@corp.example",
            [ME, "a@corp.example", "b@corp.example", "c@corp.example", "d@corp.example"],
            "주간보고 W28", "2026-07-19T09:00:00", "승인 부탁드립니다.")])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual(q, [])

    def test_queue_keeps_weak_noise_direct(self):
        # 수신 3인 미만이면 약한 노이즈라도 유지
        self.store.ingest([self._r(
            "wd", "kim@corp.example", [ME], "주간보고 관련 문의",
            "2026-07-19T09:00:00", "포함 여부 판단 부탁드립니다.")])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual([it["category"] for it in q], ["decide"])

    def test_queue_keeps_weak_noise_when_i_replied(self):
        # 내가 논의에 참여한 스레드는 약한 노이즈라도 유지 (stalled_mine 으로)
        self.store.ingest([
            self._r("w1", "kim@corp.example",
                    [ME, "a@corp.example", "b@corp.example", "c@corp.example"],
                    "주간보고 초안", "2026-07-14T09:00:00", "초안 공유드립니다."),
            self._r("w2", ME, ["kim@corp.example"], "RE: 주간보고 초안",
                    "2026-07-15T09:00:00", "3번 수치 검토 부탁드립니다.",
                    reply_to="w1"),
        ])
        q = review.intervention_queue(self.store, self.cfg, "2026-07-20", unanswered=[])
        self.assertEqual([it["category"] for it in q], ["stalled_mine"])

    def test_thread_kind_subject_noise(self):
        strong = [{"is_sent": 0, "sender_addr": "kim@corp.example",
                   "to_addrs": ME, "subject": "[nflow] 결재 알림"}]
        self.assertEqual(review.thread_kind(self.cfg, strong), "spam")
        weak = [{"is_sent": 0, "sender_addr": "kim@corp.example",
                 "to_addrs": f"{ME};a@corp.example;b@corp.example",
                 "subject": "주간보고 W28"}]
        self.assertEqual(review.thread_kind(self.cfg, weak), "notice")
        participated = weak + [{"is_sent": 1, "sender_addr": ME,
                                "to_addrs": "kim@corp.example",
                                "subject": "RE: 주간보고 W28"}]
        self.assertEqual(review.thread_kind(self.cfg, participated), "work")

    def test_today_digest_classifies_and_excludes(self):
        self.store.ingest([
            # 업무 (직접 수신)
            self._r("d1", "kim@corp.example", [ME], "발주 협의",
                    "2026-07-20T09:00:00", "납기 7/18 확정입니다. 수량 회신 부탁."),
            # 스팸/노이즈 (외부 도메인)
            self._r("d2", "promo@spam.example", [ME], "특가",
                    "2026-07-20T09:10:00", "세일 안내"),
            # 공지 (대량발송, 내 참여 없음)
            self._r("d3", "bora@corp.example",
                    [ME] + [f"e{i}@corp.example" for i in range(60)],
                    "전사 공지", "2026-07-20T09:20:00", "냉방 안내"),
        ])
        dg = review.today_digest(self.store, self.cfg, "2026-07-20")
        subjects = [w["subject"] for w in dg["work"]]
        self.assertEqual(subjects, ["발주 협의"])      # 업무만 목록에
        self.assertEqual(dg["n_spam"], 1)
        self.assertEqual(dg["n_notice"], 1)
        self.assertIn("납기 7/18", dg["work"][0]["lead"])  # 첫 의미 줄
        self.assertEqual(dg["work"][0]["who"], "kim")  # 발신인(sender_name)

    def test_digest_who_is_counterpart_when_i_replied(self):
        # 마지막이 내 답장(→)이면 발신인은 내가 아니라 상대방(직전 수신자)
        self.store.ingest([
            self._r("q1", "park@corp.example", [ME], "납기 문의",
                    "2026-07-20T09:00:00", "납기 언제인가요?"),
            self._r("q2", ME, ["park@corp.example"], "RE: 납기 문의",
                    "2026-07-20T10:00:00", "7/18 입니다.", reply_to="q1"),
        ])
        dg = review.today_digest(self.store, self.cfg, "2026-07-20")
        w = dg["work"][0]
        self.assertTrue(w["is_sent"])
        self.assertEqual(w["who"], "park")  # 내 이름 아님, 상대방

    def test_ai_digest_fills_core_and_graceful(self):
        self.store.ingest([self._r(
            "dg", "kim@corp.example", [ME], "설계 변경",
            "2026-07-20T09:00:00", "핀맵 변경 영향 검토 필요합니다.")])
        dg = review.today_digest(self.store, self.cfg, "2026-07-20")
        tid = dg["work"][0]["thread_id"]
        out = f"- #{tid}: 핀맵 변경 영향 검토 대기"
        with mock.patch.object(review, "ai_run", return_value=out):
            r = review.ai_digest(self.store, self.cfg, dg)
        self.assertEqual(r["work"][0]["ai_core"], "핀맵 변경 영향 검토 대기")
        # AIError 시 결정론 lead 유지
        dg2 = review.today_digest(self.store, self.cfg, "2026-07-20")
        with mock.patch.object(review, "ai_run", side_effect=review.AIError("x")):
            r2 = review.ai_digest(self.store, self.cfg, dg2)
        self.assertEqual(r2["work"][0]["ai_core"], "")

    def test_ai_refine_reorders_and_flags(self):
        self.store.ingest([
            self._r("a", "kim@corp.example", [ME], "A건", "2026-07-19T09:00:00", "본문A"),
            self._r("b", "lee@corp.example", [ME], "B건", "2026-07-19T09:10:00", "본문B"),
        ])
        ta, tb = self._tid("A건"), self._tid("B건")
        queue = [
            {"category": "respond", "thread_id": ta, "subject": "A건",
             "who": "kim", "days": 1, "snippet": "본문A", "tag": ""},
            {"category": "respond", "thread_id": tb, "subject": "B건",
             "who": "lee", "days": 1, "snippet": "본문B", "tag": ""},
        ]
        out = (f"- [#{tb}] 긴급도:상 · 사유: 급함 · 제안: 즉시 회신\n"
               f"- [#{ta}] 긴급도:하 · 사유: 여유 · 상태:처리됨\n"
               f"- [#999] 긴급도:중 · 사유: 없는 스레드")
        with mock.patch.object(review, "ai_run", return_value=out):
            r = review.ai_refine_intervention(self.store, self.cfg, queue)
        self.assertEqual(r[0]["thread_id"], tb)       # 긴급도 상 먼저
        self.assertEqual(r[0]["ai_priority"], "상")
        self.assertEqual(r[0]["ai_action"], "즉시 회신")
        self.assertEqual(r[-1]["thread_id"], ta)      # 처리됨은 맨 아래
        self.assertEqual(r[-1]["ai_flag"], "처리됨")
        self.assertEqual(len(r), 2)                   # 없는 #999 무시

    def test_ai_refine_graceful_on_error(self):
        queue = [{"category": "respond", "thread_id": 1, "subject": "X",
                  "who": "k", "days": 1, "snippet": "", "tag": ""}]
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AIError("boom")):
            r = review.ai_refine_intervention(self.store, self.cfg, queue)
        self.assertEqual(r, queue)  # 실패 → 결정론 큐 그대로


class TestAILayer(unittest.TestCase):
    """#10 graceful degradation / #11 ai-rules.md 주입 / #13 진행 표시."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "t.sqlite", [ME])
        self.cfg = Config(
            home=self.home, my_addresses=[ME],
            internal_domains=["corp.example"],
            ai_default="internal", ai_backends={"internal": {"cmd": ["echo"]}},
            raw={"ai": {"summary_min_msgs": 1, "summary_max_days": 3}},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_run_ai_layer_unresolved_backend_returns_note(self):
        # 내장·config 어디에도 없는 백엔드 → SystemExit graceful (결정론만)
        cfg = Config(home=self.home, my_addresses=[ME],
                     ai_summary_backend="ghost")
        det = review.deterministic(self.store, cfg, "2026-07-20")
        ai_text, note = review.run_ai_layer(self.store, cfg, det)
        self.assertIsNone(ai_text)
        self.assertIn("결정론 리뷰만", note)

    def test_ai_cmd_builtin_defaults_without_config(self):
        # config 에 [ai.backends.*] 가 없어도 sonnet/haiku/internal 은 해결됨
        cfg = Config(home=self.home, my_addresses=[ME])  # ai_backends 비어 있음
        self.assertEqual(cfg.ai_cmd("sonnet"), ["claude", "-p", "--model", "sonnet"])
        self.assertEqual(cfg.ai_cmd("haiku"), ["claude", "-p", "--model", "haiku"])
        self.assertEqual(cfg.ai_cmd("internal"), ["opencode", "run"])
        with self.assertRaises(SystemExit):
            cfg.ai_cmd("ghost")                       # 미지의 이름은 여전히 실패
        # config 값이 있으면 그게 내장보다 우선
        cfg2 = Config(home=self.home, my_addresses=[ME],
                      ai_backends={"sonnet": {"cmd": ["X"]}})
        self.assertEqual(cfg2.ai_cmd("sonnet"), ["X"])

    def test_run_ai_layer_aierror_note_and_stages(self):
        self.store.ingest([_rec("g1", "kim@corp.example", [ME], "건",
                                "2026-07-20T09:00:00")])
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        stages = []
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AIError("boom")):
            ai_text, note = review.run_ai_layer(
                self.store, self.cfg, det, progress=stages.append)
        self.assertIsNone(ai_text)
        self.assertIn("결정론 리뷰만", note)
        # 단계 순서 (#13)
        self.assertEqual(stages[0], "누적 요약 갱신 중…")
        self.assertEqual(stages[-1], "완료")
        self.assertIn("결정·신호 수확 중…", stages)         # 수확(Phase 1)
        self.assertIn("오늘 메일 핵심 요약 중…", stages)
        self.assertIn("개입 큐 AI 분류 중…", stages)       # 분류(haiku)
        self.assertIn("개입 큐 우선순위 정리 중…", stages)   # 정제(haiku)

    def test_run_ai_layer_routes_summary_and_classify_backends(self):
        # 요약/회고 → summary 백엔드(sonnet), 개입 분류/정제 → classify 백엔드(haiku)
        cfg = Config(
            home=self.home, my_addresses=[ME], internal_domains=["corp.example"],
            ai_default="internal", ai_summary_backend="sonnet",
            ai_classify_backend="haiku",
            ai_backends={"internal": {"cmd": ["I"]}, "sonnet": {"cmd": ["S"]},
                         "haiku": {"cmd": ["H"]}},
            raw={"ai": {"summary_min_msgs": 1}},
        )
        self.store.ingest([_rec("q1", "kim@corp.example", [ME], "요청건",
                                "2026-07-20T09:00:00", body="회신 부탁드립니다.")])
        det = review.deterministic(self.store, cfg, "2026-07-20")
        seen = []   # (cmd0, prompt)

        def fake_run(cmd, prompt, **kw):
            seen.append((cmd[0], prompt))
            return "(응답)"

        with mock.patch.object(review, "ai_run", side_effect=fake_run):
            review.run_ai_layer(self.store, cfg, det, persist_date="2026-07-20")
        classify_cmds = {c for c, p in seen if "지금 내 액션이 필요한지" in p}
        summary_cmds = {c for c, p in seen if "스레드의 요약을 관리" in p}
        self.assertEqual(classify_cmds, {"H"})   # 분류 = haiku
        self.assertEqual(summary_cmds, {"S"})     # 요약 = sonnet
        self.assertNotIn("I", {c for c, _ in seen})  # default(internal) 미사용

    def test_ai_rules_text_strips_comments(self):
        self.assertEqual(self.cfg.ai_rules_text(), "")  # 파일 없음 → 빈 문자열
        (self.home / "ai-rules.md").write_text(
            "<!-- 내부 주석 -->\n- ECN 은 지훈이 담당\n", encoding="utf-8")
        self.assertEqual(self.cfg.ai_rules_text(), "- ECN 은 지훈이 담당")

    def test_ai_rules_injected_into_analysis_prompt(self):
        (self.home / "ai-rules.md").write_text(
            "<!-- 주석 -->\n- ECN 은 지훈이 담당\n", encoding="utf-8")
        self.store.ingest([_rec("r1", "kim@corp.example", [ME], "규칙건",
                                "2026-07-20T09:00:00")])
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        prompts = []

        def fake_run(cmd, prompt, **kw):
            prompts.append(prompt)
            return "(응답)"

        with mock.patch.object(review, "ai_run", side_effect=fake_run):
            review.run_ai_layer(self.store, self.cfg, det)
        joined = "\n===\n".join(prompts)
        self.assertIn("[사용자 지침 — 우선 적용]", joined)
        self.assertIn("ECN 은 지훈이 담당", joined)
        self.assertNotIn("내부 주석", joined)

    def test_sync_progress_non_tty_periodic(self):
        import io

        from mailkb import cli

        class _S:  # SyncStats 흉내
            def __init__(self, f): self.fetched = f; self.inserted = 0; self.skipped = 0
        buf = io.StringIO()      # StringIO 는 isatty()=False → 비-TTY 경로
        with mock.patch("sys.stderr", buf):
            p = cli._SyncProgress()
            for f in range(1, 151):
                p.update(_S(f))
            p.done()
        self.assertFalse(p.tty)
        self.assertIn("50통", buf.getvalue())    # 50통마다 줄바꿈 출력
        self.assertIn("100통", buf.getvalue())

    def test_stage_progress_numbers_and_total(self):
        import io

        from mailkb import cli
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            sp = cli._StageProgress(4)
            for m in ["A 단계", "B 단계", "C 단계", "D 단계", "완료"]:
                sp(m)
        out = buf.getvalue()
        self.assertIn("[1/4] A 단계", out)
        self.assertIn("[4/4] D 단계", out)
        self.assertIn("AI 계층 완료", out)

    def test_stage_progress_spinner_thread_tty(self):
        import io
        import time as _t

        from mailkb import cli
        buf = io.StringIO()
        with mock.patch.object(cli, "_tty", return_value=True), \
                mock.patch("sys.stderr", buf):
            sp = cli._StageProgress(2)
            sp("첫 단계")
            _t.sleep(0.28)          # 백그라운드 스피너가 몇 번 돌도록
            sp("완료")
        out = buf.getvalue()
        self.assertIn("[1/2]", out)
        self.assertTrue(any(c in out for c in "|/-\\"))   # 스피너 프레임 렌더
        self.assertIsNone(sp._thr)                          # 스레드 정리(누수 없음)

    def test_web_review_job_graceful_without_backend(self):
        from mailkb import web
        cfg = Config(home=self.home, my_addresses=[ME],
                     ai_summary_backend="ghost")          # 미해결 백엔드
        cfg.db_path.touch()
        with web._review_lock:
            web._review_job.update(running=True, msg="")
        web._run_review_job(cfg, True, "2026-07-20")      # 동기 호출 (스레드 없이)
        with web._review_lock:
            self.assertFalse(web._review_job["running"])
            self.assertIn("결정론 리뷰만", web._review_job["msg"])
        # 결정론 데일리는 저장됨
        self.assertTrue((self.home / "vault" / "daily" / "2026-07-20.md").exists())


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "vault" / "notes").mkdir(parents=True)
        self.cfg = Config(home=self.home, my_addresses=[ME])
        self.store = Store(self.home / "t.sqlite", [ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_note_filename_unique_per_thread(self):
        # 5.2: 동일 제목의 서로 다른 두 스레드가 노트 파일명에서 충돌하지 않아야 함.
        # 30일 이상 간격을 둬 제목 폴백 병합을 피하고 별도 스레드로 만든다.
        self.store.ingest([
            _rec("n1", "kim@c", [ME], "업무 협의", "2026-05-01T09:00:00"),
            _rec("n2", "lee@c", [ME], "업무 협의", "2026-07-01T09:00:00"),
        ])
        self.assertEqual(self.store.stats()["threads"], 2)
        tid1 = self.store.message("1")["thread_id"]
        tid2 = self.store.message("2")["thread_id"]
        self.assertNotEqual(tid1, tid2)

        p1 = notes.create_thread_note(self.cfg, self.store, tid1)
        p2 = notes.create_thread_note(self.cfg, self.store, tid2)
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.exists())
        self.assertTrue(p2.exists())


class TestWeb(unittest.TestCase):
    """웹 렌더 함수 스모크 — 소켓 없이 HTML 문자열 생성만 검증."""

    def setUp(self):
        from mailkb import web
        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME], ["김도현"])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["김도현"], ignore_senders=["noreply"],
                          internal_domains=["corp.example"])
        self.store.ingest([
            MailRecord(message_id="<w1@t>", subject="검토 요청",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="판단 부탁드립니다.",
                       body_html="<p>판단 <b>부탁</b>드립니다.</p>"
                                 '<img src="http://track.x/p.gif">'),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_home_renders(self):
        out = self.web.render_home(self.store, self.cfg, "2026-07-04")
        self.assertIn("action='/review'", out)   # 리뷰 생성 버튼
        self.assertIn("action='/sync'", out)      # 동기화 버튼
        self.assertIn("오늘 메일 핵심", out)
        # 개입을 홈에 흡수(미니멀): '지금 할 일' 배너 + 큐 항목 노출
        self.assertIn("지금 할 일", out)
        self.assertIn("검토 요청", out)           # 개입 큐 항목이 홈에
        self.assertIn("/thread/", out)

    def test_home_has_refine_form(self):
        # 구 개입 페이지의 'AI 정리' 폼이 홈으로 이전됨
        out = self.web.render_home(self.store, self.cfg, "2026-07-04")
        self.assertIn("action='/refine'", out)
        self.assertIn("AI 정리", out)

    def test_win_size_arg_clamps_and_defaults(self):
        # 창 크기 인자 정규화 — 신뢰 못 할 값을 --window-size 에 그대로 안 넣음
        self.assertEqual(self.web._win_size_arg("1600,900"), "1600,900")
        self.assertEqual(self.web._win_size_arg("10,10"), "600,400")        # 하한
        self.assertEqual(self.web._win_size_arg("9999,9999"), "6000,4000")  # 상한
        self.assertEqual(self.web._win_size_arg("abc"), "2000,1200")         # 파싱 실패→기본
        self.assertEqual(self.web._win_size_arg("2000"), "2000,1200")        # 짝 안맞음→기본

    def test_thread_renders_html_and_blocks_remote_img(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("<b>부탁</b>", out)             # 서식 렌더
        self.assertIn("data-blocked-src", out)         # 원격 이미지 차단
        self.assertIn("일부 이미지를 표시할 수 없습니다", out)   # 안내 배너

    def test_search_focuses_matched_message_not_first(self):
        # 여러 메일 스레드에서 검색어가 '뒷' 메일에만 있으면, 결과 링크가 그 메일로
        # focus 돼야 한다(스레드 첫 메일이 아니라). ?focus={message_id} + #msg-{id} 앵커.
        self.store.ingest([
            MailRecord(message_id="<fa@t>", subject="분기 계획",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-06T09:00:00",
                       body_text="분기 계획 회의 일정 공유합니다."),
            MailRecord(message_id="<fb@t>", subject="RE: 분기 계획",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-06T15:00:00",
                       body_text="추가로 예산초과분 검토가 필요합니다.",
                       in_reply_to="<fa@t>", references=["<fa@t>"]),
        ])
        first = self.store.db.execute(
            "SELECT id, thread_id FROM messages WHERE message_id='<fa@t>'").fetchone()
        second = self.store.db.execute(
            "SELECT id, thread_id FROM messages WHERE message_id='<fb@t>'").fetchone()
        self.assertEqual(first["thread_id"], second["thread_id"])   # 같은 스레드
        # 스레드 상세: 두 메일 모두 앵커 id 를 가진다
        detail = self.web.render_thread(self.store, self.cfg, first["thread_id"])
        self.assertIn(f"id='msg-{first['id']}'", detail)
        self.assertIn(f"id='msg-{second['id']}'", detail)
        # '예산초과분' 은 둘째 메일에만 → 링크가 둘째 메일로 focus
        res = self.web.render_search(self.store, self.cfg, {"q": ["예산초과분"]}, "2026-07-13")
        self.assertIn(f"/thread/{second['thread_id']}?focus={second['id']}", res)
        self.assertNotIn(f"?focus={first['id']}", res)     # 첫 메일로 가지 않음
        # app.js: focusMsg 정의 + load/부트스트랩 배선
        js = self.web._APP_JS
        self.assertIn("function focusMsg", js)
        self.assertIn("if (focus) focusMsg(p, focus);", js)      # SPA 이동
        self.assertIn('focusMsg("right", new URLSearchParams', js)  # 전체 로드
        # markSelected 는 href 에 ?focus=… 가 붙어도 경로만 비교해야 목록 '선택'
        # 강조가 유지된다(정확 비교면 focus 링크가 안 맞아 강조가 사라짐).
        self.assertIn('.split("?")[0]', js)

    def test_thread_markdown_toggle_for_text_mail(self):
        # HTML 없는 메일이 마크다운으로 보이면 서식(md-rich) 기본 +
        # '텍스트 보기' 토글(md-raw — 저장 텍스트 검증용)
        self.store.ingest([
            MailRecord(message_id="<md@t>", subject="주간 보고",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-05T09:00:00",
                       body_text="# 요약\n- **완료**: 배포\n- 다음: 검토\n\n`build.sh` 실행"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='주간 보고'"
        ).fetchone()["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(">텍스트 보기</button>", out)      # 토글 버튼 (서식이 기본)
        self.assertIn("class='md-raw'", out)            # 저장 텍스트 보존(토글용)
        self.assertIn("md-rich", out)                   # 렌더 결과
        self.assertIn("<strong>완료</strong>", out)      # 굵게
        self.assertIn("<ul>", out)                      # 목록
        self.assertIn("<code>build.sh</code>", out)     # 인라인 코드

    def test_thread_no_md_toggle_for_html_mail(self):
        # HTML 메일(w1)은 이미 서식 → 마크다운 토글 없음
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertNotIn("md-toggle", out)

    def test_html_mail_wrapped_for_dark_flatten(self):
        # 메일 원본 HTML 은 .mailhtml 로 감싸 다크 평탄화 대상이 된다
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("<div class='mailhtml'>", out)
        # 다크 평탄화 규칙이 CSS 에 존재 (색·배경·링크)
        self.assertIn(":root[data-theme='dark'] .mailhtml", self.web._CSS)
        self.assertIn(".mailhtml a { color: var(--accent) !important", self.web._CSS)

    def test_thread_no_md_toggle_for_plain_text(self):
        # 마크다운 신호 없는 평문 → 토글·md-rich 없음(기존 <pre> 그대로)
        self.store.ingest([
            MailRecord(message_id="<pl@t>", subject="일반 문의",
                       sender_name="park", sender_addr="park@corp.example",
                       to=[ME], sent_on="2026-07-06T09:00:00",
                       body_text="안녕하세요. 오늘 회의 시간 확인 부탁드립니다. 감사합니다."),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='일반 문의'"
        ).fetchone()["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertNotIn("md-toggle", out)
        self.assertNotIn("md-rich", out)

    def test_mail_md_to_html_escapes_and_filters_scheme(self):
        # escape 우선(XSS 차단) + 미지원 스킴 링크는 앵커 미생성
        html = self.web._mail_md_to_html(
            "<script>bad</script>\n**굵게** 그리고 [x](javascript:alert)")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<strong>굵게</strong>", html)
        self.assertNotIn('href="javascript', html)      # 링크 미생성(텍스트만)

    def test_mail_md_link_label_with_brackets(self):
        # "[공지] 제목" 링크의 변환형 "[[공지] 제목](url)" — 라벨 속 한 겹
        # 대괄호를 렌더러가 받아준다. 링크 아닌 일반 대괄호는 오탐 없음.
        out = self.web._mail_md_to_html("[[공지] 제목](https://x.y) 본문")
        self.assertIn(">[공지] 제목</a>", out)
        out = self.web._mail_md_to_html("[메모] 참고 (자료) 그리고 [문서](https://x.y)")
        self.assertNotIn("메모] 참고</a>", out)          # 앞 대괄호는 링크 아님
        self.assertIn(">문서</a>", out)
        self.assertTrue(self.web._looks_like_markdown("[[a] b](https://x.y)"))

    def test_mail_md_strong_del_legacy_inner_space(self):
        # 구버전 변환 저장분 "**aaa **" — 공백을 태그 밖으로 빼고 살린다.
        # 평문 별표 수식("2 ** 3")은 오탐 없음.
        out = self.web._mail_md_to_html("**aaa ** 다음")
        self.assertIn("<strong>aaa</strong>", out)
        self.assertNotIn("<em>", out)                    # em 오작동 회귀 가드
        self.assertIn("<del>취소</del>",
                      self.web._mail_md_to_html("~~취소 ~~ 유지"))
        self.assertNotIn("<strong>",
                         self.web._mail_md_to_html("점수 계산은 2 ** 3 방식"))

    def test_mail_md_to_html_table(self):
        # GFM 표: 헤더/본문, 정렬 콜론, 셀 내 인라인, 이스케이프 파이프
        md = ("| 항목 | 담당 | 상태 |\n"
              "|:-----|:----:|-----:|\n"
              "| **배포** | 김대리 | 완료 |\n"
              "| a \\| b | 이과장 | 진행 |")
        html = self.web._mail_md_to_html(md)
        self.assertIn("<table class='md-table'>", html)
        self.assertIn("<th", html)
        self.assertIn("<td", html)
        self.assertIn("<strong>배포</strong>", html)     # 셀 내 인라인
        self.assertIn("text-align:center", html)         # 가운데
        self.assertIn("text-align:right", html)          # 오른쪽
        self.assertIn("a | b", html)                     # 이스케이프 파이프 → 한 셀
        self.assertTrue(self.web._looks_like_markdown(md))

    def test_mail_md_pipes_without_delimiter_not_table(self):
        # 구분행 없는 파이프 한 줄은 표가 아님(문단 텍스트로 유지)
        html = self.web._mail_md_to_html("메뉴: 국밥 | 김밥 | 라면")
        self.assertNotIn("<table", html)

    def test_thread_markdown_table_renders(self):
        # text 메일 안의 표가 토글 서식(md-rich)에서 <table> 로 렌더
        self.store.ingest([
            MailRecord(message_id="<tb@t>", subject="표 보고",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-07T09:00:00",
                       body_text="정리:\n\n| 항목 | 상태 |\n|------|------|\n"
                                 "| 배포 | 완료 |\n| QA | 진행 |"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='표 보고'"
        ).fetchone()["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("md-toggle", out)
        self.assertIn("md-table", out)
        self.assertIn("<th", out)

    def test_home_is_act_split_rule(self):
        # '지금 할 일' 전면 = 결정 + 나 지목(★) 응답. 나머지는 '그 외 개입'으로 접힘.
        self.assertTrue(self.web._is_act({"category": "decide"}))
        self.assertTrue(self.web._is_act({"category": "respond", "personal": True}))
        self.assertFalse(self.web._is_act({"category": "respond", "personal": False}))
        self.assertFalse(self.web._is_act({"category": "stalled_mine"}))
        self.assertFalse(self.web._is_act({"category": "stalled_thread"}))

    def test_escapes_user_content(self):
        # 제목에 태그가 들어와도 이스케이프되어야(자체 XSS 방지)
        self.store.ingest([
            MailRecord(message_id="<w2@t>", subject="<script>x</script>위험",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-04T10:00:00", body_text="본문"),
        ])
        out = self.web.render_threads(self.store, self.cfg)
        self.assertNotIn("<script>x</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_stats_page_uses_shared_nav_shell(self):
        # 통계도 다른 메뉴와 동일한 상단 셸(Minerva·홈·개입…), 본문만 통계
        page = self.web.render_stats_page(self.store, self.cfg, 4)
        self.assertIn("<header class='top'>", page)
        self.assertIn("<span class='brand'>Minerva</span>", page)
        for menu in ("홈", "메일함", "스레드", "검색", "기록", "통계"):
            self.assertIn(menu, page, msg=menu)
        self.assertIn("통계 분석", page)          # 본문 제목
        self.assertIn("검토 기간", page)          # 기간 선택 바
        self.assertIn("/report.js", page)         # 통계 JS 로드
        self.assertIn("--brand:", page)           # report.CSS 주입됨
        self.assertEqual(page.lower().count("<!doctype"), 1)   # 단일 문서
        # app.js 없는 전폭 페이지라 서버가 직접 통계 메뉴에 밑줄 표시
        self.assertIn('<a href="/stats" class="active">통계</a>', page)
        self.assertEqual(page.count('class="active"'), 1)      # 통계만 활성
        self.assertNotIn("← Minerva 홈", page)    # 옛 backlink 제거

    def test_same_origin_matrix(self):
        so = self.web.same_origin
        host = "localhost:8765"
        self.assertTrue(so(None, host))                              # 헤더 없음
        self.assertTrue(so("null", host))                            # no-referrer/앱모드
        self.assertTrue(so("http://localhost:8765", host))           # 정확 일치
        self.assertTrue(so("http://127.0.0.1:8765", host))           # 로컬 동등 (#17)
        self.assertTrue(so("http://localhost:8765", "127.0.0.1:8765"))
        self.assertTrue(so("http://[::1]:8765", host))               # IPv6 루프백
        self.assertFalse(so("http://localhost:9999", host))          # 포트 불일치
        self.assertFalse(so("http://evil.example", host))            # 외부
        self.assertFalse(so("http://evil.example:8765", host))

    def test_blocked_html_is_explanatory(self):
        out = self.web._blocked_html("localhost:8765")
        self.assertNotIn("교차 출처", out)          # 기술 용어 금지 (#18)
        self.assertIn("http://localhost:8765/", out)  # 무엇을 하면 되는지
        self.assertIn("직접 열어", out)

    def test_shell_has_split_layout(self):
        # #14: 상단 메뉴 + 좌/우 분할 + 스플리터 + 앱 JS
        out = self.web._shell("t", "LEFT", "RIGHT")
        for marker in ("id='left'", "id='splitter'", "id='right'",
                       "src='/app.js'", "<nav>"):
            self.assertIn(marker, out, msg=marker)
        self.assertIn("LEFT", out)
        self.assertIn("RIGHT", out)
        # 웹 서비스 표시명은 Minerva (코드/명령명은 mailkb 유지)
        self.assertIn(">Minerva</span>", out)
        self.assertIn("· Minerva</title>", out)

    def test_route_pane_assignment(self):
        today = "2026-07-04"
        cases = [("/", "left"), ("/lens/intervene", "left"),
                 ("/threads", "left"), ("/search", "left"),
                 ("/records", "left"), ("/daily", "left"),
                 ("/review/status", "right")]
        for path, want in cases:
            title, inner, code, pane = self.web.route(
                self.store, self.cfg, path, {}, today)
            self.assertEqual(pane, want, msg=path)
            self.assertEqual(code, 200, msg=path)
            self.assertNotIn("<html", inner, msg=path)   # fragment 는 문서 아님
        tid = self.store.message("1")["thread_id"]
        _, _, code, pane = self.web.route(
            self.store, self.cfg, f"/thread/{tid}", {}, today)
        self.assertEqual((code, pane), (200, "right"))

    def test_with_frag(self):
        self.assertEqual(self.web._with_frag("/thread/3"), "/thread/3?frag=1")
        self.assertEqual(self.web._with_frag("/?msg=x"), "/?msg=x&frag=1")

    def test_app_js_markers(self):
        # #15/#16 핵심 동작이 JS 에 존재하는지 (localStorage 폭 저장·fetch·pushState)
        js = self.web._APP_JS
        for marker in ("localStorage", "mailkb.leftw", "pushState",
                       "popstate",           # 뒤로가기 — pushState 의 짝
                       "X-Requested-With", "pointerdown", "form.submit()",
                       "textContent",
                       '"/stats"',           # 통계는 가로채지 않음 (전폭 페이지)
                       '"/mail"',            # 메일함 = 좌측 패널
                       "IntersectionObserver", "data-more",  # 목록 추가 로딩 (#5)
                       '.add("read")',      # 열람 시 목록 볼드 낙관적 해제 (실시간)
                       "md-toggle", "md-on",  # 마크다운 서식 토글 (#21)
                       "/winsize", "outerWidth", "resizeTo"):  # 창 크기 기억·복원
            self.assertIn(marker, js, msg=marker)
        self.assertNotIn("innerHTML = msg", js)   # 토스트는 textContent 만

    def test_find_msedge_fallback_order(self):
        # #19: PATH 우선 → 환경변수 경로 → 없으면 None
        with mock.patch("shutil.which", return_value=r"C:\path\msedge.exe"):
            self.assertEqual(self.web._find_msedge(), r"C:\path\msedge.exe")
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict("os.environ", {"ProgramFiles(x86)": self.tmp.name,
                                            "ProgramFiles": "", "LOCALAPPDATA": ""},
                             clear=False):
            edge = Path(self.tmp.name) / "Microsoft" / "Edge" / "Application"
            edge.mkdir(parents=True)
            (edge / "msedge.exe").write_bytes(b"")
            self.assertEqual(self.web._find_msedge(), str(edge / "msedge.exe"))
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict("os.environ",
                             {"ProgramFiles(x86)": "", "ProgramFiles": "",
                              "LOCALAPPDATA": ""}, clear=False):
            self.assertIsNone(self.web._find_msedge())

    def test_open_ui_non_windows_falls_back(self):
        # 비-Windows 에서 app_mode 여도 webbrowser 폴백 (#19)
        with mock.patch.object(self.web.webbrowser, "open") as wb:
            self.web._open_ui("http://127.0.0.1:1/", app_mode=True)
        if sys.platform != "win32":
            wb.assert_called_once_with("http://127.0.0.1:1/")

    def test_serve_source_is_single_thread(self):
        # #20 회귀 가드: ThreadingHTTPServer 로 바뀌면 Outlook COM 이 깨진다
        import inspect
        src = inspect.getsource(self.web.serve)
        self.assertIn("HTTPServer((host, port)", src)          # 단일 스레드 생성
        self.assertNotIn("ThreadingHTTPServer((", src)          # 호출로는 사용 금지
        self.assertIn("CoInitialize", src)

    def test_serve_binds_loopback_only(self):
        # 보안 #1: serve 는 루프백에만 바인딩 — 바인딩 주소를 바꿀 입력이 없어야 한다
        import inspect
        sig = inspect.signature(self.web.serve)
        self.assertNotIn("host", sig.parameters)               # 바인딩 주소 인자 없음
        self.assertIn('host = "127.0.0.1"', inspect.getsource(self.web.serve))

    def test_serve_writes_pidfile(self):
        # 런처가 재시작 때 옛 서버를 찾도록 serve 가 minerva.pid 기록/정리
        import inspect
        src = inspect.getsource(self.web.serve)
        self.assertIn("minerva.pid", src)
        self.assertIn("getpid", src)

    def test_favicon_svg(self):
        # 앱 창·탭·PWA 아이콘용 SVG 파비콘 + head link
        import inspect
        self.assertIn("<svg", self.web._FAVICON_SVG)
        self.assertIn("/favicon.svg", inspect.getsource(self.web._Handler.do_GET))
        self.assertIn("/favicon.svg", self.web._head("t"))

    def test_launcher_present(self):
        # 아이콘 실행기 — 존재 + 핵심 단계 포함
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "launch_minerva.pyw"
        self.assertTrue(p.exists())
        src = p.read_text(encoding="utf-8")
        for marker in ("minerva.pid", "--app=", "--user-data-dir",
                       ".wait()", "terminate"):
            self.assertIn(marker, src)

    def test_settings_update_button(self):
        # 설정의 '최신으로 업데이트' — git pull 버튼/핸들러/디스패치
        import inspect
        self.assertIn("/settings/update", inspect.getsource(self.web.render_settings))
        self.assertIn("/settings/update", inspect.getsource(self.web._Handler.do_POST))
        self.assertIn("pull", inspect.getsource(self.web._git_update))

    def test_latest_freshness(self):
        # DB 변경(새 메일)을 토큰으로 감지해 열린 목록/홈을 자동 최신화(수집 주기와 분리)
        import inspect
        self.assertIn("/latest", inspect.getsource(self.web._Handler.do_GET))
        self.assertIn("/latest", self.web._APP_JS)
        self.assertIn("refreshDisplay", self.web._APP_JS)

    def test_refresh_display_uses_left_panel_state(self):
        # 메일함에서 스레드를 열면 location=/thread/N 이지만 실제 왼쪽 패널은 /mail 이다.
        # 최신화 대상은 주소창이 아니라 leftCur 로 판정하고, 깊은 스크롤에서 미룬
        # 변경은 상단 복귀 때 다시 반영해야 한다.
        js = self.web._APP_JS
        block = js[js.index("function refreshDisplay"):
                   js.index("function checkFresh")]
        self.assertIn('new URL(leftCur || "/", location.origin)', block)
        self.assertNotIn("location.pathname", block)
        self.assertIn("listDirty = true", block)
        self.assertIn('left.addEventListener("scroll"', js)
        self.assertIn("if (listDirty && left.scrollTop < 150) refreshDisplay()", js)
        self.assertIn("if (listDirty && left && left.scrollTop < 150) refreshDisplay()", js)
        self.assertIn('? "/threads" : "/"', js)  # 직접 thread 로드의 왼쪽은 스레드 목록

    def test_timeline_newest_first(self):
        # 스레드 상세는 최신 메일이 먼저 (메일 클라이언트 관례)
        self.store.ingest([
            MailRecord(message_id="<o1@t>", subject="순서건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-01T09:00:00", body_text="첫 메일"),
            MailRecord(message_id="<o2@t>", subject="RE: 순서건",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-03T09:00:00", body_text="나중 메일",
                       in_reply_to="<o1@t>", references=["<o1@t>"]),
        ])
        tid = [r["thread_id"] for r in self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='순서건'")][0]
        d = self.web.format_detail(self.store, self.cfg, tid)
        self.assertEqual(d["timeline"][0]["sent_on"][:10], "2026-07-03")
        self.assertEqual(d["timeline"][-1]["sent_on"][:10], "2026-07-01")

    def test_nav_order_with_mail_menu(self):
        nav = self.web._NAV
        # 검색은 링크가 아니라 헤더 검색창으로 승격 — 링크 순서는 통계까지
        order = ["홈", "메일함", "스레드", "기록", "통계"]
        pos = [nav.index(f">{t}</a>") for t in order]
        self.assertEqual(pos, sorted(pos))   # 명시된 순서 그대로
        self.assertIn('href="/mail"', nav)
        # 검색창은 통계 뒤, 설정(gear) 앞
        self.assertLess(nav.index(">통계</a>"), nav.index("navsearch"))
        self.assertLess(nav.index("navsearch"), nav.index("gear"))

    def test_render_mail_list_and_noise_filter(self):
        self.store.ingest(
            [_rec(f"m{i}", "kim@corp.example", [ME], f"메일 {i}",
                  f"2026-07-{(i % 8) + 1:02d}T09:{i % 60:02d}:00") for i in range(5)]
            + [_rec("nz", "noreply@corp.example", [ME], "자동 알림",
                    "2026-07-08T10:00:00"),
               _rec("nfl", "kim@corp.example", [ME], "[nflow] 결재 알림",
                    "2026-07-08T11:00:00")])
        out = self.web.render_mail(self.store, self.cfg)
        self.assertIn("<h1>메일함</h1>", out)
        self.assertIn("전체 6", out)       # 필터 바 전체 수 = setUp 1 + 신규 5 (노이즈 2 제외)
        self.assertIn("class='mrow'", out)
        # 배치: 제목이 윗줄(mfrom 슬롯), 발신인이 아랫줄(msubj 슬롯)
        self.assertIn("<span class='mfrom'>메일 4</span>", out)
        self.assertIn("<span class='msubj'>kim</span>", out)
        self.assertNotIn("자동 알림", out)            # 발신 노이즈 제외
        self.assertNotIn("[nflow]", out)                # 제목 강한 노이즈 제외
        self.assertNotIn("data-more", out)            # 소량 → 센티널 없음

    def test_mail_read_state_bold(self):
        # 미읽음=class='mrow'(볼드), 열람하면 read 클래스 → 볼드 해제
        tid = self.store.message("1")["thread_id"]
        self.assertIn("class='mrow'", self.web.render_mail(self.store, self.cfg))
        self.assertTrue(self.store.mark_thread_read(tid))
        out = self.web.render_mail(self.store, self.cfg)
        self.assertIn("class='mrow read'", out)
        self.assertNotIn("class='mrow'>", out)         # 남은 미읽음 행 없음
        self.assertFalse(self.store.mark_thread_read(tid))  # 재열람은 no-op

    def test_route_thread_marks_read(self):
        # GET /thread/{id} 라우트가 열람=읽음 처리
        tid = self.store.message("1")["thread_id"]
        self.web.route(self.store, self.cfg, f"/thread/{tid}", {}, "2026-07-04")
        self.assertIn("class='mrow read'", self.web.render_mail(self.store, self.cfg))

    def test_thread_header_sender_first(self):
        # 본문 헤더: 발신인(mh-who)이 날짜(mh-when)보다 먼저
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("mh-who", out)
        self.assertLess(out.index("mh-who"), out.index("mh-when"))

    def test_dismiss_removed_and_signal_wording(self):
        # 추적제외 폐지(2026-07-12): 버튼 없음 + 신호 문구는 ↩/⏰ 새 표현
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertNotIn("추적 제외", out)
        self.assertNotIn("/dismiss'", out)
        self.assertIn("↩ 회신 필요", out)             # 신호 칩 (구 '⚑ 미답변')
        self.assertNotIn("⚑ 미답변", out)
        self.assertNotIn("신호 포함", out)

    def test_nav_has_settings_gear(self):
        self.assertIn("/settings", self.web._NAV)
        self.assertIn("class=\"gear\"", self.web._NAV)

    def test_settings_page_blocked_and_thresholds(self):
        self.cfg.blocked_senders = ["spam@vendor.example"]
        out = self.web.render_settings(self.store, self.cfg)
        self.assertIn("<h1>설정</h1>", out)
        self.assertIn("spam@vendor.example", out)          # 차단 목록
        self.assertIn("/settings/unblock", out)            # 해제 폼
        self.assertIn("broadcast_to", out)                 # 현재 기준

    def test_settings_image_retain_knob(self):
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn("이미지 보존(일)", page)
        self.assertIn("name='image_retain_days'", page)
        self.assertIn("value='60'", page)              # 기본값
        # 저장 경로: _SETTINGS_INTS 에 등재 → overrides.json 영구
        loc = self.web._save_settings(self.cfg.home,
                                      {"image_retain_days": ["30"]})
        self.assertIn("/settings", loc)
        import mailkb.config as cfgmod2
        self.assertEqual(
            cfgmod2.read_overrides(self.cfg.home)["web"]["image_retain_days"], 30)

    def test_settings_about_section(self):
        # 설정 하단 정보(About): 버전·GitHub 링크·저작권
        from mailkb import __version__
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn(f"v{__version__}", page)
        self.assertIn("https://github.com/dongjinpark-maker/mailkb", page)
        self.assertIn("MIT © 2026", page)
        self.assertIn("rel='noopener noreferrer'", page)   # 외부 링크 안전 속성

    def test_settings_page_no_blocked(self):
        self.cfg.blocked_senders = []
        self.assertIn("차단된 발신인 없음",
                      self.web.render_settings(self.store, self.cfg))

    def test_settings_unblock_action(self):
        from mailkb import config as cfgmod
        cfgmod.add_blocked(self.cfg, "spam@vendor.example")
        self.assertIn("spam@vendor.example", self.cfg.blocked_senders)
        loc = self.web.perform_action(
            self.store, self.cfg, "/settings/unblock",
            {"addr": ["spam@vendor.example"]})
        self.assertIn("/settings", loc)
        self.assertNotIn("spam@vendor.example", self.cfg.blocked_senders)

    def test_settings_override_persist_and_reload(self):
        from mailkb import config as cfgmod
        home = Path(self.tmp.name)
        (home / "config.toml").write_text(
            'my_addresses=["me@corp.example"]\n[review]\nbroadcast_to=50\n',
            encoding="utf-8")
        self.web._save_settings(home, {"broadcast_to": ["80"],
                                       "summary_max_days": ["5"]})
        cfg = cfgmod.load(home)
        self.assertEqual(cfg.broadcast_to, 80)                  # 오버라이드 반영
        self.assertEqual(cfg.opt("ai", "summary_max_days", default=3), 5)
        self.assertIn("broadcast_to=50",                        # 원본 무손상
                      (home / "config.toml").read_text(encoding="utf-8"))

    def test_settings_override_invalid_int_skipped(self):
        from mailkb import config as cfgmod
        home = Path(self.tmp.name)
        (home / "config.toml").write_text(
            'my_addresses=["me@corp.example"]\n[review]\ndirect_to=4\n',
            encoding="utf-8")
        self.web._save_settings(home, {"direct_to": ["abc"]})   # 파싱 실패 → 스킵
        self.assertEqual(cfgmod.load(home).direct_to, 4)

    def test_reading_width_injected_and_configurable(self):
        from mailkb import config as cfgmod
        # read_w 지정 시 CSS 변수 주입, 미지정 시 미주입(CSS 기본 1200 사용)
        self.assertIn(":root{--read-w:1500px}", self.web._shell("t", "L", "R", read_w=1500))
        self.assertNotIn(":root{--read-w", self.web._shell("t", "L", "R"))
        # 설정으로 저장 → 오버라이드
        home = Path(self.tmp.name)
        (home / "config.toml").write_text(
            'my_addresses=["me@corp.example"]\n', encoding="utf-8")
        self.web._save_settings(home, {"reading_width": ["1600"]})
        self.assertEqual(cfgmod.load(home).opt("web", "reading_width", default=1200), 1600)

    def test_settings_noise_add_remove(self):
        from mailkb import config as cfgmod
        home = Path(self.tmp.name)
        (home / "config.toml").write_text(
            'my_addresses=["me@corp.example"]\n[filters]\n'
            'ignore_senders=["noreply"]\n', encoding="utf-8")
        cfg = cfgmod.load(home)
        self.web._save_noise(cfg, {"op": ["add"], "list": ["ignore_senders"],
                                   "pattern": ["SPAM"]})          # 소문자로 저장
        cfg = cfgmod.load(home)
        self.assertIn("spam", cfg.ignore_senders)
        self.web._save_noise(cfg, {"op": ["remove"], "list": ["ignore_senders"],
                                   "pattern": ["noreply"]})
        self.assertNotIn("noreply", cfgmod.load(home).ignore_senders)

    def test_render_mail_pagination(self):
        self.store.ingest([
            _rec(f"p{i}", "kim@corp.example", [ME], f"대량 {i:03d}",
                 f"2026-06-{(i % 28) + 1:02d}T{i % 24:02d}:00:00")
            for i in range(40)])
        first = self.web.render_mail(self.store, self.cfg)
        self.assertEqual(first.count("class='mrow'"), 30)   # _PAGE 만 초기 렌더
        self.assertIn("data-more='/mail?offset=", first)
        # 다음 배치 조각: 행 + (마지막이면) 센티널 없음, 전체 문서 아님
        frag = self.web.render_mail(self.store, self.cfg, offset=30)
        self.assertNotIn("<h1>", frag)
        self.assertGreater(frag.count("class='mrow'"), 0)

    def test_render_threads_list_ui(self):
        self.store.ingest([
            _rec("t1", "kim@corp.example", [ME], "구매 협의",
                 "2026-07-01T09:00:00"),
            _rec("t2", ME, ["kim@corp.example"], "RE: 구매 협의",
                 "2026-07-02T09:00:00", reply_to="t1"),
        ])
        out = self.web.render_threads(self.store, self.cfg)
        self.assertIn("구매 협의", out)
        self.assertIn("[2통]", out)                 # 누적 메일 개수
        self.assertIn("마지막:", out)               # 마지막 발신인 행
        self.assertIn("class='mrow'", out)
        self.assertNotIn("mcnt hot", out)           # 2통·1일 — 강조 없음

    def test_thread_count_emphasis(self):
        # 3통+ 또는 논의 기간 5일+ 는 [N통] 강조색 (#3)
        self.store.ingest([
            _rec("h1", "kim@corp.example", [ME], "긴 논의",
                 "2026-07-01T09:00:00"),
            _rec("h2", ME, ["kim@corp.example"], "RE: 긴 논의",
                 "2026-07-02T09:00:00", reply_to="h1"),
            _rec("h3", "kim@corp.example", [ME], "RE: 긴 논의",
                 "2026-07-03T09:00:00", reply_to="h2"),   # 3통 → hot
            _rec("s1", "lee@corp.example", [ME], "늘어진 건",
                 "2026-07-01T10:00:00"),
            _rec("s2", ME, ["lee@corp.example"], "RE: 늘어진 건",
                 "2026-07-08T10:00:00", reply_to="s1"),   # 2통이지만 7일 → hot
        ])
        out = self.web.render_threads(self.store, self.cfg)
        hot_rows = [seg for seg in out.split("<a class='mrow'") if "mcnt hot" in seg]
        self.assertEqual(len(hot_rows), 2)
        self.assertTrue(any("긴 논의" in s for s in hot_rows))
        self.assertTrue(any("늘어진 건" in s for s in hot_rows))

    def test_route_mail_is_left_pane(self):
        _, inner, code, pane = self.web.route(
            self.store, self.cfg, "/mail", {}, "2026-07-04")
        self.assertEqual((code, pane), (200, "left"))
        self.assertNotIn("<html", inner)

    def test_inline_image_attach_names_hidden(self):
        # "제목 없는 첨부 파일 NNN.png"(붙여넣기 이미지 자동 이름)는 표시에서 제외
        self.store.ingest([
            MailRecord(message_id="<a1@t>", subject="첨부건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-05T09:00:00", body_text="본문",
                       attachments=["제목 없는 첨부 파일 00001.png",
                                    "보고서.xlsx"]),
            MailRecord(message_id="<a2@t>", subject="이미지만",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-05T10:00:00", body_text="본문",
                       attachments=["제목 없는 첨부 파일 00002.png"]),
        ])
        t1 = self.store.message("2")["thread_id"]   # 첨부건 (w1 다음)
        d1 = self.web.format_detail(self.store, self.cfg, t1)
        self.assertEqual(d1["timeline"][0]["attach"], "보고서.xlsx")
        out1 = self.web.render_thread(self.store, self.cfg, t1)
        self.assertIn("📎보고서.xlsx", out1)
        self.assertNotIn("제목 없는 첨부 파일", out1)
        t2 = self.store.message("3")["thread_id"]   # 이미지만 → 📎 자체가 없음
        out2 = self.web.render_thread(self.store, self.cfg, t2)
        self.assertNotIn("📎", out2)
        self.assertNotIn("첨부 추출", out2)   # 의미 있는 첨부 없음 → 버튼도 숨김

    def test_thread_page_has_action_forms(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        for a in ("hide", "note", "open"):
            self.assertIn(f"action='/thread/{tid}/{a}'", out)
        # 발신자 차단은 주소별 보기 페이지로 이동 → 스레드엔 없음
        self.assertNotIn(f"action='/thread/{tid}/block'", out)

    def test_perform_action_dismiss_gone(self):
        # 폐지된 동작은 '알 수 없는 동작'으로 — 상태 불변
        tid = self.store.message("1")["thread_id"]
        loc = self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/dismiss", {})
        self.assertTrue(loc.startswith("/?msg="))       # 알 수 없는 동작 → 홈
        self.assertEqual(self.store.thread(tid)["status"], "open")

    def test_perform_action_block_by_addr(self):
        # 발신자 차단은 주소별 보기 페이지에서 (주소 기반)
        loc = self.web.perform_action(self.store, self.cfg, "/block",
                                      {"addr": ["kim@corp.example"]})
        self.assertIn("kim@corp.example", self.cfg.blocked_senders)
        self.assertTrue(self.cfg.is_noise("kim@corp.example"))
        self.assertIn("/person", loc)                 # 그 주소 페이지로 복귀
        self.assertIn("Outlook", urllib_unquote(loc))

    def test_perform_action_note_creates_file(self):
        tid = self.store.message("1")["thread_id"]
        loc = self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/note", {})
        self.assertIn("노트 생성", urllib_unquote(loc))

    def test_do_sync_fake(self):
        # 동기화 실동작(수집+프룬) — 완료 msg·신규 통수 반환
        msg, n = self.web._do_sync(self.store, self.cfg)
        self.assertIn("동기화", msg)
        self.assertIsInstance(n, int)

    def test_sync_job_and_status(self):
        # 백그라운드 잡: _run_sync_job 완료 → render_sync_status 가 결과·토스트 마커 노출
        self.web._sync_job.update(running=False, msg="", n=0)
        self.web._run_sync_job(self.cfg)               # 스레드 함수 직접 호출(인라인)
        self.assertFalse(self.web._sync_job["running"])
        self.assertIn("동기화", self.web._sync_job["msg"])
        inner, running = self.web.render_sync_status()
        self.assertFalse(running)
        self.assertIn("data-sync-msg", inner)          # 폴링 토스트용 마커
        self.assertNotIn("data-sync-running", inner)

    def test_sync_status_running_marker(self):
        self.web._sync_job.update(running=True, msg="", n=0)
        inner, running = self.web.render_sync_status()
        self.web._sync_job.update(running=False, msg="", n=0)   # 정리
        self.assertTrue(running)
        self.assertIn("data-sync-running", inner)      # 폴링 훅 마커
        self.assertIn("가져오는 중", inner)

    def test_autosync_toast_only_when_new_mail(self):
        # 회귀 가드: 자동 주기 동기화는 '신규>0' 일 때만 토스트(구 동작). render 가
        # 통수(data-sync-n)를 실어 watchSyncToast 가 0 이면 조용하도록.
        self.web._sync_job.update(running=False, msg="동기화(fake): 신규 0 · 중복 5", n=0)
        inner, _ = self.web.render_sync_status()
        self.assertIn("data-sync-n='0'", inner)        # 신규 0 → 통수 0 노출(토스트 안 함)
        self.web._sync_job.update(running=False, msg="동기화(fake): 신규 3 · 중복 5", n=3)
        inner, _ = self.web.render_sync_status()
        self.assertIn("data-sync-n='3'", inner)        # 신규 3 → 통수 3 → '새 메일 3통'
        self.web._sync_job.update(running=False, msg="", n=0)   # 정리
        # app.js: watchSyncToast 가 통수 게이트 + 구 문구('새 메일 N통')를 갖는지
        js = self.web._APP_JS
        self.assertIn("data-sync-n", js)
        self.assertIn('"새 메일 "', js)

    def test_attach_button_only_with_attachment(self):
        self.store.ingest([MailRecord(
            message_id="<at@t>", subject="첨부건", sender_name="kim",
            sender_addr="kim@corp.example", to=[ME],
            sent_on="2026-07-04T11:00:00", body_text="첨부", attachments=["a.xlsx"])])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='첨부건'").fetchone()["thread_id"]
        self.assertIn(f"action='/thread/{tid}/attach'", self.web.render_thread(self.store, self.cfg, tid))
        # 첨부 없는 스레드엔 버튼 없음
        tid0 = self.store.message("1")["thread_id"]
        self.assertNotIn("/attach'", self.web.render_thread(self.store, self.cfg, tid0))

    def test_saved_ai_shown_on_home_get(self):
        # B1: 저장된 AI 정리가 홈 새로고침(GET)에도 반영 (개입 흡수)
        tid = self.store.message("1")["thread_id"]
        self.store.save_intervention_ai("2026-07-04", tid, "상", "급함", "즉시", "")
        out = self.web.render_home(self.store, self.cfg, "2026-07-04")
        self.assertIn("급함", out)

    # ─────────────────── 미개봉 필터·개수 (기능 1)
    def test_mail_unread_count_and_toggle(self):
        # setUp 메일(w1)은 미개봉 1건 → 필터 바에 '미개봉 1' 탭
        out = self.web.render_mail(self.store, self.cfg)
        self.assertIn("/mail?unread=1", out)         # 미개봉 탭 링크
        self.assertIn("미개봉 1", out)
        # 읽으면 '미개봉 0'
        self.store.mark_thread_read(self.store.message("1")["thread_id"])
        out2 = self.web.render_mail(self.store, self.cfg)
        self.assertIn("미개봉 0", out2)

    def test_mail_unread_filter_only_unread(self):
        # 읽은 메일 1 + 미개봉 메일 1 → flt='unread' 목록엔 미개봉만
        self.store.ingest([_rec("u2", "lee@corp.example", [ME], "새 문의",
                                "2026-07-06T09:00:00")])
        self.store.mark_thread_read(self.store.message("1")["thread_id"])  # w1 읽음
        out = self.web.render_mail(self.store, self.cfg, flt="unread")
        self.assertIn("새 문의", out)          # 미개봉
        self.assertNotIn("검토 요청", out)      # 읽음 → 제외
        self.assertIn("미개봉 1", out)         # 필터 바 개수

    def test_mail_unread_more_link_keeps_filter(self):
        # 무한스크롤 센티널이 unread 필터를 유지 (offset 조각)
        from mailkb import web
        self.assertIn("data-more='/mail?unread=1&offset=",
                      web._more_html("/mail?unread=1", 30))

    # ─────────────────── 플래그 (기능 2) — 아이콘 유/무
    def test_flag_toggle_action_and_badge(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(f"action='/thread/{tid}/flag'", out)       # 플래그 버튼
        self.assertIn("⚐", out)                                  # 색 없는 flag(미표시)
        self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/flag", {})
        self.assertEqual(self.store.thread(tid)["flagged"], 1)
        out2 = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(f"action='/thread/{tid}/unflag'", out2)     # 이제 해제 버튼
        self.assertIn("flag on", out2)                            # 색 있는 flag(표시)
        self.assertIn("⚑", out2)
        self.assertIn("🚩", self.web.render_threads(self.store, self.cfg, flt="flagged"))
        # 해제
        self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/unflag", {})
        self.assertEqual(self.store.thread(tid)["flagged"], 0)

    def test_threads_bold_reflects_read_state(self):
        # 스레드 목록 볼드 = 실제 미개봉 (메일함과 동일 규칙)
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_threads(self.store, self.cfg)
        self.assertIn("class='mrow'", out)             # 안 읽음 수신 있음 → 볼드
        self.store.mark_thread_read(tid)
        out2 = self.web.render_threads(self.store, self.cfg)
        self.assertIn("class='mrow read'", out2)       # 다 읽음 → 볼드 해제
        self.assertNotIn("class='mrow'>", out2)

    def test_threads_flag_filter_only_flagged(self):
        self.store.ingest([_rec("f2", "lee@corp.example", [ME], "다른 건",
                                "2026-07-06T09:00:00")])
        tid = self.store.message("1")["thread_id"]
        self.store.set_flag(tid, True)
        out = self.web.render_threads(self.store, self.cfg, flt="flagged")
        self.assertIn("검토 요청", out)        # 플래그된 것
        self.assertNotIn("다른 건", out)        # 미플래그 제외
        self.assertIn("🚩 플래그 1", out)       # 필터 바 개수

    def test_list_filter_bar_unified_both_pages(self):
        # 메일함·스레드가 같은 필터 바(전체·미개봉·응답대기·기한·플래그·숨김)
        for out in (self.web.render_mail(self.store, self.cfg),
                    self.web.render_threads(self.store, self.cfg)):
            self.assertIn("class='listtabs'", out)
            for lbl in ("전체", "미개봉", "↩ 회신 필요", "⏰ 기한",
                        "🚩 플래그", "🙈 숨김"):
                self.assertIn(lbl, out)
            self.assertNotIn("추적제외", out)
            self.assertNotIn("j/k 이동", out)   # 동작은 유지, 안내 문구는 숨김
        # 메일함 설명문 삭제 · 스레드 미답변 제거
        self.assertNotIn("노이즈 제외 수신 메일", self.web.render_mail(self.store, self.cfg))
        self.assertNotIn("미답변", self.web.render_threads(self.store, self.cfg))

    def test_awaiting_and_deadline_filters_both_pages(self):
        # 픽스처: 검토 요청(kim→me, "판단 부탁드립니다") = 응답 대기 O / 기한 X
        # 추가: 기한 메일(lee→me "내일까지 회신"), 내가 답한 스레드(응답 대기 X)
        self.store.ingest([
            MailRecord(message_id="<dl@t>", subject="기한 있는 요청",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-04T10:00:00",
                       body_text="내일까지 회신 부탁드립니다."),
            MailRecord(message_id="<my@t>", subject="검토 요청",
                       sender_name="나", sender_addr=ME,
                       to=["kim@corp.example"], sent_on="2026-07-04T11:00:00",
                       body_text="답변드립니다.", in_reply_to="<w1@t>",
                       references=["<w1@t>"]),
        ])
        dl_tid = self.store.message("2")["thread_id"]
        w_tid = self.store.message("1")["thread_id"]
        # 응답 대기: 기한 메일 스레드만 (검토 요청은 내가 마지막으로 답함)
        out = self.web.render_threads(self.store, self.cfg, flt="awaiting")
        self.assertIn("기한 있는 요청", out)
        self.assertNotIn(f"/thread/{w_tid}'", out)
        # 기한: DEADLINE_RX 매칭 스레드만
        out2 = self.web.render_threads(self.store, self.cfg, flt="deadline")
        self.assertIn("기한 있는 요청", out2)
        self.assertNotIn(f"/thread/{w_tid}'", out2)
        # 메일함도 동일 판정(스레드 소속 메일 표시) + 카운트 탭
        m = self.web.render_mail(self.store, self.cfg, flt="awaiting")
        self.assertIn("기한 있는 요청", m)
        full = self.web.render_mail(self.store, self.cfg)
        self.assertIn("↩ 회신 필요 1", full)
        self.assertIn("⏰ 기한 1", full)
        thr = self.web.render_threads(self.store, self.cfg)   # 스레드 뱃지도 1/1
        self.assertIn("↩ 회신 필요 1", thr)
        self.assertIn("⏰ 기한 1", thr)
        # 숨기면 신호 필터에서도 빠짐 — 리스트뿐 아니라 뱃지 카운트에서도(회귀 가드).
        # 신호 캐시는 hidden 을 제외하지 않으므로 카운트 지점의 라이브 제외가 필요하다.
        self.store.hide_thread(dl_tid, True)
        self.assertNotIn("기한 있는 요청",
                         self.web.render_threads(self.store, self.cfg, flt="awaiting"))
        thr2 = self.web.render_threads(self.store, self.cfg)
        self.assertIn("↩ 회신 필요 0", thr2)       # 숨김 스레드가 뱃지를 부풀리지 않음
        self.assertIn("⏰ 기한 0", thr2)
        full2 = self.web.render_mail(self.store, self.cfg)
        self.assertIn("↩ 회신 필요 0", full2)       # 메일함 뱃지도 0(원래 정상, 확인)
        self.assertIn("⏰ 기한 0", full2)

    # ─────────────────── 숨기기 (기능 2) — 추적·메일함·기본목록에서 제외
    def test_hide_excludes_from_queue_mail_and_threads(self):
        tid = self.store.message("1")["thread_id"]
        q0 = review.intervention_queue(self.store, self.cfg, "2026-07-04")
        self.assertIn(tid, [it["thread_id"] for it in q0])       # 원래 개입 큐에 있음
        loc = self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/hide", {})
        self.assertEqual(self.store.thread(tid)["hidden"], 1)
        self.assertIn("thread/%d" % tid, loc)
        # 개입 큐·메일함·스레드 기본목록에서 사라짐
        q1 = review.intervention_queue(self.store, self.cfg, "2026-07-04")
        self.assertNotIn(tid, [it["thread_id"] for it in q1])
        self.assertNotIn("검토 요청", self.web.render_mail(self.store, self.cfg))
        self.assertNotIn("검토 요청", self.web.render_threads(self.store, self.cfg))
        # 미답변 추적에서도 제외
        self.assertNotIn(tid, [r["thread_id"] for r in
                               self.store.unanswered(days=3650)])
        # 숨김 탭에서만 보임(복구용)
        self.assertIn("검토 요청",
                      self.web.render_threads(self.store, self.cfg, flt="hidden"))

    def test_unhide_restores(self):
        tid = self.store.message("1")["thread_id"]
        self.store.hide_thread(tid, True)
        self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/unhide", {})
        self.assertEqual(self.store.thread(tid)["hidden"], 0)
        self.assertIn("검토 요청", self.web.render_mail(self.store, self.cfg))

    def test_hide_button_and_unhide_button(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(f"action='/thread/{tid}/hide'", out)
        self.assertIn("숨기기", out)
        self.assertNotIn("/unread'", out)                        # 안읽음 버튼 삭제됨
        self.store.hide_thread(tid, True)
        out2 = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(f"action='/thread/{tid}/unhide'", out2)    # 숨김 중 → 해제
        self.assertIn("숨김 해제", out2)

    def test_noise_thread_excluded_but_recoverable_when_hidden(self):
        # 외부/노이즈 수신 메일: 일반 탭엔 안 뜨지만 숨기면 숨김 탭에서 복구·카운트
        self.store.ingest([MailRecord(
            message_id="<promo@t>", subject="반값 특가",
            sender_name="샵딜", sender_addr="promo@shopdeals.example",
            to=[ME], sent_on="2026-07-06T09:00:00", body_text="세일")])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='반값 특가'").fetchone()["thread_id"]
        # 노이즈 → 메일함·스레드 일반 탭에 없음
        self.assertNotIn("반값 특가", self.web.render_mail(self.store, self.cfg))
        self.assertNotIn("반값 특가", self.web.render_threads(self.store, self.cfg))
        # 숨기기 전: 메일함·스레드 숨김 카운트 0
        self.assertIn("🙈 숨김 0", self.web.render_mail(self.store, self.cfg))
        self.assertIn("🙈 숨김 0", self.web.render_threads(self.store, self.cfg))
        self.store.hide_thread(tid, True)
        # 숨긴 뒤: 양쪽 숨김 탭에서 보이고(복구), 양쪽 숨김 카운트가 노이즈 포함해 증가
        self.assertIn("반값 특가",
                      self.web.render_mail(self.store, self.cfg, flt="hidden"))
        self.assertIn("반값 특가",
                      self.web.render_threads(self.store, self.cfg, flt="hidden"))
        self.assertIn("🙈 숨김 1", self.web.render_mail(self.store, self.cfg))
        self.assertIn("🙈 숨김 1", self.web.render_threads(self.store, self.cfg))
        # 일반 탭엔 여전히 없음
        self.assertNotIn("반값 특가", self.web.render_threads(self.store, self.cfg))

    # ─────────────────── 관련 메일(양방향) (기능 3)
    def test_correspondence_both_directions(self):
        # 내가 kim 에게 보낸 것(is_sent) + kim 이 나에게 보낸 것(setUp) 모두 포함
        self.store.ingest([MailRecord(
            message_id="<s1@t>", subject="답장", sender_name="me",
            sender_addr=ME, to=["kim@corp.example"],
            sent_on="2026-07-05T09:00:00", body_text="네 확인했습니다")])
        rows = self.store.correspondence("kim@corp.example")
        subs = {r["subject"] for r in rows}
        self.assertIn("검토 요청", subs)        # 받은 것
        self.assertIn("답장", subs)             # 보낸 것
        self.assertEqual(len(rows), 2)

    def test_thread_sender_name_links_to_person(self):
        # 참여자(수신 발신자) 이름 클릭 → 주소별 메일
        out = self.web.render_thread(self.store, self.cfg, self.store.message("1")["thread_id"])
        self.assertIn("<a href='/person?addr=kim%40corp.example'", out)

    def test_render_person_page_mailbox_style(self):
        page = self.web.render_person(self.store, self.cfg, "kim@corp.example")
        self.assertIn("검토 요청", page)
        self.assertIn("(양방향)", page)
        self.assertIn("전체 ", page)                    # 건수 = "전체 x (양방향)"
        self.assertNotIn("↔", page)                    # 이름 앞 ↔ 제거
        self.assertNotIn("주고받은 메일", page)         # 옛 문구 제거
        self.assertIn("backlink", page)                # ← 뒤로
        self.assertIn("class='mrow", page)             # 메일함 스타일 행
        self.assertIn("action='/block'", page)         # 발신자 차단 버튼(여기로 이동)

    def test_person_sent_mail_distinct_background(self):
        # 내가 그에게 보낸 메일 → 배경 구별 클래스
        self.store.ingest([MailRecord(
            message_id="<s2@t>", subject="답장함", sender_name="me", sender_addr=ME,
            to=["kim@corp.example"], sent_on="2026-07-05T09:00:00", body_text="확인")])
        page = self.web.render_person(self.store, self.cfg, "kim@corp.example")
        self.assertIn("class='mrow sent'", page)
        self.assertIn(".mrow.sent", self.web._CSS)     # CSS 규칙 존재

    def test_person_page_shows_blocked_state(self):
        from mailkb import config as cfgmod
        cfgmod.add_blocked(self.cfg, "kim@corp.example")
        page = self.web.render_person(self.store, self.cfg, "kim@corp.example")
        self.assertIn("차단됨", page)
        self.assertNotIn("action='/block'", page)      # 차단되면 버튼 숨김

    def test_route_person_is_left_pane(self):
        # 주소별 메일은 왼쪽(목록) 프레임
        title, inner, code, pane = self.web.route(
            self.store, self.cfg, "/person", {"addr": ["kim@corp.example"]}, "2026-07-04")
        self.assertEqual((code, pane), (200, "left"))
        self.assertIn("검토 요청", inner)

    def test_person_header_three_columns(self):
        # (← 뒤로 · 이름 · 발신자 차단) 한 줄, 좌/가운데/우 정렬
        page = self.web.render_person(self.store, self.cfg, "kim@corp.example")
        self.assertIn("class='personhead'", page)
        self.assertIn("class='ptitle'", page)          # 이름 = 가운데
        self.assertIn("backlink", page)                # 뒤로 = 왼쪽
        self.assertIn("class='pright'", page)          # 차단 = 오른쪽
        self.assertIn(".personhead", self.web._CSS)    # 정렬 CSS 존재

    def test_appjs_left_history_and_kbd_sync(self):
        js = self.web._APP_JS
        self.assertIn("leftBack", js)                  # ← 뒤로 = 왼쪽의 이전 항목
        self.assertIn("noteLeft", js)
        self.assertIn("isTrusted", js)                 # 마우스 클릭 → 키보드 커서 동기화
        self.assertIn('classList.contains("selected")', js)  # curIdx 선택 항목 폴백
        # 스레드 상태 변경 시 왼쪽 목록 갱신
        self.assertIn("flag|unflag|hide|unhide", js)

    def test_nav_active_underline(self):
        # 현재 위치한 최상위 메뉴에 밑줄(active) 표시
        js = self.web._APP_JS
        self.assertIn("markNav", js)                    # nav 활성화 갱신 함수
        self.assertIn("navTarget", js)
        self.assertIn("header.top nav", js)             # 셸 헤더의 nav 대상
        self.assertIn('classList.add("active")', js)
        # 이동(inject) + 초기 로드에서 갱신 — 뒤로가기는 load→inject로 커버
        self.assertEqual(js.count("markNav();"), 2)     # 호출부 2곳
        # CSS 밑줄 규칙 존재
        self.assertIn("header.top nav a.active", self.web._CSS)
        self.assertIn("text-decoration: underline", self.web._CSS)

    # ─────────────────── 자동 동기화 (기능 4)
    def test_sync_interval_default_and_clamp(self):
        self.assertEqual(self.web._sync_interval_min(self.cfg), 30)   # 기본 30
        self.cfg.raw = {"web": {"sync_interval_min": 0}}
        self.assertEqual(self.web._sync_interval_min(self.cfg), 0)    # 0=끔
        self.cfg.raw = {"web": {"sync_interval_min": 5000}}
        self.assertEqual(self.web._sync_interval_min(self.cfg), 1440)  # 상한
        self.cfg.raw = {"web": {"sync_interval_min": "bad"}}
        self.assertEqual(self.web._sync_interval_min(self.cfg), 30)   # 파싱 실패→기본

    def test_settings_has_sync_interval(self):
        out = self.web.render_settings(self.store, self.cfg)
        self.assertIn("sync_interval_min", out)
        self.assertIn("자동 동기화", out)

    def test_autosync_markers_in_appjs(self):
        js = self.web._APP_JS
        self.assertIn("/syncmin", js)
        self.assertIn("/autosync", js)
        # 백그라운드 동기화: 완료 감시 토스트 + 수동 대기화면 폴링
        self.assertIn("watchSyncToast", js)
        self.assertIn("hookSyncPolling", js)
        self.assertIn("/sync/status", js)
        self.assertIn("data-sync-running", js)

    # ─────────────────── 라이트/다크 테마
    def test_theme_html_attr_and_tokens(self):
        w = self.web
        self.assertIn("data-theme='light'", w._head("t"))          # 기본 라이트
        self.assertIn("data-theme='dark'", w._head("t", theme="dark"))
        # CSS 토큰화(통일) + 다크 오버라이드 블록
        self.assertIn(":root[data-theme='dark']", w._CSS)
        for tok in ("--surface:", "--ink:", "--border:", "--accent:"):
            self.assertIn(tok, w._CSS, msg=tok)
        # 셸이 cfg 테마를 <html> 에 반영
        self.assertIn("<html lang='ko' data-theme='dark'>",
                      w._shell("홈", "L", "R", theme="dark"))
        # 특수 응답(403 차단 등)도 테마를 따름
        self.assertIn("data-theme='dark'", w._page("차단", "x", theme="dark"))

    def test_settings_theme_picker(self):
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn("화면 테마", page)
        self.assertIn("data-set-theme='light'", page)
        self.assertIn("data-set-theme='dark'", page)
        # 세그먼트 토글 — 해/달 SVG 아이콘 + radiogroup 접근성
        self.assertIn("role='radiogroup'", page)
        self.assertEqual(page.count("<svg"), 2)          # 라이트·다크 아이콘
        # 기본은 라이트가 active (aria-checked 동기화)
        self.assertIn("class='themebtn active' role='radio' aria-checked='true' "
                      "data-set-theme='light'", page)
        # 다크 저장 시 다크가 active
        self.cfg.raw = {"web": {"theme": "dark"}}
        page2 = self.web.render_settings(self.store, self.cfg)
        self.assertIn("class='themebtn active' role='radio' aria-checked='true' "
                      "data-set-theme='dark'", page2)

    def test_appjs_theme_toggle_markers(self):
        js = self.web._APP_JS
        self.assertIn("data-set-theme", js)              # 버튼 위임 처리
        self.assertIn("/settings/theme", js)             # 서버 영구화 POST
        self.assertIn('setAttribute("data-theme"', js)   # 즉시 <html> 적용

    def test_stats_page_follows_theme(self):
        self.cfg.raw = {"web": {"theme": "dark"}}
        page = self.web.render_stats_page(self.store, self.cfg, 4)
        self.assertIn("data-theme='dark'", page)         # 통계도 다크
        self.assertIn("html[data-theme='dark']", page)   # report CSS 다크 대응
        # 차트 전용 색(노드·칩 글자)도 토큰 — 다크 오버라이드가 존재해야
        for tok in ("--node:", "--chip-warn-ink:", "--chip-serious-ink:"):
            self.assertEqual(page.count(tok), 2, msg=tok)  # 라이트 정의 + 다크 오버라이드

    # ─────────────────── 키보드 네비게이션 (기능 5)
    def test_keyboard_nav_markers_in_appjs(self):
        js = self.web._APP_JS
        self.assertIn("keydown", js)
        self.assertIn("navRows", js)
        self.assertIn('=== "j"', js)      # j 키 바인딩
        self.assertIn('=== "k"', js)

    def test_review_job_writes_daily(self):
        # B4: 백그라운드 잡 로직을 동기 호출로 검증(AI 없음)
        self.web._review_job.update(running=False, msg="")
        self.web._run_review_job(self.cfg, False, "2026-07-04")
        self.assertFalse(self.web._review_job["running"])
        self.assertIn("완료", self.web._review_job["msg"])
        p = self.cfg.vault / "daily" / "2026-07-04.md"
        self.assertTrue(p.exists())
        self.assertIn("오늘 메일 핵심", p.read_text(encoding="utf-8"))

    def test_start_review_guard_when_running(self):
        self.web._review_job.update(running=True, msg="")
        self.assertFalse(self.web._start_review(self.cfg, False, "2026-07-04"))
        self.web._review_job.update(running=False, msg="")

    # ─────────────────── 기록 메뉴 · 결정 원장 (Phase 1)

    def test_records_page_tabs_default_daily(self):
        out = self.web.render_records(self.store, self.cfg, {}, "2026-07-04")
        self.assertIn("<b>데일리</b>", out)             # 기본 탭 활성
        self.assertIn("tab=decisions", out)             # 장기기억 탭 링크
        self.assertIn("데일리 리뷰 · 2026-07-04", out)  # 기존 데일리 콘텐츠
        # 날짜 이동 ◀ ▶ — 오늘이 끝이면 다음날 링크 없음
        self.assertIn("tab=daily&date=2026-07-03'>◀", out)
        self.assertNotIn("2026-07-05 ▶", out)
        past = self.web.render_records(self.store, self.cfg,
                                       {"date": ["2026-07-02"]}, "2026-07-04")
        self.assertIn("2026-07-03 ▶", past)             # 과거 날짜에선 다음날 표시

    def test_review_status_running_scene_and_progress(self):
        w = self.web
        # 시작 직후(step=0): 씬 + 흐르는 바(indet), 단계 라벨 없음
        w._review_job.update(running=True, msg="준비 중…", step=0)
        inner, running = w.render_review_status(self.store)
        self.assertTrue(running)
        self.assertIn("data-review-running", inner)      # 폴링 마커 유지
        self.assertIn("libscene", inner)                  # 사서 애니메이션 씬
        self.assertIn("rvfill indet", inner)
        self.assertNotIn("단계", inner)
        # 단계 진행(_job_progress): step 증가 → 채워지는 바 + '단계 2/5'
        w._job_progress("누적 요약 갱신 중…")
        w._job_progress("결정·신호 수확 중…")
        inner2, _ = w.render_review_status(self.store)
        self.assertIn("단계 2/5", inner2)
        self.assertIn("width:40%", inner2)                # 2/5 = 40%
        self.assertIn("결정·신호 수확 중…", inner2)
        self.assertIn("id='rv-stage'", inner2)            # app.js 패치 타깃
        w._job_progress("완료")
        self.assertEqual(w._review_job["step"], 5)
        w._review_job.update(running=False, msg="", step=0)

    def test_appjs_polls_patch_not_replace(self):
        js = self.web._APP_JS
        self.assertIn("rv-stage", js)      # 진행부 패치
        self.assertIn("rvfill", js)
        self.assertIn("data-review-running", js)

    def test_review_status_links_to_pending_queue(self):
        # 정리 완료 화면 → 반영 대기 큐 동선
        self.web._review_job.update(running=False, msg="완료: x.md")
        inner, running = self.web.render_review_status(self.store)
        self.assertFalse(running)
        self.assertNotIn("반영 대기", inner)             # 제안 없으면 링크 없음
        tid = self.store.message("1")["thread_id"]
        self.store.add_decision(tid, "2026-07-04", "A안 확정")
        inner2, _ = self.web.render_review_status(self.store)
        self.assertIn("반영 대기 1건", inner2)
        self.assertIn("/records?tab=decisions", inner2)

    def test_records_decisions_review_queue_and_confirm(self):
        tid = self.store.message("1")["thread_id"]
        did = self.store.add_decision(tid, "2026-07-04", "A안 확정", decider="kim")
        out = self.web.render_records(
            self.store, self.cfg, {"tab": ["decisions"]}, "2026-07-04")
        self.assertIn("장기기억", out)
        self.assertIn("반영 대기 (1)", out)
        self.assertIn(f"action='/decision/{did}/confirm'", out)
        self.assertIn(f"action='/decision/{did}/reject'", out)
        # 확정(사람) → 검토 큐에서 사라지고 확정 목록에
        loc = self.web.perform_action(
            self.store, self.cfg, f"/decision/{did}/confirm", {})
        self.assertIn("/records?tab=decisions", loc)
        out2 = self.web.render_records(
            self.store, self.cfg, {"tab": ["decisions"]}, "2026-07-04")
        self.assertNotIn("반영 대기", out2)
        self.assertIn("A안 확정", out2)
        self.assertEqual(self.store.decision(did)["status"], "confirmed")

    def test_decision_lists_have_flip_buttons(self):
        # 반영 목록 → '유보' 버튼, 유보 목록 → '반영'(복원) 버튼 (상호 복구)
        tid = self.store.message("1")["thread_id"]
        a = self.store.add_decision(tid, "2026-07-04", "A안 확정")
        self.store.set_decision_status(a, "confirmed")
        out = self.web.render_records(
            self.store, self.cfg, {"tab": ["decisions"]}, "2026-07-04")
        self.assertIn(f"action='/decision/{a}/reject'", out)   # 반영 목록의 유보
        self.assertIn(">유보</button>", out)
        # 유보 처리 후 유보 목록에서 복원 버튼
        self.web.perform_action(self.store, self.cfg, f"/decision/{a}/reject", {})
        out2 = self.web.render_records(
            self.store, self.cfg, {"tab": ["decisions"], "st": ["rejected"]},
            "2026-07-04")
        self.assertIn(f"action='/decision/{a}/confirm'", out2)
        self.web.perform_action(self.store, self.cfg, f"/decision/{a}/confirm", {})
        self.assertEqual(self.store.decision(a)["status"], "confirmed")  # 복원됨

    def test_decision_amend_and_reject_actions(self):
        tid = self.store.message("1")["thread_id"]
        a = self.store.add_decision(tid, "2026-07-04", "A안")
        b = self.store.add_decision(tid, "2026-07-04", "B안")
        self.web.perform_action(self.store, self.cfg, f"/decision/{a}/amend",
                                {"title": ["A-1안 확정"], "rationale": ["보완"]})
        row = self.store.decision(a)
        self.assertEqual((row["status"], row["title"], row["rationale"]),
                         ("confirmed", "A-1안 확정", "보완"))
        self.web.perform_action(self.store, self.cfg, f"/decision/{b}/reject", {})
        self.assertEqual(self.store.decision(b)["status"], "rejected")

    def test_thread_record_decision_manual(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("record-decision", out)     # 수동 기록 폼 노출
        self.assertIn("class='lbl'>장기기억<", out)   # 버튼 라벨
        self.assertIn(">✕ 닫기<", out)                # 펼침 시 교체 라벨(CSS 토글)
        self.assertIn("기억할 내용 (필수)", out)
        self.assertIn("value='kim'", out)         # 결정자 기본값 = 최신 수신 발신인
        self.web.perform_action(
            self.store, self.cfg, f"/thread/{tid}/record-decision",
            {"title": ["납기 연기 승인"], "decider": ["kim"]})
        rows = self.store.decisions(status="confirmed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "manual")   # 수동 = 즉시 확정
        # 빈 제목은 거부 — 원장 불변
        self.web.perform_action(
            self.store, self.cfg, f"/thread/{tid}/record-decision",
            {"title": [" "]})
        self.assertEqual(len(self.store.decisions()), 1)

    def test_home_ledger_lens_counts(self):
        out = self.web.render_home(self.store, self.cfg, "2026-07-04")
        self.assertIn("결정 <b>0</b>", out)
        self.assertIn("/records?tab=decisions", out)
        tid = self.store.message("1")["thread_id"]
        self.store.add_decision(tid, "2026-07-04", "X 확정")   # candidate
        out2 = self.web.render_home(self.store, self.cfg, "2026-07-04")
        self.assertIn("제안 1", out2)

    def test_unique_filename_dedup(self):
        from mailkb.sources.outlook_com import _unique_filename
        used = set()
        self.assertEqual(_unique_filename("a.pdf", used), "a.pdf")
        self.assertEqual(_unique_filename("a.pdf", used), "a-1.pdf")
        self.assertEqual(_unique_filename("a.pdf", used), "a-2.pdf")
        self.assertEqual(_unique_filename("noext", used), "noext")
        self.assertEqual(_unique_filename("noext", used), "noext-1")


class TestReport(unittest.TestCase):
    """통계 분석(/stats) — 기간 선택·신호·자기 자신 제외."""

    def setUp(self):
        from mailkb import report
        self.report = report
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          internal_domains=["corp.example"])
        # 3주치: 수신→내 답장 쌍 + 증발 요청(10일 경과) + self-CC
        self.store.ingest([
            _rec("r1", "kim@corp.example", [ME], "설계 검토",
                 "2026-06-22T09:00:00", "검토 부탁드립니다."),
            _rec("r2", ME, ["kim@corp.example"], "RE: 설계 검토",
                 "2026-06-22T14:00:00", "확인했습니다.", reply_to="r1"),
            _rec("r3", "lee@corp.example", [ME], "일정 문의",
                 "2026-06-29T10:00:00", "가능한 일정 회신 부탁드립니다."),
            _rec("r4", ME, ["lee@corp.example", ME], "RE: 일정 문의",
                 "2026-06-30T09:00:00", "7/10 가능합니다.", reply_to="r3"),
            # 증발 요청: 내가 마지막으로 질문, 이후 수신 없음 (asof 대비 10일+)
            _rec("r5", ME, ["oh@corp.example"], "지그 도면 요청",
                 "2026-06-30T11:00:00", "도면 송부 부탁드립니다. 가능할까요?"),
            _rec("r6", "kim@corp.example", [ME], "주간 진행",
                 "2026-07-10T09:00:00", "진행 상황 공유드립니다."),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_clamp_weeks(self):
        c = self.report.clamp_weeks
        self.assertEqual(c(None), 4)
        self.assertEqual(c("abc"), 4)
        self.assertEqual(c("999"), 4)
        self.assertEqual(c("8"), 8)
        self.assertEqual(c(2), 2)
        self.assertEqual(c(16), 16)

    def test_period_bounds_dataset(self):
        # [분석] 이 데이터에 실제로 먹히려면 load 가 선택 기간 밖 메일을 빼야 한다.
        # (이게 빠지면 §1 등 창 무관 섹션이 기간을 바꿔도 그대로 → '변화 없음' 버그)
        self.store.ingest([_rec("old1", "kim@corp.example", [ME], "지난달 건",
                                "2026-06-08T09:00:00", "오래된 메일입니다.")])
        d2 = self.report.load(self.store.db, 2, {ME})
        d16 = self.report.load(self.store.db, 16, {ME})
        ws2 = d2["weeks"][0].isoformat()
        self.assertTrue(all(m["sent_on"][:10] >= ws2 for m in d2["msgs"]))  # 불변식
        subj2 = {m["subject"] for m in d2["msgs"]}
        subj16 = {m["subject"] for m in d16["msgs"]}
        self.assertNotIn("지난달 건", subj2)       # 2주 창 밖 → 제외
        self.assertIn("지난달 건", subj16)          # 넓은 창 → 포함
        self.assertLess(len(d2["msgs"]), len(d16["msgs"]))

    def test_render_stats_content(self):
        # render_stats 는 이제 '콘텐츠 조각'만 반환 — nav 셸/스크립트는 web 래퍼가 씌운다
        out = self.report.render_stats(self.store, self.cfg, 4)
        for marker in ("통계 분석", "검토 기간",
                       # 기간은 곧 링크 — 누르면 그 자리에서 재분석 (별도 버튼 없음)
                       '<div class="periods">',
                       'href="/stats?weeks=2"', 'href="/stats?weeks=16"',
                       'class="popt active" href="/stats?weeks=4"',   # 현재 기간 강조
                       # 6개 개편 섹션 (2026-07-13)
                       "볼륨 추세", "활동 히트맵", "응답 시간",
                       "받은 메일 구성", "왕복 많은 논의", "자주 주고받는 상대"):
            self.assertIn(marker, out, msg=marker)
        # 제거된 옛 섹션은 없어야
        self.assertNotIn("조용해진 사람", out)
        self.assertNotIn("증발한 내 요청", out)
        self.assertNotIn("응답 지연 추세", out)
        # [분석] 버튼·라디오·폼은 제거됨
        self.assertNotIn("분석</button>", out)
        self.assertNotIn('type="radio"', out)
        self.assertNotIn("<form", out)
        # 조각이므로 셸 요소(doctype/nav/script/backlink)는 web 래퍼 몫 — 여기엔 없음
        self.assertNotIn("<!doctype", out.lower())
        self.assertNotIn("<script", out)
        self.assertNotIn("← Minerva 홈", out)
        # 답 대기 목록(구 증발)이 §3 으로 흡수됨 (r5: asof 7/10 기준 10일 경과)
        self.assertIn("지그 도면 요청", out)
        self.assertIn("답을 기다리는 내 발신", out)

    def test_stats_new_sections_render(self):
        out = self.report.render_stats(self.store, self.cfg, 4)
        # §1 2계열 라인 (발신+수신 payload — data-chart 는 escape 되어 &quot;)
        self.assertIn('id="trend"', out)
        self.assertIn("series2", out)
        # §2 히트맵 (발신/수신 2개)
        self.assertEqual(out.count('class="heatmap"'), 2)
        # §4 받은 메일 구성 누적 막대 + 범례
        self.assertIn('class="mixbar"', out)
        self.assertIn("업무 · 직접(To)", out)
        # §6 이름 클릭 → /person 링크 (그래프 노드·표)
        self.assertIn('href="/person?addr=', out)

    def test_stats_person_links_navigate(self):
        # §6 표·그래프의 이름이 그 사람 메일로 연결 (요청: 이름 클릭 → email 창)
        out = self.report.render_stats(self.store, self.cfg, 4)
        self.assertIn("/person?addr=kim%40corp.example", out)

    def test_sig_inbox_mix_priority(self):
        # 상호배타 우선순위: 스팸 > 공지 > 직접 > 참조
        d = self.report.load(self.store.db, 8, {ME})
        mix = self.report.sig_inbox_mix(d, self.cfg)
        self.assertEqual(mix["total"], sum(mix["seg"].values()))
        # r1/r3/r6 은 To=[ME] 직접 수신
        self.assertGreaterEqual(mix["seg"]["direct"], 3)

    def test_sig_pingpong_counts_turns(self):
        # kim 스레드: r1(수신)→r2(발신) = 1왕복(2미만)이라 제외.
        # 왕복 2+ 스레드를 하나 구성해 검증
        self.store.ingest([
            _rec("pp1", "kim@corp.example", [ME], "핑퐁 논의",
                 "2026-07-08T09:00:00", "질문1 부탁드립니다?"),
            _rec("pp2", ME, ["kim@corp.example"], "RE: 핑퐁 논의",
                 "2026-07-08T10:00:00", "답1.", reply_to="pp1"),
            _rec("pp3", "kim@corp.example", [ME], "RE: 핑퐁 논의",
                 "2026-07-08T11:00:00", "재질문2?", reply_to="pp2"),
            _rec("pp4", ME, ["kim@corp.example"], "RE: 핑퐁 논의",
                 "2026-07-08T12:00:00", "답2.", reply_to="pp3"),
        ])
        d = self.report.load(self.store.db, 8, {ME})
        ping = self.report.sig_pingpong(d, self.cfg)
        hit = [p for p in ping if p["subject"] == "핑퐁 논의"]
        self.assertTrue(hit)
        self.assertEqual(hit[0]["turns"], 3)   # 수→발→수→발 = 3 전환

    def test_sig_response_both_directions(self):
        d = self.report.load(self.store.db, 8, {ME})
        my = self.report._reply_pairs(d)
        their = self.report._their_pairs(d)
        resp = self.report.sig_response(d, my, their)
        # r1→r2: 내 응답 5h. r3→r4: 내 응답 23h. 표본 2건.
        self.assertEqual(resp["mine_n"], 2)
        # r5→(무응답) 은 their 에 안 잡힘; r6 은 kim 발신이나 앞선 내 발신 없음

    def test_volume_period_follows_weeks(self):
        out2 = self.report.render_stats(self.store, self.cfg, 2)
        self.assertIn("최근 14일 기준", out2)
        out8 = self.report.render_stats(self.store, self.cfg, 8)
        self.assertIn("최근 56일 기준", out8)

    def test_ego_graph_edge_for_any_exchange(self):
        # §5 그래프: 상위 8 발신/수신 목록 밖의 방향(수신 1통)도 실측(rows)
        # 기반으로 선이 그려진다 — 선 없음은 그 방향 교류 0 일 때만
        rows = [{"name": f"p{i}", "addr": f"p{i}@c", "sent": 50 - i, "recv": 30}
                for i in range(9)]
        rows.append({"name": "민수", "addr": "m@c", "sent": 40, "recv": 1})
        rows.sort(key=lambda r: -(r["sent"] + r["recv"]))
        vol = {"sent": [], "recv": [], "rows": rows, "days": 28}
        svg = self.report.svg_ego_graph(vol)   # 상위 목록이 비어도 무관해야 함
        self.assertIn("민수 → 나 · 수신 1통", svg)
        self.assertIn("나 → 민수 · 발신 40통", svg)

    def test_my_addresses_excluded_from_volume(self):
        # self-CC(r4 의 To 에 ME 포함)가 §6 상대 목록에 나오면 안 됨
        d = self.report.load(self.store.db, 4, {ME})
        vol = self.report.sig_volume(d, days=28)
        addrs = {r["addr"] for r in vol["rows"]}
        self.assertNotIn(ME, addrs)
        self.assertIn("kim@corp.example", addrs)

    def test_alias_reflagged_as_sent(self):
        # 별칭 발신이 is_sent=0 으로 들어와도 load 가 발신으로 재분류
        self.store.ingest([_rec(
            "al", "alias@corp.example", ["kim@corp.example"], "별칭 발신",
            "2026-07-09T09:00:00", "전달드립니다.")])
        d = self.report.load(self.store.db, 4, {ME, "alias@corp.example"})
        al = [m for m in d["msgs"] if m["sender_addr"] == "alias@corp.example"]
        self.assertTrue(all(m["is_sent"] == 1 for m in al))
        self.assertNotIn("alias@corp.example", d["mutual"])

    def test_empty_db_graceful(self):
        empty = Store(Path(self.tmp.name) / "e.sqlite", [ME])
        self.addCleanup(empty.close)
        out = self.report.render_stats(empty, self.cfg, 4)
        self.assertIn("메일이 없습니다", out)
        self.assertIn("검토 기간", out)   # 빈 상태에서도 기간 선택은 표시


class TestDailyMarkdown(unittest.TestCase):
    """데일리 페이지 마크다운→HTML 구조 렌더(다른 페이지와 톤 일치)."""

    def _html(self, md):
        from mailkb import web
        return web._md_to_html(md)

    def test_headings_and_refs_and_bold(self):
        html = self._html("# 2026-07-06 데일리 리뷰\n\n"
                          "## 오늘의 결정\n- **확정**: [#3] A안 채택\n")
        self.assertNotIn("# 2026", html)          # 날짜 h1 은 페이지가 이미 표시 → 스킵
        self.assertIn("<h2>오늘의 결정</h2>", html)
        self.assertIn("<strong>확정</strong>", html)
        self.assertIn('<a href="/thread/3">#3</a>', html)
        self.assertNotIn("## ", html)             # 원시 마크다운 노출 안 됨

    def test_nested_list_balanced(self):
        html = self._html("## 개입 필요\n- **🔴 결정**\n  - 항목1\n  - 항목2\n- **🟠 응답**\n")
        import re as _re
        self.assertEqual(len(_re.findall(r"<ul[ >]", html)), html.count("</ul>"))
        self.assertEqual(len(_re.findall(r"<li[ >]", html)), html.count("</li>"))
        self.assertIn("<ul>\n<li>", html)         # 중첩 존재

    def test_script_escaped(self):
        html = self._html("- <script>alert(1)</script>")
        self.assertNotIn("<script>", html)


class TestWindowsCompat(unittest.TestCase):
    """회사 PC(Windows) 배포에서 깨지던 지점의 회귀 가드."""

    def test_dasl_utc_shifts_local_to_utc(self):
        # DASL 날짜 비교는 UTC — KST 09:00 은 UTC 00:00, 오버랩 30분 빼서 23:30
        import time as _time
        if not hasattr(_time, "tzset"):
            self.skipTest("tzset 없음 (Windows)")
        from mailkb.sources.outlook_com import _dasl_utc
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Seoul"
            _time.tzset()
            self.assertEqual(_dasl_utc("2026-07-06T09:00:00"), "2026-07-05 23:30")
            self.assertEqual(
                _dasl_utc("2026-07-06T09:00:00", overlap_minutes=0),
                "2026-07-06 00:00")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            _time.tzset()

    def test_ai_resolve_absolute_path(self):
        # which 로 절대경로 해석 (Windows 에서 .cmd 셔틀을 찾는 경로와 동일)
        resolved = review._ai_resolve(["python3", "-c", "pass"])
        self.assertTrue(os.path.isabs(resolved[0]))
        self.assertEqual(resolved[1:], ["-c", "pass"])
        with self.assertRaises(FileNotFoundError):
            review._ai_resolve(["mailkb-no-such-cmd-xyz"])

    def test_ai_run_utf8_roundtrip(self):
        # subprocess 인코딩이 utf-8 고정인지 — cp949 밖 문자(이모지) 왕복
        out = review.ai_run(
            ["python3", "-c", "import sys; print(sys.stdin.read())"],
            "긴급 🔴 확인", timeout=30, retries=0)
        self.assertEqual(out, "긴급 🔴 확인")


def _recx(mid, sender, subject, when, body="본문", to=None, cc=None,
          attachments=None, sender_name=None):
    """검색 테스트용 레코드 — cc·첨부·표시명을 직접 지정."""
    return MailRecord(
        message_id=f"<{mid}@t>",
        subject=subject,
        sender_name=sender_name if sender_name is not None else sender.split("@")[0],
        sender_addr=sender,
        to=to if to is not None else [ME],
        cc=cc or [],
        sent_on=when,
        body_text=body,
        attachments=attachments or [],
    )


class TestSearchParse(unittest.TestCase):
    def test_operators_and_terms(self):
        q = search_mod.parse_query('from:강미래 after:2026-06 has:attachment 캐시 "정확한 구"')
        self.assertEqual(q.from_, ["강미래"])
        self.assertEqual(q.after, "2026-06-01")
        self.assertTrue(q.has_attach)
        self.assertIn("캐시", q.terms)
        self.assertIn("정확한 구", q.phrases)

    def test_quoted_operator_value_keeps_space(self):
        q = search_mod.parse_query('from:"강 미래" 리포트')
        self.assertEqual(q.from_, ["강 미래"])
        self.assertEqual(q.terms, ["리포트"])

    def test_is_and_thread_and_file(self):
        q = search_mod.parse_query("is:unread is:sent thread:12 file:xlsx")
        self.assertEqual(q.is_flags, {"unread", "sent"})
        self.assertEqual(q.thread, 12)
        self.assertEqual(q.files, ["xlsx"])

    def test_unknown_key_is_a_term(self):
        q = search_mod.parse_query("http://x.co/1 검토")
        self.assertIn("http://x.co/1", q.terms)
        self.assertEqual(q.from_, [])

    def test_date_boundaries(self):
        self.assertEqual(search_mod.date_floor("2026"), "2026-01-01")
        self.assertEqual(search_mod.date_floor("2026-06"), "2026-06-01")
        self.assertEqual(search_mod.date_ceil("2026-06"), "2026-07-01")
        self.assertEqual(search_mod.date_ceil("2026-12"), "2027-01-01")
        self.assertEqual(search_mod.date_ceil("2026-06-15"), "2026-06-16")
        self.assertIsNone(search_mod.date_floor("nope"))

    def test_on_sets_both_bounds(self):
        q = search_mod.parse_query("on:2026-06")
        self.assertEqual(q.after, "2026-06-01")
        self.assertEqual(q.before, "2026-07-01")

    def test_short_vs_fts_terms(self):
        q = search_mod.parse_query("모델 리포트 평가")
        self.assertEqual(set(search_mod.terms_fts(q)), {"리포트"})
        self.assertEqual(set(search_mod.terms_short(q)), {"모델", "평가"})

    def test_build_match_tiers(self):
        q = search_mod.parse_query("모델 평가 리포트")
        self.assertEqual(search_mod.build_match(q, 1), '"모델 평가 리포트"')  # 연속 구
        self.assertEqual(search_mod.build_match(q, 2), '"리포트"')            # ≥3자만 AND
        # OR: ≥3자 하나뿐이면 None (OR 무의미)
        self.assertIsNone(search_mod.build_match(q, 3))
        q2 = search_mod.parse_query("리포트 침투테스트")
        self.assertEqual(search_mod.build_match(q2, 3), '"리포트" OR "침투테스트"')


class TestSearchEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.store.ingest([
            _recx("s1", "kang@corp.example", "모델 평가 리포트 공유",
                  "2026-06-10T09:00:00", body="사내 모델 평가 파이프라인 정리",
                  sender_name="강미래 선임", attachments=["report.xlsx"]),
            _recx("s2", "kang@corp.example", "RE: 모델 평가 리포트 공유",
                  "2026-07-02T09:00:00", body="침투테스트 결과 후속 조치 필요",
                  sender_name="강미래 선임"),
            _recx("s3", "lee@corp.example", "주간 리포트 W25",
                  "2026-05-20T09:00:00", body="가동률 72% 입니다",
                  sender_name="이서연", to=[ME], cc=["kang@corp.example"]),
            _recx("s4", ME, "보낸 메일 예시", "2026-07-05T09:00:00",
                  body="회신드립니다", to=["kang@corp.example"], sender_name="나"),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_phrase_tier_and_snippet(self):
        rows = self.store.search("모델 평가")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["tier"], 1)                 # 연속 구
        self.assertIn("⟪", rows[0]["snippet"])               # 강조 마커

    def test_two_char_korean_via_like(self):
        # '평가'(2자)는 FTS 불가 → LIKE(tier3)로라도 잡혀야 한다
        rows = self.store.search("평가")
        self.assertTrue(rows)
        self.assertTrue(all(r["tier"] == 3 for r in rows))

    def test_from_name_space_normalized(self):
        # 저장은 '강미래 선임' — 공백 무시로 'from:강미래선임' 도 맞아야
        self.assertTrue(self.store.search("from:강미래선임"))
        self.assertTrue(self.store.search("from:강미래"))

    def test_from_filter_narrows(self):
        only_kang = self.store.search("from:강미래 리포트")
        self.assertTrue(only_kang)
        self.assertTrue(all("kang@" in r["sender_addr"] for r in only_kang))

    def test_date_filter(self):
        after = self.store.search("리포트 after:2026-06")
        self.assertTrue(after)
        self.assertTrue(all(r["sent_on"] >= "2026-06-01" for r in after))
        self.assertFalse(any(r["message_id"] == "<s3@t>" for r in after))  # 5월 제외

    def test_is_sent_and_has_attachment(self):
        self.assertTrue(all(r["is_sent"] for r in self.store.search("is:sent")))
        att = self.store.search("has:attachment")
        self.assertTrue(att)
        self.assertTrue(all(r["attach_names"] for r in att))

    def test_to_resolves_korean_name_via_people(self):
        # cc:강미래 → people 에서 주소 해석 후 cc_addrs 매칭 (s3)
        rows = self.store.search("cc:강미래")
        self.assertTrue(any(r["message_id"] == "<s3@t>" for r in rows))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.store.search(""), [])

    def test_structured_only_no_text(self):
        rows = self.store.search("is:sent")             # 텍스트 없이 필터만
        self.assertTrue(rows)
        self.assertTrue(all(r["tier"] == 0 for r in rows))

    def test_frequent_people(self):
        ppl = self.store.frequent_people()
        names = [p["name"] for p in ppl]
        self.assertIn("강미래 선임", names)


class TestSearchWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        from mailkb.config import Config
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])
        self.store.ingest([
            _recx("w1", "kang@corp.example", "모델 평가 공유", "2026-06-10T09:00:00",
                  body="사내 모델 평가 리포트 정리", sender_name="강미래 선임"),
            _recx("w2", "lee@corp.example", "주간 보고", "2026-06-11T09:00:00",
                  body="가동률 리포트 보고", sender_name="이서연"),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_effective_merges_advanced_fields(self):
        _, eff = web._search_effective(
            {"q": ["리포트"], "f_from": ["강미래"], "f_period": ["thismonth"],
             "f_dir": ["received"], "f_has": ["1"]}, "2026-07-13")
        self.assertIn("리포트", eff)
        self.assertIn("from:강미래", eff)
        self.assertIn("after:2026-07", eff)
        self.assertIn("is:received", eff)
        self.assertIn("has:attachment", eff)

    def test_period_tokens(self):
        self.assertEqual(web._period_tokens("thismonth", "2026-07-13"), ["after:2026-07"])
        self.assertEqual(web._period_tokens("lastmonth", "2026-07-13"),
                         ["after:2026-06", "before:2026-07"])
        self.assertEqual(web._period_tokens("thisyear", "2026-07-13"), ["after:2026"])

    def test_search_input_promoted_to_header(self):
        # 검색 입력은 헤더 상시 검색창(navsearch)으로 승격 — nav '검색' 링크 없음
        self.assertIn("class='navsearch'", web._NAV)
        self.assertIn("action='/search'", web._NAV)
        self.assertNotIn('href="/search"', web._NAV)      # 링크는 제거됨
        self.assertIn("syncNavSearch", web._APP_JS)       # /search 시 q 로 채움

    def test_render_has_box_hint_datalist(self):
        html = web.render_search(self.store, self.cfg, {"q": [""]}, "2026-07-13")
        self.assertIn("form class='search'", html)        # 페이지 검색창(질의 편집)
        self.assertIn("shint", html)                      # 힌트
        self.assertIn("<datalist id='ppl'>", html)        # 사람 자동완성
        self.assertIn("강미래 선임", html)                  # people 옵션

    def test_advanced_open_when_no_results(self):
        # 결과 없을 땐 상세 검색이 펼쳐져 보이고, 결과 있으면 접힘
        blank = web.render_search(self.store, self.cfg, {"q": [""]}, "2026-07-13")
        self.assertIn("<details class='adv' open>", blank)
        hit = web.render_search(self.store, self.cfg, {"q": ["리포트"]}, "2026-07-13")
        self.assertIn("<details class='adv'>", hit)       # 접힘

    def test_render_snippet_and_facets(self):
        html = web.render_search(self.store, self.cfg, {"q": ["리포트"]}, "2026-07-13")
        self.assertIn("<mark>", html)                     # 스니펫 강조
        self.assertIn("class='facet'", html)              # 좁히기 칩

    def test_lowrel_divider_only_for_or_tier(self):
        # 붙은 구/AND 는 '관련 낮음' 없음
        html = web.render_search(self.store, self.cfg, {"q": ["모델 평가"]}, "2026-07-13")
        self.assertNotIn("관련 낮음", html)


class TestAISearchStage1(unittest.TestCase):
    """Phase 2 Stage 1 — 번역 + 캐시 뼈대."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_parse_json_obj_variants(self):
        self.assertEqual(review._parse_json_obj('{"a": 1}'), {"a": 1})
        self.assertEqual(review._parse_json_obj('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(review._parse_json_obj('결과: {"a": 3} 끝'), {"a": 3})
        self.assertIsNone(review._parse_json_obj("no json here"))
        self.assertIsNone(review._parse_json_obj('[1,2,3]'))   # 객체 아님

    def test_translate_parses_ai_dsl(self):
        payload = ('{"dsl": "from:강미래 after:2026-06 리포트", '
                   '"fallback_dsl": "리포트", "expansions": ["리포트","report"], '
                   '"note": "발신자·기간·키워드"}')
        with mock.patch.object(review, "ai_run", return_value=payload):
            r = review.ai_translate_query(self.cfg, "지난달 강미래 리포트 찾아줘", "2026-07-13")
        self.assertEqual(r["dsl"], "from:강미래 after:2026-06 리포트")
        self.assertEqual(r["fallback_dsl"], "리포트")
        self.assertIn("report", r["expansions"])

    def test_translate_falls_back_on_junk(self):
        with mock.patch.object(review, "ai_run", return_value="죄송하지만 모르겠어요"):
            r = review.ai_translate_query(self.cfg, "원래 질의 키워드", "2026-07-13")
        self.assertEqual(r["dsl"], "원래 질의 키워드")   # 원문 폴백

    def test_translate_falls_back_on_empty_dsl(self):
        with mock.patch.object(review, "ai_run", return_value='{"dsl": ""}'):
            r = review.ai_translate_query(self.cfg, "백업 키워드", "2026-07-13")
        self.assertEqual(r["dsl"], "백업 키워드")

    def test_translate_uses_search_backend(self):
        # 기본 백엔드가 ai_search_backend(sonnet)로 라우팅되는지
        with mock.patch.object(review, "ai_run", return_value='{"dsl":"x"}'), \
                mock.patch.object(self.cfg, "ai_cmd", return_value=["echo"]) as cmd:
            review.ai_translate_query(self.cfg, "q", "2026-07-13")
        cmd.assert_called_with("sonnet")

    def test_cache_put_get_recent(self):
        self.store.ai_search_put("지난달 리포트", "지난달 리포트",
                                 "from:강미래 리포트", '{"top":[1]}', "sonnet")
        row = self.store.ai_search_get("지난달 리포트")
        self.assertIsNotNone(row)
        self.assertEqual(row["dsl"], "from:강미래 리포트")
        self.assertEqual(row["result_json"], '{"top":[1]}')
        # 덮어쓰기(UPSERT)
        self.store.ai_search_put("지난달 리포트", "지난달 리포트",
                                 "리포트", '{"top":[2]}', "sonnet")
        self.assertEqual(self.store.ai_search_get("지난달 리포트")["result_json"],
                         '{"top":[2]}')
        self.assertEqual(len(self.store.ai_search_recent()), 1)   # 같은 키 = 1건
        self.assertIsNone(self.store.ai_search_get("없는 질의"))

    def test_messages_by_ids(self):
        self.store.ingest([
            _recx("a1", "kang@corp.example", "제목1", "2026-07-01T09:00:00", body="본문1"),
            _recx("a2", "lee@corp.example", "제목2", "2026-07-02T09:00:00", body="본문2"),
        ])
        rows = self.store.messages_by_ids([1, 2])
        self.assertEqual({r["id"] for r in rows}, {1, 2})
        self.assertEqual(self.store.messages_by_ids([]), [])


class TestAISearchPipeline(unittest.TestCase):
    """Phase 2 Stage 2·3 — 재순위·자기교정·심층읽기·오케스트레이터 (ai_run 목)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])
        self.store.ingest([
            _recx("r1", "kang@corp.example", "주간 리포트 W25", "2026-07-01T09:00:00",
                  body="가동률 리포트 본문입니다", sender_name="강미래 선임"),
            _recx("r2", "lee@corp.example", "회의 안건", "2026-07-02T09:00:00",
                  body="다음 주 회의 안건 정리", sender_name="이서연"),
        ])
        self.rid = self.store.search("리포트")[0]["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ai_search_run_meters_cost(self):
        # claude -p --output-format json 응답에서 실제 비용·토큰을 뽑아 meter 에 누적
        meter = {"usd": 0.0, "in": 0, "out": 0, "calls": 0}
        payload = ('{"result": "{\\"dsl\\":\\"x\\"}", "total_cost_usd": 0.02, '
                   '"usage": {"input_tokens": 100, "output_tokens": 20}}')
        with mock.patch.object(review, "ai_run", return_value=payload) as run:
            out = review._ai_search_run(self.cfg, "p", "sonnet", 30, meter)
        self.assertEqual(out, '{"dsl":"x"}')          # result 텍스트만 추출
        self.assertIn("--output-format", run.call_args[0][0])   # json 모드로 호출
        self.assertAlmostEqual(meter["usd"], 0.02)
        self.assertEqual(meter["calls"], 1)
        self.assertEqual(meter["in"] + meter["out"], 120)

    def test_orchestrator_happy_path_and_cache(self):
        rid = self.rid
        # 번역 → 본문심사(재순위+확정 통합) = 2콜
        side = [
            f'{{"dsl": "리포트", "fallback_dsl": "", "expansions": ["report"], "note": "키워드"}}',
            f'{{"ranked": [{{"id": {rid}, "reason": "본문도 리포트", "match": true}}]}}',
        ]
        with mock.patch.object(review, "ai_run", side_effect=side) as run:
            res = review.ai_search(self.store, self.cfg, "리포트 찾아줘", "2026-07-13")
        self.assertEqual(res["dsl"], "리포트")
        self.assertEqual(res["items"][0]["id"], rid)
        self.assertEqual(res["items"][0]["reason"], "본문도 리포트")   # 본문심사 이유가 최종
        self.assertFalse(res["from_cache"])
        self.assertEqual(run.call_count, 2)                          # 번역+본문심사
        # 두 번째 동일 질의 → 캐시 히트, AI 미호출
        with mock.patch.object(review, "ai_run",
                               side_effect=AssertionError("AI 재호출 금지")) as run2:
            res2 = review.ai_search(self.store, self.cfg, "리포트 찾아줘", "2026-07-13")
        self.assertTrue(res2["from_cache"])
        run2.assert_not_called()

    def test_self_correct_retries_when_nothing_relevant(self):
        rid = self.rid
        # 1차 DSL '회의'는 후보(r2)를 잡지만 본문심사가 '부합 0' 판정 → 자기교정 재검색
        side = [
            '{"dsl": "회의", "fallback_dsl": "", "note": "1차"}',
            '{"ranked": []}',                                         # 부합 0 → 자기교정
            '{"dsl": "리포트", "fallback_dsl": "", "note": "2차 넓힘"}',
            f'{{"ranked": [{{"id": {rid}, "reason": "본문 확인", "match": true}}]}}',
        ]
        with mock.patch.object(review, "ai_run", side_effect=side) as run:
            res = review.ai_search(self.store, self.cfg, "리포트", "2026-07-13")
        self.assertEqual(res["dsl"], "리포트")                        # 자기교정 후 DSL
        self.assertEqual(res["items"][0]["id"], rid)
        self.assertEqual(run.call_count, 4)   # 번역·본문심사·재번역·본문심사

    def test_write_cache_even_when_bypassing_read(self):
        # '새로 찾기'(use_cache=False)도 결과를 캐시에 저장해 다음 조회에 반영
        rid = self.rid
        side = [
            '{"dsl": "리포트", "note": "k"}',
            f'{{"ranked": [{{"id": {rid}, "reason": "본문 확인", "match": true}}]}}',
        ]
        with mock.patch.object(review, "ai_run", side_effect=side):
            review.ai_search(self.store, self.cfg, "리포트", "2026-07-13", use_cache=False)
        self.assertIsNotNone(self.store.ai_search_get("리포트"))

    def test_body_judge_drops_nonmatch(self):
        rid = self.rid
        # 본문심사가 match=false → 탈락. 자기교정 재번역이 같은 DSL 이면 재검색 없이 끝.
        side = [
            '{"dsl": "리포트", "note": "k"}',
            f'{{"ranked": [{{"id": {rid}, "reason": "본문 보니 무관", "match": false}}]}}',
            '{"dsl": "리포트", "note": "재번역도 동일"}',
        ]
        with mock.patch.object(review, "ai_run", side_effect=side):
            res = review.ai_search(self.store, self.cfg, "리포트", "2026-07-13")
        self.assertEqual(res["items"], [])    # 본문 확인서 탈락

    def test_progress_callback_streams_stages(self):
        # 방법 7·8 훅: 단계 콜백이 순서대로 오고, prelim 에 엔진 잠정 결과가 실린다.
        rid = self.rid
        side = [
            '{"dsl": "리포트", "note": "k"}',
            f'{{"ranked": [{{"id": {rid}, "reason": "본문 확인", "match": true}}]}}',
        ]
        seen = []
        with mock.patch.object(review, "ai_run", side_effect=side):
            review.ai_search(self.store, self.cfg, "리포트", "2026-07-13",
                             progress=lambda s, p: seen.append((s, p)))
        stages = [s for s, _ in seen]
        self.assertEqual(stages[:3], ["translate", "search", "prelim"])
        self.assertEqual(stages[-1], "done")
        prelim = [p for s, p in seen if s == "prelim"][0]
        self.assertTrue(prelim["preliminary"])
        self.assertEqual(prelim["items"][0]["id"], rid)     # 본문 읽기 전 잠정 후보


class TestAISearchWeb(unittest.TestCase):
    """Phase 2 Stage 4 — 웹 UI (render_aisearch·render_search AI 분기·버튼)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME])
        self.store.ingest([
            _recx("r1", "kang@corp.example", "주간 리포트", "2026-07-01T09:00:00",
                  body="가동률 리포트", sender_name="강미래 선임"),
        ])
        # 백그라운드 잡은 모듈 전역 — 테스트 간 오염 방지로 매번 초기화
        web._aisearch_job.update(running=False, stage="", query="", fresh=False,
                                 result=None, error="", prelim=None)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    _RESULT = {
        "query": "지난달 강미래 리포트", "dsl": "from:강미래 after:2026-06 리포트",
        "note": "발신자·기간·키워드 일치", "expansions": ["리포트"],
        "items": [{"id": 1, "thread_id": 1, "subject": "주간 리포트",
                   "sender": "강미래 선임", "date": "2026-07-01T09:00",
                   "is_sent": False, "reason": "제목·발신 일치"}],
        "others": [{"id": 2, "thread_id": 2, "subject": "기타",
                    "sender": "이서연", "date": "2026-06-30T10:00",
                    "is_sent": False, "reason": "약한 관련"}],
        "candidate_count": 12, "backend": "sonnet", "from_cache": False,
    }

    def test_render_aisearch_markup(self):
        html = web.render_aisearch(self._RESULT)
        self.assertIn("AI 해석", html)
        self.assertIn("from:강미래 after:2026-06 리포트", html)   # 해석 DSL 노출
        self.assertIn("aiedit", html)                           # 편집 링크
        self.assertIn("class='aicards'", html)
        self.assertIn("제목·발신 일치", html)                    # 이유
        self.assertIn("/thread/1", html)                        # 카드 링크
        self.assertIn("그 외 후보", html)                        # others 접힘
        self.assertIn("후보 12개 검토", html)                    # 근거 푸터
        self.assertIn("sonnet", html)

    def test_render_aisearch_empty(self):
        r = dict(self._RESULT, items=[], others=[])
        html = web.render_aisearch(r)
        self.assertIn("찾지 못했습니다", html)
        self.assertIn("일반 검색으로 보기", html)

    def test_render_aisearch_shows_cost(self):
        r = dict(self._RESULT, cost={"usd": 0.037, "in": 4000, "out": 500, "calls": 3})
        html = web.render_aisearch(r)
        self.assertIn("$0.037", html)
        self.assertIn("4,500토큰", html)
        self.assertIn("3회", html)

    def test_render_search_ai_cache_hit_immediate(self):
        # 캐시 히트면 잡 없이 즉시 결과(무과금·무대기)
        import json as _json
        norm = review._normalize_q("지난달 강미래 리포트")
        self.store.ai_search_put(norm, "지난달 강미래 리포트", "리포트",
                                 _json.dumps(self._RESULT), "sonnet")
        with mock.patch.object(web, "_start_aisearch",
                               side_effect=AssertionError("잡 시작 금지")):
            html = web.render_search(self.store, self.cfg,
                                     {"q": ["지난달 강미래 리포트"], "ai": ["1"]},
                                     "2026-07-13")
        self.assertIn("class='aicards'", html)                  # AI 결과 화면
        self.assertIn("제목·발신 일치", html)

    def test_render_search_ai_miss_starts_job(self):
        # 캐시 미스 → 백그라운드 잡 시작 + 대기 화면(서버 안 멈춤)
        def fake_start(*a, **k):   # 실제 잡처럼 running 플래그만 세운다(스레드 없이)
            web._aisearch_job.update(running=True, stage="translate", query="없는질의999")
        with mock.patch.object(web, "_start_aisearch", side_effect=fake_start) as m:
            html = web.render_search(self.store, self.cfg,
                                     {"q": ["없는질의999"], "ai": ["1"]}, "2026-07-13")
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("use_cache"), True)
        self.assertIn("data-aisearch-running", html)            # 폴링 마커
        self.assertIn("AI가 찾고 있어요", html)
        self.assertNotIn("<datalist", html)                     # 일반 검색 화면 아님

    def test_render_aisearch_shows_expansions(self):
        html = web.render_aisearch(self._RESULT)      # expansions=["리포트"]
        self.assertIn("확장 검색어", html)

    def test_render_aisearch_refresh_link_when_cached(self):
        html = web.render_aisearch(dict(self._RESULT, from_cache=True))
        self.assertIn("새로 찾기", html)
        self.assertIn("fresh=1", html)

    def test_ai_fresh_bypasses_cache_read(self):
        # '새로 찾기'(fresh=1)는 캐시가 있어도 읽지 않고 use_cache=False 로 잡 시작
        import json as _json
        norm = review._normalize_q("리포트")
        self.store.ai_search_put(norm, "리포트", "리포트",
                                 _json.dumps(self._RESULT), "sonnet")
        def fake_start(*a, **k):
            web._aisearch_job.update(running=True, stage="translate", query="리포트")
        with mock.patch.object(web, "_start_aisearch", side_effect=fake_start) as m:
            html = web.render_search(self.store, self.cfg,
                                     {"q": ["리포트"], "ai": ["1"], "fresh": ["1"]},
                                     "2026-07-13")
        m.assert_called_once()
        self.assertFalse(m.call_args.kwargs.get("use_cache", True))
        self.assertIn("data-aisearch-running", html)            # 캐시 무시하고 대기 화면

    def test_aisearch_status_running_shows_stage_and_prelim(self):
        # 진행 중 상태 → 단계 바·잠정 결과(방법 7·8)
        web._aisearch_job.update(
            running=True, stage="prelim", query="강미래 리포트",
            prelim={"items": self._RESULT["items"]})
        inner, running = web.render_aisearch_status(self.store, self.cfg, "2026-07-13")
        self.assertTrue(running)
        self.assertIn("data-aisearch-running", inner)
        self.assertIn("id='ai-stage'", inner)
        self.assertIn("id='ai-prelim'", inner)
        self.assertIn("잠정", inner)
        self.assertIn("주간 리포트", inner)                     # 잠정 후보 카드

    def test_aisearch_status_done_shows_result(self):
        web._aisearch_job.update(running=False, stage="done", result=self._RESULT)
        inner, running = web.render_aisearch_status(self.store, self.cfg, "2026-07-13")
        self.assertFalse(running)
        self.assertNotIn("data-aisearch-running", inner)        # 완료 → 마커 없음
        self.assertIn("class='aicards'", inner)
        self.assertIn("제목·발신 일치", inner)

    def test_aisearch_status_error_falls_back_to_normal(self):
        web._aisearch_job.update(running=False, stage="error",
                                 query="리포트", error="CLI 없음")
        inner, running = web.render_aisearch_status(self.store, self.cfg, "2026-07-13")
        self.assertFalse(running)
        self.assertIn("aifail", inner)                          # 폴백 배너
        self.assertIn("class='item'", inner)                    # 일반 결과로 폴백

    def test_ai_button_present_in_normal_search(self):
        html = web.render_search(self.store, self.cfg, {"q": ["리포트"]}, "2026-07-13")
        self.assertIn("class='aibtn'", html)
        self.assertIn("ai=1", html)

    def test_app_js_has_ai_wait_and_css(self):
        self.assertIn("aiwait", web._APP_JS)                    # 대기 표시 핸들러
        self.assertIn("aielapsed", web._APP_JS)                 # 경과 시간 카운터
        self.assertNotIn("수 초 걸립니다", web._APP_JS)          # 비현실적 문구 제거됨
        self.assertIn(".aicards", web._CSS)                     # 카드 스타일

    def test_app_js_has_ai_polling(self):
        # 방법 7·8: 백그라운드 잡 폴링 훅 + 마커
        self.assertIn("hookAiPolling", web._APP_JS)
        self.assertIn("/aisearch/status", web._APP_JS)
        self.assertIn("data-aisearch-running", web._APP_JS)
        self.assertIn(".aifill", web._CSS)                      # 진행 바 스타일

    def test_render_aisearch_shows_time(self):
        r = dict(self._RESULT, cost={"usd": 0.21, "in": 72000, "out": 900,
                                     "calls": 3, "seconds": 154.0})
        html = web.render_aisearch(r)
        self.assertIn("2.6분", html)                            # 154초 → 2.6분
        self.assertIn("$0.210", html)

    def test_settings_has_ai_search_backend(self):
        html = web.render_settings(self.store, self.cfg)
        self.assertIn("AI 검색 백엔드", html)
        self.assertIn("search_backend", html)

    def test_save_settings_persists_search_backend(self):
        from mailkb import config as cfgmod
        cfgmod.init_home(self.cfg.home)
        web._save_settings(self.cfg.home, {"search_backend": ["haiku"]})
        self.assertEqual(cfgmod.load(self.cfg.home).ai_search_backend, "haiku")


class TestNoiseCache(unittest.TestCase):
    """노이즈 스캔 캐시 — (설정+데이터) 지문 게이트: 결과 라이브와 동일, 스캔은 변경 시만."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          ignore_senders=["noreply"])   # 직접 구성 Config 는 기본 비어있음
        self.store.ingest([
            _recx("a1", "kim@corp.example", "실제 업무", "2026-07-01T09:00:00",
                  body="검토 요청드립니다"),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_config_change_invalidates(self):
        # 정상 발신자 → 노이즈 아님. 차단(설정 변경) 후 재조회 → 노이즈로 반영(재계산).
        from mailkb import config as cfgmod
        _, msg = web._noise_sets(self.store, self.cfg)
        self.assertEqual(len(msg), 0)
        cfgmod.add_blocked(self.cfg, "kim@corp.example")   # 설정 지문 변경
        thr2, msg2 = web._noise_sets(self.store, self.cfg)
        self.assertEqual(len(msg2), 1)      # 이제 노이즈 메시지
        self.assertEqual(len(thr2), 1)      # 스레드도 전부 노이즈

    def test_new_ingest_invalidates(self):
        # 새 수집(max_rowid 변경) → 재계산으로 새 노이즈 반영
        _, msg0 = web._noise_sets(self.store, self.cfg)
        self.store.ingest([_recx("n1", "noreply@x.example", "자동 알림",
                                 "2026-07-02T09:00:00", body="자동발송")])
        _, msg1 = web._noise_sets(self.store, self.cfg)
        self.assertEqual(len(msg1), len(msg0) + 1)   # noreply = ignore_senders 매치

    def test_new_ingest_classifies_only_delta(self):
        web._noise_sets(self.store, self.cfg)
        self.store.ingest([_recx("n2", "noreply@x.example", "증분 알림",
                                 "2026-07-02T10:00:00", body="자동발송")])
        with mock.patch.object(self.cfg, "is_noise", wraps=self.cfg.is_noise) as classify:
            _, msg = web._noise_sets(self.store, self.cfg)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(len(msg), 1)

    def test_cache_hit_skips_rescan(self):
        # 변경 없으면 스캔 안 함 — is_noise 를 폭탄으로 바꿔도 캐시 히트면 호출 안 됨
        web._noise_sets(self.store, self.cfg)          # 캐시 채움
        with mock.patch.object(self.cfg, "is_noise",
                               side_effect=AssertionError("재스캔 금지")):
            thr, msg = web._noise_sets(self.store, self.cfg)   # 히트 → is_noise 미호출
        self.assertEqual(len(msg), 0)

    def test_mail_and_threads_agree_with_live(self):
        # 리팩터가 라이브 판정과 동일한지 — 노이즈 1통 섞어 넣고 필터 확인
        self.store.ingest([_recx("s1", "noreply@x.example", "자동", "2026-07-03T09:00:00",
                                 body="자동발송")])
        html = web.render_mail(self.store, self.cfg)
        self.assertNotIn("자동", html)                 # 노이즈 메시지 제외됨
        self.assertIn("실제 업무", html)                # 실제 메일은 표시

    def test_signal_hide_unhide_live(self):
        # 액션 판정은 요청 시점 라이브 — 숨김/해제가 ↩ 집합·목록에 즉시 반영
        # ("확인 요청" 제목 = 요청 표지 + 직접 수신 → REQUIRED)
        self.store.ingest([_recx("w1", "boss@corp.example", "확인 요청",
                                 "2026-07-05T09:00:00", body="확인 부탁", to=[ME])])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE sender_addr='boss@corp.example'"
        ).fetchone()[0]
        _, aw, _, _ = web._action_state(self.store, self.cfg)
        self.assertIn(tid, aw)                         # 응답 대기 신호
        self.assertIn("확인 요청", web.render_mail(self.store, self.cfg, flt="awaiting"))
        self.store.hide_thread(tid, True)              # 데이터 불변, hidden 만 변경
        _, aw2, _, _ = web._action_state(self.store, self.cfg)
        self.assertNotIn(tid, aw2)                     # 숨김 → 판정 NONE (라이브)
        self.assertNotIn("확인 요청",
                         web.render_mail(self.store, self.cfg, flt="awaiting"))
        self.store.hide_thread(tid, False)             # 해제 → 다시 보임
        self.assertIn("확인 요청", web.render_mail(self.store, self.cfg, flt="awaiting"))


class TestFeatureGating(unittest.TestCase):
    """L1 문장 게이팅 — 확인된 오탐·미탐(2026-07-17 규칙 분석)의 정식 회귀.

    원칙: 요청·기한은 같은 문장에 완료/과거 문맥이 있으면 무효, 순서 무관
    (마지막 문장 우선이 아니라 문장별 게이팅 후 OR), 인사 관용구는 요청이 아님.
    """

    def _f(self, body, **kw):
        return classify_message(body, **kw)

    def test_pleasantries_are_not_requests(self):
        for s in ("참고 부탁드립니다.", "앞으로도 잘 부탁드립니다.",
                  "많은 관심 부탁드립니다.", "양해 부탁드립니다.", "참고 바랍니다."):
            self.assertEqual(self._f(s)["has_request"], 0, msg=s)

    def test_completed_and_historical_gated(self):
        f = self._f("검토 요청 건을 완료했습니다.")
        self.assertEqual((f["has_request"], f["has_decision"], f["has_completion"]),
                         (0, 0, 1))
        for s in ("승인 요청 드렸던 건 처리됐습니다.", "요청하신 자료 송부드립니다.",
                  "지난번 검토 부탁드린 건 회신 왔습니다.", "아래와 같이 처리했습니다."):
            self.assertEqual(self._f(s)["has_request"], 0, msg=s)
        self.assertEqual(self._f("승인 올리겠습니다.")["has_decision"], 0)
        # 완료 문장의 기한도 무효 — "금요일까지 완료했습니다"는 기한이 아니다
        self.assertEqual(self._f("금요일까지 완료했습니다.")["has_deadline"], 0)
        self.assertEqual(self._f("현재까지 진행중입니다.")["has_deadline"], 0)

    def test_strong_requests_detected(self):
        for s in ("검토 부탁드립니다.", "회신 부탁드립니다.", "의견 주세요.",
                  "확인해 주시겠어요?", "한번 봐주실 수 있을까요?",
                  "Please review and let me know.", "Could you confirm?"):
            self.assertEqual(self._f(s)["has_strong_request"], 1, msg=s)

    def test_weak_vs_strong_split(self):
        f = self._f("확인 부탁드립니다.")
        self.assertEqual((f["has_strong_request"], f["has_weak_request"]), (0, 1))
        f = self._f("검토 부탁드립니다.")
        self.assertEqual(f["has_strong_request"], 1)

    def test_fullwidth_question_detected(self):
        self.assertEqual(self._f("확인 가능한가요？")["has_question"], 1)

    def test_sentence_gating_is_order_free(self):
        # 재개: 완료 문장 뒤의 새 요청은 살아있다
        f = self._f("검토는 완료했습니다. 추가 항목은 내일까지 회신 부탁드립니다.")
        self.assertEqual((f["has_strong_request"], f["has_deadline"]), (1, 1))
        # 한국어 맺음말이 마지막이어도 앞 문장의 요청은 유지(마지막 문장 우선 아님)
        f = self._f("내일까지 회신 부탁드립니다. 감사합니다. 좋은 하루 되세요.")
        self.assertEqual(f["has_strong_request"], 1)

    def test_remind_revives_historical(self):
        f = self._f("지난번 요청드렸던 자료, 다시 한번 부탁드립니다.")
        self.assertEqual(f["has_request"], 1)

    def test_withdrawal(self):
        f = self._f("회신 불필요합니다. 참고만 하세요.")
        self.assertEqual((f["has_withdrawal"], f["has_request"]), (1, 0))
        self.assertEqual(self._f("해당 요청은 취소합니다.")["has_withdrawal"], 1)
        # "무시해 주세요"가 강한 요청('~해 주세요')으로 오인되면 철회가 재개로
        # 뒤집힌다 — 독립 코퍼스 평가가 잡은 구멍(오탐 26건의 원인)
        f = self._f("문제가 해결되어 기존 요청은 무시해 주세요. 감사합니다.")
        self.assertEqual((f["has_withdrawal"], f["has_request"]), (1, 0))

    def test_conditional_not_completion(self):
        # "문제/이상 없으면 ~해 주세요"는 조건부 요청 — 완료 게이트에 걸리면 안 됨
        f = self._f("문제 없으면 승인 의견을 회신 바랍니다.")
        self.assertEqual(f["has_strong_request"], 1)
        f = self._f("이상 없으면 진행해 주세요.")
        self.assertEqual(f["has_strong_request"], 1)
        # 서술형은 여전히 완료
        self.assertEqual(self._f("검토했고 이상 없습니다.")["has_completion"], 1)

    def test_hedged_request_is_weak(self):
        # "가능하면 ~해 주시면"은 완곡 — REQUIRED 근거(강한 요청)가 아니라 약한 요청
        f = self._f("가능하면 검토해 주시면 감사하겠습니다.")
        self.assertEqual((f["has_strong_request"], f["has_weak_request"]), (0, 1))

    def test_evidence_uses_same_sentence_gate(self):
        # 근거 문장 추출도 판정과 같은 게이트(sentence_gate) — 완료·과거 문장이
        # 근거로 표시되면 안 된다(리뷰 반영)
        body = "검토 요청 건을 완료했습니다.\n다른 안건은 언제 가능할까요?"
        self.assertEqual(actions.evidence_from_body(body),
                         "다른 안건은 언제 가능할까요?")

    def test_deadline_vocabulary(self):
        for s in ("금주 내로 검토 부탁드립니다.", "오늘 중으로 부탁드립니다.",
                  "3일 내로 회신 부탁드립니다.", "가능한 빨리 회신 주세요.",
                  "ASAP 처리 부탁드립니다.", "EOD까지 부탁드립니다."):
            self.assertEqual(self._f(s)["has_deadline"], 1, msg=s)
        self.assertEqual(self._f("작년 12월까지 담당했던 건입니다.")["has_deadline"], 0)

    def test_mentions_subject_trivial(self):
        f = self._f("김도현 수석님이 확인해 주시면 좋겠습니다.", names=["김도현"])
        self.assertEqual(f["mentions_me"], 1)
        f = self._f("각 담당자는 금일까지 회신 바랍니다.")
        self.assertEqual(f["mentions_group"], 1)
        self.assertEqual(
            self._f("본문", subject="[검토 요청] 설계안")["subject_has_request"], 1)
        self.assertEqual(
            self._f("본문", subject="주간 현황 공유")["subject_has_request"], 0)
        self.assertEqual(self._f("++김수석")["is_trivial"], 1)


class TestActionFold(unittest.TestCase):
    """L2 액션 상태기계 — 전이·역순 refold·무작위 drift·백필 등가."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME], ["김도현"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _r(self, mid, sender, to, subject, when, body, reply_to=""):
        return MailRecord(
            message_id=f"<{mid}@t>", subject=subject,
            sender_name=sender.split("@")[0], sender_addr=sender,
            to=to, sent_on=when, body_text=body,
            in_reply_to=f"<{reply_to}@t>" if reply_to else "",
            references=[f"<{reply_to}@t>"] if reply_to else [])

    def _action(self, tid):
        return dict(self.store.db.execute(
            "SELECT action_source_id, action_strength, action_kind, "
            "action_has_deadline, completion_after_action "
            "FROM thread_state WHERE thread_id=?", (tid,)).fetchone())

    def _tid(self, mid):
        return self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id=?",
            (f"<{mid}@t>",)).fetchone()[0]

    def test_lifecycle_open_trivial_resolve_reopen_complete_withdraw(self):
        st = self.store
        st.ingest([self._r("a1", "kim@corp.example", [ME], "검토건",
                           "2026-07-01T09:00:00", "내일까지 검토 부탁드립니다.")])
        tid = self._tid("a1")
        a = self._action(tid)
        self.assertGreater(a["action_source_id"], 0)
        self.assertEqual((a["action_strength"], a["action_has_deadline"]),
                         ("strong", 1))
        # 내 trivial 발신(++)은 해소가 아니다
        st.ingest([self._r("a2", ME, ["kim@corp.example"], "RE: 검토건",
                           "2026-07-01T10:00:00", "++박수석", reply_to="a1")])
        self.assertGreater(self._action(tid)["action_source_id"], 0)
        # 내 실질 회신 = 해소 (⏰ 함께 소멸 — 기한이 영구히 남던 문제의 수정)
        st.ingest([self._r("a3", ME, ["kim@corp.example"], "RE: 검토건",
                           "2026-07-01T11:00:00", "검토 의견 드립니다.", reply_to="a1")])
        a = self._action(tid)
        self.assertEqual((a["action_source_id"], a["action_has_deadline"]), (0, 0))
        # 새 수신 요청 → 재개
        st.ingest([self._r("a4", "kim@corp.example", [ME], "RE: 검토건",
                           "2026-07-02T09:00:00", "추가건도 검토 부탁드립니다.",
                           reply_to="a1")])
        self.assertGreater(self._action(tid)["action_source_id"], 0)
        # 상대의 완료 통보는 해소가 아니라 표시만(잘못 닫힘 = 조용히 놓친 공)
        st.ingest([self._r("a5", "kim@corp.example", [ME], "RE: 검토건",
                           "2026-07-02T10:00:00", "추가건은 저희가 처리했습니다.",
                           reply_to="a1")])
        a = self._action(tid)
        self.assertGreater(a["action_source_id"], 0)
        self.assertEqual(a["completion_after_action"], 1)
        # 명시적 철회만 상대 측에서 닫을 수 있다
        st.ingest([self._r("a6", "kim@corp.example", [ME], "RE: 검토건",
                           "2026-07-02T11:00:00", "회신 불필요합니다.",
                           reply_to="a1")])
        self.assertEqual(self._action(tid)["action_source_id"], 0)

    def test_weak_nag_keeps_strength_updates_source(self):
        st = self.store
        st.ingest([
            self._r("b1", "lee@corp.example", [ME], "승인건",
                    "2026-07-03T09:00:00", "승인 부탁드립니다."),
            self._r("b2", "lee@corp.example", [ME], "RE: 승인건",
                    "2026-07-03T10:00:00", "확인 부탁드립니다.", reply_to="b1"),
        ])
        a = self._action(self._tid("b1"))
        # 약한 재촉이 강도·decide 를 격하시키지 않고 source 만 최신으로
        self.assertEqual((a["action_strength"], a["action_kind"]),
                         ("strong", "decide"))
        src_mid = self.store.db.execute(
            "SELECT message_id FROM messages WHERE id=?",
            (a["action_source_id"],)).fetchone()[0]
        self.assertEqual(src_mid, "<b2@t>")

    def test_out_of_order_refold(self):
        st = self.store
        # 최신(내 회신)을 먼저, 과거(요청)를 나중에 — Outlook 지연 수집 시나리오
        st.ingest([self._r("c2", ME, ["kim@corp.example"], "역순건",
                           "2026-07-04T15:00:00", "답변드립니다.")])
        st.ingest([self._r("c1", "kim@corp.example", [ME], "역순건",
                           "2026-07-04T09:00:00", "의견 주세요.")])
        self.assertEqual(self._action(self._tid("c1"))["action_source_id"], 0)

    def test_random_order_matches_refold_and_backfill(self):
        import random
        random.seed(7)
        bodies = ["검토 부탁드립니다.", "확인했습니다. 이상 없습니다.", "참고 바랍니다.",
                  "회신 불필요합니다.", "금일까지 회신 부탁드립니다.", "가능할까요?",
                  "++김수석", "처리 완료했습니다.", "잘 부탁드립니다."]
        msgs = []
        for i in range(80):
            sender = ME if random.random() < 0.3 else f"p{i % 4}@corp.example"
            to = [ME] if sender != ME else ["p0@corp.example"]
            msgs.append(self._r(
                f"d{i}", sender, to, f"스레드{i % 6}",
                f"2026-06-{(i % 28) + 1:02d}T{9 + (i % 9):02d}:{i % 60:02d}:00",
                random.choice(bodies)))
        random.shuffle(msgs)
        for m in msgs:
            self.store.ingest([m])
        cols = ("action_source_id", "action_strength", "action_kind",
                "action_has_deadline", "completion_after_action")
        q = ("SELECT thread_id, " + ", ".join(cols) + " FROM thread_state")
        before = {r["thread_id"]: tuple(r[c] for c in cols)
                  for r in self.store.db.execute(q)}
        # 증분 ≡ 전체 재접기 (같은 fold_action — drift 0 이어야)
        for tid in before:
            self.store._refold_thread_actions(tid)
        self.store.db.commit()
        after = {r["thread_id"]: tuple(r[c] for c in cols)
                 for r in self.store.db.execute(q)}
        self.assertEqual(before, after)
        # 백필(버전 리셋 → 재오픈) ≡ 증분
        path = self.store.db_path
        self.store.db.execute("DELETE FROM sync_state WHERE key='derived_version'")
        self.store.db.commit()
        self.store.close()
        self.store = Store(path, [ME], ["김도현"])
        rebuilt = {r["thread_id"]: tuple(r[c] for c in cols)
                   for r in self.store.db.execute(q)}
        self.assertEqual(after, rebuilt)

    def test_hard_noise_does_not_touch_action_state(self):
        # 자동회신·시스템 알림이 열린 요청을 오염시키면 안 된다(리뷰 반영):
        # (a) source 탈취 → L3 hard_noise → 실제 요청이 조용히 소멸
        # (b) 시스템 '완료' 문구 → completion_after_action 강등
        cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                     my_names=["김도현"], ignore_senders=["noreply", "jira@"])
        path = Path(self.tmp.name) / "n.sqlite"
        st = Store(path, [ME], ["김도현"], noise=cfg)
        self.addCleanup(st.close)
        st.ingest([
            self._r("h1", "kim@corp.example", [ME], "설계 검토",
                    "2026-07-16T09:00:00", "내일까지 검토 부탁드립니다."),
            self._r("h2", "noreply@corp.example", [ME], "RE: 설계 검토",
                    "2026-07-16T09:01:00",
                    "자동 회신: 부재중입니다. 7월 20일까지 부재이며, "
                    "급한 건은 김대리에게 부탁드립니다.", reply_to="h1"),
            self._r("h3", "jira@corp.example", [ME], "RE: 설계 검토",
                    "2026-07-16T09:02:00", "[JIRA] 빌드가 완료됐습니다.",
                    reply_to="h1"),
        ])
        tid = st.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<h1@t>'").fetchone()[0]
        row = st.db.execute(
            "SELECT action_source_id, completion_after_action FROM thread_state "
            "WHERE thread_id=?", (tid,)).fetchone()
        src = st.db.execute("SELECT message_id FROM messages WHERE id=?",
                            (row["action_source_id"],)).fetchone()[0]
        self.assertEqual(src, "<h1@t>")                       # source 유지
        self.assertEqual(row["completion_after_action"], 0)   # 강등 없음
        from mailkb import actions as actions_mod
        self.assertEqual(actions_mod.evaluate_thread(st, cfg, tid).level,
                         "required")
        # 노이즈 설정 변경(차단 추가) → derived_version 변경 → 재접기 백필
        st.close()
        cfg2 = Config(home=Path(self.tmp.name), my_addresses=[ME],
                      my_names=["김도현"],
                      ignore_senders=["noreply", "jira@", "kim@corp.example"])
        st2 = Store(path, [ME], ["김도현"], noise=cfg2)
        self.addCleanup(st2.close)
        self.assertEqual(st2.db.execute(
            "SELECT action_source_id FROM thread_state WHERE thread_id=?",
            (tid,)).fetchone()[0], 0)     # 발신자 차단 → 그 요청도 사라짐

    def test_name_config_change_triggers_backfill(self):
        st = self.store
        st.ingest([self._r("n1", "kim@corp.example",
                           ["team@corp.example", ME, "x@corp.example",
                            "y@corp.example", "z@corp.example"],
                           "지목건", "2026-07-05T09:00:00",
                           "박부장님이 회신 부탁드립니다.")])
        mid = st.db.execute("SELECT id FROM messages").fetchone()[0]
        self.assertEqual(st.db.execute(
            "SELECT mentions_me FROM message_features WHERE message_id=?",
            (mid,)).fetchone()[0], 0)
        path = st.db_path
        st.close()
        # 이름 추가 → derived_version 변경 → 자동 백필로 mentions_me 갱신
        self.store = Store(path, [ME], ["김도현", "박부장"])
        self.assertEqual(self.store.db.execute(
            "SELECT mentions_me FROM message_features WHERE message_id=?",
            (mid,)).fetchone()[0], 1)


class TestActionLadder(unittest.TestCase):
    """L3 판정 사다리 + 홈·웹·상세 일치성 + ⏰ 해소."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME], ["김도현"])
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["김도현"], ignore_senders=["noreply"],
                          internal_domains=["corp.example"],
                          raw={"filters": {"external_allowlist": ["partner.example"]}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _scen(self, records):
        before = {r[0] for r in self.store.db.execute("SELECT id FROM threads")}
        self.store.ingest(records)
        new = [r[0] for r in self.store.db.execute("SELECT id FROM threads")
               if r[0] not in before]
        return actions.evaluate_thread(self.store, self.cfg, new[0]), new[0]

    def _r(self, mid, sender, to, subject, body, cc=None, when=None):
        return _recx(mid, sender, subject,
                     when or "2026-07-16T09:00:00", body=body, to=to, cc=cc)

    def test_ladder_required(self):
        a, _ = self._scen([self._r("r1", "kim@corp.example", [ME],
                                   "설계안", "내일까지 회신 부탁드립니다.")])
        self.assertEqual((a.level, "strong_direct" in a.reasons), ("required", True))
        a, _ = self._scen([self._r("r2", "kim@corp.example", [ME],
                                   "결재안", "승인 부탁드립니다.")])
        self.assertEqual((a.level, a.kind), ("required", "decide"))
        a, _ = self._scen([self._r("r3", "kim@corp.example", ["lee@corp.example"],
                                   "협의안", "김도현 수석님이 검토 부탁드립니다.",
                                   cc=[ME])])
        self.assertEqual((a.level, "strong_named" in a.reasons), ("required", True))
        a, _ = self._scen([self._r("r4", "kim@corp.example", [ME],
                                   "질의안", "이대로 진행해도 될까요?")])
        self.assertEqual((a.level, "question_direct" in a.reasons),
                         ("required", True))

    def test_ladder_maybe(self):
        a, _ = self._scen([self._r("m1", "kim@corp.example", [ME],
                                   "자료안", "확인 부탁드립니다.")])
        self.assertEqual((a.level, "weak_direct" in a.reasons), ("maybe", True))
        # 전원·담당자 지목 + 강한 요청은 그룹이라도 REQUIRED (지목 없는 그룹은 MAYBE)
        group = [ME] + [f"g{i}@corp.example" for i in range(9)]
        a, _ = self._scen([self._r("m2", "kim@corp.example", group,
                                   "공지안", "각 담당자는 금주 내로 회신 부탁드립니다.")])
        self.assertEqual((a.level, "group_call" in a.reasons), ("required", True))
        a, _ = self._scen([self._r("m2b", "kim@corp.example", group,
                                   "공지안2", "일정 회신 부탁드립니다.")])
        self.assertEqual((a.level, "group_to" in a.reasons), ("maybe", True))
        a, _ = self._scen([
            self._r("m3", "kim@corp.example", [ME], "처리안",
                    "처리 부탁드립니다.", when="2026-07-15T09:00:00"),
            self._r("m4", "kim@corp.example", [ME], "RE: 처리안",
                    "저희 쪽에서 반영했습니다.", when="2026-07-15T10:00:00"),
        ])
        self.assertEqual((a.level, "completion_after" in a.reasons), ("maybe", True))
        a, _ = self._scen([self._r("m5", "sales@outside.example", [ME],
                                   "제안안", "검토 부탁드립니다.")])
        self.assertEqual((a.level, "external" in a.reasons), ("maybe", True))

    def test_ladder_none_and_allowlist(self):
        a, _ = self._scen([self._r("n1", "kim@corp.example", [ME],
                                   "공유안", "세미나 자료 공유드립니다. 참고 바랍니다.")])
        self.assertEqual(a.level, "none")
        a, _ = self._scen([self._r("n2", "noreply@corp.example", [ME],
                                   "알림안", "회신 부탁드립니다.")])
        self.assertEqual((a.level, a.reasons), ("none", ["hard_noise"]))
        # 허용 목록의 외부 협력사는 정상 추적
        a, _ = self._scen([self._r("n3", "kim@partner.example", [ME],
                                   "일정안", "회신 부탁드립니다.")])
        self.assertEqual(a.level, "required")

    def test_home_web_detail_consistency(self):
        self.store.ingest([
            self._r("x1", "kim@corp.example", [ME], "일치성1",
                    "내일까지 회신 부탁드립니다."),
            self._r("x2", "lee@corp.example", [ME], "일치성2",
                    "확인 부탁드립니다."),
            self._r("x3", "park@corp.example", [ME], "일치성3",
                    "요청하신 검토 완료했습니다."),
        ])
        q, cand = review.intervention_queue(
            self.store, self.cfg, "2026-07-17", return_candidates=True)
        home_req = {it["thread_id"] for it in q
                    if it["category"] in ("decide", "respond")}
        home_maybe = {c["thread_id"] for c in cand}
        _, aw, may, _ = web._action_state(self.store, self.cfg)
        self.assertEqual(home_req, set(aw))          # 홈 == 웹 ↩ (정의상 동일)
        self.assertEqual(home_maybe, set(may))       # 확인 후보도 동일
        for tid in aw:                               # 상세 신호도 동일 판정
            d = web.format_detail(self.store, self.cfg, tid)
            self.assertEqual(d["act"].level, "required")

    def test_deadline_clears_after_my_reply(self):
        # ⏰ 가 영구히 남던 문제(deadline_count 누적)의 회귀 가드
        self.store.ingest([self._r("d1", "kim@corp.example", [ME],
                                   "기한건", "금요일까지 회신 부탁드립니다.",
                                   when="2026-07-15T09:00:00")])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<d1@t>'").fetchone()[0]
        _, _, _, dl = web._action_state(self.store, self.cfg)
        self.assertIn(tid, dl)
        self.store.ingest([MailRecord(
            message_id="<d2@t>", subject="RE: 기한건", sender_name="me",
            sender_addr=ME, to=["kim@corp.example"],
            sent_on="2026-07-15T10:00:00", body_text="회신드립니다. 확정했습니다.",
            references=["<d1@t>"])])
        _, _, _, dl2 = web._action_state(self.store, self.cfg)
        self.assertNotIn(tid, dl2)


class TestSignalDismiss(unittest.TestCase):
    """신호 수동 해제(상세 칩 ✕) — source 메시지 키 오버레이.

    파생 테이블이 아니라 백필에 살아남고, 새 요청이 오면(source 변경) 자동
    복귀한다. 숨김(스레드 전체)과 달리 이 요청 건만 끈다.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["김도현"])
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME], ["김도현"],
                           noise=self.cfg)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _r(self, mid, sender, subject, body, when, to=None):
        return _recx(mid, sender, subject, when, body=body, to=to or [ME])

    def _seed(self):
        self.store.ingest([self._r(
            "d1", "kim@corp.example", "기한요청건",
            "금요일까지 회신 부탁드립니다.", "2026-07-15T09:00:00")])
        return self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<d1@t>'"
        ).fetchone()[0]

    def test_dismiss_action_clears_everywhere_and_restores(self):
        tid = self._seed()
        self.assertTrue(self.store.dismiss_signal(tid, "action"))
        a = actions.evaluate_thread(self.store, self.cfg, tid)
        self.assertEqual((a.level, a.reasons), ("none", ["user_dismissed"]))
        _, aw, may, dl = web._action_state(self.store, self.cfg)
        self.assertNotIn(tid, aw | may | dl)         # ↩·확인 후보·⏰ 모두 소멸
        q = review.intervention_queue(self.store, self.cfg, "2026-07-16")
        self.assertNotIn(tid, {it["thread_id"] for it in q})   # 홈 큐도
        self.store.restore_signal(tid)
        self.assertEqual(
            actions.evaluate_thread(self.store, self.cfg, tid).level, "required")

    def test_dismiss_deadline_only(self):
        tid = self._seed()
        self.assertTrue(self.store.dismiss_signal(tid, "deadline"))
        a = actions.evaluate_thread(self.store, self.cfg, tid)
        self.assertEqual(a.level, "required")        # 회신 필요는 유지
        self.assertFalse(a.has_deadline)             # ⏰ 만 꺼짐
        self.assertTrue(a.deadline_dismissed)
        _, aw, _, dl = web._action_state(self.store, self.cfg)
        self.assertIn(tid, aw)
        self.assertNotIn(tid, dl)

    def test_new_request_revives_signal(self):
        tid = self._seed()
        self.store.dismiss_signal(tid, "action")
        # 같은 스레드에 새 요청 → source 가 바뀌어 오버레이 자동 무효
        self.store.ingest([self._r(
            "d2", "kim@corp.example", "RE: 기한요청건",
            "추가 건도 검토 부탁드립니다.", "2026-07-15T10:00:00")])
        self.assertEqual(
            actions.evaluate_thread(self.store, self.cfg, tid).level, "required")

    def test_dismiss_survives_backfill(self):
        tid = self._seed()
        self.store.dismiss_signal(tid, "action")
        path = self.store.db_path
        self.store.db.execute("DELETE FROM sync_state WHERE key='derived_version'")
        self.store.db.commit()
        self.store.close()
        self.store = Store(path, [ME], ["김도현"], noise=self.cfg)  # 백필 재구축
        self.assertEqual(
            actions.evaluate_thread(self.store, self.cfg, tid).level, "none")

    def test_dismissed_not_resurrected_as_stalled(self):
        # 해제한 요청 건이 3영업일 뒤 '멈춘 스레드'로 재등장하면 해제를 무시하는
        # 셈 — 정체 카테고리에서도 억제된다(새 요청이 오면 해제와 함께 복귀).
        self.store.ingest([
            self._r("s1", "kim@corp.example", "정체될건",
                    "검토 부탁드립니다.", "2026-07-06T09:00:00"),
            self._r("s2", "kim@corp.example", "RE: 정체될건",
                    "참고 자료 첨부합니다.", "2026-07-07T09:00:00"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<s1@t>'"
        ).fetchone()[0]
        self.store.dismiss_signal(tid, "action")
        q = review.intervention_queue(self.store, self.cfg, "2026-07-15")
        self.assertNotIn(tid, {it["thread_id"] for it in q})

    def test_no_open_action_returns_false(self):
        self.store.ingest([self._r(
            "f1", "kim@corp.example", "공유건", "자료 공유드립니다.",
            "2026-07-15T09:00:00")])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<f1@t>'"
        ).fetchone()[0]
        self.assertFalse(self.store.dismiss_signal(tid, "action"))
        self.assertFalse(self.store.dismiss_signal(tid, "unknown"))

    def test_detail_chips_and_post_routes(self):
        tid = self._seed()
        out = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("↩ 회신 필요", out)
        self.assertIn("⏰ 기한", out)
        self.assertIn(f"/thread/{tid}/signal-off", out)
        # POST 해제 → 흐린 안내 + 복원만
        loc = web.perform_action(self.store, self.cfg,
                                 f"/thread/{tid}/signal-off",
                                 {"kind": ["action"]})
        self.assertIn("신호 해제", urllib_unquote(loc))
        out2 = web.render_thread(self.store, self.cfg, tid)
        self.assertNotIn("↩ 회신 필요", out2)
        self.assertIn("신호 수동 해제됨", out2)
        self.assertIn(f"/thread/{tid}/signal-on", out2)
        web.perform_action(self.store, self.cfg,
                           f"/thread/{tid}/signal-on", {})
        self.assertIn("↩ 회신 필요",
                      web.render_thread(self.store, self.cfg, tid))


class TestNoisePolicy(unittest.TestCase):
    """제목 강한 노이즈의 앵커 매치 + 외부 허용 목록."""

    def _cfg(self, **kw):
        return Config(home=Path("."), my_addresses=[ME], **kw)

    def test_subject_strong_anchored(self):
        c = self._cfg()
        for s in ("[nflow] 결재 알림", "Meeting Invitation: 주간회의",
                  "[자동회신] 부재중입니다", "Notification: build failed",
                  "RE: invitation"):
            self.assertTrue(c.is_noise_subject_strong(s), msg=s)
        # 핵심 수정 — 일반 단어의 부분/시작 매치로 실무 제목을 죽이지 않는다
        for s in ("notification 설정 변경 검토 요청", "설계 검토 요청",
                  "Invitation to review"):
            self.assertFalse(c.is_noise_subject_strong(s), msg=s)

    def test_external_allowlist(self):
        c = self._cfg(internal_domains=["corp.example"], ignore_senders=["noreply"],
                      raw={"filters": {"external_allowlist":
                                       ["partner.example", "kim@vendor.example"]}})
        self.assertTrue(c.is_noise("spam@evil.example"))
        self.assertFalse(c.is_noise("lee@partner.example"))      # 도메인 허용
        self.assertFalse(c.is_noise("kim@vendor.example"))       # 주소 허용
        self.assertTrue(c.is_noise("other@vendor.example"))      # 주소 허용은 그 주소만
        self.assertTrue(c.is_noise_sender_hard("noreply@corp.example"))
        self.assertFalse(c.is_noise_sender_hard("spam@evil.example"))  # 외부는 policy


if __name__ == "__main__":
    unittest.main(verbosity=2)
