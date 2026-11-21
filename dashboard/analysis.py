"""Explain settled transactions and replay hypothetical prices without side effects."""

from collections import defaultdict
import math

from calibrate import analyse, finite_number, score_submission


OUTCOMES = ("accepted_fair", "accepted_over", "accepted_unknown", "rejected_paid", "rejected_zero")


def _range(lower=None, upper=None):
    return {"lower": lower if lower is None or math.isfinite(lower) else None,
            "upper": upper if upper is None or math.isfinite(upper) else None}


def _successful_log(record, game):
    return (record is not None and record.get("game") == game and record.get("ok") is True
            and not record.get("dry") and record.get("echo_ok") is not False)


def _recorded_price(record, game, line):
    if record is None:
        return None, None
    if not _successful_log(record, game):
        return None, "Lokales Log bestätigt keine erfolgreiche Abgabe für diese Runde."
    final = record.get("final_ab")
    sources = record.get("line_sources")
    if not isinstance(final, dict) or not isinstance(sources, dict):
        return None, "Im älteren lokalen Log fehlt die bestätigte finale Abgabe."
    pair = final.get(str(line), final.get(line))
    source = sources.get(str(line), sources.get(line))
    if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not source:
        return None, "Für diese Position fehlt eine bestätigte lokale Abgabe."
    try:
        return tuple(finite_number(value, "recorded price") for value in pair), None
    except ValueError:
        return None, "Das lokale Log enthält ungültige Preise."


def _with_log(rows, line, team, pair):
    inference = analyse(rows, {line: pair}, team)[line]
    cap_lower = inference["c"][0]
    for row in inference["transactions"]:
        if row["issuer"] != team or not row["accepted"]:
            continue
        if row["amount"] + 0.011 < min(pair[0], cap_lower):
            raise ValueError("accepted payment contradicts cap lower bound")
    return inference


def _infer_line(rows, game, line, team, record):
    try:
        public = analyse(rows)[line]
    except ValueError as exc:
        return None, None, None, str(exc)
    pair, warning = _recorded_price(record, game, line)
    if pair is None or not any(team in (r["issuer"], r["reviewer"]) for r in rows):
        return public, None, warning, None
    try:
        return _with_log(rows, line, team, pair), pair, None, None
    except ValueError:
        warning = "Die lokale Endabgabe widerspricht den öffentlichen Zahlungen. Es gelten die öffentlichen Belege."
        return public, None, warning, None


def _legacy_prices(rows, game, line, team, record):
    if not _successful_log(record, game) or "final_ab" in record:
        return None
    tiers = record.get("tiers") or {}
    tier = tiers.get(record.get("winner")) or {}
    candidate = tier.get(str(line), tier.get(line))
    if not isinstance(candidate, (list, tuple)) or len(candidate) != 2:
        return None
    try:
        pair = tuple(finite_number(value, "legacy candidate") for value in candidate)
        _with_log(rows, line, team, pair)
    except ValueError:
        return None
    return {"a": pair[0], "b": pair[1]}


def _fairness(inference, team):
    if inference is None or team not in inference["a_bounds"]:
        return "unknown"
    verdict = inference["fair"].get(team)
    low, high = inference["a_bounds"][team]
    threshold_low, threshold_high = inference["t"]
    if verdict is True or high <= threshold_low:
        return "fair"
    # An upper bound on the cap implies the inclusive t <= c_upper / 4.
    if verdict is False or low >= threshold_high or low > inference["c"][1] / 4:
        return "over"
    return "unknown"


