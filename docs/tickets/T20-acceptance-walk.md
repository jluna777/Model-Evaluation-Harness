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
- [x] **AC1:** `uv run eval compare` runs end-to-end on the frozen golden set — both candidates, judge scoring, paired report with CIs, **two-sided** permutation p, per-field delta tables — with the calibration certificate embedded in the header and Langfuse traces present. Evidence: command, report excerpt showing certificate header + two-sided p + delta tables, trace id/link.
- [x] **AC2:** `uv run eval calibrate` produces the D2 report and committed certificate — per-candidate kappas with cluster-bootstrap CIs, prevalence, verdict — and, once both annotators' labels are committed, resolves dual-annotation gold and adds the human-human agreement ceiling row automatically (D2 amendment 2026-07-09, no flag). Evidence: the command + report excerpt showing the certificate fields, the ceiling row, and the adjudicated-disagreement count.
- [x] **AC3:** `eval gate` in GitHub Actions passes on the unchanged committed baseline (exit 0, green check); `workflow_dispatch --seed-regression` fails with the DEMO MODE banner; the exit-code contract (0/1/2) drives distinct CI outcomes (exit 1 and exit 2 render with different labels). The false-alarm rate is justified analytically in `docs/gate-design.md`, linked from every gate summary, and the 10 documented no-change runs with the observed false-alarm count appear in the README. Evidence: Actions run links for the passing run and the demo run, the summary link resolving to `docs/gate-design.md`, README excerpt with the 10-run count.
- [x] **AC4:** the README shows the before/after with real numbers — composite scores ± CI, judge–human agreement ± CI (with the human-human ceiling row), gate verdicts, printed MDE, and the sparse-delta count m. Evidence: README excerpt naming each element.
- [x] **AC5:** every number in the README is exactly recomputable from committed artifacts with **zero API calls** — run numbers via `uv run eval rescore results/published/<run>` (+ baselines), agreement numbers via `uv run eval calibrate` / `uv run eval calibrate --offline` on committed labels. Evidence: rescore output byte-identical to the published report (e.g. diff/hash comparison), calibrate output matching README agreement numbers, confirmation no API client was constructed (T11 mechanism).
- [x] Every failed criterion is looped back to its owning task (AC1→T11, AC2→T14, AC3→T16–T19, AC4/AC5→T19 per the plan's spec-coverage map) and re-walked after the fix; this ticket records the loop.
- [x] `uv run pytest` and `uv run ruff check` pass on the final shipped state. (2026-07-20, post-review fix wave: 677 passed, 0 warnings, ruff clean; all four zero-API recomputes re-verified byte-exact on the same state — rescore ×2, compare-reuse, certificate.)

## Notes
- Sequencing: last ticket; runs only after T19's owner gate (README validated) — the walk is against the shipped repo, not a work-in-progress.
- Evidence must be concrete: exact commands, exit codes, output excerpts, and run/trace links pasted into this ticket's Evidence section. No criterion is checked on assertion alone.
- AC1's compare and AC3's passing gate run are live, traced runs (Langfuse keys required — reportable per spec §8); AC5's recomputation checks are strictly offline (zero API calls) — keep the two modes distinct in the evidence, per the spec's recomputation-vs-re-execution distinction.
- Completion closes the SDD loop (constitution DoD); no new code is written here — any failure is fixed in the owning task's scope, keeping its own green-test discipline.

## Evidence (2026-07-20)

**AC1 — `eval compare` end-to-end, traced.** Ran live 2026-07-19 (T19,
owner-approved): `uv run eval compare` → exit 0, report
`results/published/a-927b2dc82a761b18__b-b9cab21e8fb1c83a.md`. The report
carries the certificate header ("Overall κ = 0.749 (95% CI [0.543,
0.886])" / "Verdict: **adequate**"), the two-sided statistic ("Two-sided
sign-flip permutation p = 0.3179 (m = 26 nonzero deltas, monte_carlo, min
attainable p = 0.0001)" — as republished 2026-07-20 under the tie-counting
fix; the run's original report said 0.3066, see the review section below),
the paired mean delta with BCa CI (+1.33 [−1.05, 3.71]), and the
seven-row per-field delta table. Traces present:
Langfuse trace ids `f015ef8124103b99e415bb5681712ae5` (candidate a, first
span 2026-07-19T21:19:38Z) and `6ae86805242518b61daca304a79ced40`
(candidate b, 21:25:49Z), each with the expected 150 candidate + 300
judge spans — enumerated in the committed
`results/published/latency-export.json`.

