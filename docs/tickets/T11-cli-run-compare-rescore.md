# T11 — CLI: run, compare, rescore

**Phase:** A · **Depends on:** T01, T02, T03, T04, T05, T06, T07, T08, T09, T10 · **Owner gate:** no
**Sources:** plan.md task T11 · spec.md §8, §9 (partial groundwork for AC1/AC5)

## Goal
Wire the Typer CLI's three Phase-A commands — `run`, `compare`, `rescore` — composing the runner, tracing context, and renderers; `gate` and `calibrate` are wired later (T16 / T14).

## Deliverables
- `src/harness/cli.py`
- `pyproject.toml` modified: register the `eval` script entry point so `uv run eval <command>` works (spec §9)
- `tests/unit/` tests via `typer.testing.CliRunner` + fake clients

## Interfaces
**Consumes (copied verbatim from plan.md):**
- `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir`; `load_run(run_dir) -> RunArtifact` (T08)
- `TraceContext.for_run(config, reportable: bool)` (T09)
- `render_run_report` / `render_compare_report` (T10)
- `Config`; `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` (T01)
- `ModelClient` protocol implementations (T06/T07) — constructed only when a live run is required

**Produces (copied verbatim from plan.md):**
- `eval run --model {a|b} [--dataset <path>]` (dev-path runs are non-reportable and fingerprint the dev dataset version)
- `eval compare` (artifact-reuse when fingerprints match, else re-run)
- `eval rescore <run-dir>` (recomputes reports from persisted raw outputs — zero client construction)

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] `rescore` on a fixture run dir reproduces a **byte-identical** report, and the test proves fake clients are **never instantiated** (constructor call count == 0 — zero client construction, not merely zero calls)
  - [ ] `compare` with two existing run artifacts whose fingerprints match reuses them: fake-client call count == 0, comparison report produced
  - [ ] `compare` with mismatched fingerprints re-runs (fake clients are invoked)
  - [ ] `run --dataset data/dev/dev.jsonl` (fixture dev path) with no Langfuse keys produces an untraced-allowed, **non-reportable** artifact: warning emitted, `untraced=True` in the artifact, and the dev dataset version lands in the fingerprint
- [ ] `uv run eval --help` exits 0 and lists `run`, `compare`, `rescore`
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- All Global constraints from plan.md apply (referenced, not restated).
- `eval gate [--update-baseline|--seed-regression]` is wired in T16; `eval calibrate [--retest]` is wired in T14 — the "calibrate fails fast without Langfuse keys" anchor is added there (plan T11 note), not here. Do not stub these commands with fake behavior.
- Dev-path (`--dataset`) runs are non-reportable: they may run keyless/untraced (spec §8) but can never feed baselines or the README. Golden-set runs invoked without keys follow the T09 `TraceContext` contract for their `reportable` flag.
- `rescore` recomputes all scores/statistics/reports from persisted raw outputs with zero API calls (spec §9) — this is the spec AC5 recomputation path; keep report rendering entirely inside T10's renderers so `rescore` output is byte-stable.
- `compare` behavior per spec §6: reuses existing run artifacts when fingerprints match; re-runs otherwise; report shows absolute scores alongside deltas (rendering is T10's contract).
- Temperature 0 and the transport-only retry rule (max 4 attempts) are client/runner concerns (T06/T08) — the CLI must not add retry or sampling behavior of its own.
- TDD loop (Global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
