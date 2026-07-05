# T17 — Gate-design doc + no-change demonstration

**Phase:** C · **Depends on:** T10, T16 · **Owner gate:** no
**Sources:** plan.md task T17 · spec.md §7 · decisions.md D3

## Goal
Commit the analytic false-alarm justification for the gate (`docs/gate-design.md`) — the document every gate summary links to — and execute 10 no-change gate runs, recording the observed false-alarm count in a results table appended to the same doc.

## Deliverables
- `docs/gate-design.md` (created; results table appended there)

## Interfaces
**Consumes:**
- CLI `eval gate [--update-baseline|--seed-regression]` with exit codes 0/1/2 and committed `baselines/{a,b}.json` (T16)
- `render_gate_summary` (T10), which emits a relative link to `docs/gate-design.md` — this ticket creates the link target

**Produces:**
- `docs/gate-design.md` — the committed analytic justification + threat model + re-baseline procedure + 10-run no-change results table; linked from every gate summary and summarized in the README (T19)

## Acceptance criteria
- [ ] `docs/gate-design.md` exists and contains the analytic false-alarm justification with all four required elements: (1) the exact permutation level — full enumeration of all 2^m sign assignments when m ≤ 20 (truly exact), otherwise 10,000 Monte Carlo resamples with p̂ = (b+1)/(B+1), one-sided test at α = 0.05; (2) the conditional-on-frozen-baseline caveat — the per-run false-alarm rate is exact unconditionally but the demonstrated rate is a conditional quantity given the frozen baseline realization; (3) the K_baseline reasoning — K_baseline=6 vs K=3 keeps baseline noise small so the rate stays near-exact conditional on the frozen baseline; (4) the margin non-operativity note — at v1's n the 2.0-point margin is dominated by the significance condition and is honest bookkeeping, not a false-alarm mitigation.
- [ ] The doc contains the spec §7 threat-model statement: the gate protects against harness/scoring code changes, provider-side drift of served models, and judge drift; prompt changes are not gated automatically.
- [ ] The doc contains the prompt-change re-baseline PR procedure: run `eval gate --update-baseline`, and the PR must attach the compare-vs-old-baseline report for human review.
- [ ] The doc includes the sparse-delta disclosure consistent with spec §7 errata 2026-07-04: at m ≤ 4 no rejection is possible at α=0.05; at m = 5 rejection requires all five nonzero deltas to be regressions (min p = 0.031); and the two-candidate family false-alarm statement (two tests at α=0.05 → ≤ ~9.8% worst case).
- [ ] 10 no-change gate runs are executed locally with full keys (traced — Langfuse credentials present; gate runs are reportable and fail fast without them, spec §8), against the committed baselines with no code/prompt/data change. A results table with exactly 10 rows (run index, exit code, per-candidate p, mean delta) is present in `docs/gate-design.md`, plus the observed false-alarm count (number of runs with exit code 1).
- [ ] The T10 gate-summary link resolves: generate a gate summary (e.g. re-render a fixture or run `eval gate`), grep it for the relative link to `docs/gate-design.md`, and verify the file exists at the linked path from the repo root. Expected: link present, target file exists.
- [ ] `uv run pytest` and `uv run ruff check` pass (no code changes expected; the task still ends green per Global constraints).

## Notes
- Sequencing: requires T16's real committed baselines (K=6, traced) and a passing `eval gate` smoke run; requires T10's `render_gate_summary` link for the resolution check.
- The 10 no-change runs consume real API spend (~600 judge calls per gate run per spec §11) — run locally once, not in CI.
- The observed false-alarm count recorded here is summarized in the README by T19; together with the analytic justification this satisfies both branches of the spec §7 false-alarm demonstration (constitution DoD 2).
- Numbers used in the doc must match spec §7 verbatim: α = 0.05, margin 2.0, K=3, K_baseline=6, n=32 nominal items, adversarial guardrail ≥ 10 points (threshold verified ≥3× measured SE at baseline time), MDE expected order ~5–7 points at n=32.
- This is a documentation + measurement ticket, not a TDD code ticket; no new source modules.
