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

## Browser dashboard

```bash
uv run python -m dashboard.server
```

Open [the local dashboard](http://127.0.0.1:8765/). The dashboard checks for completed games once a minute and uses already downloaded rounds from `out/leaderboard/`. It caches newly fetched public transactions in `out/dashboard/public/`. It runs independently of the submitting runner and can also be used on a second team machine. On that machine, the public results are available and the local `out/rounds.jsonl` details appear only if the team has the relevant log there.

Select your team, a round and an invoice position. The transaction list shows both sides of each matchup, payment, acceptance outcome and price bounds. The threshold `t` is shown as a range supported by the settled payments. The lower bound is inclusive and an available upper bound is exclusive. Local historical prices without a verified final submission are labelled as such.

For a simulation, change `a`, `b`, or both for a position. The hypothetical result is an interval against the same opponents and is compared with the actual net result. Automatic suggestions appear when one of the tested historical changes improves even the lower bound. They are retrospective scenarios and the dashboard uses no team API or OpenAI keys.

Add `--offline` to use only cached data, `--port 8766` to change the local port, or `--team NAME` to use a different team for the public data fetch and local runner log. To export a JSON report for completed games after game 7, run `uv run python -m dashboard.server --report --after 7`. Add `--offline` for an export from existing cached rounds.

## Learning between rounds

The scheduled runner now starts one background learner automatically (`LEARNING_AUTO=1`, `TEAM_NAME=chineseModels`). It uses only public leaderboard GETs and local round logs, with no model calls. It pauses before the next game window, retries delayed publication, and resumes partially downloaded pages. Dry runs, one-off games and `--check` do not start the learner. **Only Philip's Mac runs the submitting runner.**

Completed settlements are checked for full pagination and ordered team pairs, then compared with public per-game matrix results wherever those cells are available (two-cent tolerance). Mismatches are quarantined; unavailable matrix cells are explicitly marked. The learner atomically publishes `out/learning.json`, `out/learning-report.md` and refresh diagnostics in `out/learning-status.json`. The runner reads the artifact at the next game. Network failures or an incomplete rebuild retain the prior artifact.

Inspect active/shadow parameters and why promotion is blocked, without network or LLM requests:

```bash
uv run learn.py --status
```

The optional watcher still supplies alerts (`uv run watch.py --team chineseModels`). Its `--learn` flag rebuilds from cached data under the same process lock; it is no longer required for automatic learning. A watcher on another machine needs Philip's round logs to learn from our estimates.

Rebuild from cached data without network or LLM requests:

```bash
uv run learn.py --team chineseModels
```

`--refresh` explicitly fetches missing completed public games represented in the local logs, using the same bounded refresh and cache locks. `--before-game N` excludes game N and later data. The report separates reviewer overpayment, wrongful-rejection penalties and bounded issuer opportunity losses, grouped by round and actual source tier. It also shows opponents' acceptance of known-unfair positive claims and rejection of known-fair claims; unknown fairness remains separate.

Calibration fits price bias and an initial spread from censored earlier-round intervals, then chooses `A_MULT` (0.50–1.05) and `SIGMA` (0.30–1.40, plus the fitted spread) against the field using only training-round conservative net. It stays in shadow until there are at least six games, sixteen confirmed-covered training lines and eight later validation lines. Validation includes all otherwise eligible high-coverage predictions, including coverage mistakes. A frozen candidate must improve the held-out conservative net lower bound by at least 5% over actual baseline net. Replacing an active calibration also requires a 5% improvement over that incumbent's upper bound on the same holdout. Rebuilding unchanged evidence does not renew the validation date.

Validation and live application require matching model/prompt/estimator identities. When a target cohort lacks training observations, older estimates with missing version identifiers may supply a historical training pool: their recorded predictions and pricing parameters must reproduce verified submissions, and every borrowed game must precede the first target-cohort game. Versions are never invented. Historical rows cannot serve as holdout; the frozen proposal still needs eight eligible positions across at least two later games of the target estimator, sixteen covered training positions and six games overall. Training may therefore start immediately from old logs while activation waits for enough current validation evidence.

Changing the price defaults, log messages, or an unrelated model tier no longer resets another tier's history. Calibrated observations may be reused only when the recorded raw estimate and applied parameters reproduce the actual submission; bias is never compounded. Capped, stated-amount and experimental uncertainty-adjusted lines remain outside parameter fitting/application. Stale, malformed, future-game or unreconciled data are ignored. `learn.py --status` now shows historical training counts, actual train/holdout game IDs and exclusion reasons, alongside proposed/active parameters. Artifact schema 3 is rebuilt automatically from the existing logs and caches.

Recall supplies whole-line fair-price **ranges** only for similar descriptions with the same policy, quantity and unit. It is not a market unit-rate lookup and never copies old coverage into a new case. No August records are used. Old successful logs may recover a complete explicitly recorded winning tier, subject to settlement reconciliation. Partial or ambiguous submissions stay excluded. Missing model/prompt identities remain visible in the historical training provenance. Public payments alone are never converted into invented internal model estimates.

Michael's per-game `.env` refresh and `view_rounds.py` are retained. Manual changes to `A_MULT`, `SIGMA`, `DISAGREE` and `AUTO_TUNE` take effect before the next scheduled round without restarting. `AUTO_TUNE=0` pins manual price defaults; `LEARNING_AUTO=0` independently disables the background downloader. The older `tune.py` remains an exploratory analysis tool: it imputes unknown prices and selects and compares settings on the same data. Its `out/params.json` proposals no longer bypass validation or compete with `learning.json` for live prices. The wider field-search ranges are integrated into the single validated learner above. Its replay now scores every setting with `calibrate.score_submission` bounds (unknown limits, fairness and the payment cap stay ranges) and checks the step it would propose itself: midpoint gain, worst case not lower, 2 of 3 periods, still a gain without the best game.

**Shadow variants (logged, never sent).** Every round record carries `commit`, `confirmed` (stages the server echoed) and `shadow`: the final submission with `a` × 0.80 on all LLM lines (`a80_all`), `a` × 0.80 only on quantity-1 lines without a document amount (`a80_uncertain`) and `b ≤ 0.80 a` (`b_le_80pct_a`). Defined before game 15 from the external review; `uv run tune.py --shadow` scores them against what was actually sent once those games are settled. Decide after 3–5 games, and once without the best game.

New raw votes, actual submitted prices, per-line sources and model/prompt/configuration/learning provenance remain in ignored `out/`. Implementation tests do not establish a live profit improvement. Current shared-cap schema identifies groups by amount; distinct clauses with equal numeric limits remain a known limitation.

## Applying this update

Pull the update and restart **only Philip's runner**, between game windows. API-key code is unchanged. Keep the previous checkout available for rollback. Learning starts automatically; `LEARNING_AUTO=0` disables background refresh. `FOCUS_ENABLED=0` disables the optional follow-up call; keep `PRICING_UNCERTAINTY=0` until a chronological comparison supports enabling it. This update never overwrites `.env`.

The briefing audit, implemented gaps and remaining experiments are documented in [`notes/BRIEFING_ALIGNMENT.md`](notes/BRIEFING_ALIGNMENT.md). The internal preparation briefing contains August rules; official November rules take precedence.

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
| `claim_to_fame/learning_service.py`, `legacy.py` | Bounded background refresh and strict recovery of old complete submission logs. |
| `claim_to_fame/tuning.py`, `tune.py` | Exploratory historical field replay; proposals are not applied by the runner. |
| `tests/` | Offline domain, round orchestration and historical-import regression tests. |
| `test_runner.py` | Runs the entire offline suite using Python's standard-library `unittest`. |

Run `uv run python test_runner.py` after changes. The optional real-PDF test is skipped when `out/cases/case_000/invoices.pdf` is absent; other tests use synthetic data and fake API/LLM responses. No new dependencies are required.

## Integrating a teammate's next push

Commit the local work first, then run `git fetch origin` and inspect `git log --oneline HEAD..origin/main` and `git diff HEAD...origin/main`. Merge the remote branch with `git merge origin/main`, resolve any conflicts, and rerun `uv run python test_runner.py`. Changes to previously extracted blocks in `runner.py` belong in the corresponding module above. Review the resulting diff before pushing. Update `AGENTS.md` with the integration status.

For a one-step pull with the same merge strategy, use `git pull --no-rebase`. This keeps local and remote commits and avoids the divergent-branches preference prompt.
