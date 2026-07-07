# T13 — Golden set: draft → open-coding → freeze

**Phase:** B · **Depends on:** T01, T11, T12 · **Owner gate:** yes ◆
**Sources:** plan.md task T13 · spec.md §3, §1 · decisions.md D4

## Goal
Author, open-code, and freeze the 50-item golden dataset (`dataset_version: 1`) with its taxonomy coverage contract, so all reported numbers and gate baselines have a fixed measurement substrate.

## Deliverables
- `data/golden/golden.jsonl` (50 items)
- `data/golden/taxonomy.md` (coverage contract: category counts, difficulty tags, generator-family counts)
- `tests/unit/test_golden_dataset.py` (reconciliation test required by the plan's verification; path chosen to mirror `tests/unit/…`)
- `configs/default.yaml` updated: `dataset_version: 1` frozen

## Interfaces
**Consumes:**
- `GoldenItem` with reference-side strict validation (`expected.order_id` must match `ORD-\d{5}`) — from T1
- `eval run --model {a|b} [--dataset <path>]` — from T11 (draft runs happen in the uncalibrated-banner state)
- Frozen `EXTRACTION_PROMPT`, `prompt_version: 1` — from T12

**Produces:**
- `data/golden/golden.jsonl`, item format per spec §3 (consumed by T14 disjointness check, T15 baseline generation, T16 gate):
  `{id, email: {from, subject, body}, expected: TicketExtraction, meta: {slice: "nominal"|"adversarial", categories: [...], difficulty: 1-3, generator: "<model-id>", edited: bool, notes}}`
- `dataset_version: 1` (a component of T1's `fingerprint(...)` via config)
- `data/golden/taxonomy.md` as the coverage contract

## Acceptance criteria
- [ ] `data/golden/golden.jsonl` contains exactly 50 items; `meta.slice` counts are exactly 32 `nominal` / 18 `adversarial`.
- [ ] All 50 items pass T1's strict `GoldenItem` validation (loading the full file via `GoldenItem.model_validate` raises no error; e.g. an `expected.order_id` of `ord-12345` would be rejected).
- [ ] `data/golden/taxonomy.md` records per-category counts, difficulty tags, and per-generator-family counts; every taxonomy category has ≥2 items.
- [ ] `uv run pytest tests/unit/test_golden_dataset.py` passes and reconciles `taxonomy.md` counts against `golden.jsonl` `meta` tags — changing a count in either file makes it fail. The same test asserts the 32/18 split, per-category ≥2, and the generator bound below.
- [ ] ≥80% of items carry a `meta.generator` model family distinct from both candidates (Claude Haiku 4.5, GPT-5.4 mini) — asserted in the unit test.
- [ ] Both candidates were run once on the draft set via `eval run` before freezing; the run reports render the "uncalibrated (no certificate)" banner (no certificate exists yet). Run artifacts referenced in ticket evidence.
- [ ] Owner open-coded both candidates' outputs and recorded resulting adds/edits (`meta.edited`, `meta.notes`); every multi-request item's `expected` values were reviewed against the spec §1 canonical three-step primary-request rule (amended and restructured 2026-07-07; spec §1 owns the text), noted in `taxonomy.md`. The multi-request taxonomy category must include: plain multi-request, within-message supersession, threaded supersession (request in quoted content superseded by the newest message), and a reference-resolution request (newest text accepts a quoted offer) — each ≥1 item.
- [ ] `dataset_version: 1` recorded in `configs/default.yaml`; `uv run pytest` and `uv run ruff check` pass.
- [ ] ◆ Owner validates the golden-set freeze (signs `dataset_version: 1`).

## Notes
- Owner work (~3–4 h): curate model-drafted emails per the D4 taxonomy — drafting is assisted ad hoc; **no dataset-generation tooling ships** (constitution §5, global constraints).
- Sequencing: requires T12's frozen prompt (draft runs must use the frozen extraction prompt) and T11's `eval run`. Blocks T14 (calibration emails must be disjoint from this set) and T15/T16 (baselines pin `dataset_version`).
- Spec §3 freeze protocol is load-bearing: draft → run both candidates once → open-code → adjust → freeze. Post-freeze edits bump `dataset_version` and invalidate baselines.
- Golden items are **never used for prompt tuning** (spec §3, global constraints); prompt iteration happens only on `data/dev/`.
- Provenance: all emails synthetic, human-curated; heavy rewriting is recorded via `edited: true` (D4 amendment 2026-07-04a).
- TDD applies to the reconciliation test: write it failing against the draft data expectations, land data, go green, `uv run ruff check`, commit.
