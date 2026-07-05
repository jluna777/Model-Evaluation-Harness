# T04 — BCa bootstrap with cluster resampling

**Phase:** A · **Depends on:** T01 (project skeleton/test infra only — no interface consumption) · **Owner gate:** no
**Sources:** plan.md task T4 · spec.md §5 (cluster bootstrap resampling emails), §6 (95% BCa cluster CIs), §7 (90% BCa CI in gate output)

## Goal
Implement a BCa bootstrap confidence interval as a pure statistics function, with an optional cluster mode that resamples whole clusters (emails) so correlated within-cluster judgments move together.

## Deliverables
- `src/harness/stats/bootstrap.py`
- `tests/unit/` tests (test tree mirrors `src/`)

## Interfaces
**Consumes:** nothing from earlier tickets beyond the T01 project skeleton (numpy/scipy from the stack).
**Produces (copied verbatim from plan.md):**
- `bca_ci(values, statistic=np.mean, *, level, clusters: Sequence[Hashable]|None, n_resamples=10_000, seed) -> (lo, hi)`
- cluster mode resamples whole clusters

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] non-cluster `bca_ci` matches `scipy.stats.bootstrap(method="BCa")` within tolerance on a reference sample (fixed seed; tolerance stated in the test)
  - [ ] cluster CI on perfectly-correlated within-cluster data is **wider** than the naive (non-cluster) CI computed on the same points
  - [ ] the 0.90 CI nests inside the 0.95 CI on the same data and seed (`lo_95 <= lo_90` and `hi_90 <= hi_95`)
  - [ ] same `seed` → identical `(lo, hi)` across two calls
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- Downstream consumers fix the required behavior: T5's `cohens_kappa` uses cluster-bootstrap CIs where **all judgments of an email move together** (spec §5 — fields and candidates within an email are correlated); `eval run`/`compare` reports use **95%** BCa cluster CIs (spec §6); the gate summary uses a **90%** BCa CI (two-sided 90% ↔ the one-sided 5% test level, spec §7). Hence `level` must be a parameter, not a constant.
- `clusters` is a per-value cluster label sequence (same length as `values`); `clusters=None` is plain per-observation BCa.
- Pure function: no I/O, no config; determinism via the explicit `seed` parameter.
- All Global constraints from plan.md apply. TDD loop: failing test → minimal impl → green → `uv run ruff check` → commit.
