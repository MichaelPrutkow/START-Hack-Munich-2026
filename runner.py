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
import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import requests

from claim_to_fame import pricing, prompts
from claim_to_fame import feedback
from claim_to_fame import price_memory, price_research
from claim_to_fame.control import assess_control
from claim_to_fame import recall as recall_mod
from claim_to_fame.invoices import (
    FALLBACK_LINE, FALLBACK_MAX, LINE_END_RE, LINE_RE, STOP_RE, UNIT_RATE, UNITS,
    header_map, norm_unit, num, parse_items, parse_lines, rule_value,
)
from claim_to_fame.pricing import apply_shared_caps, ceil_c, floor_c, pos
from claim_to_fame.prompts import (
    AMOUNT_RE, CLAUSE_RE, SCHEMA, SYSTEM, build_prompt, build_focus_prompt, policy_amounts,
)

ROOT = Path(__file__).resolve().parent
CASES, OUT = ROOT / "cases", ROOT / "out"
BASE = "https://c2f.public.quantco.cloud"
ROUND_SECONDS = 60          # submission window after start_time
SAFETY = 4                  # stop writing this many seconds before the window closes
DISAGREE = float(os.environ.get("DISAGREE", "1.5"))  # max/min vote ratio that leans high (August: +81k); 99 = off
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
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            r = S.put(f"{BASE}/api/games/{gid}/submissions", headers=hdr(), json=body,
                      timeout=min(5.0, max(0.05, remaining)))
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
    # defaults measured 21.11. on a 47k-char policy with 35 lines:
    # gpt-5.6-terra none n=3: 9.6 s; gpt-6.2-astra high: 19.8 s; gpt-6.2-sol high: > 20 s
    tiers = [Tier("fast", 1, env("FAST_MODEL", "gpt-5.6-terra"), env("FAST_REASONING", "none"), int(env("FAST_VOTES", "3")), float(env("FAST_BUDGET", "20")))]
    if env("DEEP_MODEL", "gpt-6.2-astra").lower() != "off":
        tiers.append(Tier("deep", 2, env("DEEP_MODEL", "gpt-6.2-astra"), env("DEEP_REASONING", "high"),
                          int(env("DEEP_VOTES", "3")), float(env("DEEP_BUDGET", "50"))))
        # gpt-6.2-astra answers n>1 with HTTP 400; probing it after every restart cost deep in G21
        # and the focus pass in G23/G24. Parallel single calls from the start (DEEP_BUNDLE=1 to probe).
        tiers[-1].n_ok = env("DEEP_BUNDLE", "0") == "1"
    # Optional slowest tier: runs in parallel, overwrites deep only if it finishes before the
    # deadline, otherwise deep's answer stands. Uses the time deep leaves unused (~11-25 s).
    if env("MAX_MODEL", "off").lower() != "off":
        tiers.append(Tier("max", 3, env("MAX_MODEL"), env("MAX_REASONING", "xhigh"),
                          int(env("MAX_VOTES", "3")), float(env("MAX_BUDGET", "52"))))
        tiers[-1].n_ok = env("MAX_BUNDLE", "0") == "1"
    return tiers


TIERS = []


