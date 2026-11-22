"""The read-only watcher can publish a learning artifact without any runner/API calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watch
import learn
from claim_to_fame import feedback
from tests.test_feedback import fixture


class WatchLearningTests(unittest.TestCase):
    def test_finished_settlement_becomes_next_round_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rounds = root / "rounds.jsonl"
            cache = root / "leaderboard"
            cache.mkdir()
            record, game = fixture()
            rounds.write_text(json.dumps(record) + "\n")
            (cache / "game_001.json").write_text(json.dumps(game))
            target = root / "learning.json"
            with patch.object(watch.C, "CACHE", cache), patch("requests.sessions.Session.request",
                    side_effect=AssertionError("watch learning must not call APIs")):
                watch.update_learning("us", rounds, target)
            state = feedback.load_state(target, before_game=2)
            self.assertIsNotNone(state)
            self.assertEqual(state["max_game"], 1)
            self.assertIsNone(feedback.load_state(target, before_game=1))
            self.assertTrue(target.with_name("learning-report.md").exists())

    def test_watcher_without_runner_logs_does_not_replace_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "learning.json"
            target.write_text("existing artifact")
            watch.update_learning("us", root / "absent.jsonl", target)
            self.assertEqual(target.read_text(), "existing artifact")

    def test_empty_rebuild_preserves_previous_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rounds = root / "rounds.jsonl"
            rounds.write_text("")
            target = root / "learning.json"
            target.write_text("previous artifact")
            result, state = learn.rebuild_cached("us", rounds, root / "leaderboard", target)
            self.assertEqual(result["status"], "preserved")
            self.assertEqual(target.read_text(), "previous artifact")

    def test_cached_rebuild_respects_automatic_refresh_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rounds = root / "rounds.jsonl"
            rounds.write_text("")
            cache = root / "leaderboard"
            with watch.C.cache_lock(cache / ".learning-refresh.lock"):
                result, state = learn.rebuild_cached("us", rounds, cache, root / "learning.json")
            self.assertEqual(result["status"], "busy")
            self.assertIsNone(state)
