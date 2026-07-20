# Run Report -- Candidate a

## Judge Calibration Certificate

- Judge version: `534af4d82b9f121a66a4cc16005228c1258ff761c99bbca29517603909594367`
- Overall κ = 0.749 (95% CI [0.543, 0.886])
- Verdict: **adequate**
- Human-human agreement ceiling (inter-annotator κ) = 0.734 (95% CI [0.575, 0.854])
- Adjudicated disagreements: 13
- Per-candidate κ with 95% cluster-bootstrap CI:
  - candidate a: κ = 0.668 (95% CI [0.420, 0.853])
  - candidate b: κ = 0.845 (95% CI [0.639, 0.962])

Composite mode used for every aggregate below: **FULL_7**.

## Composite Score by Slice

| Slice | Rows | Mean composite | 95% BCa CI |
|---|---|---|---|
| nominal | 96 | 93.30 | [90.03, 95.68] |
| adversarial | 54 | 93.12 | [87.83, 96.56] |
| all | 150 | 93.24 | [90.48, 95.43] |

## Per-Field Accuracy

| Field | Scored | Missing (judge error) | Accuracy |
|---|---|---|---|
| category | 150 | 0 | 94.0% |
| priority | 150 | 0 | 84.0% |
| customer_name | 150 | 0 | 100.0% |
| order_id | 150 | 0 | 100.0% |
| product_name | 150 | 0 | 100.0% |
| issue_summary | 150 | 0 | 79.3% |
| requested_action | 150 | 0 | 95.3% |

## Per-Category

| Category | Rows | Mean composite |
|---|---|---|
| account | 9 | 95.24 |
| baseline | 54 | 95.77 |
| billing | 24 | 95.24 |
| contradiction_with_source | 6 | 92.86 |
| date_number_normalization | 6 | 80.95 |
| distractor_entities | 6 | 100.00 |
| embedded_instructions | 6 | 92.86 |
| fact_density | 6 | 92.86 |
| hallucinated_value | 6 | 100.00 |
| implicit_urgency | 6 | 83.33 |
| mid_thread_burial | 6 | 97.62 |
| multi_request_plain | 6 | 100.00 |
| multi_request_reference_resolution | 6 | 92.86 |
| multi_request_threaded_supersession | 6 | 85.71 |
| multi_request_within_supersession | 6 | 80.95 |
| other | 12 | 96.43 |
| product | 66 | 94.37 |
| shipping | 39 | 88.64 |
| structural_absent_field | 6 | 100.00 |
| tone_vs_content | 6 | 88.10 |
| under_extraction | 6 | 85.71 |
| wrong_field_confusion | 6 | 95.24 |

## Variance Decomposition

### Full composite (FULL_7)

- Between-item variance: 77.179
- Between-replicate variance: 6.349

### Judged-fields-only composite

- Between-item variance: 461.778
- Between-replicate variance: 77.778

_Both decompositions use numpy's population convention (`ddof=0`): these are literal descriptive decompositions of the observed array, not unbiased estimators of latent random-effects parameters._

## Score-vs-Length Correlation

Pearson r between judge verdict (0/1) and candidate field-value character length, pooled over the judged fields (issue_summary, requested_action): **0.159** (n=300 judged-field observations).

