#!/usr/bin/env python3
"""Tune A_MULT and SIGMA from the public leaderboard after every game. Submits nothing, no LLM.

Reads the transactions of all completed games (calibrate.py: 1 request/s, cached in
out/leaderboard/, only new games are fetched) and our own estimates from out/rounds.jsonl, replays
our pricing rule with other settings against the other teams' real a and b
(claim_to_fame/tuning.py) and writes the chosen setting to out/params.json. The runner reads
that file before every game when AUTO_TUNE=1 (default), so no restart is needed.
`watch.py --learn` runs this after every completed game, next to feedback.py's learning.json.

  uv run tune.py                  report only (cache only, nothing written)
  uv run tune.py --fetch --write  what watch.py --learn does after each game
  uv run tune.py --from-leaderboard 0.85   no rounds.jsonl here: our m = our public a / 0.85

Guardrails (tuning.choose): at least 8 lines, gain >= 1000 EUR (recent games weigh more),
not worse in 2 of 3 periods, at most 0.05 A_MULT and 0.30 SIGMA per update.
"""
import argparse
import datetime as dt
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
    return sorted(g["id"] for g in C.get("games", completed_only="true", page_size=1000)["items"] if g["id"] != 0)


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
        data = C.load_game(gid, team)
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
    """What the runner uses now: params.json (AUTO_TUNE=1, default), else .env, else defaults."""
    env = {**read_env(), **{k: v for k, v in os.environ.items() if k in ("A_MULT", "SIGMA", "AUTO_TUNE")}}
    p = load_params() if env.get("AUTO_TUNE", "1") != "0" else None
    if p:
        return {"a_mult": p["a_mult"], "sigma": p["sigma"]}
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


def write_params(new, report, path=None):
    path = Path(path or PARAMS)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {**new, "updated": dt.datetime.now().isoformat(timespec="seconds"), "decision": report["decision"],
           "lines": report["lines"], "games": report["games"], "fit": report.get("fit"),
           "best": report.get("best"), "current_score": report.get("current_score")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    os.replace(tmp, path)                     # atomic: the runner never reads half a file
    with open(path.parent / "tune.jsonl", "a") as f:
        f.write(json.dumps({**rec, "grid": report.get("grid")}) + "\n")
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
    new, report = learning.choose(rows, current)
    target = Path(params_path or PARAMS)
    if write and (new != current or target.exists()):
        write_params(new, report, target)       # first file only on a real change: .env stays in charge
    elif write:
        with open(target.parent / "tune.jsonl", "a") as f:
            f.write(json.dumps({"updated": dt.datetime.now().isoformat(timespec="seconds"), **new,
                                "decision": report["decision"], "lines": report["lines"]}) + "\n")
    return new, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default=os.environ.get("TEAM_NAME", "chineseModels"))
    ap.add_argument("--fetch", action="store_true", help="fetch new completed games (1 request/s)")
    ap.add_argument("--write", action="store_true", help="write out/params.json (the runner applies it)")
    ap.add_argument("--from-leaderboard", type=float, metavar="A_MULT",
                    help="games without rounds.jsonl record: m = our public a / A_MULT")
    args = ap.parse_args()
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
    print(("written to " + str(PARAMS)) if args.write else "(not written; --write to apply)")


if __name__ == "__main__":
    main()
