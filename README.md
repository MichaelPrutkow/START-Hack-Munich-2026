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

Key at T+0 → decrypt → parse all invoice pages → complete provisional submission → fast and deep models in parallel → complete merged submission with echo check. Every line retains its source tier. The larger model wins ties when consistency is comparable; a substantially more disputed answer does not automatically replace a consistent one. After the deep answer, at most two expensive/disputed lines receive a focused two-vote recheck if at least eight seconds remain for inference. Its budget is twelve seconds and never extends the submission deadline. Shared-cap groups are updated together.

Votes retain their median, amount spread, coverage dispersion and document-anchor disagreements. Disagreement no longer selects the two highest estimates. Baseline pricing remains `a = A_MULT × m`, with `b` at the existing `p · P(t ≥ b) = 2/3` threshold. Document amounts and individual/shared limits apply deterministically. The optional spread-based price adjustment is off until evaluated (`PRICING_UNCERTAINTY=0`). API-key handling is unchanged.

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
uv run eval_cases.py --games 1 2 3 4 5 --tiers deep fast --deep-votes 3 --old-ref 10ad63b --holdout-from 4
```

`watch.py` reports every finished game from the leaderboard (net, rank, our a against the t interval, no LLM) and raises a macOS alarm if we apparently did not submit.

`eval_cases.py` requires `--old-ref <commit>` or `--skip-old`. It scores actual returned prices, including document values and caps, and prints conservative payoff bounds. Their midpoint is not expected profit. Historical code/lazy imports are isolated and model/vote budgets match. Failed or incomplete comparisons are excluded. This single-tier replay does not simulate live timing or merged tiers. It submits nothing and waits outside game windows, but spends OpenAI tokens. Choose the holdout boundary before inspecting results; reusing already-tuned games is descriptive, not independent validation.

`calibrate.py` fetches each completed game once (1 request/s, cached in `out/leaderboard/`), derives censored fair-price intervals and supports `--holdout-from <game>` for parameter comparisons. Accepted payments are not assumed to equal original demands when a payment cap could have applied.

## Learning between rounds

On **Philip's runner machine**, open a second terminal for the watcher (it does not submit):

```bash
uv run watch.py --team chineseModels --learn
```

After reporting a completed game, it joins public settlements with local `out/rounds.jsonl`, atomically writes `out/learning.json`, and produces `out/learning-report.md`. The runner reads this artifact at the next game. A watcher on another machine can still monitor, but needs Philip's round logs to build learning.

Rebuild from cached data without network or LLM requests:

```bash
uv run learn.py --team chineseModels
```

`--refresh` explicitly fetches missing completed public games. `--before-game N` excludes game N and later data. The report separates reviewer overpayment, wrongful-rejection penalties and bounded issuer opportunity losses, grouped by round and actual source tier.

Calibration stays in shadow until there are at least six games, sixteen confirmed-covered training lines and eight later validation lines. Validation includes all otherwise eligible high-coverage predictions, including coverage mistakes. A frozen candidate must improve the held-out conservative net lower bound by at least 5% over actual baseline net. Only matching model/prompt/configuration cohorts qualify. Stale, malformed, future-game or unreconciled data are ignored. Already-calibrated and optional uncertainty-adjusted observations are excluded from the baseline fit.

### Tuning A_MULT and SIGMA against the field (`tune.py`)

The same watcher run (`--learn`) also replays our pricing rule on every logged line with other
`A_MULT`/`SIGMA` values against the other teams' real charges and limits from the public
transactions (`claim_to_fame/tuning.py`) and writes the chosen pair to `out/params.json`. The
runner re-reads `.env` and `out/params.json` before **every** game, so neither a manual `.env`
change nor a tuning step needs a restart. Guardrails: at least 8 lines; weighted gain at least
1,000 EUR (recent games weigh more, half-life 10 games); not worse in 2 of 3 chronological
periods; at most 0.10 `A_MULT` and 0.30 `SIGMA` per update; bounds 0.50–1.00 and 0.30–1.40.
The first `params.json` is written only on a real change, so until then `.env` stays in charge.
`AUTO_TUNE=0` in `.env` pins the `.env` values. A validated `feedback.py` calibration still
overrides `SIGMA` on its eligible lines. Report without writing: `uv run tune.py`; on a machine
without runner logs: `uv run tune.py --from-leaderboard 0.85`. History: `out/tune.jsonl`.
Why: notes/TRANSAKTIONEN_G1-9.md (games 7–9 lost mainly on the reviewer side; SIGMA 0.35 far
too narrow).

Recall supplies whole-line fair-price **ranges** only for similar descriptions with the same policy, quantity and unit. It is not a market unit-rate lookup and never copies old coverage into a new case. No August records are used. Legacy logs without `final_ab` and `line_sources` are excluded rather than guessing a merged submission.

New raw votes, actual submitted prices, per-line sources and model/prompt/configuration/learning provenance remain in ignored `out/`. Implementation tests do not establish a live profit improvement. Current shared-cap schema identifies groups by amount; distinct clauses with equal numeric limits remain a known limitation.

## Applying this update

Run the offline suite, merge this branch with Michael's current changes, and restart **only Philip's runner**, between game windows. API-key code is unchanged. Keep the previous checkout available for rollback, and start the learning watcher on Philip's machine as above. `FOCUS_ENABLED=0` disables the optional follow-up call; keep `PRICING_UNCERTAINTY=0` until a chronological comparison supports enabling it. This update never overwrites `.env`.

The one-page submission text and three-minute pitch with Q&A are in [`writeup/ONE_PAGE.md`](writeup/ONE_PAGE.md) and [`writeup/PITCH.md`](writeup/PITCH.md). Their game-8 numbers are a dated snapshot, not final standings.

## Code layout

| Path | Responsibility |
| --- | --- |
| `runner.py` | CLI, environment, logging, game API, decryption, OpenAI keys, concurrent tiers and round scheduling. Existing imports such as `runner.price` remain available. |
| `claim_to_fame/invoices.py` | Invoice table/text parsing, wrapped descriptions, units and rule-based fallback values. |
| `claim_to_fame/prompts.py` | System prompt, structured response schema and policy amount extraction. Edit prompt wording here. |
| `claim_to_fame/pricing.py` | Vote aggregation, pro-rata shared caps, rounding and the a/b rule. Parameters are explicit; the runner forwards its current settings. |
| `calibrate.py` | Read-only leaderboard analysis and calibration. |
| `watch.py` | Read-only per-game reports and missing-submission alarms. |
| `view_rounds.py` | Read-only view of `out/rounds.jsonl`: per line and tier the votes, p, m, a, b, clause, line source and (if cached) the true t interval. |
| `eval_cases.py` | Historical/current LLM comparison on finished cases. |
| `claim_to_fame/feedback.py`, `learn.py` | Settlement reconciliation, loss bounds, calibration, contextual recall and private artifacts. |
| `claim_to_fame/tuning.py`, `tune.py` | A_MULT/SIGMA replay against the field's real a and b, guarded update of `out/params.json`. |
| `tests/` | Offline domain, round orchestration and historical-import regression tests. |
| `test_runner.py` | Runs the entire offline suite using Python's standard-library `unittest`. |

Run `uv run python test_runner.py` after changes. The optional real-PDF test is skipped when `out/cases/case_000/invoices.pdf` is absent; other tests use synthetic data and fake API/LLM responses. No new dependencies are required.

## Integrating a teammate's next push

Commit the local work first, then run `git fetch origin` and inspect `git log --oneline HEAD..origin/main` and `git diff HEAD...origin/main`. Merge the remote branch with `git merge origin/main`, resolve any conflicts, and rerun `uv run python test_runner.py`. Changes to previously extracted blocks in `runner.py` belong in the corresponding module above. Review the resulting diff before pushing. Update `AGENTS.md` with the integration status.

For a one-step pull with the same merge strategy, use `git pull --no-rebase`. This keeps local and remote commits and avoids the divergent-branches preference prompt.
