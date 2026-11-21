"""Invoice parsing and rule-based fallback values; no network access."""
import logging
import re

log = logging.getLogger("c2f")


def num(s):
    s = (s or "").strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 1.0


UNITS = (r"flat[ -]?rate|lump[ -]?sum|pcs?\.?|pieces?|hrs?\.?|hours?|h|m2|m²|sqm|m3|m³|lm|rm|m|"
         r"kg|t|l|days?|weeks?|units?|sets?|km|stk\.?|std\.?|psch\.?|pauschal\w*|stück")
LINE_RE = re.compile(rf"^\s*(\d{{1,3}})\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+({UNITS})(?:\s|$)", re.I)


def header_map(low):
    def pick(keys, default):
        for j, x in enumerate(low):
            if any(k in x for k in keys):
                return j
        return default
    return (pick(("desc", "item", "service", "leistung"), 1),
            pick(("amount", "qty", "quant", "menge"), 2),
            pick(("unit", "einheit"), 3))


# Line ends with quantity + unit (TOTAL column is empty): greedy description takes the LAST
# number+unit, so "Cable 3 m  2 pcs" reads 2 pcs, not 3 m. LINE_RE stays as fallback.
LINE_END_RE = re.compile(rf"^\s*(\d{{1,3}})\s+(.+)\s+(\d+(?:[.,]\d+)?)\s+({UNITS})\s*$", re.I)
STOP_RE = re.compile(r"^\s*(created on|page \d|sub-?total\b|total\b|vat\b|net\b|gross\b|invoice\b|items\b|pos\b|from\b)", re.I)


def parse_lines(lines):
    """Item rows plus wrapped description lines (appended to the item above, max 3)."""
    items, cur, extra = {}, None, 0
    for ln in lines:
        m = LINE_END_RE.match(ln) or LINE_RE.match(ln)
        if m:
            i = int(m[1])
            cur, extra = (i, 0) if i not in items else (None, 0)
            items.setdefault(i, {"description": m[2].strip(), "quantity": num(m[3]), "unit": m[4]})
        elif cur is not None and ln.strip() and not STOP_RE.match(ln) and extra < 3:
            items[cur]["description"] += " " + ln.strip()
            extra += 1
        else:
            cur = None
    return items


def parse_items(d):
    import pdfplumber
    items, dups, text_lines = {}, [], []
    for pdf_path in sorted(d.rglob("*.pdf")):
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:                     # ALL pages, ALL stapled invoices
                text_lines += (page.extract_text() or "").splitlines()
                for table in page.extract_tables() or []:
                    cols = (1, 2, 3)
                    for row in table:
                        c = [(x or "").replace("\n", " ").strip() for x in row]
                        low = [x.lower() for x in c]
                        if low and low[0] in ("pos", "pos.", "#", "no.", "nr.", "item"):
                            cols = header_map(low)
                            continue
                        if len(c) < 3 or not re.fullmatch(r"\d{1,3}", c[0]):
                            continue
                        i = int(c[0])
                        it = {"description": c[cols[0]] if cols[0] < len(c) else "",
                              "quantity": num(c[cols[1]]) if cols[1] < len(c) else 1.0,
                              "unit": c[cols[2]] if cols[2] < len(c) else ""}
                        if i in items:
                            dups.append(i)
                        else:
                            items[i] = it
    by_text = parse_lines(text_lines)
    if len(by_text) > len(items):
        log.warning("table parser found %d lines, text parser %d: using text", len(items), len(by_text))
        items = by_text
    if dups:
        log.error("DUPLICATE POS numbers %s (stapled invoices restart numbering?)", sorted(set(dups)))
    if not items:
        raise RuntimeError("no line items parsed")
    return items


# ---------------------------------------------------------------- rule fallback (no LLM)
# Our own rough gross rates per unit. Used for the provisional submission and when no LLM
# answer arrives, so a line is never left at the 0/0 default.
UNIT_RATE = {"hrs": 65.0, "m2": 40.0, "m": 25.0, "m3": 60.0, "pcs": 60.0, "flat rate": 200.0}
FALLBACK_LINE = 150.0       # unknown unit
FALLBACK_MAX = 1500.0       # rules alone never charge more than this per line


def norm_unit(u):
    u = u.lower().strip(" .")
    for key, alts in {"pcs": ("pc", "piece", "stk", "stück", "unit", "set"),
                      "hrs": ("hr", "hour", "h", "std"),
                      "flat rate": ("flat", "lump", "psch", "pauschal"),
                      "m2": ("m²", "sqm", "qm"), "m3": ("m³",), "m": ("lm", "rm", "meter")}.items():
        if u == key or u.startswith(alts):
            return key
    return u


def rule_value(it):
    q = it["quantity"] if it["quantity"] > 0 else 1.0
    rate = UNIT_RATE.get(norm_unit(it["unit"]))
    return min(rate * q, FALLBACK_MAX) if rate else FALLBACK_LINE
