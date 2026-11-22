"""Separate price-estimate diagnostics and role-specific, bounded counterfactuals."""
import math


def _get(mapping, index, default=None):
    return mapping.get(str(index), mapping.get(index, default))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def review_line(inference, transactions, team, final, estimate, control, index):
    """Called only after full submission/settlement reconciliation by feedback."""
    import calibrate as C
    lo, hi = inference["t"]
    m = estimate.get("m_raw", estimate.get("m"))
    p = estimate.get("p")
    diagnostic = {"valuation_category": estimate.get("valuation_category", "unknown"),
                  "m_raw": m if _number(m) else None, "covered_prob": p if _number(p) else None,
                  "estimate_position": "unavailable", "variants": {}}
    if _number(m) and m > 0:
        diagnostic["estimate_position"] = ("policy_adjusted" if any(estimate.get(k) is not None for k in ("cap", "shared", "stated")) else
                                           "no_positive_fair_evidence" if lo <= 0 else
                                           "below_known_fair" if m <= lo else
                                           "above_known_unfair" if m >= hi else "unresolved")
    income = math.fsum(t["amount"] for t in transactions if t["issuer"] == team)
    paid = math.fsum(t["amount"] for t in transactions if t["reviewer"] == team)
    actual = income - paid
    candidates = {}
    a, b = final
    # Removing a recall cap isolates its effect; positive delta favors no cap.
    pre_a = estimate.get("a_before_recall", a)
    pre_b = estimate.get("b_before_recall", b)
    if estimate.get("recall_applied") or estimate.get("recall_b_applied"):
        candidates["without_recall"] = (pre_a, pre_b)
    pair = _get(control.get("ab", {}), index) if control.get("status") == "completed" else None
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        candidates["independent_control"] = pair
    for name, pair in candidates.items():
        if not all(_number(value) for value in pair):
            continue
        for role in ("a", "b", "both"):
            incoming = {"lower": income, "upper": income}
            outgoing = {"lower": -paid, "upper": -paid}
            if role in ("a", "both") and pair[0] != a:
                incoming = C.score_submission({**inference, "a_bounds": {}}, team, pair[0], 0)
            if role in ("b", "both") and pair[1] != b:
                outgoing = C.score_submission({**inference, "b": {}}, team, 0, pair[1])
            diagnostic["variants"][f"{name}_{role}"] = {
                "lower": incoming["lower"] + outgoing["lower"] - actual,
                "upper": incoming["upper"] + outgoing["upper"] - actual}
    return diagnostic


def summarize(reports):
    """Report per-game differences and robustness without the best game; no activation."""
    by_variant = {}
    for game in reports:
        for row in game["lines"]:
            for name, delta in row.get("decision_review", {}).get("variants", {}).items():
                bucket = by_variant.setdefault(name, {})
                entry = bucket.setdefault(game["game"], {"lines": 0, "lower": 0.0, "upper": 0.0})
                entry["lines"] += 1
                for bound in ("lower", "upper"):
                    entry[bound] += delta[bound]
    result = {}
    for name, games in by_variant.items():
        best = max(games, key=lambda g: games[g]["lower"])
        result[name] = {"games": games, "lines": sum(g["lines"] for g in games.values()),
                        "lower": sum(g["lower"] for g in games.values()),
                        "upper": sum(g["upper"] for g in games.values()),
                        "without_best_game_lower": sum(v["lower"] for g, v in games.items() if g != best),
                        "best_game": best}
    return result


def group_diagnostics(reports):
    """Descriptive conditional-price errors by new logged category; never apply bias."""
    import statistics
    groups = {}
    for game in reports:
        for row in game['lines']:
            d = row.get('decision_review', {})
            group = d.get('valuation_category', 'unknown')
            if group == 'unknown' or d.get('estimate_position') in ('unavailable', 'policy_adjusted', 'no_positive_fair_evidence'):
                continue
            m = d.get('m_raw')
            if not _number(m) or m <= 0 or row['t'][0] <= 0:
                continue
            groups.setdefault(group, []).append((row['t'][0]/m,
                row['t'][1]/m if row['t'][1] is not None else math.inf, game['game']))
    return {category: {'lines': len(values), 'games': sorted({v[2] for v in values}),
                      'median_fair_to_estimate_lower': round(statistics.median(v[0] for v in values), 4),
                      'median_fair_to_estimate_upper': (round(statistics.median(v[1] for v in values), 4)
                          if math.isfinite(statistics.median(v[1] for v in values)) else None),
                      'activation': 'none'} for category, values in groups.items()}
