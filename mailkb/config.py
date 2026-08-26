"""설정 — <home>/config.toml.

home 결정 순서: --home 플래그 > MAILKB_HOME 환경변수 > <mailkb 코드폴더>/data
(~/ 로밍 프로필은 쓰지 않는다 — 데이터는 코드 폴더 옆 data/ 에 두어 실행 위치 무관.)
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_TEMPLATE = '''# mailkb 설정 — 개인값은 코드가 아니라 이 파일(<home>/config.toml)에만 둔다.
# 내 주소 (발신 메일 판별 기준 — 복수 가능). 반드시 실제 주소로 채울 것.
# 메일 별칭(alias)으로도 발신한다면 별칭 주소를 함께 나열 — 별칭 발신이
# 수신으로 잘못 분류되는 것을 막는다.
# 예: my_addresses = ["gildong.hong@company.co.kr", "ghong@company.co.kr"]
my_addresses = []

# 본문에서 '나를 명시적으로 언급'했는지 판정할 이름/호칭(개입 큐 과탐 축소).
# 내 이름이 들어간 메일은 대규모 그룹메일이라도 '확인 대상'으로 유지한다.
# (내가 보낸 메일에 대한 답장도 '나를 언급한 것'과 동일하게 취급 — 내 참여 스레드)
# 예: my_names = ["홍길동"]
my_names = []

# 기본 소스: outlook(회사 PC, 클래식 Outlook COM) | fake(데모)
source = "outlook"

[sources]
# 받은 편지함 **하위 폴더**까지 수집한다. Outlook 규칙이 수신 메일을 하위 폴더로
# 자동 분류하는 환경에서 이 값이 false 면 색인이 거의 빈 채로 조용히 남는다.
# (지운 편지함·정크 메일은 받은 편지함의 형제라 구조상 이미 빠진다)
include_subfolders = true
# 추가로 뺄 폴더 — 이름 또는 "inbox/보관" 형태의 상대 경로 (대소문자 무시).
# 웹 UI 설정 › 수집 폴더에서도 켜고 끌 수 있다.
exclude_folders = []
# 동시에 여는 폴더 상한. 넘으면 얕은 폴더부터 채우고 나머지는 건너뛰되,
# sync 와 doctor 가 건너뛴 폴더를 반드시 출력한다. 0 = 무제한.
max_folders = 50

[filters]
# 이 문자열이 발신 주소에 포함되면 노이즈(공지/자동발송)로 분류
ignore_senders = ["noreply", "no-reply", "notification@", "jira@", "build@"]
# 사내 도메인 — 설정하면 외부 도메인 발신(스팸 등)은 미답변/기한/요약에서 제외.
# 외부 파트너 메일도 추적하려면 빈 리스트로. 예: ["company.co.kr"]
internal_domains = []
# 외부 허용 목록 — internal_domains 를 켠 상태에서 추적할 협력사 도메인/주소.
# 예: external_allowlist = ["partner.co.kr", "kim@vendor.com"]
external_allowlist = []
# 추가 차단 발신자는 <home>/blocked_senders.txt 에 누적된다 (mailkb block <주소>).
# 실제 수신 차단은 Outlook 규칙으로 병행 — 이 파일은 mailkb 신호에서만 제외.
# 제목 기반 노이즈 2단계 (키를 지우면 아래 기본값 적용):
#  - strong: 내 참여 여부와 무관하게 무조건 제외 (시스템 알림/설문 등).
#            앵커 매치 — '[태그]'는 포함, 일반 단어는 제목 시작 또는 '단어:' 형태만
#            ("notification 설정 변경 검토 요청" 같은 실무 제목은 안 죽는다)
#  - weak:   내가 답장하지 않았고 수신 3인 이상 대량일 때만 제외 (주간보고 등 —
#            내가 논의에 참여한 스레드는 유지). 소문자 부분 매치.
subject_noise_strong = ["invitation", "notification", "자동회신", "자동 회신",
                        "[nflow]", "[nwork]", "승계통보", "설문요청", "설문 요청"]
subject_noise_weak = ["weekly report", "주간보고", "주간 보고", "[회의록]"]

[ai]
# 폴백 백엔드. 아래 작업별 라우팅이 없는 호출이 쓴다(환경 진단의 시험 호출 등).
# 내장 이름 sonnet/haiku/opus 는 claude CLI, internal 은 opencode 를 부른다 —
# 쓰는 CLI 하나로 맞춰 두는 편이 낫다.
default = "sonnet"
# 작업별 백엔드 라우팅 (비용/품질 최적화). 웹 '설정' 화면에서도 바꿀 수 있다:
#  - summary : 메일 본문이 들어가는 '요약/회고/디제스트' → 품질 좋은 sonnet.
#  - search  : AI 검색(질의 번역·재순위·심층 읽기) → 정확도 우선.
#  - ask     : 분석(질문 조사·답변·인용 검증). 비우면 search 를 따른다.
#  - weekly  : 주간 보고 증류. 비우면 summary 를 따른다.
# sonnet/haiku/opus/internal 은 아래 [ai.backends.*] 를 지워도 내장 기본값으로 동작한다
# (config 에 있으면 그 값이 우선). --backend 를 명시하면 그것이 우선.
# 진짜 미해결 백엔드거나 호출 실패 시 결정론 결과가 그대로 남는다(AI 없어도 동작).
summary = "sonnet"
search = "sonnet"
# ask = "sonnet"
# weekly = "sonnet"
# 현안 브리핑(스레드·인물 화면 [현안 브리핑] 버튼) — 사람이 누를 때만 도는 1콜이라
# 좋은 모델을 쓴다. 실측에서 opus 가 sonnet 보다 싸고 빨랐다(출력 토큰이 4배 적다).
# 쓰는 CLI 가 opus 를 지원하는지 `mailkb diagnose` 가 실제 호출로 확인해 준다.
diagnose = "opus"
# 요약·수확 대상 날짜 창: max(마지막 실행일, 오늘−(N−1)) ~ 오늘.
# N=summary_max_days(기본 1 — 오늘만). 하루 이틀 건너뛰는 날의 소급까지
# 원하면 2~3 으로 (그만큼 첫 실행/복귀일 비용 증가).
# summary_max_days = 1   # 수확 소급 상한(요약은 스레드 화면 버튼)
# 분석(ask) 한 콜의 입력 상한(토큰). **백엔드 컨텍스트 창**을 적으면 된다 —
# 통당 자수가 아니라 총량이라, 몇 통을 읽든 창을 넘지 않는다. 코드가 이 값을
# 자수로 바꿔 통당 예산을 자동 배분하고, 조립된 프롬프트의 실제 길이로 맞춘다.
# 기본 120000 은 Claude 기준(1M 창의 여유 안에서 비용을 묶는 값)이다.
# 0 = 제한 없음. 사내 백엔드 창이 작으면 그 값으로 낮춘다.
# ask_max_input_tokens = 120000
# 토큰→자수 변환 비율. 한국어는 대략 1자당 0.7~1.5토큰이라 1.0 이 보수적이다
# (자수를 토큰보다 적게 잡아 창을 넘기지 않는 쪽). 실측 후 올리면 맥락이 늘어난다.
# chars_per_token = 1.0

[ai.backends.internal]
# opencode headless — 프롬프트는 stdin 으로 전달됨
cmd = ["opencode", "run"]
# 추론 강도 플래그 opt-in — 이 CLI 가 지원함을 **직접 확인한 뒤** 플래그 이름을
# 선언하면, 분석·주간 보고의 어려운 콜에 "<플래그> high" 가 붙는다. 미선언(기본)
# 이면 아무것도 안 붙는다 — 미지원 플래그는 전 호출을 실패시킨다(2026-07-28 사고).
# 켜기 전에 `diagnose` 로 1콜 검증을 권장.
# effort_flag = "--effort"

[ai.backends.sonnet]
# claude headless — 요약/회고용(sonnet). --backend sonnet 로도 지정 가능.
cmd = ["claude", "-p", "--model", "sonnet"]

[ai.backends.haiku]
# claude headless — 값싼 보조용(현재 기본 라우팅에는 쓰이지 않는다. 비용이
# 중요한 작업에 --backend haiku 또는 설정 화면에서 지정).
cmd = ["claude", "-p", "--model", "haiku"]

[ai.backends.opus]
# claude headless — 무거운 판단용(주간 증류·심층 분석 등 on-demand).
# sonnet 대비 수 배 비싸므로 상시 라우팅(summary/search)에 두지 말고
# --backend opus 또는 설정 화면에서 필요한 작업에만 지정한다.
# 특정 버전 고정이 필요하면 전체 모델명으로: "--model", "claude-opus-4-8"
cmd = ["claude", "-p", "--model", "opus"]

[review]
# "개입 필요" 큐의 정체 판정 기준 (영업일 — 주말·아래 holidays 제외)
stall_workdays = 2   # 내가 보낸 메일에 응답 없음 = 정체
stale_workdays = 3   # 열린 스레드 무활동 = 정체
# 대량발송 제외선: 수신인 이 수 이상이면 전사/그룹 공지로 보고 개인 액션 큐에서 제외.
# 조직 규모에 맞춰 조정 — 실무 그룹 메일은 포함되고 팀/전사 공지만 배제되게.
broadcast_to = 50
# 개입 큐 상한(일): 이보다 오래 방치된 항목은 큐(주간 보고 재료)에서 내림 (0=없음).
# queue_max_days = 21
# 수신인이 이 수 이하이면 '나에게 직접 온 메일'로 보고 개입 큐에 유지.
# 그 이상(그룹메일)은 요청/질문 신호·내 이름 언급·내 참여 스레드일 때만 유지 →
# 요청 없는 그룹 FYI 과탐(false alarm) 제거.
direct_to = 4
# 공휴일 (YYYY-MM-DD) — 영업일 계산에서 제외. 비워두면 주말만 제외.
# 왜 필요한가: 정체 판정(stall_workdays·stale_workdays)이 **영업일**로 세기
# 때문이다. 연휴가 영업일로 잡히면 연휴 직후 며칠간 멀쩡한 스레드가 무더기로
# '멈춤'으로 뜬다 — 임계값이 2~3일이라 설·추석 연휴 하나면 바로 넘는다.
# 음력 공휴일은 계산으로 못 만드니 연 1회 갱신한다(목록이 올해를 안 덮으면
# `mailkb doctor` 가 알려 준다).
holidays = [
  "2026-01-01",                                # 신정
  "2026-02-16", "2026-02-17", "2026-02-18",    # 설날 연휴
  "2026-03-01", "2026-03-02",                  # 삼일절 (+대체)
  "2026-05-05",                                # 어린이날
  "2026-05-24", "2026-05-25",                  # 부처님오신날 (+대체)
  "2026-06-06",                                # 현충일
  "2026-08-15", "2026-08-17",                  # 광복절 (+대체)
  "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-28",  # 추석 연휴 (+대체)
  "2026-10-03", "2026-10-05",                  # 개천절 (+대체)
  "2026-10-09",                                # 한글날
  "2026-12-25",                                # 성탄절
  # 2027 — 현충일(6/6 일)은 대체공휴일 대상이 아니라 대체가 없다.
  "2027-01-01",                                # 신정 (금)
  "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",  # 설날 연휴 (+대체 화)
  "2027-03-01",                                # 삼일절 (월)
  "2027-05-05",                                # 어린이날 (수)
  "2027-05-13",                                # 부처님오신날 (목)
  "2027-06-06",                                # 현충일 (일)
  "2027-08-15", "2027-08-16",                  # 광복절 (일) +대체 (월)
  "2027-09-14", "2027-09-15", "2027-09-16",    # 추석 연휴 (화~목)
  "2027-10-03", "2027-10-04",                  # 개천절 (일) +대체 (월)
  "2027-10-09", "2027-10-11",                  # 한글날 (토) +대체 (월)
  "2027-12-25", "2027-12-27",                  # 성탄절 (토) +대체 (월)
]
'''


# <home>/ai-rules.md 의 초기 내용 — **주석만** 들어 있다. 설명을 HTML 주석으로 써 둔
# 이유: "주석은 AI 가 보지 않는다"를 파일이 스스로 증명하고, 주석뿐이면
# ai_rules_text() 가 빈 문자열을 돌려줘 설치 직후 프롬프트가 바뀌지 않는다.
# 주의: 아래 제거 정규식이 비탐욕(`<!--.*?-->`)이라 설명문 안에 닫는 기호를 적으면
# 거기서 주석이 끝나 나머지가 프롬프트로 샌다 — 기호 대신 말로 쓴다(테스트가 잠근다).
_AI_RULES_TEMPLATE = """<!--
mailkb AI 지침 — 이 파일의 평문은 AI 프롬프트에 "[사용자 지침 — 우선 적용]" 으로
그대로 실린다. 들어가는 곳: 회고의 암묵지 수확 · 분석(ask) · 주간 보고.
인용을 원문과 대조하는 검증 콜에는 넣지 않는다.

