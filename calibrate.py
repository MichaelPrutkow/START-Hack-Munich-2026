#!/usr/bin/env python3
"""Read-only calibration from the public leaderboard. Submits nothing.

Organizers allowed scraping the leaderboard (21.11.), but not hammering it: every completed
game is fetched ONCE (one request per team and page, 1 s apart) and cached in
out/leaderboard/. Re-runs only fetch new games.

  uv run calibrate.py --team "<our team name>" [--rounds out/rounds.jsonl]

Per line item, from the public transactions (official payoffs):
  rejected with amount > 0  -> fair, a = amount / 1.5      (so t >= a)
  rejected with amount = 0  -> fraud                       (so t <  a)
  accepted                  -> amount = min(a, c), and the reviewer's b >= a
  t interval = [max fair a, min fraud a)
Then: our a and b against that interval, and, if rounds.jsonl has our estimates (p, m),
the ratio t/m (bias -> A_MULT, spread -> SIGMA) and a backtest of the runner's a/b rule over
A_MULT x SIGMA against the teams' real a and b.
"""
import argparse, json, math, statistics, time
from pathlib import Path

import requests

API = "https://c2f.public.quantco.cloud/leaderboard/api"
CACHE = Path(__file__).resolve().parent / "out" / "leaderboard"
PAUSE = 1.0


def get(path, **params):
    time.sleep(PAUSE)
    r = requests.get(f"{API}/{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def team_tx(gid, team):
    items, page = [], 1
    while True:
        j = get("transactions", game_id=gid, team=team, page=page)
        items += j.get("items", [])
        if page >= j.get("total_pages", 1):
            return items
        page += 1


def load_game(gid, seed):
    f = CACHE / f"game_{gid:03d}.json"
    if f.exists():
        return json.loads(f.read_text())
    first = team_tx(gid, seed)
    teams = sorted({x["issuer"] for x in first} | {x["reviewer"] for x in first})
    tx = [x for x in first if x["issuer"] == seed]
    for t in teams:
        if t != seed:
            tx += [x for x in team_tx(gid, t) if x["issuer"] == t]
    data = {"game": gid, "teams": teams, "tx": tx}
    if not tx:                                  # wrong team name or not published yet: no cache
        print(f"game {gid}: no transactions for team {seed!r} (name exactly as on the leaderboard?)")
        return data
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data))
    return data


def analyse(tx):
    """-> {line: {"a": {issuer: a}, "fair": {issuer: bool|None}, "b": {reviewer: [lo, hi)},
                  "t": [lo, hi)}}"""
    L = {}
    for x in tx:
        d = L.setdefault(x["line_item_index"], {"a": {}, "exact": set(), "fair": {}, "b": {}})
        iss, amt = x["issuer"], float(x["amount"] or 0)
        if not x["accepted"] and amt > 0:
            d["a"][iss] = amt / 1.5                      # exact
            d["exact"].add(iss)
            d["fair"][iss] = True
        elif not x["accepted"]:
            d["fair"][iss] = False
        elif iss not in d["exact"]:
            d["a"][iss] = max(d["a"].get(iss, 0.0), amt)  # min(a, c): exact unless capped fraud
    for x in tx:                                         # reviewer b bounds
        d = L[x["line_item_index"]]
        a = d["a"].get(x["issuer"])
        if a is None:
            continue
        lo, hi = d["b"].setdefault(x["reviewer"], [0.0, math.inf])
        d["b"][x["reviewer"]] = [max(lo, a), hi] if x["accepted"] else [lo, min(hi, a)]
    for d in L.values():
        fair = [d["a"][i] for i, f in d["fair"].items() if f and i in d["a"]]
        fraud = [d["a"][i] for i, f in d["fair"].items() if f is False and i in d["a"]]
        d["t"] = [max(fair, default=0.0), min(fraud, default=math.inf)]
    return L


def p_fair(a, t):
    lo, hi = t
    if a <= lo:
        return 1.0
    if a >= hi:
        return 0.0
    return 0.5


def p_acc(a, b):
    lo, hi = b
    if a <= lo:
        return 1.0
    if a >= hi:
        return 0.0
    return 0.5


def rule_b(p, m, sigma):
    """Same b rule as runner.price: p * P(T >= b) = 2/3, T ~ m * exp(sigma * Z)."""
    if p <= 2 / 3:
        return 0.0
    return m * math.exp(sigma * statistics.NormalDist().inv_cdf(max(1e-6, 1 - 2 / (3 * p))))


