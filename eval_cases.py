#!/usr/bin/env python3
"""Offline evaluation of the LLM step on already played games. Submits NOTHING.

Fetches the key of a game that has already started (read-only GET), decrypts the case into
out/cases/, runs the old prompt (git HEAD or --old-ref) and the current prompt, and scores
every line against the public leaderboard brackets t in [lo, hi) (calibrate.py, cached).
Waits outside the 60 s game windows so it never competes with the live runner for the keys.

  uv run eval_cases.py --games 1 2 3 4 5 [--tiers fast deep] [--deep-votes 3] [--old-ref HEAD]

Not a runner: no lockfile, no PUT. Results go to out/eval/.
"""
import argparse
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import runner as R
import calibrate as C

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "out" / "eval"


def load_old(ref):
    """Load one complete source revision without mixing old and current domain modules."""
    EVAL.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    paths = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", revision, "--", "runner.py", "claim_to_fame"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    snapshot = Path(tempfile.mkdtemp(prefix=f"runner_{revision[:8]}_", dir=EVAL))
    for name in paths:
        if name.endswith(".py"):
            p = snapshot / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(subprocess.run(
                ["git", "show", f"{revision}:{name}"], cwd=ROOT, check=True, capture_output=True,
            ).stdout)
    p = snapshot / "runner.py"
    spec = importlib.util.spec_from_file_location("runner_old", p)
    mod = importlib.util.module_from_spec(spec)

    def is_domain(name):
        return name == "claim_to_fame" or name.startswith("claim_to_fame.")

    # Import eagerly from the snapshot, then restore the current runner's import cache.
    # Existing function/module references retain their own revision's globals.
    saved_modules = {k: v for k, v in sys.modules.items() if is_domain(k)}
    saved_path = sys.path[:]
    try:
        for name in saved_modules:
            del sys.modules[name]
        sys.path.insert(0, str(snapshot))
        spec.loader.exec_module(mod)
    finally:
        for name in list(sys.modules):
            if is_domain(name):
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
    return mod


def wait_outside_windows(games):
    while True:
        now = time.time()
        busy = [s for _, s in games if s - 15 <= now <= s + R.ROUND_SECONDS + 5]
        if not busy:
            return
        time.sleep(busy[0] + R.ROUND_SECONDS + 6 - now)


def verdict(a, lo, hi):
    return "fair" if a <= lo else "UNFAIR" if a >= hi else "?"


def run(mod, msgs, items, fb, tier, shared):
    t0 = time.time()
    votes, usage = mod.assess(msgs, 55.0, tier)
    dt = time.time() - t0
    raw = {i: mod.aggregate([v[i] for v in votes if i in v]) for i in items if any(i in v for v in votes)}
    aggs = mod.apply_shared_caps(raw) if shared else raw
    out = {}
    for i, agg in aggs.items():
        a, b = mod.price(*agg[:4], fb[i])
        out[i] = {"p": agg[0], "m": agg[1], "m_raw": raw[i][1], "a": a, "b": b,
                  "clauses": [v[i].get("clause", "") for v in votes if i in v][:1]}
    return out, dt, usage, len(votes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, nargs="+", required=True)
    ap.add_argument("--tiers", nargs="+", default=["deep"])
    ap.add_argument("--deep-votes", type=int, default=1)
    ap.add_argument("--old-ref", default="HEAD")
    ap.add_argument("--skip-old", action="store_true")
    ap.add_argument("--team", default="chineseModels")
    args = ap.parse_args()
    R.load_env()
    R.setup_logging()
    R.POOL = R.KeyPool()
    old = None if args.skip_old else load_old(args.old_ref)
    if old:
        old.POOL = R.POOL
    games = R.list_games()
    results = []
    for gid in args.games:
        L = C.analyse(C.load_game(gid, args.team)["tx"])
        d = R.decrypt(gid, R.fetch_key(gid, time.time() + 5))
        items = R.parse_items(d)
        fb = {i: R.rule_value(it) for i, it in items.items()}
        policy, desc = R.read_text(d, "policy*.txt"), R.read_text(d, "description*.txt")
        variants = [("new", R, R.build_prompt(policy, desc, items), True)]
        if old:
            variants.insert(0, ("old", old, old.build_prompt(policy, desc, items), False))
        for tname in args.tiers:
            for label, mod, msgs, shared in variants:
                tier = [t for t in mod.build_tiers() if t.name == tname][0]
                if tname == "deep" and label == "new":
                    tier.votes = args.deep_votes
                wait_outside_windows(games)
                try:
                    out, dt, usage, nv = run(mod, msgs, items, fb, tier, shared)
                except Exception as e:
                    print(f"G{gid} {tname} {label}: {type(e).__name__} {e}")
                    continue
                net = 0.0
                for i, x in out.items():
                    if i in L:
                        net += C.backtest(L[i], args.team, x["m"], x["p"], R.A_MULT, R.SIGMA)
                print(f"\nG{gid} {tname:<4} {label:<3} {nv} votes {dt:5.1f}s tokens {usage}  backtest net {net:9.0f}")
                for i, x in sorted(out.items()):
                    lo, hi = L.get(i, {"t": [0, math.inf]})["t"]
                    raw = "" if abs(x["m"] - x["m_raw"]) < 0.01 else " (raw %.0f)" % x["m_raw"]
                    clause = x["clauses"][0][:60] if x["clauses"] else ""
                    print(f"  {i:>2} t[{lo:8.2f},{hi:8.2f})  p {x['p']:.2f}  m {x['m']:8.2f}{raw}"
                          f"  a {x['a']:8.2f} {verdict(x['a'], lo, hi):<6} b {x['b']:8.2f}  {clause}")
                results.append({"game": gid, "tier": tname, "prompt": label, "votes": nv, "secs": round(dt, 1),
                                "usage": usage, "net": round(net), "lines": out})
    EVAL.mkdir(parents=True, exist_ok=True)
    (EVAL / f"eval_{int(time.time())}.json").write_text(json.dumps(results, indent=1, default=str))
    print("\nsum backtest net by tier/prompt:")
    for key in sorted({(r["tier"], r["prompt"]) for r in results}):
        rs = [r for r in results if (r["tier"], r["prompt"]) == key]
        print(f"  {key[0]:<4} {key[1]:<3}: {sum(r['net'] for r in rs):9.0f}  over {len(rs)} games,"
              f" mean {sum(r['secs'] for r in rs) / len(rs):.1f}s")


if __name__ == "__main__":
    main()
