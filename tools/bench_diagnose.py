"""진단 벤치 — Minerva 재료 vs '원문 그대로' 재료를 같은 프롬프트로 비교한다.

왜 있나: 이 도구의 경쟁 상대는 **COM 으로 Outlook 을 직접 읽는 에이전트**다.
그쪽은 인용이 붙은 원문을 그대로 컨텍스트에 넣고, 우리는 인용을 떼고 절단한
재료를 넣는다. "우리가 더 낫다"는 주장은 **같은 프롬프트에 재료만 바꿔** 재보면
끝난다 — 입력 토큰·시간·비용은 여기서 나오고, 품질은 사람이 두 출력을 보고 고른다.

재료 두 가지
  minerva : review._diagnosis_material (인용 제거 + smart_truncate.
            내가 받기 전의 대화는 발언별로 갈라 싣는다 — 2026-11-19)
  raw     : 인용이 붙은 원문. **데모(fake 소스)에서만 만들 수 있다** —
            실환경 DB 에는 인용 포함 원문이 없다: 저장 HTML 은 스레드 첫 메일만
            인용을 보존하고(store: preserve_quotes=t_created) 나머지는 이미
            떼어 낸 뒤라, 복원한 척하면 **비교가 통째로 거짓이 된다**
            (2026-08-18 회사 PC 실측에서 그렇게 나왔다).
            그래서 raw 가 minerva 보다 크지 않으면 복원 실패로 보고 건너뛴다.

크기만 비교하려면 `--sizes` 를 쓴다 — **AI 호출 0**. 수집할 때 저장해 둔
messages.raw_chars(원본 길이)와 new_content 길이를 비교하므로 실환경에서도
정확하고 즉시 나온다. 품질까지 보려면 데모에서 돌린다.

`--earlier` 는 **앞선 대화**(내가 받기 전에 오간 발언 — 보존 인용)가 브리핑 재료에
얼마나 실리는지를 잰다 — **AI 호출 0, 숫자만 찍는다(메일 내용·제목·이름 없음)**.
앞선 대화의 발언은 받은 메일과 같은 규칙으로 실린다(최근 40건 · 합쳐서 60,000자).
스레드 번호를 주면 그 스레드의 **인용이 어떤 모양인지**를 찍는다: 발언을 몇 개로
갈랐고 각각 몇 자인지, 발언 안에 가르지 못한 경계(구분선·귀속줄·`>` 줄)가 몇 줄
있는지, 머리 낱말은 있는데 머리로 못 읽은 줄이 **어떤 꼴인지**. 꼴은 글자를 전부
가리고(한글 가 · 영문 x · 숫자 9) 라벨 낱말과 문장부호만 남긴다 — 회사 메일의
인용 형식을 개발 쪽에서 볼 수 없어서, 형식만 건네받기 위한 것이다.

`--cuts` 는 **재료에서 잘려 나간 양**을 잰다 — 역시 AI 호출 0, 숫자만. 브리핑이
"생략된 본문 48,605자에 무엇이 있는지 확인 필요" 라고 신고했을 때, 그것이 어느
조각(메일 한 통 / 앞선 대화의 발언 / 못 가른 인용 원문)인지 가른다. 스레드 번호를
주면 그 스레드의 조각별 표, 안 주면 전체에서 긴 메일의 분포와 **옛 규칙(통당 최대
3,000자)이면 잘리던 스레드가 지금 규칙(총예산을 나눠 씀)으로 몇 개 남는지**,
그 대가로 재료가 얼마나 커졌는지를 찍는다.

실행 (회사 PC 도 동일):
    python -m tools.bench_diagnose --home data 26080536001 26081240001
    python tools/bench_diagnose.py --model sonnet --out bench.md 26080536001
    python tools/bench_diagnose.py --home data --earlier
    python tools/bench_diagnose.py --home data --earlier 26080536001
    python tools/bench_diagnose.py --home data --cuts 26080536001
    python tools/bench_diagnose.py --home data --cuts

출력: 표(입력 토큰·출력 토큰·초·$)와 두 재료의 진단 전문. --out 을 주면 파일로.
AI 호출은 스레드당 2콜이다 — 부르는 쪽이 개수를 통제하도록 상한을 두지 않는다.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailkb import config as config_mod           # noqa: E402
from mailkb import review                          # noqa: E402
from mailkb.store import Store                     # noqa: E402

_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """표시용 HTML → 평문. 인용 블록을 **떼지 않는다** — 그게 이 비교의 요점이다."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    t = _TAG_RX.sub("", t)
    for ent, ch in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                    ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(ent, ch)
    return _WS_RX.sub("\n\n", t).strip()


