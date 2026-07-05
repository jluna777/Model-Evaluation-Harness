# Tickets — Model Evaluation Harness v1

**Status:** Validated v1.0 · 2026-07-04
Derived from `docs/plan.md` (Validated v1.0). One ticket per plan task; tickets are the unit of implementation and owner validation. Every ticket implicitly includes the plan's **Global constraints**. ◆ marks owner gates.

| # | Ticket | Phase | Depends on | ◆ |
|---|---|---|---|---|
| T01 | [Project skeleton, config, schema, prompt plumbing](T01-skeleton-config-schema.md) | A | — | |
| T02 | [Deterministic scoring + composite](T02-deterministic-scoring.md) | A | T01 | |
| T03 | [Sign-flip permutation test](T03-permutation-test.md) | A | T01 | |
| T04 | [BCa bootstrap with cluster resampling](T04-bootstrap.md) | A | T01 | |
| T05 | [Agreement, MDE, variance decomposition](T05-agreement-mde-variance.md) | A | T01, T04 | |
| T06 | [Model clients (candidates) + retry](T06-candidate-clients.md) | A | T01 | |
| T07 | [Judge client + rubric versioning](T07-judge-client.md) | A | T06 | |
| T08 | [Runner with persistence and resume](T08-runner.md) | A | T01, T02, T06, T07 | |
| T09 | [Tracing with bounded degradation](T09-tracing.md) | A | T08 | |
| T10 | [Reports](T10-reports.md) | A | T01, T03, T04, T05, T08, T09 | |
| T11 | [CLI: run, compare, rescore](T11-cli-run-compare-rescore.md) | A | T01–T10 | |
| T12 | [Dev set + prompt freeze](T12-dev-set-prompt-freeze.md) | B | T11 | ◆ |
| T13 | [Golden set: draft → open-coding → freeze](T13-golden-set.md) | B | T01, T11, T12 | ◆ |
| T14 | [Calibration: labels, agreement, certificate, self-consistency](T14-calibration.md) | B | T01, T05, T07, T08, T09, T11, T13 | ◆ |
| T15 | [Baseline module](T15-baseline-module.md) | C | T01, T07, T08 | |
| T16 | [Gate command + real baselines](T16-gate.md) | C | T01–T05, T07–T15 | |
| T17 | [Gate-design doc + no-change demonstration](T17-gate-design-doc.md) | C | T16 | |
| T18 | [CI workflow](T18-ci-workflow.md) | D | T16, T17 | |
| T19 | [README, published artifacts, demo](T19-readme-published.md) | D | T14 retest, T17, T18 | ◆ |
| T20 | [Acceptance walk](T20-acceptance-walk.md) | D | T19 | |

**Scheduling notes:** Phase B (owner data work) starts as soon as T06 lands and overlaps Phase A. T14's initial labeling is scheduled at the earliest possible date — the ≥1-week retest gap is a T19 entry criterion, not a T14 blocker. T16 is the convergence point: it needs the frozen prompt (T12), frozen dataset (T13), and calibration certificate (T14) before generating real baselines.
