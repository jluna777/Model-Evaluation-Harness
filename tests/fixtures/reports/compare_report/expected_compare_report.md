# Compare Report -- Candidate a vs Candidate b

## Judge Calibration Certificate

- Judge version: `fixture-judge-version-hash`
- Overall κ = 0.720 (95% CI [0.550, 0.850])
- Verdict: **adequate**
- Human-human agreement ceiling (inter-annotator κ) = 0.900
- Per-candidate κ (point estimate only -- the certificate carries a CI for the overall κ above, not per-candidate):
  - candidate a: κ = 0.700
  - candidate b: κ = 0.740

Composite mode used for every aggregate below: **FULL_7**.

Shared item set: 4 item(s).

## Composite Score (absolute, alongside deltas below)

| Candidate | Mean composite (n=4 items) |
|---|---|
| a | 91.07 |
| b | 96.43 |

## Mean Delta

Mean delta (b - a): **5.36** points (95% BCa CI [-10.71, 17.86]).
Two-sided sign-flip permutation p = 0.7500 (m = 3 nonzero deltas, exact, min attainable p = 0.2500).

## Per-Field Pass-Rate Delta (majority vote across replicates)

| Field | a pass rate | b pass rate | Delta (pp) | fail→pass flips | pass→fail flips |
|---|---|---|---|---|---|
| category | 100.0% | 100.0% | +0.0 | 0 | 0 |
| priority | 75.0% | 100.0% | +25.0 | 1 | 0 |
| customer_name | 100.0% | 100.0% | +0.0 | 0 | 0 |
| order_id | 100.0% | 100.0% | +0.0 | 0 | 0 |
| product_name | 100.0% | 75.0% | -25.0 | 0 | 1 |
| issue_summary | 100.0% | 100.0% | +0.0 | 0 | 0 |
| requested_action | 75.0% | 100.0% | +25.0 | 1 | 0 |
