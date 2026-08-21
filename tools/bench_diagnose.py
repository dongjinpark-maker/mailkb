"""진단 벤치 — Minerva 재료 vs '원문 그대로' 재료를 같은 프롬프트로 비교한다.

왜 있나: 이 도구의 경쟁 상대는 **COM 으로 Outlook 을 직접 읽는 에이전트**다.
그쪽은 인용이 붙은 원문을 그대로 컨텍스트에 넣고, 우리는 인용을 떼고 절단한
재료를 넣는다. "우리가 더 낫다"는 주장은 **같은 프롬프트에 재료만 바꿔** 재보면
끝난다 — 입력 토큰·시간·비용은 여기서 나오고, 품질은 사람이 두 출력을 보고 고른다.

재료 두 가지
  minerva : review._diagnosis_material (인용 제거 + smart_truncate)
  raw     : 인용이 붙은 원문. **데모(fake 소스)에서만 만들 수 있다** —
            실환경 DB 에는 인용 포함 원문이 없다: 저장 HTML 은 스레드 첫 메일만
            인용을 보존하고(store: preserve_quotes=t_created) 나머지는 이미
            떼어 낸 뒤라, 복원한 척하면 **비교가 통째로 거짓이 된다**
            (2026-08-18 회사 PC 실측에서 그렇게 나왔다).
            그래서 raw 가 minerva 보다 크지 않으면 복원 실패로 보고 건너뛴다.

크기만 비교하려면 `--sizes` 를 쓴다 — **AI 호출 0**. 수집할 때 저장해 둔
messages.raw_chars(원본 길이)와 new_content 길이를 비교하므로 실환경에서도
정확하고 즉시 나온다. 품질까지 보려면 데모에서 돌린다.

실행 (회사 PC 도 동일):
    python -m tools.bench_diagnose --home data 26080536001 26081240001
    python tools/bench_diagnose.py --model sonnet --out bench.md 26080536001

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Minerva 재료 vs 원문 재료 진단 비교")
    ap.add_argument("threads", nargs="*", type=int,
                    help="스레드 번호 (--sizes 는 생략 시 전체)")
    ap.add_argument("--home", default=None)
    ap.add_argument("--model", default=None, help="기본 = [ai] diagnose")
    ap.add_argument("--out", default=None, help="결과를 쓸 파일(md)")
    ap.add_argument("--sizes", action="store_true",
                    help="AI 없이 크기만 비교 (raw_chars 기반 — 실환경 권장)")
    args = ap.parse_args()
    if not args.threads and not args.sizes:
        ap.error("스레드 번호가 필요하다 (또는 --sizes 로 크기만 비교)")

    cfg = config_mod.load(args.home)
    store = Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)
    if args.sizes:
        try:
            return _sizes(store, args.threads)
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
                # 관련 스레드는 **양쪽 모두 뺀다** — 이 비교의 변수는 '재료를
                # 인용 제거했는가' 하나여야 한다(경쟁자에게는 관련 스레드를
                # 붙일 수단 자체가 없기도 하다).
                r = _run(cmd, review.THREAD_DIAGNOSE.format(
                    subject=subject, messages=blob, related=""))
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
