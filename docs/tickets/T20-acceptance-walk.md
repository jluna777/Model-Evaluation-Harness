# T20 — Acceptance walk

**Phase:** D · **Depends on:** T19 (and transitively all T1–T18) · **Owner gate:** no
**Sources:** plan.md task T20 · spec.md §12 (AC1–AC5)

## Goal
Walk all five spec §12 acceptance criteria against the shipped repo, recording evidence (commands + outputs) in this ticket; completion closes the SDD loop (constitution DoD).

## Deliverables
- `docs/tickets/T20-acceptance-walk.md` (this file, modified: an Evidence section appended with the command transcript/output excerpt for each criterion below)

## Interfaces
**Consumes:**
- The shipped repo end-to-end: `eval run | compare | gate | calibrate | rescore` (T11/T14/T16), `.github/workflows/eval-gate.yml` (T18), `README.md` + `results/published/` (T19), `docs/gate-design.md` (T17), committed `baselines/{a,b}.json` (T16), `data/calibration/certificate.json` (T14)

**Produces:**
- Recorded acceptance evidence in this ticket — no code or artifacts beyond the evidence record

## Acceptance criteria
- [ ] **AC1:** `uv run eval compare` runs end-to-end on the frozen golden set — both candidates, judge scoring, paired report with CIs, **two-sided** permutation p, per-field delta tables — with the calibration certificate embedded in the header and Langfuse traces present. Evidence: command, report excerpt showing certificate header + two-sided p + delta tables, trace id/link.
- [ ] **AC2:** `uv run eval calibrate` produces the D2 report and committed certificate — per-candidate kappas with cluster-bootstrap CIs, prevalence, verdict — and `uv run eval calibrate --retest` adds the intra-annotator ceiling row. Evidence: both commands + report excerpts showing the certificate fields and the ceiling row.
- [ ] **AC3:** `eval gate` in GitHub Actions passes on the unchanged committed baseline (exit 0, green check); `workflow_dispatch --seed-regression` fails with the DEMO MODE banner; the exit-code contract (0/1/2) drives distinct CI outcomes (exit 1 and exit 2 render with different labels). The false-alarm rate is justified analytically in `docs/gate-design.md`, linked from every gate summary, and the 10 documented no-change runs with the observed false-alarm count appear in the README. Evidence: Actions run links for the passing run and the demo run, the summary link resolving to `docs/gate-design.md`, README excerpt with the 10-run count.
- [ ] **AC4:** the README shows the before/after with real numbers — composite scores ± CI, judge–human agreement ± CI (with the ceiling row), gate verdicts, printed MDE, and the sparse-delta count m. Evidence: README excerpt naming each element.
- [ ] **AC5:** every number in the README is exactly recomputable from committed artifacts with **zero API calls** — run numbers via `uv run eval rescore results/published/<run>` (+ baselines), agreement numbers via `uv run eval calibrate [--retest]` on committed labels. Evidence: rescore output byte-identical to the published report (e.g. diff/hash comparison), calibrate output matching README agreement numbers, confirmation no API client was constructed (T11 mechanism).
- [ ] Every failed criterion is looped back to its owning task (AC1→T11, AC2→T14, AC3→T16–T19, AC4/AC5→T19 per the plan's spec-coverage map) and re-walked after the fix; this ticket records the loop.
- [ ] `uv run pytest` and `uv run ruff check` pass on the final shipped state.

## Notes
- Sequencing: last ticket; runs only after T19's owner gate (README validated) — the walk is against the shipped repo, not a work-in-progress.
- Evidence must be concrete: exact commands, exit codes, output excerpts, and run/trace links pasted into this ticket's Evidence section. No criterion is checked on assertion alone.
- AC1's compare and AC3's passing gate run are live, traced runs (Langfuse keys required — reportable per spec §8); AC5's recomputation checks are strictly offline (zero API calls) — keep the two modes distinct in the evidence, per the spec's recomputation-vs-re-execution distinction.
- Completion closes the SDD loop (constitution DoD); no new code is written here — any failure is fixed in the owning task's scope, keeping its own green-test discipline.
