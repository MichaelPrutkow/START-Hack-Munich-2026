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
  t interval = [max fair a, min known upper bound on unfair a)
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


def finite_number(value, name="value"):
    """Validate external numeric fields; booleans/NaN/infinity are not prices."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return number


def analyse(tx, submissions=None, team=None):
    """Infer conservative intervals from transactions and optional authoritative own prices.

    ``submissions`` is {line: (charge_price, acceptance_limit)} for ``team``.
    ``a`` retains known lower bounds for compatibility; only issuers in ``exact``
    have known charges. ``a_bounds`` uses closed bounds, ``t`` and ``b`` use an
    exclusive upper bound. Accepted payments reveal min(a,c), never a by themselves.
    ``c`` is the closed interval for the hidden payment cap, using c >= 4t.
    Invalid or contradictory observations raise ValueError rather than tighten bounds.
    """
    L = {}
    seen = set()
    for raw in tx:
        if not isinstance(raw, dict):
            raise ValueError("transaction must be an object")
        try:
            line = raw["line_item_index"]
            if isinstance(line, bool) or not isinstance(line, int) or line < 0:
                raise ValueError("invalid line_item_index")
            iss, rev = raw["issuer"], raw["reviewer"]
            if not isinstance(iss, str) or not iss or not isinstance(rev, str) or not rev or iss == rev:
                raise ValueError("invalid transaction teams")
            if not isinstance(raw["accepted"], bool):
                raise ValueError("accepted must be boolean")
            amt = finite_number(raw["amount"], "amount")
        except KeyError as exc:
            raise ValueError(f"missing transaction field: {exc.args[0]}") from exc
        x = {**raw, "amount": amt}
        key = (line, iss, rev)
        if key in seen:
            raise ValueError("duplicate transaction")
        seen.add(key)
        d = L.setdefault(line, {"a": {}, "a_bounds": {}, "exact": set(), "fair": {},
                                 "b": {}, "b_exact": {}, "accepted_amount": {}, "transactions": []})
        d["transactions"].append(x)
        d["a_bounds"].setdefault(iss, [0.0, math.inf])
        d["b"].setdefault(rev, [0.0, math.inf])
        if x["accepted"]:
            previous = d["accepted_amount"].get(iss)
            if previous is not None and abs(previous - amt) > 0.011:
                raise ValueError("inconsistent accepted payments for one issuer")
            d["accepted_amount"][iss] = amt
            d["a_bounds"][iss][0] = max(d["a_bounds"][iss][0], amt)
        else:
            fair = amt > 0
            if iss in d["fair"] and d["fair"][iss] != fair:
                raise ValueError("inconsistent rejection outcomes for one issuer")
            d["fair"][iss] = fair
            if fair:
                a = amt / 1.5
                previous = d["a"].get(iss)
                if iss in d["exact"] and abs(previous - a) > 0.011:
                    raise ValueError("inconsistent exact charges")
                d["a"][iss] = a
                d["exact"].add(iss)
    if submissions is not None:
        if not isinstance(team, str) or not team:
            raise ValueError("team is required with submissions")
        if not isinstance(submissions, dict):
            raise ValueError("submissions must be an object")
        for line, ab in submissions.items():
            try:
                if isinstance(line, bool) or str(line) != str(int(line)) or int(line) < 0:
                    raise ValueError("invalid submitted line index")
                line = int(line)
                if not isinstance(ab, (list, tuple)) or len(ab) != 2:
                    raise ValueError("invalid submitted prices")
                a, b = (finite_number(v, "submitted price") for v in ab)
            except (TypeError, ValueError) as exc:
                raise ValueError("submissions must map line indices to two finite prices") from exc
            if line in L:
                d = L[line]
                if team in d["exact"] and abs(d["a"][team] - a) > 0.011:
                    raise ValueError("submitted charge contradicts rejection payment")
                d["a"][team] = a
                d["exact"].add(team)
                d["a_bounds"].setdefault(team, [0.0, math.inf])
                d["b_exact"][team] = b
    for d in L.values():
        for x in d["transactions"]:
            if x["accepted"] and x["reviewer"] in d["b_exact"]:
                bounds = d["a_bounds"][x["issuer"]]
                bounds[1] = min(bounds[1], d["b_exact"][x["reviewer"]])
        # A payment below a proven lower bound on c cannot be capped. This is
        # another exact-charge proof, unlike simply equating every payment to a.
        fair_lower = max((d["a"][i] for i, fair in d["fair"].items() if fair), default=0.0)
        cap_lower = max([4 * fair_lower, *d["accepted_amount"].values()])
        for issuer, paid in d["accepted_amount"].items():
            if paid + 0.011 < cap_lower:
                if issuer in d["exact"] and abs(d["a"][issuer] - paid) > 0.011:
                    raise ValueError("payment contradicts proven uncapped charge")
                d["a"][issuer] = paid
                d["exact"].add(issuer)
        for iss, bounds in d["a_bounds"].items():
            if bounds[0] == bounds[1]:
                d["a"][iss] = bounds[0]
                d["exact"].add(iss)
            if iss in d["exact"]:
                a = d["a"][iss]
                if a + 0.011 < bounds[0] or a > bounds[1] + 0.011:
                    raise ValueError("known charge contradicts observed bounds")
                bounds[:] = [a, a]
            elif bounds[0] > 0 or iss in d["accepted_amount"]:
                d["a"][iss] = bounds[0]
            if bounds[0] > bounds[1] + 0.011:
                raise ValueError("contradictory charge bounds")
        fair = [d["a_bounds"][i][0] for i, f in d["fair"].items() if f]
        # A lower bound on an unfair charge is NOT an upper bound on t.
        fraud = [d["a_bounds"][i][1] for i, f in d["fair"].items() if f is False]
        d["t"] = [max(fair, default=0.0), min(fraud, default=math.inf)]
        if d["t"][0] >= d["t"][1]:
            raise ValueError("contradictory fairness bounds")
        cap_lo = max([4 * d["t"][0], *d["accepted_amount"].values()])
        cap_hi = math.inf
        for x in d["transactions"]:
            iss, rev = x["issuer"], x["reviewer"]
            low, high = d["a_bounds"][iss]
            bb = d["b"][rev]
            if x["accepted"]:
                bb[0] = max(bb[0], low)
                if iss in d["exact"] and low > x["amount"] + 0.011:
                    cap_hi = min(cap_hi, x["amount"])
            else:
                # Rejection gives b < a. Only an upper charge bound constrains b.
                bb[1] = min(bb[1], high)
            if rev in d["b_exact"]:
                own_b = d["b_exact"][rev]
                if (x["accepted"] and low > own_b + 0.011) or (not x["accepted"] and high <= own_b):
                    raise ValueError("submitted acceptance limit contradicts transactions")
        if cap_lo > cap_hi + 0.011 or any(lo >= hi for lo, hi in d["b"].values()):
            raise ValueError("contradictory cap or acceptance bounds")
        d["c"] = [cap_lo, max(cap_lo, cap_hi)]
    return L


def p_fair(a, t):
    """Legacy midpoint heuristic, not an inferred probability. Prefer score_submission."""
    lo, hi = t
    return 1.0 if a <= lo else 0.0 if a >= hi else 0.5


def p_acc(a, b):
    """Legacy midpoint heuristic, not an inferred probability. Prefer score_submission."""
    return p_fair(a, b)


def rule_b(p, m, sigma):
    """Same b rule as runner.price: p * P(T >= b) = 2/3."""
    if p <= 2 / 3:
        return 0.0
    return m * math.exp(sigma * statistics.NormalDist().inv_cdf(max(1e-6, 1 - 2 / (3 * p))))


def score_submission(d, us, a, b):
    """Score actual supplied prices with conservative bounds, holding opponents fixed.

    Bounds cover unknown fairness, reviewer thresholds, opponent charges and c.
    Per-opponent bounds are added conservatively; their extremes need not occur
    simultaneously. ``estimate`` is only the interval midpoint, NOT expected profit.
    For legacy dictionaries lacking ``a_bounds``, entries in ``a`` are treated as
    exact; newly analysed observations always carry explicit bounds.
    """
    a, b = finite_number(a, "charge_price"), finite_number(b, "acceptance_limit")
    tlo, thi = d["t"]
    clo, chi = d.get("c", [4 * tlo, math.inf])
    total_lo = total_hi = 0.0
    original_a = d["a"].get(us) if us in d.get("exact", set()) else None
    original_b = d.get("b_exact", {}).get(us)
    transactions = d.get("transactions", [])
    for rev, (blo, bhi) in d["b"].items():
        if rev == us:
            continue
        observed = next((x for x in transactions if x["issuer"] == us and x["reviewer"] == rev), None)
        if observed is not None and original_a is not None and a == original_a:
            total_lo += observed["amount"]
            total_hi += observed["amount"]
            continue
        possibilities = []
        if a < bhi:  # acceptance remains feasible
            possibilities.extend([min(a, clo), min(a, chi)])
        if a > blo:  # rejection remains feasible
            if a <= tlo:
                possibilities.append(1.5 * a)
            elif a >= thi:
                possibilities.append(0.0)
            else:
                possibilities.extend([0.0, 1.5 * a])
        if not possibilities:
            raise ValueError("inconsistent reviewer bounds")
        total_lo += min(possibilities)
        total_hi += max(possibilities)
    bounds = d.get("a_bounds", {issuer: [v, v] for issuer, v in d["a"].items()})
    for issuer, (alo, ahi) in bounds.items():
        if issuer == us:
            continue
        observed = next((x for x in transactions if x["issuer"] == issuer and x["reviewer"] == us), None)
        if observed is not None and original_b is not None and b == original_b:
            total_lo -= observed["amount"]
            total_hi -= observed["amount"]
            continue
        possibilities = []
        paid = d.get("accepted_amount", {}).get(issuer)
        fair = d["fair"].get(issuer)
        if alo <= b:
            upper = min(ahi, b)
            if paid is not None:
                possibilities.append(paid)
            elif fair is True or upper <= tlo:
                possibilities.extend([alo, upper])
            else:
                possibilities.extend([min(alo, clo), min(upper, chi)])
        if ahi > b:
            lower = max(alo, b)
            if fair is False or lower >= thi:
                possibilities.append(0.0)
            elif paid is not None and ahi > alo:
                # If a > the accepted payment, it is capped. Since c >= 4t,
                # that charge is necessarily unfair. Fair rejection can only
                # occur at a == paid, which also bounds the possible penalty.
                possibilities.append(0.0)
                if paid > b and paid < thi:
                    possibilities.append(1.5 * paid)
            elif fair is True:
                possibilities.extend([1.5 * lower, 1.5 * ahi])
            else:
                possibilities.append(0.0)
                if lower < thi:
                    possibilities.append(1.5 * min(ahi, thi))
        if not possibilities:
            raise ValueError("inconsistent issuer bounds")
        total_lo -= max(possibilities)
        total_hi -= min(possibilities)
    estimate = (total_lo + total_hi) / 2 if math.isfinite(total_lo) and math.isfinite(total_hi) else None
    return {"lower": total_lo, "upper": total_hi, "estimate": estimate,
            "exact": total_lo == total_hi, "assumptions": "conservative bounds; midpoint is not expected profit"}


def backtest(d, us, m, p, alpha, sigma, *, stated=None, cap=None, fallback=0.0,
             uncertainty=0.0, bias=1.0, bounds=False):
    """Compatibility parameter scan using the actual pricing rule, including overrides.

    Returns the explicitly heuristic interval midpoint by default (None if unbounded).
    Pass bounds=True for score_submission's full uncertainty report. Shared caps must
    already have been applied to m/stated, as in the runner's logged estimates.
    """
    from claim_to_fame.pricing import price
    m, p, alpha, sigma, fallback = (finite_number(v, k) for k, v in
                                  (("m", m), ("p", p), ("alpha", alpha), ("sigma", sigma), ("fallback", fallback)))
    if p > 1:
        raise ValueError("p must not exceed 1")
    stated = finite_number(stated, "stated") if stated is not None else None
    cap = finite_number(cap, "cap") if cap is not None else None
    a, b = price(p, m, stated, cap, fallback, a_mult=alpha, sigma=sigma,
                 uncertainty=uncertainty, bias=bias)
    score = score_submission(d, us, a, b)
    return score if bounds else score["estimate"]


def censored_fit(iv):
    """MLE of log(t/m) ~ N(mu, s) from intervals [lo, hi) on the log scale (hi may be inf).
    Uses one-sided lines too: dropping them can bias the estimated median."""
    N = statistics.NormalDist()

    def ll(mu, s):
        tot = 0.0
        for lo, hi in iv:
            pr = (N.cdf((hi - mu) / s) if hi < math.inf else 1.0) - N.cdf((lo - mu) / s)
            tot += math.log(max(pr, 1e-12))
        return tot
    grid = [(ll(mu / 100, s / 100), mu / 100, s / 100) for mu in range(-100, 101, 2) for s in range(5, 151)]
    best = max(grid)
    s_ok = [s for l, _, s in grid if l > best[0] - 2]
    return best[1], best[2], min(s_ok), max(s_ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="our team name as on the leaderboard")
    ap.add_argument("--rounds", default=str(Path(__file__).resolve().parent / "out" / "rounds.jsonl"))
    ap.add_argument("--tier", default="auto", choices=["auto", "fast", "deep"])
    ap.add_argument("--holdout-from", type=int, help="first game reserved for chronological holdout")
    args = ap.parse_args()
    games = [g["id"] for g in get("games", completed_only="true", page_size=1000)["items"] if g["id"] != 0]
    ours = {}
    if Path(args.rounds).exists():
        for ln in Path(args.rounds).read_text().splitlines():
            r = json.loads(ln)
            ours[r["game"]] = r
    ratios, cens = [], []
    alphas, betas = [0.7, 0.8, 0.85, 0.9, 1.0, 1.1], [0.2, 0.35, 0.5, 0.7]
    scans = {split: {(al, be): [0.0, 0.0] for al in alphas for be in betas} for split in ("train", "holdout")}
    counts = {"train": 0, "holdout": 0}
    p_rows = []
    for gid in sorted(games):
        rec = ours.get(gid, {})
        submitted = rec.get("final_ab") if rec.get("ok") and not rec.get("dry") else None
        L = analyse(load_game(gid, args.team)["tx"], submitted, args.team)
        estimates = rec.get("est") or {}
        est = (estimates.get("deep") or estimates.get("fast") or {}) if args.tier == "auto" else (estimates.get(args.tier) or {})
        split = "holdout" if args.holdout_from is not None and gid >= args.holdout_from else "train"
        print(f"\n== game {gid}: {len(L)} lines ({split})")
        for line, d in sorted(L.items()):
            lo, hi = d["t"]
            a = d["a"].get(args.team)
            b = d["b"].get(args.team)
            e = est.get(str(line)) or est.get(line)
            if args.tier == "auto" and rec.get("line_sources"):
                source = rec["line_sources"].get(str(line), rec["line_sources"].get(line))
                e = estimates.get(source, {}).get(str(line), estimates.get(source, {}).get(line))
            # A logged price cannot recover m: stated values and caps override it.
            if e:
                try:
                    m, p = finite_number(e["m"], "m"), finite_number(e["p"], "p")
                    if p > 1:
                        raise ValueError("p exceeds 1")
                except (KeyError, ValueError):
                    e = None
            exact = args.team in d["exact"]
            tag = "?" if a is None else ("fair" if exact and a <= lo else "UNFAIR" if exact and a >= hi else "uncertain")
            shown_a = "-" if a is None else f"{'=' if exact else '>='}{a:.2f}"
            print(f"  {line:>3} t in [{lo:9.2f}, {hi:9.2f})  our a {shown_a:>10} ({tag})"
                  f"  our b in {[round(x, 2) for x in b] if b else None}  m {m if e else '-'}  p {p if e else '-'}")
            if not e:
                continue
            if split == "train" and m > 0:
                if lo > 0:
                    p_rows.append(p)
                    cens.append((math.log(lo / m), math.log(hi / m) if math.isfinite(hi) else math.inf))
                if 0 < lo < hi < math.inf:
                    ratios.append(math.log(math.sqrt(lo * hi) / m))
            counts[split] += 1
            item = (rec.get("items") or {}).get(str(line), (rec.get("items") or {}).get(line))
            from claim_to_fame.invoices import rule_value
            fallback = e.get("fallback", rule_value(item) if item else 0.0)
            for params, totals in scans[split].items():
                score = backtest(d, args.team, m, p, *params, stated=e.get("stated"),
                                 cap=e.get("cap"), fallback=fallback,
                                 uncertainty=e.get("log_spread", 0.0) if e.get("uncertainty_applied") else 0.0,
                                 bias=e.get("bias", 1.0), bounds=True)
                totals[0] += score["lower"]
                totals[1] += score["upper"]
    if cens:
        mu, sd, lo_sd, hi_sd = censored_fit(cens)
        print(f"\nTraining t/m censored MLE over {len(cens)} known-covered lines: "
              f"median {math.exp(mu):.2f}, log-sd {sd:.2f} (2-LL range {lo_sd:.2f}-{hi_sd:.2f}). "
              "Coverage selection remains a limitation.")
    if ratios:
        print(f"Two-sided training intervals only (selection-biased): n {len(ratios)}, "
              f"midpoint median t/m {math.exp(statistics.median(ratios)):.2f}")
    if counts["train"]:
        print("Training counterfactual net bounds (A_MULT, SIGMA); no inferred probabilities:")
        for params, (lo, hi) in scans["train"].items():
            print(f"  {params}: [{lo:.0f}, {hi:.0f}]")
        selected = max(scans["train"], key=lambda k: scans["train"][k][0])
        print(f"Training-selected parameters by greatest lower bound: {selected}")
        if counts["holdout"]:
            lo, hi = scans["holdout"][selected]
            print(f"Chronological holdout for fixed selection: [{lo:.0f}, {hi:.0f}], "
                  f"{counts['holdout']} lines. Overlapping bounds do not establish improvement.")
        else:
            print("No holdout observations: descriptive in-sample scan only; no performance claim.")
    if p_rows:
        print(f"Training p on provably covered lines: n {len(p_rows)}, mean {statistics.mean(p_rows):.2f}; "
              "a positive upper t bound does not prove non-coverage.")
    if not ours:
        print("\n(no rounds.jsonl here: run on the runner machine for t/m and the backtest)")


if __name__ == "__main__":
    main()
