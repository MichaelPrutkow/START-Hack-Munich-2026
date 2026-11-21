"""Recover narrowly provable old runner submission provenance, without network access.

The old runner recorded ``winner`` only after a successful submission. Its winner's
complete tier can therefore be a candidate final submission. Settlement reconciliation
must still verify it; this module supplies no model identity or hidden-price evidence.
"""
from collections.abc import Mapping
import hashlib
import math
from pathlib import Path
import re


def _indexed(mapping):
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValueError("missing_indexed_mapping")
    normalized = {}
    for key, value in mapping.items():
        if type(key) is int and key >= 0:
            index = key
        elif isinstance(key, str) and re.fullmatch(r"0|[1-9]\d*", key):
            index = int(key)
        else:
            raise ValueError("invalid_index")
        if index in normalized:
            raise ValueError("duplicate_index")
        normalized[index] = value
    return normalized


def _local_policy_hash(case_dir, gid):
    if case_dir is None:
        return None
    try:
        base = Path(case_dir).resolve()
        case = (base / f"case_{gid:03d}").resolve()
        if not case.is_relative_to(base) or not case.is_dir():
            return None
        matches = list(case.rglob("policy*.txt"))
        # Multiple versions are ambiguous; never guess which text the old runner saw.
        if len(matches) != 1 or not matches[0].resolve().is_relative_to(case):
            return None
        text = matches[0].read_text(encoding="utf-8", errors="replace")
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def recover_record(rec, *, case_dir=None):
    """Return (record, diagnostic code), leaving rejected/current records untouched.

    Only a complete explicitly named winner is recovered. Missing prices are never
    filled from another tier or a fallback. Existing final provenance, including
    malformed partial provenance, is never repaired here. Recovered records remain
    ineligible for parameter fitting when original estimator identities are absent.
    """
    if not isinstance(rec, Mapping):
        return rec, "not_a_round_record"
    if "final_ab" in rec:
        return rec, "existing_final_provenance"
    if any(key in rec for key in ("line_sources", "votes", "config", "learning_version")):
        return rec, "incomplete_modern_provenance"
    if type(rec.get("game")) is not int or rec["game"] <= 0:
        return rec, "invalid_game"
    if rec.get("ok") is not True or rec.get("dry") or rec.get("echo_ok") is False:
        return rec, "not_a_successful_real_submission"
    winner, tiers = rec.get("winner"), rec.get("tiers")
    if not isinstance(winner, str) or not winner or not isinstance(tiers, Mapping) or winner not in tiers:
        return rec, "missing_explicit_winner"
    try:
        items, prices = _indexed(rec.get("items")), _indexed(tiers[winner])
        if items.keys() != prices.keys():
            return rec, "incomplete_winning_tier"
        if "lines" in rec and (type(rec["lines"]) is not int or rec["lines"] != len(items)):
            return rec, "invoice_count_mismatch"
        final = {}
        for index, pair in prices.items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                return rec, "invalid_winning_prices"
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in pair):
                return rec, "invalid_winning_prices"
            final[str(index)] = [float(value) for value in pair]
    except (ValueError, TypeError, OverflowError):
        return rec, "invalid_or_duplicate_indices"
    reason = "complete_winner_pending_settlement_verification"
    recovered = {**rec, "final_ab": final,
                 "line_sources": {str(index): winner for index in items},
                 "legacy_recovered": True, "legacy_recovery_reason": reason}
    if "policy_hash" not in recovered:
        policy_hash = _local_policy_hash(case_dir, rec["game"])
        if policy_hash:
            recovered["policy_hash"] = policy_hash
            recovered["legacy_policy_recovered"] = True
    return recovered, reason
