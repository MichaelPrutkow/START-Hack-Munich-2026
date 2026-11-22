#!/usr/bin/env python3
"""Local dashboard, single-game reconstruction and chronological replay.

./demo.sh                    dashboard, updating public completed rounds
./demo.sh --game 42          replay one game using reconstructed earlier history
./demo.sh --replay           start empty at game 1 and learn from earlier rounds
./demo.sh --offline          dashboard using cached settlements only
./demo.sh --replay --rules-only --offline  no API calls, exercise the demo flow
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from dashboard.leaderboard import Leaderboard, atomic_json, read_json
from dashboard.server import Dashboard, Handler
from demo_learning import prepare_history, load_history

ROOT = Path(__file__).resolve().parent


def case_directory(root, game):
    directory = root / f"case_{game:03d}"
    if not directory.is_dir():
        raise ValueError(f"Missing decrypted case {game}: {directory}")
    for pattern in ("policy*.txt", "description*.txt", "*.pdf"):
        if not list(directory.glob(pattern)):
            raise ValueError(f"Case {game} needs {pattern} in {directory}")
    return directory


def replay_worker(args):
    """Separate process per case: no game API, decryption or production writes."""
    import runner as R
    from unittest.mock import patch

    directory = case_directory(args.cases, args.worker)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if os.environ.get("DEMO_CONFIG_FROZEN") != "1":
        R.load_env()
    R.configure_pricing()
    R.TIERS = R.build_tiers()
    R.POOL = R.KeyPool()
    if not args.rules_only and not R.POOL.names:
        raise ValueError("No OpenAI key configured. Use --rules-only for an offline rehearsal.")
    R.GIT_COMMIT = R.git_commit()
    R.OUT = output
    history = load_history(args.history, args.worker) if args.history else None
    if history is None and args.worker > 1:
        raise ValueError("A past-only history is required after game 1")
    recall = R.recall_mod.caps_for
    recall_mode = os.environ.get("RECALL_CAP", "2")  # frozen at launch, not re-read from .env
    R.AUTO_TUNE = os.environ.get("AUTO_TUNE", "1") != "0"

    def no_game_api(*a, **kw):
        raise RuntimeError("Game API disabled in demo replay")

    def capture(name, record):
        # No production-format submission/round logs; these are not real submissions.
        pass

    def simulated_submit(gid, prices, deadline, dry, tag):
        if not dry:
            raise RuntimeError("Replay requires dry mode")
        return True

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "LEARNING_AUTO": "0", "CONTROL_SHADOW": "0",
        }))
        stack.enter_context(patch.object(R.S, "request", no_game_api))
        stack.enter_context(patch.object(R, "fetch_key", return_value=None))
        stack.enter_context(patch.object(R, "decrypt", return_value=directory))
        stack.enter_context(patch.object(R, "submit", simulated_submit))
        stack.enter_context(patch.object(R, "jlog", capture))
        stack.enter_context(patch.object(R, "recall_setting", return_value=recall_mode if history else "0"))
        if history:
            stack.enter_context(patch.object(R.recall_mod, "caps_for",
                side_effect=lambda gid, items, policy, *a: recall(
                    gid, items, policy, args.cases, args.history.parent / "leaderboard", args.team)))
        stack.enter_context(patch.object(R.feedback, "load_state", return_value=history))
        record = R.play_game(args.worker, time.time(), dry=True, use_llm=not args.rules_only)
    record.update(demo=True, dry=True, confirmed=[],
                  history=history.get("demo_history") if history else {"before_game": 1, "evidence_games": []},
                  mode="single_game" if args.game else "chronological",
                  replay_status="rules_only" if args.rules_only else
                  ("model_completed" if record["est"] else "model_failed_fallback"))
    atomic_json(output / f"replay_{args.worker:03d}.json", record)


def safe_worker_command(args, game, history=None):
    command = [sys.executable, "-B", str(ROOT / "demo.py"), "--worker", str(game),
               "--cases", str(args.cases), "--output", str(args.output), "--team", args.team]
    if history:
        command += ["--history", str(history)]
    if args.game:
        command += ["--game", str(args.game)]
    if args.rules_only:
        command.append("--rules-only")
    return command


class Replay:
    def __init__(self, args, source):
        self.args, self.source = args, source
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.child = None
        self.status = {"state": "idle", "current_game": None, "errors": {}, "missing_cases": []}
        self.attempted = set()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.status))

    def update(self, **values):
        with self.lock:
            self.status.update(values)

    def poll(self):
        try:
            while not self.stopped.is_set():
                games, _ = self.source.index()
                completed = {row["id"] for row in games if row.get("status") == "completed"}
                eligible = range(self.args.first, self.args.last + 1)
                missing = []
                for game in eligible:
                    if self.stopped.is_set():
                        return
                    if game not in completed or self.source.read_game(game) is None:
                        self.update(state="waiting_for_settlement", current_game=game)
                        break
                    if game in self.attempted:
                        continue
                    result = read_json(self.args.output / f"replay_{game:03d}.json")
                    try:
                        case_directory(self.args.cases, game)
                    except ValueError:
                        missing.append(game)
                        break
                    try:
                        self.update(state="preparing_history", current_game=game)
                        history = prepare_history(self.args, self.source, game)
                    except ValueError as exc:
                        self.update(state="blocked", error=str(exc), current_game=game)
                        return
                    if result:
                        expected = load_history(history, game)["demo_history"]["input_fingerprint"]
                        if result.get("history", {}).get("input_fingerprint") != expected:
                            self.update(state="blocked", error="Input data changed; choose a new --output directory.", current_game=game)
                            return
                        if result.get("replay_status") in ("model_completed", "rules_only", "model_failed_fallback"):
                            self.attempted.add(game)
                            continue
                    self.attempted.add(game)
                    self.update(state="running", current_game=game)
                    with (self.args.output / "replay.log").open("ab") as log:
                        with self.lock:
                            if self.stopped.is_set():
                                return
                            self.child = subprocess.Popen(safe_worker_command(self.args, game, history), cwd=ROOT,
                                                          stdout=log, stderr=log)
                        try:
                            code = self.child.wait(timeout=90)
                        except subprocess.TimeoutExpired:
                            self.child.kill()
                            self.child.wait()
                            code = -1
                    if code:
                        with self.lock:
                            self.status["errors"][str(game)] = "Replay failed; restart to retry; see local replay.log"
                        self.update(state="blocked", current_game=game)
                        return
                    self.child = None
                    if not (self.args.output / f"replay_{game:03d}.json").is_file():
                        self.update(state="blocked", error="Replay worker returned without a result.", current_game=game)
                        return
                complete = all((self.args.output / f"replay_{g:03d}.json").is_file() for g in eligible)
                self.update(state="finished" if complete else "waiting_for_data",
                            current_game=None, missing_cases=missing)
                if self.args.offline or complete:
                    return
                self.stopped.wait(15)
        except Exception as exc:
            self.update(state="failed", error=type(exc).__name__)

    def stop(self):
        self.stopped.set()
        with self.lock:
            child = self.child
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


class DemoDashboard(Dashboard):
    def __init__(self, source, team, args, replay):
        super().__init__(source, team, offline=args.offline)
        self.args, self.replay = args, replay

    def overview(self, team):
        result = super().overview(team)
        result["refresh_seconds"] = 5  # local progress only; public polling remains once a minute
        rows = []
        for path in sorted(self.args.output.glob("replay_*.json")):
            record = read_json(path)
            if not record or record.get("demo") is not True:
                continue
            rows.append({"game": record["game"], "status": record.get("replay_status"),
                         "seconds": record.get("t_end"), "winner": record.get("winner"),
                         "models": record.get("models", {}),
                         "usage": record.get("usage", {}),
                         "prices": record.get("final_ab", {})})
        manifest = read_json(self.args.output / "manifest.json") or {}
        result["demo"] = {**self.replay.snapshot(), "results": rows,
                          "mode": "Reconstructed earlier history" if self.args.game or manifest.get("mode") == "single_game" else "Chronological replay · empty at game 1",
                          "enabled": self.args.replay, "rules_only": self.args.rules_only}
        return result

    def detail(self, game, team, **kwargs):
        result = super().detail(game, team, **kwargs)
        if result is None or kwargs.get("changes") is not None or team != self.team:
            return result
        record = read_json(self.args.output / f"replay_{game:03d}.json")
        if record and record.get("demo") is True:
            # Compare against actual opponents without pretending the replay was submitted.
            changes = {i: {"a": pair[0], "b": pair[1]} for i, pair in record["final_ab"].items()}
            if set(changes) != {str(line["index"]) for line in result["lines"]}:
                simulation = {"available": False, "reason": "Replay and settlement positions differ; comparison withheld."}
            else:
                simulation = super().detail(game, team, changes=changes, records=kwargs.get("records"))
            result["demo_replay"] = {
                "status": record["replay_status"], "models": record["models"],
                "seconds": record["t_end"], "prices": record["final_ab"],
                "simulation": simulation, "history": record.get("history", {}),
            }
        return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "out", help="existing competition cache/log directory (read-only in demo)")
    parser.add_argument("--cases", type=Path, help="already decrypted case_NNN folders; default: OUT/cases")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "demo", help="private demo results and cache")
    parser.add_argument("--team", default="chineseModels")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--offline", action="store_true", help="no public leaderboard requests; --replay still calls models unless --rules-only")
    parser.add_argument("--replay", action="store_true", help="start paid model replay, sequentially, of completed local cases")
    parser.add_argument("--check", action="store_true", help="check local handover files; no server, downloads or model calls")
    parser.add_argument("--rules-only", action="store_true", help="rehearse with rule prices and no model calls")
    parser.add_argument("--game", type=int, help="replay one game with learning reconstructed from original earlier logs")
    parser.add_argument("--history", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.game is not None:
        if not 1 <= args.game <= 100:
            parser.error("game must be between 1 and 100")
        args.replay = True
        args.first = args.last = args.game
        if args.output == ROOT / "out" / "demo":
            args.output = ROOT / "out" / f"demo-game-{args.game:03d}"
    elif args.first != 1:
        parser.error("chronological replay starts at 1; resume skips completed cases, or use --game N")
    args.out = args.out.resolve()
    args.cases = (args.cases or args.out / "cases").resolve()
    args.output = args.output.resolve()
    if args.first < 1 or args.last > 100 or args.last < args.first or not 1 <= args.port <= 65535:
        parser.error("require 1 <= first <= last and a valid port")
    if args.output == args.out or args.output == args.cases or args.output in args.out.parents or args.output in args.cases.parents:
        parser.error("output must be separate from the competition/cache/case root")
    return args


def prepare_manifest(args):
    """Freeze non-secret configuration; refuse to combine different replay runs."""
    import runner as R
    R.load_env()
    R.configure_pricing()
    tiers = R.build_tiers()
    if not args.rules_only and not R.KeyPool().names:
        raise ValueError("No OpenAI key configured; use --rules-only for rehearsal.")
    names = ("A_MULT", "SIGMA", "DISAGREE", "PRICING_UNCERTAINTY", "FOCUS_ENABLED",
             "FOCUS_BUDGET", "FOCUS_MAX_LINES", "DEEP_BUNDLE", "MAX_BUNDLE",
             "AUTO_TUNE", "PRICE_LEARNING", "RECALL_CAP")
    code = hashlib.sha256()
    for path in [ROOT / "demo.py", ROOT / "demo_learning.py", ROOT / "runner.py", *sorted((ROOT / "claim_to_fame").glob("*.py"))]:
        code.update(path.name.encode() + path.read_bytes())
    manifest = {"code_sha256": code.hexdigest(), "mode": "single_game" if args.game else "chronological", "game": args.game,
                "rules_only": args.rules_only, "team": args.team, "cases": str(args.cases),
                "models": [{"tier": t.name, "model": t.model, "votes": t.votes,
                            "budget": t.budget, "extra": t.extra} for t in tiers],
                "settings": {name: os.environ.get(name) for name in names}}
    path = args.output / "manifest.json"
    old = read_json(path)
    if old is not None and old != manifest:
        raise ValueError("Replay code/settings changed. Choose a new --output directory to keep results separate.")
    if old is None and list(args.output.glob("replay_*.json")):
        raise ValueError("Existing replay results have no manifest. Choose a new --output directory.")
    atomic_json(path, manifest)
    os.environ["DEMO_CONFIG_FROZEN"] = "1"


def main(argv=None):
    args = parse_args(argv)
    if args.worker is not None:
        return replay_worker(args)
    if args.check:
        original = Leaderboard(args.out, args.team)
        downloaded = Leaderboard(args.output, args.team)
        missing_cases, missing_settlements = [], []
        for game in range(1, args.last + 1):
            try:
                case_directory(args.cases, game)
            except ValueError:
                missing_cases.append(game)
            if original.read_game(game) is None and downloaded.read_game(game) is None:
                missing_settlements.append(game)
        from dashboard.server import read_round_records
        logs = read_round_records(args.out / "rounds.jsonl")
        missing_logs = [g for g in range(1, args.last + 1) if g not in logs]
        ready = not missing_cases and not missing_settlements and (not args.game or not any(g < args.game for g in missing_logs))
        print(json.dumps({"ready": ready, "through_game": args.last, "missing_cases": missing_cases,
                          "missing_complete_settlements": missing_settlements,
                          "missing_successful_original_logs": missing_logs,
                          "note": "Original logs enable historical reconstruction and exact original-price comparisons; no API calls made."}, indent=2))
        return 0 if ready else 1
    args.output.mkdir(parents=True, exist_ok=True)
    lock_file = (args.output / "demo.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another demo is already using this output directory.")
    if args.replay:
        try:
            prepare_manifest(args)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    # Read existing caches, but put new public downloads only in the demo directory.
    source = Leaderboard(args.output, args.team)
    original = Leaderboard(args.out, args.team)
    read_demo = source.read_game
    source.read_game = lambda game: read_demo(game) or original.read_game(game)
    index_demo = source.index
    def merged_index():
        fresh, timestamp = index_demo()
        old, old_timestamp = original.index()
        merged = {row["id"]: row for row in old}
        merged.update({row["id"]: row for row in fresh})
        return sorted(merged.values(), key=lambda row: row["id"], reverse=True), timestamp or old_timestamp
    source.index = merged_index
    replay = Replay(args, source)
    dashboard = DemoDashboard(source, args.team, args, replay)
    # Official details may read the original logs; replay records never enter that path.
    original_detail = dashboard.detail
    def detail_with_logs(game, team, **kwargs):
        from dashboard.server import read_round_records
        kwargs.setdefault("records", read_round_records(args.out / "rounds.jsonl"))
        return original_detail(game, team, **kwargs)
    dashboard.detail = detail_with_logs
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.dashboard = dashboard
        if not args.offline:
            threading.Thread(target=source.poll, daemon=True).start()
        if args.replay:
            threading.Thread(target=replay.poll, daemon=True).start()
        print(f"Demo: http://127.0.0.1:{args.port}/", flush=True)
        print("Replay uses only earlier evidence; no competition submissions. Ctrl+C stops the demo.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            source.stopped.set()
            replay.stop()
            lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
