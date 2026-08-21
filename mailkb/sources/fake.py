"""FakeSource — 한국어 회사 메일 합성 생성기 (~230통, 최근 1개월).

배경: 가상 팹리스 '누리소프트' — 엣지 NPU SoC(NPX-200) 개발사.
주제: SoC 설계(타이밍/ECO/MPW)·AI(양자화/서빙/MLOps)·Security(CVE/시큐어부트/침투테스트).

회사 PC 없이 전체 파이프라인을 검증하기 위한 소스. 실제 환경의 지뢰를 재현:
  - 답장마다 이전 본문 전체를 재인용 (한국어 Outlook 헤더 블록)
  - 긴 기술 디스커션(12~14통) — 인용 누적 O(n²) 제거 검증용
  - 서명, 법적 고지, 야간 발신, 별칭(dhkim@) 발신
  - 시스템 노티(JIRA/빌드/인사) + 외부 스팸 — 노이즈 필터 검증용
  - 미답변·기한 요청·오늘 내려진 결정·증발한 요청·조용해진 사람·첨부
  - '++수신인 추가'/FYI 한 줄, 장문 1통(요약 게이트 우회), 마크다운 텍스트 메일

시드 고정(42)으로 결정론적. 날짜는 실행일 기준 상대 생성이라
review 데모가 항상 "오늘" 데이터를 갖는다.
"""

from __future__ import annotations

import hashlib
import html as _htmlmod
import random
import re
from datetime import datetime, timedelta
from typing import Iterator

from .base import MailRecord

ME = "dohyun.kim@nurisoft.co.kr"
ME_ALIAS = "dhkim@nurisoft.co.kr"     # 메일 별칭 — 별칭 발신 재분류 데모용

_URL_RX = re.compile(r'(https?://[^\s<>"\)]+)')


# 작성자 강조색 — 실환경 메일의 약 10%가 본문에 색을 쓴다. 다크 모드 색 보정
# (clean.add_dark_colors)의 회귀 재료라 합성 코퍼스에도 같은 비율로 심는다.
# Word/Outlook 기본 팔레트에서 고르고, 강조 대상은 실제로 사람이 칠하는 것들
# (결정·기한·수치)로 한정한다.
_COLOR_RULES = [
    (re.compile(r"(긴급|즉시|취소|반려|중단)"), "#C00000"),
    (re.compile(r"(확정|승인|완료|재개)"), "#008000"),
    (re.compile(r"(\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2}|오늘까지|금일|내일까지)"), "#C00000"),
    (re.compile(r"(\d+(?:\.\d+)?%p?)"), "#0070C0"),
    (re.compile(r"(보류|지연|재검토)"), "#7030A0"),
]
_TAG_SPLIT_RX = re.compile(r"(<[^>]*>)")


def _colorize(html: str, seed: int) -> str:
    """일부 구절에 강조색을 입힌다 — 태그 밖 텍스트만 건드려 href 를 안 깬다.

    글자는 감싸기만 하고 바꾸지 않는다(본문 text 와 어긋나면 인용 검증이 깨진다).
    한 통에 규칙 2종까지만 — 무지개가 되면 실물과 안 닮는다. 표기는 span 과
    레거시 <font color> 를 섞어 양쪽 처리 경로를 다 태운다."""
    use_font = seed % 3 == 0

    def wrap(col: str, inner: str) -> str:
        return (f'<font color="{col}">{inner}</font>' if use_font
                else f'<span style="color:{col}">{inner}</span>')

    parts = _TAG_SPLIT_RX.split(html)
    hits = 0
    # 씨앗 위치부터 전 규칙을 훑어 실제로 걸리는 것 2종까지 — 2종만 고르면
    # 본문에 그 말이 없을 때 그냥 색이 안 붙어 비율이 무너진다(실측 10%→1.8%).
    for off in range(len(_COLOR_RULES)):
        if hits >= 2:
            break
        rx, col = _COLOR_RULES[(seed + off) % len(_COLOR_RULES)]
        for i, seg in enumerate(parts):
            if seg.startswith("<") or not seg.strip() or not rx.search(seg):
                continue
            parts[i] = rx.sub(lambda m: wrap(col, m.group(1)), seg, count=1)
            hits += 1
            break
    if hits:
        return "".join(parts)
    # 어느 규칙도 안 걸리는 본문 — 첫 어절 몇 개를 칠한다. 사람도 결정어가
    # 없으면 그냥 첫 구절을 강조한다.
    col = _COLOR_RULES[seed % len(_COLOR_RULES)][1]
    for i, seg in enumerate(parts):
        if seg.startswith("<") or not seg.strip():
            continue
        m = re.match(r"(\s*)(\S+(?:\s+\S+){0,2})(.*)", seg, re.S)
        if m:
            parts[i] = m.group(1) + wrap(col, m.group(2)) + m.group(3)
        break
    return "".join(parts)


def _client_html(body: str, quoted: str, shape: int,
                 color_seed: int | None = None) -> str:
    """실제 메일 클라이언트가 내는 답장 HTML 구조를 흉내 낸다.

    데모의 body_html 이 '평문을 <p> 로 감싼 것'뿐이면 인용 접기(HTML 경로)가
    실기기에서 동작하는지 화면으로도 테스트로도 확인할 수 없다 — 실제 구분선은
    텍스트가 아니라 border-top div·<hr>·전용 컨테이너다(2026-07-31 리뷰:
    데모 282통 중 67통에 인용 체인이 그대로 남아 있었다)."""
    head = _plain_to_html(body, color_seed)
    if not quoted:
        return head
    qhtml = _plain_to_html(quoted)
    if shape == 0:            # 클래식 Outlook — 테두리 div
        return (head + "<div style='border:none;border-top:solid #E1E1E1 1.0pt;"
                "padding:3.0pt 0cm 0cm 0cm'>" + qhtml + "</div>")
    if shape == 1:            # OWA·새 Outlook — <hr> + 전용 id
        return head + "<hr><div id='divRplyFwdMsg'>" + qhtml + "</div>"
    return (head + "<div class='mail-editor-reference-message-container'>"
            + qhtml + "</div>")          # Outlook for Mac


def _plain_to_html(text: str, color_seed: int | None = None) -> str:
    """합성 평문 본문 → 표시용 HTML (문단·줄바꿈·URL 링크화).

    color_seed 가 주어지면 강조색을 입힌다(약 10%의 메일에만 — 호출부가 정한다)."""
    paras = re.split(r"\n\s*\n", text.strip())
    out = []
    for p in paras:
        esc = _URL_RX.sub(r'<a href="\1">\1</a>', _htmlmod.escape(p))
        if color_seed is not None:
            esc = _colorize(esc, color_seed)
        out.append("<p>" + esc.replace("\n", "<br>\n") + "</p>")
    return "\n".join(out)


def _color_seed(key: str) -> int | None:
    """이 메일에 색을 입힐지 — 키 해시로 약 10%. 파이썬 hash() 는 실행마다
    달라지므로 쓰면 안 된다(코퍼스가 재현되지 않는다)."""
    h = int(hashlib.blake2s(key.encode("utf-8"), digest_size=4).hexdigest(), 16)
    return h if h % 10 == 0 else None


# 서식(굵게·표·링크)과 추적 픽셀 차단을 웹 데모에서 보여주기 위한 리치 HTML 오버라이드
_RICH_HTML = {
    # Confluence 알림 — 중첩 레이아웃 표 셸(실환경 그대로). html_to_markdown 이
    # 이걸 파이프 표로 렌더하거나 본문을 통째 잃지 않는지 눈으로 확인하는 재료.
    "conf6": (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0"><tr><td>'
        '<div style="mso-hide:all">이 메일이 보이지 않으면 브라우저에서 확인하세요</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        "<tr><td><b>이서연 선임</b> 님이 페이지를 수정했습니다</td></tr>"
        '<tr><td><a href="https://wiki.nurisoft.co.kr/npx200/26">'
        "B0 브링업 체크리스트</a></td></tr>"
        "<tr><td><table border='1' cellpadding='4'>"
        "<tr><th>항목</th><th>상태</th></tr>"
        "<tr><td>클럭 게이팅 해제 후 리셋</td><td>지연 200us 이상 조건 추가</td></tr>"
        "<tr><td>전원 시퀀스 확인</td><td>완료</td></tr></table></td></tr>"
        "<tr><td style='display:none'>추적용 숨김 텍스트</td></tr>"
        "</table></td></tr></table>"
        '<img src="http://track.example/conf.gif" width="1" height="1" alt="">'
    ),
    # 작성자 강조색 — 다크 모드 색 보정(clean.add_dark_colors)의 회귀 재료.
    # 실환경 Outlook/Word 가 실제로 뱉는 형태를 섞는다: span style, 레거시
    # <font color>, rgb(), 이름 색, 색 안의 굵게·중첩 색·링크, 회색 안내문.
    # 본문 텍스트(body_text)와 뜻이 같아야 인용 검증이 어긋나지 않는다.
    # 글자는 body_text 와 한 자도 다르지 않아야 한다 — 표시용 HTML 이 본문과
    # 다른 말을 하면 화면과 인용·검색 결과가 어긋난다. 색만 입힌다.
    "qt4": (
        '<p><span style="color:#C00000"><b>양자화는 QAT 로 확정합니다.</b></span> '
        "근거: 고객 재학습 파이프라인이 확보되어<br>\n있고 "
        '<span style="color:#008000">정확도 회복이 확실</span>하기 때문입니다. '
        '<font color="#0070C0">mixed precision</font> 은 QAT 실패<br>\n시 '
        '<span style="color:rgb(112,48,160)">폴백으로만 유지</span>합니다. '
        '<span style="color:#C00000">강선임이 툴킷 연동 킥오프 '
        "<b>잡아 주세요.</b></span></p>"
    ),
    "mask2": (
        "<p>도현님,</p>"
        "<p>MPW 마스크 일정은 <b>8/22 tape-in 확정</b>입니다. 아래 표 참고 바랍니다.</p>"
        '<table border="1" cellpadding="4" cellspacing="0">'
        "<tr><th>단계</th><th>기한</th><th>비고</th></tr>"
        "<tr><td>GDS 제출</td><td>8/20</td><td>DRC clean 필수</td></tr>"
        '<tr><td>tape-in</td><td>8/22</td><td><i>연기 불가</i></td></tr></table>'
        '<p>탑승 블록 목록 회신 부탁드립니다. 상세: '
        '<a href="https://wiki.nurisoft.co.kr/mpw/2026q3">MPW 위키</a></p>'
        '<img src="http://track.example/open.gif" width="1" height="1" alt="">'
        # 분할 인용 라벨 — sanitize_html 절단 눈검증용 (웹 스레드 뷰에서
        # 아래 "이전 인용 내용"이 보이면 절단 회귀)
        "<div>--------- </div><div><b>Original Message</b></div>"
        "<div> ---------</div>"
        "<p>From: 김도현</p><p>이전 인용 내용입니다. 지난 셔틀 일정은…</p>"
    ),
    # 인라인(cid) 이미지 — 최근(표시)과 보존 기간 경과(마커) 두 상태 데모.
    # 같은 이미지를 두 번 참조해 '중복 생략' 표시도 함께 시연.
    "imgnew": (
        "<p>브링업 보드 부팅 파형 공유드립니다.</p>"
        '<p>정상 케이스:<br><img src="cid:wave1@nurisoft" alt="정상 파형"></p>'
        '<p>행 재현 케이스:<br><img src="cid:wave2@nurisoft" alt="행 파형"></p>'
        '<p>(참고 — 정상 파형 재게시)<br><img src="cid:wave1@nurisoft"></p>'
        "<p>행 케이스는 PLL 락 직후 리셋이 관측됩니다. 분석 의견 부탁드립니다.</p>"
    ),
    "imgold": (
        "<p>지난달 레이아웃 스냅샷 공유합니다.</p>"
        '<img src="cid:floor1@nurisoft" alt="레이아웃">'
        "<p>다음 리비전에서 매크로 배치가 바뀔 예정입니다.</p>"
    ),
}

# body_html 을 비워 텍스트 메일로 보내는 키 — 웹 마크다운 토글(#21) 데모
_NO_HTML = {"mdmail"}


