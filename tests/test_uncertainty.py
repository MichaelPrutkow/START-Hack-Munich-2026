"""Offline tests for vote uncertainty, document anchors and focused verification."""
import math
import unittest

from claim_to_fame import pricing, prompts


def vote(amount, probability=1.0, **kwargs):
    return {"covered_prob": probability, "t_if_covered": amount, **kwargs}


class VoteUncertaintyTests(unittest.TestCase):
    def test_high_outlier_does_not_raise_consensus_median(self):
        estimate = pricing.estimate_votes([vote(100), vote(100), vote(10000)])
        self.assertEqual(estimate["m"], 100)
        self.assertGreater(estimate["log_spread"], 0)
        self.assertTrue(estimate["disputed"])
        self.assertIn("amount_disagreement", estimate["reason"])
        self.assertEqual(pricing.aggregate([vote(100), vote(100), vote(10000)], disagree=1.5),
                         (1.0, 100, None, None, None))

    def test_two_vote_median_is_geometric_and_spread_is_bounded(self):
        estimate = pricing.estimate_votes([vote(1e-300), vote(1e300)])
        self.assertAlmostEqual(estimate["m"], 1.0)
        self.assertLessEqual(estimate["log_spread"], math.log(4))
        self.assertTrue(all(math.isfinite(estimate[k]) for k in ("m", "log_spread", "log_range")))

    def test_coverage_dispersion_is_preserved_separately(self):
        estimate = pricing.estimate_votes([vote(100, 0), vote(100, 0.5), vote(100, 1)])
        self.assertEqual(estimate["m"], 100)
        self.assertEqual(estimate["p"], 0.5)
        self.assertEqual(estimate["log_spread"], 0)
        self.assertGreater(estimate["p_spread"], 0.4)
        self.assertTrue(estimate["disputed"])

    def test_invalid_votes_are_rejected_instead_of_clamped(self):
        malformed = [vote(math.nan), vote(math.inf), vote(-1), vote(0), vote(100, math.nan),
                     vote(100, math.inf), vote(100, 2), vote(100, -0.1), vote(True), None, {}]
        estimate = pricing.estimate_votes([vote(120, 0.9), *malformed])
        self.assertEqual(estimate["valid_count"], 1)
        self.assertEqual(estimate["invalid_count"], len(malformed))
        self.assertEqual(estimate["m"], 120)
        self.assertEqual(estimate["p"], 0.9)
        empty = pricing.estimate_votes(malformed)
        self.assertEqual(empty["valid_count"], 0)
        self.assertEqual(empty["m"], 0)
        self.assertIn("no_valid_votes", empty["reason"])

    def test_document_anchors_need_matching_majority(self):
        estimates = [vote(100, stated_value=x, clause="2.3 Damage") for x in (100, 200, 300)]
        estimate = pricing.estimate_votes(estimates)
        self.assertIsNone(estimate["stated"])
        self.assertTrue(estimate["anchor_validity"]["stated"]["conflicting"])
        self.assertEqual(estimate["clauses"], ["2.3 Damage"])
        estimates[1]["stated_value"] = 100
        estimate = pricing.estimate_votes(estimates)
        self.assertEqual(estimate["stated"], 100)
        self.assertTrue(estimate["anchor_validity"]["stated"]["trusted"])
        self.assertEqual(estimate["anchor_validity"]["stated"]["support"], 2)

    def test_invalid_document_anchor_is_not_a_price(self):
        estimate = pricing.estimate_votes([vote(100, stated_value=math.inf, stated_cap=-10)])
        self.assertIsNone(estimate["stated"])
        self.assertIsNone(estimate["cap"])
        self.assertEqual(estimate["anchor_validity"]["stated"]["invalid"], 1)

    def test_shared_cap_scales_the_already_line_capped_amounts(self):
        # 400/100 is the external review's case (old path sent 100 + 42.50 against a limit of 100)
        for raw, stated in ((400, None), (400, 400), (1000, None), (1000, 1000)):
            adjusted = pricing.apply_shared_caps({1: (1, raw, stated, 100, 100),
                                                  2: (1, 100, None, None, 100)})
            for a_mult in (0.85, 2):
                amounts = [pricing.price(*agg, 50, a_mult=a_mult, sigma=0.35)
                           for agg in adjusted.values()]
                self.assertLessEqual(sum(a for a, _ in amounts), 100)
                self.assertLessEqual(sum(b for _, b in amounts), 100)
            self.assertEqual(adjusted[1][1], 50)

    def test_shared_budget_survives_rounding_large_multiplier_and_zero_limit(self):
        for shared in (0, 100, 1000):
            adjusted = pricing.apply_shared_caps({i: (1, 100, None, None, shared) for i in (1, 2, 3)})
            amounts = [pricing.price(*agg, 50, a_mult=100, sigma=0) for agg in adjusted.values()]
            self.assertLessEqual(sum(a for a, _ in amounts), shared)
            self.assertLessEqual(sum(b for _, b in amounts), shared)