- 이 블록처럼 HTML 주석으로 감싼 부분은 AI 가 보지 않는다. 규칙은 주석 밖에 쓴다.
- 저장하면 다음 호출부터 반영된다(재시작 없음). 상한 4,000자 — 넘는 부분은 잘린다.
- 짧은 규칙 몇 줄이 길게 쓴 것보다 잘 먹는다. 사내 용어 풀이 · 호칭 · 우선순위 ·
  표기 규칙 정도.

예시 (주석 밖에 이렇게):
  NPX-200 은 자사 NPU 제품명이다. 'NPX' 만 써도 같은 것이다.
  '팀장' 은 김민수 한 사람을 가리킨다.
  일정 관련 결정에는 항상 날짜를 붙여 적는다.
"""
# 닫는 기호는 문자열 밖에서 붙인다 — 위 주석의 함정을 이 파일 안에서도 밟지 않게
_AI_RULES_TEMPLATE += "-->\n"

_AI_RULES_COMMENT_RX = re.compile(r"<!--.*?-->", re.DOTALL)

# 답장/전달 접두 — 제목 강한 노이즈의 시작 일치 판정 전에 벗겨낸다.
_REPLY_PREFIX_RX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|답장|회신|전달)\s*:\s*|\[\s*(?:re|fw|fwd)\s*\]\s*)+",
    re.IGNORECASE)

# 제목 노이즈 기본값 — config.toml 에 키가 없어도 적용 (구버전 설정 호환)
_SUBJECT_NOISE_STRONG = ["invitation", "notification", "자동회신", "자동 회신",
                         "[nflow]", "[nwork]", "승계통보", "설문요청", "설문 요청"]
_SUBJECT_NOISE_WEAK = ["weekly report", "주간보고", "주간 보고", "[회의록]"]

# 역할 이름표 — 사람이 읽는 쪽. 'diagnose' 만 찍으면 무엇이 안 되는지 모른다.
# **설정 화면의 행 이름과 같은 말을 쓴다**(2026-08-26) — 한 화면이 한 가지를 두
# 이름으로 부르면 안 된다. doctor 와 CLI `diagnose` 도 이 값으로 역할을 적는다.
ROLE_LABEL = {"summary": "일일 회고·요약", "search": "AI 검색", "ask": "분석",
              "weekly": "주간 보고", "diagnose": "현안 브리핑"}

# 내장 백엔드 기본값 — config.toml 에 [ai.backends.<name>] 이 없어도 이 이름들은
# 동작한다. PC config 를 손대지 않아도 요약=sonnet / 현안 브리핑=opus 라우팅이
# 되도록(config 에 명시하면 그 값이 우선). internal 은 사내 opencode 기본 호출.
# **haiku 는 지금 어느 역할도 안 쓴다** — 개입 AI 분류가 2026-07-30 에 정규식
# 결정론으로 바뀌면서 소비처가 사라졌다. 그래도 목록에는 남긴다: 모델을 고르려는
# 사람은 안 쓰는 것도 부를 수 있는지 알아야 한다(ARCHITECTURE §7.12).
_BUILTIN_BACKENDS = {
    "internal": ["opencode", "run"],
    "sonnet": ["claude", "-p", "--model", "sonnet"],
    "haiku": ["claude", "-p", "--model", "haiku"],
    "opus": ["claude", "-p", "--model", "opus"],
}


@dataclass
class Config:
    home: Path
    my_addresses: list[str] = field(default_factory=list)
    my_names: list[str] = field(default_factory=list)
    source: str = "fake"
    ignore_senders: list[str] = field(default_factory=list)
    internal_domains: list[str] = field(default_factory=list)
    subject_noise_strong: list[str] = field(
        default_factory=lambda: list(_SUBJECT_NOISE_STRONG))
    subject_noise_weak: list[str] = field(
        default_factory=lambda: list(_SUBJECT_NOISE_WEAK))
    ai_default: str = "sonnet"   # 역할 라우팅이 없는 호출의 폴백
    ai_summary_backend: str = "sonnet"   # 요약/회고/디제스트 전용 (품질 우선)
    ai_search_backend: str = "sonnet"    # AI 검색(번역·재순위·심층읽기) 전용 (정확도 우선)
    # 분석(질의응답) 전용. 빈 값 = 미설정이고, __post_init__ 이 ai_search_backend 로
    # 채운다 — 예전엔 두 작업이 한 설정을 공유했고 그 동작을 깨지 않기 위해서다.
    # 번역·재순위 위주인 AI 검색과 달리 분석은 한 질문에 최대 12콜을 쓴다.
    ai_ask_backend: str = ""
    # 스레드 진단 전용(2026-08-16). 사람이 누를 때만 도는 1콜이고, 실측에서
    # opus 가 sonnet 보다 **싸고 빨랐다**(진단 1건: opus $0.23/70초 vs
    # sonnet $0.32/190초 — sonnet 이 사고를 오래 해 출력 토큰이 4배). 품질도
    # opus 가 나았다(폴백 착수 시점 누락·산정 범위 누락을 sonnet 은 못 짚었다).
    ai_diagnose_backend: str = "opus"
    ai_backends: dict = field(default_factory=dict)
    stall_workdays: int = 2
    stale_workdays: int = 3
    broadcast_to: int = 50
    direct_to: int = 4
    holidays: list[str] = field(default_factory=list)
    blocked_senders: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)   # 파싱된 config.toml 원본 (opt 조회용)

    def __post_init__(self) -> None:
        # 분석 백엔드 미설정 → AI 검색 백엔드 상속. load() 가 아니라 여기 두어야
        # Config 를 직접 만드는 경로(테스트·스크립트)도 같은 규칙을 따른다.
        if not self.ai_ask_backend:
            self.ai_ask_backend = self.ai_search_backend

    @property
    def db_path(self) -> Path:
        return self.home / "db.sqlite"

    @property
    def vault(self) -> Path:
        return self.home / "vault"

    @property
    def blocklist_path(self) -> Path:
        return self.home / "blocked_senders.txt"

    def is_noise_sender_hard(self, addr: str) -> bool:
        """확실한 노이즈 발신 — 자동발송(ignore_senders)·차단(blocked) 부분 매치.

        액션 판정(actions.py)에서 다른 증거와 무관하게 즉시 NONE 이 되는 유일한
        발신자 조건. 외부 도메인은 여기 안 들어간다(policy — is_noise_external)."""
        addr = (addr or "").lower()
        return (any(pat in addr for pat in self.ignore_senders)
                or any(pat in addr for pat in self.blocked_senders))

    def is_noise_external(self, addr: str) -> bool:
        """정책 노이즈 — internal_domains 설정 시 외부 도메인 발신(스팸 대응).

        external_allowlist(협력사 도메인·주소)는 예외 — '외부 전부 노이즈 vs
        외부 스팸 유입'의 전부 아니면 전무 구조를 허용 목록이 푼다. 액션
        판정에서는 즉시 제외가 아니라 강등 요소로만 쓴다."""
        if not self.internal_domains:
            return False
        addr = (addr or "").lower()
        domain = addr.rsplit("@", 1)[-1]
        if any(domain == d or domain.endswith("." + d)
               for d in self.internal_domains):
            return False
        allow = [str(a).lower() for a in
                 (self.opt("filters", "external_allowlist", default=None) or [])]
        if any(a in addr if "@" in a else
               (domain == a or domain.endswith("." + a)) for a in allow):
            return False
        return True

    def is_noise(self, addr: str) -> bool:
        """자동발송/스팸/차단 판정 — 미답변·기한 신호·롤링 요약·개입 큐에서 제외.

        1) ignore_senders 부분 문자열 매치 (noreply, jira@ 등)
        2) blocked_senders 부분 문자열 매치 (mailkb block 으로 누적)
        3) internal_domains 설정 시, 외부 도메인 발신 전부
           (filters.external_allowlist 의 협력사 도메인·주소는 예외)
        """
        return self.is_noise_sender_hard(addr) or self.is_noise_external(addr)

    def is_blocked(self, addr: str) -> bool:
        addr = (addr or "").lower()
        return any(pat in addr for pat in self.blocked_senders)

    def opt(self, *keys, default=None):
        """config.toml 중첩 키 범용 조회 — 예: cfg.opt("review", "new_knob", default=3).

        새 설정 키는 config.py 수정 없이 **사용처 파일에서** 이걸로 읽는다
        (기능 파일 하나만 바꿔 전송하는 단일 파일 업데이트 운용을 위해).
        기존 명시 필드는 그대로 유지 — 앞으로 추가되는 키만 opt 사용.
        """
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def is_noise_subject_strong(self, subject: str) -> bool:
        """제목 강한 노이즈 — 참여 여부 무관 무조건 제외 (앵커 매치).

        구 부분 문자열 매치는 "notification 설정 변경 검토 요청" 같은 실무 제목을
        죽였다(2026-07-17). 패턴별 매치 규칙:
        - '[태그]' 패턴: 제목 어디든 포함 (이미 정밀)
        - 일반 패턴: '패턴:' 또는 '[패턴]' 형태로 포함("Notification: …",
          "Meeting Invitation: …"), 또는 답장 접두 제거 후 제목 전체가 패턴.
          시스템 제목의 표지는 콜론/브래킷 — 맨 단어 시작 일치는 사람 제목
          ("notification 설정 변경 …")을 죽여서 뺐다.
        (약한 노이즈는 미참여+대량 조건이 이미 좁혀 부분 매치 유지 — is_noise_subject_weak)
        """
        s = (subject or "").lower()
        core = _REPLY_PREFIX_RX.sub("", s).strip()
        for pat in self.subject_noise_strong:
            p = pat.lower()
            if p.startswith("["):
                if p in s:
                    return True
            elif f"{p}:" in s or f"[{p}]" in s or core == p:
                return True
        return False

    def is_noise_subject_weak(self, subject: str) -> bool:
        """제목 약한 노이즈 후보 — 미참여+대량일 때만 제외 (판정은 review 쪽)."""
        s = (subject or "").lower()
        return any(pat in s for pat in self.subject_noise_weak)

    AI_RULES_MAX = 4000           # 프롬프트에 싣는 지침 상한(폭주 방지)

    def ai_rules_text(self, limit: int | None = AI_RULES_MAX) -> str:
        """<home>/ai-rules.md 내용 (HTML 주석 제거) — AI 판단 프롬프트에 주입.
        <home> 은 데이터 홈(--home > MAILKB_HOME > <저장소>/data)이지 사용자 홈이
        아니다. init_home 이 주석만 든 템플릿(_AI_RULES_TEMPLATE)을 만들어 둔다.

        파일이 없거나 읽기 실패면 빈 문자열(graceful). 호출 시점마다 읽으므로
        파일 수정이 즉시 반영된다. 폭주 방지 상한 4000자.
        """
        try:
            text = (self.home / "ai-rules.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        text = _AI_RULES_COMMENT_RX.sub("", text).strip()
        return text if limit is None else text[:limit]

    # 역할별 백엔드 이름 — 엔진이 각자 하던 상속 규칙을 **여기 한 곳**에 모은다.
    # 이 함수를 안 쓰면 "설정에 적힌 것"과 "실제로 부르는 것"이 갈라진다:
    # doctor 가 미설정 역할을 전부 ai_default 로 표시해 claude 만 있는 PC 에서
    # "분석은 opencode 라 안 됨"이라는 거짓 경고를 냈다(2026-08-19).
    _ROLES = ("summary", "search", "ask", "weekly", "diagnose")

    def backend_for(self, role: str) -> str:
        """이 역할이 실제로 부르는 백엔드 이름. 모르는 역할은 폴백(ai_default)."""
        if role == "summary":
            return self.ai_summary_backend
        if role == "search":
            return self.ai_search_backend
        if role == "ask":                 # __post_init__ 이 search 를 상속시킨다
            return self.ai_ask_backend
        if role == "weekly":              # 미설정이면 요약을 따른다
            return self.opt("ai", "weekly", default=None) or self.ai_summary_backend
        if role == "diagnose":
            return self.ai_diagnose_backend
        return self.ai_default

    def ai_cmd(self, backend: str | None) -> list[str]:
        name = backend or self.ai_default
        cmd = (self.ai_backends.get(name) or {}).get("cmd")
        if cmd:
            return list(cmd)                     # config 명시가 우선
        if name in _BUILTIN_BACKENDS:
            return list(_BUILTIN_BACKENDS[name])  # 내장 기본값 (config 무수정)
        raise SystemExit(
            f"AI 백엔드 '{name}' 설정 없음 — {self.home / 'config.toml'} 의 [ai.backends.{name}] 확인"
        )

    def ai_effort_flag(self, backend: str | None) -> str | None:
        """이 백엔드에 추론 강도 플래그를 방출해도 되는가 — **선언이 곧 opt-in**.

        `[ai.backends.<name>] effort_flag = "--effort"` 처럼 사용자가 플래그
        이름을 선언한 백엔드에만 방출한다. 2026-07-28 실기기 사고의 교훈이다:
        미지원 플래그 하나가 전 호출을 exit 1 로 무너뜨렸고, `--effort` 는
        지원 여부가 한 번도 검증된 적 없다(7f363d8 롤백). 무조건 방출로
        되돌리는 대신, 사용자가 자기 CLI 로 확인하고 선언했을 때만 붙인다.
        플래그 이름을 값으로 받는 이유: CLI 세대에 따라 이름이 다를 수 있어
        코드 수정 없이 대응하기 위해서다. 내장 백엔드는 선언이 없으니 None —
        기본 동작은 지금과 바이트 단위로 같다."""
        name = backend or self.ai_default
        flag = (self.ai_backends.get(name) or {}).get("effort_flag")
        return str(flag) if flag else None


# 기본 데이터 폴더: 코드 폴더(리포 루트) 기준 고정 — 실행 위치(cwd) 무관.
# config.py = <repo>/mailkb/config.py 이므로 parents[1] = <repo>.
_DEFAULT_HOME = Path(__file__).resolve().parents[1] / "data"


def resolve_home(cli_home: str | None) -> Path:
    if cli_home:
        return Path(cli_home).expanduser()
    if os.environ.get("MAILKB_HOME"):
        return Path(os.environ["MAILKB_HOME"]).expanduser()
    return _DEFAULT_HOME


def init_home(home: Path) -> Path:
    """홈 디렉토리와 기본 설정 생성. 이미 있으면 그대로 둠."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "vault" / "daily").mkdir(parents=True, exist_ok=True)
    (home / "vault" / "notes").mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(_TEMPLATE, encoding="utf-8")
    rules_path = home / "ai-rules.md"           # 있으면 사람 기록 — 건드리지 않는다
    if not rules_path.exists():
        rules_path.write_text(_AI_RULES_TEMPLATE, encoding="utf-8")
    return cfg_path


