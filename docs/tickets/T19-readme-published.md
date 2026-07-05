# T19 — README, published artifacts, demo

**Phase:** D · **Depends on:** T11, T14, T16, T17, T18 · **Owner gate:** yes ◆
**Sources:** plan.md task T19 · spec.md §5, §10, §12 (AC3/AC4/AC5) · decisions.md D2

## Goal
Publish the before/after story: a README with real numbers, every one of which is backed by a committed artifact in `results/published/` and exactly recomputable with zero API calls.

## Deliverables
- `README.md` (created/replaced)
- `results/published/` (committed run artifacts for every README number)
- `.gitignore` exception (`results/` gitignored EXCEPT `results/published/`, per spec §10)

## Interfaces
**Consumes:**
- `eval rescore <run-dir>` — recompute all scores/statistics/reports from persisted raw outputs, zero API calls (T11)
- `eval calibrate [--retest]` — agreement report + certificate from committed labels; `--retest` adds the intra-annotator ceiling row (T14)
- Gate verdicts and committed `baselines/{a,b}.json` (T16)
- 10-run no-change table + observed false-alarm count in `docs/gate-design.md` (T17)
- CI workflow `.github/workflows/eval-gate.yml` (T18, described in quickstart/demo)

**Produces:**
- `README.md` + `results/published/` — the artifacts T20 walks for AC3/AC4/AC5

## Acceptance criteria
- [ ] Entry criteria met before README numbers are written: owner retest labels committed in `data/calibration/labels.jsonl` with `round: "retest"` (25 relabels, ≥1 week after the T14 initial labeling); `eval calibrate --retest` has been run; the ceiling row is present in the certificate report.
- [ ] README shows the before/after with real numbers (spec AC4): composite scores ± CI for both candidates, judge–human agreement ± CI with the ceiling row, gate verdicts, printed MDE, and the sparse-delta count m.
- [ ] README contains the 10-run no-change table summary with the observed false-alarm count, linking `docs/gate-design.md` (spec AC3 / constitution DoD 2).
- [ ] README contains a quickstart and the documented two-command demo: `eval gate` (passes against the committed baseline) then `eval gate --seed-regression` (fails, DEMO MODE banner); in CI the demo runs via `workflow_dispatch`.
- [ ] README contains an honest-limitations section naming at least: single annotator, n (50 golden / 32 nominal), MDE coarseness (~5–7 points at n=32; the printed MDE is the source of truth), and alias pinning (candidate B resolved ID recorded in `configs/default.yaml`).
- [ ] `results/published/` contains a committed artifact for every README number; `.gitignore` keeps `results/` ignored with the `results/published/` exception in place (verifiable: `git check-ignore results/scratch` succeeds, `git check-ignore results/published/<file>` fails).
- [ ] AC5 recomputability, run numbers: `uv run eval rescore results/published/<run>` reproduces the published report byte-exact with zero API calls (no client construction — the T11 anchor's mechanism).
- [ ] AC5 recomputability, agreement numbers: `uv run eval calibrate` and `uv run eval calibrate --retest` on the committed labels reproduce the README agreement numbers ± CI and the ceiling row with zero API calls.
- [ ] ◆ Owner validates README (reviews the numbers, limitations section, and demo instructions and signs off).

## Notes
- Sequencing: blocked on the ≥1-week retest gap scheduled from T14 — the retest execution is this ticket's entry criterion, not new scheduling. Do not start README numbers until the ceiling row exists.
- Owner work: execute the 25-relabel retest (~30–45 min, `round: "retest"`); review the README (~30 min).
- Published runs are reportable: they must be traced (Langfuse keys required, fail fast without — spec §8); untraced artifacts can never feed the README.
- Spec AC5 errata: recomputation (zero API calls, byte-exact) is the contract; re-execution against live APIs is expected to differ within reported CIs — the README/limitations should carry this distinction.
- Golden and calibration items are never used for prompt tuning (Global constraints); the README numbers come from the frozen `dataset_version: 1` set.
- Documentation + publication ticket; any code touched (e.g. `.gitignore`, report tweaks) still ends green: `uv run pytest` and `uv run ruff check` pass before commit.