def _transaction(raw, inference, team):
    issuer, reviewer = raw["issuer"], raw["reviewer"]
    amount = finite_number(raw["amount"], "amount")
    if raw["accepted"]:
        outcome = "accepted_" + _fairness(inference, issuer)
    else:
        outcome = "rejected_paid" if amount > 0 else "rejected_zero"
    charge = _range()
    limit = _range()
    if inference is not None:
        charge = _range(*inference["a_bounds"][issuer])
        exact_b = inference["b_exact"].get(reviewer)
        limit = _range(exact_b, exact_b) if exact_b is not None else _range(*inference["b"][reviewer])
    return {"issuer": issuer, "reviewer": reviewer, "accepted": raw["accepted"], "amount": amount,
            "outcome": outcome, "charge": charge, "acceptance_limit": limit,
            "direction": "outgoing" if issuer == team else "incoming" if reviewer == team else "other"}


def _summary(rows, team):
    income = math.fsum(row["amount"] for row in rows if row["issuer"] == team)
    reviewing = [row for row in rows if row["reviewer"] == team]
    paid = math.fsum(row["amount"] for row in reviewing)
    counts = dict.fromkeys(OUTCOMES, 0)
    for row in reviewing:
        counts[row["outcome"]] += 1
    proven_over = math.fsum(row["amount"] for row in reviewing if row["outcome"] == "accepted_over")
    maybe_over = math.fsum(row["amount"] for row in reviewing if row["outcome"] == "accepted_unknown")
    penalty = math.fsum(row["amount"] / 3 for row in reviewing if row["outcome"] == "rejected_paid")
    return {"income": income, "paid": paid, "net": income - paid, "counts": counts,
            "review_penalty": penalty, "accepted_overpay": _range(proven_over, proven_over + maybe_over)}


def _own(inference, recorded, team, warning):
    data = {"a": None, "b": None, "a_bounds": _range(), "b_bounds": _range(), "gap": _range(),
            "charge_status": "unknown", "source": "unknown", "log_warning": warning}
    if inference is None or team not in inference["a_bounds"]:
        return data
    a = inference["a"][team] if team in inference["exact"] else None
    b = inference["b_exact"].get(team)
    data.update(a=a, b=b, a_bounds=_range(*inference["a_bounds"][team]),
                b_bounds=_range(b, b) if b is not None else _range(*inference["b"].get(team, (0, math.inf))),
                charge_status=_fairness(inference, team), source="runner" if recorded else "public")
    if a is not None:
        low, high = inference["t"]
        data["gap"] = _range(a - high, a - low)
    return data


def _prepare(data, team, record):
    grouped = defaultdict(list)
    observed_teams = set()
    seen = set()
    for row in data["tx"]:
        if not isinstance(row, dict):
            raise ValueError("transaction must be an object")
        line, issuer, reviewer = row.get("line_item_index"), row.get("issuer"), row.get("reviewer")
        if (type(line) is not int or line < 0 or not isinstance(issuer, str) or not issuer
                or not isinstance(reviewer, str) or not reviewer or issuer == reviewer
                or type(row.get("accepted")) is not bool):
            raise ValueError("malformed transaction")
        key = (line, issuer, reviewer)
        if key in seen:
            raise ValueError("duplicate transaction")
        seen.add(key)
        grouped[line].append(row)
        observed_teams.update((issuer, reviewer))
    teams = sorted(set(data.get("teams", ())) | observed_teams)
    # One observed position contains both ordered directions for every pair of teams.
    expected_pairs = len(teams) * (len(teams) - 1)
    complete = bool(grouped and len(teams) >= 2 and team in teams)
    if _successful_log(record, data["game"]) and isinstance(record.get("final_ab"), dict):
        expected_positions = {str(i) for i in record["final_ab"]}
        complete = complete and expected_positions.issubset({str(i) for i in grouped})
    prepared, lines, all_rows = {}, [], []
    for line, raw_rows in sorted(grouped.items()):
        line_complete = len(raw_rows) == expected_pairs
        complete = complete and line_complete
        inference, recorded, warning, error = _infer_line(raw_rows, data["game"], line, team, record)
        prepared[line] = inference
        rows = [_transaction(raw, inference, team) for raw in raw_rows]
        rows.sort(key=lambda r: (r["issuer"], r["reviewer"]))
        all_rows.extend(rows)
        own = _own(inference, recorded, team, warning)
        own["legacy"] = (_legacy_prices(raw_rows, data["game"], line, team, record)
                         if inference is not None and team in inference["a_bounds"] else None)
        lines.append({"index": line, "complete": line_complete,
                      "t": _range(*inference["t"]) if inference is not None else _range(),
                      "c": _range(*inference["c"]) if inference is not None else _range(),
                      "own": own, "summary": _summary(rows, team),
                      "transactions": rows, "inference_error": error})
    result = {"game": data["game"], "team": team, "complete": complete,
              "transaction_count": len(all_rows), "summary": _summary(all_rows, team),
              "lines": lines, "teams": [{"name": name, **_summary(all_rows, name)} for name in teams]}
    return result, prepared


