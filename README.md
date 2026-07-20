# Model Evaluation Harness

A structured-extraction eval harness with a **calibrated LLM judge** and a
**CI gate that only fails on statistically defensible regressions**. It
benchmarks two models — Claude Haiku 4.5 and GPT-5.4 mini — on one task
(customer support email → structured ticket), scored by a Gemini judge whose
agreement with human labels is measured before its verdicts are allowed to
count, and gated in CI with exact permutation tests against committed
baselines.

The harness is the product: every number below is backed by a committed
artifact and recomputable with **zero API calls**, and every design decision
that trades polish against measurement honesty is resolved in favor of
honesty ([constitution](docs/constitution.md)).

## Headline results

| | Candidate a (Haiku 4.5) | Candidate b (GPT-5.4 mini) |
|---|---|---|
| Composite, all 50 items (95% BCa CI, cluster) | **93.24** [90.48, 95.43] | **94.57** [92.48, 96.29] |
| Nominal slice (n=32) | 93.30 [90.03, 95.68] | 95.83 [93.45, 97.47] |
| Adversarial slice (n=18) | 93.12 [87.83, 96.56] | 92.33 [88.36, 95.50] |
| Judge–human agreement κ (vs ceiling 0.734) | 0.668 [0.420, 0.853] | 0.845 [0.639, 0.962] |

**Paired comparison (b − a): +1.33 points, 95% BCa CI [−1.05, 3.71],
two-sided permutation p = 0.32** — the gap is **not statistically
significant** at this n, and the honest headline is a trade-off, not a
winner: candidate a extracts the literal fields flawlessly (100% on
customer name, order id, product name), while candidate b writes better
judged free text (issue summary 94% vs 79%) and reads urgency better
(priority 91% vs 84%). Which one you'd ship depends on which failure is
costlier — exactly the kind of answer a measurement tool should give.

Judge calibration: **κ = 0.749** [0.543, 0.886] against dual-annotated,
adjudicated human gold — statistically indistinguishable from the
human–human ceiling of 0.734. Gate: **0/10 false alarms** in the no-change
campaign. Everything below unpacks these numbers; all of them are
recomputable offline from [committed artifacts](#reproducibility-contract).

## The judge is calibrated, not trusted

Free-text fields (issue summary, requested action) are scored by
`gemini-3-flash-preview` against a binary reference-guided rubric, one field
per call. Whether those judged verdicts are allowed to count is gated by a
committed **calibration certificate**
([`data/calibration/certificate.json`](data/calibration/certificate.json)):

- **Gold labels:** 140 candidate outputs dual-annotated (owner + an
  independent second annotator), disagreements adjudicated (13 rows, all
  recorded). Judge agreement is measured against the adjudicated gold.
- **Cohen's κ = 0.749** (95% cluster-bootstrap CI [0.543, 0.886], resampling
  emails — fields within an email are correlated), raw agreement 90.0%.
- **A human–human ceiling is reported alongside:** inter-annotator κ =
  0.734 [0.575, 0.854]. A judge κ above the ceiling is estimation noise, not
  a super-human judge.
- **Fail-probe enrichment:** real calibration outputs pass ~100% at k=1, so
  31 of 140 rows carry planted, dual-validated defects (dropped essentials,
  ungrounded additions, contradictions, entity swaps, supersession leaks),
  bringing fail prevalence to 22.9% — a judge can't demonstrate it catches
  failures on a set with none.
- **Pre-registered adequacy bar:** κ ≥ 0.6 with CI floor ≥ 0.4 → judged
  fields count (FULL_7 composite). Inadequate → judged fields are excluded
  from the gate (DETERMINISTIC_5) and flagged on every report. The current
  verdict: **adequate**, and the certificate is embedded in the header of
  every generated report.
- **Per-candidate κ is disclosed** (a = 0.668, b = 0.845): the judge is
  stricter with candidate a's terse-but-complete style — corroborated by a
  mild positive judge-verdict-vs-length correlation for a (r = 0.159) that
  is absent for b (r = −0.087), both printed on every run report. Direction
  analysis showed zero false *passes* on real rows for either candidate;
  the gate pairs each candidate against its own baseline, so stable
  per-candidate bias cancels. A terseness few-shot and re-certification is
  the booked first post-v1 judge iteration.
- Judge stability: 1 flip in 20 repeat judgments; the full selection
  history (two prior judges, one measured inadequate and reverted under the
  pre-registered rule) is dated in [docs/decisions.md](docs/decisions.md)
  D1/D2.

