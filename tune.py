#!/usr/bin/env python3
"""Exploratory historical A_MULT/SIGMA replay. Submits nothing, no LLM.

Reads the transactions of all completed games (calibrate.py: 1 request/s, cached in
out/leaderboard/, only new games are fetched) and our own estimates from out/rounds.jsonl, replays
our pricing rule with other settings against the other teams' real a and b
(claim_to_fame/tuning.py). Optional out/params.json stores a proposal only; the runner does
not apply it. This older replay imputes unknown prices and reuses evaluation data for selection.
Automatic learning instead uses feedback.py's censored bounds and chronological holdout.

  uv run tune.py                  report only (cache only, nothing written)
  uv run tune.py --fetch --write  explicitly fetch data and save an exploratory proposal
  uv run tune.py --from-leaderboard 0.85   no rounds.jsonl here: our m = our public a / 0.85

Guardrails (tuning.choose): at least 8 lines, gain >= 1000 EUR (recent games weigh more),
not worse in 2 of 3 periods, at most 0.05 A_MULT and 0.30 SIGMA per update.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import time
from pathlib import Path

import calibrate as C
from claim_to_fame import tuning as learning

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
PARAMS = OUT / "params.json"


def completed_games():
    return sorted(g["id"] for g in C.completed_games())


def cached_games():
    return sorted(int(p.stem.split("_")[1]) for p in C.CACHE.glob("game_*.json"))


def collect(team, fetch, deadline=None):
    """{gid: tx} for completed games; fetches missing ones (1 req/s) until `deadline`."""
    ids = completed_games() if fetch else cached_games()
    out = {}
    for gid in ids:
        f = C.CACHE / f"game_{gid:03d}.json"
        if not f.exists() and (not fetch or (deadline and time.time() > deadline)):
            continue
        if fetch:
            data = C.load_game(gid, team, deadline=deadline)
        else:
            try:
                data = C.validate_game(C.strict_json(f.read_text()), gid)
            except (OSError, ValueError, C.IncompleteGame):
                continue  # Cache-only analysis must never refetch an invalid cache.
        if data["tx"]:
            out[gid] = data["tx"]
    return out


def read_rounds(path):
    recs = {}
    if Path(path).exists():
        for ln in Path(path).read_text().splitlines():
            try:
                r = json.loads(ln)
                recs[int(r["game"])] = r          # last record per game wins
            except (ValueError, KeyError, TypeError):
                pass
    return recs


def from_leaderboard(games, team, a_mult):
    """Fallback without rounds.jsonl: our m = our public a / a_mult, p from whether we accepted anything."""
    recs = {}
    for gid, tx in games.items():
        L = learning.analyse(tx)
        tiers = {}
        for i, d in L.items():
            a = d["a"].get(team)
            if not a:
                continue
            b_hi = d["b"].get(team, [0.0, math.inf])[1]
            tiers[str(i)] = [a, 0.0 if b_hi < 0.5 * a else a]
        if tiers:
            recs[gid] = {"game": gid, "winner": "lb", "tiers": {"lb": tiers}, "a_mult": a_mult}
    return recs


def read_env(path=ROOT / ".env"):
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k and not k.startswith("#") and v.strip():
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def current_settings():
    """Manual runner defaults; active per-tier learning is reported by learn.py --status."""
    env = {**read_env(), **{k: v for k, v in os.environ.items() if k in ("A_MULT", "SIGMA", "AUTO_TUNE")}}
    return {"a_mult": float(env.get("A_MULT", "0.85")), "sigma": float(env.get("SIGMA", "0.35"))}


def load_params(path=None):
    """Validated content of out/params.json or None."""
    try:
        p = json.loads(Path(path or PARAMS).read_text())
        a, s = float(p["a_mult"]), float(p["sigma"])
        if not (math.isfinite(a) and math.isfinite(s)):
            return None
        return {**p, "a_mult": learning.clamp("a_mult", a), "sigma": learning.clamp("sigma", s)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def data_state(rows):
    """Fingerprint of what a decision saw: games, lines and our estimates (for tune.jsonl)."""
    key = sorted((r["g"], r["i"], round(r["m"], 2), round(r["p"], 3)) for r in rows)
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:12]


def last_logged(path):
    """Last entry of tune.jsonl next to params.json, or None."""
    try:
        lines = (Path(path).parent / "tune.jsonl").read_text().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


def log_decision(rec, path):
    with open(Path(path).parent / "tune.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def write_params(new, report, path=None):
    path = Path(path or PARAMS)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {**new, "updated": dt.datetime.now().isoformat(timespec="seconds"), "decision": report["decision"],
           "lines": report["lines"], "games": report["games"], "data": report.get("data"),
           "current": report.get("current"), "step": report.get("step"), "fit": report.get("fit"),
           "best": report.get("best"), "current_score": report.get("current_score")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    os.replace(tmp, path)                     # atomic: the runner never reads half a file
    log_decision({**rec, "grid": report.get("grid")}, path)
    return rec


def update(team, *, fetch=True, write=True, current=None, deadline=None, rounds=None, lb_a_mult=None,
           rounds_path=None, params_path=None):
    games = collect(team, fetch, deadline)
    recs = read_rounds(rounds_path or OUT / "rounds.jsonl") if rounds is None else rounds
    if lb_a_mult:
        recs = {**from_leaderboard(games, team, lb_a_mult), **{g: r for g, r in recs.items() if r.get("est")}}
    rows = learning.build(games, recs, team)
    if current is None:
        current = current_settings() if params_path is None else \
            {k: v for k, v in (load_params(params_path) or current_settings()).items() if k in ("a_mult", "sigma")}
    target = Path(params_path or PARAMS)
    state = data_state(rows)
    prev = last_logged(target) if write else None
    if prev and prev.get("data") == state:
        # Same games and estimates as the last decision (e.g. the watcher restarted): never step twice.
        cur = {"a_mult": learning.clamp("a_mult", current["a_mult"]), "sigma": learning.clamp("sigma", current["sigma"])}
        return cur, {"lines": len(rows), "games": sorted({r["g"] for r in rows}), "data": state, "current": cur,
                     "decision": f"keep: no new data since {prev.get('updated', '?')}"}
    new, report = learning.choose(rows, current)
    report["data"] = state
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
    if write and (new != report["current"] or target.exists()):
        write_params(new, report, target)       # first file only on a real change: .env stays in charge
    elif write:
        log_decision({"updated": dt.datetime.now().isoformat(timespec="seconds"), **new,
                      "decision": report["decision"], "lines": report["lines"], "games": report["games"],
                      "data": state, "step": report.get("step"), "current_score": report.get("current_score")}, target)
    return new, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default=os.environ.get("TEAM_NAME", "chineseModels"))
    ap.add_argument("--fetch", action="store_true", help="fetch new completed games (1 request/s)")
    ap.add_argument("--write", action="store_true", help="write exploratory out/params.json (never applied by runner)")
    ap.add_argument("--from-leaderboard", type=float, metavar="A_MULT",
                    help="games without rounds.jsonl record: m = our public a / A_MULT")
    ap.add_argument("--shadow", action="store_true",
                    help="score the logged shadow variants (a -20 %%, b <= 0.8 a) against what was sent")
    args = ap.parse_args()
    if args.shadow:
        res = learning.shadow_report(collect(args.team, args.fetch), read_rounds(OUT / "rounds.jsonl"), args.team)
        for name, r in sorted(res.items()):
            print(f"{name:<14} games {r['games']}  changed lines {r['lines']}  "
                  f"net change vs sent [{r['lower']:+.0f}, {r['upper']:+.0f}]")
        if not res:
            print("no rounds with shadow variants yet")
        return
    new, rep = update(args.team, fetch=args.fetch, write=args.write, lb_a_mult=args.from_leaderboard)
    print(f"lines {rep['lines']} from games {rep['games']}")
    if rep.get("fit"):
        f = rep["fit"]
        print(f"t/m censored fit over {f['n']} covered lines: median {f['median_t_over_m']}, log-sd {f['log_sd']}")
    if rep.get("grid"):
        sig = sorted({float(k.split('/')[1]) for k in rep["grid"]})
        print("backtest, weighted (rows A_MULT, cols SIGMA):" + "".join(f"{s:>9.2f}" for s in sig))
        for am in sorted({float(k.split('/')[0]) for k in rep["grid"]}):
            print(f"  {am:.2f}" + " " * 39 + "".join(f"{rep['grid'][f'{am:.2f}/{s:.2f}']:>9}" for s in sig))
    print(rep["decision"])
    print(("proposal written to " + str(PARAMS)) if args.write else "(not written; --write saves a proposal)")
    print("Exploratory estimates only; inspect validated runner learning with: uv run learn.py --status")


if __name__ == "__main__":
    main()
