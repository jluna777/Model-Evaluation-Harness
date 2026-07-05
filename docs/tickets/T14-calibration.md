# T14 — Calibration: labels, agreement, certificate, self-consistency

**Phase:** B · **Depends on:** T01, T05, T07, T08, T09, T11, T13 · **Owner gate:** yes ◆
**Sources:** plan.md task T14 · spec.md §5, §4, §8 · decisions.md D2, D1

## Goal
Produce the judge calibration: owner-labeled field judgments, the agreement report with cluster-bootstrap CIs, the committed calibration certificate, and the judge self-consistency measurement — wiring `eval calibrate [--retest]` as a reportable command.

## Deliverables
- `data/calibration/emails.jsonl` (25 dedicated calibration emails; more if the stratification loop adds items)
- `data/calibration/labels.jsonl` (owner labels)
- `data/calibration/certificate.json` (committed)
- `src/harness/calibrate.py`
- CLI `eval calibrate [--retest]` added to `src/harness/cli.py`
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
  `{judge_version, overall_kappa, kappa_ci, per_candidate_kappa, verdict: "adequate"|"adequate_with_caveat"|"inadequate", ceiling_kappa?, label_file_hash, date}`
- `eval calibrate [--retest]` — reportable command (fails fast without Langfuse keys)
- Labels file rows per spec §5: `{label_id, item_id, candidate, field, verdict, critique, label_date, round: "initial"|"retest"}`

## Acceptance criteria
- [ ] `data/calibration/emails.jsonl` uses the same item schema as golden (including `expected` reference values) and is disjoint from `data/golden/golden.jsonl` — a unit test asserts zero id/email overlap.
- [ ] `data/calibration/labels.jsonl` contains ≥100 `round: "initial"` rows (25 emails × 2 free-text fields × 2 candidates = 100 field judgments, 50 per candidate; more if stratified additions), every row validating against T1's `CalibrationLabel`.
- [ ] Stratification loop executed per spec §5: if initial fail-label rate < 20%, harder-category emails were added and labeled (labeled items never removed); the calibrate report states the fail-enrichment conservatism ("measured on a harder-than-operational distribution"). Set frozen **before** the judge is run on it — evidenced by the commit hash freezing `emails.jsonl`/`labels.jsonl` predating the judge-run artifacts (git log ordering pasted into ticket evidence).
- [ ] Certificate verdict logic verified on synthetic fixtures for all three states: κ̂ ≥ 0.6 with CI lower bound ≥ 0.4 → `adequate`; κ̂ ≥ 0.6 with CI lower bound < 0.4 → `adequate_with_caveat` (gray zone); κ̂ < 0.6 → `inadequate`.
- [ ] Per-candidate kappa gap > 0.2 fixture → D1-review flag rendered in the report (a flag, never a gate condition).
- [ ] Calibrate report shows: overall Cohen's κ with cluster-bootstrap CI (resampling emails — all judgments of an email move together), per-candidate kappas with CIs, raw agreement, and label prevalence (descriptive context only; kappa alone decides).
- [ ] Judge self-consistency: 20 fixed (email, reference, candidate-value) triples each judged 3×; mocked judge with exactly one flipping triple → flip rate 1/20 in the report and certificate context.
- [ ] `eval calibrate` with Langfuse keys unset fails fast with `MissingTracingError` before any API call (the T9/T11 fail-fast anchor lands here); with keys set it proceeds.
- [ ] `eval calibrate --retest` on a fixture containing `round: "retest"` labels adds the ceiling row: intra-annotator kappa on the intersection, with its own CI, labeled *an estimate of the consistency ceiling*.
- [ ] `data/calibration/certificate.json` committed with every spec §5 field, `label_file_hash` matching `labels.jsonl`, and `judge_version` matching T7's `judge_version()`.
- [ ] Retest date (≥1 week after the initial `label_date`) is recorded in this ticket's evidence before the owner signs — its execution is a T19 entry criterion.
- [ ] `uv run pytest` and `uv run ruff check` pass.
- [ ] ◆ Owner validates (signs) the calibration certificate.

## Notes
- Owner work: label the 100 initial field judgments with one-line critiques (~2 h), plus stratification-loop additions if the fail rate is < 20%. **Schedule the 25-relabel retest ≥1 week out now** — its execution is a T19 entry criterion, not a blocker for this ticket.
- Judge runs at temperature 0 (spec §4, global constraints). Judge errors (`verdict=None`) are never counted as fail.
- Adequacy is decided on the overall κ **point estimate** ≥ 0.6 (spec §5); `inadequate` triggers the response ladder (escalate judge model → revise rubric) and downstream judged-field exclusion (T16). Re-certification after any judge change requires **freshly drafted** calibration emails (spec §5 / D2 amendment).
- Calibration items are never used for prompt tuning (global constraints); few-shots come only from dev/hand-written (spec §4).
- Sequencing: needs T13's frozen golden set for the disjointness guarantee; blocks T16's real-baseline generation (fingerprint needs `calibration_verdict`).
- TDD loop for `calibrate.py`: failing test → minimal impl → green → `uv run ruff check` → commit.
