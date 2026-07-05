# T01 — Project skeleton, config, schema, prompt plumbing

**Phase:** A · **Depends on:** none · **Owner gate:** no
**Sources:** plan.md task T1 · spec.md §1, §2, §7, §9, §10 · decisions.md D2/D3 (config defaults)

## Goal
Stand up the typed Python project skeleton and ship the foundation types every later ticket consumes: validated config with run fingerprint, the candidate-facing and reference-side Pydantic schemas, and the versioned prompt plumbing (prompt text itself is frozen later, in T12).

## Deliverables
- `pyproject.toml` (Python 3.12+, `uv`, Pydantic v2, Typer, ruff, pytest, pinned SDK deps per Global constraints)
- `configs/default.yaml`
- `src/harness/config.py` — Pydantic config + YAML load + run fingerprint (all spec §7 fields incl. judge_version)
- `src/harness/schema.py` — `EmailInput`, `TicketExtraction` (permissive, candidate-facing), `GoldenItem` (reference-side strict validation), `CalibrationLabel`, `Certificate`
- `src/harness/prompts.py` — `PromptTemplate {version, render(email)}`, placeholder `EXTRACTION_PROMPT`
- `tests/unit/` tests mirroring the above (test tree mirrors `src/` per the plan's file structure)

## Interfaces
**Consumes:** none (foundation ticket; only the plan's Global constraints apply).
**Produces (copied verbatim from plan.md):**
- `Config` (all spec §9 fields incl. `k=3`, `k_baseline=6`, `retry_max_attempts=4`, price snapshot) — spec §9 config surface: model IDs, prompt version, dataset path/version, K, gate margin/alpha, price snapshot, Langfuse settings
- `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` (all spec §7 fields; judge_version passed as opaque string) — spec §7 fingerprint fields: prompt version, dataset version, resolved/served model versions, judge version, composite definition, calibration verdict
- `TicketExtraction` — **permissive** candidate-facing model (`order_id: str | None`, no pattern — the provider-bound JSON schema must contain no unsupported keywords)
- `GoldenItem` with reference-side strict validation (`expected.order_id` must match `ORD-\d{5}`)
- `EmailInput`, `CalibrationLabel`, `Certificate`
- `prompts.py` with `PromptTemplate{version, render(email) -> str}` and a placeholder `EXTRACTION_PROMPT`

Schema fields per spec §1 (`TicketExtraction`): `category` enum `billing|shipping|account|product|other`; `priority` enum `low|normal|high|urgent`; `customer_name: str | None`; `order_id: str | None`; `product_name: str | None`; `issue_summary` free text; `requested_action` free text. `None`/null is the required "not present" encoding.

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] config round-trips `configs/default.yaml`, asserting `k == 3`, `k_baseline == 6`, `retry_max_attempts == 4`, and a dated price snapshot (date field present, labeled approximate-at-snapshot) that survives the round-trip
  - [ ] unknown key in config YAML → Pydantic `ValidationError`
  - [ ] `fingerprint(...)` output changes when any single component changes — **including `judge_version`** — and is stable across dict key ordering (same inputs, reordered dicts → identical string)
  - [ ] candidate-side `TicketExtraction` accepts `order_id="ord-12345"` (normalization is scoring's job, T02)
  - [ ] reference-side `GoldenItem` rejects `expected.order_id="ord-12345"` (must match `ORD-\d{5}`)
  - [ ] `priority: "URGENT"` rejected by `TicketExtraction` (enum values are lowercase per spec §1)
  - [ ] the JSON schema generated from `TicketExtraction` for provider binding contains no `pattern` keyword (no unsupported keywords)
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- All Global constraints from plan.md apply (referenced, not restated); this ticket establishes the tooling they assume (`uv`, ruff, pytest).
- Config defaults are the decided D2/D3 values (K=3, K_baseline=6, margin 2.0, alpha 0.05); changing them requires a dated decision-log amendment (spec §9). Bake the defaults into `configs/default.yaml`.
- `retry_max_attempts=4` is the spec §9 retry cap — the config field ships here; the retry decorator that reads it is T6's scope.
- Price snapshot in `configs/default.yaml` is dated and labeled approximate-at-snapshot (spec §7 gate output consumes it later).
- `EXTRACTION_PROMPT` is a placeholder: text and `prompt_version: 1` are frozen in T12 against `data/dev/` only. Do not attempt to finalize wording here; only the `PromptTemplate` plumbing (version + `render(email) -> str`) must work. The frozen text will need to contain the spec §1 tie-break sentence verbatim — leave a TODO marker.
- `fingerprint` takes `judge_version` as an opaque string; the real `judge_version()` implementation arrives in T7 and is wired at T15/T16 call sites.
- TDD loop (Global constraints): failing test → minimal impl → green → `uv run ruff check` → commit.
