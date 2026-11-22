#!/usr/bin/env python3
"""Offline evaluation of the LLM step on already played games. Submits NOTHING.

Fetches the key of a game that has already started (read-only GET), decrypts the case into
out/cases/, runs an explicit --old-ref revision and the current prompt, and scores
every line against the public leaderboard brackets t in [lo, hi) (calibrate.py, cached).
Waits outside the 60 s game windows so it never competes with the live runner for the keys.

  uv run eval_cases.py --games 1 2 3 4 5 [--tiers fast deep] [--deep-votes 3] --old-ref <baseline-commit> [--holdout-from 10]

Not a runner: no lockfile, no PUT. Results go to out/eval/.
"""
import argparse
import importlib.util
import functools
import hashlib
import inspect
import os
import threading
from contextlib import contextmanager, nullcontext
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


_IMPORT_LOCK = threading.RLock()


class RevisionModule:
    """Keep lazy imports in a historical revision isolated on every public call.

    Evaluation calls revisions sequentially. The process-wide import lock also
    prevents two historical contexts from swapping sys.modules simultaneously.
    A snapshot's eager and later lazy imports remain cached within that revision.
    """
    def __init__(self, module, path, revision):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_modules", {})
        object.__setattr__(self, "revision", revision)

    @contextmanager
    def context(self):
        def domain(name):
            return name == "claim_to_fame" or name.startswith("claim_to_fame.") or name == "runner"
        with _IMPORT_LOCK:
            saved = {k: v for k, v in sys.modules.items() if domain(k)}
            saved_path = sys.path[:]
            try:
                for name in saved:
                    del sys.modules[name]
                sys.modules.update(self._modules)
                sys.modules["runner"] = self._module
                sys.path.insert(0, str(self._path))
                yield
            finally:
                self._modules.update({k: v for k, v in sys.modules.items() if domain(k)})
                for name in list(sys.modules):
                    if domain(name):
                        del sys.modules[name]
                sys.modules.update(saved)
                sys.path[:] = saved_path

    def __getattr__(self, name):
        value = getattr(self._module, name)
        if inspect.isfunction(value) or inspect.isclass(value):
            @functools.wraps(value)
            def isolated(*args, **kwargs):
                with self.context():
                    return value(*args, **kwargs)
            return isolated
        return value

    def __setattr__(self, name, value):
        setattr(self._module, name, value)


def load_old(ref):
    """Load a complete immutable source revision, including later lazy imports."""
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
    spec = importlib.util.spec_from_file_location(f"runner_old_{revision}", snapshot / "runner.py")
    mod = importlib.util.module_from_spec(spec)
    isolated = RevisionModule(mod, snapshot, revision)
    # Dataclasses and similar libraries inspect the defining module while importing.
    sys.modules[spec.name] = mod
    try:
        with isolated.context():
            spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return isolated


def refresh_settings(mod):
    """Apply .env pricing values after imports, matching the live runner startup."""
    if hasattr(mod, "configure_pricing"):
        mod.configure_pricing()
        return
    for name in ("A_MULT", "SIGMA", "DISAGREE"):
        if hasattr(mod, name):
            setattr(mod, name, C.finite_number(os.environ.get(name, getattr(mod, name)), name))


def matching_tier(mod, name, deep_votes=None):
    """Use identical model, reasoning, timeout and requested votes for both revisions."""
    template = next((t for t in R.build_tiers() if t.name == name), None)
    tier = next((t for t in mod.build_tiers() if t.name == name), None)
    if template is None or tier is None:
        raise ValueError(f"tier {name!r} must be enabled in both revisions")
    for attr in ("model", "budget", "votes"):
        setattr(tier, attr, getattr(template, attr))
    tier.extra = dict(template.extra)
    if name == "deep" and deep_votes is not None:
        tier.votes = deep_votes
    return tier


def wait_outside_windows(games):
    while True:
        now = time.time()
        busy = [s for _, s in games if s - 15 <= now <= s + R.ROUND_SECONDS + 5]
        if not busy:
            return
        time.sleep(min(30.0, busy[0] + R.ROUND_SECONDS + 6 - now))


def verdict(a, lo, hi):
    return "fair" if a <= lo else "UNFAIR" if a >= hi else "?"


