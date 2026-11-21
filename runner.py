#!/usr/bin/env python3
"""Claim to Fame runner (QuantCo, START Hack Munich 2026).

Pipeline per round: key -> decrypt -> parse PDF -> provisional submission (rules)
-> 3 LLM votes on t per line -> deterministic a/b -> final submission -> echo check.

Exactly ONE runner may play (lockfile out/runner.lock). Keys come from .env and
are never printed or logged.

  uv run runner.py              play every upcoming round (loop)
  uv run runner.py --check      preflight: keys present, API, zips, model
  uv run runner.py --game 0     play game 0 now (test round, stores nothing)
  flags: --dry (no PUT), --no-llm (rules only)
"""
import argparse, datetime as dt, fcntl, json, logging, math, os, re
import shutil, socket, statistics, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CASES, OUT = ROOT / "cases", ROOT / "out"
BASE = "https://c2f.public.quantco.cloud"
ROUND_SECONDS = 60          # submission window after start_time
SAFETY = 4                  # stop writing this many seconds before the window closes
DISAGREE = 1.5              # max/min ratio of votes counted as disagreement
log = logging.getLogger("c2f")


# ---------------------------------------------------------------- env, logging
def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v


class Redact(logging.Filter):
    PAT = re.compile(r"sk-[A-Za-z0-9_\-*]{6,}")

    def filter(self, rec):
        rec.msg = self.PAT.sub("sk-***", str(rec.msg))
        if isinstance(rec.args, tuple):
            rec.args = tuple(self.PAT.sub("sk-***", a) if isinstance(a, str) else a for a in rec.args)
        return True


def setup_logging():
    OUT.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(OUT / "runner.log")):
        h.setFormatter(fmt)
        h.addFilter(Redact())
        log.addHandler(h)
    log.setLevel(logging.INFO)


def take_lock():
    f = open(OUT / "runner.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another runner is already active on this machine (out/runner.lock). Abort.")
    f.write(f"{os.getpid()} {socket.gethostname()}\n")
    f.flush()
    return f


