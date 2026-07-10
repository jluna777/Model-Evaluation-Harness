# Spec — Model Evaluation Harness v1

**Status:** Validated v1.0 · 2026-07-04
**Depends on:** `docs/constitution.md` (principles, scope, cuts) · `docs/decisions.md` (D1–D4 choices and rationale)

One-line identity: a structured-extraction eval harness with a calibrated LLM judge and CI gating. This spec is the behavioral contract: it owns every operational number and rule; `docs/decisions.md` owns the choices and rationale. Anything not specified here and not in the constitution is out of scope for v1.

---

## 1. The task

Extract a structured support ticket from a customer support email.

**Input:** one email — `from`, `subject`, `body` (body may contain quoted replies/forwarded content). Each email is extracted independently; threading or deduplication across separate emails is upstream triage, out of v1 scope.

**Output schema (`TicketExtraction`, Pydantic):**

| Field | Type | Scoring |
|---|---|---|
| `category` | enum: `billing` \| `shipping` \| `account` \| `product` \| `other` | exact match |
| `priority` | enum: `low` \| `normal` \| `high` \| `urgent` | exact match |
| `customer_name` | `str \| None` | normalized exact match |
| `order_id` | `str \| None` (pattern `ORD-\d{5}`) | normalized exact match |
| `product_name` | `str \| None` | normalized exact match |
| `issue_summary` | free text, 1–2 sentences | LLM judge (D1) |
| `requested_action` | free text, 1–2 sentences | LLM judge (D1) |

**Normalization for entity fields:** trim, casefold, collapse internal whitespace; `None` matches only `None`. Fields absent from the email have reference value `None`; OpenAI strict mode cannot omit fields, so `None`/null is the required "not present" encoding for both providers.

**Primary-request rule (multi-request emails), canonical wording (amended and restructured 2026-07-07):** the following three-step rule appears verbatim in the extraction prompt and governs every reference answer:

> The ticket describes ONE primary request. Determine it as follows:
> 1. Consider only the newest, non-quoted part of the email. Quoted or forwarded content below it (lines starting with '>', earlier messages introduced by headers like "On ... wrote:", or trailing prior threads) is earlier conversation: any request made there is already superseded by the newest message and is never the primary request.
> 2. Within the newest, non-quoted text, the primary request is the first actionable request — unless a later statement in that same text explicitly retracts or supersedes it, in which case the superseding request is primary.
> 3. When the newest text refers to quoted content — such as accepting an option support offered earlier — use the quoted content to describe the request precisely. Entity fields (customer_name, order_id, product_name) may likewise be resolved from anywhere in the email, including quoted or forwarded sections.

Secondary requests are omitted from reference answers. Multi-ticket extraction (one ticket per request, variable-length output) is a noted v2 direction — it would showcase set-valued scoring but requires alignment machinery cut from v1.

**Extraction prompts:** one shared, versioned prompt template for both candidates; schema delivered via each provider's native structured-output mechanism (Anthropic `output_config.format`, OpenAI `json_schema` strict).

## 2. Candidates and judge

| Role | Model | Pin of record | Structured output |
|---|---|---|---|
| Candidate A | Claude Haiku 4.5 | `claude-haiku-4-5-20251001` (dated snapshot) | native, constrained decoding |
| Candidate B | GPT-5.4 mini | `gpt-5.4-mini` (dated snapshot if offered at implementation time; resolved ID recorded in `configs/default.yaml`) | native, strict JSON schema |
| Judge | Gemini 2.5 Pro | `gemini-2.5-pro` (stable GA; re-pinned 2026-07-09 per D1 amendment; fallback: `gemini-3.5-flash`) | native; output re-validated with Pydantic |

Candidates run at temperature 0. SDKs: `anthropic`, `openai`, `google-genai`, versions pinned in `pyproject.toml`.