def backtest(d, us, m, p, alpha, sigma):
    """Expected net on one line had we played a = alpha*m and the runner's b rule with sigma
    (all other teams unchanged)."""
    a, b, net = alpha * m, rule_b(p, m, sigma), 0.0
    for rev, bb in d["b"].items():                       # we issue
        if rev == us:
            continue
        pf, pa = p_fair(a, d["t"]), p_acc(a, bb)
        net += pf * (pa * a + (1 - pa) * 1.5 * a) + (1 - pf) * pa * a
    for iss, ai in d["a"].items():                       # we review
        if iss == us or ai <= 0:
            continue
        f = d["fair"].get(iss)
        pf = (1.0 if f else 0.0) if f is not None else p_fair(ai, d["t"])
        acc = ai <= b
        net -= pf * (ai if acc else 1.5 * ai) + (1 - pf) * (ai if acc else 0.0)
    return net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="our team name as on the leaderboard")
    ap.add_argument("--rounds", default=str(Path(__file__).resolve().parent / "out" / "rounds.jsonl"))
    args = ap.parse_args()
    games = [g["id"] for g in get("games", completed_only="true", page_size=1000)["items"] if g["id"] != 0]
    ours = {}
    if Path(args.rounds).exists():
        for ln in Path(args.rounds).read_text().splitlines():
            r = json.loads(ln)
            ours[r["game"]] = r
    ratios, alphas, betas = [], [0.7, 0.8, 0.9, 1.0, 1.1], [0.2, 0.35, 0.5, 0.7]
    bt = {(al, be): 0.0 for al in alphas for be in betas}
    p_rows = []
    for gid in sorted(games):
        L = analyse(load_game(gid, args.team)["tx"])
        rec = ours.get(gid, {})
        est = (rec.get("est") or {}).get("deep") or (rec.get("est") or {}).get("fast") or {}
        print(f"\n== game {gid}: {len(L)} lines")
        for line, d in sorted(L.items()):
            lo, hi = d["t"]
            a = d["a"].get(args.team)
            b = d["b"].get(args.team)
            e = est.get(str(line)) or est.get(line)
            if not e and rec.get("tiers", {}).get(rec.get("winner")):      # older rounds: no est logged
                ab = rec["tiers"][rec["winner"]].get(str(line))
                if ab and rec["winner"] != "rules":
                    e = {"m": ab[0] / rec.get("a_mult", 1.0), "p": 1.0 if ab[1] > 0 else 0.5}
            m = e["m"] if e else None
            tag = "?" if a is None else ("fair" if a <= lo else "FRAUD" if a >= hi else "unsure")
            print(f"  {line:>3} t in [{lo:9.2f}, {hi:9.2f})  our a {a if a is not None else '-':>9}"
                  f" ({tag})  our b in {[round(x, 2) for x in b] if b else None}  m {m}  p {e['p'] if e else '-'}")
            if e and m:
                if lo > 0 or hi < 0.1 * m:
                    p_rows.append((e["p"], lo > 0))
                if lo > 0 and math.isfinite(hi):
                    ratios.append(math.log(math.sqrt(lo * hi) / m))
                for k in bt:
                    bt[k] += backtest(d, args.team, m, e["p"], *k)
    if ratios:
        mu, sd = statistics.median(ratios), statistics.pstdev(ratios) if len(ratios) > 1 else float("nan")
        print(f"\nt/m over {len(ratios)} bounded lines: median {math.exp(mu):.2f}, log-sd {sd:.2f}"
              f"  -> SIGMA ~ {sd:.2f}; A_MULT ~ 0.9 x {math.exp(mu):.2f}")
        print("backtest net (rows A_MULT, cols SIGMA):" + "  ".join(f"{be:>8}" for be in betas))
        for al in alphas:
            print(f"  {al:>4} " + "  ".join(f"{bt[(al, be)]:8.0f}" for be in betas))
    if p_rows:
        for name, sel in (("covered (t>0)", True), ("not covered", False)):
            ps = [p for p, cov in p_rows if cov is sel]
            if ps:
                print(f"our p on {name} lines: n {len(ps)}, mean {statistics.mean(ps):.2f}")
    if not ours:
        print("\n(no rounds.jsonl here: run on the runner machine for t/m and the backtest)")


if __name__ == "__main__":
    main()
