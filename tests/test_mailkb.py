"""핵심 로직 단위 테스트: 인용 제거, 스레딩, 미답변 판정."""

import argparse
import contextlib
import html as html_mod
import io
import json
from datetime import date, timedelta
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote as urllib_unquote

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailkb import actions, distill, notes, promises, report, review, terms, web
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
    quote_context,
    smart_truncate,
    strip_preserved,
)
from mailkb.config import Config
from mailkb.sources.base import MailRecord
from mailkb.store import DOSSIER_VALIDATOR_VERSION, Store

ME = "me@corp.example"


class TestSourceHygiene(unittest.TestCase):
    """정적 검사 — 실행돼야만 드러나는 부류를 미리 잡는다."""

    def test_cross_module_attribute_references_resolve(self):
        # weekly_mod.report_path 처럼 **없는 함수를 부르는 코드**는 그 줄이
        # 실행될 때만 터진다(실제로 겪었다: 인증 만료 경로에서만 도는 한 줄).
        # 모듈 간 참조를 AST 로 전수 대조해 배포 전에 잡는다.
        import ast
        import importlib
        root = Path(__file__).resolve().parent.parent / "mailkb"
        bad = []
        for f in sorted(root.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            alias = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level >= 1:
                    pkg = "mailkb" + ("" if f.parent == root
                                      else "." + f.parent.name)
                    base = f"{pkg}.{node.module}" if node.module else pkg
                    for a_ in node.names:
                        alias[a_.asname or a_.name] = f"{base}.{a_.name}"
            shadowed = {n.id for n in ast.walk(tree)
                        if isinstance(n, ast.Name)
                        and isinstance(n.ctx, ast.Store)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)):
                    continue
                name = node.value.id
                if name not in alias or name in shadowed:
                    continue
                try:
                    mod = importlib.import_module(alias[name])
                except Exception:
                    continue          # 모듈이 아니라 심볼을 임포트한 경우
                if not hasattr(mod, node.attr):
                    bad.append(f"{f.name}:{node.lineno} {name}.{node.attr}")
        self.assertEqual(bad, [], f"존재하지 않는 모듈 속성 참조: {bad}")


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

    def test_multilingual_outlook_header_blocks(self):
        # 해외 파트너 스레드 실사례(2026-07-31): 프랑스어 Outlook 라벨을 못
        # 알아봐 매 답장의 전체 체인이 '신규'로 남았다 — 요약 입력 반복 부풂.
        cases = {
            "fr": ("Merci pour votre retour.\n\n"
                   "De\u00a0: Jean Dupont <jd@partner.example>\n"
                   "Envoyé\u00a0: mercredi 22 juillet 2026 14:03\n"
                   "À\u00a0: Kim Dohyun\nObjet\u00a0: RE: NPX-200\n\n"
                   "l'ancienne proposition...", "Merci pour votre retour."),
            "de": ("Danke!\n\nVon: Hans <h@x.de>\nGesendet: Mittwoch\n"
                   "An: Kim\nBetreff: AW: Angebot\n\nAlte Nachricht...",
                   "Danke!"),
            "es": ("Gracias.\n\nDe: Ana <a@x.es>\nEnviado: miércoles\n"
                   "Para: Kim\nAsunto: RE: propuesta\n\ntexto anterior",
                   "Gracias."),
            "ja": ("承知しました。\n\n差出人： 田中 <t@x.jp>\n"
                   "送信日時： 2026年7月22日\n宛先： キム\n件名： RE: 見積\n\n"
                   "以前の内容", "承知しました。"),
        }
        for lang, (body, want) in cases.items():
            self.assertEqual(extract_new_content(body), want, msg=lang)

    def test_multilingual_attribution_and_labels(self):
        # Gmail 귀속줄(fr/de) + 대시 구분선형 프랑스어 라벨
        self.assertEqual(extract_new_content(
            "D'accord.\n\nLe mer. 22 juil. à 14:03, Jean <j@x.fr> a écrit :\n"
            "> ancien"), "D'accord.")
        self.assertEqual(extract_new_content(
            "OK.\n\nAm Mi., 22. Juli, schrieb Hans <h@x.de>:\nalt"), "OK.")
        self.assertEqual(extract_new_content(
            "OK.\n\n-----Message d'origine-----\nDe : Jean\n\nvieux"), "OK.")

    def test_ambiguous_single_letter_label_not_a_header(self):
        # 실측 오탐(2026-07-31): 홑글자 'A:'를 FOLLOW 로 넣었더니 "De: …/A: …"
        # 같은 정상 본문이 잘렸다. 소급 재절단이 저장값을 덮어쓰므로 오탐은
        # 데이터 손실 — 악센트 있는 À·Para 로 충분해 홑글자는 뺐다.
        body = ("파트너 미팅 메모.\nDe: 프랑스 지사 약어\nA: 담당자 미정\n"
                "다음 주 재논의.")
        self.assertEqual(extract_new_content(body), body)
        # 진짜 프랑스어·스페인어 헤더는 그대로 잘린다
        self.assertEqual(extract_new_content(
            "Merci.\n\nDe : Jean\nEnvoyé : lundi\nÀ : Kim\nObjet : RE\n\nvieux"),
            "Merci.")
        self.assertEqual(extract_new_content(
            "Gracias.\n\nDe: Ana\nEnviado: lunes\nPara: Kim\nAsunto: RE\n\nviejo"),
            "Gracias.")

    def test_real_client_html_quote_blocks_are_cut(self):
        # 실기기 구분선은 텍스트가 아니라 border-top div·<hr>·전용 컨테이너다 —
        # 텍스트 청크만 보던 HTML 경로가 실제 답장을 하나도 못 잘랐다(데모
        # 282통 중 67통 잔존, 2026-07-31 리뷰).
        hdr = ("<b>보낸 사람:</b> 김도현<br><b>보낸 날짜:</b> 2026-07-30<br>"
               "<b>받는 사람:</b> 박서준<br><b>제목:</b> RE: 견적")
        for name, html in [
            ("클래식", "<div><p>회신 본문입니다.</p>"
                      "<div style='border:none;border-top:solid #E1E1E1 1.0pt'>"
                      f"<p>{hdr}</p></div><p>이전 메일 본문</p></div>"),
            ("OWA", f"<div>새 내용입니다.</div><hr><div id='divRplyFwdMsg'>{hdr}"
                    "</div><div>이전 메일 본문</div>"),
            ("Mac", "<div>새 내용입니다.</div>"
                    "<div class='mail-editor-reference-message-container'>"
                    f"{hdr}<div>이전 메일 본문</div></div>"),
        ]:
            out = sanitize_html(html)
            self.assertNotIn("이전 메일 본문", out, msg=name)
        # 구분선 뒤 평범한 문장은 그대로 — 경계 신호만으로는 안 자른다
        self.assertIn("다음 주 일정", sanitize_html(
            "<div>보고드립니다.</div><hr><div>다음 주 일정은 아래와 같습니다.</div>"))

    def test_cut_leaves_no_orphan_separator(self):
        # 경계 태그(<hr>·테두리 div)는 인용 '앞'이라 뒤만 버리면 그대로 남는다
        # — 본문 끝에 빈 구분선이 그어져 "아래 뭔가 있는데 안 보인다"로 읽힌다.
        hdr = ("<b>보낸 사람:</b> 김도현<br><b>보낸 날짜:</b> 2026-07-30<br>"
               "<b>받는 사람:</b> 박서준<br><b>제목:</b> RE")
        for name, html in [
            ("hr", f"<div>새 내용입니다.</div><hr><div id='divRplyFwdMsg'>{hdr}"
                   "</div><div>이전 본문</div>"),
            ("border", "<div><p>회신입니다.</p>"
                       "<div style='border-top:solid #ccc 1pt'>"
                       f"<p>{hdr}</p><p>이전 본문</p></div></div>"),
        ]:
            out = sanitize_html(html).rstrip()
            self.assertNotIn("이전 본문", out, msg=name)
            self.assertNotRegex(out, r"<hr\s*/?>\s*(</\w+>\s*)*$", msg=name)
            self.assertNotRegex(
                out, r"<(div|p)[^>]*>\s*(<(div|p)[^>]*>\s*</\3>\s*)*</\1>\s*$",
                msg=name)
            self.assertIn("내용" if name == "hr" else "회신", out)

    def test_attribution_line_requires_address(self):
        # "누가 언제 썼다:" 문형은 평범한 서술문과 겹친다 — 주소 흔적이 있을
        # 때만 인용 시작으로 본다. 길이 상한도 mailto 확장(주소 두 배)을 견딘다.
        A = "dohyun.kim@nurisoft.co.kr"
        real = [
            html_to_markdown('<div>새 내용</div><div>On Thu, Jul 30, 2026 at '
                             f'2:15 PM Kim &lt;<a href="mailto:{A}">{A}</a>&gt; '
                             'wrote:</div><div>인용된 옛 내용</div>'),
            f"새 내용\n\n2026년 7월 30일 (목) 오후 2:15, 김도현 <{A}>님이 작성:\n> 옛",
            f"새 내용\n\n2026. 7. 30. 오후 2:15, 김도현 <{A}> 작성:\n> 옛",   # Apple
            f"새 내용\n\n2026-07-30 오후 2:15에 김도현 <{A}> 이(가) 쓴 글:\n> 옛",  # TB
        ]
        for body in real:
            self.assertEqual(extract_new_content(body).strip(), "새 내용",
                             msg=body[:40])
        for body, keep in [
            ("공유드립니다.\n2026년 7월 기준 자료는 김도현 님이 작성했습니다.\n"
             "검토 부탁드립니다.", "검토 부탁"),
            ("Please review.\nOn the attached spec page 3, the reviewer wrote:\n"
             "the timing is tight.", "timing is tight"),
        ]:
            self.assertIn(keep, extract_new_content(body), msg=body[:30])

    def test_document_headers_are_not_quotes(self):
        # 한국 공문 머리·물류 구간표·번역 대조표가 잘리던 것(2026-07-31 리뷰).
        # 진짜 답장 헤더는 날짜·수신·제목이 함께 오므로 필드 3개를 요구하고,
        # 발신인 줄에 주소가 있으면 2개로 낮춘다.
        for body, keep in [
            ("아래와 같이 안내드립니다.\n\n발신: 총무팀\n수신: 각 부서장\n"
             "참조: 인사팀\n제목: 하계 휴가\n\n8월 1일부터 시행합니다.", "8월 1일부터"),
            ("이번 선적 건 정보입니다.\nFrom: Busan Port\nTo: Shanghai\n"
             "Date: 2026-08-02\n확인 부탁드립니다.", "확인 부탁"),
            ("번역안입니다.\n**From:** 보낸 사람\n**To:** 받는 사람\n"
             "**Subject:** 제목\n의견 주세요.", "의견 주세요"),
        ]:
            self.assertIn(keep, extract_new_content(body), msg=body[:24])
        # 진짜 헤더(필드 3개 이상 + 앞이 빈 줄)는 그대로 절단. 주소가 있다고
        # 기준을 낮추지 않는다 — 계정 요청 양식·반송 로그가 잘렸다(2026-07-31).
        self.assertEqual(extract_new_content(
            "확인했습니다.\n\n보낸 사람: 김도현 <k@corp.example>\n"
            "보낸 날짜: 2026-07-30\n받는 사람: 박서준\n제목: RE\n\n이전"),
            "확인했습니다.")
        self.assertIn("접수 후", extract_new_content(
            "신규 계정 요청은 아래 양식대로 보내주세요.\n\n"
            "From: 본인 사내 메일 (예: hong@nurisoft.co.kr)\n"
            "To: helpdesk@nurisoft.co.kr\nSubject: [계정요청] 부서/이름\n\n"
            "접수 후 1영업일 내 처리됩니다."))

    def test_preserve_fold_keeps_tag_balance(self):
        # 폴드 진입에서 강제로 닫은 태그의 짝이 뒤늦게 도착하면 폴드 안에 짝
        # 없는 </div> 가 남고, 브라우저가 <details> 를 팝해 인용이 새어 나온다.
        from html.parser import HTMLParser
        void = {"br", "img", "hr", "input", "meta", "link", "wbr"}

        class Bal(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack, self.stray = [], []

            def handle_startendtag(self, tag, attrs):
                pass

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if tag in self.stack:
                    i = len(self.stack) - 1 - self.stack[::-1].index(tag)
                    del self.stack[i]
                else:
                    self.stray.append(tag)

        hdr = ("<b>보낸 사람:</b> 김<br><b>보낸 날짜:</b> 7/30<br>"
               "<b>받는 사람:</b> 박<br><b>제목:</b> RE")
        for name, html in [
            ("중첩", f"<div><p>새 내용</p><div><span>____________</span>{hdr}"
                    "<br>인용 본문</div></div>"),
            ("border", "<div><p>회신</p><div style='border-top:solid #ccc 1pt'>"
                       f"<p>{hdr}</p></div><p>옛 본문</p></div>"),
        ]:
            out = sanitize_html(html, preserve_quotes=True)
            p = Bal(); p.feed(out); p.close()
            self.assertEqual((p.stray, p.stack), ([], []), msg=f"{name}: {out}")
            self.assertIn("qfold", out)

    def test_bold_labels_from_outlook_html(self):
        # 실제 Outlook 은 헤더 라벨을 <b> 로 찍고 html_to_markdown 이 "**De :**"
        # 로 바꾼다 — 줄머리 앵커만으로는 게이트가 아예 발화하지 않았다
        # (2026-07-31 리뷰: 한국어 "**보낸 사람:**" 도 동일). 언어 무관 결함.
        self.assertEqual(extract_new_content(
            "Nouveau texte.\n\n**De :** Marie <m@p.fr>\n**Envoyé :** mercredi\n"
            "**À :** Kim\n**Objet :** RE\n\nvieux"), "Nouveau texte.")
        self.assertEqual(extract_new_content(
            "확인했습니다.\n\n**보낸 사람:** 김도현\n**보낸 날짜:** 2026-07-22\n"
            "**받는 사람:** 박서준\n**제목:** RE\n\n이전 내용"), "확인했습니다.")

    def test_client_field_orders_all_cut(self):
        # 클라이언트마다 필드 순서·라벨이 다르다(Mac Outlook 은 Date/Datum/Fecha).
        # 약한 FIRST 라도 헤더 필드가 2개 이상이면 진짜 헤더 블록이다.
        for name, body, want in [
            ("fr Mac", "Nouveau.\n\nDe: Marie\nDate: 22 juillet\nA: Kim\n"
                       "Objet: RE\n\nvieux", "Nouveau."),
            ("de Mac", "Neu.\n\nVon: Hans\nDatum: 22. Juli\nAn: Kim\n"
                       "Betreff: AW\n\nalt", "Neu."),
            ("it", "Nuovo.\n\nDa: Marco\nData: 22 luglio\nA: Kim\n"
                   "Oggetto: RE\n\nvecchio", "Nuovo."),
            ("nl", "Nieuw.\n\nVan: Jan\nDatum: 22 juli\nAan: Kim\n"
                   "Onderwerp: RE\n\noud", "Nieuw."),
            ("es", "Nuevo.\n\nDe: Ana\nFecha: 22 julio\nPara: Kim\n"
                   "Asunto: RE\n\nviejo", "Nuevo."),
        ]:
            self.assertEqual(extract_new_content(body), want, msg=name)
        # 라벨 사이에 빈 줄을 끼워 넣는 클라이언트도 잡는다(창은 빈 줄을 건너뛴다)
        self.assertEqual(extract_new_content(
            "Merci.\n\nDe :\n\nJean\n\nEnvoyé : lundi\nÀ : Kim\n"
            "Objet : RE\n\nvieux"), "Merci.")

    def test_weak_first_labels_need_unambiguous_follow(self):
        # 짧은 외래 FIRST(De/Von/Da/Van)는 헤더 필드가 **하나뿐**이면 헤더로
        # 치지 않는다 — 본문에 우연히 겹친 라벨 오탐이 실측됐다(2026-07-31).
        for body, keep in [
            ("회의록 공유드립니다.\nVan: 물류팀\nAn: 안건 정리\n다음 회의는 금요일.",
             "다음 회의"),
            ("운송 견적입니다.\nDa: 부산항\nData: 3.2톤\n확인 부탁드립니다.",
             "확인 부탁"),
            ("보고 드립니다.\nDe: 프랑스 지사\nA: 담당자 미정\n회신 주세요.",
             "회신 주세요"),
        ]:
            self.assertIn(keep, extract_new_content(body), msg=body[:20])
        # 강한 FIRST(보낸 사람·From)는 모호한 FOLLOW 와도 종전대로 헤더다
        self.assertEqual(extract_new_content(
            "확인했습니다.\n\n보낸 사람: 김\n날짜: 2026-07-22\nTo: 이\n"
            "제목: RE\n\n이전"), "확인했습니다.")
        # 약한 FIRST + 확실한 FOLLOW = 진짜 외국어 헤더 → 그대로 절단
        for body, want in [
            ("Merci.\n\nDe : Jean\nEnvoyé : lundi\nÀ : Kim\nObjet : RE\n\nvieux",
             "Merci."),
            ("Danke!\n\nVon: Hans\nGesendet: Mittwoch\nAn: Kim\nBetreff: AW\n\nalt",
             "Danke!"),
            ("Dank.\n\nVan: Jan\nVerzonden: maandag\nAan: Kim\nOnderwerp: RE\n\noud",
             "Dank."),
            ("Grazie.\n\nDa: Marco\nInviato: lunedì\nA: Kim\nOggetto: RE\n\nvecchio",
             "Grazie."),
        ]:
            self.assertEqual(extract_new_content(body), want, msg=body[:18])

    def test_html_path_ignores_weak_first_labels(self):
        # HTML 경로엔 2줄 FOLLOW 게이트가 없다(청크 상태기계). 저장된 HTML 은
        # 재절단·백업 대상이 아니라 오탐이 곧 영구 손실이라 약한 라벨은 뺀다.
        kept = sanitize_html("<p>견적 안내드립니다.</p><p>--------</p>"
                             "<p>Da: 부산항</p><p>운임은 별도입니다.</p>")
        self.assertIn("운임은 별도", kept)
        # 라벨 한 줄로는 자르지 않는다 — 저장 HTML 은 백업이 없어 오탐이 영구
        # 손실이다(공문 "발신:" 한 줄에 본문 전량 폐기 실증, 2026-07-31)
        one = sanitize_html("<div>안내드립니다.</div><div>________</div>"
                            "<div><b>보낸 사람:</b> 총무팀</div>"
                            "<div>8월 1일부터 시행합니다.</div>")
        self.assertIn("8월 1일부터", one)
        # 헤더 필드가 이어지면 진짜 인용 블록 — 자른다
        cut = sanitize_html("<p>안녕하세요.</p><p>________</p>"
                            "<p>보낸 사람: 김도현</p><p>보낸 날짜: 2026-07-30</p>"
                            "<p>받는 사람: 박서준</p><p>제목: RE</p><p>이전 내용</p>")
        self.assertNotIn("이전 내용", cut)

    def test_short_labels_need_follow_gate(self):
        # De/Da/A 같은 짧은 라벨이 FIRST 단독으로 본문을 자르면 안 된다 —
        # 2줄 내 FOLLOW 필드가 있어야 헤더 블록이다.
        body = "안건 정리\nA: 예산 확정\nB: 일정 조정\nDate: 미정입니다"
        self.assertEqual(extract_new_content(body), body)
        body2 = "Da capo 부탁.\nDa: 반복 기호입니다\n좋습니다\n계속 진행하죠"
        self.assertEqual(extract_new_content(body2), body2)

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


class TestSyncWarnsWithoutMyAddresses(unittest.TestCase):
    """my_addresses 가 비면 수집 시점의 발신 판정이 전부 실패한다 (2026-08-04).

    README 대로 따라 한 사람이 회고가 텅 빈 것을 보고 "안 되는 도구"로 판단하던
    구멍이다 — 실측: 그 값 없이 fake 를 수집하면 is_sent=1 이 0통, 내 약속 0건,
    미답변 '없음'. 값을 넣고 다시 수집하면 58통·6건·46건이 된다. **나중에 채워도
    안 살아난다**(색인할 때 쓰는 값이라 재수집이 필요하다)."""

    def _sync(self, home: Path, addrs):
        from mailkb import cli
        cfg_p = home / "config.toml"
        t = cfg_p.read_text(encoding="utf-8")
        if addrs:
            t = t.replace("my_addresses = []", f"my_addresses = {addrs!r}")
        cfg_p.write_text(t, encoding="utf-8")
        args = argparse.Namespace(home=str(home), source="fake", full=True,
                                  since=None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_sync(args)
        return err.getvalue()

    def test_warns_only_when_empty_and_sent_detection_depends_on_it(self):
        from mailkb import config as cfgmod
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "a"
            cfgmod.init_home(home)
            self.assertIn("my_addresses 가 비어 있어", self._sync(home, None))
            n = sqlite3.connect(home / "db.sqlite").execute(
                "SELECT COUNT(*) FROM messages WHERE is_sent=1").fetchone()[0]
            self.assertEqual(n, 0)          # 경고가 가리키는 실제 결과

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "b"
            cfgmod.init_home(home)
            out = self._sync(home, ["dohyun.kim@nurisoft.co.kr",
                                    "dhkim@nurisoft.co.kr"])
            self.assertNotIn("my_addresses 가 비어 있어", out)   # 조용해야 한다
            n = sqlite3.connect(home / "db.sqlite").execute(
                "SELECT COUNT(*) FROM messages WHERE is_sent=1").fetchone()[0]
            self.assertGreater(n, 0)


class TestQuoteContext(unittest.TestCase):
    """인용의 원문 앞뒤 문맥 — 조각만으로는 조건·전제가 안 보여 사용자가 메일을
    다시 열게 된다는 지적(2026-08-03)의 사양. 모델이 아니라 **코드가 원문을
    복사**하므로 환각 위험이 0이다."""

    BODY = ("도현님, 김민수입니다.\n\n"
            "INT8 PTQ 정확도를 검토했습니다. mAP 가 3.2%p 하락했고, 소형 객체에서\n"
            "낙폭이 컸습니다. 양산 기준에 못 미쳐 그대로는 갈 수 없습니다.\n\n"
            "고객 재학습 파이프라인 보유가 확인돼, 검토 결과 양자화는 QAT 로 확정합니다.\n"
            "다만 고객 데이터가 8/5 까지 오지 않으면 폴백하겠습니다.")

    def test_expands_both_directions_to_sentence_bounds(self):
        pre, q, post = quote_context(self.BODY, "검토 결과 양자화는 QAT 로 확정합니다")
        self.assertTrue(pre and post, (pre, post))
        # 앞은 pad 안의 **가장 이른** 경계부터 — 가까운 경계로 자르면 몇 자만 는다
        self.assertIn("고객 재학습 파이프라인", pre)
        # 뒤는 조건절까지 — 이것이 빠지면 메일을 다시 열게 된다
        self.assertIn("8/5", post)

    def test_absorbs_trailing_punctuation_into_the_quote(self):
        _, q, post = quote_context(self.BODY, "검토 결과 양자화는 QAT 로 확정합니다")
        self.assertTrue(q.endswith("."), q)      # 「…합니다」 뒤 마침표만 따로 뜨지 않게
        self.assertFalse(post.startswith("."), post)

    def test_whitespace_differences_still_locate(self):
        # 모델은 줄바꿈을 흘린다 — 대조는 공백을 무시하므로 위치는 찾혀야 한다
        got = quote_context(self.BODY, "mAP 가\n\n  3.2%p   하락했고")
        self.assertIsNotNone(got)
        self.assertIn("3.2%p 하락했고", got[1])

    def test_missing_quote_returns_none(self):
        self.assertIsNone(quote_context(self.BODY, "본문에 없는 문장입니다"))
        self.assertIsNone(quote_context("", "무엇이든"))
        self.assertIsNone(quote_context(self.BODY, ""))

    def test_pad_zero_gives_quote_only(self):
        pre, q, post = quote_context(self.BODY, "mAP 가 3.2%p 하락했고", pad=0)
        self.assertEqual((pre, post), ("", ""))
        self.assertIn("3.2%p", q)

    def test_boundaries_of_the_body(self):
        pre, _, _ = quote_context(self.BODY, "도현님, 김민수입니다")
        self.assertEqual(pre, "")                # 맨 앞이면 앞 문맥이 없다
        _, _, post = quote_context(self.BODY, "폴백하겠습니다")
        self.assertEqual(post, "")               # 맨 뒤면 뒤 문맥이 없다


class TestSmartTruncate(unittest.TestCase):
    """프롬프트용 절단 — **앞뒤를 나눠 담고** 표를 반쪽으로 자르지 않는다.

    두 구멍의 사양이다. ① 표: 맹목 슬라이스는 남은 행 표시 없이 '온전해 보이는
    반쪽 표'를 만들어 모델이 잘린 견적표를 전체로 믿는다(2026-08-02).
    ② 꼬리: 업무 메일은 결론·요청이 끝에 오는데 앞에서만 자르면 그게 날아간다 —
    통당 2,200자 16통 실측에서 결론 생존 0통이었다(2026-08-03)."""

    TABLE = ("| 항목 | 수량 | 단가 |\n| --- | --- | --- |\n"
             + "\n".join(f"| NPX-{i} | {i*100} | {i*1000} |" for i in range(1, 8)))
    PROSE = "앞머리 설명입니다. " * 5
    CONCL = "\n결론적으로 A안으로 확정합니다. 회신 부탁드립니다."

    def test_within_limit_returns_verbatim(self):
        text = self.PROSE + "\n\n" + self.TABLE
        self.assertEqual(smart_truncate(text, 10_000), text)

    def test_tail_survives_so_the_conclusion_does(self):
        # 이 함수의 존재 이유 — 앞에서만 자르면 결론이 통째로 사라진다.
        text = self.PROSE * 8 + self.CONCL
        out = smart_truncate(text, 200)
        self.assertLessEqual(len(out), 200)
        self.assertIn("결론적으로 A안으로 확정합니다", out)
        self.assertTrue(out.startswith("앞머리 설명"))      # 머리도 남는다

    def test_omission_is_announced(self):
        # 표시가 없으면 모델이 조각을 전문으로 믿는다(종전 산문 절단의 결함)
        text = self.PROSE * 8 + self.CONCL
        out = smart_truncate(text, 200)
        m = re.search(r"\n…\(중략 — ([\d,]+)자\)…\n", out)
        self.assertIsNotNone(m, out)
        gap = int(m.group(1).replace(",", ""))
        # 적힌 생략 자수 = 원문 − (실제로 실린 머리+꼬리). 어림수가 아니라 정확값이다
        self.assertEqual(gap, len(text) - (len(out) - len(m.group(0))))

    def test_table_rows_are_never_halved(self):
        text = self.PROSE + "\n\n" + self.TABLE
        for limit in (60, 90, 120, 200, 300):
            out = smart_truncate(text, limit)
            for line in out.splitlines():
                if line.startswith("|"):
                    self.assertTrue(line.endswith("|"), f"{limit}: {line}")

    def test_table_tail_keeps_the_last_rows(self):
        # 표가 본문 끝에 있으면 꼬리 쪽에 **완결 행**으로 남아야 한다
        out = smart_truncate(self.PROSE + "\n\n" + self.TABLE, 250)
        self.assertIn("| NPX-7 | 700 | 7000 |", out)

    def test_head_only_table_marker_when_no_room_for_split(self):
        # 앞뒤로 나눌 여지가 없으면(경계 보정으로 겹침) 머리만 남기고 표를 알린다
        header = "| " + " | ".join("아주긴열이름" * 10 for _ in range(5)) + " |"
        text = "서두\n\n" + header + "\n| --- | --- | --- | --- | --- |\n| a | b | c |"
        out = smart_truncate(text, 60)
        self.assertLessEqual(len(out), 60)
        self.assertNotIn("아주긴열이름", out)   # 반쪽 머리행을 남기지 않는다

    def test_escaped_pipe_cell_is_one_row(self):
        # 셀 내 파이프는 변환기가 \| 로 이스케이프 — 행 판정이 흔들리면 안 됨
        esc = "| a\\|b | c |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |"
        out = smart_truncate("x\n" + esc, 40)
        self.assertLessEqual(len(out), 40)
        for line in out.splitlines():
            if line.startswith("|"):
                self.assertTrue(line.endswith("|"), line)

    def test_result_never_exceeds_limit(self):
        text = self.PROSE + "\n\n" + self.TABLE + "\n\n뒷말." * 30
        for limit in (40, 80, 120, 200, 350, len(text) - 1):
            self.assertLessEqual(len(smart_truncate(text, limit)), limit,
                                 f"limit={limit}")

    def test_zero_and_huge_limits(self):
        text = self.PROSE * 3
        self.assertEqual(smart_truncate(text, len(text)), text)   # 딱 맞으면 원문
        self.assertLessEqual(len(smart_truncate(text, 1)), 1)


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

    def test_layout_table_signals_survive_for_css(self):
        # 화면은 CSS 로 조판용 표의 테두리를 뺀다(web._CSS). 그 선택자가 보는
        # 신호가 정제 후에도 남아 있어야 한다 — 여기가 정제기와 CSS 사이의
        # 유일한 계약이다. _ATTR_ALLOW 에서 cellpadding 을 빼면 화면이 조용히
        # 되돌아가는데, 이 테스트만 그걸 잡는다.
        from mailkb.sources.fake import _RICH_HTML
        out = sanitize_html(_RICH_HTML["conf6"])
        self.assertIn('cellpadding="0"', out)
        self.assertIn('cellspacing="0"', out)
        self.assertEqual(out.count('role="presentation"'), 2)   # 레이아웃 표 둘
        self.assertIn('cellpadding="4"', out)                   # 데이터 표는 그대로
        # 임의 ARIA role 은 통과시키지 않는다(우리 접근성 트리 보호)
        self.assertNotIn("role=", sanitize_html(
            '<table role="grid"><tr><td>A</td></tr></table>'))

    def test_layout_table_cells_do_not_include_nested_data_table(self):
        # CSS 자식 결합자(`table[...] > * > tr > td`)의 의미를 파이썬으로 재현해
        # 중첩을 검증한다 — 레이아웃 표 **안에 든** 데이터 표의 셀은 테두리를
        # 지켜야 한다(실측 구조: zeros > td > zeros > td > border="1").
        from html.parser import HTMLParser
        from mailkb.sources.fake import _RICH_HTML

        class DirectCells(HTMLParser):
            """셀마다 '직속 표가 조판용인가'를 기록한다."""

            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack, self.layout_cells, self.data_cells = [], 0, 0

            def handle_starttag(self, tag, attrs):
                a = {k: (v or "") for k, v in attrs}
                if tag == "table":
                    self.stack.append(
                        a.get("role") == "presentation"
                        or (a.get("cellpadding") == "0"
                            and a.get("cellspacing") == "0"
                            and a.get("border", "0") in ("", "0")))
                elif tag in ("td", "th") and self.stack:
                    if self.stack[-1]:
                        self.layout_cells += 1
                    else:
                        self.data_cells += 1

            def handle_endtag(self, tag):
                if tag == "table" and self.stack:
                    self.stack.pop()

        p = DirectCells()
        p.feed(sanitize_html(_RICH_HTML["conf6"]))
        p.close()
        self.assertEqual(p.layout_cells, 5)     # 테두리를 뺄 셀
        self.assertEqual(p.data_cells, 6)       # 테두리를 지킬 셀(2 th + 4 td)
        self.assertEqual(p.stack, [])           # 표 균형

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


class TestReportDone(unittest.TestCase):
    """리포트 '처리함' — 메일 밖(회의·구두)에서 처리한 것을 접는다.

    사용자 확정(2026-08-01): "한번 처리하면 다음 판단(다음날?)에는 잡히지 않아야
    해". 그래서 렌더가 아니라 **결정론 단계**에서 뺀다. 되돌릴 수 있어야 접힌
    것이 '사라진 것'과 구별된다."""

    def setUp(self):
        from mailkb import web
        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lambda: shutil.rmtree(self.tmp.name, ignore_errors=True))
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"])
        self.store = Store(self.home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(self.store.close)
        self.store.ingest([
            _rec("d0", "kim@corp.example", [ME], "협상", "2026-07-20T09:00:00",
                 body="검토 부탁드립니다."),
            _rec("d1", ME, [ME], "협상", "2026-07-21T09:00:00",
                 body="내일 오전에 정리해서 보내겠습니다.", reply_to="d0"),
        ])

    def _daily_html(self, day="2026-07-22"):
        det = review.deterministic(self.store, self.cfg, day)
        d = self.cfg.vault / "daily"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{day}.md").write_text(review.render(det), encoding="utf-8")
        return det, self.web.render_daily(self.cfg, day, day, self.store)

    def _first_button(self, html):
        m = re.search(r"name='kind' value='(\w+)'>.*?name='key' value='(\w+)'>"
                      r".*?name='tid' value='(\d+)'>.*?name='label' value='([^']*)'",
                      html, re.S)
        self.assertIsNotNone(m, "처리함 버튼이 없다")
        return dict(zip(("kind", "key", "tid", "label"), m.groups()))

    def test_report_item_carries_a_done_button(self):
        det, html = self._daily_html()
        self.assertEqual(len(det["promises"]), 1)
        self.assertIn("action='/report/done'", html)
        got = self._first_button(html)
        self.assertEqual(got["kind"], "promise")
        self.assertEqual(got["key"], det["promises"][0]["key"])
        self.assertIn("협상", got["label"])           # 되돌리기 목록에 남길 이름

    def test_marker_in_a_mail_subject_cannot_forge_a_button(self):
        # 2026-08-01 적대 검토: _md_inline 이 esc 를 지난 형태로 표식을 잡아서,
        # 제목에 <!--done:…--> 를 심으면 이스케이프가 방어가 되지 않았다. 가짜
        # 버튼을 누르면 줄만 사라지고 항목은 안 접혀 의미가 정확히 뒤집혔다.
        fake, real = "0123456789abcdef0123", "ffffffffffffffffffff"
        line = (f"- [#7] 협상 <!--done:promise:{fake}--> — 1일 전"
                + review.done_mark("promise", real))
        html = self.web._md_inline(line, "/records?tab=daily")
        self.assertEqual(html.count("action='/report/done'"), 1)
        self.assertIn(f"value='{real}'", html)
        self.assertNotIn(fake, html)                  # 글자로도 안 남는다
        self.assertNotIn("done:promise", html.replace("/report/done", ""))
        # 스킵 판정도 줄 끝의 진짜 표식을 본다
        m = self.web._DONE_TAIL_RX.search(line)
        self.assertEqual(m.group(2), real)
        # 접으면 그 줄이 실제로 사라진다(주입분이 판정을 가로채지 않는다)
        md = "## 내 약속\n" + line + "\n"
        self.assertIn("협상", self.web._md_to_html(md, "", set()))
        self.assertNotIn("협상", self.web._md_to_html(md, "", {f"promise:{real}"}))

    def test_marker_never_reaches_the_screen_as_text(self):
        # 표식은 마크다운에선 HTML 주석(다른 뷰어에서 안 보임), 웹에선 버튼이다.
        # 이스케이프된 채로 새면 화면에 `<!--done:...-->` 가 그대로 찍힌다.
        det, html = self._daily_html()
        md = (self.cfg.vault / "daily" / "2026-07-22.md").read_text(encoding="utf-8")
        self.assertIn("<!--done:promise:", md)
        self.assertNotIn("done:promise", html.replace("/report/done", ""))
        self.assertNotIn("&lt;!--", html)

    def test_dismissed_item_is_gone_from_the_next_judgement(self):
        det, html = self._daily_html()
        got = self._first_button(html)
        loc = self.web.perform_action(
            self.store, self.cfg, "/report/done",
            {"kind": [got["kind"]], "key": [got["key"]], "tid": [got["tid"]],
             "label": [got["label"]], "back": ["/records?tab=daily&date=2026-07-22"]})
        self.assertEqual(loc, "/records?tab=daily&date=2026-07-22")
        for day in ("2026-07-22", "2026-07-23", "2026-07-28"):
            self.assertEqual(review.deterministic(self.store, self.cfg, day)["promises"],
                             [], msg=day)

    def test_undo_brings_it_back_and_the_fold_lists_it(self):
        det, html = self._daily_html()
        got = self._first_button(html)
        self.web.perform_action(self.store, self.cfg, "/report/done",
                                {"kind": [got["kind"]], "key": [got["key"]],
                                 "tid": [got["tid"]], "label": [got["label"]],
                                 "back": ["/records?tab=daily"]})
        _, html2 = self._daily_html()
        self.assertIn("처리함으로 접은 항목 (1)", html2)
        self.assertIn("action='/report/undo'", html2)
        self.assertIn("협상", html2)                  # 무엇을 접었는지 보인다
        self.web.perform_action(self.store, self.cfg, "/report/undo",
                                {"kind": [got["kind"]], "key": [got["key"]],
                                 "back": ["/records?tab=daily"]})
        self.assertEqual(
            len(review.deterministic(self.store, self.cfg, "2026-07-22")["promises"]), 1)

    def test_dismissed_item_vanishes_from_the_already_saved_report(self):
        # 저장된 리포트 파일은 다시 안 만들어진다 — 화면에서 빼지 않으면 버튼을
        # 눌러도 그 자리에 그대로 남아 아무 일도 안 일어난 것처럼 보인다.
        # 인용 줄(연속 줄)까지 같이 빠져야 항목이 통째로 사라진다.
        det, html_before = self._daily_html()
        quote = det["promises"][0]["quote"]
        self.assertIn(quote, html_before)
        got = self._first_button(html_before)
        self.web.perform_action(self.store, self.cfg, "/report/done",
                                {"kind": [got["kind"]], "key": [got["key"]],
                                 "tid": [got["tid"]], "label": [got["label"]],
                                 "back": ["/records?tab=daily"]})
        # 파일을 다시 만들지 않고 같은 파일을 그대로 렌더한다
        after = self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22", self.store)
        self.assertIn(quote, (self.cfg.vault / "daily" / "2026-07-22.md")
                      .read_text(encoding="utf-8"))          # 파일은 그대로
        self.assertNotIn(quote, after)                        # 화면에서만 빠진다
        self.assertEqual(after.count("<ul>"), after.count("</ul>"))
        self.assertEqual(after.count("<li>"), after.count("</li>"))
        self.web.perform_action(self.store, self.cfg, "/report/undo",
                                {"kind": [got["kind"]], "key": [got["key"]],
                                 "back": ["/records?tab=daily"]})
        back = self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22", self.store)
        self.assertEqual(back, html_before)                   # 되돌리면 원래 화면

    _COUNT_MD = (
        "# 2026-08-01 일간 회고\n\n"
        "## 내 약속 — 후속이 없는 것 (5건)\n"
        "- [#1] A — 1일 전<!--done:promise:aaaaaaaaaaaa-->\n"
        "  「가」\n"
        "- [#2] B — 2일 전<!--done:promise:bbbbbbbbbbbb-->\n"
        "  「나」\n"
        "- … 외 3건\n\n"
        "## 참고\n"
        "- 내가 보낸 것 (1건)\n"
        "  - 10:51 회의록\n"
        "- 오래 멈춘 스레드 (2건)\n"
        "  - [#9] X — 영업 5d<!--done:stalled:eeeeeeeeeeee-->\n"
        "  - [#10] Y — 영업 4d<!--done:stalled:ffffffffffff-->\n")

    def _balanced(self, html):
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))
        self.assertEqual(html.count("<li>"), html.count("</li>"))

    def test_counts_are_recomputed_after_hiding(self):
        # 사용자 확정(2026-08-01): 접힌 항목을 숨기면 개수도 다시 센다.
        # 저장된 파일은 안 고치므로 화면에서 세야 한다.
        h = self.web._md_to_html(self._COUNT_MD, "", {"promise:aaaaaaaaaaaa"})
        self.assertIn("<h2>내 약속 — 후속이 없는 것 (4건)</h2>", h)
        self.assertNotIn("「가」", h)
        self.assertIn("「나」", h)
        self.assertIn("… 외 3건", h)          # 상한 밖 건수는 그대로다
        self._balanced(h)
        # 중첩(참고 › 오래 멈춘 스레드)도 같은 규칙
        h2 = self.web._md_to_html(self._COUNT_MD, "", {"stalled:eeeeeeeeeeee"})
        self.assertIn("오래 멈춘 스레드 (1건)", h2)
        self._balanced(h2)

    def test_section_disappears_when_every_item_is_hidden(self):
        h = self.web._md_to_html(
            self._COUNT_MD, "", {"promise:aaaaaaaaaaaa", "promise:bbbbbbbbbbbb"})
        self.assertNotIn("내 약속", h)         # 머리도 '외 N건' 꼬리도 남지 않는다
        self.assertNotIn("외 3건", h)
        self.assertIn("오래 멈춘 스레드 (2건)", h)   # 다른 절은 그대로
        self._balanced(h)
        # 중첩 부모도 자식이 다 접히면 사라진다
        h2 = self.web._md_to_html(
            self._COUNT_MD, "", {"stalled:eeeeeeeeeeee", "stalled:ffffffffffff"})
        self.assertNotIn("오래 멈춘 스레드", h2)
        self.assertIn("내가 보낸 것 (1건)", h2)      # 형제 절은 남는다
        self._balanced(h2)

    def test_report_sections_become_cards_only_in_bento(self):
        # 레이아웃 변경은 '이미 있는 것을 다시 배치'까지만 — 데이터도 계산도
        # 그대로고 감싸는 방식만 바뀐다(2026-08-01 사용자 확정).
        md = ("# 2026-08-01 일간 회고\n\n수신 3\n\n"
              "## Executive Summary\n- 한 줄\n\n"
              "## 내 약속 — 후속이 없는 것 (1건)\n- [#1] 건\n\n"
              "## AI 회고 분석\n\n오늘은 조용했습니다.\n\n"
              "## 참고\n- 수신 3건 처리됨\n\n---\n\n조사 범위: …\n")
        plain, card = self.web._md_to_html(md), self.web._md_to_html(md, cards=True)
        self.assertNotIn("rcard", plain)                 # 클래식은 지금 그대로
        self.assertEqual(plain, self.web._md_to_html(md, cards=False))
        self.assertEqual(card.count("<section class='rcard'>"), 3)   # 참고·꼬리말 제외
        self.assertEqual(card.count("<section"), card.count("</section>"))
        self._balanced(card)
        # '참고' 접힘은 그 자체가 카드 — 이중으로 감싸지 않는다
        self.assertNotIn("<section class='rcard'><details", card.replace("\n", ""))
        # 꼬리말(조사 범위)은 카드 밖이다
        self.assertLess(card.index("<hr>"), card.index("조사 범위"))
        self.assertNotIn("</section>", card[card.index("<hr>"):])

    def test_cards_follow_the_skin_setting(self):
        det, _ = self._daily_html()
        self.assertNotIn("rcard",
                         self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22"))
        self.cfg.raw = {"web": {"skin": "bento"}}
        self.assertIn("rcard",
                      self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22"))
        self.cfg.raw = {"web": {"skin": "classic"}}
        self.assertNotIn("rcard",
                         self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22"))

    def test_reports_without_markers_are_untouched(self):
        # 개편 이전 저장분 — done 집합이 있어도 결과가 같아야 한다
        old = "# d\n\n## 오늘 델타 (2건)\n- 하나\n- 둘\n"
        self.assertEqual(self.web._md_to_html(old, "", {"promise:aaaaaaaaaaaa"}),
                         self.web._md_to_html(old, "", set()))

    def test_worth_reporting_drops_light_chatter_only(self):
        # 회식·사무용품 같은 가벼운 건을 빼되, 내 약속이나 기한이 걸려 있으면 남긴다
        light = {"thread_id": 1, "score": 2}
        self.assertFalse(review._worth_reporting(light, set()))
        self.assertTrue(review._worth_reporting(light, {1}))          # 내 약속
        self.assertTrue(review._worth_reporting(dict(light, deadline=1), set()))
        self.assertTrue(review._worth_reporting(
            {"thread_id": 2, "score": review.WORTH_SCORE}, set()))

    def test_state_shift_reports_only_what_changed(self):
        # '변화 — 어제 이후' 는 리포트의 핵심인데 회귀가 하나도 없었다
        self.store.ingest([
            _rec("v0", "kim@corp.example", [ME], "변화건", "2026-07-20T09:00:00",
                 body="김도현 님, 판단 부탁드립니다. 금요일까지 회신 주시면 반영하겠습니다."),
        ])
        det = review.deterministic(self.store, self.cfg, "2026-07-20")
        moved = [t["thread_id"] for t in det["shift"]["new_mine"]]
        self.assertTrue(moved, "새로 내 차례가 안 잡혔다")
        md = review.render(det)
        self.assertIn("## 변화 — 어제 이후", md)
        self.assertIn("새로 내 차례", md)
        # 그 다음 날은 상태가 그대로다 = '새로'가 아니다
        same = review.deterministic(self.store, self.cfg, "2026-07-21")
        self.assertEqual(same["shift"]["new_mine"], [])
        self.assertIn("새로 내 차례 (0건) — 없음", review.render(same))

    def test_render_without_store_shows_buttons_but_no_fold(self):
        # store 없이 부르는 경로가 남아 있다 — 계약을 명시해 둔다
        self.assertEqual(self.web._done_set(None), set())
        self.assertEqual(self.web._done_fold(None, "/records"), "")
        det, _ = self._daily_html()
        html = self.web.render_daily(self.cfg, "2026-07-22", "2026-07-22")
        self.assertIn("action='/report/done'", html)   # 버튼은 표식에서 나온다
        self.assertNotIn("처리함으로 접은 항목", html)

    def test_nested_item_folds_with_its_quote(self):
        # 주간 '지난 차수 점검 › 아직 내 후속 없음' 모양 — 중첩 불릿 + 4칸 인용.
        # web.py 주석이 "그 항목이 절의 첫 불릿이면 문단으로 샌다"고 적은 형태다.
        md = ("## 지난 차수 점검 (2026-07-19 ~ 2026-07-25)\n"
              "- 후속 있음 (1건): [#7] 정적분석\n"
              "- 아직 내 후속 없음 (2건)\n"
              "  - [#84] 크래시 · 기한 07/25 지남<!--done:promise:aaaaaaaaaaaa-->\n"
              "    「패치 올리겠습니다.」\n"
              "  - [#45] 데모<!--done:promise:bbbbbbbbbbbb-->\n"
              "    「스크립트 공유하겠습니다.」\n")
        one = self.web._md_to_html(md, "", {"promise:aaaaaaaaaaaa"})
        self.assertNotIn("패치 올리겠습니다", one)     # 인용 줄까지 빠진다
        self.assertIn("스크립트 공유하겠습니다", one)
        self.assertIn("아직 내 후속 없음 (1건)", one)
        self._balanced(one)
        both = self.web._md_to_html(
            md, "", {"promise:aaaaaaaaaaaa", "promise:bbbbbbbbbbbb"})
        self.assertNotIn("아직 내 후속 없음", both)    # 부모 줄까지 사라진다
        self.assertIn("후속 있음 (1건)", both)         # 형제는 남는다
        self._balanced(both)

    def test_stalled_and_deadline_kinds_work_end_to_end(self):
        # 지금까지 회귀가 promise 만 덮고 있었다 — stalled·deadline 은 커버리지 0.
        self.store.ingest([
            _rec("s0", "kim@corp.example", [ME], "정체건", "2026-07-01T09:00:00",
                 body="검토 부탁드립니다."),
            _rec("s1", ME, [ME], "정체건", "2026-07-02T09:00:00",
                 body="확인 후 회신드리겠습니다. 검토 부탁드립니다.", reply_to="s0"),
            _rec("s2", "kim@corp.example", [ME], "기한건", "2026-07-22T09:00:00",
                 body="8/20 까지 제출해 주셔야 합니다."),
        ])
        det = review.deterministic(self.store, self.cfg, "2026-07-22")
        stalled = [it for it in det["intervention"]
                   if str(it.get("category", "")).startswith("stalled")]
        self.assertTrue(stalled, "정체 픽스처가 안 잡혔다")
        self.assertTrue(det["deadlines"], "기한 픽스처가 안 잡혔다")

        skey = review.stalled_key(stalled[0]["thread_id"])
        tid, _subj, quote = det["deadlines"][0]
        dkey = Store.report_key(tid, quote)
        self.store.mark_report_done("stalled", skey)
        self.store.mark_report_done("deadline", dkey)

        again = review.deterministic(self.store, self.cfg, "2026-07-22")
        self.assertNotIn(skey, [review.stalled_key(it["thread_id"])
                                for it in again["intervention"]
                                if str(it.get("category", "")).startswith("stalled")])
        self.assertNotIn(dkey, [Store.report_key(x[0], x[2])
                                for x in again["deadlines"]])
        # 다음 날에도 안 잡힌다
        self.assertNotIn(dkey, [Store.report_key(x[0], x[2]) for x in
                                review.deterministic(self.store, self.cfg,
                                                     "2026-07-23")["deadlines"]])

    def test_stalled_key_is_shared_with_the_weekly_board(self):
        # 사용자 요구: "한 번 처리하면 다음 판단에 안 잡힌다". 일간에서 접은
        # 정체 스레드가 주간 '막힘'에 남아 있으면 그 계약이 깨진다.
        from mailkb import weekly as weekly_mod
        self.store.ingest([
            _rec("m0", "kim@corp.example", [ME], "막힘건", "2026-07-01T09:00:00",
                 body="검토 부탁드립니다."),
            _rec("m1", ME, [ME], "막힘건", "2026-07-02T09:00:00",
                 body="정리해서 보내겠습니다.", reply_to="m0"),
        ])
        det = weekly_mod.deterministic(self.store, self.cfg, 4, "2026-07-22")
        stuck = weekly_mod._bucket(det, "막힘")
        self.assertTrue(stuck, "막힘 픽스처가 안 잡혔다")
        tid = stuck[0]["thread_id"]
        # 렌더에 처리함 버튼이 붙는다(키는 일간과 같다)
        self.assertIn(review.done_mark("stalled", review.stalled_key(tid)),
                      weekly_mod.render(det, None))
        self.store.mark_report_done("stalled", review.stalled_key(tid))
        det2 = weekly_mod.deterministic(self.store, self.cfg, 4, "2026-07-22")
        self.assertNotIn(tid, [t["thread_id"] for t in weekly_mod._bucket(det2, "막힘")])
        # 제목이 바뀌어도(RE: 가 붙어도) 키는 그대로여야 한다
        self.assertEqual(review.stalled_key(tid),
                         Store.report_key("stalled", tid))

    def test_marks_are_stripped_from_terminal_and_prompts(self):
        # 표식은 웹 전용이다. `mailkb review` 는 md 를 그대로 print 하고,
        # weekly.previous_report 는 지난 보고를 AI 프롬프트에 넣는다.
        md = "- [#1] 건" + review.done_mark("promise", "aaaaaaaaaaaa") + "\n"
        self.assertNotIn("<!--done", review.strip_done_marks(md))
        self.assertIn("[#1] 건", review.strip_done_marks(md))
        from mailkb import weekly as weekly_mod
        d = self.cfg.vault / "weekly"; d.mkdir(parents=True, exist_ok=True)
        (d / "2026-07-01.md").write_text(md, encoding="utf-8")
        self.assertNotIn("<!--done",
                         weekly_mod.previous_report(self.cfg, "2026-07-10"))

    def test_bad_input_and_offsite_back_change_nothing(self):
        det, html = self._daily_html()
        good = self._first_button(html)
        for form in ({"kind": ["없는종류"], "key": [good["key"]]},
                     {"kind": ["promise"], "key": ["../../etc/passwd"]},
                     {"kind": ["promise"], "key": [""]}):
            loc = self.web.perform_action(self.store, self.cfg, "/report/done",
                                          dict(form, back=["/records?tab=daily"]))
            self.assertEqual(loc, "/records?tab=daily")
        self.assertEqual(
            len(review.deterministic(self.store, self.cfg, "2026-07-22")["promises"]), 1)
        # back 은 받은 문자열을 쓰지 않고 탭·날짜만 뽑아 **다시 만든다** —
        # 그대로 Location 에 넣으면 CRLF 로 응답 헤더를 위조할 수 있다.
        for evil in ("https://evil.example/x", "//evil.example", "/settings",
                     "/records\r\nX-Injected: yes", "/records?tab=<script>",
                     "/records?tab=daily&date=../../etc", ""):
            loc = self.web.perform_action(
                self.store, self.cfg, "/report/undo",
                {"kind": ["promise"], "key": [good["key"]], "back": [evil]})
            self.assertEqual(loc, "/records?tab=daily", msg=evil)
            self.assertNotIn("\r", loc)
            self.assertNotIn("\n", loc)
        # 정상 입력은 그대로 살아난다
        self.assertEqual(
            self.web.perform_action(
                self.store, self.cfg, "/report/undo",
                {"kind": ["promise"], "key": [good["key"]],
                 "back": ["/records?tab=weekly&date=2026-07-25"]}),
            "/records?tab=weekly&date=2026-07-25")


class TestPromises(unittest.TestCase):
    """내 약속 추적 — '내가 말해 놓고 안 한 것'을 인용과 함께 짚는다.

    원칙은 **모르겠으면 보고하지 않는다**(2026-08-01 사용자 확정). 예전 '지금 할 일'
    큐가 상대 의도를 추측하다 신뢰를 잃었기 때문에, 여기서는 내가 직접 쓴 확정
    어미만 인정하고 정황이 애매하면 뺀다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _thread(self, *bodies):
        """bodies = [(is_me, body), …] — 시간순으로 한 스레드에 넣는다."""
        recs = []
        for i, (mine, body) in enumerate(bodies):
            recs.append(_rec(f"p{i}", ME if mine else "kim@x", [ME], "협상",
                             f"2026-07-{20 + i:02d}T09:00:00", body=body,
                             reply_to="p0" if i else ""))
        self.store.ingest(recs)

    def test_extracts_only_my_confirmed_commitments(self):
        self._thread(
            (True, "패치는 내일 오전에 올리겠습니다."),          # 약속
            (False, "확인 부탁드립니다."),
        )
        got = promises.extract(self.store, today="2026-07-24")
        self.assertEqual(len(got), 1)
        self.assertIn("올리겠습니다", got[0]["quote"])
        self.assertEqual(got[0]["due"].isoformat(), "2026-07-21")   # 내일 = 발신 다음날

    def test_requests_and_conditionals_are_not_commitments(self):
        # 각각 다른 스레드로 넣는다 — 셋 다 약속이 아니므로 결과가 비어야 한다
        for i, body in enumerate(("검토해 주시면 반영하겠습니다.",   # 조건절
                                  "회신 부탁드립니다.",              # 요청
                                  "일정상 어렵겠습니다.")):          # 부정
            self.store.ingest([_rec(f"n{i}", ME, [ME], f"건 {i}",
                                    f"2026-07-2{i}T09:00:00", body=body)])
        self.assertEqual(promises.extract(self.store, today="2026-07-24"), [])

    def test_later_message_of_mine_counts_as_follow_up(self):
        self._thread(
            (True, "패치는 내일 올리겠습니다."),
            (False, "네 기다리겠습니다."),
            (True, "올렸습니다. 확인 부탁드립니다."),      # 내가 다시 보냄
        )
        self.assertEqual(promises.extract(self.store, today="2026-07-24"), [])

    def test_old_commitments_drop_out(self):
        self._thread((True, "정리해서 보내겠습니다."))
        self.assertEqual(len(promises.extract(self.store, today="2026-07-25")), 1)
        self.assertEqual(promises.extract(self.store, today="2026-08-20"), [])

    def test_done_mark_is_permanent_across_days(self):
        # 메일 밖(회의·구두)에서 처리한 것은 눌러서 접는다 — 다음날도 안 잡힌다
        self._thread((True, "정리해서 보내겠습니다."))
        got = promises.extract(self.store, today="2026-07-21")
        self.store.mark_report_done("promise", got[0]["key"], got[0]["thread_id"],
                                    got[0]["quote"])
        for day in ("2026-07-21", "2026-07-22", "2026-07-28"):
            self.assertEqual(promises.extract(self.store, today=day), [], msg=day)
        self.store.unmark_report_done("promise", got[0]["key"])
        self.assertEqual(len(promises.extract(self.store, today="2026-07-22")), 1)

    def test_new_commitment_in_same_thread_still_reported(self):
        # 스레드 단위 해제가 아니다 — 같은 스레드의 **새 약속**은 다시 올라온다
        self._thread((True, "1차는 정리해서 보내겠습니다."))
        first = promises.extract(self.store, today="2026-07-21")[0]
        self.store.mark_report_done("promise", first["key"])
        self.store.ingest([_rec("p9", ME, [ME], "협상", "2026-07-22T09:00:00",
                                body="2차 결과도 공유하겠습니다.", reply_to="p0")])
        got = promises.extract(self.store, today="2026-07-23")
        self.assertEqual(len(got), 1)
        self.assertIn("2차", got[0]["quote"])

    def test_review_period_splits_by_follow_up_not_by_judgement(self):
        # 지난 차수 점검 — 그 기간에 한 약속이 지금 어떻게 됐나. '안 지켰다'가
        # 아니라 "그 뒤 내가 보낸 것이 없다"는 사실만 가른다.
        self._thread(
            (True, "패치는 내일 올리겠습니다."),        # 07-20 · 뒤에 내가 또 보냄
            (False, "네 기다리겠습니다."),
            (True, "올렸습니다."),                      # 07-22
        )
        self.store.ingest([_rec("q0", ME, [ME], "다른 건", "2026-07-21T09:00:00",
                                body="결과는 정리해서 보내겠습니다.")])
        got = promises.review_period(self.store, "2026-07-19", "2026-07-25",
                                     today="2026-08-01")
        self.assertEqual([p["subject"] for p in got["kept"]], ["협상"])
        self.assertEqual([p["subject"] for p in got["open"]], ["다른 건"])

    def test_review_period_ignores_the_fourteen_day_cutoff(self):
        # extract 의 14일 컷을 그대로 쓰면 2주 넘은 차수는 조용히 빈 절이 된다.
        self._thread((True, "정리해서 보내겠습니다."))
        self.assertEqual(promises.extract(self.store, today="2026-08-20"), [])
        got = promises.review_period(self.store, "2026-07-19", "2026-07-25",
                                     today="2026-08-20")
        self.assertEqual(len(got["open"]), 1)
        # 창 밖 발신은 안 센다
        self.assertEqual(promises.review_period(self.store, "2026-07-01",
                                                "2026-07-10")["open"], [])

    def test_review_period_counts_dismissed_promise_as_followed_up(self):
        # 메일 밖(회의·구두)에서 처리하고 '처리함'으로 접은 것은 후속으로 센다 —
        # 접었는데 지난 차수 점검에서 다시 올라오면 접는 의미가 없다.
        self._thread((True, "정리해서 보내겠습니다."))
        one = promises.extract(self.store, today="2026-07-21")[0]
        self.store.mark_report_done("promise", one["key"])
        got = promises.review_period(self.store, "2026-07-19", "2026-07-25",
                                     today="2026-08-01")
        self.assertEqual(len(got["kept"]), 1)
        self.assertEqual(got["open"], [])

    _MIDJOIN_BODY = (
        "네, 확인했습니다.\n\n"
        "보낸 사람: 강미래 <mirae@corp.example>\n"
        "보낸 날짜: 2026년 7월 19일 월요일 오전 9:00\n"
        "받는 사람: 김도현 <me@corp.example>\n"
        "제목: 계약서 초안\n\n"
        "강미래입니다. 계약서 초안은 제가 금요일까지 보내드리겠습니다.")

    def test_past_report_excludes_promises_made_later(self):
        # 지난 날짜의 리포트를 열면 그 뒤에 한 약속이 섞여 "-2일 전" 이 찍혔다
        # (2026-08-01 실기기 데이터에서 확인 — 사용자가 그걸 '처리함' 으로 접었다).
        self._thread((True, "정리해서 보내겠습니다."))          # 07-20 발신
        self.store.ingest([_rec("late", ME, [ME], "나중 건",
                                "2026-07-25T09:00:00",
                                body="결과는 공유하겠습니다.")])
        early = promises.extract(self.store, today="2026-07-22")
        self.assertEqual([p["subject"] for p in early], ["협상"])   # 25일 건은 없다
        self.assertTrue(all(p["days"] >= 0 for p in early))
        later = promises.extract(self.store, today="2026-07-26")
        self.assertEqual(len(later), 2)                            # 그날엔 둘 다
        self.assertTrue(all(p["days"] >= 0 for p in later))

    def test_review_period_is_empty_on_odd_windows(self):
        self.assertEqual(promises.review_period(self.store, "2026-07-19",
                                                "2026-07-25", today="2026-08-01"),
                         {"kept": [], "open": [], "start": "2026-07-19",
                          "end": "2026-07-25"})          # 빈 저장소
        self._thread((True, "정리해서 보내겠습니다."))
        got = promises.review_period(self.store, "2026-07-25", "2026-07-19",
                                     today="2026-08-01")  # 끝 < 시작
        self.assertEqual((got["kept"], got["open"]), ([], []))

    def test_preserved_quote_is_not_my_promise(self):
        # 스레드 첫 보유 메일은 mid-join 보존이라 new_content 안에 상대 원문이
        # 통째로 남는다. 안 떼면 **남이 쓴 확정 어미**가 '내 약속'으로 올라간다
        # (2026-08-01 적대 검토에서 실증). 인용 검증은 "본문에 있는가"만 보므로
        # 통과해 버리는데, 그 본문이 내 문장이 아니다.
        self.store.ingest([_rec("q0", ME, [ME], "계약서 초안",
                                "2026-07-20T09:00:00", body=self._MIDJOIN_BODY)])
        row = self.store.db.execute("SELECT new_content FROM messages").fetchone()
        self.assertIn("--- 이전 대화 (인용 보존) ---", row["new_content"])  # 픽스처 전제
        self.assertEqual(promises.extract(self.store, today="2026-07-21"), [])
        self.assertEqual(
            promises.review_period(self.store, "2026-07-19", "2026-07-25",
                                   today="2026-07-26")["open"], [])
        # 내가 새로 쓴 부분의 약속은 그대로 잡힌다
        self.store.ingest([_rec("q1", ME, [ME], "다른 건", "2026-07-20T10:00:00",
                                body="정리해서 보내겠습니다.\n\n"
                                     + self._MIDJOIN_BODY.split("\n\n", 1)[1])])
        got = promises.extract(self.store, today="2026-07-21")
        self.assertEqual([p["quote"] for p in got], ["정리해서 보내겠습니다."])

    def test_relative_dates_resolve_or_are_dropped(self):
        base = date(2026, 7, 20)          # 월요일
        self.assertEqual(promises.resolve_when("내일", base), date(2026, 7, 21))
        self.assertEqual(promises.resolve_when("금요일", base), date(2026, 7, 24))
        self.assertEqual(promises.resolve_when("다음 주 화요일", base), date(2026, 7, 28))
        self.assertEqual(promises.resolve_when("8/20", base), date(2026, 8, 20))
        # 날짜가 안 잡히는 표현은 추정하지 않는다 — 틀린 기한을 박느니 비운다
        for vague in ("이번 주 중", "조만간", "가능한 빨리", ""):
            self.assertIsNone(promises.resolve_when(vague, base), msg=vague)
        # 연말의 "1/5까지"는 내년이다 — 같은 해로 두면 '⚠ 지남'이 붙는다
        self.assertEqual(promises.resolve_when("1/5", date(2026, 12, 30)),
                         date(2027, 1, 5))
        self.assertEqual(promises.resolve_when("1월 5일", date(2026, 12, 31)),
                         date(2027, 1, 5))
        # 며칠 지난 날짜를 적는 경우도 있으므로 여유를 둔다(다음 해로 안 넘긴다)
        self.assertEqual(promises.resolve_when("7/18", date(2026, 7, 20)),
                         date(2026, 7, 18))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _reopen_with_stale_clean(self, rows):
        """new_content 를 구 절단 결과로 강제 덮고 clean_version 을 지운 뒤
        재오픈 — CLEAN_VERSION 승격 상황 재현. rows = {id: content}."""
        for mid, content in rows.items():
            self.store.db.execute(
                "UPDATE messages SET new_content=? WHERE id=?", (content, mid))
        self.store.db.execute(
            "DELETE FROM sync_state WHERE key IN "
            "('clean_version', 'feature_version')")
        self.store.db.commit()
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        return self.store

    def test_reclean_migration_cuts_stored_chains(self):
        # 구 규칙이 못 자른 프랑스어 체인이 저장돼 있다 → 버전 승격 재오픈이
        # 저장값을 소급 절단하고 FTS·버전 스탬프까지 정리한다(재수집 없이).
        self.store.ingest([
            _rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00", body="첫 메일"),
            _rec("b", "jean@partner.example", [ME], "RE: 협상",
                 "2026-07-02T09:00:00", body="회신", reply_to="a"),
        ])
        chain = ("Voici notre position finale.\n\n"
                 "De\u00a0: Kim <" + ME + ">\n"
                 "Envoyé\u00a0: mercredi\nÀ\u00a0: Jean\n"
                 "Objet\u00a0: RE: 협상\n\n" + "긴 이전 체인 " * 200)
        st = self._reopen_with_stale_clean({2: chain})
        self.assertEqual(st.recleaned, 1)
        m = st.db.execute("SELECT new_content FROM messages WHERE id=2").fetchone()
        self.assertEqual(m["new_content"], "Voici notre position finale.")
        # FTS rebuild — 새 head 로 검색되고, 잘려나간 체인으로는 안 잡힌다
        hit = st.db.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            ('"position finale"',)).fetchall()
        self.assertEqual([r["rowid"] for r in hit], [2])
        ver = st.db.execute(
            "SELECT value FROM sync_state WHERE key='clean_version'").fetchone()
        from mailkb.clean import CLEAN_VERSION
        self.assertEqual(ver["value"], str(CLEAN_VERSION))
        # 재오픈해도 다시 안 돈다 (버전 일치)
        st.close()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.assertEqual(self.store.recleaned, 0)

    def test_reclean_keeps_restorable_backup(self):
        # sync 는 이미 있는 message_id 를 건너뛰므로 sync --full 로도 본문이
        # 되돌아오지 않는다 — 규칙 오탐 시 reclean_backup 이 유일한 복구 수단.
        self.store.ingest([
            _rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "jean@partner.example", [ME], "RE: 협상",
                 "2026-07-02T09:00:00", body="회신", reply_to="a"),
        ])
        chain = ("Position finale.\n\nDe\u00a0: Kim\nEnvoyé\u00a0: lundi\n"
                 "À\u00a0: Jean\nObjet\u00a0: RE\n\n" + "옛 체인 " * 100)
        st = self._reopen_with_stale_clean({2: chain})
        bk = st.db.execute(
            "SELECT old_content FROM reclean_backup WHERE message_id=2").fetchone()
        self.assertEqual(bk["old_content"], chain)      # 덮기 전 원본 보존
        # 스키마 주석의 복구 SQL 이 실제로 되돌린다
        st.db.execute(
            "UPDATE messages SET new_content=(SELECT old_content FROM "
            "reclean_backup WHERE message_id=messages.id) "
            "WHERE id IN (SELECT message_id FROM reclean_backup)")
        st.db.commit()
        back = st.db.execute(
            "SELECT new_content FROM messages WHERE id=2").fetchone()
        self.assertEqual(back["new_content"], chain)
        # 재승격돼도 최초 원본이 남는다(2차 절단 입력으로 덮어쓰지 않음)
        st.db.execute("DELETE FROM sync_state WHERE key='clean_version'")
        st.db.commit(); st.close()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        bk2 = self.store.db.execute(
            "SELECT old_content FROM reclean_backup WHERE message_id=2").fetchone()
        self.assertEqual(bk2["old_content"], chain)

    def test_reclean_preserves_midjoin_first_mail(self):
        # 스레드 첫 보유 메일은 ingest 와 같은 mid-join 보존 — 새 규칙이 찾은
        # 절단점 아래를 버리지 않고 PRESERVED_MARK 로 접는다(유일본 보호).
        from mailkb.clean import PRESERVED_MARK
        self.store.ingest([
            _rec("a", "jean@partner.example", [ME], "제안",
                 "2026-07-01T09:00:00", body="첫 메일")])
        chain = ("Bonjour, voici la proposition.\n\n"
                 "De\u00a0: Marie <m@partner.example>\n"
                 "Envoyé\u00a0: lundi\nÀ\u00a0: Kim\nObjet\u00a0: 제안\n\n"
                 "DB에 없는 유일한 과거 대화")
        st = self._reopen_with_stale_clean({1: chain})
        m = st.db.execute("SELECT new_content FROM messages WHERE id=1").fetchone()
        self.assertIn("voici la proposition", m["new_content"])
        self.assertIn(PRESERVED_MARK, m["new_content"])
        self.assertIn("유일한 과거 대화", m["new_content"])
        # 이미 마크가 있는 메일은 재절단이 건드리지 않는다 (멱등)
        st.db.execute("DELETE FROM sync_state WHERE key='clean_version'")
        st.db.commit(); st.close()
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        m2 = self.store.db.execute(
            "SELECT new_content FROM messages WHERE id=1").fetchone()
        self.assertEqual(m2["new_content"], m["new_content"])

    def test_reclean_preserve_follows_ingest_order_not_time(self):
        # --since 로 최근분을 먼저 모은 뒤 --full 로 옛 메일이 합류하면, 스레드를
        # '만든' 메일(먼저 적재)과 '가장 오래된' 메일이 다르다. 시각 기준으로
        # 잡으면 진짜 첫 보유분의 유일한 인용 체인이 잘려 사라진다(리뷰 실증).
        from mailkb.clean import PRESERVED_MARK
        self.store.ingest([                      # 먼저 적재 = 스레드 생성
            _rec("m3", "jean@partner.example", [ME], "협상",
                 "2026-07-10T09:00:00", body="최근 회신")])
        self.store.ingest([                      # 나중에 백필된 더 오래된 메일
            _rec("m1", "jean@partner.example", [ME], "협상",
                 "2026-07-01T09:00:00", body="첫 메일", reply_to="")])
        ids = {r["message_id"]: r["id"] for r in self.store.db.execute(
            "SELECT message_id, id FROM messages")}
        chain = ("Position finale.\n\nDe\u00a0: Marie\nEnvoyé\u00a0: mardi\n"
                 "À\u00a0: Kim\nObjet\u00a0: 협상\n\nDB에 없는 중간 대화(M2)")
        st = self._reopen_with_stale_clean({ids["<m3@t>"]: chain})
        m = st.db.execute("SELECT new_content FROM messages WHERE id=?",
                          (ids["<m3@t>"],)).fetchone()
        self.assertIn(PRESERVED_MARK, m["new_content"])       # 접되 버리지 않음
        self.assertIn("DB에 없는 중간 대화(M2)", m["new_content"])

    def test_reclean_invalidates_word_map_and_summaries(self):
        # 어휘 지도 캐시는 키에 본문이 없어(id·통수·날짜 지문) 재절단 후에도
        # 지워진 인용 어휘를 계속 보여줬다. 롤링 요약도 증분 가드 때문에
        # 부푼 입력으로 만든 것이 영영 남았다(2026-07-31 리뷰 실증).
        self.store.ingest([
            _rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "jean@partner.example", [ME], "RE: 협상",
                 "2026-07-02T09:00:00", body="회신", reply_to="a"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE id=2").fetchone()[0]
        self.store.save_summary(tid, "옛 요약(부푼 입력 기반)", 2)
        self.store.db.execute(
            "INSERT INTO people_word_profiles(addr, profile_json, updated) "
            "VALUES ('jean@partner.example', '{\"x\": 1}', '2026-07-02')")
        self.store.db.commit()
        chain = ("Position finale.\n\nDe\u00a0: Kim\nEnvoyé\u00a0: lundi\n"
                 "À\u00a0: Jean\nObjet\u00a0: RE\n\n" + "옛 체인 " * 200)
        st = self._reopen_with_stale_clean({2: chain})
        self.assertEqual(st.recleaned, 1)
        self.assertEqual(st.db.execute(
            "SELECT COUNT(*) FROM people_word_profiles").fetchone()[0], 0)
        # 바뀐 스레드만 증분 가드 해제 → 다음 회고가 다시 요약한다
        row = st.db.execute(
            "SELECT summary_msg_count FROM threads WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["summary_msg_count"], 0)

    def test_prune_skips_when_transaction_open(self):
        # sync 의 finally 가 부르는 프룬이 남의 미완 트랜잭션을 커밋하면,
        # 어휘 파생 없는 메일이 영구히 남는다 — ingest 의 롤백은
        # except Exception 이라 KeyboardInterrupt 를 안 잡는다(실증: 150통
        # 중단 시 39통 누락, 재수집으로도 복구 불가).
        def feed(boom):
            for i in range(6):
                if i == boom:
                    raise KeyboardInterrupt
                yield _rec(f"k{i}", "kim@x", [ME], f"건 {i}",
                           f"2026-07-{1 + i:02d}T09:00:00")
        for retain in (60, 0):
            tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
            st = Store(Path(tmp.name) / "t.sqlite", [ME])
            self.addCleanup(st.close)
            with self.assertRaises(KeyboardInterrupt):
                st.ingest(feed(5), chunk_size=2)
            self.assertIsNone(st.maybe_prune_html(retain))   # 건너뛴다
            st.close()                    # 미커밋 청크는 여기서 사라진다
            chk = Store(Path(tmp.name) / "t.sqlite", [ME])
            self.addCleanup(chk.close)
            n_msg = chk.db.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
            n_tf = chk.db.execute(
                "SELECT COUNT(*) FROM message_term_features").fetchone()[0]
            self.assertEqual(n_msg, n_tf, msg=f"retain={retain}")
            self.assertEqual(n_msg, 4)    # 완결된 청크만 남는다

    def test_signature_only_recut_does_not_force_resummary(self):
        # 서명 한 줄(20~30자)이 사라졌다고 스레드를 다시 요약하면 AI 비용이
        # 헛나간다 — 실기기 사본에서 변경 88건이 전부 서명 제거였다.
        self.store.ingest([
            _rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "kim@x", [ME], "RE: 협상", "2026-07-02T09:00:00",
                 body="답", reply_to="a"),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE id=2").fetchone()[0]
        self.store.save_summary(tid, "기존 요약", 2)
        self.store.db.commit()
        st = self._reopen_with_stale_clean(
            {2: "확인했습니다.\n--\n김도현\nSoC개발팀 | 내선 1234"})
        self.assertEqual(st.recleaned, 1)                 # 서명은 제거됐지만
        self.assertEqual(st.db.execute(                   # 재요약은 안 시킨다
            "SELECT summary_msg_count FROM threads WHERE id=?",
            (tid,)).fetchone()[0], 2)
        # 인용 체인이 빠진 경우에는 재요약한다
        st.db.execute("UPDATE threads SET summary_msg_count=2 WHERE id=?", (tid,))
        chain = ("본론.\n\n보낸 사람: 김 <k@x>\n보낸 날짜: 7/30\n받는 사람: 이\n"
                 "제목: RE\n\n" + "옛 체인 " * 100)
        st2 = self._reopen_with_stale_clean({2: chain})
        self.assertEqual(st2.db.execute(
            "SELECT summary_msg_count FROM threads WHERE id=?",
            (tid,)).fetchone()[0], 0)

    def test_reclean_backup_expiry_independent_of_image_setting(self):
        # 백업 만료가 이미지 프룬 게이트 뒤에 있어서, image_retain_days=0
        # ('임베드 끔' — 지원 옵션)인 사용자는 백업을 영구 보유했다.
        self.store.ingest([
            _rec("a", "kim@x", [ME], "건", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "kim@x", [ME], "RE: 건", "2026-07-02T09:00:00",
                 body="답", reply_to="a"),
        ])
        self.store.db.execute(
            "INSERT INTO reclean_backup(message_id, old_content, created, "
            "from_version) VALUES (1, '옛 원본', '2020-01-01T00:00:00', 4)")
        self.store.db.commit()
        self.store.maybe_prune_html(0)          # 이미지 프룬은 꺼진 설정
        left = self.store.db.execute(
            "SELECT COUNT(*) FROM reclean_backup").fetchone()[0]
        self.assertEqual(left, 0)

    def test_reclean_records_run_for_diagnose(self):
        # 재절단은 보통 웹 서버가 치르는데 pythonw 실행에선 stderr 가 없어
        # 안내가 사라진다 — 상태로 남겨 diagnose 가 나중에도 보여준다.
        self.store.ingest([
            _rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "jean@p.example", [ME], "RE: 협상", "2026-07-02T09:00:00",
                 body="답", reply_to="a"),
        ])
        chain = ("Position.\n\nDe\u00a0: Kim\nEnvoyé\u00a0: lundi\n"
                 "À\u00a0: Jean\nObjet\u00a0: RE\n\n" + "옛 " * 50)
        st = self._reopen_with_stale_clean({2: chain})
        self.assertEqual(st.recleaned, 1)
        rec = st.get_state("last_reclean")
        self.assertTrue(rec and rec.endswith(":1"), rec)
        # 백업 행에 '어느 버전 직전인지'가 남는다(만료 후 2차 백업 구분용)
        from mailkb.clean import CLEAN_VERSION
        v = st.db.execute(
            "SELECT from_version FROM reclean_backup WHERE message_id=2"
        ).fetchone()[0]
        self.assertEqual(v, CLEAN_VERSION)

    def test_reclean_never_empties_message(self):
        # 재절단 결과가 비면 옛 값 유지 — 마이그레이션은 파괴하지 않는다
        self.store.ingest([
            _rec("a", "kim@x", [ME], "공지", "2026-07-01T09:00:00", body="첫"),
            _rec("b", "kim@x", [ME], "RE: 공지", "2026-07-02T09:00:00",
                 body="회신", reply_to="a"),
        ])
        whole_chain = ("De\u00a0: Kim <k@x>\nEnvoyé\u00a0: lundi\n"
                       "Objet\u00a0: RE\n\n본문 전체가 인용인 메일")
        st = self._reopen_with_stale_clean({2: whole_chain})
        m = st.db.execute("SELECT new_content FROM messages WHERE id=2").fetchone()
        self.assertEqual(m["new_content"], whole_chain)   # 비우는 대신 보존

    def test_reclean_invalidates_derived_only_when_changed(self):
        # 재분류(1만통 ~11s)는 본문이 실제로 바뀐 DB 만 치른다 — 버전 해시에
        # 넣으면 바뀐 게 없는 사용자까지 웹 첫 화면이 멈춘다(2026-07-31 계측).
        self.store.ingest([
            _rec("a", "kim@x", [ME], "건", "2026-07-01T09:00:00", body="짧은 본문"),
            _rec("b", "kim@x", [ME], "RE: 건", "2026-07-02T09:00:00",
                 body="짧은 답", reply_to="a"),
        ])
        # (1) 바뀔 게 없으면 스탬프가 남아 재구축이 없다
        st = self._reopen_with_stale_clean({})
        self.assertEqual(st.recleaned, 0)
        keys = {r["key"] for r in st.db.execute(
            "SELECT key FROM sync_state WHERE key LIKE '%feature_version'")}
        self.assertIn("feature_version", keys)
        # (2) 실제 재절단이 일어나면 파생 스탬프를 지워 백필이 이어받는다
        chain = ("본론.\n\nDe\u00a0: Kim\nEnvoyé\u00a0: lundi\n"
                 "À\u00a0: Jean\nObjet\u00a0: RE\n\n" + "옛 체인 " * 50)
        st2 = self._reopen_with_stale_clean({2: chain})
        self.assertEqual(st2.recleaned, 1)
        # 열기 중 _ensure_derived_state 가 곧바로 다시 채운다 → 값이 현행이어야
        cur = st2.db.execute(
            "SELECT value FROM sync_state WHERE key='feature_version'").fetchone()
        self.assertEqual(cur["value"], st2._feature_version())
        # 어휘 쪽은 다음 sync 로 미뤄진다(열기에서 안 돈다)
        self.assertIsNone(st2.db.execute(
            "SELECT value FROM sync_state WHERE key='term_feature_version'"
        ).fetchone())

    def test_suspect_uncut_quotes_detector(self):
        # 감지기: 후속 메일이 직전 본문을 통째 재포함하면 의심 목록에 뜬다 —
        # 언어 무관 신호라 새 라벨이 필요한 스레드를 실기기에서 찾는 창구
        base = "이전 내용 문단입니다. " * 30            # 지문 120자 이상
        self.store.ingest([
            _rec("a", "x@ext.example", [ME], "협상", "2026-07-01T09:00:00",
                 body=base),
            _rec("b", "x@ext.example", [ME], "RE: 협상", "2026-07-02T09:00:00",
                 body="답신 하나.\n" + base + "덧붙임 " * 600, reply_to="a"),
            _rec("c", "x@ext.example", [ME], "RE: 협상", "2026-07-03T09:00:00",
                 body="답신 둘.\n" + base + "덧붙임 " * 600, reply_to="a"),
        ])
        sus = self.store.suspect_uncut_quotes()
        self.assertEqual(len(sus), 1)
        self.assertEqual(sus[0]["domain"], "ext.example")
        self.assertGreaterEqual(sus[0]["pairs"], 1)
        # 정상 절단 스레드는 안 뜬다
        self.store.ingest([
            _rec("d", "y@x", [ME], "정상", "2026-07-01T09:00:00", body="짧은 글"),
            _rec("e", "y@x", [ME], "RE: 정상", "2026-07-02T09:00:00",
                 body="짧은 답", reply_to="d"),
            _rec("f", "y@x", [ME], "RE: 정상", "2026-07-03T09:00:00",
                 body="짧은 답2", reply_to="d"),
        ])
        self.assertEqual(len(self.store.suspect_uncut_quotes()), 1)

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

    # ── 청크 커밋(잠금 완화) — 결과 불변 + 크래시 정합성 ──
    @staticmethod
    def _corpus(n=7):
        # 여러 발신자·날짜에 걸친 수신 메일(어휘 피처가 실제로 쌓이게 본문 있음)
        recs = []
        for i in range(n):
            who = ("kim@c", "lee@c", "park@c")[i % 3]
            recs.append(_rec(
                f"k{i}", who, [ME], f"검토 요청 {i}",
                f"2026-07-{(i % 27) + 1:02d}T09:{i % 60:02d}:00",
                body=f"양자화 커널 검토 부탁드립니다 사안{i}"))
        return recs

    def _term_snapshot(self, store):
        # 파생 어휘 테이블 전체 덤프 — 청크 여부와 무관하게 동일해야 한다
        snap = {}
        for tbl in ("message_term_features", "message_term_bags",
                    "message_term_subject_delta", "person_term_window"):
            snap[tbl] = sorted(
                tuple(r) for r in store.db.execute(f"SELECT * FROM {tbl}"))
        snap["last_sync"] = store.last_sync()
        snap["messages"] = store.db.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
        return snap

    def test_chunked_ingest_matches_single_commit(self):
        # 청크 커밋(chunk_size=2)이 단일 커밋(큰 chunk)과 최종 상태 동일 — 결과 불변
        recs = self._corpus(7)
        self.store.ingest(list(recs), chunk_size=1000)   # 사실상 1회 커밋
        base = self._term_snapshot(self.store)

        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        s2 = Store(Path(tmp2.name) / "t2.sqlite", [ME])
        self.addCleanup(s2.close)
        s2.ingest(list(recs), chunk_size=2)              # 여러 번 flush
        self.assertEqual(self._term_snapshot(s2), base)

    def test_chunked_ingest_equals_consecutive_syncs(self):
        # 청크 ingest ≡ 작은 sync 를 연속 실행 (불변식 직접 검증)
        recs = self._corpus(6)
        self.store.ingest(list(recs), chunk_size=2)
        chunked = self._term_snapshot(self.store)

        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        s2 = Store(Path(tmp2.name) / "t2.sqlite", [ME])
        self.addCleanup(s2.close)
        for pos in range(0, len(recs), 2):               # 2통씩 별도 sync
            s2.ingest(list(recs[pos:pos + 2]), chunk_size=1000)
        self.assertEqual(self._term_snapshot(s2), chunked)

    def test_chunked_ingest_crash_leaves_consistent_prefix(self):
        # 배치 도중 예외 → 완료된 청크는 커밋·워터마크 전진, 미완 청크만 롤백
        recs = self._corpus(4)

        def gen():
            for r in recs:            # 2통씩 flush → rec0..3 커밋 후
                yield r
            raise RuntimeError("COM 끊김 흉내")   # 그 다음 통에서 크래시

        with self.assertRaises(RuntimeError):
            self.store.ingest(gen(), chunk_size=2)
        # 완료된 청크(4통)는 살아남고 워터마크도 그만큼 전진(다음 sync 가 이어받음)
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], 4)
        self.assertEqual(self.store.last_sync(), recs[2].sent_on
                         if recs[2].sent_on > recs[3].sent_on else recs[3].sent_on)
        # 커밋된 메일은 어휘 피처도 갖춤(부분집합 누락 없음)
        feat = self.store.db.execute(
            "SELECT COUNT(*) FROM message_term_features").fetchone()[0]
        self.assertEqual(feat, 4)

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
        self.store.db.execute("DELETE FROM sync_state WHERE key='feature_version'")
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
    합류분은 종전대로 절단. docs/ARCHITECTURE.md §6.1."""

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
                "<div>보낸 사람: 김민수</div><div>보낸 날짜: 2026-07-30</div>"
                "<div>받는 사람: 박서준</div><div>제목: RE</div>"
                "<div>이전 본문 텍스트</div>")
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

    # ------------------------------------------------- 다크 모드 색 보정
    def test_dark_variant_lifts_lightness_keeps_hue(self):
        # 메일은 흰 배경 전제라 Outlook 강조색이 다크에서 전부 AA 미달이다.
        # 색상(H)은 두고 명도(L)만 올려 읽히게 한다 — 빨강은 빨강으로 남아야 한다.
        import colorsys
        from mailkb.clean import dark_variant

        def hue(hexs):
            r, g, b = [int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            return colorsys.rgb_to_hls(r, g, b)[0]

        for src in ("#C00000", "#0070C0", "#008000", "#7030A0"):
            out = dark_variant(src)
            self.assertIsNotNone(out, src)
            self.assertAlmostEqual(hue(src), hue(out), places=2)   # 색상 보존
            l_in = colorsys.rgb_to_hls(
                *[int(src[i:i + 2], 16) / 255 for i in (1, 3, 5)])[1]
            l_out = colorsys.rgb_to_hls(
                *[int(out[i:i + 2], 16) / 255 for i in (1, 3, 5)])[1]
            self.assertGreater(l_out, l_in, src)                   # 명도 상승
        # 무채색은 손대지 않는다 — 본문 글자는 테마색이 맞다
        for gray in ("#000000", "#808080", "#FFFFFF", "gray"):
            self.assertIsNone(dark_variant(gray), gray)
        self.assertIsNone(dark_variant("듣도 보도 못한 색"))
        self.assertIsNone(dark_variant(""))

    def test_add_dark_colors_covers_real_mail_shapes(self):
        from mailkb.clean import add_dark_colors as adc
        # style 의 color
        self.assertIn("--dk:", adc('<span style="color:#C00000">x</span>'))
        # 레거시 <font color> — style 로 옮겨서 처리
        out = adc('<font color="red" size="3">x</font>')
        self.assertIn("color:red", out)
        self.assertIn("--dk:", out)
        self.assertIn('size="3"', out)                 # 다른 속성 보존
        # font 에 style 이 이미 있으면 병합, color 가 있으면 그쪽 우선
        self.assertIn("font-weight:bold",
                      adc('<font color="#00B0F0" style="font-weight:bold">x</font>'))
        merged = adc('<font color="#00B0F0" style="color:#008000">x</font>')
        self.assertEqual(merged.count("--dk:"), 1)
        self.assertIn("color:#008000", merged)
        # rgb() 도 읽는다 / 무채색·배경색은 대상 아님
        self.assertIn("--dk:", adc('<span style="color:rgb(192,0,0)">x</span>'))
        self.assertNotIn("--dk", adc('<span style="color:#808080">x</span>'))
        self.assertNotIn("--dk", adc('<span style="background-color:#FF0">x</span>'))
        # 색이 없으면 원본 그대로, 두 번 돌려도 같다(멱등)
        self.assertEqual(adc("<p>보통</p>"), "<p>보통</p>")
        once = adc('<span style="color:#C00000">x</span>')
        self.assertEqual(adc(once), once)

    def test_add_dark_colors_strips_inline_important(self):
        # 인라인 !important 는 시트 !important 를 이겨 다크 평탄화를 뚫는다 —
        # color:black!important 가 다크 배경 위 순수 검정으로 남았다(2026-07-27
        # Edge 실측, Confluence·뉴스레터 표 패턴). 렌더 시점에 걷어내 테마 CSS 가
        # 주도권을 되찾는다. 라이트는 경쟁하는 시트 규칙이 없어 무영향.
        from mailkb.clean import add_dark_colors as adc
        out = adc('<td style="color:black!important">x</td>')
        self.assertNotIn("important", out.lower())
        self.assertNotIn("--dk", out)              # 무채색은 평탄화에 맡긴다
        # 유채색 + !important → 제거 후 --dk 명도 보정까지
        out2 = adc('<td style="color:#C00000 !important">x</td>')
        self.assertNotIn("important", out2.lower())
        self.assertIn("--dk:", out2)
        # color 없는 선언(border 등)의 !important 도 제거 — 다크 테두리
        # 평탄화(border-color !important)가 같은 방식으로 뚫리기 때문
        out3 = adc('<td style="border:1px solid black !important">x</td>')
        self.assertNotIn("important", out3.lower())
        # 멱등 — 두 번 돌려도 같다
        for o in (out, out2, out3):
            self.assertEqual(adc(o), o)

    def test_dark_css_revives_author_color_without_breaking_light(self):
        css = web._CSS
        # 자손 상속이 --dk 규칙보다 먼저 와야 한다. 특이도가 같아(0,4,0) 순서로
        # 결정되므로, 뒤집히면 중첩된 자기 색이 부모 색에 덮인다.
        desc = css.index('.mailhtml [style*="--dk"] :not(a)')
        own = css.index('.mailhtml :not(a)[style*="--dk"]')
        self.assertLess(desc, own)
        # 두 규칙 모두 다크에서만 — 라이트엔 --dk 를 읽는 규칙이 없어야 한다
        for frag in ('.mailhtml [style*="--dk"] :not(a)',
                     '.mailhtml :not(a)[style*="--dk"]'):
            i = css.index(frag)
            self.assertIn("data-theme='dark'", css[max(0, i - 60):i])
        self.assertEqual(css.count('[style*="--dk"]'), 2)

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


class TestPreservedTurns(unittest.TestCase):
    """보존 인용을 대화 턴으로 읽는다 (2026-08-06).

    mid-join 스레드는 **DB 메시지 수보다 실제 대화가 길다** — 내가 수신자가
    아니었던 앞부분이 인용 안에만 있다(실측: 스레드 3통, 대화 5턴). 화면은 그것을
    '이전 대화 (인용 보존)' 한 줄로 접어, 몇 턴이 누구 사이에 오갔는지도 안 보였다."""

    _MD = (PRESERVED_MARK + "\n"
           "**보낸 사람:** 오태양 책임 <taeyang.oh@corp.example>\n"
           "**보낸 날짜:** 2026년 08월 01일 Saturday PM 02:00\n"
           "**받는 사람:** mirae.kang@corp.example\n"
           "**제목:** RE: DMA 캐시 정합성\n\n"
           "드라이버 쪽 확인했습니다. sync_for_cpu 훅 호출은 UMD 책임입니다.\n"
           "--\n오태양 책임\n※ 본 메일은 기밀 정보를 포함할 수 있으며\n\n"
           "**보낸 사람:** 강미래 선임 <mirae.kang@corp.example>\n"
           "**보낸 날짜:** 2026년 07월 31일 Friday AM 10:00\n"
           "**제목:** DMA 캐시 정합성\n\n"
           "출력 텐서 캐시 미스매치가 간헐 재현됩니다.\n--\n강미래 선임\n")

    def test_turns_are_parsed_oldest_first_with_signatures_removed(self):
        from mailkb.clean import parse_preserved
        turns = parse_preserved("참조 추가드립니다.\n\n" + self._MD)
        self.assertEqual(len(turns), 2)
        self.assertEqual([t["who"] for t in turns], ["강미래 선임", "오태양 책임"])
        self.assertEqual([t["when"] for t in turns],
                         ["2026-07-31 10:00", "2026-08-01 14:00"])   # 오후 2시=14시
        self.assertEqual(turns[1]["addr"], "taeyang.oh@corp.example")
        self.assertIn("sync_for_cpu", turns[1]["body"])
        self.assertNotIn("기밀", turns[1]["body"])       # 서명·고지는 뗀다
        self.assertNotIn("오태양 책임\n", turns[1]["body"])

    def test_plain_and_english_headers_are_read_too(self):
        from mailkb.clean import parse_preserved
        en = (PRESERVED_MARK + "\nFrom: Kim <a@corp.example>\n"
              "Sent: 2026-08-01 2:00 PM\nSubject: RE: x\n\nhello there\n")
        got = parse_preserved(en)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["when"], "2026-08-01 14:00")   # 뒤에 붙는 PM 도 읽는다
        self.assertEqual(got[0]["body"], "hello there")

    def test_unreadable_block_falls_back_to_nothing(self):
        # 파싱 실패가 사용자에게 보이면 안 된다 — 빈 목록이면 호출측이 원문을 쓴다
        from mailkb.clean import parse_preserved, preserved_label
        self.assertEqual(parse_preserved("마커 없는 본문"), [])
        self.assertEqual(parse_preserved(PRESERVED_MARK + "\n헤더 없는 인용 덩어리"), [])
        self.assertEqual(preserved_label([]), "")

    def test_label_says_how_many_turns_between_whom(self):
        from mailkb.clean import parse_preserved, preserved_label
        label = preserved_label(parse_preserved(self._MD))
        self.assertEqual(label, "앞선 대화 2턴 — 강미래 선임 → 오태양 책임 · 07-31 ~ 08-01")

    def test_stored_html_fold_is_retitled_at_render_time(self):
        # 접힘은 수집 때 message_html 에 구워진다. 재수집을 강요하지 않으려면
        # 그릴 때 갈아 끼워야 한다(불변식 5).
        from mailkb.clean import QFOLD_CLOSE, QFOLD_OPEN, retitle_qfold
        html = "<p>본문</p>" + QFOLD_OPEN + "<p>인용</p>" + QFOLD_CLOSE
        out = retitle_qfold(html, "앞선 대화 2턴 — 가 → 나")
        self.assertIn("<summary>앞선 대화 2턴 — 가 → 나</summary>", out)
        self.assertNotIn("이전 대화 (인용 보존)", out)
        self.assertIn("<p>인용</p>", out)                    # 내용은 안 건드린다
        self.assertEqual(retitle_qfold(html, ""), html)      # 라벨 없으면 무변경
        self.assertEqual(retitle_qfold("<p>접힘 없음</p>", "x"), "<p>접힘 없음</p>")

    # 실제 파이프라인이 만드는 모양 — 수집이 인용 체인을 보존하며 마커를 붙인다
    _RAW = ("참조 추가드립니다.\n\n"
            "________________________________\n"
            "보낸 사람: 오태양 책임 <taeyang.oh@corp.example>\n"
            "보낸 날짜: 2026년 08월 01일 PM 02:00\n"
            "받는 사람: mirae.kang@corp.example\n"
            "제목: RE: DMA 캐시 정합성\n\n"
            "드라이버 쪽 확인했습니다. sync_for_cpu 훅 호출은 UMD 책임입니다.\n\n"
            "보낸 사람: 강미래 선임 <mirae.kang@corp.example>\n"
            "보낸 날짜: 2026년 07월 31일 AM 10:00\n"
            "제목: DMA 캐시 정합성\n\n"
            "출력 텐서 캐시 미스매치가 간헐 재현됩니다.\n")

    def test_thread_view_shows_turns_and_keeps_message_count(self):
        from mailkb import web
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        cfg = Config(home=home, my_addresses=[ME])
        store = Store(home / "t.sqlite", [ME], noise=cfg)
        self.addCleanup(store.close)
        store.ingest([_rec("mj", "mirae.kang@corp.example", [ME], "DMA 캐시 정합성",
                           "2026-08-02T09:00:00", body=self._RAW)])
        tid = store.message("1")["thread_id"]
        html = web.render_thread(store, cfg, tid)
        self.assertIn("앞선 대화 2턴", html)
        self.assertIn("class='qturn'", html)
        self.assertIn("인용에서", html)                       # 출처를 밝힌다
        self.assertIn("sync_for_cpu", html)
        # 복원 턴은 메시지가 아니다 — 링크·앵커를 주지 않는다
        turns_html = html.split("class='qfold'")[1]
        self.assertNotIn("href=", turns_html)
        self.assertNotIn("id='msg-", turns_html)
        # 목록의 통수는 그대로(1통) — 대화가 5턴이어도 메시지는 안 늘었다
        self.assertIn("[1통]", web.render_threads(store, cfg))


class TestRelationBadge(unittest.TestCase):
    """관계 배지 — 발신자와의 왕래 기록에서 나오는 사실만 (2026-08-06).

    노이즈 규칙은 주소·제목 문자열이라 설치마다 손으로 채워야 하는데, 왕래 수는
    사용자 행동 기록이라 설정 없이 개인화된다. **표시만 한다** — 정렬·필터·자동
    숨김에 쓰면 '내가 처음 답하는 사람'이 화면에서 사라진다."""

    def setUp(self):
        from mailkb import web
        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME])
        self.store = Store(self.home / "t.sqlite", [ME], noise=self.cfg)
        self.addCleanup(self.store.close)

    def _badge(self, **kw):
        base = {"p_from": 0, "p_to": 0, "p_first": "", "sent_on": "2026-08-06T09:00:00"}
        base.update(kw)
        return self.web._relation_badge(base)

    def test_one_way_needs_volume_and_zero_replies(self):
        self.assertIn("↩ 0", self._badge(p_from=3, p_to=0))
        self.assertNotIn("↩ 0", self._badge(p_from=3, p_to=1))   # 한 번이라도 답했으면 아니다
        self.assertNotIn("↩ 0", self._badge(p_from=2, p_to=0))   # 두 통은 아직 판단 못 한다

    def test_first_mail_is_judged_against_that_row_not_the_running_count(self):
        # from_count 는 '지금까지 누계'라 옛 메일에 그대로 쓰면 전부 첫 메일이 아니다
        when = "2026-08-06T09:00:00"
        self.assertIn("첫 메일", self._badge(p_from=9, p_first=when, sent_on=when))
        self.assertNotIn("첫 메일",
                         self._badge(p_from=9, p_first="2026-07-01T09:00:00", sent_on=when))

    def test_nothing_to_say_means_nothing_is_drawn(self):
        self.assertEqual(self._badge(p_from=5, p_to=4), "")
        self.assertEqual(self.web._relation_badge({"sender_name": "x"}), "")   # 조인 없는 행

    def test_lists_carry_the_badge_without_extra_queries(self):
        self.store.ingest(
            [_rec(f"n{i}", "bot@corp.example", [ME], f"알림 {i}",
                  f"2026-08-0{i}T09:00:00") for i in (1, 2, 3, 4)])
        n_before = self.store.db.total_changes
        out = self.web.render_mail(self.store, self.cfg)
        self.assertIn("↩ 0", out)
        self.assertEqual(out.count("class='rbadge first'"), 1)   # 가장 오래된 한 통만
        self.assertIn("↩ 0", self.web.render_threads(self.store, self.cfg))
        self.assertEqual(self.store.db.total_changes, n_before)   # 렌더는 쓰지 않는다


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

    def test_pre_in_table_cell_does_not_break_table(self):
        # 표 셀 안 <pre>(코드)는 멀티라인 펜스 대신 인라인(줄=<br>)으로 → 표 행이
        # 한 줄로 유지되어 표가 깨지거나 뒤 내용이 코드박스로 새지 않는다.
        from mailkb.clean import html_to_markdown
        from mailkb.web import _mail_md_to_html
        md = html_to_markdown(
            "<table><tr><th>설명</th><th>예시</th></tr>"
            "<tr><td>함수</td><td><pre>a = 1\nb = 2</pre></td></tr>"
            "<tr><td>다음</td><td>끝</td></tr></table>"
            "<pre>바깥\n코드</pre>")
        row = [ln for ln in md.splitlines() if ln.startswith("| 함수")][0]
        self.assertIn("<br>", row)                  # 코드 줄 구분
        self.assertNotIn("```", row)                # 셀 안엔 펜스 없음(행 안 깨짐)
        self.assertIn("```\n바깥\n코드\n```", md)     # 표 밖 <pre> 는 종전대로 펜스
        out = _mail_md_to_html(md)
        self.assertIn("<table class='md-table'>", out)
        self.assertIn("<code>a = 1</code>", out)
        self.assertIn("<code>b = 2</code>", out)
        self.assertIn("<td>끝</td>", out)            # 뒤 내용이 코드박스로 안 샘
        self.assertEqual(out.count("md-code"), 1)    # 표 밖 펜스 1개만 코드박스

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

    def test_hidden_thread_is_not_summarized(self):
        # 숨긴 스레드는 요약 대상이 아니다 — threads_active_between 이 hidden 을
        # 모르므로 여기(조립 직전)서 걸러야 원문이 AI 로 안 나간다(2026-08-02).
        self.store.ingest([
            MailRecord(message_id="<hd1@t>", subject="숨긴 건",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on="2026-07-04T09:00:00",
                       body_text="이 내용은 AI 에 실리면 안 됩니다."),
        ])
        tid = self._tid("숨긴 건")
        self.store.hide_thread(tid, True)
        with mock.patch("mailkb.review.ai_run", return_value="요약됨") as m:
            result = review.update_rolling_summaries(
                self.store, self.cfg, [tid], backend=None)
        self.assertNotIn(tid, result)
        m.assert_not_called()
        # 마커도 안 전진 — 나중에 숨김을 풀면 그때 밀린 만큼 한 번에 요약된다
        self.assertEqual(self.store.thread(tid)["summary_msg_count"], 0)

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

    def test_recent_delta_reads_the_section_name_the_file_actually_uses(self):
        # 모델 출력의 절 이름은 '## 오늘 델타'지만 **렌더된 파일**은
        # '## 오늘 확정·변경 (N건)'이다. 정규식이 옛 이름을 들고 있어서
        # "이미 보고한 것 반복 금지" 재료가 한 번도 프롬프트에 실린 적이 없다
        # (2026-08-06 발견). 옛 이름 파일도 계속 읽힌다.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(home=Path(tmp.name), my_addresses=[ME])
        d = cfg.vault / "daily"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-07-21.md").write_text(
            "# 2026-07-21 일간 회고\n\n수신 3 · 발신 1\n\n"
            "## 오늘 확정·변경 (1건)\n- [#34] ECN B안 확정\n\n"
            "## 참고\n- 내가 보낸 것 (0건)\n", encoding="utf-8")
        self.assertIn("[#34] ECN B안 확정", distill._recent_delta(cfg, "2026-07-22"))
        (d / "2026-07-21.md").write_text(
            "# d\n\n## 오늘 델타\n- [#9] 옛 형식\n", encoding="utf-8")
        self.assertIn("[#9] 옛 형식", distill._recent_delta(cfg, "2026-07-22"))

    def test_unanswered_semantics_survive_home_removal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        cfg = Config(home=Path(tmp.name), my_addresses=[ME],
                     ignore_senders=["noreply"], internal_domains=["corp.example"])
        today = date.today()
        when = (today - timedelta(days=2)).isoformat()
        store.ingest([
            MailRecord(message_id="<a@t>", subject="검토 요청",
                       sender_name="kim", sender_addr="kim@corp.example",
                       to=[ME], sent_on=f"{when}T09:00:00", body_text="확인 부탁"),
            # 스팸(외부) → 미답변에서 제외되어야
            MailRecord(message_id="<b@t>", subject="특가",
                       sender_name="ad", sender_addr="promo@spam.example",
                       to=[ME], sent_on=f"{when}T09:10:00", body_text="세일"),
        ])
        # 홈 대시보드 제거(2026-07-26) 후에도 데이터 계층의 의미는 유지된다
        missed = review.filtered_unanswered(store, cfg)
        self.assertEqual(len(missed), 1)           # 스팸 제외
        self.assertEqual(store.decision_counts().get("confirmed", 0), 0)

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
        # 판정은 계산만(주간 보고 재료) — 상세 화면 분석 줄엔 내지 않는다
        # (신호 노출 폐지, 2026-07-30. 칩→줄→완전 비노출 순으로 축소)
        self.assertEqual(d["act"].level, "required")
        self.assertTrue(d["act"].has_deadline)
        joined = "\n".join(d["analysis"])
        self.assertNotIn("판정:", joined)
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
        # 하루 이틀 건너뛰어도 소급 창이 건너뛴 날의 결정까지 수확한다.
        # 이 클래스는 summary_max_days=3 으로 설정했다(기본은 1 — 오늘만).
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
                           "decider": "김민수", "rationale": "비용"},
                          {"thread_id": self.tid + 100, "title": "C안 보류",
                           "decider": "", "rationale": ""}],
            "person": [{"who": "김민수", "signal": "ECN 담당",
                        "thread_id": self.tid}],
            "project": [{"thread_id": self.tid, "signal": "대기 → 완료"}],
            "dropped": 0,
        }
        md = review.render(det)
        # 델타+제안 병합 — 델타가 이미 언급한 스레드(#tid)의 제안은 별도 줄 없음,
        # 안 언급된 제안(C안)만 항목으로. 말미에 반영 대기 안내.
        self.assertIn("## 오늘 확정·변경 (2건)", md)
        self.assertIn(f"ECN 확정 #{self.tid}", md)
        self.assertNotIn("B안 채택", md)
        self.assertIn(f"[#{self.tid + 100}] C안 보류", md)
        self.assertIn("※ 장기기억 반영 대기 2건", md)
        # 인물·프로젝트 신호는 '참고'로
        self.assertIn("- 인물: 김민수 — ECN 담당", md)
        self.assertIn(f"- 프로젝트: [#{self.tid}] 대기 → 완료", md)
        # 수확 없으면(비-AI 데일리) 섹션 자체가 없음
        det.pop("harvest")
        md2 = review.render(det)
        self.assertNotIn("오늘 확정·변경", md2)
        self.assertNotIn("장기기억", md2)

    def test_render_uses_unified_korean_name(self):
        # 표시 명칭은 한글로 통일했다(웹 = '일간 회고'). CLI 출력과 vault 마크다운만
        # 옛 이름으로 남아 문서와 어긋나던 것을 맞춘다 — 웹은 md 의 H1 을 버리므로
        # 화면에는 영향이 없다.
        md = review.render(review.deterministic(self.store, self.cfg, "2026-07-20"))
        self.assertIn("일간 회고", md.splitlines()[0])
        self.assertNotIn("데일리 리뷰", md)


class TestDailyRender(unittest.TestCase):
    """데일리 재구성(2026-07-17) — 머리 요약·할 일 평탄화·교차 중복 제거·참고."""

    @staticmethod
    def _det(**kw):
        base = {
            "date": "2026-07-17", "sent": [], "received_count": 3,
            "unanswered": [], "deadlines": [], "intervention": [],
            "intervention_candidates": [],
            "digest": {"work": [], "n_spam": 0, "n_notice": 0},
            "closed_by_me": [],
        }
        base.update(kw)
        return base

    def test_head_stat_line_without_ai(self):
        # 머리줄은 부피만 — '할 일 N/최우선'은 신호 노출 폐지(2026-07-30)로 제거
        md = review.render(self._det(intervention=[
            {"category": "respond", "thread_id": 7, "who": "김", "subject": "회신건",
             "days": 1, "personal": True, "tag": "⏰", "snippet": "부탁드립니다"}]))
        self.assertEqual(md.splitlines()[2], "수신 3 · 발신 0")
        self.assertNotIn("최우선", md)

    def test_exec_summary_is_its_own_section(self):
        # 2026-08-01 개편: 부피 한 줄은 남기고, AI 한 문단은 머리글로 독립시킨다.
        # 절 제목은 주간과 같은 'Executive Summary'(사용자 확정).
        md = review.render(self._det(exec_summary="오늘은 조용했다 (#3).",
                                     exec_state="ok"))
        self.assertIn("## Executive Summary", md)
        self.assertIn("오늘은 조용했다 (#3).", md)
        self.assertIn("수신 3 · 발신 0", md)          # 부피 줄은 유지

    def test_exec_summary_section_is_absent_without_ai(self):
        # 기본 일간은 ai=False 로 자동 생성된다 — 절을 내면 **매일** 첫 줄이
        # '없음'이 된다. AI 회고 버튼은 이미 화면에 있다(2026-08-01 사용자 확정).
        md = review.render(self._det())
        self.assertNotIn("Executive Summary", md)
        self.assertNotIn("AI 요약 없음", md)

    def test_empty_summary_says_why_it_is_empty(self):
        # 넷을 한 문장으로 뭉개면 도구 탓처럼 읽힌다. 특히 **실패를 '특이사항
        # 없음'이라 말하면 거짓**이다(2026-08-01 사용자 확정).
        self.assertIn("- 특이사항 없음", review.render(self._det(exec_state="none")))
        got = review.render(self._det(exec_state="failed"))
        self.assertIn("받지 못했습니다", got)
        self.assertNotIn("특이사항 없음", got)

    def test_todo_section_removed_and_stalled_demoted(self):
        # '지금 할 일'(🔴결정/🟠회신)은 2026-07-30 제거 — 정규식 판정의 정밀도가
        # 낮아 신뢰를 깎았다. 시간 기반 사실인 정체 2종만 '참고'에 마커 없이 남는다.
        md = review.render(self._det(intervention=[
            {"category": "decide", "thread_id": 5, "who": "박", "subject": "결정건",
             "days": 2, "personal": True, "tag": "", "snippet": "승인 부탁드립니다"},
            {"category": "respond", "thread_id": 6, "who": "이", "subject": "회신건",
             "days": 0, "personal": False, "tag": "⏰", "snippet": "회신 부탁드립니다"},
            {"category": "stalled_mine", "thread_id": 7, "who": "정",
             "subject": "내공건", "days": 3, "personal": False, "tag": "",
             "snippet": ""},
            {"category": "stalled_thread", "thread_id": 8, "who": "최",
             "subject": "정체건", "days": 4, "personal": False, "tag": "",
             "snippet": "무응답"},
        ]))
        self.assertNotIn("## 지금 할 일", md)
        for gone in ("🔴결정", "🟠회신", "🟡정체", "⚪정체", "승인 부탁드립니다"):
            self.assertNotIn(gone, md)
        ref = md.split("## 참고")[1]
        self.assertIn("- 오래 멈춘 스레드 (2건)", ref)
        self.assertIn("  - [#7] 정: 내공건 — 영업 3d", ref)
        self.assertIn("  - [#8] 최: 정체건 — 영업 4d", ref)
        self.assertNotIn("[#5]", ref)                        # 결정/회신은 어디에도

    def test_flow_dedups_todo_changes_and_closed(self):
        md = review.render(self._det(
            intervention=[{"category": "respond", "thread_id": 1, "who": "김",
                           "subject": "A", "days": 0, "personal": False,
                           "tag": "", "snippet": ""}],
            harvest={"delta": ["B안 확정 (#2)"], "decisions": [],
                     "person": [], "project": []},
            closed_by_me=[{"thread_id": 4, "subject": "D"}],
            digest={"work": [
                {"thread_id": 1, "subject": "A", "who": "김", "is_sent": False,
                 "lead": "a", "ai_core": ""},
                {"thread_id": 2, "subject": "B", "who": "박", "is_sent": False,
                 "lead": "b", "ai_core": ""},
                {"thread_id": 3, "subject": "C", "who": "이", "is_sent": False,
                 "lead": "c", "ai_core": ""},
                {"thread_id": 4, "subject": "D", "who": "최", "is_sent": True,
                 "lead": "d", "ai_core": ""},
            ], "n_spam": 1, "n_notice": 2}))
        # 확정(#2)·오늘 종결(#4)만 흐름에서 제외 — '지금 할 일' 폐지(2026-07-30)
        # 후 #1 은 더는 위에서 다뤄지지 않으므로 흐름에 나와야 정보가 안 사라진다.
        self.assertIn("## 오늘 흐름 (그 외 2건)", md)
        self.assertIn("[#1] A (김) — a", md)
        self.assertIn("[#3] C (이) — c", md)
        flow = md.split("## 오늘 흐름")[1].split("## 참고")[0]
        for ref in ("#2", "#4"):
            self.assertNotIn(ref, flow)
        self.assertIn("- 수신 3건 · 공지 2 · 노이즈 1 처리됨", md)

    def test_reference_deadlines_all_listed(self):
        # '지금 할 일' 폐지(2026-07-30) 후 기한 신호는 전부 참고에 나온다 —
        # 위에서 ⏰ 로 중복 표시할 곳이 사라졌으니 여기서 빠지면 정보 유실이다.
        md = review.render(self._det(
            intervention=[{"category": "respond", "thread_id": 1, "who": "김",
                           "subject": "A", "days": 0, "personal": False,
                           "tag": "⏰", "snippet": ""}],
            deadlines=[(1, "A", "내일까지 회신"), (9, "공지", "금일 18시까지")]))
        ref = md.split("## 참고")[1]
        self.assertIn("- 기한: [#9] 공지 — 「금일 18시까지」", ref)
        self.assertIn("- 기한: [#1] A — 「내일까지 회신」", ref)

    def test_reference_sent_and_closed(self):
        md = review.render(self._det(
            sent=[{"sent_on": "2026-07-17T09:12:00", "subject": "RE: A",
                   "to_addrs": "kim@x"}],
            closed_by_me=[{"thread_id": 4, "subject": "D요청"}]))
        self.assertIn("- 내가 보낸 것 (1건)", md)
        self.assertIn("  - 09:12 RE: A → kim@x", md)
        self.assertIn("- 내 회신으로 종결된 요청 (1건): [#4] D요청", md)


class TestDailyAiLayerSurvives(unittest.TestCase):
    """AI 회고 산출은 결정론 재생성에서 살아남아야 한다 (2026-08-06 사용자 신고).

    데일리 md 는 재생성마다 통째로 덮어써진다. 웹은 새 메일·서버 재시작이면
    결정론 회고를 배경에서 다시 만드는데(ai=False), 그때 사용자가 돈 주고 받은
    `## Executive Summary` 가 파일에서 사라졌다 — 실측 재현: AI 회고 → 서버
    재시작 → 홈 1회 조회 → 절 증발. 통계의 '기억 커버리지'가 그 절 유무로 날을
    세므로(report._AI_DAILY_MARKS) 지난 기록까지 함께 거짓이 됐다."""

    DAY = "2026-07-22"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lambda: shutil.rmtree(self.tmp.name, ignore_errors=True))
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"])
        self.store = Store(self.home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(self.store.close)
        self.store.ingest([
            _rec("s0", "kim@corp.example", [ME], "협상", "2026-07-20T09:00:00",
                 body="검토 부탁드립니다."),
        ])

    def _write_daily(self, text):
        d = self.cfg.vault / "daily"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.DAY}.md").write_text(text, encoding="utf-8")

    def test_saved_summary_comes_back_into_a_deterministic_render(self):
        review.save_ai_layer(self.store, self.DAY,
                             {"exec_summary": "NPX 양자화가 확정됐다 (#3).",
                              "exec_state": "ok"})
        det = review.deterministic(self.store, self.cfg, self.DAY)
        md = review.render(det)                       # ai=False 경로와 같은 렌더
        self.assertIn("## Executive Summary", md)
        self.assertIn("NPX 양자화가 확정됐다 (#3).", md)

    def test_a_day_without_ai_still_has_no_section(self):
        # 2026-08-01 확정("AI 를 안 돌렸으면 절 자체를 내지 않는다")을 깨지 않는다
        det = review.deterministic(self.store, self.cfg, self.DAY)
        self.assertNotIn("Executive Summary", review.render(det))

    def test_harvest_and_ai_core_survive_too(self):
        # 사라진 것은 머리글만이 아니다 — '오늘 확정·변경'(수확)과 흐름 줄의
        # AI 한 줄(ai_core)도 같은 덮어쓰기로 함께 날아갔다.
        review.save_ai_layer(self.store, self.DAY, {
            "exec_state": "ok", "exec_summary": "한 줄",
            "harvest": {"delta": ["[#1] ECN B안 확정"], "decisions": [],
                        "person": [], "project": []},
            "digest": {"work": [{"thread_id": 1, "ai_core": "AI 가 쓴 한 줄"}]},
        })
        md = review.render(review.deterministic(self.store, self.cfg, self.DAY))
        self.assertIn("## 오늘 확정·변경 (1건)", md)
        self.assertIn("[#1] ECN B안 확정", md)
        # ai_core 는 그날 흐름에 아직 남아 있는 스레드에만 다시 붙는다
        det = {"date": self.DAY, "digest": {"work": [{"thread_id": 1, "lead": "x"},
                                                     {"thread_id": 9, "lead": "y"}]}}
        review.restore_ai_layer(self.store, self.cfg, self.DAY, det)
        self.assertEqual(det["digest"]["work"][0]["ai_core"], "AI 가 쓴 한 줄")
        self.assertNotIn("ai_core", det["digest"]["work"][1])

    def test_a_file_written_before_this_store_existed_is_rescued(self):
        # 이 보관 장치 이전에 돌린 회고는 파일에만 남아 있다. 업그레이드 직후의
        # 첫 결정론 재생성이 그것을 지우면 사용자 입장에선 결함이 그대로다.
        self._write_daily("# 회고\n\n수신 1 · 발신 0\n\n"
                          "## Executive Summary\n지난주 결정이 오늘 확정됐다.\n\n"
                          "## 참고\n- 내가 보낸 것 (0건)\n")
        det = review.deterministic(self.store, self.cfg, self.DAY)
        self.assertIn("지난주 결정이 오늘 확정됐다.", review.render(det))

    def test_rescue_keeps_empty_reasons_apart(self):
        # '특이사항 없음'을 AI 요약 본문으로 되살리면 실패·미실행 구분이 무너진다
        self._write_daily("# 회고\n\n## Executive Summary\n- 특이사항 없음\n")
        det = review.deterministic(self.store, self.cfg, self.DAY)
        self.assertEqual(det["exec_state"], "none")
        self.assertEqual(det["exec_summary"], "")

    def test_a_second_harvest_adds_instead_of_replacing(self):
        # 같은 날 두 번째 수확은 새 메일분만 돌아온다(last_harvest 워터마크).
        # 그것으로 덮으면 '오늘 확정·변경'이 통째로 사라진다 — 사용자가 AI 회고를
        # 한 번 더 눌렀다는 이유로 오늘 확정된 것이 화면에서 없어졌다.
        old = {"delta": ["[#1] 하나"], "decisions": [{"thread_id": 1, "title": "A"}],
               "person": [], "project": [], "dropped": 1}
        self.assertIs(review._merge_harvest(old, None), old)   # 재수확 0건
        merged = review._merge_harvest(old, {
            "delta": ["[#1] 하나", "[#2] 둘"],                 # 첫 줄은 중복
            "decisions": [{"thread_id": 1, "title": "A"},
                          {"thread_id": 2, "title": "B"}],
            "person": [], "project": [], "dropped": 2})
        self.assertEqual(merged["delta"], ["[#1] 하나", "[#2] 둘"])
        self.assertEqual([d["title"] for d in merged["decisions"]], ["A", "B"])
        self.assertEqual(merged["dropped"], 3)
        self.assertEqual(old["delta"], ["[#1] 하나"])          # 원본은 안 건드린다

    def test_a_failed_rerun_does_not_erase_the_summary_already_paid_for(self):
        review.save_ai_layer(self.store, self.DAY,
                             {"exec_summary": "이미 받아 둔 요약", "exec_state": "ok"})
        det = review.deterministic(self.store, self.cfg, self.DAY)
        with mock.patch.object(review, "refresh_summaries"), \
             mock.patch.object(distill, "harvest", return_value=None), \
             mock.patch.object(review, "ai_digest", side_effect=lambda s, c, d, **k: d), \
             mock.patch.object(review, "ai_exec_summary", return_value=("", "failed")):
            review.run_ai_layer(self.store, self.cfg, det, persist_date=self.DAY)
        self.assertIn("이미 받아 둔 요약", review.render(det))
        self.assertEqual(review.load_ai_layer(self.store, self.DAY)["exec_state"], "ok")

    def test_nothing_is_stored_when_there_is_nothing_to_store(self):
        review.save_ai_layer(self.store, self.DAY, {"digest": {"work": []}})
        self.assertEqual(review.load_ai_layer(self.store, self.DAY), {})


class TestPeopleDossier(unittest.TestCase):
    """인물 도시에 v1 — 교류 강도 순위·5카드·동명이인 스코프."""

    KIM, LEE, JIRA = "kim@corp.example", "lee@corp.example", "jira@corp.example"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"], ignore_senders=["jira@"])
        self.store = Store(self.home / "p.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.store.ingest([
            # kim ↔ 나 : 받은 2 / 보낸 1 (마지막이 kim 요청 → 미결)
            _rec("k1", self.KIM, [ME], "설계 검토", "2026-07-01T09:00:00", "검토 부탁드립니다."),
            _rec("k2", ME, [self.KIM], "RE: 설계 검토", "2026-07-01T13:00:00", "의견 드립니다."),
            _rec("k3", self.KIM, [ME], "RE: 설계 검토", "2026-07-02T09:00:00", "추가 검토 부탁드립니다."),
            # lee ↔ 나 : 받은 1 / 보낸 2 (마지막이 내 감사 → 미결 아님)
            _rec("l1", ME, [self.LEE], "일정 문의", "2026-07-03T09:00:00", "일정 알려주세요."),
            _rec("l2", self.LEE, [ME], "RE: 일정 문의", "2026-07-03T15:00:00", "확인했습니다."),
            _rec("l3", ME, [self.LEE], "RE: 일정 문의", "2026-07-04T09:00:00", "감사합니다."),
            # jira : 노이즈 (받은 3)
            _rec("j1", self.JIRA, [ME], "[JIRA] NPX-1", "2026-07-02T10:00:00", "이슈 갱신."),
            _rec("j2", self.JIRA, [ME], "[JIRA] NPX-2", "2026-07-03T10:00:00", "이슈 갱신."),
            _rec("j3", self.JIRA, [ME], "[JIRA] NPX-3", "2026-07-04T10:00:00", "이슈 갱신."),
        ])
        self.ktid = self._tid("k1")
        self.ltid = self._tid("l1")

    def _tid(self, mid):
        return self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id=?",
            (f"<{mid}@t>",)).fetchone()[0]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_window_counts_excludes_me(self):
        counts = {c["addr"]: c for c in self.store.person_window_counts()}
        self.assertNotIn(ME, counts)                      # 내 주소 제외
        self.assertEqual((counts[self.KIM]["recv"], counts[self.KIM]["sent"]), (2, 1))
        self.assertEqual((counts[self.LEE]["recv"], counts[self.LEE]["sent"]), (1, 2))

    def test_rank_excludes_noise_and_orders_by_intensity(self):
        addrs = [r["addr"] for r in report.rank_people(self.store, self.cfg)]
        self.assertNotIn(self.JIRA, addrs)                # 노이즈 발신 제외
        self.assertNotIn(ME, addrs)
        # kim(2+1*0.5=2.5) > lee(1+2*0.5=2.0)
        self.assertEqual(addrs, [self.KIM, self.LEE])

    def test_intensity_is_a_separable_pure_function(self):
        self.assertEqual(report._intensity(2, 1, "", ""), 2.5)
        # 수신 위주(사용자 지정) — 같은 총량이면 받은 쪽이 높다
        self.assertGreater(report._intensity(3, 0, "", ""),
                           report._intensity(0, 3, "", ""))

    def test_landing_lists_ordered_with_badge(self):
        page = web.render_people_page(self.store, self.cfg)
        self.assertIn("<h1>인물</h1>", page)
        self.assertIn("/people?addr=kim%40corp.example", page)
        self.assertNotIn("jira@corp.example", page)       # 노이즈 미표시
        self.assertLess(page.index("kim%40corp"), page.index("lee%40corp"))
        # '미결 N' 배지는 REQUIRED 판정 재노출이라 제거(신호 노출 폐지, 2026-07-30)
        self.assertNotIn("pbadge", page)
        self.assertNotIn("미결", page)

    def test_dossier_relationship_card(self):
        d = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("관계 수치", d)
        self.assertIn("받은 2", d)
        self.assertIn("보낸 1", d)
        self.assertNotIn("서로의 미결", d)                # 제거된 섹션

    def test_decisions_and_signals_scoped_by_participation(self):
        # 동명이인 방지: 이름이 같아도 이 사람 참여 스레드의 것만
        self.store.add_decision(self.ktid, "2026-07-02", "B안 채택",
                                decider="kim", status="confirmed")
        self.store.add_decision(self.ltid, "2026-07-03", "남의 결정",
                                decider="kim", status="confirmed")   # kim 미참여
        self.store.add_signal("2026-07-02", "person", "kim", self.ktid, "ECN 담당 이관")
        self.store.add_signal("2026-07-03", "person", "kim", self.ltid, "남의 신호")
        dec = {r["title"] for r in self.store.person_decisions(self.KIM, "kim")}
        sig = {r["signal"] for r in self.store.person_signals(self.KIM, "kim")}
        self.assertEqual(dec, {"B안 채택"})
        self.assertEqual(sig, {"ECN 담당 이관"})
        # 도시에에 두 카드 노출 + 근거
        d = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("관여한 결정", d)
        self.assertIn("B안 채택", d)
        self.assertIn("최근 변화", d)
        self.assertIn("ECN 담당 이관", d)

    def test_empty_cards_not_drawn(self):
        d = web.render_dossier(self.store, self.cfg, self.LEE)
        self.assertNotIn("관여한 결정", d)                # lee 결정 없음
        self.assertNotIn("최근 변화", d)                  # lee 신호 없음

    def test_route_people_landing_and_dossier(self):
        page = web.render_people_page(self.store, self.cfg)
        self.assertIn("교류 강도순", page)
        # nav 에 '인물' 링크
        self.assertIn('href="/people"', web._NAV)

    def test_relmetrics_card_visualized(self):
        # 관계 수치 카드 = 균형 막대(스와치·세그먼트) + 회신 비교(rsfill 막대)
        d = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("class='relbar'", d)                # 균형 막대
        self.assertIn("class='sw recv'", d)               # 색 스와치
        self.assertIn("class='sw sent'", d)
        self.assertIn("받은 2", d)
        self.assertIn("보낸 1", d)
        self.assertIn("class='rsfill'", d)                # 회신 속도 막대
        self.assertIn("이 사람", d)
        self.assertIn("최근 6개월", d)                    # 기간 캡션
        # 짧은 기간(한 주) → 스파크라인 생략(graceful)
        self.assertNotIn("class='rspark'", d)


class TestRelMetricsViz(unittest.TestCase):
    """관계 수치 시각화 — 주별 시계열·스파크라인·graceful 생략."""

    AMY = "amy@corp.example"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["나"], internal_domains=["corp.example"])
        self.store = Store(Path(self.tmp.name) / "r.sqlite", [ME], ["나"],
                           noise=self.cfg)
        # 5개 서로 다른 주에 걸친 교신 (수신 5 / 발신 2)
        self.store.ingest([
            _rec("a1", self.AMY, [ME], "논의", "2026-06-08T09:00:00", "검토 부탁."),
            _rec("a2", self.AMY, [ME], "논의", "2026-06-15T09:00:00", "추가 검토."),
            _rec("a3", ME, [self.AMY], "RE: 논의", "2026-06-16T09:00:00", "의견 드립니다."),
            _rec("a4", self.AMY, [ME], "논의", "2026-06-22T09:00:00", "재확인 부탁."),
            _rec("a5", self.AMY, [ME], "논의", "2026-06-29T09:00:00", "일정 공유."),
            _rec("a6", ME, [self.AMY], "RE: 논의", "2026-06-30T09:00:00", "확인했습니다."),
            _rec("a7", self.AMY, [ME], "논의", "2026-07-06T09:00:00", "마무리 요청."),
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_weekly_series_lengths_and_sums(self):
        m = report.person_metrics(self.store, self.cfg, self.AMY, weeks=13)
        self.assertEqual(len(m["recv_series"]), m["weeks"])
        self.assertEqual(len(m["sent_series"]), m["weeks"])
        # 창 안 데이터라 주별 합 = 스칼라 총량
        self.assertEqual(sum(m["recv_series"]), m["recv"])
        self.assertEqual(sum(m["sent_series"]), m["sent"])
        self.assertEqual(m["recv"], 5)
        self.assertEqual(m["sent"], 2)

    def test_absent_addr_series_all_zero(self):
        m = report.person_metrics(self.store, self.cfg, "nobody@corp.example")
        self.assertEqual(sum(m["recv_series"]) + sum(m["sent_series"]), 0)

    def test_sparkline_shown_across_weeks(self):
        d = web.render_dossier(self.store, self.cfg, self.AMY)
        self.assertIn("class='rspark'", d)                # 3주+ → 스파크 표시
        self.assertIn("<polyline", d)
        self.assertIn("주별 교신", d)

    def test_spark_svg_empty_when_flat_or_short(self):
        self.assertEqual(web._spark_svg([0, 0, 0, 0]), "")   # 전부 0
        self.assertEqual(web._spark_svg([3, 1]), "")         # 3주 미만
        self.assertIn("<polyline", web._spark_svg([1, 2, 3, 2]))


class TestAutoReview(unittest.TestCase):
    """결정론 데일리 리뷰 lazy-on-view 자동 갱신 — 트리거·기준선 가드."""

    DAY = "2026-07-04"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["나"], internal_domains=["corp.example"])
        (self.cfg.vault / "daily").mkdir(parents=True, exist_ok=True)
        web._auto_review_basis.clear()
        self.calls = []
        self._orig = web._start_review
        # 실제 스레드 대신 호출만 기록(성공 반환)
        web._start_review = lambda cfg, ai, day: (self.calls.append((ai, day)) or True)

    def tearDown(self):
        web._start_review = self._orig
        web._auto_review_basis.clear()
        self.tmp.cleanup()

    def _write_daily(self):
        (self.cfg.vault / "daily" / f"{self.DAY}.md").write_text("x", encoding="utf-8")

    def test_triggers_deterministic_when_missing(self):
        web._maybe_auto_review(self.cfg, self.DAY, 5)
        self.assertEqual(self.calls, [(False, self.DAY)])     # AI 없이(결정론)

    def test_noop_when_same_basis_and_file_exists(self):
        self._write_daily()
        web._maybe_auto_review(self.cfg, self.DAY, 5)          # 최초 → 기준선 기록
        self.calls.clear()
        web._maybe_auto_review(self.cfg, self.DAY, 5)          # 동일 기준선 → no-op
        self.assertEqual(self.calls, [])

    def test_retriggers_on_new_mail(self):
        self._write_daily()
        web._maybe_auto_review(self.cfg, self.DAY, 5)          # 기준선 5
        self.calls.clear()
        web._maybe_auto_review(self.cfg, self.DAY, 9)          # 새 메일 → 기준선 변화
        self.assertEqual(self.calls, [(False, self.DAY)])

    def test_retries_when_file_missing_even_if_basis_recorded(self):
        # 지난 생성이 실패해 파일이 없으면 같은 기준선이라도 다시 시도
        web._maybe_auto_review(self.cfg, self.DAY, 5)          # 기록되지만 파일 미생성
        self.calls.clear()
        web._maybe_auto_review(self.cfg, self.DAY, 5)
        self.assertEqual(self.calls, [(False, self.DAY)])


class TestHiddenAIExclusion(unittest.TestCase):
    """숨긴 스레드는 AI 프롬프트 재료에서 빠진다 (2026-08-02 신설 사양).

    숨김은 '조용히'라는 뜻인데 종전에는 weekly.collect 만 걸렀다 — 롤링 요약·
    수확·분석·AI 검색·인물 요약이 숨긴 원문을 그대로 실었다. 예외는 사용자가
    그 스레드를 직접 지목한 온디맨드 분석([분석] 버튼) 하나뿐이다."""

    KIM = "kim@corp.example"

    def setUp(self):
        from mailkb import ask
        self.ask = ask
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.cfg = Config(home=home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.store.ingest([
            _rec("h1", self.KIM, [ME], "연봉 협상", "2026-07-10T09:00:00",
                 body="연봉 조정안을 검토 부탁드립니다. 세부 수치는 첨부 표 참조."),
            _rec("v1", self.KIM, [ME], "양자화 결정", "2026-07-11T09:00:00",
                 body="per-channel 로 확정합니다."),
        ])
        rows = {r["subject"]: r for r in self.store.db.execute(
            "SELECT id, subject, thread_id FROM messages")}
        self.hid_mid = rows["연봉 협상"]["id"]
        self.hid_tid = rows["연봉 협상"]["thread_id"]
        self.vis_mid = rows["양자화 결정"]["id"]
        self.vis_tid = rows["양자화 결정"]["thread_id"]
        self.store.hide_thread(self.hid_tid, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hidden_thread_ids_helper(self):
        self.assertEqual(self.store.hidden_thread_ids(),
                         frozenset({self.hid_tid}))
        self.store.hide_thread(self.hid_tid, False)
        self.assertEqual(self.store.hidden_thread_ids(), frozenset())

    def test_search_seed_read_expand_all_skip_hidden(self):
        deny = self.store.hidden_thread_ids()
        hits: dict = {}
        self.ask._search(self.store, self.cfg, "연봉", hits, deny=deny)
        self.assertEqual(hits, {})
        self.ask._seed(self.store, self.cfg, [self.hid_mid], hits, deny=deny)
        self.assertEqual(hits, {})
        read: dict = {}
        # _read 는 최종 방어선 — 이어 묻기의 부모 read_ids(숨기기 전 캐시)도 여길 지난다
        self.ask._read(self.store, [self.hid_mid], hits, read, deny=deny)
        self.assertEqual(read, {})
        # 스레드 전개는 노이즈 필터마저 우회하는 직조회 경로 — deny 가 유일한 문
        self.assertEqual(
            self.ask._thread_evidence_ids(self.store, [self.hid_tid], {},
                                          deny=deny), [])

    def test_ask_prompt_never_contains_hidden_content(self):
        prompts = []
        replies = iter([
            json.dumps({"action": "search", "queries": ["연봉"]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "근거 부족", "answer": "관련 메일이 없습니다",
                        "claims": []}),
        ])

        def fake(cmd, prompt, **kw):
            prompts.append(prompt)
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            res = self.ask.ask(self.store, self.cfg, "연봉 검토 상태?",
                               today="2026-07-14")
        joined = "\n".join(prompts)
        self.assertNotIn("연봉 조정안", joined)     # 본문이 어느 콜에도 없다
        self.assertNotIn("연봉 협상", joined)       # 제목(훑기 목록)도 없다
        self.assertEqual(res["state"], "근거 부족")

    def test_analyze_mail_allows_target_hidden_thread(self):
        # 사용자가 숨긴 스레드의 메일에서 [분석]을 눌렀다 — 명시 의도가 우선이라
        # 그 스레드는 조사되고, 인용 검증까지 통과해야 한다.
        replies = iter([
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "연봉 조정안 검토 요청입니다.",
                        "claims": [{"text": "검토 요청", "mid": self.hid_mid,
                                    "quote": "연봉 조정안을 검토 부탁드립니다"}]}),
        ])

        def fake(cmd, prompt, **kw):
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"], "answer_supported": True})
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            res = self.ask.analyze_mail(self.store, self.cfg, self.hid_mid,
                                        today="2026-07-14")
        self.assertEqual(res["state"], "확인됨")
        self.assertEqual(res["claims"][0]["mid"], self.hid_mid)

    def test_ai_search_pool_skips_hidden(self):
        prompts = []
        replies = iter([
            '{"dsl": "연봉", "fallback_dsl": "", "note": "1차"}',
            '{"dsl": "연봉 조정", "fallback_dsl": "", "note": "재시도"}',
        ])

        def fake(cmd, prompt, **kw):
            prompts.append(prompt)
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            res = review.ai_search(self.store, self.cfg, "연봉", "2026-07-14")
        self.assertEqual(res["items"], [])          # 숨긴 스레드는 후보조차 아니다
        self.assertNotIn("연봉 조정안", "\n".join(prompts))

    def test_harvest_items_skip_hidden(self):
        blocks, _ = distill._harvest_items(
            self.store, self.cfg, "2026-07-10", "2026-07-11", "")
        self.assertNotIn("연봉", blocks)
        self.assertIn("양자화", blocks)

    def test_digest_prompt_skips_hidden_but_display_keeps_it(self):
        digest = {"work": [
            {"thread_id": self.hid_tid, "subject": "연봉 협상", "lead": "연봉 조정안"},
            {"thread_id": self.vis_tid, "subject": "양자화 결정", "lead": "확정"},
        ]}
        seen = {}

        def fake(cmd, prompt, **kw):
            seen["prompt"] = prompt
            return f"[#{self.vis_tid}] 확정 통보"

        with mock.patch.object(review, "ai_run", side_effect=fake):
            out = review.ai_digest(self.store, self.cfg, digest)
        self.assertNotIn("연봉", seen["prompt"])
        # 표시 축은 불변 — digest 구조에는 숨긴 항목이 그대로 남는다
        self.assertEqual(len(out["work"]), 2)

    def test_dossier_materials_skip_hidden(self):
        ctx = self.store.person_thread_context(self.KIM)
        self.assertNotIn(self.hid_tid, {c["thread_id"] for c in ctx})
        self.assertTrue(all("연봉" not in t
                            for t in self.store.person_sent_texts(self.KIM)))

    def test_tone_samples_skip_hidden(self):
        # 숨긴 스레드에 내가 보낸 보고성 장문이 있어도 문체 표본이 되지 않는다.
        # (내 발신은 hidden 을 자동 해제하지 않는다 — _insert 는 수신만 해제)
        long_body = "주간 진행 상황을 정리해 보고드립니다. " * 20
        self.store.ingest([
            _rec("t1", ME, [self.KIM], "주간 보고", "2026-07-12T09:00:00",
                 body=long_body, reply_to="h1"),
        ])
        self.assertIn(self.hid_tid, self.store.hidden_thread_ids())
        from mailkb import weekly as weekly_mod
        self.assertEqual(weekly_mod.tone_samples(self.store), "(없음)")


class TestAskContextBudget(unittest.TestCase):
    """분석의 입력 예산 — **한 콜 총량**으로 묶고 통당 배분은 코드가 정한다.

    통당 상한으로는 총량을 못 묶는다(같은 3,000자 설정에서 3통이면 10K,
    24통이면 75K). 사용자가 아는 값은 백엔드 컨텍스트 창이므로 손잡이를
    토큰 하나로 두고, 토큰→자수 추정은 한 번만 쓴 뒤 **조립된 프롬프트의
    실제 길이**로 맞춘다(2026-08-03)."""

    KIM = "kim@corp.example"

    def setUp(self):
        from mailkb import ask
        self.ask = ask
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.cfg = Config(home=home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"], ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(self.store.close)
        # 통당 ~4,000자(실사용 상세 메일) · 20통 — 스레드 전개 상한(12)보다 길어
        # 반드시 '부분 열람'이 생긴다
        body = ("서두 배경입니다. " * 400) + "\n★결론은 per-channel 로 확정합니다."
        self.store.ingest([
            _rec(f"c{i}", self.KIM if i % 2 == 0 else ME,
                 [ME] if i % 2 == 0 else [self.KIM], "양자화 방식 결정",
                 f"2026-07-{10+i:02d}T09:00:00", body=body,
                 reply_to="c0" if i else "")
            for i in range(20)])
        self.mids = [r["id"] for r in self.store.db.execute(
            "SELECT id FROM messages ORDER BY sent_on")]

    def _run(self, read_ids, **raw):
        if raw:
            self.cfg.raw = {"ai": raw}
        seen = []
        replies = iter([json.dumps({"action": "read", "ids": read_ids}),
                        json.dumps({"action": "answer"}),
                        json.dumps({"state": "근거 부족", "answer": "x", "claims": [],
                                    "conflicts": [], "leads": []})])

        def fake(cmd, prompt, **kw):
            seen.append(prompt)
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            self.ask.ask(self.store, self.cfg, "양자화 방식 결정", today="2026-07-20")
        return seen

    def _read_block(self, prompt):
        m = re.search(r"\[정독한 본문\]\n(.*?)\n\[(?:남은 예산|찾았지만)", prompt, re.S)
        return m.group(1) if m else ""

    def test_rounds_get_a_digest_and_the_answer_gets_the_full_text(self):
        seen = self._run(self.mids[:3])
        rounds = [p for p in seen if '"action"' in p]
        answer = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
        # 마지막 라운드(정독 뒤)의 본문 블록이 답변의 것보다 작아야 한다
        self.assertLess(len(self._read_block(rounds[-1])),
                        len(self._read_block(answer)))
        # 라운드 요지에도 결론이 남는다 — 앞뒤 분할이라서
        self.assertIn("★결론은", self._read_block(rounds[-1]))
        self.assertIn("★결론은", self._read_block(answer))

    def test_zero_means_no_limit_at_all(self):
        seen = self._run(self.mids[:2], ask_max_input_tokens=0)
        answer = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
        self.assertNotIn("중략", self._read_block(answer))   # 전문 그대로

    def test_budget_bounds_the_whole_prompt_not_each_mail(self):
        # 이 설계의 요지 — 통당이 아니라 **콜 전체**가 예산 안에 든다.
        for tok in (8000, 20000, 60000):
            self.setUp()
            seen = self._run(self.mids[:6], ask_max_input_tokens=tok)
            answer = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
            self.assertLessEqual(len(answer), tok, f"{tok}토큰 예산 초과")

    def test_below_the_floor_it_sends_the_smallest_it_can(self):
        # 템플릿·목록만으로도 예산을 넘는 극단값에서는 바닥까지 줄이고 보낸다 —
        # 정독 통수를 깎는 것은 조사 품질을 깎는 일이라 여기서 하지 않는다.
        seen = self._run(self.mids[:6], ask_max_input_tokens=1500)
        answer = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
        block = self._read_block(answer)
        self.assertIn("중략", block)                       # 바닥까지 줄였다
        per = [len(b) for b in re.split(r"\n\n(?=\[#)", block)]
        self.assertTrue(all(x < 700 for x in per), per)    # 통당 바닥 수준

    def test_more_mails_means_smaller_per_mail_share(self):
        # 같은 예산이면 통수가 늘수록 통당 몫이 준다 — 창을 지키는 방식이다
        self.setUp()
        few = self._read_block([p for p in self._run(
            self.mids[:2], ask_max_input_tokens=20000)
            if "저장된 업무 메일만 근거로" in p][-1])
        self.setUp()
        many = self._read_block([p for p in self._run(
            self.mids[:12], ask_max_input_tokens=20000)
            if "저장된 업무 메일만 근거로" in p][-1])
        self.assertGreater(few.count("중략"), 0) if len(few) > 20000 else None
        # 통수가 6배인데 블록 길이는 예산에 묶여 6배가 되지 않는다
        self.assertLess(len(many), len(few) * 6)

    def test_partial_thread_view_is_stated(self):
        # 조각만 보고 있다는 사실을 모델이 알아야 '더 읽자'를 고를 수 있다.
        # 요약을 지어 넣는 것보다 정직하고 비용이 0 이다.
        seen = self._run(self.mids[:2])
        answer = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
        m = re.search(r"이 스레드 (\d+)통 중 (\d+)통 열람", answer)
        self.assertIsNotNone(m, "부분 열람 표기가 없다")
        self.assertEqual(int(m.group(1)), 20)
        self.assertLess(int(m.group(2)), 20)

    def test_no_note_when_the_whole_thread_was_read(self):
        # 다 읽었으면 군더더기를 붙이지 않는다 — 짧은 스레드가 대다수다
        short = Store(Path(self.tmp.name) / "s.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(short.close)
        short.ingest([_rec(f"s{i}", self.KIM, [ME], "짧은 결정",
                           f"2026-07-{10+i:02d}T09:00:00", body="본문입니다.",
                           reply_to="s0" if i else "") for i in range(2)])
        read = {m["id"]: {"id": m["id"], "thread_id": m["thread_id"], "subject": "짧은 결정",
                          "sender": "kim", "sent_on": m["sent_on"], "is_sent": 0,
                          "body": "본문입니다."}
                for m in short.db.execute("SELECT id, thread_id, sent_on FROM messages")}
        out = self.ask._fmt_read(read, 3000, self.ask._thread_totals(short, read))
        self.assertNotIn("통 중", out)


class TestAskPromptParity(unittest.TestCase):
    """ANSWER 와 REPAIR 는 같은 답을 요구해야 한다 (2026-08-04).

    실기기 확인에서 잡혔다 — 첫 실제 조사(sonnet)에서 검증기가 answer 를 떨어뜨려
    _repair_answer 가 돌았는데, 그때 나온 답이 예전 톤으로 되돌아갔다. ANSWER 만
    '3~6문장 + 조건·기한·수치 필수' 로 고치고 REPAIR 는 '2~4문장' 인 채로 뒀기
    때문이다. 재작성은 드문 경로가 아니라 **첫 조사에서 바로** 탔다."""

    def test_repair_asks_for_the_same_answer_as_answer(self):
        from mailkb import ask
        self.assertIn("3~6문장", ask.ANSWER)
        self.assertIn("3~6문장", ask.REPAIR)      # 두 프롬프트가 같은 길이를 요구
        self.assertNotIn("2~4문장", ask.REPAIR)   # 예전 규칙이 남아 있지 않다
        for rule in ("조건", "기한", "수치"):
            self.assertIn(rule, ask.REPAIR, f"REPAIR 에 '{rule}' 요구가 없다")
        # 재작성도 인용 밖으로 나가면 안 된다 — 나가면 재검증에서 통째로 떨어진다
        self.assertIn("quote", ask.REPAIR)


class TestAskAnswerShape(unittest.TestCase):
    """답변의 형태 — headline·role·open·문맥 (2026-08-03).

    사용자 지적: "답변이 담백해서 내용 확인을 위해 메일을 다시 읽게 된다."
    인용 조각 대신 원문 앞뒤를 붙이고, 결론을 구조로 올린다."""

    KIM = "kim@corp.example"
    BODY = ("도현님, 김민수입니다.\n\n"
            "INT8 PTQ 정확도를 검토했습니다. mAP 가 3.2%p 하락해 양산 기준에\n"
            "못 미칩니다.\n\n"
            "고객 재학습 파이프라인 보유가 확인돼, 검토 결과 양자화는 QAT 로 확정합니다.\n"
            "다만 고객 데이터가 8/5 까지 오지 않으면 폴백하겠습니다.")

    def setUp(self):
        from mailkb import ask
        self.ask = ask
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.cfg = Config(home=home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"], ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(self.store.close)
        self.store.ingest([
            _rec("s1", self.KIM, [ME], "양자화 방식 결정",
                 "2026-07-26T09:00:00", body=self.BODY)])
        self.mid = self.store.db.execute("SELECT id FROM messages").fetchone()["id"]

    def _run(self, payload, supported=("c0",)):
        replies = iter([json.dumps({"action": "read", "ids": [self.mid]}),
                        json.dumps({"action": "answer"}),
                        json.dumps(payload)])

        def fake(cmd, prompt, **kw):
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": list(supported),
                                   "answer_supported": True})
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            return self.ask.ask(self.store, self.cfg, "양자화 방식 결정",
                                today="2026-08-01")

    def _payload(self, **over):
        base = {"state": "확인됨", "headline": "QAT 로 확정", "answer": "경위입니다.",
                "claims": [{"text": "김민수 팀장이 QAT 로 확정했다", "mid": self.mid,
                            "quote": "검토 결과 양자화는 QAT 로 확정합니다",
                            "role": "결론"}],
                "conflicts": [], "open": [], "leads": []}
        base.update(over)
        return base

    def test_claims_carry_original_context(self):
        res = self._run(self._payload())
        ctx = res["claims"][0].get("context")
        self.assertIsNotNone(ctx, "문맥이 안 붙었다")
        self.assertIn("고객 재학습 파이프라인", ctx["pre"])
        self.assertIn("8/5", ctx["post"])        # 조건이 보여야 메일을 안 연다
        # 표시용이라 줄바꿈은 접는다 — CLI 들여쓰기·웹 문단이 깨지지 않게
        self.assertNotIn("\n", ctx["pre"] + ctx["post"] + res["claims"][0]["quote"])

    def test_headline_needs_a_verified_claim(self):
        res = self._run(self._payload(
            claims=[{"text": "지어낸 것", "mid": self.mid,
                     "quote": "본문에 없는 문장", "role": "결론"}]))
        self.assertEqual(res["state"], "근거 부족")
        self.assertEqual(res["headline"], "")    # 근거 없는 결론은 안 낸다

    def test_valid_role_survives_verification(self):
        # _verify 가 필드를 화이트리스트로 재구성해 role 이 버려지던 버그
        # (실서버 확인에서 잡음, 2026-08-03) — 유효한 role 은 살아남아야 한다
        res = self._run(self._payload(claims=[
            {"text": "a", "mid": self.mid, "quote": "검토 결과 양자화는 QAT 로 확정합니다",
             "role": "결론"},
            {"text": "b", "mid": self.mid, "quote": "mAP 가 3.2%p 하락해", "role": "근거"}]),
            supported=("c0", "c1"))
        self.assertEqual([c["role"] for c in res["claims"]], ["결론", "근거"])

    def test_role_is_parsed_leniently(self):
        res = self._run(self._payload(claims=[
            {"text": "a", "mid": self.mid, "quote": "mAP 가 3.2%p 하락해", "role": "엉뚱"},
            {"text": "b", "mid": self.mid, "quote": "검토 결과 양자화는 QAT 로 확정합니다"}]),
            supported=("c0", "c1"))
        self.assertEqual([c["role"] for c in res["claims"]], ["배경", "배경"])

    def test_open_goes_through_quote_verification(self):
        res = self._run(self._payload(open=[
            {"text": "진짜", "mid": self.mid, "quote": "8/5 까지 오지 않으면"},
            {"text": "가짜", "mid": self.mid, "quote": "본문에 없는 마감"}]))
        self.assertEqual([o["text"] for o in res["open"]], ["진짜"])

    def test_scope_carries_the_basis_line_material(self):
        # '기준' 줄은 코드가 만든다 — 자기가 뭘 안 봤는지는 모델이 모른다
        res = self._run(self._payload())
        self.assertIn("2026-07-26", res["scope"]["span"])
        self.assertIn("partial", res["scope"])

    def test_prompt_puts_evidence_before_the_rules(self):
        # 긴 문맥에서는 생성 지점에 가까운 지시가 힘을 받는다 — 자료를 앞에,
        # 과업을 뒤에 둔다(예산 모델로 근거가 100배 길어졌다)
        seen = []

        def fake(cmd, prompt, **kw):
            seen.append(prompt)
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"], "answer_supported": True})
            if '"action"' in prompt:
                return json.dumps({"action": "answer"})
            return json.dumps(self._payload(claims=[]))

        with mock.patch.object(review, "ai_run", side_effect=fake):
            self.ask.ask(self.store, self.cfg, "양자화 방식 결정", today="2026-08-01")
        ans = [p for p in seen if "저장된 업무 메일만 근거로" in p][-1]
        self.assertLess(ans.index("[정독한 본문]"), ans.index("[규칙]"))
        self.assertLess(ans.index("[규칙]"), ans.index("[출력]"))
        self.assertIn("[근거 시간축]", ans)
        self.assertIn("가로질러 인용하지 마라", ans)     # 중략 마커 보호
        self.assertIn("2개 이상", ans)                   # 상충함 최소 개수


class TestPersonPromisesAndDailyExcerpt(unittest.TestCase):
    """인물 카드의 '이 사람에게 한 내 약속' 과 일간 '변화' 절 발췌 (2026-08-03).

    둘 다 "확인하러 원문을 다시 열지 않게" 하는 같은 취지다. 인물 쪽은 promises
    (내가 직접 쓴 확정 어미)를 재사용한다 — 정규식 요청 판정은 쓰지 않는다."""

    KIM, LEE = "kim@corp.example", "lee@corp.example"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.cfg = Config(home=home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"])
        self.store = Store(home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(self.store.close)
        self.store.ingest([
            _rec("p0", self.KIM, [ME], "양자화 결정", "2026-07-30T09:00:00",
                 body="검토 결과 알려주세요."),
            _rec("p1", ME, [self.KIM], "RE: 양자화 결정", "2026-07-31T09:00:00",
                 body="내일까지 정리해서 보내겠습니다.", reply_to="p0"),
            _rec("q0", self.LEE, [ME], "다른 건", "2026-07-30T10:00:00",
                 body="확인 부탁드립니다."),
            _rec("q1", ME, [self.LEE], "RE: 다른 건", "2026-07-31T10:00:00",
                 body="자료를 공유하겠습니다.", reply_to="q0"),
        ])

    def test_person_promises_are_scoped_to_that_person(self):
        from mailkb import promises
        ptids = self.store.person_thread_ids(self.KIM)
        mine = [x for x in promises.extract(self.store, today="2026-08-01")
                if x["thread_id"] in ptids]
        self.assertEqual(len(mine), 1)
        self.assertIn("보내겠습니다", mine[0]["quote"])
        # 다른 사람 스레드의 약속은 섞이지 않는다
        self.assertNotIn("공유하겠습니다", mine[0]["quote"])

    def test_person_card_shows_the_promise(self):
        from mailkb import web
        html = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("이 사람에게 한 내 약속", html)
        self.assertIn("보내겠습니다", html)
        self.assertNotIn("공유하겠습니다", html)     # 다른 사람 스레드는 안 섞인다

    def test_daily_change_section_carries_an_excerpt(self):
        # 상태판을 직접 넣어 '변화' 절이 반드시 생기게 한다
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='양자화 결정'").fetchone()[0]
        det = review.deterministic(self.store, self.cfg, "2026-07-31")
        det["shift"] = {"new_mine": [{"thread_id": tid, "subject": "양자화 결정"}],
                        "new_stuck": [], "resolved": []}
        md = review.render(det, None, self.store)
        self.assertIn("## 변화 — 어제 이후", md)
        self.assertIn("[#%d] 양자화 결정" % tid, md)
        # 발췌 — 마지막 메시지의 발신자 + 첫 문장. 왜 내 차례가 됐는지가 보인다
        self.assertIn("나: 내일까지 정리해서 보내겠습니다.", md)
        # store 없이 부르면 종전 그대로(발췌 줄이 없다)
        plain = review.render(det, None)
        self.assertIn("## 변화 — 어제 이후", plain)
        self.assertNotIn("나: 내일까지", plain)


class TestAskAnswerRender(unittest.TestCase):
    """답변 렌더 — headline·role 그룹·문맥·기준 줄 (2026-08-03)."""

    def _res(self, **over):
        base = {
            "state": "확인됨", "headline": "QAT 로 확정", "answer": "경위입니다.",
            "claims": [
                {"text": "확정했다", "mid": 1, "thread_id": 7, "sender": "김민수",
                 "sent_on": "2026-07-26T09:00", "subject": "양자화", "role": "결론",
                 "quote": "QAT 로 확정합니다.",
                 "context": {"pre": "파이프라인 보유가 확인돼", "post": "다만 8/5 까지"}},
                {"text": "하락했다", "mid": 2, "thread_id": 7, "sender": "강미래",
                 "sent_on": "2026-07-25T09:00", "subject": "양자화", "role": "배경",
                 "quote": "3.2%p 하락"}],
            "conflicts": [], "leads": [],
            "open": [{"text": "GDS 8/20", "mid": 3, "thread_id": 9, "sender": "박지현",
                      "sent_on": "2026-07-28T09:00", "quote": "8/20 까지 제출"}],
            "scope": {"queries": ["양자화"], "calls": 4, "read": 2, "hits": 2,
                      "dropped": 0, "backend": "sonnet", "span": "2026-07-25 ~ 07-26 · 2통",
                      "partial": ["#7 12통 중 2통"]},
        }
        base.update(over)
        return base

    def test_headline_role_groups_context_and_basis(self):
        from mailkb import web
        h = web._ask_answer_body(self._res())
        self.assertIn("class='askhead'>QAT 로 확정", h)      # 한 줄 결론
        self.assertIn("근거 — 결론", h)                       # role 로 갈린다
        self.assertIn("배경", h)
        self.assertIn("열린 것", h)
        self.assertIn("파이프라인 보유가 확인돼", h)           # 앞 문맥
        self.assertIn("다만 8/5 까지", h)                     # 뒤 문맥 — 조건이 보인다
        self.assertIn("qhit", h)                              # 인용은 계속 구분된다
        self.assertIn("askreach", h)
        self.assertIn("기준 · 2026-07-25 ~ 07-26", h)
        self.assertIn("#7 12통 중 2통", h)                    # 덜 본 스레드

    def test_old_answers_without_context_render_as_before(self):
        # 캐시에 남은 옛 답은 context·role·headline 이 없다 — 안 깨져야 한다
        from mailkb import web
        old = self._res(headline="", open=[], claims=[
            {"text": "확정했다", "mid": 1, "thread_id": 7, "sender": "김민수",
             "sent_on": "2026-07-26T09:00", "subject": "양자화",
             "quote": "QAT 로 확정합니다."}])
        h = web._ask_answer_body(old)
        self.assertNotIn("askhead", h)
        self.assertIn("QAT 로 확정합니다.", h)
        self.assertNotIn("qhit", h)          # 문맥이 없으면 종전 인용 스타일


class TestAskMemoryInjection(unittest.TestCase):
    """장기기억(승인 결정)·사용자 지침(ai-rules.md)의 분석 프롬프트 주입.

    승인된 결정 원장은 이 앱에서 가장 비싸게 만들어지는 자산인데 종전에는 어떤
    AI 도 다시 읽지 않았다(2026-08-02 점검). 문맥 전용으로 싣고, 강제는 코드가
    한다 — 인용 검증은 여전히 정독 본문만 통과시킨다."""

    KIM = "kim@corp.example"

    def setUp(self):
        from mailkb import ask
        self.ask = ask
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(self.home / "t.sqlite", [ME], ["김도현"],
                           noise=self.cfg)
        self.store.ingest([
            _rec("m1", self.KIM, [ME], "양자화 결정", "2026-07-10T09:00:00",
                 body="per-channel 로 확정합니다."),
            _rec("m2", self.KIM, [ME], "숨긴 협의", "2026-07-11T09:00:00",
                 body="이 스레드는 숨긴다."),
        ])
        rows = {r["subject"]: r["thread_id"] for r in self.store.db.execute(
            "SELECT subject, thread_id FROM messages")}
        self.tid = rows["양자화 결정"]
        self.hid_tid = rows["숨긴 협의"]
        self.store.hide_thread(self.hid_tid, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _confirm(self, tid, title, decider="김도현", on="2026-07-09"):
        did = self.store.add_decision(tid, on, title, decider=decider)
        self.store.set_decision_status(did, "confirmed")
        return did

    def test_memory_block_picks_confirmed_by_word_overlap(self):
        self._confirm(self.tid, "양자화는 per-channel 로 확정")
        # 후보(미승인)와 무관 결정은 실리지 않는다
        self.store.add_decision(self.tid, "2026-07-09", "양자화 검토 착수")
        self._confirm(self.tid, "회식 장소는 본사 앞")
        block = self.ask._memory_block(self.store, "양자화 최종 결정?",
                                       frozenset())
        self.assertIn("장기기억", block)
        self.assertIn("인용 금지", block)
        self.assertIn("per-channel 로 확정", block)
        self.assertNotIn("검토 착수", block)      # candidate 제외
        self.assertNotIn("회식", block)           # 어휘 겹침 0 제외
        # 겹치는 것이 하나도 없으면 블록 자체가 없다
        self.assertEqual(
            self.ask._memory_block(self.store, "테이프아웃 일정?", frozenset()),
            "")

    def test_memory_block_skips_hidden_thread_decisions(self):
        self._confirm(self.hid_tid, "숨긴 협의 결론은 보류")
        block = self.ask._memory_block(self.store, "숨긴 협의 결론?",
                                       self.store.hidden_thread_ids())
        self.assertEqual(block, "")

    def test_memory_block_rows_mode_for_brief(self):
        # 인물 브리핑 — 어휘 매칭 없이 인물 범위 결정을 최신순으로 싣는다
        self._confirm(self.tid, "양자화는 per-channel 로 확정")
        rows = self.store.person_decisions(self.KIM, "김도현")
        self.assertTrue(rows)
        block = self.ask._memory_block(self.store, "브리핑", frozenset(),
                                       rows=rows)
        self.assertIn("per-channel", block)

    def test_prompts_carry_memory_and_rules_but_verify_does_not(self):
        self._confirm(self.tid, "양자화는 per-channel 로 확정")
        (self.home / "ai-rules.md").write_text(
            "NPX 는 누리소프트 프로젝트 코드명이다.", encoding="utf-8")
        mid = self.store.db.execute(
            "SELECT id FROM messages WHERE subject='양자화 결정'").fetchone()["id"]
        prompts = []
        replies = iter([
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "확정됐습니다.",
                        "claims": [{"text": "확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ])

        def fake(cmd, prompt, **kw):
            prompts.append(prompt)
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"],
                                   "answer_supported": True})
            return next(replies)

        with mock.patch.object(review, "ai_run", side_effect=fake):
            res = self.ask.ask(self.store, self.cfg, "양자화 결정 상태?",
                               today="2026-07-14")
        self.assertEqual(res["state"], "확인됨")
        step_and_answer = [p for p in prompts if "보수적인 근거 검증기" not in p]
        verify = [p for p in prompts if "보수적인 근거 검증기" in p]
        self.assertTrue(verify)
        for p in step_and_answer:
            self.assertIn("장기기억", p)
            self.assertIn("사용자 지침 — 우선 적용", p)
            self.assertIn("프로젝트 코드명", p)
        # 검증 콜은 문맥 주입에서 자유로워야 한다 — "외부 지식 금지" 계약 유지
        for p in verify:
            self.assertNotIn("장기기억", p)
            self.assertNotIn("사용자 지침", p)


class TestPeopleDossierAI(unittest.TestCase):
    """인물 도시에 v2 — AI 요약 캐시·근거 검증·증분 갱신 가드."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["나"], internal_domains=["corp.example"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}},
                          raw={"ai": {"summary_min_msgs": 1}})
        self.store = Store(Path(self.tmp.name) / "d.sqlite", [ME], ["나"],
                           noise=self.cfg)
        self.KIM = "kim@corp.example"
        self.store.ingest([
            _rec("k1", self.KIM, [ME], "NPX-200 타이밍 클로저",
                 "2026-07-01T09:00:00", "B0 타이밍 hold 위반 재현됩니다. 재검증 필요."),
            _rec("k2", ME, [self.KIM], "RE", "2026-07-01T13:00:00",
                 "제가 최종 승인 담당입니다. 이번 릴리스를 승인합니다.",
                 reply_to="k1"),
            _rec("k3", self.KIM, [ME], "RE", "2026-07-02T09:00:00",
                 "LEC 스크립트 점검 완료했습니다. 16일 슬롯 문제없습니다.",
                 reply_to="k2"),
        ])
        self.t1 = self._tid("k1")

    def _tid(self, mid):
        return self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id=?",
            (f"<{mid}@t>",)).fetchone()[0]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, fake, backend="internal", addr=None):
        with mock.patch.object(review, "ai_run", return_value=fake):
            return distill.refresh_person_dossier(
                self.store, self.cfg, addr or self.KIM, backend=backend).status

    def test_generates_and_verifies_grounding(self):
        # 근거 인용이 실제 본문에 있는 줄만 살아남는다(환각·오귀속·근거없음 제거)
        fake = (f"## 역할\n- [#{self.t1}] 타이밍 클로저 담당 · 인용: "
                f'"B0 타이밍 hold 위반 재현됩니다"\n'
                f"## 지금 함께 하는 일\n"
                f'- [#999] 없는 스레드 · 인용: "존재하지 않는 인용문 열두자이상"\n'
                f'- [#{self.t1}] 근거 없음 · 인용: "본문에 전혀 없는 문장 열두자이상"')
        self.assertEqual(self._run(fake), "ok")
        md = self.store.people_dossier(self.KIM)["dossier_md"]
        self.assertIn("타이밍 클로저 담당", md)     # 검증 통과
        self.assertNotIn("#999", md)               # 날조 스레드 제거
        self.assertNotIn("근거 없음", md)           # 미검증 인용 제거
        self.assertNotIn("인용:", md)              # 표시부엔 인용 꼬리 없음
        self.assertNotIn("지금 함께 하는 일", md)   # 살아남은 줄 없으면 섹션째 제거

    def test_manual_refresh_always_regenerates_and_advances_basis(self):
        # 배치 시절엔 basis 이후 새 메일이 없으면 건너뛰었다(비용 통제). 버튼은
        # 사용자가 명시적으로 누른 것이라 항상 돈다 — 죽은 버튼을 만들지 않는다.
        fake = f'## 역할\n- [#{self.t1}] 담당 · 인용: "B0 타이밍 hold 위반 재현됩니다"'
        self.assertEqual(self._run(fake), "ok")
        self.assertEqual(self.store.people_dossier(self.KIM)["basis_msg_count"], 3)
        self.assertEqual(self._run(fake), "ok")      # 새 메일 0통이어도 재생성
        self.assertEqual(self.store.people_dossier(self.KIM)["basis_msg_count"], 3)
        # 새 메시지 도착 → basis 가 함께 전진
        self.store.ingest([_rec("k4", self.KIM, [ME], "RE", "2026-07-03T09:00:00",
                                "추가 재현됩니다.")])
        self.assertEqual(self._run(fake), "ok")
        self.assertEqual(self.store.people_dossier(self.KIM)["basis_msg_count"], 4)

    def test_rejects_my_quote_as_their_role(self):
        # 회귀 재현: 같은 스레드의 내 발언은 대상 인물 역할의 근거가 될 수 없다.
        raw = (f"## 역할\n- [#{self.t1}] 최종 승인 담당 · 인용: "
               f'"제가 최종 승인 담당입니다"')
        md = distill._sanitize_dossier(
            raw, distill._PersonQuoteChecker(self.store, self.KIM))
        self.assertEqual(md, "")

    def test_rejects_third_party_quote_in_same_thread(self):
        lee = "lee@corp.example"
        self.store.ingest([
            _rec("k4", lee, [ME, self.KIM], "RE", "2026-07-03T09:00:00",
                 "제가 릴리스 승인 담당입니다. 결과는 오늘 공유합니다.",
                 reply_to="k1"),
        ])
        self.assertEqual(self._tid("k4"), self.t1)
        raw = (f"## 역할\n- [#{self.t1}] 릴리스 승인 담당 · 인용: "
               f'"제가 릴리스 승인 담당입니다"')
        md = distill._sanitize_dossier(
            raw, distill._PersonQuoteChecker(self.store, self.KIM))
        self.assertEqual(md, "")

    def test_rejects_preserved_quote_inside_target_message(self):
        quoted = "제가 최종 승인 담당입니다. 이번 릴리스를 승인합니다."
        own = "B0 타이밍 hold 위반 재현됩니다. 재검증 필요."
        self.store.db.execute(
            "UPDATE messages SET new_content=? WHERE message_id='<k1@t>'",
            (own + "\n\n" + PRESERVED_MARK + "\n" + quoted,))
        self.store.db.commit()
        checker = distill._PersonQuoteChecker(self.store, self.KIM)
        self.assertTrue(checker.ok(self.t1, "B0 타이밍 hold 위반 재현됩니다"))
        self.assertFalse(checker.ok(self.t1, "제가 최종 승인 담당입니다"))

    def test_rejects_quote_spanning_two_messages(self):
        self.store.ingest([
            _rec("k4", self.KIM, [ME], "RE", "2026-07-03T09:00:00",
                 "첫번째메시지마지막구절", reply_to="k1"),
            _rec("k5", self.KIM, [ME], "RE", "2026-07-03T10:00:00",
                 "두번째메시지시작구절", reply_to="k4"),
        ])
        self.assertEqual(self._tid("k4"), self.t1)
        self.assertEqual(self._tid("k5"), self.t1)
        checker = distill._PersonQuoteChecker(self.store, self.KIM)
        self.assertFalse(
            checker.ok(self.t1, "첫번째메시지마지막구절두번째메시지시작구절"))

    def test_materials_only_include_target_authored_excerpts(self):
        ctx = self.store.person_thread_context(self.KIM)
        text = "\n".join(
            e["text"] for c in ctx for e in c["excerpts"])
        self.assertIn("B0 타이밍 hold 위반", text)
        self.assertIn("LEC 스크립트 점검", text)
        self.assertNotIn("제가 최종 승인 담당", text)
        materials = distill._dossier_materials(
            self.store, self.cfg, self.KIM, "kim")
        self.assertIn("대상 인물 직접 작성 발췌", materials["threads"])
        self.assertIn("문맥 전용, 인용 금지", materials["threads"])

    def test_no_target_material_skips_ai_call(self):
        only = "only-recipient@corp.example"
        self.store.ingest([
            _rec("only1", ME, [only], "단방향 공유", "2026-07-04T09:00:00",
                 "제가 보낸 내용만 있고 상대 발신은 없습니다."),
        ])
        with mock.patch.object(review, "ai_run") as run:
            result = distill._gen_dossier(
                self.store, self.cfg, ["echo"], only, "수신자", "")
        self.assertEqual(result.status, "no_material")
        run.assert_not_called()

    def test_empty_validation_marks_checked_and_keeps_card_empty(self):
        invalid = (f"## 역할\n- [#{self.t1}] 근거 없는 담당 · 인용: "
                   f'"본문에 존재하지 않는 무효 인용문입니다"')
        self.assertEqual(self._run(invalid), "empty")
        row = self.store.people_dossier(self.KIM)
        self.assertIsNotNone(row)
        self.assertEqual(row["dossier_md"], "")
        self.assertEqual(row["basis_msg_count"], 3)

    def test_stale_validator_hidden_then_regenerated_without_old_prompt(self):
        self.store.save_people_dossier(
            self.KIM, "## 역할\n- [#1] 오래된 잘못된 역할", 3,
            validator_version=1)
        self.assertIsNone(self.store.people_dossier(self.KIM))
        self.assertNotIn(self.KIM, self.store.dossier_roles())
        self.assertNotIn("오래된 잘못된 역할",
                         web.render_dossier(self.store, self.cfg, self.KIM))
        valid = (f"## 역할\n- [#{self.t1}] 타이밍 클로저 담당 · 인용: "
                 f'"B0 타이밍 hold 위반 재현됩니다"')
        with mock.patch.object(review, "ai_run", return_value=valid) as run:
            self.assertEqual(
                distill.refresh_person_dossier(
                    self.store, self.cfg, self.KIM, backend="internal").status,
                "ok")
        self.assertNotIn("오래된 잘못된 역할", run.call_args.args[1])
        row = self.store.people_dossier(self.KIM)
        self.assertEqual(row["validator_version"], DOSSIER_VALIDATOR_VERSION)
        self.assertIn("타이밍 클로저 담당", row["dossier_md"])

    def test_ai_card_and_landing_role(self):
        self._run(f'## 역할\n- [#{self.t1}] 타이밍 클로저 담당 · 인용: '
                  f'"B0 타이밍 hold 위반 재현됩니다"')
        d = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("AI 추정", d)                 # AI 카드
        self.assertIn("타이밍 클로저 담당", d)
        self.assertIn(f"/thread/{self.t1}", d)      # 근거 링크
        self.assertEqual(self.store.dossier_roles().get(self.KIM),
                         "타이밍 클로저 담당")       # 랜딩 역할줄(헤더 아님)

    def test_refresh_person_dossier_status_matrix(self):
        # 상태별로 저장·basis 전진이 다르다 — UI 가 무슨 일이 있었는지 말할 수 있게
        good = (f'## 역할\n- [#{self.t1}] 담당 · 인용: '
                f'"B0 타이밍 hold 위반 재현됩니다"')
        self.assertEqual(self._run(good), "ok")
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AIError("백엔드 죽음")):
            self.assertEqual(
                distill.refresh_person_dossier(
                    self.store, self.cfg, self.KIM, backend="internal").status,
                "error")
        # 실패는 basis 를 전진시키지 않는다 — 다음 클릭에 재시도가 남는다
        self.assertEqual(self.store.people_dossier(self.KIM)["basis_msg_count"], 3)
        self.assertIn("담당", self.store.people_dossier(self.KIM)["dossier_md"])
        # 재료 없는 사람(내가 보낸 메일만) → AI 콜 자체를 안 한다
        with mock.patch.object(review, "ai_run") as run:
            self.assertEqual(
                distill.refresh_person_dossier(
                    self.store, self.cfg, "nobody@corp.example",
                    backend="internal").status,
                "no_material")
            run.assert_not_called()

    def test_refresh_person_dossier_ignores_unknown_addresses(self):
        # 배치 시절엔 후보가 rank_people 에서만 왔다. 버튼은 임의 주소가 들어올
        # 수 있고, 교신 없는 주소에 행을 만들면 people_dossier(재수집으로 복구
        # 안 되는 표)에 쓰레기가 쌓인다.
        before = {r["addr"] for r in self.store.db.execute(
            "SELECT addr FROM people_dossier")}
        for junk in ("", "   ", "<script>x</script>", "nobody@corp.example"):
            with mock.patch.object(review, "ai_run") as run:
                res = distill.refresh_person_dossier(
                    self.store, self.cfg, junk, backend="internal")
                run.assert_not_called()          # AI 콜도 안 나간다
            self.assertEqual(res.status, "no_material")
        after = {r["addr"] for r in self.store.db.execute(
            "SELECT addr FROM people_dossier")}
        self.assertEqual(before, after)          # 새 행 없음

    def test_refresh_person_dossier_propagates_cancel(self):
        # 취소는 실패가 아니다 — AIError 로 삼키면 basis 가 전진해 재시도가 사라진다
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AICancelled("중지")):
            with self.assertRaises(review.AICancelled):
                distill.refresh_person_dossier(
                    self.store, self.cfg, self.KIM, backend="internal")

    def test_dossier_card_shows_age_and_unreflected_count(self):
        # 자동 갱신이 사라진 뒤로는 '얼마나 낡았나'가 카드의 신뢰도 정보다
        self._run(f'## 역할\n- [#{self.t1}] 담당 · 인용: '
                  f'"B0 타이밍 hold 위반 재현됩니다"')
        out = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("갱신", out)
        self.assertNotIn("미반영", out)          # 새 메일 0통이면 조각 생략
        self.assertIn("새 메일 없음", out)        # 대신 버튼 옆 안내
        self.store.ingest([_rec("k9", self.KIM, [ME], "새 건",
                                "2026-07-13T09:00:00", "추가 재현됩니다.")])
        out2 = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("새 메일 1통 미반영", out2)
        self.assertIn("요약 갱신", out2)          # 캐시 있음 → '갱신'

    def test_dossier_stale_validator_offers_rebuild(self):
        # 검증 규약이 바뀌면 카드가 숨겨진다. 배치가 없어진 뒤로는 이 안내가
        # 유일한 복구 동선이다.
        self.store.save_people_dossier(self.KIM, "## 역할\n- [#1] 옛 카드", 3,
                                       validator_version=1)
        out = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("요약 형식이 바뀌어", out)
        self.assertIn("요약 다시 만들기", out)
        self.assertNotIn("옛 카드", out)

    def test_dossier_wait_card_and_cancel_marker(self):
        try:
            with web._dossier_lock:
                web._dossier_job.update(running=True, addr=self.KIM,
                                        stage="본문 읽고 인용 뽑는 중…",
                                        model="claude-x", last_ev=time.time(),
                                        phase="writing", recv=42)
            out = web.render_dossier(self.store, self.cfg, self.KIM)
            self.assertIn("data-dossier-running", out)
            self.assertIn("class='waitcard'", out)
            self.assertIn("id='dz-stage'>본문 읽고 인용 뽑는 중…<", out)
            self.assertIn("id='dz-model'>claude-x<", out)
            self.assertIn("action='/people/dossier/cancel'", out)
            self.assertIn(f"name='addr' value='{self.KIM}'", out)
            inner, running = web.render_dossier_status(
                self.store, self.cfg, self.KIM)
            self.assertTrue(running)
            self.assertIn("data-dossier-running", inner)
            self.assertNotIn("관계", inner)      # 진행 중엔 가벼운 fragment 만
            # 다른 사람 화면에는 남의 진행이 새지 않는다
            self.assertNotIn("data-dossier-running",
                             web.render_dossier(self.store, self.cfg,
                                                "other@corp.example"))
        finally:
            with web._dossier_lock:
                web._dossier_job.update(running=False, addr="", stage="",
                                        model="", last_ev=0.0, phase="", recv=0)

    def test_dossier_job_states_are_explained(self):
        # 조용히 끝나면 버튼이 고장 난 줄 안다 — 상태마다 한 줄로 말한다
        for stage in ("empty", "no_material", "no_backend", "error",
                      "cancelled"):
            try:
                with web._dossier_lock:
                    web._dossier_job.update(running=False, addr=self.KIM,
                                            stage=stage, done_at=time.time())
                out = web.render_dossier(self.store, self.cfg, self.KIM)
                self.assertIn(web._DOSSIER_NOTE[stage], out)
                # 오래된 결과는 유령 배너가 된다 — 다음 방문에는 사라져야 한다
                with web._dossier_lock:
                    web._dossier_job["done_at"] = time.time() - web._NOTE_TTL - 1
                self.assertNotIn(web._DOSSIER_NOTE[stage],
                                 web.render_dossier(self.store, self.cfg,
                                                    self.KIM))
            finally:
                with web._dossier_lock:
                    web._dossier_job.update(addr="", stage="", done_at=0.0)

    def test_review_ai_layer_no_longer_touches_dossiers(self):
        # 인물 요약은 인물 화면 버튼으로 분리됐다 — 회고 파이프라인에 없다
        self.assertFalse(hasattr(distill, "refresh_people_dossiers"))
        self.assertIn("refresh_person_dossier", dir(distill))

    def test_dossier_status_pane_matches_polling(self):
        # paneFor 와 route() 가 어긋나면 대기 화면이 폴링 안 되는 패널에 박혀
        # 영영 멈춘다(주간 보고에서 겪은 함정).
        self.assertIn('if (path === "/people/dossier/status") return "left"',
                      web._APP_JS)
        _, _, _, pane = web.route(self.store, self.cfg,
                                  "/people/dossier/status", {}, "2026-07-14")
        self.assertEqual(pane, "left")

    def test_appjs_dossier_polling_hook(self):
        js = web._APP_JS
        self.assertIn("function hookDossierPolling", js)
        self.assertIn("/people/dossier/status", js)
        self.assertIn("data-dossier-running", js)
        self.assertIn('patchJob(tmp, left, "dz")', js)
        self.assertNotIn("hookDossierPolling(right)", js)

    def test_graceful_without_backend(self):
        cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                     ai_summary_backend="ghost")
        self.assertEqual(
            distill.refresh_person_dossier(self.store, cfg, self.KIM,
                                           backend="ghost").status,
            "no_backend")
        # 도시에 없으면 AI 카드 없이 결정론 카드만(graceful)
        d = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertNotIn("AI 추정", d)


class TestPeopleDossierSchema(unittest.TestCase):
    def test_old_table_gets_validator_column_without_resync(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.sqlite"
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE people_dossier (
                     addr TEXT PRIMARY KEY, dossier_md TEXT DEFAULT '',
                     updated TEXT DEFAULT '', basis_msg_count INTEGER DEFAULT 0
                   )""")
            db.execute(
                "INSERT INTO people_dossier VALUES (?,?,?,?)",
                ("kim@corp.example", "## 역할\n- [#1] 구버전", "2026-07-01", 2))
            db.commit()
            db.close()

            store = Store(path, [ME])
            try:
                cols = {r["name"] for r in
                        store.db.execute("PRAGMA table_info(people_dossier)")}
                self.assertIn("validator_version", cols)
                self.assertIsNone(store.people_dossier("kim@corp.example"))
                stale = store.people_dossier(
                    "kim@corp.example", include_stale=True)
                self.assertEqual(stale["validator_version"], 1)
            finally:
                store.close()


class TestWordCloud(unittest.TestCase):
    """도시에 업무 어휘 지도 — 기존 tokenizer 호환 + 새 분석·표시 임계."""

    def test_josa_strip_without_overcut(self):
        f = report._strip_josa
        self.assertEqual(f("검토를"), "검토")       # 조사 제거
        self.assertEqual(f("결과가"), "결과")
        self.assertEqual(f("양자화의"), "양자화")
        self.assertEqual(f("타이밍을"), "타이밍")
        # 어간이 1자로 줄면 과잉절단이므로 원형 유지 (결과·회의·성과 보존)
        self.assertEqual(f("결과"), "결과")
        self.assertEqual(f("회의"), "회의")
        self.assertEqual(f("성과"), "성과")
        self.assertEqual(f("data"), "data")          # 영문은 조사 무관

    def test_top_words_stopwords_and_domain_terms(self):
        texts = [
            "타이밍 클로저 검토를 진행했습니다. 감사합니다.",
            "타이밍 마진 결과 공유드립니다. 확인 부탁드립니다.",
            "양자화 QAT 방식 확정. 양자화 회귀 해결.",
        ]
        words = dict(report.top_words(texts, limit=20))
        self.assertIn("타이밍", words)               # 도메인어 잔존
        self.assertIn("양자화", words)
        self.assertEqual(words["타이밍"], 2)
        # 상투어·업무동사는 제외
        for stop in ("감사합니다", "부탁드립니다", "검토", "진행", "확인", "공유"):
            self.assertNotIn(stop, words)
        # 1회성 단어(min_count 미만) 제외 — 클로저/마진/QAT 는 1회
        self.assertNotIn("클로저", words)

    def test_top_words_extra_stop(self):
        texts = ["김도현 타이밍 검토", "김도현 타이밍 회의", "타이밍 마진 김도현"]
        words = dict(report.top_words(texts, extra_stop=["김도현"]))
        self.assertNotIn("김도현", words)             # 내 이름 제외
        self.assertIn("타이밍", words)

    def test_english_two_char_dropped_acronyms_kept(self):
        t = ["EC ED DB AI CVE SoC 타이밍", "EC ED DB AI CVE SoC 타이밍"]
        words = dict(report.top_words(t, min_count=2))
        for noise in ("EC", "ED", "DB", "AI"):       # 2자 영문 = 노이즈
            self.assertNotIn(noise, words)
        self.assertIn("CVE", words)                  # 3자+ 도메인 약어 유지
        self.assertIn("SoC", words)

    def test_urls_and_web_terms_dropped(self):
        t = ["https://confluence.corp/x 참고 타이밍 검증",
             "www.example.com jira 티켓 타이밍 검증 me@corp.example"]
        words = dict(report.top_words(t, min_count=2))
        for noise in ("http", "https", "www", "confluence", "jira", "com", "corp"):
            self.assertNotIn(noise, {k.lower() for k in words})
        self.assertIn("타이밍", words)                # 도메인어는 남음

    def test_english_function_words_and_josa_dropped(self):
        t = ["this is the report for the meeting 타이밍 in progress 에서",
             "that was the summary of the work 타이밍 as noted 에서"]
        words = {k.lower() for k in dict(report.top_words(t, min_count=2))}
        for fn in ("the", "and", "for", "this", "that", "was", "is", "in",
                   "of", "as", "에서"):
            self.assertNotIn(fn, words)
        self.assertIn("타이밍", words)

    def _seed_sent(self, addr, n, body):
        recs = [_rec(f"wc{i}", addr, [ME], "건", f"2026-07-{i+1:02d}T09:00:00",
                     body=body) for i in range(n)]
        self.store.ingest(recs)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          my_names=["김도현"], internal_domains=["corp.example"],
                          ignore_senders=["jira@"])
        self.store = Store(Path(self.tmp.name) / "w.sqlite", [ME], ["김도현"],
                           noise=self.cfg)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_card_hidden_below_threshold(self):
        # 발신 5통(<8) → 카드 없음
        self._seed_sent("kim@corp.example", 5, "타이밍 클로저 마진 검증 진행")
        d = web.render_dossier(self.store, self.cfg, "kim@corp.example")
        self.assertNotIn("업무 어휘 지도", d)

    def test_card_shows_wordmap_above_threshold(self):
        self._seed_sent("kim@corp.example", 10,
                        "타이밍 클로저 마진 재검증. 양자화 회귀 확인 부탁드립니다.")
        d = web.render_dossier(self.store, self.cfg, "kim@corp.example")
        self.assertIn("업무 어휘 지도", d)
        self.assertIn("class='wordmap'", d)
        self.assertIn("반복 구문", d)
        self.assertIn("발신 10통", d)
        self.assertIn("메일 단위 출현", d)
        self.assertIn("타이밍", d)
        self.assertRegex(d, r"href='/thread/\d+'")      # 표현별 근거
        self.assertNotIn("김도현", d)                 # 내 이름 제외
        self.assertNotIn("부탁드립니다", d)           # 상투어 제외

    def test_noise_sender_skips_wordmap(self):
        self._seed_sent("jira@corp.example", 12, "이슈 NPX-1 갱신되었습니다 담당자")
        d = web.render_dossier(self.store, self.cfg, "jira@corp.example")
        self.assertNotIn("업무 어휘 지도", d)         # 봇은 어휘 지도 생략

    def test_other_person_name_kept_own_name_dropped(self):
        # 주인공(kim) 발신 메일에 다른 사람(박서준)이 자주 나오면 그건 신호 →
        # 유지. 본인 이름(kim)만 서명 누출로 제외. (전원 이름 제거는 오설계였음)
        self._seed_sent("kim@corp.example", 10, "박서준 타이밍 재검증 kim 진행")
        d = web.render_dossier(self.store, self.cfg, "kim@corp.example")
        wordmap = d.split("업무 어휘 지도", 1)[1]
        self.assertIn("박서준", wordmap)               # 미등록 이름은 정보 손실 없이 유지
        self.assertIn("타이밍", wordmap)
        self.assertNotIn(">kim<", wordmap)            # 본인 이름은 칩에서 제외

    def test_only_counts_person_sent_not_mine(self):
        # 내가 이 사람에게 보낸 것(is_sent=1)은 대상 아님
        self._seed_sent("kim@corp.example", 3, "짧음")
        self.store.ingest([_rec("mine", ME, ["kim@corp.example"], "RE",
                                "2026-07-20T09:00:00", body="내 발신어 잔뜩 타이밍")])
        texts = self.store.person_sent_texts("kim@corp.example")
        self.assertEqual(len(texts), 3)              # kim 발신 3통만
        self.assertTrue(all("내 발신어" not in t for t in texts))


class TestWordMapAnalysis(unittest.TestCase):
    """6개월 업무 어휘 지도 — 대조·문서빈도·군집·추세·근거의 합성 회귀."""

    KIM = "kim@corp.example"
    LEE = "lee@corp.example"
    PARK = "park@corp.example"

    @staticmethod
    def _row(mid, addr, body, when, thread=None, subject=""):
        return {
            "id": mid, "thread_id": thread or mid, "subject": subject,
            "sender_name": addr.split("@")[0], "sender_addr": addr,
            "sent_on": when, "new_content": body,
        }

    @staticmethod
    def _all_terms(profile):
        out = {x["term"]: x for x in profile["terms"]}
        for cluster in profile["clusters"]:
            out.update({x["term"]: x for x in cluster["terms"]})
        return out

    def test_document_frequency_beats_repetition_and_common_terms(self):
        rows = []
        for i in range(8):
            rows.append(self._row(
                i + 1, self.KIM, "공통검증 양자화 양자화 양자화",
                f"2026-07-{i+1:02d}T09:00:00"))
            rows.append(self._row(
                i + 20, self.LEE, "공통검증 일정협의",
                f"2026-07-{i+1:02d}T10:00:00"))
        profile = terms.analyze(
            rows, self.KIM, names={self.KIM: "김민수", self.LEE: "이영희"})
        found = self._all_terms(profile)
        self.assertEqual(found["양자화"]["support"], 8)  # 24회가 아니라 메일 8통
        self.assertGreater(found["양자화"]["score"], found["공통검증"]["score"])

    def test_one_mail_repetition_is_not_a_characteristic(self):
        rows = [self._row(1, self.KIM, "칩렛 " * 20, "2026-07-01T09:00:00")]
        rows += [self._row(i, self.KIM, "패키지", f"2026-07-{i:02d}T09:00:00")
                 for i in range(2, 9)]
        profile = terms.analyze(rows, self.KIM)
        self.assertNotIn("칩렛", self._all_terms(profile))  # 지지 메일 1통

    def test_signature_is_removed_and_phrase_has_evidence(self):
        rows = [
            self._row(i, self.KIM,
                      "타이밍 클로저 결과입니다.\n--\n홍길동 책임\n전화: 1234",
                      f"2026-07-{i:02d}T09:00:00", thread=100 + i)
            for i in range(1, 5)
        ]
        profile = terms.analyze(rows, self.KIM, names={self.KIM: "홍길동"})
        phrases = {x["term"]: x for x in profile["phrases"]}
        self.assertIn("타이밍 클로저", phrases)
        self.assertTrue(phrases["타이밍 클로저"]["evidence"])
        self.assertNotIn("전화", self._all_terms(profile))
        self.assertNotIn("홍길동", self._all_terms(profile))

    def test_cooccurring_terms_form_separate_clusters(self):
        rows = [
            self._row(i, self.KIM, "타이밍 클로저", f"2026-07-{i:02d}T09:00:00")
            for i in range(1, 5)
        ]
        rows += [
            self._row(10 + i, self.KIM, "양자화 회귀",
                      f"2026-07-{i+4:02d}T09:00:00")
            for i in range(1, 5)
        ]
        profile = terms.analyze(rows, self.KIM)
        groups = [{x["term"] for x in c["terms"]} for c in profile["clusters"]]
        self.assertIn({"타이밍", "클로저"}, groups)
        self.assertIn({"양자화", "회귀"}, groups)

    def test_recent_rising_term_uses_six_week_overlay(self):
        rows = [
            self._row(i, self.KIM, "기존업무", f"2026-02-{i:02d}T09:00:00")
            for i in range(1, 6)
        ]
        rows += [
            self._row(10 + i, self.KIM, "기존업무 칩렛",
                      f"2026-07-{i:02d}T09:00:00")
            for i in range(1, 4)
        ]
        profile = terms.analyze(rows, self.KIM)
        self.assertIn("칩렛", {x["term"] for x in profile["rising"]})

    def test_known_person_mentions_are_separate_from_terms(self):
        rows = [
            self._row(i, self.KIM, "박서준과 양자화 검토", f"2026-07-{i:02d}T09:00:00")
            for i in range(1, 5)
        ]
        names = {self.KIM: "김민수", self.PARK: "박서준"}
        profile = terms.analyze(rows, self.KIM, names=names)
        self.assertEqual(profile["mentions"][0]["addr"], self.PARK)
        self.assertEqual(profile["mentions"][0]["support"], 4)
        self.assertNotIn("박서준", self._all_terms(profile))

    def test_precomputed_features_are_identical_to_raw_analysis(self):
        rows = [
            self._row(1, self.KIM, "타이밍 김민수 클로저 검토",
                      "2026-02-01T09:00:00", thread=10,
                      subject="RE: 양자화 검토"),
            self._row(2, self.KIM, "박서준과 칩렛 패키지",
                      "2026-07-01T09:00:00", thread=11,
                      subject="칩렛 일정"),
            self._row(3, self.LEE, "타이밍 일정 칩렛",
                      "2026-07-02T09:00:00", thread=12,
                      subject="칩렛 일정"),
        ]
        names = {self.KIM: "김민수", self.LEE: "이영희", self.PARK: "박서준"}
        expected = terms.analyze(
            rows, self.KIM, names=names, extra_stop=["일정"])
        prepared = [
            {k: v for k, v in row.items()
             if k not in ("new_content", "subject")}
            | {"term_features": terms.encode_features(
                row["new_content"], row["subject"])}
            for row in rows
        ]
        actual = terms.analyze(
            prepared, self.KIM, names=names, extra_stop=["일정"])
        self.assertEqual(actual, expected)

    def test_rolling_background_is_identical_to_full_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "aggregate.sqlite", [ME])
            try:
                records = []
                for i in range(1, 5):
                    records.append(_rec(
                        f"ak{i}", self.KIM, [ME], "칩렛 타이밍",
                        f"2026-07-{i:02d}T09:00:00",
                        "칩렛 패키지 타이밍"))
                    records.append(_rec(
                        f"al{i}", self.LEE, [ME], "칩렛 일정",
                        f"2026-07-{i:02d}T10:00:00",
                        "칩렛 일정 양자화"))
                store.ingest(records)
                names = store.word_people_names()
                all_rows = store.people_word_rows([self.KIM, self.LEE])
                expected = terms.analyze(
                    all_rows, self.KIM, names=names)
                target = store.person_word_bag_rows(self.KIM)
                candidates = terms.background_candidates(
                    target, self.KIM, names=names)
                background = store.people_word_background(
                    [self.KIM, self.LEE], self.KIM,
                    candidates=candidates)
                actual = terms.analyze(
                    target, self.KIM, names=names, background=background)
                self.assertEqual(actual, expected)

                lee_expected = terms.analyze(
                    all_rows, self.LEE, names=names)
                lee_rows = store.person_word_bag_rows(self.LEE)
                lee_candidates = terms.background_candidates(
                    lee_rows, self.LEE, names=names)
                lee_background = store.people_word_background(
                    [self.KIM, self.LEE], self.LEE,
                    candidates=lee_candidates)
                lee_actual = terms.analyze(
                    lee_rows, self.LEE, names=names,
                    background=lee_background)
                self.assertEqual(lee_actual, lee_expected)
                self.assertEqual(len(store._word_background_cache), 1)
            finally:
                store.close()

    def test_store_window_and_exact_corpus_cache_basis(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "wordmap.sqlite", [ME])
            try:
                store.ingest([
                    _rec("old", self.KIM, [ME], "구자료",
                         "2025-12-01T09:00:00", "구형어휘"),
                    _rec("new", self.KIM, [ME], "신자료",
                         "2026-07-19T09:00:00", "신규어휘"),
                    _rec("lee", self.LEE, [ME], "대조",
                         "2026-07-19T10:00:00", "대조어휘"),
                ])
                basis = store.person_word_basis(self.KIM)
                self.assertEqual(basis["mail_count"], 1)   # 6개월 밖 메일 제외
                rows = store.people_word_rows([self.KIM, self.LEE])
                old_id = store.db.execute(
                    "SELECT id FROM messages WHERE message_id='<old@t>'"
                ).fetchone()[0]
                self.assertNotIn(old_id, {r["id"] for r in rows})
                self.assertIsNone(store.db.execute(
                    "SELECT 1 FROM message_term_features WHERE message_id=?",
                    (old_id,)).fetchone())
                self.assertTrue(all(r["term_features"] for r in rows))
                self.assertEqual(store.person_sent_texts(self.KIM), ["신규어휘"])
                before = basis["basis_message_id"]
                fp_before = store.people_word_corpus_fingerprint(
                    [self.KIM, self.LEE])
                version = f"test-v1:{fp_before}"
                cached = {"mail_count": 1, "terms": [{"term": "신규어휘"}]}
                store.save_people_word_profile(
                    self.KIM, cached, basis, 26, version)
                self.assertEqual(
                    store.people_word_profile(
                        self.KIM, basis, 26, version), cached)
                store.ingest([_rec("lee2", self.LEE, [ME], "대조2",
                                   "2026-07-19T11:00:00", "새 대조어휘")])
                unchanged = store.person_word_basis(self.KIM)
                self.assertEqual(unchanged["basis_message_id"], before)
                fp_other = store.people_word_corpus_fingerprint(
                    [self.KIM, self.LEE])
                self.assertNotEqual(fp_other, fp_before)
                self.assertIsNone(store.people_word_profile(
                    self.KIM, unchanged, 26, f"test-v1:{fp_other}"))
                store.ingest([_rec("new2", self.KIM, [ME], "신자료2",
                                   "2026-07-19T12:00:00", "새 특징어")])
                changed = store.person_word_basis(self.KIM)
                self.assertGreater(changed["basis_message_id"], before)
                self.assertIsNone(store.people_word_profile(
                    self.KIM, changed, 26, "test-v1"))
            finally:
                store.close()

    def test_corpus_fingerprint_distinguishes_equal_id_aggregates(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "fingerprint.sqlite", [ME])
            try:
                senders = (
                    self.KIM, self.KIM, self.LEE,
                    self.LEE, self.KIM, self.KIM,
                )
                store.ingest([
                    _rec(f"fp{i}", sender, [ME], f"안건 {i}",
                         f"2026-07-{i:02d}T09:00:00", "공통 본문")
                    for i, sender in enumerate(senders, 1)
                ])
                first = store.people_word_corpus_fingerprint([self.KIM])

                # {1,2,5,6}과 {1,3,4,6}은 count/min/max/sum이 모두 같다.
                store.db.execute(
                    "UPDATE messages SET sender_addr=? WHERE id IN (2, 5)",
                    (self.LEE,))
                store.db.execute(
                    "UPDATE messages SET sender_addr=? WHERE id IN (3, 4)",
                    (self.KIM,))
                second = store.people_word_corpus_fingerprint([self.KIM])
                self.assertNotEqual(first, second)
            finally:
                store.close()

    def test_legacy_term_features_backfill_only_during_sync(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy-wordmap.sqlite"
            store = Store(path, [ME])
            store.ingest([
                _rec("legacy1", self.KIM, [ME], "검토",
                     "2026-07-01T09:00:00", "타이밍 클로저"),
                _rec("legacy2", self.LEE, [ME], "일정",
                     "2026-07-02T09:00:00", "양자화 회귀"),
                _rec("legacy-mine", ME, [self.KIM], "회신",
                     "2026-07-03T09:00:00", "내 발신 본문"),
            ])
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM message_term_features").fetchone()[0], 2)
            store.db.execute("DELETE FROM message_term_features")
            store.db.execute(
                "DELETE FROM sync_state WHERE key='term_feature_version'")
            store.db.commit()
            store.close()

            reopened = Store(path, [ME])
            try:
                self.assertFalse(reopened._term_features_ready)
                self.assertEqual(reopened.db.execute(
                    "SELECT COUNT(*) FROM message_term_features").fetchone()[0], 0)
                raw_rows = reopened.people_word_rows([self.KIM, self.LEE])
                self.assertIn("new_content", raw_rows[0].keys())
                reopened.ingest([])  # Outlook sync 진입점에서 기존 본문 1회 백필
                self.assertTrue(reopened._term_features_ready)
                self.assertEqual(reopened.db.execute(
                    "SELECT COUNT(*) FROM message_term_features").fetchone()[0], 2)
                prepared = reopened.people_word_rows([self.KIM, self.LEE])
                self.assertIn("term_features", prepared[0].keys())
                self.assertNotIn("new_content", prepared[0].keys())
            finally:
                reopened.close()

    def test_rolling_window_subtracts_expired_message_df(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "rolling-wordmap.sqlite", [ME])
            try:
                store.ingest([
                    _rec("expires", self.LEE, [ME], "만료",
                         "2026-01-01T09:00:00", "만료어휘"),
                    _rec("basis", self.KIM, [ME], "기준",
                         "2026-06-30T09:00:00", "유지어휘"),
                ])
                self.assertEqual(store.db.execute(
                    """SELECT mail_df FROM person_term_window
                       WHERE sender_addr=? AND term='만료어휘' AND kind='term'""",
                    (self.LEE,)).fetchone()[0], 1)

                # latest가 7월 3일로 이동하면 26주 시작은 1월 2일이 된다.
                store.ingest([
                    _rec("advance", ME, [self.KIM], "내 발신",
                         "2026-07-03T09:00:00", "창 이동"),
                ])
                self.assertIsNone(store.db.execute(
                    """SELECT 1 FROM person_term_window
                       WHERE sender_addr=? AND term='만료어휘'""",
                    (self.LEE,)).fetchone())
                expired_id = store.db.execute(
                    "SELECT id FROM messages WHERE message_id='<expires@t>'"
                ).fetchone()[0]
                for table in (
                        "message_term_features", "message_term_bags",
                        "message_term_subject_delta"):
                    self.assertIsNone(store.db.execute(
                        f"SELECT 1 FROM {table} WHERE message_id=?",
                        (expired_id,)).fetchone(), msg=table)
            finally:
                store.close()

    def test_projection_setting_change_falls_back_until_sync(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(
                home=Path(td), my_addresses=[ME],
                raw={"dossier": {"word_stop_extra": []}})
            store = Store(Path(td) / "projection.sqlite", [ME], noise=cfg)
            try:
                store.ingest([
                    _rec(f"setting{i}", self.KIM, [ME], f"안건 {i}",
                         f"2026-07-{i:02d}T09:00:00", "타이밍 클로저")
                    for i in range(1, 4)
                ])
                self.assertIsNotNone(store.person_word_bag_rows(self.KIM))

                cfg.raw["dossier"]["word_stop_extra"] = ["타이밍"]
                self.assertIsNone(store.person_word_bag_rows(self.KIM))
                fallback = store.people_word_rows([self.KIM])
                self.assertIn("term_features", fallback[0].keys())

                store.ingest([])
                rebuilt = store.person_word_bag_rows(self.KIM)
                self.assertIsNotNone(rebuilt)
                profile = terms.analyze(
                    rebuilt, self.KIM, extra_stop=["타이밍"],
                    background={"mail_count": 0})
                self.assertNotIn("타이밍", self._all_terms(profile))
            finally:
                store.close()

    def test_candidate_df_query_uses_term_first_primary_key(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "candidate-plan.sqlite", [ME])
            try:
                store.ingest([
                    _rec(f"plan{i}", self.KIM if i % 2 else self.LEE, [ME],
                         f"안건 {i}", f"2026-07-{i:02d}T09:00:00",
                         f"타이밍 클로저 고유어휘{i}")
                    for i in range(1, 9)
                ])
                target = store.person_word_bag_rows(self.KIM)
                candidates = terms.background_candidates(target, self.KIM)
                store.people_word_background(
                    [self.KIM, self.LEE], self.KIM,
                    candidates=candidates)
                plan = [
                    row[3] for row in store.db.execute(
                        """EXPLAIN QUERY PLAN
                           SELECT d.kind, d.term, d.mail_df
                           FROM word_term_candidates c
                           CROSS JOIN person_term_window d
                           WHERE d.kind=c.kind AND d.term=c.term
                             AND d.sender_addr IN (?, ?)""",
                        (self.KIM, self.LEE))
                ]
                self.assertTrue(any(
                    "SEARCH d USING PRIMARY KEY" in step for step in plan), plan)
                self.assertFalse(any(
                    "SCAN d" in step or "USE TEMP B-TREE" in step
                    for step in plan), plan)
            finally:
                store.close()


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

    def test_intervention_ai_stages_removed(self):
        # 개입 AI 분류·우선순위(haiku 2콜)는 2026-07-30 제거 — '지금 할 일' 큐
        # 폐지로 소비처가 사라졌다. 회고 파이프라인에 되돌아오면 안 된다.
        self.assertFalse(hasattr(review, "ai_classify_intervention"))
        self.assertFalse(hasattr(review, "ai_refine_intervention"))
        self.assertFalse(hasattr(review, "apply_saved_ai"))

    def test_candidates_collected_for_borderline(self):
        # 요청 약한 경계 항목(종결/FYI)은 결정론에서 빠지되 candidates 로 남는다
        self.store.ingest([self._r("c", "kim@corp.example", [ME], "리뷰완료",
            "2026-07-19T09:00:00", "요청하신 리뷰 완료했습니다. 코멘트 확인 바랍니다.")])
        tid, un = self._un1("리뷰완료")
        queue, cands = review.intervention_queue(
            self.store, self.cfg, "2026-07-20", unanswered=un, return_candidates=True)
        self.assertEqual(queue, [])                              # 결정론 제외
        self.assertEqual([c["thread_id"] for c in cands], [tid])  # 후보로 보존











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



class TestAIAuthError(unittest.TestCase):
    """인증 만료(AWS SSO 류) — 재시도 없이 즉시 안내하고 멈춘다(2026-07-31).

    AIAuthError 가 AIError 하위가 아닌 이유는 AICancelled 와 같다: 콜 단위
    graceful 삼킴(except AIError)을 전부 통과해 꼭대기에서 '안내+중단'해야 한다.
    자동 폴백은 하지 않는다(사용자 결정 — 몰래 백엔드를 갈아타지 않는다)."""

    SSO = ("AI 호출 실패 (exit 1): claude -p\n"
           "Error: The SSO session associated with this profile has expired. "
           "To refresh this SSO session run 'aws sso login'.")

    def test_classifier_judges_backend_channel_only(self):
        # 판정 근거는 detail(stderr·CLI 오류 봉투)뿐이다.
        for s in (self.SSO,
                  "ExpiredTokenException: token expired",
                  "The security token included in the request is expired",
                  "Unable to locate credentials"):
            e = review._ai_error("AI 호출 실패", s)
            self.assertIsInstance(e, review.AIAuthError, msg=s[:40])
            self.assertIn("aws sso login", str(e))
            self.assertIn("근거:", str(e))          # 판정 근거 줄을 보여준다
        # 무관한 실패는 그대로 AIError — 오진 방지
        for s in ("AI 호출 시간 초과 (300s)", "exit 1: 알 수 없는 오류",
                  "prompt is too long", "429 rate limit"):
            self.assertIsInstance(review._ai_error("실패", s), review.AIError,
                                  msg=s)
            self.assertNotIsInstance(review._ai_error("실패", s),
                                     review.AIAuthError)

    def test_model_output_never_triggers_auth_verdict(self):
        # stdout 은 모델이 사용자 메일을 요약한 텍스트다. 본문에 인증 문구가
        # 있다고 인증 만료로 오진하면 재시도 생략 + 파이프라인 중단 + 틀린
        # 안내가 된다(2026-07-31 리뷰 실증). msg 는 판정에 쓰지 않는다.
        leaked = ("AI 호출 실패 (exit 1): claude\n[#41] 공지 요약: IT팀이 "
                  "'our AWS credentials have expired' 라고 알렸고 재발급 예정")
        e = review._ai_error(leaked, "")
        self.assertIsInstance(e, review.AIError)
        self.assertNotIsInstance(e, review.AIAuthError)
        # 빈 msg 도 안전(과거 splitlines()[-1] 이 IndexError)
        self.assertIsInstance(review._ai_error("", "SSO session expired"),
                              review.AIAuthError)

    def test_fatal_failure_card_says_stopped_not_continuing(self):
        # 인증 만료의 failed 이벤트에 "이어서 진행"이 붙으면 안내를 부정한다
        job = web._new_job(); job["running"] = True     # 콜백은 도는 잡에만 반영
        web._job_stream_event(job, threading.Lock())(
            {"ev": "failed", "fatal": True, "error": review.AUTH_DEAD_HINT})
        line = web._job_live_line(job)
        self.assertIn("중단됨", line)
        self.assertIn("aws sso login", line)
        self.assertNotIn("이어서 진행", line)
        # 일반 실패는 종전 문구 그대로(weekly 는 실제로 이어서 진행한다)
        job2 = web._new_job(); job2["running"] = True
        web._job_stream_event(job2, threading.Lock())(
            {"ev": "failed", "error": "타임아웃"})
        self.assertIn("이어서 진행", web._job_live_line(job2))
        # 재시도가 시작되면 치명 표시는 걷힌다(낡은 안내 금지)
        web._job_stream_event(job, threading.Lock())(
            {"ev": "retry", "attempt": 1, "total": 2, "wait": 2})
        self.assertFalse(job["fatal"])

    def test_dossier_screen_shows_auth_guidance(self):
        # 인물 요약은 '잠시 후 다시' 고정 문구를 쓰고 있었다 — SSO 만료에선
        # 정반대 조언이라 잡이 남긴 사유를 우선한다.
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        store = Store(Path(home.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        store.ingest([_rec("a", "kim@x", [ME], "건", "2026-07-01T09:00:00")])
        try:
            with web._dossier_lock:
                web._dossier_job.update(running=False, addr="kim@x",
                                        stage="error", done_at=time.time(),
                                        error=review.AUTH_DEAD_HINT)
            out = web.render_dossier(store, cfg, "kim@x")
            self.assertIn("aws sso login", out)
            self.assertNotIn("잠시 후 다시 눌러 주세요", out)
        finally:
            with web._dossier_lock:
                web._dossier_job.update(addr="", stage="", error="",
                                        done_at=0.0)

    def test_empty_output_with_sso_stderr_detected(self):
        # 07-28 실측: stdout 이 비고 exit 0 — stderr 문자열로도 판정한다
        e = review._ai_error("AI 응답이 비어 있음",
                             "SSO session expired, run aws sso login")
        self.assertIsInstance(e, review.AIAuthError)

    def test_ai_run_fails_fast_without_retries(self):
        calls, events = [], []

        def dead(cmd, prompt, timeout):
            calls.append(1)
            raise review.AIAuthError(review.AUTH_DEAD_HINT)

        with mock.patch.object(review, "_ai_run_once", side_effect=dead), \
             mock.patch.object(review.time, "sleep",
                               side_effect=AssertionError("백오프 금지")):
            with self.assertRaises(review.AIAuthError):
                review.ai_run(["x"], "p", retries=2, on_event=events.append)
        self.assertEqual(len(calls), 1)          # 재시도 0 — 죽은 백엔드 3연타 금지
        self.assertEqual(events[-1]["ev"], "failed")
        self.assertIn("인증 만료", events[-1]["error"])

    def test_run_ai_layer_aborts_remaining_stages(self):
        # 요약 단계에서 인증 만료 → 수확·디제스트·하루요약 전부 생략,
        # 결정론 데일리는 보존(note 만 남긴다)
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        store = Store(Path(home.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        det = review.deterministic(store, cfg)
        with mock.patch.object(review, "refresh_summaries",
                               side_effect=review.AIAuthError(
                                   review.AUTH_DEAD_HINT)) as rs, \
             mock.patch.object(review, "ai_digest") as dg, \
             mock.patch.object(review, "ai_exec_summary") as ex:
            ai_text, note = review.run_ai_layer(store, cfg, det)
        self.assertIn("AI 중단", note)
        self.assertIn("aws sso login", note)
        dg.assert_not_called()
        ex.assert_not_called()
        self.assertTrue(review.render(det))       # 결정론 렌더 생존

    def test_weekly_job_does_not_overwrite_report_on_auth_stop(self):
        # 인증 만료로 AI 가 중단되면 뼈대만 남는데, 그걸로 기존 보고서(AI 서술
        # 포함)를 덮으면 손실이다. 잡은 기존 파일을 지키고 사유를 보고한다.
        # (report_path 부재로 AttributeError 가 나던 회귀도 여기서 막는다)
        import mailkb.weekly as weekly_mod
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        target = cfg.vault / "weekly" / "2026-07-31.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 기존 보고서\n\nAI 서술 포함", encoding="utf-8")

        def stopped(store, cfg_, **kw):
            return "# 뼈대만", {"end": "2026-07-31", "stat": {}, "items": [],
                              "ai_error": review.AUTH_DEAD_HINT}

        with web._weekly_lock:
            web._weekly_job.update(running=True, stage="", error="",
                                   weeks=1, date="")
        try:
            with mock.patch.object(weekly_mod, "generate", side_effect=stopped):
                web._run_weekly_job(cfg, 1, threading.Event())
            with web._weekly_lock:
                st = dict(web._weekly_job)
            self.assertFalse(st["running"])
            self.assertEqual(st["stage"], "error")
            self.assertIn("aws sso login", st["error"])
            self.assertIn("기존 보고서를 유지", st["error"])
            self.assertTrue(target.read_text(encoding="utf-8")
                            .startswith("# 기존 보고서"))
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", error="")
        # 기존 보고가 없으면 뼈대라도 쓴다(정보 0보다 낫다)
        target.unlink()
        with web._weekly_lock:
            web._weekly_job.update(running=True, stage="", error="",
                                   weeks=1, date="")
        try:
            with mock.patch.object(weekly_mod, "generate", side_effect=stopped):
                web._run_weekly_job(cfg, 1, threading.Event())
            self.assertTrue(target.exists())
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", error="")

    def test_weekly_keeps_skeleton_with_banner(self):
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        store = Store(Path(home.name) / "t.sqlite", [ME])
        self.addCleanup(store.close)
        store.ingest([_rec("a", "kim@x", [ME], "협상", "2026-07-01T09:00:00")])
        import mailkb.weekly as weekly_mod
        with mock.patch.object(weekly_mod, "run_ai_layer",
                               side_effect=review.AIAuthError(
                                   review.AUTH_DEAD_HINT)):
            md, det = weekly_mod.generate(store, cfg, ai=True)
        self.assertIn("주간 보고", md)             # 뼈대 생존
        self.assertIn("aws sso login", md)        # 머리 배너로 원인 노출
        self.assertIn("AI 보강 없이 뼈대만", md)
        self.assertIn("ai_error", det)

    def test_all_web_jobs_release_slot_with_clean_message(self):
        # 단일 슬롯 설계라 잡이 running=True 로 남으면 이후 모든 AI 기능이
        # 죽는다. 또 repr(e) 로 새면 "AIAuthError('⚠ …')" 가 사용자에게 보인다.
        import mailkb.distill as distill_mod
        import mailkb.weekly as weekly_mod
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        err = review.AIAuthError(review.AUTH_DEAD_HINT)

        def check(job, lock, fn, seed):
            with lock:
                job.update(**seed, running=True, error="")
            try:
                fn()
                with lock:
                    self.assertFalse(job["running"])
                    msg = str(job.get("error") or job.get("msg") or "")
                self.assertIn("aws sso login", msg)
                self.assertNotIn("AIAuthError", msg)  # repr 유출 금지
            finally:                                  # 전역 잡 dict — 잔재 금지
                with lock:
                    job.update(running=False, stage="", error="", msg="",
                               addr="", done_at=0.0)

        with mock.patch.object(distill_mod, "refresh_person_dossier",
                               side_effect=err):
            check(web._dossier_job, web._dossier_lock,
                  lambda: web._run_dossier_job(cfg, "a@x", "A",
                                               threading.Event()),
                  dict(addr="a@x", name="A", stage=""))
        with mock.patch.object(weekly_mod, "generate", side_effect=err):
            check(web._weekly_job, web._weekly_lock,
                  lambda: web._run_weekly_job(cfg, 1, threading.Event()),
                  dict(stage=""))
        with mock.patch.object(review, "run_ai_layer", side_effect=err), \
             mock.patch.object(review, "deterministic", return_value={
                 "date": "2026-07-31", "received_count": 0, "sent": [],
                 "closed_by_me": [], "intervention": [], "digest": {},
                 "harvest": None}):
            check(web._review_job, web._review_lock,
                  lambda: web._run_review_job(cfg, True, "2026-07-31",
                                              threading.Event()),
                  dict(msg="", step=0, total=4, date="2026-07-31", ai=True))
        with mock.patch.object(review, "ai_search", side_effect=err):
            check(web._aisearch_job, web._aisearch_lock,
                  lambda: web._run_aisearch_job(cfg, "q", "2026-07-31", True,
                                                threading.Event()),
                  dict(stage="", query="q"))

    def test_web_ask_job_falls_back_with_guidance(self):
        # 웹 분석 잡 — 인증 만료가 잡 error 로 남고(크래시 없음) 폴백 배너에 안내
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        from mailkb import ask as ask_engine
        with mock.patch.object(ask_engine, "ask",
                               side_effect=review.AIAuthError(
                                   review.AUTH_DEAD_HINT)):
            with web._ask_lock:
                web._ask_job.update(running=True, stage="s", question="q",
                                    parent=None, person="", mail=None,
                                    token="t", result=None, error="")
            try:
                web._run_ask_job(cfg, "q", None, "", threading.Event())
                st = dict(web._ask_job)
            finally:
                with web._ask_lock:
                    web._ask_job.update(running=False, error="", question="")
        self.assertFalse(st["running"])
        self.assertIn("aws sso login", st["error"])


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
        # config 에 [ai.backends.*] 가 없어도 sonnet/haiku/opus/internal 은 해결됨
        cfg = Config(home=self.home, my_addresses=[ME])  # ai_backends 비어 있음
        self.assertEqual(cfg.ai_cmd("sonnet"), ["claude", "-p", "--model", "sonnet"])
        self.assertEqual(cfg.ai_cmd("haiku"), ["claude", "-p", "--model", "haiku"])
        self.assertEqual(cfg.ai_cmd("opus"), ["claude", "-p", "--model", "opus"])
        self.assertEqual(cfg.ai_cmd("internal"), ["opencode", "run"])
        with self.assertRaises(SystemExit):
            cfg.ai_cmd("ghost")                       # 미지의 이름은 여전히 실패
        # config 값이 있으면 그게 내장보다 우선
        cfg2 = Config(home=self.home, my_addresses=[ME],
                      ai_backends={"sonnet": {"cmd": ["X"]}})
        self.assertEqual(cfg2.ai_cmd("sonnet"), ["X"])

    def test_run_ai_layer_aierror_note_and_stages(self):
        # 오늘 날짜로 실행 — 과거 날짜는 백필 한정(분류·정리·도시에 생략)이라 별도 테스트
        d = date.today().isoformat()
        self.store.ingest([_rec("g1", "kim@corp.example", [ME], "건",
                                f"{d}T09:00:00")])
        det = review.deterministic(self.store, self.cfg, d)
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
        self.assertIn("하루 요약 작성 중…", stages)         # Executive Summary
        # 인물 요약(2026-07-29)과 개입 분류·정리(2026-07-30)는 여기 없다 —
        # 전자는 인물 화면 버튼으로, 후자는 '지금 할 일' 폐지와 함께 제거.
        self.assertNotIn("인물 도시에 갱신 중…", stages)
        self.assertNotIn("개입 큐 AI 분류 중…", stages)
        self.assertEqual(len(stages), 5)                    # 4단계 + '완료' 

    def test_run_ai_layer_backfill_same_four_stages(self):
        # 개입 분류·정리 제거(2026-07-30) 후 백필도 오늘과 같은 4단계다 —
        # 진행 바 total 이 하나(4)로 굳었으니 단계 수가 어긋나면 안 된다.
        self.store.ingest([_rec("b1", "kim@corp.example", [ME], "과거요청건",
                                "2026-07-01T09:00:00", body="회신 부탁드립니다.")])
        det = review.deterministic(self.store, self.cfg, "2026-07-01")
        stages = []
        with mock.patch.object(review, "ai_run", return_value="(응답)"):
            review.run_ai_layer(self.store, self.cfg, det,
                                persist_date="2026-07-01",
                                progress=stages.append)
        self.assertEqual(stages[0], "누적 요약 갱신 중…")
        self.assertIn("결정·신호 수확 중…", stages)
        self.assertIn("오늘 메일 핵심 요약 중…", stages)
        self.assertIn("하루 요약 작성 중…", stages)
        self.assertEqual(stages[-1], "완료")
        self.assertEqual(len(stages), 5)                    # 4단계 + '완료' 

    def _seed_for_stages(self, day="2026-07-20"):
        """여러 단계가 실제로 AI 를 부르도록 메일을 심는다 — 빈 저장소면
        요약·수확·디제스트가 콜 전에 반환해 배관 검증이 1콜에 그친다."""
        self.store.ingest([
            _rec("s1", "kim@corp.example", [ME], "스펙 검토 요청",
                 f"{day}T09:00:00", body="검토 의견 부탁드립니다. 회신 주세요."),
            _rec("s2", "lee@corp.example", [ME], "일정 확정 요청",
                 f"{day}T10:00:00", body="5월 8일로 확정하려 합니다. 확인 부탁드립니다."),
        ])
        return review.deterministic(self.store, self.cfg, day)

    def test_run_ai_layer_forwards_on_event_and_cancel(self):
        # 회고도 다른 잡과 같은 대기 카드를 쓰려면 수신 이벤트가 흘러야 한다.
        # 여러 단계가 실제로 콜을 내는 코퍼스로 검증한다(1콜만 도는 빈 저장소로는
        # 나머지 체인의 배관 누락을 못 잡는다).
        det = self._seed_for_stages()
        seen = []

        def fake_ai(cmd, prompt, **kw):
            seen.append((kw.get("on_event"), kw.get("cancel")))
            return "(응답)"

        ev, cancel = [], threading.Event()
        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            review.run_ai_layer(self.store, self.cfg, det,
                                on_event=ev.append, cancel=cancel)
        self.assertGreaterEqual(len(seen), 3)   # 요약·수확·디제스트… 여러 단계
        for on_event, got_cancel in seen:
            self.assertIsNotNone(on_event)      # 모든 콜이 이벤트를 흘린다
            self.assertIs(got_cancel, cancel)

    def test_run_ai_layer_cancel_aborts_remaining_stages(self):
        # 취소는 실패가 아니다 — 어느 한 단계가 AIError 로 삼키면 남은 단계가
        # 계속 돌아 중지가 무의미해진다. 첫 콜에서 취소시키고 그 뒤 단계가
        # 하나도 안 도는지 본다.
        det = self._seed_for_stages()
        stages, calls = [], []

        def fake_ai(cmd, prompt, **kw):
            calls.append(prompt)
            raise review.AICancelled("중지")

        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            with self.assertRaises(review.AICancelled):
                review.run_ai_layer(self.store, self.cfg, det,
                                    progress=stages.append)
        self.assertEqual(len(calls), 1)         # 첫 콜에서 파이프라인이 선다
        self.assertNotIn("완료", stages)

    def test_run_ai_layer_routes_summary_backend_only(self):
        # 회고 파이프라인은 전부 summary 백엔드(sonnet)로 — classify(haiku)는
        # 개입 분류 제거(2026-07-30)와 함께 회고에서 더는 쓰지 않는다.
        cfg = Config(
            home=self.home, my_addresses=[ME], internal_domains=["corp.example"],
            ai_default="internal", ai_summary_backend="sonnet",
            ai_backends={"internal": {"cmd": ["I"]}, "sonnet": {"cmd": ["S"]},
                         "haiku": {"cmd": ["H"]}},
            raw={"ai": {"summary_min_msgs": 1}},
        )
        d = date.today().isoformat()
        self.store.ingest([_rec("q1", "kim@corp.example", [ME], "요청건",
                                f"{d}T09:00:00", body="회신 부탁드립니다.")])
        det = review.deterministic(self.store, cfg, d)
        seen = []   # (cmd0, prompt)

        def fake_run(cmd, prompt, **kw):
            seen.append((cmd[0], prompt))
            return "(응답)"

        with mock.patch.object(review, "ai_run", side_effect=fake_run):
            review.run_ai_layer(self.store, cfg, det, persist_date=d)
        cmds = {c for c, _ in seen}
        self.assertEqual(cmds, {"S"})            # 요약 계열만, 전부 sonnet
        summary_cmds = {c for c, p in seen if "스레드의 요약을 관리" in p}
        self.assertEqual(summary_cmds, {"S"})

    def test_exec_summary_prompt_facts_and_graceful(self):
        det = {
            "date": "2026-07-20", "received_count": 5,
            "sent": [{"sent_on": "2026-07-20T09:12:00", "subject": "RE: A",
                      "to_addrs": "kim@x"}],
            "closed_by_me": [{"thread_id": 4, "subject": "D요청"}],
            "intervention": [
                {"category": "respond", "thread_id": 7, "who": "김",
                 "subject": "회신건", "days": 1, "personal": True, "tag": "⏰",
                 "snippet": "내일까지 회신 부탁드립니다"}],
            "harvest": {"delta": ["B안 확정 (#2)"], "decisions": [
                {"thread_id": 9, "title": "C안 채택", "decider": "박"}]},
            "digest": {"work": [
                {"thread_id": 3, "subject": "흐름건", "who": "이",
                 "is_sent": False, "lead": "리드", "ai_core": ""}]},
        }
        prompts = []

        def fake(cmd, prompt, **kw):
            prompts.append(prompt)
            return "하루 요약입니다 (#7)."

        det["headline"] = {"thread_id": 7, "subject": "회신건", "state": "내 차례",
                           "state_note": "내 답이 없다", "people": {"김": 2}}
        with mock.patch.object(review, "ai_run", side_effect=fake):
            out = review.ai_exec_summary(self.store, self.cfg, det)
        self.assertEqual(out, ("하루 요약입니다 (#7).", "ok"))
        p = prompts[0]
        # 이미 추출된 사실만 — 활동(발신·종결)·흐름. '지금 할 일' 블록은 신호
        # 노출 폐지(2026-07-30)로 프롬프트에서도 뺐다(요약이 판정을 재노출 금지).
        self.assertNotIn("지금 할 일", p)
        self.assertNotIn("회신 필요", p)
        self.assertNotIn("⏰기한", p)
        self.assertIn("보낸 메일 1건", p)
        self.assertIn("- 09:12 RE: A", p)
        self.assertIn("내 회신으로 요청 종결: [#4] D요청", p)
        self.assertIn("B안 확정 (#2)", p)
        self.assertIn("[#9] C안 채택 (박)", p)
        # 대상 1건이 프롬프트의 중심이다(결정론 선정, 문장만 AI)
        self.assertIn("[오늘의 한 건]", p)
        self.assertIn("[#7] 회신건", p)
        self.assertIn("문체 표본", p)
        # '그 외 흐름'은 뺐다 — 3~5문장으로 전부 훑던 옛 계약의 재료였다
        self.assertNotIn("[#3] 흐름건 — 리드", p)
        # 실패와 '고를 것이 없음'은 다른 사실이다
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AIError("x")):
            self.assertEqual(review.ai_exec_summary(self.store, self.cfg, det),
                             ("", "failed"))
        cfg_none = Config(home=self.home, my_addresses=[ME],
                          ai_summary_backend="ghost")
        self.assertEqual(review.ai_exec_summary(self.store, cfg_none, det,
                                                backend="ghost"), ("", "failed"))
        # 고를 만한 한 건이 없으면 호출하지 않는다 = 특이사항 없음
        det.pop("headline")
        self.assertEqual(review.ai_exec_summary(self.store, self.cfg, det),
                         ("", "none"))

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

    def test_cli_init_works_before_config_exists(self):
        # AI 실패 로그 목적지 주입(cli.main)이 init 전의 config.load 가 던지는
        # SystemExit 에 죽으면 안 된다 — except Exception 은 SystemExit 을 못
        # 잡아 init 자체가 불가능해지는 회귀가 실제로 있었다(2026-07-28 e2e).
        import io

        from mailkb import cli
        old = review.AI_ERROR_LOG_DIR
        try:
            with tempfile.TemporaryDirectory() as t, \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                cli.main(["--home", t, "init"])
                self.assertTrue((Path(t) / "config.toml").exists())
        finally:
            review.AI_ERROR_LOG_DIR = old

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
        # 홈 렌더는 결정론 데일리를 배경 스레드로 재생성한다(lazy-on-view) —
        # 그 쓰기가 정리와 겹치면 'Directory not empty' 로 죽는다. 테스트가
        # 잡을 대상은 렌더 결과이지 정리 경합이 아니다.
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def test_home_is_analysis_chat(self):
        # 홈(/) = 분석 대화(2026-07-26). 구 대시보드(지금 할 일·개입·오늘 핵심)
        # 는 제거 — 개입 신호는 메일함 칩·x 키, 회수는 메일함 '확인 후보' 폴드.
        title, inner, code, pane = self.web.route(
            self.store, self.cfg, "/", {}, "2026-07-04")
        self.assertEqual((title, code, pane), ("분석", 200, "right"))
        self.assertIn("무엇이 궁금하세요", inner)     # 분석 랜딩
        self.assertIn("class='chatbar'", inner)
        for gone in ("지금 할 일", "그 외 개입", "오늘 메일 핵심", "확인 후보"):
            self.assertNotIn(gone, inner)
        # 구 개입 경로도 홈(분석)으로 흡수 유지
        _, inner2, _, pane2 = self.web.route(
            self.store, self.cfg, "/lens/intervene", {}, "2026-07-04")
        self.assertEqual(pane2, "right")
        self.assertIn("chatbar", inner2)

    def test_home_landing_next_up(self):
        # 랜딩 상태줄 '이어서 볼 것' — 장기기억·주간 보고·자주 왕래 인물
        out = self.web._ask_landing(self.store, self.cfg)
        self.assertIn("이어서 볼 것", out)
        self.assertIn("/records?tab=decisions", out)   # 장기기억
        self.assertIn("결정 <b>0</b>", out)
        self.assertIn("/records?tab=weekly", out)      # 주간 보고
        self.assertIn("자주 왕래", out)                # 인물 칩 → 인물 분석 동선
        self.assertIn("/people?addr=", out)
        tid = self.store.message("1")["thread_id"]
        self.store.add_decision(tid, "2026-07-04", "X 확정")   # candidate
        out2 = self.web._ask_landing(self.store, self.cfg)
        self.assertIn("제안 1", out2)

    def test_nav_has_back_button(self):
        # 앱 모드(--app)엔 브라우저 뒤로 버튼이 없다 — nav ← 가 history.back()
        # 호출. 앱 내부 depth=0 에서는 외부로 나가지 않고, 좌우 상태를 함께 복원한다.
        nav = self.web._NAV
        self.assertIn("class='navback'", nav)
        self.assertIn("type='button'", nav)             # <a> 아님 — markNav 무충돌
        self.assertLess(nav.index(">통계</a>"), nav.index("navback"))
        self.assertLess(nav.index("navback"), nav.index("navsearch"))  # 검색창 왼쪽
        self.assertIn("appDepth > 0) history.back()", self.web._APP_JS)
        self.assertIn("leftUrl: leftCur", self.web._APP_JS)
        self.assertIn("rightUrl: rightCur", self.web._APP_JS)
        self.assertIn("Promise.all(jobs)", self.web._APP_JS)
        # 통계 전폭 페이지(app.js 미로드)도 동작 — report.js 에 같은 배선
        self.assertIn("closest('.navback')) history.back()", report.REPORT_JS)

    def test_nav_has_sync_icon(self):
        # 수동 동기화는 전역 아이콘(↻) — 홈 대시보드의 버튼을 대체
        self.assertIn("class='navsync' method='post' action='/sync'", self.web._NAV)
        self.assertIn("메일 동기화", self.web._NAV)    # title/aria-label
        self.assertLess(self.web._NAV.index("navsearch"),
                        self.web._NAV.index("navsync"))
        self.assertLess(self.web._NAV.index("navsync"),
                        self.web._NAV.index("gear"))

    def test_daily_page_has_ai_button_only(self):
        # 일간 회고 페이지엔 'AI 회고'(ai=1) 버튼 하나. '기록만 남기기'는 제거.
        out = self.web.render_daily(self.cfg, "2026-07-04", "2026-07-04")
        self.assertIn("AI 회고", out)
        self.assertIn("action='/review'", out)
        self.assertIn("value='1'", out)              # ai=1 (AI 계층 포함)
        self.assertNotIn("기록만 남기기", out)

    def test_review_button_forms_single_ai(self):
        forms = self.web._review_button_forms()
        self.assertEqual(forms.count("<button"), 1)
        self.assertIn("AI 회고", forms)
        self.assertIn("class='aibtn ghost'", forms)
        self.assertNotIn("오늘 메일 정리", forms)
        self.assertNotIn("class='refine'", self.web._CSS)
        self.assertNotIn("/refine", self.web._APP_JS)

    def test_daily_ai_button_carries_viewed_date(self):
        # 과거 날짜 페이지의 'AI 회고'는 그 날짜를 실어 보낸다 → 그 날짜가 갱신됨
        out = self.web.render_daily(self.cfg, "2026-07-01", today="2026-07-22")
        self.assertIn("name='date' value='2026-07-01'", out)
        self.assertIn("action='/review'", out)
        # 날짜 미지정 폼(구 진입점)은 date 필드 없음 — 서버가 오늘로 처리
        self.assertNotIn("name='date'", self.web._review_button_forms())

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
        # 통계도 다른 메뉴와 동일한 상단 셸(Minerva·분석·메일함…), 본문만 통계
        page = self.web.render_stats_page(self.store, self.cfg)
        self.assertIn("<header class='top'>", page)
        self.assertIn("<span class='brand'>Minerva</span>", page)
        for menu in ("분석", "메일함", "스레드", "검색", "기억", "통계"):
            self.assertIn(menu, page, msg=menu)
        self.assertIn("통계 분석", page)          # 본문 제목
        self.assertNotIn("검토 기간", page)       # 기간 선택 바 폐지(2026-08-02)
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
        cases = [("/", "right"), ("/lens/intervene", "right"),  # 홈=분석(우측 대화록)
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
        self.assertIn('new URL(leftCur || "/mail", location.origin)', block)
        self.assertNotIn("location.pathname", block)
        self.assertIn("listDirty = true", block)
        self.assertIn('left.addEventListener("scroll"', js)
        self.assertIn("if (listDirty && left.scrollTop < 150) refreshDisplay()", js)
        self.assertIn("if (listDirty && left && left.scrollTop < 150) refreshDisplay()", js)
        self.assertIn('? "/threads" : "/ask"', js)  # 우측 전체 로드의 좌측 기본 = 분석 이력

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
        # 첫 메뉴 = 분석(href=/, 첫 화면) — 위치명(홈) 대신 기능명
        order = ["분석", "메일함", "스레드", "인물", "기억", "통계"]
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
        # 발신인 뒤에 관계 배지가 붙을 수 있다(2026-08-06) — 이름 자리는 그대로
        self.assertIn("<span class='msubj'>kim", out)
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
        self.assertNotIn("↩ 회신 필요", out)          # 신호 칩도 제거(2026-07-30)
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
        self.assertIn("판정 기준 저장", out)               # 저장 범위가 버튼에 명시됨
        self.assertIn("표시 설정 저장", out)

    def test_settings_weekly_backend_knob(self):
        # [ai] weekly 는 run_ai_layer 가 이미 읽는 키 — 설정 화면에도 노출돼야
        # opus 같은 무거운 백엔드를 주간에만 지정할 수 있다(미설정=요약 상속).
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn("주간 백엔드", page)
        self.assertIn("name='weekly_backend'", page)
        self.web._save_settings(self.cfg.home, {"weekly_backend": ["opus"]})
        import mailkb.config as cfgmod2
        self.assertEqual(
            cfgmod2.read_overrides(self.cfg.home)["ai"]["weekly"], "opus")

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

    def test_reading_font_injected_and_configurable(self):
        from mailkb import config as cfgmod
        # read_fs 지정 시 크기+배율(--read-zoom, 메일 HTML zoom 확대용) 주입,
        # 미지정 시 미주입(CSS 폴백 = 현행 크기)
        one = self.web._shell("t", "L", "R", read_fs=18)
        self.assertIn(":root{--read-fs:18px;--read-zoom:1.125}", one)
        self.assertNotIn(":root{--read-fs", self.web._shell("t", "L", "R"))
        both = self.web._shell("t", "L", "R", read_w=1500, read_fs=18)
        self.assertIn(":root{--read-w:1500px}", both)   # rw 문자열과 독립
        self.assertIn("--read-fs:18px", both)
        home = Path(self.tmp.name)
        (home / "config.toml").write_text(
            'my_addresses=["me@corp.example"]\n', encoding="utf-8")
        self.web._save_settings(home, {"reading_font": ["18"]})
        self.assertEqual(cfgmod.load(home).opt("web", "reading_font", default=0), 18)
        self.web._save_settings(home, {"reading_font": ["8"]})    # 최소 12 클램프
        self.assertEqual(cfgmod.load(home).opt("web", "reading_font", default=0), 12)

    def test_settings_reading_font_field(self):
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn("본문 글자 크기(px)", page)
        # 미설정이면 빈 값 + placeholder — _save_settings 가 빈 필드를 건너뛰므로
        # 다른 항목 저장 시 reading_font 오버라이드가 오기록되지 않는다
        self.assertIn("name='reading_font' value='' placeholder='16'", page)

    def test_css_strips_borders_from_layout_tables_only(self):
        # 알림 메일의 조판용 표가 빈 테두리 박스로 보이던 것 — 기본 표 규칙은
        # 그대로 두고 조판용 표의 **직속** 셀만 뺀다.
        css = web._CSS
        self.assertIn(".msg .mbody td, .msg .mbody th { border: 1px solid", css)
        for sel in ('table[role="presentation"] > * > tr > td',
                    'table[cellpadding="0"][cellspacing="0"]:not([border]) '
                    '> * > tr > td',
                    'table[cellpadding="0"][cellspacing="0"][border="0"] '
                    '> * > tr > th'):
            self.assertIn(sel, css, msg=sel)
        self.assertIn("border: 0; padding: 0;", css)
        # 다크 평탄화는 border-color 만 !important 로 덮으므로(폭·스타일은 우리가
        # 이긴다) 새 !important 를 도입하지 않는다 — 특이도로만 이긴다.
        rule = css.split('table[role="presentation"]', 1)[1].split("}", 1)[0]
        self.assertNotIn("!important", rule)

    def test_css_read_fs_scoped_to_mbody(self):
        css = self.web._CSS
        self.assertIn(".msg .mbody { padding: 12px 14px; "
                      "font-size: var(--read-fs, 16px); }", css)
        self.assertIn(".msg .mbody pre { font-size: var(--read-fs, 13px); }", css)
        self.assertEqual(css.count("var(--read-fs"), 2)  # 본문(mbody) 밖 누출 없음
        # 메일 원본 HTML 은 인라인 pt 가 상속을 이김 — zoom 비례 확대 + 이중 확대 방지
        self.assertIn(".mailhtml { font-size: 16px; zoom: var(--read-zoom, 1); }", css)

    def test_inline_assets_have_no_mangled_octal_escapes(self):
        # _CSS·report.CSS·REPORT_JS 는 raw 문자열이 아니다(_APP_JS 만 r""").
        # CSS 이스케이프를 `\201C` 로 쓰면 파이썬이 \201 을 8진수로 먹어 제어문자
        # U+0081 + 'C' 가 되고, 브라우저엔 인용 부호 대신 두부가 뜬다(2026-07-26
        # 실제 발생). 백슬래시를 두 번 써야 한다. _APP_JS 는 raw 라 이 함정이
        # 없지만, 나중에 r 접두어가 빠질 수 있으니 함께 지킨다.
        for name, blob in (("_CSS", self.web._CSS), ("_APP_JS", self.web._APP_JS),
                           ("report.CSS", report.CSS),
                           ("REPORT_JS", report.REPORT_JS)):
            bad = sorted({f"U+{ord(c):04X}" for c in blob if 0x80 <= ord(c) <= 0x9F})
            self.assertEqual(bad, [], f"{name} 에 C1 제어문자: {bad}")
        # 인용 부호가 의도한 코드포인트로 들어갔는지 — 렌더 결과가 아니라 CSS 원문
        self.assertIn('content: "\\201C"', self.web._CSS)
        self.assertIn('content: "\\201D"', self.web._CSS)

    def test_date_group_buckets(self):
        dg = self.web._date_group
        today = date(2026, 7, 22)                       # 수요일
        self.assertEqual(dg("2026-07-22T09:00:00", today), "t")
        self.assertEqual(dg("2026-07-21T23:00:00", today), "y")
        self.assertEqual(dg("2026-07-20T08:00:00", today), "w")   # 이번 주 월요일
        self.assertEqual(dg("2026-07-19T08:00:00", today), "lw")  # 지난주 일요일
        self.assertEqual(dg("2026-07-13T08:00:00", today), "lw")  # 지난주 월요일
        self.assertEqual(dg("2026-07-01T08:00:00", today), "m")
        self.assertEqual(dg("2026-06-30T08:00:00", today), "2026-06")
        self.assertEqual(dg("2025-12-01T08:00:00", today), "2025-12")
        # 월요일의 '어제'(지난주 일요일)는 주(週) 판정보다 앞 — 순서 역행 없음
        mon = date(2026, 7, 20)
        self.assertEqual(dg("2026-07-19T08:00:00", mon), "y")
        self.assertEqual(dg("2026-07-18T08:00:00", mon), "lw")
        self.assertEqual(self.web._group_label("t"), "오늘")
        self.assertEqual(self.web._group_label("lw"), "지난 주")
        self.assertEqual(self.web._group_label("2026-06"), "2026년 6월")

    def test_mail_date_group_headers(self):
        today = date.today()
        self.store.ingest([
            _rec("g1", "kim@corp.example", [ME], "오늘 메일",
                 today.isoformat() + "T09:00:00"),
            _rec("g2", "kim@corp.example", [ME], "옛날 메일",
                 "2026-03-05T09:00:00")])
        out = self.web.render_mail(self.store, self.cfg)
        self.assertIn("<div class='dghead'>오늘</div>", out)
        self.assertIn("<div class='dghead'>2026년 3월</div>", out)
        # 경계에서만 방출: 헤더 수 = 정렬(최신순)된 행 버킷의 런(run) 수
        dates = sorted([today.isoformat(), "2026-07-04", "2026-03-05"],  # g1·w1·g2
                       reverse=True)
        keys = [self.web._date_group(s, today) for s in dates]
        runs = sum(1 for i, k in enumerate(keys) if i == 0 or k != keys[i - 1])
        self.assertEqual(out.count("class='dghead'"), runs)
        self.assertLess(out.index("dghead'>오늘</div>"), out.index("오늘 메일"))

    def test_mail_more_carries_group_key(self):
        # 같은 옛 달 35통 → 첫 배치 센티널에 &g=, 다음 배치가 이어받아 헤더 억제
        self.store.ingest([
            _rec(f"gm{i}", "kim@corp.example", [ME], f"과거 {i:02d}",
                 f"2026-03-{(i % 27) + 1:02d}T09:00:00") for i in range(35)])
        first = self.web.render_mail(self.store, self.cfg)
        url = first.split("data-more='")[1].split("'")[0]
        self.assertIn("&g=2026-03", url)
        self.assertIn("?offset=", url)                  # g 는 offset 뒤(접두 보존)
        off = int(url.split("offset=")[1].split("&")[0])
        gkey = url.split("&g=")[1]
        frag = self.web.render_mail(self.store, self.cfg, offset=off, g=gkey)
        self.assertNotIn("dghead", frag)                # 같은 그룹 계속 — 헤더 없음
        frag2 = self.web.render_mail(self.store, self.cfg, offset=off, g="")
        self.assertIn("dghead'>2026년 3월</div>", frag2)  # 핸드오프 없으면 방출

    def test_threads_date_group_headers(self):
        today = date.today()
        self.store.ingest([
            _rec("tg1", "kim@corp.example", [ME], "오늘 스레드",
                 today.isoformat() + "T08:00:00"),
            _rec("tg2", "kim@corp.example", [ME], "옛 스레드",
                 "2026-03-05T09:00:00")])
        out = self.web.render_threads(self.store, self.cfg)
        self.assertIn("dghead'>오늘</div>", out)
        self.assertIn("dghead'>2026년 3월</div>", out)
        self.assertLess(out.index("dghead'>오늘</div>"),
                        out.index("dghead'>2026년 3월</div>"))

    def test_more_html_group_param(self):
        # 그룹 키는 offset 뒤 — 기존 접두 단정(unread=1&offset=)과 공존
        h = self.web._more_html("/mail?unread=1", 30, "2026-05")
        self.assertIn("data-more='/mail?unread=1&offset=30&g=2026-05'", h)
        self.assertIn("data-more='/mail?offset=30'",
                      self.web._more_html("/mail", 30))  # group 없으면 종전 그대로

    def test_thread_sticky_header_markup(self):
        tid = self.store.message("1")["thread_id"]
        out = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn("<div class='sticksentinel'></div>", out)
        self.assertIn("<div class='threadhead'><h1>", out)
        # 신호 칩은 sticky 래퍼 안 — 액션 바는 래퍼 밖(뒤)
        self.assertLess(out.index("threadhead"), out.index("class='actions'"))

    def test_css_threadhead_and_scroll_margin(self):
        css = self.web._CSS
        self.assertIn(".threadhead { position: sticky; top: 0;", css)
        self.assertIn(".threadhead.stuck h1", css)
        self.assertIn("scroll-margin-top: 72px", css)   # stuck 헤더에 안 가리게
        self.assertIn(".dghead", css)

    def test_appjs_escape_and_msgnav(self):
        js = self.web._APP_JS
        self.assertIn('e.key === "Escape"', js)         # 입력란 탈출('/' 의 짝)
        self.assertIn("t.blur()", js)
        self.assertIn("function msgNav", js)            # n/p 메시지 이동
        self.assertIn('k === "n"', js)
        self.assertIn('k === "p"', js)
        # n/p 는 커서 '상태'로 이동 — 스크롤 위치 추정은 짧은 스레드(스크롤 없음)
        # 에서 같은 메시지만 맴돌았다. 우측 교체 시 리셋, focusMsg 와 커서 공유.
        self.assertIn("var msgCurId = null", js)
        self.assertIn("msgs[i].id === msgCurId", js)
        self.assertIn("msgCurId = null;        /* 새 내용", js)
        self.assertIn('if (pane === "right") msgCurId = el.id', js)
        self.assertIn("function hookThreadHead", js)    # sticky 컴팩트 토글
        self.assertIn('classList.toggle("stuck"', js)
        # 본문 글자 크기 저장 즉시 반영 — CSS 변수 라이브 갱신
        self.assertIn('setProperty("--read-fs"', js)
        self.assertIn('setProperty("--read-zoom"', js)
        # Space: 읽기 창 페이지 스크롤(문서는 height:100% 라 안 움직임) + 바닥이면 다음 메일
        self.assertIn('k === " "', js)
        self.assertIn("function paneScroll", js)
        self.assertIn("function paneAtBottom", js)
        self.assertIn("right.scrollBy", js)
        # 스페이스로 활성화되는 것에만 양보 — <a>(목록 행) 포함 시 왼쪽 패널이 넘어감
        self.assertIn("""t.closest("button, summary, [role='button']")""", js)
        self.assertNotIn('t.closest("button, a, summary', js)             # 회귀 가드
        # n/p 가 더 갈 곳 없을 때 먹통 대신 창 끝까지 스크롤(한 통짜리 스레드)
        self.assertIn("right.scrollTo({ top: dir > 0 ? right.scrollHeight : 0", js)

    def test_settings_section_order(self):
        # 판정 기준 → 표시·동기화 → 차단된 발신인 (2026-07-22 사용자 지정)
        page = self.web.render_settings(self.store, self.cfg)
        self.assertLess(page.index("<h2>판정 기준</h2>"),
                        page.index("<h2>표시 · 동기화</h2>"))
        self.assertLess(page.index("<h2>표시 · 동기화</h2>"),
                        page.index("<h2>차단된 발신인</h2>"))

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
        self.assertIn("aria-label='플래그' aria-pressed='false'", out)
        self.web.perform_action(self.store, self.cfg, f"/thread/{tid}/flag", {})
        self.assertEqual(self.store.thread(tid)["flagged"], 1)
        out2 = self.web.render_thread(self.store, self.cfg, tid)
        self.assertIn(f"action='/thread/{tid}/unflag'", out2)     # 이제 해제 버튼
        self.assertIn("flag on", out2)                            # 색 있는 flag(표시)
        self.assertIn("⚑", out2)
        self.assertIn("aria-label='플래그 해제' aria-pressed='true'", out2)
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
        # 메일함·스레드가 같은 필터 바(전체·미개봉·플래그·숨김) —
        # ↩/⏰ 탭은 2026-07-30 제거(판정 정밀도가 낮아 신뢰를 깎았다)
        for out in (self.web.render_mail(self.store, self.cfg),
                    self.web.render_threads(self.store, self.cfg)):
            self.assertIn("class='listtabs'", out)
            for lbl in ("전체", "미개봉", "🚩 플래그", "🙈 숨김"):
                self.assertIn(lbl, out)
            for gone in ("↩ 회신 필요", "⏰ 기한", "추적제외"):
                self.assertNotIn(gone, out)
            # 오른쪽 끝 (i) 키보드 도움말 — 기본은 CSS 로 숨김(호버/포커스 시 노출)
            self.assertIn("class='kbdhelp'", out)
            self.assertNotIn("x 신호", out)          # x 키 도움말도 제거
            for key in ("j / k 목록 이동", "Space 본문 넘기기(끝이면 다음 메일)",
                        "n / p 스레드 안 메일 이동",
                        "f 플래그", "h 숨김", "/ 검색", "Esc 검색창 빠져나오기"):
                self.assertIn(key, out)
        self.assertIn(".kbdpop", self.web._CSS)          # 도움말 팝오버 스타일
        self.assertIn("display: none", self.web._CSS)
        # 메일함 설명문 삭제 · 스레드 미답변 제거
        self.assertNotIn("노이즈 제외 수신 메일", self.web.render_mail(self.store, self.cfg))
        self.assertNotIn("미답변", self.web.render_threads(self.store, self.cfg))

    def test_signal_filters_removed_old_bookmarks_degrade(self):
        # ↩/⏰ 탭은 2026-07-30 제거. 구 북마크(?awaiting=1 등)는 크래시 없이
        # '전체' 로 강등된다 — 목록 자체는 항상 산다.
        self.store.ingest([
            MailRecord(message_id="<dl@t>", subject="기한 있는 요청",
                       sender_name="lee", sender_addr="lee@corp.example",
                       to=[ME], sent_on="2026-07-04T10:00:00",
                       body_text="내일까지 회신 부탁드립니다."),
        ])
        self.assertEqual(self.web._list_flt({"awaiting": ["1"]}), "")
        self.assertEqual(self.web._list_flt({"deadline": ["1"]}), "")
        self.assertEqual(self.web._list_flt({"unread": ["1"]}), "unread")
        out = self.web.render_threads(self.store, self.cfg)
        self.assertIn("기한 있는 요청", out)
        for gone in ("↩ 회신 필요", "⏰ 기한", "awaiting=1", "deadline=1"):
            self.assertNotIn(gone, out)


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

    def test_thread_sender_name_links_to_dossier(self):
        # 참여자(수신 발신자) 이름 클릭 → 그 사람 도시에(/people, 2026-07-18 승격)
        out = self.web.render_thread(self.store, self.cfg, self.store.message("1")["thread_id"])
        self.assertIn("<a href='/people?addr=kim%40corp.example'", out)

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
        # 스레드 상태 변경(플래그·숨김) 시 왼쪽 목록 즉시 갱신
        self.assertIn("flag|unflag|hide|unhide", js)
        self.assertNotIn("signal-off", js)   # 신호 UI 제거(2026-07-30)

    def test_appjs_fh_toggle_keys(self):
        # f 플래그 · h 숨김 — 대상은 우측에 열린 스레드 1순위(주소 →
        # 우측 폼 action 순), 없으면 j/k 커서 행. 서버 -toggle 호출 + 커서 복원.
        # (x 신호 토글은 2026-07-30 제거 — 판정 노출 폐지와 함께)
        js = self.web._APP_JS
        self.assertIn("function openTid", js)          # 우측 열린 스레드 판별
        self.assertIn('load("/thread/" + tid, "right", false)', js)  # 우측 상세 동기화
        self.assertIn("right.scrollTop = rsc", js)     # 갱신 후 읽던 위치 유지
        self.assertNotIn('k === "s"', js)              # 구 's' 분기 제거
        self.assertNotIn('k === "x"', js)              # 신호 토글 소멸
        self.assertIn('k === "f"', js)
        self.assertIn('k === "h"', js)
        self.assertIn('"-toggle"', js)                 # /thread/N/<kind>-toggle 호출
        self.assertIn("restoreKbd", js)                # 커서 복원(머무름/다음 행)
        self.assertIn("tokenToast", js)                # 상태명 토스트 매핑
        # .mrow 는 행 자체가 <a> — 자식만 찾으면 목록에서 무동작(회귀 가드)
        self.assertIn("row.matches", js)
        # 접힌 확인 후보 폴드 안 행 제외 — 안 보이는 행에 커서·토글 금지(회귀 가드)
        self.assertIn("offsetParent", js)

    def test_nav_active_underline(self):
        # 현재 위치한 최상위 메뉴에 밑줄(active) 표시
        js = self.web._APP_JS
        self.assertIn("markNav", js)                    # nav 활성화 갱신 함수
        self.assertIn("navTarget", js)
        self.assertIn("header.top nav", js)             # 셸 헤더의 nav 대상
        self.assertIn('classList.add("active")', js)
        # 이동(inject) + 초기 로드 + 좌우 이력 동시 복원 뒤에 갱신
        self.assertEqual(js.count("markNav();"), 3)
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
        for tok in ("--surface:", "--ink:", "--border:", "--accent:", "--accent-fg:"):
            self.assertIn(tok, w._CSS, msg=tok)
        # 셸이 cfg 테마를 <html> 에 반영
        self.assertIn("<html lang='ko' data-theme='dark' data-skin='classic'>",
                      w._shell("홈", "L", "R", theme="dark"))
        # 특수 응답(403 차단 등)도 테마를 따름
        self.assertIn("data-theme='dark'", w._page("차단", "x", theme="dark"))

    # ── 화면 스킨 (모양·밀도 축, 2026-08-01) ──────────────────────────
    @staticmethod
    def _resolve_tokens(state):
        """(테마, 스킨) 상태에서 :root 블록들의 캐스케이드를 실제로 풀어 본다.

        스킨의 라이트 블록과 다크 토큰 블록은 특이도가 같아서, 순서만 믿으면
        '다크+벤토'에서 라이트 색이 다크를 덮어쓴다. 그 버그를 잡는 검사다."""
        from mailkb import web as w
        import re as _re
        blocks = _re.findall(r"(:root[^{\n]*)\{([^}]*)\}", w._CSS + w._SKIN_CSS)
        out = {}
        for order, (sel, body) in enumerate(blocks):
            sel = sel.strip()
            ok = True
            for attr, val in _re.findall(r"(?<!:not\()\[data-(\w+)='(\w+)'\]", sel):
                if state.get(attr) != val:
                    ok = False
            for attr, val in _re.findall(r":not\(\[data-(\w+)='(\w+)'\]\)", sel):
                if state.get(attr) == val:
                    ok = False
            if not ok:
                continue
            spec = sel.count("[")                      # 속성 선택자 수 = 특이도 근사
            for m in _re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body):
                k, v = m.group(1), m.group(2).strip()
                prev = out.get(k)
                if prev is None or spec > prev[0] or (spec == prev[0] and order >= prev[1]):
                    out[k] = (spec, order, v)
        return {k: v[2] for k, v in out.items()}

    def test_bento_dark_is_not_overwritten_by_bento_light(self):
        # :not([data-theme='dark']) 가드가 빠지면 다크+벤토에서 라이트 색이 이긴다
        dark_bento = self._resolve_tokens({"theme": "dark", "skin": "bento"})
        light_bento = self._resolve_tokens({"theme": "light", "skin": "bento"})
        dark_classic = self._resolve_tokens({"theme": "dark", "skin": "classic"})
        self.assertEqual(dark_bento["--bg"], "#15181c")
        self.assertEqual(light_bento["--bg"], "#eef2f7")
        self.assertNotEqual(dark_bento["--bg"], light_bento["--bg"])
        # 다크의 코랄 강조는 스킨이 건드리지 않는다(앱의 기존 판단을 존중)
        self.assertEqual(dark_bento["--accent"], dark_classic["--accent"])
        # 모양은 밝기와 무관하게 두 테마가 공유한다
        self.assertEqual(dark_bento["--r-md"], light_bento["--r-md"])

    def test_classic_is_untouched_by_the_skin_block(self):
        w = self.web
        # 스킨 CSS 의 모든 규칙은 [data-skin='bento'] 를 요구한다 —
        # 고르지 않은 사용자에게는 존재하지 않는 것과 같아야 한다.
        import re as _re
        body = _re.sub(r"/\*.*?\*/", "", w._SKIN_CSS, flags=_re.S)
        sels = [s.strip() for chunk in body.split("}") if "{" in chunk
                for s in chunk.split("{")[0].split(",") if s.strip()]
        self.assertTrue(sels)
        for s in sels:
            if s.startswith("@"):
                continue
            self.assertIn("[data-skin='bento']", s, msg=s)
        # classic 토큰은 지금 값 그대로
        cl = self._resolve_tokens({"theme": "light", "skin": "classic"})
        self.assertEqual(cl["--bg"], "#fafafa")
        self.assertEqual((cl["--r-sm"], cl["--r-md"], cl["--r-lg"]), ("6px", "8px", "10px"))
        self.assertEqual(cl["--shadow-card"], "none")   # 카드 그림자 없음 = 지금과 같다

    def test_skin_attribute_and_picker(self):
        w = self.web
        self.assertIn("data-skin='classic'", w._head("t"))            # 기본
        self.assertIn("data-skin='bento'", w._head("t", skin="bento"))
        self.assertIn("data-skin='classic'", w._head("t", skin="../evil"))
        self.assertEqual(w._skin_ok("bento"), "bento")
        self.assertEqual(w._skin_ok("nope"), "classic")
        page = w.render_settings(self.store, self.cfg)
        self.assertIn("화면 스킨", page)
        # 설명이 실제 동작과 맞아야 한다 — 벤토는 배치도 바꾼다(2026-08-01)
        self.assertIn("배치", page)
        self.assertNotIn("레이아웃은 그대로", page)
        self.assertIn("data-set-skin='classic'", page)
        self.assertIn("data-set-skin='bento'", page)
        self.assertIn("class='themebtn active' aria-pressed='true' "
                      "data-set-skin='classic'", page)
        self.cfg.raw = {"web": {"skin": "bento"}}
        self.assertIn("class='themebtn active' aria-pressed='true' "
                      "data-set-skin='bento'", w.render_settings(self.store, self.cfg))

    def _landing(self, skin, today="2026-07-04"):
        self.cfg.raw = {"web": {"skin": skin}}
        return self.web._ask_landing(self.store, self.cfg, today)

    def test_bento_home_is_a_grid_and_classic_is_not(self):
        # 레이아웃 변경은 '이미 있는 것을 다시 배치'까지만 — 홈은 저장된 회고
        # 파일과 지금 랜딩이 이미 읽는 값만 쓴다(2026-08-01 사용자 확정).
        d = self.cfg.vault / "daily"; d.mkdir(parents=True, exist_ok=True)
        (d / "2026-07-04.md").write_text(
            "# 2026-07-04 일간 회고\n\n수신 3\n\n"
            "## Executive Summary\n- 오늘은 조용했습니다.\n\n"
            "## 내 약속 — 후속이 없는 것 (2건)\n"
            "- [#1] 가<!--done:promise:aaaaaaaaaaaa-->\n  「하겠습니다.」\n"
            "- [#2] 나<!--done:promise:bbbbbbbbbbbb-->\n", encoding="utf-8")
        classic, bento = self._landing("classic"), self._landing("bento")
        self.assertNotIn("bhome", classic)
        self.assertIn("이어서 볼 것", classic)          # 클래식은 지금 그대로
        self.assertIn("bhome", bento)
        self.assertIn("오늘의 요약", bento)
        self.assertIn("내 약속", bento)
        self.assertIn("(2건)", bento)
        self.assertEqual(bento.count("<div"), bento.count("</div>"))

    def test_bento_tiles_have_size_contrast_and_fill_rows(self):
        # 전부 같은 크기면 격자가 아니라 표다(2026-08-01 실기기 소감).
        # 그리고 줄이 12열 배수로 안 떨어지면 깨진 것처럼 보인다.
        import re as _re
        d = self.cfg.vault / "daily"; d.mkdir(parents=True, exist_ok=True)
        def spans():
            h = self._landing("bento")
            return [int(_re.search(r"s(\d+)", m).group(1))
                    for m in _re.findall(r"class='btile ([^']*)'", h)]
        base = ("# 2026-07-04 일간 회고\n\n수신 3\n\n"
                "## 내 약속 — 후속이 없는 것 (1건)\n- [#1] 가\n\n"
                "## 변화 — 어제 이후\n- 새로 내 차례 (0건) — 없음\n")
        # 회고 없음 → 작은 칸만
        self.assertEqual(spans(), [4, 4, 4])
        # AI 요약 없음(기본) → 8 + 4 로 대비가 난다
        (d / "2026-07-04.md").write_text(base, encoding="utf-8")
        self.assertEqual(spans()[:2], [8, 4])
        # AI 요약 있음 → 전폭 하나 + 반씩 둘
        (d / "2026-07-04.md").write_text(
            "# 2026-07-04 일간 회고\n\n수신 3\n\n"
            "## Executive Summary\n- 한 줄\n\n" + base.split("\n\n", 2)[2],
            encoding="utf-8")
        self.assertEqual(spans()[:3], [12, 6, 6])
        # 어느 경우든 줄이 딱 떨어진다
        for _ in range(1):
            self.assertEqual(sum(spans()) % 12, 0)

    def test_bento_home_omits_what_it_has_no_data_for(self):
        # 회고가 없으면 그 타일을 그리지 않는다 — **새로 만들지 않는다**
        bento = self._landing("bento", "2026-07-04")
        self.assertIn("bhome", bento)
        self.assertNotIn("오늘의 요약", bento)
        self.assertNotIn("내 약속", bento)
        self.assertIn("장기기억", bento)                # 값싼 타일은 남는다

    def test_bento_home_shows_no_done_buttons(self):
        # 홈에서 접으면 화면이 튄다 — 접는 자리는 리포트 화면이다.
        # 다만 이미 접힌 항목은 홈에서도 안 보여야 한다(같은 집합을 쓴다).
        d = self.cfg.vault / "daily"; d.mkdir(parents=True, exist_ok=True)
        (d / "2026-07-04.md").write_text(
            "# 2026-07-04 일간 회고\n\n수신 3\n\n"
            "## 내 약속 — 후속이 없는 것 (2건)\n"
            "- [#1] 가<!--done:promise:aaaaaaaaaaaa-->\n"
            "- [#2] 나<!--done:promise:bbbbbbbbbbbb-->\n", encoding="utf-8")
        bento = self._landing("bento")
        self.assertIn(">#1</a>] 가", bento)              # #1 은 스레드 링크가 된다
        self.assertNotIn("report/done", bento)          # 버튼 없음
        self.assertNotIn("done:promise", bento)         # 표식도 안 샌다
        self.store.mark_report_done("promise", "aaaaaaaaaaaa")
        after = self._landing("bento")
        self.assertNotIn(">#1</a>] 가", after)           # 접은 것은 홈에서도 빠진다
        self.assertIn(">#2</a>] 나", after)

    def test_settings_theme_picker(self):
        page = self.web.render_settings(self.store, self.cfg)
        self.assertIn("화면 테마", page)
        self.assertIn("data-set-theme='light'", page)
        self.assertIn("data-set-theme='dark'", page)
        # 세그먼트 토글 — 해/달 SVG 아이콘 + 눌림 상태
        self.assertIn("role='group'", page)
        self.assertEqual(page.count("data-set-theme"), 2)   # 라이트·다크 둘
        # 기본은 라이트가 active (aria-pressed 동기화)
        self.assertIn("class='themebtn active' aria-pressed='true' "
                      "data-set-theme='light'", page)
        # 다크 저장 시 다크가 active
        self.cfg.raw = {"web": {"theme": "dark"}}
        page2 = self.web.render_settings(self.store, self.cfg)
        self.assertIn("class='themebtn active' aria-pressed='true' "
                      "data-set-theme='dark'", page2)

    def test_appjs_theme_toggle_markers(self):
        js = self.web._APP_JS
        self.assertIn("data-set-theme", js)              # 버튼 위임 처리
        self.assertIn("/settings/theme", js)             # 서버 영구화 POST
        self.assertIn('setAttribute("data-theme"', js)   # 즉시 <html> 적용
        self.assertIn('setAttribute("aria-pressed"', js)

    def test_button_roles_keep_theme_contrast_and_component_hover(self):
        css = self.web._CSS
        self.assertIn("--accent-fg:#ffffff", css)         # 라이트: 파랑 위 흰 글자
        self.assertIn("--accent-fg:#16181b", css)         # 다크: 코랄 위 어두운 글자
        self.assertIn(".btn-primary", css)
        self.assertIn(".btn-caution", css)
        self.assertIn("button.danger", css)
        self.assertIn(".chatbar button:hover", css)
        # 공통 hover가 전용 채팅/AI hover 뒤에서 덮어쓰면 안 된다.
        self.assertLess(css.index("button, .btn"), css.index(".chatbar button"))
        self.assertLess(css.index("button, .btn"), css.index(".aibtn {"))
        self.assertIn("button:disabled", css)
        self.assertIn('setAttribute("aria-busy"', self.web._APP_JS)

    def test_stats_page_follows_theme(self):
        self.cfg.raw = {"web": {"theme": "dark"}}
        page = self.web.render_stats_page(self.store, self.cfg)
        self.assertIn("data-theme='dark'", page)         # 통계도 다크
        # report CSS 의 다크 대응은 **토큰 오버라이드**가 본체다. 예전엔
        # html[data-theme='dark'] .melabel 한 줄로 대신 검사했는데, 그 규칙은
        # 관계 그래프 전용이라 절이 없어지며 함께 지워졌다(2026-08-02).
        self.assertIn(":root[data-theme='dark']", page)
        # 차트 전용 색(격자·축·노드)도 토큰 — 다크 오버라이드가 존재해야.
        # 칩 토큰으로 검사하던 것을 살아 있는 것으로 옮겼다(2026-08-02 —
        # 심각도 칩은 쓰는 마크업이 없어 규칙·토큰 모두 삭제).
        for tok in ("--node:", "--grid:", "--base:"):
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
        body = p.read_text(encoding="utf-8")
        self.assertNotIn("## 지금 할 일", body)       # 2026-07-30 제거(신호 불신)
        self.assertIn("## 참고", body)

    def test_start_review_guard_when_running(self):
        self.web._review_job.update(running=True, msg="")
        self.assertFalse(self.web._start_review(self.cfg, False, "2026-07-04"))
        self.web._review_job.update(running=False, msg="")

    # ─────────────────── 기억 메뉴(구 기록) · 장기기억 (Phase 1)

    def test_records_page_tabs_default_daily(self):
        out = self.web.render_records(self.store, self.cfg, {}, "2026-07-04")
        self.assertIn("<b>일간 회고</b>", out)          # 기본 탭 활성(한글 대칭 명칭)
        self.assertIn("tab=decisions", out)             # 장기기억 탭 링크
        self.assertIn("일간 회고 · 2026-07-04", out)   # 기존 일간 회고 콘텐츠
        # 날짜 이동 ◀ ▶ — 오늘이 끝이면 다음날 링크 없음
        self.assertIn("tab=daily&date=2026-07-03'>◀", out)
        self.assertNotIn("2026-07-05 ▶", out)
        past = self.web.render_records(self.store, self.cfg,
                                       {"date": ["2026-07-02"]}, "2026-07-04")
        self.assertIn("2026-07-03 ▶", past)             # 과거 날짜에선 다음날 표시

    def test_review_status_running_scene_and_progress(self):
        w = self.web
        # 시작 직후(step=0): 씬 + 흐르는 바(indet), 단계 라벨 없음
        w._review_job.update(running=True, msg="준비 중…", step=0, total=6,
                             ai=True)
        inner, running = w.render_review_status(self.store)
        self.assertTrue(running)
        self.assertIn("data-review-running", inner)      # 폴링 마커 유지
        self.assertIn("class='waitcard'", inner)         # 다른 잡과 같은 대기 카드
        self.assertIn("rvfill indet' id='rv-fill'", inner)
        self.assertNotIn("단계", inner)
        self.assertIn("AI 회고 작성 중", inner)
        self.assertIn("action='/review/cancel'", inner)
        # 단계 진행(_job_progress): step 증가 → 채워지는 바 + '단계 2/6'
        w._job_progress("누적 요약 갱신 중…")
        w._job_progress("결정·신호 수확 중…")
        inner2, _ = w.render_review_status(self.store)
        self.assertIn("단계 2/6", inner2)
        self.assertIn("id='rv-fill' style='width:33%'", inner2)
        self.assertIn("결정·신호 수확 중…", inner2)
        self.assertIn("id='rv-stage'", inner2)            # app.js 패치 타깃
        w._job_progress("완료")
        self.assertEqual(w._review_job["step"], 6)
        w._review_job.update(running=False, msg="", step=0, ai=False)

    def test_review_non_ai_job_says_so_and_offers_no_cancel(self):
        # 홈 진입마다 도는 결정론 자동 갱신(_maybe_auto_review)이 같은 슬롯을
        # 쓴다 — 제목이 같으면 AI 회고가 도는 줄 착각하고, 끊을 대상도 없다.
        w = self.web
        try:
            w._review_job.update(running=True, msg="준비 중…", step=0, total=6,
                                 ai=False)
            inner, running = w.render_review_status(self.store)
            self.assertTrue(running)
            self.assertIn("일간 회고 정리 중", inner)
            self.assertNotIn("AI 회고 작성 중", inner)
            self.assertNotIn("/review/cancel", inner)
            self.assertNotIn("중지", inner)
        finally:
            w._review_job.update(running=False, msg="", step=0)

    def test_appjs_polls_patch_not_replace(self):
        js = self.web._APP_JS
        self.assertIn('patchJob(tmp, right, "rv")', js)
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

    def test_unique_filename_dedup(self):
        from mailkb.sources.outlook_com import _unique_filename
        used = set()
        self.assertEqual(_unique_filename("a.pdf", used), "a.pdf")
        self.assertEqual(_unique_filename("a.pdf", used), "a-1.pdf")
        self.assertEqual(_unique_filename("a.pdf", used), "a-2.pdf")
        self.assertEqual(_unique_filename("noext", used), "noext")
        self.assertEqual(_unique_filename("noext", used), "noext-1")


class TestStatsWindows(unittest.TestCase):
    """창은 절마다 고정이다 — 전역 기간 선택기(2/4/8/16W) 폐지 사양(2026-08-02).

    선택기는 ① 절마다 필요한 창이 달라 하나로 강제하면 절반이 틀린 창을 보고
    ② 한동안 고장난 채였는데 코드 검토로야 발견됐으며(= 안 쓰였다)
    ③ 응답 지표만 늘 '최근 2주' 앵커라 같은 이름이 다른 값을 냈다."""

    def setUp(self):
        from mailkb import report
        self.report = report
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "t.sqlite", [ME])
        self.addCleanup(self.store.close)
        self.cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                          internal_domains=["corp.example"])
        # 옛 왕복(9주 전, 4왕복) vs 최근 왕복(1주 전, 2왕복) — 창이 갈라야 한다
        old = [_rec(f"o{i}", "kim@corp.example" if i % 2 == 0 else ME,
                    [ME] if i % 2 == 0 else ["kim@corp.example"],
                    "옛 논의", f"2026-05-0{i + 1}T09:00:00",
                    "본문입니다.", reply_to="o0" if i else "")
               for i in range(5)]
        new = [_rec(f"n{i}", "lee@corp.example" if i % 2 == 0 else ME,
                    [ME] if i % 2 == 0 else ["lee@corp.example"],
                    "최근 논의", f"2026-06-2{i + 4}T09:00:00",
                    "본문입니다.", reply_to="n0" if i else "")
               for i in range(3)]
        hid = [_rec(f"h{i}", "oh@corp.example" if i % 2 == 0 else ME,
                    [ME] if i % 2 == 0 else ["oh@corp.example"],
                    "숨긴 논의", f"2026-06-2{i + 4}T10:00:00",
                    "본문입니다.", reply_to="h0" if i else "")
               for i in range(3)]
        self.store.ingest(old + new + hid)
        self.hid_tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE subject='숨긴 논의' LIMIT 1"
        ).fetchone()["thread_id"]
        self.store.hide_thread(self.hid_tid, True)

    def _out(self):
        return self.report.render_stats(self.store, self.cfg)

    def test_no_period_selector_and_weeks_param_is_gone(self):
        out = self._out()
        for gone in ("검토 기간", 'class="periods"', "/stats?weeks=", "popt"):
            self.assertNotIn(gone, out, msg=gone)

    def test_every_section_prints_its_own_window(self):
        # 전역 선택기를 없앤 대신 각 절이 자기 창을 적는다 — 이게 그 계약이다.
        out = self._out()
        tw, rw = self.report.TREND_WEEKS, self.report.RECENT_WEEKS
        self.assertNotEqual(tw, rw)
        # 긴 창: 응답 타일 2 + 볼륨 + 히트맵 + 기억 / 짧은 창: 구성 + 왕복
        self.assertEqual(out.count(f'<span class="win">최근 {tw}주</span>'), 5)
        self.assertEqual(out.count(f'<span class="win">최근 {rw}주</span>'), 2)

    def test_pingpong_window_drops_dead_discussions(self):
        # '회의 전환 후보'라는 조언은 살아 있는 논의에만 성립한다 —
        # 9주 전 4왕복은 이미 끝난 것이라 짧은 창에서 빠진다.
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        subs = lambda rows: {p["subject"] for p in rows}
        full = subs(self.report.sig_pingpong(d, self.cfg))
        recent = subs(self.report.sig_pingpong(
            d, self.cfg, since=self.report._recent_since(d)))
        self.assertIn("옛 논의", full)
        self.assertNotIn("옛 논의", recent)
        self.assertIn("최근 논의", recent)

    def test_pingpong_skips_hidden_but_aggregates_count_it(self):
        # 목록 절은 숨김을 존중하고, 집계(볼륨·히트맵)는 전량 센다 —
        # 숨김은 "조용히 하라"이지 "없던 일로 하라"가 아니다.
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        tids = {p["thread_id"] for p in self.report.sig_pingpong(d, self.cfg)}
        self.assertNotIn(self.hid_tid, tids)
        trend = self.report.sig_volume_trend(d)
        self.assertEqual(sum(trend["sent"]) + sum(trend["recv"]), len(d["msgs"]))

    def test_person_card_progress_also_respects_hidden(self):
        # sig_pingpong 은 인물 카드의 '진행 중'도 만든다 — 숨김 제외가 거기까지
        # 전파된다. 같은 규칙(목록은 숨김 존중)이라 의도된 것이고, 모르고
        # '되돌리는' 일이 없도록 사양으로 박아 둔다(2026-08-02 자체 점검).
        subs = lambda: {p["subject"] for p in
                        self.report.person_metrics(self.store, self.cfg,
                                                   "oh@corp.example")["pingpong"]}
        self.store.hide_thread(self.hid_tid, False)
        self.assertIn("숨긴 논의", subs())
        self.store.hide_thread(self.hid_tid, True)
        self.assertNotIn("숨긴 논의", subs())

    def test_pingpong_keeps_the_thread_subject_not_the_windowed_first(self):
        # 창으로 자르면 첫 메일이 'RE: …' 라 원 제목이 아니다 — 제목·노이즈
        # 판정은 스레드 전체의 첫 메일로 한다.
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        rows = self.report.sig_pingpong(d, self.cfg,
                                        since=self.report._recent_since(d))
        self.assertTrue(rows)
        self.assertTrue(all(not p["subject"].startswith("RE:") for p in rows))

    def test_inbox_mix_window_shrinks_the_sample(self):
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        full = self.report.sig_inbox_mix(d, self.cfg)
        recent = self.report.sig_inbox_mix(
            d, self.cfg, since=self.report._recent_since(d))
        self.assertLess(recent["total"], full["total"])

    def test_response_pairs_have_matching_shape(self):
        # 두 지표는 나란히 놓고 대비하는 것이 유일한 쓸모라 형태가 같아야 한다
        # (둘 다 주 idx 를 담아 같은 스파크라인을 그린다).
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        mine = self.report._reply_pairs(d)
        theirs = self.report._their_pairs(d)
        self.assertTrue(mine and theirs)
        self.assertEqual(len(mine[0]), len(theirs[0]))
        for p in theirs:
            self.assertIsInstance(p[3], int)


class TestStatsMemoryCoverage(unittest.TestCase):
    """§5 기억 커버리지 — 이미 있는 트레이드오프의 계량기(2026-08-02 신설).

    `review._summary_window` 는 소급 상한이 ai.summary_max_days(기본 1일)라
    **앱을 안 연 날의 메일은 요약·수확에서 영구히 빠진다.** 의도된 설계인데
    그 대가가 어디에도 보이지 않아, 사용자가 상한을 올릴지 판단할 근거가
    없었다. 이 절이 그 구멍을 그린다."""

    def setUp(self):
        from mailkb import report
        self.report = report
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME],
                          internal_domains=["corp.example"])
        self.store = Store(self.home / "t.sqlite", [ME])
        self.addCleanup(self.store.close)
        self.store.ingest([
            _rec("m1", "kim@corp.example", [ME], "설계 검토",
                 "2026-07-06T09:00:00", "검토 부탁드립니다."),
            _rec("m2", ME, ["kim@corp.example"], "RE: 설계 검토",
                 "2026-07-08T09:00:00", "확인했습니다.", reply_to="m1"),
        ])
        self.daily = self.cfg.vault / "daily"
        self.daily.mkdir(parents=True, exist_ok=True)

    def _write_daily(self, day: str, body: str):
        (self.daily / f"{day}.md").write_text(body, encoding="utf-8")

    def _mem(self):
        d = self.report.load(self.store.db, self.report.TREND_WEEKS, {ME})
        return self.report.sig_memory(self.store, self.cfg, d)

    def test_file_without_ai_sections_does_not_count(self):
        # 웹의 자동 생성 회고는 ai=False 라 파일은 매일 생기지만 요약·수확은
        # 안 돈다. 파일 존재만 세면 지표가 거짓말을 한다.
        self._write_daily("2026-07-08", "# 2026-07-08 일간 회고\n\n## 오늘 흐름\n- x\n")
        mem = self._mem()
        self.assertEqual(mem["days_on"], 0)
        self.assertFalse(mem["any"])

    def test_ai_sections_mark_the_day_as_covered(self):
        for mark in ("## Executive Summary", "## AI 회고 분석"):
            with self.subTest(mark=mark):
                self._write_daily("2026-07-08", f"# 회고\n\n{mark}\n- 한 줄\n")
                self.assertEqual(self._mem()["days_on"], 1)

    def test_grid_covers_every_day_up_to_asof_and_marks_weekends(self):
        mem = self._mem()
        days = mem["days"]
        self.assertEqual(len(days), mem["days_total"])
        asof = self.store.db.execute(
            "SELECT MAX(sent_on) FROM messages").fetchone()[0][:10]
        self.assertEqual(max(x["date"].isoformat() for x in days), asof)
        self.assertTrue(all(x["weekend"] == (x["date"].weekday() >= 5)
                            for x in days))
        # 날짜가 빠짐없이 하루씩 이어져야 구멍이 구멍으로 보인다
        seq = sorted(x["date"] for x in days)
        self.assertEqual((seq[-1] - seq[0]).days + 1, len(seq))

    def test_counts_summarized_threads_and_confirmed_decisions(self):
        tid = self.store.db.execute(
            "SELECT id FROM threads LIMIT 1").fetchone()["id"]
        self.assertEqual(self._mem()["threads_sum"], 0)
        self.store.save_summary(tid, "요약", 2)
        did = self.store.add_decision(tid, "2026-07-08", "A안 확정")
        self.store.set_decision_status(did, "confirmed")
        mem = self._mem()
        self.assertEqual(mem["threads_sum"], 1)
        self.assertEqual(mem["threads_total"], 1)
        self.assertEqual(mem["decisions"], 1)
        self.assertTrue(mem["any"])

    def test_untouched_install_gets_a_sentence_not_an_empty_grid(self):
        # 실패/미실행/없음을 가르는 관례(review.EXEC_EMPTY)와 같은 태도 —
        # AI 를 한 번도 안 돌린 사람에게 0/N 격자를 들이대지 않는다.
        out = self.report.render_stats(self.store, self.cfg)
        self.assertIn("기억 커버리지", out)
        self.assertIn("AI 회고를 아직 돌리지 않아", out)
        self.assertNotIn('class="covcell', out)

    def test_grid_renders_one_to_one_not_stretched(self):
        # svg.heatmap 의 max-width:600px 는 24열(538px) 히트맵에 맞춘 값이다.
        # 12열(286px)인 이 격자에 물려 쓰면 화면에서 2.1배로 늘어나 같은 뷰박스
        # 셀 치수인데도 칸이 두 배로 보인다(2026-08-02 사용자 지적).
        self._write_daily("2026-07-08", "# 회고\n\n## Executive Summary\n- 한 줄\n")
        out = self.report.render_stats(self.store, self.cfg)
        m = re.search(r'<svg class="covgrid" width="(\d+)" height="(\d+)" '
                      r'viewBox="0 0 (\d+) (\d+)"', out)
        self.assertIsNotNone(m, "커버리지 격자가 전용 클래스·내재 크기를 안 실었다")
        self.assertEqual((m.group(1), m.group(2)), (m.group(3), m.group(4)))
        # 히트맵 클래스를 쓰면 CSS 가 다시 늘린다 — 두 격자가 섞이면 안 된다
        grid = out[out.index('<svg class="covgrid"'):]
        self.assertNotIn("heatmap", grid[:grid.index("</svg>")])

    def test_grid_is_not_a_keyboard_trap(self):
        # 빈 칸도 정보라 전부 탭 정지로 주면 최대 84번을 눌러야 빠져나간다.
        # 격자는 보조물이고 같은 사실을 요약 줄이 글자로 말하므로, 셀은
        # tabindex 를 안 주고 svg 에 aria-label 로 요지를 싣는다.
        self._write_daily("2026-07-08", "# 회고\n\n## Executive Summary\n- 한 줄\n")
        out = self.report.render_stats(self.store, self.cfg)
        grid = out[out.index('class="covcell'):]
        self.assertNotIn("tabindex", grid[:grid.index("</svg>")])
        self.assertIn('aria-label="날짜별 기억 커버리지', out)

    def test_covered_install_renders_the_grid(self):
        self._write_daily("2026-07-08", "# 회고\n\n## Executive Summary\n- 한 줄\n")
        out = self.report.render_stats(self.store, self.cfg)
        self.assertIn('class="covcell on"', out)
        self.assertIn("지식이 쌓인 날", out)
        self.assertIn("summary_max_days", out)      # 구멍을 메울 방법을 함께
        self.assertNotIn("AI 회고를 아직", out)


class TestReport(unittest.TestCase):
    """통계 분석(/stats) — 신호·자기 자신 제외."""

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
        out = self.report.render_stats(self.store, self.cfg)
        for marker in ("통계 분석",
                       # 남긴 4절 + 응답 타일 (2026-08-02 정리)
                       "볼륨 추세", "활동 히트맵",
                       "받은 메일 구성", "왕복 많은 논의",
                       "내 응답 중앙값", "상대 응답 중앙값",
                       # 절마다 창이 다르므로 화면에 적는다 — 전역 선택기의 대체물
                       '<span class="win">최근 12주</span>',
                       '<span class="win">최근 4주</span>'):
            self.assertIn(marker, out, msg=marker)
        # 제거된 옛 섹션·컨트롤은 없어야
        for gone in ("조용해진 사람", "증발한 내 요청", "응답 지연 추세",
                     # 2026-08-02 정리분
                     "자주 주고받는 상대", "답을 기다리는", "야간·주말",
                     # 검토 기간 선택 바 폐지 — 창은 절마다 고정
                     "검토 기간", '<div class="periods">', "/stats?weeks="):
            self.assertNotIn(gone, out, msg=gone)
        # [분석] 버튼·라디오·폼은 제거됨
        self.assertNotIn("분석</button>", out)
        self.assertNotIn('type="radio"', out)
        self.assertNotIn("<form", out)
        # 조각이므로 셸 요소(doctype/nav/script/backlink)는 web 래퍼 몫 — 여기엔 없음
        self.assertNotIn("<!doctype", out.lower())
        self.assertNotIn("<script", out)
        self.assertNotIn("← Minerva 홈", out)
        # 답 대기 목록은 폐지 — 정규식 요청 판정이라 2026-07-30 신호 노출
        # 전면 폐기와 같은 부류였다(실측 오탐: '별첨 참고 바랍니다'가 1위)
        self.assertNotIn("지그 도면 요청", out)

    def test_stats_new_sections_render(self):
        out = self.report.render_stats(self.store, self.cfg)
        # §1 2계열 라인 (발신+수신 payload — data-chart 는 escape 되어 &quot;)
        self.assertIn('id="trend"', out)
        self.assertIn("series2", out)
        # §2 히트맵 (발신/수신 2개). §5 커버리지 격자는 전용 클래스(covgrid)라
        # 여기 안 섞인다 — 섞으면 CSS 가 그걸 2.1배로 늘린다(2026-08-02).
        self.assertEqual(out.count('class="heatmap"'), 2)
        # §3 받은 메일 구성 누적 막대 + 범례
        self.assertIn('class="mixbar"', out)
        self.assertIn("업무 · 직접(To)", out)
        # 인물 그래프(구 §6)는 인물 화면이 흡수 — 통계에는 /person 링크가 없다
        self.assertNotIn('href="/person?addr=', out)

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
        out = self.report.render_stats(empty, self.cfg)
        self.assertIn("메일이 없습니다", out)
        self.assertNotIn("검토 기간", out)   # 기간 선택 바는 폐지(2026-08-02)


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

    # ─────────────── 재구성 레이아웃(2026-07-17) 장식·구조

    def test_lead_paragraphs_become_summary_card(self):
        html = self._html("# 2026-07-17 데일리 리뷰\n\n요약 문장 하나 (#3).\n\n"
                          "## 지금 할 일 (0건)\n- 없음\n")
        self.assertIn("<div class='dsum'><p>요약 문장 하나", html)
        self.assertIn('<a href="/thread/3">#3</a>', html)
        # 옛 형식(머리 문단 없음) → 카드 없음, 기존 렌더 그대로
        self.assertNotIn("dsum", self._html("# d\n\n## 오늘 델타\n- x\n"))

    def test_reference_section_folds_as_details(self):
        html = self._html("## 지금 할 일 (0건)\n- 없음\n\n"
                          "## 참고\n- 수신 3건 처리됨\n")
        self.assertIn("<details class='dref'><summary>참고</summary>", html)
        self.assertNotIn("<h2>참고</h2>", html)
        self.assertTrue(html.rstrip().endswith("</details></div>"))

    def test_ai_review_escapes_the_reference_fold(self):
        # 2026-08-01: 사용자가 AI 회고 버튼을 눌러 얻은 분석이 접힌 '참고' 안쪽에
        # 제목도 없이 들어가 있었다 — `#` 제목을 버리고 details 도 안 닫았다.
        # 저장돼 있는 옛 형식(참고 뒤에 `# AI 회고 분석`)도 제대로 나와야 한다.
        html = self._html("# 2026-07-30 일간 회고\n\n수신 12\n\n"
                          "## 참고\n- 내가 보낸 것 (1건)\n\n---\n\n"
                          "# AI 회고 분석\n\n드라이버 스펙 검토가 진행됐습니다.\n")
        self.assertIn("<h2>AI 회고 분석</h2>", html)
        self.assertLess(html.index("</details>"), html.index("AI 회고 분석"))
        self.assertIn("드라이버 스펙 검토", html)
        # 맨 위 날짜 제목은 페이지 h1 과 중복이라 계속 건너뛴다
        self.assertNotIn("일간 회고</h2>", html)

    def test_rule_and_blockquote_are_not_literal(self):
        # `---` 가 <p>---</p> 로, 주간 머리의 `> ⚠ 인증 만료` 안내가 `&gt; ⚠ …` 로
        # 찍히고 있었다. 하필 꼭 읽혀야 하는 줄이다.
        html = self._html("# 주간 보고\n\n> ⚠ AI 백엔드 인증 만료 — 다시 로그인\n\n"
                          "## 내 차례 (1건)\n- [#5] 건\n\n---\n\n조사 범위: …\n")
        self.assertIn("<blockquote><p>", html)
        self.assertIn("AI 백엔드 인증 만료", html)
        self.assertIn("<hr>", html)
        self.assertNotIn("<p>---</p>", html)
        self.assertNotIn("<p>&gt;", html)
        self.assertEqual(html.count("<blockquote>"), html.count("</blockquote>"))

    def test_priority_chip_star_and_continuation(self):
        html = self._html("## 지금 할 일 (1건)\n"
                          "- 🔴결정 [상] ★ [#5] 박: 결정건 — D+2 · ⏰기한\n"
                          "  ↳ 품의 대기 → 승인 회신\n")
        self.assertIn("<span class='pri hi'>상</span>", html)
        self.assertIn("<span class='star'>★</span>", html)
        self.assertIn("<span class='ddl'>⏰기한</span>", html)
        # 들여쓴 부연(↳)은 항목의 연속 줄 — 목록을 끊지 않는다
        self.assertIn("<br><span class='cont'>↳ 품의 대기 → 승인 회신</span>", html)
        import re as _re
        self.assertEqual(len(_re.findall(r"<ul[ >]", html)), 1)


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

    def _fake_stream_backend(self, tmpdir):
        """stream-json 각본을 뱉는 가짜 claude — basename 이 claude 라 스트리밍
        게이트에 걸린다. 프롬프트에 FAIL/NORESULT 가 있으면 각 경로를 연기."""
        script = Path(tmpdir) / "claude"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "p = sys.stdin.read()\n"
            "if 'FAIL' in p:\n"
            "    sys.stderr.write('가짜 백엔드 오류'); sys.exit(3)\n"
            "def out(d): print(json.dumps(d, ensure_ascii=False))\n"
            "if 'ERRRESULT' in p:\n"
            "    out({'type':'result','is_error':True,"
            "'result':'과부하 — 잠시 후 다시'}); sys.exit(0)\n"
            "if 'NOISE' in p:\n"
            "    out({'error':{'message':'overloaded'}}); sys.exit(0)\n"
            "if 'PLAIN' in p:\n"                      # 스트리밍 플래그 무시
            "    print('평문 답변입니다'); sys.exit(0)\n"
            "out({'type':'system','subtype':'init','model':'claude-testmodel-9'})\n"
            "out({'type':'stream_event','event':{'type':'content_block_start',"
            "'content_block':{'type':'thinking'}}})\n"
            "out({'type':'stream_event','event':{'type':'content_block_delta',"
            "'delta':{'type':'thinking_delta','thinking':'생각'}}})\n"
            "out({'type':'stream_event','event':{'type':'content_block_start',"
            "'content_block':{'type':'text'}}})\n"
            "out({'type':'stream_event','event':{'type':'content_block_delta',"
            "'delta':{'type':'text_delta','text':'부분 '}}})\n"
            "out({'type':'stream_event','event':{'type':'content_block_delta',"
            "'delta':{'type':'text_delta','text':'초안'}}})\n"
            "if 'NORESULT' not in p:\n"
            "    out({'type':'result','is_error':False,'result':'최종 본문'})\n",
            encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_stream_runner_events_result_and_fallback(self):
        # 이벤트 중립 어휘(model/phase/delta)와 최종 텍스트 계약 — result 이벤트
        # 우선, 없으면 text 델타 누적 폴백. 모르는 이벤트는 조용히 버린다.
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            events = []
            out = review._ai_run_stream([script], "질문", 30, events.append)
            self.assertEqual(out, "최종 본문")
            kinds = [(e["ev"], e.get("phase")) for e in events]
            self.assertIn(("model", None), kinds)
            self.assertIn(("phase", "thinking"), kinds)
            self.assertIn(("phase", "writing"), kinds)
            self.assertEqual(events[0]["model"], "claude-testmodel-9")
            writing = [e for e in events
                       if e["ev"] == "delta" and e["phase"] == "writing"]
            self.assertEqual("".join(e["text"] for e in writing), "부분 초안")
            # result 이벤트가 없으면 델타 누적으로 폴백
            out2 = review._ai_run_stream([script], "NORESULT", 30, lambda e: None)
            self.assertEqual(out2, "부분 초안")

    def test_stream_runner_error_and_cancel(self):
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            # 비정상 종료 → AIError + ai_error.jsonl 기록
            old = review.AI_ERROR_LOG_DIR
            review.AI_ERROR_LOG_DIR = Path(t) / "logs"
            try:
                with self.assertRaises(review.AIError) as cm:
                    review._ai_run_stream([script], "FAIL", 30, lambda e: None)
                self.assertIn("가짜 백엔드 오류", str(cm.exception))
                rec = json.loads((review.AI_ERROR_LOG_DIR / "ai_error.jsonl")
                                 .read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(rec["exit"], 3)
            finally:
                review.AI_ERROR_LOG_DIR = old
            # 취소 — 재시도 없이 AICancelled (오류 아님)
            ev = threading.Event()
            ev.set()
            with self.assertRaises(review.AICancelled):
                review._ai_run_stream([script], "질문", 30,
                                      lambda e: None, cancel=ev)

    def test_ai_run_emits_failed_only_on_exhaustion(self):
        # 재시도 소진 시에만 failed 1회 — weekly 처럼 AIError 를 삼키는 호출부의
        # 유일한 실패 가시화. 성공 호출에선 나오지 않는다.
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            events = []
            with self.assertRaises(review.AIError):
                review.ai_run([script], "FAIL", timeout=30, retries=1,
                              on_event=events.append)
            kinds = [e["ev"] for e in events]
            self.assertEqual(kinds.count("retry"), 1)
            self.assertEqual(kinds.count("failed"), 1)
            self.assertLess(kinds.index("retry"), kinds.index("failed"))
            fail = [e for e in events if e["ev"] == "failed"][0]
            self.assertIn("가짜 백엔드 오류", fail["error"])
            self.assertNotIn("\n", fail["error"])           # 한 줄 보장
            ok_events = []
            review.ai_run([script], "질문", timeout=30, retries=1,
                          on_event=ok_events.append)
            self.assertNotIn("failed", [e["ev"] for e in ok_events])

    def test_stream_runner_error_result_and_noise_are_diagnosable(self):
        # 실사례(2026-07-28 21:21~32): API 오류 창에서 CLI 가 exit 0 으로
        # (a) is_error result 또는 (b) type 없는 오류 JSON 한 줄만 냈고,
        # 로그엔 'empty' 만 남아 원인 페이로드를 잃었다. 둘 다 로그와 오류
        # 메시지에 원문이 실려야 재현 없이 진단된다.
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            old = review.AI_ERROR_LOG_DIR
            review.AI_ERROR_LOG_DIR = Path(t) / "logs"
            try:
                with self.assertRaises(review.AIError) as cm:
                    review._ai_run_stream([script], "ERRRESULT", 30,
                                          lambda e: None)
                self.assertIn("과부하", str(cm.exception))     # 원인 노출
                rec = json.loads((review.AI_ERROR_LOG_DIR / "ai_error.jsonl")
                                 .read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(rec["reason"], "error_result")
                self.assertIn("과부하", rec["error_result"])

                with self.assertRaises(review.AIError) as cm:
                    review._ai_run_stream([script], "NOISE", 30,
                                          lambda e: None)
                self.assertIn("overloaded", str(cm.exception))  # 원문 노출
                rec = json.loads((review.AI_ERROR_LOG_DIR / "ai_error.jsonl")
                                 .read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(rec["reason"], "error_result")
                self.assertIn("overloaded", rec["error_result"])
            finally:
                review.AI_ERROR_LOG_DIR = old

    def test_stream_runner_falls_back_to_plain_output(self):
        # CLI 가 stream-json 을 안 낼 수도 있다(플래그 유실·구버전). 답 자체는
        # 멀쩡하므로 실패시키지 않고 그대로 쓴다 — 진행 표시만 잃는다.
        # 2026-07-28 실사고: 이 폴백이 없어 분석·주간 보고가 전부 죽었다.
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            old = review.AI_ERROR_LOG_DIR
            review.AI_ERROR_LOG_DIR = Path(t) / "logs"
            try:
                evs = []
                out = review._ai_run_stream([script], "PLAIN", 30, evs.append)
                self.assertEqual(out, "평문 답변입니다")
                self.assertEqual(evs, [])              # 진행 이벤트는 없다
                rec = json.loads((review.AI_ERROR_LOG_DIR / "ai_error.jsonl")
                                 .read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(rec["reason"], "plain_output")
            finally:
                review.AI_ERROR_LOG_DIR = old

    def test_system_prompt_is_folded_to_one_line(self):
        # Windows 의 npm .cmd 셔틀은 cmd.exe 를 거치고, cmd.exe 는 명령줄의
        # 개행에서 줄을 끊는다 — 여러 줄 --system-prompt 를 주면 그 뒤 인자가
        # 통째로 사라져(--tools·--setting-sources·스트리밍 플래그) 분석이
        # 평문을 받고 죽었다(2026-07-28 실기기 확정). 리눅스 execve 에서는
        # 드러나지 않으므로 플랫폼 분기 없이 항상 접는다.
        multi = "첫 줄 지시.\n둘째 줄 지시.\n\n셋째 줄."
        out, _ = review._ai_request(["claude", "-p"], "질문", multi, None, None)
        sp = out[out.index("--system-prompt") + 1]
        self.assertNotIn("\n", sp)
        self.assertEqual(sp, "첫 줄 지시. 둘째 줄 지시. 셋째 줄.")
        # 접은 뒤에도 나머지 플래그가 뒤에 온전히 붙는다
        for flag in ("--tools", "--no-session-persistence", "--setting-sources"):
            self.assertIn(flag, out)
        self.assertGreater(out.index("--setting-sources"),
                           out.index("--system-prompt"))
        # 실제 사용되는 시스템 프롬프트 상수도 cmd.exe 특수문자가 없어야 한다
        from mailkb import ask as ask_mod, weekly as weekly_mod
        for text in (review.MAIL_EVIDENCE_SYSTEM, ask_mod.ANALYSIS_SYSTEM,
                     weekly_mod.WEEKLY_SYSTEM):
            self.assertNotRegex(" ".join(text.split()), r'[%&|<>^"]')
        # claude 외 백엔드는 원문 그대로 [SYSTEM] 블록 — 개행 보존
        _, prompt = review._ai_request(["echo"], "질문", multi, None, None)
        self.assertIn("첫 줄 지시.\n둘째 줄 지시.", prompt)

    def test_ai_run_streams_only_for_claude_and_emits_retry(self):
        with tempfile.TemporaryDirectory() as t:
            script = self._fake_stream_backend(t)
            # claude(basename) + on_event → 스트리밍 경로
            events = []
            out = review.ai_run([script], "질문", timeout=30, retries=0,
                                on_event=events.append)
            self.assertEqual(out, "최종 본문")
            self.assertTrue(any(e["ev"] == "delta" for e in events))
            # 비-claude 는 on_event 를 무시하고 블로킹 경로(재시도 이벤트만 공통)
            events2 = []
            out2 = review.ai_run(["python3", "-c", "print('평문')"],
                                 "질문", timeout=30, retries=0,
                                 on_event=events2.append)
            self.assertEqual(out2, "평문")
            self.assertEqual(events2, [])
            # 실패 시 재시도 이벤트가 흐른다
            events3 = []
            with self.assertRaises(review.AIError):
                review.ai_run([script], "FAIL", timeout=30, retries=1,
                              on_event=events3.append)
            self.assertIn({"ev": "retry", "attempt": 1, "total": 1, "wait": 2},
                          events3)

    def test_ai_request_claude_flags_and_holdouts(self):
        # 재도입: --system-prompt·--tools ""·--no-session-persistence·
        # --setting-sources user. setting-sources 값은 반드시 `user` — `""` 는
        # user env(모델 라우팅)까지 제외해 --model 별칭이 다른 모델을 찾다
        # 전 모델 exit 1 로 실패했다(2026-07-28 실기기 확정 — 실사고 원인).
        # 보류 유지(사용자 결정): --json-schema. --effort 는 opt-in 전환
        # (2026-08-02) — 선언(effort_flag) 없으면 여전히 방출하지 않는다.
        for cmd in (["claude", "-p", "--model", "sonnet"],
                    ["C:\\Users\\x\\AppData\\Roaming\\npm\\claude.CMD", "-p"]):
            out, prompt = review._ai_request(
                cmd, "메일 원문", "고정 역할", {"type": "object"}, "high")
            self.assertEqual(prompt, "메일 원문")
            self.assertEqual(out[out.index("--system-prompt") + 1], "고정 역할")
            self.assertEqual(out[out.index("--tools") + 1], "")
            self.assertIn("--no-session-persistence", out)
            self.assertEqual(out[out.index("--setting-sources") + 1], "user")
            for held in ("--json-schema", "--effort"):
                self.assertNotIn(held, out, held)

    def test_ai_request_effort_flag_opt_in(self):
        # 선언한 백엔드에만 [플래그, 값]이 붙는다 — 비-claude 도 선언이 곧
        # "이 CLI 가 안다"는 사용자 확인이므로 방출한다.
        out, _ = review._ai_request(["claude", "-p"], "q", None, None, "high",
                                    effort_flag="--effort")
        self.assertEqual(out[out.index("--effort") + 1], "high")
        out2, _ = review._ai_request(["python3", "adapter.py"], "q", None,
                                     None, "high", effort_flag="--effort")
        self.assertEqual(out2[out2.index("--effort") + 1], "high")
        # cmd 에 이미 있으면 중복 방출하지 않는다
        out3, _ = review._ai_request(["claude", "-p", "--effort", "low"], "q",
                                     None, None, "high", effort_flag="--effort")
        self.assertEqual(out3.count("--effort"), 1)
        # effort 값이 없으면(요약 등) 선언이 있어도 안 붙는다
        out4, _ = review._ai_request(["claude", "-p"], "q", None, None, None,
                                     effort_flag="--effort")
        self.assertNotIn("--effort", out4)

    def test_ai_request_effort_flag_rejects_cmd_exe_hazards(self):
        # cmd.exe .cmd 셔틀은 공백·개행·특수문자에서 뒤 인자를 삼킨다 —
        # 잘못된 선언은 방출을 조용히 생략한다(전 호출을 죽이는 것보다 낫다).
        for bad in ("--effort high", "--eff\nort", "--effort%X", '--e"f'):
            out, _ = review._ai_request(["claude", "-p"], "q", None, None,
                                        "high", effort_flag=bad)
            self.assertNotIn(bad, out, bad)
        # 값 쪽이 오염돼도 마찬가지
        out, _ = review._ai_request(["claude", "-p"], "q", None, None,
                                    "hi gh", effort_flag="--effort")
        self.assertNotIn("--effort", out)

    def test_config_ai_effort_flag_declaration(self):
        cfg = Config(home=Path("/tmp/x"), my_addresses=[ME],
                     ai_backends={"internal": {"cmd": ["opencode", "run"],
                                               "effort_flag": "--effort"},
                                  "sonnet": {"cmd": ["claude", "-p"]}})
        self.assertEqual(cfg.ai_effort_flag("internal"), "--effort")
        self.assertIsNone(cfg.ai_effort_flag("sonnet"))    # 미선언 = 무방출
        self.assertIsNone(cfg.ai_effort_flag("haiku"))     # 내장 백엔드도 무방출

    def test_ai_request_adapters_get_system_block_by_basename(self):
        # 판별은 실행 파일 basename — 절대 경로(~/claude_work/...)의 bedrock
        # 어댑터가 claude 로 오인되면 전용 플래그에 argparse 가 죽는다(실측).
        for cmd in (["python3", "adapter.py"],
                    ["python3", "/home/x/claude_work/mailkb/tools/bedrock_run.py"]):
            out, prompt = review._ai_request(
                cmd, "메일 원문", "고정 역할", None, None)
            self.assertEqual(out, cmd, cmd)            # 플래그 없음
            self.assertIn("[SYSTEM]\n고정 역할\n[/SYSTEM]", prompt)
            self.assertTrue(prompt.endswith("메일 원문"))

    def test_ai_error_log_captures_cmd_and_output(self):
        # 실패 시 재현 없이 로그만으로 원인을 찾는다 — 명령·exit·stderr/stdout
        # 꼬리를 <home>/logs/ai_error.jsonl 에 남기고, claude -p 가 오류를
        # stdout 으로 내는 경우를 위해 stderr 가 비면 stdout 을 메시지에 싣는다.
        tmp = tempfile.TemporaryDirectory()
        old = review.AI_ERROR_LOG_DIR
        review.AI_ERROR_LOG_DIR = Path(tmp.name) / "logs"
        try:
            with self.assertRaises(review.AIError) as cm:
                review.ai_run(
                    ["python3", "-c",
                     "import sys; print('진짜 원인은 stdout'); sys.exit(7)"],
                    "p", timeout=30, retries=0)
            self.assertIn("진짜 원인은 stdout", str(cm.exception))
            rec = json.loads((review.AI_ERROR_LOG_DIR / "ai_error.jsonl")
                             .read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(rec["reason"], "exit")
            self.assertEqual(rec["exit"], 7)
            self.assertIn("진짜 원인은 stdout", rec["stdout"])
            self.assertIn("python3", rec["cmd"][0])
            self.assertIn("ts", rec)
        finally:
            review.AI_ERROR_LOG_DIR = old
            tmp.cleanup()


def _act_sets(store, cfg):
    """(REQUIRED, MAYBE, ⏰) 스레드 집합 — 구 web._action_state 대체.
    신호 UI(탭·칩·x키)는 2026-07-30 제거됐지만 판정 엔진은 주간 보고 재료로
    남아, 엔진 수준 회귀는 계속 지킨다."""
    acts = actions.classify_threads(store, cfg)
    req = {t for t, a in acts.items() if a.level == actions.REQUIRED}
    may = {t for t, a in acts.items() if a.level == actions.MAYBE}
    dl = {t for t, a in acts.items()
          if a.has_deadline and a.level != actions.NONE}
    return req, may, dl


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

    # ---- 선택 검색(본문에서 드래그해 온 질의) — 2026-08-07 ----
    def _hit_ids(self, qs):
        html = web.render_search(self.store, self.cfg, qs, "2026-07-13")
        # focus 뒤에 hl(칠할 말)이 더 붙을 수 있다 — 따옴표를 기대하지 않는다
        return set(re.findall(r"\?focus=(\d+)", html))

    def test_selection_search_drops_the_mail_being_read(self):
        # 안 빼면 1등이 늘 방금 읽던 그 문장이다(실측) — 그 한 건만 빠져야 한다
        base = self._hit_ids({"q": ["리포트"]})
        self.assertTrue(base, "이 질의에 결과가 있어야 시험이 성립한다")
        drop = sorted(base)[0]
        got = self._hit_ids({"q": ["리포트"], "exclude": [drop]})
        self.assertEqual(got, base - {drop})

    def test_bad_exclude_is_ignored_not_fatal(self):
        base = self._hit_ids({"q": ["리포트"]})
        for junk in ("", "abc", "1e3", "-"):
            self.assertEqual(self._hit_ids({"q": ["리포트"], "exclude": [junk]}), base, junk)

    def test_selection_header_only_when_it_came_from_a_selection(self):
        sel = web.render_search(self.store, self.cfg,
                                {"q": ["리포트"], "sel": ["1"]}, "2026-07-13")
        self.assertIn("class='selq'", sel)
        self.assertIn("「리포트」", sel)
        plain = web.render_search(self.store, self.cfg, {"q": ["리포트"]}, "2026-07-13")
        self.assertNotIn("class='selq'", plain)

    def test_selected_text_is_escaped_in_the_header(self):
        # 선택은 사용자가 고른 메일 본문이라 태그가 섞여 들어올 수 있다
        html = web.render_search(self.store, self.cfg,
                                 {"q": ["<img src=x onerror=alert(1)>"], "sel": ["1"]},
                                 "2026-07-13")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<img src=x", html)

    def _chips(self, qs):
        html = web.render_search(self.store, self.cfg, qs, "2026-07-13")
        return re.findall(r"class='nchip' href='([^']+)'>([^<]+)<", html)

    def test_no_chips_when_the_selection_already_matched_precisely(self):
        # 정밀 결과(tier 1~3)가 있으면 좁힐 이유가 없다
        self.assertEqual(self._chips({"q": ["모델 평가"], "sel": ["1"]}), [])

    def test_chips_offer_words_instead_of_rewriting_the_query(self):
        # 자동 정제(상위 3개 AND)는 실측에서 0건이 30~64% 였다 — 고르지 말고 낸다.
        sel = "이번 분기 온디바이스 추론 벤치마크 파이프라인 구축 일정 확인 부탁드립니다"
        chips = self._chips({"q": [sel], "sel": ["1"]})
        self.assertTrue(chips, "정밀 결과가 없으면 좁힐 말을 내야 한다")
        self.assertLessEqual(len(chips), 3)
        labels = [c[1] for c in chips]
        self.assertIn("파이프라인", labels)                  # 긴 말 순
        self.assertNotIn("부탁드립니다", labels)             # 서술형 어미는 뺀다
        for href, _ in chips:
            self.assertIn("sel=1", href)                    # 눌러도 선택 검색 맥락 유지

    def test_chips_carry_the_exclusion_forward(self):
        sel = "이번 분기 온디바이스 추론 벤치마크 파이프라인 구축 일정 확인 부탁드립니다"
        chips = self._chips({"q": [sel], "sel": ["1"], "exclude": ["1"]})
        self.assertTrue(chips)
        for href, _ in chips:
            self.assertIn("exclude=1", href)   # 좁혀도 읽던 메일은 계속 빠진다

    def test_chips_only_for_selections_not_typed_queries(self):
        # 직접 친 질의는 검색창에서 고치면 된다 — 참견하지 않는다
        sel = "이번 분기 온디바이스 추론 벤치마크 파이프라인 구축 일정 확인 부탁드립니다"
        self.assertEqual(self._chips({"q": [sel]}), [])

    def test_no_chip_section_when_there_is_nothing_to_offer(self):
        html = web.render_search(self.store, self.cfg,
                                 {"q": ["zzz"], "sel": ["1"]}, "2026-07-13")
        self.assertNotIn("class='narrow'", html)   # 후보 0개면 절 자체가 없다

    # ---- 검색으로 들어간 스레드에서 그 낱말 강조 — 2026-08-07 ----
    def test_link_carries_only_the_words_not_the_filters(self):
        # from:·after: 는 칠하면 안 된다 — 본문 낱말만 넘긴다
        self.assertEqual(web._hl_terms('from:강미래 after:2026-06 "모델 평가" 리포트'),
                         "모델 평가 리포트")
        self.assertEqual(web._hl_terms("from:강미래"), "")     # 낱말이 없으면 빈 문자열
        self.assertEqual(web._hl_terms(""), "")

    def test_search_result_links_carry_hl(self):
        html = web.render_search(self.store, self.cfg, {"q": ["리포트"]}, "2026-07-13")
        self.assertIn("&hl=%EB%A6%AC%ED%8F%AC%ED%8A%B8", html)
        # 낱말이 없는 질의(필터만)면 hl 을 안 붙인다
        only = web.render_search(self.store, self.cfg,
                                 {"q": ["from:강미래"]}, "2026-07-13")
        self.assertNotIn("&hl=", only)

    def test_highlight_uses_ranges_and_opens_folded_quotes(self):
        # JS 계약 고정 — 본문 마크업을 건드리지 않고, 접힘을 **먼저** 편다.
        js = web._APP_JS
        self.assertIn("CSS.highlights", js)
        self.assertIn("createRange", js)
        self.assertNotIn("insertNode", js)              # Range 로 DOM 을 바꾸지 않는다
        self.assertIn("details.qfold", js)              # 접힌 인용을 연다
        self.assertLess(js.index("applyHl(fin.searchParams"),
                        js.index("if (focus) focusMsg(p, focus)"))   # 강조 → 스크롤 순
        self.assertIn("HL_MAX", js)                     # 흔한 말 상한

    def test_selection_hook_is_wired_and_guarded(self):
        # JS 는 문법 검사밖에 못 하므로 계약만 고정한다(브라우저 확인은 별도).
        js = web._APP_JS
        self.assertIn("selfind", js)
        self.assertIn('"/search?sel=1&q="', js.replace("'", '"'))   # 질의 형태
        self.assertIn("#right .mbody", js)          # 우측 본문에서만 뜬다
        self.assertIn("mousedown", js)              # 포커스를 훔치지 않는다
        self.assertIn("SEL_MAX", js)                # 원문 상한
        self.assertIn("load(url, \"left\")", js)    # 결과는 좌측 — 읽던 자리 보존


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

    def test_ai_search_run_basename_detection_skips_adapters(self):
        # _ai_request 와 같은 결함의 잔존 지점 — 절대 경로(~/claude_work/...)의
        # bedrock 어댑터가 claude 로 오인되면 --output-format json 이 붙어
        # argparse 가 죽는다. 판별은 실행 파일 basename 만 본다.
        self.cfg.ai_backends["bedrock-opus"] = {"cmd": [
            "python3", "/home/x/claude_work/mailkb/tools/bedrock_run.py",
            "--model", "opus"]}
        with mock.patch.object(review, "ai_run", return_value="평문 응답") as run:
            out = review._ai_search_run(self.cfg, "p", "bedrock-opus", 30, None)
        self.assertEqual(out, "평문 응답")
        self.assertNotIn("--output-format", run.call_args[0][0])

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
        self.assertIn("id='ai-extra'", inner)
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
        # 클릭 즉시 그리는 낙관적 대기 화면도 서버 대기 카드와 같은 골격이어야
        # 폴링이 슬롯을 패치할 수 있고 모양도 튀지 않는다.
        self.assertIn("waitcard", web._APP_JS)
        self.assertIn("#ai-elapsed", web._APP_JS)               # 경과 시간 카운터
        self.assertIn("jobT0.ai = Date.now()", web._APP_JS)     # 경과 소유자 일원화
        self.assertNotIn("aielapsed", web._APP_JS)              # 옛 id 잔재 없음
        self.assertNotIn("수 초 걸립니다", web._APP_JS)          # 비현실적 문구 제거됨
        self.assertIn(".aicards", web._CSS)                     # 카드 스타일

    def test_aisearch_cancelled_shows_normal_results(self):
        # 중지는 실패가 아니다 — 오류 배너나 '진행 중 없음' 빈 화면이 아니라
        # 일반 검색 결과로 내려앉는다(다른 잡의 중지 처리와 같은 결).
        try:
            with web._aisearch_lock:
                web._aisearch_job.update(running=False, stage="cancelled",
                                         query="양자화", result=None, error="")
            inner, running = web.render_aisearch_status(
                self.store, self.cfg, "2026-07-14")
            self.assertFalse(running)
            self.assertIn("중지했습니다", inner)
            self.assertNotIn("진행 중인 AI 검색이 없습니다", inner)
            self.assertNotIn("쓸 수 없습니다", inner)      # 오류 배너 아님
        finally:
            with web._aisearch_lock:
                web._aisearch_job.update(running=False, stage="", query="",
                                         result=None, error="")

    def test_app_js_has_ai_polling(self):
        # 방법 7·8: 백그라운드 잡 폴링 훅 + 마커
        self.assertIn("hookAiPolling", web._APP_JS)
        self.assertIn("/aisearch/status", web._APP_JS)
        self.assertIn("data-aisearch-running", web._APP_JS)
        self.assertIn(".rvfill", web._CSS)                      # 진행 바 스타일

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
        # 액션 판정은 요청 시점 라이브 — 숨김/해제가 판정 집합·목록에 즉시 반영.
        # (↩ 탭은 2026-07-30 제거 — 판정 엔진과 전체 목록으로 같은 회귀를 지킨다)
        self.store.ingest([_recx("w1", "boss@corp.example", "확인 요청",
                                 "2026-07-05T09:00:00", body="확인 부탁", to=[ME])])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE sender_addr='boss@corp.example'"
        ).fetchone()[0]
        aw, _, _ = _act_sets(self.store, self.cfg)
        self.assertIn(tid, aw)                         # 응답 대기 신호
        self.assertIn("확인 요청", web.render_mail(self.store, self.cfg))
        self.store.hide_thread(tid, True)              # 데이터 불변, hidden 만 변경
        aw2, _, _ = _act_sets(self.store, self.cfg)
        self.assertNotIn(tid, aw2)                     # 숨김 → 판정 NONE (라이브)
        self.assertNotIn("확인 요청", web.render_mail(self.store, self.cfg))
        self.store.hide_thread(tid, False)             # 해제 → 다시 보임
        self.assertIn("확인 요청", web.render_mail(self.store, self.cfg))


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
        self.store.db.execute("DELETE FROM sync_state WHERE key='feature_version'")
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
        # 노이즈 설정 변경(차단 추가) → action_version 변경 → 액션만 재접기
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
        # 이름 추가 → feature_version 변경 → 자동 백필로 mentions_me 갱신
        self.store = Store(path, [ME], ["김도현", "박부장"])
        self.assertEqual(self.store.db.execute(
            "SELECT mentions_me FROM message_features WHERE message_id=?",
            (mid,)).fetchone()[0], 1)

    def test_action_closed_by_me_on_replay(self):
        # 데일리 '내 활동' 팩트 — 오늘 내 실질 회신이 열린 슬롯을 종결시킨 스레드
        st = self.store
        st.ingest([self._r("cb1", "kim@corp.example", [ME], "자료건",
                           "2026-07-01T09:00:00", "자료 검토 부탁드립니다.")])
        tid = self._tid("cb1")
        st.ingest([self._r("cb2", ME, ["kim@corp.example"], "RE: 자료건",
                           "2026-07-02T10:00:00", "검토 의견 드립니다.",
                           reply_to="cb1")])
        got = st.action_closed_by_me_on("2026-07-02")
        self.assertEqual([(r["thread_id"], r["subject"]) for r in got],
                         [(tid, "자료건")])
        self.assertEqual(st.action_closed_by_me_on("2026-07-01"), [])
        # 이후 새 요청으로 다시 열려도 '그날 종결' 사실은 유지
        st.ingest([self._r("cb3", "kim@corp.example", [ME], "RE: 자료건",
                           "2026-07-03T09:00:00", "추가 검토 부탁드립니다.",
                           reply_to="cb1")])
        self.assertEqual([r["thread_id"]
                          for r in st.action_closed_by_me_on("2026-07-02")], [tid])

    def test_action_closed_by_me_needs_open_slot_and_substance(self):
        st = self.store
        # 열린 슬롯이 없던 스레드에 내 회신 → 종결 아님
        st.ingest([self._r("cu1", "kim@corp.example", [ME], "공유건",
                           "2026-07-01T09:00:00", "자료 공유드립니다. 참고 바랍니다."),
                   self._r("cu2", ME, ["kim@corp.example"], "RE: 공유건",
                           "2026-07-02T10:00:00", "잘 받았습니다.", reply_to="cu1")])
        self.assertEqual(st.action_closed_by_me_on("2026-07-02"), [])
        # trivial 발신(++수신인 추가)은 해소가 아님 → 종결 집계 안 됨
        st.ingest([self._r("cu3", "kim@corp.example", [ME], "요청건2",
                           "2026-07-03T09:00:00", "검토 부탁드립니다."),
                   self._r("cu4", ME, ["kim@corp.example"], "RE: 요청건2",
                           "2026-07-03T10:00:00", "++박수석", reply_to="cu3")])
        self.assertEqual(st.action_closed_by_me_on("2026-07-03"), [])


class TestDerivedVersionSplit(unittest.TestCase):
    """파생 캐시 수명주기 분리(2026-07-17) — 노이즈 설정 변경이 본문 재분류를
    유발하지 않는다. 차단 1회에 전 메일을 재분류하던 정지(1만 통 ~9s)를 없앤 것."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.path = self.home / "v.sqlite"
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"])
        st = Store(self.path, [ME], ["김도현"], noise=self.cfg)
        st.ingest([
            # 사람의 실제 요청 → 액션 열림
            _rec("v1", "kim@corp.example", [ME], "설계 검토",
                 "2026-07-16T09:00:00", body="내일까지 검토 부탁드립니다."),
            # 나중에 차단할 광고 — 같은 스레드에서 요청 신호를 덮는다
            _rec("v2", "promo@ads.example", [ME], "RE: 설계 검토",
                 "2026-07-16T10:00:00", body="지금 신청 부탁드립니다. 오늘까지!"),
        ])
        self.tid = st.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<v1@t>'").fetchone()[0]
        st.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _sentinel(self):
        """message_features 가 재생성됐는지 알아내는 표식 — 재분류되면 지워진다."""
        st = Store(self.path, [ME], ["김도현"], noise=self.cfg)
        st.db.execute("UPDATE message_features SET has_question=9")
        st.db.commit()
        st.close()

    def _sentinel_alive(self, store) -> bool:
        return bool(store.db.execute(
            "SELECT COUNT(*) FROM message_features WHERE has_question=9"
        ).fetchone()[0])

    def _blocked_cfg(self, *pats):
        return Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                      internal_domains=["corp.example"], blocked_senders=list(pats))

    def test_block_refolds_actions_without_reclassifying_bodies(self):
        self._sentinel()
        cfg2 = self._blocked_cfg("promo@ads.example")
        st = Store(self.path, [ME], ["김도현"], noise=cfg2)
        self.addCleanup(st.close)
        # 본문 사실 캐시는 그대로 — 차단은 '액션 계산에서 뺄지'만 바꾼다
        self.assertTrue(self._sentinel_alive(st))
        # 액션은 다시 접혔다 — 광고가 뺏었던 source 가 사람의 요청으로 복귀
        src = st.db.execute(
            "SELECT message_id FROM messages WHERE id=("
            " SELECT action_source_id FROM thread_state WHERE thread_id=?)",
            (self.tid,)).fetchone()[0]
        self.assertEqual(src, "<v1@t>")

    def test_name_change_still_reclassifies(self):
        self._sentinel()
        # 이름 변경은 mentions_me(저장 비트)를 바꾸므로 전체 백필이 맞다
        st = Store(self.path, [ME], ["김도현", "박부장"], noise=self.cfg)
        self.addCleanup(st.close)
        self.assertFalse(self._sentinel_alive(st))

    def test_unblock_restores_blocked_senders_action(self):
        only_ad = Store(self.path, [ME], ["김도현"],
                        noise=self._blocked_cfg("kim@corp.example"))
        # 사람을 차단하면 그 요청이 사라지고 광고 요청만 남는다
        src = only_ad.db.execute(
            "SELECT message_id FROM messages WHERE id=("
            " SELECT action_source_id FROM thread_state WHERE thread_id=?)",
            (self.tid,)).fetchone()[0]
        self.assertEqual(src, "<v2@t>")
        only_ad.close()
        # 차단 해제 → 원래 액션 복원
        st = Store(self.path, [ME], ["김도현"], noise=self.cfg)
        self.addCleanup(st.close)
        src = st.db.execute(
            "SELECT message_id FROM messages WHERE id=("
            " SELECT action_source_id FROM thread_state WHERE thread_id=?)",
            (self.tid,)).fetchone()[0]
        self.assertEqual(src, "<v2@t>")   # 아무도 차단 안 됨 → 최신 요청이 source

    def test_block_clears_action_when_nothing_left(self):
        # 빈 상태 미기록 함정: 전체 백필은 테이블이 새것이라 안 써도 됐지만,
        # 제자리 재접기는 반드시 지워야 한다 — 안 그러면 옛 액션이 남는다.
        st = Store(self.path, [ME], ["김도현"],
                   noise=self._blocked_cfg("kim@corp.example", "promo@ads.example"))
        self.addCleanup(st.close)
        row = st.db.execute(
            "SELECT action_source_id, action_strength, action_has_deadline "
            "FROM thread_state WHERE thread_id=?", (self.tid,)).fetchone()
        self.assertEqual(tuple(row), (0, "", 0))

    def test_unrelated_thread_untouched_and_version_recorded(self):
        st0 = Store(self.path, [ME], ["김도현"], noise=self.cfg)
        st0.ingest([_rec("v9", "park@corp.example", [ME], "무관건",
                         "2026-07-16T11:00:00", body="회신 부탁드립니다.")])
        other = st0.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<v9@t>'").fetchone()[0]
        cols = ("action_source_id", "action_strength", "action_kind",
                "action_has_deadline", "completion_after_action")
        q = f"SELECT {','.join(cols)} FROM thread_state WHERE thread_id=?"
        before = tuple(st0.db.execute(q, (other,)).fetchone())
        st0.close()
        st = Store(self.path, [ME], ["김도현"],
                   noise=self._blocked_cfg("promo@ads.example"))
        self.addCleanup(st.close)
        self.assertEqual(tuple(st.db.execute(q, (other,)).fetchone()), before)
        # 메시지·액션·어휘 토큰·설정별 bag 버전이 기록돼 다음 열기는 재구축 안 함
        keys = dict(st.db.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE '%_version'"))
        self.assertEqual(set(keys), {
            "clean_version", "feature_version", "action_version",
            "term_feature_version", "term_bag_version",
        })

    def test_allowlist_change_triggers_nothing(self):
        # external_allowlist 는 fold 가 아니라 질의 시점(actions.evaluate)에만
        # 쓰인다 → 어느 버전에도 없어야 한다(백필·재접기 모두 불필요)
        st0 = Store(self.path, [ME], ["김도현"], noise=self.cfg)
        v0 = dict(st0.db.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE '%_version'"))
        st0.close()
        cfg2 = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                      internal_domains=["corp.example"],
                      raw={"filters": {"external_allowlist": ["partner.example"]}})
        st = Store(self.path, [ME], ["김도현"], noise=cfg2)
        self.addCleanup(st.close)
        self.assertEqual(dict(st.db.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE '%_version'")), v0)


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
        aw, may, _ = _act_sets(self.store, self.cfg)
        self.assertEqual(home_req, set(aw))          # 큐 == 판정기 (정의상 동일)
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
        _, _, dl = _act_sets(self.store, self.cfg)
        self.assertIn(tid, dl)
        self.store.ingest([MailRecord(
            message_id="<d2@t>", subject="RE: 기한건", sender_name="me",
            sender_addr=ME, to=["kim@corp.example"],
            sent_on="2026-07-15T10:00:00", body_text="회신드립니다. 확정했습니다.",
            references=["<d1@t>"])])
        _, _, dl2 = _act_sets(self.store, self.cfg)
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
        aw, may, dl = _act_sets(self.store, self.cfg)
        self.assertNotIn(tid, aw | may | dl)         # 판정 세 집합 모두에서 소멸
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
        aw, _, dl = _act_sets(self.store, self.cfg)
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
        self.store.db.execute("DELETE FROM sync_state WHERE key='feature_version'")
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

    def test_signal_ui_fully_removed(self):
        # 신호 칩·해제 라우트·x 토글은 2026-07-30 제거 — 정규식 판정의 정밀도가
        # 낮아 상시 노출이 도구 신뢰를 깎았다. 화면·라우트·JS 어디에도 남지
        # 않아야 한다(판정 엔진과 store 오버레이는 주간 보고 재료로 휴면 유지).
        tid = self._seed()
        out = web.render_thread(self.store, self.cfg, tid)
        for gone in ("↩ 회신 필요", "⏰ 기한", "signal-off", "signal-on",
                     "신호 수동 해제됨", "sigchips"):
            self.assertNotIn(gone, out)
        # 구 해제 라우트는 미지 액션으로 떨어져 안내 리다이렉트(크래시 없음)
        loc = web.perform_action(self.store, self.cfg,
                                 f"/thread/{tid}/signal-off",
                                 {"kind": ["action"]})
        self.assertIn("msg=", loc)
        self.assertNotIn("신호 해제", urllib_unquote(loc))
        # x 토글 서버 분기도 소멸 — 빈 토큰(무동작)
        self.assertEqual(web._toggle_thread(self.store, self.cfg, tid, "signal"),
                         "")
        self.assertNotIn('toggleRow("signal")', web._APP_JS)
        self.assertNotIn("signal-off", web._APP_JS)


    def test_toggle_flag_and_hide_flip(self):
        tid = self._seed()
        self.assertEqual(web._toggle_thread(self.store, self.cfg, tid, "flag"),
                         "flag:on")
        self.assertEqual(self.store.thread(tid)["flagged"], 1)
        self.assertEqual(web._toggle_thread(self.store, self.cfg, tid, "flag"),
                         "flag:off")
        self.assertEqual(self.store.thread(tid)["flagged"], 0)
        self.assertEqual(web._toggle_thread(self.store, self.cfg, tid, "hide"),
                         "hide:on")
        self.assertEqual(self.store.thread(tid)["hidden"], 1)
        self.assertEqual(web._toggle_thread(self.store, self.cfg, tid, "hide"),
                         "hide:off")
        self.assertEqual(self.store.thread(tid)["hidden"], 0)


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


class TestAsk(unittest.TestCase):
    """질문하기 — 적응형 라운드 루프 · 인용 검증 · 3-상태 강등 · 캐시."""

    KIM = "kim@corp.example"

    def test_step_prompt_guides_query_expansion(self):
        # FTS 는 문자 매칭이라 질문 어휘 ≠ 메일 어휘면 후보가 안 잡힌다.
        # 재현율은 질의 다변화가 유일한 레버 — STEP 이 질의 팬아웃(한 번에
        # 2~3개, 전부 실행됨)과 사람/기간 축 우회를 지시하는지 사양으로 고정.
        from mailkb import ask
        self.assertIn("2~3개 질의", ask.STEP)
        self.assertIn("서로 다른 각도", ask.STEP)
        self.assertIn("from:담당자", ask.STEP)
        self.assertIn("하이픈/공백 변형", ask.STEP)

    def setUp(self):
        from mailkb import ask
        self.ask = ask
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(self.home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)
        self.store.ingest([
            _rec("q1", self.KIM, [ME], "양자화 방식 결정", "2026-07-10T09:00:00",
                 body="per-channel 로 확정합니다. 손실 0.7%p 로 목표 이내입니다."),
            _rec("q2", self.KIM, [ME], "일정 안내", "2026-07-03T09:00:00",
                 body="4월 17일 테이프아웃 목표로 진행합니다."),
            _rec("q3", self.KIM, [ME], "RE: 일정 안내", "2026-07-11T09:00:00",
                 body="셔틀이 밀려 5월 8일로 변경합니다."),
        ])
        self.mid = {r["subject"]: r["id"] for r in self.store.db.execute(
            "SELECT id, subject FROM messages")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, replies, verify=None, **kw):
        answers = iter(replies)

        def fake_ai(cmd, prompt, **kwargs):
            if "보수적인 근거 검증기" in prompt:
                verdict = verify if verify is not None else {
                    "supported": [f"c{i}" for i in range(20)]
                    + [f"x{i}" for i in range(20)],
                    "answer_supported": True,
                }
                return json.dumps(verdict, ensure_ascii=False)
            if "검증을 통과한 메일 근거만 사용해 답변을 다시 쓴다" in prompt:
                return json.dumps({"answer": "검증된 메일 근거만 반영한 답변입니다."},
                                  ensure_ascii=False)
            return next(answers)

        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            return self.ask.ask(self.store, self.cfg, kw.pop("q", "질문"),
                                today="2026-07-14", **kw)

    def test_loop_searches_then_reads_then_answers(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"], "why": "1차"}),
            json.dumps({"action": "read", "ids": [mid], "why": "유망"}),
            json.dumps({"action": "answer", "why": "충분"}),
            json.dumps({"state": "확인됨", "answer": "per-channel 로 확정됐습니다.",
                        "claims": [{"text": "per-channel 확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["state"], "확인됨")
        self.assertEqual(len(res["claims"]), 1)
        self.assertEqual(res["claims"][0]["mid"], mid)
        self.assertEqual(res["scope"]["read"], 1)          # 정독 1통
        self.assertEqual(res["scope"]["calls"], 5)         # 3라운드 + 답변 + 의미 검증
        self.assertIn("양자화", res["scope"]["queries"][0])
        self.assertTrue(res["scope"]["counter_queries"])    # 반전 근거는 호스트가 강제

    def test_progress_reports_call_and_input_size(self):
        # 콜 하나가 수 분까지 갈 수 있어 진행 문구에 콜 번호·입력 크기를 싣는다 —
        # 경과초는 클라이언트(#ask-elapsed)가 센다. 라운드·답변 두 지점 사양 고정.
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "read", "ids": [mid], "why": "유망"}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "확정됐습니다.",
                        "claims": [{"text": "확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        msgs = []
        self._run(replies, progress=msgs.append)
        self.assertTrue(any(re.search(r"조사 1라운드.*콜 1/12 · 송신 [\d.]+(?:B|KB)", m)
                            for m in msgs), msgs)
        self.assertTrue(any(re.search(r"답변 작성 중 · 콜 \d+/12 · 송신 [\d.]+(?:B|KB)", m)
                            for m in msgs), msgs)

    def test_unverified_claims_dropped_and_state_demoted(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "read", "ids": [mid]}),   # hits 없음 → 정독 실패
            json.dumps({"state": "확인됨", "answer": "지어낸 답",
                        "claims": [{"text": "환각", "mid": mid,
                                    "quote": "본문에 없는 문장입니다"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["claims"], [])
        self.assertEqual(res["state"], "근거 부족")          # 검증 0 → 강등
        self.assertGreaterEqual(res["scope"]["dropped"], 1)

    def test_quote_must_come_from_the_cited_mail(self):
        # 인용은 있으나 '다른 메일'을 지목 → 메시지 단위 검증에서 탈락
        a, b = self.mid["양자화 방식 결정"], self.mid["일정 안내"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화 일정"]}),
            json.dumps({"action": "read", "ids": [a, b]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "x",
                        "claims": [{"text": "엉뚱한 출처", "mid": b,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["claims"], [])
        self.assertEqual(res["state"], "근거 부족")

    def test_semantic_verifier_rejects_unrelated_exact_quote(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "일정은 8월 1일입니다.",
                        "claims": [{"text": "일정은 8월 1일", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        res = self._run(
            replies,
            verify={"supported": [], "answer_supported": False},
        )
        self.assertEqual(res["state"], "근거 부족")
        self.assertEqual(res["claims"], [])
        self.assertNotIn("8월 1일", res["answer"])
        self.assertIn("근거를 확인하지 못했습니다", res["answer"])

    def _run_with_broken_verifier(self, boom):
        """검증 콜만 고장 내고 나머지 각본은 그대로 — 고장 vs 거부를 가른다."""
        mid = self.mid["양자화 방식 결정"]
        answers = iter([
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "모델의 자유 서술 답변입니다.",
                        "claims": [{"text": "per-channel 확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ])

        def fake_ai(cmd, prompt, **kw):
            if "보수적인 근거 검증기" in prompt:
                return boom()
            return next(answers)

        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            return self.ask.ask(self.store, self.cfg, "양자화 결정?",
                                today="2026-07-14", seed_ids=[mid])

    def test_semantic_verify_failure_keeps_quote_verified_evidence(self):
        # 검증기 고장은 '거부' 가 아니다 — 인용 대조를 통과한 근거까지 버리면 앞선
        # 조사 콜이 통째로 날아간다. 근거는 남기되 답변 본문은 _safe_answer 로.
        def boom():
            raise review.AIError("검증 콜 타임아웃(240s)")

        res = self._run_with_broken_verifier(boom)
        self.assertEqual(res["state"], "확인됨")
        self.assertEqual(len(res["claims"]), 1)
        self.assertFalse(res["scope"]["semantic_checked"])   # 못 했음을 기록
        self.assertNotIn("자유 서술", res["answer"])          # 미검증 서술은 안 내보냄
        self.assertIn("메일에서 확인된 내용", res["answer"])

    def test_semantic_verify_broken_json_is_not_total_rejection(self):
        # 응답 파손도 '전량 거부' 로 읽지 않는다 — 거부와 고장이 구분돼야 한다.
        res = self._run_with_broken_verifier(lambda: "이건 JSON 이 아님")
        self.assertEqual(len(res["claims"]), 1)
        self.assertFalse(res["scope"]["semantic_checked"])

    def test_counter_search_reads_later_mail_in_same_thread(self):
        earlier = self.mid["일정 안내"]
        later = self.mid["RE: 일정 안내"]
        hits, read = {}, {}
        self.ask._seed(self.store, self.cfg, [earlier], hits)
        self.ask._read(self.store, [earlier], hits, read)
        counter = self.ask._counter_search(
            self.store, self.cfg, "일정이 언제인가", ["일정"], hits, read)
        self.assertTrue(any(q.startswith("thread:") for q in counter))
        self.assertIn(later, read)                         # 변경 메일까지 호스트가 정독

    def test_counter_reads_relevant_initial_hit_even_if_search_adds_nothing(self):
        # 최종 메일이 최초 훑기 목록에 이미 있었지만 모델이 앞선 메일만 골라 읽은
        # 회귀: 반전 검색의 '새 id'가 아니어도 선택 스레드 시간축에서 정독해야 한다.
        earlier = self.mid["일정 안내"]
        later = self.mid["RE: 일정 안내"]
        hits, read = {}, {}
        self.ask._seed(self.store, self.cfg, [earlier, later], hits)
        self.ask._read(self.store, [earlier], hits, read)
        with mock.patch.object(self.ask, "_search", return_value=0):
            self.ask._counter_search(
                self.store, self.cfg, "일정이 언제인가", ["일정"], hits, read)
        self.assertIn(later, read)

    def test_search_relevance_floor_drops_or_fallback(self):
        # store.search 의 tier4 = FTS-OR('관련 낮음'). 모델이 직접 고른 질의는 그대로
        # 존중하고(기본 4), 호스트가 조립한 질의에만 하한을 건다.
        q = "양자화 테이프아웃 변경"                    # AND 실패 → OR 폴백
        rows = self.store.search(q, self.ask.HITS_PER_QUERY)
        self.assertTrue(rows and all(r["tier"] == 4 for r in rows))
        loose, tight = {}, {}
        self.assertEqual(self.ask._search(self.store, self.cfg, q, loose), len(rows))
        self.assertEqual(
            self.ask._search(self.store, self.cfg, q, tight, max_tier=3), 0)
        self.assertEqual(tight, {})

    def test_counter_search_rejects_low_relevance_fallback(self):
        # 앵커에 반전어를 덧붙이면 AND 가 되레 깨져 OR 로 폴백한다 — 질문이 구체적일수록
        # 무관한 메일이 쏟아지는 뒤집힌 특성이라, 반전 검색은 그 등급을 받지 않는다.
        seed = self.mid["양자화 방식 결정"]
        hits, read = {}, {}
        self.ask._seed(self.store, self.cfg, [seed], hits)
        self.ask._read(self.store, [seed], hits, read)
        self.ask._counter_search(self.store, self.cfg, "양자화 결정?",
                                 ["양자화 테이프아웃"], hits, read)
        self.assertNotIn(self.mid["일정 안내"], read)      # OR 로만 걸리던 무관 메일

    def test_counter_search_caps_unseen_thread_reads(self):
        # 관련도 하한을 통과해도 안 본 스레드가 근거 풀을 덮지 않게 상한을 둔다.
        # 이 검색의 본래 이득(이미 본 스레드의 후속 메일)은 제한하지 않는다.
        for i in range(1, 5):
            self.store.ingest([_rec(f"rv{i}", self.KIM, [ME], f"리뷰 건 {i}",
                                    f"2026-07-1{i}T09:00:00",
                                    body=f"리뷰 일정을 변경합니다. 안건 {i}.")])
        seed = self.mid["양자화 방식 결정"]
        hits, read = {}, {}
        self.ask._seed(self.store, self.cfg, [seed], hits)
        self.ask._read(self.store, [seed], hits, read)
        seen = {m["thread_id"] for m in read.values()}
        self.ask._counter_search(self.store, self.cfg, "리뷰 결론?", ["리뷰"],
                                 hits, read)
        off = [m for m in read.values() if m["thread_id"] not in seen]
        self.assertEqual(len(off), self.ask.COUNTER_OFF_THREAD)   # 4건 후보 중 2통만

    def test_conflict_state_requires_two_dated_sources(self):
        a, b = self.mid["일정 안내"], self.mid["RE: 일정 안내"]
        replies = [
            # 검색은 AND 라 두 메일을 다 걸려면 공통어로 — 둘 다 훑기 목록에 올라야 정독 가능
            json.dumps({"action": "search", "queries": ["일정"]}),
            json.dumps({"action": "read", "ids": [a, b]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "상충함", "answer": "일정이 변경됐습니다.",
                        "conflicts": [
                            {"label": "나중", "value": "5월 8일", "mid": b,
                             "quote": "셔틀이 밀려 5월 8일로 변경합니다"},
                            {"label": "먼저", "value": "4월 17일", "mid": a,
                             "quote": "4월 17일 테이프아웃 목표로 진행합니다"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["state"], "상충함")
        self.assertEqual([c["value"] for c in res["conflicts"]],
                         ["4월 17일", "5월 8일"])

    def test_unknown_state_demotes_to_insufficient(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "완료", "answer": "상태 형식이 잘못된 답",
                        "claims": [{"text": "per-channel 확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["state"], "근거 부족")
        self.assertEqual(len(res["claims"]), 1)       # 검증된 주변 사실은 보존

    def test_single_conflict_downgrades_to_confirmed(self):
        a = self.mid["일정 안내"]
        replies = [
            json.dumps({"action": "read", "ids": [a]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "상충함", "answer": "x",
                        "claims": [{"text": "초기 일정", "mid": a,
                                    "quote": "4월 17일 테이프아웃 목표로 진행합니다"}],
                        "conflicts": [{"label": "먼저", "value": "4월 17일", "mid": a,
                                       "quote": "4월 17일 테이프아웃 목표로 진행합니다"}]}),
        ]
        # hits 가 없어 read 가 안 되면 검증도 실패 → 먼저 검색을 거치게 한다
        replies = [json.dumps({"action": "search", "queries": ["일정"]})] + replies
        res = self._run(replies)
        self.assertEqual(res["state"], "확인됨")            # 상충 근거 1개면 상충 아님

    def test_insufficient_keeps_partial_facts_and_leads(self):
        mid = self.mid["양자화 방식 결정"]
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE id=?", (mid,)).fetchone()[0]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "근거 부족",
                        "answer": "배포 시점은 확인되지 않습니다.",
                        "claims": [{"text": "확정 사실만 확인됨", "mid": mid,
                                    "quote": "손실 0.7%p 로 목표 이내입니다"}],
                        "leads": [{"tid": tid, "why": "이 스레드에 후속이 있을 수 있음"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["state"], "근거 부족")          # 모델 판정 유지(강등 아님)
        self.assertEqual(len(res["claims"]), 1)             # 확인한 것은 남긴다
        self.assertEqual(res["leads"][0]["thread_id"], tid)
        self.assertIn("확인되지 않", res["answer"])

    def test_leads_must_reference_seen_threads(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "근거 부족", "answer": "x",
                        "leads": [{"tid": 9999, "why": "존재하지 않는 스레드"}]}),
        ]
        res = self._run(replies)
        self.assertEqual(res["leads"], [])                  # 못 본 스레드는 버린다

    def test_round_budget_caps_calls(self):
        # 계속 search 만 하는 모델 → 라운드/콜 상한에서 멈추고 답변 단계로
        searches = [json.dumps({"action": "search", "queries": [f"질의{i}"]})
                    for i in range(10)]
        replies = searches + [json.dumps({"state": "근거 부족", "answer": "못 찾음"})]
        res = self._run(replies)
        self.assertLessEqual(res["scope"]["calls"], self.ask.MAX_CALLS)
        self.assertLessEqual(len(res["scope"]["queries"]), self.ask.MAX_ROUNDS * 3)
        self.assertEqual(res["state"], "근거 부족")

    def test_repeated_query_stops_loop(self):
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "search", "queries": ["양자화"]}),  # 같은 질의 반복
            json.dumps({"state": "근거 부족", "answer": "x"}),
        ]
        res = self._run(replies)
        self.assertEqual(res["scope"]["queries"], ["양자화"])   # 중복은 추가 안 함
        self.assertEqual(res["scope"]["calls"], 3)

    def test_noise_sender_excluded_from_hits(self):
        self.store.ingest([_rec("n1", "noreply@corp.example", [ME], "양자화 자동알림",
                                "2026-07-12T09:00:00", body="양자화 빌드 완료")])
        cfg = Config(home=self.home, my_addresses=[ME],
                     internal_domains=["corp.example"], ignore_senders=["noreply"],
                     ai_default="internal", ai_backends={"internal": {"cmd": ["echo"]}})
        hits = {}
        self.ask._search(self.store, cfg, "양자화", hits)
        self.assertTrue(all("자동알림" not in h["subject"] for h in hits.values()))

    def test_cache_returns_without_ai_calls(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "근거 부족", "answer": "첫 조사"}),
        ]
        replies = [json.dumps({"action": "search", "queries": ["양자화"]})] + replies
        first = self._run(replies)
        self.assertFalse(first["cached"])
        # 두 번째 호출은 AI 를 아예 부르지 않아야 한다(부르면 StopIteration)
        with mock.patch.object(review, "ai_run", side_effect=AssertionError("호출 금지")):
            second = self.ask.ask(self.store, self.cfg, "질문", today="2026-07-14")
        self.assertTrue(second["cached"])
        self.assertEqual(second["answer"], first["answer"])

    def test_new_mail_invalidates_cache(self):
        replies = [json.dumps({"action": "answer"}),
                   json.dumps({"state": "근거 부족", "answer": "이전 답"})]
        self._run(replies)
        basis = self.store.ask_basis()
        self.store.ingest([_rec("z1", self.KIM, [ME], "새 메일", "2026-07-13T09:00:00",
                                body="새 내용입니다.")])
        self.assertGreater(self.store.ask_basis(), basis)   # 기준선 전진 → 캐시 미스
        replies2 = [json.dumps({"action": "answer"}),
                    json.dumps({"state": "근거 부족", "answer": "새 답"})]
        res = self._run(replies2)
        self.assertEqual(res["answer"], "새 답")
        self.assertFalse(res["cached"])

    def test_fresh_bypasses_cache_read_but_writes(self):
        replies = [json.dumps({"action": "answer"}),
                   json.dumps({"state": "근거 부족", "answer": "1차"})]
        self._run(replies)
        replies2 = [json.dumps({"action": "answer"}),
                    json.dumps({"state": "근거 부족", "answer": "2차"})]
        res = self._run(replies2, use_cache=False)
        self.assertEqual(res["answer"], "2차")             # 캐시 우회
        key = self.ask.cache_key(self.store, "질문")
        self.assertTrue(key.startswith(f"v{self.ask.ASK_FEATURE_VERSION}:"))
        row = self.store.ask_get(key)
        self.assertIn("2차", row["result_json"])           # 쓰기는 항상

    def test_empty_question_raises(self):
        with self.assertRaises(review.AIError):
            self.ask.ask(self.store, self.cfg, "   ")

    def test_unresolved_backend_raises_systemexit(self):
        cfg = Config(home=self.home, my_addresses=[ME], ai_search_backend="ghost")
        with self.assertRaises(SystemExit):
            self.ask.ask(self.store, cfg, "질문")

    def test_ask_backend_defaults_to_search_then_honors_own_setting(self):
        # 분석은 오래 AI 검색과 한 설정을 공유했다. [ai] ask 를 새로 두되 기존
        # 사용자 동작을 깨지 않는다 — 미설정이면 search 를 그대로 상속.
        inherited = Config(home=self.home, my_addresses=[ME],
                           ai_search_backend="haiku")
        self.assertEqual(inherited.ai_ask_backend, "haiku")
        own = Config(home=self.home, my_addresses=[ME],
                     ai_search_backend="haiku", ai_ask_backend="sonnet")
        self.assertEqual(own.ai_ask_backend, "sonnet")   # 명시하면 독립
        self.assertEqual(own.ai_search_backend, "haiku")  # 검색은 안 끌려간다

    def test_ask_uses_ask_backend_and_cli_override_is_per_run(self):
        # scope.backend 는 그 답을 실제로 만든 백엔드여야 한다(사이드바 footer 와
        # 대조할 근거). CLI --backend 는 이번 실행만 덮어쓴다.
        mid = self.mid["양자화 방식 결정"]
        replies = [json.dumps({"action": "answer"}),
                   json.dumps({"state": "근거 부족", "answer": "없음"})]
        cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                     ai_search_backend="haiku", ai_ask_backend="sonnet",
                     ai_backends={"sonnet": {"cmd": ["echo"]},
                                  "haiku": {"cmd": ["echo"]},
                                  "ghost": {"cmd": ["echo"]}})
        with mock.patch.object(review, "ai_run", side_effect=list(replies)):
            res = self.ask.ask(self.store, cfg, "설정 백엔드", today="2026-07-14",
                               seed_ids=[mid])
        self.assertEqual(res["scope"]["backend"], "sonnet")
        with mock.patch.object(review, "ai_run", side_effect=list(replies)):
            res2 = self.ask.ask(self.store, cfg, "일회성 덮어쓰기", backend="ghost",
                                today="2026-07-14", seed_ids=[mid])
        self.assertEqual(res2["scope"]["backend"], "ghost")
        self.assertEqual(cfg.ai_ask_backend, "sonnet")    # 설정은 안 바뀐다

    def test_render_text_shows_state_evidence_scope(self):
        mid = self.mid["양자화 방식 결정"]
        replies = [
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "per-channel 확정.",
                        "claims": [{"text": "확정됨", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ]
        out = self.ask.render_text(self._run(replies))
        self.assertIn("확인됨", out)
        self.assertIn("per-channel 확정.", out)
        # 인용에 종결부호가 붙는다 — 문맥을 떼어 붙일 때 「…합니다」 뒤에 마침표만
        # 따로 뜨는 것을 막으려고 quote_context 가 흡수한다(2026-08-03)
        self.assertIn("「per-channel 로 확정합니다.」", out)
        self.assertIn("조사 범위", out)
        self.assertIn("검색: 양자화", out)

    def test_history_lists_latest_per_question(self):
        for ans in ("첫 답", "다시 물음"):
            self._run([json.dumps({"action": "answer"}),
                       json.dumps({"state": "근거 부족", "answer": ans})],
                      use_cache=False)
        self._run([json.dumps({"action": "answer"}),
                   json.dumps({"state": "근거 부족", "answer": "다른 질문 답"})],
                  q="다른 질문")
        hist = self.ask.history(self.store)
        self.assertEqual(len(hist), 2)                  # 질문별 최신 1건씩
        self.assertEqual({h["question"] for h in hist}, {"질문", "다른 질문"})
        self.assertTrue(all(h["id"] for h in hist))

    def test_load_reopens_stored_answer_with_staleness(self):
        res = self._run([json.dumps({"action": "answer"}),
                         json.dumps({"state": "근거 부족", "answer": "그때의 답"})])
        rid = res["id"]
        self.store.ingest([_rec("later", self.KIM, [ME], "이후 메일",
                                "2026-07-13T09:00:00", body="새 내용")])
        got = self.ask.load(self.store, rid)             # 새 메일이 와도 그때 답 보존
        self.assertEqual(got["answer"], "그때의 답")
        self.assertTrue(got["cached"])
        self.assertGreaterEqual(got["stale"], 1)         # 이후 새 메일 수 표시
        self.assertIsNone(self.ask.load(self.store, 99999))

    def test_followup_inherits_reading_and_queries(self):
        mid = self.mid["양자화 방식 결정"]
        parent = self._run([
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "per-channel 확정.",
                        "claims": [{"text": "확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ])
        self.assertEqual(parent["scope"]["read_ids"], [mid])
        # 추가 질문: 검색·정독 없이 곧바로 답해도 부모의 본문으로 인용 검증이 통과해야
        prompts = []

        def spy(cmd, prompt, **kw):
            prompts.append(prompt)
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"], "answer_supported": True})
            return (json.dumps({"action": "answer"}) if len(prompts) == 1 else
                    json.dumps({"state": "확인됨", "answer": "손실은 0.7%p 입니다.",
                                "claims": [{"text": "손실 수치", "mid": mid,
                                            "quote": "손실 0.7%p 로 목표 이내입니다"}]}))

        with mock.patch.object(review, "ai_run", side_effect=spy):
            child = self.ask.ask(self.store, self.cfg, "손실은 얼마였지?",
                                 today="2026-07-14", parent_id=parent["id"])
        self.assertEqual(child["state"], "확인됨")
        self.assertEqual(len(child["claims"]), 1)        # 물려받은 본문으로 검증 통과
        self.assertEqual(child["scope"]["read"], 1)      # 재정독 없이 승계
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["parent_question"], "질문")
        self.assertIn("양자화", child["scope"]["queries"])  # 부모 질의 승계(중복 방지)
        self.assertIn("[이전 질문] 질문", prompts[0])       # 프롬프트에 이전 문답
        self.assertIn("추가 질문", prompts[0])

    def test_followup_cache_key_differs_from_parent(self):
        parent = self._run([json.dumps({"action": "answer"}),
                            json.dumps({"state": "근거 부족", "answer": "부모"})])
        child = self._run([json.dumps({"action": "answer"}),
                           json.dumps({"state": "근거 부족", "answer": "자식"})],
                          q="질문", parent_id=parent["id"])
        self.assertEqual(child["answer"], "자식")        # 같은 문구여도 별개 캐시
        self.assertNotEqual(child["id"], parent["id"])

    def test_render_shows_followup_hint_and_parent(self):
        parent = self._run([json.dumps({"action": "answer"}),
                            json.dumps({"state": "근거 부족", "answer": "부모 답"})])
        out = self.ask.render_text(parent)
        self.assertIn("--follow", out)                   # 이어서 묻는 법 안내
        child = self._run([json.dumps({"action": "answer"}),
                           json.dumps({"state": "근거 부족", "answer": "자식 답"})],
                          q="추가", parent_id=parent["id"])
        self.assertIn("원 질문: 질문", self.ask.render_text(child))

    def test_cli_has_ask_command(self):
        from mailkb import cli
        self.assertTrue(hasattr(cli, "cmd_ask"))

    # ── 웹 진입점(/ask · 검색 버튼) ──
    def _answer(self, state="확인됨", q="질문"):
        mid = self.mid["양자화 방식 결정"]
        return self._run([
            json.dumps({"action": "search", "queries": ["양자화"]}),
            json.dumps({"action": "read", "ids": [mid]}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": state, "answer": "per-channel 로 확정됐습니다.",
                        "claims": [{"text": "확정 근거", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ], q=q)

    def test_nav_home_is_analysis(self):
        # 첫 메뉴 = '분석'(href=/) — 위치명(홈) 대신 기능명. /ask* 도 같은 밑줄
        self.assertIn('<a href="/">분석</a>', web._NAV)
        self.assertNotIn('href="/ask"', web._NAV)
        self.assertNotIn(">홈</a>", web._NAV)
        js = web._APP_JS
        self.assertIn('if (path.indexOf("/ask") === 0) return "/"', js)
        active = web._nav_html("/")
        self.assertIn('<a href="/" class="active">분석</a>', active)

    def test_ask_two_pane_layout(self):
        # 좌: 대화 목록(ChatGPT 사이드바) · 우: 대화록 + 하단 입력
        self._answer(q="지난 질문")
        right = web.render_ask(self.store, self.cfg, {})
        self.assertIn("class='chatbar'", right)        # 우측 하단 고정 입력
        self.assertIn("method='post'", right)
        self.assertIn("action='/ask/jobs'", right)
        self.assertNotIn("mlist", right)               # 대화 목록은 우측에 없다
        left = web.render_ask_list(self.store)
        self.assertIn("지난 질문", left)                # 좌측 목록에서 다시 연다
        self.assertIn("/ask?id=", left)
        self.assertIn("신규 분석", left)                # ＋ 신규 분석

    def test_basis_of_survives_scoped_and_followup_keys(self):
        # 캐시 키는 v2:질문@기준선[~범위][#부모]. 질문과 범위 둘 다 '@'를 품을 수
        # 있어서(인물 범위가 이메일 주소) split('@')[-1] 로는 못 뽑는다 —
        # 인물 브리핑의 낡음이 늘 0으로 삼켜지던 버그.
        b = self.ask.basis_of
        self.assertEqual(b("v2:양자화 최종 결정?@250"), 250)
        self.assertEqual(b("v2:손실은?@250#7"), 250)
        self.assertEqual(b("v2:김민수 브리핑@250~minsu.kim@nurisoft.co.kr"), 250)
        self.assertEqual(b("v2:추가@250~minsu.kim@nurisoft.co.kr#7"), 250)
        self.assertEqual(b("v2:주소 kim@99~x 문의@250~a@b.c"), 250)  # 질문 속 '@'
        self.assertIsNone(b(""))
        self.assertIsNone(b("망가진키"))

    def test_conversation_list_shows_staleness(self):
        # 목록에서 석 달 전 '확인됨'과 오늘 '확인됨'이 똑같아 보이면 낡은 결론을
        # 그대로 믿게 된다. 답 이후 들어온 메일 수를 행에 붙인다.
        self._answer(q="낡을 질문")
        cfg = Config(home=self.home, my_addresses=[ME])
        fresh = web.render_ask_list(self.store, cfg=cfg)
        self.assertNotIn("askstale", fresh)          # 새 메일 0 → 줄이지 않는다
        self.store.ingest([
            _rec(f"new{i}", self.KIM, [ME], f"새 메일 {i}",
                 f"2026-07-2{i}T09:00:00", body="새 내용") for i in range(1, 4)])
        convs = self.ask.conversations(self.store)
        self.assertEqual(convs[0]["stale"], 3)
        listed = web.render_ask_list(self.store, cfg=cfg)
        self.assertIn("askstale", listed)
        self.assertIn("이후 3통", listed)

    def test_ask_sidebar_footer_shows_basis_and_backend(self):
        # 질문하기 전에 알아야 할 두 가지: 어느 시점까지 반영됐나 / 어느 AI 를 쓰나.
        # 대화가 없을 때도 붙어야 한다(빈 화면이 그 정보가 제일 필요한 자리).
        cfg = Config(home=self.home, my_addresses=[ME], ai_ask_backend="sonnet")
        empty = web.render_ask_list(self.store, cfg=cfg)
        self.assertIn("askbasis", empty)
        self.assertIn(f"메일 {len(self.mid)}통", empty)
        self.assertIn("동기화 ", empty)                 # setUp 의 ingest 가 남긴 시각
        self.assertIn("새 분석 · <a href='/settings'", empty)
        self.assertIn(">sonnet</a>", empty)
        # 기록이 아예 없는 DB(구버전에서 올라온 경우)는 지어내지 않는다
        self.store.db.execute(
            "DELETE FROM sync_state WHERE key='last_sync_checked_at'")
        self.assertIn("동기화 기록 없음",
                      web.render_ask_list(self.store, cfg=cfg))
        self._answer(q="지난 질문")
        listed = web.render_ask_list(self.store, cfg=cfg)
        self.assertIn("askbasis", listed)               # 목록이 있어도 유지
        self.assertLess(listed.index("mlist"), listed.index("askbasis"))  # 목록 뒤
        # cfg 없이 부르면(구 호출부) footer 를 만들지 않는다 — 조용히 생략
        self.assertNotIn("askbasis", web.render_ask_list(self.store))

    def test_ask_sidebar_footer_says_syncing_while_job_runs(self):
        # 동기화 중에 옛 시각을 그대로 보이면 '이 시점까지 반영됨'이 거짓이 된다
        cfg = Config(home=self.home, my_addresses=[ME])
        try:
            with web._sync_lock:
                web._sync_job.update(running=True, msg="수집 중…", n=0)
            out = web.render_ask_list(self.store, cfg=cfg)
            self.assertIn("동기화 중", out)
            self.assertNotIn("동기화 기록 없음", out)
        finally:
            with web._sync_lock:
                web._sync_job.update(running=False, msg="", n=0)

    def test_ask_sidebar_footer_avoids_full_body_scan(self):
        # footer 때문에 stats() 를 부르면 SUM(LENGTH(new_content)) 로 본문을 전부
        # 훑는다. 화면을 그릴 때마다 낼 비용이 아니다.
        cfg = Config(home=self.home, my_addresses=[ME])
        with mock.patch.object(type(self.store), "stats") as st:
            web.render_ask_list(self.store, cfg=cfg)
        st.assert_not_called()
        info = self.store.basis_info()
        self.assertEqual(info["messages"], len(self.mid))
        self.assertIn("checked_at", info)

    def test_sync_run_timestamp_is_separate_from_watermark(self):
        # last_sync 는 수집한 메일의 sent_on 워터마크라, 새 메일이 0통이면 안 움직인다.
        # 그걸 '마지막 동기화 시각'으로 보여주면 거짓말이 된다 — 실행 시각은 따로 기록.
        before = self.store.basis_info()["checked_at"]
        self.assertTrue(before)                          # setUp 의 ingest 로 기록됨
        watermark = self.store.last_sync()
        self.store.ingest([])                            # 신규 0통 동기화
        after = self.store.basis_info()["checked_at"]
        self.assertEqual(self.store.last_sync(), watermark)  # 워터마크는 그대로
        self.assertGreaterEqual(after, before)               # 실행 시각은 전진
        self.assertNotEqual(after, watermark)                # 둘은 다른 값

    def test_failed_sync_does_not_advance_run_timestamp(self):
        before = self.store.basis_info()["checked_at"]

        def boom():
            raise RuntimeError("수집 실패")
            yield                                        # pragma: no cover

        with self.assertRaises(RuntimeError):
            self.store.ingest(boom())
        self.assertEqual(self.store.basis_info()["checked_at"], before)

    def test_ask_routes_to_right_pane_with_history_on_left(self):
        # 메뉴 클릭(app.js paneFor)과 새로고침(서버 route)이 같은 패널을 써야 한다
        title, inner, code, pane = web.route(
            self.store, self.cfg, "/ask", {}, "2026-07-14")
        self.assertEqual((code, pane), (200, "right"))
        _, _, _, pane2 = web.route(self.store, self.cfg, "/ask/status", {},
                                   "2026-07-14")
        self.assertEqual(pane2, "right")
        js = web._APP_JS
        self.assertIn('inject("right", html, null)', js)     # 폴링도 우측을 갱신
        self.assertIn('/ask?id=" + askId', js)               # 이력 선택 표시

    def test_ask_cache_hit_renders_card_without_job(self):
        res = self._answer()
        out = web.render_ask(self.store, self.cfg, {"q": ["질문"]})
        self.assertNotIn("data-ask-running", out)      # 잡 없이 즉시
        self.assertIn("askbadge ok", out)              # 상태 배지(확인됨)
        self.assertIn("per-channel 로 확정됐습니다.", out)
        self.assertIn("「per-channel 로 확정합니다」".replace("「", "").replace("」", ""),
                      out.replace("&quot;", ""))       # 인용은 CSS 따옴표로 감쌈
        tid = res["claims"][0]["thread_id"]
        self.assertIn(f"/thread/{tid}?focus={res['claims'][0]['mid']}", out)  # 근거 링크

    def test_ask_card_has_followup_form(self):
        res = self._answer()
        out = web.render_ask(self.store, self.cfg, {"q": ["질문"]})
        self.assertIn("class='chatbar'", out)          # 하단 고정 입력이 이어 묻기
        self.assertIn("이어서 묻기", out)               # placeholder
        self.assertIn(f"name='follow' value='{res['id']}'", out)  # 이 대화에 붙는다

    def test_ask_by_id_shows_stored_answer_and_staleness(self):
        res = self._answer()
        self.store.ingest([_rec("new1", self.KIM, [ME], "이후 메일",
                                "2026-07-13T09:00:00", body="새 내용")])
        out = web.render_ask(self.store, self.cfg, {"id": [str(res["id"])]})
        self.assertIn("class='chat'", out)             # 저장된 대화를 대화록으로
        self.assertIn("이후 새 메일", out)              # 그 뒤 들어온 메일 수(낡음 표시)
        self.assertIn("per-channel 로 확정됐습니다.", out)

    def test_ask_states_use_semantic_badges(self):
        for state, cls in (("확인됨", "ok"), ("상충함", "warn"), ("근거 부족", "thin")):
            body = web._ask_answer_body({"question": "q", "state": state,
                                         "answer": "a", "claims": [], "conflicts": [],
                                         "leads": [], "scope": {}})
            self.assertIn(f"askbadge {cls}", body)

    def test_legacy_cached_answer_is_not_shown_as_verified(self):
        body = web._ask_answer_body({
            "question": "q", "state": "확인됨", "answer": "과거 답",
            "claims": [], "conflicts": [], "leads": [], "scope": {}, "cached": True,
        })
        self.assertIn("askbadge thin", body)
        self.assertIn("검증 전 답변", body)
        self.assertNotIn(">확인됨</span>", body)

    def _body_with_one_claim(self, scope):
        return web._ask_answer_body({
            "question": "q", "state": "확인됨", "answer": "답",
            "claims": [{"text": "사실", "mid": 1, "quote": "인용", "thread_id": 1,
                        "sender": "kim", "sent_on": "2026-07-10T09:00"}],
            "conflicts": [], "leads": [], "scope": scope})

    def test_unverified_answer_says_verification_did_not_run(self):
        # 의미 검증을 못 한 답에서 그 줄을 빼버리면 '해당 없음' 으로 읽힌다 —
        # 어떤 보증까지 받았는지(인용 대조만) 명시해야 판단 재료가 된다.
        body = self._body_with_one_claim({"semantic_checked": False})
        self.assertIn("의미 검증 안 됨", body)
        self.assertIn("인용 대조만 통과", body)
        self.assertNotIn("의미 검증 완료", body)

    def test_verified_answer_does_not_look_unverified(self):
        body = self._body_with_one_claim({"semantic_checked": True})
        self.assertIn("주장-인용 의미 검증 완료", body)
        self.assertNotIn("의미 검증 안 됨", body)

    def test_no_evidence_answer_omits_verification_line(self):
        # 근거가 0건이면 검증할 대상 자체가 없다 — 없는 줄로 소음 만들지 않는다
        body = web._ask_answer_body({
            "question": "q", "state": "근거 부족", "answer": "없음",
            "claims": [], "conflicts": [], "leads": [],
            "scope": {"semantic_checked": False}})
        self.assertNotIn("의미 검증", body)

    def test_ask_insufficient_labels_partial_facts(self):
        body = web._ask_answer_body({
            "question": "q", "state": "근거 부족", "answer": "확인되지 않습니다.",
            "claims": [{"text": "주변 사실", "mid": 1, "quote": "인용", "thread_id": 2,
                        "sender": "kim", "sent_on": "2026-07-10T09:00", "subject": "s"}],
            "conflicts": [],
            "leads": [{"thread_id": 3, "subject": "관련 스레드", "why": "여기 있을 수 있음"}],
            "scope": {}})
        self.assertIn("확인한 것", body)                # '근거'가 아니라 '확인한 것'
        self.assertIn("여기부터 보면 됩니다", body)
        self.assertIn("관련 스레드", body)

    def test_ask_conflict_marks_latest(self):
        body = web._ask_answer_body({
            "question": "q", "state": "상충함", "answer": "변경됨",
            "claims": [], "leads": [], "scope": {},
            "conflicts": [
                {"label": "나중", "value": "5월 8일", "mid": 2, "quote": "b",
                 "thread_id": 1, "sender": "kim", "sent_on": "2026-04-21T09:00"},
                {"label": "먼저", "value": "4월 17일", "mid": 1, "quote": "a",
                 "thread_id": 1, "sender": "kim", "sent_on": "2026-04-03T09:00"}]})
        self.assertIn("부딪히는 근거", body)
        self.assertEqual(body.count("class='askside"), 2)   # 좌우 두 장
        self.assertIn("class='askside win'", body)          # 최신 쪽만 강조
        self.assertLess(body.index("4월 17일"), body.index("5월 8일"))  # 시간순
        self.assertEqual(body.count("최신 근거"), 1)

    def test_ask_buttons_do_not_paint_search_wait_into_left(self):
        # aibtn 스타일의 /ask 링크(＋신규 분석·브리핑)는 우측 패널행 —
        # AI검색 대기화면("AI가 찾고 있어요")은 /search?ai=1 링크에만 그린다
        js = web._APP_JS
        self.assertIn('href.slice(0, 8) === "/search?"', js)
        self.assertIn('href.indexOf("ai=1")', js)

    def test_ask_list_fragment_route_feeds_left_pane(self):
        # 폴링 완료 후 좌측 갱신은 /ask/list — /ask?frag=1 은 우측(대화록)이라 못 쓴다
        self._answer(q="목록 질문")
        title, inner, code, pane = web.route(
            self.store, self.cfg, "/ask/list", {}, "2026-07-14")
        self.assertEqual((code, pane), (200, "left"))
        self.assertIn("신규 분석", inner)
        self.assertIn("목록 질문", inner)
        js = web._APP_JS
        self.assertIn('load("/ask/list", "left", false)', js)
        self.assertNotIn('load("/ask", "left"', js)

    def test_ask_menu_click_fills_left_with_history(self):
        # 메뉴 클릭(SPA)은 우측만 갱신 — /ask 를 우측에 열 때 좌측에 대화 이력이
        # 없으면 함께 채운다(F5 는 서버 _panes 가 채움). 이미 목록이면 유지.
        js = web._APP_JS
        self.assertIn('!left.querySelector(".asklisthd")', js)
        self.assertIn('fin.pathname.indexOf("/ask") === 0', js)
        self.assertIn('noteLeft("/ask")', js)   # 표시 최신화의 좌측 되돌림 방지

    def test_ask_polling_bootstrap_watches_right_pane(self):
        # 조사 대기화면은 우측(대화록) — F5 복원 시에도 우측을 후킹해야 폴링이 붙는다
        self.assertNotIn("hookAskPolling(left)", web._APP_JS)

    def test_ask_result_without_id_still_rendered(self):
        # 캐시 기록 실패로 결과에 id 가 없어도 방금 답은 보여준다(랜딩으로 증발 금지)
        try:
            with web._ask_lock:
                web._ask_job.update(
                    running=False, stage="done", question="q", error="",
                    result={"question": "q", "state": "확인됨", "answer": "귀한 답",
                            "claims": [], "conflicts": [], "leads": [], "scope": {}})
            inner, running = web.render_ask_status(self.store, self.cfg)
            self.assertFalse(running)
            self.assertIn("귀한 답", inner)
            self.assertNotIn("무엇이 궁금하세요", inner)   # 랜딩 아님
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    result=None, error="")

    def test_ask_thread_offers_fresh_reinvestigate(self):
        # 저장된 대화록엔 POST '다시 조사' — 질문·fresh 를 URL 에 노출하지 않는다
        self._answer(q="재조사 질문")
        out = web.render_ask(self.store, self.cfg, {"q": ["재조사 질문"]})
        self.assertIn("다시 조사", out)
        self.assertIn("name='fresh' value='1'", out)
        self.assertIn("action='/ask/jobs'", out)
        self.assertNotIn("/ask?fresh", out)
        waiting = web.render_ask_thread(
            self.store, self.cfg, {"turns": [], "latest_id": None}, pending="새 질문")
        self.assertNotIn("다시 조사", waiting)

    def test_ask_input_rejects_empty_question(self):
        # 빈 질문 제출로 대화록이 랜딩으로 바뀌지 않게 — 브라우저 단 required
        self.assertIn("required", web._ask_input(None))

    def test_ask_list_rows_offer_delete(self):
        # 행 hover ✕ — 앵커는 .mrow 유지(j/k·선택 표시), 삭제는 별도 POST 폼
        self._answer(q="지울 질문")
        left = web.render_ask_list(self.store)
        self.assertIn("class='askconv'", left)
        self.assertIn("action='/ask/delete'", left)
        self.assertIn("class='mrow read'", left)
        self.assertIn("대화 삭제", left)

    def test_ask_delete_removes_whole_conversation(self):
        res = self._answer(q="뿌리 질문")
        mid = self.mid["양자화 방식 결정"]
        follow = self._run([                       # 이어 묻기 — 정독 승계로 즉답
            json.dumps({"action": "answer", "why": "승계로 충분"}),
            json.dumps({"state": "확인됨", "answer": "추가 답",
                        "claims": [{"text": "재확인", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ], q="추가 질문", parent_id=res["id"])
        # 아무 턴 id 로도 대화 전체가 잡힌다
        for member in (res["id"], follow["id"]):
            self.assertEqual(sorted(self.ask.conversation_ids(self.store, member)),
                             sorted([res["id"], follow["id"]]))
        loc = web.perform_action(self.store, self.cfg, "/ask/delete",
                                 {"id": [str(res["id"])]})
        self.assertIn("/ask/list?msg=", loc)       # 삭제 후 좌측 목록으로
        self.assertEqual(self.ask.conversations(self.store), [])
        self.assertIsNone(self.store.ask_by_id(res["id"]))
        self.assertIsNone(self.store.ask_by_id(follow["id"]))

    def test_ask_delete_rejects_bad_id(self):
        loc = web.perform_action(self.store, self.cfg, "/ask/delete",
                                 {"id": ["abc"]})
        self.assertIn("/ask/list?msg=", loc)       # 크래시 없이 안내만

    def test_ask_delete_js_wiring(self):
        js = web._APP_JS
        self.assertIn('path === "/ask/list"', js)  # 삭제 후 목록은 좌측 패널로
        self.assertIn("askdel", js)
        self.assertIn("window.confirm", js)        # 문답 전체 삭제 — 한 번 확인
        # 열려 있던 대화를 지우면 우측 대화록도 비운다
        self.assertIn('load("/ask", "right", false)', js)

    def test_ask_status_running_then_result(self):
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, question="질문",
                                    stage="조사 2라운드 — 검색 1회 · 정독 3통",
                                    result=None, error="")
            inner, running = web.render_ask_status(self.store, self.cfg)
            self.assertTrue(running)
            self.assertIn("data-ask-running", inner)
            self.assertIn("조사 2라운드", inner)          # 엔진 진행 문구 노출
            self.assertIn("ask-stage", inner)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    result=None, error="")

    def test_ask_status_rejects_another_job_token(self):
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, token="current-job", question="질문",
                                    stage="조사 중", result=None, error="")
            inner, running = web.render_ask_status(
                self.store, self.cfg, token="old-job")
            self.assertFalse(running)
            self.assertIn("찾지 못했습니다", inner)
            self.assertNotIn("질문", inner)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, token="", stage="", question="",
                                    result=None, error="")

    def test_ask_job_system_exit_finishes_as_error(self):
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, stage="조사 준비 중…", question="질문",
                                    parent=None, person="", result=None, error="")
            with mock.patch.object(self.ask, "ask",
                                   side_effect=SystemExit("AI 백엔드 설정 없음")):
                web._run_ask_job(self.cfg, "질문", None)
            with web._ask_lock:
                job = dict(web._ask_job)
            self.assertFalse(job["running"])
            self.assertEqual(job["stage"], "error")
            self.assertIn("AI 백엔드 설정 없음", job["error"])
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    parent=None, person="", result=None, error="")

    def test_ask_wait_screen_shows_elapsed_not_estimate(self):
        # AI CLI 는 호출당 수십 초 — 경과가 없으면 멈춘 것처럼 보인다. 다만
        # **예상 시간은 싣지 않는다**: 콜 수·모델·프롬프트 크기에 따라 배 단위로
        # 갈려 맞출 수 없고, 경과·수신 줄·무수신 경고가 실제 데이터로 같은 일을
        # 한다. 틀린 추정은 신뢰만 깎는다(2026-07-29 사용자 지적).
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, question="질문", stage="조사 중…",
                                    result=None, error="")
            inner, _ = web.render_ask_status(self.store, self.cfg)
            self.assertIn("ask-elapsed", inner)
            self.assertIn("초 경과", inner)
            self.assertNotIn("보통", inner)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="", error="")
        js = web._APP_JS
        self.assertIn("function jobElapsed", js)
        self.assertIn("#ask-elapsed", js)
        self.assertIn("#wk-elapsed", js)

    def test_ask_wait_screen_streams_recv_model_preview_and_cancel(self):
        # 2a·2b·2c·2d — 수신 줄(단계·수신량·실모델)·작성 중 초안(검증 전)·중지
        # 버튼이 대기 화면에 함께 나온다. 값은 잡 상태를 아는 status 렌더만 채운다.
        try:
            with web._ask_lock:
                web._ask_job.update(
                    running=True, question="질문", stage="조사 중…", token="tk1",
                    result=None, error="", phase="writing", recv=2345,
                    model="claude-real-9", retry="",
                    tail='{"state": "확인됨", "answer": "납기가 5월 8일로',
                    last_ev=time.time())
            inner, running = web.render_ask_status(self.store, self.cfg)
            self.assertTrue(running)
            self.assertIn("class='waitcard'", inner)         # 카드형 대기 화면
            self.assertIn("id='ask-live'", inner)
            self.assertIn("작성 중 · 수신 2.3KB", inner)
            # 모델은 live 줄이 아니라 전용 배지 — 잡 끝까지 유지된다
            self.assertIn("id='ask-model'>claude-real-9<", inner)
            self.assertNotIn("· 모델 claude-real-9", inner)
            self.assertIn("id='ask-preview'", inner)
            self.assertIn("작성 중 초안(검증 전)", inner)
            self.assertIn("납기가 5월 8일로", inner)
            self.assertIn("action='/ask/cancel'", inner)
            self.assertIn("name='job' value='tk1'", inner)
            self.assertIn("중지", inner)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="", token="",
                                    result=None, error="", phase="", recv=0,
                                    model="", retry="", tail="", last_ev=0.0)

    def test_ask_wait_screen_without_stream_events_stays_plain(self):
        # claude 외 백엔드는 이벤트가 없다 — 수신 줄·초안 슬롯을 비워 두면
        # CSS(:empty)가 숨긴다. 관측 안 되는 걸 아는 척하지 않는다.
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, question="질문", stage="조사 중…",
                                    result=None, error="", phase="", recv=0,
                                    model="", retry="", tail="", last_ev=0.0)
            inner, _ = web.render_ask_status(self.store, self.cfg)
            self.assertIn("id='ask-live'></p>", inner)
            self.assertIn("id='ask-preview'></blockquote>", inner)
            self.assertIn("id='ask-model'></span>", inner)   # 빈 배지 — :empty 숨김
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    result=None, error="")

    def test_ask_cancelled_stage_shows_notice_not_fallback(self):
        # 중지는 실패가 아니다 — 검색 폴백·오류 배너 대신 안내 + 새 질문 입력
        try:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="cancelled",
                                    question="질문", result=None, error="")
            inner, running = web.render_ask_status(self.store, self.cfg)
            self.assertFalse(running)
            self.assertIn("조사를 중지했습니다", inner)
            self.assertNotIn("일반 검색 결과", inner)
            self.assertIn("chatbar", inner)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    result=None, error="")

    def test_run_ask_job_cancelled_sets_stage(self):
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, stage="조사 준비 중…",
                                    question="질문", parent=None, person="",
                                    result=None, error="")
            with mock.patch.object(self.ask, "ask",
                                   side_effect=review.AICancelled("중지")):
                web._run_ask_job(self.cfg, "질문", None)
            with web._ask_lock:
                job = dict(web._ask_job)
            self.assertFalse(job["running"])
            self.assertEqual(job["stage"], "cancelled")
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    parent=None, person="", result=None, error="")

    def test_analyze_mail_seeds_thread_without_scope_lock(self):
        # 메일 분석 = 그 메일+스레드에서 출발하되 범위를 잠그지 않는다 —
        # 대상 메일이 가리키는 다른 스레드를 라운드 루프가 따라가야 한다.
        mid = self.mid["양자화 방식 결정"]
        seen = {}

        def fake_ai(cmd, prompt, **kw):
            if "다음 한 걸음만 정하라" in prompt:
                seen["step"] = prompt
                return json.dumps({"action": "answer", "why": "충분"})
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": [], "answer_supported": False})
            seen["answer"] = prompt
            return json.dumps({"state": "근거 부족", "answer": "확인 불가.",
                               "claims": [], "conflicts": [], "leads": []})

        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            res = self.ask.analyze_mail(self.store, self.cfg, mid,
                                        today="2026-07-14")
        # 질문 자동 생성 + 대상 메일 표식
        self.assertIn(f"메일 #{mid}", res["question"])
        self.assertEqual(res["mail"]["mid"], mid)
        # seed: 그 메일이 훑기 목록에 미리 올라 있다(검색 없이)
        self.assertIn(f"#{mid} ", seen["step"])
        # 답변 형식 가이드(의미·맥락·액션·관련 스레드) 주입
        self.assertIn("메일 하나의 분석", seen["answer"])
        # 영구 저장 — 캐시 키에 mail 스코프, 같은 상태에서 즉시 재열람
        hit = self.ask.cached(self.store, res["question"], None, f"mail:{mid}")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["cached"])

    def test_analyze_mail_includes_noise_sender_target(self):
        # 노이즈 발신자의 메일을 분석하면 정작 그 메일이 seed 에서 빠져
        # '훑기 0통' 빈 분석이 되던 버그(2026-07-30 실측)의 회귀 가드 —
        # 사용자가 직접 지목한 조사에는 노이즈 필터를 적용하지 않는다.
        cfg = Config(home=Path(self.tmp.name), my_addresses=[ME],
                     my_names=["김도현"], internal_domains=["corp.example"],
                     ignore_senders=["noreply"], ai_default="internal",
                     ai_backends={"internal": {"cmd": ["echo"]}})
        self.store.ingest([_rec("nz1", "noreply@corp.example", [ME],
                                "빌드 알림", "2026-07-11T09:00:00",
                                body="빌드 1234 실패했습니다. 로그 확인 바랍니다.")])
        nmid = self.store.db.execute(
            "SELECT id FROM messages WHERE message_id='<nz1@t>'").fetchone()[0]
        seen = {}

        def fake(cmd, prompt, **kw):
            if "다음 한 걸음만 정하라" in prompt:
                seen["step"] = prompt
                return json.dumps({"action": "answer"})
            if "검증기" in prompt:
                return json.dumps({"supported": [], "answer_supported": False})
            return json.dumps({"state": "근거 부족", "answer": "x",
                               "claims": [], "conflicts": [], "leads": []})

        with mock.patch.object(review, "ai_run", side_effect=fake):
            self.ask.analyze_mail(self.store, cfg, nmid, use_cache=False)
        self.assertIn(f"#{nmid} [", seen["step"])   # 대상이 훑기 목록에 있다

    def test_analyze_mail_target_survives_seed_cap(self):
        # 스레드가 SEED_MAX 를 넘어도 대상 메일은 seed 맨 앞이라 절단되지 않는다
        mid = self.mid["양자화 방식 결정"]
        m = self.store.message(str(mid))
        with mock.patch.object(self.ask, "SEED_MAX", 1), \
             mock.patch.object(self.ask, "_seed", wraps=self.ask._seed) as sp, \
             mock.patch.object(review, "ai_run", side_effect=lambda *a, **k:
                               json.dumps({"action": "answer"})
                               if "한 걸음" in a[1] else
                               json.dumps({"state": "근거 부족", "answer": "x",
                                           "claims": [], "conflicts": [],
                                           "leads": []})
                               if "근거로" in a[1] else
                               json.dumps({"supported": [],
                                           "answer_supported": False})):
            self.ask.analyze_mail(self.store, self.cfg, mid, use_cache=False)
        self.assertEqual(sp.call_args.args[2][0], mid)   # seed 첫 원소 = 대상

    def test_analyze_mail_missing_id_raises(self):
        with self.assertRaises(review.AIError):
            self.ask.analyze_mail(self.store, self.cfg, 99999)

    def test_thread_view_offers_mail_analysis_controls(self):
        # 분석 없는 메일 = '분석' 버튼(POST mid). 저장된 분석이 있으면
        # '분석 보기 · 경과' 링크 + '다시' — 인물 요약과 같은 낡음 문법.
        mid = self.mid["양자화 방식 결정"]
        tid = self.store.message(str(mid))["thread_id"]
        out = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("class='mh-ai'", out)
        self.assertIn(f"name='mid' value='{mid}'", out)
        self.assertIn(">분석</button>", out)
        # 저장된 분석을 심으면 링크로 바뀐다
        key = self.ask.cache_key(self.store, f"메일 #{mid} (양자화 방식 결정) — "
                                 "이 메일의 의미와 필요한 액션",
                                 None, f"mail:{mid}")
        self.store.ask_put(key, "질문", json.dumps({"state": "확인됨"}), "internal")
        out2 = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("분석 보기", out2)
        self.assertIn("다시</button>", out2)
        rid = self.store.ask_get(key)["id"]
        self.assertIn(f"/ask?id={rid}", out2)
        # 분석 후 **이 스레드에** 새 메일이 오면 낡음 표시 — 무관한 스레드
        # 메일로는 숫자가 늘지 않는다(전역으로 세면 낡음 표시가 거짓말이 된다)
        self.store.ingest([_rec("other", self.KIM, [ME], "무관한 새 건",
                                "2026-07-12T08:00:00", body="다른 이야기입니다.")])
        self.assertNotIn("이후 새 메일",
                         web.render_thread(self.store, self.cfg, tid))
        self.store.ingest([_rec("newer", self.KIM, [ME], "RE: 양자화 방식 결정",
                                "2026-07-12T09:00:00", body="추가 내용입니다.",
                                reply_to="q1")])
        out3 = web.render_thread(self.store, self.cfg, tid)
        self.assertIn("이후 새 메일 1통", out3)

    def test_submit_ask_job_mid_builds_question_and_scope(self):
        mid = self.mid["양자화 방식 결정"]
        with mock.patch.object(web, "_start_ask", return_value="tok9") as start:
            loc = web._submit_ask_job(self.store, self.cfg,
                                      {"mid": [str(mid)]})
        self.assertIn("job=tok9", loc)
        args = start.call_args.args
        self.assertIn(f"메일 #{mid}", args[1])       # 자동 질문
        self.assertEqual(args[4], mid)               # mail_id 전달
        # 없는 메일은 시작 없이 안내
        loc2 = web._submit_ask_job(self.store, self.cfg, {"mid": ["99999"]})
        self.assertIn("찾을 수 없습니다", urllib_unquote(loc2))

    def test_submit_fresh_pierces_engine_cache(self):
        # '다시'(fresh=1)는 웹 사전 캐시 검사만이 아니라 **엔진 캐시까지** 뚫어야
        # 한다 — basis 불변이면 같은 키로 즉시 히트해 재분석이 무동작이 된다.
        mid = self.mid["양자화 방식 결정"]
        with mock.patch.object(web, "_start_ask", return_value="t1") as start:
            web._submit_ask_job(self.store, self.cfg,
                                {"mid": [str(mid)], "fresh": ["1"]})
            self.assertFalse(start.call_args.kwargs["use_cache"])
            web._submit_ask_job(self.store, self.cfg,
                                {"q": ["질문"], "fresh": ["1"]})
            self.assertFalse(start.call_args.kwargs["use_cache"])
        # 잡 스레드가 그 플래그를 엔진 인자로 넘긴다
        from mailkb import ask as ask_engine
        with mock.patch.object(ask_engine, "analyze_mail",
                               return_value={"answer": "a", "id": 1}) as am, \
             mock.patch.object(web, "_ask_job",
                               dict(web._new_job(question="", parent=None,
                                                 person="", token="", mail=None,
                                                 result=None), running=True)):
            web._run_ask_job(self.cfg, "q", None, "", threading.Event(),
                             mail_id=mid, use_cache=False)
            self.assertFalse(am.call_args.kwargs["use_cache"])

    def test_ask_fallback_keeps_mail_context(self):
        # 메일 분석 실패의 재시도는 mid 로 재제출돼야 스레드 머리글의 '분석
        # 보기'와 같은 이력으로 이어진다. 검색어도 자동 질문이 아니라 메일 제목.
        mid = self.mid["양자화 방식 결정"]
        from mailkb import ask as ask_engine
        q = ask_engine.mail_question(mid, "양자화 방식 결정")
        out = web._ask_fallback(self.store, self.cfg, q, "백엔드 없음",
                                mail_id=mid)
        banner = out.split("aifail")[1].split("</div>")[0]
        self.assertIn(f"name='mid' value='{mid}'", banner)
        self.assertIn("name='fresh'", banner)
        self.assertNotIn("name='q'", banner)             # 질문 재제출 아님
        self.assertNotIn(web.esc(q), banner)             # 자동 질문 비노출
        # 검색어는 자동 질문("메일 #N …")이 아니라 그 메일 제목
        self.assertIn("value='양자화 방식 결정'", out)
        # 일반 질문 폴백은 기존 그대로 q 재제출
        out2 = web._ask_fallback(self.store, self.cfg, "일반 질문", "err")
        self.assertIn("name='q'", out2)

    def test_ask_records_real_model_in_scope(self):
        # 백엔드 '이름'(opus 등)은 움직이는 별칭 — scope 에는 스트리밍 init 이
        # 알려준 실모델 ID 를 남긴다. 원래 on_event 콜백도 그대로 불려야 한다.
        mid = self.mid["양자화 방식 결정"]
        replies = iter([
            json.dumps({"action": "read", "ids": [mid], "why": "유망"}),
            json.dumps({"action": "answer"}),
            json.dumps({"state": "확인됨", "answer": "확정됐습니다.",
                        "claims": [{"text": "확정", "mid": mid,
                                    "quote": "per-channel 로 확정합니다"}]}),
        ])

        def fake_ai(cmd, prompt, **kwargs):
            cb = kwargs.get("on_event")
            if cb:
                cb({"ev": "model", "model": "claude-testmodel-9"})
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"], "answer_supported": True})
            return next(replies)

        events = []
        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            res = self.ask.ask(self.store, self.cfg, "질문",
                               today="2026-07-14", on_event=events.append)
        self.assertEqual(res["scope"]["model"], "claude-testmodel-9")
        self.assertTrue(any(e.get("ev") == "model" for e in events))

    def test_ask_busy_with_other_question_says_so(self):
        # 단일 슬롯 — 새 POST 는 현재 잡 화면으로 합류하지만, 거기엔 남의 질문이
        # 떠 있다. 왜 그런지와 '내 질문은 안 걸렸다' 를 말하지 않으면 조용한 유실이다.
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, question="먼저 물은 것",
                                    token="opaque-job", stage="조사 중…",
                                    result=None, error="")
            loc = web._submit_ask_job(self.store, self.cfg, {"q": ["나중 질문"]})
            self.assertTrue(loc.startswith("/ask/status?job=opaque-job&msg="))
            self.assertNotIn("나중 질문", loc)          # 내 질문은 주소에 안 남는다
            msg = urllib_unquote(loc.split("msg=", 1)[1])
            self.assertIn("먼저 물은 것", msg)          # 남의 질문이 보이는 이유
            self.assertIn("시작되지 않았습니다", msg)     # 내 질문은 안 걸렸다
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="",
                                    token="", error="")

    def test_ask_busy_without_token_still_says_so(self):
        # 토큰 없는 낡은 잡이어도 안내는 남는다 — 합류할 화면만 홈으로 바뀐다
        try:
            with web._ask_lock:
                web._ask_job.update(running=True, question="먼저 물은 것",
                                    token="", stage="조사 중…", result=None, error="")
            loc = web._submit_ask_job(self.store, self.cfg, {"q": ["나중 질문"]})
            self.assertTrue(loc.startswith("/?msg="))
            self.assertIn("시작되지 않았습니다", urllib_unquote(loc))
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="", error="")

    def test_ask_post_starts_opaque_job_without_question_url(self):
        with mock.patch.object(web, "_start_ask", return_value="opaque-job") as start:
            loc = web.perform_action(
                self.store, self.cfg, "/ask/jobs", {"q": ["민감한 프로젝트 질문"]})
        self.assertEqual(loc, "/ask/status?job=opaque-job")
        self.assertNotIn("민감한", loc)
        start.assert_called_once_with(self.cfg, "민감한 프로젝트 질문", None,
                                      "", None, use_cache=True)

    def test_ask_get_miss_never_starts_job(self):
        with mock.patch.object(web, "_start_ask") as start:
            out = web.render_ask(self.store, self.cfg, {"q": ["새로운 질문"]})
        start.assert_not_called()
        self.assertIn("무엇이 궁금하세요", out)

    def test_ask_error_falls_back_to_search(self):
        try:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="error", question="양자화",
                                    error="백엔드 없음", result=None)
            inner, running = web.render_ask_status(self.store, self.cfg)
            self.assertFalse(running)
            self.assertIn("질문에 답할 수 없습니다", inner)
            self.assertIn("백엔드 없음", inner)
            self.assertIn("<h1>검색</h1>", inner)        # 일반 검색 결과로 폴백(#10)
        finally:
            with web._ask_lock:
                web._ask_job.update(running=False, stage="", question="", error="")

    def test_search_page_has_no_ask_button(self):
        # 분석(질문) 진입은 상단 메뉴로 일원화 — 검색 화면의 '질문하기'는 제거
        out = web.render_search(self.store, self.cfg, {"q": ["양자화"]}, "2026-07-14")
        self.assertNotIn("질문하기", out)
        self.assertNotIn("/ask?q=", out)
        self.assertIn("AI로 다시 찾기", out)          # AI 검색 진입점은 유지

    # ── 인물 브리핑(같은 엔진, 범위만 고정) ──
    def test_person_ids_cover_both_directions(self):
        self.store.ingest([
            _rec("p1", self.KIM, [ME], "그가 보낸 것", "2026-07-12T09:00:00",
                 body="확인 부탁드립니다."),
            _rec("p2", ME, [self.KIM], "내가 보낸 것", "2026-07-12T10:00:00",
                 body="검토했습니다."),
            _rec("p3", "other@corp.example", [ME], "무관한 사람",
                 "2026-07-12T11:00:00", body="다른 건입니다."),
        ])
        ids = self.ask.person_message_ids(self.store, self.cfg, self.KIM,
                                          months=3, today="2026-07-14")
        subs = {r["subject"] for r in self.store.messages_by_ids(ids)}
        self.assertIn("그가 보낸 것", subs)         # 그가 나에게
        self.assertIn("내가 보낸 것", subs)         # 내가 그에게
        self.assertNotIn("무관한 사람", subs)
        # 창 밖은 제외
        old = self.ask.person_message_ids(self.store, self.cfg, self.KIM,
                                          months=1, today="2026-09-30")
        self.assertEqual(old, [])

    def test_brief_seeds_scope_without_searching(self):
        self.store.ingest([
            _rec("b1", self.KIM, [ME], "드라이버 검토 요청", "2026-07-12T09:00:00",
                 body="스펙 검토 의견 부탁드립니다."),
        ])
        mid = self.store.db.execute(
            "SELECT id FROM messages WHERE message_id='<b1@t>'").fetchone()[0]
        prompts = []

        def spy(cmd, prompt, **kw):
            prompts.append(prompt)
            if "보수적인 근거 검증기" in prompt:
                return json.dumps({"supported": ["c0"], "answer_supported": True})
            non_verify = sum("보수적인 근거 검증기" not in p for p in prompts)
            return [json.dumps({"action": "read", "ids": [mid]}),
                    json.dumps({"action": "answer"}),
                    json.dumps({"state": "확인됨", "answer": "검토 요청이 대기 중.",
                                "claims": [{"text": "요청 접수", "mid": mid,
                                            "quote": "스펙 검토 의견 부탁드립니다"}]})
                    ][non_verify - 1]

        with mock.patch.object(review, "ai_run", side_effect=spy):
            res = self.ask.brief(self.store, self.cfg, self.KIM, name="김",
                                 today="2026-07-14")
        self.assertEqual(res["state"], "확인됨")
        self.assertEqual(res["person"]["addr"], self.KIM)
        self.assertEqual(res["scope"]["queries"], [])   # 검색 없이 범위 seed 로 시작
        self.assertEqual(res["scope"]["read"], 1)
        self.assertIn("드라이버 검토 요청", prompts[0])   # 1라운드부터 후보가 보인다
        self.assertTrue(any("인물 브리핑" in p for p in prompts))  # 답변 형식 지시 주입

    def test_brief_without_history_raises(self):
        with self.assertRaises(review.AIError):
            self.ask.brief(self.store, self.cfg, "nobody@corp.example",
                           today="2026-07-14")

    def test_brief_cache_scoped_per_person(self):
        for addr in (self.KIM, "lee@corp.example"):
            self.store.ingest([_rec(f"c{addr[:3]}", addr, [ME], "건",
                                    "2026-07-12T09:00:00", body="내용입니다.")])
        replies = [json.dumps({"action": "answer"}),
                   json.dumps({"state": "근거 부족", "answer": "kim 브리핑"})]
        with mock.patch.object(review, "ai_run", side_effect=replies):
            a = self.ask.brief(self.store, self.cfg, self.KIM, today="2026-07-14")
        replies2 = [json.dumps({"action": "answer"}),
                    json.dumps({"state": "근거 부족", "answer": "lee 브리핑"})]
        with mock.patch.object(review, "ai_run", side_effect=replies2):
            b = self.ask.brief(self.store, self.cfg, "lee@corp.example",
                               today="2026-07-14")
        self.assertEqual(a["answer"], "kim 브리핑")
        self.assertNotEqual(a["answer"], b["answer"])   # 사람별로 캐시 분리

    def test_dossier_has_briefing_button(self):
        # 버튼 이름은 상단 '분석' 메뉴와 일관 — 결과물 명칭(브리핑)은 대화록에 유지
        self.store.ingest([_rec("d1", self.KIM, [ME], "건", "2026-07-12T09:00:00",
                                body="내용")])
        out = web.render_dossier(self.store, self.cfg, self.KIM)
        self.assertIn("action='/ask/jobs'", out)
        self.assertIn(f"name='person' value='{self.KIM}'", out)
        self.assertNotIn("/ask?person=", out)
        self.assertIn("대화 분석", out)          # 이 사람과의 왕래를 분석
        self.assertNotIn("이 사람 분석", out)
        self.assertNotIn("이 사람 브리핑", out)
        # 요약 갱신 버튼도 함께 — 하는 일이 다르므로 라벨로 구분한다
        self.assertIn("action='/people/dossier'", out)
        self.assertIn("AI 요약 만들기", out)     # 캐시 없음 → '만들기'
        self.assertIn("요약 = 이 사람 카드", out)

    def test_ask_briefing_turn_shows_person_header(self):
        # 인물 브리핑은 질문 말풍선 자리에 '대상·기간'을 대신 표기한다
        turn = web._ask_turn({"question": "q", "state": "확인됨", "answer": "a",
                              "claims": [], "conflicts": [], "leads": [], "scope": {},
                              "person": {"addr": self.KIM, "name": "김민수",
                                         "months": 3}})
        self.assertIn("김민수 브리핑", turn)
        self.assertIn("최근 3개월", turn)
        self.assertIn("class='chatq'", turn)            # 내 말풍선 자리

    def test_appjs_ask_polling_hook(self):
        js = web._APP_JS
        self.assertIn("function hookAskPolling", js)
        self.assertIn("/ask/status", js)
        self.assertIn("data-ask-running", js)
        self.assertIn('patchJob(tmp, right, "ask")', js)


class TestWeekly(unittest.TestCase):
    """주간 보고 — 관여도 수집(결정론) · 인용 검증 · graceful.

    재료는 내 발신 + 나 지목 + 직접 수신(To·소수). 종결 여부는 점수에 안 들어간다.
    """

    KIM = "kim@corp.example"
    LEE = "lee@corp.example"

    def setUp(self):
        from mailkb import weekly, web
        self.weekly = weekly
        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = Config(home=self.home, my_addresses=[ME], my_names=["김도현"],
                          internal_domains=["corp.example"],
                          ignore_senders=["noreply"],
                          ai_default="internal",
                          ai_backends={"internal": {"cmd": ["echo"]}})
        self.store = Store(self.home / "t.sqlite", [ME], ["김도현"], noise=self.cfg)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _rx(self, mid, sender, subject, when, body, to=None, cc=None):
        return MailRecord(
            message_id=f"<{mid}@t>", subject=subject,
            sender_name=sender.split("@")[0], sender_addr=sender,
            to=to if to is not None else [ME], cc=cc or [],
            sent_on=when, body_text=body,
            in_reply_to="", references=[])

    def test_bounds_window(self):
        self.assertEqual(self.weekly.WINDOW_WEEKS, 1)
        s, e = self.weekly.bounds(2, "2026-07-14")
        self.assertEqual((s, e), ("2026-07-01", "2026-07-14"))   # 종료일 포함 14일
        s1, _ = self.weekly.bounds(1, "2026-07-14")
        self.assertEqual(s1, "2026-07-08")
        s0, _ = self.weekly.bounds(0, "2026-07-14")              # 0 이하는 1주로
        self.assertEqual(s0, "2026-07-08")

    def test_direct_to_me_excludes_cc_and_broadcast(self):
        f, me = self.weekly._direct_to_me, {ME}
        self.assertTrue(f(f"{ME};a@x", me, 4))
        self.assertFalse(f("a@x;b@x", me, 4))                    # To 에 내가 없음
        self.assertFalse(f(f"{ME};a@x;b@x;c@x;d@x", me, 4))      # 수신인 과다(대량)

    def test_collect_scores_and_filters(self):
        self.store.ingest([
            # ① 나를 지목 + 직접 수신 + 내 발신에 답장 → 최상위
            self._rx("a1", self.KIM, "타이밍 클로저", "2026-07-10T09:00:00",
                     "김도현님 확인 부탁드립니다."),
            self._rx("a2", ME, "RE: 타이밍 클로저", "2026-07-10T10:00:00",
                     "재합성 돌렸습니다.", to=[self.KIM]),
            self._rx("a3", self.KIM, "RE: 타이밍 클로저", "2026-07-11T09:00:00",
                     "결과 공유 감사합니다."),
            # ② 대량 공지(수신인 5명·내 발신 없음) — 직접 수신이 아니라 '미관여'
            self._rx("b1", self.LEE, "전사 공지", "2026-07-10T09:00:00", "안내드립니다.",
                     to=[ME, "x@corp.example", "y@corp.example",
                         "z@corp.example", "w@corp.example"]),
            # ③ 노이즈 발신 — 제외
            self._rx("c1", "noreply@corp.example", "자동 알림", "2026-07-10T09:00:00",
                     "김도현 담당자 알림"),
            # ④ 직접 수신만(지목·발신 없음) — 낮은 점수로 포함
            self._rx("d1", self.LEE, "자료 공유", "2026-07-10T09:00:00", "첨부 확인 바랍니다."),
        ])
        items = self.weekly.collect(self.store, self.cfg, "2026-07-01", "2026-07-14")
        subj = [t["subject"] for t in items]
        self.assertNotIn("자동 알림", subj)                       # 노이즈 제외
        self.assertNotIn("전사 공지", subj)                       # 대량 공지 = 미관여
        top = items[0]
        self.assertEqual(top["subject"], "타이밍 클로저")          # 지목·답장이 최상위
        self.assertEqual((top["named"], top["sent"], top["replies"]), (1, 1, 1))
        self.assertGreaterEqual(top["direct"], 1)
        low = next(t for t in items if t["subject"] == "자료 공유")
        self.assertEqual(low["direct"], 1)
        self.assertLess(low["score"], top["score"])               # 지목·답장이 더 높다

    def test_collect_skips_uninvolved_and_hidden(self):
        self.store.ingest([
            self._rx("u1", self.KIM, "참조만 된 건", "2026-07-10T09:00:00", "공유합니다.",
                     to=["x@corp.example"], cc=[ME]),
            self._rx("h1", self.KIM, "숨긴 건", "2026-07-10T09:00:00", "확인 바랍니다."),
        ])
        tid = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<h1@t>'").fetchone()[0]
        self.store.hide_thread(tid, True)
        subj = [t["subject"] for t in self.weekly.collect(
            self.store, self.cfg, "2026-07-01", "2026-07-14")]
        self.assertNotIn("숨긴 건", subj)                          # 숨김 제외
        self.assertNotIn("참조만 된 건", subj)                     # CC 만 = 미관여

    def test_states_are_code_computed(self):
        self.store.ingest([
            self._rx("s1", self.KIM, "회신 필요건", "2026-07-13T09:00:00",
                     "검토 후 회신 부탁드립니다."),
            self._rx("s2", ME, "내가 마지막", "2026-07-13T09:00:00",
                     "자료 보냅니다.", to=[self.KIM]),
        ])
        det = self.weekly.deterministic(self.store, self.cfg, weeks=2,
                                        today="2026-07-14")
        by = {t["subject"]: t for t in det["items"]}
        self.assertEqual(by["회신 필요건"]["state"], "내 차례")
        self.assertIn(by["내가 마지막"]["state"], ("상대 대기", "막힘"))

    def test_candidate_stage_does_not_cut_at_twenty_threads(self):
        self.store.ingest([
            self._rx(f"wide{i}", self.KIM, f"관여 사안 {i}",
                     "2026-07-10T09:00:00", f"사안 {i} 검토 부탁드립니다.")
            for i in range(25)
        ])
        det = self.weekly.deterministic(
            self.store, self.cfg, weeks=2, today="2026-07-14")
        self.assertEqual(len(det["items"]), 25)
        self.assertEqual(len(self.weekly._candidate_items(det["items"])), 25)

    def _seed_topic(self):
        self.store.ingest([
            self._rx("t1", self.KIM, "드라이버 API 검토", "2026-07-10T09:00:00",
                     "스펙 v0.9 검토 의견 부탁드립니다."),
            self._rx("t2", ME, "RE: 드라이버 API 검토", "2026-07-11T09:00:00",
                     "인터럽트 처리 부분만 수정 요청했습니다.", to=[self.KIM]),
        ])
        return self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<t1@t>'").fetchone()[0]

    def _ai_layer(self, replies, weeks=2, today="2026-07-14", progress=None):
        det = self.weekly.deterministic(self.store, self.cfg, weeks, today)
        answers = iter(replies)

        def fake_ai(cmd, prompt, **kwargs):
            # 각 테스트는 토픽 편집 결과에 집중한다. 새 원문 카드 단계는 빈 응답으로
            # 두어 결정론 원문 fallback을 쓰고, 마지막 의미 검증은 명시적으로 통과시키며,
            # 해석 층은 기본 비활성(빈 배열) — 해석 테스트만 각본으로 채운다.
            if "기간 내 원문 메일에서 주간 보고 후보 사실을 추출한다" in prompt:
                return json.dumps({"threads": []})
            if "주간 보고의 각 서술이 붙어 있는 원문 인용으로" in prompt:
                return json.dumps({
                    "supported": [f"r{i}" for i in range(30)],
                    "summary_supported": True,
                })
            if "그래서 무엇을 주목해야 하나" in prompt:
                try:
                    return next(answers)      # 해석 각본이 있으면 사용
                except StopIteration:
                    return json.dumps({"insights": []})
            return next(answers)

        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            ai = self.weekly.run_ai_layer(self.store, self.cfg, det,
                                          progress=progress)
        return det, ai

    def test_ai_layer_verifies_quotes_and_sent_only_for_mine(self):
        tid = self._seed_topic()
        mids = {r["message_id"]: r["id"] for r in self.store.db.execute(
            "SELECT id, message_id FROM messages WHERE thread_id=?", (tid,))}
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                # ① 상대 인용 — 통과
                {"text": "검토 요청이 왔다", "tid": tid,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"},
                # ② mine=true 인데 상대 문장을 인용 — 탈락해야 함
                {"text": "내가 검토를 요청했다", "tid": tid, "mine": True,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"},
                # ③ mine=true + 내 발신 인용 — 통과
                {"text": "인터럽트 수정을 요청했다", "tid": tid, "mine": True,
                 "quote": "인터럽트 처리 부분만 수정 요청했습니다"},
                # ④ quote는 맞지만 다른 mid를 명시 — 메시지 출처가 틀려 탈락
                {"text": "출처를 바꿔 단 주장", "tid": tid, "mid": mids["<t2@t>"],
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"},
                # ⑤ 없는 인용 — 탈락
                {"text": "지어낸 주장", "tid": tid, "quote": "이런 문장은 본문에 없다"},
            ], "issues": [], "next": []}),
            json.dumps({"summary": "드라이버 API 검토가 진행 중이다.",
                        "order": ["드라이버 API"]}),
            json.dumps({"missed": []}),
        ]
        det, ai = self._ai_layer(replies)
        self.assertIsNotNone(ai)
        texts = [p["text"] for p in ai["topics"][0]["progress"]]
        self.assertIn("검토 요청이 왔다", texts)
        self.assertIn("인터럽트 수정을 요청했다", texts)
        self.assertNotIn("내가 검토를 요청했다", texts)   # 남의 문장으로 내 성과 금지
        self.assertNotIn("출처를 바꿔 단 주장", texts)     # 다른 메시지 quote로 갈아끼우기 금지
        self.assertNotIn("지어낸 주장", texts)            # 인용 검증 실패
        self.assertEqual(ai["dropped"], 3)
        # summary 는 항목 리스트가 정본 — 모델이 문자열로 줘도 받아 준다
        self.assertEqual(ai["summary"], ["드라이버 API 검토가 진행 중이다."])
        # 원문카드1+묶기1+서술1+총평1+의미검증1+해석1
        self.assertEqual(ai["calls"], 6)

    def test_completeness_check_recovers_missed_thread(self):
        tid = self._seed_topic()
        self.store.ingest([self._rx(   # 토픽에 안 들어간 별개 사안
            "m1", self.LEE, "인증 기한 통보", "2026-07-12T09:00:00",
            "7월 30일까지 제출 부탁드립니다.")])
        other = self.store.db.execute(
            "SELECT thread_id FROM messages WHERE message_id='<m1@t>'").fetchone()[0]
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "검토 요청이 왔다", "tid": tid,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"}], "issues": [], "next": []}),
            json.dumps({"summary": "x", "order": []}),
            json.dumps({"missed": [{"tid": other, "why": "기한이 임박한 제출 건"}]}),
        ]
        _, ai = self._ai_layer(replies)
        self.assertEqual(ai["calls"], 7)                  # 누락 점검·해석 콜 포함
        self.assertEqual(ai["missed"][0]["tid"], other)
        self.assertIn("7월 30일", ai["missed"][0]["why"])  # 모델의 자유 이유가 아닌 원문 사실
        self.assertIn("짚어둘 것", self.weekly.render(
            self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14"), ai))

    def test_progress_reports_call_count_and_input_size(self):
        # 콜 하나가 수 분까지 가는 동안 대기 화면이 멈춘 것처럼 보이지 않아야
        # 한다 — 각 단계 진행 문구에 '콜 n/N · 입력 KB' 를 싣는다. 경과초는
        # 클라이언트(#wk-elapsed)가 세므로 여기선 정적 정보만 사양으로 고정.
        tid = self._seed_topic()
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "검토 요청이 왔다", "tid": tid,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": "x", "order": []}),
        ]
        msgs = []
        _, ai = self._ai_layer(replies, progress=msgs.append)
        self.assertIsNotNone(ai)
        for label in ("원문 근거 1/1 묶음 읽는 중", "토픽 묶는 중",
                      "토픽 1/1 서술 중", "총평 정리 중",
                      "보고 근거 검증 중", "해석 정리 중"):
            self.assertTrue(any(m.startswith(label + " · 콜 ") for m in msgs),
                            (label, msgs))
        # 송신·수신이 같은 자를 쓴다(review.fmt_bytes) — 한 카드에서 위 줄만
        # KB, 아래 줄만 '자' 로 갈려 보이던 것을 맞춘 것(2026-07-29).
        self.assertTrue(re.search(
            r"콜 \d+/%d · 송신 [\d.]+(?:B|KB)" % self.weekly.MAX_AI_CALLS,
            "\n".join(msgs)), msgs)

    def test_ai_layer_captures_model_for_render_footer(self):
        # 2b 축소 — 보고 푸터에 실행 모델의 실측 ID(별칭 아님)를 남긴다.
        # 몇 달 뒤 같은 별칭이 다른 모델을 가리켜도 기록은 진실을 유지한다.
        tid = self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        answers = iter([
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "검토 요청이 왔다", "tid": tid,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": "요약", "order": []}),
        ])

        def fake_ai(cmd, prompt, **kwargs):
            cb = kwargs.get("on_event")
            if cb:
                cb({"ev": "model", "model": "claude-testmodel-9"})
            if "기간 내 원문 메일에서 주간 보고 후보 사실을 추출한다" in prompt:
                return json.dumps({"threads": []})
            if "주간 보고의 각 서술이 붙어 있는 원문 인용으로" in prompt:
                return json.dumps({"supported": [f"r{i}" for i in range(30)],
                                   "summary_supported": True})
            if "그래서 무엇을 주목해야 하나" in prompt:
                return json.dumps({"insights": []})
            return next(answers)

        events = []
        with mock.patch.object(review, "ai_run", side_effect=fake_ai):
            ai = self.weekly.run_ai_layer(self.store, self.cfg, det,
                                          on_event=events.append)
        self.assertEqual(ai["model"], "claude-testmodel-9")
        self.assertTrue(any(e.get("ev") == "model" for e in events))
        self.assertIn("모델 claude-testmodel-9", self.weekly.render(det, ai))
        # 이벤트가 없으면(비스트리밍 백엔드) 푸터에 모델 줄 자체가 없다
        self.assertNotIn("모델 ", self.weekly.render(det, dict(ai, model="")))

    def test_generate_does_not_swallow_cancelled(self):
        # 취소가 graceful 삼킴(AIError) 경로를 타면 뼈대 보고가 '완료'로 저장돼
        # 버린다 — AICancelled 는 그대로 올라와 잡이 cancelled 로 끝나야 한다.
        self._seed_topic()
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AICancelled("중지")):
            with self.assertRaises(review.AICancelled):
                self.weekly.generate(self.store, self.cfg, weeks=2, ai=True,
                                     today="2026-07-14",
                                     cancel=threading.Event())

    def test_insight_layer_labeled_and_optional(self):
        # 해석 층 — 검증 통과 사실만 재료로 쓰는 유일한 비인용 단계. 사실 나열로
        # 수렴하는 인용 강제의 보완이되, '참고 의견' 라벨로 사실 층과 구분한다.
        tid = self._seed_topic()
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "검토 요청이 왔다", "tid": tid,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": "x", "order": []}),
            # 후보가 전부 토픽에 들어가 누락 점검 콜은 생략 — 다음 각본이 해석
            json.dumps({"insights": [
                {"topic": "드라이버 API",
                 "text": "리뷰 병목이 다음 주 일정의 변수로 보인다"},
                {"text": ""},                    # 빈 텍스트 → 버린다
                "문자열",                         # 형식 이탈 → 버린다
            ]}),
        ]
        det, ai = self._ai_layer(replies)
        self.assertEqual(len(ai["insights"]), 1)
        self.assertEqual(ai["insights"][0]["topic"], "드라이버 API")
        md = self.weekly.render(det, ai)
        self.assertIn("## 해석", md)
        self.assertIn("참고 의견", md)            # 비인용 층임을 라벨로 명시
        self.assertIn("리뷰 병목", md)
        # 해석 응답이 없으면(빈 배열 폴백) 섹션 자체가 없다 — 억지로 안 채운다
        det2, ai2 = self._ai_layer(replies[:3])
        self.assertEqual(ai2["insights"], [])
        self.assertNotIn("## 해석", self.weekly.render(det2, ai2))

    def test_ai_layer_rejects_unknown_thread_ids(self):
        tid = self._seed_topic()
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "다른 스레드 근거", "tid": tid + 999,
                 "quote": "스펙 v0.9 검토 의견 부탁드립니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": "x", "order": []}),
            json.dumps({"missed": []}),
        ]
        _, ai = self._ai_layer(replies)
        self.assertIsNone(ai)          # 살아남은 서술이 없으면 AI 계층 없음

    def test_ai_layer_graceful_on_backend_missing(self):
        self._seed_topic()
        cfg = Config(home=self.home, my_addresses=[ME], ai_summary_backend="ghost")
        det = self.weekly.deterministic(self.store, cfg, 2, "2026-07-14")
        self.assertIsNone(self.weekly.run_ai_layer(self.store, cfg, det))

    def test_ai_layer_graceful_on_call_failure(self):
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        with mock.patch.object(review, "ai_run",
                               side_effect=review.AIError("boom")):
            self.assertIsNone(self.weekly.run_ai_layer(self.store, self.cfg, det))

    def test_tone_samples_pick_my_report_style_mail_only(self):
        # 문체 표본 선정은 결정론이다 — 고르는 일까지 AI 에게 맡기면 '무엇이
        # 중요한가'가 표본 쪽으로 샌다.
        long_body = "지난 주 진행 사항을 정리해 드립니다. " * 30
        self.store.ingest([
            self._rx("s1", ME, "주간 업무 보고 (7월 1주)", "2026-07-01T09:00:00",
                     long_body, to=[self.KIM]),
            self._rx("s2", ME, "주간 업무 보고 (7월 2주)", "2026-07-08T09:00:00",
                     long_body, to=[self.KIM]),
            self._rx("s3", ME, "주간 업무 보고 (7월 3주)", "2026-07-09T09:00:00",
                     long_body, to=[self.KIM]),
            self._rx("s4", ME, "회의실 예약 부탁", "2026-07-10T09:00:00",
                     long_body, to=[self.KIM]),           # 보고성 제목 아님
            self._rx("s5", ME, "월간 보고", "2026-07-11T09:00:00",
                     "짧습니다.", to=[self.KIM]),          # 문체가 안 드러난다
            self._rx("s6", self.KIM, "주간 보고 회신", "2026-07-12T09:00:00",
                     long_body),                          # 내가 쓴 글이 아니다
        ])
        got = self.weekly.tone_samples(self.store)
        self.assertEqual(got.count("--- ") - got.count("--- 이전 대화"),
                         self.weekly.TONE_SAMPLES)
        self.assertIn("7월 3주", got)          # 최신순
        self.assertIn("7월 2주", got)
        self.assertNotIn("7월 1주", got)
        for skipped in ("회의실 예약", "짧습니다", "주간 보고 회신"):
            self.assertNotIn(skipped, got, msg=skipped)
        self.assertEqual(self.weekly.tone_samples(Store(self.home / "e.sqlite",
                                                        [ME])), "(없음)")

    def test_tone_samples_measure_what_i_wrote_not_the_quote_chain(self):
        # 보존 인용을 안 떼면 '내가 쓴 400자'가 아니라 '인용이 붙어 400자를 넘긴
        # 메일'을 고르게 된다 — 문체 표본이 남의 문체가 된다(2026-08-01 실증).
        long_quote = "강미래입니다. 원가율 관련해 말씀드립니다. " * 40
        self.store.ingest([self._rx(
            "tq", ME, "주간 현황 보고", "2026-07-09T09:00:00",
            "네, 확인했습니다.\n\n"
            "보낸 사람: 강미래 <mirae@corp.example>\n"
            "보낸 날짜: 2026년 7월 8일 수요일 오전 9:00\n"
            "받는 사람: 김도현 <me@corp.example>\n"
            "제목: 원가율\n\n" + long_quote, to=[self.KIM])])
        row = self.store.db.execute(
            "SELECT new_content FROM messages WHERE message_id='<tq@t>'").fetchone()
        self.assertIn("--- 이전 대화 (인용 보존) ---", row["new_content"])  # 전제
        got = self.weekly.tone_samples(self.store)
        self.assertNotIn("원가율", got)          # 남이 쓴 글은 표본이 아니다
        self.assertEqual(got, "(없음)")          # 내가 쓴 분량은 26자뿐

    def test_tone_sample_enters_only_the_overview_with_its_label(self):
        # previous_report 와 같은 3중 제약 — 라벨·규칙문·총평 검증. 사실이 새면
        # 검증을 통과한 서술만 요약한다는 계약이 깨진다.
        # 표본은 보고 기간 밖에서 온다 — 기간 안이면 근거 메일로도 정당하게
        # 들어가 버려서, 표본 경로만 검사할 수가 없다
        self.store.ingest([self._rx(
            "tone", ME, "주간 업무 보고", "2026-06-20T09:00:00",
            "TONE_POISON_이 문장의 사실은 이번 기간 것이 아니다. " * 20,
            to=[self.KIM])])
        tid = self._seed_topic()
        prompts = []
        answers = iter([
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [{
                "text": "인터럽트 수정을 요청했다", "tid": tid, "mine": True,
                "quote": "인터럽트 처리 부분만 수정 요청했습니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": ["한 줄"], "order": []}),
            json.dumps({"missed": []}),
        ])

        def spy(cmd, prompt, **kwargs):
            prompts.append(prompt)
            if "기간 내 원문 메일에서" in prompt:
                return json.dumps({"threads": []})
            if "주간 보고의 각 서술이" in prompt:
                return json.dumps({"supported": [f"r{i}" for i in range(30)],
                                   "summary_supported": True})
            if "그래서 무엇을 주목해야 하나" in prompt:
                return json.dumps({"insights": []})
            return next(answers)

        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        with mock.patch.object(review, "ai_run", side_effect=spy):
            self.weekly.run_ai_layer(self.store, self.cfg, det)
        carrying = [p for p in prompts if "TONE_POISON" in p]
        self.assertEqual(len(carrying), 1)                 # 머리글 프롬프트에만
        self.assertIn("머리글(executive summary)", carrying[0])
        self.assertIn("사실·상태 근거 사용 금지", carrying[0])

    def test_board_facts_are_the_only_basis_for_period_comparison(self):
        # '지난주 대비'를 쓸 근거는 코드가 센 값뿐이다. 이전 보고 **문장**은
        # 계속 머리글에 넣지 않는다(사실 오염 위험이 그대로다).
        det = {"start": "2026-07-08", "end": "2026-07-14", "weeks": 1,
               "stat": {"threads": 3, "sent": 9, "named": 2, "direct": 4},
               "items": [{"state": "내 차례"}, {"state": "막힘"}, {"state": "막힘"}],
               "calendar": [{"due": None}],
               "last_round": {"start": "2026-07-01", "end": "2026-07-07",
                              "kept": [{}], "open": [{}, {}]}}
        got = self.weekly.board_facts(det)
        self.assertIn("스레드 3건 · 내 발신 9통", got)
        self.assertIn("내 차례 1건 · 막힘 2건", got)
        self.assertIn("확정 기한 1건", got)
        # '처리함'으로 접은 것을 합쳐 세므로 "내 후속 있음"이라 단정하지 않는다
        self.assertIn("내 약속 3건 중 후속 또는 처리함 1 · 아직 없음 2", got)
        # 지난 차수가 없으면 그 줄이 없다 — 없는 비교를 쓰게 두지 않는다
        self.assertNotIn("지난 차수", self.weekly.board_facts(dict(det, last_round=None)))

    def test_executive_summary_lists_items_and_falls_back(self):
        # 사용자 확정: 일간 1건 · 주간 5건. AI 가 없으면 비운다(결정론 흉내는
        # 읽는 값이 없다).
        det = {"start": "2026-07-08", "end": "2026-07-14", "weeks": 1, "items": [],
               "stat": {"threads": 0, "sent": 0, "named": 0, "direct": 0}}
        many = [f"항목 {i}" for i in range(self.weekly.EXEC_TOP + 2)]
        md = self.weekly.render(det, {"summary": many, "calls": 1})
        self.assertIn("- 항목 0", md)
        self.assertNotIn("항목 5", md)                     # EXEC_TOP 상한
        # AI 를 안 돌리면 절 자체가 없다
        self.assertNotIn("Executive Summary", self.weekly.render(det, None))
        # 돌렸는데 비면 이유를 말한다 — 일간과 같은 문구 표를 쓴다
        self.assertIn("- 특이사항 없음", self.weekly.render(
            det, {"summary": [], "summary_state": "none", "calls": 1}))
        self.assertIn("받지 못했습니다", self.weekly.render(
            det, {"summary": [], "summary_state": "failed", "calls": 1}))
        self.assertIn("근거 검증", self.weekly.render(
            det, {"summary": [], "summary_state": "unverified", "calls": 1}))
        # 모델이 문자열·글머리표로 돌려줘도 받아 준다
        self.assertEqual(self.weekly._exec_lines("- 가\n* 나\n\n"), ["가", "나"])
        self.assertEqual(self.weekly._exec_lines(None), [])
        # 문자열이 아닌 것이 섞이면 버린다 — repr 이 새면 화면에 그대로 찍힌다
        self.assertEqual(self.weekly._exec_lines([None, ["중첩"], True, "다"]), ["다"])
        self.assertEqual(self.weekly._exec_lines({"summary": "x"}), [])

    def test_last_round_section_states_facts_not_verdicts(self):
        # 사용자 요청: "지난 차수에 약속한 것이 지켜졌는지 확인해서 알려주는 것".
        # 단 '안 지켰다'고 단정하지 않는다 — 다른 스레드나 메일 밖에서 처리했을
        # 수 있고, 예전 '지금 할 일'이 그 추측으로 신뢰를 잃었다.
        from datetime import date as _date
        det = {"start": "2026-07-26", "end": "2026-08-01", "weeks": 1,
               "items": [], "stat": {"threads": 0, "sent": 0, "named": 0, "direct": 0},
               "last_round": {
                   "start": "2026-07-19", "end": "2026-07-25",
                   "kept": [{"thread_id": 7, "subject": "정적분석 처리 방안"}],
                   "open": [{"thread_id": 84, "subject": "nightly 회귀 크래시",
                             "key": "a1b2c3d4e5f6a1b2c3d4",
                             "due": _date(2026, 7, 25), "quote": "패치 올리겠습니다."}]}}
        md = self.weekly.render(det, None)
        self.assertIn("## 지난 차수 점검 (2026-07-19 ~ 2026-07-25)", md)
        self.assertIn("- 후속 있음 (1건): [#7] 정적분석 처리 방안", md)
        self.assertIn("- 아직 내 후속 없음 (1건)", md)
        self.assertIn("[#84] nightly 회귀 크래시 · 기한 07/25 지남", md)
        self.assertIn("「패치 올리겠습니다.」", md)
        for verdict in ("안 지켰", "미이행", "약속 위반", "불이행"):
            self.assertNotIn(verdict, md, msg=verdict)

    def test_last_round_section_absent_without_a_previous_round(self):
        # 첫 보고이거나 그 차수에 약속이 없었으면 절 자체를 내지 않는다
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        self.assertIsNone(det["last_round"])         # 금고에 지난 보고가 없다
        self.assertNotIn("지난 차수 점검", self.weekly.render(det, None))
        empty = dict(det, last_round={"start": "2026-06-24", "end": "2026-06-30",
                                      "kept": [], "open": []})
        self.assertNotIn("지난 차수 점검", self.weekly.render(empty, None))

    def test_last_round_window_comes_from_the_previous_report_file(self):
        # 보고서는 시작일을 남기지 않는다 — 직전 파일 stem(종료일)에서 같은
        # weeks 로 되짚어 창을 만든다. 현재 창과 겹치지 않아야 한다.
        (self.cfg.vault / "weekly").mkdir(parents=True, exist_ok=True)
        for stem in ("2026-06-16", "2026-06-30",
                     "2026-06-30 (사본)", "메모"):      # 날짜 아닌 stem 은 건너뛴다
            (self.cfg.vault / "weekly" / f"{stem}.md").write_text("x", encoding="utf-8")
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        self.assertEqual(det["last_round"]["end"], "2026-06-30")
        self.assertEqual(det["last_round"]["start"], "2026-06-17")
        self.assertLess(det["last_round"]["end"], det["start"])

    def test_report_rounds_lists_only_date_named_files(self):
        d = self.cfg.vault / "weekly"; d.mkdir(parents=True, exist_ok=True)
        for stem in ("2026-06-30", "2026-07-07", "메모", "2026-07-07 (사본)"):
            (d / f"{stem}.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.weekly.report_rounds(self.cfg, "2026-07-14"),
                         ["2026-06-30", "2026-07-07"])          # 오래된 순
        self.assertEqual(self.weekly.report_rounds(self.cfg, "2026-07-01"),
                         ["2026-06-30"])                        # before 로 자른다
        self.assertEqual(self.weekly.report_rounds(self.cfg, "2026-01-01"), [])

    def test_board_facts_without_calendar_or_last_round(self):
        det = {"start": "2026-07-08", "end": "2026-07-14", "weeks": 1,
               "stat": {"threads": 0, "sent": 0, "named": 0, "direct": 0},
               "items": [], "calendar": [], "last_round": None}
        got = self.weekly.board_facts(det)
        self.assertNotIn("확정 기한", got)      # 없는 줄은 만들지 않는다
        self.assertNotIn("지난 차수", got)
        self.assertIn("스레드 0건", got)

    def test_tone_sample_is_truncated(self):
        long_body = "지난 주 진행 사항을 정리해 드립니다. " * 200
        self.assertGreater(len(long_body), self.weekly.TONE_CHARS)
        self.store.ingest([self._rx("tt", ME, "주간 업무 보고",
                                    "2026-07-09T09:00:00", long_body, to=[self.KIM])])
        got = self.weekly.tone_samples(self.store)
        self.assertLessEqual(len(got), self.weekly.TONE_CHARS + 40)   # 머리줄 여유

    def test_last_round_window_sits_between_saved_reports(self):
        # 적대 검토(2026-08-01)에서 실측한 결함: 이 차수 창에서 'weeks*7 일 전'으로
        # 되짚으면, 07-25 에 낸 보고를 07-29 에 열 때 두 차수 전을 가리키고 그
        # 사이(07-19~07-22)에 한 약속은 어느 창에도 안 잡혔다. 창은 저장된 보고서
        # 두 개 사이여야 한다.
        d = self.cfg.vault / "weekly"; d.mkdir(parents=True, exist_ok=True)
        for stem in ("2026-07-18", "2026-07-25"):
            (d / f"{stem}.md").write_text("x", encoding="utf-8")
        self._seed_topic()
        lr = self.weekly.deterministic(self.store, self.cfg, 1, "2026-07-29")["last_round"]
        self.assertEqual((lr["start"], lr["end"]), ("2026-07-19", "2026-07-25"))
        # 차수 간격이 창보다 길어도 직전 보고의 기간을 그대로 본다
        (d / "2026-08-01.md").write_text("x", encoding="utf-8")
        lr = self.weekly.deterministic(self.store, self.cfg, 1, "2026-08-10")["last_round"]
        self.assertEqual((lr["start"], lr["end"]), ("2026-07-26", "2026-08-01"))
        # 이 차수 파일이 이미 있어도(재생성) 자기를 지난 차수로 세지 않는다
        lr = self.weekly.deterministic(self.store, self.cfg, 1, "2026-08-01")["last_round"]
        self.assertEqual(lr["end"], "2026-07-25")

    def test_last_round_survives_a_nonsense_date_file(self):
        # 날짜가 아닌 stem 은 건너뛰고, 연산이 넘치는 날짜는 점검을 건너뛴다.
        # 이게 새면 일간 회고까지 같이 죽는다(주간 엔진을 공유한다).
        d = self.cfg.vault / "weekly"; d.mkdir(parents=True, exist_ok=True)
        (d / "0001-01-01.md").write_text("x", encoding="utf-8")
        (d / "메모.md").write_text("x", encoding="utf-8")
        self._seed_topic()
        self.assertIsNone(
            self.weekly.deterministic(self.store, self.cfg, 1, "2026-07-14")["last_round"])
        self.assertIsNotNone(review.deterministic(self.store, self.cfg, "2026-07-14"))

    def test_daily_state_board_does_not_read_the_weekly_vault(self):
        # 일간의 '어제 대비'는 주간 엔진의 items 만 쓴다 — 그 경로까지 금고를
        # 읽으면 하루 두 번 도는 계산에 파일 I/O 가 얹힌다.
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 1, "2026-07-14",
                                        report_extras=False)
        self.assertIsNone(det["last_round"])
        self.assertEqual(det["calendar"], [])      # 보고서 전용 계산은 건너뛴다
        self.assertEqual(det["promise_tids"], set())
        self.assertTrue(det["items"])
        with mock.patch.object(self.weekly, "_last_round") as lr, \
                mock.patch.object(self.weekly, "_calendar") as cal:
            review._state_map(self.store, self.cfg, "2026-07-14")
        lr.assert_not_called()
        cal.assert_not_called()

    def test_weekly_files_ignore_non_date_names(self):
        # 문자열 정렬에서 한글 stem 이 날짜보다 뒤라 '최신 차수'를 차지했다
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        d = cfg.vault / "weekly"; d.mkdir(parents=True)
        for stem in ("2026-07-25", "2026-08-01", "메모", "2026-08-01 (사본)"):
            (d / f"{stem}.md").write_text("보고", encoding="utf-8")
        self.assertEqual(self.web.weekly_files(cfg), ["2026-08-01", "2026-07-25"])
        self.assertIn("<b>2026-08-01</b>", self.web.render_weekly(cfg, {}))

    def test_weekly_walks_to_adjacent_reports(self):
        # 일간은 날짜 산술로 ◀▶ 를 만들지만 주간 파일은 생성한 날만 있어 간격이
        # 불규칙하다 — 목록의 앞뒤 원소를 가리켜야 실제로 이동이 된다.
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        cfg = Config(home=Path(home.name), my_addresses=[ME])
        d = cfg.vault / "weekly"; d.mkdir(parents=True)
        for x in ("2026-07-18", "2026-07-25", "2026-08-01"):
            (d / f"{x}.md").write_text(f"# {x} 주간 보고", encoding="utf-8")
        mid = web.render_weekly(cfg, {"date": ["2026-07-25"]})
        self.assertIn("◀ 2026-07-18", mid)
        self.assertIn("2026-08-01 ▶", mid)
        newest = web.render_weekly(cfg, {})            # 기본은 최신 차수
        self.assertIn("◀ 2026-07-25", newest)
        self.assertNotIn("▶", newest)                  # 더 최신이 없다
        oldest = web.render_weekly(cfg, {"date": ["2026-07-18"]})
        self.assertNotIn("◀", oldest)
        self.assertIn("2026-07-25 ▶", oldest)

    def test_state_board_leads_and_limits_count(self):
        # 2026-08-01 재구성: 토픽 서술 앞에 상태판을 세운다. 읽고 나서 무엇을
        # 해야 할지가 남아야 하는데, 진행 항목이 같은 무게로 나열되면 안 보였다.
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        md = self.weekly.render(det, None)
        self.assertLess(md.index("## 내 차례"), md.index("## 막힘"))
        self.assertLess(md.index("## 내 차례"), md.index("## 막힘"))
        # 개수 제한 — 상위 WEEKLY_TOP 만 본문에, 나머지는 접는다
        many = dict(det, items=[dict(det["items"][0], thread_id=100 + i,
                                     subject=f"건 {i}", score=50 - i)
                                for i in range(self.weekly.WEEKLY_TOP + 3)])
        many["items"] = [dict(t, state="내 차례") for t in many["items"]]
        out = self.weekly.render(many, None)
        self.assertIn(f"## 내 차례 ({self.weekly.WEEKLY_TOP + 3}건)", out)
        self.assertIn("… 외 3건", out)
        self.assertIn("건 0", out)              # 점수 높은 것부터
        self.assertNotIn("건 7", out)

    def test_calendar_keeps_resolved_dates_and_marks_low(self):
        # 기한은 전부 두되 정보성 공지는 표시로 낮춘다(사용자 확정). 날짜가
        # 환산되지 않는 표현은 아예 싣지 않는다 — 틀린 기한을 박느니 비운다.
        from datetime import date as _date
        det = {"start": "2026-07-01", "end": "2026-07-14", "weeks": 2,
               "items": [], "stat": {"threads": 0, "sent": 0, "named": 0, "direct": 0},
               "calendar": [
                   {"due": _date(2026, 7, 10), "thread_id": 5, "subject": "사무용품 마감",
                    "quote": "금요일 마감입니다", "low": True},
                   {"due": _date(2026, 7, 12), "thread_id": 6, "subject": "GDS 제출",
                    "quote": "8/20 까지 제출", "low": False}]}
        md = self.weekly.render(det, None)
        self.assertIn("## 기한 (2건)", md)
        self.assertIn("**07/10** [#5] 사무용품 마감 · 중요도 낮음", md)
        self.assertNotIn("GDS 제출 · 중요도 낮음", md)      # 낮음 표시 없음
        self.assertLess(md.index("07/10"), md.index("07/12"))   # 날짜순

    def test_render_skeleton_without_ai(self):
        self._seed_topic()
        det = self.weekly.deterministic(self.store, self.cfg, 2, "2026-07-14")
        md = self.weekly.render(det, None)
        self.assertIn("주간 보고", md)
        # 도구는 항상 산다(#10) — AI 가 없어도 상태판(내 차례·막힘·기한)은 나온다.
        # 2026-08-01 재구성 전에는 '(AI 계층 없음)' + 전량 나열이었는데, 상태판이
        # 그 역할을 대신하므로 중복 나열을 없앴다.
        self.assertNotIn("Executive Summary", md)   # AI 를 안 돌렸으면 절이 없다
        self.assertIn("## 막힘", md)
        self.assertIn("조사 범위", md)
        self.assertIn("드라이버 API 검토", md)
        self.assertIn("별표(*)는 내 발신 메일에서 인용한", md)
        self.assertNotIn("\\* 표시", md)

    def test_render_with_topics(self):
        tid = self._seed_topic()
        replies = [
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [
                {"text": "인터럽트 수정을 요청했다", "tid": tid, "mine": True,
                 "quote": "인터럽트 처리 부분만 수정 요청했습니다"}],
                "issues": [], "next": []}),
            json.dumps({"summary": "한 줄 총평.", "order": ["드라이버 API"]}),
            json.dumps({"missed": []}),
        ]
        det, ai = self._ai_layer(replies)
        md = self.weekly.render(det, ai)
        self.assertIn("한 줄 총평.", md)
        self.assertIn("## 1. 드라이버 API", md)
        self.assertIn("**진행**", md)
        self.assertIn("「인터럽트 처리 부분만 수정 요청했습니다」", md)
        self.assertIn("AI 6콜", md)                # 조사 범위에 콜 수 노출(해석 포함)

    def test_previous_report_loaded_as_reference(self):
        d = self.cfg.vault / "weekly"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-06-30.md").write_text("# 지난 보고\n- 이전 내용", encoding="utf-8")
        (d / "2026-07-20.md").write_text("# 이후 보고", encoding="utf-8")
        prev = self.weekly.previous_report(self.cfg, "2026-07-01")
        self.assertIn("지난 보고", prev)          # 시작일 이전 것 중 최신
        self.assertNotIn("이후 보고", prev)

    def test_summary_metadata_is_never_a_fact_or_selection_input(self):
        tid = self._seed_topic()
        self.store.db.execute(
            "UPDATE threads SET rolling_summary=? WHERE id=?",
            ("ROLLING_POISON_현재 사실 아님", tid))
        self.store.db.commit()
        daily = self.cfg.vault / "daily"
        daily.mkdir(parents=True, exist_ok=True)
        (daily / "2026-07-10.md").write_text(
            "DAILY_POISON_현재 사실 아님", encoding="utf-8")
        weekly_dir = self.cfg.vault / "weekly"
        weekly_dir.mkdir(parents=True, exist_ok=True)
        (weekly_dir / "2026-06-30.md").write_text(
            "PREVIOUS_REFERENCE_ONLY", encoding="utf-8")

        prompts = []
        answers = iter([
            json.dumps({"threads": []}),
            json.dumps({"topics": [{"name": "드라이버 API", "threads": [tid]}]}),
            json.dumps({"progress": [{
                "text": "인터럽트 수정을 요청했다", "tid": tid, "mine": True,
                "quote": "인터럽트 처리 부분만 수정 요청했습니다",
            }], "issues": [], "next": []}),
            json.dumps({"summary": "한 줄 총평", "order": ["드라이버 API"]}),
            json.dumps({"supported": ["r0"], "summary_supported": True}),
            json.dumps({"insights": []}),
        ])

        def spy(cmd, prompt, **kwargs):
            prompts.append(prompt)
            return next(answers)

        det = self.weekly.deterministic(
            self.store, self.cfg, weeks=2, today="2026-07-14")
        with mock.patch.object(review, "ai_run", side_effect=spy):
            ai = self.weekly.run_ai_layer(self.store, self.cfg, det)
        self.assertIsNotNone(ai)
        joined = "\n".join(prompts)
        self.assertNotIn("ROLLING_POISON", joined)
        self.assertNotIn("DAILY_POISON", joined)
        previous_prompts = [p for p in prompts if "PREVIOUS_REFERENCE_ONLY" in p]
        self.assertEqual(len(previous_prompts), 1)
        self.assertIn("표현 중복 회피 전용", previous_prompts[0])

    def test_write_saves_to_vault_weekly(self):
        det = {"end": "2026-07-14"}
        p = self.weekly.write(self.cfg, det, "# 본문")
        self.assertTrue(p.exists())
        self.assertEqual(p.name, "2026-07-14.md")
        self.assertEqual(p.parent.name, "weekly")

    def test_cli_has_weekly_command(self):
        from mailkb import cli
        self.assertTrue(hasattr(cli, "cmd_weekly"))

    # ── 웹 진입점(기억 › 주간) ──
    def _saved(self, day, body="# 보고\n\n내용"):
        d = self.cfg.vault / "weekly"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{day}.md").write_text(body, encoding="utf-8")

    def test_records_has_weekly_tab(self):
        out = web.render_records(self.store, self.cfg, {"tab": ["weekly"]},
                                 "2026-07-14")
        self.assertIn("주간", out)
        self.assertIn("/records?tab=daily", out)       # 다른 탭으로 이동 가능
        self.assertIn("/records?tab=decisions", out)
        self.assertIn("action='/weekly'", out)         # 생성 버튼(POST)

    def test_weekly_tab_renders_saved_report(self):
        self._saved("2026-07-14", "# 주간 보고\n\n- **진행** 무언가 진행됨")
        out = web.render_weekly(self.cfg, {})
        self.assertIn("진행", out)
        self.assertIn("무언가 진행됨", out)
        self.assertNotIn("저장된 주간 보고가 없습니다", out)

    def test_weekly_tab_empty_state(self):
        out = web.render_weekly(self.cfg, {})
        self.assertIn("저장된 주간 보고가 없습니다", out)
        self.assertIn("보고 만들기", out)
        # 버튼 위 안내도 소요 시간을 약속하지 않는다 — 카드가 '1~3분', 여기가
        # '2~5분' 이라 같은 작업을 두 화면이 다르게 말하고 있었다(2026-07-29).
        self.assertIn(f"AI 최대 {self.weekly.MAX_AI_CALLS}콜", out)
        self.assertNotIn("보통", out)

    def test_weekly_tab_lists_past_reports(self):
        for d in ("2026-07-14", "2026-06-30", "2026-06-16"):
            self._saved(d)
        self.assertEqual(web.weekly_files(self.cfg)[0], "2026-07-14")   # 최신순
        out = web.render_weekly(self.cfg, {})
        self.assertIn("지난 보고", out)
        self.assertIn("tab=weekly&date=2026-06-30", out)
        # 특정 날짜 선택
        one = web.render_weekly(self.cfg, {"date": ["2026-06-16"]})
        self.assertIn("tab=weekly&date=2026-07-14", one)   # 선택분은 목록에서 빠짐

    def test_weekly_status_running_then_done(self):
        try:
            with web._weekly_lock:
                web._weekly_job.update(running=True, stage="토픽 3/5 서술 중…",
                                       date="", error="")
            inner, running = web.render_weekly_status(self.cfg)
            self.assertTrue(running)
            self.assertIn("data-weekly-running", inner)   # 폴링 마커
            self.assertIn("토픽 3/5 서술 중…", inner)      # 엔진 진행 문구 노출
            self.assertIn("wk-stage", inner)
            # 완료 → 그 날짜 보고로
            self._saved("2026-07-14", "# 주간 보고\n\n완료된 내용")
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="done",
                                       date="2026-07-14")
            inner2, running2 = web.render_weekly_status(self.cfg)
            self.assertFalse(running2)
            self.assertIn("완료된 내용", inner2)
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", date="", error="")

    def test_weekly_status_error_shows_notice(self):
        try:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="error",
                                       error="백엔드 없음")
            inner, running = web.render_weekly_status(self.cfg)
            self.assertFalse(running)
            self.assertIn("보고를 만들지 못했습니다", inner)
            self.assertIn("백엔드 없음", inner)
            self.assertIn("보고 만들기", inner)          # 재시도 경로는 남긴다
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", error="")

    def test_weekly_status_running_shows_live_preview_and_cancel(self):
        try:
            with web._weekly_lock:
                web._weekly_job.update(
                    running=True, stage="토픽 1/3 서술 중…", date="", error="",
                    phase="writing", recv=1234, model="claude-real-9", retry="",
                    tail='{"progress": [{"text": "납기가 5월 8일로 변경",',
                    last_ev=time.time())
            inner, running = web.render_weekly_status(self.cfg)
            self.assertTrue(running)
            self.assertIn("class='waitcard'", inner)         # 카드형 대기 화면
            self.assertIn("id='wk-live'", inner)
            self.assertIn("작성 중 · 수신 1.2KB", inner)
            # 모델은 live 줄이 아니라 전용 배지 — 잡 끝까지 유지된다
            self.assertIn("id='wk-model'>claude-real-9<", inner)
            self.assertNotIn("· 모델 claude-real-9", inner)
            self.assertIn("id='wk-preview'", inner)
            self.assertIn("작성 중 초안(검증 전)", inner)
            self.assertIn("납기가 5월 8일로 변경", inner)
            self.assertIn("action='/weekly/cancel'", inner)
            self.assertIn("중지", inner)
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", date="", error="",
                                       phase="", recv=0, model="", retry="",
                                       tail="", last_ev=0.0)

    def test_weekly_status_stall_and_retry_lines(self):
        try:
            with web._weekly_lock:                # 30초 무수신 → 정체 경고 (2d)
                web._weekly_job.update(running=True, stage="s", date="", error="",
                                       phase="thinking", recv=10, model="m",
                                       retry="", tail="",
                                       last_ev=time.time() - 45)
            inner, _ = web.render_weekly_status(self.cfg)
            self.assertIn("초째 무수신", inner)
            with web._weekly_lock:                # 재시도 안내는 정체보다 우선
                web._weekly_job.update(retry="호출 실패 — 재시도 1/2 (2초 뒤)")
            inner, _ = web.render_weekly_status(self.cfg)
            self.assertIn("재시도 1/2", inner)
            self.assertNotIn("무수신", inner)
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", date="", error="",
                                       phase="", recv=0, model="", retry="",
                                       tail="", last_ev=0.0)

    def test_weekly_status_cancelled_branch(self):
        try:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="cancelled",
                                       date="", error="")
            inner, running = web.render_weekly_status(self.cfg)
            self.assertFalse(running)
            self.assertIn("중지했습니다", inner)
            self.assertIn("보고 만들기", inner)      # 재시도 경로는 남긴다
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", date="", error="")

    def test_cancel_routes_wired_and_start_sets_event(self):
        # POST 디스패치에 취소 라우트가 있고, 잡 시작이 Event 를 싣는다 —
        # Event 만 켜면 스트리밍 루프가 0.5초 안에 프로세스를 죽인다.
        import inspect
        src = inspect.getsource(web._Handler.do_POST)
        self.assertIn('"/weekly/cancel"', src)
        self.assertIn('"/ask/cancel"', src)
        try:
            with mock.patch.object(web.threading, "Thread"):
                self.assertTrue(web._start_weekly(self.cfg, 1))
            with web._weekly_lock:
                self.assertIsInstance(web._weekly_job.get("cancel"),
                                      threading.Event)
        finally:
            with web._weekly_lock:
                web._weekly_job.update(running=False, stage="", date="",
                                       error="", cancel=None)

    def test_appjs_weekly_polling_hook(self):
        js = web._APP_JS
        self.assertIn("function hookWeeklyPolling", js)
        self.assertIn("/weekly/status", js)
        self.assertIn("data-weekly-running", js)
        self.assertIn('patchJob(tmp, left, "wk")', js)

    def test_weekly_status_pane_matches_polling(self):
        # POST /weekly 의 303 이 /weekly/status 로 온다 — 클라이언트 paneFor 가
        # 우측이면 대기 화면이 우측에 박히고 폴링(좌측 기준)이 못 봐 멈춘다.
        # 서버 route(left)와 반드시 일치해야 한다.
        self.assertIn('if (path === "/weekly/status") return "left"', web._APP_JS)
        _, _, _, pane = web.route(  # 서버 쪽도 좌측(F5 복원)
            self.store, self.cfg, "/weekly/status", {}, "2026-07-14")
        self.assertEqual(pane, "left")


class TestJobStreamHelpers(unittest.TestCase):
    """웹 잡 스트리밍 표시 헬퍼 — 이벤트 반영 · 수신 한 줄 · 초안 미리보기."""

    def _job(self):
        return {"running": True, "phase": "", "recv": 0, "model": "",
                "retry": "", "tail": "", "failed": "", "last_ev": 0.0}

    def test_stream_event_accumulates_and_resets_per_call(self):
        job = self._job()
        cb = web._job_stream_event(job, threading.Lock())
        cb({"ev": "model", "model": "m1"})
        cb({"ev": "phase", "phase": "thinking"})
        cb({"ev": "delta", "phase": "thinking", "bytes": 7})
        cb({"ev": "phase", "phase": "writing"})
        cb({"ev": "delta", "phase": "writing", "bytes": 5, "text": "부분 "})
        cb({"ev": "delta", "phase": "writing", "bytes": 5, "text": "초안"})
        self.assertEqual(job["model"], "m1")
        self.assertEqual(job["phase"], "writing")
        self.assertEqual(job["recv"], 17)
        self.assertEqual(job["tail"], "부분 초안")
        self.assertGreater(job["last_ev"], 0)
        cb({"ev": "model", "model": "m2"})     # 다음 콜 시작 — 수신량 이월 금지,
        # 단 tail(초안 재료)은 유지: 다음 콜 사고 중에도 미리보기가 남아야 한다
        self.assertEqual((job["recv"], job["tail"], job["phase"]),
                         (0, "부분 초안", ""))
        cb({"ev": "retry", "attempt": 1, "total": 2, "wait": 2})
        self.assertIn("재시도 1/2", job["retry"])
        job["running"] = False                 # 완료 후 늦게 온 이벤트는 무시
        cb({"ev": "delta", "phase": "writing", "bytes": 99})
        self.assertEqual(job["recv"], 0)

    def test_stream_event_caps_tail(self):
        # 꼬리는 미리보기 재료일 뿐 — 무한 누적이면 긴 콜에서 메모리가 샌다
        job = self._job()
        cb = web._job_stream_event(job, threading.Lock())
        cb({"ev": "delta", "phase": "writing", "bytes": 2000, "text": "가" * 2000})
        self.assertEqual(len(job["tail"]), 800)

    def test_live_line_priorities(self):
        # 우선순위: 재시도 > 직전 실패 > 무수신 정체 > 단계별 수신량.
        # 이벤트가 없으면 빈 줄. 모델은 이 줄이 아니라 전용 배지 담당.
        self.assertEqual(web._job_live_line({"last_ev": 0.0}), "")
        st = {"last_ev": time.time(), "phase": "thinking", "recv": 1234,
              "model": "claude-x", "retry": "", "failed": ""}
        self.assertEqual(web._job_live_line(st), "모델 사고 중 · 수신 1.2KB")
        st["phase"] = "writing"
        self.assertIn("작성 중 · 수신 1.2KB", web._job_live_line(st))
        st["phase"] = ""
        self.assertIn("모델 응답 대기 중", web._job_live_line(st))
        # 수신 0 은 정보가 아니다 — 사고 구간은 백엔드에 따라 내용이 아예 안 오고
        # (실기기 관찰), 작성 구간도 전환 직후엔 0이다. 숫자 없이 단계만 말한다.
        zero = dict(st, recv=0, phase="thinking")
        self.assertEqual(web._job_live_line(zero), "모델 사고 중")
        self.assertEqual(web._job_live_line(dict(zero, phase="writing")), "작성 중")
        st["last_ev"] = time.time() - 31
        self.assertIn("초째 무수신", web._job_live_line(st))
        st["retry"] = "호출 실패 — 재시도 1/2 (2초 뒤)"
        self.assertIn("재시도 1/2", web._job_live_line(st))
        self.assertNotIn("무수신", web._job_live_line(st))

    def test_live_line_and_preview_strip_control_chars(self):
        # 백엔드 stderr 에는 ANSI 색상 escape 나 바이너리 쓰레기가 섞인다.
        # esc() 는 <>&'" 만 막고 제어문자는 통과시켜, 그대로 실으면 화면에
        # 두부(□)로 뜬다(2026-07-26 CSS 이스케이프 사고와 같은 부류).
        st = {"last_ev": time.time(), "failed": "\x1b[31m치명적\x1b[0m 오류\x00",
              "retry": "", "cancel": None, "phase": "", "recv": 0}
        line = web._job_live_line(st)
        self.assertIn("치명적", line)
        self.assertNotRegex(line, "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
        st["retry"] = "재시도\x07 1/2"
        self.assertNotRegex(web._job_live_line(st),
                            "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
        prev = web._job_preview({"tail": '{"answer": "초안\x1b[1m입니다'})
        self.assertIn("초안", prev)
        self.assertNotRegex(prev, "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
        # 모델 배지도 CLI 가 준 값이다 — 잡 상태에 들어갈 때 걸러진다
        job = self._job()
        web._job_stream_event(job, threading.Lock())(
            {"ev": "model", "model": "claude\x00-x"})
        self.assertEqual(job["model"], "claude-x")

    def test_fmt_bytes_ladder(self):
        # 1KB 미만은 바이트 — 응답 초반 수 초가 거기 머문다(실측: 짧은 콜은
        # 델타 19건 중 12건이 1KB 아래). 10KB 부터는 소수점이 자리만 차지한다.
        f = review.fmt_bytes
        self.assertEqual(f(0), "0B")
        self.assertEqual(f(18), "18B")
        self.assertEqual(f(1023), "1023B")
        self.assertEqual(f(1024), "1.0KB")
        self.assertEqual(f(1442), "1.4KB")
        self.assertEqual(f(10240), "10KB")
        self.assertEqual(f(38900), "38KB")
        self.assertNotIn(".", f(38900))          # 큰 값엔 소수점 없음

    def test_live_line_announces_pending_cancel(self):
        # 비스트리밍 백엔드에서는 중지가 콜 경계까지 안 듣는다 — 화면이 그대로면
        # 사용자는 버튼이 안 먹은 줄 안다.
        ev = threading.Event()
        st = {"last_ev": time.time(), "phase": "thinking", "recv": 5,
              "retry": "", "failed": "", "cancel": ev, "stream": False}
        self.assertIn("사고 중", web._job_live_line(st))
        ev.set()
        self.assertIn("진행 중인 호출이 끝나면", web._job_live_line(st))
        st["stream"] = True
        self.assertEqual(web._job_live_line(st), "중지하는 중…")
        # 이벤트가 0건인 백엔드(AI 검색·opencode)가 바로 '안 멈추는 것처럼
        # 보이는' 경우다 — last_ev 없어도 안내가 나와야 한다
        self.assertIn("중지", web._job_live_line(
            {"last_ev": 0.0, "cancel": ev, "stream": False}))

    def test_live_line_failed_beats_stall_loses_to_retry(self):
        # 직전 실패는 무수신보다 먼저, 재시도보다 나중 — weekly 는 실패 콜을
        # 삼키고 계속 가므로 이 안내가 유일한 실패 가시화다.
        st = {"last_ev": time.time() - 45, "phase": "", "recv": 0,
              "model": "", "retry": "", "failed": "AI 호출 실패 (exit 3)"}
        line = web._job_live_line(st)
        self.assertIn("직전 호출 실패 — 이어서 진행", line)
        self.assertIn("exit 3", line)
        self.assertNotIn("무수신", line)
        st["retry"] = "호출 실패 — 재시도 1/1 (2초 뒤)"
        self.assertIn("재시도 1/1", web._job_live_line(st))

    def test_stream_event_failed_notice_and_model_clears(self):
        job = self._job()
        cb = web._job_stream_event(job, threading.Lock())
        cb({"ev": "delta", "phase": "writing", "bytes": 5, "text": "초안"})
        cb({"ev": "failed", "error": "exit 3"})
        self.assertEqual(job["failed"], "exit 3")
        cb({"ev": "model", "model": "m2"})     # 다음 콜 시작 — 실패 안내 클리어
        self.assertEqual(job["failed"], "")
        self.assertEqual(job["tail"], "초안")   # 초안은 유지(sticky)
        cb({"ev": "failed", "error": "다시 실패"})
        cb({"ev": "retry", "attempt": 1, "total": 2, "wait": 2})
        self.assertEqual(job["failed"], "")     # 새 시도 중 — 낡은 안내 제거

    def test_preview_sticky_regardless_of_phase(self):
        # thinking 이 긴 모델에서도 직전 수신 초안이 계속 보인다
        st = {"phase": "thinking", "tail": '{"answer": "납기 변경'}
        self.assertIn("납기 변경", web._job_preview(st))
        self.assertIn("작성 중 초안(검증 전)", web._job_preview(st))
        self.assertEqual(web._job_preview({"phase": "writing", "tail": ""}), "")

    def test_arm_job_backend_marks_stream_and_arms_watchdog(self):
        # 이벤트 0건 장애에서도 무수신 경고가 뜨려면 잡 시작 시각을 심어야
        # 하는데, 이벤트가 애초에 없는 백엔드에 심으면 오탐이다. stream 플래그는
        # 중지 안내 문구가 갈라지는 근거.
        cfg = Config(home=Path("."), my_addresses=[], my_names=[],
                     ai_backends={"c": {"cmd": ["claude", "-p"]},
                                  "e": {"cmd": ["echo"]}})
        job = {"running": True, "last_ev": 0.0, "stream": False}
        web._arm_job_backend(job, threading.Lock(), cfg, "c")
        self.assertGreater(job["last_ev"], 0)
        self.assertTrue(job["stream"])
        job2 = {"running": True, "last_ev": 0.0, "stream": True}
        web._arm_job_backend(job2, threading.Lock(), cfg, "e")
        self.assertEqual(job2["last_ev"], 0.0)
        self.assertFalse(job2["stream"])
        web._arm_job_backend(job2, threading.Lock(), cfg, "없는백엔드")
        self.assertEqual(job2["last_ev"], 0.0)  # SystemExit 무해 통과
        self.assertIn("즉시", web._cancel_hint(True))
        self.assertIn("끝난 뒤", web._cancel_hint(False))

    def test_job_start_takes_single_slot_and_resets_stream(self):
        job = web._new_job(question="")
        job.update(recv=99, model="old", tail="옛 초안", failed="옛 실패")
        cancel = web._job_start(job, threading.Lock(), stage="시작",
                                question="새 질문")
        self.assertIsNotNone(cancel)
        self.assertTrue(job["running"])
        self.assertEqual((job["recv"], job["model"], job["tail"],
                          job["failed"]), (0, "", "", ""))
        self.assertEqual(job["question"], "새 질문")
        self.assertIsNone(web._job_start(job, threading.Lock(), stage="두번째"))
        self.assertEqual(job["stage"], "시작")   # 남의 잡 상태를 덮지 않는다

    def test_job_wait_card_shared_structure(self):
        card = web._job_wait_card("wk", "제목", stage="단계", live="수신",
                                  preview="<초안>", model="claude-x",
                                  hint="힌트", cancel_action="/weekly/cancel")
        for frag in ("class='waitcard'", "class='spin'",
                     "rvfill indet' id='wk-fill'",
                     "id='wk-model'>claude-x<", "id='wk-stage'>단계<",
                     "id='wk-live'>수신<", "id='wk-elapsed'>0</span>초 경과",
                     "askbadge thin waitslot", "aibtn ghost compact",
                     "action='/weekly/cancel'", "id='wk-extra'"):
            self.assertIn(frag, card)
        self.assertIn("&lt;초안&gt;", card)     # 동적 값 esc
        ask = web._job_wait_card("ask", "제목", stage="s",
                                 cancel_action="/ask/cancel",
                                 cancel_extra="<input type='hidden'>")
        self.assertIn("id='ask-preview'></blockquote>", ask)
        self.assertIn("<input type='hidden'>", ask)

    def test_job_wait_card_determinate_bar_and_optional_cancel(self):
        # 단계를 세는 잡(회고·AI 검색)은 결정론 바를, 끊을 대상이 없는 잡
        # (동기화)은 중지 버튼째 생략한다 — 죽은 버튼을 두지 않는다.
        card = web._job_wait_card("rv", "회고", stage="s", step=2, total=6)
        self.assertIn("class='rvfill' id='rv-fill' style='width:33%'", card)
        self.assertNotIn("indet", card)
        self.assertNotIn("중지", card)
        self.assertNotIn("<form", card)
        # 0단계에서도 막대가 보이도록 하한이 있다(빈 막대는 멈춘 것처럼 보인다)
        self.assertIn("width:4%", web._job_wait_card("rv", "x", step=0, total=6))

    def test_job_wait_card_extra_slot_is_raw_html(self):
        # extra 만 이스케이프하지 않는다(호출부가 이미 esc 한 마크업) — 카드
        # 폭에 눌리지 않게 카드 밖 형제로 나온다.
        card = web._job_wait_card("ai", "검색", stage="<s>",
                                  extra="<ol class='aicards'></ol>")
        self.assertIn("&lt;s&gt;", card)
        self.assertIn("id='ai-extra'><ol class='aicards'></ol></div>", card)
        self.assertTrue(card.index("</div>") < card.index("id='ai-extra'"))

    def test_css_hides_empty_slots_by_class(self):
        # 새 잡이 CSS 를 건드리지 않아도 빈 슬롯이 숨도록 클래스 하나로 판정
        self.assertIn(".waitslot:empty", web._CSS)
        self.assertNotIn("#wk-model:empty", web._CSS)
        self.assertIn("waitslot' id='wk-model'", web._job_wait_card("wk", "t"))

    def test_preview_text_extracts_readable_fragment(self):
        # JSON 스트림 꼬리에서 서술 값만 — 중괄호·키 노출 금지, 이스케이프 복원
        tail = ('{"topics": [{"name": "NPX-200", "text": "납기가 8월 말로 변경\\n'
                '다음 주 확정')
        self.assertEqual(web._preview_text(tail),
                         "납기가 8월 말로 변경 다음 주 확정")
        self.assertEqual(web._preview_text('{"claims": [{"mid": 3'), "")
        self.assertEqual(web._preview_text(""), "")
        self.assertEqual(len(web._preview_text('{"answer": "' + "가" * 300)), 120)

    def test_every_running_marker_has_js_off_fallback(self):
        # JS 꺼진 환경에서 대기 화면이 영영 안 넘어가는 잡이 없어야 한다 —
        # 주간 보고·분석이 실제로 그랬다.
        import inspect
        src = inspect.getsource(web.do_GET_body) if hasattr(web, "do_GET_body") \
            else inspect.getsource(web._Handler.do_GET)
        self.assertIn("_RUNNING_MARKERS", src)
        for job in ("review", "aisearch", "sync", "weekly", "ask", "dossier"):
            self.assertIn(f"data-{job}-running", web._RUNNING_MARKERS)

    def test_inline_css_braces_balanced(self):
        # 규칙 블록을 지울 때 고아 `}` 가 남으면 CSS 파서가 **다음 규칙을 통째로
        # 폐기**한다(실제로 .libscene 제거 때 `.imgstrip` 이 죽었다). 문자열
        # 검사로는 안 잡히므로 균형을 직접 센다.
        from mailkb import report as report_mod
        for name, css in (("web._CSS", web._CSS),
                          ("report.CSS", report_mod.CSS)):
            depth = 0
            for i, ch in enumerate(css):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    self.assertGreaterEqual(
                        depth, 0, f"{name}: {i}번째 문자에서 고아 닫는 괄호 "
                                  f"— …{css[max(0, i - 80):i + 1]!r}")
            self.assertEqual(depth, 0, f"{name}: 닫히지 않은 블록 {depth}개")
        # 삼켜졌던 규칙이 살아 있는지도 함께 못박는다
        self.assertIn(".imgstrip {", web._CSS)

    def test_elapsed_counter_survives_other_pane_injection(self):
        # inject() 는 폴링 훅 6종을 주입된 패널로 전부 부른다 — 소유 패널을
        # 확인하지 않고 t0 를 지우면 남의 패널을 갱신할 때마다 경과가 0으로
        # 되감긴다(AI 검색은 옛 setInterval 방식에선 없던 회귀였다).
        js = web._APP_JS
        for marker, owner, key in (
                ("data-review-running", "right", "rv"),
                ("data-aisearch-running", "left", "ai"),
                ("data-weekly-running", "left", "weekly"),
                ("data-ask-running", "right", "ask"),
                ("data-dossier-running", "left", "dz"),
                ("data-sync-running", "right", "sy")):
            self.assertIn(
                f'if (!{owner}.querySelector("[{marker}]")) jobT0.{key} = 0;', js,
                f"{key}: 소유 패널 확인 없이 경과를 리셋한다")

    def test_sync_card_elapsed_actually_ticks(self):
        # 카드에 경과 슬롯을 그려 놓고 아무도 안 만지면 영원히 '0초 경과' 다
        self.assertIn('jobElapsed("sy", "#sy-elapsed", right)', web._APP_JS)

    def test_dossier_polling_reads_addr_from_marker(self):
        # DOM 순서(input[name=addr] 첫 번째)에 기대면 화면에 입력이 하나만
        # 추가돼도 조용히 엉뚱한 사람을 폴링한다 — 마커가 주소를 싣는다.
        self.assertIn('running.getAttribute("data-dossier-addr")', web._APP_JS)
        self.assertNotIn('querySelector("input[name=addr]")', web._APP_JS)
        st = {"running": True, "addr": "kim@corp.example", "stage": "s",
              "last_ev": 0.0, "cancel": None, "stream": False, "model": "",
              "tail": "", "retry": "", "failed": "", "phase": "", "recv": 0}
        self.assertIn("data-dossier-addr='kim@corp.example'",
                      web._dossier_wait_html(st))

    def test_css_dropped_libscene_and_legacy_wait(self):
        # 대기 화면 자산이 셋으로 갈려 있던 것을 카드 하나로 모았다
        for gone in ("libscene", ".aibar", ".aifill", ".aiwaitbody"):
            self.assertNotIn(gone, web._CSS)
            self.assertNotIn(gone, web._APP_JS)
        self.assertIn(".waitcard .spin", web._CSS)
        # 접근성: 무한 애니메이션은 reduced-motion 에서 전부 멈춘다
        rm = web._CSS[web._CSS.index("prefers-reduced-motion"):]
        self.assertIn(".rvfill.indet", rm[:200])
        self.assertIn(".waitcard .spin", rm[:200])

    def test_no_wait_card_promises_a_duration(self):
        # 새 잡을 만들 때 예상 시간을 다시 넣기 쉬운데, 그 숫자는 맞출 수 없다.
        # 여섯 카드를 한 번에 훑어 재유입을 막는다.
        import re as _re
        cards = [web._job_wait_card(p, "제목", stage="s", hint=h)
                 for p, h in (("rv", "메일을 읽습니다."), ("ai", "후보를 확정합니다."),
                              ("wk", "토픽을 씁니다."), ("ask", "근거를 대조합니다."),
                              ("dz", "인용을 검증합니다."), ("sy", "색인합니다."))]
        for card in cards:
            self.assertNotIn("보통", card)
            self.assertNotRegex(card, r"\(\s*(수 초|약|대략)")
            self.assertNotRegex(card, r"[0-9]+\s*[~-]\s*[0-9]+\s*분")

    def test_sync_card_has_no_ai_slots_or_cancel(self):
        try:
            with web._sync_lock:
                web._sync_job.update(running=True, msg="", n=0)
            inner, running = web.render_sync_status()
            self.assertTrue(running)
            self.assertIn("class='waitcard'", inner)
            self.assertIn("data-sync-running", inner)
            self.assertIn("id='sy-live'></p>", inner)      # 빈 슬롯(:empty 로 숨음)
            self.assertIn("id='sy-model'></span>", inner)
            self.assertNotIn("중지", inner)                # 끊을 대상이 없다
        finally:
            with web._sync_lock:
                web._sync_job.update(running=False, msg="", n=0)

    def test_elapsed_is_anchored_to_job_start(self):
        # 경과를 클라이언트 변수로만 세면 패널이 다시 그려지거나 페이지가
        # 리로드될 때 '0초 경과' 로 되감긴다(2026-07-29 실기기 증상: 조사 중간에
        # 리셋). 잡 시작 시각을 카드가 싣고 클라이언트가 그것으로 계산한다.
        job = web._new_job()
        cancel = web._job_start(job, threading.Lock(), stage="s")
        self.assertGreater(job["started"], 0)          # 시작 시각이 남는다
        card = web._job_wait_card("wk", "t", started=job["started"])
        self.assertRegex(card, r"id='wk-elapsed' data-since='\d+'>")
        # 시작 시각이 없으면(구 상태) 속성 없이 — 클라이언트 폴백이 산다
        self.assertNotIn("data-since", web._job_wait_card("wk", "t"))
        js = web._APP_JS
        self.assertIn('getAttribute("data-since")', js)
        self.assertIn("Date.now() / 1000 - since", js)
        # patchJob 은 경과 슬롯을 건드리지 않는다(클라이언트가 매 틱 갱신)
        self.assertNotIn('"#" + p + "-elapsed"', js)

    def test_meta_refresh_only_for_js_off(self):
        # 진행 화면 자동 새로고침은 JS 꺼짐 폴백이다. noscript 밖에 두면 JS 가
        # 켜져 있어도 2초마다 전체 페이지가 리로드돼, 매번 CSS 기본값(--left-w)
        # 으로 그려졌다가 app.js 가 저장 폭을 덮어써 좌/우 분리선이 떨린다
        # (2026-07-29 실기기 증상). 창 크기 복원(resizeTo)도 매번 재실행된다.
        head = web._head("제목", refresh=2)
        self.assertIn("<noscript><meta http-equiv='refresh' content='2'></noscript>",
                      head)
        # noscript 밖에 벌거벗은 refresh 태그가 남아 있으면 안 된다
        self.assertNotIn("<meta http-equiv='refresh' content='2'><title", head)
        self.assertNotIn("http-equiv", web._head("제목"))

    def test_polling_responses_are_not_cacheable(self):
        # 진행 중 화면은 1.5초마다 **같은 URL** 을 다시 부른다. 캐시 지시가 없으면
        # 브라우저 메모리 캐시가 같은 응답을 돌려줘 수신량·단계가 첫 값에 굳는다
        # (2026-07-29 실기기 증상: 서버는 계속 바뀌는데 화면만 고정).
        import inspect
        self.assertIn('send_header("Cache-Control", "no-store")',
                      inspect.getsource(web._Handler._send_html))
        # 클라이언트도 캐시 우회를 건다(이중 안전장치) — 폴링 fetch 전수 확인
        js = web._APP_JS
        for m in re.finditer(r'new URL\("([^"]+)"[^)]*\)(.{0,400}?)fetch\(', js, re.S):
            self.assertIn("Date.now()", m.group(2),
                          f"{m.group(1)}: 폴링 URL 에 캐시 우회가 없다")
        self.assertIn('fetch("/latest?_=" + Date.now())', js)

    def test_appjs_patches_card_slots_generically(self):
        # 잡마다 셀렉터를 나열하지 않는다 — prefix 규약 하나로 슬롯을 패치한다.
        js = web._APP_JS
        self.assertIn("function patchText", js)
        self.assertIn("function patchJob", js)
        for frag in ('"#" + p + "-stage"', '"#" + p + "-live"',
                     '"#" + p + "-preview"', '"#" + p + "-model"',
                     '"#" + p + "-fill"', '"#" + p + "-extra"'):
            self.assertIn(frag, js)
        self.assertIn('patchJob(tmp, right, "ask")', js)
        self.assertIn('patchJob(tmp, left, "wk")', js)


class TestBedrockRunAdapter(unittest.TestCase):
    """tools/bedrock_run.py(레거시 Bedrock 최소 어댑터) — ai_run 계약 목 검증.

    확인된 사내 조합: 레거시 엔드포인트(AnthropicBedrock) + --insecure + --proxy,
    모델 global.anthropic.claude-sonnet-5. anthropic 미설치에서도 돌도록
    _make_client 를 교체(실호출 없음)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        p = Path(__file__).resolve().parent.parent / "tools" / "bedrock_run.py"
        spec = importlib.util.spec_from_file_location("bedrock_run", p)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)          # 지연 임포트라 anthropic 불필요

    def _run(self, argv, stdin_text, factory, env_over=None):
        import io
        orig = self.mod._make_client
        self.mod._make_client = factory
        out, err = io.StringIO(), io.StringIO()
        clean = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                                 "http_proxy", "AWS_REGION")}
        if env_over:
            clean.update(env_over)
        try:
            with mock.patch.dict(os.environ, clean), \
                 mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                 mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = self.mod.main(argv)
        finally:
            self.mod._make_client = orig
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _client(calls, blocks):
        from types import SimpleNamespace
        msg = SimpleNamespace(content=blocks)

        class _C:
            class messages:
                @staticmethod
                def create(**kw):
                    calls.append(kw)
                    return msg
        return _C()

    def test_defaults_model_and_region(self):
        from types import SimpleNamespace
        calls, made = [], {}

        def factory(region, proxy=None, insecure=False):
            made["region"], made["proxy"], made["insecure"] = region, proxy, insecure
            return self._client(calls, [SimpleNamespace(type="text", text="응답")])
        rc, out, _ = self._run([], "질문입니다", factory)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "응답")                       # stdout = 텍스트만
        self.assertEqual(made["region"], "ap-northeast-2")  # 기본 서울
        self.assertTrue(made["insecure"])                   # 기본 검증 끔
        self.assertIsNone(made["proxy"])                    # 기본 프록시 미설정
        self.assertEqual(calls[0]["model"], "global.anthropic.claude-sonnet-5")
        self.assertEqual(calls[0]["messages"],
                         [{"role": "user", "content": "질문입니다"}])

    def test_model_alias_resolution(self):
        # claude CLI 백엔드와 같은 별칭(opus/sonnet)이 Bedrock 에서도 동작해야
        # 한다. Bedrock 은 서버측 별칭이 없어 전체 ID 로 풀어 보낸다(opus=4.8
        # 고정 — 사용자 확정). 별칭이 아니면 그대로(전체 ID 직접 지정 경로).
        from types import SimpleNamespace
        calls = []

        def factory(region, proxy=None, insecure=False):
            return self._client(calls, [SimpleNamespace(type="text", text="ok")])
        rc, _, _ = self._run(["--model", "opus"], "q", factory)
        self.assertEqual(rc, 0)
        # [1M] 접미는 사내 환경 확정 이름 — AWS 공개 문서의 무접미 형태로 되돌리지 말 것
        self.assertEqual(calls[0]["model"], "global.anthropic.claude-opus-4-8[1M]")
        calls.clear()
        rc, _, _ = self._run(
            ["--model", "global.anthropic.claude-opus-5"], "q", factory)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["model"], "global.anthropic.claude-opus-5")

    def test_secure_flag_enables_verify(self):
        from types import SimpleNamespace
        calls, made = [], {}

        def factory(region, proxy=None, insecure=False):
            made["insecure"] = insecure
            return self._client(calls, [SimpleNamespace(type="text", text="ok")])
        rc, _, _ = self._run(["--secure"], "q", factory)
        self.assertEqual(rc, 0)
        self.assertFalse(made["insecure"])                  # --secure → 검증 켬

    def test_explicit_proxy_passed(self):
        from types import SimpleNamespace
        calls, made = [], {}

        def factory(region, proxy=None, insecure=False):
            made["proxy"] = proxy
            return self._client(calls, [SimpleNamespace(type="text", text="ok")])
        rc, _, _ = self._run(
            ["--proxy", "http://proxy.corp:8080", "--region", "us-west-2",
             "--max-tokens", "512"], "q", factory)
        self.assertEqual(rc, 0)
        self.assertEqual(made["proxy"], "http://proxy.corp:8080")
        self.assertEqual(calls[0]["max_tokens"], 512)

    def test_env_proxy_not_forced(self):
        # env HTTPS_PROXY 가 있어도 --proxy 없으면 명시 전달 안 함(httpx trust_env 가 읽음)
        from types import SimpleNamespace
        calls, made = [], {}

        def factory(region, proxy=None, insecure=False):
            made["proxy"] = proxy
            return self._client(calls, [SimpleNamespace(type="text", text="ok")])
        rc, _, _ = self._run([], "q", factory,
                             env_over={"HTTPS_PROXY": "http://env:3128"})
        self.assertEqual(rc, 0)
        self.assertIsNone(made["proxy"])                    # env 폴백/강제 없음

    def test_make_client_verify_false_default(self):
        # _make_client 기본: httpx verify=False(검증 끔), proxy 주면 proxy= 직접
        import sys as _sys
        from types import SimpleNamespace
        cap = {}

        class _FakeHttpx:
            def __init__(self, **kw):
                cap.update(kw)

        class _FakeClient:
            def __init__(self, **kw):
                cap["client_kw"] = kw
        fake_httpx = SimpleNamespace(Client=_FakeHttpx)
        fake_anth = SimpleNamespace(AnthropicBedrock=_FakeClient)
        with mock.patch.dict(_sys.modules, {"httpx": fake_httpx,
                                            "anthropic": fake_anth}):
            self.mod._make_client("ap-northeast-2", proxy="http://p:8080")
        self.assertIs(cap.get("verify"), False)             # 기본 insecure
        self.assertEqual(cap.get("proxy"), "http://p:8080")
        self.assertEqual(cap["client_kw"]["aws_region"], "ap-northeast-2")

    def test_empty_prompt_exits_2(self):
        rc, out, err = self._run([], "   \n",
                                 lambda r, proxy=None, insecure=False: self.fail("호출 금지"))
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("빈 프롬프트", err)

    def test_call_failure_exits_1(self):
        def factory(region, proxy=None, insecure=False):
            raise Exception("boom")
        rc, out, err = self._run([], "q", factory)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")                           # 실패 시 stdout 오염 금지
        self.assertIn("Bedrock 호출 실패", err)

    def test_missing_sdk_exits_2(self):
        def factory(region, proxy=None, insecure=False):
            raise ModuleNotFoundError("No module named 'anthropic'")
        rc, _, err = self._run([], "q", factory)
        self.assertEqual(rc, 2)
        self.assertIn("anthropic[bedrock]", err)

    def test_empty_text_blocks_exit_1(self):
        from types import SimpleNamespace
        calls = []
        rc, out, err = self._run(
            [], "q", lambda r, proxy=None, insecure=False: self._client(
                calls, [SimpleNamespace(type="tool_use", text="")]))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("텍스트 블록 없음", err)


class TestFakeCorpusColors(unittest.TestCase):
    """합성 코퍼스의 작성자 강조색 — 다크 모드 색 보정의 회귀 재료.

    실환경 메일의 약 10%가 본문에 색을 쓴다. 코퍼스에 그 비율이 없으면
    clean.add_dark_colors 가 깨져도 아무 테스트가 안 운다(실제로 196건 중
    0건이라 처음엔 회귀 가드가 없었다)."""

    @classmethod
    def setUpClass(cls):
        from mailkb.sources.fake import FakeSource
        cls.recs = list(FakeSource().fetch(None, None))
        cls.colored = [r for r in cls.recs
                       if re.search(r"(?i)color\s*[:=]", r.body_html or "")]

    def test_about_one_in_ten_mails_use_color(self):
        share = len(self.colored) / len(self.recs)
        self.assertGreater(share, 0.07, f"색 사용 {share:.1%} — 너무 적다")
        self.assertLess(share, 0.14, f"색 사용 {share:.1%} — 너무 많다")

    def test_both_color_markup_forms_present(self):
        # span style 과 레거시 <font color> 는 add_dark_colors 에서 처리 경로가
        # 다르다 — 둘 다 코퍼스에 있어야 양쪽이 지켜진다.
        self.assertTrue([r for r in self.colored if 'style="color' in r.body_html])
        self.assertTrue([r for r in self.colored if "<font color" in r.body_html])

    def test_corpus_is_reproducible(self):
        from mailkb.sources.fake import FakeSource
        again = list(FakeSource().fetch(None, None))
        self.assertEqual([r.body_html for r in self.recs],
                         [r.body_html for r in again])

    def test_color_only_wraps_never_changes_text(self):
        # 착색기는 감싸기만 해야 한다. 글자가 바뀌면 화면과 인용·검색이 어긋난다.
        # 색 있는 HTML 과 색 없는 HTML 의 텍스트가 같은지로 본다(서명·인용 사슬은
        # 양쪽에 똑같이 들어 있으므로 이 비교가 착색기만 딱 검사한다).
        from mailkb.sources.fake import _color_seed, _plain_to_html, _scenario
        norm = lambda h: re.sub(r"\s+", "", html_mod.unescape(
            re.sub(r"<[^>]+>", "", h)))
        checked = 0
        for m in _scenario():
            seed = _color_seed(m.key)
            if seed is None:
                continue
            self.assertEqual(norm(_plain_to_html(m.full_body, seed)),
                             norm(_plain_to_html(m.full_body, None)),
                             f"착색이 글자를 바꿨다: {m.subject[:40]}")
            checked += 1
        self.assertGreater(checked, 20)          # 표본이 비면 통과해도 의미 없다

    def test_dark_transform_covers_every_colored_mail(self):
        from mailkb.clean import add_dark_colors
        for r in self.colored:
            out = add_dark_colors(r.body_html)
            self.assertIn("--dk:", out, f"보정 누락: {r.subject[:40]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