def _load_blocklist(home: Path) -> list[str]:
    """<home>/blocked_senders.txt — 한 줄에 한 패턴(부분 문자열), '#' 주석 허용."""
    path = home / "blocked_senders.txt"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.append(line)
    return out


def add_blocked(cfg: Config, addr: str) -> bool:
    """발신자 패턴을 차단 목록에 추가. 이미 있으면 False. cfg 도 즉시 갱신."""
    addr = (addr or "").strip().lower()
    if not addr:
        return False
    existing = _load_blocklist(cfg.home)
    if addr in existing:
        return False
    path = cfg.blocklist_path
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new_file:
            f.write("# mailkb 차단 발신자 (부분 문자열 매치). Outlook 규칙과 병행.\n")
        f.write(addr + "\n")
    if addr not in cfg.blocked_senders:
        cfg.blocked_senders.append(addr)
    return True


def remove_blocked(cfg: Config, addr: str) -> bool:
    """차단 목록에서 정확히 일치하는 한 줄 제거. 제거했으면 True."""
    addr = (addr or "").strip().lower()
    path = cfg.blocklist_path
    if not addr or not path.exists():
        return False
    kept, removed = [], False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.split("#", 1)[0].strip().lower() == addr:
            removed = True
            continue
        kept.append(raw)
    if removed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        cfg.blocked_senders = [p for p in cfg.blocked_senders if p != addr]
    return removed


