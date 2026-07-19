"""인물 업무 어휘 지도 — AI 없이 메일 단위 특징어·구문·공기어를 계산한다.

단순 출현 횟수 대신 한 메일에서 여러 번 반복한 단어도 지지 메일 1통으로 세고,
다른 인물 메일에도 흔한 단어는 낮춘다. 결과는 JSON 직렬화 가능한 dict라 웹과
SQLite 파생 캐시가 같은 계산 결과를 공유한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import report
from .clean import normalize_subject, strip_preserved

WORDMAP_VERSION = 1
RECENT_WEEKS = 6
MIN_SUPPORT = 2

_SENTENCE_RX = re.compile(r"[\n.!?？;:]+")
_SIGNATURE_RX = re.compile(r"^\s*--\s*$")
_TAIL_RX = re.compile(
    r"^\s*(?:보낸\s*사람|sent\s+from\s+my|"
    r"(?:이|본)\s*(?:전자\s*)?메일은.{0,30}(?:기밀|비밀|보안)|"
    r"(?:mobile|휴대폰|전화|내선|직통)\s*[:：])",
    re.IGNORECASE,
)
_TITLE_WORDS = frozenset(
    "대표 이사 상무 전무 본부장 실장 팀장 수석 책임 선임 주임 사원 대리 과장 차장 부장"
    .split()
)


def _value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, default)
    return default if value is None else value


def _strip_signature(text: str) -> str:
    """저장 단계에서 놓친 짧은 서명·고지 꼬리를 어휘 입력에서 한 번 더 걷는다."""
    lines = strip_preserved(text or "").splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        if i and (_SIGNATURE_RX.match(line) or _TAIL_RX.match(line)):
            cut = i
            break
    return "\n".join(lines[:cut]).strip()


def _aliases(name: str) -> set[str]:
    """표시 이름에서 실제 이름 후보를 보수적으로 만든다."""
    raw = re.sub(r"[<>()\[\],/|]", " ", name or "")
    out = set()
    for part in raw.split():
        if part in _TITLE_WORDS:
            continue
        if re.fullmatch(r"[가-힣]{2,4}", part):
            out.add(part)
        elif re.fullmatch(r"[A-Za-z][A-Za-z.\-]{2,}", part):
            out.add(part.lower())
    return out


def _stop_set(extra_stop=()) -> frozenset[str]:
    extra = {str(v).strip() for v in extra_stop if str(v).strip()}
    return report.WORD_STOP | frozenset(extra) | frozenset(v.lower() for v in extra)


def _token_sentences(text: str, stop: frozenset[str]) -> tuple[list[list[str]], Counter]:
    """본문을 정규화 토큰 문장으로 바꾸고, 정규형별 대표 표기를 모은다."""
    body = report._URL_RX.sub(" ", _strip_signature(text))
    sentences: list[list[str]] = []
    surfaces: Counter = Counter()
    for sentence in _SENTENCE_RX.split(body):
        tokens = []
        for token in report._WORD_RX.findall(sentence):
            surface = report._stem(token)
            key = surface.lower() if surface and surface[0].isascii() else surface
            if len(key) < 2 or key in stop or key.lower() in stop:
                continue
            tokens.append(key)
            surfaces[(key, surface)] += 1
        if tokens:
            sentences.append(tokens)
    return sentences, surfaces


def _display_map(surfaces: Counter) -> dict[str, str]:
    by_key: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (key, surface), count in surfaces.items():
        by_key[key].append((count, surface))
    return {
        key: sorted(values, key=lambda x: (-x[0], x[1].lower()))[0][1]
        for key, values in by_key.items()
    }


def _score(support: int, total: int, other_support: int, other_total: int,
           phrase: bool = False) -> tuple[float, float]:
    """메일 지지도와 다른 인물 대비 lift를 결합한 결정론 점수."""
    own_rate = (support + 0.5) / (total + 1)
    other_rate = (other_support + 0.5) / (other_total + 1)
    lift = math.log(own_rate / other_rate)
    distinctiveness = 0.35 + max(0.0, lift)
    score = math.log1p(support) * math.sqrt(support / max(1, total))
    score *= distinctiveness * (1.18 if phrase else 1.0)
    return score, lift


def _evidence(docs: list[dict], item, kind: str) -> list[int]:
    out = []
    for doc in reversed(docs):
        bag = doc["phrases"] if kind == "phrase" else doc["terms"]
        if item in bag and doc["thread_id"] not in out:
            out.append(doc["thread_id"])
            if len(out) == 3:
                break
    return out


def _npmi(a: str, b: str, docs: list[dict], df: Counter,
          adjacent: Counter) -> float:
    co = sum(1 for d in docs if a in d["terms"] and b in d["terms"])
    if co < MIN_SUPPORT:
        return 0.0
    n = len(docs)
    pab = co / n
    if pab >= 1.0:
        assoc = 0.0
    else:
        assoc = math.log(pab / ((df[a] / n) * (df[b] / n))) / -math.log(pab)
    pair = tuple(sorted((a, b)))
    adjacency = adjacent[pair] / max(1, min(df[a], df[b]))
    return max(assoc, adjacency * 0.35)


def _clusters(items: list[dict], docs: list[dict]) -> list[list[dict]]:
    """선택 특징어를 평균 연결 공기어 군집으로 묶는다."""
    if len(items) < 2:
        return []
    df = Counter()
    adjacent = Counter()
    for doc in docs:
        df.update(doc["terms"])
        for phrase in doc["phrases"]:
            adjacent[tuple(sorted(phrase))] += 1
    weights = {}
    keys = [item["_key"] for item in items]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            weights[tuple(sorted((a, b)))] = _npmi(a, b, docs, df, adjacent)

    groups = [[item] for item in items]
    while True:
        best = (0.0, -1, -1)
        for i, left in enumerate(groups):
            for j in range(i + 1, len(groups)):
                right = groups[j]
                if len(left) + len(right) > 6:
                    continue
                vals = []
                for a in left:
                    for b in right:
                        key = tuple(sorted((a["_key"], b["_key"])))
                        vals.append(weights.get(key, 0.0))
                avg = sum(vals) / len(vals)
                if avg > best[0]:
                    best = (avg, i, j)
        if best[0] < 0.12:
            break
        _, i, j = best
        groups[i] = groups[i] + groups[j]
        del groups[j]
    return [g for g in groups if len(g) >= 2]


def profile_signature(extra_stop, eligible_addrs, top_n: int,
                      person_name: str, people_names=None) -> str:
    """설정·대조 집단 변화까지 포함한 파생 캐시 버전."""
    payload = {
        "v": WORDMAP_VERSION,
        "stop": sorted(str(v).strip().lower() for v in extra_stop if str(v).strip()),
        "eligible": sorted(a.lower() for a in eligible_addrs),
        "top": int(top_n),
        "name": person_name.strip().lower(),
        "people_names": sorted(
            (str(addr).lower(), str(name or "").strip().lower())
            for addr, name in (people_names or {}).items()
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"{WORDMAP_VERSION}:{hashlib.sha256(raw).hexdigest()[:16]}"


def analyze(rows, target_addr: str, names: dict[str, str] | None = None,
            extra_stop=(), top_n: int = 25,
            recent_weeks: int = RECENT_WEEKS) -> dict:
    """최근 창의 수신 메일 행에서 한 인물의 업무 어휘 지도를 만든다.

    rows는 같은 시간창의 대조 대상 전원 메일이다. 각 항목의 support는 횟수가
    아니라 그 표현이 나타난 메일 수이고, evidence는 최신 근거 스레드 ID다.
    """
    target_addr = (target_addr or "").lower()
    names = {str(k).lower(): str(v or "") for k, v in (names or {}).items()}
    global_stop = _stop_set(extra_stop)
    alias_by_addr = {addr: _aliases(name) for addr, name in names.items()}
    all_person_tokens = set().union(*alias_by_addr.values()) if alias_by_addr else set()
    docs: list[dict] = []
    surfaces: Counter = Counter()
    seen_subjects: set[tuple[str, int]] = set()

    ordered = sorted(rows, key=lambda r: (
        str(_value(r, "sent_on")), int(_value(r, "id", 0))))
    for row in ordered:
        addr = str(_value(row, "sender_addr")).lower()
        if not addr:
            continue
        own_stop = global_stop | frozenset(alias_by_addr.get(addr, set()))
        body_sentences, body_surfaces = _token_sentences(
            str(_value(row, "new_content")), own_stop)
        sentences = body_sentences
        tid = int(_value(row, "thread_id", 0))
        subject_key = (addr, tid)
        if subject_key not in seen_subjects:
            seen_subjects.add(subject_key)
            subject = normalize_subject(str(_value(row, "subject")))
            subject_sentences, subject_surfaces = _token_sentences(subject, own_stop)
            sentences = subject_sentences + sentences
            body_surfaces.update(subject_surfaces)

        terms = {token for sentence in sentences for token in sentence}
        mentioned_tokens = set(terms)
        # 사람 이름은 업무 특징어에서 분리해 별도 "함께 언급" 신호로 보여준다.
        terms -= all_person_tokens
        phrases = {
            (a, b)
            for sentence in sentences
            for a, b in zip(sentence, sentence[1:])
            if a != b and a not in all_person_tokens and b not in all_person_tokens
        }
        docs.append({
            "id": int(_value(row, "id", 0)),
            "thread_id": tid,
            "addr": addr,
            "sent_on": str(_value(row, "sent_on")),
            "terms": terms,
            "phrases": phrases,
            "mentioned_tokens": mentioned_tokens,
        })
        if addr == target_addr:
            surfaces.update(body_surfaces)

    target_docs = [d for d in docs if d["addr"] == target_addr]
    other_docs = [d for d in docs if d["addr"] != target_addr]
    if not target_docs:
        return {"mail_count": 0, "clusters": [], "terms": [], "phrases": [],
                "rising": [], "mentions": []}

    own_df = Counter()
    own_phrase_df = Counter()
    other_df = Counter()
    other_phrase_df = Counter()
    for doc in target_docs:
        own_df.update(doc["terms"])
        own_phrase_df.update(doc["phrases"])
    for doc in other_docs:
        other_df.update(doc["terms"])
        other_phrase_df.update(doc["phrases"])
    display = _display_map(surfaces)

    term_items = []
    for key, support in own_df.items():
        if support < MIN_SUPPORT:
            continue
        score, lift = _score(
            support, len(target_docs), other_df[key], len(other_docs))
        term_items.append({
            "_key": key,
            "term": display.get(key, key),
            "support": support,
            "score": round(score, 4),
            "lift": round(lift, 3),
            "evidence": _evidence(target_docs, key, "term"),
        })
    term_items.sort(key=lambda x: (-x["score"], -x["support"], x["term"].lower()))
    term_items = term_items[:max(1, top_n)]

    phrase_items = []
    for key, support in own_phrase_df.items():
        if support < MIN_SUPPORT:
            continue
        score, lift = _score(
            support, len(target_docs), other_phrase_df[key], len(other_docs),
            phrase=True)
        phrase_items.append({
            "term": " ".join(display.get(v, v) for v in key),
            "support": support,
            "score": round(score, 4),
            "lift": round(lift, 3),
            "evidence": _evidence(target_docs, key, "phrase"),
        })
    phrase_items.sort(key=lambda x: (-x["score"], -x["support"], x["term"].lower()))

    groups = _clusters(term_items, target_docs)
    grouped_keys = {item["_key"] for group in groups for item in group}
    cluster_items = []
    for group in groups:
        keys = {item["_key"] for item in group}
        label = next((
            p["term"] for p in phrase_items
            if set(p["term"].lower().split()) <= keys
        ), " · ".join(item["term"] for item in group[:2]))
        clean_group = [{k: v for k, v in item.items() if k != "_key"} for item in group]
        cluster_items.append({
            "label": label,
            "score": round(sum(i["score"] for i in group), 4),
            "terms": clean_group,
        })
    cluster_items.sort(key=lambda x: -x["score"])

    singles = [
        {k: v for k, v in item.items() if k != "_key"}
        for item in term_items if item["_key"] not in grouped_keys
    ]

    rising = []
    try:
        asof = max(datetime.fromisoformat(d["sent_on"]) for d in target_docs)
    except ValueError:
        asof = None
    if asof is not None:
        cutoff = asof - timedelta(weeks=recent_weeks)
        recent = [d for d in target_docs
                  if datetime.fromisoformat(d["sent_on"]) >= cutoff]
        older = [d for d in target_docs
                 if datetime.fromisoformat(d["sent_on"]) < cutoff]
        if len(recent) >= 2 and len(older) >= 3:
            recent_df, older_df = Counter(), Counter()
            for doc in recent:
                recent_df.update(doc["terms"])
            for doc in older:
                older_df.update(doc["terms"])
            for item in term_items:
                key = item["_key"]
                if recent_df[key] < 2:
                    continue
                rr = (recent_df[key] + 0.5) / (len(recent) + 1)
                old = (older_df[key] + 0.5) / (len(older) + 1)
                ratio = rr / old
                if ratio >= 1.8:
                    rising.append({
                        "term": item["term"],
                        "support": recent_df[key],
                        "ratio": round(ratio, 1),
                        "evidence": _evidence(recent, key, "term"),
                    })
            rising.sort(key=lambda x: (-x["ratio"], -x["support"], x["term"].lower()))

    mentions = []
    for addr, aliases in alias_by_addr.items():
        if addr == target_addr or not aliases:
            continue
        matched = [
            d for d in target_docs
            if any(alias in d["mentioned_tokens"] for alias in aliases)
        ]
        if len(matched) < MIN_SUPPORT:
            continue
        mentions.append({
            "addr": addr,
            "name": names.get(addr) or addr,
            "support": len(matched),
            "evidence": list(dict.fromkeys(
                d["thread_id"] for d in reversed(matched)))[:3],
        })
    mentions.sort(key=lambda x: (-x["support"], x["name"].lower()))

    return {
        "mail_count": len(target_docs),
        "clusters": cluster_items[:5],
        "terms": singles[:8],
        "phrases": phrase_items[:5],
        "rising": rising[:5],
        "mentions": mentions[:5],
    }
