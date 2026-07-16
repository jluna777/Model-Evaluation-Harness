# T14 — Calibration: labels, agreement, certificate, self-consistency

**Phase:** B · **Depends on:** T01, T05, T07, T08, T09, T11, T13 · **Owner gate:** yes ◆
**Sources:** plan.md task T14 · spec.md §5, §4, §8 · decisions.md D2, D1

**Amended 2026-07-09 (owner):** dual-annotation upgrade — this ticket's original single-annotator + test-retest design is replaced by dual independent annotation with owner adjudication (decisions.md D2 amendment 2026-07-09). Both annotators label the full set independently; the owner adjudicates disagreements; inter-annotator agreement sets the measured ceiling. The acceptance criteria and notes below reflect the amended design.

**Amended 2026-07-10 (owner):** fail-probe perturbation set — the owner's initial fail rate on the 100 real calibration judgments came back at 0%, and harder real emails demonstrably do not reach spec §5's ≥20% floor (decisions.md D2 amendment 2026-07-10). A second, disjoint item source (`data/calibration/emails-fail-probe.jsonl`) plus a committed perturbation overlay (`data/calibration/perturbations.jsonl`) supplies controlled, disclosed fail-side content instead. The deliverables, acceptance criteria, and notes below are updated accordingly.

**Evidence note 2026-07-16 (owner):** the certificate committed under this ticket (`data/calibration/certificate.json`, commit e321cec) was signed under judge `gemini-2.5-pro`. The judge is now re-pinned to `gemini-2.5-flash` (decisions.md D1 amendment 2026-07-16, operational/quota grounds — not a rubric or few-shot change). Re-certification against `gemini-2.5-flash` is pending, on the SAME committed gold (no freshly drafted calibration emails this round; decisions.md D2 amendment 2026-07-16 records why that's sound here). The pre-registered acceptance rule governs: the new certificate's adequacy verdict alone decides (κ̂ ≥ 0.6, CI lower bound ≥ 0.4) — not a comparison against the prior judge's certified 0.719 — and an `inadequate` result reverts this judge switch, leaving the e321cec certificate standing.

## Goal
Produce the judge calibration: dual-annotator field judgments (owner + a second, independent annotator), owner adjudication of disagreements, the agreement report with cluster-bootstrap CIs (judge-vs-gold and the human-human IAA ceiling), the committed calibration certificate, and the judge self-consistency measurement — wiring `eval calibrate` as a reportable command that resolves gold and the ceiling automatically whenever both annotators' labels are present.

## Deliverables
- `data/calibration/emails.jsonl` (25 dedicated calibration emails; more if the stratification loop adds items)
- `data/calibration/emails-fail-probe.jsonl` (~10 fail-probe items, same schema, ids continuing the `cal-0NN` sequence, disjoint from `emails.jsonl` — D2 amendment 2026-07-10)
- `data/calibration/perturbations.jsonl` (committed perturbation overlay rows — D2 amendment 2026-07-10)
- `data/calibration/labels.jsonl` (both annotators' labels + owner adjudication rows, covering real AND fail-probe items)
- `data/calibration/certificate.json` (committed)
- `src/harness/calibrate.py`
- CLI `eval calibrate` added to `src/harness/cli.py` (dual annotation is automatic; no flag; `--fail-probe-emails`/`--perturbations` optional, absent = pre-amendment behavior)
- `tests/unit/test_calibrate.py` (synthetic fixtures per acceptance criteria)

## Interfaces
**Consumes:**
- `cohens_kappa(a, b, *, clusters=None) -> KappaResult{kappa, ci, raw_agreement, prevalence}` — from T5 (CIs are cluster bootstrap resampling emails)
- `judge_version() -> str` (hash of model id + prompt + rubric + few-shots) — from T7
- `judge_field(email, field_name, reference, candidate_value) -> JudgeResult{verdict: "pass"|"fail"|None, error: str|None, rationale, raw: str}` — from T7
- `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir`; `load_run(run_dir) -> RunArtifact` — from T8
- `TraceContext.for_run(config, reportable: bool)`; `reportable=True` + missing keys → `MissingTracingError` at startup — from T9
- `CalibrationLabel`, `Certificate` — from T1

**Produces:**
- `data/calibration/certificate.json` per spec §5, consumed by T10 report headers and by T1's `fingerprint(...)` `calibration_verdict` argument at T15/T16 call sites:
  `{judge_version, overall_kappa, kappa_ci, per_candidate_kappa, per_candidate_kappa_ci?, verdict: "adequate"|"adequate_with_caveat"|"inadequate", ceiling_kappa?, ceiling_kappa_ci?, n_adjudicated?, label_file_hash, date, n_perturbed?, achieved_fail_prevalence?, real_only_kappa?, real_only_kappa_ci?, perturbed_rows_passed_by_gold?}`
- `eval calibrate` — reportable command (fails fast without Langfuse keys); resolves dual-annotation gold + the human-human ceiling automatically when both annotators' `round="initial"` labels are complete, else a clean, loud error (`DualAnnotationError`); loads the fail-probe set and applies the perturbation overlay when present, else a clean, loud error (`PerturbationOverlayError`) on a malformed overlay
- Labels file rows per spec §5: `{label_id, item_id, candidate, field, annotator, verdict, critique, label_date, round: "initial"|"adjudication"}`
- Perturbation overlay rows per spec §5: `{item_id, candidate, field, perturbed_value, corruption_type, rationale}`

## Acceptance criteria
- [ ] `data/calibration/emails.jsonl` uses the same item schema as golden (including `expected` reference values) and is disjoint from `data/golden/golden.jsonl` — a unit test asserts zero id/email overlap.
- [ ] `data/calibration/labels.jsonl` contains ≥100 `round: "initial"` rows from the OWNER and ≥100 `round: "initial"` rows from the SECOND annotator, covering the exact same keys (25 emails × 2 free-text fields × 2 candidates = 100 field judgments per annotator; more if stratified additions), every row validating against T1's `CalibrationLabel`.
- [ ] Stratification loop executed per spec §5: if the owner's initial fail-label rate < 20%, harder-category emails were added and labeled by **both** annotators (labeled items never removed); the calibrate report states the fail-enrichment conservatism ("measured on a harder-than-operational distribution"). Set frozen **before** the judge is run on it — evidenced by the commit hash freezing `emails.jsonl`/`labels.jsonl` predating the judge-run artifacts (git log ordering pasted into ticket evidence).
- [ ] Dual-annotation gold resolution verified on synthetic fixtures: agreement → owner's verdict (`source="agreement"`); disagreement + owner adjudication row → adjudicated verdict wins (`source="adjudication"`); disagreement with no adjudication row → loud error naming every unadjudicated key; incomplete second-annotator coverage → loud error naming every missing key ("second annotator labels incomplete: N keys missing").
- [ ] Human-human agreement (IAA) ceiling verified on synthetic fixtures: perfect agreement → κ = 1; an engineered disagreement pattern → hand-computed κ matches; a cross-annotator `output_sha256` mismatch → `CalibrationBindingError`.
- [ ] Certificate verdict logic verified on synthetic fixtures for all three states: κ̂ ≥ 0.6 with CI lower bound ≥ 0.4 → `adequate`; κ̂ ≥ 0.6 with CI lower bound < 0.4 → `adequate_with_caveat` (gray zone); κ̂ < 0.6 → `inadequate`.
- [ ] Per-candidate kappa gap > 0.2 fixture → D1-review flag rendered in the report (a flag, never a gate condition).
- [ ] Calibrate report shows: overall Cohen's κ (judge vs. resolved gold) with cluster-bootstrap CI (resampling emails — all judgments of an email move together), per-candidate kappas with CIs, raw agreement, label prevalence (descriptive context only; kappa alone decides), the human-human agreement ceiling with its own CI, and the adjudicated-disagreement count.
- [ ] Judge self-consistency: 20 fixed (email, reference, candidate-value) triples each judged 3×; mocked judge with exactly one flipping triple → flip rate 1/20 in the report and certificate context.
- [ ] `eval calibrate` with Langfuse keys unset fails fast with `MissingTracingError` before any API call (the T9/T11 fail-fast anchor lands here); with keys set it proceeds.
- [ ] `eval calibrate` on a fixture with both annotators' complete, correctly-bound labels adds the ceiling row automatically (no flag) — human-human κ on the intersection, with its own CI, labeled *the human-human agreement ceiling*; the offline (`--offline`) path reproduces the same gold resolution and ceiling identically from persisted `judgments.jsonl` + `labels.jsonl`.
- [ ] `data/calibration/certificate.json` committed with every spec §5 field, `label_file_hash` matching `labels.jsonl`, and `judge_version` matching T7's `judge_version()`.
- [ ] The second annotator has read the written labeling conventions/rubric before labeling (D2 amendment 2026-07-09: the ceiling measures task ambiguity only if both annotators apply the same rules) — recorded in this ticket's evidence.
- [ ] Perturbation overlay validation verified on synthetic fixtures (D2 amendment 2026-07-10): a valid overlay row is accepted and its value flows into the reconstructed triple/labeling sheet/label binding hash; a key targeting the original `emails.jsonl`, a nonexistent (item_id, candidate, field) key, and a duplicate key each raise `PerturbationOverlayError` naming every offending key, all-or-nothing.
- [ ] Fail-probe disclosure fields verified on synthetic fixtures: `n_perturbed` counts overlaid rows; the achieved fail prevalence is computed over the combined (real + probe) valid population; `perturbed_rows_passed_by_gold` counts overlaid rows whose resolved gold is nonetheless `"pass"`; a real-only Cohen's κ (judge vs. gold restricted to non-probe items) is computed alongside — never replacing — the primary overall κ, which remains the sole adequacy-decision statistic over the full, probe-included population.
- [ ] Absent-probe-file backward compatibility: `eval calibrate` with no `emails-fail-probe.jsonl`/`perturbations.jsonl` present reproduces pre-amendment behavior exactly (every fail-probe disclosure field `None`/absent from the report) — existing tests predating this amendment keep passing unchanged.
- [ ] Offline-path parity: `eval calibrate --offline` re-validates the SAME overlay file against `judgments.jsonl`'s persisted `is_probe` flag and reproduces the identical disclosure numbers as the live run, with zero API calls and no fail-probe emails file needed offline.
- [ ] Population-parity checks (D2 amendment 2026-07-09b) span the union of real + probe triples unchanged: a probe-set judge error, or a probe key labeled by neither annotator, is handled identically to a real-set one.
- [ ] Blindness protocol recorded in this ticket's evidence (decisions.md D2 amendment 2026-07-10): the second annotator's fail-probe sheet is uniform and indistinguishable from a real-item sheet; the owner knows the fail-probe batch exists but not which rows are perturbed or how, while labeling.
- [ ] `uv run pytest` and `uv run ruff check` pass.
- [ ] ◆ Owner validates (signs) the calibration certificate.

## Notes
- Owner work: label the 100 initial field judgments with one-line critiques (~2 h), plus stratification-loop additions if the fail rate is < 20%; adjudicate whatever disagreements the second annotator's labels surface. **No calendar gap to schedule** — the dual-annotation upgrade (2026-07-09) removes the retired design's ≥1-week test-retest wait; the second annotator labels in parallel with the owner.
- Second-annotator work: label the same 100 (+ stratification additions) field judgments independently, from their own hash-bound sheet (`labeling_template_rows(triples, annotator)`), after reading the written labeling conventions — never seeing the owner's verdicts.
- Judge runs at temperature 0 (spec §4, global constraints). Judge errors (`verdict=None`) are never counted as fail.
- Adequacy is decided on the overall κ **point estimate** ≥ 0.6 (spec §5); `inadequate` triggers the response ladder (escalate judge model → revise rubric) and downstream judged-field exclusion (T16). Re-certification after any judge change requires **freshly drafted** calibration emails (spec §5 / D2 amendment).
- Calibration items are never used for prompt tuning (global constraints); few-shots come only from dev/hand-written (spec §4).
- Sequencing: needs T13's frozen golden set for the disjointness guarantee; blocks T16's real-baseline generation (fingerprint needs `calibration_verdict`).
- TDD loop for `calibrate.py`: failing test → minimal impl → green → `uv run ruff check` → commit.