**Alias-drift guard:** every run records the provider-reported model version from response metadata into its fingerprint; the gate exits with a measurement error (§7) when the served version differs from the baseline's recorded version. This keeps undated aliases honest.

The model interface is a thin internal protocol (`complete_structured(email, schema) -> TicketExtraction + usage + served_model_version`) with exactly three implementations — no plugin system (constitution §5).

## 3. Golden dataset (D4)

- **Size/mix:** 50 items — 32 nominal, 18 adversarial/edge per the D4 taxonomy. `data/golden/taxonomy.md` is the coverage contract: category counts, difficulty tags, and generator-family counts; every taxonomy category has ≥2 items.
- **Item format** (`data/golden/golden.jsonl`): `{id, email: {from, subject, body}, expected: TicketExtraction, meta: {slice: "nominal"|"adversarial", categories: [...], difficulty: 1-3, generator: "<model-id>", edited: bool, notes}}`
- **Provenance:** all emails are synthetic, drafted by generator models and then human-curated (curation may rewrite heavily; `edited` records it). **≥80% of items come from model families distinct from both candidates**; per-family counts live in `taxonomy.md`.
- **Freeze protocol:** draft set → run both candidates once → owner open-codes outputs for unanticipated failure modes → adjust/add items → freeze as `dataset_version: 1`. Post-freeze edits bump the version and invalidate baselines.
- **Never used for prompt tuning** (constitution Principle 6). Prompt iteration uses `data/dev/` (~10 items), excluded from all reported numbers.

## 4. Judge (D1)

- Pointwise, reference-guided: each free-text field of each candidate output is judged in its own call — inputs are the email, the field's reference value, the candidate's value, and the binary rubric; output is `{verdict: pass|fail, rationale}`, Pydantic-validated.
- Rubric (binary, per field, amended 2026-07-09): *pass = same issue/action as the reference, with no missing essentials — additional detail is acceptable when it is accurate and grounded in the email; fail = content not grounded in the email (invented or hallucinated), contradicting the email or reference, or missing something essential; wording may differ freely.* Rubric text, ordering, and examples are pinned and identical for both candidates.
- **Few-shot examples:** pass/fail-labeled examples with one-line critiques, hand-written or drawn exclusively from `data/dev/` — never from golden or calibration items. They are part of the versioned judge prompt; changing them is a judge change.
- Judge config: `gemini-2.5-pro` (re-pinned 2026-07-09, D1 amendment; fallback `gemini-3.5-flash`), temperature 0. The judge version = hash of {judge model, prompt, rubric, few-shots}; any change invalidates the current calibration certificate (§5).
- **Judge health diagnostics (reported, never gating):**
  - score-vs-length correlation per candidate;
  - **judge self-consistency:** at certification time, 20 fixed (email, reference, candidate-value) triples are each judged 3 times; the verdict flip rate is reported. (Cross-replicate verdict variance is a different quantity — *judged-field run variance* — and feeds the §6 variance decomposition.)

## 5. Judge calibration (D2)