def _raw_material(store: Store, cfg, tid: int) -> tuple[str, str]:
    """(출처, 인용 포함 원문 블록) — 만들 수 없으면 ('', '')."""
    msgs = store.thread_messages(tid)
    if not msgs:
        return "", ""
    subject = (msgs[0]["subject"] or "").replace("RE: ", "").strip()

    # ① 데모: fake 소스가 수집 전 원문을 그대로 만들어 준다
    if (cfg.source or "") == "fake":
        try:
            from mailkb.sources import fake
            blocks = []
            for r in fake.FakeSource().fetch(None):
                if (r.subject or "").replace("RE: ", "").strip() == subject:
                    blocks.append(f"[{r.sent_on[:16]} {r.sender_name}]\n"
                                  f"{r.body_text or ''}")
            if blocks:
                return "fake 원문", "\n---\n".join(blocks)
        except Exception:
            pass

    # ② 실환경: 저장된 표시용 HTML(보존 기간 안)에 인용이 남아 있다
    blocks = []
    for m in msgs:
        html = m["body_html"] if "body_html" in m.keys() else ""
        text = _html_to_text(html) if html else ""
        if not text:
            continue
        who = "나" if m["is_sent"] else (m["sender_name"] or m["sender_addr"])
        blocks.append(f"[{(m['sent_on'] or '')[:16]} {who}]\n{text}")
    if blocks:
        return "저장 HTML", "\n---\n".join(blocks)
    return "", ""


def _run(cmd: list[str], prompt: str) -> dict:
    use: dict = {}

    def on_event(info):
        if info.get("ev") == "usage":
            use.update(info)

    t0 = time.time()
    out = review.ai_run(cmd, prompt, timeout=300, retries=0, on_event=on_event)
    return {"text": review.strip_summary_header(out).strip(),
            "secs": time.time() - t0, "in": use.get("in", 0),
            "out": use.get("out", 0), "usd": use.get("usd", 0.0)}


def _sizes(store: Store, tids: list[int]) -> int:
    """AI 없이 크기만 — 수집 시 저장한 원본 길이(raw_chars) 대 색인 길이.

    경쟁자(COM 에이전트)는 인용이 붙은 원본을 읽어야 하고 우리는 인용 제거본을
    읽는다. 그 배수가 곧 우리가 같은 예산으로 더 볼 수 있는 양이다.
    """
    rows = store.db.execute(
        "SELECT thread_id, COUNT(*) n, SUM(raw_chars) raw, "
        "SUM(LENGTH(new_content)) kept FROM messages "
        + ("WHERE thread_id IN (%s) " % ",".join("?" * len(tids)) if tids else "")
        + "GROUP BY thread_id HAVING n > 0 ORDER BY raw DESC",
        [int(t) for t in tids]).fetchall()
    tot = store.db.execute(
        "SELECT COUNT(*) n, SUM(raw_chars) raw, SUM(LENGTH(new_content)) kept "
        "FROM messages").fetchone()
    print("| 스레드 | 통수 | 원본(경쟁자) | 색인(Minerva) | 배수 |")
    print("|---|---|---|---|---|")
    for r in rows[:10]:
        kept = max(1, r["kept"] or 0)
        print(f"| #{r['thread_id']} | {r['n']} | {r['raw'] or 0:,}자 | "
              f"{kept:,}자 | {(r['raw'] or 0) / kept:.1f}배 |")
    kept = max(1, tot["kept"] or 0)
    print(f"\n**전체 {tot['n']:,}통: 원본 {tot['raw'] or 0:,}자 → 색인 {kept:,}자 "
          f"= {(tot['raw'] or 0) / kept:.1f}배**")
    return 0


def _spread(vals: list[int]) -> str:
    """`중앙 · 90% · 최대` — 표 한 줄의 세 칸. 값이 없으면 '-'."""
    if not vals:
        return "- | - | -"
    v = sorted(vals)

    def at(q: float) -> int:
        return v[min(len(v) - 1, int(len(v) * q))]
    return f"{at(0.5):,} | {at(0.9):,} | {v[-1]:,}"


