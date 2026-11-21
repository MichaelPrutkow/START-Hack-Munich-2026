"""Synthetic completed settlements only. No network, live runner, or case data."""
import copy
import datetime as dt
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import calibrate as C
import learn
from claim_to_fame import feedback as F


NOW = dt.datetime(2026, 11, 21, 16, tzinfo=dt.timezone.utc)
POLICY = "Synthetic November test policy."


def fixture(gid=1, lines=1, *, own=None, m=100, low=100, high=120, true_t=110):
    own = own or [F.floor_c(0.85 * m), F.ceil_c(C.rule_b(1.0, m, 0.35))]
    prices = {"us": own, "fair": [low, 150], "high": [high, 150], "zero": [0, 0]}
    tx = []
    for line in range(1, lines + 1):
        for issuer, (a, _) in prices.items():
            for reviewer, (_, b) in prices.items():
                if issuer == reviewer:
                    continue
                accepted = a <= b
                amount = min(a, 4 * true_t + 100) if accepted else 1.5 * a if a <= true_t else 0
                tx.append({"line_item_index": line, "issuer": issuer, "reviewer": reviewer,
                           "accepted": accepted, "amount": amount})
    rec = {"game": gid, "ok": True, "dry": False, "items": {}, "final_ab": {}, "line_sources": {},
           "models": {"fast": "model-a", "deep": "model-b"}, "winner": "deep",
           "est": {"fast": {}}, "a_mult": 0.85, "sigma": 0.35,
           "prompt_hash": "prompt-a", "config_hash": "config-a", "policy_hash": F.policy_fingerprint(POLICY)}
    for line in range(1, lines + 1):
        key = str(line)
        rec["items"][key] = {"description": "Replace damaged copper supply pipe", "quantity": 2, "unit": "m"}
        rec["final_ab"][key] = own
        rec["line_sources"][key] = "fast"  # A partial deep response did NOT replace this line.
        rec["est"]["fast"][key] = {"p": 1.0, "m": m, "cap": None, "shared": None, "stated": None}
    return rec, {"game": gid, "teams": list(prices), "tx": tx}


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.rounds = self.root / "rounds.jsonl"
        self.cache = self.root / "leaderboard"
        self.cache.mkdir()

    def add(self, rec, data):
        with self.rounds.open("a") as handle:
            handle.write(json.dumps(rec) + "\n")
        (self.cache / f"game_{rec['game']:03d}.json").write_text(json.dumps(data))

    def state(self, **kwargs):
        return F.build_state(self.rounds, self.cache, "us", now=NOW, **kwargs)

    def test_final_line_source_and_euro_loss_bounds(self):
        rec, data = fixture()
        self.add(rec, data)
        state = self.state()
        self.assertEqual(state["excluded"], [])
        row = state["reports"][0]["lines"][0]
        self.assertEqual(row["tier"], "fast")
        self.assertEqual(row["t"], [100, 120])
        self.assertEqual(row["net"], 147.5)
        self.assertEqual(row["losses"]["reviewer_penalty"], {"lower": 50, "upper": 50})
        self.assertEqual(row["losses"]["reviewer_overpay"], {"lower": 0, "upper": 0})
        # Actual oracle improvement at t=110: (110+110+165) - (85+85+127.5).
        regret = row["losses"]["issuer_undercharge"]
        self.assertLessEqual(regret["lower"], 87.5)
        self.assertGreaterEqual(regret["upper"], 87.5)
        self.assertEqual(state["calibrations"][0]["status"], "shadow")
        self.assertIsNone(F.calibration_for(state, "fast", model="model-a", prompt_hash="prompt-a", config_hash="config-a"))

    def test_reviewer_unfair_outflow_and_issuer_unfair_are_distinct(self):
        rec, data = fixture(own=[130, 150])
        self.add(rec, data)
        row = self.state()["reports"][0]["lines"][0]
        self.assertEqual(row["charge_status"], "unfair")
        self.assertEqual(row["losses"]["reviewer_overpay"], {"lower": 120, "upper": 120})
        self.assertEqual(row["losses"]["issuer_undercharge"], {"lower": 0, "upper": 0})
        self.assertGreater(row["losses"]["issuer_unfair"]["upper"], 0)

    def test_incomplete_duplicate_and_nonreconciled_games_rejected(self):
        for bad in ("missing_row", "duplicate", "bad_amount", "missing_final", "legacy_winner", "wrong_campaign"):
            with self.subTest(bad=bad):
                rec, data = fixture()
                if bad == "missing_row":
                    data["tx"].pop()
                elif bad == "duplicate":
                    data["tx"].append(data["tx"][0])
                elif bad == "bad_amount":
                    data["tx"][0]["amount"] = 9999
                elif bad == "missing_final":
                    rec["final_ab"] = {}
                elif bad == "legacy_winner":
                    del rec["line_sources"]
                else:
                    rec["campaign"] = "2026-08"
                self.rounds.unlink(missing_ok=True)
                self.add(rec, data)
                state = self.state()
                self.assertEqual(state["games"], [])
                self.assertEqual(state["memory"], [])
                self.assertEqual(state["calibrations"], [])
                self.assertEqual(len(state["excluded"]), 1)

    def test_dry_run_does_not_replace_real_submission_and_future_is_excluded(self):
        rec, data = fixture()
        self.add(rec, data)
        dry = copy.deepcopy(rec)
        dry.update(dry=True, final_ab={"1": [999, 999]})
        self.add(dry, data)
        self.add(*fixture(2))
        self.assertEqual(self.state(before_game=2)["games"], [1])

    def test_uncapped_or_policy_limited_memory_is_never_a_market_unit_rate(self):
        rec, data = fixture()
        rec["est"]["fast"]["1"]["shared"] = 500
        self.add(rec, data)
        state = self.state()
        self.assertEqual(state["calibrations"], [])
        context = F.context_for_items(state, rec["items"], POLICY)
        self.assertIn('"fair_total_eur":[100.0,120.0]', context)
        self.assertIn('"policy_limited":true', context)
        self.assertIn('"coverage_evidence":', context)
        self.assertIn("not market unit rates", context)
        self.assertLessEqual(len(context), 2200)
        self.assertEqual(F.context_for_items(state, rec["items"], POLICY + " changed"), "")
        other = copy.deepcopy(rec["items"])
        other["1"]["quantity"] = 3
        self.assertEqual(F.context_for_items(state, other, POLICY), "")
        other["1"]["quantity"], other["1"]["unit"] = 2, "hrs"
        self.assertEqual(F.context_for_items(state, other, POLICY), "")
        other["1"]["unit"], other["1"]["description"] = "m", "Annual boiler maintenance"
        self.assertEqual(F.context_for_items(state, other, POLICY), "")

    def test_load_rejects_stale_lookahead_schema_and_malformed_state(self):
        self.add(*fixture())
        state = self.state()
        target = self.root / "learning.json"
        F.write_state(state, target, report_path=self.root / "report.md")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIsNotNone(F.load_state(target, 2, now=NOW))
        self.assertIsNone(F.load_state(target, 1, now=NOW))
        self.assertIsNone(F.load_state(target, 2, now=NOW + dt.timedelta(hours=37)))
        self.assertIsNone(F.load_state(target, 2, now=NOW - dt.timedelta(hours=1)))
        for bad in ([], {**state, "schema_version": 99}, {**state, "memory": [{}]}, {**state, "games": [1, 2]}):
            target.write_text(json.dumps(bad))
            self.assertIsNone(F.load_state(target, 3, now=NOW))

    def test_chronological_holdout_can_promote_without_model_or_prompt_leakage(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        state = self.state()
        candidate = state["calibrations"][0]
        self.assertEqual(candidate["train_games"], [1, 2, 3, 4])
        self.assertEqual(candidate["holdout_games"], [5, 6])
        self.assertEqual(candidate["status"], "validated", candidate)
        calibrated = F.calibration_for(state, "fast", model="model-a", prompt_hash="prompt-a", config_hash="config-a")
        self.assertGreater(calibrated["bias"], 1)
        self.assertGreaterEqual(calibrated["conservative_gain"], 0.05)
        self.assertIsNone(F.calibration_for(state, "fast", model="model-b", prompt_hash="prompt-a", config_hash="config-a"))
        self.assertIsNone(F.calibration_for(state, "fast", model="model-a", prompt_hash="new-prompt", config_hash="config-a"))
        self.assertIsNone(F.calibration_for(state, "fast", model="model-a", prompt_hash="prompt-a", config_hash="new-config"))
        path = self.root / "learning.json"
        F.write_state(state, path)
        self.assertIsNotNone(F.load_state(path, 7, now=NOW))
        self.assertEqual(F.load_state(path, 7, now=NOW, model="other")["calibrations"], [])

    def test_harmful_holdout_keeps_candidate_in_shadow(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110 if gid < 5 else 85, high=120,
                              true_t=115 if gid < 5 else 85))
        state = self.state()
        self.assertEqual(state["calibrations"][0]["status"], "shadow")

    def test_holdout_includes_false_positive_coverage_predictions(self):
        for gid in range(1, 5):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        for gid in (5, 6):
            rec, data = fixture(gid, lines=4, low=110, high=120, true_t=0)
            # Peers accepted our old a=85 at b=85; increasing it may lose
            # everything. These p=1 predictions had NO evidence of coverage.
            for row in data["tx"]:
                if row["issuer"] in ("fair", "high"):
                    row.update(accepted=False, amount=0)
            self.add(rec, data)
        candidate = self.state()["calibrations"][0]
        self.assertEqual(candidate["fit_count"], 16)
        self.assertEqual(candidate["holdout_count"], 8)
        self.assertEqual(candidate["holdout_games"], [5, 6])
        self.assertEqual(candidate["status"], "shadow")

    def test_model_prompt_and_config_cohorts_do_not_pool(self):
        for gid in range(1, 7):
            rec, data = fixture(gid, lines=4)
            rec["prompt_hash"] = f"prompt-{gid}"
            self.add(rec, data)
        state = self.state()
        self.assertEqual(len(state["calibrations"]), 6)
        self.assertTrue(all(c["status"] == "shadow" for c in state["calibrations"]))

    def test_already_applied_calibration_or_uncertainty_is_not_refitted(self):
        for key in ("calibration_applied", "uncertainty_applied"):
            rec, data = fixture()
            rec["est"]["fast"]["1"][key] = True
            self.rounds.unlink(missing_ok=True)
            self.add(rec, data)
            self.assertEqual(self.state()["calibrations"], [])

    def test_one_sided_totals_stay_unbounded(self):
        rec, data = fixture()
        # Everyone rejects the high claim; no public accepted payment constrains it.
        for row in data["tx"]:
            if row["issuer"] == "high":
                row.update(accepted=False, amount=0)
            if row["reviewer"] in ("fair", "high") and row["issuer"] == "fair":
                row.update(accepted=False, amount=150)
        self.add(rec, data)
        state = self.state()
        self.assertEqual(state["reports"][0]["lines"][0]["t"], [100, None])
        self.assertIsNone(state["reports"][0]["lines"][0]["losses"]["issuer_undercharge"]["upper"])
        F.write_state(state, self.root / "one-sided.json")

    def test_cli_default_is_cached_and_does_not_call_public_api(self):
        self.add(*fixture())
        with patch.object(C, "get", side_effect=AssertionError("network forbidden")), \
             patch.object(C, "load_game", side_effect=AssertionError("network forbidden")):
            state = learn.main(["--team", "us", "--rounds", str(self.rounds), "--cache", str(self.cache),
                                "--output", str(self.root / "learning.json"), "--report", str(self.root / "loss.md")])
        self.assertEqual(state["games"], [1])
        self.assertIn("Reviewer penalty", (self.root / "loss.md").read_text())


if __name__ == "__main__":
    unittest.main()