- **Calibration data:** 25 dedicated calibration emails — same taxonomy, same item schema as golden including `expected` reference values, **disjoint from the golden set**. Both candidates run on them → 25 × 2 fields × 2 candidates = **100 field judgments (50 per candidate)**. Dual annotation (D2 amendment 2026-07-09): **both** annotators — the owner (primary) and a second, independent annotator — label all 100 field judgments independently, each with a one-line critique, from their own per-annotator hash-bound labeling sheet (`labeling_template_rows`, called once per annotator); neither annotator sees the other's verdicts.
- **Stratification loop (stated procedure):** draft calibration emails skewed toward hard taxonomy categories → run both candidates → owner labels all fields (drives the stratification decision below) → if the owner's fail-label rate < 20%, add emails from harder categories and label those too with **both** annotators (labeled items are never removed) → freeze the calibration set **before** the judge is run on it. Fail-enrichment means measured agreement is estimated on a harder-than-operational distribution (conservative); the report says so.
- **Labels** (`data/calibration/labels.jsonl`): `{label_id, item_id, candidate, field, annotator, verdict, critique, label_date, round: "initial"|"adjudication"}`. `annotator` is a free string (`"owner"` is always the primary annotator/adjudicator; any other string identifies the second annotator). `round: "adjudication"` rows are always `annotator: "owner"`.
- **Dual-annotation gold resolution and ceiling (D2 amendment 2026-07-09 — replaces test-retest):** for every key both annotators labeled — the two rounds must cover the exact same set of keys, or `eval calibrate` errors loudly naming what's missing — the FINAL gold verdict is the owner's verdict where the two annotators agree, or the owner's adjudication verdict (`round: "adjudication"`) where they disagree; a disagreement with no adjudication row is a loud error naming every unadjudicated key. Judge agreement is computed against this resolved gold, never against either annotator's raw label directly. **Population parity (D2 amendment 2026-07-09b):** every certificate number — judge kappa (overall and per-candidate), the ceiling kappa and its CI, and `n_adjudicated` — is computed over exactly the same paired, validly-judged key set: a gold label with no corresponding judgment, or a judged key that neither annotator labeled, is a loud error naming every offending key, never a silent exclusion (this replaces an earlier `unlabeled_excluded` tolerance that only excluded such a key from judge kappa while leaving the ceiling computed over the full labeled set). A judge error on a key is the one tolerated gap, excluded from judge kappa, the ceiling kappa, *and* `n_adjudicated` alike, disclosed via the existing judge-error count. `eval calibrate` reports Cohen's kappa **between the two annotators**' verdicts over that same paired population, with its own cluster-bootstrap CI — *the human-human agreement ceiling* — labeled explicitly as such: the judge's kappa exceeding it indicates estimation noise, not a super-human judge (same semantics as the retired test-retest ceiling). Output-binding (`output_sha256`) applies to every row of every annotator and adjudication round, verified pairwise before any gold is resolved.
- **Statistics:** Cohen's kappa is the single agreement statistic and the only one that decides (constitution §5 conformance). Raw agreement and label prevalence are reported as descriptive context. All CIs are **cluster bootstrap resampling emails** (all judgments of an email move together — fields and candidates within an email are correlated); the ±0.15–0.25 resolution statement in D2 is the pre-clustering floor and may widen.
- **Adequacy policy:** decided on the overall Cohen's κ **point estimate** ≥ 0.6 (pragmatic v1 rule). Gray zone: κ̂ ≥ 0.6 with CI lower bound < 0.4 → *adequate-with-caveat*, flagged in every downstream report. Per-candidate kappas are reported with CIs; a per-candidate gap > 0.2 flags a D1 review — a flag, never a gate condition.
- **Certificate** (`data/calibration/certificate.json`, committed): `{judge_version, overall_kappa, kappa_ci, per_candidate_kappa, per_candidate_kappa_ci?, verdict: "adequate"|"adequate_with_caveat"|"inadequate", ceiling_kappa?, ceiling_kappa_ci?, n_adjudicated?, label_file_hash, date}`. `ceiling_kappa`/`ceiling_kappa_ci` now carry the human-human IAA ceiling (D2 amendment 2026-07-09); `n_adjudicated` discloses how many gold labels required owner adjudication rather than spontaneous agreement. Every `eval run/compare/gate` report embeds the certificate header (judge version, κ ± CI per candidate, verdict, the ceiling row, adjudicated-disagreement count). The certificate pins the **judge** only; candidate-prompt changes shift the judged-output distribution and are accepted as a stated v1 limitation.
- **Inadequate:** judged fields are excluded from the gate (see §7 degraded mode) and flagged in all reports. Response ladder: escalate judge model, then revise rubric. **Re-certification after any judge change uses freshly drafted calibration emails** — re-certifying a revised judge on emails whose disagreements drove the revision would make the published kappa selected, not measured. The certificate records which iteration produced it.

