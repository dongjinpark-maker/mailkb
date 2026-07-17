"""Deterministic, labeled mail corpus for action-classifier evaluation.

The regular FakeSource stays small enough for the interactive demo.  This
corpus is deliberately larger and stratified: 490 threads / 1,020 messages
cover direct requests, decisions, CC and group targeting, ambiguous requests,
resolved conversations, FYI traffic, automated mail, schedules, and mixed
human/noise threads.

All identities and content are fictional.  Truth records contain synthetic
IDs, authored test text, and expected labels, so the evaluator can report
confusion matrices without touching an operating mailbox.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Mapping

from mailkb.sources.base import MailRecord


ME = "dohyun.kim@nurisoft.co.kr"
ME_ALIAS = "dhkim@nurisoft.co.kr"
CORP_DOMAIN = "nurisoft.co.kr"

ACTION_REQUIRED = "required"
ACTION_MAYBE = "maybe"
ACTION_NONE = "none"

KIND_RESPOND = "respond"
KIND_DECIDE = "decide"
KIND_NONE = "none"

NOISE_NONE = "none"
NOISE_HARD = "hard"
NOISE_POLICY = "policy"
NOISE_MIXED = "mixed"

SCHEDULE_NONE = "none"
SCHEDULE_EVENT = "event"
SCHEDULE_CHANGE = "change"
SCHEDULE_CANCEL = "cancel"
SCHEDULE_INFO = "info"


_PEOPLE = [
    ("김민수 팀장", "minsu.kim@nurisoft.co.kr"),
    ("정우진 수석", "woojin.jung@nurisoft.co.kr"),
    ("윤성호 책임", "seongho.yoon@nurisoft.co.kr"),
    ("이서연 선임", "seoyeon.lee@nurisoft.co.kr"),
    ("오태양 책임", "taeyang.oh@nurisoft.co.kr"),
    ("서지훈 수석", "jihoon.seo@nurisoft.co.kr"),
    ("강미래 선임", "mirae.kang@nurisoft.co.kr"),
    ("한예린 주임", "yerin.han@nurisoft.co.kr"),
    ("박지현 책임", "jihyun.park@nurisoft.co.kr"),
    ("최하늘 주임", "haneul.choi@nurisoft.co.kr"),
    ("김보라 매니저", "bora.kim@nurisoft.co.kr"),
    ("문지호 책임", "jiho.moon@nurisoft.co.kr"),
]

_PROJECTS = [
    "NPX-200 B0", "시큐어부트", "MPW 셔틀", "DDR 트레이닝",
    "NPU 컴파일러", "INT8 QAT", "브링업 보드", "전력 최적화",
    "RTL 린트", "CVE 대응", "양산 테스트", "MLOps 배포",
    "모델 서빙", "DMA 성능", "표준셀 검증", "펌웨어 릴리스",
    "고객 PoC", "패키지 SI", "수율 분석", "키 세리머니",
]

_STRONG_REQUESTS = [
    "첨부한 결과를 검토한 뒤 오늘 중으로 회신 부탁드립니다.",
    "B0 적용 가능 여부를 금요일까지 답변해 주세요.",
    "재현 로그와 분석 의견을 내일 오전까지 보내주세요.",
    "두 대안 중 선호하는 안을 알려주실 수 있을까요?",
    "담당 블록의 최종 수치를 확인해서 공유해 주세요.",
    "문제 없으면 승인 의견을 회신 바랍니다.",
    "배포 전에 체크리스트 검토를 부탁드립니다.",
    "언제 수정본 전달이 가능하신가요?",
    "회의 전에 위험 항목 세 가지를 정리해 주시겠습니까?",
    "Please reply with the owner and expected completion date by EOD.",
    "금요일 배포가 가능하신가요？",
    "아래 질문에 항목별로 답변을 요청드립니다.",
]

_DECISION_REQUESTS = [
    "A안과 B안 중 양산 적용안을 결정해 주세요.",
    "예외 승인 여부를 오늘 안으로 회신 부탁드립니다.",
    "테이프인 진행 가부를 판단해 주시기 바랍니다.",
    "비용 증가안을 승인할 수 있는지 검토 부탁드립니다.",
    "릴리스 중단 여부에 대한 최종 의견을 주세요.",
    "보안 패치 우선순위를 결정해 주시면 반영하겠습니다.",
]

_TARGETED_REQUESTS = [
    "김도현님, 담당 영역의 검토 결과를 회신 부탁드립니다.",
    "도현님은 3번 항목의 승인 여부를 확인해 주세요.",
    "각 담당자는 자신의 블록 상태를 오늘까지 회신 바랍니다.",
    "수신자 전원은 가능한 회의 시간을 알려주세요.",
    "김도현 책임 담당 건으로 확인 후 의견 부탁드립니다.",
]

_WEAK_REQUESTS = [
    "자료 확인 바랍니다.",
    "참고 부탁드립니다.",
    "가능하면 검토해 주시면 감사하겠습니다.",
    "김도현님, 지난 회의 자료 공유드립니다.",
    "향후에도 많은 협조 부탁드립니다.",
    "관련 내용 전달드리니 확인 부탁드립니다.",
    "각 담당자 참고 바랍니다.",
    "일정에 반영할 수 있을지 확인 바랍니다.",
]

_CLOSURES = [
    "확인했습니다. 별도 의견 없습니다.",
    "요청하신 자료를 전달드립니다.",
    "검토 완료했습니다. 이상 없습니다.",
    "승인 처리가 완료되었습니다.",
    "회신 부탁드렸던 건은 내부에서 해결되었습니다.",
    "검토 요청 건을 완료했습니다.",
    "빠른 대응 감사합니다. 좋은 하루 되세요.",
    "현재까지 결과를 정리해 공유드립니다.",
    "문의하신 값은 42이며 추가 조치는 없습니다.",
    "최종 일정은 다음 주 화요일로 확정했습니다.",
]

_FYI = [
    "지난주 측정 결과를 공유드립니다.",
    "회의록과 발표 자료를 첨부합니다.",
    "금주 진행 현황입니다. 참고 바랍니다.",
    "배포가 정상적으로 완료되어 알려드립니다.",
    "품질 지표 대시보드가 갱신되었습니다.",
    "다음 리비전의 변경 목록을 전달드립니다.",
]

_SCHEDULES = [
    (SCHEDULE_EVENT, "리뷰 회의는 7월 22일 오후 2시에 진행합니다."),
    (SCHEDULE_EVENT, "다음 주 월요일 오전 10시에 킥오프 예정입니다."),
    (SCHEDULE_CHANGE, "검토 회의가 수요일에서 금요일로 변경되었습니다."),
    (SCHEDULE_CHANGE, "테이프인 일정이 8월 둘째 주로 연기되었습니다."),
    (SCHEDULE_CANCEL, "오늘 오후 예정이던 회의는 취소되었습니다."),
    (SCHEDULE_CANCEL, "외부 감사 일정이 취소되어 별도 참석은 없습니다."),
    (SCHEDULE_INFO, "7월 개발 일정표와 담당자 목록을 공유드립니다."),
    (SCHEDULE_INFO, "회의실 예약은 금요일 오전 11시입니다."),
]

_SYSTEM_SENDERS = [
    ("시스템 알림", "noreply@nurisoft.co.kr"),
    ("JIRA", "jira@nurisoft.co.kr"),
    ("빌드 서버", "build@nurisoft.co.kr"),
    ("전자결재", "notification@nurisoft.co.kr"),
]

_SYSTEM_SUBJECTS = [
    "[시스템] 상태 변경 알림",
    "[nflow] 결재 상태 통보",
    "JIRA 이슈 업데이트",
    "nightly build notification",
    "자동회신: 부재중",
]

_SIGNATURE = (
    "\n\n--\n{name}\nSoC개발본부 | 내선 {ext}\n"
    "본 메일은 지정된 수신자를 위한 가상의 테스트 메시지입니다."
)


@dataclass(frozen=True)
class MessageTruth:
    message_id: str
    thread_key: str
    authored_body: str
    intent: str
    target: str
    schedule: str = SCHEDULE_NONE
    noise: str = NOISE_NONE


@dataclass(frozen=True)
class ThreadTruth:
    thread_key: str
    action: str
    action_kind: str
    noise: str
    schedule: str
    source_message_id: str
    rationale: str


@dataclass
class SyntheticCorpus:
    records: list[MailRecord]
    threads: dict[str, ThreadTruth]
    messages: dict[str, MessageTruth]


@dataclass(frozen=True)
class _Spec:
    sender_name: str
    sender_addr: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    intent: str
    target: str
    schedule: str = SCHEDULE_NONE
    noise: str = NOISE_NONE


class _CorpusBuilder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self._person_offset = self.rng.randrange(len(_PEOPLE))
        self._project_offset = self.rng.randrange(len(_PROJECTS))
        self._minute_offset = self.rng.randrange(60)
        self.records: list[MailRecord] = []
        self.threads: dict[str, ThreadTruth] = {}
        self.messages: dict[str, MessageTruth] = {}
        self._record_seq = 0

    def person(self, index: int) -> tuple[str, str]:
        return _PEOPLE[(index + self._person_offset) % len(_PEOPLE)]

    def project(self, index: int) -> str:
        return _PROJECTS[(index + self._project_offset) % len(_PROJECTS)]

    def _times(self, last_days_ago: int, count: int, index: int) -> list[datetime]:
        now = datetime.now()
        last = (now - timedelta(days=last_days_ago)).replace(
            hour=8 + index % 11,
            minute=(index * 7 + self._minute_offset) % 60,
            second=0,
            microsecond=0,
        )
        if last_days_ago == 0:
            last = min(
                last,
                now.replace(second=0, microsecond=0)
                - timedelta(minutes=1 + index % 30),
            )
        step = timedelta(hours=3 + index % 5)
        return [last - step * (count - 1 - i) for i in range(count)]

    @staticmethod
    def _decorate(body: str, sequence: int, human: bool) -> str:
        authored = body.strip()
        if not human:
            return authored
        if sequence % 5 == 0:
            authored = "안녕하세요.\n\n" + authored
        if sequence % 6 == 0:
            authored += "\n\n참고로 수치는 내부 검증 환경 기준입니다."
        if sequence % 4 == 0 and not authored.endswith("감사합니다."):
            authored += "\n\n감사합니다."
        return authored

    @staticmethod
    def _quote(parent: MailRecord, korean: bool) -> str:
        if korean:
            return (
                "\n\n________________________________\n"
                f"보낸 사람: {parent.sender_name} <{parent.sender_addr}>\n"
                f"보낸 날짜: {parent.sent_on}\n"
                f"받는 사람: {'; '.join(parent.to)}\n"
                f"제목: {parent.subject}\n\n{parent.body_text}"
            )
        return (
            "\n\n-----Original Message-----\n"
            f"From: {parent.sender_name} <{parent.sender_addr}>\n"
            f"Sent: {parent.sent_on}\n"
            f"To: {'; '.join(parent.to)}\n"
            f"Subject: {parent.subject}\n\n{parent.body_text}"
        )

    @staticmethod
    def _html_body(authored: str, parent: MailRecord | None) -> str:
        parts = [
            "<p>" + html.escape(line) + "</p>"
            for line in authored.splitlines()
            if line.strip()
        ]
        if parent is not None:
            parts.extend([
                "<div>-----Original Message-----</div>",
                f"<div>From: {html.escape(parent.sender_name)}</div>",
                f"<div>Subject: {html.escape(parent.subject)}</div>",
                f"<blockquote>{html.escape(parent.body_text)}</blockquote>",
            ])
        return "".join(parts)

    def add_thread(
        self,
        *,
        key: str,
        specs: list[_Spec],
        last_days_ago: int,
        action: str,
        action_kind: str,
        noise: str = NOISE_NONE,
        schedule: str = SCHEDULE_NONE,
        source_index: int | None = None,
        rationale: str,
    ) -> None:
        times = self._times(last_days_ago, len(specs), len(self.threads))
        parent: MailRecord | None = None
        source_message_id = ""
        for i, (spec, when) in enumerate(zip(specs, times)):
            message_id = f"<synthetic-{key}-{i}@mailkb.example>"
            human = not (
                spec.noise == NOISE_HARD
                or spec.sender_addr.startswith(("noreply", "jira@", "build@", "notification@"))
            )
            authored = self._decorate(spec.body, self._record_seq, human)
            full_body = authored
            if human:
                if spec.sender_addr.endswith("@" + CORP_DOMAIN):
                    full_body += _SIGNATURE.format(
                        name=spec.sender_name,
                        ext=1000 + self._record_seq % 8000,
                    )
                else:
                    full_body += (
                        f"\n\n--\n{spec.sender_name}\n"
                        "External Partner Technical Support"
                    )
            if parent is not None:
                full_body += self._quote(parent, korean=(self._record_seq % 2 == 0))
            attachment = []
            if self._record_seq % 7 == 0:
                attachment = [
                    ("검토결과.xlsx", "회의자료.pptx", "로그.txt", "일정표.pdf")[
                        self._record_seq % 4
                    ]
                ]
            body_html = (
                self._html_body(authored, parent)
                if self._record_seq % 3 != 0
                else ""
            )
            rec = MailRecord(
                message_id=message_id,
                subject=spec.subject if i == 0 else f"RE: {spec.subject}",
                sender_name=spec.sender_name,
                sender_addr=spec.sender_addr,
                to=list(spec.to),
                cc=list(spec.cc),
                sent_on=when.isoformat(timespec="seconds"),
                body_text=full_body,
                body_html=body_html,
                entry_id=f"SYNTHETIC-{self._record_seq:06d}",
                in_reply_to=parent.message_id if parent else "",
                references=[parent.message_id] if parent else [],
                conversation_key=f"SYNTHETIC-{key}",
                attachments=attachment,
                folder="sent" if spec.sender_addr in {ME, ME_ALIAS} else "inbox",
            )
            self.records.append(rec)
            self.messages[message_id] = MessageTruth(
                message_id=message_id,
                thread_key=key,
                authored_body=authored,
                intent=spec.intent,
                target=spec.target,
                schedule=spec.schedule,
                noise=spec.noise,
            )
            if source_index == i:
                source_message_id = message_id
            parent = rec
            self._record_seq += 1
        self.threads[key] = ThreadTruth(
            thread_key=key,
            action=action,
            action_kind=action_kind,
            noise=noise,
            schedule=schedule,
            source_message_id=source_message_id,
            rationale=rationale,
        )

    def build(self) -> SyntheticCorpus:
        self.records.sort(key=lambda r: (r.sent_on, r.message_id))
        return SyntheticCorpus(self.records, self.threads, self.messages)


def _human_spec(
    sender: tuple[str, str],
    to: list[str],
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    intent: str = "neutral",
    target: str = "none",
    schedule: str = SCHEDULE_NONE,
) -> _Spec:
    return _Spec(
        sender[0], sender[1], to, cc or [], subject, body,
        intent, target, schedule,
    )


def _my_spec(
    to: list[str],
    subject: str,
    body: str,
    *,
    intent: str = "neutral",
    alias: bool = False,
) -> _Spec:
    return _Spec(
        "김도현", ME_ALIAS if alias else ME, to, [], subject, body,
        intent, "outbound",
    )


def build_synthetic_corpus(seed: int = 20260717) -> SyntheticCorpus:
    """Build 1,020 messages in 490 labeled, deterministic mail threads."""
    b = _CorpusBuilder(seed)

    # 50 direct response requests, including English, full-width punctuation,
    # subject-only requests, and trailing greetings.
    for i in range(50):
        person = b.person(i)
        project = b.project(i)
        subject = f"[{project}] 검토 요청 {i + 1:02d}"
        body = _STRONG_REQUESTS[i % len(_STRONG_REQUESTS)]
        if i % 10 == 0:
            subject = f"[{project}] 회신 요청: 담당자와 완료일 {i + 1:02d}"
            body = "관련 자료를 공유드립니다. 상세 내용은 첨부를 참고해 주세요."
        if i % 4 == 0:
            body += "\n감사합니다. 좋은 하루 되세요."
        b.add_thread(
            key=f"required-direct-{i:03d}",
            specs=[
                _my_spec(
                    [person[1]], subject, "초안과 확인 항목을 전달드립니다.",
                    alias=i % 17 == 0,
                ),
                _human_spec(
                    person, [ME], subject, body,
                    intent="strong_request", target="direct",
                ),
            ],
            last_days_ago=i % 12,
            action=ACTION_REQUIRED,
            action_kind=KIND_RESPOND,
            source_index=1,
            rationale="direct strong request",
        )

    # 25 explicit decision/approval requests.
    for i in range(25):
        person = b.person(i + 3)
        project = b.project(i + 5)
        subject = f"[{project}] 의사결정 필요 {i + 1:02d}"
        b.add_thread(
            key=f"required-decide-{i:03d}",
            specs=[
                _human_spec(person, [ME], subject, "대안별 비용과 일정을 정리했습니다."),
                _human_spec(
                    person, [ME], subject,
                    _DECISION_REQUESTS[i % len(_DECISION_REQUESTS)],
                    intent="decision_request", target="direct",
                ),
            ],
            last_days_ago=i % 10,
            action=ACTION_REQUIRED,
            action_kind=KIND_DECIDE,
            source_index=1,
            rationale="explicit decision request",
        )

    # 25 CC/group requests where recipient count alone must not decide.
    # CC + collective call-out ("전원/각 담당자") without the user's name stays
    # MAYBE — CC is an FYI convention, recoverable in the review fold
    # (user-confirmed, 2026-07-17).  Name mentions and To-recipients stay
    # REQUIRED.
    for i in range(25):
        person = b.person(i + 6)
        project = b.project(i + 9)
        subject = f"[{project}] 담당자 확인 {i + 1:02d}"
        mode = i % 3
        if mode == 0:
            to = [b.person(i + 1)[1]]
            cc = [ME, b.person(i + 2)[1]]
            target = "cc_mention"
        elif mode == 1:
            to = [ME] + [b.person(i + j + 1)[1] for j in range(5)]
            cc = []
            target = "group_mention"
        else:
            to = [ME, b.person(i + 1)[1], b.person(i + 2)[1]]
            cc = []
            target = "group_role"
        request = _TARGETED_REQUESTS[i % len(_TARGETED_REQUESTS)]
        cc_collective = mode == 0 and ("김도현" not in request and "도현" not in request)
        b.add_thread(
            key=f"required-targeted-{i:03d}",
            specs=[
                _human_spec(person, to, subject, "현재 상태와 이슈를 공유드립니다.", cc=cc),
                _human_spec(
                    person, to, subject, request,
                    cc=cc, intent="strong_request", target=target,
                ),
            ],
            last_days_ago=i % 12,
            action=ACTION_MAYBE if cc_collective else ACTION_REQUIRED,
            action_kind=KIND_RESPOND,
            source_index=1,
            rationale=(
                "collective request seen via CC — review candidate"
                if cc_collective
                else "CC or group request explicitly targets the user"
            ),
        )

    # 60 ambiguous or formulaic threads.  Genuinely weak requests should be
    # recoverable in MAYBE; pure pleasantries ("참고/협조 부탁드립니다") are the
    # false-positive class that motivated the classifier rework, so their gold
    # label is NONE (user-confirmed, 2026-07-17).
    _PLEASANTRY_BODY = {1, 4, 6}     # indexes into _WEAK_REQUESTS
    for i in range(60):
        person = b.person(i + 2)
        project = b.project(i + 11)
        external = i % 10 == 0
        if external:
            person = (f"외부 협력사 담당자{i:02d}", f"partner{i:02d}@foundry.example")
        subject = f"[{project}] 참고 및 확인 {i + 1:02d}"
        extra_recipients = 4 if i % 4 == 0 else 1
        to = [ME] + [
            b.person(i + j + 1)[1] for j in range(extra_recipients)
        ]
        pleasantry = i % len(_WEAK_REQUESTS) in _PLEASANTRY_BODY
        b.add_thread(
            key=(f"pleasantry-{i:03d}" if pleasantry else f"maybe-{i:03d}"),
            specs=[
                _human_spec(person, to, subject, "관련 배경과 이전 결론을 정리했습니다."),
                _human_spec(
                    person, to, subject,
                    _WEAK_REQUESTS[i % len(_WEAK_REQUESTS)],
                    intent="pleasantry" if pleasantry else "weak_request",
                    target="external" if external else ("group" if len(to) > 2 else "direct"),
                ),
            ],
            last_days_ago=i % 15,
            action=ACTION_NONE if pleasantry else ACTION_MAYBE,
            action_kind=KIND_NONE if pleasantry else KIND_RESPOND,
            noise=NOISE_POLICY if external else NOISE_NONE,
            source_index=None if pleasantry else 1,
            rationale=(
                "closing pleasantry is FYI, not a request"
                if pleasantry
                else "weak, group, or external request needs user review"
            ),
        )

    # 80 resolved conversations. Variants end in my substantive reply, the
    # other party's thanks, or an explicit withdrawal.
    for i in range(80):
        person = b.person(i + 4)
        project = b.project(i + 2)
        subject = f"[{project}] 처리 완료 대화 {i + 1:02d}"
        request = _STRONG_REQUESTS[i % len(_STRONG_REQUESTS)]
        variant = i % 3
        if variant == 0:
            specs = [
                _human_spec(person, [ME], subject, request, intent="strong_request", target="direct"),
                _my_spec(
                    [person[1]], subject, "검토 결과와 수정본을 회신드립니다.",
                    intent="substantive_reply", alias=i % 19 == 0,
                ),
                _human_spec(person, [ME], subject, "확인했습니다. 빠른 대응 감사합니다.", intent="completion", target="direct"),
            ]
        elif variant == 1:
            specs = [
                _human_spec(person, [ME], subject, "관련 배경을 먼저 공유드립니다."),
                _human_spec(person, [ME], subject, request, intent="strong_request", target="direct"),
                _my_spec(
                    [person[1]], subject, "요청하신 항목을 반영했습니다.",
                    intent="substantive_reply", alias=i % 19 == 0,
                ),
            ]
        else:
            specs = [
                _human_spec(person, [ME], subject, request, intent="strong_request", target="direct"),
                _human_spec(person, [ME], subject, "일정상 내일까지 확인 부탁드립니다.", intent="reminder", target="direct"),
                _human_spec(person, [ME], subject, "문제가 해결되어 기존 요청은 무시해 주세요.", intent="withdrawal", target="direct"),
            ]
        b.add_thread(
            key=f"resolved-{i:03d}",
            specs=specs,
            last_days_ago=i % 30,
            action=ACTION_NONE,
            action_kind=KIND_NONE,
            rationale="request was answered or explicitly withdrawn",
        )

    # 100 normal non-action threads: FYI, answers to my question, completion
    # sentences containing historical request words, and closing greetings.
    for i in range(100):
        person = b.person(i + 1)
        project = b.project(i + 7)
        subject = f"[{project}] 진행 현황 공유 {i + 1:03d}"
        if i % 2 == 0:
            first = _my_spec(
                [person[1]], subject, "현재 값과 완료 일정을 문의드립니다.",
                intent="outbound_question", alias=i % 23 == 0,
            )
        else:
            first = _human_spec(person, [ME], subject, _FYI[i % len(_FYI)], intent="fyi", target="direct")
        b.add_thread(
            key=f"none-{i:03d}",
            specs=[
                first,
                _human_spec(
                    person, [ME], subject,
                    _CLOSURES[i % len(_CLOSURES)],
                    intent="completion", target="direct",
                ),
            ],
            last_days_ago=i % 45,
            action=ACTION_NONE,
            action_kind=KIND_NONE,
            rationale="FYI, answer, or completed historical request",
        )

    # 70 noise messages from automated systems and external promotions.
    # Hard = explicitly configured senders/subjects (ignore/blocked/strong
    # subject); external newsletters are policy noise — they become hard only
    # when the user blocks them (definition unified with the classifier,
    # 2026-07-17).
    for i in range(70):
        if i % 7 == 0:
            sender = (f"외부 뉴스레터{i:02d}", f"news{i:02d}@marketing.example")
            noise = NOISE_POLICY
            subject = f"무료 웨비나 및 기술 뉴스레터 #{i + 1:04d}"
            body = "오늘까지 신청하면 무료 자료를 받을 수 있습니다."
        else:
            sender = _SYSTEM_SENDERS[i % len(_SYSTEM_SENDERS)]
            noise = NOISE_HARD
            subject = (
                f"{_SYSTEM_SUBJECTS[i % len(_SYSTEM_SUBJECTS)]} "
                f"#{i + 1:04d}"
            )
            body = "상태가 변경되었습니다. 상세 내용은 시스템에서 확인 바랍니다."
        spec = _Spec(
            sender[0], sender[1], [ME], [], subject, body,
            "automated", "direct", SCHEDULE_NONE, noise,
        )
        b.add_thread(
            key=f"noise-{i:03d}",
            specs=[spec],
            last_days_ago=i % 30,
            action=ACTION_NONE,
            action_kind=KIND_NONE,
            noise=noise,
            rationale="automated sender or promotional mail",
        )

    # 50 schedule-only threads. Dates, changes, cancellations, and schedule
    # sharing are knowledge signals but not reply obligations.
    for i in range(50):
        person = b.person(i + 5)
        project = b.project(i + 13)
        schedule, body = _SCHEDULES[i % len(_SCHEDULES)]
        subject = f"[{project}] 일정 안내 {i + 1:02d}"
        b.add_thread(
            key=f"schedule-{i:03d}",
            specs=[
                _human_spec(person, [ME, b.person(i + 1)[1]], subject, "관련 일정 배경을 공유드립니다."),
                _human_spec(
                    person, [ME, b.person(i + 1)[1]], subject, body,
                    intent="schedule", target="group", schedule=schedule,
                ),
            ],
            last_days_ago=i % 20,
            action=ACTION_NONE,
            action_kind=KIND_NONE,
            schedule=schedule,
            rationale="schedule information without a response request",
        )

    # 30 mixed threads: legitimate conversation followed by a system message
    # that contains request/deadline language. The latest noise message must
    # not reopen the resolved human action.
    for i in range(30):
        person = b.person(i + 8)
        project = b.project(i + 4)
        subject = f"[{project}] 혼합 알림 스레드 {i + 1:02d}"
        system = _SYSTEM_SENDERS[i % len(_SYSTEM_SENDERS)]
        noise_spec = _Spec(
            system[0], system[1], [ME], [], subject,
            "오늘까지 수신 여부를 회신 바랍니다.",
            "automated_request", "direct", SCHEDULE_NONE, NOISE_HARD,
        )
        b.add_thread(
            key=f"mixed-{i:03d}",
            specs=[
                _human_spec(person, [ME], subject, "참고 자료와 결과를 공유드립니다.", intent="fyi", target="direct"),
                _my_spec(
                    [person[1]], subject, "확인했습니다. 감사합니다.",
                    intent="substantive_reply", alias=i % 11 == 0,
                ),
                noise_spec,
            ],
            last_days_ago=i % 10,
            action=ACTION_NONE,
            action_kind=KIND_NONE,
            noise=NOISE_MIXED,
            rationale="latest automated message must not create a thread action",
        )

    return b.build()


def evaluate_action_predictions(
    corpus: SyntheticCorpus,
    predictions: Mapping[str, str],
) -> dict:
    """Return an API-independent action confusion matrix and REQUIRED metrics.

    ``predictions`` is keyed by ``ThreadTruth.thread_key`` and must contain one
    of ``required``, ``maybe``, or ``none`` for every truth thread.
    """
    expected_keys = set(corpus.threads)
    actual_keys = set(predictions)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(
            f"prediction keys differ: missing={missing[:5]}, extra={extra[:5]}"
        )
    labels = (ACTION_REQUIRED, ACTION_MAYBE, ACTION_NONE)
    confusion = {gold: {pred: 0 for pred in labels} for gold in labels}
    false_alarms: list[str] = []
    misses: list[str] = []
    maybe_recovery: list[str] = []
    for key, truth in corpus.threads.items():
        predicted = predictions[key]
        if predicted not in labels:
            raise ValueError(f"invalid action prediction for {key}: {predicted}")
        confusion[truth.action][predicted] += 1
        if predicted == ACTION_REQUIRED and truth.action != ACTION_REQUIRED:
            false_alarms.append(key)
        if truth.action == ACTION_REQUIRED and predicted != ACTION_REQUIRED:
            misses.append(key)
            if predicted == ACTION_MAYBE:
                maybe_recovery.append(key)

    tp = confusion[ACTION_REQUIRED][ACTION_REQUIRED]
    fp = sum(
        confusion[gold][ACTION_REQUIRED]
        for gold in (ACTION_MAYBE, ACTION_NONE)
    )
    fn = sum(
        confusion[ACTION_REQUIRED][pred]
        for pred in (ACTION_MAYBE, ACTION_NONE)
    )
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "confusion": confusion,
        "required": {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        },
        "false_alarms": false_alarms,
        "misses": misses,
        "maybe_recovery": maybe_recovery,
    }


def evaluate_axis_predictions(
    corpus: SyntheticCorpus,
    predictions: Mapping[str, str],
    axis: str,
) -> dict:
    """Evaluate exact labels for ``action``, ``noise``, or ``schedule``."""
    labels_by_axis = {
        "action": (ACTION_REQUIRED, ACTION_MAYBE, ACTION_NONE),
        "noise": (NOISE_HARD, NOISE_POLICY, NOISE_MIXED, NOISE_NONE),
        "schedule": (
            SCHEDULE_EVENT,
            SCHEDULE_CHANGE,
            SCHEDULE_CANCEL,
            SCHEDULE_INFO,
            SCHEDULE_NONE,
        ),
    }
    if axis not in labels_by_axis:
        raise ValueError(f"unknown truth axis: {axis}")
    expected_keys = set(corpus.threads)
    actual_keys = set(predictions)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(
            f"prediction keys differ: missing={missing[:5]}, extra={extra[:5]}"
        )
    labels = labels_by_axis[axis]
    confusion = {gold: {pred: 0 for pred in labels} for gold in labels}
    mismatches: list[str] = []
    for key, truth in corpus.threads.items():
        predicted = predictions[key]
        if predicted not in labels:
            raise ValueError(
                f"invalid {axis} prediction for {key}: {predicted}"
            )
        gold = getattr(truth, axis)
        confusion[gold][predicted] += 1
        if predicted != gold:
            mismatches.append(key)
    correct = len(corpus.threads) - len(mismatches)
    return {
        "axis": axis,
        "accuracy": correct / len(corpus.threads),
        "correct": correct,
        "total": len(corpus.threads),
        "confusion": confusion,
        "mismatches": mismatches,
    }


class SyntheticEvaluationSource:
    """Source adapter for loading the labeled corpus into a Store."""

    name = "synthetic-evaluation"

    def __init__(self, seed: int = 20260717):
        self.corpus = build_synthetic_corpus(seed)

    def fetch(
        self,
        since_iso: str | None,
        image_cutoff: str | None = None,
    ) -> Iterator[MailRecord]:
        del image_cutoff
        for record in self.corpus.records:
            if since_iso and record.sent_on <= since_iso:
                continue
            yield record
