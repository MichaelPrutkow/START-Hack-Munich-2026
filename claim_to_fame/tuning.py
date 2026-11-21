"""Exploratory A_MULT/SIGMA field replay. Pure functions: no network, no LLM.

This historical estimator imputes unknown amounts and reuses selection data for comparison.
Its proposals are not applied by the runner. feedback.py owns the validated automatic path.

Input per finished game: the public transactions (all teams) and our own record from
out/rounds.jsonl (estimate m, coverage p, stated value and cap per line). From the transactions we
reconstruct, per line, every team's charge a, whether it was fair, every reviewer's interval for b
and the interval for t (as calibrate.py). Then we replay our pricing rule on every line we played
with other (A_MULT, SIGMA) against the other teams' real a and b, and keep the setting that earns
most, with guardrails (minimum data, minimum gain, robust across time, limited step per update).

Why automatic: games 7-9 showed that the spread of t/m is far wider than SIGMA 0.35 assumes
(notes/TRANSAKTIONEN_G1-9.md). These exploratory results motivated the wider parameter grid
now validated in feedback.py. This module retains the original analysis for comparison. A fixed
setting chosen early kept accepting the field's inflated claims, and from game 7 on our m was
about 2.4 times t (games 7-12: median t/m 0.42), so A_MULT may go down to 0.50. The guardrails
keep one odd game from swinging the parameters.
"""
import math
import statistics

from . import pricing

A_GRID = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00)
S_GRID = (0.35, 0.50, 0.65, 0.80, 1.00, 1.20)
BOUNDS = {"a_mult": (0.50, 1.00), "sigma": (0.30, 1.40)}
STEP = {"a_mult": 0.10, "sigma": 0.30}          # largest change per update
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
                           "stated": e.get("stated"), "cap": e.get("cap"), "a": ab[0], "ab": list(ab)}
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
        sent = list(ab) if ab and winner != "lb" else None   # "lb": reconstructed from the leaderboard, b unknown
        if e and pricing.pos(e.get("m")):
            out[int(k)] = {"m": float(e["m"]), "p": float(e.get("p") or 0), "stated": e.get("stated"),
                           "cap": e.get("cap"), "a": a, "ab": sent}
        elif ab and ab[0] > 0:
            out[int(k)] = {"m": ab[0] / float(rec.get("a_mult") or 1.0), "p": 1.0 if ab[1] > 0 else 0.5,
                           "stated": None, "cap": None, "a": ab[0], "ab": sent}
    return out


# ---------------------------------------------------------------- replay with bounds
def score(d, team, a, b):
    """(lower, upper) net of charging a and accepting up to b on this line, other teams as played.
    calibrate.score_submission: unknown reviewer limits, unknown fairness and the payment cap c
    (c >= 4t, accepted pays min(a, c)) are kept as ranges, never interpolated."""
    import calibrate as C
    sc = C.score_submission(d, team, a, b)
    return sc["lower"], sc["upper"]


def build(games, rounds, team):
    """games {gid: tx}, rounds {gid: rec} -> rows {"g", "i", "d", "team", "m", "p", "stated", "cap"}.
    Brackets come from calibrate.analyse, closed with our own submitted prices where we know them."""
    import calibrate as C
    rows = []
    for gid in sorted(games):
        mine = own_lines(rounds.get(gid) or {})
        if not mine:
            continue
        sub = {i: x["ab"] for i, x in mine.items() if x.get("ab")}
        try:
            L = C.analyse(games[gid], sub, team) if sub else C.analyse(games[gid])
        except ValueError:                    # contradicting own record: brackets from the field only
            L = C.analyse(games[gid])
        for i, x in sorted(mine.items()):
            d = L.get(i)
            if d is None or not any(k != team for k in d["b"]):
                continue
            row = {"g": gid, "i": i, "d": d, "team": team, **{k: x[k] for k in ("m", "p", "stated", "cap")}}
            try:
                lo, hi = score(d, team, *pricing.price(row["p"], row["m"], row["stated"], row["cap"], row["m"],
                                                       a_mult=0.85, sigma=0.35, uncertainty_aware=False))
            except ValueError:
                continue
            if math.isfinite(lo) and math.isfinite(hi):
                rows.append(row)
    return rows


def replay(rows, a_mult, sigma):
    """-> {gid: (lower, upper)} for our pricing rule with these settings."""
    out = {}
    for r in rows:
        a, b = pricing.price(r["p"], r["m"], r["stated"], r["cap"], r["m"], a_mult=a_mult, sigma=sigma,
                             uncertainty_aware=False)
        lo, hi = score(r["d"], r["team"], a, b)
        plo, phi = out.get(r["g"], (0.0, 0.0))
        out[r["g"]] = (plo + lo, phi + hi)
    return out