def _earlier(store: Store) -> int:
    """AI 없이 — 앞선 대화(보존 인용)가 브리핑 재료에 실리는 양의 분포.

    **실제 재료를 읽는다**(`review._diagnosis_material` → `_pieces`). 조립을 흉내
    내면 규칙을 고친 뒤 이 숫자와 실제 재료가 어긋난다 — 이틀 사이에 규칙이 세 번
    바뀌었다.
    """
    from mailkb.clean import (PRESERVED_MARK, attributed_turns, strip_preserved,
                              take_preserved)

    total = store.db.execute(
        "SELECT COUNT(DISTINCT thread_id) FROM messages").fetchone()[0]
    tids = [r[0] for r in store.db.execute(
        "SELECT DISTINCT thread_id FROM messages WHERE new_content LIKE ? "
        "ORDER BY thread_id", (f"%{PRESERVED_MARK}%",))]
    raw_len, put_len, n_turns, longest = [], [], [], []
    parsed = unparsed = all_dup = only_earlier = with_rest = 0
    gone_threads = gone = cut_turns = cut_raw = dup = turns_all = 0
    for tid in tids:
        msgs = store.thread_messages(tid)
        t_raw = t_n = t_long = n_raw = 0
        any_raw = any_rest = False
        for m in msgs:
            if PRESERVED_MARK not in (m["new_content"] or ""):
                continue
            before = attributed_turns(m["new_content"] or "")[0]
            turns, raw = review.earlier_turns(msgs, m)
            dup += len(before) - len(turns)
            # 못 가른 것 둘: 통째로(발언 0개) / 발언 안쪽의 나머지(발언은 있다)
            any_raw = any_raw or bool(raw and not before)
            any_rest = any_rest or bool(raw and before)
            n_raw += bool(raw)
            t_raw += len(take_preserved(m["new_content"] or ""))
            t_n += len(turns)
            if turns:
                t_long = max(t_long, max(len(t["body"]) for t in turns))
        ps = [p for p in _pieces(review._diagnosis_material(store, tid)[1])
              if p["kind"] != "메일"]
        said = [p for p in ps if p["kind"] == _K_TURN]
        t_put = sum(p["put"] for p in ps)
        cut_turns += sum(1 for p in said if p["cut"])
        cut_raw += sum(1 for p in ps if p["kind"] == _K_RAW and p["cut"])
        hidden = (t_n + n_raw) - len(ps)
        if hidden > 0:
            gone_threads += 1
            gone += hidden
        turns_all += t_n
        if any_raw:
            unparsed += 1
        elif t_n:
            parsed += 1
        else:
            all_dup += 1
        if any_rest:
            with_rest += 1
        if t_put and not any(strip_preserved(m["new_content"] or "").strip()
                             for m in msgs):
            only_earlier += 1
        raw_len.append(t_raw)
        if t_put:
            put_len.append(t_put)
        if t_n:
            n_turns.append(t_n)
            longest.append(t_long)

    pct = f"{len(tids) / total * 100:.1f}%" if total else "-"
    print(f"앞선 대화(보존 인용)가 브리핑 재료에 실리는 양 — AI 호출 0, 숫자만\n")
    print(f"스레드 {total:,} · 인용이 실려 온 스레드 {len(tids):,} ({pct})")
    print(f"  발언을 가른 스레드 {parsed:,} · 못 가른 스레드 {unparsed:,} "
          f"(원문 그대로 싣는다 — 근거의 출처에 이름이 안 붙는다)")
    print(f"  발언 안쪽에 가르지 못한 더 앞선 인용이 남은 스레드 {with_rest:,} "
          f"(그 부분만 원문으로 싣는다)")
    print(f"  발언이 전부 실제 메일과 겹쳐 실을 것이 없는 스레드 {all_dup:,}")
    print(f"  받은 본문은 없고 앞선 대화만 있는 스레드 {only_earlier:,} "
          f"(지금까지 브리핑을 못 만들던 것)\n")
    print("| 스레드당 | 중앙 | 90% | 최대 |")
    print("|---|---|---|---|")
    print(f"| 인용 원문(자) | {_spread(raw_len)} |")
    print(f"| 재료에 실리는 양(자) | {_spread(put_len)} |")
    print(f"| 발언 수(턴) | {_spread(n_turns)} |")
    print(f"| 가장 긴 발언(자) | {_spread(longest)} |")
    print(f"\n받은 메일과 같은 규칙이다 — 최근 {review._DIAG_MSGS}건 · "
          f"합쳐서 {review._DIAG_TOTAL:,}자")
    print(f"  최근 {review._DIAG_MSGS}건 밖이라 안 실리는 앞선 대화 {gone:,}건 "
          f"(스레드 {gone_threads:,}개)")
    print(f"  가운데가 잘리는 발언 {cut_turns:,} / {turns_all:,} · "
          f"잘리는 원문 덩어리 {cut_raw:,}")
    print(f"  실제 메일과 겹쳐 뺀 발언 {dup:,}")
    print("\n스레드 하나의 인용이 어떤 모양인지 보려면 --earlier <번호>.")
    return 0


