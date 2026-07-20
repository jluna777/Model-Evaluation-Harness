# Gate design: the analytic false-alarm justification

This document is the analytic half of the spec §7 false-alarm demonstration
(constitution DoD 2); the other half is the "Results" section at the bottom
(§8), completed 2026-07-17: 10 local no-change runs, 0 observed false
alarms. Linked from every `eval gate` summary (`render_gate_summary`,
`src/harness/reports.py`).

## 1. Threat model

The gate protects against (spec §7, D3):

- **Harness/scoring code changes** — regressions in scoring, judge-calling,
  or composite computation.
- **Provider-side model drift** — every run records the served model
  version in its fingerprint (`served_versions`); a mismatch vs baseline is
  a fingerprint mismatch (exit 2). Scope caveat (2026-07-20): this
  automatic guard is only as good as what the provider echoes. Both
  candidates resolve to dated snapshots (`claude-haiku-4-5-20251001`,
  `gpt-5.4-mini-2026-03-17`), so their drift is caught; the judge's
  provider echoes the requested alias itself (`gemini-3-flash-preview`),
  so a silent re-point of that preview alias would NOT trip the
  fingerprint. The judge's drift guard is instead the dated snapshot
  recorded at pin time (`3-flash-preview-12-2025`, decisions.md D1
  2026-07-16c — re-verifiable via `models.get`) plus the booked post-v1
  re-certification; stamping a dated judge snapshot into
  `served_versions` is the booked mechanical fix.
- **Judge drift** — a change to judge model/prompt/rubric/few-shots changes
  `judge_version`, also a fingerprint component.

It deliberately does **not** gate:

- **Prompt changes** — `prompt_version` is a fingerprint component like the
  others, but a prompt change is never adjudicated by the statistical rule:
  any prompt bump forces a fingerprint mismatch (exit 2), and that mismatch
  is the mechanism that routes the operator to a human-reviewed
  `--update-baseline` (§6) — not an automatic pass/fail.
- **Fine-grained adversarial-slice regressions** — the statistical rule
  (§2) runs on the nominal slice only. The coarse guardrail (§4) is the
  adversarial slice's sole protection, by design: it closes the
  injection-category blind spot without extending permutation-test
  machinery to a slice too small/heterogeneous to support it.

## 2. Decision rule and false-alarm control

**Statistic.** One-sided sign-flip permutation test on K-averaged (K=3)
paired per-item composite deltas (`current - baseline`), nominal slice,
n=32 items nominally (`sign_flip_test(..., sided="one")`). Extreme means a
resampled mean ≤ the observed mean — the regression direction only.

**Exactness.** Full enumeration of all `2**m` sign assignments over the `m`
nonzero deltas when `m ≤ 20` (truly exact). Above that, 10,000 seeded Monte
Carlo sign resamples with the bias-corrected `p̂ = (b + 1) / (B + 1)` (never
zero). At n=32 with a healthy delta distribution, exact mode is expected — a prediction, not a guarantee; the no-change runs (§8) will show the observed m values.

**Fail condition.** `p < α AND mean_regression > margin`, α = 0.05,
margin = 2.0 points (`configs/default.yaml`, `decide_candidate_result`).
The margin is AND-ed on, never a substitute for significance.

**Margin's honest status.** At v1's n the margin does no independent
false-alarm-control work: a mean regression significant at n=32 already
exceeds 2.0 points (D3 amendment 2026-07-04a withdraws the earlier
mitigation claim). It stays as an explicit practical floor that only binds
if n grows or variance shrinks. (An owner-considered non-inferiority
framing, H0: regression ≤ margin, would make it do real work at the cost
of a blunter gate; v1 does not adopt it.)

**Per-run α:** 0.05, one-sided, per candidate.

**Family rate.** The gate fails if *either* candidate trips (spec §7).
Treating the two tests as independent: `1 - (1 - 0.05)² ≈ 0.0975` — printed
as "≤ ~9.8% worst case." The two candidates share judge and dataset, so
true independence isn't guaranteed; the assumption-free union bound without
it is ≤ 2α = 0.10 — the ~10% order of magnitude holds either way.

**Sparse-delta resolution limits.** Minimum attainable one-sided p at `m`
nonzero deltas is `2**-m`. At `m ≤ 4`, `2**-4 = 0.0625 > 0.05`: no
rejection is possible at α=0.05 regardless of regression size. At `m = 5`,
`2**-5 = 0.03125 < 0.05`, attained only when all five nonzero deltas share
the regression sign — rejection at m=5 requires every nonzero delta to be a
regression. Printed whenever `m < 6` (*errata 2026-07-04*: original spec
wording claimed impossibility for all m < 6, false at m = 5).

