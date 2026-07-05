# T09 — Tracing with bounded degradation

**Phase:** A · **Depends on:** T08 · **Owner gate:** no
**Sources:** plan.md task T9 · spec.md §8

## Goal
Implement Langfuse tracing with the spec §8 bounded-degradation contract: runs that produce reported numbers fail fast without credentials, dev iteration may proceed keyless but is flagged `untraced`, and a mid-run transport failure never aborts measurement.

## Deliverables
- `src/harness/tracing.py`
- `tests/unit/` tests using a fake Langfuse transport (no live Langfuse calls in tests)

## Interfaces
**Consumes:** runner hooks (T08): `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir` with per-(item, replicate) JSONL rows written incrementally; `load_run(run_dir) -> RunArtifact`.
**Produces (copied verbatim from plan.md):** `TraceContext.for_run(config, reportable: bool)` — spans per spec §8; `reportable=True` + missing keys → `MissingTracingError` at startup; keyless dev run proceeds with warning and `untraced=True` in the artifact; mid-run transport failure → run completes, flagged untraced.

Spec §8 span contract: every candidate and judge call is a span in a per-run Langfuse trace carrying run id, fingerprint, item id, replicate index; scores attached per item. Keys arrive via environment variables; v1 targets Langfuse Cloud free tier.

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors (the three context behaviors from plan T9):
  - [ ] `TraceContext.for_run(config, reportable=True)` with Langfuse keys absent from the environment raises `MissingTracingError` at startup — before any candidate or judge call is made (fake client call count is 0 when the error fires)
  - [ ] `TraceContext.for_run(config, reportable=False)` with keys absent proceeds: exactly one warning line is emitted and the resulting artifact carries `untraced=True`
  - [ ] fake Langfuse transport that starts failing mid-run → the run completes with all items scored, and the artifact is flagged untraced
  - [ ] with keys present and a working fake transport: captured spans form one per-run trace tagged with run id and fingerprint; there is one span per candidate call and one per judge call, each carrying item id and replicate index; scores are attached per item (spec §8)
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- All Global constraints from plan.md apply (referenced, not restated).
- CLI fail-fast wiring is asserted where the commands exist, not here: `eval calibrate` (command + fail-fast anchor land in T14) and `eval gate`/baseline paths (T16). This ticket only proves the `TraceContext` behaviors with fake transports. (Plan T9's parenthetical names T11 for calibrate; plan T11 and T14 place the calibrate command and its fail-fast anchor in T14 — follow T14.)
- Untraced artifacts can never feed baselines or the README (spec §8) — enforcement lives at the reportable call sites (T14/T16/T19); this ticket must make the `untraced` flag reliably present in the artifact so those sites and T10's banner can act on it.
- A mid-run Langfuse failure does not abort measurement: the run completes and gate verdicts stand; baseline updates and published numbers still require complete traces (spec §8).
- TDD loop (Global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