# 머리 낱말 — 턴 파서가 아는 것보다 **넓게** 잡는다. 못 읽은 머리를 찾는 그물이라
# 넓어야 하고, 오탐은 꼴을 보고 사람이 거른다. 절단기의 라벨(clean._HDR_FIRST·
# _HDR_FOLLOW)에 흔한 변형을 더했다.
_LABELS = (r"보낸\s*사람|보낸\s*이|발신자|발신인|발신|송신자|작성자|보낸\s*날짜|보낸\s*시간"
           r"|날짜|일시|받는\s*사람|받는\s*이|수신자|수신인|수신|참조|제목"
           r"|From|Sender|Sent|Date|To|Cc|Bcc|Subject|Title"
           r"|De|Von|Da|Van|Envoyé|Objet|Gesendet|Datum|Betreff|An"
           r"|发件人|寄件者|差出人|送信日時|件名|宛先|收件人|主题|发送时间")
_LABEL_LINE = re.compile(rf"^(?P<pre>[^\w가-힣]{{0,12}})(?P<lab>{_LABELS})"
                         rf"(?P<mid>[^\w가-힣:：]{{0,6}})[:：]", re.IGNORECASE)


def _shape(line: str) -> str:
    """머리 꼴 — 라벨 낱말과 문장부호만 남기고 **글자를 전부 가린다**.

    한글 → 가 · 영문 → x · 숫자 → 9, 같은 것이 이어지면 하나로 줄인다. 이름·주소·
    제목이 무엇이었는지는 남지 않고, 줄이 어떤 문법인지만 남는다."""
    m = _LABEL_LINE.match(line)
    if not m:
        return ""
    lab = " ".join(m.group("lab").split())
    rest = line[m.end():]
    rest = re.sub(r"[가-힣]+", "가", rest)
    rest = re.sub(r"[A-Za-z]+", "x", rest)
    rest = re.sub(r"\d+", "9", rest)
    rest = re.sub(r"[^\x00-\x7f가]+", "?", rest)        # 그 밖의 문자(한자·기호)
    return (m.group("pre") + lab + m.group("mid") + ":" + rest)[:90]


def _mask_all(line: str) -> str:
    """라벨을 모르는 줄의 꼴 — **전부 가린다.** 콜론 앞 낱말은 길이만 남긴다.

    `보내는분 : 한바다 <a@b.c>` → `가가가가 : 가 <x@x.x>`. 낱말이 무엇인지는 안
    나가고 "콜론 앞에 한글 네 글자짜리 라벨이 있다"만 나간다 — 그러면 그 라벨이
    무엇인지 사용자에게 물어볼 수 있다."""
    def blur(text: str, squeeze: bool) -> str:
        plus = "+" if squeeze else ""
        text = re.sub(rf"[가-힣]{plus}", "가", text)
        text = re.sub(rf"[A-Za-z]{plus}", "x", text)
        text = re.sub(rf"\d{plus}", "9", text)
        return re.sub(r"[^\x00-\x7f가]+", "?", text)

    m = re.match(r"^([^:：]{1,14})([:：])(.*)$", line)
    if m:
        return (blur(m.group(1), False) + m.group(2) + blur(m.group(3), True))[:90]
    return blur(line, True)[:90]


def _after_labels(lines: list[str]) -> dict[str, int]:
    """인용 라벨 줄(`Original Message` 류) 바로 아래 줄들의 꼴 — 머리 블록의 문법.

    아는 라벨이면 낱말을 남기고(_shape), 모르면 전부 가린다(_mask_all). 턴 파서가
    머리를 못 읽을 때 **무엇이 적혀 있었는지**를 볼 유일한 자리다."""
    from mailkb import clean

    out: dict[str, int] = {}
    for i, ln in enumerate(lines):
        if not clean._pres_label(ln.strip()):
            continue
        got = 0
        for nxt in lines[i + 1:i + 12]:
            t = nxt.strip()
            if not t:
                if got:
                    break
                continue
            if clean._pres_label(t) or got >= 6:
                break
            got += 1
            sh = _shape(t) or _mask_all(t)
            out[sh] = out.get(sh, 0) + 1
    return out


