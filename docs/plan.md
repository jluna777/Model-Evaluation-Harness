# Model Evaluation Harness v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Tasks use checkbox (`- [ ]`) syntax for tracking. Each task becomes one ticket in `docs/tickets/` with verifiable acceptance criteria — tickets are the unit of owner validation.

**Status:** Validated v1.0 · 2026-07-04
**Goal:** Ship the v1 defined by `docs/spec.md` (Validated v1.0): one extraction task, two candidates, a calibrated pointwise judge, a statistically gated CI, Langfuse tracing — demoable end-to-end.

**Architecture:** A thin, typed Python library (`src/harness/`) with pure-function statistics and scoring at the core, three hand-written model clients behind one protocol, a run loop that persists raw outputs incrementally, and a CLI whose five commands compose those pieces. All measurement logic is unit-tested against reference implementations before any live API call is made; data authoring and calibration are owner work streams that overlap implementation.

**Tech stack:** Python 3.12+ · `uv` · Pydantic v2 · Typer · `anthropic` / `openai` / `google-genai` (pinned) · `langfuse` · numpy/scipy (stats) · pytest · ruff.

## Global constraints

- Python 3.12+, `uv`, `pyproject.toml`, `ruff`, `pytest`, type hints throughout, Pydantic v2 for schemas/config (constitution §3).
- TDD for scoring, statistics, and parsing logic (constitution §3): failing test → minimal implementation → pass → commit.
- No provider plugin system; exactly three clients (spec §2). No dataset-generation tooling ships (constitution §5).
- Temperature 0 for candidates and judge (spec §2/§4) — asserted on outgoing requests in client tests.
- Retries: transport errors only (429/5xx/timeout), capped at 4 attempts (config `retry_max_attempts`), jittered exponential backoff; a returned response is never re-sampled (spec §9).
- Config defaults are the decided D2/D3 values (K=3, K_baseline=6, margin 2.0, alpha 0.05); changing them requires a dated decision-log amendment (spec §9).
- Golden and calibration items are never used for prompt tuning; prompt iteration only on `data/dev/` (spec §3).
- Commit messages: subject + change summary; no attribution or process-status lines.
- Every task ends green: `uv run pytest` and `uv run ruff check` pass before its commit.

## File structure (decomposition contract)

```
src/harness/
  config.py            # Pydantic config + YAML load + run fingerprint (all spec §7 fields incl. judge_version)
  schema.py            # EmailInput, TicketExtraction (permissive, candidate-facing), GoldenItem
                       #   (reference-side strict validation), CalibrationLabel, Certificate
  prompts.py           # PromptTemplate {version, render(email)}, EXTRACTION_PROMPT, DEGRADED_DEMO_PROMPT
  models/__init__.py   # ModelClient protocol + StructuredResult (incl. raw payload)
  models/anthropic_client.py | openai_client.py | gemini_client.py
  models/retry.py      # transport-only retry/backoff decorator
  judge/rubric.py      # rubric text, few-shots, judge_version() hash
  judge/judge.py       # judge_field() — one field per call, error state distinct from fail
  scoring/deterministic.py  # normalization + exact-match per-field scores
  scoring/composite.py      # per-email composite, CompositeMode (FULL_7 | DETERMINISTIC_5)
  stats/permutation.py # sign_flip_test: full enumeration m<=20, else MC with (b+1)/(B+1)
  stats/bootstrap.py   # bca_ci with optional cluster resampling
  stats/agreement.py   # cohens_kappa + prevalence + cluster-bootstrap CI
  stats/mde.py         # minimum detectable effect (one-sided convention, matches gate)
  stats/variance.py    # between-item vs between-replicate decomposition (full + judged-only)
  gate/baseline.py     # baseline artifact I/O + fingerprint check + guardrail noise floor
  gate/gate.py         # decision rule, sparse-delta warnings, adversarial guardrail, exit codes
  runner.py            # items x replicates x candidates, bounded concurrency, persist/resume
  calibrate.py         # agreement report + certificate.json + self-consistency measurement
  tracing.py           # Langfuse spans + bounded degradation (spec §8)
  reports.py           # markdown rendering, certificate header (incl. uncalibrated state)
  cli.py               # Typer: run | compare | gate | calibrate | rescore
tests/unit/…           # mirrors src; tests/fixtures/ holds canned API payloads & run artifacts
data/dev/ data/golden/ data/calibration/  baselines/  configs/default.yaml
docs/gate-design.md    # analytic false-alarm justification + threat model + re-baseline procedure
.github/workflows/eval-gate.yml
```

