# Demo: single-game reconstruction and chronological replay

Use a separate checkout with Python 3.11+, `uv`, the team's private data handover and a local `.env` containing the intended current model/pricing settings. The launcher uses locked dependencies. No demo mode submits competition prices or changes production logs.

## Files to get from Philip after game 100

Preserve relative paths. The simplest handover is a private copy of the entire `out/` directory plus a non-secret configuration export and the Git commit ID. The essential and useful subsets are:

| Path in Philip's checkout | Purpose | Needed? |
| --- | --- | --- |
| `out/cases/case_NNN/` for games 1–100, with **all contents** | Decrypted policy text, damage-description text, invoice PDFs and any supporting files. Keep original names/subfolders. | Required for full replay; cases through N support a single-game replay and earlier recall. |
| `out/leaderboard/game_NNN.json` | Complete settled transactions for every directed team pair and position. | Required evidence/scoring, unless the same complete data exists in the dashboard cache or is fetched online. |
| `out/dashboard/public/` including `game_NNN.json` and `index.json`, if present | Alternative complete settlement cache and completed-game index. | Copy if present; avoids repeated downloads. The two cache directories can complement each other. |
| `out/rounds.jsonl` | Original model estimates, final submitted prices, source tiers, identities and per-round settings. | Required for original-history reconstruction; strongly recommended for exact original-price comparisons in both demos. Not needed to start the chronological model replay empty. |
| `out/submissions.jsonl` | Submission stages and sent prices. | Recommended for audit/debugging; the demo does not consume it directly. |
| `out/runner.log` | Timing, failures and operational context. | Recommended for audit/debugging; not an inference input. |
| Saved per-round learning snapshots, if any; final `out/learning.json`, `out/learning-report.md`, `out/learning-status.json` | Archive of learning and diagnostics. | Optional. Current implementation reconstructs from earlier logs/settlements; it does not restore snapshots. A final state is not evidence of what was known before an earlier game. |
| Non-secret model/pricing configuration and `git rev-parse HEAD` output | Identify the production version/settings and configure the newest replay. | Recommended. Configure model API keys locally; no team submission key is needed for replay. |

Encrypted `cases/*.zip` files are optional backup and do not replace decrypted `out/cases/` files. Do not put this handover or API keys in the public Git repository. Ensure game 100 has finished settling before making the final copy. Hidden true fair prices are not required: uncertainty remains represented by intervals.

Check the handover without API calls or starting a server:

```bash
./demo.sh --check --out /absolute/path/to/philip/out
./demo.sh --check --game 42 --out /absolute/path/to/philip/out
```

The check lists missing case folders/files, incomplete settlement caches and missing successful original logs. A failed check exits nonzero. The full check covers games 1–100 by default (`--last 96` checks only through 96). Missing original logs do not prevent a full chronological replay, but can limit original-price comparisons. For single-game reconstruction, missing earlier logs make the check fail; an explicitly launched replay can still display a partial reconstruction with the gaps identified.

## Single-game demo

```bash
./demo.sh --game 42 --out /absolute/path/to/philip/out
```

Open http://127.0.0.1:8765. The newest checked-out pipeline re-assesses game 42 with reconstructed learning from **games 1–41 only**. It uses original earlier logs for validated calibration/research and earlier public settlements/cases for recall. Production compatibility gates still apply: an older model/prompt cohort may not qualify for current calibration. Missing or excluded original records remain visible in the saved history metadata.

This reconstructs history using today's learning code; it is **not** an exact restoration of an old cache snapshot. The current game's settlement is used to verify completion and, after inference, to score the result. It is not supplied to the assessment or learning state. No later rounds enter the history.

Results default to `out/demo-game-042/`. The dashboard shows original competition results alongside replay prices, prior evidence provenance and hypothetical payoff bounds against the recorded opponents.

## Full chronological replay

```bash
./demo.sh --replay --out /absolute/path/to/philip/out
```

This starts **game 1 empty**, then runs games 1–100 sequentially. Before game N, it rebuilds memory using only earlier replay case metadata and real completed settlements, and enables earlier-case recall. Original competition estimates are not used as the replay's learning history. A missing case, missing earlier settlement or failed worker stops progress rather than silently skipping the gap. While online, missing data can be picked up when it becomes available.

**Calibration boundary:** replay prices were never actually submitted. The demo therefore accumulates settlement-bound comparable memory and recall, but does not invent exact replay outcomes or activate payoff-based calibration from hypothetical profits. Those validation routines require genuine reconciled submissions. This is chronological evidence learning, not a reconstruction of the counterfactual tournament's full learning dynamics. The single-game mode can use validated calibration reconstructed from genuine earlier logs.

Every case uses the production parser, fallback, configured model tiers, focus, pricing and inference time budgets. Shadow control is disabled because it does not influence final prices. Replay records remain `dry`, never become production round logs and cannot be mistaken for confirmed submissions.

## Offline rehearsal, resume and presentation

Both `--game N` and `--replay` explicitly enable **paid model calls** unless `--rules-only` is set. `--offline` disables public-data downloads, not model calls.

```bash
# Full offline rehearsal, two cases, no model calls:
./demo.sh --replay --rules-only --offline --last 2 --output out/demo-rehearsal

# One case with reconstructed history, no model calls:
./demo.sh --game 42 --rules-only --offline --output out/demo-game-042-rehearsal

# Present previously computed full-replay results, no external calls:
./demo.sh --offline

# Present a previously computed single-game result, no external calls:
./demo.sh --offline --output out/demo-game-042
```

Pass `--out` in these commands when the handed-over data is outside the demo checkout. Use `--cases /absolute/path/to/cases` if case folders are elsewhere. New downloads, manifests, snapshots and results stay under the selected ignored output folder.

Ctrl+C stops the server and terminates the current worker. Restart the same replay command to resume. Completed records, including explicitly labelled model-failure fallback decisions, are preserved so later learning stays consistent. Failed workers without a result can be retried by restarting. To reassess an already completed game, use `--game N` with a new `--output` directory. Code, configuration and input fingerprints prevent silently mixing incompatible runs; changed inputs require a new output folder. Sequential replay starts at 1; do not use `--first` to skip earlier learning.

A complete run can take around 100 minutes at 60 seconds per case, plus learning reconstruction and downloads. Start before the pitch. Extra model calls consume quota; use an appropriate demo key or run after the competition finishes. Installing, viewing the dashboard and `--check` make no model calls.

## Pitch sequence

1. Show the completed tournament overview and actual result.
2. Choose a prepared game, explain the earlier-evidence cutoff, then show the new assessment and price changes.
3. Show bounded hypothetical impact against actual opponents; do not present a midpoint as expected profit.
4. Show the full replay's progress as the supporting experiment: empty at game 1, growing memory thereafter.

Both modes are retrospective: today's prompts were developed with tournament experience. Keep a precomputed result and recorded walkthrough for unreliable Wi-Fi. Update final standings before submission. The announced submission window on 22 November is 12:00–12:45; press **Submit project**, not only Save draft. Pitches begin at 13:15.
