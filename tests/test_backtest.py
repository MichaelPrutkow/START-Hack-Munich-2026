"""Synthetic payout and replay regression tests; never call network services."""
import math
import io
import json
from pathlib import Path
import tempfile
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import calibrate as C
import eval_cases as E
import runner as R


def tx(issuer, reviewer, accepted, amount, line=1):
    return {"line_item_index": line, "issuer": issuer, "reviewer": reviewer,
            "accepted": accepted, "amount": amount}


class AnalysisTests(unittest.TestCase):
    def test_capped_acceptance_does_not_prove_charge_or_upper_bounds(self):
        d = C.analyse([tx("inflated", "yes", True, 400), tx("inflated", "no", False, 0)])[1]
        self.assertEqual(d["a_bounds"]["inflated"], [400, math.inf])
        self.assertNotIn("inflated", d["exact"])
        self.assertEqual(d["t"], [0, math.inf])
        self.assertEqual(d["b"]["no"], [0, math.inf])
        self.assertEqual(d["b"]["yes"], [400, math.inf])

    def test_own_zero_income_inflated_claim_tightens_fairness_bound(self):
        rows = [tx("us", "other", False, 0), tx("other", "us", False, 150)]
        d = C.analyse(rows, {1: [120, 50]}, "us")[1]
        self.assertEqual(d["t"], [100, 120])
        self.assertEqual(d["a_bounds"]["us"], [120, 120])
        self.assertEqual(d["b"]["other"], [0, 120])
        score = C.score_submission(d, "us", 120, 50)
        self.assertEqual((score["lower"], score["upper"]), (-150, -150))

    def test_rejection_penalty_is_exact_even_after_accepted_payment(self):
        d = C.analyse([tx("issuer", "yes", True, 100), tx("issuer", "no", False, 150)])[1]
        self.assertEqual(d["a_bounds"]["issuer"], [100, 100])
        self.assertEqual(d["t"], [100, math.inf])

    def test_cap_lower_bound_can_prove_small_payment_uncapped(self):
        rows = [tx("fair", "us", False, 150), tx("small", "us", True, 80),
                tx("small", "fair", False, 0)]
        # t >= 100 and a = 80 cannot also be unfair: contradiction must surface.
        with self.assertRaises(ValueError):
            C.analyse(rows)
        d = C.analyse(rows[:2])[1]
        self.assertEqual(d["a_bounds"]["small"], [80, 80])
        self.assertIn("small", d["exact"])

    def test_known_own_acceptance_limit_bounds_opponent_charge(self):
        rows = [tx("other", "us", True, 400), tx("other", "third", False, 0)]
        d = C.analyse(rows, {1: [0, 700]}, "us")[1]
        self.assertEqual(d["a_bounds"]["other"], [400, 700])
        self.assertEqual(d["t"], [0, 700])

    def test_original_prices_reproduce_observed_capped_payment(self):
        rows = [tx("us", "other", True, 400), tx("other", "us", False, 0)]
        d = C.analyse(rows, {1: [1000, 20]}, "us")[1]
        self.assertEqual(d["c"], [400, 400])
        s = C.score_submission(d, "us", 1000, 20)
        self.assertEqual((s["lower"], s["upper"]), (400, 400))
        changed = C.score_submission(d, "us", 800, 20)
        self.assertEqual((changed["lower"], changed["upper"]), (400, 400))

    def test_unknown_capped_charge_has_finite_penalty_bound(self):
        d = C.analyse([tx("other", "third", True, 400), tx("us", "third", False, 0)])[1]
        # The other's fair rejected charge can only be 400; any capped a > 400 is unfair.
        score = C.score_submission(d, "us", 0, 0)
        self.assertEqual((score["lower"], score["upper"]), (-600, 0))

    def test_schema_rejects_nonfinite_and_boolean_amounts(self):
        for value in (None, True, math.nan, math.inf, -1, "NaN"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                C.analyse([tx("one", "two", True, value)])
        for field, value in (("accepted", "false"), ("line_item_index", True), ("issuer", None)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                C.analyse([{**tx("one", "two", True, 10), field: value}])

    def test_contradictory_fairness_and_duplicate_rows_fail(self):
        with self.assertRaises(ValueError):
            C.analyse([tx("us", "x", False, 0), tx("x", "us", False, 150)], {1: [80, 0]}, "us")
        row = tx("one", "two", True, 10)
        with self.assertRaises(ValueError):
            C.analyse([row, row])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.d = {"t": [100, 110], "b": {"other": [0, 1]}, "a": {"other": 20},
                  "fair": {"other": True}, "c": [400, math.inf]}

    def test_uncertainty_is_an_interval_not_an_expected_profit(self):
        d = {"t": [40, 60], "b": {"other": [0, 100]}, "a": {}, "fair": {}}
        s = C.score_submission(d, "us", 50, 0)
        self.assertEqual((s["lower"], s["upper"], s["estimate"]), (0, 75, 37.5))
        self.assertFalse(s["exact"])
        self.assertIn("not expected profit", s["assumptions"])

    def test_cap_and_stated_override_are_scored_as_played(self):
        with patch.multiple(R, A_MULT=0.85, SIGMA=0.35):
            a, b = R.price(1, 100, 120, 40, 0)
        self.assertEqual((a, b), (40, 40))
        self.assertEqual(C.score_submission(self.d, "us", a, b)["estimate"], 40)
        self.assertEqual(C.backtest(self.d, "us", 100, 1, 0.85, 0.35, stated=120, cap=40), 40)
        self.assertNotEqual(C.backtest(self.d, "us", 100, 1, 0.85, 0.35), 40)

    def test_score_lines_uses_prices_instead_of_reconstructing_m(self):
        s = E.score_lines({1: {"m": 100, "p": 1, "a": 40, "b": 40}}, {1: self.d}, "us")
        self.assertEqual((s["lower"], s["upper"]), (40, 40))
        self.assertEqual(s["missing_lines"], [])

    def test_nonfinite_candidate_price_rejected(self):
        for a, b in ((math.nan, 0), (10, math.inf), (True, 5)):
            with self.assertRaises(ValueError):
                C.score_submission(self.d, "us", a, b)

    def test_chronological_split_is_deterministic(self):
        self.assertEqual([E.chronological_split(g, 4) for g in (1, 3, 4, 8)],
                         ["train", "train", "holdout", "holdout"])

    def test_counterfactual_bounds_contain_synthetic_hidden_truth(self):
        rng = random.Random(2026)
        teams = ["us", "one", "two", "three"]
        def payout(a, b, t, c):
            return min(a, c) if a <= b else 1.5 * a if a <= t else 0
        for trial in range(100):
            t = rng.choice([0, 20, 50, 100])
            c = max(4 * t, rng.choice([40, 100, 200]))
            prices = {team: (rng.choice([0, 10, 40, 80, 150, 500]), rng.choice([0, 10, 50, 200, 600]))
                      for team in teams}
            rows = [tx(iss, rev, prices[iss][0] <= prices[rev][1],
                       payout(prices[iss][0], prices[rev][1], t, c))
                    for iss in teams for rev in teams if iss != rev]
            d = C.analyse(rows, {1: prices["us"]}, "us")[1]
            for a, b in ((15, 0), (50, 60), (300, 400), prices["us"]):
                score = C.score_submission(d, "us", a, b)
                actual = sum(payout(a, prices[other][1], t, c) - payout(prices[other][0], b, t, c)
                             for other in teams if other != "us")
                with self.subTest(trial=trial, a=a, b=b):
                    self.assertLessEqual(score["lower"], actual)
                    self.assertGreaterEqual(score["upper"], actual)


class ReplayTests(unittest.TestCase):
    def test_main_persists_actual_priced_score_without_live_calls(self):
        tier = SimpleNamespace(name="deep", votes=1, budget=10, model="stub", extra={})
        votes = [{1: {"covered_prob": 1, "t_if_covered": 100, "stated_value": 120, "stated_cap": 40}}]
        rows = [tx("us", "other", True, 100), tx("other", "us", False, 150)]
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(E, "EVAL", directory))
        self.enterContext(patch("sys.argv", ["eval_cases.py", "--games", "1", "--skip-old", "--team", "us"]))
        self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.enterContext(patch.object(C, "load_game", return_value={"tx": rows}))
        self.enterContext(patch.object(E, "refresh_settings"))
        self.enterContext(patch.multiple(R, A_MULT=0.85, SIGMA=0.35))
        for name, result in {"load_env": None, "setup_logging": None, "KeyPool": object(),
                             "list_games": [(1, 0)], "fetch_key": "synthetic", "decrypt": directory,
                             "parse_items": {1: {}}, "rule_value": 20, "read_text": "",
                             "build_prompt": [], "build_tiers": [tier], "assess": (votes, {})}.items():
            self.enterContext(patch.object(R, name, return_value=result))
        E.main()
        saved = json.loads(next(directory.glob("eval_*.json")).read_text())[0]
        self.assertEqual((saved["lines"]["1"]["a"], saved["lines"]["1"]["b"]), (40, 40))
        self.assertEqual((saved["net_bounds"]["lower"], saved["net_bounds"]["upper"]), (-110, -110))
        self.assertEqual(saved["split"], "train")

    def test_shared_cap_applies_for_both_variants_and_missing_lines_keep_rules(self):
        votes = [{1: {"covered_prob": 1, "t_if_covered": 80, "shared_cap": 100},
                  2: {"covered_prob": 1, "t_if_covered": 120, "shared_cap": 100}}]
        tier = SimpleNamespace(name="deep", budget=10)
        with patch.object(R, "assess", return_value=(votes, {})), patch.multiple(R, A_MULT=0.85, SIGMA=0.35):
            out, _, _, _ = E.run(R, [], {1: {}, 2: {}, 3: {}}, {1: 10, 2: 20, 3: 30}, tier)
        self.assertEqual((out[1]["a"], out[2]["a"]), (34, 51))
        self.assertEqual((out[3]["a"], out[3]["b"], out[3]["source"]), (30, 30, "rules"))

    def test_legacy_four_field_aggregate_needs_no_shared_cap_helper(self):
        mod = SimpleNamespace(assess=lambda *a: ([{1: {}}], {}), aggregate=lambda votes: (1, 100, None, None),
                              price=lambda p,m,s,c,f: (80, 90), floor_c=R.floor_c, ceil_c=R.ceil_c)
        out, _, _, _ = E.run(mod, [], {1: {}}, {1: 10}, SimpleNamespace(name="deep", budget=10))
        self.assertEqual((out[1]["a"], out[1]["b"]), (80, 90))

    def test_invalid_votes_preserve_live_rule_fallback(self):
        with patch.object(R, "assess", return_value=([{1: {"covered_prob": math.nan}}], {})):
            out, _, _, _ = E.run(R, [], {1: {}}, {1: 50}, SimpleNamespace(name="deep", budget=10))
        self.assertEqual((out[1]["a"], out[1]["b"], out[1]["source"]), (50, 50, "rules"))

    def test_matching_tiers_use_equal_requested_votes_and_model(self):
        old = SimpleNamespace(build_tiers=lambda: [SimpleNamespace(name="deep", votes=1, model="legacy", budget=5, extra={})])
        current = SimpleNamespace(name="deep", votes=3, model="current", budget=50, extra={"reasoning_effort": "high"})
        with patch.object(R, "build_tiers", return_value=[current]):
            got = E.matching_tier(old, "deep", 2)
        self.assertEqual((got.votes, got.model, got.budget), (2, "current", 50))
        self.assertEqual(got.extra, current.extra)