class UncertaintyPriceTests(unittest.TestCase):
    def price(self, **kwargs):
        args = {"p": 1, "m": 100, "stated": None, "cap": None, "fallback": 50,
                "a_mult": 0.85, "sigma": 0.35}
        args.update(kwargs)
        return pricing.price(**args)

    def test_zero_spread_preserves_baseline_and_opt_out(self):
        baseline = self.price()
        self.assertEqual(baseline[0], 85)
        self.assertTrue(85 < baseline[1] < 87)
        self.assertEqual(self.price(uncertainty=0, bias=1), baseline)
        self.assertEqual(self.price(uncertainty=1, uncertainty_aware=False), baseline)

    def test_same_median_different_spread_changes_decisions_separately(self):
        tight = pricing.estimate_votes([vote(100), vote(100), vote(100)])
        wide = pricing.estimate_votes([vote(50), vote(100), vote(200)])
        self.assertEqual(tight["m"], wide["m"])
        a, b = self.price(uncertainty=tight["log_spread"])
        uncertain_a, uncertain_b = self.price(uncertainty=wide["log_spread"])
        self.assertLess(uncertain_a, a)
        self.assertLess(uncertain_b, b)
        self.assertNotAlmostEqual(uncertain_a / a, uncertain_b / b, places=3)
        self.assertEqual(self.price(uncertainty=1e100), self.price(uncertainty=math.log(4)))

    def test_stated_amounts_and_caps_override_price_adjustments(self):
        self.assertEqual(self.price(stated=120, bias=0.5, uncertainty=1), (120, 120))
        self.assertEqual(self.price(stated=120, cap=90, bias=2, uncertainty=1), (90, 90))
        self.assertEqual(self.price(stated=120, p=0.6, uncertainty=1), (120, 0))
        self.assertEqual(self.price(cap=40), (40, 40))
        self.assertEqual(self.price(stated=0), (0, 0))
        self.assertEqual(self.price(cap=0), (0, 0))

    def test_bias_is_multiplicative_and_coverage_threshold_still_applies(self):
        self.assertEqual(self.price(bias=0.8)[0], 68)
        self.assertEqual(self.price(p=2 / 3, uncertainty=0.5)[1], 0)
        self.assertEqual(self.price(m=0, p=0.2)[0], 42.5)

    def test_invalid_price_inputs_fail_before_producing_submission_values(self):
        for field in ("p", "m", "stated", "cap", "fallback", "a_mult", "sigma", "uncertainty", "bias"):
            for invalid in (math.nan, math.inf, -1, True, "invalid"):
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    self.price(**{field: invalid})
        with self.assertRaises(ValueError):
            self.price(p=1.1)
        with self.assertRaises(ValueError):
            self.price(m=1e308, bias=10)

    def test_extreme_finite_amounts_and_rounding_remain_finite(self):
        for value in (1e-300, 1e-9, 1e100, 1e308):
            self.assertTrue(all(math.isfinite(x) and x >= 0 for x in self.price(m=value)))
        for value in (None, math.nan, math.inf, "bad", True):
            self.assertEqual(pricing.floor_c(value), 0)
            self.assertEqual(pricing.ceil_c(value), 0)


class FocusPromptTests(unittest.TestCase):
    def setUp(self):
        self.items = {1: {"description": "Large repair", "quantity": 2, "unit": "hrs"},
                      4: {"description": "Disputed material", "quantity": 3, "unit": "m2"},
                      9: {"description": "Small repair", "quantity": 1, "unit": "pcs"}}
        self.estimates = {1: {"m": 1000, "log_spread": 0},
                          4: {"m": 400, "log_spread": 1, "disputed": True},
                          9: {"m": 600, "log_spread": 0}}

    def test_selects_largest_and_high_monetary_disagreement(self):
        selected = pricing.select_focus_items(self.items, self.estimates, limit=2)
        self.assertEqual(list(selected), [1, 4])
        self.assertEqual(pricing.select_focus_items(self.items, self.estimates, limit=0), {})
        self.assertEqual(len(pricing.select_focus_items(self.items, self.estimates, limit=10)), 3)

    def test_focus_prompt_requires_exact_subset_with_original_context(self):
        subset = {i: self.items[i] for i in (1, 4)}
        messages = prompts.build_focus_prompt("Original policy clause", "Original damage context", subset,
                                             self.estimates, historical_context="Comparable range EUR 80–120")
        content = messages[1]["content"]
        self.assertEqual(messages[0]["content"], prompts.SYSTEM)
        for text in ("Original policy clause", "Original damage context", "1 | Large repair | 2 | hrs",
                     "4 | Disputed material | 3 | m2", "exactly one complete schema entry", "1, 4.",
                     "untrusted hypotheses", "gross EUR", "shared caps", "scope of every stated document amount",
                     "Comparable range EUR 80–120"):
            self.assertIn(text, content)
        self.assertNotIn("9 |", content)
        self.assertNotIn('"9":', content)

    def test_legacy_prompt_call_and_optional_history(self):
        baseline = prompts.build_prompt("Policy", "Damage", self.items)
        self.assertEqual(len(baseline), 2)
        self.assertNotIn("## HISTORICAL RANGE EVIDENCE", baseline[1]["content"])
        contextual = prompts.build_prompt("Policy", "Damage", self.items, {"range": [80, 120], "n": 2})
        self.assertIn('"range": [80, 120]', contextual[1]["content"])
        self.assertIn("Preserve ranges and censoring bounds", contextual[1]["content"])
        with self.assertRaises(ValueError):
            prompts.build_focus_prompt("", "", {}, {})


if __name__ == "__main__":
    unittest.main()