## 6. Scoring and comparison

- **Per-field score:** deterministic fields → 0/1; free-text fields → judge verdict 0/1. Candidate output that is schema-invalid or a refusal → all 7 fields scored 0 for that replicate, raw response persisted (a real candidate failure). Transport-level API failure surviving retries → the run aborts as a measurement error; never scored.
- **Composite per email:** unweighted mean of the *included* fields — 7 normally; 5 (deterministic only) in judge-excluded mode. The composite definition is part of the run fingerprint.
- **Replicates:** gate/compare runs use K=3 per item, averaged per item before any pairing; baselines use K=6 (§7).
- **Per-model eval (`eval run`):** composite mean per slice (nominal / adversarial / all) with 95% BCa cluster bootstrap CIs; per-field accuracies; per-category table; variance decomposition — between-item vs between-replicate components, with judged-field run variance separated — informing the future K decision (D3).
- **Pairwise comparison (`eval compare`):** paired per-item deltas on the shared item set; mean delta with 95% BCa CI; **two-sided** sign-flip permutation p (the gate alone is one-sided); per-field pass-rate delta tables with flip counts (majority vote across replicates) — descriptive counts, no additional tests (constitution §5). Absolute scores always shown alongside deltas. Reuses existing run artifacts when fingerprints match; re-runs otherwise.

## 7. CI gate (D3)

**Threat model (stated):** the gate protects against harness/scoring code changes, provider-side drift of served models, and judge drift. Prompt changes are *not* gated automatically: a prompt bump requires `eval gate --update-baseline`, and that PR must attach the compare-vs-old-baseline report for human review — the regression surface is reviewed, not silently bypassed.

