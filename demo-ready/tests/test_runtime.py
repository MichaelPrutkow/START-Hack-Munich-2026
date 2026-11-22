"""Exercise module boundaries with fake case data, API and LLM responses."""
from concurrent.futures import Future
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import runner as R
from claim_to_fame import pricing


def llm_response(*contents):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=content))
                                                for content in contents])


class AssessmentTests(unittest.TestCase):
    def test_unbundled_votes_use_only_remaining_time(self):
        tier = R.Tier("deep", 2, "fake", "high", 3, 10)
        now = [100.0]
        calls = []

        def provider(messages, n, tier, timeout):
            calls.append((n, timeout))
            if n > 1:
                now[0] += 3
                tier.n_ok = False
                raise RuntimeError("unsupported")
            return llm_response('{"items":[{"index":1}]}')

        with patch.object(R.time, "monotonic", side_effect=lambda: now[0]), patch.object(R, "llm_call", provider):
            votes, usage = R.assess([], 10, tier)
        self.assertEqual(calls, [(3, 10), (1, 7), (1, 7), (1, 7)])
        self.assertEqual(len(votes), 3)
        self.assertEqual(usage["calls"], 3)

    def test_deadline_and_request_timeout_are_distinct(self):
        tier = R.Tier("deep", 2, "fake", "high", 1, 10)
        pending = Future()
        with patch.object(R, "ThreadPoolExecutor", return_value=Mock(submit=Mock(return_value=pending))), \
                patch.object(R, "wait", return_value=(set(), {pending})):
            with self.assertRaises(R.AssessmentError) as deadline:
                R.assess([], 10, tier)
        self.assertEqual(deadline.exception.details, {
            "reason": "deadline", "attempted": 1, "pending": 1, "failures": [],
        })
        with patch.object(R, "llm_call", side_effect=TimeoutError("private contents")):
            with self.assertRaises(R.AssessmentError) as request:
                R.assess([], 10, tier)
        self.assertEqual(request.exception.details, {
            "reason": "request_timeout", "attempted": 1, "pending": 0, "failures": ["TimeoutError"],
        })

    def test_bad_choices_do_not_discard_other_votes(self):
        tier = R.Tier("deep", 2, "fake", "high", 3, 10)
        response = llm_response(None, '{"items":[{"index":1}]}', "not JSON")
        with patch.object(R, "llm_call", return_value=response):
            votes, usage = R.assess([], 10, tier)
        self.assertEqual(votes, [{1: {"index": 1}}])
        self.assertEqual(usage["calls"], 1)

    def test_http_failure_records_status_and_never_body(self):
        class ProviderError(RuntimeError):
            status_code = 503

        tier = R.Tier("deep", 2, "fake", "high", 1, 10)
        with patch.object(R, "llm_call", side_effect=ProviderError("private contents")):
            with self.assertRaises(R.AssessmentError) as failure:
                R.assess([], 10, tier)
        self.assertEqual(str(failure.exception), "request_error")
        self.assertEqual(failure.exception.details["failures"], ["ProviderError/HTTP503"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FOCUS_ENABLED": "0", "PRICING_UNCERTAINTY": "0", "CONTROL_SHADOW": "0"}))
        self.enterContext(patch.multiple(
            R, POOL=None, TIERS=[], A_MULT=0.85, SIGMA=0.35, DISAGREE=1.5,
        ))
        # Fail immediately if a test accidentally reaches a live endpoint.
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")))

    def test_main_applies_env_settings_to_extracted_pricing(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("sys.argv", ["runner.py", "--check"]))
        self.enterContext(patch.object(R, "load_env", side_effect=lambda: os.environ.update(
            A_MULT="0.72", SIGMA="0.44", DISAGREE="99", FAST_VOTES="2", DEEP_MODEL="off",
        )))
        self.enterContext(patch.object(R, "setup_logging"))
        check = self.enterContext(patch.object(R, "check"))
        R.main()
        check.assert_called_once_with()
        self.assertEqual(R.price(1, 100, None, None, 50), pricing.price(
            1, 100, None, None, 50, a_mult=0.72, sigma=0.44,
        ))
        votes = [{"covered_prob": 1, "t_if_covered": m} for m in (100, 300, 500)]
        self.assertAlmostEqual(R.aggregate(votes)[1], 300)
        self.assertEqual([(t.name, t.votes) for t in R.TIERS], [("fast", 2)])

    def prepare_round(self, tiers):
        R.POOL = SimpleNamespace(new_round=Mock(), current=lambda: "fake")
        R.TIERS = tiers
        self.items = {
            1: {"description": "Synthetic repair", "quantity": 2, "unit": "hrs"},
            4: {"description": "Synthetic part", "quantity": 1, "unit": "pcs"},
        }
        self.enterContext(patch.object(R, "fetch_key", return_value="fake-key"))
        self.enterContext(patch.object(R, "decrypt", return_value=Path("unused")))
        self.enterContext(patch.object(R, "parse_items", return_value=self.items))
        self.enterContext(patch.object(R, "read_text", return_value="Synthetic document"))
        self.records = []
        self.enterContext(patch.object(R, "jlog", side_effect=lambda name, rec: self.records.append((name, rec))))
        self.sent = []

        def submit(gid, ab, deadline, dry, tag):
            self.assertTrue(dry)
            self.assertEqual(set(ab), {1, 4})
            self.sent.append((tag, ab.copy()))
            return True

        self.enterContext(patch.object(R, "submit", side_effect=submit))

    def test_partial_llm_vote_keeps_missing_lines_on_fallback(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        votes = [{1: {"covered_prob": 1, "t_if_covered": 100}}]
        self.enterContext(patch.object(R, "assess", return_value=(votes, {})))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "fast")
        self.assertEqual([tag for tag, _ in self.sent], ["provisional", "fast"])
        final = self.sent[-1][1]
        self.assertEqual(final[1], R.price(1, 100, None, None, 130))
        self.assertEqual(final[4], (60, 60))
        self.assertEqual(self.records[-1], ("rounds.jsonl", rec))

    def test_slower_lower_tier_does_not_overwrite_deep(self):
        self.prepare_round([
            R.Tier("fast", 1, "fake", None, 1, 20),
            R.Tier("deep", 2, "fake", None, 1, 50),
        ])
        deep_sent = threading.Event()
        original_submit = R.submit.side_effect

        def submit(*args):
            result = original_submit(*args)
            if args[-1] == "deep":
                deep_sent.set()
            return result

        R.submit.side_effect = submit

        def assess(messages, timeout, tier):
            if tier.name == "fast":
                if not deep_sent.wait(2):
                    raise AssertionError("deep tier never submitted")
            m = 100 if tier.name == "fast" else 200
            return [{1: {"covered_prob": 1, "t_if_covered": m}}], {}

        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "deep")
        self.assertEqual(set(rec["tiers"]), {"rules", "fast", "deep"})
        self.assertEqual([tag for tag, _ in self.sent], ["provisional", "deep"])


    def test_validated_price_correction_preserves_raw_estimate_and_submission_provenance(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.object(R, "assess", return_value=([{1: {"covered_prob": 1, "t_if_covered": 100}}], {})))
        self.enterContext(patch.object(R.price_research, "adjust", return_value=(120, {"applied": True})))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(self.sent[-1][1][1], R.price(1, 120, None, None, 130))
        detail = rec['est']['fast'][1]
        self.assertEqual((detail['m_raw'], detail['m_before_price_learning'], detail['m']), (100, 100, 120))


    def test_failed_llm_keeps_complete_provisional_submission(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.object(R, "assess", side_effect=R.NoKeys))
        with self.assertLogs("c2f", level="ERROR"):
            rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "rules")
        self.assertTrue(rec["ok"])
        self.assertEqual(self.sent, [("provisional", {1: (130, 130), 4: (60, 60)})])

    def test_failed_deep_keeps_fast_and_records_reason(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20), R.Tier("deep", 2, "fake", None, 1, 50)])

        def assess(messages, timeout, tier):
            if tier.name == "deep":
                raise R.AssessmentError("deadline", attempted=1, pending=1, failures=[])
            return [{1: {"covered_prob": 1, "t_if_covered": 100}}], {}

        with patch.object(R, "assess", assess), self.assertLogs("c2f", level="ERROR") as logs:
            rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "fast")
        self.assertEqual(rec["confirmed"], ["provisional", "fast"])
        self.assertEqual(rec["llm_failures"]["deep"]["reason"], "deadline")
        self.assertLessEqual(rec["llm_failures"]["deep"]["budget_seconds"], 50)
        self.assertIn("deep failed", logs.output[0])

    def test_disputed_deep_line_keeps_consistent_fast_and_records_real_sources(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 3, 20),
                            R.Tier("deep", 2, "fake", None, 3, 50)])
        fast_sent = threading.Event()
        original = R.submit.side_effect
        def submit(*args):
            result = original(*args)
            if args[-1] == "fast":
                fast_sent.set()
            return result
        R.submit.side_effect = submit
        def assess(messages, timeout, tier):
            if tier.name == "deep":
                self.assertTrue(fast_sent.wait(2))
            amounts = [100, 100, 100] if tier.name == "fast" else [80, 200, 700]
            return [{1: {"covered_prob": 1, "t_if_covered": m},
                     4: {"covered_prob": 1, "t_if_covered": 50 if tier.name == "fast" else 60}}
                    for m in amounts], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["line_sources"], {1: "fast", 4: "deep"})
        self.assertEqual(rec["final_ab"], self.sent[-1][1])
        self.assertEqual(rec["final_ab"][1][0], 85)
        self.assertEqual(rec["final_ab"][4][0], 51)
        self.assertGreater(rec["est"]["deep"][1]["log_spread"], 0)
        self.assertTrue(rec["dry"])
        self.assertEqual(rec["campaign"], "2026-11")
        # Reconstructable afterwards: code, confirmed stages, shadow variants that were not sent
        self.assertIn("commit", rec)
        self.assertEqual(rec["confirmed"], ["provisional", "fast", "deep"])
        self.assertEqual(rec["shadow"]["a80_all"][1][0], 68.0)
        self.assertEqual(len(self.sent), 3)

    def test_all_invalid_votes_keep_fallback(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.object(R, "assess", return_value=(
            [{1: {"covered_prob": float("nan"), "t_if_covered": 100}}], {})))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "rules")
        self.assertEqual(rec["line_sources"], {1: "rules", 4: "rules"})
        self.assertEqual(rec["final_ab"], self.sent[0][1])

    def test_focus_is_bounded_and_keeps_complete_submission(self):
        self.enterContext(patch.dict(os.environ, {"FOCUS_ENABLED": "1", "FOCUS_MAX_LINES": "1"}))
        self.prepare_round([R.Tier("deep", 2, "fake", None, 3, 50)])
        calls = []
        def assess(messages, timeout, tier):
            calls.append((tier.name, timeout, messages))
            if tier.name == "deep":
                return [{1: {"covered_prob": 1, "t_if_covered": 500},
                         4: {"covered_prob": 1, "t_if_covered": 60}} for _ in range(3)], {}
            return [{1: {"covered_prob": 1, "t_if_covered": 400}} for _ in range(2)], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual([c[0] for c in calls], ["deep", "focus"])
        self.assertLessEqual(calls[1][1], 12)
        self.assertIn("1 | Synthetic repair", calls[1][2][1]["content"])
        self.assertNotIn("4 | Synthetic part", calls[1][2][1]["content"])
        self.assertEqual(rec["line_sources"], {1: "focus", 4: "deep"})
        self.assertEqual(rec["final_ab"][4], rec["tiers"]["deep"][4])

    def test_late_llm_result_cannot_replace_submitted_fallback(self):
        self.prepare_round([R.Tier("deep", 2, "fake", None, 3, 50)])
        clock = [100.0]
        self.enterContext(patch.object(R.time, "time", side_effect=lambda: clock[0]))
        def assess(*args):
            clock[0] = 200.0
            return [{1: {"covered_prob": 1, "t_if_covered": 999}}], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, 100.0, dry=True)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(rec["winner"], "rules")
        self.assertEqual(rec["final_ab"], self.sent[0][1])

    def test_shared_groups_cannot_be_partially_updated_through_overlap(self):
        old = {1: {"shared": 100}, 4: {"shared": 100}, 8: {"shared": None}}
        new = {1: {"shared": None}, 4: {"shared": 200}, 8: {"shared": 200}}
        self.assertEqual(R.atomic_selection([1, 4], old, new), set())
        self.assertEqual(R.atomic_selection([1, 4, 8], old, new), {1, 4, 8})
        self.assertEqual(R.atomic_selection([1], {1: {"shared": 0}, 4: {"shared": 0}}), set())

    def test_validated_learning_only_applies_to_eligible_pricing(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 3, 20)])
        state = {"schema_version": R.feedback.SCHEMA_VERSION, "max_game": 3,
                 "created_at": "2026-11-21T00:00:00+00:00"}
        self.enterContext(patch.object(R.feedback, "load_state", return_value=state))
        self.enterContext(patch.object(R, "strategy_identity", return_value=R.strategy_identity()))
        self.enterContext(patch.object(R.feedback, "context_for_items", return_value="past range evidence"))
        self.enterContext(patch.object(R.feedback, "calibration_for", return_value={
            "bias": 0.8, "sigma": 0.4, "a_mult": 0.7, "min_covered_prob": 0.9}))
        self.enterContext(patch.object(R, "assess", return_value=([
            {1: {"covered_prob": 1, "t_if_covered": 100},
             4: {"covered_prob": 1, "t_if_covered": 100, "stated_value": 50}}
            for _ in range(3)], {})))
        rec = R.play_game(4, time.time(), dry=True)
        self.assertEqual(rec["final_ab"][1][0], 56)
        self.assertEqual(rec["final_ab"][4], (50, 50))
        self.assertTrue(rec["est"]["fast"][1]["calibration_applied"])
        self.assertEqual(rec["est"]["fast"][1]["a_mult"], 0.7)
        self.assertEqual(rec["est"]["fast"][1]["m"], 100)
        self.assertEqual(rec["a_mult"], 0.85)
        self.assertFalse(rec["est"]["fast"][4]["calibration_applied"])
        self.assertEqual(rec["learning_max_game"], 3)
        self.assertEqual(rec["learning_version"], R.feedback.SCHEMA_VERSION)

    def test_experimental_uncertainty_does_not_apply_incompatible_calibration(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 3, 20)])
        self.enterContext(patch.dict(os.environ, {"PRICING_UNCERTAINTY": "1"}))
        self.enterContext(patch.object(R.feedback, "calibration_for", return_value={
            "bias": 0.8, "sigma": 0.4, "a_mult": 0.7, "min_covered_prob": 0.9}))
        self.enterContext(patch.object(R, "assess", return_value=([
            {i: {"covered_prob": 1, "t_if_covered": 100} for i in (1, 4)}
            for _ in range(3)], {})))
        rec = R.play_game(4, time.time(), dry=True)
        for detail in rec["est"]["fast"].values():
            self.assertFalse(detail["calibration_applied"])
            self.assertTrue(detail["uncertainty_applied"])
            self.assertEqual((detail["a_mult"], detail["bias"], detail["sigma"]), (0.85, 1.0, 0.35))

    def test_manual_pin_disables_otherwise_validated_parameters(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 3, 20)])
        self.enterContext(patch.object(R, "AUTO_TUNE", False))
        self.enterContext(patch.object(R.feedback, "calibration_for", return_value={
            "bias": 0.8, "sigma": 1.0, "a_mult": 0.7, "min_covered_prob": 0.9}))
        self.enterContext(patch.object(R, "assess", return_value=([
            {i: {"covered_prob": 1, "t_if_covered": 100} for i in (1, 4)}
            for _ in range(3)], {})))
        rec = R.play_game(4, time.time(), dry=True)
        self.assertEqual(rec["final_ab"][1][0], 85)
        self.assertFalse(rec["est"]["fast"][1]["calibration_applied"])
