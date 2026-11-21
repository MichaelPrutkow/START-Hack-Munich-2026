"""Conservative, offline learning from this tournament's completed settlements.

The artifact is private case data. Unknown fair totals stay intervals; public accepted
payments are never treated as exact charges or market unit prices. This module performs
no API/LLM calls and never imports the live runner.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile

from .invoices import norm_unit
from .pricing import ceil_c, floor_c


SCHEMA_VERSION = 3
ALGORITHM = "interval-fit-historical-bootstrap-v3"
CAMPAIGN = "2026-11"
MIN_GAMES, MIN_TRAIN, MIN_HOLDOUT = 6, 16, 8
MIN_GAIN = 0.05
MIN_COVERED_PROB = 0.9
A_MULT_GRID = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05)
SIGMA_GRID = (0.30, 0.35, 0.50, 0.65, 0.80, 1.00, 1.20, 1.40)
PARAMETER_KEYS = ("a_mult", "bias", "sigma")
CONTROLS = {"minimum_games": MIN_GAMES, "minimum_fit_lines": MIN_TRAIN,
            "minimum_holdout_lines": MIN_HOLDOUT, "minimum_gain": MIN_GAIN,
            "minimum_covered_probability": MIN_COVERED_PROB, "a_mult_grid": list(A_MULT_GRID),
            "sigma_grid": list(SIGMA_GRID),
            "selection": "training conservative net only; frozen chronological holdout",
            "baseline": "actual observed holdout net; denominator max(abs(net), 1 EUR)",
            "scope": "uncapped, no stated amount, no uncertainty adjustment",
            "historical_training": "verified raw predictions before first target-cohort game; never holdout"}


def policy_fingerprint(policy):
    return hashlib.sha256(policy.encode("utf-8")).hexdigest()


def _finite(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean amount")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("invalid amount")
    return value


def _get(mapping, key, default=None):
    return mapping.get(str(key), mapping.get(key, default))


def _identity(rec, tier):
    def field(name):
        value = rec.get(name)
        return value.get(tier) if isinstance(value, dict) else value
    return {"tier": tier, "model": (rec.get("models") or {}).get(tier),
            "prompt_hash": field("prompt_hash"), "config_hash": field("config_hash")}


def _range(lo, hi):
    return {"lower": math.floor(lo * 100 + 1e-8) / 100 if math.isfinite(lo) else None,
            "upper": math.ceil(hi * 100 - 1e-8) / 100 if math.isfinite(hi) else None}


def _strict_json(text):
    def reject(value):
        raise ValueError(f"non-finite JSON value: {value}")
    return json.loads(text, parse_constant=reject)


def _reconcile(rec, data, team):
    """Require actual final submission and a full, consistent settlement matrix."""
    import calibrate as C

    gid = rec["game"]
    if not isinstance(gid, int) or isinstance(gid, bool) or gid <= 0 or data.get("game") != gid:
        raise ValueError("game identifier mismatch")
    if rec.get("campaign", CAMPAIGN) != CAMPAIGN or data.get("campaign", CAMPAIGN) != CAMPAIGN:
        raise ValueError("outside November tournament")
    if rec.get("dry") or rec.get("ok") is not True or rec.get("echo_ok") is False:
        raise ValueError("round was not a successful real submission")
    if data.get("completed") is False or data.get("status", "completed") != "completed":
        raise ValueError("game is not completed")
    items, final, sources = rec.get("items"), rec.get("final_ab"), rec.get("line_sources")
    if not items or not final or not sources:
        raise ValueError("missing final_ab/items/line_sources; winner is not submission provenance")
    indices = {int(i) for i in items}
    if {int(i) for i in final} != indices or {int(i) for i in sources} != indices:
        raise ValueError("incomplete final submission provenance")
    ab = {}
    for line in indices:
        pair = _get(final, line)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("invalid submitted pair")
        ab[line] = [_finite(v) for v in pair]
        if not isinstance(_get(sources, line), str) or not _get(sources, line):
            raise ValueError("missing per-line source tier")
    teams, tx = data.get("teams", []), data.get("tx", [])
    if len(set(teams)) != len(teams) or len(teams) < 2 or team not in teams:
        raise ValueError("incomplete team roster")
    expected = {(i, issuer, reviewer) for i in indices for issuer in teams for reviewer in teams
                if issuer != reviewer}
    observed = set()
    for row in tx:
        key = (row["line_item_index"], row["issuer"], row["reviewer"])
        if key in observed or key not in expected or type(row.get("accepted")) is not bool:
            raise ValueError("duplicate, unexpected, or malformed transaction")
        observed.add(key)
        _finite(row["amount"])
    if expected != observed:
        raise ValueError("incomplete settlement matrix")
    lines = C.analyse(tx, submissions=ab, team=team)
    for line, d in lines.items():
        if d.get("valid") is False or d.get("errors"):
            raise ValueError("contradictory settlement bounds")
        lo, hi = d["t"]
        if not math.isfinite(lo) or lo < 0 or hi <= lo:
            raise ValueError("contradictory fair-total interval")
        a, b = ab[line]
        for row in (r for r in tx if r["line_item_index"] == line):
            amount = float(row["amount"])
            if row["issuer"] == team:
                if row["accepted"]:
                    if amount > a + 0.02 or amount + 0.02 < min(a, 4 * lo):
                        raise ValueError("accepted own payment contradicts final charge/cap")
                elif amount > 0 and abs(amount - 1.5 * a) > 0.02:
                    raise ValueError("own rejection penalty contradicts final charge")
                elif amount == 0 and a <= lo:
                    raise ValueError("unpaid fair own charge")
            if row["reviewer"] == team:
                bounds = d.get("a_bounds", {}).get(row["issuer"], [0, math.inf])
                if row["accepted"] and (amount > b + 0.02 or bounds[0] > b + 0.02):
                    raise ValueError("acceptance contradicts final acceptance limit")
                if not row["accepted"] and bounds[1] <= b:
                    raise ValueError("rejection contradicts final acceptance limit")
    return lines, ab


def _line_loss(d, tx, team, a):
    """Actual cashflows plus bounded regret; category bounds are not point estimates."""
    lo, hi = d["t"]
    incoming = [r for r in tx if r["issuer"] == team]
    outgoing = [r for r in tx if r["reviewer"] == team]
    income = sum(float(r["amount"]) for r in incoming)
    paid = sum(float(r["amount"]) for r in outgoing)
    overpay_lo = overpay_hi = penalty = 0.0
    for row in outgoing:
        amount = float(row["amount"])
        if not row["accepted"]:
            penalty += amount / 3  # 1.5a paid instead of a: extra 0.5a.
            continue
        issuer = row["issuer"]
        fair = d["fair"].get(issuer)
        bounds = d.get("a_bounds", {}).get(issuer, [0, math.inf])
        if fair is False or bounds[0] >= hi:
            overpay_lo += amount
            overpay_hi += amount
        elif fair is not True and bounds[1] > lo:
            overpay_hi += amount
    tag = "fair" if a <= lo else "unfair" if a >= hi or d["fair"].get(team) is False else "unresolved"
    # An oracle may choose ANY fair charge, including our charge when it is fair.
    # Charging the established lower bound guarantees at least n*lo; any fair
    # charge yields at most 1.5*n*hi. Infinity stays unknown, never a guessed t.
    gap = _range(max(0.0, len(incoming) * lo - income), max(0.0, 1.5 * len(incoming) * hi - income))
    losses = {"reviewer_overpay": _range(overpay_lo, overpay_hi),
              "reviewer_penalty": _range(penalty, penalty),
              "issuer_unfair": gap if tag == "unfair" else _range(0, 0),
              "issuer_undercharge": gap if tag == "fair" else _range(0, 0),
              "issuer_unresolved": gap if tag == "unresolved" else _range(0, 0)}
    return {"income": round(income, 2), "paid": round(paid, 2), "net": round(income - paid, 2),
            "charge_status": tag, "losses": losses}


def _summarize(lines):
    result = {"lines": len(lines), "net": round(sum(x["net"] for x in lines), 2), "losses": {}}
    for key in ("reviewer_overpay", "reviewer_penalty", "issuer_unfair", "issuer_undercharge", "issuer_unresolved"):
        values = [r["losses"][key] for r in lines]
        result["losses"][key] = _range(sum(v["lower"] for v in values),
            math.inf if any(v["upper"] is None for v in values) else sum(v["upper"] for v in values))
    return result


def _field_counts(lines, tx, team):
    counts = {"claims": {label: 0 for label in ("fair", "unfair", "unknown")},
              "decisions": {label: {"accepted": 0, "rejected": 0} for label in ("fair", "unfair", "unknown")}}
    for line, d in lines.items():
        for issuer, bounds in d.get("a_bounds", {}).items():
            fair = d["fair"].get(issuer)
            if bounds[0] <= 0 and fair is not False:
                continue  # Zero/default prices are not positive-claim evidence.
            label = ("fair" if fair is True or bounds[1] <= d["t"][0] else
                     "unfair" if fair is False or bounds[0] >= d["t"][1] else "unknown")
            counts["claims"][label] += 1
            for row in tx:
                if row["line_item_index"] == line and row["issuer"] == issuer and row["reviewer"] != team:
                    counts["decisions"][label]["accepted" if row["accepted"] else "rejected"] += 1
    return counts


def _field_summary(reports):
    counts = {"claims": {label: 0 for label in ("fair", "unfair", "unknown")},
              "decisions": {label: {"accepted": 0, "rejected": 0} for label in ("fair", "unfair", "unknown")}}
    for report in reports:
        for label in counts["claims"]:
            counts["claims"][label] += report["field_counts"]["claims"][label]
            for decision in counts["decisions"][label]:
                counts["decisions"][label][decision] += report["field_counts"]["decisions"][label][decision]
    for label, event in (("fair", "rejected"), ("unfair", "accepted")):
        known = sum(counts["decisions"][label].values())
        counts[f"{label}_{event}_rate"] = counts["decisions"][label][event] / known if known else None
        counts[f"{label}_decision_count"] = known
    counts["scope"] = "opponent reviewer decisions on positive claims; unknown fairness remains separate"
    return counts


def _fit_candidate(rows, checkpoint=None):
    """One fixed interval-likelihood fit, trained chronologically, then shrunk."""
    N = statistics.NormalDist()
    iv = [(math.log(r["d"]["t"][0] / r["m"]),
           math.log(r["d"]["t"][1] / r["m"])) for r in rows]
    best = (-math.inf, 0.0, 0.35)
    # Include right-censored observations; never replace an interval by its midpoint.
    for mu_i in range(-90, 31, 3):
        if checkpoint:
            checkpoint()
        mu = mu_i / 100
        for sd_i in range(20, 141, 5):
            sd = sd_i / 100
            ll = sum(math.log(max(1e-12, (N.cdf((hi - mu) / sd) if math.isfinite(hi) else 1.0)
                                  - N.cdf((lo - mu) / sd))) for lo, hi in iv)
            if ll > best[0]:
                best = ll, mu, sd
    weight = len(rows) / (len(rows) + 40)
    baseline = statistics.median(r["baseline_sigma"] for r in rows)
    return {"bias": round(min(1.25, max(0.75, math.exp(best[1] * weight))), 4),
            "sigma": round(min(1.40, max(0.2, baseline + weight * (best[2] - baseline))), 4)}


def _score_rows(rows, params, team, checkpoint=None):
    import calibrate as C

    lower = upper = 0.0
    for row in rows:
        if checkpoint:
            checkpoint()
        # m is always the original estimator output, never the previously biased price.
        m = row["m"] * params["bias"]
        a = floor_c(params["a_mult"] * m)
        b = ceil_c(C.rule_b(row["p"], m, params["sigma"]))
        score = C.score_submission(row["d"], team, a, b)
        lower += score["lower"]
        upper += score["upper"]
    return lower, upper


def _calibrate(rows, identity, team, checkpoint=None, historical_rows=()):
    games = sorted({r["game"] for r in rows})
    n_holdout = max(2, len(games) // 3)
    train_games, holdout_games = games[:-n_holdout], games[-n_holdout:]
    train = [r for r in rows if r["game"] in train_games]
    # Missing versions do not invalidate observed predictions. Borrow them only
    # for fitting when this target cohort lacks training evidence. Validation
    # always uses the target's own fully identified, later games.
    historical = []
    if sum(r["d"]["t"][0] > 0 for r in train) < MIN_TRAIN:
        historical = [r for r in historical_rows if r["game"] < min(games)]
        train = historical + train
        train_games = sorted({r["game"] for r in train})
    fit = [r for r in train if r["d"]["t"][0] > 0]
    holdout = [r for r in rows if r["game"] in holdout_games]
    baseline_params = {"a_mult": statistics.median(r["baseline_alpha"] for r in (train or rows)),
                       "bias": 1.0, "sigma": min(1.40, max(0.2,
                           statistics.median(r["baseline_sigma"] for r in (train or rows))))}
    evidence = [{"game": r["game"], "line": r["line"], "m": r["m"], "p": r["p"],
                 "applied": r["applied_parameters"], "baseline": [r["baseline_alpha"], r["baseline_sigma"]],
                 "transactions": sorted([(x["issuer"], x["reviewer"], x["accepted"], x["amount"])
                                           for x in r["d"]["transactions"]]),
                 "source_identity": r["identity"], "historical_only": bool(r.get("historical_only"))}
                for r in train + holdout]
    evidence_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, allow_nan=False).encode()).hexdigest()
    candidate = {**identity, "algorithm": ALGORITHM, "evidence_hash": evidence_hash,
                 "status": "shadow", "activation": "none",
                 "reason": "insufficient chronological evidence", "reason_code": "insufficient_samples",
                 "sample_count": len(train) + len(holdout), "train_count": len(train), "holdout_count": len(holdout),
                 "training_source": "historical_bootstrap" if historical else "same_estimator",
                 "historical_train_count": len(historical),
                 "historical_train_games": sorted({r["game"] for r in historical}),
                 "historical_sources": [{"game": r["game"], "line": r["line"], **r["identity"]}
                                        for r in historical],
                 "target_cohort_games": games,
                 "fit_count": len(fit), "fit_population": "confirmed-covered training lines; holdout includes all eligible predictions",
                 "fit_limitation": "positive-bound training selection may bias distribution fit; promotion depends on all eligible holdout lines",
                 "calibrated_observation_count": sum(r["calibration_applied"] for r in rows),
                 "fit_intervals": {"two_sided": sum(math.isfinite(r["d"]["t"][1]) for r in fit),
                                   "right_censored": sum(not math.isfinite(r["d"]["t"][1]) for r in fit)},
                 "train_games": train_games, "holdout_games": holdout_games,
                 "min_covered_prob": MIN_COVERED_PROB, "minimum_gain": MIN_GAIN,
                 "baseline_parameters": baseline_params, "active_parameters": None,
                 "training_scores": [], **baseline_params}
    if not fit:
        candidate["proposed_parameters"] = dict(baseline_params)
        return candidate
    candidate.update(_fit_candidate(fit, checkpoint))
    # Select the charge multiplier AND reviewer spread using TRAINING settlements only.
    # Michael's field replay showed why a narrow fitted spread need not be optimal
    # against opponents. Keep the fitted spread in the grid and freeze before holdout.
    fitted_sigma = candidate["sigma"]
    candidate["fitted_sigma"] = fitted_sigma
    scores = []
    for alpha in A_MULT_GRID:
        for sigma in sorted(set(SIGMA_GRID + (fitted_sigma,))):
            lo, hi = _score_rows(train, {**candidate, "a_mult": alpha, "sigma": sigma}, team, checkpoint)
            candidate["training_scores"].append({"a_mult": alpha, "sigma": sigma, **_range(lo, hi)})
            if math.isfinite(lo) and math.isfinite(hi):
                scores.append((lo, -abs(alpha - baseline_params["a_mult"]),
                               -abs(sigma - fitted_sigma), -alpha, -sigma, alpha, sigma))
    if scores:
        candidate["a_mult"], candidate["sigma"] = max(scores)[-2:]
    candidate["proposed_parameters"] = {key: candidate[key] for key in PARAMETER_KEYS}
    if not scores:
        candidate.update(reason="training payoff bounds are unbounded", reason_code="unbounded_training")
        return candidate
    if (len(set(train_games + holdout_games)) < MIN_GAMES or len(fit) < MIN_TRAIN
            or len(holdout) < MIN_HOLDOUT or len(holdout_games) < 2):
        return candidate
    baseline = sum(r["net"] for r in holdout)
    lower, upper = _score_rows(holdout, candidate, team, checkpoint)
    candidate["holdout_by_game"] = [{"game": gid,
        "baseline_net": round(sum(r["net"] for r in holdout if r["game"] == gid), 2),
        "candidate": _range(*_score_rows([r for r in holdout if r["game"] == gid], candidate, team, checkpoint))}
        for gid in holdout_games]
    if not math.isfinite(lower) or not math.isfinite(upper):
        candidate.update(holdout_baseline_net=round(baseline, 2), holdout_candidate=_range(lower, upper),
                         conservative_gain=None, reason="held-out payoff bounds are unbounded", reason_code="unbounded_holdout")
        return candidate
    gain = (lower - baseline) / max(abs(baseline), 1.0)
    candidate.update(holdout_baseline_net=round(baseline, 2), holdout_candidate=_range(lower, upper),
                     conservative_gain=round(gain, 6), reason="held-out lower bound does not improve actual baseline by 5%",
                     reason_code="holdout_gain_below_threshold")
    if math.isfinite(gain) and gain >= MIN_GAIN:
        candidate.update(status="validated", activation="new", active_parameters=candidate["proposed_parameters"].copy(),
                         reason="frozen training parameters improve chronological holdout lower bound by at least 5%",
                         reason_code="validated_holdout")
    return candidate


def _training_observation(rec, source, est, final, *, allow_historical=False):
    """Return raw estimator and recorded pricing provenance, or an explicit exclusion."""
    import calibrate as C

    identity = _identity(rec, source)
    if source == "rules":
        return None, "rule_fallback"
    missing_identity = any(not isinstance(value, str) or not value for value in identity.values())
    if missing_identity and not allow_historical:
        return None, "missing_model_prompt_or_config_identity"
    if not est:
        return None, "missing_estimate"
    if any(est.get(k) is not None for k in ("stated", "cap", "shared")):
        return None, "policy_anchor_or_cap"
    if est.get("uncertainty_applied"):
        return None, "uncertainty_policy_not_modelled"
    try:
        applied = est.get("calibration_applied", False)
        if type(applied) is not bool:
            return None, "invalid_calibration_provenance"
        if applied and ("m_raw" not in est or any(k not in est for k in ("bias", "sigma", "a_mult"))):
            return None, "missing_applied_parameters_or_raw_estimate"
        m = _finite(est.get("m_raw", est["m"]), positive=True)
        p = _finite(est["p"])
        if not MIN_COVERED_PROB <= p <= 1:
            return None, "coverage_probability_outside_scope"
        baseline_alpha = _finite(rec["a_mult"], positive=True)
        baseline_sigma = _finite(rec["sigma"], positive=True)
        alpha = _finite(est.get("a_mult", baseline_alpha), positive=True)
        sigma = _finite(est.get("sigma", baseline_sigma), positive=True)
        bias = _finite(est.get("bias", 1.0), positive=True)
        if not applied and (bias != 1 or alpha != baseline_alpha or sigma != baseline_sigma):
            return None, "unmarked_parameter_adjustment"
        expected = [floor_c(alpha * m * bias), ceil_c(C.rule_b(p, m * bias, sigma))]
        if any(abs(x - y) > 0.011 for x, y in zip(expected, final)):
            return None, "logged_parameters_do_not_reproduce_submission"
        return {"m": m, "p": p, "baseline_alpha": baseline_alpha, "baseline_sigma": baseline_sigma,
                "applied_parameters": {"a_mult": alpha, "bias": bias, "sigma": sigma},
                "calibration_applied": applied, "identity": identity,
                "historical_only": missing_identity}, None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "invalid_price_or_estimate_provenance"


def build_state(rounds_path, cache_dir, team, *, before_game=None, now=None, previous_state=None, checkpoint=None,
                case_dir=None):
    """Join local records to cached complete games. Bad games are quarantined, not fitted."""
    import calibrate as C

    rounds_path, cache_dir = Path(rounds_path), Path(cache_dir)
    records, excluded = {}, []
    if rounds_path.exists():
        for number, raw in enumerate(rounds_path.read_text().splitlines(), 1):
            if not raw.strip():
                continue
            try:
                rec = _strict_json(raw)
                if not isinstance(rec, dict):
                    raise ValueError("round must be an object")
                gid = rec["game"]
                if type(gid) is not int or gid <= 0:
                    continue
                if rec.get("dry"):
                    continue
                if before_game is not None and gid >= before_game:
                    continue
                # Final appended record wins, including a failed rerun: fail closed.
                records[gid] = rec
            except (TypeError, ValueError, KeyError):
                excluded.append({"record": number, "reason": "malformed round JSON"})
    reports, memory, cohorts, training_exclusions, historical_rows = [], [], {}, [], []
    for gid, rec in sorted(records.items()):
        if checkpoint:
            checkpoint()  # Outside quarantine: a scheduling deadline aborts the whole rebuild.
        cache = cache_dir / f"game_{gid:03d}.json"
        try:
            if not rec.get("final_ab"):
                from .legacy import recover_record
                rec, recovery_reason = recover_record(rec, case_dir=case_dir or rounds_path.parent / "cases")
                if not rec.get("final_ab"):
                    raise ValueError(f"legacy provenance unavailable: {recovery_reason}")
            data = _strict_json(cache.read_text())
            lines, final = _reconcile(rec, data, team)
            game_rows = []
            for line, d in sorted(lines.items()):
                source = _get(rec["line_sources"], line)
                est = _get((rec.get("est") or {}).get(source, {}), line, {})
                tx = [x for x in data["tx"] if x["line_item_index"] == line]
                row = {"game": gid, "line": line, "tier": source, "final_ab": final[line],
                       "t": [d["t"][0], d["t"][1] if math.isfinite(d["t"][1]) else None],
                       **_line_loss(d, tx, team, final[line][0])}
                from .decision_review import review_line
                row["decision_review"] = review_line(d, tx, team, final[line], est,
                                                     rec.get("control_shadow") or {}, line)
                game_rows.append(row)
                item = _get(rec["items"], line)
                policy_hash = rec.get("policy_hash")
                if policy_hash and d["t"][0] > 0:
                    memory.append({"game": gid, "line": line, "description": str(item.get("description", ""))[:500],
                        "quantity": _finite(item["quantity"], positive=True), "unit": norm_unit(str(item["unit"])),
                        "policy_hash": policy_hash, "fair_total": row["t"],
                        "coverage": "historically covered; reassess damage relation and coverage for this case",
                        "policy_limited": bool(est.get("cap") or est.get("shared") or est.get("stated")),
                        "source": {"cache": cache.name, "tier": source, **_identity(rec, source)}})
                observation, reason = _training_observation(rec, source, est, final[line], allow_historical=True)
                row["learning"] = {"eligible": observation is not None, "reason": reason,
                                   "identity": _identity(rec, source)}
                if reason:
                    training_exclusions.append({"game": gid, "line": line, "tier": source, "reason": reason})
                    continue
                row["learning"]["applied_parameters"] = observation["applied_parameters"]
                row["learning"]["scope"] = "historical_training_only" if observation["historical_only"] else "target_cohort"
                key = tuple(observation["identity"].values())
                actual_net = (math.fsum(float(x["amount"]) for x in tx if x["issuer"] == team)
                              - math.fsum(float(x["amount"]) for x in tx if x["reviewer"] == team))
                target = historical_rows if observation["historical_only"] else cohorts.setdefault(key, [])
                target.append({"game": gid, "line": line, "d": d, "net": actual_net, **observation})
            reports.append({"game": gid, "summary": _summarize(game_rows), "lines": game_rows,
                            "commit": rec.get("commit", "unknown"),
                            "control_status": (rec.get("control_shadow") or {}).get("status", "not_recorded"),
                            "field_counts": _field_counts(lines, data["tx"], team),
                            "legacy_recovered": bool(rec.get("legacy_recovered")),
                            "recovery_reason": rec.get("legacy_recovery_reason")})
        except (OSError, TypeError, ValueError, KeyError, OverflowError, AttributeError, IndexError) as exc:
            # A partially processed game must not leak into recall or calibration.
            memory = [m for m in memory if m["game"] != gid]
            cohorts = {k: [r for r in rows if r["game"] != gid] for k, rows in cohorts.items()}
            historical_rows = [r for r in historical_rows if r["game"] != gid]
            training_exclusions = [r for r in training_exclusions if r["game"] != gid]
            excluded.append({"game": gid, "reason": str(exc)[:160]})
    all_rows = [r for game in reports for r in game["lines"]]
    created = now or dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    games = [g["game"] for g in reports]
    prior = None
    try:
        if (previous_state and previous_state.get("team") == team
                and set(previous_state["games"]).issubset(games)
                and _valid_state(previous_state, max(games, default=0) + 1, created, math.inf)):
            prior = previous_state
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    calibrations = []
    for rows in cohorts.values():
        if not rows:
            continue
        if checkpoint:
            checkpoint()
        candidate = _calibrate(rows, rows[0]["identity"], team, checkpoint, historical_rows)
        candidate["validated_at"] = created.isoformat() if candidate["status"] == "validated" else None
        calibrations.append(_retain_candidate(candidate, rows, prior, team, created, checkpoint))
    return {"schema_version": SCHEMA_VERSION, "algorithm": ALGORITHM,
            "controls": {**CONTROLS, "a_mult_grid": list(A_MULT_GRID)},
            "campaign": CAMPAIGN, "team": team,
            "created_at": created.isoformat(), "max_game": max((g["game"] for g in reports), default=0),
            "games": games, "excluded": excluded, "training_exclusions": training_exclusions,
            "learning_counts": {"eligible": len(historical_rows) + sum(len(rows) for rows in cohorts.values()),
                "historical_training_only": len(historical_rows),
                "target_cohort": sum(len(rows) for rows in cohorts.values()),
                "ineligible": len(training_exclusions),
                "calibrated_observations": sum(r["calibration_applied"] for rows in cohorts.values() for r in rows),
                "recovered_legacy_games": sum(g["legacy_recovered"] for g in reports)},
            "field_behavior": _field_summary(reports),
            "method": "completed settlement bounds; no exact hidden t; no August data",
            "summary": _summarize(all_rows), "reports": reports,
            "by_tier": {tier: _summarize([r for r in all_rows if r["tier"] == tier])
                        for tier in sorted({r["tier"] for r in all_rows})},
            "memory": memory[-500:], "calibrations": calibrations}


def _retain_candidate(proposed, rows, prior, team, now, checkpoint=None):
    """Do not erase an established policy merely because it cannot beat itself again."""
    if not prior:
        return proposed
    for old in prior.get("calibrations", []):
        if any(old.get(k) != proposed[k] for k in ("tier", "model", "prompt_hash", "config_hash")):
            continue
        if old.get("expired_evidence_hash") == proposed["evidence_hash"]:
            proposed.update(status="shadow", activation="none", active_parameters=None, validated_at=None,
                            expired_evidence_hash=proposed["evidence_hash"],
                            reason="validation expired; unchanged settlements cannot refresh its date",
                            reason_code="validation_expired_no_new_evidence")
            return proposed
        if not _validated(old):
            continue
        validated_at = dt.datetime.fromisoformat(old["validated_at"])
        fresh = 0 <= (now - validated_at).total_seconds() <= 36 * 3600
        same_params = all(old[k] == proposed[k] for k in PARAMETER_KEYS)
        if same_params and old["evidence_hash"] == proposed["evidence_hash"]:
            if not fresh:
                proposed.update(status="shadow", activation="none", active_parameters=None, validated_at=None,
                                expired_evidence_hash=proposed["evidence_hash"],
                                reason="validation expired; unchanged settlements cannot refresh its date",
                                reason_code="validation_expired_no_new_evidence")
                return proposed
            unchanged = dict(old)
            unchanged.update(activation="retained", reason="same parameters and evidence; original validation date retained",
                             reason_code="unchanged_validated_evidence")
            return unchanged
        if not fresh:
            return proposed  # New evidence may qualify; an expired incumbent never stays active by inheritance.
        held = [r for r in rows if r["game"] in proposed["holdout_games"]]
        incumbent = _range(*_score_rows(held, old, team, checkpoint))
        if proposed["status"] == "validated":
            if same_params:
                return proposed  # Same policy has a fresh, distinct validation dataset.
            lower, upper = proposed["holdout_candidate"]["lower"], incumbent["upper"]
            gain = (lower - upper) / max(abs(upper), 1) if upper is not None else None
            proposed.update(holdout_incumbent=incumbent, incumbent_gain=gain)
            if gain is not None and math.isfinite(gain) and gain >= MIN_GAIN:
                proposed.update(reason="frozen proposal improves both actual baseline and incumbent holdout upper bound by at least 5%",
                                reason_code="validated_over_incumbent")
                return proposed
            proposed.update(status="shadow", activation="none", active_parameters=None, validated_at=None,
                            reason="proposal beats historical baseline but does not prove 5% improvement over the incumbent",
                            reason_code="no_gain_over_incumbent")
        # When new evidence can prove the incumbent worse than the configured
        # uncalibrated defaults, stop retaining it. Unknown gaps are not proof.
        default_bounds = [_score_rows([r], {"a_mult": r["baseline_alpha"], "bias": 1.0,
                                           "sigma": r["baseline_sigma"]}, team, checkpoint) for r in held]
        default_lower = sum(b[0] for b in default_bounds)
        if (incumbent["upper"] is not None and math.isfinite(default_lower)
                and incumbent["upper"] < default_lower - MIN_GAIN * max(abs(default_lower), 1)):
            proposed.update(reason="new holdout proves incumbent worse than default; latest proposal remains unvalidated",
                            reason_code="incumbent_degraded")
            return proposed
        kept = {k: v for k, v in old.items() if k not in ("latest_proposal", "monitoring")}
        kept.update(activation="retained", reason="retained prior validated parameters; latest proposal has no new 5% proof",
                    reason_code="retained_validated_incumbent", latest_proposal=proposed,
                    proposed_parameters=proposed["proposed_parameters"],
                    monitoring={"games": proposed["holdout_games"], "incumbent": incumbent,
                                "default": _range(default_lower, sum(b[1] for b in default_bounds)),
                                "eligible_observations": len(rows),
                                "calibrated_observations": sum(r["calibration_applied"] for r in rows)})
        return kept
    return proposed


def _valid_state(state, before_game, now, max_age_hours):
    if (state.get("schema_version") != SCHEMA_VERSION or state.get("campaign") != CAMPAIGN
            or state.get("algorithm") != ALGORITHM or state.get("controls") != CONTROLS):
        return False
    created = dt.datetime.fromisoformat(state["created_at"])
    if created.tzinfo is None or not -60 <= (now - created).total_seconds() <= max_age_hours * 3600:
        return False
    games, max_game = state["games"], state["max_game"]
    if (type(before_game) is not int or type(max_game) is not int or max_game < 1 or max_game >= before_game
            or games != sorted(set(games)) or not games or max(games) != max_game
            or any(type(g) is not int or g < 1 for g in games)):
        return False
    if not isinstance(state.get("memory"), list) or not isinstance(state.get("calibrations"), list):
        return False
    for entry in state["memory"]:
        if (entry["game"] not in games or not isinstance(entry["description"], str)
                or not isinstance(entry["unit"], str) or not isinstance(entry["coverage"], str)
                or not isinstance(entry["source"]["cache"], str)
                or type(entry["policy_limited"]) is not bool):
            return False
        _finite(entry["quantity"], positive=True)
        lo, hi = entry["fair_total"]
        _finite(lo, positive=True)
        if hi is not None and _finite(hi) <= lo:
            return False
        if not isinstance(entry.get("policy_hash"), str) or not entry["policy_hash"]:
            return False
    identities = set()
    for candidate in state["calibrations"]:
        train, held = candidate["train_games"], candidate["holdout_games"]
        if (any(g not in games for g in train + held) or train != sorted(set(train))
                or held != sorted(set(held)) or (train and held and max(train) >= min(held))):
            return False
        if (candidate.get("status") not in ("validated", "shadow")
                or candidate.get("algorithm") != ALGORITHM
                or any(not isinstance(candidate.get(k), str) or not candidate[k]
                       for k in ("tier", "model", "prompt_hash", "config_hash"))
                or candidate.get("min_covered_prob") != MIN_COVERED_PROB
                or candidate.get("minimum_gain") != MIN_GAIN):
            return False
        identity = tuple(candidate[k] for k in ("tier", "model", "prompt_hash", "config_hash"))
        if identity in identities:
            return False
        identities.add(identity)
        for key in ("sample_count", "fit_count", "train_count", "holdout_count", "calibrated_observation_count"):
            if type(candidate.get(key)) is not int or candidate[key] < 0:
                return False
        if (candidate["sample_count"] != candidate["train_count"] + candidate["holdout_count"]
                or candidate["fit_count"] > candidate["train_count"]):
            return False
        if not _historical_provenance_valid(candidate):
            return False
        if not 0 < _finite(candidate["a_mult"], positive=True) <= 3:
            return False
        if not 0.75 <= _finite(candidate["bias"], positive=True) <= 1.25:
            return False
        if not 0.2 <= _finite(candidate["sigma"], positive=True) <= 1.40:
            return False
        if candidate.get("status") == "validated" and not _validated(candidate):
            return False
        if candidate.get("status") == "validated":
            validated_at = dt.datetime.fromisoformat(candidate["validated_at"])
            if (validated_at.tzinfo is None or not -60 <= (now - validated_at).total_seconds() <= max_age_hours * 3600):
                return False
        elif candidate.get("active_parameters") is not None:
            return False
    return True


def load_state(path, before_game, *, max_age_hours=36, now=None, model=None, prompt_hash=None, config_hash=None):
    """Fail closed on stale, malformed or lookahead artifacts. Never throw in runner."""
    try:
        path = Path(path)
        if path.stat().st_size > 20_000_000:
            return None
        state = _strict_json(path.read_text())
        now = now or dt.datetime.now(dt.timezone.utc)
        if not _valid_state(state, before_game, now, max_age_hours):
            return None
        filters = {"model": model, "prompt_hash": prompt_hash, "config_hash": config_hash}
        state["calibrations"] = [c for c in state["calibrations"]
            if all(value is None or c.get(key) == value for key, value in filters.items())]
        return state
    except (OSError, TypeError, ValueError, KeyError, OverflowError, AttributeError, IndexError):
        return None


load_learning = load_state


def _historical_provenance_valid(candidate):
    """Historical predictions may train a target policy, never impersonate its validation."""
    try:
        sources = candidate["historical_sources"]
        count, games = candidate["historical_train_count"], candidate["historical_train_games"]
        target = candidate["target_cohort_games"]
        return (type(count) is int and 0 <= count <= candidate["train_count"]
                and isinstance(sources, list) and len(sources) == count
                and len({(r["game"], r["line"]) for r in sources}) == count
                and games == sorted({r["game"] for r in sources})
                and target == sorted(set(target)) and bool(target)
                and set(candidate["holdout_games"]).issubset(target)
                and set(target) <= set(candidate["train_games"] + candidate["holdout_games"])
                and set(games).issubset(candidate["train_games"])
                and all(g < min(target) for g in games)
                and candidate["training_source"] == ("historical_bootstrap" if count else "same_estimator"))
    except (KeyError, TypeError, ValueError):
        return False


def _validated(candidate):
    try:
        train, held = candidate["train_games"], candidate["holdout_games"]
        baseline, lower = candidate["holdout_baseline_net"], candidate["holdout_candidate"]["lower"]
        return (candidate.get("status") == "validated" and candidate.get("algorithm") == ALGORITHM
                and _historical_provenance_valid(candidate)
                and candidate.get("a_mult") in A_MULT_GRID
                and 0.75 <= candidate["bias"] <= 1.25 and 0.2 <= candidate["sigma"] <= 1.40
                and candidate.get("active_parameters") == {k: candidate[k] for k in PARAMETER_KEYS}
                and candidate.get("minimum_gain") == MIN_GAIN
                and len(set(train + held)) >= MIN_GAMES
                and train and len(held) >= 2 and max(train) < min(held)
                and candidate["train_count"] >= MIN_TRAIN and candidate["holdout_count"] >= MIN_HOLDOUT
                and candidate["fit_count"] >= MIN_TRAIN
                and math.isfinite(lower) and math.isfinite(baseline)
                and (lower - baseline) / max(abs(baseline), 1.0) >= MIN_GAIN - 1e-8
                and candidate["conservative_gain"] >= MIN_GAIN
                and candidate["min_covered_prob"] == MIN_COVERED_PROB)
    except (TypeError, ValueError, KeyError, OverflowError):
        return False


def calibration_for(state, tier, *, model=None, prompt_hash=None, config_hash=None):
    """Only an exact model/prompt/config match may activate validated calibration."""
    if (not state or state.get("schema_version") != SCHEMA_VERSION
            or state.get("algorithm") != ALGORITHM or not all((model, prompt_hash, config_hash))):
        return None
    for candidate in state.get("calibrations", []):
        if (_validated(candidate) and candidate.get("tier") == tier
                and candidate.get("model") == model and candidate.get("prompt_hash") == prompt_hash
                and candidate.get("config_hash") == config_hash):
            return {key: candidate[key] for key in ("a_mult", "bias", "sigma", "min_covered_prob", "train_count",
                                                   "holdout_count", "fit_count", "conservative_gain",
                                                   "activation", "validated_at")}
    return None


def calibration_status(state, tier, *, model=None, prompt_hash=None, config_hash=None):
    """Explain inactivity, including identity mismatches, without loosening selection."""
    wanted = {"tier": tier, "model": model, "prompt_hash": prompt_hash, "config_hash": config_hash}
    if not state:
        return {"status": "unavailable", "reason_code": "no_valid_state", "reason": "learning state missing, stale, or invalid"}
    if state.get("schema_version") != SCHEMA_VERSION or state.get("algorithm") != ALGORITHM:
        return {"status": "unavailable", "reason_code": "schema_mismatch", "reason": "rebuild learning artifact with current algorithm"}
    if not all(wanted.values()):
        return {"status": "unavailable", "reason_code": "missing_runtime_identity", "reason": "model, prompt and tier configuration are required"}
    candidates = [c for c in state.get("calibrations", []) if c.get("tier") == tier]
    for candidate in candidates:
        if all(candidate.get(key) == value for key, value in wanted.items()):
            active = calibration_for(state, tier, model=model, prompt_hash=prompt_hash, config_hash=config_hash)
            current = candidate.get("latest_proposal", candidate)
            return {"status": "active" if active else "shadow", "reason_code": candidate["reason_code"],
                    "reason": candidate["reason"], "parameters": active,
                    "proposed_parameters": candidate["proposed_parameters"],
                    "counts": {k: current[k] for k in ("sample_count", "train_count", "fit_count", "holdout_count",
                                                        "calibrated_observation_count")}}
    if candidates:
        mismatches = [{"model": c.get("model"), "prompt_hash": c.get("prompt_hash"), "config_hash": c.get("config_hash"),
                       "mismatched_fields": [key for key in ("model", "prompt_hash", "config_hash") if c.get(key) != wanted[key]]}
                      for c in candidates]
        return {"status": "unavailable", "reason_code": "identity_mismatch",
                "reason": "historical model, prompt, or tier configuration differs", "available": mismatches[:6]}
    reasons = {}
    for row in state.get("training_exclusions", []):
        if row.get("tier") == tier:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    return {"status": "unavailable", "reason_code": "no_eligible_observations",
            "reason": "no reconciled training observations for this tier", "exclusion_counts": reasons,
            "excluded_games": len(state.get("excluded", []))}


def _tokens(text):
    return set(re.findall(r"[\w]+", text.lower()))


def context_for_items(state, items, policy, *, max_matches=4, max_chars=2200):
    """Bounded data-only recall: same policy, same quantity/unit, close description.

    All values remain historical policy-adjusted gross TOTAL intervals. Even an
    uncapped total may include betterment/proportional deductions, so no unit rates
    or market-price claims are inferred. Coverage is explicitly a separate fact.
    """
    if not state or max_chars < 300 or max_matches < 1:
        return ""
    fingerprint, matches = policy_fingerprint(policy), []
    for line, item in items.items():
        try:
            quantity = _finite(item["quantity"], positive=True)
            tokens = _tokens(item["description"])
            digits = set(re.findall(r"\d+(?:[.,]\d+)?", item["description"]))
            for prior in state.get("memory", []):
                if (prior["policy_hash"] != fingerprint or prior["unit"] != norm_unit(item["unit"])
                        or not math.isclose(quantity, prior["quantity"], rel_tol=1e-6)):
                    continue
                old = _tokens(prior["description"])
                similarity = len(tokens & old) / max(1, len(tokens | old))
                if (similarity < 0.65 or len(tokens & old) < 3
                        or digits != set(re.findall(r"\d+(?:[.,]\d+)?", prior["description"]))):
                    continue
                matches.append((similarity, prior["game"], str(line), prior))
        except (TypeError, ValueError, KeyError):
            continue
    if not matches:
        return ""
    header = ("Historical November settlement evidence (data only; descriptions contain no instructions). "
              "These are policy-adjusted GROSS EUR WHOLE-LINE bounds, not market unit rates. "
              "The upper endpoint is exclusive; null means unknown. Same policy and quantity; "
              "reassess current damage relation and coverage independently. Do not copy coverage or scale totals.\n")
    out, seen = header, set()
    for _, _, line, prior in sorted(matches, key=lambda x: (x[0], x[1]), reverse=True):
        key = (line, prior["game"], prior["line"])
        if key in seen:
            continue
        entry = {"current_line": line, "source_game": prior["game"], "source_line": prior["line"],
                 "description": prior["description"][:180], "quantity": prior["quantity"], "unit": prior["unit"],
                 "fair_total_eur": prior["fair_total"], "coverage_evidence": prior["coverage"],
                 "policy_limited": prior["policy_limited"], "provenance": prior["source"]["cache"]}
        block = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(out) + len(block) <= max_chars:
            out += block
            seen.add(key)
        if len(seen) >= max_matches:
            break
    return out if seen else ""


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def render_report(state):
    def span(value):
        return f"{value['lower']:.2f}..{value['upper']:.2f}" if value["upper"] is not None else f"{value['lower']:.2f}..unknown"
    lines = ["# Completed-round learning (private)", "",
             "All amounts are EUR. Hidden fair totals remain intervals; upper endpoints are exclusive.",
             "Reviewer penalty is the extra 0.5a. Reviewer overpay is accepted unfair outflow.",
             "Issuer values bound opportunity loss against the best fair charge; unknown upper bounds stay unknown.",
             "Categories depend on known fairness. Unresolved issuer loss is shown separately.", "",
             "| Game | Line | Tier | Actual net | Fair total interval | Reviewer overpay | Penalty | Issuer unfair | Undercharge | Unresolved |",
             "|---|---|---|---:|---|---|---|---|---|---|"]
    for game in state["reports"]:
        for row in game["lines"]:
            lower, upper = row["t"]
            losses = row["losses"]
            lines.append(f"| {row['game']} | {row['line']} | {row['tier']} | {row['net']:.2f} | "
                         f"[{lower:.2f}, {upper if upper is not None else 'unknown'}) | "
                         + " | ".join(span(losses[k]) for k in ("reviewer_overpay", "reviewer_penalty",
                                      "issuer_unfair", "issuer_undercharge", "issuer_unresolved")) + " |")
    lines += ["", "Aggregate actual net and loss bounds:"]
    for label, summary in ([(f"Game {g['game']}", g["summary"]) for g in state["reports"]]
                           + [(f"Tier {tier}", summary) for tier, summary in state["by_tier"].items()]):
        lines.append(f"- {label}: {summary['lines']} lines, net {summary['net']:.2f}; "
                     + "; ".join(f"{key} {span(value)}" for key, value in summary["losses"].items()))
    lines += ["", "Recent rounds (association with code version is not causal proof):"]
    for game in state["reports"][-5:]:
        lines.append(f"- Game {game['game']}: commit {game.get('commit', 'unknown')}, "
                     f"net {game['summary']['net']:.2f}, control {game.get('control_status', 'not_recorded')}.")
    worst = sorted((row for game in state["reports"][-5:] for row in game["lines"]), key=lambda row: row["net"])[:5]
    lines += ["", "Largest negative line results in the last five reconciled rounds:"]
    for row in worst:
        if row["net"] < 0:
            lines.append(f"- Game {row['game']} line {row['line']}: net {row['net']:.2f}; "
                         f"overpay {span(row['losses']['reviewer_overpay'])}; "
                         f"extra rejection penalty {span(row['losses']['reviewer_penalty'])}.")
    lines += ["", "Calibration remains shadow until enough chronological evidence demonstrates at least 5% conservative net improvement."]
    for candidate in state["calibrations"]:
        lines.append(f"- {candidate['tier']} / {candidate['model']}: {candidate['status']}; "
                     f"A_MULT {candidate['a_mult']:.4f}, bias {candidate['bias']:.4f}, sigma {candidate['sigma']:.4f}; "
                     f"train {candidate['train_count']}, covered fit {candidate['fit_count']}, holdout {candidate['holdout_count']}; "
                     f"{candidate['reason']}.")
        lines.append(f"  Training source: {candidate['training_source']}; "
                     f"historical {candidate['historical_train_count']} lines from games {candidate['historical_train_games']}; "
                     f"target-cohort holdout games {candidate['holdout_games']}.")
        if candidate.get("latest_proposal"):
            latest = candidate["latest_proposal"]
            lines.append(f"  Latest proposal {latest['proposed_parameters']}: {latest['reason']}; "
                         f"{latest['sample_count']} eligible observations, {latest['calibrated_observation_count']} previously calibrated.")
        for game in candidate.get("holdout_by_game", []):
            if game["candidate"]["lower"] is not None:
                lines.append(f"  Holdout game {game['game']}: actual baseline {game['baseline_net']:.2f}; "
                             f"candidate {span(game['candidate'])}.")
    behavior = state.get("field_behavior", {})
    if behavior:
        lines += ["", "Opponent behavior (known labels only; unknown fairness retained):",
                  f"- Positive claims by fairness: {behavior['claims']}."]
        for label, event in (("fair", "rejected"), ("unfair", "accepted")):
            value = behavior[f"{label}_{event}_rate"]
            rate = f"{value:.1%}" if value is not None else "unknown"
            lines.append(f"- {label} claims {event}: {rate}, {behavior[f'{label}_decision_count']} known-label decisions.")
    from .decision_review import summarize
    decisions = summarize(state["reports"])
    lines += ["", "Price quality (raw estimates; bounds are not exact fair prices):",
              "below_known_fair means a low estimate, not proof that raising the charge improves profit."]
    for game in state["reports"]:
        for row in game["lines"]:
            review = row.get("decision_review", {})
            if review.get("estimate_position") in ("below_known_fair", "above_known_unfair"):
                lines.append(f"- Game {game['game']} line {row['line']}: raw m={review['m_raw']}, "
                             f"p={review['covered_prob']}, {review['estimate_position']}; "
                             f"submitted a/b={row['final_ab']}.")
    lines += ["", "Decision experiments: change in net versus actual, opponents fixed.",
              "Independent control never submits; without_recall removes the recorded cap.",
              "Positive delta favors the named alternative. a and b are evaluated separately.",
              "These are selected-line comparisons, not blind holdout validation or expected profit."]
    for name, result in decisions.items():
        lines.append(f"- {name}: {len(result['games'])} games, {result['lines']} lines, "
                     f"delta {result['lower']:.2f}..{result['upper']:.2f}; "
                     f"without best game {result['best_game']}: lower {result['without_best_game_lower']:.2f}.")
        for gid, game in result["games"].items():
            lines.append(f"  Game {gid}: delta {game['lower']:.2f}..{game['upper']:.2f}.")
    exclusions = {}
    for row in state.get("training_exclusions", []):
        exclusions[row["reason"]] = exclusions.get(row["reason"], 0) + 1
    for reason, count in sorted(exclusions.items()):
        lines.append(f"- Training excluded {count} lines: {reason}.")
    for game in state["reports"]:
        if game.get("legacy_recovered"):
            lines.append(f"- Game {game['game']} legacy prices recovered and settlement-reconciled: {game['recovery_reason']}.")
    for excluded in state["excluded"]:
        lines.append(f"- Excluded {excluded.get('game', 'record ' + str(excluded.get('record')))}: {excluded['reason']}")
    return "\n".join(lines) + "\n"


def write_state(state, path, *, report_path=None):
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    if report_path is not None:
        _atomic_write(report_path, render_report(state))