def _inner(lines: list[str]) -> dict:
    """본문 줄들의 속 — 가르지 못한 경계로 보이는 줄의 수와, 못 읽은 머리의 꼴."""
    from mailkb import clean

    out = {"lines": len(lines), "table": 0, "gt": 0, "rule": 0, "attrib": 0,
           "labels": 0, "shapes": {}}
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("|"):
            out["table"] += 1
        if s.startswith(">"):
            out["gt"] += 1
        if any(rx.search(s) for rx in clean._CUT_LINE_RES):
            out["rule"] += 1
        if any(rx.search(s) for rx in clean._ATTRIB_RES):
            out["attrib"] += 1
        sh = _shape(s)
        if sh:
            out["labels"] += 1
            out["shapes"][sh] = out["shapes"].get(sh, 0) + 1
    return out


def _earlier_shape(store: Store, tid: int) -> None:
    """한 스레드의 인용이 어떤 모양인가 — 숫자와 가린 꼴만 찍는다."""
    from mailkb import clean
    from mailkb.clean import (PRESERVED_MARK, attributed_turns, parse_preserved,
                              take_preserved)

    msgs = store.thread_messages(tid)
    carriers = [(i, m) for i, m in enumerate(msgs, 1)
                if PRESERVED_MARK in (m["new_content"] or "")]
    print(f"\n#{tid} — 메일 {len(msgs)}통 · 인용을 실어 온 메일 {len(carriers)}통")
    for i, m in carriers:
        text = m["new_content"] or ""
        raw = take_preserved(text)
        lines = raw.splitlines()
        seen = parse_preserved(text)                  # 화면이 쓰는 턴(안쪽 사슬 포함)
        said, rest = attributed_turns(text)           # 화자를 댈 수 있는 부분만
        kept, _ = review.earlier_turns(msgs, m)
        starts = clean._pres_starts(lines)
        n_label = sum(1 for k in starts if clean._pres_label(lines[k].strip()))
        print(f"\n{i}번째 메일({(m['sent_on'] or '')[5:10]}) — 인용 원문 {len(raw):,}자 · "
              f"{len(lines):,}줄")
        print(f"  발언을 여는 줄 {len(starts)}개 — 인용 라벨 줄(Original Message 류) "
              f"{n_label} · 발신인 머리 줄 {len(starts) - n_label}")
        if not seen:
            print("  발언을 하나도 가르지 못했다 — 라벨 줄도 발신인 머리 줄도 못 읽었다")
            body = lines
        else:
            sizes = [len(t["body"]) for t in seen]
            head = " · ".join(f"{x:,}" for x in sizes[:30]) + (" …" if len(sizes) > 30 else "")
            print(f"  가른 발언 {len(seen)}개(오래된 것부터, 자): {head}")
            print(f"  이름을 읽은 발언 {sum(1 for t in seen if t['who'])}개 · "
                  f"시각을 읽은 발언 {sum(1 for t in seen if t['when'])}개 · "
                  f"가장 긴 발언 {max(sizes):,}자")
            print(f"  발언 안쪽의 가르지 못한 나머지 {len(rest):,}자 · "
                  f"실제 메일과 겹쳐 뺀 발언 {len(said) - len(kept)}개")
            body = [ln for t in seen for ln in t["body"].splitlines()]
        inner = _inner(body)
        print(f"  {'원문' if not seen else '발언 본문'}의 속 — {inner['lines']:,}줄 · "
              f"표 줄 {inner['table']:,} · `>` 로 시작하는 줄 {inner['gt']:,} · "
              f"구분선 {inner['rule']:,} · 귀속줄 {inner['attrib']:,} · "
              f"머리 낱말이 있는 줄 {inner['labels']:,}")
        note = "(글자는 가림: 한글 가 · 영문 x · 숫자 9)"
        if inner["shapes"]:
            print(f"  본문에 남은 머리 낱말 줄의 꼴 — 머리로 못 읽은 것 {note}:")
            for sh, n in sorted(inner["shapes"].items(), key=lambda kv: -kv[1])[:12]:
                print(f"    {n:>4}× `{sh}`")
        after = _after_labels(lines)
        if after:
            print(f"  인용 라벨 줄 바로 아래 줄들의 꼴 {note}:")
            for sh, n in sorted(after.items(), key=lambda kv: -kv[1])[:12]:
                print(f"    {n:>4}× `{sh}`")
        # 발신인 머리로 읽은 줄의 꼴 — 읽은 것과 못 읽은 것을 나란히 봐야 차이가 보인다
        heads: dict[str, int] = {}
        for k in range(len(lines)):
            if lines[k].strip() and clean._pres_is_who(lines, k):
                sh = _shape(lines[k].strip()) or _mask_all(lines[k].strip())
                heads[sh] = heads.get(sh, 0) + 1
        if heads:
            print(f"  발신인 머리로 읽은 줄의 꼴 {note}:")
            for sh, n in sorted(heads.items(), key=lambda kv: -kv[1])[:6]:
                print(f"    {n:>4}× `{sh}`")