def run(mod, msgs, items, fb, tier, shared=None):
    """Replay a single tier, retaining rule fallback for omitted invoice lines."""
    with mod.context() if isinstance(mod, RevisionModule) else nullcontext():
        return _run(mod, msgs, items, fb, tier, shared)


def _run(mod, msgs, items, fb, tier, shared):
    t0 = time.time()
    votes, usage = mod.assess(msgs, min(55.0, tier.budget), tier)
    dt = time.time() - t0
    by_line = {i: [v[i] for v in votes if i in v] for i in items}
    estimator = getattr(getattr(mod, "pricing", None), "estimate_votes", None)
    details_by_line = {i: estimator(vs, disagree=mod.DISAGREE) for i, vs in by_line.items() if vs} if estimator else {}
    raw = {i: mod.aggregate(vs) for i, vs in by_line.items()
           if vs and (not estimator or details_by_line[i]["valid_count"] > 0)}
    use_shared = hasattr(mod, "apply_shared_caps") and all(len(agg) >= 5 for agg in raw.values())
    if shared is False:
        use_shared = False
    aggs = mod.apply_shared_caps(raw) if use_shared else raw
    out = {i: {"p": None, "m": fb[i], "m_raw": fb[i], "a": mod.floor_c(fb[i]),
               "b": mod.ceil_c(fb[i]), "clauses": [], "source": "rules"} for i in items}
    for i, agg in aggs.items():
        kwargs = {}
        details = details_by_line.get(i, {})
        if "uncertainty" in inspect.signature(mod.price).parameters and details:
            kwargs["uncertainty"] = details["log_spread"]
        a, b = mod.price(*agg[:4], fb[i], **kwargs)
        C.finite_number(a, "charge_price")
        C.finite_number(b, "acceptance_limit")
        out[i] = {"p": agg[0], "m": agg[1], "m_raw": raw[i][1], "a": a, "b": b,
                  "stated": agg[2], "cap": agg[3], "uncertainty": details,
                  "clauses": [v[i].get("clause", "") for v in votes if i in v][:1], "source": tier.name}
    return out, dt, usage, len(votes)


def score_lines(lines, brackets, team):
    """Score the prices returned by replay, never reconstruct them from m/p."""
    scores = [C.score_submission(brackets[i], team, x["a"], x["b"]) for i, x in lines.items() if i in brackets]
    lo, hi = sum(s["lower"] for s in scores), sum(s["upper"] for s in scores)
    return {"lower": lo, "upper": hi, "estimate": (lo + hi) / 2 if math.isfinite(lo) and math.isfinite(hi) else None,
            "lines": len(scores), "missing_lines": sorted(set(lines) - set(brackets))}


def chronological_split(game, holdout_from):
    return "holdout" if holdout_from is not None and game >= holdout_from else "train"