def _png(rgb: tuple, size: int = 24) -> bytes:
    """합성 단색 PNG (stdlib) — 인라인 이미지 데모용 (수백 바이트)."""
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d)))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# 인라인(cid) 이미지 메일 — sanitize 가 cid 를 차단 마크로 바꾸고 store 가
# inline_images 바이트를 주입한다 (docs/ARCHITECTURE.md §6.1 경로 그대로 재현)
_INLINE_IMAGES = {
    "imgnew": {"wave1@nurisoft": ("image/png", _png((70, 130, 220))),
               "wave2@nurisoft": ("image/png", _png((220, 120, 70)))},
    "imgold": {"floor1@nurisoft": ("image/png", _png((120, 190, 120)))},
}

_PEOPLE = {
    # 사내 인물 (NPX-200 엣지 NPU SoC 개발 조직)
    "kim": ("김민수 팀장", "minsu.kim@nurisoft.co.kr"),     # SoC개발팀장
    "jung": ("정우진 수석", "woojin.jung@nurisoft.co.kr"),  # RTL/백엔드
    "yoon": ("윤성호 책임", "seongho.yoon@nurisoft.co.kr"), # PD/타이밍
    "lee": ("이서연 선임", "seoyeon.lee@nurisoft.co.kr"),   # 검증(DV)/브링업
    "oh": ("오태양 책임", "taeyang.oh@nurisoft.co.kr"),     # 플랫폼SW/드라이버
    "seo": ("서지훈 수석", "jihoon.seo@nurisoft.co.kr"),    # 보안(시큐어부트/암호)
    "kang": ("강미래 선임", "mirae.kang@nurisoft.co.kr"),   # AI 모델/컴파일러
    "han": ("한예린 주임", "yerin.han@nurisoft.co.kr"),     # MLOps/인프라 (조용해질 사람)
    "park": ("박지현 책임", "jihyun.park@nurisoft.co.kr"),  # 파운드리/구매
    "choi": ("최하늘 주임", "haneul.choi@nurisoft.co.kr"),  # PM/회의록
    "me": ("김도현", ME),
    "me2": ("김도현", ME_ALIAS),                            # 별칭 발신
    "gm": ("김보라 총무", "bora.kim@nurisoft.co.kr"),
    # 시스템 발신 (ignore_senders 로 필터되어야 함)
    "sys": ("사내공지", "noreply@nurisoft.co.kr"),
    "jira": ("JIRA", "jira@nurisoft.co.kr"),
    "build": ("빌드서버", "build@nurisoft.co.kr"),
    "hr": ("인사팀", "noreply-hr@nurisoft.co.kr"),
    # 협업도구·자동 리포트 (모두 'noreply' 로 시작 — 기본 ignore_senders 에 걸린다)
    "conf": ("사내위키", "noreply-confluence@nurisoft.co.kr"),
    "scan": ("정적분석", "noreply-scan@nurisoft.co.kr"),
    "ci": ("CI리포트", "noreply-ci@nurisoft.co.kr"),
    # 추가 인물
    "yang": ("양준호 변호사", "junho.yang@nurisoft.co.kr"),  # 법무/라이선스
    "shin": ("신다은 선임", "daeun.shin@nurisoft.co.kr"),    # 전력/DVFS
    # 외부 스팸 (internal_domains 로 필터되어야 함)
    "spam_news": ("테크뉴스레터", "news@techletter.example"),
    "spam_shop": ("오피스몰", "promo@shopdeals.example"),
    "spam_webi": ("웨비나사무국", "invite@bizwebinar.example"),
}

# 대량 수신 메일용 가상 직원 명단
for _i in range(44):
    _PEOPLE[f"emp{_i}"] = (f"직원{_i:02d}", f"employee{_i:02d}@nurisoft.co.kr")

_NO_SIG = {"sys", "jira", "build", "hr", "conf", "scan", "ci",
           "spam_news", "spam_shop", "spam_webi"}

_SIGNATURE = """
--
{name}
SoC개발팀 | 내선 {ext}
※ 본 메일은 기밀 정보를 포함할 수 있으며, 지정된 수신자 외의 사용을 금합니다.
"""


_QUOTE_HEAD_RX = re.compile(
    r"\n\n(?:_{10,}\n)?(?=\*{0,2}(?:보낸 사람|De )\s*\*{0,2}\s*:)")


def _split_quote(full: str) -> tuple[str, str]:
    """본문을 (새로 쓴 부분, 인용 블록)으로 가른다 — HTML 구조화용."""
    m = _QUOTE_HEAD_RX.search(full)
    if not m:
        return full, ""
    return full[:m.start()], full[m.end():]


def _quote_block(parent: "_Mail") -> str:
    """Outlook 답장 인용 블록 — 실기기에서 보이는 형태를 번갈아 낸다.

    실제 body_text 는 HTMLBody→마크다운 변환 결과라 라벨이 **굵게** 오고
    구분선이 테두리 div 라 흔적이 안 남는 경우가 많다. 데모가 밑줄+평문만
    만들면 절단이 되는 것처럼 보여도 실기기에서 안 잘린다(2026-07-31)."""
    when = parent.when.strftime('%Y년 %m월 %d일 %A %p %I:%M')
    shape = parent.when.day % 3
    if shape == 0:                       # 클래식 — 밑줄 구분선 + 평문 라벨
        head = ("\n\n________________________________\n"
                f"보낸 사람: {parent.sender_name} <{parent.sender_addr}>\n"
                f"보낸 날짜: {when}\n"
                f"받는 사람: {'; '.join(parent.to)}\n"
                f"제목: {parent.subject}\n\n")
    elif shape == 1:                     # 굵은 라벨, 구분선 없음
        head = (f"\n\n**보낸 사람:** {parent.sender_name} "
                f"<{parent.sender_addr}>\n"
                f"**보낸 날짜:** {when}\n"
                f"**받는 사람:** {'; '.join(parent.to)}\n"
                f"**제목:** {parent.subject}\n\n")
    else:                                # 해외 파트너(프랑스어 클라이언트)
        head = (f"\n\n**De :** {parent.sender_name} <{parent.sender_addr}>\n"
                f"**Envoyé :** {when}\n"
                f"**À :** {'; '.join(parent.to)}\n"
                f"**Objet :** {parent.subject}\n\n")
    return head + parent.full_body


class _Mail:
    def __init__(self, key, sender, to, cc, subject, body, when,
                 attachments=None, sig=True, ext="1234"):
        name, addr = _PEOPLE[sender]
        self.key = key
        self.sender_name = name
        self.sender_addr = addr
        self.to = [_PEOPLE[t][1] for t in to]
        self.cc = [_PEOPLE[c][1] for c in cc]
        self.subject = subject
        self.when = when
        self.attachments = attachments or []
        self.parent = None
        use_sig = sig and sender not in _NO_SIG
        self.full_body = body.strip() + (_SIGNATURE.format(name=name, ext=ext) if use_sig else "")


def _day(days_ago: int, hour: int, minute: int = 0) -> datetime:
    # 실행일 기준 상대 날짜 — review 데모가 항상 "오늘" 데이터를 갖도록
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _sched(start_day: int, n: int, rng: random.Random) -> list[datetime]:
    """start_day 일 전 → 오늘까지 n 개의 시각을 단조 증가로 배치.

    미래로 넘치지 않고(과거 랜덤 워크의 버그), 답장이 부모보다
    빠른 시각을 갖지 않도록 보장한다.
    """
    out: list[datetime] = []
    prev_day, hour = None, 9
    for i in range(n):
        day = start_day * (n - 1 - i) // (n - 1) if n > 1 else start_day
        if day == prev_day:
            hour = min(hour + rng.choice([1, 2, 3]), 19)
        else:
            hour = rng.choice([9, 10])
        out.append(_day(day, hour, rng.randint(0, 59)))
        prev_day = day
    return out


def _sched_range(start_day: int, end_day: int, n: int, rng: random.Random) -> list[datetime]:
    """start_day 일 전 → end_day 일 전(둘 다 과거) 사이에 n 개 시각을 단조 증가로 배치.

    _sched 는 항상 오늘(0일)까지 오지만, 한 달 밴드의 '오래된 스레드'는
    과거에서 끝나야(오늘까지 안 옴) 하므로 끝점을 지정하는 변형.
    """
    span = start_day - end_day
    out: list[datetime] = []
    prev_day, hour = None, 9
    for i in range(n):
        day = start_day - span * i // (n - 1) if n > 1 else start_day
        if day == prev_day:
            hour = min(hour + rng.choice([1, 2, 3]), 19)
        else:
            hour = rng.choice([9, 10])
        out.append(_day(day, hour, rng.randint(0, 59)))
        prev_day = day
    return out


