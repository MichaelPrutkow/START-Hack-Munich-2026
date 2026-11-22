"""Historical predictions train; only later identified estimates validate. Offline only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claim_to_fame import feedback as F
from tests.test_feedback import fixture, NOW


class HistoricalLearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rounds, self.cache = self.root / "rounds.jsonl", self.root / "cache"
        self.cache.mkdir()
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")))

    def add(self, gid, *, historical=False, lines=4, low=110, high=120, true_t=115, mutate=None):
        rec, data = fixture(gid, lines=lines, low=low, high=high, true_t=true_t)
        if historical:
            rec.pop("prompt_hash")
            rec.pop("config_hash")
            rec.pop("models")
            rec["winner"] = "fast"
            rec["tiers"] = {"fast": rec.pop("final_ab")}
            rec.pop("line_sources")
        if mutate:
            mutate(rec)
        with self.rounds.open("a") as handle:
            handle.write(json.dumps(rec) + "\n")
        (self.cache / f"game_{gid:03d}.json").write_text(json.dumps(data))

    def build(self, **kwargs):
        return F.build_state(self.rounds, self.cache, "us", now=NOW, **kwargs)

    def history(self):
        for gid in range(1, 7):
            self.add(gid, historical=True)

    def test_reported_five_line_case_now_trains_but_does_not_claim_validation(self):
        self.history()
        self.add(13, lines=2)
        self.add(14, lines=3)
        state = self.build()
        candidate = state["calibrations"][0]
        self.assertEqual((candidate["fit_count"], candidate["holdout_count"]), (24, 5))
        self.assertEqual(candidate["train_games"], list(range(1, 7)))
        self.assertEqual(candidate["holdout_games"], [13, 14])
        self.assertEqual(candidate["status"], "shadow")
        self.assertTrue(candidate["training_scores"])
        self.assertNotEqual(candidate["proposed_parameters"], candidate["baseline_parameters"])
        self.assertEqual(state["learning_counts"]["historical_training_only"], 24)
        self.assertEqual(state["training_exclusions"], [])
        self.assertIsNone(candidate["historical_sources"][0]["prompt_hash"])

    def test_can_activate_historical_fit_after_current_cohort_passes_holdout(self):
        self.history()
        self.add(13)
        self.add(14)
        state = self.build()
        candidate = state["calibrations"][0]
        self.assertEqual(candidate["status"], "validated")
        path = self.root / "learning.json"
        F.write_state(state, path)
        loaded = F.load_state(path, 15, now=NOW)
        self.assertIsNotNone(loaded)
        self.assertIsNotNone(F.calibration_for(loaded, "fast", model="model-a", prompt_hash="prompt-a", config_hash="config-a"))
        self.assertIsNone(F.calibration_for(loaded, "fast", model="other", prompt_hash="prompt-a", config_hash="config-a"))

    def test_bad_current_holdout_rejects_unchanged_historical_proposal(self):
        self.history()
        self.add(13)
        self.add(14)
        good = self.build()["calibrations"][0]
        self.add(13, low=85, true_t=85)
        self.add(14, low=85, true_t=85)
        bad = self.build()["calibrations"][0]
        self.assertEqual(good["proposed_parameters"], bad["proposed_parameters"])
        self.assertEqual(good["training_scores"], bad["training_scores"])
        self.assertEqual(bad["status"], "shadow")

    def test_history_alone_can_never_self_validate(self):
        self.history()
        state = self.build()
        self.assertEqual(state["calibrations"], [])
        self.assertEqual(state["learning_counts"]["historical_training_only"], 24)

    def test_recent_unknown_identity_cannot_leak_into_training_or_holdout(self):
        self.history()
        self.add(13)
        self.add(14)
        before = self.build()["calibrations"][0]
        self.add(15, historical=True, low=85, true_t=85)
        after = self.build()["calibrations"][0]
        self.assertEqual(before["evidence_hash"], after["evidence_hash"])
        self.assertNotIn(15, after["train_games"] + after["holdout_games"])

    def test_invalid_history_stays_excluded_and_prices_are_not_used_to_invent_estimates(self):
        self.add(1, historical=True, mutate=lambda r: r.pop("est"))
        self.add(2, historical=True, mutate=lambda r: r["est"]["fast"]["1"].update(m=999))
        self.add(3, historical=True, mutate=lambda r: r["est"]["fast"]["1"].update(cap=100))
        state = self.build()
        reasons = {r["reason"] for r in state["training_exclusions"]}
        self.assertTrue({"missing_estimate", "logged_parameters_do_not_reproduce_submission", "policy_anchor_or_cap"} <= reasons)

    def test_tampered_historical_validation_provenance_fails_closed(self):
        self.history()
        self.add(13)
        self.add(14)
        state = self.build()
        for field, value in (("historical_train_count", 0), ("historical_train_games", [13]),
                             ("target_cohort_games", [1, 2]), ("historical_sources", [])):
            changed = copy.deepcopy(state)
            changed["calibrations"][0][field] = value
            path = self.root / "state.json"
            path.write_text(json.dumps(changed))
            self.assertIsNone(F.load_state(path, 15, now=NOW), field)

    def test_one_target_game_cannot_validate_even_with_many_positions(self):
        self.history()
        self.add(13, lines=8)
        self.assertEqual(self.build()["calibrations"][0]["status"], "shadow")
