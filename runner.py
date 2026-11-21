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
import datetime as dt
import fcntl
import json
import logging
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

from claim_to_fame import pricing
from claim_to_fame.invoices import (
    FALLBACK_LINE, FALLBACK_MAX, LINE_END_RE, LINE_RE, STOP_RE, UNIT_RATE, UNITS,
    header_map, norm_unit, num, parse_items, parse_lines, rule_value,
)
from claim_to_fame.pricing import apply_shared_caps, ceil_c, combine_votes, floor_c, pick_escalation, pos
from claim_to_fame.prompts import (
    AMOUNT_RE, CLAUSE_RE, ESC_PROMPT, SCHEMA, SYSTEM, build_escalation, build_prompt, policy_amounts,
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
    return tiers


TIERS = []
ESC = None                  # escalation tier: second look at the largest lines after deep


def build_esc_tier():
    """Runs after deep in the same round, only while ESC_MIN_SECONDS remain before the deadline.
    Off by default (ESC_LINES=0): paired backtest on games 1-8 showed no gain (notes/DECISIONS.md).
    Model defaults to the deep model (known latency)."""
    env = os.environ.get
    if int(env("ESC_LINES", "0")) <= 0:
        return None
    t = Tier("esc", 3, env("ESC_MODEL", env("DEEP_MODEL", "gpt-6.2-astra")),
             env("ESC_REASONING", env("DEEP_REASONING", "high")), int(env("ESC_VOTES", "3")), 0.0)
    t.lines, t.min_left = int(env("ESC_LINES")), float(env("ESC_MIN_SECONDS", "18"))
    # deep + esc votes pooled: lean-high over 6 votes would ignore every lower second look
    t.disagree = float(env("ESC_DISAGREE", "99"))
    return t


def llm_call(messages, n, tier, timeout=20.0):
    import openai
    while True:
        name = POOL.current()
        if not name:
            raise NoKeys()
        try:
            return POOL.client(name).chat.completions.create(
                model=tier.model, messages=messages, **({"n": n} if n > 1 else {}), **tier.extra, timeout=max(3.0, timeout),
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
            fut = ex.submit(llm_call, messages, tier.votes, tier, timeout)
            done, _ = wait([fut], timeout=timeout)
            if fut in done and fut.exception() is None:
                return add(fut.result()), usage
            if fut in done and isinstance(fut.exception(), NoKeys):
                raise NoKeys()
            if fut not in done or tier.n_ok:       # timeout or other error: give up
                raise RuntimeError(f"LLM failed: {type(fut.exception()).__name__ if fut in done else 'timeout'}")
        futs = [ex.submit(llm_call, messages, 1, tier, timeout) for _ in range(tier.votes)]
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
# Keep the runner's public helpers and mutable settings compatible with offline tools.
# Domain functions take settings explicitly, so they can also be used without a runner.
A_MULT = float(os.environ.get("A_MULT", "0.85"))
SIGMA = float(os.environ.get("SIGMA", "0.35"))


def aggregate(votes):
    return pricing.aggregate(votes, disagree=DISAGREE)


def b_mult(p):
    return pricing.b_mult(p, sigma=SIGMA)


def price(p, m, stated, cap, fallback):
    return pricing.price(p, m, stated, cap, fallback, a_mult=A_MULT, sigma=SIGMA)


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
    out, usage, slock, est = {"rules": rules}, {}, threading.Lock(), {}

    def price_votes(name, by_line, disagree=None):
        """{i: [votes]} -> {i: (a, b)}; records est and logs one line per item."""
        dis = DISAGREE if disagree is None else disagree
        raw = {i: pricing.aggregate(vs, disagree=dis) for i, vs in by_line.items()}
        part = {}
        for i, agg in apply_shared_caps(raw).items():
            vs = by_line[i]
            part[i] = price(*agg, fb[i])
            clause = next((v.get("clause") for v in vs if v.get("clause")), "") or ""
            est.setdefault(name, {})[i] = {
                "p": round(agg[0], 3), "m": round(agg[1], 2), "m_raw": round(raw[i][1], 2), "stated": agg[2],
                "cap": agg[3], "shared": raw[i][4], "a": part[i][0], "b": part[i][1],
                "votes": [[round(float(v.get("covered_prob") or 0), 2), pos(v.get("t_if_covered"))] for v in vs],
                "clause": clause[:300]}
            log.info("[%s] %s #%s p %.2f m %.0f%s votes %s -> a %.2f b %.2f | %s", gid, name, i, agg[0], agg[1],
                     "" if abs(agg[1] - raw[i][1]) < 0.01 else f" (raw {raw[i][1]:.0f})",
                     "/".join(f"{x or 0:.0f}" for _, x in est[name][i]["votes"]), *part[i], clause[:120])
        return raw, part

    def send(tier, part):
        out[tier.name] = part
        with slock:                                 # ordered: a slower lower tier never overwrites
            if tier.level < best["level"]:
                log.info("[%s] %s done after %s: not sent", gid, tier.name, best["tier"])
                return False
            merged = {**best["ab"], **part}             # always a complete submission
            if submit(gid, merged, deadline, dry, tier.name):
                best.update(level=tier.level, ab=merged, tier=tier.name)
                return True
        return False

    def run_escalation(voices, raw, msgs):
        tier = ESC
        left = deadline - time.time() - 3
        picked = pick_escalation(raw, tier.lines)
        if not picked or left < tier.min_left:
            log.info("[%s] esc skipped: %s, %.1fs left", gid, "no covered line" if not picked else "too late", left)
            return
        deep = next((t for t in TIERS if t.name == "deep"), None)
        if deep is not None and deep.model == tier.model:
            tier.n_ok = deep.n_ok                   # learnt n support carries over
        evoices, usage["esc"] = assess(build_escalation(msgs, picked), left, tier)
        _, part = price_votes("esc", combine_votes(items, voices, evoices, picked), tier.disagree)
        sent = send(tier, part)
        log.info("[%s] esc (%s) lines %s: %d extra votes, %.1fs after key, %s, tokens %s", gid, tier.model,
                 picked, len(evoices), time.time() - t_key, "sent" if sent else "NOT sent", usage["esc"])

    def run_tier(tier, msgs):
        try:
            budget = min(tier.budget - (time.time() - t_key), deadline - time.time() - 3)
            voices, usage[tier.name] = assess(msgs, budget, tier)
            raw, part = price_votes(tier.name, combine_votes(items, voices, [], ()))
            send(tier, part)
            log.info("[%s] %s (%s): %d/%d lines, %.1fs after key, tokens %s", gid, tier.name, tier.model,
                     len(part), len(items), time.time() - t_key, usage[tier.name])
            if tier.name == "deep" and ESC is not None:
                run_escalation(voices, raw, msgs)
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
           "models": {t.name: t.model for t in TIERS + ([ESC] if ESC else [])}, "items": items, "tiers": out,
           "est": est, "a_mult": A_MULT, "sigma": SIGMA}
    jlog("rounds.jsonl", rec)
    log.info("[%s] DONE winner %s, sum a %.0f", gid, best["tier"], sum(a for a, _ in final.values()))
    return rec


# ---------------------------------------------------------------- preflight and loop
def check():
    print("TEAM_API_KEY:", "set" if os.environ.get("TEAM_API_KEY") else "MISSING")
    print("OpenAI keys:", POOL.names or "NONE (rules only)")
    print("tiers:", [(t.name, t.model, t.extra.get("reasoning_effort"), t.votes) for t in TIERS])
    print("escalation:", (ESC.model, ESC.extra.get("reasoning_effort"), ESC.votes, f"top {ESC.lines} lines",
                          f"if >= {ESC.min_left:.0f}s left") if ESC else "off (ESC_LINES=0)")
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
    global POOL, TIERS, ESC, A_MULT, SIGMA, DISAGREE
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int, help="play this game now (0 = test game)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry", action="store_true", help="do not send submissions")
    ap.add_argument("--no-llm", action="store_true", help="rules only")
    args = ap.parse_args()
    load_env()
    A_MULT, SIGMA = float(os.environ.get("A_MULT", A_MULT)), float(os.environ.get("SIGMA", SIGMA))
    DISAGREE = float(os.environ.get("DISAGREE", DISAGREE))
    TIERS = build_tiers()
    ESC = build_esc_tier()
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
