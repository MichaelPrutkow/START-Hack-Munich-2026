"""Replay safety, isolation, resumability and dashboard labels; no external calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import io
from unittest.mock import Mock, patch

import demo
import runner as R


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.args = demo.parse_args(["--out", str(self.root / "production"),
                                    "--output", str(self.root / "demo"), "--rules-only", "--offline"])
        self.args.output.mkdir()
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(R, "load_env"))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")))

    def make_case(self):
        directory = self.args.cases / "case_001"
        directory.mkdir(parents=True)
        for name in ("policy.txt", "description.txt", "invoice.pdf"):
            (directory / name).write_text("synthetic test")
        return directory

    def test_worker_has_no_game_api_learning_or_production_writes(self):
        directory = self.make_case()
        self.args.worker = 1
        real_out = R.OUT
        self.enterContext(patch.multiple(R, OUT=R.OUT, POOL=None, TIERS=[], GIT_COMMIT="test"))
        self.enterContext(patch.object(R, "git_commit", return_value="test"))

        def play(gid, start, dry, use_llm):
            self.assertTrue(dry)
            self.assertFalse(use_llm)
            self.assertEqual(R.OUT, self.args.output)
            self.assertEqual(R.decrypt(gid, None), directory)
            self.assertIsNone(R.fetch_key(gid, 0))
            self.assertEqual(R.recall_setting(), "0")
            self.assertIsNone(R.feedback.load_state(Path("anything")))
            with self.assertRaises(RuntimeError):
                R.S.put("https://example.invalid")
            with self.assertRaises(RuntimeError):
                R.submit(gid, {}, 0, False, "final")
            self.assertTrue(R.submit(gid, {}, 0, True, "final"))
            R.jlog("rounds.jsonl", {"secret": "not written"})
            return {"game": gid, "est": {}, "final_ab": {1: [10, 10]}, "confirmed": ["provisional"]}

        with patch.object(R, "play_game", side_effect=play):
            demo.replay_worker(self.args)
        record = json.loads((self.args.output / "replay_001.json").read_text())
        self.assertTrue(record["dry"])
        self.assertTrue(record["demo"])
        self.assertEqual(record["confirmed"], [])
        self.assertEqual(record["replay_status"], "rules_only")
        self.assertFalse((self.args.output / "rounds.jsonl").exists())
        self.assertNotEqual(real_out, self.args.output)

    def test_manifest_rejects_changed_settings_and_contains_no_secrets(self):
        os.environ["OPENAI_KEY"] = "secret-not-for-manifest"
        demo.prepare_manifest(self.args)
        demo.prepare_manifest(self.args)
        self.assertNotIn("secret-not-for-manifest", (self.args.output / "manifest.json").read_text())
        os.environ["A_MULT"] = "0.71"
        with self.assertRaisesRegex(ValueError, "changed"):
            demo.prepare_manifest(self.args)

    def test_existing_manifestless_results_are_not_adopted(self):
        (self.args.output / "replay_001.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "no manifest"):
            demo.prepare_manifest(self.args)

    def test_replay_only_uses_completed_cached_games_and_reports_missing_cases(self):
        source = Mock()
        source.index.return_value = ([{"id": 1, "status": "completed"},
                                      {"id": 2, "status": "running"},
                                      {"id": 3, "status": "completed"}], None)
        source.read_game.side_effect = lambda g: {"game": g} if g != 3 else None
        replay = demo.Replay(self.args, source)
        with patch.object(demo.subprocess, "Popen") as process:
            replay.poll()
        process.assert_not_called()
        self.assertEqual(replay.snapshot()["missing_cases"], [1])

    def test_successful_results_resume_without_spending_tokens(self):
        self.make_case()
        source = Mock()
        source.index.return_value = ([{"id": 1, "status": "completed"}], None)
        source.read_game.return_value = {"game": 1}
        history = demo.prepare_history(self.args, source, 1)
        meta = demo.load_history(history, 1)["demo_history"]
        (self.args.output / "replay_001.json").write_text(json.dumps({"replay_status":"model_completed", "history":meta}))
        with patch.object(demo.subprocess, "Popen") as process:
            demo.Replay(self.args, source).poll()
        process.assert_not_called()

    def test_resume_blocks_changed_case_inputs(self):
        directory = self.make_case()
        source = Mock()
        source.index.return_value = ([{"id": 1, "status": "completed"}], None)
        source.read_game.return_value = {"game": 1}
        history = demo.prepare_history(self.args, source, 1)
        meta = demo.load_history(history, 1)["demo_history"]
        (self.args.output / "replay_001.json").write_text(json.dumps({"replay_status":"rules_only", "history":meta}))
        (directory / "policy.txt").write_text("changed policy")
        replay = demo.Replay(self.args, source)
        with patch.object(demo.subprocess, "Popen") as process:
            replay.poll()
        process.assert_not_called()
        self.assertEqual(replay.snapshot()["state"], "blocked")

    def test_single_game_cli_and_chronological_start(self):
        args = demo.parse_args(["--game", "42"])
        self.assertEqual((args.first, args.last), (42, 42))
        self.assertTrue(args.replay)
        with self.assertRaises(SystemExit):
            demo.parse_args(["--replay", "--first", "2"])

    def test_handover_check_uses_no_model_or_network(self):
        from tests.test_feedback import fixture
        from dashboard.leaderboard import atomic_json
        self.make_case()
        _, settlement = fixture()
        atomic_json(self.args.out / "leaderboard/game_001.json", settlement)
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(R, "KeyPool", side_effect=AssertionError("no model setup")):
            code = demo.main(["--check", "--last", "1", "--out", str(self.args.out), "--output", str(self.args.output)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ready"])
        self.assertEqual(report["missing_successful_original_logs"], [1])

    def test_output_cannot_replace_production_root(self):
        with self.assertRaises(SystemExit):
            demo.parse_args(["--out", str(self.root), "--output", str(self.root)])

    def test_real_round_orchestration_and_dashboard_comparison(self):
        from tests.test_feedback import fixture
        from dashboard.leaderboard import Leaderboard, atomic_json
        rec, settlement = fixture()
        self.make_case()
        self.args.worker = 1
        self.args.team = "us"
        self.enterContext(patch.multiple(R, OUT=R.OUT, POOL=None, TIERS=[], GIT_COMMIT="test"))
        with patch.object(R, "parse_items", return_value={1: rec["items"]["1"]}), \
                patch.object(R, "git_commit", return_value="test"):
            demo.replay_worker(self.args)
        record = json.loads((self.args.output / "replay_001.json").read_text())
        self.assertEqual(record["final_ab"], {"1": [50.0, 50.0]})
        source = Leaderboard(self.args.output, "us")
        atomic_json(source.cache / "game_001.json", settlement)
        dashboard = demo.DemoDashboard(source, "us", self.args, demo.Replay(self.args, source))
        before = dashboard.overview("us")["games"][0]["net"]
        detail = dashboard.detail(1, "us")
        self.assertTrue(detail["demo_replay"]["simulation"]["available"])
        self.assertEqual(detail["demo_replay"]["status"], "rules_only")
        self.assertEqual(detail["summary"]["net"], before)
        self.assertNotIn("demo_replay", dashboard.detail(1, "fair"))
        record["final_ab"]["99"] = [1, 1]
        atomic_json(self.args.output / "replay_001.json", record)
        self.assertFalse(dashboard.detail(1, "us")["demo_replay"]["simulation"]["available"])


if __name__ == "__main__":
    unittest.main()