def overrides_path(home: Path) -> Path:
    return home / "overrides.json"


def read_overrides(home: Path) -> dict:
    """<home>/overrides.json — 웹 설정 페이지에서 런타임에 바꾼 값(영구).
    config.toml 위에 병합된다. 없거나 깨졌으면 빈 dict."""
    try:
        return json.loads(overrides_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    """중첩 dict 병합(override 우선). 리스트·스칼라는 통째 교체."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def set_override(home: Path, section: str, key: str, value) -> None:
    """overrides.json 의 [section][key] 를 갱신(사람이 안 건드리는 파일이라 안전)."""
    ov = read_overrides(home)
    ov.setdefault(section, {})[key] = value
    overrides_path(home).write_text(
        json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")


def load(cli_home: str | None) -> Config:
    home = resolve_home(cli_home)
    cfg_path = home / "config.toml"
    if not cfg_path.exists():
        raise SystemExit(f"설정 없음: {cfg_path} — 먼저 `mailkb init` 실행")
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    # 웹 설정 페이지가 쓴 오버라이드를 config.toml 위에 병합(영구·주석 무손상).
    data = _deep_merge(data, read_overrides(home))
    review = data.get("review", {})
    return Config(
        home=home,
        my_addresses=data.get("my_addresses", []),
        my_names=data.get("my_names", []),
        source=data.get("source", "fake"),
        ignore_senders=data.get("filters", {}).get("ignore_senders", []),
        internal_domains=data.get("filters", {}).get("internal_domains", []),
        subject_noise_strong=data.get("filters", {}).get(
            "subject_noise_strong", list(_SUBJECT_NOISE_STRONG)),
        subject_noise_weak=data.get("filters", {}).get(
            "subject_noise_weak", list(_SUBJECT_NOISE_WEAK)),
        ai_default=data.get("ai", {}).get("default", "sonnet"),
        ai_summary_backend=data.get("ai", {}).get("summary", "sonnet"),
        ai_search_backend=data.get("ai", {}).get("search", "sonnet"),
        ai_ask_backend=data.get("ai", {}).get("ask", ""),   # 빈 값 → search 상속
        ai_diagnose_backend=data.get("ai", {}).get("diagnose", "opus"),
        ai_backends=data.get("ai", {}).get("backends", {}),
        stall_workdays=review.get("stall_workdays", 2),
        stale_workdays=review.get("stale_workdays", 3),
        broadcast_to=review.get("broadcast_to", 50),
        direct_to=review.get("direct_to", 4),
        holidays=review.get("holidays", []),
        blocked_senders=_load_blocklist(home),
        raw=data,
    )
