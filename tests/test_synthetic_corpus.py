"""Integrity and realism checks for the labeled classifier corpus."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from mailkb import actions
from mailkb.clean import extract_new_content
from mailkb.config import Config
from mailkb.sources.fake import FakeSource
from mailkb.store import Store
from tests.synthetic_corpus import (
    ACTION_MAYBE,
    ACTION_NONE,
    ACTION_REQUIRED,
    ME,
    ME_ALIAS,
    NOISE_HARD,
    NOISE_MIXED,
    NOISE_POLICY,
    SCHEDULE_CANCEL,
    SCHEDULE_CHANGE,
    SCHEDULE_EVENT,
    SCHEDULE_INFO,
    SyntheticEvaluationSource,
    build_synthetic_corpus,
    evaluate_action_predictions,
    evaluate_axis_predictions,
)


class TestSyntheticCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = build_synthetic_corpus()

    def test_size_and_label_distribution(self):
        self.assertEqual(len(self.corpus.records), 1020)
        self.assertEqual(len(self.corpus.messages), 1020)
        self.assertEqual(len(self.corpus.threads), 490)
        # 라벨 정의 확정(2026-07-17): 관용구 22건 NONE, CC+전원 3건 MAYBE,
        # 외부 뉴스레터 10건은 hard 가 아니라 policy(차단 설정 시에만 hard).
        self.assertEqual(
            Counter(t.action for t in self.corpus.threads.values()),
            Counter({
                ACTION_REQUIRED: 97,
                ACTION_MAYBE: 41,
                ACTION_NONE: 352,
            }),
        )
        self.assertEqual(
            Counter(t.noise for t in self.corpus.threads.values()),
            Counter({
                "none": 384,
                NOISE_HARD: 60,
                NOISE_MIXED: 30,
                NOISE_POLICY: 16,
            }),
        )
        schedules = Counter(t.schedule for t in self.corpus.threads.values())
        self.assertEqual(schedules[SCHEDULE_EVENT], 14)
        self.assertEqual(schedules[SCHEDULE_CHANGE], 12)
        self.assertEqual(schedules[SCHEDULE_CANCEL], 12)
        self.assertEqual(schedules[SCHEDULE_INFO], 12)

    def test_is_about_five_times_the_interactive_demo(self):
        demo_count = len(list(FakeSource().fetch(None)))
        ratio = len(self.corpus.records) / demo_count
        self.assertGreaterEqual(ratio, 4.5)
        self.assertLessEqual(ratio, 5.5)

    def test_ids_references_and_chronology_are_consistent(self):
        records = self.corpus.records
        self.assertEqual(
            records,
            sorted(records, key=lambda r: (r.sent_on, r.message_id)),
        )
        by_id = {r.message_id: r for r in records}
        self.assertEqual(len(by_id), len(records))
        for record in records:
            self.assertEqual(record.references, (
                [record.in_reply_to] if record.in_reply_to else []
            ))
            if not record.in_reply_to:
                continue
            parent = by_id[record.in_reply_to]
            self.assertEqual(parent.conversation_key, record.conversation_key)
            self.assertLess(parent.sent_on, record.sent_on)

    def test_truth_sources_exist_and_match_threads(self):
        for key, truth in self.corpus.threads.items():
            if truth.action in {ACTION_REQUIRED, ACTION_MAYBE}:
                self.assertTrue(truth.source_message_id, msg=key)
                source = self.corpus.messages[truth.source_message_id]
                self.assertEqual(source.thread_key, key)
            else:
                self.assertEqual(truth.source_message_id, "", msg=key)

    def test_realistic_content_shapes_are_present(self):
        records = self.corpus.records
        self.assertGreaterEqual(sum(bool(r.body_html) for r in records), 650)
        self.assertGreaterEqual(sum(bool(r.attachments) for r in records), 140)
        self.assertGreaterEqual(sum(bool(r.in_reply_to) for r in records), 500)
        self.assertGreaterEqual(sum(r.sender_addr in {ME, ME_ALIAS} for r in records), 150)
        self.assertGreaterEqual(sum(len(r.to) >= 5 for r in records), 40)
        text = "\n".join(r.body_text for r in records)
        self.assertIn("Please reply", text)
        self.assertIn("？", text)
        self.assertIn("-----Original Message-----", text)
        self.assertIn("보낸 사람:", text)
        self.assertIn("기존 요청은 무시해 주세요", text)

    def test_corpus_spans_more_than_one_month(self):
        stamps = [datetime.fromisoformat(r.sent_on) for r in self.corpus.records]
        self.assertGreaterEqual((max(stamps) - min(stamps)).days, 40)

    def test_quoted_replies_extract_exact_authored_content(self):
        replies = [r for r in self.corpus.records if r.in_reply_to]
        for record in replies:
            expected = self.corpus.messages[record.message_id].authored_body
            self.assertEqual(extract_new_content(record.body_text), expected)

    def test_all_addresses_are_fictional_test_domains(self):
        allowed = {"nurisoft.co.kr", "foundry.example", "marketing.example"}
        for record in self.corpus.records:
            addresses = [record.sender_addr] + record.to + record.cc
            for address in addresses:
                self.assertIn(address.rsplit("@", 1)[-1], allowed)

    def test_source_since_filter_is_strict(self):
        source = SyntheticEvaluationSource()
        pivot = source.corpus.records[len(source.corpus.records) // 2].sent_on
        fetched = list(source.fetch(pivot))
        self.assertTrue(fetched)
        self.assertTrue(all(record.sent_on > pivot for record in fetched))

    def test_action_evaluator_accepts_any_classifier_api(self):
        perfect = {
            key: truth.action for key, truth in self.corpus.threads.items()
        }
        result = evaluate_action_predictions(self.corpus, perfect)
        self.assertEqual(result["required"]["precision"], 1.0)
        self.assertEqual(result["required"]["recall"], 1.0)
        self.assertEqual(result["false_alarms"], [])
        self.assertEqual(result["misses"], [])

        changed = dict(perfect)
        required_key = next(
            key for key, truth in self.corpus.threads.items()
            if truth.action == ACTION_REQUIRED
        )
        changed[required_key] = ACTION_MAYBE
        result = evaluate_action_predictions(self.corpus, changed)
        self.assertEqual(result["required"]["fn"], 1)
        self.assertEqual(result["maybe_recovery"], [required_key])

    def test_generic_axis_evaluator_covers_noise_and_schedule(self):
        for axis in ("noise", "schedule"):
            perfect = {
                key: getattr(truth, axis)
                for key, truth in self.corpus.threads.items()
            }
            result = evaluate_axis_predictions(self.corpus, perfect, axis)
            self.assertEqual(result["accuracy"], 1.0)
            self.assertEqual(result["mismatches"], [])

    def test_store_ingests_all_threads_and_derived_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "corpus.sqlite", [ME, ME_ALIAS])
            try:
                stats = store.ingest(self.corpus.records)
                self.assertEqual(stats.inserted, 1020)
                self.assertEqual(store.stats()["messages"], 1020)
                self.assertEqual(store.stats()["threads"], 490)
                self.assertEqual(
                    store.db.execute(
                        "SELECT COUNT(*) FROM message_features"
                    ).fetchone()[0],
                    1020,
                )
                self.assertEqual(
                    store.db.execute(
                        "SELECT COUNT(*) FROM thread_state"
                    ).fetchone()[0],
                    490,
                )
                keys = {
                    row["conversation_key"]
                    for row in store.db.execute(
                        "SELECT conversation_key FROM threads"
                    )
                }
                self.assertEqual(
                    keys,
                    {f"SYNTHETIC-{key}" for key in self.corpus.threads},
                )
            finally:
                store.close()


class TestClassifierGate(unittest.TestCase):
    """구현 판정기를 코퍼스 정답에 상시 배선한 회귀 게이트 (액션 + 노이즈 축).

    라벨은 2026-07-17 사용자 확정 기준. 규칙을 바꿔 지표가 내려가면 여기서
    잡힌다 — 의도된 정의 변경이면 코퍼스 라벨과 이 게이트를 함께 갱신할 것.
    """

    @classmethod
    def setUpClass(cls):
        cls.corpus = build_synthetic_corpus()
        cls.tmp = tempfile.TemporaryDirectory()
        # 액션 축: 협력사(foundry.example)는 허용 목록 — allowlist 가 policy
        # 노이즈 분기를 우회시켜 약한 요청도 확인 후보로 남는 것까지가 계약.
        cls.cfg = Config(
            home=Path(cls.tmp.name),
            my_addresses=[ME, ME_ALIAS],
            my_names=["김도현", "도현"],
            internal_domains=["nurisoft.co.kr"],
            ignore_senders=["noreply", "no-reply", "notification@",
                            "jira@", "build@"],
            raw={"filters": {"external_allowlist": ["foundry.example"]}},
        )
        cls.store = Store(Path(cls.tmp.name) / "corpus.sqlite",
                          [ME, ME_ALIAS], ["김도현", "도현"], noise=cls.cfg)
        cls.store.ingest(cls.corpus.records)
        cls.key_of = {
            row["id"]: row["conversation_key"][len("SYNTHETIC-"):]
            for row in cls.store.db.execute(
                "SELECT id, conversation_key FROM threads")
        }

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_action_axis_gate(self):
        acts = actions.classify_threads(self.store, self.cfg)
        pred = {key: (acts[tid].level if tid in acts else ACTION_NONE)
                for tid, key in self.key_of.items()}
        result = evaluate_action_predictions(self.corpus, pred)
        self.assertEqual(result["required"]["precision"], 1.0,
                         msg=result["false_alarms"][:5])
        self.assertEqual(result["required"]["recall"], 1.0,
                         msg=result["misses"][:5])
        exact = evaluate_axis_predictions(self.corpus, pred, "action")
        self.assertEqual(exact["mismatches"], [])

    def _predict_noise(self, tid: int) -> str:
        """스레드 노이즈 축 예측 — 설정 중립 외부 판정(allowlist 무관).

        코퍼스의 policy 라벨은 '외부라는 사실'이다. allowlist 는 액션 축의
        추적 여부를 바꿀 뿐 외부성 자체를 바꾸지 않는다.
        """
        kinds = set()
        for m in self.store.db.execute(
                "SELECT sender_addr, subject FROM messages "
                "WHERE thread_id=? AND is_sent=0", (tid,)):
            if (self.cfg.is_noise_sender_hard(m["sender_addr"])
                    or self.cfg.is_noise_subject_strong(m["subject"])):
                kinds.add(NOISE_HARD)
            elif not m["sender_addr"].endswith("@nurisoft.co.kr"):
                kinds.add(NOISE_POLICY)
            else:
                kinds.add("none")
        if not kinds:
            return "none"
        if kinds == {NOISE_HARD}:
            return NOISE_HARD
        if NOISE_HARD in kinds:
            return NOISE_MIXED
        if NOISE_POLICY in kinds:
            return NOISE_POLICY
        return "none"

    def test_noise_axis_gate(self):
        pred = {key: self._predict_noise(tid)
                for tid, key in self.key_of.items()}
        result = evaluate_axis_predictions(self.corpus, pred, "noise")
        self.assertEqual(result["mismatches"], [], msg=result["mismatches"][:8])


if __name__ == "__main__":
    unittest.main()
