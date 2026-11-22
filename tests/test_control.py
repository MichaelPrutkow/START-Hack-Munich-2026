import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import runner as R
from claim_to_fame.control import assess_control
from claim_to_fame.decision_review import review_line, summarize
from tests import test_runtime


class ControlTests(unittest.TestCase):
    def test_control_sees_documents_but_no_previous_prices_or_history(self):
        def assess(messages, budget, tier):
            content = str(messages)
            self.assertIn("Original damage", content)
            self.assertNotIn("HISTORICAL RANGE EVIDENCE", content)
            self.assertNotIn("PRIOR ESTIMATES", content)
            self.assertNotIn("12345", content)
            self.assertLessEqual(budget, 8)
            return [{1: {"covered_prob": 1, "t_if_covered": 100}}], {"calls": 1}
        result = assess_control("Policy", "Original damage", {1: {"description": "repair", "quantity": 1, "unit": "pcs"}},
                                {1: {"m": 12345}}, assess=assess,
                                tier=SimpleNamespace(model="fake"), budget=8, a_mult=.75, sigma=.35, disagree=1.5)
        self.assertEqual(result["ab"][1][0], 75)
        self.assertFalse(result["prior_estimates"])

    def test_control_does_not_price_partial_shared_caps(self):
        result = assess_control("P", "D", {1: {"description": "repair", "quantity": 1, "unit": "pcs"}},
                                {1: {"m": 100}}, assess=lambda *args: ([{1: {
                                    "covered_prob": 1, "t_if_covered": 100, "shared_cap": 500}}], {}),
                                tier=SimpleNamespace(model="fake"), budget=8, a_mult=.75, sigma=.35, disagree=1.5)
        self.assertEqual(result["status"], "no_usable_estimates")
        self.assertEqual(result["ab"], {})


class ControlRuntimeTests(test_runtime.RuntimeTests):
    def prepare_control(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.dict(os.environ, {"CONTROL_SHADOW": "1"}))

    def test_control_can_never_publish_or_replace_live_estimates(self):
        self.prepare_control()
        calls = []
        def assess(messages, budget, tier):
            calls.append(tier.name)
            if tier.name == "control":
                self.assertEqual(self.sent[-1][0], "fast")
                self.assertLessEqual(budget, 8)
            return [{i: {"covered_prob": 1, "t_if_covered": 999 if tier.name == "control" else 100}
                     for i in (1, 4)}], {"calls": 1}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(calls, ["fast", "control"])
        self.assertEqual(rec["confirmed"], ["provisional", "fast"])
        self.assertEqual(rec["final_ab"], self.sent[-1][1])
        self.assertEqual(rec["final_ab"][1][0], 85)
        self.assertEqual(rec["control_shadow"]["status"], "completed")
        self.assertNotIn("control", rec["est"])

    def test_control_failure_keeps_confirmed_submission(self):
        self.prepare_control()
        self.enterContext(patch.object(R, "assess", return_value=([{1: {"covered_prob": 1, "t_if_covered": 100}}], {})))
        self.enterContext(patch.object(R, "assess_control", side_effect=RuntimeError("private error")))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["control_shadow"]["status"], "failed")
        self.assertEqual(rec["control_shadow"]["failure"], "RuntimeError")
        self.assertEqual(rec["final_ab"], self.sent[-1][1])

    def test_no_control_without_confirmed_model_submission(self):
        self.prepare_control()
        self.enterContext(patch.object(R, "assess", side_effect=RuntimeError("failed")))
        call = self.enterContext(patch.object(R, "assess_control"))
        rec = R.play_game(1, time.time(), dry=True)
        call.assert_not_called()
        self.assertEqual(rec["winner"], "rules")

    def test_no_control_when_production_uses_budget(self):
        self.prepare_control()
        now = [1000.0]
        self.enterContext(patch.object(R.time, "time", side_effect=lambda: now[0]))
        def assess(*args):
            now[0] += 52
            return [{1: {"covered_prob": 1, "t_if_covered": 100}}], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        call = self.enterContext(patch.object(R, "assess_control"))
        R.play_game(1, now[0], dry=True)
        call.assert_not_called()


for _name in [n for n in vars(test_runtime.RuntimeTests) if n.startswith("test_")]:
    if _name not in vars(ControlRuntimeTests):
        setattr(ControlRuntimeTests, _name, None)


class DecisionReviewTests(unittest.TestCase):
    def test_role_separation_and_recall_effect_use_actual_cashflows(self):
        import calibrate as C
        tx = [dict(line_item_index=1, issuer="us", reviewer="other", accepted=True, amount=80),
              dict(line_item_index=1, issuer="other", reviewer="us", accepted=False, amount=150)]
        inference = C.analyse(tx, {1: [80, 50]}, "us")[1]
        result = review_line(inference, tx, "us", [80, 50],
                             {"m": 110, "p": 1, "recall_b_applied": True, "b_before_recall": 100},
                             {"status": "completed", "ab": {1: [80, 100]}}, 1)
        variants = result["variants"]
        self.assertEqual(variants["independent_control_a"], {"lower": 0, "upper": 0})
        self.assertEqual(variants["independent_control_b"], {"lower": 50, "upper": 50})
        self.assertEqual(variants["without_recall_both"], variants["independent_control_b"])

    def test_no_price_error_claim_without_positive_coverage_evidence(self):
        result = review_line({"t": [0, 5]}, [], "us", [10, 0], {"m": 100, "p": 0}, {}, 1)
        self.assertEqual(result["estimate_position"], "no_positive_fair_evidence")

    def test_without_best_game_is_per_game_not_per_line(self):
        reports = [{"game": g, "lines": [{"decision_review": {"variants": {"control_a": {
            "lower": value, "upper": value+2}}}}]} for g, value in [(1, 100), (2, -5)]]
        result = summarize(reports)["control_a"]
        self.assertEqual(result["lower"], 95)
        self.assertEqual(result["without_best_game_lower"], -5)