def shadow_report(games, rounds, team):
    """Pre-registered shadow variants from rounds.jsonl (runner.shadow_variants, never sent) against
    the real submission: {variant: {"games", "lines", "lower", "upper"}}, change in net vs actual.
    Same bounds as everywhere: unknown limits, fairness and c stay ranges."""
    import calibrate as C
    out = {}
    for gid in sorted(games):
        rec = rounds.get(gid) or {}
        final, shadow = rec.get("final_ab"), rec.get("shadow")
        if rec.get("dry") or not isinstance(final, dict) or not isinstance(shadow, dict):
            continue
        try:
            L = C.analyse(games[gid], {int(i): ab for i, ab in final.items()}, team)
        except ValueError:
            continue
        for name, variant in shadow.items():
            acc = out.setdefault(name, {"games": [], "lines": 0, "lower": 0.0, "upper": 0.0})
            acc["games"].append(gid)
            for i, ab in variant.items():
                d, real = L.get(int(i)), final.get(str(i)) or final.get(i)
                if d is None or not real or list(ab) == list(real):
                    continue
                try:
                    vlo, vhi = score(d, team, *ab)
                    rlo, rhi = score(d, team, *real)
                except ValueError:
                    continue
                acc["lines"] += 1
                acc["lower"] += vlo - rhi
                acc["upper"] += vhi - rlo
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
    """current {"a_mult", "sigma"} -> (new settings, report). The grid's best midpoint only gives the
    direction; the step that would actually be written (at most one STEP) must itself pass, against
    current: enough lines; weighted midpoint gain >= min_gain; weighted lower bound (worst case over
    unknown limits, fairness and c) not worse; midpoint not worse in at least 2 of 3 chronological thirds; still a gain without the game
    that gains most (no single threshold hit). Otherwise keep `current`."""
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

    def weighted(per, k):
        return sum(w[g] * (v[k] if k < 2 else (v[0] + v[1]) / 2) for g, v in per.items())

    base = replay(rows, cur["a_mult"], cur["sigma"])
    grid = {(am, s): replay(rows, am, s) for am in A_GRID for s in S_GRID}
    (am, s), best = max(grid.items(), key=lambda kv: weighted(kv[1], 2))
    report["grid"] = {f"{k[0]:.2f}/{k[1]:.2f}": round(weighted(v, 2)) for k, v in grid.items()}
    report["current_score"] = {"mid": round(weighted(base, 2)), "lower": round(weighted(base, 0)),
                               "upper": round(weighted(base, 1))}
    report["best"] = {"a_mult": am, "sigma": s, "score": round(weighted(best, 2))}
    new = {}
    for k, target in (("a_mult", am), ("sigma", s)):
        step = max(-STEP[k], min(STEP[k], target - cur[k]))
        new[k] = round(clamp(k, cur[k] + step), 3)
    if new == cur:
        report["decision"] = f"keep: current {cur['a_mult']:.2f}/{cur['sigma']:.2f} is the best grid point"
        return cur, report
    step = replay(rows, new["a_mult"], new["sigma"])
    gain = {k: weighted(step, k) - weighted(base, k) for k in (0, 1, 2)}
    per_game = {g: (step[g][0] + step[g][1] - base[g][0] - base[g][1]) / 2 for g in games}
    top = max(per_game, key=per_game.get)
    without_top = sum(w[g] * v for g, v in per_game.items() if g != top)
    wins = sum(1 for T in thirds if sum(per_game[g] for g in T) >= -1e-9)
    report["step"] = {**new, "gain_mid": round(gain[2]), "gain_lower": round(gain[0]), "gain_upper": round(gain[1]),
                      "periods": wins, "best_game": top, "gain_without_best_game": round(without_top)}
    label = f"{cur['a_mult']:.2f}/{cur['sigma']:.2f} -> {new['a_mult']:.2f}/{new['sigma']:.2f}"
    reasons = []
    if gain[2] < min_gain:
        reasons.append(f"midpoint gain {gain[2]:+.0f} < {min_gain:.0f}")
    if gain[0] < -1e-9:
        reasons.append(f"worst case {gain[0]:+.0f} < 0")
    if wins < 2:
        reasons.append(f"better in only {wins} of 3 periods")
    if without_top <= 0:
        reasons.append(f"without G{top} {without_top:+.0f}")
    if reasons:
        report["decision"] = f"keep: step {label} fails: " + "; ".join(reasons) + f" (grid best {am:.2f}/{s:.2f})"
        return cur, report
    report["decision"] = (f"change: {label} (+{gain[2]:.0f} weighted midpoint, bounds {gain[0]:+.0f}/{gain[1]:+.0f}, "
                          f"{wins}/3 periods, without G{top} {without_top:+.0f}; grid best {am:.2f}/{s:.2f})")
    return new, report
