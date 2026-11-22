# chineseModels — QuantCo Claim to Fame

*START Hack Munich · 21 November 2026 · Method and interim results*

We built an insurance assessment system that turns policy interpretation into explicit financial decisions within a 60-second submission window. Each invoice line needs both a charge and an acceptance limit. Rejecting a fair charge costs 150% of that charge; accepting an unfair one can also lose money. Our approach separates coverage, price estimation, and the decision rule so that failures can be diagnosed independently.

One designated machine submits a complete rule-based fallback immediately after parsing the case. Fast and deeper model assessments run within fixed budgets, returning structured estimates and relevant policy clauses. Updates retain fallback values for missing lines, respect the deadline, and preserve complete allocations for shared policy limits. Optional focused review targets consequential disagreements when time remains. Submission logs record actual final prices, each line’s contributing tier, model votes, and prompt, configuration, and policy identifiers.

The models estimate coverage probability separately from the plausible gross total if covered. Deterministic code applies stated amounts, individual caps, and shared limits. Our baseline reviewer rule uses the two-thirds fairness threshold implied by the uncapped payoff: accepting costs the charge, while rejecting costs 1.5 times that charge if fair. Ensemble disagreement is retained rather than hidden by aggregation. An optional uncertainty adjustment is implemented but disabled by default pending evidence.

Evaluation reconstructs the prices actually submitted. Public transactions constrain the hidden fair amount; they do not generally reveal its exact value. Accepted payments may be capped, while positive rejection penalties can establish exact charges. Our corrected evaluator therefore reports conservative payoff bounds and keeps unresolved outcomes unresolved. Historical replay isolates code revisions and records model and vote settings. Replay results cannot establish live timing performance or gains from merged model tiers.

Between rounds, a background learner joins completed settlements with verified submissions and available public per-game net results. It reports reviewer overpayment, rejection penalties, and bounded issuer opportunity losses. Recall requires matching policy, quantity, unit, and description; it preserves price intervals and keeps coverage separate. Price bias, spread and charge multiplier are fitted on earlier rounds, then frozen for later validation, including eligible coverage mistakes. Activation requires six games, sixteen confirmed-covered training lines, eight held-out lines, matching estimator identifiers, and a conservative held-out improvement of at least 5%. Replacements must also outperform the active calibration.

The interim snapshot after game 8 on 21 November places chineseModels third with cumulative net €12,087.65. This is not the final ranking. The new evaluation and learning safeguards have synthetic correctness tests; qualifying production validation is still pending, so we claim no demonstrated profit uplift from these additions.

*Implementation evidence: [runner](../runner.py), [pricing](../claim_to_fame/pricing.py), [evaluation](../calibrate.py), [replay](../eval_cases.py), [learning](../claim_to_fame/feedback.py), [offline tests](../tests/).*
# Demo access

The local dashboard and retrospective replay are launched with `./demo.sh`; `--replay` explicitly enables model calls on completed, locally decrypted cases. Single-game replay reconstructs earlier original history; full replay starts empty at game 1 and accumulates earlier evidence, and its hypothetical payoff bounds remain separate from actual competition results. Setup and presentation instructions: [DEMO.md](DEMO.md). Remove this operational note when exporting the final one-page submission if needed to preserve the page limit.