def _extra(send, rng: random.Random) -> None:
    """추가 시나리오 — 협업도구·자동 리포트와 **그것을 참조하는 사람 메일**.

    실환경의 큰 덩어리는 위키/CI 알림이고, 정작 중요한 논의는 그 알림을 인용하며
    사람들 사이에서 벌어진다. 알림 자체는 노이즈로 걸러지되(모두 noreply-*),
    그것을 참조한 사람 스레드는 살아남아야 한다 — 필터가 논의까지 죽이지 않는지
    확인하는 재료다. 아울러 새 기능 데모용 신호를 의도적으로 넣는다:
      · 본문이 나를 지목("김도현님") — 주간 보고 관여도의 최상위 신호
      · 내 발신에 대한 답장 — 두 번째 신호
      · 일정이 한 번 뒤집히는 사안 — 질문하기의 '상충함' 데모
      · 특정 인물과의 지속 교신 — 인물 브리핑 데모
    """
    WIKI = "https://wiki.nurisoft.co.kr"

    # ── 사내 위키(Confluence) 알림 ─────────────────────────────────────────
    conf_pages = [
        ("B0 브링업 체크리스트", "lee", 19), ("NPU 드라이버 API 스펙 v0.9", "oh", 17),
        ("양자화 정확도 실험 로그", "kang", 15), ("MPW 셔틀 일정표", "park", 13),
        ("시큐어부트 키 관리 절차", "seo", 12), ("전력 모드 전환 시퀀스", "shin", 10),
        ("B0 브링업 체크리스트", "lee", 8), ("주간 리스크 보드", "choi", 6),
        ("NPU 드라이버 API 스펙 v0.9", "oh", 5), ("PDK 2.1 마이그레이션 노트", "park", 4),
        ("양자화 정확도 실험 로그", "kang", 3), ("B0 브링업 체크리스트", "lee", 2),
    ]
    for i, (page, who, day) in enumerate(conf_pages):
        name = _PEOPLE[who][0]
        kind = ("수정했습니다" if i % 3 else "댓글을 남겼습니다")
        send(f"conf{i}", "conf", ["me"], [],
             f"[Confluence] {name} 님이 \"{page}\" 페이지를 {kind}",
             f"{name} 님이 페이지를 {kind}.\n\n"
             f"페이지: {page}\n"
             f"공간: NPX-200 개발\n"
             f"바로가기: {WIKI}/npx200/{20 + i}\n\n"
             "이 알림은 자동 발송되었습니다. 알림 설정은 프로필에서 변경하세요.",
             _day(day, rng.choice([8, 11, 16]), rng.randint(0, 59)), sig=False)

    # ── 위키를 참조한 사람 스레드 1: 체크리스트 항목 문의 (나를 지목) ──────
    send("wref0", "lee", ["me"], ["kim"],
         "브링업 체크리스트 3항 — 클럭 게이팅 확인 절차 문의",
         "김도현님, 위키에 올린 B0 브링업 체크리스트 3항 관련해 문의드립니다.\n"
         f"{WIKI}/npx200/26 의 '클럭 게이팅 해제 후 리셋 시퀀스' 항목인데,\n"
         "런타임 초기화 순서와 충돌하는지 확인이 필요합니다.\n"
         "이번 주 중으로 의견 부탁드립니다.",
         _day(7, 10, 12))
    send("wref1", "me", ["lee"], ["kim"],
         "RE: 브링업 체크리스트 3항 — 클럭 게이팅 확인 절차 문의",
         "확인했습니다. 런타임은 리셋 해제 후 200us 지연을 두고 진입하므로\n"
         "체크리스트 순서와 충돌하지 않습니다.\n"
         "다만 3항에 '지연 200us 이상' 조건을 명시해 두는 게 좋겠습니다.",
         _day(6, 15, 40), reply_to="wref0")
    send("wref2", "lee", ["me"], ["kim"],
         "RE: 브링업 체크리스트 3항 — 클럭 게이팅 확인 절차 문의",
         "반영해서 위키 갱신했습니다. 감사합니다.",
         _day(6, 17, 5), reply_to="wref1")

    # ── 위키를 참조한 사람 스레드 2: 문서 정리 요청(내가 시작 → 답장) ──────
    send("wref3", "me", ["oh", "kang"], ["choi"],
         "드라이버 API 스펙 문서 정리 요청",
         "v0.9 스펙 위키가 절 단위로 갈라져 있어 참조가 어렵습니다.\n"
         f"{WIKI}/npx200/21 기준으로 한 페이지로 합치고 변경 이력만 따로 두면\n"
         "어떨까요? 다음 주 리뷰 전까지 정리되면 좋겠습니다.",
         _day(9, 11, 20))
    send("wref4", "oh", ["me"], ["kang", "choi"],
         "RE: 드라이버 API 스펙 문서 정리 요청",
         "동의합니다. 제가 이번 주에 병합하겠습니다.\n"
         "다만 ioctl 표는 분량이 커서 하위 페이지로 남기는 편이 나을 것 같습니다.",
         _day(9, 16, 5), reply_to="wref3")
    send("wref5", "kang", ["me", "oh"], ["choi"],
         "RE: 드라이버 API 스펙 문서 정리 요청",
         "컴파일러 쪽 인터페이스 절도 같이 옮기겠습니다.",
         _day(8, 9, 45), reply_to="wref4")

    # ── 자동 리포트: 정적분석 · 회귀 커버리지 · OSS 라이선스 · 성능 ────────
    for i, (day, high, med) in enumerate(
            [(26, 0, 11), (19, 1, 12), (12, 2, 14), (5, 2, 9)]):
        send(f"scan{i}", "scan", ["me"], [],
             f"[정적분석] NPX-200 SDK 주간 리포트 — High {high}, Medium {med}",
             f"분석 대상: npx200-sdk (main)\n"
             f"신규 결함: High {high} · Medium {med} · Low {rng.randint(20, 40)}\n"
             f"대시보드: http://scan.nurisoft.co.kr/npx200/report/{100 + i}\n\n"
             "이 리포트는 매주 월요일 자동 발송됩니다.",
             _day(day, 6, rng.randint(0, 59)), sig=False)
    for i, (day, cov) in enumerate([(27, 71.2), (20, 73.8), (13, 74.1), (6, 76.5)]):
        send(f"cicov{i}", "ci", ["me"], [],
             f"[CI] 주간 회귀 커버리지 리포트 — 라인 {cov}%",
             f"RTL 회귀 커버리지: 라인 {cov}% · 브랜치 {cov - 9:.1f}%\n"
             f"목표(80%) 대비 {80 - cov:.1f}%p 미달\n"
             f"상세: http://ci.nurisoft.co.kr/coverage/w{30 + i}",
             _day(day, 5, rng.randint(0, 59)), sig=False)
    send("oss0", "scan", ["me"], [],
         "[OSS] 라이선스 스캔 결과 — 확인 필요 1건",
         "npx200-sdk 의존성에서 라이선스 확인이 필요한 항목이 발견되었습니다.\n\n"
         "패키지: fastnn-utils 0.4.2\n라이선스: GPL-3.0 (의심)\n"
         "위치: third_party/fastnn\n\n"
         "법무 검토가 필요할 수 있습니다.",
         _day(11, 7, 12), sig=False)
    for i, (day, ms) in enumerate([(21, 18.4), (14, 17.9), (7, 19.6), (2, 17.2)]):
        send(f"perf{i}", "ci", ["me"], [],
             f"[Perf] 야간 벤치마크 — ResNet50 {ms}ms/frame",
             f"NPX-200 B0 야간 벤치마크 결과\n"
             f"ResNet50: {ms} ms/frame (목표 18.0)\n"
             f"MobileNetV3: {ms - 12:.1f} ms/frame\n"
             f"상세: http://ci.nurisoft.co.kr/perf/{200 + i}",
             _day(day, 4, rng.randint(0, 59)), sig=False)

    # ── 리포트를 참조한 사람 스레드 1: 정적분석 High 처리 (서지훈 — 브리핑용) ──
    send("sref0", "seo", ["me"], ["kim"],
         "정적분석 High 2건 처리 방안",
         "김도현님, 이번 주 정적분석 리포트에서 High 2건이 나왔습니다.\n"
         "둘 다 런타임 메모리 해제 경로(use-after-free 의심)라 SW 쪽 확인이\n"
         "필요합니다. 금요일까지 판단 부탁드립니다.",
         _day(11, 9, 30))
    send("sref1", "me", ["seo"], ["kim"],
         "RE: 정적분석 High 2건 처리 방안",
         "확인했습니다. 1건은 오탐입니다 — 해제 후 포인터를 NULL 로 밀어두는\n"
         "패턴을 분석기가 못 읽는 경우입니다.\n"
         "나머지 1건은 실제 결함으로 보여 핫픽스 준비하겠습니다.",
         _day(10, 14, 10), reply_to="sref0")
    send("sref2", "seo", ["me"], ["kim"],
         "RE: 정적분석 High 2건 처리 방안",
         "감사합니다. 오탐 건은 suppress 주석으로 정리해 주시면\n"
         "다음 리포트에서 빠집니다.",
         _day(10, 16, 40), reply_to="sref1")
    send("sref3", "me", ["seo"], ["kim"],
         "RE: 정적분석 High 2건 처리 방안",
         "핫픽스 머지했고 suppress 주석도 반영했습니다.\n"
         "다음 주 리포트에서 확인 부탁드립니다.",
         _day(6, 11, 25), reply_to="sref2")

    # ── 리포트 참조 스레드 2: 커버리지 미달 대응 (이서연) ──────────────────
    send("cref0", "lee", ["me", "jung"], ["kim"],
         "회귀 커버리지 목표 미달 — DV 계획 조정 필요",
         "김도현님, 이번 주 CI 커버리지 리포트가 76.5% 로 목표 80% 에 못 미칩니다.\n"
         "미달 구간이 대부분 전력 모드 전환 경로라 시나리오 추가가 필요합니다.\n"
         "런타임 쪽에서 진입 조건을 알려주시면 케이스를 짜겠습니다.",
         _day(5, 9, 50))
    send("cref1", "me", ["lee"], ["jung", "kim"],
         "RE: 회귀 커버리지 목표 미달 — DV 계획 조정 필요",
         "전력 모드는 4단계(P0~P3)이고 P2→P0 직행 경로가 예외입니다.\n"
         "그 경로만 별도 케이스로 두면 커버리지가 크게 오를 겁니다.",
         _day(5, 15, 30), reply_to="cref0")
    send("cref2", "jung", ["me", "lee"], ["kim"],
         "RE: 회귀 커버리지 목표 미달 — DV 계획 조정 필요",
         "RTL 쪽에서도 해당 경로 assertion 을 추가하겠습니다.",
         _day(4, 10, 15), reply_to="cref1")

    # ── 리포트 참조 스레드 3: OSS 라이선스 (법무) ──────────────────────────
    send("lref0", "yang", ["me"], ["kim", "park"],
         "[법무] fastnn-utils GPL 의심 건 확인 요청",
         "김도현님, OSS 스캔에서 fastnn-utils 0.4.2 가 GPL-3.0 으로 표시됐습니다.\n"
         "제품 바이너리에 정적 링크되는지 확인 부탁드립니다.\n"
         "정적 링크라면 대체 라이브러리 검토가 필요합니다.",
         _day(10, 13, 20))
    send("lref1", "me", ["yang"], ["kim", "park"],
         "RE: [법무] fastnn-utils GPL 의심 건 확인 요청",
         "확인했습니다. 빌드 스크립트 확인 결과 개발용 테스트 도구에서만\n"
         "쓰이고 제품 바이너리에는 포함되지 않습니다.\n"
         "다만 배포 스크립트에 예외 처리가 없어 정리하겠습니다.",
         _day(9, 17, 45), reply_to="lref0")
    send("lref2", "yang", ["me"], ["kim"],
         "RE: [법무] fastnn-utils GPL 의심 건 확인 요청",
         "확인 감사합니다. 배포 스크립트 정리되면 알려주세요.\n"
         "그때 최종 의견 회신하겠습니다.",
         _day(9, 18, 10), reply_to="lref1")

    # ── 업무 스레드: 고객 PoC 일정 (한 번 뒤집힘 — '상충함' 데모) ──────────
    send("poc0", "kim", ["me", "kang"], ["choi"],
         "A사 PoC 데모 일정 안내",
         "A사 PoC 데모를 다음 달 12일로 잡았습니다.\n"
         "데모 항목은 실시간 객체 검출과 전력 측정 두 가지입니다.\n"
         "준비 상황 공유 부탁드립니다.",
         _day(16, 9, 20))
    send("poc1", "me", ["kim"], ["kang", "choi"],
         "RE: A사 PoC 데모 일정 안내",
         "런타임 쪽은 12일까지 준비 가능합니다.\n"
         "전력 측정 지그는 신다은 선임 쪽 일정 확인이 필요합니다.",
         _day(16, 14, 30), reply_to="poc0")
    send("poc2", "kim", ["me", "kang"], ["choi"],
         "RE: A사 PoC 데모 일정 안내 — 일정 변경",
         "고객사 요청으로 PoC 데모가 다음 달 5일로 앞당겨졌습니다.\n"
         "일주일 당겨진 만큼 데모 항목을 객체 검출 하나로 줄이겠습니다.\n"
         "전력 측정은 별도 자료로 대체합니다.",
         _day(8, 11, 5), reply_to="poc1")
    send("poc3", "kang", ["kim", "me"], ["choi"],
         "RE: A사 PoC 데모 일정 안내 — 일정 변경",
         "모델 쪽은 5일 기준으로도 문제없습니다.\n"
         "양자화 모델 정확도 자료는 미리 정리해 두겠습니다.",
         _day(8, 15, 50), reply_to="poc2")
    send("poc4", "me", ["kim", "kang"], ["choi"],
         "RE: A사 PoC 데모 일정 안내 — 일정 변경",
         "런타임도 5일 기준으로 맞추겠습니다.\n"
         "데모 시나리오 스크립트는 이번 주 중 공유하겠습니다.",
         _day(7, 9, 15), reply_to="poc3")

    # ── 업무 스레드: 전력/DVFS (신다은 — 새 인물, 브리핑 다양성) ───────────
    send("pwr0", "shin", ["me"], ["yoon"],
         "P2→P0 전환 시 전류 스파이크 관측",
         "김도현님, 전력 모드 P2 에서 P0 로 직행할 때 순간 전류가 스펙 대비\n"
         "1.4배까지 튑니다. 런타임에서 중간 단계를 거치도록 할 수 있을까요?",
         _day(13, 10, 40))
    send("pwr1", "me", ["shin"], ["yoon"],
         "RE: P2→P0 전환 시 전류 스파이크 관측",
         "가능합니다. 다만 P1 경유 시 지연이 3ms 정도 늘어납니다.\n"
         "실시간 경로에서는 부담이라 조건부로 적용하는 편이 좋겠습니다.",
         _day(13, 16, 20), reply_to="pwr0")
    send("pwr2", "shin", ["me"], ["yoon"],
         "RE: P2→P0 전환 시 전류 스파이크 관측",
         "3ms 면 데모 시나리오에는 영향 없습니다.\n"
         "우선 조건부로 넣고 실측해 보시죠.",
         _day(12, 9, 30), reply_to="pwr1")
    send("pwr3", "me", ["shin"], ["yoon"],
         "RE: P2→P0 전환 시 전류 스파이크 관측",
         "조건부 경유를 넣어 측정했습니다. 스파이크는 1.05배까지 내려왔고\n"
         "지연 증가는 2.8ms 로 예상 범위입니다.",
         _day(9, 14, 55), reply_to="pwr2")
    send("pwr4", "shin", ["me"], ["yoon", "kim"],
         "RE: P2→P0 전환 시 전류 스파이크 관측",
         "좋습니다. 이 설정으로 확정하고 위키에 반영하겠습니다.",
         _day(9, 17, 25), reply_to="pwr3")

    # ── 업무 스레드: PDK 2.1 마이그레이션 (박지현) ─────────────────────────
    send("pdk0", "park", ["me", "jung"], ["kim"],
         "PDK 2.1 마이그레이션 일정 공유",
         "파운드리에서 PDK 2.1 이 배포됐습니다. B0 는 2.0 으로 마감하고\n"
         "C0 부터 2.1 을 적용하는 방향을 제안드립니다.\n"
         "SW 쪽 영향이 있는지 확인 부탁드립니다.",
         _day(14, 11, 10))
    send("pdk1", "me", ["park"], ["jung", "kim"],
         "RE: PDK 2.1 마이그레이션 일정 공유",
         "SW 영향은 크지 않습니다. 메모리 컨트롤러 타이밍 파라미터만\n"
         "런타임에서 읽어 쓰고 있어 값 갱신이면 됩니다.",
         _day(14, 15, 5), reply_to="pdk0")
    send("pdk2", "jung", ["park", "me"], ["kim"],
         "RE: PDK 2.1 마이그레이션 일정 공유",
         "RTL 은 재합성이 필요합니다. C0 일정에 2주 정도 반영해 주세요.",
         _day(13, 9, 55), reply_to="pdk1")

    # ── 단발: FYI·참고 공유(요약 게이트·큐 오염 방지 확인용) ───────────────
    fyi = [
        ("choi", "[회의록] 주간 개발 동기화 (7월 3주차)",
         "지난주 주요 결정과 액션 아이템 공유드립니다.\n자세한 내용은 위키를 참고하세요.", 4),
        ("choi", "[회의록] 주간 개발 동기화 (7월 2주차)",
         "지난주 주요 결정과 액션 아이템 공유드립니다.", 11),
        ("kang", "FYI: 컴파일러 릴리즈 노트 v1.4",
         "참고만 부탁드립니다. 별도 조치는 필요 없습니다.", 6),
        ("oh", "++ 수신인 추가",
         "관련자 추가합니다.", 3),
        ("park", "FYI: 파운드리 셔틀 캘린더 업데이트",
         "3분기 셔틀 일정이 갱신됐습니다. 참고 부탁드립니다.", 8),
        ("gm", "사무용품 신청 마감 안내",
         "이번 달 사무용품 신청은 금요일 마감입니다.", 5),
    ]
    for i, (who, subj, body, day) in enumerate(fyi):
        send(f"fyi{i}", who, ["me"], [], subj, body,
             _day(day, rng.choice([9, 13, 17]), rng.randint(0, 59)))

    # ── 알림 볼륨 보강 — 실환경은 자동 알림이 사람 메일보다 많다 ───────────
    more_pages = [
        ("검증 시나리오 목록", "lee", 25), ("컴파일러 패스 설계", "kang", 23),
        ("보안 위협 모델", "seo", 22), ("MPW 셔틀 일정표", "park", 18),
        ("주간 리스크 보드", "choi", 16), ("전력 모드 전환 시퀀스", "shin", 14),
        ("검증 시나리오 목록", "lee", 9), ("주간 리스크 보드", "choi", 1),
    ]
    for i, (page, who, day) in enumerate(more_pages, start=len(conf_pages)):
        name = _PEOPLE[who][0]
        send(f"conf{i}", "conf", ["me"], [],
             f"[Confluence] {name} 님이 \"{page}\" 페이지를 수정했습니다",
             f"{name} 님이 페이지를 수정했습니다.\n\n페이지: {page}\n"
             f"공간: NPX-200 개발\n바로가기: {WIKI}/npx200/{40 + i}\n\n"
             "이 알림은 자동 발송되었습니다.",
             _day(day, rng.choice([8, 12, 15]), rng.randint(0, 59)), sig=False)

    for i in range(8):
        day = rng.randint(1, 28)
        st = rng.choice(["SUCCESS", "SUCCESS", "SUCCESS", "UNSTABLE"])
        send(f"cinight{i}", "ci", ["me"], [],
             f"[CI] npx200-sdk nightly #{880 + i} {st}",
             f"빌드 #{880 + i}: {st}\n"
             f"테스트 {rng.randint(1180, 1240)}건 중 실패 "
             f"{0 if st == 'SUCCESS' else rng.randint(1, 4)}건\n"
             f"상세: http://ci.nurisoft.co.kr/job/npx200-sdk/{880 + i}",
             _day(day, 3, rng.randint(0, 59)), sig=False)

    for i, (subj, day) in enumerate([
            ("[공지] 사내 시스템 정기 점검 안내 (토요일 02:00~06:00)", 12),
            ("[공지] 보안 교육 이수 기한 안내", 7),
            ("[공지] 하계 휴가 신청 시스템 오픈", 20),
            ("[공지] 사옥 출입 게이트 교체 작업 안내", 3)]):
        send(f"notice{i}", "sys", ["me"], [], subj,
             "자세한 내용은 사내 포털을 참고하시기 바랍니다.\n"
             "문의: 총무팀 (내선 1234)",
             _day(day, 9, rng.randint(0, 59)), sig=False)

    # ── 업무 스레드: B0 실장 이슈 (윤성호 — 기한 있는 요청) ────────────────
    send("mnt0", "yoon", ["me", "jung"], ["kim"],
         "B0 실장 보드 클럭 지터 측정 결과",
         "김도현님, B0 보드에서 PLL 출력 지터가 스펙 상한에 근접합니다.\n"
         "런타임에서 클럭 소스 전환 빈도를 줄일 수 있는지 확인 부탁드립니다.\n"
         "다음 주 화요일까지 회신 주시면 실장 일정에 반영하겠습니다.",
         _day(6, 10, 5))
    send("mnt1", "me", ["yoon"], ["jung", "kim"],
         "RE: B0 실장 보드 클럭 지터 측정 결과",
         "확인했습니다. 전환은 전력 모드 변경 시에만 일어나므로\n"
         "빈도 자체는 낮습니다. 다만 측정 구간이 P2 구간과 겹치는지\n"
         "로그를 대조해 보겠습니다.",
         _day(5, 16, 30), reply_to="mnt0")
    send("mnt2", "yoon", ["me"], ["jung", "kim"],
         "RE: B0 실장 보드 클럭 지터 측정 결과",
         "로그 대조 결과 공유 부탁드립니다. 겹친다면 측정 조건을 바꾸겠습니다.",
         _day(4, 9, 20), reply_to="mnt1")

    # ── 업무 스레드: 온보딩 (최하늘 — 가벼운 요청) ─────────────────────────
    send("onb0", "choi", ["me"], [],
         "신입 온보딩 문서 검토 요청",
         "김도현님, 다음 달 합류하는 SW 신입 온보딩 문서를 정리했습니다.\n"
         "런타임 파트 설명이 정확한지 봐주실 수 있을까요? 급하지 않습니다.",
         _day(10, 15, 40))
    send("onb1", "me", ["choi"], [],
         "RE: 신입 온보딩 문서 검토 요청",
         "런타임 파트 읽었습니다. 빌드 절차가 구버전 기준이라\n"
         "수정 포인트 세 곳 코멘트 남겼습니다.",
         _day(8, 11, 50), reply_to="onb0")
    send("onb2", "choi", ["me"], [],
         "RE: 신입 온보딩 문서 검토 요청",
         "반영했습니다. 감사합니다!",
         _day(8, 13, 5), reply_to="onb1")

    # ── 내가 시작한 공유(발신 비중·주간보고 재료) ──────────────────────────
    for i, (subj, body, day, to) in enumerate([
            ("런타임 1.5 릴리즈 노트 공유",
             "런타임 1.5 를 태깅했습니다. 주요 변경은 전력 모드 조건부 경유와\n"
             "DMA 캐시 동기화 수정입니다.", 3, ["kang", "oh", "lee"]),
            ("주간 SW 진행 상황 공유",
             "이번 주 런타임 쪽 진행 상황 공유드립니다.\n"
             "핫픽스 2건 머지, 커버리지 케이스 추가 작업 중입니다.", 5, ["kim"]),
            ("데모 시나리오 스크립트 초안",
             "PoC 데모용 스크립트 초안입니다. 검토 의견 주시면 반영하겠습니다.",
             6, ["kang", "kim"])]):
        send(f"mine{i}", "me", to, ["choi"], subj, body,
             _day(day, rng.choice([10, 14, 18]), rng.randint(0, 59)))


