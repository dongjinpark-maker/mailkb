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

[filters]
# 이 문자열이 발신 주소에 포함되면 노이즈(공지/자동발송)로 분류
ignore_senders = ["noreply", "no-reply", "notification@", "jira@", "build@"]
# 사내 도메인 — 설정하면 외부 도메인 발신(스팸 등)은 미답변/기한/요약에서 제외.
# 외부 파트너 메일도 추적하려면 빈 리스트로. 예: ["company.co.kr"]
internal_domains = []
# 추가 차단 발신자는 <home>/blocked_senders.txt 에 누적된다 (mailkb block <주소>).
# 실제 수신 차단은 Outlook 규칙으로 병행 — 이 파일은 mailkb 신호에서만 제외.
# 제목 기반 노이즈 2단계 (소문자 부분 매치, 키를 지우면 아래 기본값 적용):
#  - strong: 내 참여 여부와 무관하게 무조건 제외 (시스템 알림/설문 등)
#  - weak:   내가 답장하지 않았고 수신 3인 이상 대량일 때만 제외 (주간보고 등 —
#            내가 논의에 참여한 스레드는 유지)
subject_noise_strong = ["invitation", "notification", "자동회신", "자동 회신",
                        "[nflow]", "[nwork]", "승계통보", "설문요청", "설문 요청"]
subject_noise_weak = ["weekly report", "주간보고", "주간 보고", "[회의록]"]

[ai]
# 폴백 백엔드. 아래 summary/classify 로 라우팅되지 않는 호출이 쓴다.
default = "internal"
# 작업별 백엔드 라우팅 (비용/품질 최적화):
#  - summary  : 메일 본문이 들어가는 '요약/회고/디제스트' → 품질 좋은 sonnet.
#  - classify : 개입 큐 '액션 필요?' 분류 → 값싸고 빠른 haiku.
# sonnet/haiku/internal 은 아래 [ai.backends.*] 를 지워도 내장 기본값으로 동작한다
# (config 에 있으면 그 값이 우선). --backend 를 명시하면 그것이 우선.
# 진짜 미해결 백엔드거나 호출 실패 시 결정론 결과가 그대로 남는다(AI 없어도 동작).
summary = "sonnet"
classify = "haiku"
# 요약·수확 대상 날짜 창: max(마지막 실행일, 오늘−(N−1)) ~ 오늘.
# N=summary_max_days(기본 1 — 오늘만). 하루 이틀 건너뛰는 날의 소급까지
# 원하면 2~3 으로 (그만큼 첫 실행/복귀일 비용 증가).
# summary_max_days = 1
# 요약 최소 스레드 길이: 실질 메시지(++·FYI 제외)가 이 통수 미만이면 요약 안 함.
# 단, 실질 본문 합계가 summary_min_chars(기본 1000자) 이상이면 통수가 적어도 요약
# (장문 기획안 1통 등). min_chars=0 이면 내용 우회로 끔.
# summary_min_msgs = 3
# summary_min_chars = 1000

[ai.backends.internal]
# opencode headless — 프롬프트는 stdin 으로 전달됨
cmd = ["opencode", "run"]

[ai.backends.sonnet]
# claude headless — 요약/회고용(sonnet). --backend sonnet 로도 지정 가능.
cmd = ["claude", "-p", "--model", "sonnet"]

[ai.backends.haiku]
# claude headless — 개입 분류용(default haiku, 특정 버전 고정 안 함).
# 짧은 한국어 의도 분류라 haiku 로 충분하고 sonnet 대비 훨씬 싸다.
cmd = ["claude", "-p", "--model", "haiku"]