- **Baseline artifact** (`baselines/{candidate}.json`, committed): per-item, per-replicate, **per-field** scores for all 50 items, raw model and judge outputs (or a pointer to their committed location), config fingerprint, and K_baseline=6. Larger K_baseline keeps baseline noise small so the per-run false-alarm rate — exact unconditionally — stays near-exact *conditional on the frozen baseline* (the demonstrated rate is a conditional quantity; the gate docs say so).
- **Fingerprint:** prompt version, dataset version, resolved/served model versions, judge version, composite definition, calibration verdict. Any mismatch vs baseline → exit 2 (measurement error) with a re-baseline instruction. Sole exception: `--seed-regression` injects a documented prompt degradation at runtime, is exempt from the fingerprint check, and banners the entire output as **DEMO MODE**.
- **Statistical rule (nominal slice, n=32, per candidate):** one-sided sign-flip permutation test on K-averaged paired per-item deltas vs baseline. Full enumeration of all 2^m sign assignments when the count of nonzero deltas m ≤ 20 (truly exact); otherwise 10,000 Monte Carlo resamples with p̂ = (b+1)/(B+1). **Fail iff p < 0.05 AND mean regression > 2.0 points.** The margin is honest bookkeeping, not a false-alarm mitigation: at v1's n it is dominated by the significance condition (any significant regression already exceeds it); it binds only if n grows or variance shrinks, and it makes the practical floor explicit.
- **Sparse-delta disclosure:** the summary prints m (nonzero deltas). At m ≤ 4 no rejection is possible at α=0.05 regardless of regression size; at m = 5 rejection requires all five nonzero deltas to be regressions (min p = 0.031). The gate warns accordingly whenever m < 6 — the known blind spot of sign tests on mostly-unchanged runs. *(Errata 2026-07-04: original wording claimed impossibility for all m < 6, which is false at m = 5.)*
- **MDE:** computed from observed delta variance and printed on every run ("catches ≥X-point composite regressions at 80% power"). The printed MDE is the source of truth; the expected order at n=32 is ~5–7 points.
- **Adversarial guardrail (coarse, non-statistical):** the gate also hard-fails if the adversarial-slice composite drops ≥ 10 points vs baseline — a deterministic threshold set far above observed run noise (≥3× the adversarial composite's run-to-run SE, verified at baseline time), claiming no statistical control and labeled as coarse in the summary. The adversarial delta is always printed. This closes the injection-category blind spot without new statistical machinery.
- **Two candidates, one verdict:** the gate fails if the rule fires for either candidate; the summary reports both verdicts and states the per-PR family false-alarm rate (two tests at α=0.05 → ≤ ~9.8% worst case).
- **Judge-error handling:** judge refusal / validation failure / exhausted retries → the field is *missing* (not fail); items with missing fields are excluded from that run's paired deltas, with the exclusion count printed. Judge-error rate > 5% of calls → exit 2. Judge failures can never register as candidate regressions.
- **Exit codes:** 0 = pass · 1 = regression detected · 2 = measurement error (missing baseline — with instruction to run `--update-baseline`, never auto-created; fingerprint mismatch; judge-error budget exceeded; aborted run). CI renders each state distinctly; GitHub Actions treats 1 and 2 as failed checks with different labels.
- **Gate output:** markdown job summary + PR comment — verdict per candidate, mean delta with 90% BCa CI (two-sided 90% ↔ the one-sided 5% test level; other reports use 95%), permutation p, m, MDE, calibration certificate header, adversarial guardrail status, per-field delta tables, token totals and approximate cost from a dated price snapshot in `configs/default.yaml` (labeled approximate-at-snapshot).
- **Demo (the documented two-command sequence):** `eval gate` (passes against the committed baseline) then `eval gate --seed-regression` (fails, DEMO MODE banner). In CI, the demo runs via `workflow_dispatch`.
- **False-alarm demonstration (constitution DoD 2):** both constitutional branches are satisfied — the analytic justification (exact permutation level, conditional-on-baseline caveat, K_baseline reasoning) is committed in the repo and linked from every gate summary, **and** 10 no-change gate runs are executed once before v1 ships, with the observed false-alarm count recorded in the README as a conditional check.

## 8. Tracing (Langfuse)

- Every candidate and judge call is a span in a per-run Langfuse trace (run id, fingerprint, item id, replicate index; scores attached per item).
- **Scope of degradation (bounded):** runs that produce *reported* numbers — baseline updates, README/published numbers, calibration certification — require Langfuse credentials and fail fast without them. Dev iteration may run keyless with a one-line warning; such runs are flagged `untraced` in their report header and can never feed baselines or the README. A mid-run Langfuse transport failure does not abort measurement: the run completes, is flagged untraced, and gate verdicts stand (baseline updates and published numbers still require complete traces).
- v1 targets Langfuse Cloud free tier; keys via environment variables (optionally loaded from a git-ignored `.env` file at the repo root; OS-level variables take precedence).

## 9. CLI and reports

`uv run eval <command>` (Typer):

| Command | Does |
|---|---|
| `eval run --model {a\|b}` | eval one candidate on the golden set → markdown + JSONL |
| `eval compare` | both candidates + paired comparison report |
| `eval gate [--update-baseline\|--seed-regression]` | CI gate vs baseline; exit code contract per §7 |
| `eval calibrate` | agreement report + certificate from committed labels; dual-annotation gold resolution + human-human ceiling computed automatically when both annotators' labels are present |
| `eval rescore <run-dir>` | recompute all scores/statistics/reports from persisted raw outputs — zero API calls |

- **Config** (`configs/default.yaml`, Pydantic-validated): model IDs, prompt version, dataset path/version, K, gate margin/alpha, price snapshot, Langfuse settings. Defaults are the decided D2/D3 values; changing margin, alpha, or K requires a dated decision-log amendment, and the gate summary prints the values used.
- **Retries:** transport-level errors only (429/5xx/timeouts), capped jittered exponential backoff (cap: 4 attempts, exposed in config). A successfully returned response is never re-sampled regardless of content (the constitution's retries-as-quality-strategy cut draws exactly this line). Per-item results persist incrementally; an aborted run resumes rather than re-spending calls.

## 10. Repo layout (target)

```
src/harness/          # models/ (3 clients), judge/, scoring/, stats/, gate/, tracing/, cli.py
data/golden/          # golden.jsonl, taxonomy.md
data/calibration/     # emails.jsonl, labels.jsonl, certificate.json
data/dev/             # scratch set for prompt iteration (never reported)
baselines/            # committed gate baselines (per-field, per-replicate, raw outputs)
configs/              # default.yaml
docs/                 # SDD artifacts + gate analytic justification
results/              # generated reports — gitignored, EXCEPT results/published/
results/published/    # committed raw artifacts for every run whose numbers appear in the README
.github/workflows/    # eval-gate.yml
```

`results/published/` and the baselines are the only stored run data — this is the single baseline artifact plus published-run evidence permitted alongside the constitution's run-history cut, not trend storage.

## 11. Build order, CI wiring, risks

**Build order (load-bearing, plan must follow):** clients + scoring on `data/dev/` → extraction prompt freeze → golden draft → open-coding run → dataset freeze → calibration emails + dual-annotation labeling (both annotators label independently, in parallel, per D2 amendment 2026-07-09) → adequacy verdict → baseline generation (K=6) → CI wiring → demo + README.

**CI wiring:** same-repo PRs only (fork PRs skip the gate — documented); path filter on `src/`, prompts, `data/`, `baselines/`, `configs/`, the workflow file; `pull-requests: write` for the PR comment; 30-minute job timeout; **CI uses a paid-tier Gemini key** — at ~600 judge calls per gate run, free-tier limits are a flake risk the false-alarm story cannot absorb.

**Risks:** Gemini structured-output API surface is newer than the SDK patterns in training data — judge client is written against current `google-genai` docs, with Pydantic re-validation regardless. Owner authoring effort is real and named: ~85 emails (50 golden + 25 calibration + 10 dev) plus ~100 initial labels plus owner adjudication of whatever disagreements the second, independently-labeling annotator's ~100 initial labels surface (D2 amendment 2026-07-09) plus 20×3 self-consistency triples' worth of review (~4–6 hours total for the owner's own labeling/adjudication/review) — no ≥1-week retest calendar gap to wait out, per the amendment. MDE at n=32 is coarse (~5–7 points); the printed MDE is the source of truth.

## 12. Acceptance criteria (v1)

1. `eval compare` runs end-to-end on the frozen golden set — both candidates, judge scoring, paired report with CIs, two-sided permutation p, per-field delta tables — with the calibration certificate embedded in the header and Langfuse traces present (reported runs are always traced; keyless dev mode exists but never feeds reports).
2. `eval calibrate` produces the D2 report and committed certificate — per-candidate kappas with cluster-bootstrap CIs, prevalence, verdict — and, once both annotators' labels are committed, resolves the dual-annotation gold set and adds the human-human agreement ceiling row automatically (D2 amendment 2026-07-09; no flag required).
3. `eval gate` in GitHub Actions: passes on the unchanged committed baseline, fails via `workflow_dispatch --seed-regression` with the DEMO MODE banner, and the exit-code contract (0/1/2) drives distinct CI outcomes. The false-alarm rate is justified analytically in a committed document linked from every summary, and 10 documented no-change runs with the observed false-alarm count appear in the README.
4. The README shows the before/after with real numbers: composite scores ± CI, judge–human agreement ± CI (with the ceiling row), gate verdicts, printed MDE, and the sparse-delta count m.
5. Every number in the README is exactly recomputable from committed artifacts with zero API calls — run numbers via `eval rescore` on `results/published/` + baselines, agreement numbers via `eval calibrate` on committed labels. Re-execution against live APIs is expected to differ within reported CIs — the spec distinguishes recomputation from re-execution.
