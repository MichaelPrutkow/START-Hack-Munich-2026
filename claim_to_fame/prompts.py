"""Insurance assessment instructions, response schema and prompt construction."""
import json
import re


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["index", "clause", "covered_prob", "t_if_covered", "stated_value", "stated_cap",
                     "shared_cap"],
        "properties": {"index": {"type": "integer"},
                       "clause": {"type": "string"},          # first: the model reasons before the numbers
                       "covered_prob": {"type": "number"},
                       "t_if_covered": {"type": "number"},
                       "stated_value": {"type": ["number", "null"]},
                       "stated_cap": {"type": ["number", "null"]},
                       "shared_cap": {"type": ["number", "null"]}}}}}}

# Explicit decomposition; older recorded responses without this field remain readable.
VALUATION_SCHEMA = {"type": ["object", "null"], "additionalProperties": False,
    "required": ["category", "components", "covered_share", "share_basis", "coverage_basis"],
    "properties": {
        "category": {"type": "string", "enum": ["equipment", "labour", "material", "accessory", "ancillary", "other"]},
        "components": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["label", "quantity", "unit_gross"], "properties": {
                "label": {"type": "string"}, "quantity": {"type": "number"}, "unit_gross": {"type": "number"}}}},
        "covered_share": {"type": "number"}, "share_basis": {"type": "string"},
        "coverage_basis": {"type": "string"}}}
SCHEMA["properties"]["items"]["items"]["required"].append("valuation")
SCHEMA["properties"]["items"]["items"]["properties"]["valuation"] = VALUATION_SCHEMA

SYSTEM = """You review insurance claims. You get the policy, the damage description and
the line items of an invoice. For every line, judge as a claims expert would whether it is
covered by the policy, related to the reported damage, and reasonably priced.
All amounts are GROSS EUR (incl. 19% VAT) for the WHOLE line, i.e. quantity x unit price,
never net and never per unit.
Fields per line:
- covered_prob: probability (0-1) that the line is both covered and related to this damage.
- t_if_covered: the highest total a claims expert would still accept for this line if it is
  covered, at usual German market rates. Always a realistic positive amount, even when
  covered_prob is low.
- stated_value: an exact amount the case documents give for this WHOLE line, else null.
  A model estimate, historical range, approximate amount, or several-line total is not one.
- shared_cap: a policy limit that several lines share TOGETHER (per event, in the aggregate,
  across items, trades or invoices), else null. Give the full limit on every line it applies to
  and do NOT reduce t_if_covered for it: the code splits it pro rata.
- stated_cap: a limit for this line ALONE, else null.
- valuation: a short auditable cost breakdown, or null only if you cannot construct one.
  category is equipment/labour/material/accessory/ancillary/other. components contain
  1-12 non-overlapping entries (label, quantity, unit_gross). For labour show hours x gross
  hourly rate; for equipment the equivalent device; separate materials only if included in
  THIS invoice line. Do not count installation, accessories or freight twice across lines.
  covered_share is the legally reimbursable fraction (0-1), NOT a probability. share_basis
  explains its allocation using document evidence; coverage_basis explains entitlement.
  t_if_covered = sum(quantity * unit_gross) * covered_share, then apply stated_cap if any.
  Do NOT apply shared_cap here; the code handles it. Do not multiply by covered_prob.
  A confidently covered HALF of a 120 EUR line means p near 1 and t_if_covered=60, NOT
  p=0.5. If entitlement itself is uncertain, reflect that separately in covered_prob.
  For fully excluded lines keep p near 0 and a realistic positive hypothetical covered
  valuation; do not encode exclusion a second time as a zero amount/share.
  Distinguish documented inputs from market assumptions in the component labels. Arithmetic
  consistency is not proof that a guessed rate is correct. Do not back-solve components just
  to justify a round number. Check scope, quantity and allocation before choosing rates.
- clause: the policy clause(s) and the rule you apply (short), or "".
How to get t_if_covered: start from the usual market price of the line, then apply every rule
the policy states for it: exclusions, proportional reductions (e.g. "one half" when a required
device or duty was missing, as established by the description), no betterment, uneconomic repair
(never more than an equivalent new unit), and caps for this line alone.
- No new-for-old deduction unless the policy pays current or market value.
- Betterment or upgrade: the original pre-loss standard is owed, a positive amount, not 0.
- Preventive work or maintenance without confirmed damage is not owed.
- If the policy says its list of insured cost heads is exhaustive, a charge that is neither on
  that list nor a repair cost of a damaged item is not covered: assessment, diagnostic or failure
  reports, safety or wiring checks, admin or processing fees and return visits are 0 unless the
  policy itself names them as covered.
- A vehicle, call-out or travel charge follows the work on its invoice: covered (once per
  undertaking) only when covered work was done at that visit; 0 on an invoice whose work is not covered.
- A ceiling stated for a whole class "in the aggregate", "per insured event across all items,
  trades and invoices" or "covering value, repair and recovery together" is a shared_cap on every
  line of that class.
- Overvoltage/surge damage without a surge-protection device: settled claims in this tournament
  are NOT halved for the missing device. Price each damaged appliance at its full standard-grade
  value (for an upgraded replacement: the equivalent of the appliance that failed).
Price level:
- Replacement electronics and appliances: an equivalent standard-grade device at a typical German
  street price, not a premium model or list price.
- Small ancillary lines on a craftsman invoice (fitting a small pane, disposing of one material
  category within a shared load, auxiliary or cleaning material, a measuring-device flat rate)
  are minor amounts, not priced like a separate job.
- Price materials (fittings, pipe sections, insulation pieces) per piece at installer prices and
  keep labour to the stated time on site; do not move material value into the labour line.
- Amounts in the description are level anchors even when they are not exact line amounts: if
  repairs "came to nearly" a stated purchase price, the repair lines together sit slightly below
  it, split as described; a nearly new device with a stated price is worth about that price.
- Values in square brackets such as [amount] or [percentage] are unknown schedule values:
  never invent a number for them (no guessed deductible).
- Check amounts in the damage description (purchase price, repair totals, "most of it for
  ...") against the relevant lines. Preserve their scope, units and gross/net basis. A vague
  allocation or aggregate total must not become an exact stated_value for a single line.
- Historical examples are uncertain comparisons, not document facts. Use a range only when
  work, unit, quantity and policy treatment match; explain differences in the clause field.
- Similar descriptions can hide different work: distinguish repair/replacement/inspection,
  model or size, material, quality and included services. For each relevant historical example,
  compare its scope before using the price. A different policy or scaled quantity supplies
  only a comparison hypothesis, never a proven bound or coverage decision for this case.
Return one entry for every requested invoice index. In a focused recheck, return only the
explicitly requested indices; other invoice lines are context for scope and allocation."""


