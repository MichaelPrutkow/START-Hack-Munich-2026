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

    def test_missing_applied_parameters_or_uncertainty_is_not_refitted(self):
        for key in ("calibration_applied", "uncertainty_applied"):
            rec, data = fixture()
            rec["est"]["fast"]["1"][key] = True
            self.rounds.unlink(missing_ok=True)
            self.add(rec, data)
            self.assertEqual(self.state()["calibrations"], [])

    def test_charge_multiplier_selected_on_training_only(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        good = self.state()["calibrations"][0]
        self.assertNotEqual(good["a_mult"], 0.85)
        self.assertIn(good["a_mult"], F.A_MULT_GRID)
        self.assertEqual(good["status"], "validated")
        selected = next(s for s in good["training_scores"] if s["a_mult"] == good["a_mult"])
        self.assertEqual(selected["lower"], max(s["lower"] for s in good["training_scores"]))
        # Change ONLY holdout settlements. Proposed parameters cannot change.
        for gid in (5, 6):
            _, data = fixture(gid, lines=4, low=85, high=120, true_t=85)
            (self.cache / f"game_{gid:03d}.json").write_text(json.dumps(data))
        bad = self.state()["calibrations"][0]
        self.assertEqual(bad["proposed_parameters"], good["proposed_parameters"])
        self.assertEqual(bad["training_scores"], good["training_scores"])
        self.assertEqual(bad["status"], "shadow")
        self.assertIsNone(bad["active_parameters"])

    def test_calibrated_rows_relearn_raw_predictions_without_compounding(self):
        params = {"a_mult": 0.9, "bias": 1.1, "sigma": 0.4}
        own = [F.floor_c(100 * params["bias"] * params["a_mult"]),
               F.ceil_c(C.rule_b(1, 100 * params["bias"], params["sigma"]))]
        for gid in range(1, 7):
            rec, data = fixture(gid, lines=4, own=own, low=110, high=120, true_t=115)
            for est in rec["est"]["fast"].values():
                est.update(m_raw=100, m=110, calibration_applied=True, **params)
            self.add(rec, data)
        state = self.state()
        self.assertEqual(state["learning_counts"]["eligible"], 24)
        self.assertEqual(state["learning_counts"]["calibrated_observations"], 24)
        candidate = state["calibrations"][0]
        self.assertEqual(candidate["calibrated_observation_count"], 24)
        self.assertGreater(candidate["bias"], 1)  # Fitted against raw100, not adjusted110.
        self.assertLess(candidate["bias"], 1.1)  # Previous1.1 is not compounded into newbias.
        observation, reason = F._training_observation(rec, "fast", rec["est"]["fast"]["1"], own)
        self.assertIsNone(reason)
        self.assertEqual(observation["m"], 100)
        self.assertEqual(observation["applied_parameters"], params)
        self.assertEqual(state["training_exclusions"], [])

    def test_mislogged_calibrated_prices_are_reported_but_not_trained(self):
        rec, data = fixture()
        rec["est"]["fast"]["1"].update(m_raw=100, calibration_applied=True, a_mult=0.9, bias=1.1, sigma=0.4)
        self.add(rec, data)
        state = self.state()
        self.assertEqual(state["games"], [1])
        self.assertEqual(state["calibrations"], [])
        self.assertEqual(state["training_exclusions"][0]["reason"], "logged_parameters_do_not_reproduce_submission")

    def test_validated_incumbent_survives_new_rounds_without_beating_itself(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        previous = self.state()
        old = previous["calibrations"][0]
        params = old["active_parameters"]
        own = [F.floor_c(100 * params["bias"] * params["a_mult"]),
               F.ceil_c(C.rule_b(1, 100 * params["bias"], params["sigma"]))]
        for gid in (7, 8):
            rec, data = fixture(gid, lines=4, own=own, low=110, high=120, true_t=115)
            for est in rec["est"]["fast"].values():
                est.update(m_raw=100, calibration_applied=True, **params)
            self.add(rec, data)
        state = self.state(previous_state=previous)
        kept = state["calibrations"][0]
        self.assertEqual(kept["activation"], "retained")
        self.assertEqual(kept["active_parameters"], params)
        self.assertEqual(kept["validated_at"], old["validated_at"])
        self.assertEqual(kept["latest_proposal"]["sample_count"], 32)
        self.assertEqual(kept["monitoring"]["calibrated_observations"], 8)
        self.assertEqual(kept["latest_proposal"]["status"], "shadow")
        path = self.root / "retained.json"
        F.write_state(state, path)
        self.assertIsNotNone(F.load_state(path, 9, now=NOW))
        stale = F.build_state(self.rounds, self.cache, "us", now=NOW + dt.timedelta(hours=37), previous_state=previous)
        self.assertNotEqual(stale["calibrations"][0]["activation"], "retained")

    def test_schema_one_and_tampered_controls_fail_closed(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        original = self.state()
        path = self.root / "state.json"
        for change in ("schema", "algorithm", "threshold", "active", "alpha", "counts"):
            state = copy.deepcopy(original)
            if change == "schema":
                state["schema_version"] = 1
                self.assertIsNone(F.calibration_for(state, "fast", model="model-a", prompt_hash="prompt-a", config_hash="config-a"))
            elif change == "algorithm":
                state["algorithm"] = "old"
            elif change == "threshold":
                state["controls"]["minimum_gain"] = 0
            elif change == "active":
                state["calibrations"][0]["active_parameters"]["a_mult"] = 0.5
            elif change == "alpha":
                state["calibrations"][0].pop("a_mult")
            else:
                state["calibrations"][0]["fit_count"] = 0
            path.write_text(json.dumps(state))
            self.assertIsNone(F.load_state(path, 7, now=NOW), change)

    def test_rebuilding_identical_evidence_cannot_refresh_validation(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        original = self.state()
        rebuilt = F.build_state(self.rounds, self.cache, "us", now=NOW + dt.timedelta(hours=1),
                                previous_state=original)
        self.assertEqual(rebuilt["calibrations"][0]["validated_at"], original["calibrations"][0]["validated_at"])
        expired = F.build_state(self.rounds, self.cache, "us", now=NOW + dt.timedelta(hours=37),
                                previous_state=rebuilt)
        again = F.build_state(self.rounds, self.cache, "us", now=NOW + dt.timedelta(hours=38),
                              previous_state=expired)
        for state in (expired, again):
            candidate = state["calibrations"][0]
            self.assertEqual(candidate["status"], "shadow")
            self.assertEqual(candidate["reason_code"], "validation_expired_no_new_evidence")

    def test_replacement_must_beat_incumbent_on_same_holdout(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, low=110, high=120, true_t=115))
        original = self.state()
        proposal = copy.deepcopy(original["calibrations"][0])
        proposal["a_mult"] = 0.65 if proposal["a_mult"] != 0.65 else 1.05
        proposal["proposed_parameters"]["a_mult"] = proposal["a_mult"]
        proposal["active_parameters"]["a_mult"] = proposal["a_mult"]
        proposal["evidence_hash"] = "new-evidence"
        rows = [{"game": 5, "baseline_alpha": .85, "baseline_sigma": .35, "calibration_applied": True}]
        upper = proposal["holdout_candidate"]["lower"] + 100
        with patch.object(F, "_score_rows", return_value=(upper, upper)):
            result = F._retain_candidate(proposal, rows, original, "us", NOW)
        self.assertEqual(result["active_parameters"], original["calibrations"][0]["active_parameters"])
        self.assertEqual(result["latest_proposal"]["reason_code"], "no_gain_over_incumbent")

    def test_per_tier_identity_maps_and_mismatch_diagnostics(self):
        for gid in range(1, 7):
            rec, data = fixture(gid, lines=4, low=110, high=120, true_t=115)
            rec["prompt_hash"] = {"fast": "stable-prompt", "deep": f"other-prompt-{gid}"}
            rec["config_hash"] = {"fast": "stable-config", "deep": f"other-config-{gid}"}
            self.add(rec, data)
        state = self.state()
        self.assertEqual(len(state["calibrations"]), 1)
        active = F.calibration_status(state, "fast", model="model-a", prompt_hash="stable-prompt", config_hash="stable-config")
        self.assertEqual(active["status"], "active")
        self.assertIn("a_mult", active["parameters"])
        mismatch = F.calibration_status(state, "fast", model="model-a", prompt_hash="stable-prompt", config_hash="changed-config")
        self.assertEqual(mismatch["reason_code"], "identity_mismatch")
        self.assertEqual(mismatch["available"][0]["mismatched_fields"], ["config_hash"])
        missing = F.calibration_status(state, "deep", model="model-b", prompt_hash="x", config_hash="y")
        self.assertEqual(missing["reason_code"], "no_eligible_observations")

    def test_legacy_complete_winner_has_reporting_and_memory_without_fabricated_identity(self):
        rec, data = fixture()
        rec["winner"] = "fast"
        rec["tiers"] = {"fast": rec.pop("final_ab")}
        rec.pop("line_sources")
        rec.pop("prompt_hash")
        rec.pop("config_hash")
        self.add(rec, data)
        state = self.state()
        self.assertEqual(state["games"], [1])
        self.assertEqual(state["learning_counts"]["recovered_legacy_games"], 1)
        self.assertEqual(len(state["memory"]), 1)
        self.assertEqual(state["calibrations"], [])
        self.assertEqual(state["training_exclusions"][0]["reason"], "missing_model_prompt_or_config_identity")
        self.assertIn("legacy prices recovered", F.render_report(state))

    def test_field_behavior_does_not_label_zero_prices_or_unknown_as_fraud(self):
        self.add(*fixture())
        behavior = self.state()["field_behavior"]
        self.assertEqual(sum(behavior["claims"].values()), 3)  # Excludes the fourth issuer's0/0.
        self.assertEqual(behavior["claims"], {"fair": 2, "unfair": 1, "unknown": 0})
        self.assertEqual(behavior["unfair_accepted_rate"], 0.5)
        self.assertEqual(behavior["unfair_decision_count"], 2)
        self.assertEqual(behavior["fair_decision_count"], 5)

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
