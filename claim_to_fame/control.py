"""Independent, bounded assessment for evaluation only. No submission API."""
import hashlib
import json

from . import pricing, prompts


def assess_control(policy, description, items, estimates, *, assess, tier, budget,
                   a_mult, sigma, disagree):
    # Select by economic size, but never expose those estimates to the control.
    eligible = {i: item for i, item in items.items()
                if i in estimates and estimates[i].get("shared") is None
                and estimates[i].get("stated") is None}
    selected = pricing.select_focus_items(eligible, estimates, limit=2)
    if not selected:
        return {"status": "no_eligible_lines"}
    messages = prompts.build_prompt(policy, description, selected, historical_context="")
    voices, usage = assess(messages, budget, tier)
    pairs, details = {}, {}
    for i in selected:
        summary = pricing.estimate_votes([v[i] for v in voices if i in v], disagree=disagree)
        # A subset cannot allocate an aggregate policy limit safely.
        if not summary["valid_count"] or summary.get("shared") is not None:
            continue
        pairs[i] = pricing.price(summary["p"], summary["m"], summary["stated"], summary["cap"],
                                 0, a_mult=a_mult, sigma=sigma, uncertainty_aware=False)
        details[i] = summary
    return {"status": "completed" if pairs else "no_usable_estimates", "selected": list(selected),
            "ab": pairs, "est": details, "usage": usage, "model": tier.model,
            "a_mult": a_mult, "sigma": sigma, "votes_requested": 1,
            "historical_context": False, "prior_estimates": False,
            "prompt_hash": hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()}
