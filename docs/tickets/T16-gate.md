# T16 — Gate command + real baselines

**Phase:** C · **Depends on:** T01, T02, T03, T04, T05, T07, T08, T09, T10, T11, T12, T13, T14, T15 · **Owner gate:** no
**Sources:** plan.md task T16 · spec.md §7, §8, §9 · decisions.md D3

## Goal
Implement the CI gate decision rule exactly per spec §7 — `eval gate [--update-baseline|--seed-regression]` with the 0/1/2 exit-code contract — then generate and commit the real baselines and smoke-run the gate green against them.

## Deliverables
- `src/harness/gate/gate.py`
- `DEGRADED_DEMO_PROMPT` added to `src/harness/prompts.py`
- CLI `eval gate [--update-baseline|--seed-regression]` added to `src/harness/cli.py`
- `tests/unit/gate/test_gate.py` (synthetic fixtures under `tests/fixtures/`)
- `baselines/a.json`, `baselines/b.json` — generated (K=6, traced, reportable) and committed

## Interfaces
**Consumes:**
- `sign_flip_test(deltas, *, sided: Literal["one","two"], n_resamples=10_000, seed) -> PermutationResult{p, m_nonzero, method: "exact"|"monte_carlo", min_attainable_p}` — from T3
- `bca_ci(values, statistic=np.mean, *, level, clusters: Sequence[Hashable]|None, n_resamples=10_000, seed) -> (lo, hi)` — from T4
- `mde(delta_sd, n, *, alpha=0.05, power=0.80) -> float` (one-sided convention, matching the gate) — from T5
- `CompositeMode (FULL_7 | DETERMINISTIC_5)` — from T2
- `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir`; `load_run(run_dir) -> RunArtifact` — from T8
- `TraceContext.for_run(config, reportable: bool)` — from T9 (gate/baseline paths are reportable)
- `render_gate_summary` — from T10
- `generate_baseline(config, model_key) -> BaselineFile`; `check_fingerprint(baseline, run) -> list[Mismatch]` — from T15
- `judge_version()` (T7) and `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` (T1) at the gate call site
- `Certificate` / `data/calibration/certificate.json` verdict — from T14

**Produces:**
- `eval gate [--update-baseline|--seed-regression]` with exit codes 0 = pass · 1 = regression detected · 2 = measurement error (consumed by T17 no-change runs and T18 CI workflow)
- `DEGRADED_DEMO_PROMPT` (consumed by T18's `workflow_dispatch` demo)
- Committed `baselines/{a,b}.json` (consumed by T17, T18, T19)

## Acceptance criteria
All fixture criteria run via `typer.testing.CliRunner` (or equivalent) against synthetic run/baseline fixtures — no live API calls in tests.

- [ ] Decision rule per spec §7: nominal-slice (n=32) K-averaged paired per-item deltas vs baseline; fail iff one-sided sign-flip p < 0.05 AND mean regression > 2.0 points; per candidate; gate fails if the rule fires for **either** candidate (fixture: only candidate B regresses → exit 1).
- [ ] Unchanged fixture (run == baseline) → exit 0.
- [ ] 12-point regression on 20 items → exit 1.
- [ ] 12-point regression on 3 items → exit 0 **plus** the "rejection impossible" warning (m ≤ 4: no rejection possible at α=0.05 regardless of regression size).
- [ ] Regression on exactly 5 items, each regressing 15 points (mean regression over the 32-item slice = 2.34 > the 2.0 margin), all five nonzero deltas negative → p = 0.031 → exit 1 with the m=5 notice (rejection at m=5 requires all five deltas to be regressions, min p = 0.031 — spec §7 errata 2026-07-04). Both fail conditions must fire: the same fixture with 5 items regressing 8 points each (mean 1.25 < margin) → exit 0. Sparse-delta warning printed whenever m < 6; m always printed.
- [ ] Adversarial guardrail: adversarial-only 15-point composite drop → exit 1 via the guardrail (≥10-point drop → fail), labeled "coarse" in the summary; the adversarial delta is printed on every run, firing or not.
- [ ] Judge-error handling: fixture with 2 judge-error items → those items excluded from paired deltas (missing ≠ fail), exclusion count printed, exit code unchanged; fixture with 6% judge-error rate (> 5% of calls) → exit 2.
- [ ] Inadequate-certificate fixture → deltas computed on `DETERMINISTIC_5`, judged fields excluded, flagged in the summary.
- [ ] Fingerprint mismatch vs baseline (fixtures including judge_version drift) → exit 2 with a re-baseline instruction.
- [ ] Missing baseline → exit 2 with the `--update-baseline` instruction; the baseline is **never auto-created**; the message also prints the prompt-bump PR instruction: attach the compare-vs-old-baseline report for human review.
- [ ] `--seed-regression` applies `DEGRADED_DEMO_PROMPT` at runtime, skips the fingerprint check, and banners the entire output as **DEMO MODE** (fixture asserts banner presence).
- [ ] Gate is reportable: with Langfuse keys unset, `eval gate` (and `--update-baseline`) fails fast with `MissingTracingError` before any API call.
- [ ] Gate summary (via T10 `render_gate_summary`) shows: verdict per candidate, mean delta with 90% BCa CI, one-sided p, m, MDE, judge-error exclusion count, adversarial delta + guardrail status, the family false-alarm line (two tests at α=0.05 → ≤ ~9.8% worst case), config values used (margin 2.0 / alpha 0.05 / K=3), token totals + approximate cost from the price snapshot, and the relative link to `docs/gate-design.md`.
- [ ] Real baselines generated with full keys: `eval gate --update-baseline` for both candidates (K=6, traced, reportable), `baselines/a.json` and `baselines/b.json` committed; then `eval gate` smoke-run against them exits 0 (command + output pasted into ticket evidence).
- [ ] `uv run pytest` and `uv run ruff check` pass.

## Notes
- Sequencing: real-baseline generation requires T12 (frozen prompt), T13 (frozen `dataset_version: 1`), and T14 (committed certificate — `calibration_verdict` is a fingerprint component). Implement and green all fixture tests **before** spending live-API baseline runs. This ticket blocks T17 (no-change runs) and T18 (CI wiring).
- Spec clauses that bind implementation: gate test is **one-sided** (compare's is two-sided); gate CI is **90%** BCa (two-sided 90% ↔ the one-sided 5% test level; other reports use 95%); the 2.0 margin is honest bookkeeping dominated by the significance condition at v1's n (spec §7); permutation p uses full enumeration at m ≤ 20, else Monte Carlo (b+1)/(B+1) (T3); temperature 0 and the transport-only retry policy (cap 4, never re-sample a returned response) apply to the live baseline runs via T6/T8.
- Config guardrail (spec §9 / global constraints): margin 2.0, alpha 0.05, K=3, K_baseline=6 are the decided D2/D3 defaults; changing them requires a dated decision-log amendment.
- Exit codes 0/1/2 are a public contract — GitHub Actions (T18) renders 1 and 2 as distinctly labeled failures; keep them stable.
- TDD loop: failing test → minimal impl → green → `uv run ruff check` → commit; the live baseline generation + smoke run happens after green, before the final commit.
