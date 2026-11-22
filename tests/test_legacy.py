"""Synthetic legacy logs only; never start the runner or access an API."""
import copy
import hashlib
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claim_to_fame.legacy import recover_record


def old_record():
    return {"game": 4, "ok": True, "winner": "fast", "lines": 2,
            "items": {"1": {"description": "Synthetic line"}, "4": {"description": "Other synthetic line"}},
            "tiers": {"rules": {"1": [50, 50], "4": [30, 30]},
                      "fast": {"1": [80, 90], "4": [0, 0]}, "deep": {"1": [999, 999]}},
            "models": {"fast": "original-model"}}


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")))

    def test_only_explicit_complete_winner_is_recovered_without_mutation(self):
        rec = old_record()
        saved = copy.deepcopy(rec)
        recovered, reason = recover_record(rec)
        self.assertEqual(rec, saved)
        self.assertEqual(recovered["final_ab"], rec["tiers"]["fast"])
        self.assertEqual(recovered["line_sources"], {"1": "fast", "4": "fast"})
        self.assertTrue(recovered["legacy_recovered"])
        self.assertIn("pending_settlement_verification", reason)
        self.assertEqual(recovered["legacy_recovery_reason"], reason)
        self.assertNotIn("prompt_hash", recovered)
        self.assertNotIn("config_hash", recovered)
        self.assertNotIn("policy_hash", recovered)
        self.assertEqual(recovered["models"], rec["models"])

    def test_partial_winner_never_uses_other_tiers_or_provisional_ok(self):
        for winner in ("deep", "unknown", None):
            rec = old_record()
            rec["winner"] = winner
            result, _ = recover_record(rec)
            self.assertIs(result, rec)
            self.assertNotIn("final_ab", result)
        rec = old_record()
        rec["winner"] = "rules"
        self.assertEqual(recover_record(rec)[0]["final_ab"], rec["tiers"]["rules"])

    def test_existing_or_partial_modern_provenance_is_never_repaired(self):
        for field, value in (("final_ab", None), ("final_ab", {}), ("final_ab", {"1": [1, 2]}),
                             ("line_sources", {}), ("config", {}), ("votes", {}), ("learning_version", None)):
            rec = old_record()
            rec[field] = value
            result, _ = recover_record(rec)
            self.assertIs(result, rec)
            self.assertNotIn("legacy_recovered", result)

    def test_failed_dry_or_nonpositive_games_are_rejected(self):
        for field, value in (("game", 0), ("game", -1), ("game", True), ("game", "4"),
                             ("ok", False), ("ok", 1), ("dry", True), ("echo_ok", False)):
            rec = old_record()
            rec[field] = value
            self.assertIs(recover_record(rec)[0], rec)

    def test_empty_duplicate_extra_and_malformed_indices_are_rejected(self):
        for bad in ({}, {"1": [1, 2]}, {"1": [1, 2], 1: [1, 2], "4": [3, 4]},
                    {"1": [1, 2], "4": [3, 4], "8": [3, 4]}, {"01": [1, 2], "4": [3, 4]},
                    {True: [1, 2], "4": [3, 4]}):
            rec = old_record()
            rec["tiers"]["fast"] = bad
            self.assertIs(recover_record(rec)[0], rec)
        rec = old_record()
        rec["lines"] = 3
        self.assertIs(recover_record(rec)[0], rec)

    def test_nonfinite_negative_boolean_and_non_numeric_prices_are_rejected(self):
        for pair in ([math.nan, 1], [1, math.inf], [-1, 0], [True, 0], ["80", 90], [1], None):
            rec = old_record()
            rec["tiers"]["fast"]["1"] = pair
            self.assertIs(recover_record(rec)[0], rec)

    def test_policy_hash_comes_only_from_one_local_case_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            case = base / "case_004"
            case.mkdir()
            text = "Synthetic policy only.\n"
            (case / "policy.txt").write_text(text)
            recovered, _ = recover_record(old_record(), case_dir=base)
            self.assertEqual(recovered["policy_hash"], hashlib.sha256(text.encode()).hexdigest())
            self.assertTrue(recovered["legacy_policy_recovered"])
            (case / "policy-old.txt").write_text("Ambiguous second policy")
            self.assertNotIn("policy_hash", recover_record(old_record(), case_dir=base)[0])
            rec = old_record()
            rec["policy_hash"] = "existing-hash"
            self.assertEqual(recover_record(rec, case_dir=base)[0]["policy_hash"], "existing-hash")

    def test_recovered_record_still_needs_complete_consistent_settlements(self):
        from claim_to_fame.feedback import _reconcile
        from tests.test_feedback import fixture
        rec, settlement = fixture(gid=4)
        expected = rec.pop("final_ab")
        rec.pop("line_sources")
        rec["winner"] = "fast"
        rec["tiers"] = {"fast": expected}
        rec.pop("prompt_hash")
        rec.pop("config_hash")
        recovered, _ = recover_record(rec)
        _, final = _reconcile(recovered, settlement, "us")
        self.assertEqual(final[1], expected["1"])
        self.assertNotIn("prompt_hash", recovered)
        settlement["tx"].pop()
        with self.assertRaises(ValueError):
            _reconcile(recovered, settlement, "us")


if __name__ == "__main__":
    unittest.main()
