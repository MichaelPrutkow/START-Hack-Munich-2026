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

`calibrate.py` reads the public leaderboard once per finished game (1 request/s, cached in `out/leaderboard/`; organizers allowed scraping but not hammering), derives the t interval per line, and with `out/rounds.jsonl` backtests `A_MULT` × `SIGMA`.
