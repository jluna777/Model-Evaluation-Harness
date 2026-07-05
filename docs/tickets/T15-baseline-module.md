# T15 — Baseline module

**Phase:** C · **Depends on:** T01, T07, T08 · **Owner gate:** no
**Sources:** plan.md task T15 · spec.md §7 · decisions.md D3

## Goal
Implement baseline artifact I/O, generation, and fingerprint checking as library code (CLI wiring lands in T16), including the adversarial-guardrail noise floor measured and recorded at baseline time.

## Deliverables
- `src/harness/gate/baseline.py`
- `tests/unit/gate/test_baseline.py` (path mirrors `src/` per the file-structure contract; fixtures under `tests/fixtures/`)

## Interfaces
**Consumes:**
- `fingerprint(config, served_versions, judge_version: str, composite_mode, calibration_verdict) -> str` — from T1 (all spec §7 fields; judge_version passed as opaque string)
- `judge_version() -> str` — from T7 (fed into the fingerprint at this call site)
- `run_eval(config, model_key, *, k, dataset, prompt: PromptTemplate) -> RunDir`; `load_run(run_dir) -> RunArtifact` — from T8

**Produces (consumed by T16):**
- `generate_baseline(config, model_key) -> BaselineFile` — library callable; CLI wiring (`eval gate --update-baseline`) lands in T16
- `check_fingerprint(baseline, run) -> list[Mismatch]`
- Baseline artifact format per spec §7 (`baselines/{candidate}.json`): per-item, per-replicate, **per-field** scores for all 50 items, raw model and judge outputs (or a pointer to their committed location), config fingerprint, K_baseline=6, plus the measured adversarial noise floor

## Acceptance criteria
- [ ] Baseline artifact round-trips: a baseline written by `generate_baseline` with fake clients reloads with per-item, per-replicate, per-field scores, raw outputs (or committed-location pointer), and the config fingerprint intact.
- [ ] `generate_baseline` uses K = `config.k_baseline` = 6 — proven by fake-client call counts (items × 6 candidate calls).
- [ ] `check_fingerprint` returns an empty list on a matching pair; on a pair differing in multiple components it enumerates every differing field by name, including `judge_version` drift (fixture: baseline and run identical except judge_version → exactly one `Mismatch` naming `judge_version`).
- [ ] Loading a v0-format (or otherwise unrecognized-schema) baseline file fails loudly with a distinct exception naming the format problem — never a silent misparse or partial load.
- [ ] Adversarial-guardrail noise floor: measured at baseline time and recorded in the baseline artifact; the floor check verifies the 10-point guardrail threshold is ≥3× the measured adversarial-composite run-to-run SE. Fixture with SE > 10/3 points → check fails; fixture with SE ≤ 10/3 → check passes and the measured SE is present in the artifact.
- [ ] `uv run pytest` and `uv run ruff check` pass.

## Notes
- Spec §7 constraints that bind here: K_baseline=6 keeps baseline noise small so the per-run false-alarm rate stays near-exact *conditional on the frozen baseline*; the guardrail threshold (10 points) must be verified ≥3× measured SE **at baseline time**, not at gate time.
- Baselines are **never auto-created** by the gate (spec §7 exit-code contract) — this module only exposes `generate_baseline` as a callable; T16 owns the `--update-baseline` UX and the exit-2 instruction.
- Real committed baselines (traced, reportable) are generated in T16 after T12–T14 land; this ticket is fake-client/unit-test only — no live API calls.
- Retry policy per global constraints (transport-only, cap 4, never re-sample a returned response) is inherited from T6/T8 and not re-implemented here.
- TDD loop: failing test → minimal impl → green → `uv run ruff check` → commit.
