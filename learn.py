#!/usr/bin/env python3
"""Build private learning and loss reports; cached/offline unless --refresh is explicit."""
import argparse
from collections import Counter
import json
from pathlib import Path

from claim_to_fame.feedback import build_state, load_state, write_state


ROOT = Path(__file__).resolve().parent


def previous_state(path, before_game=None, *, for_rebuild=False):
    try:
        value = json.loads(Path(path).read_text())
        cutoff = before_game if before_game is not None else int(value["max_game"]) + 1
        return load_state(path, cutoff, max_age_hours=float("inf") if for_rebuild else 36)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def rebuild_cached(team, rounds, cache, output, report=None, *, before_game=None):
    """Offline rebuild under the same lock as the automatic public refresher."""
    import calibrate as C
    if not Path(rounds).exists():
        return {"status": "preserved", "reason": "local rounds.jsonl is absent"}, None
    try:
        with C.cache_lock(Path(cache) / ".learning-refresh.lock"):
            previous = previous_state(output, before_game, for_rebuild=True)
            state = build_state(rounds, cache, team, before_game=before_game, previous_state=previous)
            if not state["games"] or (previous and not set(previous["games"]) <= set(state["games"])):
                return {"status": "preserved", "reason": "no complete data or fewer reconciled games"}, previous
            write_state(state, output, report_path=report)
            return {"status": "updated"}, state
    except C.RefreshBusy:
        return {"status": "busy", "reason": "another process is refreshing learning"}, None


def show_state(state):
    if state is None:
        print("No current usable learning artifact; runner keeps baseline parameters.")
        return
    validated = sum(c["status"] == "validated" for c in state["calibrations"])
    print(f"Learning: {len(state['games'])} reconciled games, {state['summary']['lines']} lines, "
          f"{len(state['excluded'])} excluded; {validated} validated calibrations.")
    print("Training data:", state.get("learning_counts", {}))
    print("Training exclusions:", dict(Counter(x["reason"] for x in state.get("training_exclusions", []))))
    from claim_to_fame.decision_review import summarize
    print("Independent control:", dict(Counter(g.get("control_status", "not_recorded")
                                              for g in state.get("reports", []))))
    for name, result in summarize(state.get("reports", [])).items():
        print(f"  {name}: {len(result['games'])} games, {result['lines']} lines; "
              f"delta vs actual EUR {result['lower']:.2f}..{result['upper']:.2f}; "
              f"without best game lower {result['without_best_game_lower']:.2f}; evaluation only")
    for candidate in state["calibrations"]:
        params = {key: candidate.get(key) for key in ("a_mult", "bias", "sigma")}
        print(f"  {candidate['tier']} ({candidate.get('model')}): {candidate['status']}; "
              f"{candidate['reason']}; parameters={params}; "
              f"fit={candidate['fit_count']}, holdout={candidate['holdout_count']}; "
              f"historical_train={candidate['historical_train_count']}; "
              f"train_games={candidate['train_games']}, holdout_games={candidate['holdout_games']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", default="chineseModels")
    parser.add_argument("--rounds", type=Path, default=ROOT / "out" / "rounds.jsonl")
    parser.add_argument("--cache", type=Path, default=ROOT / "out" / "leaderboard")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "learning.json")
    parser.add_argument("--report", type=Path, default=ROOT / "out" / "learning-report.md")
    parser.add_argument("--before-game", type=int, help="exclude this game and all later games")
    parser.add_argument("--refresh", action="store_true", help="explicitly fetch completed public games through calibrate (no LLM)")
    parser.add_argument("--status", action="store_true", help="show current learning state without rebuilding or network requests")
    args = parser.parse_args(argv)
    if args.status:
        state = previous_state(args.output, args.before_game)
        diagnostic = args.output.with_name("learning-status.json")
        if diagnostic.exists():
            try:
                print("Last refresh:", json.dumps(json.loads(diagnostic.read_text()), ensure_ascii=False))
            except (OSError, ValueError):
                print("Last refresh status unavailable.")
    elif args.refresh:
        from claim_to_fame.learning_service import refresh_once
        result = refresh_once(args.team, args.rounds, args.cache, args.output, args.report,
                              before_game=args.before_game)
        print(f"Refresh: {result['status']}; {result.get('reason', 'completed')}")
        state = previous_state(args.output, args.before_game)
    else:
        result, state = rebuild_cached(args.team, args.rounds, args.cache, args.output, args.report,
                                       before_game=args.before_game)
        print(f"Rebuild: {result['status']}; {result.get('reason', 'completed')}")
    show_state(state)
    print(f"Private artifact: {args.output}\nLoss report: {args.report}")
    return state


if __name__ == "__main__":
    main()
