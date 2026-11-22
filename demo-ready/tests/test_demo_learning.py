"""Temporal isolation and genuine evidence, never fictional replay settlements."""
import json
from pathlib import Path
import tempfile
import unittest

import demo
from demo_learning import prepare_history, load_history
from dashboard.leaderboard import Leaderboard, atomic_json
from tests.test_feedback import fixture


class DemoLearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.args = demo.parse_args(["--out", str(self.root / "original"),
                                    "--output", str(self.root / "demo"), "--rules-only", "--offline"])
        self.args.team = "us"
        self.source = Leaderboard(self.args.out, "us")
        self.records = []
        for gid in range(1, 4):
            rec, settlement = fixture(gid=gid)
            self.records.append(rec)
            atomic_json(self.source.shared_cache / f"game_{gid:03d}.json", settlement)
        (self.args.out / "rounds.jsonl").write_text("".join(json.dumps(r)+'\n' for r in self.records))

    def test_chronological_starts_empty_then_uses_only_earlier_replays(self):
        first = load_history(prepare_history(self.args, self.source, 1), 1)
        self.assertEqual(first["memory"], [])
        self.assertEqual(first["max_game"], 0)
        with self.assertRaisesRegex(ValueError, "complete game 1"):
            prepare_history(self.args, self.source, 2)
        rec = dict(self.records[0], demo=True, dry=True)
        # Hypothetical prices deliberately contradict the actual settlements.
        rec["final_ab"] = {"1": [99999, 99999]}
        atomic_json(self.args.output / "replay_001.json", rec)
        second = load_history(prepare_history(self.args, self.source, 2), 2)
        self.assertEqual(second["demo_history"]["evidence_games"], [1])
        self.assertEqual({r["game"] for r in second["memory"]}, {1})
        self.assertEqual(second["memory"][0]["fair_total"], [100.0, 120.0])
        self.assertEqual(second["calibrations"], [])
        self.assertFalse(second["price_research"])
        self.assertFalse((self.args.output / "history/before_002/leaderboard/game_002.json").exists())

    def test_single_game_rebuild_filters_original_logs_and_future_data(self):
        self.args.game = 2
        path = prepare_history(self.args, self.source, 2)
        state = load_history(path, 2)
        self.assertEqual(state["demo_history"]["mode"], "reconstructed_from_original_history")
        self.assertEqual(state["demo_history"]["evidence_games"], [1])
        self.assertEqual(state["max_game"], 1)
        fingerprint = state["demo_history"]["input_fingerprint"]
        self.records[-1]["final_ab"] = {"1": [99999, 99999]}
        (self.args.out / "rounds.jsonl").write_text("".join(json.dumps(r)+'\n' for r in self.records))
        atomic_json(self.source.shared_cache / "game_003.json", {"game": 3})
        again = load_history(prepare_history(self.args, self.source, 2), 2)
        self.assertEqual(again["demo_history"]["input_fingerprint"], fingerprint)
        self.assertEqual(again["memory"], state["memory"])

    def test_rejects_future_history_and_missing_earlier_settlements(self):
        path = prepare_history(self.args, self.source, 1)
        state = json.loads(path.read_text())
        state["max_game"] = 1
        atomic_json(path, state)
        with self.assertRaisesRegex(ValueError, "future"):
            load_history(path, 1)
        self.args.game = 2
        (self.source.shared_cache / "game_001.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing complete settlement"):
            prepare_history(self.args, self.source, 2)
