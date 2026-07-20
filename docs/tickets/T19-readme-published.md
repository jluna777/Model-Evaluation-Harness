# T19 — README, published artifacts, demo

**Phase:** D · **Depends on:** T11, T14, T16, T17, T18 · **Owner gate:** yes ◆
**Sources:** plan.md task T19 · spec.md §5, §10, §12 (AC3/AC4/AC5) · decisions.md D2

**Amended 2026-07-09 (owner):** dual-annotation upgrade (decisions.md D2 amendment 2026-07-09) removes this ticket's original ≥1-week test-retest entry criterion. The entry criterion simplifies to "the calibration certificate exists" — dual-annotation gold resolution and the human-human ceiling are computed automatically by `eval calibrate` whenever both annotators' labels are committed, with no calendar gap to wait out.

## Goal
Publish the before/after story: a README with real numbers, every one of which is backed by a committed artifact in `results/published/` and exactly recomputable with zero API calls.

## Deliverables
- `README.md` (created/replaced)
- `results/published/` (committed run artifacts for every README number)
- `.gitignore` exception (`results/` gitignored EXCEPT `results/published/`, per spec §10)

## Interfaces
**Consumes:**
- `eval rescore <run-dir>` — recompute all scores/statistics/reports from persisted raw outputs, zero API calls (T11)
- `eval calibrate` — agreement report + certificate from committed labels; resolves dual-annotation gold and adds the human-human agreement ceiling row automatically once both annotators' labels are complete (T14, D2 amendment 2026-07-09)
- Gate verdicts and committed `baselines/{a,b}.json` (T16)
- 10-run no-change table + observed false-alarm count in `docs/gate-design.md` (T17)
- CI workflow `.github/workflows/eval-gate.yml` (T18, described in quickstart/demo)

**Produces:**
- `README.md` + `results/published/` — the artifacts T20 walks for AC3/AC4/AC5

