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

import html as _htmlmod
import random
import re
from datetime import datetime, timedelta
from typing import Iterator

from .base import MailRecord

ME = "dohyun.kim@nurisoft.co.kr"
ME_ALIAS = "dhkim@nurisoft.co.kr"     # 메일 별칭 — 별칭 발신 재분류 데모용

_URL_RX = re.compile(r'(https?://[^\s<>"\)]+)')


def _plain_to_html(text: str) -> str:
    """합성 평문 본문 → 표시용 HTML (문단·줄바꿈·URL 링크화)."""
    paras = re.split(r"\n\s*\n", text.strip())
    out = []
    for p in paras:
        esc = _URL_RX.sub(r'<a href="\1">\1</a>', _htmlmod.escape(p))
        out.append("<p>" + esc.replace("\n", "<br>\n") + "</p>")
    return "\n".join(out)


# 서식(굵게·표·링크)과 추적 픽셀 차단을 웹 데모에서 보여주기 위한 리치 HTML 오버라이드
_RICH_HTML = {
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
# inline_images 바이트를 주입한다 (docs/PROPOSAL-images.md 경로 그대로 재현)
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
    # 외부 스팸 (internal_domains 로 필터되어야 함)
    "spam_news": ("테크뉴스레터", "news@techletter.example"),
    "spam_shop": ("오피스몰", "promo@shopdeals.example"),
    "spam_webi": ("웨비나사무국", "invite@bizwebinar.example"),
}

# 대량 수신 메일용 가상 직원 명단
for _i in range(44):
    _PEOPLE[f"emp{_i}"] = (f"직원{_i:02d}", f"employee{_i:02d}@nurisoft.co.kr")

_NO_SIG = {"sys", "jira", "build", "hr", "spam_news", "spam_shop", "spam_webi"}

_SIGNATURE = """
--
{name}
SoC개발팀 | 내선 {ext}
※ 본 메일은 기밀 정보를 포함할 수 있으며, 지정된 수신자 외의 사용을 금합니다.
"""


def _quote_block(parent: "_Mail") -> str:
    """한국어 Outlook 답장 인용 블록 (이전 본문 전체 포함)."""
    return (
        "\n\n________________________________\n"
        f"보낸 사람: {parent.sender_name} <{parent.sender_addr}>\n"
        f"보낸 날짜: {parent.when.strftime('%Y년 %m월 %d일 %A %p %I:%M')}\n"
        f"받는 사람: {'; '.join(parent.to)}\n"
        f"제목: {parent.subject}\n\n"
        f"{parent.full_body}"
    )


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

    # 내 결정 대기 (decide): 그룹 승인 요청
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
    send("fyi1", "choi", ["jung"], ["me"], "주간 일정표 공유",
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
                           else _RICH_HTML.get(m.key) or _plain_to_html(m.full_body)),
                entry_id=f"FAKE-ENTRY-{i:04d}",
                in_reply_to=parent_id,
                references=[parent_id] if parent_id else [],
                conversation_key="",
                attachments=m.attachments,
                inline_images=_INLINE_IMAGES.get(m.key, {}),
                folder="sent" if m.sender_addr in (ME, ME_ALIAS) else "inbox",
            )
