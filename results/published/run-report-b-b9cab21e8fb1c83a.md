# Run Report -- Candidate b

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
| nominal | 96 | 95.83 | [93.45, 97.47] |
| adversarial | 54 | 92.33 | [88.36, 95.50] |
| all | 150 | 94.57 | [92.48, 96.29] |

## Per-Field Accuracy

| Field | Scored | Missing (judge error) | Accuracy |
|---|---|---|---|
| category | 150 | 0 | 96.0% |
| priority | 150 | 0 | 91.3% |
| customer_name | 150 | 0 | 92.0% |
| order_id | 150 | 0 | 96.0% |
| product_name | 150 | 0 | 94.0% |
| issue_summary | 150 | 0 | 94.0% |
| requested_action | 150 | 0 | 98.7% |

## Per-Category

| Category | Rows | Mean composite |
|---|---|---|
| account | 9 | 95.24 |
| baseline | 54 | 96.83 |
| billing | 24 | 95.83 |
| contradiction_with_source | 6 | 92.86 |
| date_number_normalization | 6 | 92.86 |
| distractor_entities | 6 | 100.00 |
| embedded_instructions | 6 | 95.24 |
| fact_density | 6 | 95.24 |
| hallucinated_value | 6 | 85.71 |
| implicit_urgency | 6 | 100.00 |
| mid_thread_burial | 6 | 92.86 |
| multi_request_plain | 6 | 95.24 |
| multi_request_reference_resolution | 6 | 90.48 |
| multi_request_threaded_supersession | 6 | 85.71 |
| multi_request_within_supersession | 6 | 95.24 |
| other | 12 | 96.43 |
| product | 66 | 95.67 |
| shipping | 39 | 91.21 |
| structural_absent_field | 6 | 92.86 |
| tone_vs_content | 6 | 97.62 |
| under_extraction | 6 | 88.10 |
| wrong_field_confusion | 6 | 92.86 |

## Variance Decomposition

### Full composite (FULL_7)

- Between-item variance: 45.361
- Between-replicate variance: 8.163

### Judged-fields-only composite

- Between-item variance: 114.333
- Between-replicate variance: 55.556

_Both decompositions use numpy's population convention (`ddof=0`): these are literal descriptive decompositions of the observed array, not unbiased estimators of latent random-effects parameters._

## Score-vs-Length Correlation

Pearson r between judge verdict (0/1) and candidate field-value character length, pooled over the judged fields (issue_summary, requested_action): **-0.087** (n=300 judged-field observations).

