# Claim to Fame runner (team chineseModels)

QuantCo "Claim to Fame", START Hack Munich 2026. Shared context for people and agents: [AGENTS.md](AGENTS.md); decisions with evidence: [notes/DECISIONS.md](notes/DECISIONS.md).

> **Only ONE machine plays (currently Philip's Mac).** Never start `runner.py` anywhere else, not even `--game`: a second runner overwrites our submissions.

## Setup (once, macOS)

```bash
brew install uv sevenzip
git clone https://github.com/MichaelPrutkow/START-Hack-Munich-2026.git
cd START-Hack-Munich-2026
uv sync
cp .env.example .env
mkdir -p cases
```

Then fill in `.env` (never commit or paste it): `TEAM_API_KEY=...`, `OPENAI_KEY_1=...` (more keys as `_2`, `_3`), no quotes. Copy all case zips from the Drive into `cases/` (any name, the game number must be in it, e.g. `case_007.zip`). Without 7-Zip the runner falls back to pyzipper.

## Play

```bash
source .venv/bin/activate && python3 runner.py --check && caffeinate -i python3 runner.py
```

`--check` shows keys, models, the next game and missing zips. Games start every 800 s from 14:00:00 (14:13:20, 14:26:40, 14:40:00, ...), each with a 60 s window. Restart only between two windows. Logs: `out/runner.log`, submissions `out/submissions.jsonl`, rounds incl. estimates and tokens `out/rounds.jsonl`.

## Each round

Key at T+0 → decrypt (7z/7zz, else pyzipper) → parse the invoice PDF (all pages, wrapped lines) → provisional rule-based submission → fast and deep LLM tiers in parallel (structured output: per line coverage probability p and fair total m) → final submission with echo check. Pricing per line: `a = A_MULT × m`; `b` such that `p · P(t ≥ b) = 2/3` (log-normal spread `SIGMA`), `b = 0` if `p ≤ 2/3`. OpenAI key cascade: 401/insufficient_quota disables a key, 429 only for the round. Without LLM the rule submission stands, never 0/0.

## Analyse and test (read-only, safe on any machine)

```bash
uv run calibrate.py --team chineseModels
```
```bash
uv run python test_runner.py
```
```bash
uv run watch.py --team chineseModels
```
```bash
uv run eval_cases.py --games 1 2 3 4 5 --tiers deep fast --deep-votes 3
```

`watch.py` reports every finished game from the leaderboard (net, rank, our a against the t interval, no LLM) and raises a macOS alarm if we apparently did not submit.

`eval_cases.py` replays the LLM step (Git `HEAD` vs current by default; select a baseline with `--old-ref <commit>`) on finished games and scores it against the leaderboard brackets; it submits nothing, waits outside game windows, but spends OpenAI tokens. Historical code is loaded with the domain modules from the same commit, so both single-file and modular revisions work. `calibrate.py` reads the public leaderboard once per finished game (1 request/s, cached in `out/leaderboard/`; organizers allowed scraping but not hammering), derives the t interval per line, and with `out/rounds.jsonl` fits t/m with a censored MLE (one-sided brackets included; `--tier fast|deep` picks whose estimate) and backtests `A_MULT` × `SIGMA`.

## Code layout

| Path | Responsibility |
| --- | --- |
| `runner.py` | CLI, environment, logging, game API, decryption, OpenAI keys, concurrent tiers and round scheduling. Existing imports such as `runner.price` remain available. |
| `claim_to_fame/invoices.py` | Invoice table/text parsing, wrapped descriptions, units and rule-based fallback values. |
| `claim_to_fame/prompts.py` | System prompt, structured response schema and policy amount extraction. Edit prompt wording here. |
| `claim_to_fame/pricing.py` | Vote aggregation, pro-rata shared caps, rounding and the a/b rule. Parameters are explicit; the runner forwards its current settings. |
| `calibrate.py` | Read-only leaderboard analysis and calibration. |
| `watch.py` | Read-only per-game reports and missing-submission alarms. |
| `eval_cases.py` | Historical/current LLM comparison on finished cases. |
| `tests/` | Offline domain, round orchestration and historical-import regression tests. |
| `test_runner.py` | Runs the entire offline suite using Python's standard-library `unittest`. |

Run `uv run python test_runner.py` after changes. The optional real-PDF test is skipped when `out/cases/case_000/invoices.pdf` is absent; other tests use synthetic data and fake API/LLM responses. No new dependencies are required.

## Integrating a teammate's next push

Commit the local work first, then run `git fetch origin` and inspect `git log --oneline HEAD..origin/main` and `git diff HEAD...origin/main`. Merge the remote branch with `git merge origin/main`, resolve any conflicts, and rerun `uv run python test_runner.py`. Changes to previously extracted blocks in `runner.py` belong in the corresponding module above. Review the resulting diff before pushing. Update `AGENTS.md` with the integration status.

For a one-step pull with the same merge strategy, use `git pull --no-rebase`. This keeps local and remote commits and avoids the divergent-branches preference prompt.