## 3. Conditional-on-frozen-baseline caveat

The per-run α is **exact unconditionally** (averaged over all possible
baseline realizations). It is only **approximate conditional on the one
frozen baseline** actually committed, since that baseline is itself a
noisy K-replicate measurement.

`K_baseline = 6 > K_run = 3` is the mitigation: more baseline replicates
keep baseline noise small relative to a gate run's, keeping the conditional
rate close to unconditional α (D3 amendment 2026-07-04a). This is a
qualitative "keeps it near-exact" argument, not a derived numerical bound.

The 10 no-change runs executed in §8 are therefore a **conditional check
against the one committed baseline**, not 10 independent α draws — they
share the same frozen baseline noise, so outcomes are correlated, not
i.i.d. Bernoulli(α) trials. The observed **0/10** false-alarm count in §8
should be read accordingly: it **supports**, rather than **proves**, the
per-run α claim above — one correlated data point sitting alongside the
analytic argument, not 10 independent confirmations of it.

## 4. Guardrail floor

The adversarial guardrail hard-fails a candidate if its adversarial-slice
composite drops ≥ 10.0 points vs baseline (`GUARDRAIL_THRESHOLD_POINTS`) —
fixed and deterministic, not a config value, not derived from a
significance level. It **claims no statistical control**: coarse by
design, meant to catch large adversarial-slice regressions the nominal-only
statistical rule structurally can't see, not to bound a false-alarm rate.

**Verified-at-baseline-time floor check.** `check_guardrail_floor` requires
`10.0 ≥ 3.0 × measured SE`, checked once at baseline-generation time, never
at gate time. The measured SE is **the standard deviation of the
K_baseline (typically 6) per-replicate adversarial-slice composites,
divided by `sqrt(K_run)`** (K_run=3) — not `sqrt(K_baseline)`. Rationale: a
live gate run reports a K_run-averaged composite, so the noise floor the
10-point threshold must clear is the SE of a K_run-averaged mean, estimated
from the more-plentiful K_baseline observations. `update_baseline(s)`
raises `GuardrailFloorError` and leaves committed baselines untouched if
this fails on a freshly generated baseline (§6).

## 5. MDE reporting

One-sided formula matching the gate's own test:
`mde = (z_alpha + z_beta) * delta_sd / sqrt(n)`. `z_alpha` is the
**one-sided** `(1-α)` quantile (≈1.6449 at α=0.05) — the two-sided quantile
would overstate detectable effect size. `z_beta` ≈0.8416 at 80% power.
`delta_sd`/`n` are the observed per-run delta SD and paired-item count.

Because `delta_sd` is empirical, **the printed MDE on each summary is the
source of truth**; this document states expected order only: **~5–7
composite points at n=32** (spec §7, §11).

## 6. Re-baseline procedure (prompt changes)

1. Run `eval gate --update-baseline` locally, traced, with full keys. This
   atomically regenerates **both** baselines (`update_baselines`): each
   candidate is staged and guardrail-checked into a shared `.staging`
   directory, promoted to `baselines/{a,b}.json` only if **both** pass. Any
   failure (either `GuardrailFloorError` or another exception) leaves
   **neither** committed file touched — no partial pair.
   When the judge provider's daily request quota cannot fit one dual
   regeneration (~1,200 judge calls; see decisions.md D3 amendment
   2026-07-16), run `eval gate --update-baseline --model a` and
   `... --model b` as separate invocations instead — each regenerates,
   guardrail-checks, and atomically promotes only its own
   `baselines/{model}.json`, leaving the other committed file untouched.
   Do not run the two single-candidate invocations concurrently: they
   share the `.staging` scratch directory.
2. The PR must attach the compare-vs-old-baseline report for human review
   — the regression surface is reviewed, never silently bypassed.

**Gate runs are always fresh.** Every `eval gate` invocation — plain,
`--seed-regression`, `--update-baseline` — uses a fresh, invocation-unique
run directory and never reuses a persisted run artifact, unlike `eval
compare`'s fingerprint-match reuse. A persisted run only proves "these
inputs were run once, under whatever scoring code existed then" — it says
nothing about whether harness/scoring code has since changed, exactly what
the gate exists to catch (§1); replaying a stale run would defeat that.

## 7. Exit-code contract

