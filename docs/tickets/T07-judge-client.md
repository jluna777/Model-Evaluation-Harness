# T07 — Judge client + rubric versioning

**Phase:** A · **Depends on:** T06 · **Owner gate:** no
**Sources:** plan.md task T7 · spec.md §2, §4 · decisions.md D1

## Goal
Implement the Gemini judge client and the pointwise, reference-guided judging function — one field per call, binary rubric, error state distinct from fail — plus the versioned rubric module whose hash pins the calibration certificate.

## Deliverables
- `src/harness/judge/rubric.py` (created — rubric text, few-shots, `judge_version()` hash)
- `src/harness/judge/judge.py` (created — `judge_field()`)
- `src/harness/models/gemini_client.py` (created — third and final client)
- `tests/unit/judge/test_rubric.py`, `tests/unit/judge/test_judge.py`, `tests/unit/models/test_gemini_client.py` (created — mocked transports; canned payloads in `tests/fixtures/`)

## Interfaces
**Consumes:** (T06) `ModelClient` protocol: `complete_structured(prompt: str, schema: type[BaseModel]) -> StructuredResult{output: BaseModel|None, failure: None|"schema_invalid"|"refusal", raw: str, usage, served_model_version}`; `retry_transport(max_attempts=config)` decorator.
**Produces:**
- `judge_version() -> str` (hash of model id + prompt + rubric + few-shots) — consumed by T01's fingerprint at call sites (T15/T16)
- `judge_field(email, field_name, reference, candidate_value) -> JudgeResult{verdict: "pass"|"fail"|None, error: str|None, rationale, raw: str}`

## Acceptance criteria
- [ ] Valid mocked judge response parses to `JudgeResult{verdict: "pass"|"fail", error: None}` with rationale and `raw` populated (Pydantic-validated output)
- [ ] Unparseable or refused judge output → `verdict=None, error=<reason>` with `raw` populated — a test asserts the verdict is **never** coerced to `"fail"`
- [ ] Changing one few-shot example changes `judge_version()`; unchanged inputs give a stable hash across runs
- [ ] Rubric text in `rubric.py` matches spec §4 verbatim: *pass = same factual content as the reference — same issue/action, no added claims, no missing essentials; wording may differ freely.* (test asserts the exact string is present)
- [ ] Outgoing judge request asserts **temperature=0**
- [ ] `judge_field` issues exactly one model call per invocation (one field per call — asserted via mock call count)
- [ ] The few-shots module docstring states the provenance rule: examples are hand-written or drawn exclusively from `data/dev/` — never from golden or calibration items (test asserts the docstring contains the rule)
- [ ] `uv run pytest tests/unit/judge tests/unit/models` exits 0
- [ ] `uv run ruff check` exits 0

## Notes
- TDD loop (global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
- Judge config (spec §2/§4): `gemini-3.5-flash` (stable GA; escalation `gemini-2.5-pro`), **temperature 0**, native structured output with Pydantic re-validation regardless. Judge inputs per call: the email, the field's reference value, the candidate's value, and the binary rubric; output `{verdict: pass|fail, rationale}`.
- Rubric text, ordering, and examples are pinned and identical for both candidates (spec §4, D1); `judge_version()` = hash of {judge model, prompt, rubric, few-shots} — any change invalidates the calibration certificate (spec §5), which is why the hash must be sensitive to every component.
- Error-vs-fail line (spec §7): judge refusal / validation failure must surface as `verdict=None` so downstream (T08 rows, T16 gate) can exclude rather than score them — judge failures can never register as candidate regressions.
- Risk note (spec §11): the Gemini structured-output API surface is newer than SDK patterns in training data — write the client against current `google-genai` docs and keep the Pydantic re-validation unconditional.
- The `judge_version()` string is passed as an opaque `str` into `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` (T01) at T15/T16 call sites — do not import fingerprint logic here.