def llm_call(messages, n, tier, timeout=20.0):
    import openai
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("LLM request budget exhausted")
        name = POOL.current()
        if not name:
            raise NoKeys()
        try:
            return POOL.client(name).chat.completions.create(
                model=tier.model, messages=messages, **({"n": n} if n > 1 else {}), **tier.extra, timeout=remaining,
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


class AssessmentError(RuntimeError):
    def __init__(self, reason, *, attempted, pending, failures):
        self.details = {"reason": reason, "attempted": attempted, "pending": pending, "failures": failures}
        super().__init__(reason)


def assess(messages, timeout, tier):
    """Returns (list of votes {index: dict}, usage dict). Votes may be fewer than tier.votes."""
    from openai import APITimeoutError

    deadline = time.monotonic() + timeout
    usage = {"prompt": 0, "cached": 0, "completion": 0, "calls": 0}
    votes, request_errors, invalid_votes = [], [], []
    attempted = pending = 0
    out_of_time = False

    def add(resp):
        u = resp.usage
        if u:
            usage["prompt"] += u.prompt_tokens
            usage["completion"] += u.completion_tokens
            d = getattr(u, "prompt_tokens_details", None)
            usage["cached"] += (getattr(d, "cached_tokens", 0) or 0) if d else 0
        usage["calls"] += 1
        if not resp.choices:
            invalid_votes.append("no_choices")
        for choice in resp.choices:
            try:
                vote = parse_vote(choice.message.content)
            except (ValueError, TypeError, AttributeError) as exc:
                invalid_votes.append(type(exc).__name__)
            else:
                if vote:
                    votes.append(vote)
                else:
                    invalid_votes.append("empty_vote")

    def collect(ex, n, count):
        nonlocal attempted, pending, out_of_time
        futures = []
        for _ in range(count):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                out_of_time = True
                break
            futures.append(ex.submit(llm_call, messages, n, tier, remaining))
        attempted += len(futures)
        done, unfinished = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        pending += len(unfinished)
        for future in futures:
            if future not in done:
                continue
            error = future.exception()
            if error is not None:
                request_errors.append(error)
            else:
                add(future.result())

    ex = ThreadPoolExecutor(tier.votes)          # no `with`: never block on a hanging call
    try:
        if tier.n_ok or tier.votes == 1:
            collect(ex, tier.votes, 1)
            # A provider that rejects bundled votes can use the remaining budget.
            if not votes and not pending and tier.votes > 1 and not tier.n_ok:
                collect(ex, 1, tier.votes)
        else:
            collect(ex, 1, tier.votes)
        if votes:
            return votes, usage
        if pending or out_of_time:
            reason = "deadline"
        elif any(isinstance(exc, (TimeoutError, APITimeoutError)) for exc in request_errors):
            reason = "request_timeout"
        elif request_errors and all(isinstance(exc, NoKeys) for exc in request_errors):
            raise NoKeys()
        elif invalid_votes:
            reason = "invalid_response"
        else:
            reason = "request_error"
        failures = list(invalid_votes)
        for exc in request_errors:
            status = getattr(exc, "status_code", None)
            category = type(exc).__name__
            failures.append(f"{category}/HTTP{status}" if type(status) is int and 400 <= status <= 599 else category)
        raise AssessmentError(reason, attempted=attempted, pending=pending, failures=failures)
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
# Keep the runner's public helpers and mutable settings compatible with offline tools.
# Domain functions take settings explicitly, so they can also be used without a runner.
A_MULT = float(os.environ.get("A_MULT", "0.85"))
SIGMA = float(os.environ.get("SIGMA", "0.35"))


def aggregate(votes):
    return pricing.aggregate(votes, disagree=DISAGREE)


def b_mult(p):
    return pricing.b_mult(p, sigma=SIGMA)


def price(p, m, stated, cap, fallback, *, uncertainty=0.0, bias=1.0, sigma=None, a_mult=None):
    return pricing.price(p, m, stated, cap, fallback, a_mult=A_MULT if a_mult is None else a_mult,
                         sigma=SIGMA if sigma is None else sigma,
                         uncertainty=uncertainty, bias=bias,
                         uncertainty_aware=os.environ.get("PRICING_UNCERTAINTY", "0") == "1")


def configure_pricing():
    """Apply loaded environment settings in both runner and offline evaluation."""
    global A_MULT, SIGMA, DISAGREE
    values = [float(os.environ.get(k, v)) for k, v in
              (("A_MULT", A_MULT), ("SIGMA", SIGMA), ("DISAGREE", DISAGREE))]
    if not all(math.isfinite(v) and v > 0 for v in values) or values[2] < 1:
        raise ValueError("A_MULT, SIGMA and DISAGREE must be finite and positive")
    A_MULT, SIGMA, DISAGREE = values


ENV_PATH = ROOT / ".env"
SETTINGS_SOURCE = "env"
AUTO_TUNE = True
TUNABLE = ("A_MULT", "SIGMA", "DISAGREE", "AUTO_TUNE")


def read_env(path):
    """{key: value} of a .env file (empty values skipped); unlike load_env, no side effects."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if v:
            out[k] = v
    return out


def recall_setting():
    """RECALL_CAP from .env each game: '2' (default) caps a and b at 0.95 x recalled unfair level,
    '1' two-sided midpoint caps on a, 'loose' also one-sided, 'shadow' logs, '0' off."""
    try:
        mode = read_env(ENV_PATH).get("RECALL_CAP", os.environ.get("RECALL_CAP", "2")).strip().lower()
    except Exception:
        mode = "2"
    return mode if mode in ("0", "1", "2", "loose", "shadow") else "2"


def refresh_settings():
    """Re-read manual defaults each game. AUTO_TUNE gates validated per-line learning.

    Exploratory params.json proposals have no independent holdout/estimator identity;
    only feedback.py's matching validated policy may override defaults. Never raises.
    """
    global A_MULT, SIGMA, DISAGREE, SETTINGS_SOURCE, AUTO_TUNE
    try:
        env = {**{k: os.environ[k] for k in TUNABLE if k in os.environ}, **read_env(ENV_PATH)}
        a, s = float(env.get("A_MULT", A_MULT)), float(env.get("SIGMA", SIGMA))
        dis, src = float(env.get("DISAGREE", DISAGREE)), "env"
        auto = env.get("AUTO_TUNE", "1") != "0"
        if not all(math.isfinite(x) and x > 0 for x in (a, s, dis)) or dis < 1:
            raise ValueError(f"bad settings A_MULT {a} SIGMA {s} DISAGREE {dis}")
        if (a, s, dis) != (A_MULT, SIGMA, DISAGREE):
            log.info("settings: A_MULT %.3f -> %.3f, SIGMA %.3f -> %.3f, DISAGREE %.2f -> %.2f (%s)",
                     A_MULT, a, SIGMA, s, DISAGREE, dis, src)
        A_MULT, SIGMA, DISAGREE, SETTINGS_SOURCE, AUTO_TUNE = a, s, dis, src, auto
    except Exception as e:
        log.warning("settings refresh failed, keeping A_MULT %.3f SIGMA %.3f: %s", A_MULT, SIGMA, e)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


GIT_COMMIT = "unknown"          # set once in main(); round records name the code that played


def git_commit():
    """Short HEAD commit, '+dirty' with uncommitted tracked changes. Never raises."""
    try:
        run = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True,
                                        timeout=5).stdout.strip()
        head = run("rev-parse", "--short", "HEAD")
        return (head or "unknown") + ("+dirty" if run("status", "--porcelain", "--untracked-files=no") else "")
    except Exception:                                  # pragma: no cover
        return "unknown"


# Pre-registered shadow variants (external review 21.11. 16:49): logged next to the real submission,
# never sent. "uncertain" = quantity <= 1 (lump sum, single device, mixed load) and no document amount.
SHADOW_A, SHADOW_B = 0.80, 0.80


def shadow_variants(items, final, details):
    """{variant: {i: (a, b)}} derived from the final submission; rule-based lines stay unchanged."""
    out = {"a80_all": {}, "a80_uncertain": {}, "b_le_80pct_a": {}}
    for i, (a, b) in final.items():
        e = details.get(i)
        llm = e is not None and e.get("stated") is None
        q = items.get(i, {}).get("quantity")
        uncertain = llm and isinstance(q, (int, float)) and q <= 1
        out["a80_all"][i] = (floor_c(a * SHADOW_A) if llm else a, b)
        out["a80_uncertain"][i] = (floor_c(a * SHADOW_A) if uncertain else a, b)
        out["b_le_80pct_a"][i] = (a, min(b, ceil_c(SHADOW_B * a)) if llm else b)
    return out


def strategy_identity():
    """Stable per-estimator identity; changing prices/logging does not erase evidence."""
    def code(fn):
        return digest(ast.dump(ast.parse(inspect.getsource(fn)), include_attributes=False))
    from claim_to_fame import assessment
    common = {"price_learning": price_research.signature(), "assessment_audit": code(assessment.audit), "assessment_number": code(assessment.number),
              "selection_policy": code(assessment.selection), "selection_wrapper": code(selection_decision),
              "disagreement": code(assessment.disagreement), "valuation_categories": assessment.CATEGORIES,
              "max_unreviewed_ratio": assessment.MAX_UNREVIEWED_RATIO,
              "aggregation": code(pricing.estimate_votes), "number_validation": code(pricing._number),
              "shared_caps": code(apply_shared_caps),
              "selection": code(prefer_estimate), "quality": code(estimate_quality),
              "disagree": DISAGREE}
    prompt = {"system": SYSTEM, "schema": SCHEMA, "builder": code(build_prompt),
              "policy_amounts": code(policy_amounts), "context": code(prompts._context_text),
              "amount_pattern": AMOUNT_RE.pattern,
              "recall": code(feedback.context_for_items), "recall_tokens": code(feedback._tokens),
              "comparable_memory": price_memory.identity(), "units": code(norm_unit)}
    configs, prompt_hashes = {}, {}
    tiers = list(TIERS)
    deep = next((t for t in tiers if t.name == "deep"), None)
    if deep is not None:
        tiers.append(Tier("focus", deep.level + 0.5, deep.model,
                          deep.extra.get("reasoning_effort"), 2, 12))
    for tier in tiers:
        configs[tier.name] = {**common, "model": tier.model, "votes": tier.votes,
                              "extra": dict(tier.extra), "tier": tier.name}
        prompt_hashes[tier.name] = digest({**prompt, **({"focus": code(build_focus_prompt),
                                                   "focus_selection": code(pricing.select_focus_items)}
                                                   if tier.name == "focus" else {})})
    return {"prompt_hash": prompt_hashes, "config_hash": {k: digest(v) for k, v in configs.items()},
            "config": configs, "pricing_defaults": {"a_mult": A_MULT, "sigma": SIGMA}}


def estimate_quality(estimate):
    """A conservative consistency heuristic, not a measured probability of accuracy."""
    n = max(1, estimate.get("valid_count", 1))
    invalid = estimate.get("invalid_count", 0) / max(n + estimate.get("invalid_count", 0), 1)
    spread = estimate.get("log_spread", 0.0) + estimate.get("p_spread", 0.0)
    anchors = sum(bool(a.get("conflicting") or a.get("invalid"))
                  for a in estimate.get("anchor_validity", {}).values())
    return spread + invalid + 0.25 * anchors


def selection_decision(candidate, previous, *, level, previous_level):
    from claim_to_fame import assessment
    return assessment.selection(candidate, previous, level=level, previous_level=previous_level,
                                new_quality=estimate_quality(candidate),
                                old_quality=estimate_quality(previous) if previous else 0)


def prefer_estimate(candidate, previous, *, level, previous_level):
    return selection_decision(candidate, previous, level=level, previous_level=previous_level)[0]


def atomic_selection(selected, *versions):
    """Retain whole old/new shared-cap groups, including transitive overlaps."""
    groups = {}
    for version in versions:
        for i, e in version.items():
            if e.get("shared") is not None:
                groups.setdefault(e["shared"], set()).add(i)
    selected = set(selected)
    while True:
        previous = selected.copy()
        for group in groups.values():
            if group.intersection(selected) and not group.issubset(selected):
                selected.difference_update(group)
        if selected == previous:
            return selected


# ---------------------------------------------------------------- one round
def log_prices(gid, name, items, part, est=None):
    """Logging only: one row per line with a/b (and p, m) of one stage. Never raises."""
    try:
        est = est or {}
        rows = [f"[{gid}] {name:<5} pos  description                     p      m          a          b"]
        for i in sorted(part):
            a, b = part[i]
            e = est.get(i, {})
            desc = str(items.get(i, {}).get("description", ""))[:30]
            pp = f"{e['p']:.2f}" if "p" in e else "   -"
            mm = f"{e['m']:9.2f}" if "m" in e else "        -"
            rows.append(f"[{gid}] {name:<5} {i:>3}  {desc:<30} {pp} {mm} {a:10.2f} {b:10.2f}")
        log.info("\n".join(rows))
    except Exception as ex:                          # pragma: no cover
        log.warning("[%s] price table %s failed: %s", gid, name, type(ex).__name__)


def play_game(gid, start, dry=False, use_llm=True):
    POOL.new_round()
    deadline = start + ROUND_SECONDS - SAFETY if gid != 0 else time.time() + ROUND_SECONDS
    key = fetch_key(gid, deadline)
    t_key = time.time()
    d = decrypt(gid, key)
    items = parse_items(d)
    fb = {i: rule_value(it) for i, it in items.items()}
    rules = {i: (floor_c(v), ceil_c(v)) for i, v in fb.items()}
    prov_ok = submit(gid, rules, deadline, dry, "provisional")
    confirmed = ["provisional"] if prov_ok else []       # stages whose submission the server confirmed
    t_prov = time.time() - t_key
    log.info("[%s] %d lines, provisional after %.1fs", gid, len(items), t_prov)
    log.info("[%s] .env: A_MULT %.3f  SIGMA %.3f", gid, A_MULT, SIGMA)
    identity = strategy_identity()
    policy, desc = read_text(d, "policy*.txt"), read_text(d, "description*.txt")
    state_path = Path(os.environ.get("LEARNING_FILE", str(OUT / "learning.json")))
    state = feedback.load_state(state_path, before_game=gid) if gid else None
    history = feedback.context_for_items(state, items, policy) if state else ""
    recall_mode, recall_caps = recall_setting(), {}
    if recall_mode != "0" and gid:
        try:
            recall_caps = recall_mod.caps_for(gid, items, policy, OUT / "cases", OUT / "leaderboard",
                                              os.environ.get("TEAM_NAME", "chineseModels"))
        except Exception as e:                      # recall is optional; never blocks a round
            log.warning("[%s] recall failed: %s", gid, type(e).__name__)
    log.info("[%s] recall (%s): %d lines with same-policy evidence, %d with a cap, %d with an unfair level",
             gid, recall_mode, len(recall_caps), sum(1 for v in recall_caps.values() if v["cap"] is not None),
             sum(1 for v in recall_caps.values() if v.get("cap_hi") is not None))
    log.info("[%s] learning: %s; completed game %s; recalled context %d chars", gid,
             "available" if state else "no usable artifact; baseline parameters",
             state.get("max_game") if state else "-", len(history))
    best = {"ab": rules, "tier": "rules", "est": {},
            "sources": {i: "rules" for i in items}, "levels": {i: 0 for i in items}}
    out, usage, est, vote_log = {"rules": rules}, {}, {}, {}
    llm_failures = {}
    slock, closed = threading.Lock(), threading.Event()
    log_prices(gid, "rules", items, rules)

    def candidate(tier, voices, selected_items):
        summaries = {i: pricing.estimate_votes([v[i] for v in voices if i in v], disagree=DISAGREE)
                     for i in selected_items}
        summaries = {i: e for i, e in summaries.items() if e["valid_count"] > 0}
        raw = {i: tuple(e[k] for k in ("p", "m", "stated", "cap", "shared"))
               for i, e in summaries.items()}
        # Focused reviews carry the other lines into shared-cap allocation.
        # Never reinterpret a partial group as a standalone policy limit.
        all_raw = dict(raw)
        with slock:
            previous = dict(best["est"])
        for i, e in previous.items():
            if i not in all_raw:
                all_raw[i] = (e["p"], e.get("m_raw", e["m"]), e.get("stated_raw", e.get("stated")),
                              e.get("cap_raw", e.get("cap")), e.get("shared"))
        adjusted = apply_shared_caps(all_raw)
        calibration = feedback.calibration_for(
            state, tier.name, model=tier.model, prompt_hash=identity["prompt_hash"].get(tier.name),
            config_hash=identity["config_hash"].get(tier.name)) if state else None
        diagnostic = feedback.calibration_status(
            state, tier.name, model=tier.model, prompt_hash=identity["prompt_hash"].get(tier.name),
            config_hash=identity["config_hash"].get(tier.name))
        if not AUTO_TUNE:
            diagnostic = {"status": "disabled", "reason": "AUTO_TUNE=0; manual defaults pinned"}
        elif os.environ.get("PRICING_UNCERTAINTY", "0") == "1":
            diagnostic = {"status": "disabled", "reason": "experimental uncertainty policy has no matching validation"}
        log.info("[%s] %s learning: %s", gid, tier.name, diagnostic)
        part, details, category_models = {}, {}, {}
        for i, summary in summaries.items():
            p, m, stated, cap = adjusted[i]
            # Group-affecting focused corrections need a full-case pass. Keeping
            # the previous complete allocation is safer than changing one share.
            if set(selected_items) != set(items) and (summary.get("shared") is not None or
                    previous.get(i, {}).get("shared") is not None):
                continue
            eligible = (AUTO_TUNE and calibration and os.environ.get("PRICING_UNCERTAINTY", "0") != "1"
                        and p >= calibration.get("min_covered_prob", 0.9)
                        and m > 0 and stated is None and cap is None and summary.get("shared") is None)
            bias = calibration["bias"] if eligible else 1.0
            sigma = calibration["sigma"] if eligible else SIGMA
            alpha = calibration.get("a_mult", A_MULT) if eligible else A_MULT
            detail = {**summary, "p": p, "m": m, "m_raw": summary["m"],
                      "stated": stated, "stated_raw": summary["stated"], "cap": cap,
                      "cap_raw": summary["cap"],
                      "calibration_applied": bool(eligible), "bias": bias, "sigma": sigma, "a_mult": alpha,
                      "independent_recheck": tier.name == "focus",
                      "uncertainty_applied": os.environ.get("PRICING_UNCERTAINTY", "0") == "1"}
            try:
                who = {"tier": tier.name, "model": tier.model,
                       "prompt_hash": identity["prompt_hash"].get(tier.name),
                       "config_hash": identity["config_hash"].get(tier.name)}
                corrected, evidence = price_research.adjust(
                    state, items[i], detail, who, price_memory.hash_policy(policy), gid,
                    mode=os.environ.get("PRICE_LEARNING", "validated"), category_models=category_models)
                detail["price_learning"] = evidence
                if evidence["applied"]:
                    detail["m_before_price_learning"], detail["m"] = m, corrected
                    m = corrected
            except Exception as e:
                detail["price_learning"] = {"applied": False, "reason": type(e).__name__}
            part[i] = price(p, m, stated, cap, fb[i], uncertainty=summary["log_spread"],
                            bias=bias, sigma=sigma, a_mult=alpha)
            if i in recall_caps:
                part[i] = recall_mod.apply_cap(part[i], recall_caps[i], detail, recall_mode)
            details[i] = detail
        return part, details

    def publish(tier, part, details, voices, used):
        with slock:
            if closed.is_set() or time.time() >= deadline:
                return False
            out[tier.name], est[tier.name], usage[tier.name] = part, details, used
            vote_log[tier.name] = voices
            log_prices(gid, tier.name, items, part, details)
            decisions = {i: selection_decision(details[i], best["est"].get(i),
                         level=tier.level, previous_level=best["levels"][i]) for i in part}
            for i, (accepted, reason) in decisions.items():
                details[i]["selection"] = {"accepted": accepted, "reason": reason}
                log.info("[%s] %s line %s selection: %s", gid, tier.name, i, reason)
            selected = [i for i in part if decisions[i][0]]
            # A shared-cap group is an atomic decision: never mix two allocations.
            selected = atomic_selection(selected, best["est"], details)
            for i in part:
                if decisions[i][0] and i not in selected:
                    details[i]["selection"] = {"accepted": False, "reason": "shared_group_retained"}
            if not selected:
                log.info("[%s] %s kept for evaluation; current estimates retained", gid, tier.name)
                return False
            merged = {**best["ab"], **{i: part[i] for i in selected}}
            if not submit(gid, merged, deadline, dry, tier.name):
                for i in selected:
                    details[i]["selection"]["submitted"] = False
                return False
            for i in selected:
                details[i]["selection"]["submitted"] = True
            confirmed.append(tier.name)
            best["ab"], best["tier"] = merged, tier.name
            for i in selected:
                best["est"][i], best["sources"][i], best["levels"][i] = details[i], tier.name, tier.level
            return True

    def run_tier(tier, msgs):
        stage = tier.name
        try:
            budget = min(tier.budget - (time.time() - t_key), deadline - time.time() - 3)
            if budget <= 0:
                return
            voices, used = assess(msgs, budget, tier)
            part, details = candidate(tier, voices, items)
            publish(tier, part, details, voices, used)
            log.info("[%s] %s (%s): %d/%d lines, %d/%d votes, %.1fs after key, tokens %s", gid,
                     tier.name, tier.model, len(part), len(items), len(voices), tier.votes, time.time() - t_key, used)
            # A bounded second look uses remaining time only after a complete
            # submission exists; no deadline extension and no extra submitter.
            if tier.name != "deep" or os.environ.get("FOCUS_ENABLED", "1") == "0" or closed.is_set():
                return
            budget = min(float(os.environ.get("FOCUS_BUDGET", "12")), deadline - time.time() - 5)
            if budget < 8:
                return
            from claim_to_fame import assessment
            with slock:
                comparisons = [{i: dict(e) for i, e in entries.items()} for entries in est.values()]
            focus_estimates = {i: dict(e) for i, e in details.items()}
            for i, e in focus_estimates.items():
                differences = [assessment.disagreement(e, entries.get(i)) for entries in comparisons]
                e["p"] = max([e.get("p", 0)] + [entries[i].get("p", 0) for entries in comparisons if i in entries])
                e["cross_stage_log_range"] = max((math.log(r) for r, _ in differences), default=0)
                e["disputed"] = e.get("disputed", False) or any(r > assessment.MAX_UNREVIEWED_RATIO or p > .2 for r, p in differences)
            focused_items = pricing.select_focus_items(
                {i: items[i] for i in details if details[i].get("shared") is None}, focus_estimates,
                limit=int(os.environ.get("FOCUS_MAX_LINES", "2")))
            # Reassess shared groups only with full-case calls, never partial caps.
            if not focused_items:
                return
            focused = Tier("focus", tier.level + 0.5, tier.model, tier.extra.get("reasoning_effort"), 2, budget)
            focused.n_ok = tier.n_ok                    # same model: never re-probe a refused n>1
            stage = focused.name
            fm = build_focus_prompt(policy, desc, focused_items, focus_estimates, all_items=items)
            fv, fu = assess(fm, budget, focused)
            fp, fe = candidate(focused, fv, focused_items)
            publish(focused, fp, fe, fv, fu)
        except NoKeys:
            log.error("[%s] %s: all OpenAI keys exhausted", gid, stage)
        except AssessmentError as exc:
            details = {**exc.details, "budget_seconds": round(budget, 2), "elapsed_seconds": round(time.time() - t_key, 2)}
            with slock:
                llm_failures[stage] = details
            log.error("[%s] %s failed: %s", gid, stage, details)
        except Exception as e:
            log.error("[%s] %s error %s", gid, stage, type(e).__name__)

    if use_llm and POOL.current():
        msgs = build_prompt(policy, desc, items, historical_context=history)
        threads = [threading.Thread(target=run_tier, args=(t, msgs), daemon=True) for t in TIERS]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=max(0.0, deadline - time.time()))
    elif use_llm:
        log.error("[%s] no usable OpenAI key: rule-based submission stands", gid)
    # Run only after production workers have finished, using leftover time. It never
    # calls publish/submit and never supplies estimates to the live selection path.
    control = {"status": "disabled"}
    if use_llm and os.environ.get("CONTROL_SHADOW", "1") == "1":
        control = {"status": "no_time_or_confirmed_estimates"}
        budget = min(8.0, deadline - time.time() - 3.0)
        if budget >= 4 and best["est"] and TIERS and not any(th.is_alive() for th in threads):
            template = min(TIERS, key=lambda t: t.level)
            tier = Tier("control", 0, template.model, template.extra.get("reasoning_effort"), 1, budget)
            control_started = time.monotonic()
            try:
                control = assess_control(policy, desc, items, best["est"], assess=assess, tier=tier,
                                         budget=budget, a_mult=A_MULT, sigma=SIGMA, disagree=DISAGREE)
            except AssessmentError as exc:
                control = {"status": "failed", "failure": exc.details}
            except Exception as exc:
                control = {"status": "failed", "failure": type(exc).__name__}
            control["elapsed_seconds"] = round(time.monotonic() - control_started, 3)
            control["budget_seconds"] = budget
            if control.get("usage"):
                usage["control"] = control["usage"]
        log.info("[%s] independent control (shadow): %s", gid, control["status"])
    with slock:
        closed.set()
        final = best["ab"].copy()
        log_prices(gid, "final", items, final, best["est"])
        # Serialize the actually stored merged submission, not just the last tier.
        rec = {"game": gid, "campaign": "2026-11", "lines": len(items), "winner": best["tier"],
               "ok": prov_ok or best["tier"] != "rules", "dry": dry,
               "t_provisional": round(t_prov, 2), "t_end": round(time.time() - t_key, 2),
               "usage": dict(usage), "llm_failures": llm_failures.copy(), "models": {t.name: t.model for t in TIERS},
               "items": items, "tiers": dict(out), "est": dict(est), "votes": dict(vote_log),
               "final_ab": final, "line_sources": best["sources"].copy(),
               "a_mult": A_MULT, "sigma": SIGMA, "settings": SETTINGS_SOURCE, "commit": GIT_COMMIT,
               "confirmed": list(confirmed), "control_shadow": control, **identity,
               "policy_hash": hashlib.sha256(policy.encode()).hexdigest(),
               "recall": {"mode": recall_mode, "lines": recall_caps},
               "learning_version": state.get("schema_version") if state else None,
               "learning_max_game": state.get("max_game") if state else None,
               "learning_created_at": state.get("created_at") if state else None}
        if "focus" in est:
            rec["models"]["focus"] = next((t.model for t in TIERS if t.name == "deep"), "")
        try:
            rec["shadow"] = shadow_variants(items, final, best["est"])
        except Exception as e:                      # logging only, never blocks the record
            log.warning("[%s] shadow variants failed: %s", gid, type(e).__name__)
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


def make_learning_service(dry):
    """Only the live scheduled runner starts background public-result collection."""
    if dry or os.environ.get("LEARNING_AUTO", "1") == "0":
        return None
    from claim_to_fame.learning_service import LearningService
    output = Path(os.environ.get("LEARNING_FILE", str(OUT / "learning.json")))
    return LearningService(
        team=os.environ.get("TEAM_NAME", "chineseModels"), rounds_path=OUT / "rounds.jsonl",
        cache_dir=OUT / "leaderboard", output_path=output,
        report_path=output.with_name("learning-report.md"))


def run_forever(dry, use_llm):
    learning = make_learning_service(dry)
    log.info("automatic learning %s", "enabled between rounds" if learning else "disabled")
    try:
        return run_schedule(dry, use_llm, learning)
    finally:
        if learning is not None:
            learning.close()


def run_schedule(dry, use_llm, learning=None):
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
        if learning is not None:
            try:
                learning.tick(games, now=now)
            except Exception as exc:
                log.warning("learning refresh deferred: %s", type(exc).__name__)
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
        refresh_settings()
        time.sleep(max(0.0, start - time.time() - 0.2))
        try:
            play_game(gid, start, dry, use_llm)
        except Exception as e:
            log.exception("[%s] round failed: %s", gid, type(e).__name__)
        done.add(gid)


def main():
    global POOL, TIERS, A_MULT, SIGMA, DISAGREE, GIT_COMMIT
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int, help="play this game now (0 = test game)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry", action="store_true", help="do not send submissions")
    ap.add_argument("--no-llm", action="store_true", help="rules only")
    args = ap.parse_args()
    load_env()
    configure_pricing()
    TIERS = build_tiers()
    POOL = KeyPool()
    setup_logging()
    GIT_COMMIT = git_commit()
    log.info("code %s", GIT_COMMIT)
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