## Build order and owner gates

Phases honor spec §11's load-bearing order. **◆ = owner validation gate.** Owner data work (Phase B) overlaps library work (Phase A); calibration labeling is scheduled at the earliest possible date, with both annotators labeling in parallel (D2 amendment 2026-07-09) — no calendar gap to block shipping, since T19's entry criterion is the committed certificate.

- **Phase A — library (T1–T11):** skeleton → scoring → stats → clients → judge → runner → tracing → reports → CLI.
- **Phase B — data (T12–T14, owner-heavy):** dev set + prompt freeze ◆ → golden draft + open-coding + freeze ◆ → calibration labeling + certificate ◆. B starts as soon as T6 lands.
- **Phase C — gate (T15–T17):** baseline module → gate + demo mode + real committed baselines → gate-design doc + no-change runs.
- **Phase D — ship (T18–T20):** CI workflow → README + published artifacts ◆ → acceptance walk.

---

## Tasks

### T1 — Project skeleton, config, schema, prompt plumbing
**Files:** create `pyproject.toml`, `configs/default.yaml`, `src/harness/{config,schema,prompts}.py`, tests
**Produces:** `Config` (all spec §9 fields incl. `k=3`, `k_baseline=6`, `retry_max_attempts=4`, price snapshot); `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` (all spec §7 fields; judge_version passed as opaque string); `TicketExtraction` — **permissive** candidate-facing model (`order_id: str | None`, no pattern — the provider-bound JSON schema must contain no unsupported keywords); `GoldenItem` with reference-side strict validation (`expected.order_id` must match `ORD-\d{5}`); `EmailInput`, `CalibrationLabel`, `Certificate`; `prompts.py` with `PromptTemplate{version, render(email) -> str}` and a placeholder `EXTRACTION_PROMPT` (text frozen in T12; plumbing lives here from day one).
**Test anchors:** config round-trips `default.yaml` asserting k=3/k_baseline=6/retry cap 4; unknown key → `ValidationError`; fingerprint changes when any component **including judge_version** changes, stable across dict ordering; candidate-side `TicketExtraction` accepts `ord-12345` (normalization is scoring's job), reference-side `GoldenItem` rejects it; `priority: "URGENT"` rejected.
- [ ] failing tests → minimal impl → green → `uv run ruff check` → commit

### T2 — Deterministic scoring + composite
**Files:** create `src/harness/scoring/{deterministic,composite}.py`, tests
**Consumes:** `TicketExtraction` (T1). **Produces:** `score_deterministic(expected, actual) -> dict[str, int]`; `normalize(s: str|None) -> str|None` (trim, casefold, collapse whitespace); `composite(field_scores, mode: CompositeMode) -> float` (unweighted mean over included fields ×100).
**Test anchors:** `" Jane  DOE "` matches `"jane doe"`; `None` matches only `None` (empty string ≠ None); `ORD-12345` vs `ord-12345` matches after normalization (reachable — candidate model is permissive per T1); FULL_7 vs DETERMINISTIC_5 differ on a fixture with judge scores; all-passing composite = 100.0.
- [ ] failing tests → impl → green → commit

### T3 — Sign-flip permutation test
**Files:** create `src/harness/stats/permutation.py`, tests
**Produces:** `sign_flip_test(deltas, *, sided: Literal["one","two"], n_resamples=10_000, seed) -> PermutationResult{p, m_nonzero, method: "exact"|"monte_carlo", min_attainable_p}`; full enumeration when `m_nonzero <= 20`, else Monte Carlo with `p = (b+1)/(B+1)`; `min_attainable_p` = `2^-m` in exact mode, `1/(B+1)` in MC mode.
**Test anchors:** m=2 all-negative → exact one-sided p = 0.25; m=5 all-negative → p = 0.03125 (rejection possible — the spec §7 m=5 case); all-zero deltas → p = 1.0, m=0; MC agreement with `scipy.stats.permutation_test((deltas,), np.mean, permutation_type="samples", alternative="less", n_resamples=10_000)` within `4*sqrt(p(1-p)/B)` on a 40-delta fixture; seeded MC reproducible.
- [ ] failing tests → impl → green → commit

### T4 — BCa bootstrap with cluster resampling
**Files:** create `src/harness/stats/bootstrap.py`, tests
**Produces:** `bca_ci(values, statistic=np.mean, *, level, clusters: Sequence[Hashable]|None, n_resamples=10_000, seed) -> (lo, hi)`; cluster mode resamples whole clusters.
**Test anchors:** matches `scipy.stats.bootstrap(method="BCa")` within tolerance on a reference sample; cluster CI on perfectly-correlated within-cluster data wider than naive CI on the same points; 0.90 CI nests inside 0.95.
- [ ] failing tests → impl → green → commit

### T5 — Agreement, MDE, variance decomposition
**Files:** create `src/harness/stats/{agreement,mde,variance}.py`, tests
**Consumes:** `bca_ci` (T4). **Produces:** `cohens_kappa(a, b, *, clusters=None) -> KappaResult{kappa, ci, raw_agreement, prevalence}`; `mde(delta_sd, n, *, alpha=0.05, power=0.80) -> float` where **z_alpha is the one-sided (1−α) quantile, matching the gate's one-sided test**; `variance_components(scores: item×replicate array) -> {between_item, between_replicate}` — callers run it twice: full composite and judged-fields-only (the spec §6 "judged-field run variance separated" requirement; report wiring in T10/T11).
**Test anchors:** kappa matches `sklearn.metrics.cohen_kappa_score` incl. a skewed 90/10 prevalence fixture; perfect → κ=1, independent → κ≈0; `mde(12.0, 32) == 5.27 ± 0.01` (one-sided; two-sided 1.960 would give 5.94 and fail); variance components recover simulated ratios, incl. a judged-only fixture.
- [ ] failing tests → impl → green → commit

### T6 — Model clients (candidates) + retry
**Files:** create `src/harness/models/{__init__,anthropic_client,openai_client,retry}.py`, tests (mocked transports); `tests/live/test_smoke.py` (marked `live`, excluded from CI)
**Consumes:** T1 (`TicketExtraction`, `PromptTemplate`). **Produces:** `ModelClient` protocol: `complete_structured(prompt: str, schema: type[BaseModel]) -> StructuredResult{output: BaseModel|None, failure: None|"schema_invalid"|"refusal", raw: str, usage, served_model_version}` — `raw` is always populated, including on failures (spec §6/§7 raw persistence); `retry_transport(max_attempts=config)` decorator.
**Test anchors:** mocked 429→429→200 succeeds; 4×429 → `TransportExhausted`; returned-but-invalid JSON → `failure="schema_invalid"` **with `raw` populated**, never retried; refusal → `failure="refusal"`; `served_model_version` captured from both SDK response shapes; outgoing requests assert Anthropic `output_config.format` / OpenAI strict `json_schema`, **temperature=0**, and a JSON schema free of unsupported keywords (no `pattern`).
- [ ] failing tests → impl → green → live smoke run once locally, output pasted into PR notes → commit

### T7 — Judge client + rubric versioning
**Files:** create `src/harness/judge/{rubric,judge}.py`, `src/harness/models/gemini_client.py`, tests (mocked)
**Consumes:** `ModelClient` (T6). **Produces:** `judge_version() -> str` (hash of model id + prompt + rubric + few-shots) — consumed by T1's fingerprint at call sites (T15/T16); `judge_field(email, field_name, reference, candidate_value) -> JudgeResult{verdict: "pass"|"fail"|None, error: str|None, rationale, raw: str}`; few-shots module docstring states the provenance rule (dev-set/hand-written only, spec §4).
**Test anchors:** valid mocked verdict parses; unparseable/refused judge output → `verdict=None, error=...` with `raw` populated (never "fail"); changing one few-shot changes `judge_version()`; rubric text matches spec §4 verbatim; outgoing judge request asserts **temperature=0**.
- [ ] failing tests → impl → green → commit

### T8 — Runner with persistence and resume
**Files:** create `src/harness/runner.py`, tests using fake clients
**Consumes:** T1/T2/T6/T7. **Produces:** `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir` — bounded concurrency; per-(item, replicate) JSONL rows `{item_id, replicate, raw_output, raw_judge, field_scores, usage, served_model_version, judge_rationales}` written incrementally; `load_run(run_dir) -> RunArtifact`.
**Test anchors:** interrupt after item 3 → re-invoke resumes at item 4 (fake-client call counts prove no re-spend); `TransportExhausted` mid-run aborts with partial file intact, exit distinct from completion; judge-error rows carry `verdict=None` and are counted; the prompt's version lands in the run fingerprint.
- [ ] failing tests → impl → green → commit

### T9 — Tracing with bounded degradation
**Files:** create `src/harness/tracing.py`, tests with fake Langfuse transport
**Consumes:** runner hooks (T8). **Produces:** `TraceContext.for_run(config, reportable: bool)` — spans per spec §8; `reportable=True` + missing keys → `MissingTracingError` at startup; keyless dev run proceeds with warning and `untraced=True` in the artifact; mid-run transport failure → run completes, flagged untraced.
**Test anchors:** the three context behaviors above (CLI fail-fast wiring is asserted where the commands exist: T11 for calibrate, T16 for gate/baseline).
- [ ] failing tests → impl → green → commit

### T10 — Reports
**Files:** create `src/harness/reports.py`, tests (golden-file)
**Consumes:** `RunArtifact` (T8), stats (T3–T5), `Certificate` (T1). **Produces:**
- `render_run_report`: composite mean per slice (nominal/adversarial/all) with 95% BCa cluster CIs, per-field accuracies, per-category table, variance decomposition (full + judged-only), score-vs-length correlation per candidate;
- `render_compare_report`: mean delta + 95% BCa CI, two-sided permutation p, per-field pass-rate delta tables with flip counts (majority vote across replicates), absolute scores alongside deltas;
- `render_gate_summary`: verdict per candidate, delta + 90% BCa CI, one-sided p, m + sparse-delta warnings, MDE, judge-error exclusion count, adversarial delta (always) + guardrail status, family false-alarm rate line (two tests at α=0.05 → ≤ ~9.8%), config values used (margin/alpha/K), token totals + approx cost from price snapshot, relative link to `docs/gate-design.md`;
- every report embeds the certificate header — including an explicit **"uncalibrated (no certificate)" state** for dev-stage runs, rendered as a banner and disallowed on reportable runs (T9 flag); untraced artifacts render the untraced banner.
**Test anchors:** golden-file comparisons for all three renderers on fixture runs; inadequate-verdict fixture shows judged-fields-excluded flag; missing-certificate fixture shows the uncalibrated banner.
- [ ] failing tests → impl → green → commit

### T11 — CLI: run, compare, rescore
**Files:** create `src/harness/cli.py`, tests via `typer.testing.CliRunner` + fake clients
**Consumes:** everything above. **Produces:** `eval run --model {a|b} [--dataset <path>]` (dev-path runs are non-reportable and fingerprint the dev dataset version), `eval compare` (artifact-reuse when fingerprints match, else re-run), `eval rescore <run-dir>` (recomputes reports from persisted raw outputs — zero client construction).
**Test anchors:** `rescore` on a fixture run dir reproduces a byte-identical report with fake clients never instantiated; `compare` with matching fingerprints calls no clients; `run --dataset data/dev/dev.jsonl` produces an untraced-allowed, non-reportable artifact; `eval calibrate` (wired in T14) fails fast without Langfuse keys — anchor added there.
- [ ] failing tests → impl → green → commit

### T12 ◆ — Dev set + prompt freeze
**Files:** create `data/dev/dev.jsonl` (10 items); finalize `EXTRACTION_PROMPT` text in `src/harness/prompts.py`
**Owner work:** review/edit 10 dev emails; iterate the extraction prompt against dev only; freeze `prompt_version: 1`. (Plumbing exists since T1 — this task freezes text and version only.)
**Verification:** `eval run --dataset data/dev/dev.jsonl` completes for both candidates in the uncalibrated-banner state; prompt contains the spec §1 tie-break sentence verbatim; **◆ owner signs the freeze**.

### T13 ◆ — Golden set: draft → open-coding → freeze
**Files:** create `data/golden/golden.jsonl` (50 items), `data/golden/taxonomy.md`
**Owner work (~3–4 h):** curate model-drafted emails per D4 taxonomy (drafting assisted ad hoc — no tooling ships); verify ≥80% generator-family bound and per-category ≥2; **review every multi-request item's expected values against the canonical tie-break rule**; run both candidates once (uncalibrated state); open-code outputs; adjust; freeze `dataset_version: 1`.
**Verification:** a unit test reconciles taxonomy.md counts with jsonl tags; 32/18 slice split; reference-side validation passes (T1 strict `GoldenItem`); **◆ owner signs the freeze**.

### T14 ◆ — Calibration: labels, agreement, certificate, self-consistency
**Files:** create `data/calibration/{emails.jsonl,labels.jsonl,certificate.json}`, `src/harness/calibrate.py`, CLI `eval calibrate`
**Consumes:** T5, T7, T8, T9. **Owner work:** label 100 field judgments (+ spec §5 stratification loop additions if fail rate < 20%; ~2 h); adjudicate whatever disagreements the second, independently-labeling annotator's labels surface (dual-annotation upgrade, D2 amendment 2026-07-09 — no calendar gap to schedule).
**Produces:** `certificate.json` per spec §5; calibrate report with cluster-bootstrap CIs, prevalence, per-candidate kappas, gray-zone logic, **per-candidate kappa gap > 0.2 → D1-review flag**; **judge self-consistency: 20 fixed (email, reference, candidate-value) triples each judged 3×, verdict flip rate in the report and certificate context**; `eval calibrate` is reportable (fails fast without Langfuse keys — T9 anchor lands here); resolves dual-annotation gold + the human-human agreement ceiling automatically once both annotators' labels are complete.
**Verification:** certificate verdict correct on synthetic fixtures for all three states + gray zone + divergence flag; mocked judge with one flipping triple → flip rate 1/20; dual-annotation gold resolution and the human-human ceiling row verified (agreement/adjudication/incomplete-coverage/binding-mismatch fixtures); **◆ owner signs the certificate**.

### T15 — Baseline module
**Files:** create `src/harness/gate/baseline.py`, tests
**Consumes:** T1 fingerprint (incl. T7 `judge_version()`), runner (T8). **Produces:** baseline artifact I/O per spec §7 (per-item, per-replicate, per-field scores, raw outputs, fingerprint, K=6); `generate_baseline(config, model_key) -> BaselineFile` as a library callable (CLI wiring lands in T16); `check_fingerprint(baseline, run) -> list[Mismatch]`; adversarial-guardrail noise floor measured and recorded at baseline time (threshold verified ≥3× measured SE).
**Test anchors:** fingerprint mismatch enumerates differing fields (incl. judge_version drift); v0-format file fails loudly; guardrail floor check fails if 10 points < 3× measured SE.
- [ ] failing tests → impl → green → commit

### T16 — Gate command + real baselines
**Files:** create `src/harness/gate/gate.py`, `DEGRADED_DEMO_PROMPT` in `prompts.py`, CLI `eval gate [--update-baseline|--seed-regression]`, tests; generate and commit `baselines/{a,b}.json`
**Consumes:** T3–T5, T8–T10, T15. **Produces:** spec §7 exactly — nominal-slice K-averaged paired deltas; fail iff one-sided p < 0.05 AND mean regression > 2.0; sparse-delta warnings (**m ≤ 4: rejection impossible; m = 5: requires all five deltas negative, min p = 0.031** — spec errata 2026-07-04); **certificate verdict `inadequate` → deltas on DETERMINISTIC_5, judged fields excluded, flagged**; **items with any missing judged field excluded from paired deltas, exclusion count printed**; adversarial guardrail (≥10-point drop → fail, "coarse" label); either-candidate combination; judge-error budget (>5% → exit 2); exit codes 0/1/2; missing baseline → exit 2 with `--update-baseline` instruction (never auto-create; prints the attach-compare-report instruction for prompt-bump PRs); `--seed-regression` applies `DEGRADED_DEMO_PROMPT` at runtime, skips the fingerprint check, banners DEMO MODE; gate/baseline paths are reportable (fail fast without Langfuse keys).
**Test anchors (synthetic fixtures):** unchanged → exit 0; 12-point regression on 20 items → exit 1; on 3 items → exit 0 + "impossible" warning; on exactly 5 items each regressing 15 points (mean 2.34 > the 2.0 margin), all-negative → p=0.031 → exit 1 with the m=5 notice; adversarial-only 15-point drop → exit 1 via guardrail; 2 judge-error items → excluded, count printed, exit unchanged; 6% judge-error rate → exit 2; inadequate-certificate fixture → 5-field deltas used + flag; fingerprint mismatch (incl. judge_version) → exit 2; `--seed-regression` → DEMO MODE banner.
- [ ] failing tests → impl → green → **generate real committed baselines (K=6, traced, reportable) and smoke-run the gate against them → exit 0** → commit

### T17 — Gate-design doc + no-change demonstration
**Files:** create `docs/gate-design.md`; results table appended there
**Produces:** the analytic false-alarm justification (exact permutation level, conditional-on-frozen-baseline caveat, K_baseline reasoning, margin non-operativity note), **the spec §7 threat-model statement, and the prompt-change re-baseline PR procedure** (run `--update-baseline`, attach the compare-vs-old-baseline report). Then 10 no-change gate runs executed **locally with full keys (traced)**; observed false-alarm count recorded here and summarized in the README (T19).
**Verification:** doc committed; 10-run table present; the T10 gate-summary link resolves.

### T18 — CI workflow
**Files:** create `.github/workflows/eval-gate.yml`
**Produces:** spec §11 wiring — same-repo PRs only (fork PRs skip, documented), path filter (`src/`, prompts, `data/`, `baselines/`, `configs/`, workflow file), `pull-requests: write`, 30-min timeout, **five secrets (three providers + two Langfuse keys — CI gate runs are traced)**, gate markdown to job summary **and** PR comment, `workflow_dispatch` demo input.
**Verification:** docs-only PR skips the gate; `src/` PR runs it; exit 1 vs exit 2 produce visibly distinct check labels; `workflow_dispatch --seed-regression` fails with DEMO MODE banner (log linked in ticket).

### T19 ◆ — README, published artifacts, demo
**Entry criteria (simplified, D2 amendment 2026-07-09):** the calibration certificate exists — `eval calibrate` has resolved both annotators' labels into gold and the human-human ceiling row is present in the certificate report. No calendar gap to schedule.
**Files:** create/replace `README.md`, `results/published/` (committed artifacts for every README number), `.gitignore` exception
**Produces:** the before/after story per AC3+AC4: scores ± CI, agreement ± CI + the human-human ceiling row, gate verdicts, MDE, m, **the 10-run no-change table summary with observed false-alarm count (linking gate-design.md)**; quickstart; the two-command demo; honest-limitations section (dual annotation's own limits, n, MDE, alias pinning).
**Verification (AC3/AC4/AC5):** run numbers reproduce byte-exact via `eval rescore results/published/<run>`; agreement numbers reproduce via `eval calibrate`/`eval calibrate --offline` on committed labels — both zero API calls (spec AC5 errata); **◆ owner reviews README**.

### T20 — Acceptance walk
Walk all five spec §12 criteria against the shipped repo, recording evidence (commands + outputs) in the ticket. Any failure loops back to its owning task. Completion closes the SDD loop (constitution DoD).

---

## Self-review (performed, updated after adversarial review)

- **Spec coverage:** §1–§2 → T1/T2/T6/T12; §3 → T13; §4 → T7 + T14 (self-consistency triples in T14's Produces) + T10 (score-vs-length); §5 → T14; §6 → T2–T5/T10/T11; §7 → T15–T17 (threat model + re-baseline in T17; degraded mode, judge-error exclusion, m=5 wording in T16); §8 → T9 (+T11/T14/T16 fail-fast anchors); §9 → T11/T16 + Global constraints (retry cap, config guardrail); §10 → T1/T19; §11 → phase order + T18; §12 → T20 (AC1→T11, AC2→T14, AC3→T16–T19, AC4/AC5→T19).
- **Type consistency:** `StructuredResult.raw`/`JudgeResult.raw` (T6/T7) feed T8's row schema and AC5's rescore path; `judge_version()` (T7) feeds T1's fingerprint signature at T15/T16 call sites; `CompositeMode` names match T2/T15/T16; `PromptTemplate` (T1) is consumed by T6/T8/T12/T16.
- **Spec erratas applied (owner sign-off requested with plan validation):** (1) §7 sparse-delta wording corrected — impossibility holds at m ≤ 4, m = 5 has min p = 0.031; (2) §9 retry cap pinned at 4 attempts, config-exposed; (3) AC5 recomputability names both paths (`eval rescore` for run numbers, `eval calibrate` for agreement numbers).
