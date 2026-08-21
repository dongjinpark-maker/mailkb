"""mailkb CLI.

python -m mailkb <command>
사용 흐름: init → sync → ls/search/thread → note → review [--ai]
"""

from __future__ import annotations

import argparse
import platform
import sys
import threading
import time
from datetime import date

from . import actions
from . import config as config_mod
from . import notes, review
from . import store as store_mod
from .store import Store


def _store(cfg) -> Store:
    return Store(cfg.db_path, cfg.my_addresses, cfg.my_names, noise=cfg)


# 콘솔 인코딩 대체표 — cp949 에 없는 활자를 '?' 대신 알아볼 수 있는 것으로.
#
# 실제 Windows 콘솔은 UTF-16 API 라 이 경로를 안 탄다. 문제는 **리다이렉트와
# 작업 스케줄러 로그**다: 거기서는 스트림 인코딩이 로케일(cp949)이 되고, 종전
# errors="replace" 는 '—' 를 통째로 '?' 로 바꿔 "수집 ? 신규" 같은 줄을 남겼다.
# 코드가 자연스러운 활자를 계속 쓰게 두고 출력 경계에서만 바꾼다.
# 여기 있는 것은 전부 **cp949 에 실제로 없는** 문자다(있는 것을 적어 두면 죽은
# 항목이 된다 — … “ ” ≥ × 는 cp949 에 있어서 이 경로를 안 탄다).
_CONSOLE_FALLBACK = {
    "—": "―", "–": "-", "‐": "-",
    "✓": "●", "✗": "■", "⚠": "▲", "✕": "x",
    "⏰": "[기한]", "📎": "[첨부]", "🚩": "[플래그]", "🙈": "[숨김]",
    "⛔": "[차단]", "🧠": "[기억]", "↻": "[새로고침]",
}


def _console_fallback(err):
    bad = err.object[err.start:err.end]
    return ("".join(_CONSOLE_FALLBACK.get(c, "?") for c in bad), err.end)


def _install_console_fallback() -> None:
    import codecs
    try:
        codecs.lookup_error("mailkb")
    except LookupError:
        codecs.register_error("mailkb", _console_fallback)