# 2026-11-19 까지의 통당 상한 — 지금은 없다(review._body_share). 고치기 전과 견줘
# "몇 개가 이제 전문이 실리고 재료는 얼마나 커졌나"를 찍는 데만 쓴다.
_OLD_BODY_MAX = 3_000
_K_TURN, _K_RAW = "앞선 대화(발언)", "앞선 대화(못 가른 원문)"
_CUT_RX = re.compile(r"…\(중략 — ([\d,]+)자\)…")
_HEAD_RX = re.compile(r"^\[(?:(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}|시각 미상) .+\]$")
_RAW_HEAD = f"[{review.EARLIER_TAG} · 인용 원문]"


def _pieces(blob: str) -> list[dict]:
    """재료 블록을 조각으로 — [{kind, day, put, cut, rows}]. **내용은 돌려주지 않는다.**

    조립을 흉내 내지 않고 **실제 재료를 읽는다**: 모델이 본 것이 곧 이 문자열이라,
    상한이나 조립 순서를 고쳐도 이 표가 어긋날 수 없다.
    """
    out: list[dict] = []
    for blk in blob.split("\n---\n"):
        head, _, body = blk.partition("\n")
        m = _HEAD_RX.match(head)
        if head == _RAW_HEAD or m:
            kind = (_K_RAW if head == _RAW_HEAD
                    else _K_TURN if head.endswith(f"· {review.EARLIER_TAG}]")
                    else "메일")
            out.append({"kind": kind, "day": (m.group(1) or "")[5:] if m else "",
                        "put": 0, "cut": 0, "rows": 0})
        elif not out or (blk.startswith("(") and blk.endswith(")") and "\n" not in blk):
            continue                    # 안내 줄(경과일·생략 통수·앞선 대화 머리줄)
        else:
            body = blk                  # 본문 안의 `---` 로 갈린 뒷조각 — 앞 조각에 잇는다
        cur = out[-1]
        cur["put"] += len(body)
        cur["cut"] += sum(int(x.replace(",", "")) for x in _CUT_RX.findall(body))
        cur["rows"] += body.count("…(표 잘림")
    return out


def _cuts_thread(store: Store, tid: int) -> None:
    subject, blob, n = review._diagnosis_material(store, tid)
    if not blob:
        print(f"#{tid}: 재료 없음")
        return
    ps = _pieces(blob)
    count = lambda k: sum(1 for p in ps if p["kind"] == k)
    put = sum(p["put"] for p in ps)
    print(f"\n#{tid} — 메일 {n}통 · 재료 조각 {len(ps)}개(받은 메일 {count('메일')} · "
          f"앞선 대화의 발언 {count(_K_TURN)} · 못 가른 원문 {count(_K_RAW)}) · "
          f"예산 {review._DIAG_TOTAL:,}자 중 {put:,}자 사용")
    print("| 조각 | 종류 | 날짜 | 실린 자수 | 생략 자수 | 표 잘림 |")
    print("|---|---|---|---|---|---|")
    for i, p in enumerate(ps, 1):
        if p["cut"] or p["rows"]:
            print(f"| {i} | {p['kind']} | {p['day'] or '-'} | {p['put']:,} | "
                  f"{p['cut']:,} | {p['rows'] or '-'} |")
    cut = [p for p in ps if p["cut"]]
    if not cut:
        print("| - | 잘린 조각 없음 | | | | |")
        return
    worst = max(cut, key=lambda p: p["cut"])
    print(f"조각 {len(ps)}개 중 {len(cut)}개가 잘렸다 · 가장 큰 생략 {worst['cut']:,}자"
          f"({worst['kind']}) · 예산의 남은 자리 {max(0, review._DIAG_TOTAL - put):,}자")


