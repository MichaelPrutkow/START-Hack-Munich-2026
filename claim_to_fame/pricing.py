"""Vote aggregation, shared caps and deterministic prices. Settings are explicit arguments."""
import logging
import math
import statistics

log = logging.getLogger("c2f")


def floor_c(x):
    x = float(x) if x is not None else 0.0
    return math.floor(round(x * 100, 6)) / 100 if math.isfinite(x) and x > 0 else 0.0   # round: float noise


def ceil_c(x):
    x = float(x) if x is not None else 0.0
    return math.ceil(round(x * 100, 6)) / 100 if math.isfinite(x) and x > 0 else 0.0   # round: float noise


def pos(x):
    try:
        x = float(x)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError):
        return None


def aggregate(votes, *, disagree):
    """votes for ONE line -> (p, m, stated, cap, shared)."""
    p = statistics.median(min(max(float(v.get("covered_prob") or 0), 0.0), 1.0) for v in votes)
    vals = sorted(x for x in (pos(v.get("t_if_covered")) for v in votes) if x)
    m = 0.0
    if vals:
        m = math.exp(statistics.median(math.log(x) for x in vals))
        # Votes disagree: lean high (geo mean of the two highest). Official payoffs: rejecting
        # a fair claim costs 1.5a, accepting an inflated one at most min(a, c).
        if len(vals) >= 2 and vals[-1] / vals[0] >= disagree:
            m = math.sqrt(vals[-1] * vals[-2])

    def majority(key):                              # only trust a stated value most votes report
        xs = [x for x in (pos(v.get(key)) for v in votes) if x]
        return statistics.median(xs) if len(xs) * 2 > len(votes) else None
    return p, m, majority("stated_value"), majority("stated_cap"), majority("shared_cap")


def apply_shared_caps(aggs):
    """aggs {i: (p, m, stated, cap, shared)} -> same without shared, values scaled pro rata where
    the lines that share one limit add up to more than it (policy wording: applied rateably)."""
    groups = {}
    for i, (p, m, stated, cap, shared) in aggs.items():
        if shared and p > 0.5:
            groups.setdefault(round(shared, 2), []).append(i)
    out = {i: (p, m, stated, cap) for i, (p, m, stated, cap, _) in aggs.items()}
    for limit, idx in groups.items():
        total = sum(min(out[i][2] or out[i][1], out[i][3] or math.inf) for i in idx)
        if total > limit > 0:
            f = limit / total
            for i in idx:
                p, m, stated, cap = out[i]
                out[i] = (p, m * f, stated * f if stated else stated, cap)
            log.info("shared cap %.2f on lines %s: sum %.2f -> scale %.3f", limit, idx, total, f)
    return out


# a and b relative to m (our median of t), from the official payoff table:
# - accepting an incoming a pays off iff P(t >= a) > 2/3, so b = the point where
#   p * P(T >= b) = 2/3 with T ~ m * exp(SIGMA * Z); b = 0 if p <= 2/3.
# - charging: a * P(t >= a) peaks below m; a = A_MULT * m. 0.85 from the backtest on games 1-2
#   (notes/DECISIONS.md): our LLM estimate m tends to sit slightly above t.
def b_mult(p, *, sigma):
    if p <= 2 / 3:
        return 0.0
    return math.exp(sigma * statistics.NormalDist().inv_cdf(max(1e-6, 1 - 2 / (3 * p))))


def price(p, m, stated, cap, fallback, *, a_mult, sigma):
    if stated:
        a = stated
        b = stated if p > 2 / 3 else 0.0
    else:
        base = m if m > 0 else fallback             # t = 0 lines still charge: costs nothing if rejected
        a = a_mult * base
        b = b_mult(p, sigma=sigma) * base
    if cap:
        a, b = min(a, cap), min(b, cap)
    return floor_c(a), ceil_c(b)
