"""Insurance assessment instructions, response schema and prompt construction."""
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
- stated_value: an amount the case documents give for exactly this line, else null.
- shared_cap: a policy limit that several lines share TOGETHER (per event, in the aggregate,
  across items, trades or invoices), else null. Give the full limit on every line it applies to
  and do NOT reduce t_if_covered for it: the code splits it pro rata.
- stated_cap: a limit for this line ALONE, else null.
- clause: the policy clause(s) and the rule you apply (short), or "".
How to get t_if_covered: start from the usual market price of the line, then apply every rule
the policy states for it: exclusions, proportional reductions (e.g. "one half" when a required
device or duty was missing, as established by the description), no betterment, uneconomic repair
(never more than an equivalent new unit), and caps for this line alone.
- No new-for-old deduction unless the policy pays current or market value.
- Betterment or upgrade: the original pre-loss standard is owed, a positive amount, not 0.
- Preventive work or maintenance without confirmed damage is not owed.
- Values in square brackets such as [amount] or [percentage] are unknown schedule values:
  never invent a number for them (no guessed deductible).
- Amounts in the damage description (purchase price, what the repairs came to, "most of it for
  ...") are hard anchors for the lines they concern.
Return one entry for every invoice index."""


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


def build_prompt(policy, desc, items):
    lines = "\n".join(f"{i} | {it['description']} | {it['quantity']:g} | {it['unit']}"
                      for i, it in sorted(items.items()))
    amounts = "\n".join(f"- {x}" for x in policy_amounts(policy)) or "- none"
    user = f"## POLICY\n{policy}\n\n## DAMAGE DESCRIPTION\n{desc}\n\n" \
           f"## CONCRETE AMOUNTS IN THE POLICY (extracted by code, check which lines they cap)\n{amounts}\n\n" \
           f"## INVOICE LINE ITEMS (index | description | quantity | unit)\n{lines}\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


ESC_PROMPT = """SECOND LOOK. Lines {lines} carry most of the money in this claim. Re-assess ONLY
these lines, slowly, in this order: (1) what exactly was done or supplied and in which quantity;
(2) the realistic German market price of exactly that work today, gross; (3) every clause that
changes the amount for this line: exclusions, proportional reductions, betterment, uneconomic
repair, attendance rules, limits for the line alone (stated_cap) or shared with other lines
(shared_cap, full limit, do not split it yourself); (4) any amount in the damage description that
anchors the value. Then give the fields as before. Return entries ONLY for indices {lines}."""


def build_escalation(messages, lines):
    """Same conversation (cached prefix) plus one focused follow-up for the chosen lines."""
    text = ESC_PROMPT.format(lines=", ".join(str(i) for i in lines))
    return list(messages) + [{"role": "user", "content": text}]