def _scenario() -> list[_Mail]:
    mails: list[_Mail] = []
    rng = random.Random(42)

    def send(key, sender, to, cc, subject, body, when, reply_to=None,
             attachments=None, sig=True):
        m = _Mail(key, sender, to, cc, subject, body, when, attachments, sig)
        if reply_to is not None:
            parent = next(x for x in mails if x.key == reply_to)
            m.parent = parent
            m.full_body += _quote_block(parent)
        mails.append(m)
        return m

    # ═════════════════ 긴 기술 디스커션 1: 타이밍 클로저 (14통, 10일) ═══════
    # NPX-200 B0 백엔드 — CTS 후 hold 위반 대량 발생 → 코너 분석 → ECO 전략
    # 핑퐁 → 결정("hold ECO 분리, 넷리스트 프리즈 7/21") → 후속. '++' 한 줄 포함.
    tc = [  # (sender, to, cc, body, attachments)
        ("yoon", ["jung", "me"], ["kim"],
         "CTS 이후 STA 돌린 결과 공유합니다.\n"
         "ss0p72v_m40c 코너에서 hold 위반 1,847건 나왔습니다. 대부분 NPU 코어\n"
         "MAC 어레이 쪽 스캔 체인입니다. 리포트 첨부합니다.",
         ["sta_hold_ss_m40c.rpt"]),
        ("jung", ["yoon"], ["me", "kim"],
         "스캔 체인이면 기능 경로는 아니네요. useful skew 로 흡수 가능한 규모인지,\n"
         "아니면 hold 버퍼 삽입으로 가야 하는지 판단이 필요합니다.\n"
         "위반 slack 분포(worst/median) 뽑아 주시겠어요?", None),
        ("yoon", ["jung"], ["me", "kim"],
         "분포 뽑았습니다. worst -87ps, median -23ps 입니다.\n"
         "-50ps 이하가 214건이라 skew 만으로는 어렵고 버퍼 삽입 병행이 필요해 보입니다.",
         None),
        ("me", ["yoon", "jung"], ["kim"],
         "SW 관점 하나만 확인 부탁드립니다. 스캔 체인 ECO 가 BIST 패턴 재생성을\n"
         "유발하는지요? 재생성이면 테스트 벡터 릴리즈 일정에 영향이 있습니다.", None),
        ("yoon", ["me"], ["jung", "kim"],
         "체인 순서는 안 바뀌고 버퍼만 들어가서 패턴 재생성은 불필요합니다.\n"
         "ATPG 재실행만 하면 됩니다. 하루 작업입니다.", None),
        ("lee", ["yoon", "jung"], ["me", "kim"],
         "DV 쪽 우려 하나 — 지난 A0 때 hold ECO 와 기능 ECO 를 한 넷리스트에\n"
         "섞었다가 등가성 검증(LEC)이 이틀 밀렸습니다. 이번엔 분리하면 좋겠습니다.",
         None),
        ("jung", ["lee"], ["yoon", "me", "kim"],
         "동의합니다. 기능 ECO(FIFO depth 수정)는 이번 주 내 프리즈하고,\n"
         "hold ECO 는 그 위에 별도 커밋으로 얹는 순서를 제안합니다.", None),
        ("choi", ["jung"], ["yoon", "me", "kim", "park"],
         "++박지현 책임", None),              # 수신인 추가 한 줄 (trivial 데모)
        ("park", ["jung"], ["yoon", "me", "kim", "choi"],
         "구매입니다. 파운드리 쪽 tape-in 슬롯 기준으로는 넷리스트 프리즈가\n"
         "7/21(화)을 넘기면 다음 셔틀로 밀립니다. 참고 부탁드립니다.", None),
        ("kim", ["jung", "yoon", "lee", "me"], ["choi", "park"],
         "정리합니다. 기능 ECO 는 7/16 프리즈, hold ECO 는 분리 커밋으로 7/21\n"
         "넷리스트 프리즈 확정합니다. 근거: LEC 리스크 분리와 셔틀 슬롯 마감.\n"
         "각 파트 일정 역산해서 내일까지 회신 바랍니다.", None),
        ("yoon", ["kim"], ["jung", "me", "lee"],
         "hold ECO 7/18 완료 가능합니다. ATPG 재실행 포함입니다.", None),
        ("jung", ["kim"], ["yoon", "me", "lee"],
         "기능 ECO 7/15 완료로 잡겠습니다. LEC 는 16일 오전 예약했습니다.", None),
        ("me", ["kim"], ["jung", "yoon", "lee"],
         "테스트 벡터는 ATPG 산출물 받는 대로 D+1 릴리즈 가능합니다.\n"
         "브링업 보드 쪽 준비는 별도 스레드로 공유하겠습니다.", None),
        ("lee", ["kim"], ["jung", "yoon", "me"],
         "LEC 스크립트 사전 점검 완료했습니다. 16일 슬롯 문제없습니다.", None),
    ]
    for i, ((sender, to, cc, body, att), when) in enumerate(
            zip(tc, _sched(10, len(tc), rng))):
        subj = ("NPX-200 B0 타이밍 클로저 — hold 위반 대응" if i == 0
                else "RE: NPX-200 B0 타이밍 클로저 — hold 위반 대응")
        send(f"tc{i}", sender, to, cc, subj, body, when,
             reply_to=f"tc{i-1}" if i else None, attachments=att)

    # ═════════════════ 긴 기술 디스커션 2: CVE 보안 대응 (12통, 6일) ════════
    # 시큐어 모니터 SMC 핸들러 OOB write → 재현 → 패치 → 서명/배포 → 오늘 내
    # 재발 방지안. 야간(23시) 별칭(dhkim@) 발신 1통 포함 — §4·별칭 재분류 데모.
    cve = [
        ("seo", ["me", "oh"], ["kim"],
         "보안팀 내부 점검에서 시큐어 모니터 취약점을 확인했습니다.\n"
         "CVE-2026-31337 로 예약했습니다. SMC 핸들러의 길이 검증 누락으로\n"
         "비보안 월드에서 시큐어 메모리에 OOB write 가 가능합니다.\n"
         "심각도는 CVSS 8.4 로 산정했습니다. 대응 논의가 필요합니다.", None),
        ("oh", ["seo"], ["me", "kim"],
         "재현 확인했습니다. smc_handle_mem_share() 에서 페이지 수 인자를\n"
         "검증 없이 memcpy 길이로 씁니다. PoC 는 EL1 권한 필요라 원격 악용은\n"
         "어렵지만 루팅된 단말에서는 치명적입니다.", None),
        ("me", ["seo", "oh"], ["kim"],
         "고객 출하 물량 기준 영향 범위 정리했습니다.\n"
         "FW 2.3 이상 전 버전 해당, OTA 가능 물량 94%. 나머지 6%는 오프라인\n"
         "업데이트 안내가 필요합니다. 패치 우선순위 상으로 제안합니다.", None),
        ("seo", ["me", "oh"], ["kim"],
         "패치 초안입니다. 페이지 수 상한 검증 + 오버플로 체크 2중화했습니다.\n"
         "리뷰 부탁드립니다. diff 첨부.", ["sm_patch_v1.diff"]),
        ("oh", ["seo"], ["me", "kim"],
         "리뷰 코멘트 2건 남겼습니다. 검증 위치가 TOCTOU 창을 남깁니다 —\n"
         "매핑 락 안쪽으로 옮기시죠. 나머지는 이상 없습니다.", None),
        ("seo", ["oh", "me"], ["kim"],
         "반영했습니다. v2 첨부합니다. 락 안쪽 검증으로 옮기고 회귀 테스트\n"
         "3건 추가했습니다.", ["sm_patch_v2.diff", "제목 없는 첨부 파일 00001.png"]),
        ("me2", ["seo", "oh"], ["kim"],
         "긴급 서명 파이프라인 예약 완료했습니다 (모바일에서 보냅니다).\n"
         "내일 오전 HSM 키 세리머니 후 릴리즈 서명 진행하겠습니다.", None),
        ("kim", ["me", "seo", "oh"], [],
         "고객 통보 문구는 법무 검토가 필요합니다. 기술 요약 한 장으로\n"
         "정리해 주시면 제가 법무에 넘기겠습니다. 공개 일정은 패치 배포\n"
         "14일 후로 하겠습니다 — 이 일정으로 확정합니다.", None),
        ("seo", ["kim"], ["me", "oh"],
         "기술 요약 초안 첨부합니다. CVSS 산정 근거 포함입니다.",
         ["CVE-2026-31337_요약.docx"]),
        ("oh", ["me"], ["seo", "kim"],
         "OTA 스테이징 배포 시작했습니다. 단계 배포 1% → 10% → 100%,\n"
         "각 단계 24시간 모니터링입니다.", None),
        ("lee", ["me", "oh"], ["seo", "kim"],
         "스테이징 1% 구간 크래시 리포트 0건, 부팅 실패 0건입니다.\n"
         "10% 확대 진행해도 될 것 같습니다.", None),
        ("me", ["kim", "seo", "oh"], ["lee"],
         "재발 방지안 공유드립니다.\n"
         "1) SMC 핸들러 전수 퍼징을 CI 에 통합 (주 1회 → 커밋마다)\n"
         "2) 시큐어 모니터 정적분석 룰셋에 길이 검증 패턴 추가\n"
         "3) 분기별 외부 침투테스트 범위에 TEE 포함\n"
         "상세 계획은 문서로 정리해 다음 주 보안 리뷰에서 다루겠습니다.", None),
    ]
    cve_when = _sched(6, len(cve), rng)
    cve_when[6] = _day(3, 23, 10)          # 야간 발신 (별칭) — §4 데모
    for i, ((sender, to, cc, body, att), when) in enumerate(zip(
            [(x[0], x[1], x[2], x[3], x[4]) for x in cve], cve_when)):
        subj = ("[보안] CVE-2026-31337 시큐어 모니터 취약점 대응" if i == 0
                else "RE: [보안] CVE-2026-31337 시큐어 모니터 취약점 대응")
        send(f"cve{i}", sender, to, cc, subj, body, when,
             reply_to=f"cve{i-1}" if i else None, attachments=att)

    # ═════════════════ 오늘 결정: INT8 양자화 방식 (5통, 3일) ═══════════════
    # 수확(harvest) 데모의 주 재료 — 오늘 "QAT 로 확정합니다" 선언.
    qt = [
        ("kang", ["me", "kim"], [],
         "NPX-200 컴파일러 INT8 PTQ 결과가 안 좋습니다.\n"
         "디텍션 모델 mAP 이 FP16 대비 3.2%p 떨어집니다. 고객 수용 기준(1%p)\n"
         "초과라 방식 재검토가 필요합니다. 민감도 분석 결과 첨부합니다.",
         ["ptq_sensitivity.xlsx"]),
        ("me", ["kang"], ["kim"],
         "분석 봤습니다. 첫 conv 와 마지막 head 레이어가 하락분의 80%네요.\n"
         "두 가지 대안이 있습니다.\n"
         "A) 민감 레이어만 FP16 유지 (mixed precision) — 컴파일러 수정 1주\n"
         "B) QAT 재학습 — 정확도 회복 확실하지만 고객 학습 파이프라인 필요\n"
         "고객이 재학습 가능한지가 관건입니다.", None),
        ("kim", ["kang", "me"], [],
         "고객 미팅에서 확인했습니다. 재학습 파이프라인 보유하고 있고\n"
         "학습 데이터 제공도 가능하답니다. QAT 시 일정과 비용 산정 부탁합니다.", None),
        ("kang", ["kim"], ["me"],
         "QAT 산정입니다. 툴킷 연동 2주 + 레퍼런스 모델 재학습 1주.\n"
         "mixed precision 대비 3주 더 걸리지만 mAP 회복은 0.3%p 이내로\n"
         "확실합니다. 벤치 데이터 첨부합니다.", ["qat_bench.xlsx"]),
        ("kim", ["kang", "me"], [],
         "양자화는 QAT 로 확정합니다. 근거: 고객 재학습 파이프라인이 확보되어\n"
         "있고 정확도 회복이 확실하기 때문입니다. mixed precision 은 QAT 실패\n"
         "시 폴백으로만 유지합니다. 강선임이 툴킷 연동 킥오프 잡아 주세요.", None),
    ]
    for i, ((sender, to, cc, body, att), when) in enumerate(zip(
            [(x[0], x[1], x[2], x[3], x[4]) for x in qt], _sched(3, len(qt), rng))):
        subj = ("INT8 양자화 정확도 회귀 — 방식 결정 필요" if i == 0
                else "RE: INT8 양자화 정확도 회귀 — 방식 결정 필요")
        send(f"qt{i}", sender, to, cc, subj, body, when,
             reply_to=f"qt{i-1}" if i else None, attachments=att)

    # ═════════════════ 최근 핵심 단발·짧은 스레드 ══════════════════════════

    # 사양 검토 → 오늘 내가 회신 완료
    send("spec1", "oh", ["me"], ["kim"], "NPU 드라이버 API 스펙 v0.9 검토 요청",
         "드라이버 API 스펙 v0.9 입니다. 특히 DMA 버퍼 소유권 규약(4장)과\n"
         "에러 코드 체계(부록 A) 검토 부탁드립니다.",
         _day(1, 11), attachments=["npu_driver_api_v0.9.docx"])
    send("spec2", "me", ["oh"], ["kim"], "RE: NPU 드라이버 API 스펙 v0.9 검토 요청",
         "검토 완료했습니다.\n\n4.3절 버퍼 반환 시점이 인터럽트 컨텍스트 기준인지\n"
         "명시가 필요합니다. 에러 코드는 기존 SDK 와 충돌 없음 확인했습니다.\n"
         "나머지는 이상 없습니다.", _day(0, 15, 40), reply_to="spec1")

    # 오늘 온 요청 — 미답변 D+0 + 기한
    send("brd1", "lee", ["me"], [], "브링업 보드 UART 로그 분석 요청",
         "도현님,\n\nB0 브링업 보드에서 부팅 중 간헐 행이 재현됩니다.\n"
         "UART 로그 첨부합니다. PLL 락 대기 쪽으로 보이는데 확인 부탁드립니다.\n"
         "내일 오전까지 회신 주시면 오후 디버깅 세션에 반영하겠습니다.",
         _day(0, 14, 10), attachments=["uart_boot_hang.log"])

    # 이틀째 미답변 + 금요일 기한
    send("keyc1", "seo", ["me"], [], "시큐어부트 키 세리머니 일정 확정 요청",
         "도현님,\n\n양산 키 세리머니 참관인 2인이 필요합니다.\n"
         "도현님과 오책임으로 생각 중인데 가능 여부 판단 부탁드립니다.\n"
         "HSM 룸 예약 때문에 이번 주 금요일까지 회신 주시면 됩니다.",
         _day(2, 16), attachments=["키세리머니_절차서.pdf"])

    # 결정 필요 (decide): 그룹 승인 요청
    send("mpw1", "park", ["me", "jung", "kim"], [], "MPW 셔틀 탑승 승인 요청",
         "도현님,\n\n3분기 MPW 셔틀 견적 첨부합니다. 테스트 칩 2종 탑승 기준\n"
         "1.8억 원입니다. 예산 규모가 커서 팀 차원 결정이 필요합니다.\n"
         "가부 회신 부탁드립니다.", _day(2, 10), attachments=["mpw_견적_2026Q3.xlsx"])

    # 내가 넘긴 공 (stalled_mine): 내 재검토 요청에 무응답
    send("dma1", "oh", ["me"], [], "DMA 대역폭 측정 결과 초안",
         "도현님,\n\nNPX-200 DMA 대역폭 측정 초안입니다. 확인 부탁드립니다.",
         _day(6, 9), attachments=["dma_bw_초안.xlsx"])
    send("dma2", "me", ["oh"], [], "RE: DMA 대역폭 측정 결과 초안",
         "오책임님,\n\n초안 봤습니다. 측정이 burst 길이 16 고정인데 고객 워크로드는\n"
         "4/8 혼합입니다. 혼합 조건으로 재측정 부탁드립니다.\n"
         "회신 주실 수 있을까요?", _day(5, 15), reply_to="dma1")

    # 멈춘 스레드 (stalled_thread): 그룹 논의 8일째 무활동
    send("conv1", "jung", ["me", "kim"], ["oh"], "RTL 코딩 컨벤션 개정 논의",
         "린트 룰셋 개정안입니다. 현행 룰이 신규 툴 버전과 충돌하는 항목이\n"
         "12건 있어 정리가 필요합니다.", _day(9, 10))
    send("conv2", "kim", ["jung", "oh"], ["me"], "RE: RTL 코딩 컨벤션 개정 논의",
         "이 건은 오책임 의견도 들어가야 할 것 같은데 어떻게 진행할까요?\n"
         "다음 주에 다시 논의하시죠.", _day(8, 14), reply_to="conv1")

    # 마스크 일정 문의 → 리치 HTML 답장 (표·추적픽셀·분할 인용 라벨)
    send("mask1", "me", ["park"], [], "MPW 마스크 일정 문의",
         "지현님,\n\nMPW 마스크 tape-in 일정이 어떻게 되는지 확인 부탁드립니다.\n"
         "hold ECO 일정 역산에 필요합니다.", _day(1, 9))
    send("mask2", "park", ["me"], [], "RE: MPW 마스크 일정 문의",
         "도현님,\n\ntape-in 은 8/22 확정입니다. GDS 는 8/20 까지 제출해 주셔야\n"
         "합니다. 탑승 블록 목록 회신 부탁드립니다.",
         _day(0, 13, 20), reply_to="mask1")

    # 킥오프 공지 (어제)
    send("kick1", "kim", ["me", "jung", "lee", "yoon", "oh"], [],
         "NPX-200 B0 킥오프 일정",
         "B0 킥오프를 다음 주 화요일 10시로 하겠습니다.\n장소는 대회의실입니다.",
         _day(1, 17))

    # 참조만 걸린 메일 (미답변 대상 아님)
    send("fyiref", "choi", ["jung"], ["me"], "주간 일정표 공유",
         "이번 주 일정표 공유드립니다. 참고 부탁드립니다.", _day(0, 9, 30))

    # 마크다운 텍스트 메일 (#21 토글 데모 — body_html 없음)
    send("mdmail", "jung", ["me"], [], "B0 브링업 체크리스트 초안",
         "브링업 체크리스트 초안입니다. 마크다운으로 정리했습니다.\n\n"
         "## 전원 시퀀스\n"
         "- [ ] VDD_CORE 0.72V 확인\n"
         "- [ ] PLL 락 타임 < 100us\n"
         "- [ ] **전류 프로파일** 기록\n\n"
         "## 부팅\n"
         "1. ROM 부트 로그 확인\n"
         "2. 시큐어부트 서명 검증 통과\n"
         "3. DRAM 트레이닝 결과 저장\n\n"
         "| 단계 | 담당 | 상태 |\n"
         "|---|---|---|\n"
         "| 전원 | 윤성호 | 대기 |\n"
         "| 부팅 | 이서연 | 대기 |\n\n"
         "```\nuart_cfg --baud 921600 --flow none\n```\n"
         "수정 의견 주시면 반영하겠습니다.", _day(1, 13))

    # 장문 1통 (1200자+) — 요약 게이트(내용 우회) 데모
    send("long1", "me", ["kim"], [], "온디바이스 LLM 데모 회고와 차기 계획",
         "팀장님,\n\n지난주 전시회 온디바이스 LLM 데모 회고와 차기 계획입니다.\n\n"
         "1. 성과. 3B 모델을 NPX-100 보드에서 실시간 구동(12 tok/s)한 것은\n"
         "경쟁사 대비 처음이었고, 부스 방문 고객 34개사 중 11개사가 후속 미팅을\n"
         "요청했습니다. 특히 오프라인 음성 비서 시나리오에 대한 반응이 좋았고,\n"
         "통신 불가 환경(산업 현장·차량)에서의 수요가 검증되었습니다.\n\n"
         "2. 한계. 첫째, 컨텍스트 4K 초과 시 프리필 지연이 3초를 넘어 대화형\n"
         "체감이 급락합니다. KV 캐시를 DRAM 이 아니라 온칩 SRAM 에 분할 상주시키는\n"
         "구조 개선이 필요합니다. 둘째, INT4 가중치 압축 시 한국어 성능 하락이\n"
         "영어보다 큽니다(퍼플렉시티 +18% vs +9%). 한국어 코퍼스 기반 캘리브레이션\n"
         "셋을 자체 구축해야 합니다. 셋째, 발열 — 15분 연속 추론 시 스로틀링으로\n"
         "토큰 속도가 30% 하락했습니다. DVFS 정책을 추론 페이즈(프리필/디코드)\n"
         "인지형으로 바꾸면 개선 여지가 있습니다.\n\n"
         "3. 차기 계획 제안. (a) NPX-200 에서 7B 모델 15 tok/s 목표 — 메모리\n"
         "대역폭 산정 근거 별첨. (b) 프리필 전용 가속 경로(배치 어텐션) 컴파일러\n"
         "지원 — 강선임과 범위 협의 완료. (c) 3분기 내 한국어 캘리브레이션 셋 v1\n"
         "구축 — 외부 코퍼스 라이선스 검토 필요. (d) 데모 프레임워크를 고객 배포\n"
         "가능한 SDK 예제로 승격 — 문서화 리소스 1인 필요합니다.\n\n"
         "위 (c), (d)는 리소스 배정이 필요해 다음 주간회의 안건으로 올리겠습니다.\n"
         "상세 데이터는 첨부 참고 바랍니다.",
         _day(2, 18), attachments=["llm_demo_회고.pptx"])

    # 인라인 이미지 메일 — 최근(이미지 표시) / 보존 기간(demo 14일) 경과(마커)
    send("imgnew", "lee", ["me"], [], "브링업 부팅 파형 공유",
         "브링업 보드 부팅 파형 공유드립니다. 행 케이스는 PLL 락 직후 리셋이\n"
         "관측됩니다. 분석 의견 부탁드립니다.", _day(1, 15, 20))
    send("imgold", "jung", ["me"], [], "레이아웃 스냅샷 공유(구건)",
         "지난달 레이아웃 스냅샷 공유합니다. 다음 리비전에서 매크로 배치가\n"
         "바뀔 예정입니다.", _day(20, 11, 10))

    # 야간 발신 단발 (§4 야간·주말 데모 보강)
    send("night1", "me", ["lee"], [], "RE: nightly 회귀 크래시 분석",
         "krash 덤프 확인했습니다. 널 포인터가 아니라 스택 오버플로입니다.\n"
         "내일 오전에 스택 사이즈 조정 패치 올리겠습니다.", _day(8, 22, 40))

    # ═════════════════ 중간 스레드 (최근 1주) ══════════════════════════════

    mid_threads = [
        ("wk", "주간회의 안건 취합", 4, [
            ("choi", ["me", "jung", "lee", "yoon"], [], "이번 주 회의 안건 회신 부탁드립니다."),
            ("jung", ["choi"], [], "안건: B0 타이밍 클로저 일정 공유."),
            ("me",   ["choi"], [], "안건: CVE 대응 현황과 재발 방지안 리소스."),
            ("choi", ["me", "jung", "lee", "yoon"], [], "안건 마감합니다. 회의록으로 정리하겠습니다."),
        ]),
        ("gpu", "학습용 GPU 서버 증설 검토", 6, [
            ("choi", ["me"], ["kim"], "QAT 재학습용 GPU 서버 증설 견적입니다. 8카드 2대 기준입니다."),
            ("me",   ["choi"], ["kim"], "카드당 메모리가 40GB 면 7B 학습엔 부족합니다. 80GB 로 상향 필요합니다."),
            ("choi", ["me"], ["kim"], "80GB 상향 시 예산 35% 초과합니다. 조정안 주시겠어요?"),
            ("me",   ["choi"], ["kim"], "2대를 1대로 줄이고 80GB 로 가시죠. QAT 는 순차 실행으로 감당됩니다."),
            ("choi", ["me"], ["kim"], "1대 80GB 안으로 수정해서 품의 올리겠습니다."),
        ]),
        ("iv", "보안 엔지니어 경력 채용 인터뷰 일정", 3, [
            ("gm",  ["seo"], ["me"], "다음 주 인터뷰 가능 시간 회신 부탁드립니다."),
            ("seo", ["gm"], ["me"], "화요일 오후 가능합니다."),
            ("gm",  ["seo"], ["me"], "화요일 15시로 확정하겠습니다."),
        ]),
    ]
    for prefix, subject, start_day, script in mid_threads:
        for i, ((sender, to, cc, body), when) in enumerate(
            zip(script, _sched(start_day, len(script), rng))
        ):
            subj = subject if i == 0 else f"RE: {subject}"
            send(f"{prefix}{i}", sender, to, cc, subj, body, when,
                 reply_to=f"{prefix}{i-1}" if i else None)

    # ═════════════════ 최근 단발 업무 메일 ═════════════════════════════════

    oneoffs = [
        ("seo",  ["me", "jung", "oh"], [], "기술 세미나 발표자료 공유",
         "지난주 사이드채널 공격 동향 세미나 자료 공유드립니다.", 7, 15,
         ["세미나_사이드채널.pdf"]),
        ("gm",   ["me", "jung", "lee", "choi", "yoon"], [], "사무실 좌석 이동 안내",
         "다음 주 월요일 좌석 이동이 있습니다. 배치도 참고 바랍니다.", 6, 11, ["배치도.xlsx"]),
        ("me",   ["kim"], [], "고객사 기술미팅 출장 보고",
         "지난주 고객사 온디바이스 AI 기술미팅 출장 보고서 제출합니다.", 5, 17,
         ["출장보고_0705.docx"]),
        ("lee",  ["jung"], ["me"], "에뮬레이터 예약 현황 공유",
         "이번 달 팔라디움 예약 현황입니다. 참고하세요.", 4, 13, None),
        ("me",   ["yoon"], [], "IR drop 해석 조건 문의",
         "B0 파워 그리드 IR drop 해석 조건 초안이 있을까요?", 3, 10, None),
        ("yoon", ["me"], [], "RE: IR drop 해석 조건 문의",
         "A0 기준 문서 공유드립니다. B0 은 개정 예정입니다.", 3, 14,
         ["ir_drop_조건_A0.pdf"]),
        ("choi", ["me", "jung", "lee"], [], "[회의록] NPX-200 주간회의",
         "지난주 회의록 공유드립니다.", 5, 9, ["회의록_0706.docx"]),
        ("lee",  ["jung"], ["me"], "회귀 테스트 팜 이전 안내",
         "시뮬레이션 팜 서버 이전으로 이번 주까지 잡 스크립트 경로 수정 바랍니다.", 2, 15, None),
        ("jung", ["me", "yoon"], [], "코드 리뷰 완료",
         "요청하신 npu-dma-fix 브랜치 리뷰 완료했습니다. 코멘트 확인 바랍니다.", 2, 11, None),
        ("kim",  ["me", "jung", "lee", "choi", "yoon"], [], "부서 회식 일정",
         "다음 주 목요일 저녁 회식입니다. 참석 여부 알려주세요.", 3, 16, None),
        ("me",   ["choi"], [], "RE: 부서 회식 일정",
         "참석합니다.", 3, 17, None),
        ("kang", ["me"], ["kim"], "컴파일러 릴리즈 노트 v2.4",
         "이번 릴리즈에 융합 커널 3종과 QAT 전처리 패스가 들어갔습니다.\n"
         "변경점 정리 첨부합니다.", 4, 10, ["compiler_v2.4_notes.md"]),
        ("park", ["kim"], ["me"], "파운드리 PDK 1.3 업데이트 접수",
         "PDK 1.3 이 릴리즈되었습니다. 표준셀 타이밍 모델 변경이 있어\n"
         "영향 검토 의견 취합 예정입니다.", 1, 10, None),
        ("gm",   ["me", "jung", "lee", "choi", "yoon", "kang"], [], "여름 휴가 계획 취합",
         "7월 휴가 계획을 이번 주까지 회신 바랍니다.", 2, 9, None),
        ("me",   ["gm"], [], "RE: 여름 휴가 계획 취합",
         "7/27~7/31 로 제출합니다.", 1, 11, None),
        ("oh",   ["me"], [], "SDK 예제 빌드 오류 제보",
         "고객 포럼에 SDK 0.9 예제 빌드 오류 제보가 2건 올라왔습니다.\n"
         "툴체인 버전 이슈로 보입니다. 확인 후 공유드리겠습니다.", 1, 13, None),
        ("kang", ["me", "oh"], [], "모델 zoo 벤치마크 갱신",
         "NPX-200 시뮬레이터 기준 모델 zoo 벤치마크 갱신했습니다.\n"
         "위키에 반영 완료: https://wiki.nurisoft.co.kr/npx/modelzoo", 2, 14, None),
        ("seo",  ["me"], ["oh"], "펌웨어 서명키 로테이션 공지",
         "분기 정기 키 로테이션을 다음 주 수요일 진행합니다.\n"
         "빌드 파이프라인 중단은 없습니다. 참고 바랍니다.", 4, 16, None),
    ]
    for i, (sender, to, cc, subject, body, day, hour, att) in enumerate(oneoffs):
        reply_to = None
        if subject.startswith("RE: "):
            base = subject[4:]
            parent = next(m for m in reversed(mails) if m.subject == base)
            reply_to = parent.key
        send(f"oo{i}", sender, to, cc, subject, body,
             _day(day, hour, rng.randint(0, 59)), reply_to=reply_to,
             attachments=att)

    # ═════════════ 한 달 밴드 (12~30일 전 — 오래된 스레드·단발) ═════════════
    # 원칙: 오래된 스레드는 마지막이 '나 아닌 사람'으로 종결되게 하여
    #       개입 큐(내가 넘긴 공·멈춘 스레드)를 오염시키지 않는다 — 명백히 끝난 논의.

    # ── 긴 오래된 디스커션: 모델 서빙 vLLM 전환 (11통, 30~18일 전) ──────────
    vllm = [
        ("kim",  ["me", "kang", "han"], [],
         "사내 모델 평가 파이프라인 서빙을 정해야 합니다.\n"
         "현행 자체 서버 유지와 vLLM 전환 중 의견 주세요."),
        ("kang", ["kim"], ["me", "han"],
         "vLLM 전환은 커스텀 샘플러 포팅 부담이 있습니다. 범위 산정이 먼저입니다."),
        ("me",   ["kim", "kang"], ["han"],
         "평가 파이프라인 API 는 그대로 두고 백엔드만 교체 가능합니다.\n"
         "실질 포팅은 샘플러 2종과 로깅 훅 정도로 추정합니다."),
        ("han",  ["kim"], ["me", "kang"],
         "인프라 관점으로는 vLLM 쪽이 GPU 활용률이 확실히 좋습니다.\n"
         "현행 대비 배치 처리량 2.3배 나옵니다. 벤치 첨부합니다."),
        ("me",   ["han"], ["kim", "kang"],
         "동의합니다. 1단계는 평가용 오프라인 배치만 전환하는 것을 제안합니다.\n"
         "온라인 데모 서버는 안정화 후 2단계로."),
        ("kang", ["me"], ["kim", "han"],
         "단계 전환이면 리스크가 관리됩니다. 샘플러 포팅은 제가 맡겠습니다."),
        ("me",   ["kang", "kim"], ["han"],
         "PoC 범위 정리했습니다. 계획서 초안 첨부합니다.",),
        ("kim",  ["me"], ["kang", "han"],
         "계획서 잘 봤습니다. 단계 전환으로 확정합니다. 1단계는 이번 달 내\n"
         "완료 목표로 진행해 주세요."),
        ("han",  ["me", "kang"], ["kim"],
         "PoC 클러스터 셋업 지원하겠습니다. 필요 스펙 알려주세요."),
        ("me",   ["han"], ["kim", "kang"],
         "80GB 카드 2장이면 됩니다. 노드 목록 정리해서 보내겠습니다."),
        ("han",  ["me"], ["kim", "kang"],
         "확인했습니다. 노드 예약 잡아두겠습니다."),
        ("me",   ["han"], ["kim", "kang"],
         "감사합니다. 1단계 착수하고 진행 상황은 주간회의에서 공유하겠습니다."),
    ]
    for i, (msg, when) in enumerate(zip(vllm, _sched_range(30, 18, len(vllm), rng))):
        sender, to, cc, body = msg[0], msg[1], msg[2], msg[3]
        att = ["vllm_poc_계획_v1.pdf"] if i == 6 else None
        subj = ("모델 평가 서빙 vLLM 전환 검토" if i == 0
                else "RE: 모델 평가 서빙 vLLM 전환 검토")
        send(f"vllm{i}", sender, to, cc, subj, body, when,
             reply_to=f"vllm{i-1}" if i else None, attachments=att)

    # ── 오래된 중간 스레드 2종 (과거에서 종결) ──────────────────────────────
    old_threads = [
        ("pent", "외부 침투테스트 결과 후속 조치", 28, 21, [
            ("seo",  ["me", "oh"], ["kim"],
             "2분기 외부 침투테스트 결과입니다. High 2건, Medium 5건.\n"
             "High 는 디버그 포트 인증 우회와 OTA 롤백 방어 미비입니다.",
             ["pentest_2026Q2.pdf"]),
            ("me",   ["seo"], ["oh", "kim"],
             "디버그 포트 건은 퓨즈 비트로 양산에서 막혀 있지 않나요?\n"
             "개발 보드 한정 이슈인지 확인 부탁드립니다."),
            ("seo",  ["me"], ["oh", "kim"],
             "맞습니다. 양산 퓨즈에선 막힙니다. 다만 개발 보드 유출 시나리오가\n"
             "있어 인증 추가를 권고안에 넣었습니다."),
            ("oh",   ["seo"], ["me", "kim"],
             "OTA 롤백 방어는 안티롤백 카운터 활성화로 대응 가능합니다.\n"
             "다음 FW 릴리즈에 포함하겠습니다."),
            ("seo",  ["me", "oh"], ["kim"],
             "정리 감사합니다. 조치 계획 취합해서 경영 보고에 반영하겠습니다."),
            ("me",   ["seo"], ["oh", "kim"],
             "수고하셨습니다. 다음 분기 테스트 범위는 별도 논의로 이어가겠습니다."),
        ]),
        ("cert", "차량용 기능안전·보안 인증 준비", 24, 16, [
            ("lee",  ["me", "choi"], ["kim"],
             "차량 고객 대응으로 ISO 21434 사이버보안 프로세스 갭 분석이\n"
             "필요합니다. 현행 개발 프로세스 문서 목록부터 취합하겠습니다."),
            ("me",   ["lee"], ["choi", "kim"],
             "위협 분석(TARA) 템플릿은 보안팀 것을 재사용할 수 있습니다.\n"
             "서수석께 공유 요청해 두겠습니다."),
            ("choi", ["lee", "me"], ["kim"],
             "인증 컨설팅 업체 3곳 견적 요청했습니다. 다음 주 취합됩니다."),
            ("lee",  ["me", "choi"], ["kim"],
             "갭 분석 1차 결과 정리해서 다음 달 초 공유하겠습니다."),
            ("me",   ["lee"], ["choi", "kim"],
             "감사합니다. 1차 결과 나오면 그때 다시 모이겠습니다."),
        ]),
    ]
    for prefix, subject, start_day, end_day, script in old_threads:
        sched = _sched_range(start_day, end_day, len(script), rng)
        for i, (msg, when) in enumerate(zip(script, sched)):
            sender, to, cc, body = msg[0], msg[1], msg[2], msg[3]
            att = msg[4] if len(msg) > 4 else None
            subj = subject if i == 0 else f"RE: {subject}"
            send(f"{prefix}{i}", sender, to, cc, subj, body, when,
                 reply_to=f"{prefix}{i-1}" if i else None, attachments=att)

    # ── 조용해진 사람 (§2): 한예린 — 28~16일 전 주기 발신 후 최근 2주 침묵 ──
    quiet = [
        (28, "MLOps 주간 리포트 W25", "학습 클러스터 가동률 72%, 큐 대기 평균 40분입니다."),
        (25, "학습 데이터 레이크 용량 알림", "데이터 레이크 사용률 81%입니다. 정리 계획 공유 예정입니다."),
        (22, "MLOps 주간 리포트 W26", "가동률 78%. 신규 노드 2대 투입 완료했습니다."),
        (19, "실험 추적 대시보드 개편 안내", "실험 추적 대시보드를 개편했습니다. 피드백 주세요."),
        (16, "MLOps 주간 리포트 W27", "가동률 74%. 다음 주 정기 점검 예정입니다."),
    ]
    for i, (day, subj, body) in enumerate(quiet):
        send(f"quiet{i}", "han", ["me", "kang"], [], subj, body,
             _day(day, rng.randint(9, 17), rng.randint(0, 59)))

    # ── 증발한 요청 (§1): 내 질문에 10일+ 무응답 ────────────────────────────
    send("evap1", "me", ["park"], [], "협력사 NDA 갱신 확인 요청",
         "지현님,\n\nIP 벤더 2곳 NDA 가 이번 분기 만료입니다.\n"
         "갱신 진행 상황 확인 부탁드립니다.", _day(12, 10))
    send("evap2", "me", ["yoon"], [], "표준셀 특성화 데이터 재추출 요청",
         "성호님,\n\nPDK 1.3 기준 표준셀 특성화 데이터 재추출이 필요합니다.\n"
         "가능 일정 회신 부탁드립니다.", _day(15, 11))

    # ── 오래된 단발 업무 메일 (10~30일 전, 내가 보낸/답장한 것 다수) ─────────
    old_oneoffs = [
        ("kim",  ["me", "jung", "lee", "choi", "yoon"], [], "상반기 목표 대비 진척 점검",
         "상반기 마무리 점검입니다. 각자 진척 현황 정리 부탁합니다.", 27, 10, None),
        ("me",   ["kim"], [], "RE: 상반기 목표 대비 진척 점검",
         "담당 과제 3건 모두 계획 대비 정상 진행 중입니다. 상세는 별첨 참고 바랍니다.", 26, 11,
         ["진척현황_김도현.xlsx"]),
        ("choi", ["me", "jung", "lee"], [], "[회의록] 월간 기술회의",
         "지난달 기술회의 회의록 공유드립니다.", 24, 9, ["회의록_월간.docx"]),
        ("me",   ["jung"], [], "RTL 리뷰 코멘트 반영본",
         "지난 리뷰 코멘트 반영본입니다. 재확인 부탁드립니다.", 22, 14, ["rtl_review_반영.pdf"]),
        ("jung", ["me"], [], "RE: RTL 리뷰 코멘트 반영본",
         "반영 확인했습니다. 이상 없습니다.", 21, 16, None),
        ("me",   ["jung"], [], "RE: RTL 리뷰 코멘트 반영본",
         "확인 감사합니다. 머지하겠습니다.", 21, 17, None),
        ("yoon", ["me", "jung"], [], "표준셀 라이브러리 정기 점검 일정",
         "이번 달 라이브러리 QA 일정 공유합니다.", 20, 11, None),
        ("me",   ["lee"], [], "에뮬레이터 슬롯 예약 문의",
         "다음 주 NPU 서브시스템 검증용 슬롯 예약 가능한지 확인 부탁드립니다.", 25, 10, None),
        ("lee",  ["me"], [], "RE: 에뮬레이터 슬롯 예약 문의",
         "수요일 야간 슬롯 비어 있습니다. 예약 걸어드릴까요?", 25, 15, None),
        ("me",   ["lee"], [], "RE: 에뮬레이터 슬롯 예약 문의",
         "네 수요일 야간으로 부탁드립니다.", 24, 9, None),
        ("lee",  ["me"], [], "RE: 에뮬레이터 슬롯 예약 문의",
         "예약 완료했습니다. 계정으로 확인 가능합니다.", 23, 10, None),
        ("me",   ["lee"], [], "RE: 에뮬레이터 슬롯 예약 문의",
         "확인했습니다. 감사합니다.", 23, 11, None),
        ("lee",  ["me", "jung", "choi"], [], "검증 커버리지 리포트 W25",
         "기능 커버리지 87% 도달했습니다. 미커버 항목 목록 첨부합니다.", 19, 13,
         ["coverage_w25.xlsx"]),
        ("seo",  ["me"], [], "펌웨어 서명 파이프라인 점검 결과",
         "정기 점검 결과 이상 없습니다. 키 만료 90일 전 알림 추가했습니다.", 23, 8, None),
        ("me",   ["seo"], [], "RE: 펌웨어 서명 파이프라인 점검 결과",
         "알림 추가 좋습니다. 대상에 저 대신 팀 메일링을 넣어 주세요.", 23, 14, None),
        ("seo",  ["me"], [], "RE: 펌웨어 서명 파이프라인 점검 결과",
         "메일링으로 변경 완료했습니다.", 22, 10, None),
        ("me",   ["seo"], [], "RE: 펌웨어 서명 파이프라인 점검 결과",
         "감사합니다. 마무리하겠습니다.", 22, 11, None),
        ("kang", ["me"], ["kim"], "컴파일러 융합 커널 성능 회귀 공유",
         "conv-bn-relu 융합에서 특정 shape 성능 회귀 발견, 원인 분석 중입니다.", 18, 16, None),
        ("me",   ["kang"], ["kim"], "RE: 컴파일러 융합 커널 성능 회귀 공유",
         "타일링 휴리스틱 변경 커밋부터 의심해 보시죠. 어제 머지된 것 있습니다.", 17, 11, None),
        ("kang", ["me"], ["kim"], "RE: 컴파일러 융합 커널 성능 회귀 공유",
         "맞았습니다. 해당 커밋 리버트하고 재현 테스트 추가했습니다.", 16, 13, None),
        ("me",   ["kang"], ["kim"], "RE: 컴파일러 융합 커널 성능 회귀 공유",
         "잘 처리되었네요. 수고하셨습니다.", 16, 15, None),
        ("gm",   ["me", "jung", "lee", "choi", "yoon"], [], "직무교육 이수 현황 안내",
         "상반기 직무교육 이수 현황입니다. 미이수자는 확인 바랍니다.", 21, 9, None),
        ("oh",   ["me"], [], "SDK 0.9 릴리즈 완료",
         "SDK 0.9 릴리즈했습니다. 릴리즈 노트 첨부합니다.", 14, 15, ["sdk_0.9_notes.md"]),
        ("me",   ["oh"], [], "RE: SDK 0.9 릴리즈 완료",
         "수고하셨습니다. 예제 빌드 CI 는 다음 릴리즈부터 필수로 하시죠.", 14, 17, None),
        ("jung", ["me", "yoon", "lee"], [], "합성 툴 버전업 검증 결과",
         "신규 버전 QoR 비교 결과 면적 -1.2%, 타이밍 동등입니다. 전환 권장합니다.", 13, 10,
         ["synth_qor_비교.xlsx"]),
        ("me",   ["jung"], [], "RE: 합성 툴 버전업 검증 결과",
         "동의합니다. B0 부터 적용하시죠.", 12, 9, None),
        ("park", ["me"], [], "IP 벤더 기술지원 계약 갱신 안내",
         "인터커넥트 IP 기술지원 계약이 다음 달 만료라 갱신 진행합니다.", 11, 14, None),
    ]
    for i, (sender, to, cc, subject, body, day, hour, att) in enumerate(old_oneoffs):
        reply_to = None
        if subject.startswith("RE: "):
            base = subject[4:]
            parent = next(m for m in reversed(mails) if m.subject == base)
            reply_to = parent.key
        send(f"old{i}", sender, to, cc, subject, body,
             _day(day, hour, rng.randint(0, 59)), reply_to=reply_to,
             attachments=att)

    # ═════════════════ 대량 수신 메일 (수신인 50명) ═════════════════════════
    # 사람 발신 + 대량 To — 미답변/기한 추적에서 제외되어야 함

    everyone = ["me", "kim", "jung", "lee", "choi", "yoon"] + \
               [f"emp{i}" for i in range(44)]  # 50명

    send("mass1", "gm", everyone, [], "[전사] 여름철 냉방 운영 안내",
         "7월부터 냉방 온도를 26도로 운영합니다.\n개인 냉방기기 사용은 자제 바랍니다.",
         _day(3, 10, 30), sig=False)
    send("mass2", "gm", everyone, [], "[전사] 지하주차장 도장 공사 안내",
         "다음 주 월~수 지하 2층 주차가 통제됩니다.\n인근 공영주차장을 이용 바랍니다.",
         _day(1, 9, 40), sig=False)
    # 오늘 + "까지" 포함 — 기한 신호 오염 테스트
    send("mass3", "gm", everyone, [], "[전사] 보안 점검: PC 전원 관리 안내",
         "금일 18시까지 자리 비우실 때 PC 전원을 종료해 주시기 바랍니다.\n"
         "미준수 부서는 별도 안내 예정입니다.", _day(0, 8, 50), sig=False)
    # 회신 요청 문구가 있어도 대량 발송은 미답변 추적 제외 (트레이드오프 — 필요시 직접 회신)
    send("mass4", "kim", everyone, [], "상반기 성과 공유회 개최 안내",
         "7/17(금) 상반기 성과 공유회를 개최합니다.\n"
         "발표 희망 팀은 이번 주까지 회신 바랍니다.", _day(2, 14))
    # 주간보고 (제목 약한 노이즈 — 미답장+대량이면 notice 분류 데모)
    send("mass5", "choi", ["me", "jung", "lee", "yoon", "oh"], [], "주간보고 W28 취합",
         "이번 주 주간보고 취합합니다. 금요일 오전까지 부탁드립니다.",
         _day(1, 15), sig=False)

    # ═════════════════ 시스템 노티 (필터 대상) ══════════════════════════════

    send("n1", "sys", ["me"], [], "[공지] 정보보호 서약서 갱신 안내",
         "연 1회 정보보호 서약서 갱신 기간입니다. 7/20까지 완료 바랍니다.\n"
         "본 메일은 발신 전용입니다.", _day(1, 8), sig=False)
    send("n2", "sys", ["me"], [], "[시스템] 회의실 예약 시스템 점검 안내",
         "금일 22시부터 익일 02시까지 예약 시스템 점검이 진행됩니다.", _day(0, 9), sig=False)
    send("n3", "sys", ["me"], [], "[시스템] VPN 정기 점검 안내",
         "토요일 새벽 VPN 점검이 있습니다.", _day(3, 9), sig=False)
    send("h1", "hr", ["me"], [], "[인사] 연차 사용 촉진 안내",
         "미사용 연차 현황을 확인해 주세요.", _day(4, 10), sig=False)
    send("h2", "hr", ["me"], [], "[인사] 건강검진 예약 안내",
         "하반기 건강검진 예약이 시작되었습니다.", _day(2, 10), sig=False)
    send("h3", "hr", ["me"], [], "설문요청: 정보보호 인식 조사",
         "전 임직원 대상 정보보호 인식 조사입니다. 5분 소요됩니다.",
         _day(1, 10), sig=False)

    jira_status = ["Open → In Progress", "In Progress → Resolved",
                   "Resolved → Closed", "새 댓글 등록"]
    for i in range(18):
        day, hour = rng.randint(0, 28), rng.randint(8, 18)
        send(f"jira{i}", "jira", ["me"], [],
             f"[JIRA] NPX-{101 + i} {rng.choice(jira_status)}",
             f"이슈 NPX-{101 + i} 이(가) 갱신되었습니다.\n"
             f"담당자: {rng.choice(['김도현', '정우진', '이서연'])}\n"
             "이 메일은 자동 발송되었습니다.",
             _day(day, hour, rng.randint(0, 59)), sig=False)

    for i in range(12):
        day, hour = rng.randint(0, 28), rng.choice([2, 3, 7])
        result = rng.choice(["SUCCESS", "SUCCESS", "FAILED"])
        send(f"build{i}", "build", ["me"], [],
             f"[Build] rtl-regression nightly #{500 + i} {result}",
             f"nightly 회귀 #{500 + i}: {result}\n"
             "상세: http://build.nurisoft.co.kr/rtl/512", _day(day, hour), sig=False)

    # ═════════════════ mid-join: 내가 중간에 추가된 스레드 ══════════════════
    # 처음 두 통은 강미래·오태양 둘만 주고받아 내 사서함에 없다(미배달 — mails
    # 리스트에 안 넣는다). 세 번째에서 내가 참조 추가 — References 가 미보유
    # 메일을 가리켜 새 스레드가 되고, 첫 보유분의 인용 체인(유일본)이
    # 마커/접힘으로 보존된다 (docs/ARCHITECTURE.md §6.1 데모).
    mj_h0 = _Mail("mj_h0", "kang", ["oh"], [],
                  "NPX-200 커널 드라이버 DMA 캐시 정합성",
                  "런타임에서 출력 텐서 캐시 미스매치가 간헐 재현됩니다.\n"
                  "dma_alloc 경로가 non-coherent 버퍼를 주는데 컴파일러 런타임이\n"
                  "invalidate 를 생략하는 경우가 있는 것으로 보입니다.",
                  _day(6, 10))
    mj_h1 = _Mail("mj_h1", "oh", ["kang"], [],
                  "RE: NPX-200 커널 드라이버 DMA 캐시 정합성",
                  "드라이버 쪽 확인했습니다. v2.3 부터 CMA 영역이 non-coherent 로\n"
                  "바뀌었고 sync_for_cpu 훅 호출은 UMD 책임입니다.\n"
                  "런타임 팀 확인이 필요합니다.",
                  _day(5, 14))
    mj_h1.full_body += _quote_block(mj_h0)
    mj0 = send("mj0", "kang", ["oh", "me"], ["kim"],
               "RE: NPX-200 커널 드라이버 DMA 캐시 정합성",
               "김도현 님, 런타임 쪽 확인이 필요해서 참조 추가드립니다.\n"
               "아래 히스토리 참고 부탁드립니다 — UMD 의 sync_for_cpu 호출 누락\n"
               "가능성이 있습니다.",
               _day(4, 9))
    mj0.parent = mj_h1                  # References 가 미보유 메일을 가리킨다
    mj0.full_body += _quote_block(mj_h1)
    send("mj1", "me", ["kang", "oh"], ["kim"],
         "RE: NPX-200 커널 드라이버 DMA 캐시 정합성",
         "확인했습니다. UMD 2.3.1 에서 output 경로 invalidate 가 조건부로 빠지는\n"
         "커밋을 찾았습니다. 핫픽스 브랜치로 내일까지 공유하겠습니다.",
         _day(4, 15), reply_to="mj0")
    send("mj2", "kang", ["me"], ["oh", "kim"],
         "RE: NPX-200 커널 드라이버 DMA 캐시 정합성",
         "감사합니다. 재현 케이스 3종으로 검증 준비해 두겠습니다.",
         _day(3, 10), reply_to="mj1")

    # ═════════════════ 외부 스팸 (필터 대상) ════════════════════════════════

    spam = [
        ("spam_news", "이번 주 반도체 뉴스레터 #204 — 엣지 AI 특집", 6),
        ("spam_news", "이번 주 반도체 뉴스레터 #205 — RISC-V 동향", 3),
        ("spam_news", "[재발송] 뉴스레터 구독 혜택 안내", 1),
        ("spam_shop", "사무용품 여름 특가전 최대 60%", 5),
        ("spam_shop", "오늘까지! 모니터암 반값 특가", 0),      # 기한 신호 오염 테스트
        ("spam_shop", "[광고] 프리미엄 의자 신제품 출시", 2),
        ("spam_webi", "무료 웨비나 초대: 2026 온디바이스 AI 트렌드", 4),
        ("spam_webi", "마감 임박! 클라우드 보안 웨비나", 1),   # 역시 오염 테스트
        ("spam_news", "이번 주 반도체 뉴스레터 #203 — 칩렛 특집", 13),
        ("spam_shop", "여름 정기세일 사전 안내", 17),
        ("spam_webi", "웨비나 다시보기 링크 안내", 11),
        ("spam_news", "이번 주 반도체 뉴스레터 #202 — 보안 특집", 20),
        ("spam_webi", "무료 웨비나: LLM 서빙 최적화 실전", 24),
        ("spam_news", "이번 주 반도체 뉴스레터 #201 — 파운드리 동향", 27),
    ]
    for i, (sender, subject, day) in enumerate(spam):
        send(f"spam{i}", sender, ["me"], [], subject,
             "안녕하세요!\n지금 바로 확인해 보세요. 오늘까지 신청 시 혜택이 제공됩니다.\n"
             "수신거부는 하단 링크를 이용하세요.",
             _day(day, rng.randint(7, 20), rng.randint(0, 59)), sig=False)

    _extra(send, rng)          # 협업도구·자동 리포트와 그것을 참조하는 사람 스레드
    return mails