| Code | Meaning | Rationale |
|---|---|---|
| 0 | Pass | A completed measurement crossed neither the statistical rule nor the guardrail. |
| 1 | Regression detected | A completed measurement produced a fail verdict: the paired-delta rule fired for either candidate, the guardrail tripped, **or** (`--update-baseline` only) `GuardrailFloorError` — a freshly measured baseline's noise floor failed the required margin. That last case is exit 1, not 2: a real measurement completed and was refused for cause, not aborted before one existed. |
| 2 | Measurement error | Everything firing **before** a completed measurement exists: missing baseline, fingerprint mismatch, judge-error budget exceeded (>5%), nominal item-set mismatch, aborted run, run-config mismatch, missing tracing/certificate/API-key, SDK construction failure. None of these is "a completed measurement whose verdict is fail" — conflating them with exit 1 would blur "we don't know" with "we know, and it's bad." |

Standing rule feeding this table: a judge refusal/validation
failure/exhausted retry marks a field **missing**, never **fail** — items
with any missing field are excluded from paired deltas (disclosed via an
exclusion count), so judge failures can never register as a regression.

## 8. Results: no-change demonstration

**Executed 2026-07-17.** Ten sequential `eval gate` invocations against the
committed `baselines/{a,b}.json`, no code/prompt/dataset/config change
between runs; each a fresh run (§6), full API keys and Langfuse tracing
present throughout (gate runs are reportable and fail fast without tracing,
spec §8). All 20 candidate-runs (10 runs × 2 candidates) used exact
permutation enumeration (`m` ranged 4–14, well under the `m ≤ 20` exact
threshold, §2) and reported the coarse guardrail as **not tripped** in
every case.

**Observed false-alarm count: 0/10.** All 10 runs exited **0 (pass)**; both
candidates passed the paired-delta rule in every run.

| Run | Exit | a: mean Δ | a: p (one-sided) | a: m | a: MDE | a: adv. Δ | b: mean Δ | b: p (one-sided) | b: m | b: MDE | b: adv. Δ | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1  | 0 | -0.07 | 0.3750 | 4 | 0.50 | 0.00  | 0.52  | 0.8359 | 9  | 1.35 | 0.00  | PASS |
| 2  | 0 | 0.07  | 0.7500 | 4 | 0.50 | 0.00  | 0.37  | 0.6538 | 11 | 2.03 | -0.26 | PASS |
| 3  | 0 | 0.22  | 0.6875 | 4 | 1.34 | 0.26  | -0.37 | 0.1836 | 8  | 0.93 | -0.79 | PASS |
| 4  | 0 | 0.67  | 0.8438 | 5 | 1.51 | 0.79  | -0.22 | 0.4189 | 14 | 1.92 | -0.53 | PASS |
| 5  | 0 | 0.82  | 0.8750 | 6 | 1.54 | 0.00  | 0.52  | 0.7827 | 11 | 1.80 | -0.79 | PASS |
| 6  | 0 | 1.26  | 1.0000 | 5 | 1.53 | 0.00  | 0.37  | 0.6819 | 13 | 2.13 | -1.06 | PASS |
| 7  | 0 | 0.07  | 0.5938 | 5 | 1.34 | 0.00  | 0.82  | 0.8809 | 9  | 1.69 | 0.00  | PASS |
| 8  | 0 | 0.52  | 0.7812 | 5 | 1.32 | 1.06  | -0.22 | 0.3691 | 10 | 1.73 | -0.53 | PASS |
| 9  | 0 | 0.67  | 0.9688 | 5 | 1.20 | 0.79  | 0.52  | 0.7236 | 11 | 1.91 | -0.26 | PASS |
| 10 | 0 | 1.12  | 1.0000 | 5 | 1.36 | -0.26 | 1.41  | 0.9785 | 11 | 1.74 | -0.79 | PASS |

Deltas and MDE in composite points, `current - baseline`; `m` = nonzero
paired deltas feeding the permutation test; adversarial Δ is always printed
regardless of guardrail status (§4). Runs 4 and 7 are each the final
successful attempt of that run index — see "Aborted attempts" below for the
6 and 3 measurement-error attempts that preceded them respectively.

**Errata (2026-07-20, tie-handling fix).** The p-values in this table were
produced before the final whole-branch review found and fixed a
floating-point tie-counting defect in exact-mode `sign_flip_test`
(resampled statistics mathematically tied with the observed value could be
excluded from the extreme count by last-ulp noise — the anti-conservative
direction). The fix can only *raise* a p-value (tied blocks are now
counted as extreme), so every PASS verdict and the 0/10 false-alarm count
above stand a fortiori; the recorded p-values are lower bounds on their
corrected values. For scale: the published comparison's Monte-Carlo p
moved 0.3066 → 0.3179 under the fix (113 of 10,000 resamples were tied
with the observed statistic). These runs' raw outputs were not retained
run-by-run, so the table's p-values are not individually recomputed; the
regression tests pinning the fix live in
`tests/unit/stats/test_permutation.py::TestTieBlockCounting`.

