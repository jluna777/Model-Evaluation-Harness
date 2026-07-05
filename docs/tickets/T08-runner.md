# T08 — Runner with persistence and resume

**Phase:** A · **Depends on:** T01, T02, T06, T07 · **Owner gate:** no
**Sources:** plan.md task T8 · spec.md §6, §7, §9

## Goal
Implement the run loop — items × replicates × candidates with bounded concurrency — persisting every raw output incrementally so an aborted run resumes instead of re-spending API calls, and exposing a loader for downstream reports and baselines.

## Deliverables
- `src/harness/runner.py` (created)
- `tests/unit/test_runner.py` (created — fake clients; run-artifact fixtures in `tests/fixtures/`)

## Interfaces
**Consumes:**
- (T01) `Config`; `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str`; `TicketExtraction`; `PromptTemplate{version, render(email) -> str}`
- (T02) `score_deterministic(expected, actual) -> dict[str, int]`; `composite(field_scores, mode: CompositeMode) -> float`
- (T06) `ModelClient` protocol: `complete_structured(prompt: str, schema: type[BaseModel]) -> StructuredResult{output: BaseModel|None, failure: None|"schema_invalid"|"refusal", raw: str, usage, served_model_version}`
- (T07) `judge_field(email, field_name, reference, candidate_value) -> JudgeResult{verdict: "pass"|"fail"|None, error: str|None, rationale, raw: str}`

**Produces:**
- `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir` — bounded concurrency; per-(item, replicate) JSONL rows `{item_id, replicate, raw_output, raw_judge, field_scores, usage, served_model_version, judge_rationales}` written incrementally
- `load_run(run_dir) -> RunArtifact`

## Acceptance criteria
- [ ] Interrupt after item 3 → re-invoking `run_eval` on the same run dir resumes at item 4; fake-client call counts prove items 1–3 are not re-called (no re-spend)
- [ ] `TransportExhausted` mid-run aborts the run with the partial JSONL file intact on disk, and the abort outcome is programmatically distinct from a completed run (distinct return/exception, asserted by test)
- [ ] JSONL rows are written incrementally: a test asserts rows for completed (item, replicate) pairs exist on disk before the run finishes (e.g. by inspecting the file at the interrupt point)
- [ ] Judge-error rows persist `verdict=None` (never `"fail"`) and the run artifact counts them (judge-error count retrievable from `RunArtifact`)
- [ ] Candidate `schema_invalid` / `refusal` rows score all 7 fields 0 for that replicate with `raw_output` persisted (spec §6)
- [ ] The prompt's `version` lands in the run fingerprint — a test changes `PromptTemplate.version` and asserts the fingerprint differs
- [ ] `load_run` round-trips a fixture run dir into a `RunArtifact` whose rows match the persisted JSONL
- [ ] `uv run pytest tests/unit/test_runner.py` exits 0
- [ ] `uv run ruff check` exits 0

## Notes
- TDD loop (global constraints): failing test → minimal impl → green → `uv run ruff check` → commit. All tests use fake clients — no live API calls.
- Spec §9 constrains persistence: per-item results persist incrementally; an aborted run resumes rather than re-spending calls. Retries are transport-only, capped at 4 attempts (handled inside T06 clients); **a returned response is never re-sampled** — the runner must never re-invoke a client for an (item, replicate) that already produced a persisted row, whatever its content.
- Spec §6 constrains scoring at the row level: deterministic fields 0/1 via T02; free-text fields carry the judge verdict; schema-invalid or refusal → all 7 fields 0, raw persisted (a real candidate failure); transport-level failure surviving retries → the run aborts as a measurement error, never scored.
- Spec §7 constrains judge-error handling downstream: judge failure → the field is *missing* (not fail); the runner's job is to persist `verdict=None` faithfully and count it so T16 can exclude those items from paired deltas and enforce the >5% judge-error budget.
- Replicates: gate/compare runs use K=3 per item; baselines use K=6 (spec §6/§7) — `k` is a parameter, defaults come from `Config` (`k=3`, `k_baseline=6`).
- `raw_output`, `raw_judge`, and `judge_rationales` in every row are load-bearing for AC5: `eval rescore` (T11) recomputes all numbers from these persisted fields with zero API calls.
- Downstream consumers: T09 hooks tracing into this loop; T10 renders from `RunArtifact`; T15 builds baseline artifacts from runner output. Keep the row schema and both signatures exactly as above.