class FakeSource:
    name = "fake"

    def fetch(self, since_iso: str | None,
              image_cutoff: str | None = None) -> Iterator[MailRecord]:
        # image_cutoff 는 무시 — store 의 ingest 게이트가 동일 판정을 한다
        #  (데모에선 '경과 메일의 cid 흔적 → 마커' 경로 시연에 오히려 필요)
        mails = sorted(_scenario(), key=lambda m: m.when)
        for i, m in enumerate(mails):
            sent_on = m.when.strftime("%Y-%m-%dT%H:%M:%S")
            if since_iso and sent_on <= since_iso:
                continue
            parent_id = f"<fake-{m.parent.key}@nurisoft.co.kr>" if m.parent else ""
            yield MailRecord(
                message_id=f"<fake-{m.key}@nurisoft.co.kr>",
                subject=m.subject,
                sender_name=m.sender_name,
                sender_addr=m.sender_addr,
                to=m.to,
                cc=m.cc,
                sent_on=sent_on,
                body_text=m.full_body,
                body_html=("" if m.key in _NO_HTML
                           else _RICH_HTML.get(m.key)
                           or _client_html(*_split_quote(m.full_body),
                                           m.when.day % 3,
                                           _color_seed(m.key))),
                entry_id=f"FAKE-ENTRY-{i:04d}",
                in_reply_to=parent_id,
                references=[parent_id] if parent_id else [],
                conversation_key="",
                attachments=m.attachments,
                inline_images=_INLINE_IMAGES.get(m.key, {}),
                folder="sent" if m.sender_addr in (ME, ME_ALIAS) else "inbox",
            )
