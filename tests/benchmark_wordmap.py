"""인물 업무 어휘 지도 성능·정확성 재현 벤치마크.

일반 단위 테스트의 시간 제한으로 쓰지 않는다. 실행 환경별 시간을 기록하고,
어휘 다양성이 높은 코퍼스에서도 fast/raw 결과가 같은지 확인하는 수동 도구다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailkb import terms
from mailkb.sources.base import MailRecord
from mailkb.store import Store

ME = "me@corp.example"


def _record(index: int, people: int, tokens: int,
            diverse: bool, unique_threads: bool) -> MailRecord:
    addr = f"person{index % people:02d}@corp.example"
    common = [f"common{n:02d}" for n in range(10)]
    if diverse:
        variable = [
            f"m{index:05d}t{n:03d}"
            for n in range(max(1, tokens // 5 - len(common)))
        ]
    else:
        variable = [f"domain{n:02d}" for n in range(10)]
    sequence = common + variable
    repeats = max(1, tokens // len(sequence))
    body = " ".join((sequence * repeats)[:tokens])
    subject = (f"고유 안건 topic{index:05d}"
               if unique_threads else "공통 검토")
    sent_on = (
        datetime(2026, 7, 1, 9) + timedelta(minutes=index)
    ).isoformat()
    return MailRecord(
        message_id=f"<word-bench-{index}@test>",
        subject=subject,
        sender_name=addr.split("@")[0],
        sender_addr=addr,
        to=[ME],
        sent_on=sent_on,
        body_text=body,
    )


def _milliseconds(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def run(messages: int, people: int, tokens: int,
        diverse: bool, unique_threads: bool) -> dict:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "wordmap-benchmark.sqlite"
        store = Store(path, [ME])
        records = (
            _record(i, people, tokens, diverse, unique_threads)
            for i in range(messages)
        )

        started = time.perf_counter()
        store.ingest(records)
        sync_ms = _milliseconds(started)

        eligible = {
            f"person{i:02d}@corp.example" for i in range(people)
        }
        target = "person00@corp.example"
        names = store.word_people_names()

        started = time.perf_counter()
        target_rows = store.person_word_bag_rows(target)
        target_read_ms = _milliseconds(started)

        started = time.perf_counter()
        candidates = terms.background_candidates(
            target_rows, target, names=names)
        candidate_ms = _milliseconds(started)

        started = time.perf_counter()
        background = store.people_word_background(
            eligible, target, candidates=candidates)
        background_ms = _milliseconds(started)

        started = time.perf_counter()
        fast = terms.analyze(
            target_rows, target, names=names, background=background)
        analyze_ms = _milliseconds(started)

        started = time.perf_counter()
        store.people_word_background(
            eligible, target, candidates=candidates)
        cached_background_ms = _milliseconds(started)

        basis = store.person_word_basis(target)
        latest, since = basis["window_end"], basis["since"]
        marks = ",".join("?" * len(eligible))
        raw_rows = store.db.execute(
            f"""SELECT id, thread_id, subject, sender_addr,
                       sent_on, new_content
                FROM messages
                WHERE is_sent=0 AND sender_addr IN ({marks})
                  AND sent_on >= ?""",
            [*sorted(eligible), since],
        ).fetchall()
        started = time.perf_counter()
        raw = terms.analyze(raw_rows, target, names=names)
        raw_ms = _milliseconds(started)
        page_count = store.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
        rolling_rows = store.db.execute(
            "SELECT COUNT(*) FROM person_term_window").fetchone()[0]
        try:
            derived_bytes = store.db.execute(
                """SELECT COALESCE(SUM(pgsize), 0) FROM dbstat
                   WHERE name IN (
                     'message_term_features', 'message_term_bags',
                     'message_term_subject_delta', 'person_term_window',
                     'idx_messages_word_thread'
                   )"""
            ).fetchone()[0]
            derived_mib = round(derived_bytes / 1024 / 1024, 1)
        except sqlite3.OperationalError:
            # dbstat is optional in SQLite builds; timing/parity remain portable.
            derived_mib = None

        started = time.perf_counter()
        store.ingest([
            _record(messages, people, tokens, diverse, unique_threads)
        ])
        incremental_sync_ms = _milliseconds(started)

        result = {
            "messages": messages,
            "people": people,
            "tokens_per_message": tokens,
            "diverse": diverse,
            "unique_threads": unique_threads,
            "window_end": latest,
            "candidate_terms": len(candidates["terms"]),
            "candidate_phrases": len(candidates["phrases"]),
            "rolling_df_rows": rolling_rows,
            "db_mib": round(
                page_count * page_size / 1024 / 1024, 1),
            "word_derived_mib": derived_mib,
            "sync_ms": sync_ms,
            "incremental_sync_ms": incremental_sync_ms,
            "target_read_ms": target_read_ms,
            "candidate_ms": candidate_ms,
            "background_ms": background_ms,
            "analyze_ms": analyze_ms,
            "cached_background_ms": cached_background_ms,
            "raw_analyze_ms": raw_ms,
            "parity": fast == raw,
        }
        store.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=5000)
    parser.add_argument("--people", type=int, default=50)
    parser.add_argument("--tokens", type=int, default=300)
    parser.add_argument("--repetitive", action="store_true")
    parser.add_argument("--unique-threads", action="store_true")
    args = parser.parse_args()
    result = run(
        max(1, args.messages), max(2, args.people), max(20, args.tokens),
        diverse=not args.repetitive,
        unique_threads=args.unique_threads,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["parity"]:
        raise SystemExit("fast/raw word-map results differ")


if __name__ == "__main__":
    main()
