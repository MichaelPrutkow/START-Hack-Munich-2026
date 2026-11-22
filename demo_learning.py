"""Past-only evidence preparation for demo workers; never invent settlements."""
import hashlib
import json

from claim_to_fame import feedback as F, price_memory as M
from dashboard.leaderboard import atomic_json, read_json


def prepare_history(args, source, game):
    """Materialize only earlier settlements, physically excluding the target game."""
    from dashboard.server import read_round_records
    import calibrate as C
    root = args.output / "history" / f"before_{game:03d}"
    cache = root / "leaderboard"
    cache.mkdir(parents=True, exist_ok=True)
    evidence_games, memory = [], []
    missing_prior_cases = []
    fingerprint = hashlib.sha256()
    originals = read_round_records(args.out / "rounds.jsonl") if args.game else {}
    records = []
    for gid in range(1, game):
        data = source.read_game(gid)
        if data is None:
            raise ValueError(f"Missing complete settlement for earlier game {gid}")
        atomic_json(cache / f"game_{gid:03d}.json", data)
        fingerprint.update(json.dumps(data, sort_keys=True).encode())
        evidence_games.append(gid)
        directory = args.cases / f"case_{gid:03d}"
        if not directory.is_dir() or not all(list(directory.glob(pattern)) for pattern in ("policy*.txt", "description*.txt", "*.pdf")):
            missing_prior_cases.append(gid)
        if args.game:
            if gid in originals:
                records.append(originals[gid])
                fingerprint.update(json.dumps(originals[gid], sort_keys=True).encode())
            continue
        rec = read_json(args.output / f"replay_{gid:03d}.json")
        if not rec or rec.get("demo") is not True or rec.get("game") != gid:
            raise ValueError(f"Replay must complete game {gid} before game {game}")
        fingerprint.update(json.dumps(rec, sort_keys=True).encode())
        inference = C.analyse(data["tx"])
        for line, bounds in inference.items():
            key = str(line)
            item = rec.get("items", {}).get(key)
            if not item:
                continue
            tier = rec.get("line_sources", {}).get(key)
            est = rec.get("est", {}).get(tier, {}).get(key, {})
            # Unknown policy limits must not be assumed absent for fallback lines.
            if not est:
                est = {"cap": 0, "valuation_category": "unknown"}
            entry = M.make_entry(rec, line, item, est, bounds["t"], f"game_{gid:03d}.json")
            if entry:
                memory.append(entry)
    if args.game:
        if game > 1 and not (args.out / "rounds.jsonl").is_file():
            raise ValueError("Single-game reconstruction requires the original rounds.jsonl")
        log = root / "original_rounds.jsonl"
        log.write_text("".join(json.dumps(rec) + "\n" for rec in records))
        state = F.build_state(log, cache, args.team, before_game=game, case_dir=args.cases)
        missing_logs = sorted(set(evidence_games) - {r["game"] for r in records})
        label = "reconstructed_from_original_history"
    else:
        state = {"schema_version": F.SCHEMA_VERSION, "algorithm": F.ALGORITHM,
                 "campaign": F.CAMPAIGN, "max_game": game - 1, "memory": memory,
                 "calibrations": [], "price_research": {}, "games": evidence_games}
        missing_logs = []
        label = "chronological_replay_evidence"
    # Case contents affect current parsing and earlier recall. Detect changed inputs on resume.
    for gid in range(1, game + 1):
        directory = args.cases / f"case_{gid:03d}"
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                fingerprint.update(str(file.relative_to(args.cases)).encode())
                fingerprint.update(hashlib.sha256(file.read_bytes()).digest())
    meta = {"mode": label, "before_game": game, "evidence_games": evidence_games,
            "input_fingerprint": fingerprint.hexdigest(),
            "memory_entries": len(state.get("memory", [])), "missing_original_logs": missing_logs,
            "missing_prior_cases": missing_prior_cases,
            "excluded_original_records": state.get("excluded", []),
            "calibration_policy": "original_logs_only" if args.game else "no_fabricated_payoff_calibration"}
    state["demo_history"] = meta
    path = root / "learning.json"
    atomic_json(path, state)
    return path


def load_history(path, game):
    state = read_json(path)
    if not state or state.get("demo_history", {}).get("before_game") != game:
        raise ValueError("History does not match the requested game")
    if state.get("max_game", game) >= game:
        raise ValueError("History contains target or future evidence")
    if any(g >= game or g < 1 for g in state["demo_history"]["evidence_games"]):
        raise ValueError("History contains target or future settlements")
    if any(row.get("game", game) >= game for row in state.get("memory", [])):
        raise ValueError("Memory contains target or future evidence")
    return state