def analyse_game(data, team, record=None):
    """Return cashflows and evidence-based bounds by completed game and position.

    Every amount is in euros. ``t`` is lower-inclusive/upper-exclusive and own
    ``gap`` is the signed difference between the actual charge and that interval.
    The team's ``counts`` categorize only the transactions it reviewed.
    """
    result, _ = _prepare(data, team, record)
    return result


def _simulation_result(actual, low, high):
    if math.isclose(actual, low, rel_tol=0, abs_tol=1e-9):
        low = actual
    if math.isclose(actual, high, rel_tol=0, abs_tol=1e-9):
        high = actual
    exact = math.isfinite(low) and math.isclose(low, high, rel_tol=0, abs_tol=1e-9)
    return {"actual_net": actual, **_range(low, high), "delta": _range(low - actual, high - actual),
            "exact": exact}


def _normalized_changes(changes, positions):
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object indexed by position")
    result = {}
    for key, values in changes.items():
        try:
            line = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid position") from exc
        if isinstance(key, bool) or str(line) != str(key) or line not in positions:
            raise ValueError(f"unknown position: {key}")
        if not isinstance(values, dict) or set(values) - {"a", "b"}:
            raise ValueError("each changed position must define a and/or b")
        result[line] = {key: finite_number(value, key) for key, value in values.items() if value is not None}
    return result


def simulate_game(data, team, changes, record=None):
    """Replay proposed a/b values against the same opponents and settled evidence.

    Missing prices stay at the observed result, including for games whose logged
    limit is unknown. Wide bounds are expected when opponent prices or t are censored.
    """
    actual, prepared = _prepare(data, team, record)
    changes = _normalized_changes(changes, prepared)
    actual_net = actual["summary"]["net"]
    if not actual["complete"]:
        return {"available": False, "reason": "Transaktionsdaten für diese Runde sind unvollständig.",
                "lines": [], "actual_net": actual_net, **_range(), "delta": _range(), "exact": False}
    lines = []
    for line in actual["lines"]:
        index, summary = line["index"], line["summary"]
        changes_at_line = changes.get(index, {})
        inference = prepared[index]
        if changes_at_line and inference is None:
            return {"available": False, "reason": f"Widersprüchliche öffentliche Daten für Position {index}.",
                    "lines": [], "actual_net": actual_net, **_range(), "delta": _range(), "exact": False}
        incoming = {"lower": summary["income"], "upper": summary["income"]}
        outgoing = {"lower": -summary["paid"], "upper": -summary["paid"]}
        if "a" in changes_at_line:
            incoming = score_submission({**inference, "a_bounds": {}}, team, changes_at_line["a"], 0)
        if "b" in changes_at_line:
            outgoing = score_submission({**inference, "b": {}}, team, 0, changes_at_line["b"])
        lower = incoming["lower"] + outgoing["lower"]
        upper = incoming["upper"] + outgoing["upper"]
        lines.append(({"index": index, **_simulation_result(summary["net"], lower, upper)}, lower, upper))
    lower = sum(row[1] for row in lines)
    upper = sum(row[2] for row in lines)
    return {"available": True, **_simulation_result(actual_net, lower, upper),
            "lines": [row[0] for row in lines]}
