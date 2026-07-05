# T03 — Sign-flip permutation test

**Phase:** A · **Depends on:** T01 (project skeleton/test infra only — no interface consumption) · **Owner gate:** no
**Sources:** plan.md task T3 · spec.md §7 (statistical rule, sparse-delta disclosure), §6 (two-sided use in `eval compare`)

## Goal
Implement the sign-flip permutation test as a pure statistics function: truly exact by full enumeration for small m, seeded Monte Carlo with the (b+1)/(B+1) estimator otherwise, exposing everything the gate and reports need (p, m, method, minimum attainable p).

## Deliverables
- `src/harness/stats/permutation.py`
- `tests/unit/` tests (test tree mirrors `src/`)

## Interfaces
**Consumes:** nothing from earlier tickets beyond the T01 project skeleton (numpy/scipy from the stack).
**Produces (copied verbatim from plan.md):**
- `sign_flip_test(deltas, *, sided: Literal["one","two"], n_resamples=10_000, seed) -> PermutationResult{p, m_nonzero, method: "exact"|"monte_carlo", min_attainable_p}`
- full enumeration when `m_nonzero <= 20`, else Monte Carlo with `p = (b+1)/(B+1)`
- `min_attainable_p` = `2^-m` in exact mode, `1/(B+1)` in MC mode

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] m=2, all-negative deltas → exact one-sided p = 0.25
  - [ ] m=5, all-negative deltas → exact one-sided p = 0.03125 (rejection possible — the spec §7 m=5 case)
  - [ ] all-zero deltas → p = 1.0, `m_nonzero = 0`
  - [ ] on a 40-delta fixture (forces MC mode since m > 20), MC p agrees with `scipy.stats.permutation_test((deltas,), np.mean, permutation_type="samples", alternative="less", n_resamples=10_000)` within `4*sqrt(p(1-p)/B)`
  - [ ] same `seed` → identical MC p across two calls (seeded MC reproducible)
  - [ ] `method` reports `"exact"` when `m_nonzero <= 20` and `"monte_carlo"` otherwise; `min_attainable_p` equals `2^-m` (exact) / `1/(B+1)` (MC)
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- Spec §7 statistical rule this function must support: one-sided sign-flip permutation test on K-averaged paired per-item deltas vs baseline; full enumeration of all 2^m sign assignments when m ≤ 20 (truly exact); otherwise 10,000 Monte Carlo resamples with p̂ = (b+1)/(B+1). The `sided="two"` variant serves `eval compare` (spec §6 — the gate alone is one-sided).
- `m_nonzero` and `min_attainable_p` are load-bearing for the gate's sparse-delta disclosure (spec §7, errata 2026-07-04): at m ≤ 4 no rejection is possible at α=0.05; at m = 5 rejection requires all five nonzero deltas negative (min p = 0.031). The warning logic itself lives in T16 — this ticket only surfaces the numbers.
- Pure function: no I/O, no config; determinism via the explicit `seed` parameter.
- All Global constraints from plan.md apply. TDD loop: failing test → minimal impl → green → `uv run ruff check` → commit.
