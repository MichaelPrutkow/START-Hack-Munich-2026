# chineseModels — three-minute pitch

*Approximately three minutes with brief pauses. Interim snapshot: after game 8, 21 November 2026.*

**0:00–0:40 · The decision**

Imagine reviewing a €100 insurance charge. If you accept it, you pay €100, assuming no payment cap applies. If you reject it and the charge is fair, you pay €150. That makes uncertainty part of the financial decision.

We are chineseModels. We built a system that separates three questions: Is this damage covered? What is a reasonable total for this invoice line? And what charge and acceptance limit should we submit?

**0:40–1:25 · The system**

Our first priority is a complete submission within sixty seconds. One designated runner immediately submits rule-based fallback prices. Fast and deeper assessments then work within fixed time budgets. Missing model answers keep their fallback values. Policy caps and shared limits are applied in code.

We preserve disagreement between model votes. Optional focused review can revisit consequential lines while time remains. Each final price is traceable to its contributing model tier and settings. That lets us investigate the decision that reached the server, including partial updates.

**1:25–2:10 · Learning responsibly**

The difficult part is learning when the true fair price stays hidden. An accepted payment may have been capped. A positive rejection penalty gives stronger evidence. We turn these observations into intervals and evaluate actual submitted prices against conservative payoff bounds.

Our learning report separates expensive reviewer mistakes from issuer opportunity losses. Recall matches policy, quantity, unit, and description; it keeps previous coverage judgments separate from price evidence.

Between rounds, automatic learning adjusts price bias, spread and the charge multiplier using earlier games. The frozen candidate then needs sufficient samples and at least five percent improvement on later games before activation. Eligible coverage mistakes remain in that validation. A replacement must also beat the active calibration.

**2:10–3:00 · Evidence and limits**

After game eight on November twenty-first, our cumulative net was €12,087.65, placing us third at that snapshot. Those are interim results.

The new learning safeguards have synthetic correctness tests. We have not yet demonstrated a production profit uplift, and the optional uncertainty adjustment remains disabled by default.

Our contribution is an auditable decision process: a complete fallback, explicit uncertainty, and a learning rule that requires evidence before changing the strategy.

## Two-minute Q&A preparation

**Why use a two-thirds threshold?**

For an uncapped charge, acceptance costs (a); rejection costs (1.5a) with probability that the charge is fair. Acceptance is preferable above two-thirds. This is fairness at a particular price, including coverage and price adequacy. It is not coverage probability alone.

**Do you know the true fair prices?**

Generally, we know bounds. Positive rejection payments reveal a fair charge through the 1.5 multiplier. Accepted payments can be capped, so they are exact charges only when additional evidence proves that. Unresolved bounds remain explicit in evaluation and loss attribution.

**What prevents overfitting?**

Chronological separation, matching model/prompt/configuration identities, and minimum evidence: six games, sixteen confirmed-covered training lines, and eight held-out eligible lines. Validation includes uncertain-coverage predictions. A candidate stays in shadow unless its conservative payoff lower bound beats actual baseline net by at least 5%.

**How do you control time and cost?**

We submit a complete fallback before model analysis, enforce tier budgets and a submission deadline, and log token usage. Focused review is optional and uses remaining time. Background learning uses bounded public-data requests between rounds and no model calls. Reports can also be rebuilt entirely from cached data.

**Can historical recall transfer the wrong coverage or price?**

Recall requires the same policy and quantity, matching units, and similar descriptions. It presents historical policy-adjusted total intervals, never inferred market unit rates. Current damage relation and coverage must be assessed independently. August data is excluded.

**Which improvement have you proved?**

We have tested accounting, bounds, provenance, and activation safeguards on synthetic scenarios. We have not yet proved production gains from the new policy. The game-eight result is an interim baseline snapshot, and earlier heuristic backtest totals are not evidence for the corrected method.

*Evidence: [runner](../runner.py), [pricing](../claim_to_fame/pricing.py), [evaluation](../calibrate.py), [replay](../eval_cases.py), [learning](../claim_to_fame/feedback.py), [offline tests](../tests/).*
