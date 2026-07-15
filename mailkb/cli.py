"""mailkb CLI.

python -m mailkb <command>
사용 흐름: init → sync → ls/search/thread → note → review [--ai]
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import date

from . import config as config_mod
from . import notes, review
from . import store as store_mod
from .store import Store


def _store(cfg) -> Store:
    return Store(cfg.db_path, cfg.my_addresses)


def _fmt_row(m) -> str:
    mark = "→" if m["is_sent"] else " "
    att = " 📎" if m["attach_names"] else ""
    return (
        f"{m['id']:>5} {mark} {m['sent_on'][:16]}  [{m['thread_id']:>4}] "
        f"{(m['sender_name'] or m['sender_addr'])[:14]:14} {m['subject'][:52]}{att}"
    )


# ------------------------------------------------------------------ commands

def cmd_init(args) -> None:
    home = config_mod.resolve_home(args.home)
    cfg_path = config_mod.init_home(home)
    print(f"초기화 완료: {home}")
    print(f"설정 확인/수정: {cfg_path}  (my_addresses 를 실제 주소로!)")


# 진행 표시는 stderr(결과 stdout 과 분리, #13). TTY 면 \r 로 제자리 갱신,
# 비-TTY(스케줄러·리다이렉트)면 주기적 줄바꿈. 이모지·ANSI 색 없음 —
# Windows cp949 콘솔에서도 안전해야 한다(스피너는 ASCII).
_SPIN = "|/-\\"


def _tty() -> bool:
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


class _SyncProgress:
    """수집 라이브 카운터 — 수집/신규/중복 + 스피너 + 경과."""

    def __init__(self):
        self.tty = _tty()
        self.t0 = time.monotonic()
        self.spin = 0
        self.last = 0.0

    def update(self, s) -> None:
        now = time.monotonic()
        if self.tty:
            if now - self.last < 0.08:        # 초당 ~12회로 제한
                return
            self.last = now
            self.spin = (self.spin + 1) % len(_SPIN)
            sys.stderr.write(
                f"\r  {_SPIN[self.spin]} 수집 {s.fetched:>4}   "
                f"신규 {s.inserted:>4}   중복 {s.skipped:>4}   "
                f"{now - self.t0:4.1f}s   ")
            sys.stderr.flush()
        elif s.fetched % 50 == 0:
            print(f"수집·인덱싱 중… {s.fetched}통", file=sys.stderr, flush=True)

    def done(self) -> None:
        if self.tty:
            sys.stderr.write("\r" + " " * 64 + "\r")   # 라이브 줄 지우기
            sys.stderr.flush()


class _StageProgress:
    """review --ai 단계 시각화 — [i/N] 단계 + 회전 스피너 + 경과.

    각 단계(AI 호출)는 수 초 블로킹된다. 그 동안 데몬 스레드가 스피너를
    제자리 갱신(TTY)해 '멈춘 것처럼' 보이지 않게 한다. 비-TTY 는 정적 줄만.
    """

    def __init__(self, total: int):
        self.total = total
        self.n = 0
        self.tty = _tty()
        self.msg = ""
        self.t_stage = None
        self.t0 = time.monotonic()
        self._stop = None
        self._thr = None

    def _tick(self, stop) -> None:
        spin = 0
        while not stop.wait(0.12):            # ~8fps
            if self.t_stage is None:
                continue
            spin = (spin + 1) % len(_SPIN)
            sys.stderr.write(
                f"\r  [{self.n}/{self.total}] {_SPIN[spin]} {self.msg}"
                f"  {time.monotonic() - self.t_stage:.0f}s   ")
            sys.stderr.flush()

    def _stop_ticker(self) -> None:
        if self._thr:
            self._stop.set()
            self._thr.join(timeout=0.5)       # 스레드 완전 종료 후에만 다음 출력
            self._thr = None

    def __call__(self, msg: str) -> None:
        now = time.monotonic()
        if self.tty:
            self._stop_ticker()               # 직전 단계 애니메이션 정지(경쟁 방지)
        if self.t_stage is not None:          # 직전 단계 마감(소요시간)
            dt = now - self.t_stage
            if self.tty:
                sys.stderr.write(
                    f"\r  [{self.n}/{self.total}] {self.msg}  {dt:.1f}s   \n")
                sys.stderr.flush()
            else:
                print(f"  … {dt:.1f}s", file=sys.stderr, flush=True)
        if msg == "완료":
            self.t_stage = None
            print(f"AI 계층 완료 · 총 {now - self.t0:.1f}s", file=sys.stderr, flush=True)
            return
        self.n += 1
        self.msg = msg
        self.t_stage = now
        if self.tty:
            sys.stderr.write(f"  [{self.n}/{self.total}] {_SPIN[0]} {msg}")
            sys.stderr.flush()
            self._stop = threading.Event()
            self._thr = threading.Thread(
                target=self._tick, args=(self._stop,), daemon=True)
            self._thr.start()
        else:
            print(f"  [{self.n}/{self.total}] {msg}", file=sys.stderr, flush=True)


def cmd_sync(args) -> None:
    from .sources import get_source

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    source = get_source(args.source or cfg.source)
    if args.since:  # 소량 시험 수집용 (예: --since 2026-07-01)
        since = args.since + "T00:00:00" if len(args.since) == 10 else args.since
    else:
        since = None if args.full else store.last_sync()

    mode = "전체" if since is None and not args.since else "증분"
    print(f"sync 시작 · {source.name} · {mode}"
          + (f" (since {since[:16]})" if since else ""), file=sys.stderr, flush=True)
    t0 = time.monotonic()
    prog = _SyncProgress()
    retain = int(cfg.opt("web", "image_retain_days", default=60) or 0)
    cutoff = store_mod.image_cutoff_for(retain)
    try:
        stats = store.ingest(source.fetch(since, image_cutoff=cutoff),
                             progress=prog.update, image_cutoff=cutoff)
    finally:
        # 프룬은 COM 불필요 — 수집 실패(Outlook 꺼짐 등)에도 실행
        pruned = store.maybe_prune_html(retain)
    prog.done()
    dt = time.monotonic() - t0

    saved = 100 - (stats.kept_chars * 100 // max(stats.raw_chars, 1))
    # 결과는 stdout — 정렬된 한눈 요약
    print(f"sync 완료 · {source.name} · {dt:.1f}s")
    print(f"  수집 {stats.fetched:>4}   신규 {stats.inserted:>4}   "
          f"중복 {stats.skipped:>4}   새 스레드 {stats.new_threads:>3}")
    if stats.inserted:
        print(f"  인용 제거 {stats.raw_chars:,}자 → {stats.kept_chars:,}자 (절감 {saved}%)")
    elif stats.skipped and not stats.fetched - stats.skipped:
        print("  변경 없음 (겹쳐 읽은 경계 메일만 — 정상)")
    if stats.img_embedded or stats.img_failed:
        print(f"  인라인 이미지 임베드 {stats.img_embedded}"
              + (f"   실패 {stats.img_failed} (Outlook에서 확인)" if stats.img_failed else ""))
    if pruned:
        print(f"  본문 압축(보존 {retain}일 경과): 이미지 마커 {pruned[0]} · HTML 회수 {pruned[1]}")


def cmd_ls(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    if args.unanswered:
        rows = review.filtered_unanswered(store, cfg)
        if not rows:
            print("미답변 없음")
            return
        print(f"미답변 스레드 {len(rows)}건:")
        for r in rows:
            warn = " ⚠" if r["days_old"] >= 2 else ""
            print(
                f"  [#{r['thread_id']:>4}] D+{r['days_old']} "
                f"{r['sender_name']}: {r['subject']}{warn}"
            )
        return
    for m in reversed(store.recent(args.limit, today_only=args.today)):
        print(_fmt_row(m))


def cmd_search(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    if getattr(args, "ai", False):
        try:
            res = review.ai_search(store, cfg, args.query, date.today().isoformat())
        except review.AIError as e:
            raise SystemExit(f"AI 검색 불가: {e}")
        if getattr(args, "json", False):
            import json
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"AI 해석: {res['dsl']}")
        if res.get("note"):
            print(f"  ({res['note']})")
        if not res["items"]:
            print("정확히 맞는 메일을 찾지 못했습니다.")
            return
        for i, it in enumerate(res["items"], 1):
            arrow = "→" if it.get("is_sent") else " "
            print(f"{i}. [{it['thread_id']:>4}] {it['date']} {arrow} "
                  f"{it['sender']}: {it['subject']}")
            if it.get("reason"):
                print(f"       └ {it['reason']}")
        return
    rows = store.search(args.query, args.limit)
    if getattr(args, "json", False):
        import json
        # skill·도구 소비용 구조화 출력 — snippet 의 ⟪⟫ 강조 마커는 그대로 둔다.
        out = [{
            "id": m["id"], "thread_id": m["thread_id"], "subject": m["subject"],
            "sender": m["sender_name"] or m["sender_addr"],
            "sender_addr": m["sender_addr"], "date": m["sent_on"][:16],
            "is_sent": bool(m["is_sent"]), "has_attach": bool(m["attach_names"]),
            "tier": m["tier"], "snippet": m["snippet"],
        } for m in rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("결과 없음")
        return
    for m in rows:
        print(_fmt_row(m))
        snip = (m["snippet"] or "").replace("\n", " ").strip()
        if snip:
            print(f"        {snip[:88]}")


def cmd_show(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    m = store.message(args.ref)
    if not m:
        raise SystemExit(f"메일 없음: {args.ref}")
    def _addrs(label: str, joined: str) -> None:
        addrs = [a for a in joined.split(";") if a]
        if len(addrs) > 5:
            print(f"{label}: {'; '.join(addrs[:3])} 외 {len(addrs) - 3}명")
        else:
            print(f"{label}: {joined}")

    print(f"제목: {m['subject']}")
    print(f"보낸 사람: {m['sender_name']} <{m['sender_addr']}>")
    _addrs("받는 사람", m["to_addrs"])
    if m["cc_addrs"]:
        _addrs("참조", m["cc_addrs"])
    print(f"일시: {m['sent_on']}  스레드: #{m['thread_id']}")
    if m["attach_names"]:
        print(f"첨부: {m['attach_names']} (내용은 Outlook 에서 — mailkb open {m['id']})")
    print(f"Message-ID: {m['message_id']}")
    print("─" * 60)
    print(m["new_content"])


def cmd_thread(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    msgs = store.thread_messages(args.thread_id)
    if not msgs:
        raise SystemExit(f"스레드 없음: #{args.thread_id}")
    t = store.thread(args.thread_id)
    print(f"스레드 #{args.thread_id}: {msgs[0]['subject']}  ({len(msgs)}통)")
    if t and t["rolling_summary"]:
        print(f"\n[누적 요약]\n{t['rolling_summary']}\n")
    for m in msgs:
        print("─" * 60)
        print(f"{m['sent_on'][:16]}  {m['sender_name']} → {m['to_addrs']}")
        print()
        print(m["new_content"])


def cmd_note(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    path = notes.create_thread_note(cfg, store, args.thread_id)
    print(f"노트: {path}")
    print("요지·결정 항목을 직접 채우세요. 첨부 보존이 필요하면 회사 PC에서:")
    print(f"  mailkb attach {args.thread_id}")


def cmd_review(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    d = args.date or date.today().isoformat()
    det = review.deterministic(store, cfg, d)

    ai_text = None
    if args.ai:
        print(f"review --ai · {d} · 요약 {cfg.ai_summary_backend} / 분류 "
              f"{cfg.ai_classify_backend}", file=sys.stderr, flush=True)
        # graceful — AI 가 실패해도 결정론 리뷰는 항상 출력·저장 (#10)
        # run_ai_layer 은 5개 작업 단계(요약·수확·디제스트·분류·정리) 후 '완료'.
        ai_text, note = review.run_ai_layer(
            store, cfg, det, backend=args.backend, persist_date=d,
            progress=_StageProgress(5),
        )
        if note:
            print(note, file=sys.stderr, flush=True)

    content = review.render(det, ai_text)
    path = notes.write_daily(cfg, d, content)
    print(content)
    print(f"저장됨: {path}")


def cmd_hide(args) -> None:
    cfg = config_mod.load(args.home)
    _store(cfg).hide_thread(args.thread_id, not args.undo)
    if args.undo:
        print(f"스레드 #{args.thread_id} 숨김 해제")
    else:
        print(f"스레드 #{args.thread_id} 숨김 — 목록·추적 제외, 새 메일 오면 자동 해제")


def cmd_open(args) -> None:
    from .sources import get_source

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    m = store.message(args.ref)
    if not m:
        raise SystemExit(f"메일 없음: {args.ref}")
    source = get_source("outlook")  # Windows 전용
    if source.open_in_outlook(m["entry_id"], m["message_id"]):
        print("Outlook 에서 열림")
    else:
        raise SystemExit("Outlook 에서 찾지 못함 (삭제되었을 수 있음)")


def cmd_attach(args) -> None:
    from .sources import get_source

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    msgs = store.thread_messages(args.thread_id)
    if not msgs:
        raise SystemExit(f"스레드 없음: #{args.thread_id}")
    dest = cfg.vault / "notes" / f"attachments-{args.thread_id}"
    dest.mkdir(parents=True, exist_ok=True)
    source = get_source("outlook")
    total, used = [], set()
    for m in msgs:
        if m["attach_names"]:
            total += source.save_attachments(
                m["entry_id"], str(dest), m["message_id"], used=used)
    print(f"{len(total)}개 첨부 저장: {dest}")


def cmd_act(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    d = date.today().isoformat()
    queue = review.intervention_queue(store, cfg, d)
    if args.ai:
        queue = review.ai_refine_intervention(
            store, cfg, queue, extra_context=args.note, backend=args.backend,
            persist_date=d,
        )
    else:  # 오늘 저장된 AI 정리가 있으면 반영(웹에서 정리한 결과 CLI 에도)
        queue = review.apply_saved_ai(queue, store.load_intervention_ai(d))
    lines: list[str] = []
    review.render_intervention(lines, queue)
    print("\n".join(lines).rstrip())


def cmd_block(args) -> None:
    cfg = config_mod.load(args.home)
    if config_mod.add_blocked(cfg, args.addr):
        print(f"차단 추가: {args.addr}")
        print("→ 실제 수신 차단은 Outlook 규칙으로: 이 주소를 규칙에 추가하세요.")
    else:
        print(f"이미 차단 목록에 있음(또는 빈 값): {args.addr}")
    print(f"목록 파일: {cfg.blocklist_path}")


def cmd_unblock(args) -> None:
    cfg = config_mod.load(args.home)
    if config_mod.remove_blocked(cfg, args.addr):
        print(f"차단 해제: {args.addr}")
    else:
        print(f"목록에 정확히 일치하는 항목 없음: {args.addr}")


def cmd_noise(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    rows = store.top_senders(args.limit)
    if not rows:
        print("발신자 데이터 없음 — 먼저 sync")
        return
    print("발신자별 수신량 (⛔ 차단됨 · ~ 노이즈 · ← 일방=답장 0):")
    for r in rows:
        if cfg.is_blocked(r["addr"]):
            mark = "⛔"
        elif cfg.is_noise(r["addr"]):
            mark = "~ "
        else:
            mark = "  "
        oneway = "  ← 일방" if r["to_count"] == 0 else f"  (내 답장 {r['to_count']})"
        print(f"  {mark} {r['from_count']:>3}통  {(r['name'] or '')[:12]:12} "
              f"{r['addr']:34}{oneway}")
    print("\n제외하려면: mailkb block <주소>   (Outlook 규칙에도 추가)")


def cmd_diagnose(args) -> None:
    """실 데이터 진단 — 스레딩·본문품질·요약·AI 백엔드·개입 큐 과탐을 수치로.

    회사 PC에서 '요약이 안 됨 / false alarm 많음' 을 데이터로 짚기 위한 도구.
    읽기 전용(AI 백엔드는 짧은 시험 호출 1회만).
    """
    from collections import Counter

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    db = store.db
    s = store.stats()
    print("mailkb 진단  " + "=" * 34)
    print(f"메시지 {s['messages']:,} · 스레드 {s['threads']:,} · 인물 {s['people']:,}")
    nthreads = max(s["threads"], 1)

    # 1) 스레딩 건강도 — 단일메일 비율이 높으면 대화가 안 묶이는 것
    if s["threads"]:
        single = db.execute(
            "SELECT COUNT(*) n FROM (SELECT thread_id FROM messages "
            "GROUP BY thread_id HAVING COUNT(*)=1)"
        ).fetchone()["n"]
        conv = db.execute(
            "SELECT COUNT(*) n FROM threads WHERE conversation_key!=''"
        ).fetchone()["n"]
        print(f"\n[스레딩] 평균 {s['messages']/nthreads:.1f}통/스레드 · "
              f"단일메일 스레드 {single} ({single*100//nthreads}%) · "
              f"대화키 보유 {conv}/{s['threads']}")
        if single * 100 // nthreads >= 60:
            print("  ⚠ 단일메일 비율 높음 — 스레딩 미결합. "
                  "References/ConversationID/제목정규화 확인 필요")

    # 2) 본문 품질 (#2 HTML→마크다운 반영 여부)
    if s["messages"]:
        row = db.execute(
            "SELECT AVG(LENGTH(new_content)) a, "
            "SUM(CASE WHEN LENGTH(TRIM(new_content))=0 THEN 1 ELSE 0 END) empty, "
            "SUM(CASE WHEN new_content LIKE '%<%>%' THEN 1 ELSE 0 END) htmlish "
            "FROM messages"
        ).fetchone()
        print(f"\n[본문] 평균 신규내용 {int(row['a'] or 0)}자 · 빈 본문 {row['empty']} · "
              f"HTML태그 잔존 {row['htmlish']}")
        if row["htmlish"]:
            print("  ⚠ HTML 태그 남은 메일 존재 — #2 수정 후 재수집 필요: sync --full")
        if row["empty"] and row["empty"] * 100 // max(s["messages"], 1) >= 20:
            print("  ⚠ 빈 본문 비율 높음 — 인용 제거 과잉 또는 본문 추출 실패")

    # 3) 요약 커버리지
    summ = db.execute(
        "SELECT COUNT(*) n FROM threads WHERE TRIM(rolling_summary)!=''"
    ).fetchone()["n"]
    print(f"\n[요약] 누적 요약 보유 스레드 {summ}/{s['threads']}")
    if summ == 0:
        print("  ⚠ 요약 0건 — review --ai 를 성공적으로 돌린 적 없거나 AI 백엔드 미작동")

    # 4) AI 백엔드 점검 — '요약이 안 됨'의 직접 원인 확인
    print("\n[AI 백엔드]")
    try:
        cmd = cfg.ai_cmd(args.backend)
    except SystemExit as e:
        print(f"  설정 없음: {e}")
        cmd = None
    if cmd:
        print(f"  명령: {' '.join(cmd)}")
        try:
            out = review.ai_run(cmd, "한 단어로만 답하라. 정상이면 OK.",
                                timeout=30, retries=0)
            print(f"  ✓ 응답: {out.splitlines()[0][:60]!r}")
        except review.AIError as e:
            print(f"  ⚠ 실패: {str(e)[:180]}")
            print("  → opencode/claude CLI 설치·PATH·인증 확인. 요약/분석이 이 때문에 빔.")

    # 5) 개입 큐 과탐 분해 (false alarm)
    d = date.today().isoformat()
    queue = review.intervention_queue(store, cfg, d)
    by_cat = Counter(it["category"] for it in queue)
    print(f"\n[개입 큐] 총 {len(queue)}건  (broadcast_to={cfg.broadcast_to}, "
          f"stall={cfg.stall_workdays}, stale={cfg.stale_workdays} 영업일)")
    for key, label in review.CATEGORIES:
        print(f"  {label}: {by_cat.get(key, 0)}")
    resp = [it for it in queue if it["category"] == "respond"]
    if resp:
        personal = sum(1 for it in resp if it.get("personal"))
        print(f"  └ 🟠 중 ★나 지목(이름 언급/내 참여): {personal} · "
              f"직접수신만: {len(resp) - personal}  "
              f"(요청 없는 대규모 그룹 FYI 는 이미 제외됨)")

    # 6) 이미지·본문 수명주기 상태 — "프룬이 안 도는" 문제의 1차 진단
    retain = int(cfg.opt("web", "image_retain_days", default=60) or 0)
    stamp = store.get_state("last_image_prune") or "(없음)"
    n_html = db.execute("SELECT COUNT(*) n FROM message_html").fetchone()["n"]
    n_mark = db.execute(
        "SELECT COUNT(*) n FROM message_html WHERE html LIKE '<div class=''imgstrip''%'"
    ).fetchone()["n"]
    n_img = db.execute(
        "SELECT COUNT(*) n FROM message_html WHERE html LIKE '%data:image/%'"
    ).fetchone()["n"]
    print(f"\n[이미지·본문] 보존 {retain}일 (config [web] image_retain_days)"
          f" · 마지막 프룬 {stamp}")
    print(f"  html {n_html}행 · 이미지 임베드 {n_img} · 프룬 마커 {n_mark}")
    if retain == 60 and cfg.opt("web", "image_retain_days") is None:
        print("  (설정 미검출 — config.toml 에 [web] 섹션 헤더 아래 두었는지 확인)")

    # 7) 노이즈 설정 요약
    print(f"\n[노이즈] ignore_senders {len(cfg.ignore_senders)}개 · "
          f"internal_domains {cfg.internal_domains} · 차단 {len(cfg.blocked_senders)}개")
    print("  발신자 상위/일방 다량 후보:  mailkb noise")
    store.close()


def cmd_serve(args) -> None:
    cfg = config_mod.load(args.home)
    from . import web  # 지연 import

    web.serve(cfg, port=args.port,
              open_browser=args.open, app_mode=args.app)


def cmd_stats(args) -> None:
    cfg = config_mod.load(args.home)
    s = _store(cfg).stats()
    saved = 100 - (s["kept_chars"] * 100 // max(s["raw_chars"], 1))
    print(f"메시지 {s['messages']:,} / 스레드 {s['threads']:,} / 인물 {s['people']:,}")
    print(f"DB {s['db_bytes'] / 1024 / 1024:.1f}MB, FTS={s['fts']}")
    print(f"인용 제거 절감: {saved}% ({s['raw_chars']:,} → {s['kept_chars']:,}자)")


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    # Windows에서 출력을 파일로 리다이렉트하면 스트림 인코딩이 cp949 가 되어
    # "—" 같은 문자가 UnicodeEncodeError 로 죽는다 → 깨진 문자로 대체하고 계속.
    # (실제 콘솔은 UTF-16 API 라 무관 — 리다이렉트/스케줄러 로그 경로 방어)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    p = argparse.ArgumentParser(prog="mailkb", description="Outlook 위의 기억 계층")
    p.add_argument("--home", help="데이터 디렉토리 (기본 <mailkb>/data, env MAILKB_HOME)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="홈 디렉토리·설정 생성").set_defaults(fn=cmd_init)

    sp = sub.add_parser("sync", help="메일 수집 (증분)")
    sp.add_argument("--source", choices=["fake", "outlook"])
    sp.add_argument("--full", action="store_true", help="전체 재수집")
    sp.add_argument("--since", help="이 날짜 이후만 (YYYY-MM-DD) — 첫 시험 수집용")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("ls", help="메일 목록")
    sp.add_argument("--unanswered", action="store_true", help="미답변 스레드")
    sp.add_argument("--today", action="store_true")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("search", help="검색 (연산자 from: after: is: 등 지원)")
    sp.add_argument("query", help='예: from:강미래 after:2026-06 리포트  ·  "정확한 구"')
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true", help="구조화 JSON 출력(도구·skill용)")
    sp.add_argument("--ai", action="store_true",
                    help="흐릿한 기억 AI 검색(번역·재순위·심층읽기; AI CLI 필요)")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("show", help="메일 본문 (인용 제거본)")
    sp.add_argument("ref", help="번호 또는 Message-ID")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("thread", help="스레드 타임라인")
    sp.add_argument("thread_id", type=int)
    sp.set_defaults(fn=cmd_thread)

    sp = sub.add_parser("note", help="스레드 → 지식 노트 템플릿")
    sp.add_argument("thread_id", type=int)
    sp.set_defaults(fn=cmd_note)

    sp = sub.add_parser("review", help="데일리 리뷰 (기본: AI 없음)")
    sp.add_argument("--ai", action="store_true", help="누적 요약 + 회고 분석")
    sp.add_argument("--backend", help="AI 백엔드 이름 (기본: config)")
    sp.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("hide", help="스레드 숨김 (목록·추적 제외, 새 메일 오면 자동 해제)")
    sp.add_argument("thread_id", type=int)
    sp.add_argument("--undo", action="store_true", help="숨김 해제")
    sp.set_defaults(fn=cmd_hide)

    sp = sub.add_parser("open", help="Outlook 에서 원문 열기 (회사 PC)")
    sp.add_argument("ref")
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("attach", help="스레드 첨부를 vault 로 추출 (회사 PC)")
    sp.add_argument("thread_id", type=int)
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser("act", help="개입 필요 큐 (결정론; --ai 로 정리)")
    sp.add_argument("--ai", action="store_true", help="AI 정리 (재분류·우선순위·사유/제안)")
    sp.add_argument("--note", help="AI 에 줄 추가 정보 (예: 'ECN은 처리 중, 납기건 우선')")
    sp.add_argument("--backend", help="AI 백엔드 이름")
    sp.set_defaults(fn=cmd_act)

    sp = sub.add_parser("noise", help="발신자별 수신량·차단 후보")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_noise)

    sp = sub.add_parser("block", help="발신자 제외(차단 목록) — Outlook 규칙과 병행")
    sp.add_argument("addr", help="발신 주소(부분 문자열 가능)")
    sp.set_defaults(fn=cmd_block)

    sp = sub.add_parser("unblock", help="차단 해제")
    sp.add_argument("addr")
    sp.set_defaults(fn=cmd_unblock)

    sp = sub.add_parser("serve", help="Minerva 웹 UI (localhost) — 질문 렌즈+메일 서식 렌더")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--open", action="store_true", help="브라우저 자동 열기")
    sp.add_argument("--app", action="store_true",
                    help="Edge 앱 모드(주소창 없는 독립 창)로 열기 — 실패 시 기본 브라우저")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("diagnose", help="진단 (스레딩·본문·요약·AI백엔드·과탐)")
    sp.add_argument("--backend", help="점검할 AI 백엔드 이름")
    sp.set_defaults(fn=cmd_diagnose)

    sub.add_parser("stats", help="저장소 통계").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except BrokenPipeError:
        sys.exit(0)
