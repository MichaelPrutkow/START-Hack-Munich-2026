"""Recall caps from earlier same-policy games: offline, synthetic files only."""
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import runner as R
from claim_to_fame import recall
from tests import test_runtime


def write_game(root, gid, policy, items, tx):
    case = Path(root) / "cases" / f"case_{gid:03d}"
    case.mkdir(parents=True)
    (case / "policy.txt").write_text(policy)
    (case / "items.json").write_text(json.dumps(items))
    cache = Path(root) / "leaderboard"
    cache.mkdir(exist_ok=True)
    (cache / f"game_{gid:03d}.json").write_text(json.dumps({"game": gid, "teams": 3, "tx": tx}))


def fake_parse(case_dir):
    return {int(k): v for k, v in json.loads((Path(case_dir) / "items.json").read_text()).items()}


def tv_transactions():
    # Line 1: X's 600 rejected by Y and paid 1.5x (fair); Y's 650 accepted by Z (a known) but
    # rejected by X with 0 (unfair) -> t in [600, 650).
    return [
        {"issuer": "X", "reviewer": "Y", "line_item_index": 1, "accepted": False, "amount": 900.0},
        {"issuer": "X", "reviewer": "Z", "line_item_index": 1, "accepted": True, "amount": 600.0},
        {"issuer": "Y", "reviewer": "X", "line_item_index": 1, "accepted": False, "amount": 0.0},
        {"issuer": "Y", "reviewer": "Z", "line_item_index": 1, "accepted": True, "amount": 650.0},
        {"issuer": "Z", "reviewer": "X", "line_item_index": 1, "accepted": False, "amount": 0.0},
        {"issuer": "Z", "reviewer": "Y", "line_item_index": 1, "accepted": False, "amount": 0.0},
    ]


class RecallUnitTests(unittest.TestCase):
    def setUp(self):
        recall._CACHE.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.enterContext(patch.object(recall, "parse_items", side_effect=fake_parse))

    def tearDown(self):
        self.dir.cleanup()

    def caps(self, gid, items, policy):
        return recall.caps_for(gid, items, policy, self.root / "cases", self.root / "leaderboard", "us")

    def test_same_policy_similar_line_gets_midpoint_cap(self):
        write_game(self.root, 3, "POLICY A", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}},
                   tv_transactions())
        caps = self.caps(5, {2: {"description": "TV set (surge damaged)", "quantity": 1, "unit": "pcs"}}, "POLICY A")
        self.assertEqual(caps[2]["t"], [600.0, 650.0])
        self.assertEqual(caps[2]["cap"], 625.0)
        self.assertEqual(caps[2]["matches"][0]["game"], 3)

    def test_other_policy_or_later_game_is_never_recalled(self):
        write_game(self.root, 3, "POLICY A", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}},
                   tv_transactions())
        item = {2: {"description": "TV set", "quantity": 1, "unit": "pcs"}}
        self.assertEqual(self.caps(5, item, "POLICY B"), {})
        self.assertEqual(self.caps(3, item, "POLICY A"), {})      # no look-ahead into the same/later game

    def test_quantity_scaling_and_dissimilar_lines(self):
        write_game(self.root, 3, "P", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}},
                   tv_transactions())
        caps = self.caps(4, {1: {"description": "TV set", "quantity": 2, "unit": "pcs"},
                             2: {"description": "Plumber call-out", "quantity": 1, "unit": "pcs"}}, "P")
        self.assertEqual(caps[1]["t"], [1200.0, 1300.0])
        self.assertNotIn(2, caps)

    def test_one_sided_evidence_has_no_strict_cap(self):
        self.assertIsNone(recall.cap_from(0.0, 300.0))
        self.assertIsNone(recall.cap_from(300.0, math.inf))
        self.assertEqual(recall.loose_cap_from(0.0, 300.0), 291.0)
        self.assertIsNone(recall.loose_cap_from(0.0, 10.0))
        self.assertIsNone(recall.loose_cap_from(100.0, 300.0))

    def test_inconsistent_evidence_uses_most_similar_then_latest(self):
        memory = [(1, {1: (recall.tokens("speaker system"), 1.0, "pcs", (390.0, 450.0))}),
                  (2, {1: (recall.tokens("speaker system"), 1.0, "pcs", (0.0, 336.0))})]
        lo, hi, matches = recall.recall_line({"description": "speaker system", "quantity": 1, "unit": "pcs"}, memory)
        self.assertEqual((lo, hi), (0.0, 336.0))
        self.assertEqual(len(matches), 2)

    def test_apply_cap_only_lowers_and_respects_mode(self):
        entry = {"cap": 500.0, "cap_loose": None, "t": [450.0, 550.0]}
        detail = {"stated": None}
        self.assertEqual(recall.apply_cap((600.0, 700.0), entry, detail, "1"), (500.0, 700.0))
        self.assertTrue(detail["recall_applied"])
        self.assertEqual(detail["a_before_recall"], 600.0)
        detail = {"stated": None}
        self.assertEqual(recall.apply_cap((600.0, 700.0), entry, detail, "shadow"), (600.0, 700.0))
        self.assertFalse(detail["recall_applied"])
        self.assertEqual(detail["recall_cap"], 500.0)
        self.assertEqual(recall.apply_cap((400.0, 700.0), entry, {"stated": None}, "1"), (400.0, 700.0))
        self.assertEqual(recall.apply_cap((600.0, 700.0), entry, {"stated": 600.0}, "1"), (600.0, 700.0))
        loose = {"cap": None, "cap_loose": 291.0, "t": [0.0, 300.0]}
        self.assertEqual(recall.apply_cap((400.0, 0.0), loose, {"stated": None}, "1"), (400.0, 0.0))
        self.assertEqual(recall.apply_cap((400.0, 0.0), loose, {"stated": None}, "loose"), (291.0, 0.0))


