"""Exercise module boundaries with fake case data, API and LLM responses."""
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import runner as R
from claim_to_fame import pricing


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.multiple(
            R, POOL=None, TIERS=[], ESC=None, A_MULT=0.85, SIGMA=0.35, DISAGREE=1.5,
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

    def test_failed_llm_keeps_complete_provisional_submission(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.object(R, "assess", side_effect=R.NoKeys))
        with self.assertLogs("c2f", level="ERROR"):
            rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual(rec["winner"], "rules")
        self.assertTrue(rec["ok"])
        self.assertEqual(self.sent, [("provisional", {1: (130, 130), 4: (60, 60)})])