def _cuts(store: Store, tids: list[int]) -> int:
    """AI 없이 — 재료에서 잘려 나간 양. 번호를 주면 조각별, 안 주면 전체 분포."""
    print("브리핑 재료에서 잘려 나간 양 — AI 호출 0, 숫자만")
    if tids:
        for tid in tids:
            _cuts_thread(store, int(tid))
        return 0
    from mailkb.clean import PRESERVED_MARK

    # 길이만 읽는다 — 본문을 올리지 않으므로 큰 DB 에서도 금방 끝난다.
    # INSTR 은 1부터 센다: 표식이 k 번째 글자에서 시작하면 신규 작성분은 k-1 자다.
    rows = store.db.execute(
        "SELECT thread_id, INSTR(new_content, ?) > 0 AS pre, "
        "CASE WHEN INSTR(new_content, ?) > 0 "
        "THEN INSTR(new_content, ?) - 1 ELSE LENGTH(new_content) END AS own "
        "FROM messages ORDER BY thread_id, sent_on, id",
        (PRESERVED_MARK, PRESERVED_MARK, PRESERVED_MARK)).fetchall()
    by: dict[int, list[int]] = {}
    carried: set[int] = set()           # 앞선 대화가 실려 온 스레드 — 같은 예산을 나눠 쓴다
    for r in rows:
        by.setdefault(r["thread_id"], []).append(r["own"] or 0)
        if r["pre"]:
            carried.add(r["thread_id"])
    n_all = len(rows)
    over = [sum(1 for r in rows if (r["own"] or 0) > k)
            for k in (_OLD_BODY_MAX, 10_000, 30_000)]
    old_hit = now_hit = old_lost = now_lost = 0
    old_put: list[int] = []
    now_put: list[int] = []
    top: list[tuple] = []
    for tid, lens in by.items():
        win = lens[-review._DIAG_MSGS:]
        was = max(review._DIAG_BODY_MIN,
                  min(_OLD_BODY_MAX, review._DIAG_TOTAL // max(1, len(win))))
        lost_was = sum(max(0, size - was) for size in win)
        if not lost_was:
            continue                    # 옛 규칙으로도 전문이던 스레드 — 달라진 것이 없다
        if tid in carried:              # 앞선 대화까지 넣은 실제 재료로 센다
            ps = _pieces(review._diagnosis_material(store, tid)[1])
            lost = sum(p["cut"] for p in ps)
            put = sum(p["put"] for p in ps)
        else:
            now = review._body_share(win)
            lost = sum(max(0, size - now) for size in win)
            put = sum(win) - lost
        old_hit += 1
        old_lost += lost_was
        old_put.append(sum(win) - lost_was)
        now_put.append(put)
        if lost:
            now_hit += 1
            now_lost += lost
        top.append((max(win), tid, len(lens), lost_was, lost, put))
    pc = f"{over[0] / n_all * 100:.1f}%" if n_all else "-"
    print(f"\n메일 {n_all:,}통 · 스레드 {len(by):,}개")
    print(f"  신규 본문이 {_OLD_BODY_MAX:,}자 넘는 메일 {over[0]:,}통({pc}) · "
          f"10,000자 넘는 {over[1]:,}통 · 30,000자 넘는 {over[2]:,}통")
    print(f"  옛 규칙(통당 최대 {_OLD_BODY_MAX:,}자)이면 메일 본문이 잘리던 스레드 "
          f"{old_hit:,}개 — 생략 합계 {old_lost:,}자")
    print(f"  지금 규칙(총예산 {review._DIAG_TOTAL:,}자를 나눠 씀)으로 잘리는 스레드 "
          f"{now_hit:,}개 — 생략 합계 {now_lost:,}자")
    print(f"  → {old_hit - now_hit:,}개는 이제 전문이 실린다")
    if top:
        print("\n치른 값 — 그 스레드들의 재료 크기(메일 본문 합, 자)")
        print("| | 중앙 | 90% | 최대 |")
        print("|---|---|---|---|")
        print(f"| 옛 규칙 | {_spread(old_put)} |")
        print(f"| 지금 | {_spread(now_put)} |")
        print("\n| 가장 긴 메일(자) | 스레드 | 통수 | 옛 생략(자) | 지금 생략(자) | 지금 재료(자) |")
        print("|---|---|---|---|---|---|")
        for big, tid, n, lost_was, lost, put in sorted(top, reverse=True)[:10]:
            print(f"| {big:,} | #{tid} | {n} | {lost_was:,} | {lost:,} | {put:,} |")
    print("\n길이는 SQL 로만 읽은 근사다(몇 자 차이가 난다). 앞선 대화가 실려 온 "
          "스레드의 '지금'은 실제 재료로 셌다.\n스레드 하나를 정확히 보려면 "
          "--cuts <번호>, 앞선 대화(보존 인용) 쪽은 --earlier.")
    return 0


def _safe_console() -> None:
    """출력을 파일로 돌린 Windows(cp949)에서 `—` 한 글자로 죽지 않게 한다.

    콘솔에 바로 찍을 때는 파이썬이 유니코드로 쓰지만, `> 결과.txt` 로 돌리면 코드
    페이지로 내려가 긴줄표에서 UnicodeEncodeError 가 난다 — 숫자를 받아 적으려던
    바로 그 순간이다. cli.main 과 같은 장치를 쓴다."""
    from mailkb import cli

    cli._install_console_fallback()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="mailkb")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


def main() -> int:
    _safe_console()
    ap = argparse.ArgumentParser(description="Minerva 재료 vs 원문 재료 진단 비교")
    ap.add_argument("threads", nargs="*", type=int,
                    help="스레드 번호 (--sizes 는 생략 시 전체)")
    ap.add_argument("--home", default=None)
    ap.add_argument("--model", default=None, help="기본 = [ai] diagnose")
    ap.add_argument("--out", default=None, help="결과를 쓸 파일(md)")
    ap.add_argument("--sizes", action="store_true",
                    help="AI 없이 크기만 비교 (raw_chars 기반 — 실환경 권장)")
    ap.add_argument("--earlier", action="store_true",
                    help="AI 없이 — 앞선 대화(보존 인용)가 재료에 실리는 양의 분포 "
                         "(번호를 주면 그 스레드의 인용이 어떤 모양인지)")
    ap.add_argument("--cuts", action="store_true",
                    help="AI 없이 — 재료에서 잘려 나간 양 (번호를 주면 조각별, 생략 시 전체)")
    args = ap.parse_args()
    if not args.threads and not (args.sizes or args.earlier or args.cuts):
        ap.error("스레드 번호가 필요하다 (또는 --sizes · --earlier · --cuts)")

    cfg = config_mod.load(args.home)
    store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
    if args.sizes or args.earlier or args.cuts:
        try:
            if args.cuts:
                return _cuts(store, args.threads)
            if args.earlier and args.threads:
                print("앞선 대화(보존 인용)의 모양 — AI 호출 0, 숫자와 가린 꼴만")
                for tid in args.threads:
                    _earlier_shape(store, int(tid))
                return 0
            return (_earlier(store) if args.earlier
                    else _sizes(store, args.threads))
        finally:
            store.close()
    cmd = cfg.ai_cmd(args.model or cfg.ai_diagnose_backend)
    lines, rows = [], []
    try:
        for tid in args.threads:
            subject, mine, n = review._diagnosis_material(store, tid)
            src, raw = _raw_material(store, cfg, tid)
            if not mine:
                print(f"#{tid}: 재료 없음 — 건너뜀", file=sys.stderr)
                continue
            if not raw or len(raw) <= len(mine) * 1.2:
                # 인용이 붙은 원문이 인용 제거본보다 크지 않다 = 복원 실패다.
                # 그대로 돌리면 '두 재료가 비슷하다'는 **거짓 결론**이 나온다.
                print(f"#{tid}: 인용 포함 원문 복원 실패 — 건너뜀 "
                      f"(raw {len(raw):,}자 vs minerva {len(mine):,}자). "
                      f"실환경 DB 로는 복원되지 않는다 — --sizes 를 쓰라.",
                      file=sys.stderr)
                continue
            lines.append(f"\n## #{tid} {subject} ({n}통 · 원문 출처: {src})\n")
            for label, blob in (("minerva", mine), ("raw", raw)):
                # 관련 스레드·노트·지식은 **양쪽 모두 뺀다** — 이 비교의 변수는
                # '재료를 인용 제거했는가' 하나여야 한다(경쟁자에게는 관련
                # 스레드를 붙일 수단 자체가 없기도 하다).
                r = _run(cmd, review.THREAD_DIAGNOSE.format(
                    subject=subject, messages=blob, related="", memory=""))
                rows.append((tid, label, len(blob), r))
                lines.append(f"### {label} — 재료 {len(blob):,}자 · "
                             f"입력 {r['in']:,}토큰 · 출력 {r['out']:,}토큰 · "
                             f"{r['secs']:.0f}초 · ${r['usd']:.4f}\n")
                lines.append(r["text"] + "\n")
    finally:
        store.close()

    head = ["| 스레드 | 재료 | 재료 자수 | 입력 토큰 | 출력 토큰 | 초 | $ |",
            "|---|---|---|---|---|---|---|"]
    for tid, label, chars, r in rows:
        head.append(f"| #{tid} | {label} | {chars:,} | {r['in']:,} | "
                    f"{r['out']:,} | {r['secs']:.0f} | {r['usd']:.4f} |")
    text = "\n".join(head) + "\n" + "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"저장: {args.out}")
    print(text if not args.out else "\n".join(head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
