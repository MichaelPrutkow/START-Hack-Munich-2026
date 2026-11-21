"""Vote aggregation, shared caps and deterministic prices. Settings are explicit arguments."""
import logging
import math
import statistics
from collections import Counter
from collections.abc import Mapping

log = logging.getLogger("c2f")


def floor_c(x):
    x = pos(x)
    # Above this magnitude floats cannot represent cents; avoid overflowing x * 100.
    return (x if x >= 2 ** 53 / 100 else math.floor(round(x * 100, 6)) / 100) if x else 0.0


def ceil_c(x):
    x = pos(x)
    return (x if x >= 2 ** 53 / 100 else math.ceil(round(x * 100, 6)) / 100) if x else 0.0


def pos(x):
    try:
        if isinstance(x, bool):
            return None
        x = float(x)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value, name, *, minimum=0.0, maximum=None):
    """Reject invalid inputs instead of letting NaNs pass comparison-based guards."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f"{name} is outside its finite allowed range")
    return number


def estimate_votes(votes, *, disagree=1.5):
    """Summarize ONE line without treating model disagreement as evidence for a higher price.

    ``log_spread`` is an ensemble diagnostic, not a calibrated confidence interval. It is
    the RMS log-distance from the median, with each distance capped at log(4) so a single
    absurd outlier cannot dominate it. Coverage dispersion is retained separately.
    Malformed votes are discarded; callers should retain their fallback when valid_count=0.
    """
    disagree = _number(disagree, "disagree", minimum=1.0)
    votes = list(votes)
    valid = []
    for vote in votes:
        if not isinstance(vote, Mapping):
            continue
        try:
            p = _number(vote.get("covered_prob"), "covered_prob", maximum=1.0)
            m = _number(vote.get("t_if_covered"), "t_if_covered")
            if m <= 0:
                continue
        except ValueError:
            continue
        valid.append((vote, p, m))

    probabilities = [p for _, p, _ in valid]
    vals = sorted(m for _, _, m in valid)
    logs = [math.log(m) for m in vals]
    center = statistics.median(logs) if logs else 0.0
    # Preserve an odd-sample median exactly rather than introducing exp/log roundoff.
    m = (vals[len(vals) // 2] if len(vals) % 2 else math.exp(center)) if vals else 0.0
    spread = math.sqrt(statistics.fmean(min(abs(x - center), math.log(4)) ** 2 for x in logs)) if logs else 0.0
    p_spread = statistics.pstdev(probabilities) if probabilities else 0.0
    log_range = logs[-1] - logs[0] if logs else 0.0

    anchors, validity = {}, {}
    for key, output in (("stated_value", "stated"), ("stated_cap", "cap"), ("shared_cap", "shared")):
        amounts, invalid = [], 0
        for vote, _, _ in valid:
            if vote.get(key) is None:
                continue
            try:
                amounts.append(round(_number(vote[key], key), 2))
            except ValueError:
                invalid += 1
        counts = Counter(amounts)
        value, support = counts.most_common(1)[0] if counts else (None, 0)
        trusted = support * 2 > len(valid)
        anchors[output] = value if trusted else None
        validity[output] = {"trusted": trusted, "support": support,
                            "reported": len(amounts), "invalid": invalid,
                            "conflicting": len(counts) > 1}

    reasons = []
    if not valid:
        reasons.append("no_valid_votes")
    elif len(valid) == 1:
        reasons.append("single_vote")
    if len(valid) != len(votes):
        reasons.append("invalid_votes")
    amount_disagreement = len(valid) > 1 and log_range >= math.log(disagree)
    if amount_disagreement:
        reasons.append("amount_disagreement")
    if p_spread >= 0.2:
        reasons.append("coverage_disagreement")
    if any(v["conflicting"] or v["invalid"] for v in validity.values()):
        reasons.append("document_anchor_disagreement")
    clauses = list(dict.fromkeys(v["clause"].strip() for v, _, _ in valid
                                if isinstance(v.get("clause"), str) and v["clause"].strip()))
    return {"p": statistics.median(probabilities) if probabilities else 0.0, "m": m,
            **anchors, "log_spread": spread, "log_range": log_range, "p_spread": p_spread,
            "vote_count": len(votes), "valid_count": len(valid), "invalid_count": len(votes) - len(valid),
            "clauses": clauses, "anchor_validity": validity,
            "disputed": amount_disagreement or p_spread >= 0.2 or any(v["conflicting"] for v in validity.values()),
            "reason": "; ".join(reasons) if reasons else "consistent_votes", "reasons": reasons}


def aggregate(votes, *, disagree):
    """Compatibility API: votes for ONE line -> (p, m, stated, cap, shared)."""
    estimate = estimate_votes(votes, disagree=disagree)
    return tuple(estimate[key] for key in ("p", "m", "stated", "cap", "shared"))


def select_focus_items(items, estimates, limit=2):
    """Select the largest estimated line, then other lines by monetary disagreement impact.

    This is a call-budget priority, not an estimate of expected profit. Missing estimates
    get no invented price anchor. Returns an invoice subset for build_focus_prompt.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if not limit or not items:
        return {}

    def metrics(index):
        estimate = estimates.get(index, estimates.get(str(index), {}))
        if not isinstance(estimate, Mapping):
            estimate = {}
        amount = estimate.get("stated") if estimate.get("stated") is not None else estimate.get("m")
        value = pos(amount) or 0.0
        if estimate.get("cap") is not None:
            try:
                value = min(value, _number(estimate["cap"], "cap"))
            except ValueError:
                pass
        spread = min(pos(estimate.get("log_spread")) or 0.0, math.log(4))
        p_spread = min(pos(estimate.get("p_spread")) or 0.0, 0.5)
        # Use logs for ranking so large finite monetary values cannot overflow.
        log_value = math.log(value) if value > 0 else -math.inf
        score = log_value + math.log1p(spread + 2 * p_spread + bool(estimate.get("disputed")))
        return value, score

    metrics_by_index = {i: metrics(i) for i in items}
    largest = max(sorted(items), key=lambda i: metrics_by_index[i][0])
    remaining = sorted((i for i in items if i != largest),
                       key=lambda i: (-metrics_by_index[i][1], i))
    return {i: items[i] for i in [largest, *remaining][:limit]}