def _folder_scope_lines(source) -> list:
    """수집 범위를 사람이 읽을 줄로 — 건너뛴 폴더는 **반드시** 이유와 함께.

    fake 소스처럼 폴더 개념이 없으면 빈 목록이다.
    """
    plan = getattr(source, "folder_plan", None)
    if plan is None:
        return []
    try:
        p = plan()
    except Exception as e:                 # 폴더 순회 실패도 조용히 넘기지 않는다
        return [f"  폴더 목록을 읽지 못했습니다 — {' '.join(str(e).split())[:120]}"]
    out = ["  " + p.summary_line()]
    fresh = p.unknown()
    if fresh:
        head = " · ".join(fresh[:3]) + (f" 외 {len(fresh) - 3}" if len(fresh) > 3 else "")
        out.append(f"  최초 수집 폴더 {len(fresh)}개 — 이번 한 번만 전체를 "
                   f"읽습니다(오래 걸릴 수 있음): {head}")
    for sk in p.skipped[:8]:
        out.append(f"  · 건너뜀 {sk.label} — {sk.reason}")
    if len(p.skipped) > 8:
        out.append(f"  · 건너뜀 {len(p.skipped) - 8}개 더")
    return out


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
    # 안내가 홈의 상태를 보고 갈린다 — 데모 홈에는 합성 코퍼스의 '나'(김도현)가
    # 이미 들어 있는데 "실제 주소로!"라고 하면 그대로 바꿔 발신 판정이 깨지고
    # 회고·미답변·내 약속이 통째로 빈다(README 는 반대로 "바꾸지 않는다"고 한다).
    # ai-rules.md 템플릿이 생기면서 데모 사용자도 이 명령을 반드시 거친다.
    try:
        filled = bool(config_mod.load(home).my_addresses)
    except Exception:                       # 설정이 깨졌으면 종전 안내가 맞다
        filled = False
    if filled:
        print(f"설정: {cfg_path}  (my_addresses 가 이미 있습니다 — "
              "데모 홈이면 그대로 두세요)")
    else:
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
    from .sources import folder_labels, get_source, remember_folder_plan

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    if store.recleaned:
        print(f"인용 재절단 {store.recleaned}건 — 절단 규칙 갱신 소급 적용",
              file=sys.stderr, flush=True)
    # 명시적 --since/--full 은 사용자가 범위를 직접 정한 것 → 깜짝 백필 금지.
    # 그 밖의 증분에서만 '처음 보는 폴더'를 한 번 통째로 읽는다(폴더 범위를
    # 넓혔을 때 옛 메일이 워터마크 뒤에 숨어 영영 안 들어오는 문제).
    known = None if (args.since or args.full) else store.synced_folders()
    source = get_source(args.source or cfg.source, cfg=cfg, known_folders=known)
    if args.since:  # 소량 시험 수집용 (예: --since 2026-07-01)
        since = args.since + "T00:00:00" if len(args.since) == 10 else args.since
    else:
        since = None if args.full else store.last_sync()

    # my_addresses 가 비면 **수집 시점의 발신 판정**이 전부 실패한다 — is_sent=0 이
    # 되어 회고의 '내 약속'·미답변·주간 '내 차례'가 통째로 빈다. 나중에 설정을
    # 채워도 안 살아난다(그 값은 색인할 때 쓰인다). 여기가 알려 줄 유일한 시점이다.
    if not cfg.my_addresses:
        print("경고: my_addresses 가 비어 있어 '내 발신'을 판정하지 못합니다 — "
              "회고·미답변이 빈 채로 나옵니다.\n"
              f"  {cfg.db_path.parent / 'config.toml'} 에 내 주소를 넣고 "
              "다시 sync 하세요.\n"
              '  (fake 데모라면 my_addresses = ["dohyun.kim@nurisoft.co.kr", '
              '"dhkim@nurisoft.co.kr"])',
              file=sys.stderr, flush=True)

    mode = "전체" if since is None and not args.since else "증분"
    print(f"sync 시작 · {source.name} · {mode}"
          + (f" (since {since[:16]})" if since else ""), file=sys.stderr, flush=True)
    # 폴더 계획은 **수집 전에** 찍는다 — 수십 분짜리 백필이 시작되고 나서
    # 알려 주면 사용자는 멈춘 것과 구별하지 못한다. 계획은 메모이즈되므로
    # fetch 가 같은 것을 재사용한다(미리보기와 실제 범위가 어긋날 수 없다).
    for line in _folder_scope_lines(source):
        print(line, file=sys.stderr, flush=True)
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
    # 성공으로 끝난 뒤에만 기록 — 부분 백필을 완료로 적으면 메일이 누락된다.
    # in_scope 를 함께 넘겨 범위를 벗어난 폴더를 기록에서 뺀다(껐다 켜는 사이의
    # 메일이 워터마크 뒤에 숨어 영영 안 들어오는 것을 막는다).
    store.mark_synced_folders(getattr(source, "drained_folders", None),
                              in_scope=folder_labels(source))
    remember_folder_plan(store, source)     # 웹 설정 화면이 쓸 폴더 목록
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
            warn = " ▲" if r["days_old"] >= 2 else ""
            print(
                f"  [#{r['thread_id']}] D+{r['days_old']} "
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
        except (review.AIError, review.AIAuthError) as e:
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
        cost = res.get("cost") or {}
        if cost.get("calls"):
            tok = int(cost.get("in", 0)) + int(cost.get("out", 0))
            secs = cost.get("seconds")
            tstr = (f"{secs / 60:.1f}분 · " if secs and secs >= 60
                    else f"{secs:.0f}초 · " if secs else "")
            print(f"— {tstr}${cost.get('usd', 0):.3f} · {tok:,}토큰 · {cost['calls']}회 호출")
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
        # 진단 형식이면 슬롯을 풀어 쓴다 — 저장 원문(`문제: … | 근거: "…"`)을
        # 그대로 찍으면 터미널에서 읽기 나쁘다. 옛 산문 요약은 그대로.
        diag = review.parse_diagnosis(t["rolling_summary"])
        if diag:
            print("\n[현안 브리핑]")
            for kind, body, quote in diag:
                print(f"  {kind}: {body}" + (f'\n      근거: "{quote}"' if quote else ""))
            print()
        else:
            print(f"\n[누적 요약]\n{t['rolling_summary']}\n")
    for m in msgs:
        print("─" * 60)
        print(f"{m['sent_on'][:16]}  {m['sender_name']} → {m['to_addrs']}")
        print()
        print(m["new_content"])


def cmd_note(args) -> None:
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    try:
        path = notes.create_thread_note(cfg, store, args.thread_id)
    except notes.NoThread as e:      # CLI 는 종전대로 메시지 + 종료 코드
        raise SystemExit(str(e))
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
        print(f"review --ai · {d} · 요약 {cfg.ai_summary_backend}",
              file=sys.stderr, flush=True)
        # graceful — AI 가 실패해도 결정론 리뷰는 항상 출력·저장 (#10)
        # run_ai_layer 은 3개 작업 단계(수확·디제스트·하루요약) 후 '완료'.
        # 인물 요약은 인물 화면 버튼(2026-07-29), 개입 분류·정리는 '지금 할 일'
        # 폐지와 함께 제거(2026-07-30) — 오늘·백필 모두 같은 단계 구성.
        # 누적 요약 갱신은 2026-08-15 에 스레드 화면 버튼으로 분리됐다.
        total = 3
        ai_text, note = review.run_ai_layer(
            store, cfg, det, backend=args.backend, persist_date=d,
            progress=_StageProgress(total),
        )
        if note:
            print(note, file=sys.stderr, flush=True)

    content = review.render(det, ai_text, store)
    path = notes.write_daily(cfg, d, content)
    print(review.strip_done_marks(content))   # 표식은 웹 전용 — 터미널엔 잡음이다
    print(f"저장됨: {path}")


def _pick_threads(store, cfg, n: int) -> list[int]:
    """진단해 볼 만한 업무 스레드 n 개 — **최근 활동 우선**, 그 안에서 큰 순.

    평가용 표본을 사람이 고르지 않아도 되게 한다(고르는 순간 편향이 든다).
    정렬 기준이 처음에는 통수·본문량뿐이었는데, 그러면 **크지만 이미 끝난
    스레드**가 뽑힌다 — 2026-08-18 회사 PC 실측에서 문제 21개 중 12개가 기각됐고
    사유가 전부 "그때는 문제였지만 회의·결정·프로젝트 종료로 해소됐다"였다.
    진단은 그 시점의 스냅샷이라, 끝난 스레드에 들이대면 맞는 지적도 폐기된다.
    그래서 마지막 활동일을 1차 기준으로 둔다(같은 날이면 통수·본문량 순).
    """
    rows = []
    for r in store.db.execute("SELECT id FROM threads WHERE hidden=0").fetchall():
        msgs = store.thread_messages(r["id"])
        if not msgs or review.thread_kind(cfg, msgs) != "work":
            continue
        chars = sum(len(m["new_content"] or "") for m in msgs)
        rows.append(((msgs[-1]["sent_on"] or "")[:10], len(msgs), chars, r["id"]))
    rows.sort(reverse=True)
    return [r[-1] for r in rows[:max(1, n)]]


def cmd_thread_diag(args) -> None:
    """스레드 현안 브리핑 — 웹 [현안 브리핑] 버튼과 같은 산출을 터미널로.

    함수 이름이 cmd_diagnose 가 아닌 이유: 그 이름은 **환경 진단**이 이미 쓰고
    있고, 파이썬은 나중 정의가 이겨서 조용히 그쪽이 실행된다(실제로 그렇게
    만들었다가 스모크에서 잡았다).

    평가·자동화용 진입점이다(웹은 클릭이라 표본 10건을 돌리기 번거롭다).
    AI 를 부르므로 사용자가 요청했을 때만 실행한다.
    """
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    tids = args.threads or _pick_threads(store, cfg, args.pick)
    for tid in tids:
        msgs = store.thread_messages(tid)
        if not msgs:
            print(f"#{tid}: 스레드 없음", file=sys.stderr)
            continue
        subject = (msgs[0]["subject"] or "").strip()
        try:
            text = review.diagnose_thread(store, cfg, tid, backend=args.backend)
        except review.AIError as e:
            print(f"#{tid} 실패: {str(e).splitlines()[0][:120]}", file=sys.stderr)
            continue
        dropped = getattr(review.diagnose_thread, "last_dropped", 0)
        drop_s = f" · 근거 검증 탈락 {dropped}줄" if dropped else ""
        print(f"\n=== #{tid} {subject} ({len(msgs)}통){drop_s} ===")
        if not text:
            print("  (현안 브리핑을 만들지 못했습니다)")
            continue
        for kind, body, quote in review.parse_diagnosis(text):
            print(f"  {kind}: {body}")
            if quote:
                print(f'      근거: "{quote}"')


def cmd_person_diag(args) -> None:
    """인물 현안 브리핑 — 인물 화면 [현안 브리핑] 버튼과 같은 산출을 터미널로(AI 1콜)."""
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    addr = (args.addr or "").strip().lower()
    name = store.person_name(addr)
    try:
        text = review.diagnose_person(store, cfg, addr, name,
                                      backend=args.backend)
    except review.AIError as e:
        raise SystemExit(str(e).splitlines()[0][:160])
    dropped = getattr(review.diagnose_person, "last_dropped", 0)
    drop_s = f" · 근거 검증 탈락 {dropped}줄" if dropped else ""
    print(f"\n=== {name or addr}{drop_s} ===")
    if not text:
        print("  (현안 브리핑을 만들지 못했습니다 — 교신 기록이 없습니다)")
        return
    for kind, body, quote in review.parse_diagnosis(text):
        print(f"  {kind}: {body}")
        if quote:
            print(f'      근거: "{quote}"')


def cmd_ask(args) -> None:
    from . import ask as ask_mod

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    if args.history:                        # 이력만 보고 끝
        rows = ask_mod.history(store)
        if not rows:
            print("질문 이력 없음")
            return
        for h in rows:
            mark = "↳ " if h["parent_id"] else ""
            print(f"[{h['id']:>3}] {h['created'][:16]} {h['state']:<6} "
                  f"근거 {h['claims']}  {mark}{h['question']}")
        print("\n다시 열기:  mailkb ask --show <번호>")
        return
    if args.show:                           # 저장된 답변 그대로 열기
        res = ask_mod.load(store, args.show)
        if not res:
            raise SystemExit(f"저장된 답변 없음: {args.show}")
        print(ask_mod.render_text(res))
        return
    if args.context:                        # 결정론 문맥만 — AI 호출 0
        q = args.question or ""
        if args.person and not q:            # 인물 브리핑과 같은 질문으로 고른다
            q = ask_mod.brief_question(store.person_name(args.person)
                                       or args.person)
        print(ask_mod.context_text(store, cfg, q))
        return
    if args.person:                         # 인물 브리핑 — 같은 엔진, 범위만 고정
        try:
            res = ask_mod.brief(store, cfg, args.person,
                                name=store.person_name(args.person) or "",
                                backend=args.backend, use_cache=not args.fresh)
        except (review.AIError, review.AIAuthError) as e:
            raise SystemExit(f"브리핑 불가: {e}")
        print(ask_mod.render_text(res))
        return
    if not args.question:
        raise SystemExit("질문을 입력하세요 (또는 --person / --history / --show)")
    prog = None
    if sys.stderr.isatty():
        def prog(msg):                      # 라운드 진행을 stderr 로(결과는 stdout)
            print(f"  … {msg}", file=sys.stderr, flush=True)
    try:
        res = ask_mod.ask(store, cfg, args.question, backend=args.backend,
                          use_cache=not args.fresh, progress=prog,
                          parent_id=args.follow)
    except (review.AIError, review.AIAuthError) as e:
        # graceful — AI 불가면 일반 검색 결과라도 보여준다(#10)
        print(f"AI 조사 불가: {e}\n일반 검색으로 대체합니다.", file=sys.stderr)
        rows = store.search(args.question, 10)
        if not rows:
            raise SystemExit("검색 결과도 없습니다.")
        for r in rows:
            print(f"[{r['thread_id']:>4}] {r['sent_on'][:16]} "
                  f"{r['sender_name'] or r['sender_addr']}: {r['subject']}")
        return
    print(ask_mod.render_text(res))


def cmd_weekly(args) -> None:
    from . import weekly as weekly_mod

    cfg = config_mod.load(args.home)
    store = _store(cfg)
    if args.ai:
        print(f"weekly --ai · 최근 {args.weeks}주 · "
              f"토픽 최대 {weekly_mod.MAX_TOPICS}개", file=sys.stderr, flush=True)
    # graceful — AI 실패해도 결정론 뼈대는 출력·저장 (#10). 단계 수는 최대치 기준.
    prog = _StageProgress(weekly_mod.MAX_AI_CALLS) if args.ai else None
    content, det = weekly_mod.generate(
        store, cfg, weeks=args.weeks, ai=args.ai, backend=args.backend,
        today=args.date, progress=prog)
    path = weekly_mod.write(cfg, det, content)
    print(review.strip_done_marks(content))   # 표식은 웹 전용
    print(f"\n저장됨: {path}")


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
    source = get_source("outlook", cfg=cfg)   # Windows 전용
    ok = source.open_in_outlook(m["entry_id"], m["message_id"])
    # 열기의 성공/실패는 "Outlook 에 아직 있나"에 대한 공짜 답이다 — 추가 COM
    # 왕복 없이 여기서 기록해 두면 유령 메일이 미답변 목록에서 빠진다.
    store.set_gone(m["id"], not ok)
    if ok:
        print("Outlook 에서 열림")
    else:
        raise SystemExit("Outlook 에서 찾지 못함 — 지웠거나 수집 범위 밖 폴더로 "
                         "옮긴 메일입니다. 목록에는 'Outlook 에 없음'으로 "
                         "표시되고 미답변 판정에서 빠집니다")


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


_AUDIT_LEVELS = {"required": "REQUIRED", "maybe": "MAYBE", "none": "NONE"}


def _audit_rows(store, cfg, sample: int) -> list[dict]:
    """감사 대상 — 최근 활동 스레드(수신 있는 것) 최신순 sample 건 + 현재 판정.

    열린 액션이 없는(NONE) 스레드도 섞는다 — 오탐만이 아니라 놓침(FN)도 보여야
    규칙 품질이 측정된다."""
    acts = actions.classify_threads(store, cfg)
    rows = []
    for t in store.open_thread_tails():
        if t["last_is_sent"] and t["msg_count"] == t["my_msg_count"]:
            continue                     # 내 발신 전용 스레드는 감사 대상 아님
        a = acts.get(t["thread_id"]) or actions.Action(actions.NONE)
        rows.append({
            "thread_id": t["thread_id"], "subject": t["subject"],
            "who": t["sender_name"] or t["sender_addr"],
            "sent_on": (a.sent_on or t["sent_on"])[:16],
            "level": a.level, "kind": a.kind,
            "reasons": list(a.reasons),
            "reason_text": a.reason_text() or "열린 요청 없음",
            "evidence": actions.evidence_sentence(store, a),
        })
        if len(rows) >= sample:
            break
    return rows


def _audit_print(i: int, r: dict) -> None:
    lv = _AUDIT_LEVELS.get(r["level"], r["level"])
    kind = f"/{r['kind']}" if r["kind"] else ""
    print(f"[{i:>3}] #{r['thread_id']} {lv}{kind}  {r['subject'][:48]}")
    print(f"      {r['who']} · {r['sent_on']} · {r['reason_text']}")
    if r["evidence"]:
        print(f"      「{r['evidence'][:76]}」")


def cmd_audit(args) -> None:
    """분류 판정 감사 — 실메일 위에서 판정+근거를 보고, 라벨을 쌓아 측정한다.

    합성 문장 평가의 한계(자기 채점) 보완: 라벨은 <home>/labels.jsonl 에만
    쌓인다(개인정보는 data/ 원칙). --report 는 저장 라벨을 현재 규칙으로
    재판정해 혼동 행렬을 낸다 — 규칙을 고칠 때마다 실메일 기준 개선/후퇴가 보인다.
    """
    import json as _json
    cfg = config_mod.load(args.home)
    store = _store(cfg)
    labels_path = cfg.home / "labels.jsonl"

    if args.report:
        if not labels_path.exists():
            print("라벨 없음 — 먼저 `mailkb audit --label` 로 라벨을 쌓으세요.")
            return
        recs = [_json.loads(ln) for ln in
                labels_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # 같은 스레드 재라벨 시 마지막 것만
        latest = {r["thread_id"]: r for r in recs}
        matrix: dict[tuple, int] = {}
        mismatch = []
        for r in latest.values():
            cur = actions.evaluate_thread(store, cfg, r["thread_id"]).level
            matrix[(r["truth"], cur)] = matrix.get((r["truth"], cur), 0) + 1
            if r["truth"] != cur:
                mismatch.append((r, cur))
        lv = ["required", "maybe", "none"]
        print(f"라벨 {len(latest)}건 (정답 × 현재 판정)")
        print(f"{'':>10} " + " ".join(f"{_AUDIT_LEVELS[c]:>9}" for c in lv))
        for t in lv:
            print(f"{_AUDIT_LEVELS[t]:>10} "
                  + " ".join(f"{matrix.get((t, c), 0):>9}" for c in lv))
        ok = sum(matrix.get((t, t), 0) for t in lv)
        print(f"일치 {ok}/{len(latest)}")
        for r, cur in mismatch:
            print(f"  ■ #{r['thread_id']} {r['subject'][:40]} — 정답 "
                  f"{_AUDIT_LEVELS[r['truth']]}, 현재 {_AUDIT_LEVELS[cur]}")
        return

    rows = _audit_rows(store, cfg, args.sample)
    if not rows:
        print("감사할 스레드 없음")
        return
    if not args.label:
        for i, r in enumerate(rows, 1):
            _audit_print(i, r)
        print(f"\n{len(rows)}건 — 라벨을 쌓으려면 `mailkb audit --label`")
        return

    today = date.today().isoformat()
    keymap = {"y": None, "r": "required", "m": "maybe", "x": "none"}
    n_saved = 0
    print("판정이 맞으면 y, 틀리면 정답을 r(equired)/m(aybe)/x(none), s 건너뜀, q 종료")
    with labels_path.open("a", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            _audit_print(i, r)
            try:
                ans = input("  → [y/r/m/x/s/q] ").strip().lower()
            except EOFError:
                break
            if ans == "q":
                break
            if ans not in keymap:
                continue
            truth = keymap[ans] or r["level"]
            f.write(_json.dumps({
                "date": today, "thread_id": r["thread_id"],
                "subject": r["subject"], "level": r["level"],
                "kind": r["kind"], "reasons": r["reasons"], "truth": truth,
            }, ensure_ascii=False) + "\n")
            n_saved += 1
    print(f"라벨 {n_saved}건 저장 → {labels_path} (요약: mailkb audit --report)")


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


def cmd_doctor(args) -> None:
    """사전 점검 — 수집 전에 '이 PC 에서 되는가' 를 30초 안에.

    AI 호출 0 · 네트워크 0. 설정도 DB 도 없는 상태(init 전)에서 돌아야 하고,
    Linux 에서는 죽지 않고 '데모만 가능' 을 말해야 한다. 아무것도 만들지 않는다.
    """
    from . import doctor as doctor_mod

    home = config_mod.resolve_home(args.home)
    try:
        cfg = config_mod.load(args.home)
    except (SystemExit, Exception):
        # init 전 = doctor 에게는 **정상 입력**이다. load 는 설정이 없으면
        # SystemExit 로 죽는데(다른 명령에는 맞는 동작), 여기서는 그게 곧
        # 점검 결과 한 줄이어야 한다. main 도 같은 이유로 둘 다 잡는다.
        cfg = None
    ol = None
    if sys.platform == "win32":
        # 가드 프로브가 모달을 띄울 수 있다. 그게 목적이지만(20분 뒤 대신 지금)
        # 아무 예고 없이 뜨면 사용자는 멈춘 줄 안다.
        print("점검 중… Outlook 보안 경고가 뜨면 [허용]을 누르세요 "
              "(그 팝업이 곧 결과입니다).", file=sys.stderr, flush=True)
        try:
            from .sources.outlook_com import probe_outlook
            known = None
            if cfg is not None and cfg.db_path.exists():
                store = _store(cfg)
                known = store.synced_folders()
                store.close()
            ol = probe_outlook(cfg, known=known)
        except Exception as e:              # 프로브 자체의 사고도 결과로 보고
            ol = {"available": False,
                  "error": " ".join(str(e).split())[:200]}
    checks = doctor_mod.run(cfg, home, ol)
    head = (f"{platform.system()} {platform.release()} · "
            f"Python {platform.python_version()} · home {home}")
    print(doctor_mod.render(checks, head))
    raise SystemExit(doctor_mod.exit_code(checks))


def cmd_diagnose(args) -> None:
    """실 데이터 진단 — 스레딩·본문품질·요약·AI 백엔드·개입 큐 과탐을 수치로.

    회사 PC에서 '요약이 안 됨 / false alarm 많음' 을 데이터로 짚기 위한 도구.
    읽기 전용(AI 백엔드는 **역할이 쓰는 백엔드마다** 짧은 시험 호출 1회 —
    기본 설정이면 sonnet·opus 둘. `--backend` 를 주면 그것 하나만).
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
            print("  ▲ 단일메일 비율 높음 — 스레딩 미결합. "
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
            print("  ▲ HTML 태그 남은 메일 존재 — #2 수정 후 재수집 필요: sync --full")
        if row["empty"] and row["empty"] * 100 // max(s["messages"], 1) >= 20:
            print("  ▲ 빈 본문 비율 높음 — 인용 제거 과잉 또는 본문 추출 실패")
        last = store.get_state("last_reclean")
        if last:
            when, _, cnt = last.rpartition(":")
            print(f"  인용 재절단 {cnt}건 실행됨 ({when[:16]}) — 규칙 갱신 소급 적용")
        bk = db.execute("SELECT COUNT(*) n, COALESCE(SUM(LENGTH(old_content)),0) b, "
                        "MAX(from_version) v FROM reclean_backup").fetchone()
        if bk["n"]:
            print(f"  재절단 백업 {bk['n']}건 · {bk['b']/1e6:.1f}MB "
                  f"(v{bk['v']} 직전 원본, {store_mod.RECLEAN_BACKUP_DAYS}일 보관) "
                  "— 절단이 정상이면 지워도 된다: DELETE FROM reclean_backup")
        # 인용 절단 실패 의심 — 후속 메일이 직전 본문을 통째 재포함(미지원 언어
        # 헤더 등). 여기 뜨는 스레드의 헤더 라벨을 clean.py 에 추가하면 된다.
        sus = store.suspect_uncut_quotes()
        if sus:
            print(f"  ▲ 인용 절단 실패 의심 {len(sus)}개 스레드 — "
                  "요약 입력이 반복 부풀 수 있음:")
            for d in sus:
                print(f"    [#{d['thread_id']}] {d['subject']} "
                      f"({d['domain']} · 재포함 {d['pairs']}쌍)")

    # 3) 요약 커버리지
    summ = db.execute(
        "SELECT COUNT(*) n FROM threads WHERE TRIM(rolling_summary)!=''"
    ).fetchone()["n"]
    print(f"\n[요약] 진단 보유 스레드 {summ}/{s['threads']}")
    if summ == 0:
        print("  ▲ 요약 0건 — review --ai 를 성공적으로 돌린 적 없거나 AI 백엔드 미작동")

    # 4) AI 백엔드 점검 — '요약이 안 됨'의 직접 원인 확인.
    #    --backend 를 주면 그것만, 아니면 **역할이 실제로 부르는 백엔드 전부**를
    #    중복 없이 시험한다(보통 sonnet·opus 둘). 요약(sonnet)만 보면 현안
    #    브리핑(기본 opus)이 그 CLI 에서 되는지 알 수 없고, 실패가 웹에서 버튼을
    #    누른 뒤에야 드러난다 — 같은 이유로 doctor 도 이 역할을 함께 본다.
    print("\n[AI 백엔드]")
    if args.backend:
        targets = [(args.backend, "")]
    else:
        roles: dict[str, list[str]] = {}
        for role in cfg._ROLES:
            name = cfg.backend_for(role)
            if name:
                roles.setdefault(name, []).append(
                    config_mod.ROLE_LABEL.get(role, role))
        targets = [(n, "·".join(r)) for n, r in roles.items()]
    for name, roles_s in targets:
        try:
            cmd = cfg.ai_cmd(name)
        except SystemExit as e:
            print(f"  설정 없음: {e}")
            continue
        print(f"  {name}" + (f" ({roles_s})" if roles_s else "")
              + f" — {' '.join(cmd)}")
        try:
            out = review.ai_run(cmd, "한 단어로만 답하라. 정상이면 OK.",
                                timeout=30, retries=0)
            print(f"    ● 응답: {out.splitlines()[0][:60]!r}")
        except review.AITimeout:
            # '안 된다'가 아니라 '늦는다' — 웹 점검 화면과 같은 어휘를 쓴다
            print("    ▲ 무응답: 30초 안에 대답이 없습니다")
            print("    → 느린 백엔드일 수 있습니다. 다시 돌려 보고, 계속 이러면 "
                  "그 CLI 를 직접 실행해 로그인·프록시를 확인하세요.")
        except (review.AIError, review.AIAuthError) as e:
            print(f"    ■ 실패: {str(e)[:180]}")
            print("    → CLI 설치·PATH·인증, 그리고 그 CLI 가 이 모델을 "
                  "지원하는지 확인. 이 역할이 이 때문에 빈다.")

    # 5) 개입 큐 과탐 분해 (false alarm)
    d = date.today().isoformat()
    queue = review.intervention_queue(store, cfg, d)
    by_cat = Counter(it["category"] for it in queue)
    # 리포트는 '처리함'으로 접은 것을 뺀 뒤 보여 준다 — 여기 숫자와 다른 이유다
    folded = len(store.report_done_keys("stalled"))
    print(f"\n[개입 큐] 총 {len(queue)}건  (broadcast_to={cfg.broadcast_to}, "
          f"stall={cfg.stall_workdays}, stale={cfg.stale_workdays} 영업일"
          + (f", 리포트에선 처리함 {folded}건 제외" if folded else "") + ")")
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
    _install_console_fallback()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="mailkb")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
    p = argparse.ArgumentParser(prog="mailkb",
                                description="Outlook 메일을 AI 로 읽고, 결정을 기억한다")
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

    # 이름이 `diagnose`(환경 진단)와 겹치지 않게 thread-diag 다 — 둘 다 '진단'
    # 이지만 하나는 설치 환경, 하나는 메일 스레드다.
    sp = sub.add_parser("thread-diag",
                        help="스레드 현안 브리핑 (AI — 웹 [현안 브리핑] 버튼과 동일)")
    sp.add_argument("threads", nargs="*", type=int, help="스레드 번호 (없으면 --pick)")
    sp.add_argument("--pick", type=int, default=5,
                    help="번호를 안 주면 통수·본문량이 큰 업무 스레드 N 개 (기본 5)")
    sp.add_argument("--backend", default=None, help="기본 = [ai] diagnose")
    sp.set_defaults(fn=cmd_thread_diag)

    sp = sub.add_parser("person-diag",
                        help="인물 현안 브리핑 (AI — 인물 화면 [현안 브리핑]과 동일)")
    sp.add_argument("addr", help="상대 메일 주소")
    sp.add_argument("--backend", default=None, help="기본 = [ai] diagnose")
    sp.set_defaults(fn=cmd_person_diag)

    sp = sub.add_parser("note", help="스레드 → 지식 노트 템플릿")
    sp.add_argument("thread_id", type=int)
    sp.set_defaults(fn=cmd_note)

    sp = sub.add_parser("review", help="일간 회고 (기본: AI 없음)")
    sp.add_argument("--ai", action="store_true", help="수확·핵심 한 줄·하루 요약")
    sp.add_argument("--backend", help="AI 백엔드 이름 (기본: config)")
    sp.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("ask", help="질문하기 — 저장된 메일에서 근거 달린 답 (AI CLI 필요)")
    sp.add_argument("question", nargs="?", help='예: "NPX-200 양자화 최종 결정 뭐였지?"')
    sp.add_argument("--follow", type=int, metavar="번호",
                    help="그 답변에 이어지는 추가 질문(이전 조사 승계)")
    sp.add_argument("--person", metavar="주소",
                    help="인물 브리핑 — 그 사람과의 최근 교신에서 알아야 할 것")
    sp.add_argument("--history", action="store_true", help="질문 이력 목록")
    sp.add_argument("--show", type=int, metavar="번호", help="저장된 답변 다시 보기")
    sp.add_argument("--context", action="store_true",
                    help="엔진이 이 질문에 실을 지침·내 노트·지식만 보기 (AI 호출 0)")
    sp.add_argument("--backend", help="AI 백엔드 이름 (기본: config)")
    sp.add_argument("--fresh", action="store_true", help="캐시 무시하고 다시 조사")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("weekly", help="주간 보고 — 내가 관여한 사안을 토픽별 진행·이슈·향후로")
    sp.add_argument("--weeks", type=int, default=1, help="기간(주, 기본 1)")
    sp.add_argument("--ai", action="store_true", help="토픽 묶기·서술 (AI CLI 필요)")
    sp.add_argument("--backend", help="AI 백엔드 이름 (기본: config)")
    sp.add_argument("--date", help="YYYY-MM-DD 기준 종료일 (기본: 오늘)")
    sp.set_defaults(fn=cmd_weekly)

    sp = sub.add_parser("hide", help="스레드 숨김 (목록·추적·AI 프롬프트 제외, 새 메일 오면 자동 해제)")
    sp.add_argument("thread_id", type=int)
    sp.add_argument("--undo", action="store_true", help="숨김 해제")
    sp.set_defaults(fn=cmd_hide)

    sp = sub.add_parser("open", help="Outlook 에서 원문 열기 (회사 PC)")
    sp.add_argument("ref")
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("attach", help="스레드 첨부를 vault 로 추출 (회사 PC)")
    sp.add_argument("thread_id", type=int)
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser("audit", help="분류 판정 감사 — 실메일 라벨링·혼동 행렬")
    sp.add_argument("--sample", type=int, default=30, help="샘플 수 (최신순)")
    sp.add_argument("--label", action="store_true", help="대화형 라벨링 → labels.jsonl")
    sp.add_argument("--report", action="store_true", help="라벨 대비 현재 판정 혼동 행렬")
    sp.set_defaults(fn=cmd_audit)

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
                    help="Edge 앱 모드(주소창 없는 독립 창)로 열기 — 실패 시 기본 "
                         "브라우저. 창을 닫으면 서버도 함께 종료된다")
    sp.set_defaults(fn=cmd_serve)

    sub.add_parser("doctor", help="사전 점검 — 환경·Outlook·폴더 범위·설정·DB·"
                                  "AI 경로 (AI 호출 0)").set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("diagnose", help="진단 (스레딩·본문·요약·AI백엔드·과탐)")
    sp.add_argument("--backend", help="점검할 AI 백엔드 이름")
    sp.set_defaults(fn=cmd_diagnose)

    sub.add_parser("stats", help="저장소 통계").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    # AI 실패 로그 목적지 주입 — ai_run 은 cfg 를 모르는 함수라 여기서 한 번
    # 지정한다(<home>/logs/ai_error.jsonl). 진단 편의 기능이 본 명령 실행을
    # 막으면 안 되므로 설정 로드 실패는 로그 없이 계속한다(init 전의
    # config.load 는 SystemExit 을 던진다 — Exception 계열이 아니라서 반드시
    # 함께 잡는다, e2e 로 실제 init 이 죽는 회귀 확인됨). [ai] error_log=false 로 끔.
    try:
        _cfg0 = config_mod.load(args.home)
        if _cfg0.opt("ai", "error_log", default=True):
            review.AI_ERROR_LOG_DIR = _cfg0.home / "logs"
    except (SystemExit, Exception):
        pass
    try:
        args.fn(args)
    except BrokenPipeError:
        sys.exit(0)
