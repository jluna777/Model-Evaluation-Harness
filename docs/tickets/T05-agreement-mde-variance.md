# T05 — Agreement, MDE, variance decomposition

**Phase:** A · **Depends on:** T04 · **Owner gate:** no
**Sources:** plan.md task T5 · spec.md §5, §6, §7 · decisions.md D2, D3

## Goal
Implement the three remaining pure-function statistics modules: Cohen's kappa with cluster-bootstrap CI (judge–human agreement), minimum detectable effect matching the gate's one-sided convention, and between-item vs between-replicate variance decomposition.

## Deliverables
- `src/harness/stats/agreement.py` (created)
- `src/harness/stats/mde.py` (created)
- `src/harness/stats/variance.py` (created)
- `tests/unit/stats/test_agreement.py`, `tests/unit/stats/test_mde.py`, `tests/unit/stats/test_variance.py` (created)

## Interfaces
**Consumes:** (T04) `bca_ci(values, statistic=np.mean, *, level, clusters: Sequence[Hashable]|None, n_resamples=10_000, seed) -> (lo, hi)`; cluster mode resamples whole clusters.
**Produces:**
- `cohens_kappa(a, b, *, clusters=None) -> KappaResult{kappa, ci, raw_agreement, prevalence}`
- `mde(delta_sd, n, *, alpha=0.05, power=0.80) -> float` where **z_alpha is the one-sided (1−α) quantile, matching the gate's one-sided test**
- `variance_components(scores: item×replicate array) -> {between_item, between_replicate}` — callers run it twice: full composite and judged-fields-only (the spec §6 "judged-field run variance separated" requirement; report wiring in T10/T11)

## Acceptance criteria
- [ ] `cohens_kappa` matches `sklearn.metrics.cohen_kappa_score` on reference fixtures, including a skewed 90/10 prevalence fixture (test asserts equality within numerical tolerance)
- [ ] Perfect agreement fixture → κ = 1; statistically independent labels fixture → κ ≈ 0 (asserted within tolerance)
- [ ] `KappaResult.ci` is produced via cluster bootstrap (T04 `bca_ci` with `clusters` set) — a test with perfectly-correlated within-cluster judgments yields a wider CI than the unclustered CI on the same points
- [ ] `KappaResult` also carries `raw_agreement` and `prevalence` as descriptive context (values asserted on a fixture)
- [ ] `mde(12.0, 32) == 5.27 ± 0.01` (one-sided z at 1−α; a test documents that the two-sided quantile 1.960 would give 5.94 and must NOT be produced)
- [ ] `variance_components` recovers simulated between-item / between-replicate ratios on synthetic item×replicate arrays, including a judged-fields-only fixture
- [ ] `uv run pytest tests/unit/stats` exits 0
- [ ] `uv run ruff check` exits 0

## Notes
- TDD loop (global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
- Spec §5 constrains agreement statistics: Cohen's kappa is the single deciding agreement statistic; raw agreement and prevalence are descriptive only. All calibration CIs are **cluster bootstrap resampling emails** (all judgments of an email move together — fields and candidates within an email are correlated). D2 records the rationale.
- Spec §7 constrains MDE: computed from observed delta variance, printed on every gate run; the one-sided convention must match the gate's one-sided sign-flip test (D3). Expected order at n=32 is ~5–7 points — the 5.27 anchor sits inside that band.
- Spec §6 constrains variance decomposition: between-item vs between-replicate components, with judged-field run variance separated — informing the future K decision (D3). This module is pure computation; T10 (reports) and T11 (CLI) wire the two invocations.
- Downstream consumers: T10 `render_run_report` (variance decomposition, agreement display), T14 `eval calibrate` (kappa + CIs + prevalence), T16 gate summary (MDE line). Keep signatures exactly as above.
- `sklearn` may be a test-only (dev) dependency used as reference implementation; the runtime implementation must not require it.
