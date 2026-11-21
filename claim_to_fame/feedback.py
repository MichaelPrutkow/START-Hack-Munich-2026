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


SCHEMA_VERSION = 1
CAMPAIGN = "2026-11"
MIN_GAMES, MIN_TRAIN, MIN_HOLDOUT = 6, 16, 8
MIN_GAIN = 0.05
MIN_COVERED_PROB = 0.9


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


def _fit_candidate(rows):
    """One fixed interval-likelihood fit, trained chronologically, then shrunk."""
    N = statistics.NormalDist()
    iv = [(math.log(r["d"]["t"][0] / r["m"]),
           math.log(r["d"]["t"][1] / r["m"])) for r in rows]
    best = (-math.inf, 0.0, 0.35)
    # Include right-censored observations; never replace an interval by its midpoint.
    for mu_i in range(-30, 31, 3):
        mu = mu_i / 100
        for sd_i in range(20, 76, 5):
            sd = sd_i / 100
            ll = sum(math.log(max(1e-12, (N.cdf((hi - mu) / sd) if math.isfinite(hi) else 1.0)
                                  - N.cdf((lo - mu) / sd))) for lo, hi in iv)
            if ll > best[0]:
                best = ll, mu, sd
    weight = len(rows) / (len(rows) + 40)
    baseline = statistics.median(r["sigma"] for r in rows)
    return {"bias": round(min(1.25, max(0.75, math.exp(best[1] * weight))), 4),
            "sigma": round(min(0.75, max(0.2, baseline + weight * (best[2] - baseline))), 4)}


