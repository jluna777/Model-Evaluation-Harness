# T12 — Dev set + prompt freeze

**Phase:** B · **Depends on:** T06, T11 · **Owner gate:** yes ◆
**Sources:** plan.md task T12 · spec.md §1, §3, §8

## Goal
Author the 10-item dev set and finalize the extraction prompt text — iterated against `data/dev/` only — freezing it as `prompt_version: 1`.

## Deliverables
- `data/dev/dev.jsonl` (10 items)
- finalized `EXTRACTION_PROMPT` text in `src/harness/prompts.py` (version frozen at 1)
- `tests/unit/` test asserting the tie-break sentence is present verbatim in the rendered prompt

## Interfaces
**Consumes:**
- `PromptTemplate{version, render(email) -> str}` and the placeholder `EXTRACTION_PROMPT` (T01 — plumbing exists since day one; this task freezes text and version only)
- `eval run --model {a|b} [--dataset <path>]` (T11)
- candidate clients via `ModelClient` (T06)

**Produces:**
- frozen `EXTRACTION_PROMPT` with `prompt_version: 1` — consumed by T13 (golden open-coding runs), T16 (committed baselines), and every run fingerprint (the prompt's version lands in the run fingerprint per T08)
- `data/dev/dev.jsonl` — the only permitted data for prompt iteration (spec §3) and the only permitted non-hand-written source of judge few-shots (spec §4 / T07 provenance rule)

## Acceptance criteria
- [ ] `data/dev/dev.jsonl` contains exactly 10 items; a unit test parses every line against the item schema (each `email` validates as `EmailInput`)
- [ ] a unit test asserts `EXTRACTION_PROMPT.version == 1`
- [ ] a unit test asserts the rendered prompt contains the spec §1 canonical three-step primary-request rule **verbatim** (amended and restructured 2026-07-07; spec §1 owns the text).
- [ ] `uv run eval run --model a --dataset data/dev/dev.jsonl` exits 0 and the report header shows the "uncalibrated (no certificate)" banner
- [ ] `uv run eval run --model b --dataset data/dev/dev.jsonl` exits 0, same banner state
- [ ] `uv run pytest` and `uv run ruff check` exit 0; committed (subject + change summary; no attribution or process-status lines)
- [ ] ◆ Owner validates the freeze: signs off `prompt_version: 1` and the 10-item dev set, attesting that prompt iteration touched `data/dev/` only (golden and calibration items never used — Global constraints / spec §3)

## Notes
- All Global constraints from plan.md apply (referenced, not restated).
- **Owner work:** review/edit 10 dev emails; iterate the extraction prompt against dev only; freeze `prompt_version: 1`. Plan gives no per-task estimate; spec §11 budgets ~4–6 h total owner authoring across all data work (~85 emails + labels) — expect roughly 30–60 min here for 10 emails plus prompt iteration.
- Sequencing: Phase B starts as soon as T06 lands (prompt iteration needs live candidates); the verification commands above need T11's CLI. Dev-set runs may be keyless/untraced (spec §8) — these runs are non-reportable by construction and never feed reported numbers.
- Dev items are excluded from all reported numbers (spec §3); they exist solely for prompt iteration and judge few-shot provenance.
- Candidates run at temperature 0 (Global constraints) — prompt iteration happens under the same decoding settings as measurement.
- Post-freeze prompt edits bump `prompt_version` and later require the `eval gate --update-baseline` procedure with the compare-vs-old-baseline report attached (spec §7 threat model, wired in T16/T17).
- The verbatim-sentence and version tests are code work: failing test → minimal impl (freeze the text) → green → `uv run ruff check` → commit.