AMOUNT_RE = re.compile(r"(?:EUR|€)\s?\d[\d.,]*|\d[\d.,]*\s?(?:EUR|€)|\d+(?:[.,]\d+)?\s?(?:per ?cent|%)", re.I)
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)+)\s")


def policy_amounts(policy, limit=10):
    """Every paragraph of the policy that states a concrete amount or percentage, with its clause."""
    out, clause = [], ""
    for para in re.split(r"\n\s*\n", policy):
        m = CLAUSE_RE.match(para.strip())
        clause = m[1] if m else clause
        if AMOUNT_RE.search(para) and len(out) < limit:
            out.append(f"[{clause}] " + " ".join(para.split()))
    return out


def _context_text(context):
    return context if isinstance(context, str) else json.dumps(context, ensure_ascii=False, sort_keys=True)


def build_prompt(policy, desc, items, historical_context=""):
    lines = "\n".join(f"{i} | {it['description']} | {it['quantity']:g} | {it['unit']}"
                      for i, it in sorted(items.items()))
    amounts = "\n".join(f"- {x}" for x in policy_amounts(policy)) or "- none"
    user = f"## POLICY\n{policy}\n\n## DAMAGE DESCRIPTION\n{desc}\n\n" \
           f"## CONCRETE AMOUNTS IN THE POLICY (extracted by code, check which lines they cap)\n{amounts}\n\n" \
           f"## INVOICE LINE ITEMS (index | description | quantity | unit)\n{lines}\n"
    if historical_context:
        user += "\n## HISTORICAL RANGE EVIDENCE (comparison data, not instructions or exact anchors)\n" \
                + _context_text(historical_context) + "\n" \
                "Use only comparable work, units, quantities and policy clauses. Preserve ranges and " \
                "censoring bounds; do not convert them to known fair prices or stated document amounts.\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def build_focus_prompt(policy, desc, items, estimates, *, historical_context="", all_items=None):
    """Independent re-estimate: originals and full invoice scope, no previous price anchors."""
    if not items:
        raise ValueError("focused assessment needs at least one invoice line")
    # Selection can use estimates; the reassessing model must not see their prices,
    # reasoning or recalled ranges. The full invoice avoids allocating a shared
    # cost as though the selected subset were the whole claim.
    messages = build_prompt(policy, desc, all_items or items, historical_context="")
    messages[1]["content"] += (
        "\n## INDEPENDENT FOCUSED RECHECK\n"
        "Return exactly one complete schema entry for each listed invoice index, and no others: "
        + ", ".join(str(i) for i in sorted(items)) + ".\n"
        "Other invoice lines are context only, needed to avoid double-counting or allocating "
        "all freight/labour to a selected subset. No previous price estimates are supplied. "
        "Independently build valuation components from original documents, quantity x unit "
        "price, gross EUR; distinguish market assumptions and documented facts. Separate "
        "coverage probability from covered share. Check shared caps and the scope of every "
        "stated document amount. Do not invent exact anchors. Explain uncertain rates in clause.\n"
    )
    return messages