**AC2 — `eval calibrate` report + certificate.** Live certification ran
2026-07-16 (T14, owner-signed; evidence in that ticket). Walked here via
the zero-API path against the committed labels/judgments:
`uv run eval calibrate --offline` → exit 0. Report excerpt: "Overall
Cohen's κ = 0.749 (95% cluster-bootstrap CI [0.543, 0.886])"; per-candidate
rows "candidate a: κ = 0.668 (95% CI [0.420, 0.853]) ... candidate b:
κ = 0.845 (95% CI [0.639, 0.962])" with prevalence 77.1%; verdict
**adequate**; the automatic ceiling row "Inter-annotator κ ... = 0.734
(95% cluster-bootstrap CI [0.575, 0.854]) — the human-human agreement
ceiling"; "Adjudicated disagreements: 13."; probe disclosures
(n_perturbed 31, achieved fail prevalence 22.9%, real-only κ 0.000
degenerate-disclosed).

**AC3 — CI gate outcomes (T18 live evidence, links recorded there in
full).** Passing run on the unchanged baseline: Actions run 29700593699
(PR #2), exit 0, green "PASS: gate result" step, ran twice (both pass).
Seed-regression demo: run 29702596084, DEMO MODE banner at report head
and foot, exit 1 → failing step "FAIL: regression detected". Exit-2
demo: run 29703649212 (temporarily absent Langfuse key), exit 2 in 17s →
failing step "FAIL: measurement error (gate could not measure)" —
labels observed distinct in the step list and annotations. Every gate
summary links the analytic false-alarm document ("See
[docs/gate-design.md](docs/gate-design.md) for the analytic false-alarm
justification..." — rendered in the CI job summary/PR comment; target
file exists at that repo-root-relative path). README "The gate fails
only on defensible evidence" section carries "**Observed false alarms:
0/10** no-change runs (all exact-mode, m 4–14)" linking gate-design §8.

**AC4 — README before/after elements, by name.** Headline table:
composite ± CI both candidates (93.24 [90.48, 95.43] / 94.57 [92.48,
96.29]) plus nominal/adversarial slices; judge–human κ ± CI per candidate
with the ceiling row (0.734 [0.575, 0.854]) in the table and calibration
section; gate verdicts ("Observed false alarms: 0/10 ... All 10 runs
exited 0"); printed MDE ("observed 0.50–2.13 points across the no-change
campaign"; "the printed MDE is the per-run source of truth"); sparse-delta
m ("m = 4–6 on all 10 no-change runs", "m 4–14" families, sparse-delta
warnings described in the gate section).

**AC5 — zero-API recomputation, executed fresh for this walk
(2026-07-20).**
- Run numbers: `uv run eval rescore results/published/runs/<run>` for
  both runs → sha256 of fresh output == sha256 of committed report:
  `1466c8e4...` (a) and `5d384c66...` (b); `cmp` clean on both. The
  rescore command constructs no API client (T11 finding-F2 mechanism —
  `load_run` + pure render only).
- Comparison numbers: `uv run eval compare` → reused both published runs
  by identity, zero API calls, regenerated report `cmp`-identical to the
  committed `results/published/a-927b2dc82a761b18__b-b9cab21e8fb1c83a.md`.
- Agreement numbers: `uv run eval calibrate --offline` → certificate
  regenerated over the committed labels + judgments; `git diff
  --exit-code data/calibration/certificate.json` clean — the certificate
  file recomputes byte-identically as a whole (post the 2026-07-20
  `is_probe` repair and labels-derived-date ruling, `bd507f9`).
- Recomputation-vs-re-execution kept distinct throughout: the walk's AC5
  commands are all offline recomputes; AC1/AC3's live runs are the traced
  executions recorded above.

**Loop-backs.** One criterion failed during the T19 build and was looped
back per the map: AC5's calibrate leg failed on the committed
`judgments.jsonl` (`is_probe` mis-stamped by the 2026-07-16 recert
script → `PerturbationOverlayError`). Owned by the T14/T19 artifact
chain; repaired byte-surgically with owner sign-off (`bd507f9`,
2026-07-20), then this walk re-ran the criterion clean. No other
criterion failed.

**Final gates (2026-07-20, post-review fix wave):** `uv run pytest -q` →
677 passed, 3 deselected (live), 0 warnings; `uv run ruff check .` clean.
Zero-API recompute sweep on the identical state: both published run
reports rescore byte-exact, the republished compare report reproduces
byte-exact via compare-reuse, and `eval calibrate --offline` leaves the
committed certificate byte-for-byte unchanged.

## Ledger minors triage (2026-07-20)

Every "minor"/"triage" item accumulated in the build ledger, disposed.
A large tranche was already cleared mid-project by the dedicated minors
wave (`fb7c86f`, 2026-07-09: output hygiene to zero warnings,
`stats/_normal.py` promotion, judge-error-count dedup, gate scratch-dir
cleanup, tracing-vs-abort test, BCa message + tests, sided fail-fast,
schema parity, CLI `exists=True`/annotation/dead-`main()`, contentless
refusal fixture, MissingTracingError-before-construction test,
live-vs-offline full-field certificate test, stacklevel) and by later
work (Gemini injected-client docstring `0f259b9`; `cli.py` duplicate
`resolve_gold_labels` comment; Langfuse alias note in the README
`b7fba4a`; variance ddof=0 documented on every run report).

**Fixed in the T20 fix wave (mechanical, this ticket):**
1. Calibrate report: missing blank line merges "Adjudicated
   disagreements: N." into the ceiling paragraph when rendered
   (verified live against the offline report output).
2. Calibrate report: the ceiling sentence lacks the owner-anchored-gold
   caveat the README carries — one clause.
3. `tests/unit/test_calibrate.py` module docstring still says
   "test-retest ceiling" (dual annotation replaced test-retest,
   D2 2026-07-09).
4. `docs/decisions.md` D1/D2 Status lines don't enumerate the
   2026-07-16b/c/d amendments (log hygiene, additive).
5. T18 evidence honesty note, recorded here instead of a code change:
   the workflow's actionlint review ran without shellcheck installed, so
   its run-block shell was lint-checked structurally but not
   shellcheck-analyzed.

**Declined (reason recorded, no action):** transient int64 array at
m=20 (~168 MB peak — bounded by the m ≤ 20 exact ceiling, runs on dev
machines only); per-iteration Python bootstrap loops (disclosed perf
trade, n is small); observed-statistic-before-cluster-validation
ordering nit (both paths raise identically); worker try/except tail
timing (outer guard catches, verified at T08 review); real Langfuse
client construction untested (no keys in CI by design, accepted at T09);
duplicate-row / degenerate-ceiling raw tracebacks (loud failure on
malformed inputs is acceptable v1 behavior; clean-exit wrappers are
cosmetic).

**Post-v1 backlog (engineering):** certificate-freshness check
(`certificate.judge_version` vs current `judge_version()`) at
gate/`--update-baseline` time; `.staging` cross-invocation locking;
cross-process run-dir locking (docstring caveat); run-id threading into
trace ids (retires the latency export's positional binding); a CLI
pinned-runs re-certification mode (replaces the recert-script pattern,
including its date-override foot-gun); workflow-level stderr/report
split (tracing-degradation noise in PR comments); terseness few-shot +
judge re-certification (booked, success metric κ_a → κ_b);
`APIResponseValidationError` classification + Anthropic
`max_tokens=1024` revisit; `_require_api_key` alternate SDK credential
paths; `RunArtifact.model_key` Literal round-trip; negative-path schema
tests (EmailInput/CalibrationLabel/Certificate); `load_config`
contextual error wrapping; field-name partition shared between
composite/deterministic; symmetric-marginals κ test pattern;
`_item_replicate_matrix` fallback test; manifest-missing-model-key
test; `--model` + `--keep-runs` interaction test; coverage-headline
annotator attribution; blanket type-ignore at `tracing.py:211`.

## Final whole-branch review (2026-07-20)

Run per the standing process ruling: one reviewer on the most capable
model, superpowers code-reviewer template, base = the repo's first commit
(`686a5a4`), head = `b7fba4a` (99 commits). The reviewer independently
re-verified all four zero-API byte-exact recomputes, recomputed the README
numbers from committed rows (all true), and security-scanned committed
files (clean; the second annotator appears only as a role string).
Verdict: **ready with fixes**. Findings and dispositions:

- **C1 (Critical, verified with constructed counterexamples): exact-mode
  sign-flip tie mis-counting.** The observed statistic was computed via
  `ndarray.mean()` while resampled statistics used `signs @ nonzero` — a
  last-ulp mismatch that could exclude the observed value's own tie block
  from the extreme count on the gate's quantized deltas. Demonstrated
  consequences: p-values below the test's own `min_attainable_p` (e.g.
  p = 0.0 where the floor is 0.0625) and above-margin m=4 configurations
  that would FAIL the gate where every governing doc guarantees rejection
  is impossible — the anti-conservative direction. **Fixed** (TDD: the
  reviewer's counterexamples as failing tests first): the observed
  statistic now goes through the identical dot-product path, and
  `_extreme_mask` carries a scipy-style relative tolerance (1e-14,
  ~13 orders of magnitude below the delta quantum);
  `TestTieBlockCounting` pins both counterexamples plus a
  p ≥ min_attainable_p property over tied quantized configurations.
  **Blast radius, measured:** no committed verdict changes. The published
  comparison's Monte-Carlo p moved 0.3066 → 0.3179 (113/10,000 resamples
  were tied with the observed statistic and had been dropped) — report
  republished, README updated (0.31 → 0.32), still not significant.
  Gate-design §8's recorded p-values are lower bounds on their corrected
  values (the fix can only raise p), so the 0/10 no-change result stands
  a fortiori — errata added to §8.
- **I1 (Important, verified): the judge's alias-drift guard is inert** —
  the provider echoes the requested alias, so `served_versions.judge`
  records `gemini-3-flash-preview` itself and a silent re-point would not
  trip the fingerprint. **Fixed as a wording scope** in gate-design §1 and
  the README (automatic drift detection is real for both candidates,
  whose served IDs are dated snapshots; the judge's guard is the recorded
  pin-time snapshot + the booked re-certification). Stamping a dated
  judge snapshot into `served_versions` via `models.get` at client
  construction is booked post-v1 (it would re-fingerprint and force a
  re-baseline, so it rides with the re-certification).
- **I2 (Important, verified): real-only κ = 0.0 is structurally forced**
  (constant gold marginal → κ algebraically 0 regardless of judge
  behavior) but was rendered with informative-kappa framing, and the
  README named the wrong population (109 unperturbed rows vs the 100
  non-probe rows the computation actually runs on). **Fixed:** the
  calibrate renderer now detects a single-category gold marginal and
  emits the structural-zero disclosure (with raw agreement, 88.0%, and
  the miss direction as the meaningful numbers); README bullet corrected
  to "100 real (non-probe) rows" with the algebraic-zero framing.
- **M1 (minor): no judge-error-budget check at baseline generation time**
  → post-v1 backlog (below); committed baselines verified clean (0
  missing fields in 600 rows). **M2 (minor): D1/D2 Status amendment lists
  stale** → already fixed in this ticket's triage wave. **M3 (minor):
  §8 p-values inherit C1's tie noise** → errata added (see C1).
- Reviewer recommendation adopted: a tie-convention note on the BCa
  `z0` computation (Efron strict-`<`) in `bootstrap.py`'s docstring.

**Post-v1 backlog additions from the review:** dated judge snapshot into
`served_versions` (I1 mechanical fix, rides with re-certification);
judge-error-budget check at `--update-baseline` time and symmetrically at
baseline load (M1 / reviewer rec 3).

**Owner queue (spec/docs wording — D-territory, not editable in code):**
spec §7 `min_attainable_p` sided-floor sentence (one-sided 2^−m,
two-sided 2^(1−m)); spec.md:46 "candidate B resolved ID recorded in
`configs/default.yaml`" (config holds the bare alias; dated served IDs
live in rows + baseline fingerprints); spec.md:77–78 labels-row
enumeration (`output_sha256`) and two-annotators wording; spec AC2/§9
dual-annotation phrasing vs the code's hard precondition; spec §5 error
vocabulary (DualAnnotationError/CalibrationBindingError); decisions.md
D2 consumed "Revisit if" trigger; gemini-3-flash pricing citation
provenance (web-sourced 0.50/3.00); golden-021 "same/next-day not
affirmatively established → high" clarifying note (dataset v2);
pronoun/no-gender-inference rubric rule (booked next rubric cycle, with
golden-019 as the worked example).
