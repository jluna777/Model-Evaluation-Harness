# T06 — Model clients (candidates) + retry

**Phase:** A · **Depends on:** T01 · **Owner gate:** no
**Sources:** plan.md task T6 · spec.md §1, §2, §6, §7, §9

## Goal
Implement the `ModelClient` protocol with the two candidate clients (Anthropic, OpenAI) behind provider-native structured output, plus the transport-only retry decorator. No plugin system — these are two of exactly three hand-written clients (the third, Gemini, lands in T07).

## Deliverables
- `src/harness/models/__init__.py` (created — protocol + `StructuredResult`)
- `src/harness/models/anthropic_client.py` (created)
- `src/harness/models/openai_client.py` (created)
- `src/harness/models/retry.py` (created)
- `tests/unit/models/test_anthropic_client.py`, `tests/unit/models/test_openai_client.py`, `tests/unit/models/test_retry.py` (created — mocked transports; canned API payloads in `tests/fixtures/`)
- `tests/live/test_smoke.py` (created — marked `live`, excluded from CI)

## Interfaces
**Consumes:** (T01) `TicketExtraction` — **permissive** candidate-facing model (`order_id: str | None`, no pattern — the provider-bound JSON schema must contain no unsupported keywords); `PromptTemplate{version, render(email) -> str}`; `Config` (incl. `retry_max_attempts=4`, model IDs).
**Produces:**
- `ModelClient` protocol: `complete_structured(prompt: str, schema: type[BaseModel]) -> StructuredResult{output: BaseModel|None, failure: None|"schema_invalid"|"refusal", raw: str, usage, served_model_version}` — `raw` is always populated, including on failures (spec §6/§7 raw persistence)
- `retry_transport(max_attempts=config)` decorator

## Acceptance criteria
- [ ] Mocked transport sequence 429 → 429 → 200 succeeds (test asserts final success and exactly 3 attempts)
- [ ] Mocked 4×429 → raises `TransportExhausted` (cap = `retry_max_attempts` = 4 from config)
- [ ] Returned-but-invalid JSON → `StructuredResult.failure == "schema_invalid"` **with `raw` populated**, and the mocked transport is called exactly once (never retried)
- [ ] Mocked refusal response → `failure == "refusal"` with `raw` populated
- [ ] `served_model_version` captured from both SDK response shapes (one test per provider's response object)
- [ ] Outgoing-request assertions per provider: Anthropic request uses `output_config.format`; OpenAI request uses strict `json_schema`; both send **temperature=0**; the JSON schema bound to the request contains no unsupported keywords (assert no `pattern` key anywhere in the emitted schema)
- [ ] `uv run pytest` exits 0 and collects zero `live`-marked tests by default; `uv run pytest -m live tests/live/test_smoke.py` is the explicit opt-in
- [ ] `uv run ruff check` exits 0
- [ ] Live smoke run executed once locally against real keys; output pasted into the PR notes

## Notes
- TDD loop (global constraints): failing test → minimal impl → green → `uv run ruff check` → commit. Live smoke happens after green, before commit.
- Model pins (spec §2): Candidate A = Claude Haiku 4.5 `claude-haiku-4-5-20251001` (dated snapshot); Candidate B = GPT-5.4 mini `gpt-5.4-mini` (resolved ID recorded in `configs/default.yaml`). SDKs `anthropic` / `openai` pinned in `pyproject.toml`. Candidates run at **temperature 0** (global constraint; asserted on outgoing requests here).
- Retry line (spec §9, global constraints): transport errors only (429/5xx/timeout), capped at 4 attempts (config `retry_max_attempts`), jittered exponential backoff; **a returned response is never re-sampled regardless of content** — `schema_invalid` and `refusal` are candidate failures to be scored (spec §6), not retry triggers.
- Alias-drift guard (spec §2): `served_model_version` comes from provider response metadata and later feeds the run fingerprint — capture it faithfully from both SDK shapes.
- Spec §1: OpenAI strict mode cannot omit fields, so `None`/null is the required "not present" encoding for both providers; the schema handed to providers derives from the permissive `TicketExtraction` (normalization is scoring's job, T02).
- Interface note: the plan refines spec §2's `complete_structured(email, schema)` to `complete_structured(prompt: str, schema)` — the prompt argument is `PromptTemplate.render(email)`, so spec §2's contract is satisfied by composition. Follow the plan signature.
- Sequencing: Phase B (owner data work, T12–T14) starts as soon as this ticket lands — do not let the live smoke step linger.