def jlog(name, rec):
    with open(OUT / name, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# ---------------------------------------------------------------- game API
S = requests.Session()


def hdr():
    return {"X-API-Key": os.environ["TEAM_API_KEY"]}


def parse_time(s):
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def list_games():
    r = S.get(f"{BASE}/api/games/list", headers=hdr(), timeout=5)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("items") or data.get("games") or []
    return sorted((int(g["id"]), parse_time(g["start_time"])) for g in data)


def fetch_key(gid, deadline):
    while time.time() < deadline:
        try:
            r = S.get(f"{BASE}/api/games/{gid}/key", headers=hdr(), timeout=3)
            if r.status_code == 200:
                j = r.json()
                return j.get("decryption_key") or j.get("key")
            if r.status_code in (401, 404):
                raise RuntimeError(f"key endpoint HTTP {r.status_code}")
        except requests.RequestException as e:
            log.warning("key fetch: %s", type(e).__name__)
        time.sleep(0.25)
    raise TimeoutError("no key before deadline")


def submit(gid, ab, deadline, dry, tag):
    """ab: {index: (a, b)} for EVERY line. Returns True if stored as sent."""
    body = [{"index": i, "charge_price": floor_c(a), "acceptance_limit": ceil_c(b)}
            for i, (a, b) in sorted(ab.items())]
    jlog("submissions.jsonl", {"t": time.time(), "game": gid, "tag": tag, "dry": dry, "body": body})
    if dry:
        log.info("[%s] DRY %s: %d lines not sent", gid, tag, len(body))
        return True
    for attempt in range(3):
        if time.time() > deadline:
            log.error("[%s] %s: deadline passed, not sent", gid, tag)
            return False
        try:
            r = S.put(f"{BASE}/api/games/{gid}/submissions", headers=hdr(), json=body, timeout=5)
            if r.ok:
                ok = echo_ok(body, r.json() if r.content else [], gid)
                log.info("[%s] %s sent: %d lines, HTTP %d, echo %s",
                         gid, tag, len(body), r.status_code, "ok" if ok else "MISMATCH")
                if ok or gid == 0:
                    return True
            else:
                log.error("[%s] %s HTTP %d: %s", gid, tag, r.status_code, r.text[:200])
                if r.status_code in (401, 403, 422):
                    return False
        except requests.RequestException as e:
            log.warning("[%s] %s attempt %d: %s", gid, tag, attempt + 1, type(e).__name__)
        time.sleep(0.5)
    return False


def echo_ok(body, echo, gid):
    if isinstance(echo, dict):
        echo = echo.get("items") or echo.get("submissions") or []
    got = {}
    for x in echo if isinstance(echo, list) else []:
        i = x.get("line_item_index", x.get("index"))
        got[i] = (x.get("charge_price"), x.get("acceptance_limit"))
    if gid == 0 and not got:
        return True                     # game 0 stores nothing
    for row in body:
        a, b = got.get(row["index"], (None, None))
        if a is None or abs(a - row["charge_price"]) > 0.005 or abs(b - row["acceptance_limit"]) > 0.005:
            return False
    return True


# ---------------------------------------------------------------- case files
def find_zip(gid):
    for p in sorted(CASES.rglob("*.zip")):
        nums = re.findall(r"\d+", p.stem)
        if nums and int(nums[-1]) == gid:
            return p
    raise FileNotFoundError(f"no zip for game {gid} in cases/")


def decrypt(gid, key):
    z = find_zip(gid)
    d = OUT / "cases" / f"case_{gid:03d}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    exe = shutil.which("7z") or shutil.which("7zz")
    if exe:
        subprocess.run([exe, "x", "-y", f"-p{key}", f"-o{d}", str(z)],
                       check=True, capture_output=True, timeout=20)
    else:                               # pure-Python AES fallback
        import pyzipper
        with pyzipper.AESZipFile(z) as zf:
            zf.setpassword(key.encode())
            zf.extractall(d)
    return d


def read_text(d, pattern):
    hits = sorted(d.rglob(pattern))
    return "\n\n".join(p.read_text(errors="replace") for p in hits)


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


def parse_lines(lines):
    items = {}
    for ln in lines:
        m = LINE_RE.match(ln)
        if m:
            items.setdefault(int(m[1]), {"description": m[2].strip(), "quantity": num(m[3]), "unit": m[4]})
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


# ---------------------------------------------------------------- OpenAI with key cascade
class NoKeys(Exception):
    pass


class KeyPool:
    def __init__(self):
        names = sorted((k for k in os.environ if re.fullmatch(r"OPENAI_KEY_\d+", k)),
                       key=lambda k: int(k.rsplit("_", 1)[1]))
        names.append("OPENAI_KEY")                 # starter-kit name, last resort
        self.names = [n for n in names if os.environ.get(n)]
        self.dead, self.round_blocked, self.clients = set(), set(), {}
        self.lock = threading.Lock()

    def new_round(self):
        self.round_blocked.clear()

    def current(self):
        with self.lock:
            return next((n for n in self.names if n not in self.dead | self.round_blocked), None)

    def disable(self, name, permanent, why):
        with self.lock:
            if name in self.dead or (name in self.round_blocked and not permanent):
                return
            (self.dead if permanent else self.round_blocked).add(name)
        log.warning("KEY SWITCH: %s off (%s) for %s; now using %s", name, why,
                    "rest of tournament" if permanent else "this round", self.current() or "NONE -> rules")

    def client(self, name):
        from openai import OpenAI
        if name not in self.clients:
            self.clients[name] = OpenAI(api_key=os.environ[name], max_retries=0, timeout=20)
        return self.clients[name]


POOL = None


class Tier:
    """One LLM pipeline. All tiers run in parallel; a higher level overwrites a lower one."""
    def __init__(self, name, level, model, reasoning, votes, budget):
        self.name, self.level, self.model, self.votes, self.budget = name, level, model, votes, budget
        self.extra = {"reasoning_effort": reasoning} if reasoning else {}   # dropped if rejected
        self.n_ok = True          # one request with n votes (prompt billed once) until refused


def build_tiers():
    env = os.environ.get
    tiers = [Tier("fast", 1, env("FAST_MODEL") or env("OPENAI_MODEL") or "gpt-5.4-mini",
                  env("FAST_REASONING", "low"), int(env("FAST_VOTES", "3")), float(env("FAST_BUDGET", "20")))]
    if env("DEEP_MODEL", "gpt-5.4").lower() != "off":
        tiers.append(Tier("deep", 2, env("DEEP_MODEL", "gpt-5.4"), env("DEEP_REASONING", "high"),
                          int(env("DEEP_VOTES", "1")), float(env("DEEP_BUDGET", "50"))))
    return tiers


TIERS = []


def llm_call(messages, n, tier):
    import openai
    while True:
        name = POOL.current()
        if not name:
            raise NoKeys()
        try:
            return POOL.client(name).chat.completions.create(
                model=tier.model, messages=messages, n=n, **tier.extra,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "line_items", "strict": True, "schema": SCHEMA}})
        except openai.AuthenticationError:
            POOL.disable(name, True, "401")
        except openai.PermissionDeniedError:
            POOL.disable(name, True, "403")
        except openai.RateLimitError as e:
            quota = getattr(e, "code", None) == "insufficient_quota" or "insufficient_quota" in str(e)
            POOL.disable(name, quota, "429 insufficient_quota" if quota else "429 rate limit")
        except openai.BadRequestError as e:
            param, msg = getattr(e, "param", None) or "", str(e).lower()
            dropped = [k for k in list(tier.extra) if k == param or k in msg]
            if dropped:
                for k in dropped:
                    tier.extra.pop(k)
                log.warning("%s (%s) rejects %s: dropped", tier.name, tier.model, dropped)
            elif n > 1 and (param == "n" or "'n'" in msg or "n parameter" in msg):
                tier.n_ok = False
                raise
            else:
                raise


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["index", "covered_prob", "t_if_covered", "stated_value", "stated_cap", "clause"],
        "properties": {"index": {"type": "integer"},
                       "covered_prob": {"type": "number"},
                       "t_if_covered": {"type": "number"},
                       "stated_value": {"type": ["number", "null"]},
                       "stated_cap": {"type": ["number", "null"]},
                       "clause": {"type": "string"}}}}}}

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
- stated_cap: a policy limit that caps this line, else null.
- clause: the policy clause you rely on (short), or "".
Return one entry for every invoice index."""


def build_prompt(policy, desc, items):
    lines = "\n".join(f"{i} | {it['description']} | {it['quantity']:g} | {it['unit']}"
                      for i, it in sorted(items.items()))
    user = f"## POLICY\n{policy}\n\n## DAMAGE DESCRIPTION\n{desc}\n\n" \
           f"## INVOICE LINE ITEMS (index | description | quantity | unit)\n{lines}\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def assess(messages, timeout, tier):
    """Returns (list of votes {index: dict}, usage dict). Votes may be fewer than tier.votes."""
    usage = {"prompt": 0, "cached": 0, "completion": 0, "calls": 0}

    def add(resp):
        u = resp.usage
        if u:
            usage["prompt"] += u.prompt_tokens
            usage["completion"] += u.completion_tokens
            d = getattr(u, "prompt_tokens_details", None)
            usage["cached"] += (getattr(d, "cached_tokens", 0) or 0) if d else 0
        usage["calls"] += 1
        return [parse_vote(c.message.content) for c in resp.choices]

    ex = ThreadPoolExecutor(tier.votes)          # no `with`: never block on a hanging call
    try:
        if tier.n_ok or tier.votes == 1:
            fut = ex.submit(llm_call, messages, tier.votes, tier)
            done, _ = wait([fut], timeout=timeout)
            if fut in done and fut.exception() is None:
                return add(fut.result()), usage
            if fut in done and isinstance(fut.exception(), NoKeys):
                raise NoKeys()
            if fut not in done or tier.n_ok:       # timeout or other error: give up
                raise RuntimeError(f"LLM failed: {type(fut.exception()).__name__ if fut in done else 'timeout'}")
        futs = [ex.submit(llm_call, messages, 1, tier) for _ in range(tier.votes)]
        done, _ = wait(futs, timeout=timeout)
        votes = []
        for f in done:
            if f.exception() is None:
                votes += add(f.result())
        if not votes:
            raise RuntimeError("all LLM calls failed")
        return votes, usage
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def parse_vote(content):
    out = {}
    for x in json.loads(content).get("items", []):
        try:
            out[int(x["index"])] = x
        except (KeyError, ValueError, TypeError):
            pass
    return out


# ---------------------------------------------------------------- deterministic pricing
def floor_c(x):
    x = float(x) if x is not None else 0.0
    return math.floor(round(x * 100, 6)) / 100 if math.isfinite(x) and x > 0 else 0.0   # round: float noise


def ceil_c(x):
    x = float(x) if x is not None else 0.0
    return math.ceil(round(x * 100, 6)) / 100 if math.isfinite(x) and x > 0 else 0.0   # round: float noise


def pos(x):
    try:
        x = float(x)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError):
        return None


def aggregate(votes):
    """votes for ONE line -> (p, m, stated, cap)."""
    p = statistics.median(min(max(float(v.get("covered_prob") or 0), 0.0), 1.0) for v in votes)
    vals = sorted(x for x in (pos(v.get("t_if_covered")) for v in votes) if x)
    m = 0.0
    if vals:
        m = math.exp(statistics.median(math.log(x) for x in vals))
        # Votes disagree: lean high (geo mean of the two highest). Official payoffs: rejecting
        # a fair claim costs 1.5a, accepting an inflated one at most min(a, c).
        if len(vals) >= 2 and vals[-1] / vals[0] >= DISAGREE:
            m = math.sqrt(vals[-1] * vals[-2])

    def majority(key):                              # only trust a stated value most votes report
        xs = [x for x in (pos(v.get(key)) for v in votes) if x]
        return statistics.median(xs) if len(xs) * 2 > len(votes) else None
    return p, m, majority("stated_value"), majority("stated_cap")


def price(p, m, stated, cap, fallback):
    if stated:
        a = stated
        b = stated if p > 2 / 3 else 0.0
    else:
        a = m if m > 0 else fallback                # t = 0 lines still charge: costs nothing if rejected
        b = a if p > 2 / 3 else 0.0                 # accept if 1.5*P(fair) > 1, i.e. P(fair) > 2/3
    if cap:
        a, b = min(a, cap), min(b, cap)
    return floor_c(a), ceil_c(b)


# ---------------------------------------------------------------- one round
def play_game(gid, start, dry=False, use_llm=True):
    POOL.new_round()
    deadline = start + ROUND_SECONDS - SAFETY if gid != 0 else time.time() + ROUND_SECONDS
    key = fetch_key(gid, deadline)
    t_key = time.time()
    d = decrypt(gid, key)
    items = parse_items(d)
    fb = {i: rule_value(it) for i, it in items.items()}
    prov_ok = submit(gid, {i: (v, v) for i, v in fb.items()}, deadline, dry, "provisional")
    t_prov = time.time() - t_key
    log.info("[%s] %d lines, provisional after %.1fs", gid, len(items), t_prov)

    rules = {i: (floor_c(v), ceil_c(v)) for i, v in fb.items()}
    best = {"level": 0, "ab": rules, "tier": "rules"}
    out, usage, slock = {"rules": rules}, {}, threading.Lock()

    def run_tier(tier, msgs):
        try:
            budget = min(tier.budget - (time.time() - t_key), deadline - time.time() - 3)
            voices, usage[tier.name] = assess(msgs, budget, tier)
            part = {}
            for i in items:
                votes = [v[i] for v in voices if i in v]
                if votes:
                    part[i] = price(*aggregate(votes), fb[i])
            out[tier.name] = part
            with slock:                             # ordered: a slower lower tier never overwrites
                if tier.level < best["level"]:
                    log.info("[%s] %s done after %s: not sent", gid, tier.name, best["tier"])
                    return
                merged = {**best["ab"], **part}
                if submit(gid, merged, deadline, dry, tier.name):
                    best.update(level=tier.level, ab=merged, tier=tier.name)
            log.info("[%s] %s (%s): %d/%d lines, %.1fs after key, tokens %s", gid, tier.name, tier.model,
                     len(part), len(items), time.time() - t_key, usage[tier.name])
        except NoKeys:
            log.error("[%s] %s: all OpenAI keys exhausted", gid, tier.name)
        except Exception as e:
            log.error("[%s] %s error %s", gid, tier.name, type(e).__name__)

    if use_llm and POOL.current():
        msgs = build_prompt(read_text(d, "policy*.txt"), read_text(d, "description*.txt"), items)
        threads = [threading.Thread(target=run_tier, args=(t, msgs), daemon=True) for t in TIERS]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=max(0.0, deadline - time.time()))
    elif use_llm:
        log.error("[%s] no usable OpenAI key: rule-based submission stands", gid)
    final = best["ab"]
    rec = {"game": gid, "lines": len(items), "winner": best["tier"], "ok": prov_ok or best["level"] > 0,
           "t_provisional": round(t_prov, 2), "t_end": round(time.time() - t_key, 2), "usage": usage,
           "models": {t.name: t.model for t in TIERS}, "items": items, "tiers": out}
    jlog("rounds.jsonl", rec)
    log.info("[%s] DONE winner %s, sum a %.0f", gid, best["tier"], sum(a for a, _ in final.values()))
    return rec


# ---------------------------------------------------------------- preflight and loop
def check():
    print("TEAM_API_KEY:", "set" if os.environ.get("TEAM_API_KEY") else "MISSING")
    print("OpenAI keys:", POOL.names or "NONE (rules only)")
    print("tiers:", [(t.name, t.model, t.extra.get("reasoning_effort"), t.votes) for t in TIERS])
    print("7z:", shutil.which("7z") or shutil.which("7zz") or "not found, using pyzipper")
    for k, n in enumerate(POOL.names):
        try:
            c = POOL.client(n)
            for t in TIERS:
                c.models.retrieve(t.model)
            print(f"  {n}: ok, tier models reachable")
            if k == 0:
                ids = sorted(m.id for m in c.models.list() if m.id.startswith(("gpt", "o")))
                print("  models on this key:", ", ".join(ids))
        except Exception as e:
            print(f"  {n}: {type(e).__name__}")
    zips = sorted(CASES.rglob("*.zip"))
    print(f"zips in cases/: {len(zips)}")
    try:
        games = list_games()
        have = {int(re.findall(r'\d+', z.stem)[-1]) for z in zips if re.findall(r'\d+', z.stem)}
        missing = [g for g, _ in games if g not in have]
        nxt = [(g, s) for g, s in games if s > time.time()][:1]
        print(f"games: {len(games)}, missing zips: {missing[:15]}{' ...' if len(missing) > 15 else ''}")
        if nxt:
            print(f"next: game {nxt[0][0]} at {dt.datetime.fromtimestamp(nxt[0][1]).strftime('%a %H:%M:%S')}")
    except Exception as e:
        print("game API:", type(e).__name__, getattr(getattr(e, "response", None), "status_code", ""))


def run_forever(dry, use_llm):
    done = set()
    log.info("runner up: pid %d on %s, keys %s, tiers %s", os.getpid(), socket.gethostname(), POOL.names,
             [(t.name, t.model) for t in TIERS])
    while True:
        try:
            games = list_games()
        except Exception as e:
            log.warning("list games: %s", type(e).__name__)
            time.sleep(5)
            continue
        now = time.time()
        todo = [(g, s) for g, s in games if g != 0 and g not in done and s + ROUND_SECONDS - SAFETY > now]
        if not todo:
            time.sleep(30)
            continue
        gid, start = todo[0]
        if start - now > 40:
            time.sleep(min(start - now - 30, 60))
            continue
        try:
            find_zip(gid)
        except FileNotFoundError:
            log.critical("[%s] ZIP MISSING in cases/ - download it now!", gid)
        time.sleep(max(0.0, start - time.time() - 0.2))
        try:
            play_game(gid, start, dry, use_llm)
        except Exception as e:
            log.exception("[%s] round failed: %s", gid, type(e).__name__)
        done.add(gid)


def main():
    global POOL, TIERS
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int, help="play this game now (0 = test game)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry", action="store_true", help="do not send submissions")
    ap.add_argument("--no-llm", action="store_true", help="rules only")
    args = ap.parse_args()
    load_env()
    TIERS = build_tiers()
    POOL = KeyPool()
    setup_logging()
    if args.check:
        return check()
    if not os.environ.get("TEAM_API_KEY"):
        sys.exit("TEAM_API_KEY missing in .env")
    lock = take_lock()                              # noqa: F841 (held for process lifetime)
    if args.game is not None:
        play_game(args.game, time.time(), args.dry, not args.no_llm)
    else:
        run_forever(args.dry, not args.no_llm)


if __name__ == "__main__":
    main()
