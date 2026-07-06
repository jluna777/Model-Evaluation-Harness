# Run Report -- Candidate a

## Judge Calibration Certificate

- Judge version: `fixture-judge-version-hash`
- Overall κ = 0.720 (95% CI [0.550, 0.850])
- Verdict: **adequate**
- Test-retest intra-annotator consistency ceiling κ = 0.900
- Per-candidate κ (point estimate only -- the certificate carries a CI for the overall κ above, not per-candidate):
  - candidate a: κ = 0.700
  - candidate b: κ = 0.740

Composite mode used for every aggregate below: **FULL_7**.

## Composite Score by Slice

| Slice | Rows | Mean composite | 95% BCa CI |
|---|---|---|---|
| nominal | 4 | 96.43 | [92.86, 96.43] |
| adversarial | 4 | 85.71 | [71.43, 100.00] |
| all | 8 | 91.07 | [76.79, 98.21] |

## Per-Field Accuracy

| Field | Scored | Missing (judge error) | Accuracy |
|---|---|---|---|
| category | 8 | 0 | 87.5% |
| priority | 8 | 0 | 75.0% |
| customer_name | 8 | 0 | 100.0% |
| order_id | 8 | 0 | 100.0% |
| product_name | 8 | 0 | 100.0% |
| issue_summary | 7 | 1 | 100.0% |
| requested_action | 8 | 0 | 75.0% |

## Per-Category

| Category | Rows | Mean composite |
|---|---|---|
| account | 2 | 100.00 |
| billing | 4 | 96.43 |
| product | 2 | 71.43 |
| shipping | 2 | 100.00 |

## Variance Decomposition

### Full composite (FULL_7)

- Between-item variance: 137.117
- Between-replicate variance: 12.755

### Judged-fields-only composite

- Between-item variance: 468.750
- Between-replicate variance: 0.000

_Both decompositions use numpy's population convention (`ddof=0`): these are literal descriptive decompositions of the observed array, not unbiased estimators of latent random-effects parameters._

## Score-vs-Length Correlation

Pearson r between judge verdict (0/1) and candidate field-value character length, pooled over the judged fields (issue_summary, requested_action): **0.502** (n=15 judged-field observations).
