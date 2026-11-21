#!/usr/bin/env python3
"""Watchdog and per-game report from the public leaderboard. Read-only, no LLM, submits nothing.

After every game it waits until the leaderboard marks it completed, fetches the transactions
once (calibrate.py: 1 request/s, cached in out/leaderboard/) and reports our net, our rank and,
per line, our a against the t interval. It raises a macOS alarm (notification + sound) when we
apparently did not submit or the game never shows up as completed.

  uv run watch.py --team chineseModels            follow all upcoming games
  uv run watch.py --team chineseModels --once 7   report one finished game and exit

Safe on any machine: it never touches runner.py, the game API keys or OpenAI.
Log: out/watch.log (one line per game: game, net, rank, fair/unfair counts).
"""
import argparse, collections, datetime as dt, json, math, subprocess, sys, time
from pathlib import Path

import requests

import calibrate as C

OUT = Path(__file__).resolve().parent / "out"
LATE = 15 * 60          # alarm if a game is not completed this long after its start


def update_learning(team, rounds, learning_file):
    """Runs after settlement publication, never inside the submitting runner."""
    if not rounds.exists():
        print("Learning skipped: rounds.jsonl is absent; use --learn on the runner machine.", flush=True)
        return
    from claim_to_fame.feedback import build_state, write_state
    state = build_state(rounds, C.CACHE, team)
    write_state(state, learning_file, report_path=learning_file.with_name("learning-report.md"))
    ready = sum(c["status"] == "validated" for c in state["calibrations"])
    print(f"Learning updated: {len(state['games'])} reconciled games; {ready} validated calibrations.", flush=True)


def alarm(title, text, loud):
    print(f"\a{'!!! ' if loud else ''}{title}: {text}", flush=True)
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)}' + \
             (' sound name "Sosumi"' if loud else "")
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        if loud:
            subprocess.run(["say", title], timeout=10, capture_output=True)
    except Exception:
        pass


def games():
    j = requests.get(f"{C.API}/games", params={"page_size": 1000}, timeout=10).json()
    return {g["id"]: (dt.datetime.fromisoformat(g["start_time"].replace("Z", "+00:00")).timestamp(),
                      g.get("status")) for g in j["items"] if g["id"] != 0}


def report(gid, team):
    data = C.load_game(gid, team)
    tx = data["tx"]
    if not tx:
        return None
    net = collections.Counter()
    for x in tx:
        net[x["issuer"]] += x["amount"]
        net[x["reviewer"]] -= x["amount"]
    ranking = sorted(net.items(), key=lambda kv: -kv[1])
    rank = [t for t, _ in ranking].index(team) + 1 if team in net else None
    income = sum(x["amount"] for x in tx if x["issuer"] == team)
    accepted = sum(1 for x in tx if x["reviewer"] == team and x["accepted"])
    L = C.analyse(tx)
    lines, counts = [], collections.Counter()
    for i, d in sorted(L.items()):
        lo, hi = d["t"]
        a = d["a"].get(team)
        tag = "?" if a is None else "fair" if a <= lo else "UNFAIR" if a >= hi else "?"
        if a is None and d["fair"].get(team) is False:
            tag = "UNFAIR"                      # rejected everywhere as inflated, amount unknown
        counts[tag] += 1
        b = d["b"].get(team)
        lines.append(f"  {i:>2} t[{lo:9.2f}, {hi:9.2f})  our a {a if a is not None else '-':>9}  {tag:<6}"
                     f"  our b in {[round(v, 2) for v in b] if b else '-'}  best fair a {lo:.2f}")
    return {"game": gid, "net": round(net[team], 2), "rank": rank, "teams": len(net), "top": ranking[0],
            "income": round(income, 2), "accepted_as_reviewer": accepted, "counts": dict(counts), "lines": lines}


def show(r, team):
    head = (f"game {r['game']}: {team} net {r['net']:+.0f}, rank {r['rank']}/{r['teams']}"
            f" (top {r['top'][0]} {r['top'][1]:+.0f}); our claims {r['counts']}")
    print(f"\n{dt.datetime.now():%H:%M:%S} {head}")
    print("\n".join(r["lines"]), flush=True)
    with open(OUT / "watch.log", "a") as f:
        f.write(json.dumps({k: v for k, v in r.items() if k != "lines"}) + "\n")
    silent = r["income"] == 0 and r["accepted_as_reviewer"] == 0
    if silent:
        alarm(f"Spiel {r['game']}: keine Abgabe?", "Keine Einnahmen und nichts angenommen. Runner auf Philips Mac prüfen!", True)
    else:
        alarm(f"Spiel {r['game']}: {r['net']:+.0f}", f"Rang {r['rank']}/{r['teams']}, Forderungen {r['counts']}", False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default="chineseModels")
    ap.add_argument("--once", type=int, help="report this finished game and exit")
    ap.add_argument("--learn", action="store_true", help="update private learning from local runner logs after reporting")
    ap.add_argument("--rounds", type=Path, default=OUT / "rounds.jsonl")
    ap.add_argument("--learning-file", type=Path, default=OUT / "learning.json")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.once is not None:
        r = report(args.once, args.team)
        if r:
            show(r, args.team)
            if args.learn:
                update_learning(args.team, args.rounds, args.learning_file)
        else:
            print(f"game {args.once}: no transactions yet")
        return
    done = set()
    try:
        done = {json.loads(l)["game"] for l in (OUT / "watch.log").read_text().splitlines()}
    except (FileNotFoundError, ValueError, KeyError):
        pass
    alarmed = set()
    print(f"watching {args.team}; already reported: {sorted(done)[-5:]}", flush=True)
    if args.learn:
        update_learning(args.team, args.rounds, args.learning_file)
    while True:
        try:
            G = games()
        except Exception as e:
            print(f"{dt.datetime.now():%H:%M:%S} leaderboard: {type(e).__name__}", flush=True)
            time.sleep(60)
            continue
        now = time.time()
        for gid, (start, status) in sorted(G.items()):
            if gid in done or start > now - 70:
                continue
            if status == "completed":
                try:
                    r = report(gid, args.team)
                except Exception as e:
                    print(f"game {gid}: {type(e).__name__} {e}", flush=True)
                    continue
                if r:
                    show(r, args.team)
                    done.add(gid)
                    if args.learn:
                        try:
                            update_learning(args.team, args.rounds, args.learning_file)
                        except Exception as e:
                            print(f"Learning update failed: {type(e).__name__}; prior artifact retained.", flush=True)
            elif now - start > LATE and gid not in alarmed:
                alarm(f"Spiel {gid} nicht abgeschlossen", "Leaderboard zeigt das Spiel 15 min nach Start nicht als fertig.", True)
                alarmed.add(gid)
        pending = [g for g, (s, _) in G.items() if g not in done and s <= now]
        upcoming = [s for s, _ in G.values() if s > now]
        nxt = min(upcoming) if upcoming else now + 600
        wake = 45.0 if pending else nxt + 90 - time.time()     # started but not reported: poll
        time.sleep(max(30.0, min(wake, 600)))


if __name__ == "__main__":
    main()
