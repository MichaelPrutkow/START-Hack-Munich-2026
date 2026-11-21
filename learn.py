#!/usr/bin/env python3
"""Build private learning and loss reports; cached/offline unless --refresh is explicit."""
import argparse
from pathlib import Path

from claim_to_fame.feedback import build_state, write_state


ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", default="chineseModels")
    parser.add_argument("--rounds", type=Path, default=ROOT / "out" / "rounds.jsonl")
    parser.add_argument("--cache", type=Path, default=ROOT / "out" / "leaderboard")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "learning.json")
    parser.add_argument("--report", type=Path, default=ROOT / "out" / "learning-report.md")
    parser.add_argument("--before-game", type=int, help="exclude this game and all later games")
    parser.add_argument("--refresh", action="store_true", help="explicitly fetch completed public games through calibrate (no LLM)")
    args = parser.parse_args(argv)
    if args.refresh:
        import calibrate as C
        C.CACHE = args.cache
        games = C.get("games", completed_only="true", page_size=1000)["items"]
        for game in sorted(games, key=lambda g: g["id"]):
            gid = game["id"]
            if gid > 0 and (args.before_game is None or gid < args.before_game):
                C.load_game(gid, args.team)
    state = build_state(args.rounds, args.cache, args.team, before_game=args.before_game)
    write_state(state, args.output, report_path=args.report)
    validated = sum(c["status"] == "validated" for c in state["calibrations"])
    print(f"Learning: {len(state['games'])} reconciled games, {state['summary']['lines']} lines, "
          f"{len(state['excluded'])} excluded; {validated} validated calibrations.")
    print(f"Private artifact: {args.output}\nLoss report: {args.report}")
    return state


if __name__ == "__main__":
    main()
