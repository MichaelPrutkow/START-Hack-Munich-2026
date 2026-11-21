"""Offline checks for the dashboard's public cache and private log boundary."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.leaderboard import Leaderboard, atomic_json, is_complete, read_json
from dashboard.server import Dashboard
from tests.test_feedback import fixture


class LeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = Leaderboard(self.out, "us")

    def test_validation_requires_every_directed_pair_at_each_position(self):
        _, game = fixture(gid=4, lines=2)
        self.assertTrue(is_complete(game))
        rows = game["tx"]
        first = rows[0]
        mutations = {
            "missing": rows[1:],
            "duplicate": [rows[1], *rows[1:]],
            "foreign_team": [{**first, "reviewer": "another team"}, *rows[1:]],
            "self_review": [{**first, "reviewer": first["issuer"]}, *rows[1:]],
            "non_boolean_decision": [{**first, "accepted": 1}, *rows[1:]],
            "non_finite_amount": [{**first, "amount": float("inf")}, *rows[1:]],
        }
        for label, invalid in mutations.items():
            with self.subTest(label=label):
                self.assertFalse(is_complete({**game, "tx": invalid}))

    def paged_team_view(self, game, *, corrupt=False):
        def get(endpoint, *, page, game_id, team):
            self.assertEqual(endpoint, "transactions")
            self.assertEqual(game_id, game["game"])
            rows = [row for row in game["tx"] if team in (row["issuer"], row["reviewer"])]
            split = len(rows) // 2
            if corrupt and team != "us":
                rows = [{**row, "amount": row["amount"] + 1} if "us" in (row["issuer"], row["reviewer"])
                        else row for row in rows]
            # Team views already overlap and a repeated row also crosses the page boundary.
            return {"items": rows[:split] if page == 1 else rows[split - 1:], "total_pages": 2}

        return get

    def test_paginated_team_views_recover_every_unique_transaction(self):
        _, game = fixture(gid=4, lines=2)
        with patch.object(self.source, "get", side_effect=self.paged_team_view(game)) as get:
            loaded = self.source.fetch_game(4)
        self.assertEqual(get.call_count, 2 * len(game["teams"]))
        self.assertTrue(is_complete(loaded))
        self.assertCountEqual(loaded["tx"], game["tx"])
        self.assertEqual(read_json(self.source.cache / "game_004.json"), loaded)

    def test_conflicting_duplicate_never_becomes_a_completed_cache_entry(self):
        _, game = fixture(gid=4, lines=2)
        with patch.object(self.source, "get", side_effect=self.paged_team_view(game, corrupt=True)):
            with self.assertRaisesRegex(ValueError, "Widersprüchliche Abrechnung"):
                self.source.fetch_game(4)
        self.assertIsNone(self.source.read_game(4))

    def test_reuses_shared_cache_when_dashboard_cache_is_incomplete(self):
        _, game = fixture(gid=4, lines=2)
        atomic_json(self.source.cache / "game_004.json", {**game, "tx": game["tx"][1:]})
        atomic_json(self.source.shared_cache / "game_004.json", game)
        with patch.object(self.source, "get", side_effect=AssertionError("re-read of settled game")):
            self.assertEqual(self.source.fetch_game(4), game)
        self.assertEqual([row["id"] for row in self.source.index()[0]], [4])

    def test_pending_round_and_connection_failure_keep_last_complete_results(self):
        _, settled = fixture(gid=4)
        atomic_json(self.source.shared_cache / "game_004.json", settled)

        def get(endpoint, **params):
            if endpoint == "games":
                return {"items": [{"id": 4}, {"id": 5}], "total_pages": 1}
            self.assertEqual(params["game_id"], 5)
            return {"items": [], "total_pages": 1}

        with patch.object(self.source, "get", side_effect=get):
            self.source.refresh()
        dashboard = Dashboard(self.source, "us")
        overview = dashboard.overview("us")
        self.assertIn("wartet auf Transaktionen", overview["error"])
        ready = overview["games"]
        self.assertEqual([(game["id"], game["ready"]) for game in ready], [(5, False), (4, True)])
        self.assertIsNone(ready[0]["net"])
        self.assertEqual(ready[1]["net"], 147.5)

        failure = subprocess.CalledProcessError(6, "curl", stderr=b"temporary lookup failure")
        with patch.object(self.source, "get", side_effect=failure):
            self.source.refresh()
        failed = dashboard.overview("us")
        self.assertEqual(failed["error"], "temporary lookup failure")
        self.assertEqual(failed["games"], ready)
        self.assertEqual(failed["updated_at"], overview["updated_at"])
        self.assertFalse(failed["fetching"])


class DashboardTests(unittest.TestCase):
    def test_recommendations_work_with_multiple_confirmed_local_positions(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = Leaderboard(root, "us")
        record, game = fixture(gid=4, lines=2)
        atomic_json(source.shared_cache / "game_004.json", game)
        dashboard = Dashboard(source, "us", offline=True)

        result = dashboard.detail(4, "us", records={4: record})

        self.assertTrue(result["complete"])
        self.assertEqual([line["index"] for line in result["lines"]], [1, 2])
        for line in result["lines"]:
            with self.subTest(line=line["index"]):
                recommendation = line["recommendation"]
                self.assertGreater(recommendation["delta"]["lower"], 0)
                self.assertEqual(recommendation.get("a"), 100)

    def test_team_switch_never_attaches_our_private_log_to_another_team(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = Leaderboard(root, "us")
        record, game = fixture(gid=4)
        atomic_json(source.shared_cache / "game_004.json", game)
        dry = copy.deepcopy(record)
        dry["dry"] = True
        dry["items"]["1"]["description"] = "Dry-run item should not appear"
        (root / "rounds.jsonl").write_text(f"{json.dumps(record)}\n{json.dumps(dry)}\n{{", encoding="utf-8")
        dashboard = Dashboard(source, "us", offline=True)

        own = dashboard.detail(4, "us", recommend=False)["lines"][0]
        rival = dashboard.detail(4, "fair", recommend=False)["lines"][0]
        self.assertEqual(own["description"], record["items"]["1"]["description"])
        self.assertEqual(own["own"]["estimate"], {"m": 100, "p": 1.0, "tier": "fast"})
        self.assertEqual(own["own"]["source"], "runner")
        self.assertNotIn("description", rival)
        self.assertNotIn("estimate", rival["own"])
        self.assertEqual(rival["own"]["source"], "public")
        self.assertEqual(dashboard.detail(4, "us", recommend=False)["lines"][0], own)
        self.assertTrue(dashboard.overview("fair")["offline"])
        self.assertEqual(dashboard.overview("us")["games"][0]["transaction_count"], len(game["tx"]))
