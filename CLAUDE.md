# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A structured-extraction eval harness with a calibrated LLM judge and CI gating. It benchmarks two LLMs (Claude Haiku 4.5 vs GPT-5.4 mini) on **one** task — customer support email → structured ticket — scored by a Gemini judge whose agreement with human labels is measured, and gated in CI on statistically meaningful regressions.

**It is a portfolio artifact, not a product.** The repo itself is the thing under scrutiny; every design choice must survive an engineer who builds evals for a living. When polish trades against measurement honesty, honesty wins (`docs/constitution.md` §1–2). This means the *process* and the *docs* are as load-bearing as the code — read the governance section below before making design changes.

## Governing documents (read before non-trivial work)

The project is spec-driven; these markdown files are the contract, in precedence order:

- `docs/constitution.md` — principles + locked v1 scope + the four owner decisions. Changes rarely, only with owner sign-off.
- `docs/spec.md` — the behavioral contract. Owns every operational number and rule (schema, K values, gate thresholds, calibration procedure).
- `docs/decisions.md` — D1–D4 choices and rationale (judge selection/bias, judge calibration, CI gating, golden-set design).
- `docs/plan.md` + `docs/tickets/` — the T1–T20 implementation plan; tickets are the unit of owner validation.
- `docs/gate-design.md` — the gate's analytic false-alarm justification, threat model, and re-baseline procedure (linked from every gate summary).

**Amendments are dated entries, never edits.** When a decision changes, add a dated amendment paragraph; do not rewrite the original. The four decisions D1–D4 are owner territory — present tradeoffs and recommend, never decide them in code or autonomously.

Current build state lives in `.superpowers/sdd/progress.md` (git-ignored ledger, the most current record) and `HANDOFF-TWO.md` (narrative index). Read both to know what's done and what's next.

## Commands

Package manager is `uv`. Python 3.12+.

```bash
uv sync                        # install deps (uses uv.lock)
uv run pytest -q               # run the unit suite (live tests excluded by default)
uv run pytest -m live          # run live tests — HITS REAL PROVIDER APIS, needs keys + spends money
uv run ruff check .            # lint (E, F, I, UP, B; line-length 100)
uv run ruff check --fix .      # lint + autofix

# single test file / node:
uv run pytest tests/unit/stats/test_permutation.py
uv run pytest "tests/unit/stats/test_permutation.py::TestExactModeAnchors::test_m5_all_negative_deltas_one_sided_p_is_one_over_32"
```

**Every commit must be green: `uv run pytest -q` passing with 0 warnings AND `uv run ruff check .` clean.** The `-m "not live"` default is set in `pyproject.toml`; live tests (`tests/live/`) are opt-in only.

### The CLI (`eval`, a Typer app — `src/harness/cli.py`)

```bash
uv run eval run --model {a|b} [--dataset <path>]   # eval one candidate → markdown + JSONL
uv run eval compare                                # both candidates + paired comparison report
uv run eval gate [--update-baseline [--model {a|b}] | --seed-regression]  # CI gate vs baseline
uv run eval calibrate [--offline]                  # agreement report + certificate from committed labels
uv run eval rescore <run-dir>                      # recompute all reports from persisted raw outputs — ZERO API calls
```

