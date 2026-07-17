# Gate Summary

## Judge Calibration Certificate

- Judge version: `fixture-judge-version-hash`
- Overall κ = 0.720 (95% CI [0.550, 0.850])
- Verdict: **adequate**
- Human-human agreement ceiling (inter-annotator κ) = 0.900
- Per-candidate κ (point estimate only -- the certificate carries a CI for the overall κ above, not per-candidate):
  - candidate a: κ = 0.700
  - candidate b: κ = 0.740

**Overall verdict: FAIL**

Composite mode used for every figure below: **FULL_7**.

## Config

- margin: 2.0
- alpha: 0.05
- K: 3

## Per-Candidate Results

### Candidate a

- Verdict: **PASS**
- Mean delta (nominal slice, current - baseline): **0.80** points (90% BCa CI [-1.20, 2.50])¹
- One-sided sign-flip permutation p = 0.4200 (m = 18 nonzero deltas, exact, min attainable p = 0.0000)
- MDE (α=0.05, 80% power): **6.10** points
- Judge-error exclusions: 1 item(s) excluded from paired deltas (a missing judged field is never scored as a fail)
- Adversarial-slice delta (current - baseline, always printed): **-2.00** points -- coarse guardrail (>=10-point drop): **not tripped**
- Candidate token usage: 50000 in / 20000 out (~$0.1500, approximate-at-snapshot 2026-07-16)
- Judge token usage: 80000 in / 30000 out (~$0.1300; judge calls dominate cost -- one call per judged field per replicate, vs one candidate call per replicate)

### Candidate b

- Verdict: **FAIL**
- Mean delta (nominal slice, current - baseline): **-3.40** points (90% BCa CI [-6.10, -1.00])¹
- One-sided sign-flip permutation p = 0.0120 (m = 20 nonzero deltas, exact, min attainable p = 0.0000)
- MDE (α=0.05, 80% power): **5.80** points
- Judge-error exclusions: 2 item(s) excluded from paired deltas (a missing judged field is never scored as a fail)
- Adversarial-slice delta (current - baseline, always printed): **-11.50** points -- coarse guardrail (>=10-point drop): **TRIPPED**
- Candidate token usage: 52000 in / 21000 out (~$0.1335, approximate-at-snapshot 2026-07-16)
- Judge token usage: 81000 in / 31000 out (~$0.1335; judge calls dominate cost -- one call per judged field per replicate, vs one candidate call per replicate)

## Family False-Alarm Rate

Family false-alarm rate: two tests at α=0.05 → ≤ ~9.8% worst case (union bound over both candidates' independent decision rules: 1 - (1 - 0.05)² ≈ 0.0975).

## Further Reading

See [docs/gate-design.md](docs/gate-design.md) for the analytic false-alarm justification, threat model, and re-baseline procedure.

---
¹ _This summary's delta CIs are 90% two-sided, which corresponds to the one-sided 5% significance level the decision rule actually tests against -- `eval run`/`eval compare` reports use 95%._