**Predicted vs. observed rate.** §2's family false-alarm rate (≤ ~9.8%
worst case per run) predicts at most about 1 false alarm in expectation
across 10 runs (10 × 0.0975 ≈ 0.975). Observing 0 is consistent with that
prediction — not a rejection of it, and not proof the true rate is lower;
10 correlated conditional checks (§3) have limited power to distinguish "0
false alarms" from "a false alarm was simply never drawn this time."

**Observed `m` values, and an honest sparse-delta disclosure.** Candidate
a's `m` was 4, 4, 4, 5, 6, 5, 5, 5, 5, 5 across the 10 runs — a mostly-4–6
range as expected going in. But per §2's minimum-attainable-p analysis,
that range put candidate a in the sparse-delta regime on **9 of its 10
runs**: 3 runs at `m = 4`, where rejection at α=0.05 was **structurally
impossible** regardless of regression size (min attainable p = 0.0625 >
0.05), and 6 runs at `m = 5`, where rejection was possible only if *every*
nonzero delta shared the regression sign (min attainable p = 0.03125). Only
run 5 (`m = 6`) sat outside that regime. This is disclosed plainly rather
than glossed over: for most of candidate a's runs, a PASS verdict carries
less evidentiary weight against a real regression than it would at higher
`m` — in three of those runs, no regression size could have produced a
FAIL at all. Candidate b's `m` ranged 8–14 (min attainable p from 0.0039
down to 0.00006), giving real rejection room throughout, and it still never
tripped the rule. MDE stayed well under the ~5–7 point expected order
quoted in §5/§7 for both candidates across all 10 runs (a: 0.50–1.54 pts;
b: 0.93–2.13 pts) — the empirical delta spread at this n was tighter than
the conservative expectation, i.e. realized power was higher than assumed.

**Correlated, not i.i.d. — supports, doesn't prove.** Per §3, these 10 runs
share one frozen baseline realization; they are a conditional check against
that baseline, not 10 independent α draws. "0/10" is offered as supporting
evidence for the α claim alongside the analytic argument of §2–§3, not as
an independent statistical proof of it.

### Aborted attempts (non-measurements)

The campaign also logged **9 aborted attempts**, each exiting **2**
(measurement error) per the §7 exit-code contract — none produced a
pass/fail verdict, and none counts toward or against the false-alarm rate
above, exactly as §7 intends: exit 2 is "we don't know," not "we know, and
it's bad."

- **6 aborts** were run 4's first six attempts, all on the judge provider's
  (`gemini-3-flash-preview`) 503 load-shedding bursts — observed live as
  2–5 minute outages that exceeded the then-current retry patience.
- **3 aborts** were run 7's first three attempts, on a candidate provider's
  billing depletion — an account-level quota exhaustion, a distinct
  failure class from a transport error, unaffected by retry patience and
  resolved by topping up the account rather than a code change.

Between run 4's completion and run 5's attempt, the transport retry policy
was hardened in two same-day commits: `a502b3f` widened
`retry_max_attempts` from 4 to 12 and capped each backoff wait at 120s
(aimed at the observed 503 bursts), and `cf25499` added a 600s wall-clock
`PATIENCE_BUDGET_SECONDS` on top of that backoff schedule, correcting an
undercounted worst-case wait once per-request timeouts were also made
retryable. Runs 5, 6, 8, 9, and 10 each completed cleanly on their first
attempt after that hardening, with no further 503-driven aborts observed.
Run 7 still needed three attempts before succeeding, but for the separate
billing-depletion reason above, which transport-retry patience cannot
address — the hardening was never expected to touch it.

Both commits change **transport policy only**: how long the harness waits
for a call to succeed or fail before giving up, never what it does with a
call that did succeed. `retry_max_attempts` (and the patience budget) stay
outside the run fingerprint by design (spec §9, a pinned test enforces
this) — scores, deltas, p-values, and verdicts never depend on transport
patience, and none of the numbers in the results table above are affected
by which side of these commits a run happened to fall on.

## Sources

- `docs/spec.md` §7 (gate contract incl. 2026-07-04 sparse-delta errata), §6
- `docs/decisions.md` D3 (decision + amendment 2026-07-04a)
- `src/harness/gate/gate.py`, `src/harness/gate/baseline.py`,
  `src/harness/stats/permutation.py`, `src/harness/stats/mde.py`,
  `src/harness/reports.py` (`render_gate_summary`)