def _calibrate(rows, identity, team):
    import calibrate as C

    games = sorted({r["game"] for r in rows})
    n_holdout = max(2, len(games) // 3)
    train_games, holdout_games = games[:-n_holdout], games[-n_holdout:]
    train = [r for r in rows if r["game"] in train_games]
    fit = [r for r in train if r["d"]["t"][0] > 0]
    holdout = [r for r in rows if r["game"] in holdout_games]
    candidate = {**identity, "status": "shadow", "reason": "insufficient chronological evidence",
                 "sample_count": len(rows), "train_count": len(train), "holdout_count": len(holdout),
                 "fit_count": len(fit), "fit_population": "confirmed-covered training lines; holdout includes all eligible predictions",
                 "train_games": train_games, "holdout_games": holdout_games,
                 "min_covered_prob": MIN_COVERED_PROB, "minimum_gain": MIN_GAIN,
                 "bias": 1.0, "sigma": min(0.75, max(0.2, statistics.median(r["sigma"] for r in rows)))}
    if not fit:
        return candidate
    candidate.update(_fit_candidate(fit))
    if len(games) < MIN_GAMES or len(fit) < MIN_TRAIN or len(holdout) < MIN_HOLDOUT:
        return candidate
    baseline = sum(r["net"] for r in holdout)
    bounds = []
    for r in holdout:
        m = r["m"] * candidate["bias"]
        a = floor_c(r["alpha"] * m)
        b = ceil_c(C.rule_b(r["p"], m, candidate["sigma"]))
        bounds.append(C.score_submission(r["d"], team, a, b))
    lower = sum(b["lower"] for b in bounds)
    upper = sum(b["upper"] for b in bounds)
    if not math.isfinite(lower) or not math.isfinite(upper):
        candidate.update(holdout_baseline_net=round(baseline, 2), holdout_candidate=_range(lower, upper),
                         conservative_gain=None, reason="held-out payoff bounds are unbounded")
        return candidate
    gain = (lower - baseline) / max(abs(baseline), 1.0)
    candidate.update(holdout_baseline_net=round(baseline, 2), holdout_candidate=_range(lower, upper),
                     conservative_gain=round(gain, 6), reason="held-out lower bound does not improve baseline by 5%")
    if math.isfinite(gain) and gain >= MIN_GAIN:
        candidate.update(status="validated", reason="frozen training fit improves chronological holdout lower bound by at least 5%")
    return candidate


def build_state(rounds_path, cache_dir, team, *, before_game=None, now=None):
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
    reports, memory, cohorts = [], [], {}
    for gid, rec in sorted(records.items()):
        cache = cache_dir / f"game_{gid:03d}.json"
        try:
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
                identity = _identity(rec, source)
                if (source == "rules" or not all(identity.values()) or not est
                        or any(est.get(k) for k in ("stated", "cap", "shared", "calibration_applied", "uncertainty_applied"))):
                    continue
                m, p = _finite(est["m"], positive=True), _finite(est["p"])
                if not MIN_COVERED_PROB <= p <= 1:
                    continue
                alpha = _finite(rec.get("a_mult", 0.85), positive=True)
                sigma = _finite(rec.get("sigma", 0.35), positive=True)
                # Calibration applies to the actual unchanged pricing rule, not estimates
                # from another tier or a run whose winner label hides partial fallback.
                expected = [floor_c(alpha * m), ceil_c(C.rule_b(p, m, sigma))]
                if any(abs(x - y) > 0.05 for x, y in zip(expected, final[line])):
                    continue
                key = tuple(identity.values())
                cohorts.setdefault(key, []).append({"game": gid, "d": d, "m": m, "p": p,
                    "alpha": alpha, "sigma": sigma, "net": row["net"], "identity": identity})
            reports.append({"game": gid, "summary": _summarize(game_rows), "lines": game_rows})
        except (OSError, TypeError, ValueError, KeyError, OverflowError, AttributeError, IndexError) as exc:
            # A partially processed game must not leak into recall or calibration.
            memory = [m for m in memory if m["game"] != gid]
            cohorts = {k: [r for r in rows if r["game"] != gid] for k, rows in cohorts.items()}
            excluded.append({"game": gid, "reason": str(exc)[:160]})
    all_rows = [r for game in reports for r in game["lines"]]
    created = now or dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return {"schema_version": SCHEMA_VERSION, "campaign": CAMPAIGN, "team": team,
            "created_at": created.isoformat(), "max_game": max((g["game"] for g in reports), default=0),
            "games": [g["game"] for g in reports], "excluded": excluded,
            "method": "completed settlement bounds; no exact hidden t; no August data",
            "summary": _summarize(all_rows), "reports": reports,
            "by_tier": {tier: _summarize([r for r in all_rows if r["tier"] == tier])
                        for tier in sorted({r["tier"] for r in all_rows})},
            "memory": memory[-500:], "calibrations": [_calibrate(rows, rows[0]["identity"], team)
                                                      for rows in cohorts.values() if rows]}


def _valid_state(state, before_game, now, max_age_hours):
    if state.get("schema_version") != SCHEMA_VERSION or state.get("campaign") != CAMPAIGN:
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
    for candidate in state["calibrations"]:
        train, held = candidate["train_games"], candidate["holdout_games"]
        if (any(g not in games for g in train + held) or train != sorted(set(train))
                or held != sorted(set(held)) or (train and held and max(train) >= min(held))):
            return False
        if (candidate.get("status") not in ("validated", "shadow")
                or any(not isinstance(candidate.get(k), str) or not candidate[k]
                       for k in ("tier", "model", "prompt_hash", "config_hash"))
                or candidate.get("min_covered_prob") != MIN_COVERED_PROB):
            return False
        if not 0.75 <= _finite(candidate["bias"], positive=True) <= 1.25:
            return False
        if not 0.2 <= _finite(candidate["sigma"], positive=True) <= 0.75:
            return False
        if candidate.get("status") == "validated" and not _validated(candidate):
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


def _validated(candidate):
    try:
        train, held = candidate["train_games"], candidate["holdout_games"]
        baseline, lower = candidate["holdout_baseline_net"], candidate["holdout_candidate"]["lower"]
        return (candidate.get("status") == "validated" and len(set(train + held)) >= MIN_GAMES
                and train and held and max(train) < min(held)
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
    if not state or not all((model, prompt_hash, config_hash)):
        return None
    for candidate in state.get("calibrations", []):
        if (_validated(candidate) and candidate.get("tier") == tier
                and candidate.get("model") == model and candidate.get("prompt_hash") == prompt_hash
                and candidate.get("config_hash") == config_hash):
            return {key: candidate[key] for key in ("bias", "sigma", "min_covered_prob", "train_count",
                                                   "holdout_count", "conservative_gain")}
    return None


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
    lines += ["", "Calibration remains shadow until enough chronological evidence demonstrates at least 5% conservative net improvement."]
    for candidate in state["calibrations"]:
        lines.append(f"- {candidate['tier']} / {candidate['model']}: {candidate['status']}; "
                     f"bias {candidate['bias']:.4f}, sigma {candidate['sigma']:.4f}; "
                     f"train {candidate['train_count']}, holdout {candidate['holdout_count']}; {candidate['reason']}.")
    for excluded in state["excluded"]:
        lines.append(f"- Excluded {excluded.get('game', 'record ' + str(excluded.get('record')))}: {excluded['reason']}")
    return "\n".join(lines) + "\n"


def write_state(state, path, *, report_path=None):
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    if report_path is not None:
        _atomic_write(report_path, render_report(state))