class RecallHiCapTests(unittest.TestCase):
    """Mode "2": 0.95 x the recalled unfair level caps a and b, one- or two-sided."""

    def test_hi_cap_value(self):
        self.assertEqual(recall.hi_cap_from(300.0), 285.0)
        self.assertIsNone(recall.hi_cap_from(math.inf))
        self.assertIsNone(recall.hi_cap_from(0.0))

    def test_mode_2_caps_a_and_b_only_downwards(self):
        entry = {"cap": None, "cap_loose": None, "cap_hi": 285.0, "t": [0.0, 300.0]}
        detail = {"stated": None}
        self.assertEqual(recall.apply_cap((400.0, 390.0), entry, detail, "2"), (285.0, 285.0))
        self.assertTrue(detail["recall_applied"] and detail["recall_b_applied"])
        self.assertEqual((detail["a_before_recall"], detail["b_before_recall"]), (400.0, 390.0))
        detail = {"stated": None}
        self.assertEqual(recall.apply_cap((200.0, 250.0), entry, detail, "2"), (200.0, 250.0))
        self.assertFalse(detail["recall_applied"] or detail["recall_b_applied"])
        self.assertEqual(recall.apply_cap((400.0, 0.0), entry, {"stated": None}, "2"), (285.0, 0.0))

    def test_mode_2_respects_stated_amounts_and_missing_level(self):
        entry = {"cap": None, "cap_loose": None, "cap_hi": 285.0, "t": [0.0, 300.0]}
        self.assertEqual(recall.apply_cap((400.0, 390.0), entry, {"stated": 400.0}, "2"), (400.0, 390.0))
        none = {"cap": None, "cap_loose": None, "cap_hi": None, "t": [500.0, None]}
        self.assertEqual(recall.apply_cap((400.0, 390.0), none, {"stated": None}, "2"), (400.0, 390.0))

    def test_other_modes_never_touch_b(self):
        entry = {"cap": 500.0, "cap_loose": 520.0, "cap_hi": 522.5, "t": [450.0, 550.0]}
        for mode in ("1", "loose", "shadow", "0"):
            self.assertEqual(recall.apply_cap((600.0, 700.0), entry, {"stated": None}, mode)[1], 700.0)

    def test_caps_for_reports_hi_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            recall._CACHE.clear()
            with patch.object(recall, "parse_items", side_effect=fake_parse):
                write_game(root, 3, "P", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}},
                           tv_transactions())
                caps = recall.caps_for(4, {1: {"description": "TV set", "quantity": 1, "unit": "pcs"}}, "P",
                                       root / "cases", root / "leaderboard", "us")
        self.assertEqual(caps[1]["cap_hi"], 617.5)
        self.assertEqual(caps[1]["cap"], 625.0)


class RecallSafeguardTests(unittest.TestCase):
    """Units must match, a known-fair level is a floor, contradictory evidence sets no cap."""

    def memory(self, *entries):
        return [(g, {1: (recall.tokens(desc), q, u, t)}) for g, desc, q, u, t in entries]

    def test_unit_mismatch_is_not_evidence(self):
        mem = self.memory((1, "paint wall", 10.0, "m2", (100.0, 120.0)))
        self.assertIsNone(recall.recall_line({"description": "paint wall", "quantity": 1, "unit": "flat rate"}, mem))

    def test_single_lump_items_are_comparable(self):
        mem = self.memory((1, "vehicle costs", 1.0, "pcs", (0.0, 20.0)))
        lo, hi, _ = recall.recall_line({"description": "vehicle costs", "quantity": 1, "unit": "flat rate"}, mem)
        self.assertEqual((lo, hi), (0.0, 20.0))
        self.assertIsNone(recall.recall_line({"description": "vehicle costs", "quantity": 2, "unit": "flat rate"}, mem))

    def test_cap_never_below_known_fair_level(self):
        self.assertEqual(recall.hi_cap_from(100.0, lo=98.0), 98.0)
        self.assertEqual(recall.hi_cap_from(100.0, lo=50.0), 95.0)

    def test_contradictory_evidence_sets_no_hi_cap(self):
        self.assertFalse(recall.consistent([{"t": [390.0, 450.0]}, {"t": [0.0, 336.0]}]))
        self.assertTrue(recall.consistent([{"t": [517.0, 616.0]}, {"t": [586.0, 602.0]}]))
        self.assertTrue(recall.consistent([{"t": [300.0, None]}]))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            recall._CACHE.clear()
            low = [dict(x, amount=x["amount"] * 0.5) for x in tv_transactions()]   # t in [300, 325)
            with patch.object(recall, "parse_items", side_effect=fake_parse):
                write_game(root, 3, "P", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}},
                           tv_transactions())                                      # t in [600, 650)
                write_game(root, 4, "P", {"1": {"description": "TV set", "quantity": 1, "unit": "pcs"}}, low)
                caps = recall.caps_for(5, {1: {"description": "TV set", "quantity": 1, "unit": "pcs"}}, "P",
                                       root / "cases", root / "leaderboard", "us")
        self.assertFalse(caps[1]["consistent"])
        self.assertIsNone(caps[1]["cap_hi"])


