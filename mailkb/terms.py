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
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import report
from .clean import normalize_subject, strip_preserved

# 각 파생 단계의 입력 계약을 따로 버전화한다. 점수/표시 규칙만 바뀌었을 때
# 원문 토큰이나 bag 전체를 다시 만들지 않기 위함이다.
WORD_FEATURE_VERSION = 2
WORD_BAG_VERSION = 3
WORDMAP_VERSION = 2
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


def _surface_rows(surfaces: Counter) -> list[list]:
    return [[key, surface, count]
            for (key, surface), count in sorted(surfaces.items())]


def _surface_counter(rows) -> Counter:
    out: Counter = Counter()
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) == 3:
            out[(str(row[0]), str(row[1]))] += int(row[2])
    return out


def extract_features(new_content: str, subject: str = "") -> dict:
    """원문에서 설정 독립적인 어휘 사실을 한 번 추출한다.

    내 이름·등록 인물·사용자 추가 불용어는 바뀔 수 있으므로 여기서 제거하지 않는다.
    표준 불용어까지만 제거한 문장 토큰을 보존해 조회 시 현재 설정을 정확히 적용한다.
    """
    body, body_surfaces = _token_sentences(new_content or "", report.WORD_STOP)
    subj, subject_surfaces = _token_sentences(
        normalize_subject(subject or ""), report.WORD_STOP)
    return {
        "body": body,
        "subject": subj,
        "body_surfaces": _surface_rows(body_surfaces),
        "subject_surfaces": _surface_rows(subject_surfaces),
    }


