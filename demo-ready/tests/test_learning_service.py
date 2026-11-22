"""Public ingestion and background scheduling with synthetic HTTP fixtures only."""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import calibrate as C
from claim_to_fame import feedback as F
from claim_to_fame import learning_service as L


def fixture(gid=1):
    rows = [{"line_item_index": 1, "issuer": "us", "reviewer": "other", "accepted": True, "amount": 85},
            {"line_item_index": 1, "issuer": "other", "reviewer": "us", "accepted": False, "amount": 150}]
    rec = {"game": gid, "ok": True, "dry": False,
           "items": {"1": {"description": "Synthetic pipe", "quantity": 1, "unit": "m"}},
           "final_ab": {"1": [85, C.rule_b(1, 100, .35)]}, "line_sources": {"1": "fast"},
           "est": {"fast": {"1": {"m": 100, "m_raw": 100, "p": 1, "bias": 1, "sigma": .35, "a_mult": .85}}},
           "models": {"fast": "synthetic"}, "prompt_hash": "prompt", "config_hash": "config",
           "policy_hash": "policy", "a_mult": .85, "sigma": .35}
    return rec, {"game": gid, "teams": ["us", "other"], "tx": rows}


class Transport:
    def __init__(self, games=(1,), *, paginated=False):
        self.games, self.paginated, self.calls = list(games), paginated, []
        self.fail = None
        self.matrix_offset = 0

    def __call__(self, path, **params):
        self.calls.append((path, {k: v for k, v in params.items() if k not in ("deadline", "timeout")}))
        if self.fail:
            self.fail(path, params)
        page = params.get("page", 1)
        if path == "games":
            rows = [{"id": gid, "status": "completed"} for gid in self.games]
            return {"items": rows[page-1:page] if self.paginated else rows,
                    "total_pages": len(rows) if self.paginated else 1, "page": page}
        if path == "matrix":
            rows = [{"team_name": "us", "cells": [-65 + self.matrix_offset for _ in self.games]},
                    {"team_name": "other", "cells": [65 for _ in self.games]}]
            return {"items": rows[page-1:page] if self.paginated else rows,
                    "total_pages": 2 if self.paginated else 1, "game_ids": self.games}
        if path == "transactions":
            rows = fixture(params["game_id"])[1]["tx"]
            return {"items": rows[page-1:page] if self.paginated else rows,
                    "total_pages": 2 if self.paginated else 1, "page": page}
        raise AssertionError(path)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = self.root / "cache"
        self.transport = Transport()

    def test_complete_pagination_and_private_cache(self):
        self.transport = Transport((1, 2), paginated=True)
        self.assertEqual([g["id"] for g in C.completed_games(getter=self.transport)], [1, 2])
        matrix = C.performance_matrix(getter=self.transport)
        result = C.load_game(1, "us", cache_dir=self.cache, getter=self.transport,
                             expected_lines={1}, matrix=matrix)
        self.assertEqual(result["reconciliation"]["status"], "matrix_verified")
        self.assertEqual(result["campaign"], "2026-11")
        self.assertEqual((self.cache / "game_001.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(result["tx"]), 2)
        calls = len(self.transport.calls)
        C.load_game(1, "us", cache_dir=self.cache, getter=self.transport, matrix=matrix)
        self.assertEqual(len(self.transport.calls), calls)

    def test_corrupt_or_partial_full_cache_is_refetched(self):
        self.cache.mkdir()
        path = self.cache / "game_001.json"
        for text in ("{broken", json.dumps({**fixture()[1], "tx": fixture()[1]["tx"][:1]})):
            path.write_text(text)
            C.load_game(1, "us", cache_dir=self.cache, getter=self.transport, expected_lines={1})
            self.assertEqual(len(json.loads(path.read_text())["tx"]), 2)
        self.assertEqual(sum(path == "transactions" for path, _ in self.transport.calls), 4)

    def test_incomplete_publication_never_creates_complete_cache_and_retries(self):
        def bad(path, **params):
            data = self.transport(path, **params)
            if path == "transactions":
                data["items"] = data["items"][:1]
            return data
        with self.assertRaises(C.IncompleteGame):
            C.load_game(1, "us", cache_dir=self.cache, getter=bad)
        self.assertFalse((self.cache / "game_001.json").exists())
        self.assertFalse((self.cache / "_partial" / "game_001.json").exists())
        self.assertEqual(len(C.load_game(1, "us", cache_dir=self.cache, getter=self.transport)["tx"]), 2)

    def test_deadline_checkpoints_resume_completed_pages(self):
        self.transport = Transport(paginated=True)
        def fail(path, params):
            if path == "transactions" and params["team"] == "us" and params["page"] == 2:
                raise C.DeadlineExceeded()
        self.transport.fail = fail
        with self.assertRaises(C.DeadlineExceeded):
            C.load_game(1, "us", cache_dir=self.cache, getter=self.transport)
        self.assertFalse((self.cache / "game_001.json").exists())
        checkpoint = self.cache / "_partial" / "game_001.json"
        self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)
        self.transport.fail = None
        self.transport.calls.clear()
        C.load_game(1, "us", cache_dir=self.cache, getter=self.transport)
        self.assertNotIn(("transactions", {"game_id": 1, "team": "us", "page": 1}), self.transport.calls)
        self.assertFalse(checkpoint.exists())

    def test_corrupt_checkpoint_is_rebuilt(self):
        path = self.cache / "_partial" / "game_001.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"game": 1, "created_at": time.time(), "teams": {"us": "broken"}}))
        self.assertEqual(len(C.load_game(1, "us", cache_dir=self.cache, getter=self.transport)["tx"]), 2)
        self.assertFalse(path.exists())

    def test_matrix_mismatch_is_quarantined_and_never_used(self):
        with self.assertRaises(C.SettlementMismatch):
            C.load_game(1, "us", cache_dir=self.cache, getter=self.transport, matrix={"us": {1: 500}})
        self.assertFalse((self.cache / "game_001.json").exists())
        self.assertTrue((self.cache / "_quarantine" / "game_001.json").exists())

    def test_nonfinite_and_changing_pagination_rejected(self):
        with self.assertRaises(ValueError):
            C.strict_json('{"amount":NaN}')
        transport = Transport(paginated=True)
        def changing(path, **params):
            data = transport(path, **params)
            if params.get("page") == 2:
                data["total_pages"] = 3
            return data
        with self.assertRaises(C.IncompleteGame):
            C.load_game(1, "us", cache_dir=self.cache, getter=changing)

    def test_get_obeys_rate_and_request_budget_without_network(self):
        response = type("Response", (), {"raise_for_status": lambda self: None, "text": '{"items":[]}'})()
        with patch.object(C.time, "sleep") as sleep, patch.object(C.requests, "get", return_value=response) as request:
            C.get("games", deadline=time.time() + 3)
        sleep.assert_called_once_with(1.0)
        self.assertLessEqual(request.call_args.kwargs["timeout"], 1.5)
        with patch.object(C.requests, "get") as request, self.assertRaises(C.DeadlineExceeded):
            C.get("games", deadline=time.time() - 1)
        request.assert_not_called()

    def test_deadline_at_atomic_commit_preserves_previous_file(self):
        path = self.root / "saved.json"
        C.atomic_json(path, {"old": True})
        calls = []
        def deadline():
            calls.append(True)
            return time.time() + 10 if len(calls) == 1 else time.time() - 1
        with self.assertRaises(C.DeadlineExceeded):
            C.atomic_json(path, {"new": True}, deadline=deadline)
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(list(self.root.glob(".saved.json.*")), [])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rounds, self.cache = self.root / "rounds.jsonl", self.root / "cache"
        self.output, self.report = self.root / "learning.json", self.root / "report.md"
        self.now = time.time()
        self.schedule = [(1, self.now - 200), (2, self.now + 300)]
        self.transport = Transport()
        self.write_rounds([fixture()[0]])

    def write_rounds(self, records):
        self.rounds.write_text("".join(json.dumps(rec) + "\n" for rec in records))

    def refresh(self, **kwargs):
        options = dict(schedule=self.schedule, getter=self.transport)
        options.update(kwargs)
        return L.refresh_once("us", self.rounds, self.cache, self.output, self.report, **options)

    def test_only_completed_local_past_games_are_ingested(self):
        self.write_rounds([fixture(1)[0], fixture(2)[0]])
        self.transport = Transport((1, 2, 50))
        status = self.refresh()
        self.assertEqual(status["status"], "updated", status)
        self.assertEqual(json.loads(self.output.read_text())["games"], [1])
        self.assertEqual({params["game_id"] for path, params in self.transport.calls if path == "transactions"}, {1})
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)

    def test_publication_delay_is_retried(self):
        self.transport = Transport(())
        self.assertEqual(self.refresh()["status"], "pending")
        self.assertFalse(self.output.exists())
        self.transport = Transport()
        self.assertEqual(self.refresh()["status"], "updated")

    def test_dry_rerun_and_game_zero_do_not_block_completed_real_record(self):
        real = fixture()[0]
        self.write_rounds([real, {**real, "dry": True, "ok": False}])
        self.schedule.append((0, self.now + 1))
        self.assertEqual(self.refresh()["status"], "updated")
        self.assertEqual(json.loads(self.output.read_text())["games"], [1])

    def test_legacy_recall_uses_original_case_directory(self):
        record = fixture()[0]
        record["winner"] = "fast"
        record["tiers"] = {"fast": record.pop("final_ab")}
        record.pop("line_sources")
        record.pop("policy_hash")
        self.write_rounds([record])
        case = self.root / "cases" / "case_001"
        case.mkdir(parents=True)
        (case / "policy.txt").write_text("Synthetic policy")
        self.assertEqual(self.refresh()["status"], "updated")
        state = json.loads(self.output.read_text())
        self.assertEqual(state["memory"][0]["policy_hash"], F.policy_fingerprint("Synthetic policy"))

    def test_failure_or_empty_rebuild_preserves_previous_state(self):
        self.assertEqual(self.refresh()["status"], "updated")
        original = self.output.read_bytes()
        self.transport.fail = lambda *args: (_ for _ in ()).throw(OSError("offline"))
        self.assertEqual(self.refresh()["status"], "error")
        self.assertEqual(self.output.read_bytes(), original)
        self.transport.fail = None
        with patch.object(F, "build_state", return_value={"games": []}):
            self.assertEqual(self.refresh()["status"], "preserved")
        self.assertEqual(self.output.read_bytes(), original)

    def test_regressed_completed_game_set_preserves_previous_state(self):
        self.write_rounds([fixture(1)[0], fixture(2)[0]])
        self.schedule = [(1, self.now-400), (2, self.now-200), (3, self.now+300)]
        self.transport = Transport((1, 2))
        self.assertEqual(self.refresh()["status"], "updated")
        original = self.output.read_bytes()
        self.transport = Transport((2,))
        self.assertEqual(self.refresh()["status"], "preserved")
        self.assertEqual(self.output.read_bytes(), original)

    def test_window_during_model_fit_cancels_without_replacing_state(self):
        self.assertEqual(self.refresh()["status"], "updated")
        original = self.output.read_bytes()
        schedule = list(self.schedule)
        def build(*args, checkpoint, **kwargs):
            schedule[:] = [(2, time.time()+5)]
            checkpoint()
            raise AssertionError("checkpoint did not stop fitting")
        with patch.object(F, "build_state", side_effect=build):
            self.assertEqual(self.refresh(schedule=lambda: schedule)["status"], "deferred")
        self.assertEqual(self.output.read_bytes(), original)

    def test_near_window_and_dynamic_schedule_stop_before_more_requests(self):
        self.assertEqual(self.refresh(schedule=[(2, time.time() + 5)])["status"], "deferred")
        self.assertEqual(self.transport.calls, [])
        schedule = list(self.schedule)
        def move_window(path, params):
            if path == "games":
                schedule[:] = [(2, time.time() + 5)]
        self.transport.fail = move_window
        status = self.refresh(schedule=lambda: schedule)
        self.assertEqual(status["status"], "deferred")
        self.assertEqual([path for path, _ in self.transport.calls], ["games"])
        self.assertFalse(self.output.exists())

    def test_shared_process_lock_prevents_duplicate_fetch(self):
        with C.cache_lock(self.cache / ".learning-refresh.lock"):
            self.assertEqual(self.refresh()["status"], "busy")
        self.assertEqual(self.transport.calls, [])

    def test_tick_is_nonblocking_single_worker_cooldown_and_close_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def pause(path, params):
            if path == "games":
                entered.set()
                release.wait(2)
        self.transport.fail = pause
        service = L.LearningService("us", self.rounds, self.cache, self.output, self.report,
                                    getter=self.transport, cooldown=120)
        self.assertTrue(service.tick(self.schedule))
        self.assertTrue(entered.wait(1))
        self.assertFalse(service.tick(self.schedule))
        service.close()
        release.set()
        service._thread.join(1)
        self.assertFalse(service.running)
        self.assertFalse(service.tick(self.schedule, now=time.time()+200))
        self.assertEqual([path for path, _ in self.transport.calls], ["games"])
        self.assertFalse(self.output.exists())

    def test_tick_cooldown_survives_finished_worker(self):
        service = L.LearningService("us", self.rounds, self.cache, self.output, getter=self.transport)
        self.assertTrue(service.tick(self.schedule))
        service._thread.join(2)
        self.assertFalse(service.running)
        self.assertFalse(service.tick(self.schedule))
