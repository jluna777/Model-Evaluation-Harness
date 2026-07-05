# T10 — Reports

**Phase:** A · **Depends on:** T01, T03, T04, T05, T08, T09 · **Owner gate:** no
**Sources:** plan.md task T10 · spec.md §5, §6, §7, §8

## Goal
Implement the three markdown renderers (run report, compare report, gate summary) plus the certificate/untraced banner rules, verified by golden-file tests on fixture runs.

## Deliverables
- `src/harness/reports.py`
- `tests/unit/` golden-file tests; fixture run artifacts + expected markdown under `tests/fixtures/`

## Interfaces
**Consumes (copied verbatim from plan.md):**
- `load_run(run_dir) -> RunArtifact` (T08); JSONL rows `{item_id, replicate, raw_output, raw_judge, field_scores, usage, served_model_version, judge_rationales}`
- `sign_flip_test(deltas, *, sided: Literal["one","two"], n_resamples=10_000, seed) -> PermutationResult{p, m_nonzero, method: "exact"|"monte_carlo", min_attainable_p}` (T03)
- `bca_ci(values, statistic=np.mean, *, level, clusters: Sequence[Hashable]|None, n_resamples=10_000, seed) -> (lo, hi)` (T04)
- `mde(delta_sd, n, *, alpha=0.05, power=0.80) -> float`; `variance_components(scores: item×replicate array) -> {between_item, between_replicate}` (T05)
- `Certificate` (T01); the `untraced` artifact flag (T09)

**Produces (copied verbatim from plan.md):**
- `render_run_report`: composite mean per slice (nominal/adversarial/all) with 95% BCa cluster CIs, per-field accuracies, per-category table, variance decomposition (full + judged-only), score-vs-length correlation per candidate;
- `render_compare_report`: mean delta + 95% BCa CI, two-sided permutation p, per-field pass-rate delta tables with flip counts (majority vote across replicates), absolute scores alongside deltas;
- `render_gate_summary`: verdict per candidate, delta + 90% BCa CI, one-sided p, m + sparse-delta warnings, MDE, judge-error exclusion count, adversarial delta (always) + guardrail status, family false-alarm rate line (two tests at α=0.05 → ≤ ~9.8% worst case), config values used (margin/alpha/K), token totals + approx cost from price snapshot, relative link to `docs/gate-design.md`;
- every report embeds the certificate header — including an explicit **"uncalibrated (no certificate)" state** for dev-stage runs, rendered as a banner and disallowed on reportable runs (T9 flag); untraced artifacts render the untraced banner.

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] golden-file comparison for `render_run_report` on a fixture run: rendered markdown is byte-identical to the committed expected file, and contains all five run-report elements listed above (all three slice groupings — nominal/adversarial/all — with 95% BCa cluster CIs, per-field accuracies, per-category table, full + judged-only variance decomposition, score-vs-length correlation)
  - [ ] golden-file comparison for `render_compare_report` on a fixture pair: byte-identical, containing mean delta + 95% BCa CI, two-sided permutation p, per-field pass-rate delta tables with flip counts (majority vote across replicates), and absolute scores alongside deltas
  - [ ] golden-file comparison for `render_gate_summary` on a fixture gate result: byte-identical, containing every element in the Produces list — including the literal family false-alarm line (two tests at α=0.05 → ≤ ~9.8% worst case), the config values used (margin 2.0 / alpha 0.05 / K=3 from the fixture config), token totals + approximate cost from the dated price snapshot, and a **relative** link to `docs/gate-design.md`
  - [ ] inadequate-certificate-verdict fixture → rendered report shows the judged-fields-excluded flag (DETERMINISTIC_5 mode surfaced)
  - [ ] missing-certificate fixture → rendered report shows the "uncalibrated (no certificate)" banner
  - [ ] rendering a **reportable** artifact with no certificate is disallowed (raises / refuses rather than rendering the banner)
  - [ ] fixture artifact with `untraced=True` → rendered report shows the untraced banner
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- All Global constraints from plan.md apply (referenced, not restated).
- CI level convention (spec §7): the gate summary uses a **90%** BCa CI (two-sided 90% ↔ the one-sided 5% test level); run and compare reports use **95%** (spec §6). Do not blur this.
- Certificate header content per spec §5: judge version, κ ± CI per candidate, verdict — embedded in every report.
- The compare report's per-field delta tables are descriptive counts only — no additional statistical tests (spec §6).
- Cost figures come from the dated price snapshot in `configs/default.yaml` and are labeled approximate-at-snapshot (spec §7).
- `docs/gate-design.md` does not exist yet (T17); emit the relative link now — T17 verifies it resolves.
- Renderers are pure functions over persisted artifacts: no client construction, no API calls (this is what makes T11's `rescore` and spec AC5 possible).
- TDD loop (Global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