def source_hash():
    """Identify the current source, including uncommitted changes, without case data."""
    digest = hashlib.sha256()
    for path in [ROOT / "runner.py", *sorted((ROOT / "claim_to_fame").glob("*.py"))]:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, nargs="+", required=True)
    ap.add_argument("--tiers", nargs="+", default=["deep"])
    ap.add_argument("--deep-votes", type=int, help="same vote budget for both revisions; default uses live config")
    baseline = ap.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--old-ref", help="explicit Git baseline (resolved and recorded as a commit)")
    baseline.add_argument("--skip-old", action="store_true")
    ap.add_argument("--holdout-from", type=int, help="first chronologically held-out game; choose before running")
    ap.add_argument("--team", default="chineseModels")
    args = ap.parse_args()
    if args.deep_votes is not None and args.deep_votes < 1:
        ap.error("--deep-votes must be positive")
    R.load_env()
    refresh_settings(R)
    R.setup_logging()
    R.POOL = R.KeyPool()
    old = None if args.skip_old else load_old(args.old_ref)
    if old:
        old.POOL = R.POOL
        refresh_settings(old)
        for setting in ("A_MULT", "SIGMA", "DISAGREE"):
            if hasattr(old, setting):
                setattr(old, setting, getattr(R, setting))
    games = R.list_games()
    starts = dict(games)
    if any(gid <= 0 or gid not in starts or starts[gid] + R.ROUND_SECONDS > time.time() for gid in args.games):
        ap.error("evaluation requires completed positive game ids")
    results = []
    current_source = source_hash()
    for gid in sorted(set(args.games)):
        L = C.analyse(C.load_game(gid, args.team)["tx"])
        d = R.decrypt(gid, R.fetch_key(gid, time.time() + 5))
        items = R.parse_items(d)
        fb = {i: R.rule_value(it) for i, it in items.items()}
        policy, desc = R.read_text(d, "policy*.txt"), R.read_text(d, "description*.txt")
        variants = [("new", R, R.build_prompt(policy, desc, items))]
        if old:
            variants.insert(0, ("old", old, old.build_prompt(policy, desc, items)))
        for tname in args.tiers:
            for label, mod, msgs in variants:
                tier = matching_tier(mod, tname, args.deep_votes)
                wait_outside_windows(games)
                try:
                    out, dt, usage, nv = run(mod, msgs, items, fb, tier)
                    score = score_lines(out, L, args.team)
                except Exception as e:
                    print(f"G{gid} {tname} {label}: {type(e).__name__} {e}")
                    continue
                print(f"\nG{gid} {tname:<4} {label:<3} {nv}/{tier.votes} votes {dt:5.1f}s tokens {usage}"
                      f"  net bounds [{score['lower']:.0f}, {score['upper']:.0f}]")
                for i, x in sorted(out.items()):
                    lo, hi = L.get(i, {"t": [0, math.inf]})["t"]
                    raw = "" if abs(x["m"] - x["m_raw"]) < 0.01 else " (raw %.0f)" % x["m_raw"]
                    clause = x["clauses"][0][:60] if x["clauses"] else ""
                    probability = "-" if x["p"] is None else f"{x['p']:.2f}"
                    print(f"  {i:>2} t[{lo:8.2f},{hi:8.2f})  p {probability}  m {x['m']:8.2f}{raw}"
                          f"  a {x['a']:8.2f} {verdict(x['a'], lo, hi):<6} b {x['b']:8.2f}  {clause}")
                results.append({"game": gid, "split": chronological_split(gid, args.holdout_from),
                                "tier": tname, "prompt": label, "revision": old.revision if label == "old" else "working-tree",
                                "source_sha256": current_source if label == "new" else None,
                                "votes": nv, "requested_votes": tier.votes, "secs": round(dt, 1),
                                "model": tier.model, "pricing": {k: getattr(mod, k, None) for k in ("A_MULT", "SIGMA", "DISAGREE")},
                                "uncertainty_pricing": os.environ.get("PRICING_UNCERTAINTY", "0") == "1",
                                "usage": usage, "net_bounds": score, "lines": out})
    EVAL.mkdir(parents=True, exist_ok=True)
    (EVAL / f"eval_{int(time.time())}.json").write_text(json.dumps(results, indent=1, default=str))
    print("\nCounterfactual payoff bounds by split/tier/prompt; midpoint is not expected profit:")
    for split in ("train", "holdout"):
        for tname in args.tiers:
            relevant = [r for r in results if r["split"] == split and r["tier"] == tname]
            paired = {r["game"] for r in relevant if r["votes"] == r["requested_votes"] and not r["net_bounds"]["missing_lines"]}
            if old:
                paired = {gid for gid in paired if len([r for r in relevant if r["game"] == gid and
                          r["votes"] == r["requested_votes"] and not r["net_bounds"]["missing_lines"]]) == 2}
            for label in ("old", "new") if old else ("new",):
                rs = [r for r in relevant if r["game"] in paired and r["prompt"] == label]
                if rs:
                    lo = sum(r["net_bounds"]["lower"] for r in rs)
                    hi = sum(r["net_bounds"]["upper"] for r in rs)
                    print(f"  {split} {tname} {label}: [{lo:.0f}, {hi:.0f}], {len(rs)} comparable games")
            excluded = len({r["game"] for r in relevant} - paired)
            if excluded:
                print(f"  {split} {tname}: excluded {excluded} incomplete or unequal-vote games")
    print("Single-tier replay with rule fallback; no live timing/merged-tier performance claim. "
          "Overlapping bounds do not establish improvement.")
    if args.holdout_from is None:
        print("No chronological holdout selected: all results are descriptive in-sample observations.")


if __name__ == "__main__":
    main()