class RecallRuntimeTests(test_runtime.RuntimeTests):
    """play_game applies the cap only with RECALL_CAP=1 and always records the evidence."""

    def run_with_mode(self, mode):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        caps = {1: {"cap": 50.0, "cap_loose": None, "t": [40.0, 60.0], "matches": [{"game": 1, "line": 1}]}}
        self.enterContext(patch.object(R.recall_mod, "caps_for", return_value=caps))
        self.enterContext(patch.object(R, "recall_setting", return_value=mode))
        votes = [{1: {"covered_prob": 1, "t_if_covered": 100}, 4: {"covered_prob": 1, "t_if_covered": 100}}]
        self.enterContext(patch.object(R, "assess", return_value=(votes, {})))
        return R.play_game(2, time.time(), dry=True)

    def test_live_mode_caps_charge_and_keeps_limit(self):
        rec = self.run_with_mode("1")
        uncapped = R.price(1, 100, None, None, 130)
        self.assertEqual(rec["final_ab"][1], (50.0, uncapped[1]))
        self.assertEqual(rec["final_ab"][4], R.price(1, 100, None, None, 60))
        self.assertTrue(rec["est"]["fast"][1]["recall_applied"])
        self.assertEqual(rec["recall"]["mode"], "1")

    def test_mode_2_caps_charge_and_limit(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        caps = {1: {"cap": 50.0, "cap_loose": None, "cap_hi": 57.0, "t": [40.0, 60.0],
                    "matches": [{"game": 1, "line": 1}]}}
        self.enterContext(patch.object(R.recall_mod, "caps_for", return_value=caps))
        self.enterContext(patch.object(R, "recall_setting", return_value="2"))
        votes = [{1: {"covered_prob": 1, "t_if_covered": 100}, 4: {"covered_prob": 1, "t_if_covered": 100}}]
        self.enterContext(patch.object(R, "assess", return_value=(votes, {})))
        rec = R.play_game(2, time.time(), dry=True)
        self.assertEqual(rec["final_ab"][1], (57.0, 57.0))
        self.assertEqual(rec["final_ab"][4], R.price(1, 100, None, None, 60))
        self.assertTrue(rec["est"]["fast"][1]["recall_b_applied"])

    def test_shadow_mode_only_records(self):
        rec = self.run_with_mode("shadow")
        self.assertEqual(rec["final_ab"][1], R.price(1, 100, None, None, 130))
        self.assertFalse(rec["est"]["fast"][1]["recall_applied"])
        self.assertEqual(rec["est"]["fast"][1]["recall_cap"], 50.0)

    def test_recall_failure_never_blocks_the_round(self):
        self.prepare_round([R.Tier("fast", 1, "fake", None, 1, 20)])
        self.enterContext(patch.object(R.recall_mod, "caps_for", side_effect=OSError("disk")))
        self.enterContext(patch.object(R, "recall_setting", return_value="1"))
        votes = [{1: {"covered_prob": 1, "t_if_covered": 100}}]
        self.enterContext(patch.object(R, "assess", return_value=(votes, {})))
        rec = R.play_game(2, time.time(), dry=True)
        self.assertEqual(rec["winner"], "fast")
        self.assertEqual(rec["recall"]["lines"], {})

    def test_recall_setting_reads_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            self.enterContext(patch.object(R, "ENV_PATH", env))
            os.environ.pop("RECALL_CAP", None)          # setUp's patch.dict restores the environment
            self.assertEqual(R.recall_setting(), "2")
            env.write_text("RECALL_CAP=1\n")
            self.assertEqual(R.recall_setting(), "1")
            env.write_text("RECALL_CAP=shadow\n")
            self.assertEqual(R.recall_setting(), "shadow")
            env.write_text("RECALL_CAP=nonsense\n")
            self.assertEqual(R.recall_setting(), "2")


# The inherited RuntimeTests methods already run in test_runtime; run only the recall ones here.
for _name in [n for n in vars(test_runtime.RuntimeTests) if n.startswith("test_")]:
    if _name not in vars(RecallRuntimeTests):
        setattr(RecallRuntimeTests, _name, None)


if __name__ == "__main__":
    unittest.main()
