"""Recall caps from our own settled November games (no August data, no network).

Cases repeat with the identical policy text and reworded invoices (for example the
surge-damaged speaker and TV under the same policy in games 1, 9 and 15). For a line
of the current game we look up lines of earlier games with the *same policy text* and
a similar description. Their public transactions bound the fair price t from both
sides. When the recalled evidence has a known-fair level (lo > 0) and a known-unfair
level (finite hi), the charge is capped at the midpoint of that range. The cap only
lowers a; it never raises a and never touches b.

Inputs are files the runner already keeps locally: decrypted cases in
``out/cases/case_NNN`` and the public settlement cache ``out/leaderboard/game_NNN.json``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .invoices import norm_unit, parse_items
from .pricing import floor_c

THETA = 0.3
HI_FRAC = 0.95      # mode "2": a and b stay below 0.95 x the lowest recalled unfair level
STOP = set("and the of from for with incl including into in on at to an by per its this that "
           "after before or is are be as".split())
_CACHE: dict = {}


def policy_text(case_dir):
    """Same concatenation as runner.read_text(d, 'policy*.txt')."""
    hits = sorted(Path(case_dir).rglob("policy*.txt"))
    return "\n\n".join(p.read_text(errors="replace") for p in hits)


def policy_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def tokens(text):
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 2 and w not in STOP}


def similarity(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def _description(item):
    return item.get("description") or item.get("desc") or ""


def _finite_or_none(x):
    return round(x, 2) if math.isfinite(x) else None


def _past_game(gid, cases_dir, cache_dir, team, want_hash):
    """{line: (tokens, qty, unit, t)} for one settled same-policy game, else None."""
    case_dir, cache = Path(cases_dir) / f"case_{gid:03d}", Path(cache_dir) / f"game_{gid:03d}.json"
    if not case_dir.is_dir() or not cache.is_file():
        return None
    text = policy_text(case_dir)
    if not text or policy_hash(text) != want_hash:
        return None                             # cheap check first: parse only matching cases
    key = (gid, cache.stat().st_mtime_ns, case_dir.stat().st_mtime_ns)
    if key in _CACHE:
        return _CACHE[key]
    import calibrate as C                       # top-level helper module of this repo

    data = json.loads(cache.read_text())
    tx = data.get("tx") or []
    if data.get("game") not in (None, gid) or not tx:
        return None
    L = C.analyse(tx)
    items = parse_items(case_dir)
    lines = {}
    for i, it in items.items():
        if i not in L:
            continue
        lo, hi = L[i]["t"]
        if lo <= 0 and not math.isfinite(hi):
            continue                            # no information about t
        lines[i] = (tokens(_description(it)), float(it.get("quantity") or 1.0),
                    norm_unit(it.get("unit")), (float(lo), float(hi)))
    _CACHE[key] = lines
    return lines


def recall_line(item, memory, theta=THETA):
    """Recalled t interval for one line: (lo, hi, matches) or None."""
    tok, qty, unit = tokens(_description(item)), float(item.get("quantity") or 1.0), norm_unit(item.get("unit"))
    hits = []
    for gid, lines in memory:
        for j, (mtok, mqty, munit, (lo, hi)) in lines.items():
            s = similarity(tok, mtok)
            if s < theta:
                continue
            f = qty / mqty if (munit == unit and mqty > 0) else 1.0
            hits.append((s, gid, j, lo * f, hi * f))
    if not hits:
        return None
    lo, hi = max(h[3] for h in hits), min(h[4] for h in hits)
    if lo >= hi:                                # inconsistent evidence: most similar, then latest
        best = max(hits, key=lambda h: (h[0], h[1]))
        lo, hi = best[3], best[4]
    return lo, hi, [{"game": h[1], "line": h[2], "similarity": round(h[0], 3),
                     "t": [round(h[3], 2), _finite_or_none(h[4])]} for h in hits]


def cap_from(lo, hi):
    """Midpoint cap when the range is bounded on both sides; otherwise no cap."""
    if lo > 0 and math.isfinite(hi) and hi > lo:
        return floor_c((lo + hi) / 2)
    return None


def loose_cap_from(lo, hi, minimum=20.0):
    """Below a known-unfair level when no fair level is known (only for RECALL_CAP=loose)."""
    if lo <= 0 and math.isfinite(hi) and hi >= minimum:
        return floor_c(0.97 * hi)
    return None


def hi_cap_from(hi, frac=HI_FRAC):
    """Mode "2": a known-unfair level (finite hi), one- or two-sided, caps a AND b.

    Replay G1-18 (score_submission, A_MULT 0.68, opponents fixed): midpoint cap on a only
    +0.3k, 0.95*hi on a only +1.6k, 0.95*hi on a and b +4.9k; positive in G1-10 and G11-18.
    """
    if math.isfinite(hi) and hi > 0:
        return floor_c(frac * hi)
    return None


def caps_for(gid, items, policy, cases_dir, cache_dir, team, *, theta=THETA):
    """{line: {"cap", "t", "matches"}} for the current game from earlier same-policy games."""
    current = policy_hash(policy)
    memory = []
    for h in range(1, gid):
        past = _past_game(h, cases_dir, cache_dir, team, current)
        if past:
            memory.append((h, past))
    out = {}
    for i, item in items.items():
        r = recall_line(item, memory, theta)
        if not r:
            continue
        lo, hi, matches = r
        out[i] = {"cap": cap_from(lo, hi), "cap_loose": loose_cap_from(lo, hi), "cap_hi": hi_cap_from(hi),
                  "t": [round(lo, 2), _finite_or_none(hi)], "matches": matches}
    return out


def apply_cap(ab, entry, detail, mode):
    """Return (a, b) after the recall cap.

    mode '2' caps a and b at 0.95 x the recalled unfair level (one- or two-sided),
    '1' applies the two-sided midpoint cap to a, 'loose' also the one-sided one, 'shadow'
    only records. Lines priced from a stated document amount stay unchanged. Never raises a or b.
    """
    a, b = ab
    entry = entry or {}
    strict, loose, hi_cap = entry.get("cap"), entry.get("cap_loose"), entry.get("cap_hi")
    detail["recall_cap"], detail["recall_cap_loose"], detail["recall_cap_hi"] = strict, loose, hi_cap
    detail["recall_t"] = entry.get("t")
    if mode == "2":
        cap, cap_b = hi_cap, hi_cap
    else:
        cap = strict if strict is not None else (loose if mode == "loose" else None)
        cap_b = None
    free = detail.get("stated") is None
    detail["recall_applied"] = bool(cap is not None and mode in ("1", "loose", "2") and free and a > cap)
    detail["recall_b_applied"] = bool(cap_b is not None and free and b > cap_b)
    if detail["recall_applied"]:
        detail["a_before_recall"] = a
        a = cap
    if detail["recall_b_applied"]:
        detail["b_before_recall"] = b
        b = cap_b
    return a, b