def _encode_compact(value: dict) -> bytes:
    raw = json.dumps(
        value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(raw, level=6)


def _decode_compact(encoded) -> dict:
    try:
        if isinstance(encoded, memoryview):
            encoded = encoded.tobytes()
        if isinstance(encoded, (bytes, bytearray)):
            encoded = zlib.decompress(encoded).decode("utf-8")
        value = json.loads(encoded) if isinstance(encoded, str) else encoded
    except (TypeError, ValueError, zlib.error, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def encode_features(new_content: str, subject: str = "") -> bytes:
    """SQLite 저장용 압축 JSON."""
    return _encode_compact(extract_features(new_content, subject))


def decode_features(encoded) -> dict:
    value = _decode_compact(encoded)
    if isinstance(value.get("body"), list):
        return value
    return {"body": [], "subject": [],
            "body_surfaces": [], "subject_surfaces": []}


def _row_features(row) -> dict:
    encoded = _value(row, "term_features", None)
    if encoded is not None:
        # Store 버전 백필이 다음 sync에서 손상된 행을 원문으로 복구한다. 웹에서는
        # 한 행 때문에 대형 본문 경로로 조용히 되돌아가지 않는다.
        return decode_features(encoded)
    return extract_features(
        str(_value(row, "new_content")), str(_value(row, "subject")))


def _filter_sentences(sentences, stop: frozenset[str]) -> list[list[str]]:
    return [[token for token in sentence
             if token not in stop and token.lower() not in stop]
            for sentence in sentences or []]


def analysis_context(names: dict[str, str] | None = None, extra_stop=()) -> dict:
    normalized = {str(k).lower(): str(v or "")
                  for k, v in (names or {}).items()}
    alias_by_addr = {addr: _aliases(name) for addr, name in normalized.items()}
    all_people = set().union(*alias_by_addr.values()) if alias_by_addr else set()
    return {
        "names": normalized,
        "global_stop": _stop_set(extra_stop),
        "alias_by_addr": alias_by_addr,
        "all_person_tokens": all_people,
    }


def document_bags(feature: dict, sender_addr: str, context: dict,
                  parts=("body",)) -> dict:
    """한 메시지 파생 특징을 현재 이름·불용어 설정으로 문서 bag에 투영한다."""
    addr = (sender_addr or "").lower()
    stop = context["global_stop"] | frozenset(
        context["alias_by_addr"].get(addr, set()))
    sentences = []
    surfaces: Counter = Counter()
    for part in parts:
        sentences.extend(_filter_sentences(feature.get(part), stop))
        surfaces.update(_surface_counter(
            feature.get(f"{part}_surfaces")))
    mentioned = {token for sentence in sentences for token in sentence}
    people = context["all_person_tokens"]
    terms = mentioned - people
    phrases = {
        (a, b)
        for sentence in sentences
        for a, b in zip(sentence, sentence[1:])
        if a != b and a not in people and b not in people
    }
    return {
        "terms": terms,
        "phrases": phrases,
        "mentioned_tokens": mentioned,
        "surfaces": surfaces,
    }


def encode_bag(bag: dict) -> bytes:
    value = {
        "terms": sorted(bag.get("terms") or ()),
        "phrases": [list(v) for v in sorted(bag.get("phrases") or ())],
        "mentioned": sorted(bag.get("mentioned_tokens") or ()),
        "surfaces": _surface_rows(bag.get("surfaces") or Counter()),
    }
    return _encode_compact(value)


def decode_bag(encoded) -> dict:
    value = _decode_compact(encoded)
    return {
        "terms": {str(v) for v in value.get("terms") or ()},
        "phrases": {
            (str(v[0]), str(v[1]))
            for v in value.get("phrases") or ()
            if isinstance(v, (list, tuple)) and len(v) == 2
        },
        "mentioned_tokens": {
            str(v) for v in value.get("mentioned") or ()
        },
        "surfaces": _surface_counter(value.get("surfaces")),
    }


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
                      person_name: str, people_names=None,
                      corpus_fingerprint: str = "") -> str:
    """설정·대조 집단 변화까지 포함한 파생 캐시 버전."""
    payload = {
        "v": WORDMAP_VERSION,
        "feature": WORD_FEATURE_VERSION,
        "bag": WORD_BAG_VERSION,
        "stop": sorted(str(v).strip().lower() for v in extra_stop if str(v).strip()),
        "eligible": sorted(a.lower() for a in eligible_addrs),
        "top": int(top_n),
        "name": person_name.strip().lower(),
        "people_names": sorted(
            (str(addr).lower(), str(name or "").strip().lower())
            for addr, name in (people_names or {}).items()
        ),
        "corpus": corpus_fingerprint,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"{WORDMAP_VERSION}:{hashlib.sha256(raw).hexdigest()[:16]}"


def _collect_documents(rows, target_addr: str, context: dict
                       ) -> tuple[list[dict], Counter]:
    """원본·feature·bag 행을 동일한 메일 단위 문서 표현으로 투영한다."""
    docs: list[dict] = []
    surfaces: Counter = Counter()
    seen_subjects: set[tuple[str, int]] = set()
    ordered = sorted(rows, key=lambda r: (
        str(_value(r, "sent_on")), int(_value(r, "id", 0))))
    for row in ordered:
        addr = str(_value(row, "sender_addr")).lower()
        if not addr:
            continue
        encoded_bag = _value(row, "term_body_bag", None)
        feature = None
        if encoded_bag is not None:
            bag = decode_bag(encoded_bag)
        else:
            feature = _row_features(row)
            bag = document_bags(feature, addr, context, ("body",))
        tid = int(_value(row, "thread_id", 0))
        subject_key = (addr, tid)
        if subject_key not in seen_subjects:
            seen_subjects.add(subject_key)
            encoded_subject = _value(row, "term_subject_bag", None)
            if encoded_subject is not None:
                subject_bag = decode_bag(encoded_subject)
            else:
                subject_bag = document_bags(
                    feature or _row_features(row), addr, context, ("subject",))
            bag["terms"] |= subject_bag["terms"]
            bag["phrases"] |= subject_bag["phrases"]
            bag["mentioned_tokens"] |= subject_bag["mentioned_tokens"]
            bag["surfaces"].update(subject_bag["surfaces"])
        docs.append({
            "id": int(_value(row, "id", 0)),
            "thread_id": tid,
            "addr": addr,
            "sent_on": str(_value(row, "sent_on")),
            "terms": bag["terms"],
            "phrases": bag["phrases"],
            "mentioned_tokens": bag["mentioned_tokens"],
        })
        if addr == target_addr:
            surfaces.update(bag["surfaces"])
    return docs, surfaces


def background_candidates(
        rows, target_addr: str, names: dict[str, str] | None = None,
        extra_stop=()) -> dict:
    """대조 DF가 실제로 필요한 대상 표현과 대상 자체 DF를 반환한다.

    최종 지도는 대상 support가 MIN_SUPPORT 이상인 표현만 점수화한다. 대조군도
    그 표현만 조회하면 전체 집계와 결과가 같고, 고유 bigram 대부분을 건너뛴다.
    """
    target = (target_addr or "").lower()
    context = analysis_context(names, extra_stop)
    docs, _ = _collect_documents(rows, target, context)
    target_docs = [d for d in docs if d["addr"] == target]
    term_df, phrase_df = Counter(), Counter()
    for doc in target_docs:
        term_df.update(doc["terms"])
        phrase_df.update(doc["phrases"])
    return {
        "mail_count": len(target_docs),
        "term_df": term_df,
        "phrase_df": phrase_df,
        "terms": {term for term, n in term_df.items() if n >= MIN_SUPPORT},
        "phrases": {
            phrase for phrase, n in phrase_df.items() if n >= MIN_SUPPORT
        },
    }


def analyze(rows, target_addr: str, names: dict[str, str] | None = None,
            extra_stop=(), top_n: int = 25,
            recent_weeks: int = RECENT_WEEKS,
            background: dict | None = None) -> dict:
    """최근 창의 수신 메일 행에서 한 인물의 업무 어휘 지도를 만든다.

    background가 없으면 rows는 같은 시간창의 대조 대상 전원 메일이다. 정확한
    대조 DF가 background로 주어지면 rows는 대상 인물 메일만 받아도 된다. 각
    항목의 support는 횟수가 아니라 표현이 나타난 메일 수이고, evidence는 최신
    근거 스레드 ID다.
    """
    target_addr = (target_addr or "").lower()
    context = analysis_context(names, extra_stop)
    names = context["names"]
    alias_by_addr = context["alias_by_addr"]
    docs, surfaces = _collect_documents(rows, target_addr, context)

    target_docs = [d for d in docs if d["addr"] == target_addr]
    other_docs = [d for d in docs if d["addr"] != target_addr]
    if not target_docs:
        return {"mail_count": 0, "clusters": [], "terms": [], "phrases": [],
                "rising": [], "mentions": []}

    own_df = Counter()
    own_phrase_df = Counter()
    other_df = Counter((background or {}).get("term_df") or {})
    other_phrase_df = Counter((background or {}).get("phrase_df") or {})
    for doc in target_docs:
        own_df.update(doc["terms"])
        own_phrase_df.update(doc["phrases"])
    if background is None:
        for doc in other_docs:
            other_df.update(doc["terms"])
            other_phrase_df.update(doc["phrases"])
        other_total = len(other_docs)
    else:
        other_total = int(background.get("mail_count", 0))
    display = _display_map(surfaces)

    term_items = []
    for key, support in own_df.items():
        if support < MIN_SUPPORT:
            continue
        score, lift = _score(
            support, len(target_docs), other_df[key], other_total)
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
            support, len(target_docs), other_phrase_df[key], other_total,
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
