# Constitution

**Project:** Model Evaluation Harness
**Identity:** A structured-extraction eval harness with a calibrated LLM judge and CI gating.
**Status:** Draft v0.1 — pending owner validation · 2026-07-04

This document states the principles and locked v1 scope. It changes rarely and only with the owner's sign-off. The spec, plan, and tickets must not contradict it; if they need to, this document is amended first.

---

## 1. Purpose

This repo is a portfolio artifact. Its job is to demonstrate evaluation-design judgment, not feature count. Every design decision should survive scrutiny by an engineer who builds evals for a living. When a choice trades polish against measurement honesty, honesty wins.

## 2. Product principles

1. **Honest measurement over impressive numbers.** Every reported score carries its uncertainty. No bare point estimates, no cherry-picked runs.
2. **The judge is an instrument, and instruments get calibrated.** "Calibrated" means exactly this here: the judge is validated against human labels and the measured agreement is reported. If agreement turns out inadequate, judged scores are flagged or withheld — the method, statistic, threshold, and response are Decision D2.
3. **Bias is designed around, not disclaimed.** Position bias, verbosity bias, and self-preference are each addressed deliberately — measured, mitigated, or shown immaterial for this task, per Decision D1 — never left to a limitations paragraph.
4. **Golden sets are engineered, not collected.** The dataset starts from a documented failure-mode taxonomy; examples exist to probe specific failure modes and to anchor nominal behavior, never to pad n. The model that generates synthetic examples is recorded, and its relationship to the candidates and judge is documented; coverage mix and provenance handling are Decision D4.
5. **Gate on signal, not raw deltas.** CI fails a build only when a regression is distinguishable from measurement noise. At n≈50 the gate has limited resolution: it catches regressions above a documented minimum detectable effect, and it reports that limit with every gate decision — below it, silence is expected, not failure. Which noise sources count, how they are estimated, and the gating method are Decision D3.
6. **Never overfit the eval.** Candidate and judge prompts are not tuned against the items that produce reported numbers; any judge-prompt change invalidates prior agreement measurement and triggers re-validation.
7. **Re-executable runs, reproducible results.** A run is fully specified by pinned config + versioned dataset + pinned model IDs. Hosted APIs are nondeterministic, so every run persists raw model and judge outputs — all scores, comparisons, and reports are exactly reproducible from stored artifacts. Every call is traced in Langfuse.

## 3. Engineering principles

- **Spec-driven:** Constitution → Spec → Plan → Tickets → Implement → Validate. All artifacts are markdown in this repo: `docs/constitution.md`, `docs/spec.md`, `docs/decisions.md`, `docs/plan.md`, `docs/tickets/`. The owner validates each artifact before the next stage begins; no implementation code before the validated spec.
- **Validated increments:** each ticket has verifiable acceptance criteria and ships independently. Scope expansion is flagged and decided, never silently absorbed.
- **Stack:** Python 3.12+, `uv`, `pyproject.toml`, `ruff`, `pytest`, type hints throughout, Pydantic for schemas and config. Test-driven for scoring, statistics, and parsing logic.
- **Config as data:** eval runs are defined by declarative config files; Principle 7 defines what pins a run.

## 4. v1 scope (locked)

| Dimension | Decision |
|---|---|
| Task | ONE task: customer support email → structured ticket (enum fields + entity fields + free-text fields) |
| Golden set | ~50 synthetic, human-curated examples, seeded from a documented failure-mode taxonomy (D4) |
| Judge calibration data | A small human-labeled calibration set, hand-curated in versioned files; its design and the agreement method are Decision D2 |
| Candidates | Two models from different providers (one Anthropic, one OpenAI); exact model IDs pinned in the spec |
| Judge | One judge model; provider/model selection and bias handling are Decision D1. Selection reconciles "capable enough to judge" with "neutral to both candidates" — neutrality (likely a third provider) is the primary criterion, and judging capability is validated by D2 agreement, not assumed from model strength |
| Capabilities | Golden-set eval (deterministic scoring for exact fields, LLM judge for free-text fields) · pairwise comparison · CI gate in GitHub Actions · Langfuse tracing · CLI with markdown report |

## 5. Cut from v1 (explicit)

- Additional extraction tasks or a multi-task abstraction
- Provider-agnostic plugin system (LiteLLM-style) — v1 keeps a thin model interface with exactly the providers it needs, so cuts can return in v2
- Any web dashboard or UI — Langfuse is the UI
- Judge ensembles / multi-judge voting — v1 ships one judge, validated per D2
- Human annotation tooling — golden data and calibration labels are hand-curated, versioned files
- Dataset-generation pipeline — synthetic examples are generated ad hoc, human-curated, and checked in as versioned data; no generation tooling ships in v1
- Prompt-optimization loops, fine-tuning, retries-as-quality-strategy
- Run-history storage and trend reporting — v1 keeps only the baseline artifact the D3 gate compares against
- Cost dashboards, response caching, async orchestration beyond bounded concurrent API calls
- Statistical machinery beyond what the committed measurements need — the D3 gate, per-score uncertainty (Principle 1), and the D2 agreement metric; one method each, no stacked layers

## 6. Decision rights

Four decisions carry this project's demonstrated-judgment value. They are decided by the owner at spec time and recorded with rationale in `docs/decisions.md`. Claude proposes options and tradeoffs; the owner decides. These decisions are never made implicitly by code:

- **D1 — Judge selection and bias:** which judge model is used and how it handles position bias, verbosity bias, and self-preference. Selection weighs neutrality toward both candidates (the structural defense against self-preference) against being capable enough to judge — noting that position and verbosity bias tend to worsen in weaker models, so neutrality and capability must be reconciled, not traded blindly.
- **D2 — Judge calibration:** how the judge is validated against a human-labeled set, how agreement is reported, and what happens when agreement is inadequate. Calibration items are held out from the reported golden set; any overlap must be a consciously justified choice recorded in the decision. Labels come from a single annotator (the owner) — a known limitation: in production, multiple annotators with inter-annotator agreement would set the ceiling the judge is measured against.
- **D3 — CI gating:** how the gate distinguishes statistically meaningful regressions from noise, and how its detection limit is computed and traded against false alarms.
- **D4 — Golden set design:** how the failure-mode taxonomy is constructed, the coverage mix, and how generator provenance is handled.

## 7. Definition of done for v1

1. One documented command evaluates both candidates on the golden set, scores exact fields deterministically and free-text fields with the judge (agreement reported per D2), compares pairwise, and emits a markdown report — every call traced in Langfuse.
2. The CI gate fails a deliberately seeded regression, and passes an unchanged baseline at its designed false-alarm rate — demonstrated over repeated no-change runs or justified analytically, per D3.
3. The README shows that before/after with real numbers: scores with uncertainty, judge–human agreement, and the gate's minimum detectable effect.