[review]
# "개입 필요" 큐의 정체 판정 기준 (영업일 — 주말·아래 holidays 제외)
stall_workdays = 2   # 내가 보낸 메일에 응답 없음 = 정체
stale_workdays = 3   # 열린 스레드 무활동 = 정체
# 대량발송 제외선: 수신인 이 수 이상이면 전사/그룹 공지로 보고 개인 액션 큐에서 제외.
# 조직 규모에 맞춰 조정 — 실무 그룹 메일은 포함되고 팀/전사 공지만 배제되게.
broadcast_to = 50
# 수신인이 이 수 이하이면 '나에게 직접 온 메일'로 보고 개입 큐에 유지.
# 그 이상(그룹메일)은 요청/질문 신호·내 이름 언급·내 참여 스레드일 때만 유지 →
# 요청 없는 그룹 FYI 과탐(false alarm) 제거.
direct_to = 4
# 공휴일 (YYYY-MM-DD) — 영업일 계산에서 제외. 비워두면 주말만 제외.
# 대한민국 공휴일 2026 (대체공휴일 포함). 음력 공휴일이 매년 바뀌므로 연 1회 갱신.
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
]
'''


_AI_RULES_COMMENT_RX = re.compile(r"<!--.*?-->", re.DOTALL)

# 제목 노이즈 기본값 — config.toml 에 키가 없어도 적용 (구버전 설정 호환)
_SUBJECT_NOISE_STRONG = ["invitation", "notification", "자동회신", "자동 회신",
                         "[nflow]", "[nwork]", "승계통보", "설문요청", "설문 요청"]
_SUBJECT_NOISE_WEAK = ["weekly report", "주간보고", "주간 보고", "[회의록]"]

# 내장 백엔드 기본값 — config.toml 에 [ai.backends.<name>] 이 없어도 이 이름들은
# 동작한다. PC config 를 손대지 않아도 요약=sonnet / 분류=haiku 라우팅이 되도록
# (config 에 명시하면 그 값이 우선). internal 은 사내 opencode 기본 호출.
_BUILTIN_BACKENDS = {
    "internal": ["opencode", "run"],
    "sonnet": ["claude", "-p", "--model", "sonnet"],
    "haiku": ["claude", "-p", "--model", "haiku"],
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
    ai_default: str = "internal"
    ai_summary_backend: str = "sonnet"   # 요약/회고/디제스트 전용 (품질 우선)
    ai_classify_backend: str = "haiku"   # 개입 분류 전용 (비용 우선)
    ai_backends: dict = field(default_factory=dict)
    stall_workdays: int = 2
    stale_workdays: int = 3
    broadcast_to: int = 50
    direct_to: int = 4
    holidays: list[str] = field(default_factory=list)
    blocked_senders: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)   # 파싱된 config.toml 원본 (opt 조회용)

    @property
    def db_path(self) -> Path:
        return self.home / "db.sqlite"

    @property
    def vault(self) -> Path:
        return self.home / "vault"

    @property
    def blocklist_path(self) -> Path:
        return self.home / "blocked_senders.txt"

    def is_noise(self, addr: str) -> bool:
        """자동발송/스팸/차단 판정 — 미답변·기한 신호·롤링 요약·개입 큐에서 제외.

        1) ignore_senders 부분 문자열 매치 (noreply, jira@ 등)
        2) blocked_senders 부분 문자열 매치 (mailkb block 으로 누적)
        3) internal_domains 설정 시, 외부 도메인 발신 전부 (스팸 대응)
        """
        addr = (addr or "").lower()
        if any(pat in addr for pat in self.ignore_senders):
            return True
        if any(pat in addr for pat in self.blocked_senders):
            return True
        if self.internal_domains:
            domain = addr.rsplit("@", 1)[-1]
            if not any(domain == d or domain.endswith("." + d)
                       for d in self.internal_domains):
                return True
        return False

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
        """제목 강한 노이즈 — 참여 여부 무관 무조건 제외 (소문자 부분 매치)."""
        s = (subject or "").lower()
        return any(pat in s for pat in self.subject_noise_strong)

    def is_noise_subject_weak(self, subject: str) -> bool:
        """제목 약한 노이즈 후보 — 미참여+대량일 때만 제외 (판정은 review 쪽)."""
        s = (subject or "").lower()
        return any(pat in s for pat in self.subject_noise_weak)

    def ai_rules_text(self) -> str:
        """<home>/ai-rules.md 내용 (HTML 주석 제거) — AI 판단 프롬프트에 주입.

        파일이 없거나 읽기 실패면 빈 문자열(graceful). 호출 시점마다 읽으므로
        파일 수정이 즉시 반영된다. 폭주 방지 상한 4000자.
        """
        try:
            text = (self.home / "ai-rules.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        return _AI_RULES_COMMENT_RX.sub("", text).strip()[:4000]

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
        ai_default=data.get("ai", {}).get("default", "internal"),
        ai_summary_backend=data.get("ai", {}).get("summary", "sonnet"),
        ai_classify_backend=data.get("ai", {}).get("classify", "haiku"),
        ai_backends=data.get("ai", {}).get("backends", {}),
        stall_workdays=review.get("stall_workdays", 2),
        stale_workdays=review.get("stale_workdays", 3),
        broadcast_to=review.get("broadcast_to", 50),
        direct_to=review.get("direct_to", 4),
        holidays=review.get("holidays", []),
        blocked_senders=_load_blocklist(home),
        raw=data,
    )
