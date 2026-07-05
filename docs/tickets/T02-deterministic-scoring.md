# T02 — Deterministic scoring + composite

**Phase:** A · **Depends on:** T01 · **Owner gate:** no
**Sources:** plan.md task T2 · spec.md §1 (normalization rule), §6 (per-field scoring, composite definition)

## Goal
Implement the deterministic per-field scorers (normalized exact match) and the per-email composite score with its two modes (full 7-field vs deterministic-5 judge-excluded), as pure, unit-tested functions.

## Deliverables
- `src/harness/scoring/deterministic.py` — normalization + exact-match per-field scores
- `src/harness/scoring/composite.py` — per-email composite, `CompositeMode (FULL_7 | DETERMINISTIC_5)`
- `tests/unit/` tests for both modules (test tree mirrors `src/`)

## Interfaces
**Consumes (from T01, verbatim from plan.md):** `TicketExtraction` — **permissive** candidate-facing model (`order_id: str | None`, no pattern).
**Produces (copied verbatim from plan.md):**
- `score_deterministic(expected, actual) -> dict[str, int]`
- `normalize(s: str|None) -> str|None` (trim, casefold, collapse whitespace)
- `composite(field_scores, mode: CompositeMode) -> float` (unweighted mean over included fields ×100)
- `CompositeMode` with members `FULL_7 | DETERMINISTIC_5` (names are load-bearing — consumed by T15/T16 gate code and by T01's fingerprint `composite_mode` argument)

## Acceptance criteria
- [ ] `uv run pytest` passes, including these anchors:
  - [ ] `normalize(" Jane  DOE ")` matches `"jane doe"` (trim, casefold, collapse internal whitespace)
  - [ ] `None` matches only `None`; empty string `""` does **not** match `None`
  - [ ] `ORD-12345` vs `ord-12345` scores as a match after normalization (this input is reachable because the candidate model is permissive per T01)
  - [ ] `composite` under `FULL_7` and `DETERMINISTIC_5` return different values on a fixture that includes judge-field scores
  - [ ] all fields passing → `composite(...) == 100.0`
- [ ] `uv run ruff check` exits 0
- [ ] committed (subject + change summary; no attribution or process-status lines)

## Notes
- Normalization contract is spec §1: trim, casefold, collapse internal whitespace; `None` matches only `None`. Deterministic fields per spec §1: `category`, `priority` (exact match), `customer_name`, `order_id`, `product_name` (normalized exact match). `issue_summary` / `requested_action` are judge-scored (T7) — this ticket only needs their scores as inputs to `composite`.
- Composite per spec §6: unweighted mean of the *included* fields — 7 normally; 5 (deterministic only) in judge-excluded mode. The composite definition is part of the run fingerprint (T01's `composite_mode` parameter), so mode names must not drift.
- Scoring of schema-invalid/refusal outputs (all 7 fields → 0) is runner-level policy (spec §6) handled in T8; keep these functions pure over provided values.
- All Global constraints from plan.md apply. TDD loop: failing test → minimal impl → green → `uv run ruff check` → commit.