## Acceptance criteria
- [x] **Entry criterion (simplified, D2 amendment 2026-07-09): the calibration certificate exists** — `data/calibration/certificate.json` is committed, produced by `eval calibrate` from both annotators' complete `round: "initial"` labels (+ any owner adjudication rows), with the human-human ceiling row (`ceiling_kappa`) present. No ≥1-week calendar gap to schedule or wait out.
- [x] README shows the before/after with real numbers (spec AC4): composite scores ± CI for both candidates, judge–human agreement ± CI with the human-human ceiling row, gate verdicts, printed MDE, and the sparse-delta count m.
- [x] README contains the 10-run no-change table summary with the observed false-alarm count, linking `docs/gate-design.md` (spec AC3 / constitution DoD 2).
- [x] README contains a quickstart and the documented two-command demo: `eval gate` (passes against the committed baseline) then `eval gate --seed-regression` (fails, DEMO MODE banner); in CI the demo runs via `workflow_dispatch`.
- [x] README contains an honest-limitations section naming at least: dual annotation's own limits (the ceiling only measures task ambiguity if both annotators applied the same written conventions; adjudication is owner-sourced, so it is not a fully independent third check), n (50 golden / 32 nominal), MDE coarseness (~5–7 points at n=32; the printed MDE is the source of truth), and alias pinning (candidate B resolved ID recorded in `configs/default.yaml`).
- [x] `results/published/` contains a committed artifact for every README number; `.gitignore` keeps `results/` ignored with the `results/published/` exception in place (verifiable: `git check-ignore results/scratch` succeeds, `git check-ignore results/published/<file>` fails).
- [x] AC5 recomputability, run numbers: `uv run eval rescore results/published/<run>` reproduces the published report byte-exact with zero API calls (no client construction — the T11 anchor's mechanism).
- [x] AC5 recomputability, agreement numbers: `uv run eval calibrate` and `uv run eval calibrate --offline` on the committed labels/judgments reproduce the README agreement numbers ± CI, the human-human ceiling row, and `n_adjudicated` identically, with zero API calls.
- [x] ◆ Owner validates README (reviews the numbers, limitations section, and demo instructions and signs off). Signed 2026-07-20, in session, together with the `judgments.jsonl` `is_probe` repair and the certificate-date ruling (labels-derived, option B).

## Notes
- Sequencing: no longer blocked on a calendar gap (D2 amendment 2026-07-09 removed the retired test-retest wait) — README numbers can start as soon as T14's certificate exists with both annotators' labels resolved.
- Owner work: review the README (~30 min). (T14 already covers the owner's labeling/adjudication effort.)
- Published runs are reportable: they must be traced (Langfuse keys required, fail fast without — spec §8); untraced artifacts can never feed the README.
- Spec AC5 errata: recomputation (zero API calls, byte-exact) is the contract; re-execution against live APIs is expected to differ within reported CIs — the README/limitations should carry this distinction.
- Golden and calibration items are never used for prompt tuning (Global constraints); the README numbers come from the frozen `dataset_version: 1` set.
- Documentation + publication ticket; any code touched (e.g. `.gitignore`, report tweaks) still ends green: `uv run pytest` and `uv run ruff check` pass before commit.

## Evidence (2026-07-20)

**Published comparison (the README's headline artifact).** One `eval
compare` run 2026-07-19 (owner-approved that day): 2 candidates × 50 items
× K=3, exit 0, $0.78 total at the committed price snapshot. Composite a =
93.24 [90.48, 95.43], b = 94.57 [92.48, 96.29]; paired delta (b − a) =
+1.33 [−1.05, 3.71], two-sided sign-flip p = 0.3066 (m = 26, Monte Carlo)
— not significant; the README frames the result as a per-field trade-off,
not a winner. Artifacts: `results/published/runs/{a-927b2dc82a761b18,
b-b9cab21e8fb1c83a}` (manifests + raw rows), the compare report, and both
rescored run reports.

**Recompute verification (AC5).** `eval rescore` on each published run dir
reproduced its committed run report **byte-identically** (verified twice:
build session and independent review, `cmp` clean, zero API calls).
Re-invoking `eval compare` reused both published runs by identity — 10s
wall-clock, zero API calls — and reproduced the committed comparison
report byte-identically. `.gitattributes` marks `results/published/**
-text` so checkout-time CRLF normalization can never silently break these
comparisons. `git check-ignore` verified both directions of the
`results/` / `results/published/` split.

**`eval calibrate --offline` repair (found by the T19 review).** The
review discovered the committed `judgments.jsonl` carried `is_probe:
false` on all 140 judgment rows — the 2026-07-16 re-certification ran
through the pinned-runs script (ledger 2026-07-16), which stamped the flag
wrong on the 40 fail-probe rows; the certificate itself was computed from
the correct in-memory probe attribution, but the offline path re-derives
probe membership from the persisted flags and died on
`PerturbationOverlayError`. Repair: the 40 rows on `cal-026`..`cal-035`
were flipped to `is_probe: true` by a byte-surgical edit (no other bytes
touched). After the repair, `eval calibrate --offline` runs clean and
reproduces **every measured certificate field byte-identically** (κ, all
CIs, per-candidate, ceiling, n_perturbed, prevalence, real-only κ,
label_file_hash — and the same 2/10000 NaN-replicate bootstrap
disclosure). The single non-measured difference: the certificate `date`
field — the committed value (2026-07-16) came from the recert script's
explicit override, while `resolve_certificate_date`'s own contract derives
it from the newest label (2026-07-15). **Owner ruling 2026-07-20:** the
labels-derived date stands (the field's documented wall-clock-independent
contract); the certificate was regenerated through `eval calibrate
--offline` itself and now recomputes **byte-exactly as a whole file**,
verified idempotent across two consecutive offline runs.

**Latency (owner-selected mechanism: API script).** `tools/pull_latency.py`
pulled the comparison window from the Langfuse public API:
`results/published/latency-export.json`, 900 observations in exactly 2
traces (the expected 150 candidate + 300 judge spans per run). Candidate a
p50 1.6s / p95 3.0s; candidate b p50 1.4s / p95 3.1s; judge p50 2.2–6.2s /
p95 9.7–16.0s across the two runs, worst call 103.7s (spans include
transport retries). Trace binding is positional (documented in the tool
and the export; run-id threading into trace ids is the booked post-v1
fix). Latency is a disclosed one-time pull, outside the zero-API recompute
contract.

**Facts previously recorded only in the session ledger, given a committed
home here (2026-07-20):** (1) three of the 40 fail-probe supplement rows'
perturbed values (cal-027/a/requested_action, cal-028/b/requested_action,
cal-030/b/issue_summary) were visible in a session transcript before owner
labeling; the owner reported not having read that output, and the one
exposed row discussed during adjudication had its gold outcome unchanged
(the owner's independent initial verdict already matched). (2) The
measurement campaign hit three provider billing/quota incidents (Gemini
prepaid-credit depletion 2026-07-10, Gemini tier-1 daily-quota wall
2026-07-16, Anthropic billing depletion during T17) — every one surfaced
as a loud exit-2 measurement error; none produced a wrong verdict.

**Review.** Lean single-reviewer pass over README + tool + published
artifacts: every README number independently re-verified against its
backing artifact (full coverage list in the review record); three
substantive findings (the `is_probe` defect above; a backwards
judge-vs-candidate cost-share claim that also lived in
`reports.py`'s gate-summary renderer, corrected with its pinned test
fixture; an MDE range digit 2.16 → 2.13) all fixed before owner review.

**Addendum (2026-07-20, from the T20 final review):** the whole-branch
review found a floating-point tie-counting defect in exact/MC
`sign_flip_test` extremeness masks (T20 ticket, finding C1). Under the
fixed code the published comparison's Monte-Carlo p recomputes to 0.3179
(was 0.3066; 113/10,000 resamples were mathematically tied with the
observed statistic and had been dropped from the count). The published
compare report was regenerated under the fixed code and the README's
paired-comparison line updated (p = 0.31 → 0.32); the delta, CI, and the
not-significant conclusion are unchanged. This section's 0.3066 is left
as written — it was the artifact's value at signing.