- `eval gate` exit codes: **0** = pass, **1** = regression detected, **2** = measurement error (missing baseline / fingerprint mismatch / judge-error budget exceeded / aborted run). Never auto-creates baselines.
- `--update-baseline` regenerates and commits both baselines atomically by default; `--model {a|b}` restricts to one candidate (added because the judge provider's daily quota can't fit a dual-candidate run — don't run two single-candidate invocations concurrently, they share a `.staging` dir).
- `--seed-regression` injects a documented prompt degradation, skips the fingerprint check, and banners **DEMO MODE**. It is mutually exclusive with `--update-baseline`.
- `eval calibrate --offline` recomputes the certificate from `judgments.jsonl` + labels with zero API calls; the live path requires Langfuse keys (fails fast without them).

## Architecture

A thin, typed library (`src/harness/`) with **pure-function statistics and scoring at the core**, three hand-written model clients behind one protocol, a run loop that persists raw outputs incrementally, and a CLI whose five commands compose those pieces. All measurement logic is unit-tested against reference implementations (scipy/sklearn) before any live call.

```
src/harness/
  config.py       # Pydantic config + YAML load + run FINGERPRINT (the identity of a run)
  schema.py       # TicketExtraction (permissive, candidate-facing — no regex patterns, provider schemas reject them),
                  #   GoldenItem (strict reference-side), CalibrationLabel, Certificate
  prompts.py      # PromptTemplate{version, render}; EXTRACTION_PROMPT (frozen at v4), DEGRADED_DEMO_PROMPT
  models/         # ModelClient protocol + StructuredResult; anthropic/openai/gemini clients; retry.py (transport-only)
  judge/          # rubric.py (rubric text + few-shots + judge_version() hash), judge.py (one field per call)
  scoring/        # deterministic.py (normalize + exact match), composite.py (per-email mean; FULL_7 | DETERMINISTIC_5)
  stats/          # permutation, bootstrap (BCa + cluster), agreement (Cohen's kappa), mde, variance — all pure
  gate/           # baseline.py (artifact I/O + fingerprint check + guardrail floor), gate.py (decision rule, exit codes)
  runner.py       # items × replicates × candidates; bounded concurrency; persist/resume by-item
  calibrate.py    # agreement report + certificate.json + self-consistency + dual-annotation gold resolution
  tracing.py      # Langfuse spans + bounded degradation
  reports.py      # markdown rendering; embeds the calibration certificate header on every report
  cli.py          # Typer composition root — the ONLY place that constructs provider SDK clients
```

Load-bearing invariants that span files:

- **Reproducibility by fingerprint.** A run is fully identified by `fingerprint(config, served_versions, judge_version, composite_mode, calibration_verdict)` (`config.py`). The gate exits 2 on any mismatch vs baseline. `served_model_version` is captured from each provider's response metadata (alias-drift guard against undated model aliases). `judge_version()` (`judge/rubric.py`) is a hash of {judge model, prompt, rubric, few-shots} — changing any of them invalidates the calibration certificate.
- **Raw outputs are always persisted**, including on failures. `StructuredResult.raw` / `JudgeResult.raw` feed the runner's per-row JSONL, which is what `eval rescore` and `eval calibrate --offline` recompute from — the AC that every README number is byte-exactly recomputable with zero API calls.
- **Runner never imports provider SDKs** — clients are injected from `cli.py`. This keeps the runner testable with fake clients and enables the persist/resume seam (an aborted run resumes by-item rather than re-spending calls).
- **Candidate failure vs measurement error are different.** Schema-invalid output or refusal → all 7 fields scored 0 (a real candidate failure, scored). Transport error surviving retries → the run aborts as a measurement error (never scored). A judge error → the field is *missing* (excluded from paired deltas, not scored as fail — judge failures can never register as candidate regressions).
- **Retries are transport-only** (429/5xx/timeouts), capped-jittered backoff with a 600s wall-clock patience budget (`models/retry.py`). A successfully returned response is *never* re-sampled regardless of content — retries-as-quality-strategy is an explicit constitutional cut.

### The judge and calibration (D1/D2 — the project's centerpiece)

The judge is pointwise and reference-guided: each free-text field is judged in its own call against a binary rubric, Pydantic-validated. It is **calibrated** — validated against a dual-annotated human label set (owner + a second annotator, disagreements adjudicated by the owner), with Cohen's kappa the single agreement statistic and a human-human agreement *ceiling* reported alongside it. All CIs are **cluster bootstrap resampling emails** (fields/candidates within an email are correlated). The certificate (`data/calibration/certificate.json`, committed) gates whether judged fields count: adequate (κ̂ ≥ 0.6) → judged; inadequate → judged fields excluded from the gate (DETERMINISTIC_5 mode) and flagged everywhere. A **fail-probe perturbation set** enriches fail prevalence to ≥20% because the real calibration emails pass ~100% at k=1.

### The gate (D3)

Nominal-slice (n=32), one-sided sign-flip permutation test on K-averaged paired per-item deltas vs baseline; full enumeration when nonzero deltas m ≤ 20, else Monte Carlo. A candidate fails on **either** of two independent paths: the statistical path (**p < 0.05 AND mean regression > 2.0 points**), or the coarse non-statistical **adversarial guardrail** (hard-fail if the adversarial slice drops ≥10 pts). Prints m with sparse-delta warnings (at m ≤ 4 no rejection is possible), the MDE (source of truth for detection limit, ~5–7 pts at this n), and the adversarial delta. K=3 for gate/compare runs, K_baseline=6 for committed baselines.

## Data layout

```
data/golden/         # golden.jsonl (50 items, frozen dataset v1) + taxonomy.md coverage contract
data/calibration/    # emails.jsonl, emails-fail-probe.jsonl, perturbations.jsonl, labels.jsonl,
                     #   certificate.json, judgments.jsonl
data/dev/            # ~10-item scratch set for prompt iteration — NEVER used for reported numbers
baselines/           # committed per-candidate gate baselines (per-item, per-replicate, per-field, raw outputs)
results/             # generated reports — gitignored EXCEPT results/published/ (README evidence)
configs/default.yaml # model ids, prompt version, dataset version, K, gate margin/alpha, price snapshot
tools/               # csv-grader.html + grade.py — local browser grader for calibration labeling sheets
```

Golden and calibration items are **never used for prompt tuning** (constitution Principle 6); prompt iteration happens only on `data/dev/`. Changing `default.yaml`'s margin/alpha/K requires a dated decision-log amendment. Post-freeze edits to the golden set bump `dataset.version` and invalidate baselines.

## Workflow conventions

- **Commit messages:** imperative subject + change-summary body. NO `Co-Authored-By`/attribution lines, no process-status words ("review", "owner", "signed", "validated") outside doc files, plain words over jargon. Keep to what changed and why.
- **TDD** for scoring, statistics, and parsing logic: failing test → minimal impl → green → commit (constitution §3).
- **Review loop (owner ruling):** lean single-reviewer loop only — one implementer, one reviewer, a fix pass. Do NOT run multi-agent / ultracode review workflows (one burned >30% of the owner's Fable budget). Reserve the most capable model for the final whole-branch review (T20).
- **Data work:** new/edited dataset items get dual blind label audits (independent agents re-deriving every expected value) before the owner reviews them.

## Environment quirks (Windows / PowerShell)

- The repo path contains a space (`Model Evaluation Harness`) — quote it in commands.
- Console encoding is cp1252 — set `$env:PYTHONIOENCODING='utf-8'` before `eval` commands or Unicode (e.g. `→`) in reports crashes `typer.echo`.
- PowerShell here-strings mangle `git commit -m` — write the message to a scratchpad file and use `git commit -F <file>`.
- Background/`run_in_background` commands are killed at ~10 min. For long live runs use a detached `Start-Process` + PID file + the `Monitor` tool; machine sleep/restart kills detached processes, but scripts and logs survive in the scratchpad (relaunch is one command).
- The repo is **local and unpushed** (no remote yet) — legitimate draft editing, but content erasure is the line not crossed.
