"""Offline regression checks for the max-tier/logging merge; all responses are fake."""
import os
import threading
import time
import unittest
from unittest.mock import patch

import runner as R
from tests import test_runtime as runtime_tests


class MergeTierTests(unittest.TestCase):
    setUp = runtime_tests.RuntimeTests.setUp
    prepare_round = runtime_tests.RuntimeTests.prepare_round

    def test_max_tier_is_optional_and_retains_teammate_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual([t.name for t in R.build_tiers()], ["fast", "deep"])
            with patch.dict(os.environ, {"MAX_MODEL": "fake-max", "MAX_REASONING": "high",
                                        "MAX_VOTES": "2", "MAX_BUDGET": "48"}):
                maximum = R.build_tiers()[-1]
                self.assertEqual((maximum.name, maximum.level, maximum.model, maximum.votes, maximum.budget),
                                 ("max", 3, "fake-max", 2, 48))
                self.assertEqual(maximum.extra, {"reasoning_effort": "high"})

    def run_ordered_round(self, first):
        self.enterContext(patch.dict(os.environ, {"FOCUS_ENABLED": "1"}))
        self.prepare_round([R.Tier("deep", 2, "fake-deep", None, 3, 50),
                            R.Tier("max", 3, "fake-max", None, 3, 52)])
        ready = threading.Event()
        old_submit = R.submit.side_effect
        def submit(*args):
            result = old_submit(*args)
            if args[-1] == first:
                ready.set()
            return result
        R.submit.side_effect = submit
        stages = []
        def assess(messages, budget, tier):
            stages.append((tier.name, tier.level))
            if (first == "focus" and tier.name == "max") or (first == "max" and tier.name == "deep"):
                if not ready.wait(2):
                    raise AssertionError("expected first stage never submitted")
            amount = {"deep": 100, "focus": 90, "max": 80}[tier.name]
            return [{1: {"covered_prob": 1, "t_if_covered": amount}}
                    for _ in range(tier.votes)], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        log_prices = self.enterContext(patch.object(R, "log_prices"))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["line_sources"][1], "max")
        self.assertEqual(rec["final_ab"][1][0], 68)
        self.assertEqual(rec["final_ab"][4], (60, 60))
        self.assertIn(("focus", 2.5), stages)
        self.assertEqual(rec["models"]["focus"], "fake-deep")
        self.assertEqual(rec["models"]["max"], "fake-max")
        self.assertEqual({c.args[1] for c in log_prices.call_args_list},
                         {"rules", "deep", "max", "focus", "final"})
        final_call = [c for c in log_prices.call_args_list if c.args[1] == "final"][0]
        self.assertEqual(final_call.args[3], rec["final_ab"])

    def test_max_after_focus_wins_when_consistency_is_equal(self):
        self.run_ordered_round("focus")

    def test_later_focus_does_not_overwrite_equally_consistent_max(self):
        self.run_ordered_round("max")

    def test_late_max_keeps_complete_deep_submission(self):
        self.prepare_round([R.Tier("deep", 2, "fake-deep", None, 3, 50),
                            R.Tier("max", 3, "fake-max", None, 3, 52)])
        sent = threading.Event()
        clock = [100.0]
        self.enterContext(patch.object(R.time, "time", side_effect=lambda: clock[0]))
        old_submit = R.submit.side_effect
        def submit(*args):
            result = old_submit(*args)
            if args[-1] == "deep":
                sent.set()
            return result
        R.submit.side_effect = submit
        def assess(messages, budget, tier):
            if tier.name == "max":
                if not sent.wait(2):
                    raise AssertionError("deep never submitted")
                clock[0] = 200.0
            amount = 100 if tier.name == "deep" else 999
            return [{1: {"covered_prob": 1, "t_if_covered": amount}}], {}
        self.enterContext(patch.object(R, "assess", side_effect=assess))
        rec = R.play_game(1, 100.0, dry=True)
        self.assertEqual([tag for tag, _ in self.sent], ["provisional", "deep"])
        self.assertEqual(rec["winner"], "deep")
        self.assertEqual(rec["final_ab"], self.sent[-1][1])
