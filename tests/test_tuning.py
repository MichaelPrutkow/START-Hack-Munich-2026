"""Leaderboard tuning (claim_to_fame/tuning.py, tune.py) and the runner's settings refresh.
Synthetic transactions only; no network."""
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tune
import runner as R
from claim_to_fame import tuning as LR


def tx(iss, rev, line, acc, amt):
    return {"issuer": iss, "reviewer": rev, "line_item_index": line, "accepted": acc, "amount": amt}


def example_game():
    """Official example: t = 100. Alpha a 100 b 130, Beta a 150 b 90, Delta a 100 b 110."""
    return [tx("Alpha", "Beta", 1, False, 150.0), tx("Alpha", "Delta", 1, True, 100.0),
            tx("Delta", "Alpha", 1, True, 100.0), tx("Delta", "Beta", 1, False, 150.0),
            tx("Beta", "Alpha", 1, False, 0.0), tx("Beta", "Delta", 1, False, 0.0)]


class AnalyseTests(unittest.TestCase):
    def test_brackets_from_official_example(self):
        d = LR.analyse(example_game())[1]
        self.assertEqual(d["a"], {"Alpha": 100.0, "Delta": 100.0})       # Beta's a unknown (rejected everywhere)
        self.assertEqual(d["fair"], {"Alpha": True, "Delta": True, "Beta": False})
        self.assertEqual(d["t"], [100.0, math.inf])
        self.assertEqual(d["b"]["Beta"], [0.0, 100.0])                   # rejected Alpha's 100
        self.assertEqual(d["b"]["Delta"], [100.0, math.inf])

    def test_own_a_closes_the_bracket(self):
        d = LR.analyse(example_game(), {1: 150.0}, "Beta")[1]
        self.assertEqual(d["t"], [100.0, 150.0])
        self.assertEqual(d["b"]["Alpha"], [100.0, 150.0])

    def test_line_net_matches_official_payoffs(self):
        d = LR.analyse(example_game(), {1: 150.0}, "Beta")[1]
        self.assertAlmostEqual(LR.line_net(d, "Beta", 150.0, 90.0), -300.0)   # table: Beta -300
        self.assertAlmostEqual(LR.line_net(d, "Beta", 100.0, 130.0), 100 + 100 - 100 - 100)


class ChooseTests(unittest.TestCase):
    def rows(self, n_games=9, lines_per_game=2):
        """Every line: t = 60 while our m = 100, five reviewers with b = 80, five claims at 70."""
        out = []
        for g in range(1, n_games + 1):
            for i in range(1, lines_per_game + 1):
                d = {"a": {f"T{k}": 70.0 for k in range(5)}, "fair": {f"T{k}": False for k in range(5)},
                     "b": {f"T{k}": [80.0, 95.0] for k in range(5)}, "t": [55.0, 70.0]}
                out.append({"g": g, "i": i, "d": d, "team": "us", "m": 100.0, "p": 0.99,
                            "stated": None, "cap": None})
        return out

    def test_keeps_with_too_few_lines(self):
        new, rep = LR.choose(self.rows(2, 1), {"a_mult": 0.85, "sigma": 0.35})
        self.assertEqual(new, {"a_mult": 0.85, "sigma": 0.35})
        self.assertIn("keep", rep["decision"])

    def test_moves_one_step_toward_better_setting(self):
        new, rep = LR.choose(self.rows(), {"a_mult": 0.85, "sigma": 0.35})
        self.assertIn("change", rep["decision"])
        self.assertLessEqual(abs(new["a_mult"] - 0.85), LR.STEP["a_mult"] + 1e-9)
        self.assertLessEqual(abs(new["sigma"] - 0.35), LR.STEP["sigma"] + 1e-9)
        self.assertGreater(new["sigma"], 0.35)          # accepting 70 at t = 60 loses: b must drop

    def test_keeps_when_gain_is_small(self):
        new, rep = LR.choose(self.rows(), {"a_mult": 0.85, "sigma": 0.35}, min_gain=1e9)
        self.assertEqual(new, {"a_mult": 0.85, "sigma": 0.35})

    def test_own_lines_reads_estimates_and_old_records(self):
        rec = {"winner": "deep", "a_mult": 0.85, "tiers": {"deep": {"1": [85.0, 86.0], "2": [40.0, 0.0]}},
               "est": {"deep": {"1": {"p": 0.99, "m": 100.0, "stated": None, "cap": None}}}}
        own = LR.own_lines(rec)
        self.assertEqual(own[1]["m"], 100.0)
        self.assertAlmostEqual(own[2]["m"], 40.0 / 0.85)
        self.assertEqual(own[2]["p"], 0.5)
        self.assertEqual(LR.own_lines({"winner": "rules"}), {})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / ".env"
        self.params = self.tmp / "params.json"
        self.enterContext(patch.multiple(R, A_MULT=0.85, SIGMA=0.35, DISAGREE=1.5, SETTINGS_SOURCE="env", AUTO_TUNE=True))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(R, "ENV_PATH", self.env))
        self.enterContext(patch.object(tune, "PARAMS", self.params))

    def test_env_change_applies_without_restart(self):
        self.env.write_text("A_MULT=0.8\nSIGMA=1.0\nAUTO_TUNE=0\n")
        R.refresh_settings()
        self.assertEqual((R.A_MULT, R.SIGMA, R.SETTINGS_SOURCE), (0.8, 1.0, "env"))

    def test_exploratory_params_cannot_override_manual_defaults(self):
        self.env.write_text("A_MULT=0.8\nSIGMA=1.0\n")
        self.params.write_text(json.dumps({"a_mult": 0.75, "sigma": 0.65, "updated": "now"}))
        R.refresh_settings()
        self.assertEqual((R.A_MULT, R.SIGMA), (0.8, 1.0))
        self.assertEqual(R.SETTINGS_SOURCE, "env")
        self.assertTrue(R.AUTO_TUNE)  # Enables independently validated per-line feedback only.

    def test_invalid_or_unvalidated_params_file_is_ignored(self):
        self.params.write_text(json.dumps({"a_mult": 5, "sigma": 0.01}))
        R.refresh_settings()
        self.assertEqual((R.A_MULT, R.SIGMA), (0.85, 0.35))
        self.params.write_text("{not json")
        self.env.write_text("SIGMA=0.5\n")
        R.refresh_settings()
        self.assertEqual(R.SIGMA, 0.5)

    def test_bad_env_value_keeps_old_settings(self):
        self.env.write_text("SIGMA=abc\nAUTO_TUNE=0\n")
        with self.assertLogs("c2f", level="WARNING"):
            R.refresh_settings()
        self.assertEqual((R.A_MULT, R.SIGMA), (0.85, 0.35))

    def test_cache_only_analysis_does_not_refetch_corrupt_cache(self):
        (self.tmp / "game_001.json").write_text("{broken")
        with patch.object(tune.C, "CACHE", self.tmp), \
             patch.object(tune.C, "load_game", side_effect=AssertionError("network forbidden")):
            self.assertEqual(tune.collect("us", fetch=False), {})


if __name__ == "__main__":
    unittest.main()
