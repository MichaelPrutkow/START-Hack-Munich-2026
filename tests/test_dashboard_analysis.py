"""Regression tests for the read-only dashboard's transaction and what-if views."""

import json
import unittest

from dashboard.analysis import analyse_game, simulate_game


def game(prices, t, cap):
    rows = []
    for issuer, (charge, _) in prices.items():
        for reviewer, (_, limit) in prices.items():
            if issuer == reviewer:
                continue
            accepted = charge <= limit
            amount = min(charge, cap) if accepted else 1.5 * charge if charge <= t else 0
            rows.append({"issuer": issuer, "reviewer": reviewer, "line_item_index": 1,
                         "accepted": accepted, "amount": amount})
    return {"game": 3, "teams": list(prices), "tx": rows}


def recorded(a, b):
    return {"game": 3, "ok": True, "final_ab": {"1": [a, b]}, "line_sources": {"1": "deep"}}


class DashboardAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.game = game({"us": (120, 50), "other": (100, 90)}, t=110, cap=440)

    def test_own_observed_prices_and_directed_transactions(self):
        result = analyse_game(self.game, "us", recorded(120, 50))
        self.assertTrue(result["complete"])
        self.assertEqual(result["summary"]["net"], -150)
        self.assertEqual(result["summary"]["review_penalty"], 50)
        self.assertEqual(sum(team["net"] for team in result["teams"]), 0)
        line = result["lines"][0]
        self.assertEqual(line["t"], {"lower": 100, "upper": 120})
        self.assertEqual(line["own"]["gap"], {"lower": 0, "upper": 20})
        self.assertEqual(line["own"]["charge_status"], "over")
        self.assertEqual(line["own"]["source"], "runner")
        self.assertEqual([(tx["direction"], tx["outcome"]) for tx in line["transactions"]],
                         [("incoming", "rejected_paid"), ("outgoing", "rejected_zero")])

    def test_legacy_candidate_shown_without_claiming_final_provenance(self):
        old = {"game": 3, "ok": True, "winner": "deep", "tiers": {"deep": {"1": [120, 50]}}}
        own = analyse_game(self.game, "us", old)["lines"][0]["own"]
        self.assertEqual(own["legacy"], {"a": 120, "b": 50})
        self.assertIsNone(own["a"])
        self.assertIsNone(own["b"])
        self.assertEqual(own["charge_status"], "over")

    def test_rejected_log_falls_back_to_public_evidence(self):
        info = analyse_game(self.game, "us", recorded(85, 110))["lines"][0]
        self.assertEqual(info["own"]["source"], "public")
        self.assertIsNone(info["own"]["a"])
        self.assertTrue(info["own"]["log_warning"])
        self.assertIsNone(info["t"]["upper"])

    def test_capped_accepted_payment_is_not_displayed_as_exact_charge(self):
        data = game({"us": (1000, 20), "other": (900, 0), "third": (50, 1000)}, t=75, cap=405)
        report = analyse_game(data, "us")
        line = report["lines"][0]
        self.assertEqual(line["t"], {"lower": 50, "upper": None})
        tx = next(row for row in line["transactions"] if row["issuer"] == "us" and row["accepted"])
        self.assertEqual(tx["outcome"], "accepted_over")
        self.assertEqual(tx["amount"], 405)
        self.assertEqual(tx["charge"], {"lower": 405, "upper": None})
        json.dumps(report, allow_nan=False)

    def test_preserves_observed_default_for_omitted_legs(self):
        unchanged = simulate_game(self.game, "us", {})
        self.assertTrue(unchanged["available"])
        self.assertTrue(unchanged["exact"])
        self.assertEqual(unchanged["delta"], {"lower": 0, "upper": 0})
        changed = simulate_game(self.game, "us", {1: {"b": 100}})
        self.assertEqual(changed["delta"], {"lower": 50, "upper": 50})
        self.assertEqual(changed["lines"][0]["actual_net"], -150)
        # The opponent who rejected 120 may accept or reject a new fair price of 100.
        changed = simulate_game(self.game, "us", {1: {"a": 100}}, recorded(120, 50))
        self.assertEqual(changed["delta"], {"lower": 100, "upper": 150})

    def test_incomplete_data_never_extrapolates_missing_cashflows(self):
        self.game["tx"].pop()
        self.assertFalse(analyse_game(self.game, "us")["complete"])
        result = simulate_game(self.game, "us", {1: {"b": 100}})
        self.assertFalse(result["available"])
        self.assertEqual(result["delta"], {"lower": None, "upper": None})

    def test_known_missing_position_makes_game_incomplete(self):
        rec = recorded(120, 50)
        rec["final_ab"]["2"] = [120, 50]
        self.assertFalse(simulate_game(self.game, "us", {}, rec)["available"])

    def test_bad_input_and_duplicates_never_count_as_valid_transactions(self):
        for changes in ({2: {"a": 20}}, {1: {"a": -20}}, {1: {"b": float("inf")}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                simulate_game(self.game, "us", changes)
        self.game["tx"].append(self.game["tx"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            analyse_game(self.game, "us")


if __name__ == "__main__":
    unittest.main()
