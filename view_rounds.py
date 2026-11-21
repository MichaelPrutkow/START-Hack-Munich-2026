#!/usr/bin/env python3
"""Show what the models said, per game and line, from out/rounds.jsonl. Read-only.

  uv run view_rounds.py                 last 3 games
  uv run view_rounds.py --game 9        one game
  uv run view_rounds.py --last 10 --brief

Per line and tier: coverage p, estimate m (after shared caps; raw in brackets), the single
votes, our a and b, and the clause the model gave. If out/leaderboard/ has the game cached
(watch.py or calibrate.py), the true t interval and whether our a was fair are shown too.
No network unless --fetch (then one cached leaderboard read per game via calibrate.py).
"""
import argparse, json, math
from pathlib import Path

import calibrate as C

OUT = Path(__file__).resolve().parent / "out"


def brackets(gid, team, fetch):
    f = C.CACHE / f"game_{gid:03d}.json"
    if not f.exists() and not fetch:
        return {}
    try:
        data = C.load_game(gid, team) if fetch else json.loads(f.read_text())
        return {i: d["t"] for i, d in C.analyse(data["tx"]).items()}
    except Exception:
        return {}


def tag(a, t):
    if not t or a is None:
        return ""
    lo, hi = t
    return "fair" if a <= lo else "UNFAIR" if a >= hi else "?"


def show(rec, team, brief, fetch):
    gid = rec["game"]
    T = brackets(gid, team, fetch)
    tiers = [n for n in ("fast", "deep", "esc") if n in (rec.get("est") or {})]
    usage = {k: f"{v.get('calls', 0)} calls/{v.get('prompt', 0) // 1000}k in"
             for k, v in (rec.get("usage") or {}).items()}
    print(f"\n=== game {gid}: winner {rec.get('winner')}, done after {rec.get('t_end')} s, "
          f"A_MULT {rec.get('a_mult')}, usage {usage}")
    final = (rec.get("tiers") or {}).get(rec.get("winner")) or {}
    for i, it in sorted(((int(k), v) for k, v in (rec.get("items") or {}).items())):
        t = T.get(i)
        ab = final.get(str(i)) or final.get(i)
        tt = f"t[{t[0]:.2f}, {t[1]:.2f})" if t else "t ?"
        sent = f"sent a {ab[0]:.2f} {tag(ab[0], t)} b {ab[1]:.2f}" if ab else "sent -"
        print(f"  #{i} {it['description'][:70]} ({it['quantity']:g} {it['unit']})  {tt}  {sent}")
        if brief:
            continue
        for name in tiers:
            e = (rec["est"][name] or {}).get(str(i)) or (rec["est"][name] or {}).get(i)
            if not e:
                continue
            raw = "" if abs((e.get("m_raw") or e["m"]) - e["m"]) < 0.01 else f" (raw {e['m_raw']:.0f})"
            votes = "/".join(f"{x or 0:.0f}" for _, x in e.get("votes", [])) or "-"
            ps = "/".join(f"{p:.2f}" for p, _ in e.get("votes", [])) or f"{e['p']:.2f}"
            ab = (rec.get("tiers", {}).get(name) or {}).get(str(i)) or [e.get("a"), e.get("b")]
            a_b = f"a {ab[0]:.2f} b {ab[1]:.2f}" if ab and ab[0] is not None else ""
            extra = "".join(f" {k} {e[k]:.0f}" for k in ("stated", "cap", "shared") if e.get(k))
            print(f"      {name:<4} p {e['p']:.2f} [{ps}]  m {e['m']:.0f}{raw} [{votes}]{extra}  {a_b}")
            if e.get("clause"):
                print(f"           {e['clause'][:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int)
    ap.add_argument("--last", type=int, default=3)
    ap.add_argument("--brief", action="store_true", help="only what was sent")
    ap.add_argument("--fetch", action="store_true", help="read missing leaderboard games (cached, 1 req/s)")
    ap.add_argument("--team", default="chineseModels")
    ap.add_argument("--rounds", default=str(OUT / "rounds.jsonl"))
    args = ap.parse_args()
    recs = {}
    for ln in Path(args.rounds).read_text().splitlines():
        r = json.loads(ln)
        recs[r["game"]] = r                     # a replayed game keeps its last record
    sel = [recs[args.game]] if args.game is not None else [recs[g] for g in sorted(recs)[-args.last:]]
    for r in sel:
        show(r, args.team, args.brief, args.fetch)


if __name__ == "__main__":
    main()
