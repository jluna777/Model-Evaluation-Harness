# Gate design: the analytic false-alarm justification

This document is the analytic half of the spec §7 false-alarm demonstration
(constitution DoD 2); the other half is the "Results" section at the bottom,
pending 10 local no-change runs. Linked from every `eval gate` summary
(`render_gate_summary`, `src/harness/reports.py`).

## 1. Threat model

The gate protects against (spec §7, D3):

- **Harness/scoring code changes** — regressions in scoring, judge-calling,
  or composite computation.
- **Provider-side model drift** — every run records the served model
  version in its fingerprint (`served_versions`); a mismatch vs baseline is
  a fingerprint mismatch (exit 2).
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

The planned 10 no-change runs (§8) are therefore a **conditional check
against the one committed baseline**, not 10 independent α draws — they
share the same frozen baseline noise, so outcomes are correlated, not
i.i.d. Bernoulli(α) trials.

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

## 8. Results: no-change demonstration (PENDING)

**Not yet executed** — deferred until Phase B's real, traced baselines and
API keys are available.

**Planned protocol:** 10 sequential `eval gate` invocations, no
code/prompt/dataset/config change between runs, against the committed
`baselines/{a,b}.json`; each a fresh run (§6): the per-invocation
run root already makes cross-run leakage structurally impossible, and
scratch directories are additionally cleared between invocations as disk
hygiene;
full API keys and Langfuse credentials present throughout (gate runs are
reportable and fail fast without tracing, spec §8). Recorded per run: run
index, exit code, per-candidate one-sided p, mean delta. Observed
false-alarm count = number of runs exiting 1. Per §3, this is a
**conditional check** against the one frozen baseline, not 10 independent
α draws — reported and interpreted as such.

## Sources

- `docs/spec.md` §7 (gate contract incl. 2026-07-04 sparse-delta errata), §6
- `docs/decisions.md` D3 (decision + amendment 2026-07-04a)
- `src/harness/gate/gate.py`, `src/harness/gate/baseline.py`,
  `src/harness/stats/permutation.py`, `src/harness/stats/mde.py`,
  `src/harness/reports.py` (`render_gate_summary`)