## The gate fails only on defensible evidence

`eval gate` compares a fresh K=3 run against committed K=6 baselines
([design + threat model](docs/gate-design.md)):

- One-sided **sign-flip permutation test** on paired per-item deltas
  (nominal slice, n=32), full enumeration when nonzero deltas m ≤ 20 —
  exact, not asymptotic. Fails only when p < 0.05 AND the mean regression
  exceeds 2.0 points.
- Every summary prints **m** (with sparse-delta warnings — at m ≤ 4 no
  rejection is even possible) and the **MDE** at 80% power, so a PASS
  carries its own evidentiary weight, or lack of it.
- A coarse non-statistical **guardrail** hard-fails if the adversarial
  slice (prompt injection, contradictions, distractors) drops ≥ 10 points.
- **Exit codes distinguish "regression" from "can't measure":** 0 = pass,
  1 = regression detected, 2 = measurement error (fingerprint mismatch,
  missing baseline, judge-error budget, missing credentials). A judge
  failure can never register as a candidate regression.
- **Observed false alarms: 0/10** no-change runs (all exact-mode, m 4–14;
  [full table + honest caveats](docs/gate-design.md#8-results-no-change-demonstration)),
  alongside the analytic ≤ ~9.8% worst-case family rate per run. Nine
  aborted attempts during the campaign (judge 503 shedding, billing
  depletion) all exited 2 — disclosed as non-measurements, never verdicts.

CI: the gate runs on every same-repo PR touching the measurement surface
(path-filtered; fork PRs are skipped by design). Live evidence from the
first CI executions, including the exit-1 vs exit-2 demos:
[docs/tickets/T18-ci-workflow.md](docs/tickets/T18-ci-workflow.md).

## Quickstart

```bash
uv sync                          # Python 3.12+, uses uv.lock
cp .env.example .env             # three provider keys + two Langfuse keys
uv run eval run --model a        # one candidate → markdown + JSONL
uv run eval compare              # both + paired comparison report
```

**Two-command demo** (the before/after story):

```bash
uv run eval gate                    # exit 0 — passes against committed baselines
uv run eval gate --seed-regression  # exit 1 — DEMO MODE: a documented prompt
                                    # degradation is injected and detected
```

In CI the same demo runs via `workflow_dispatch` (demo=true) — see the
[recorded runs](docs/tickets/T18-ci-workflow.md).

**Recompute the published numbers (zero API calls):**

```bash
uv run eval rescore results/published/runs/<run-dir>  # byte-exact report recompute
uv run eval calibrate --offline                       # certificate from committed labels
```

## Reproducibility contract

- A run's identity is a **fingerprint** over config, served model versions
  (captured from provider responses — candidate alias drift is caught;
  the judge's provider echoes the requested alias, so judge drift is
  guarded by a recorded dated snapshot + the booked re-certification, not
  the fingerprint — see [gate-design §1](docs/gate-design.md)), judge
  version (hash of judge model + prompt + rubric + few-shots), composite
  mode, and calibration verdict. The gate exits 2 on any mismatch.
- **Raw outputs are always persisted**, including failures; reports are
  pure functions of the persisted rows. `eval rescore` reproduces every
  published run report byte-exactly with zero API calls; re-invoking
  `eval compare` over the published runs reuses them by identity (zero API
  calls, verified 10s wall-clock) and reproduces the comparison report
  byte-exactly; `eval calibrate --offline` does the same for the
  certificate.
- Recomputation is the contract; *re-execution* against live APIs is
  expected to differ within the reported CIs.
- Committed evidence map (`results/published/`): the two comparison run
  dirs (`runs/a-927b2dc82a761b18`, `runs/b-b9cab21e8fb1c83a` — raw rows +
  manifests), their rescored run reports, the paired comparison report
  backing the headline table, and `latency-export.json`. Agreement numbers
  are backed by `data/calibration/` (labels, judgments, certificate); gate
  claims by `baselines/` and `docs/gate-design.md` §8.

## Costs and latency

- The published comparison (2 candidates × 50 items × K=3, plus one judge
  call per judged field per replicate — 300 candidate + 600 judge calls)
  cost **$0.78** at the committed price snapshot (2026-07-16,
  [`configs/default.yaml`](configs/default.yaml)): candidate a $0.45,
  candidate b $0.33. Judge calls outnumber candidate calls 2:1, but each
  is cheap — candidate calls are the majority of spend (72% for a, 62%
  for b). The figures are computed from the per-call token usage persisted
  in the published `rows.jsonl` files, so they are recomputable offline
  like every other number. A gate run executes the same volume (its n=32
  is the *statistic's* nominal slice, not the run size) and costs the
  same.
- Per-call latency (from the committed
  [`latency-export.json`](results/published/latency-export.json), spans
  include transport retries): candidate a p50 1.6s / p95 3.0s; candidate b
  p50 1.4s / p95 3.1s; judge p50 2.2–6.2s / p95 9.7–16.0s across the two
  runs, worst single call 103.7s — the long tail the transport patience
  budget (600s wall-clock) exists to absorb.

## Honest limitations

- **Owner-anchored gold.** Calibration gold is resolved by the project
  owner; adjudication upheld the owner's initial verdict in 11 of 13
  disagreements, so gold is not a fully independent third check. The
  ceiling honestly measures agreement under the written conventions —
  6 of 13 disagreements traced to one convention (pronoun wording), booked
  for the next rubric cycle.
- **Small n.** 50 golden items (32 nominal / 18 adversarial). The A-vs-B
  gap is not significant at this n; the printed MDE is the per-run source
  of truth (observed 0.50–2.13 points across the no-change campaign —
  tighter than the conservative ~5–7-point design expectation).
- **Sparse deltas for candidate a.** Its outputs are so replicate-stable
  that its nonzero-delta count sat at m = 4–6 on all 10 no-change runs,
  which put 9 of the 10 in the sparse-delta regime — and in 3 of them
  (m = 4) no regression size could have produced a FAIL at all. Disclosed
  on every summary; more items or a sensitivity redesign is the v2 answer.
- **Candidate B is pinned by alias.** `gpt-5.4-mini` in
  [`configs/default.yaml`](configs/default.yaml) is an undated alias; the
  provider-resolved dated ID (`gpt-5.4-mini-2026-03-17`) is captured on
  every persisted row (`served_model_version` in the published
  `rows.jsonl`) and folded into the committed baseline fingerprints, so
  silent provider re-pointing surfaces as a fingerprint mismatch (exit 2)
  rather than a wrong verdict — but the alias itself is outside this
  repo's control.
- **Real-only κ is structurally zero.** On the 100 real (non-probe) rows
  the resolved gold passes everything — and with a constant gold marginal,
  Cohen's κ is algebraically 0 no matter what the judge does (the judge's
  actual real-row behavior: 88% raw agreement, every miss a false-fail,
  none a false-pass). The informative number is overall κ on the enriched
  set; 1 of 31 planted defects was passed by gold.
- **Preview judge pin.** `gemini-3-flash-preview` is a preview ID
  (retirement/re-pointing risk; the served snapshot
  `3-flash-preview-12-2025` is recorded). It also load-sheds at peak hours
  — observed live as exit-2 aborts, never wrong verdicts. Re-certification
  on a GA model is the booked first post-v1 iteration.
- **Probe rows appended, not interleaved** in the labeling sheets
  (fatigue/order effect possible); 3 of 40 supplement rows were exposed in
  a session transcript before owner labeling (owner reports unread;
  outcomes unchanged).
- **Trace binding is positional** for the latency export (run identity is
  not yet stamped into trace IDs — booked post-v1); latency numbers are a
  one-time disclosed pull, not part of the zero-API recompute contract.
  Local `.env` uses the deprecated `LANGFUSE_HOST` alias; CI uses the
  canonical `LANGFUSE_BASE_URL` variable.
- **Ops incidents disclosed:** three multi-provider billing/quota
  incidents during the campaign — all loud exit-2s, none produced a wrong
  verdict.

## Repo map

| Path | What it is |
|---|---|
| `docs/constitution.md` · `docs/spec.md` · `docs/decisions.md` | Principles + locked scope · behavioral contract · dated decision log (D1–D4) |
| `docs/gate-design.md` | Gate false-alarm justification, threat model, re-baseline playbook |
| `src/harness/` | Typed library: pure-function stats/scoring core, 3 hand-written clients behind one protocol, persist/resume runner |
| `data/golden/` · `data/calibration/` | Frozen dataset v1 (50 items + taxonomy) · emails, labels, certificate, judgments |
| `baselines/` · `results/published/` | Committed gate baselines (K=6) · committed evidence for every README number |
| `tools/` | Browser-based labeling grader · Langfuse latency pull |

Statistical machinery (permutation, BCa + cluster bootstrap, Cohen's κ,
MDE) is unit-tested against scipy/sklearn reference implementations —
664 tests, zero warnings.
