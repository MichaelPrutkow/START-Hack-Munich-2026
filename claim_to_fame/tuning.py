"""Tune A_MULT and SIGMA from the public leaderboard. Pure functions: no network, no LLM.

Input per finished game: the public transactions (all teams) and our own record from
out/rounds.jsonl (estimate m, coverage p, stated value and cap per line). From the transactions we
reconstruct, per line, every team's charge a, whether it was fair, every reviewer's interval for b
and the interval for t (as calibrate.py). Then we replay our pricing rule on every line we played
with other (A_MULT, SIGMA) against the other teams' real a and b, and keep the setting that earns
most, with guardrails (minimum data, minimum gain, robust across time, limited step per update).

Why automatic: games 7-9 showed that the spread of t/m is far wider than SIGMA 0.35 assumes
(notes/TRANSAKTIONEN_G1-9.md). Complements feedback.py: that module calibrates our estimate m
(bias, spread) per model/prompt and only after a chronological holdout; this one tunes the two
pricing multipliers against the field from the first game on, using every logged game.; a fixed setting chosen early kept accepting the field's inflated
claims. The guardrails keep one odd game from swinging the parameters.
"""
import math
import statistics

from . import pricing

A_GRID = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00)
S_GRID = (0.35, 0.50, 0.65, 0.80, 1.00, 1.20)
BOUNDS = {"a_mult": (0.70, 1.00), "sigma": (0.30, 1.40)}
STEP = {"a_mult": 0.05, "sigma": 0.30}          # largest change per update
MIN_LINES = 8                                    # lines with a usable estimate and bracket
MIN_GAIN = 1000.0                                # EUR over all games (weighted), else keep
HALF_LIFE = 10.0                                 # games; recent games weigh more


def clamp(name, x):
    lo, hi = BOUNDS[name]
    return min(hi, max(lo, float(x)))


# ---------------------------------------------------------------- brackets from transactions
def analyse(tx, own_a=None, team=None):
    """tx: list of {issuer, reviewer, line_item_index, accepted, amount}.
    own_a: {line: our a} from our own submission (fills the case where every reviewer rejected us).
    -> {line: {"a": {issuer: a}, "fair": {issuer: bool}, "b": {reviewer: [lo, hi)}, "t": [lo, hi)}}"""
    L = {}
    for x in tx:
        d = L.setdefault(int(x["line_item_index"]), {"a": {}, "exact": set(), "fair": {}, "b": {}})
        iss, amt = x["issuer"], float(x["amount"] or 0)
        if not x["accepted"] and amt > 0:               # fair and rejected: paid 1.5 a
            d["a"][iss] = amt / 1.5
            d["exact"].add(iss)
            d["fair"][iss] = True
        elif not x["accepted"]:                          # inflated and rejected
            d["fair"][iss] = False
        elif iss not in d["exact"]:                      # accepted: amount = min(a, c)
            d["a"][iss] = max(d["a"].get(iss, 0.0), amt)
    if team and own_a:
        for i, a in own_a.items():
            if int(i) in L and a and a > 0:
                L[int(i)]["a"].setdefault(team, float(a))
    for x in tx:
        d = L[int(x["line_item_index"])]
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


# ---------------------------------------------------------------- our estimates per line
def own_lines(rec):
    """Our record of one game (rounds.jsonl) -> {line: {"m", "p", "stated", "cap", "a"}}.
    New records: per line the tier that was submitted (line_sources, final_ab). Older records: the
    winning tier's estimate; without `est`: m = a / a_mult. Rule-based lines are skipped."""
    if rec.get("dry"):
        return {}
    if isinstance(rec.get("final_ab"), dict) and isinstance(rec.get("line_sources"), dict):
        out = {}
        for k, src in rec["line_sources"].items():
            e = ((rec.get("est") or {}).get(src) or {}).get(str(k))
            ab = rec["final_ab"].get(str(k))
            if src == "rules" or not e or not ab or not pricing.pos(e.get("m")):
                continue
            out[int(k)] = {"m": float(e["m"]) * float(e.get("bias") or 1.0), "p": float(e.get("p") or 0),
                           "stated": e.get("stated"), "cap": e.get("cap"), "a": ab[0]}
        return out
    winner = rec.get("winner")
    if not winner or winner == "rules":
        return {}
    est = (rec.get("est") or {}).get(winner) or {}
    tiers = (rec.get("tiers") or {}).get(winner) or {}
    out = {}
    for k in set(est) | set(tiers):
        e, ab = est.get(k), tiers.get(k)
        a = ab[0] if ab else (e or {}).get("a")
        if e and pricing.pos(e.get("m")):
            out[int(k)] = {"m": float(e["m"]), "p": float(e.get("p") or 0), "stated": e.get("stated"),
                           "cap": e.get("cap"), "a": a}
        elif ab and ab[0] > 0:
            out[int(k)] = {"m": ab[0] / float(rec.get("a_mult") or 1.0), "p": 1.0 if ab[1] > 0 else 0.5,
                           "stated": None, "cap": None, "a": ab[0]}
    return out


# ---------------------------------------------------------------- replay one line
def _p_acc(a, b):
    lo, hi = b
    if a <= lo:
        return 1.0
    if a >= hi:
        return 0.0
    return 0.5 if hi == math.inf else (hi - a) / (hi - lo)


def _p_fair(a, t):
    lo, hi = t
    if a <= lo:
        return 1.0
    if a >= hi:
        return 0.0
    if hi == math.inf:                       # nobody overshot: closed brackets ran to 1.35-1.6 x lo
        return 0.5 if a <= 1.25 * lo else 0.25 if a <= 1.6 * lo else 0.0
    return (hi - a) / (hi - lo)