def apply_shared_caps(aggs):
    """aggs {i: (p, m, stated, cap, shared)} -> same without shared, values scaled pro rata where
    the lines that share one limit add up to more than it (policy wording: applied rateably).

    The returned cap also includes each line's allocation of the shared budget, so a larger
    issuer multiplier or upward cent rounding cannot breach the group limit. Callers that
    retain estimates across passes must preserve the original line cap separately.
    Legacy tuples identify shared groups by amount; distinct equal-valued clauses still need
    separate group identifiers before they can be distinguished here.
    """
    groups = {}
    for i, (p, m, stated, cap, shared) in aggs.items():
        if shared is not None and p > 0.5:
            groups.setdefault(round(shared, 2), []).append(i)
    out = {i: (p, m, stated, cap) for i, (p, m, stated, cap, _) in aggs.items()}
    for limit, idx in groups.items():
        total = sum(min(out[i][2] if out[i][2] is not None else out[i][1],
                        out[i][3] if out[i][3] is not None else math.inf) for i in idx)
        f = min(1.0, limit / total) if total > 0 else 1.0
        for i in idx:
            p, m, stated, cap = out[i]
            bound = cap if cap is not None else math.inf
            amount = min(stated if stated is not None else m, bound)
            allocation = floor_c(limit * (amount / total)) if total > 0 else 0.0
            # The denominator respects line caps. Scale those same bounded
            # amounts, or the capped line can consume the shared limit again.
            out[i] = (p, min(m, bound) * f if f < 1 else m,
                      min(stated, bound) * f if stated is not None and f < 1 else stated,
                      min(bound, allocation))
        if f < 1:
            log.info("shared cap %.2f on lines %s: sum %.2f -> scale %.3f", limit, idx, total, f)
    return out


# a and b relative to m (our median of t), from the official payoff table:
# - accepting an incoming a pays off iff P(t >= a) > 2/3, so b = the point where
#   p * P(T >= b) = 2/3 with T ~ m * exp(SIGMA * Z); b = 0 if p <= 2/3.
# - charging: a * P(t >= a) peaks below m; a = A_MULT * m. 0.85 from the backtest on games 1-2
#   (notes/DECISIONS.md): our LLM estimate m tends to sit slightly above t.
def b_mult(p, *, sigma):
    p = _number(p, "p", maximum=1.0)
    sigma = _number(sigma, "sigma")
    if p <= 2 / 3:
        return 0.0
    return math.exp(sigma * statistics.NormalDist().inv_cdf(max(1e-6, 1 - 2 / (3 * p))))


def price(p, m, stated, cap, fallback, *, a_mult, sigma, uncertainty=0.0, bias=1.0,
          uncertainty_aware=True):
    """Price with an optional ensemble-spread adjustment and validated multiplicative bias.

    Zero uncertainty preserves the established A_MULT/SIGMA baseline. With uncertainty,
    demand receives a separate exp(-spread/2) discount (at most 50%), while reviewer acceptance uses
    sqrt(SIGMA**2 + spread**2) in its existing 2/3 quantile rule. These are conservative
    heuristics for evaluation, not a measured profit improvement or an issuer optimum.
    Exact document amounts bypass both adjustments; caps and coverage still apply.
    """
    p = _number(p, "p", maximum=1.0)
    m, fallback = _number(m, "m"), _number(fallback, "fallback")
    a_mult, sigma = _number(a_mult, "a_mult"), _number(sigma, "sigma")
    uncertainty, bias = _number(uncertainty, "uncertainty"), _number(bias, "bias")
    stated = _number(stated, "stated") if stated is not None else None
    cap = _number(cap, "cap") if cap is not None else None
    if stated is not None:
        a = stated
        b = stated if p > 2 / 3 else 0.0
    else:
        base = m if m > 0 else fallback             # t = 0 lines still charge: costs nothing if rejected
        base *= bias
        a = a_mult * base
        spread = min(uncertainty, math.log(4)) if uncertainty_aware else 0.0
        a *= math.exp(-0.5 * spread)
        b = b_mult(p, sigma=math.hypot(sigma, spread)) * base
    if cap is not None:
        a, b = min(a, cap), min(b, cap)
    _number(a, "charge_price")
    _number(b, "acceptance_limit")
    return floor_c(a), ceil_c(b)