def line_net(d, team, a, b):
    """Expected net of charging a and accepting up to b on this line, other teams as played."""
    net = 0.0
    if a > 0:
        pf = _p_fair(a, d["t"])
        for rev, bb in d["b"].items():
            if rev == team:
                continue
            pa = _p_acc(a, bb)
            net += pf * (pa * a + (1 - pa) * 1.5 * a) + (1 - pf) * pa * a
    for iss, ai in d["a"].items():
        if iss == team or ai <= 0:
            continue
        f = d["fair"].get(iss)
        pf = (1.0 if f else 0.0) if f is not None else _p_fair(ai, d["t"])
        net -= ai if ai <= b else pf * 1.5 * ai
    return net


def build(games, rounds, team):
    """games {gid: tx}, rounds {gid: rec} -> list of rows {"g", "i", "d", "m", "p", "stated", "cap"}."""
    rows = []
    for gid in sorted(games):
        mine = own_lines(rounds.get(gid) or {})
        if not mine:
            continue
        L = analyse(games[gid], {i: x["a"] for i, x in mine.items()}, team)
        for i, x in sorted(mine.items()):
            d = L.get(i)
            if d is None or not any(k != team for k in d["b"]):
                continue
            rows.append({"g": gid, "i": i, "d": d, "team": team, **{k: x[k] for k in ("m", "p", "stated", "cap")}})
    return rows


def replay(rows, a_mult, sigma):
    """-> {gid: net} for our pricing rule with these settings."""
    out = {}
    for r in rows:
        a, b = pricing.price(r["p"], r["m"], r["stated"], r["cap"], r["m"], a_mult=a_mult, sigma=sigma,
                             uncertainty_aware=False)
        out[r["g"]] = out.get(r["g"], 0.0) + line_net(r["d"], r["team"], a, b)
    return out


def fit_log_ratio(rows):
    """Censored MLE of log(t/m) ~ N(mu, s) over lines we judged covered; for the report only."""
    iv = []
    for r in rows:
        if r["p"] <= 2 / 3:
            continue
        lo, hi = r["d"]["t"]
        if lo <= 0 and not math.isfinite(hi):
            continue
        iv.append((math.log(lo / r["m"]) if lo > 0 else -math.inf,
                   math.log(hi / r["m"]) if math.isfinite(hi) else math.inf))
    if len(iv) < 3:
        return None
    N = statistics.NormalDist()

    def ll(mu, s):
        tot = 0.0
        for lo, hi in iv:
            pr = (N.cdf((hi - mu) / s) if hi < math.inf else 1.0) - (N.cdf((lo - mu) / s) if lo > -math.inf else 0.0)
            tot += math.log(max(pr, 1e-12))
        return tot
    best = max((ll(mu / 50, s / 50), mu / 50, s / 50) for mu in range(-75, 51) for s in range(5, 101))
    return {"n": len(iv), "median_t_over_m": round(math.exp(best[1]), 3), "log_sd": round(best[2], 3)}


# ---------------------------------------------------------------- choose settings
def choose(rows, current, *, min_lines=MIN_LINES, min_gain=MIN_GAIN, half_life=HALF_LIFE):
    """current {"a_mult", "sigma"} -> (new settings, report). Keeps `current` unless the backtest
    clearly prefers another setting: enough lines, weighted gain >= min_gain, not worse than
    current in at least 2 of 3 chronological thirds, and at most one STEP per update."""
    cur = {"a_mult": clamp("a_mult", current["a_mult"]), "sigma": clamp("sigma", current["sigma"])}
    report = {"lines": len(rows), "games": sorted({r["g"] for r in rows}), "current": cur,
              "fit": fit_log_ratio(rows)}
    if len(rows) < min_lines:
        report["decision"] = f"keep: only {len(rows)} lines (< {min_lines})"
        return cur, report
    games = report["games"]
    last = games[-1]
    w = {g: 0.5 ** ((last - g) / half_life) for g in games}
    thirds = [set(games[k * len(games) // 3:(k + 1) * len(games) // 3]) for k in range(3)]

    def score(am, s):
        per = replay(rows, am, s)
        return sum(w[g] * v for g, v in per.items()), [sum(per.get(g, 0.0) for g in T) for T in thirds]

    base, base_t = score(cur["a_mult"], cur["sigma"])
    grid = {(am, s): score(am, s) for am in A_GRID for s in S_GRID}
    (am, s), (best, best_t) = max(grid.items(), key=lambda kv: kv[1][0])
    report["grid"] = {f"{k[0]:.2f}/{k[1]:.2f}": round(v[0]) for k, v in grid.items()}
    report["current_score"], report["best"] = round(base), {"a_mult": am, "sigma": s, "score": round(best)}
    wins = sum(1 for x, y in zip(best_t, base_t) if x >= y - 1e-9)
    if best - base < min_gain:
        report["decision"] = f"keep: best {am:.2f}/{s:.2f} gains {best - base:+.0f} (< {min_gain:.0f})"
        return cur, report
    if wins < 2:
        report["decision"] = f"keep: best {am:.2f}/{s:.2f} better in only {wins} of 3 periods"
        return cur, report
    new = {}
    for k, target in (("a_mult", am), ("sigma", s)):
        step = max(-STEP[k], min(STEP[k], target - cur[k]))
        new[k] = round(clamp(k, cur[k] + step), 3)
    report["decision"] = (f"change: {cur['a_mult']:.2f}/{cur['sigma']:.2f} -> {new['a_mult']:.2f}/{new['sigma']:.2f}"
                          f" (best {am:.2f}/{s:.2f}, +{best - base:.0f} weighted, {wins}/3 periods)")
    return new, report
