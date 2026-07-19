# T18 — CI workflow

**Phase:** D · **Depends on:** T16, T17 · **Owner gate:** no
**Sources:** plan.md task T18 · spec.md §7, §11 · decisions.md D3

## Goal
Wire the gate into GitHub Actions per spec §11: a workflow that runs `eval gate` on same-repo PRs touching measurement-relevant paths, posts the gate markdown to the job summary and as a PR comment, and exposes the demo via `workflow_dispatch`.

## Deliverables
- `.github/workflows/eval-gate.yml` (created)

## Interfaces
**Consumes:**
- CLI `eval gate [--update-baseline|--seed-regression]` with exit codes 0 = pass · 1 = regression detected · 2 = measurement error (T16)
- Gate markdown summary produced by the gate command (T10 `render_gate_summary` via T16), including the relative link to `docs/gate-design.md` (T17)
- Committed `baselines/{a,b}.json` (T16)

**Produces:**
- `.github/workflows/eval-gate.yml` — the CI wiring consumed by T19 (README describes it) and T20 (AC3 evidence)

## Acceptance criteria
- [x] Workflow triggers on same-repo PRs only: fork PRs skip the gate, and the skip is documented (comment in the workflow file and/or README note). Verifiable: workflow condition guards on head repo == base repo (or equivalent), and the documentation line exists.
- [x] Path filter covers exactly the measurement surface: `src/`, prompts, `data/`, `baselines/`, `configs/`, and the workflow file itself.
- [x] Workflow declares `pull-requests: write` permission (required for the PR comment).
- [x] Job timeout is 30 minutes (`timeout-minutes: 30`).
- [x] Exactly five secrets are referenced: three provider API keys (Anthropic, OpenAI, Gemini) + two Langfuse keys — CI gate runs are traced and reportable (spec §8 fail-fast applies).
- [x] Owner attests in this ticket's evidence that the configured Gemini key is paid-tier (spec §11: ~600 judge calls per gate run make free-tier limits a flake risk) — key tier is not verifiable from repo artifacts, so this is an attestation, not an automated check.
- [x] Gate markdown is written to the GitHub Actions job summary **and** posted as a PR comment.
- [x] `workflow_dispatch` trigger exists with a demo input that runs `eval gate --seed-regression`.
- [x] Observed: a docs-only PR does **not** run the gate job (skipped by path filter); a PR touching `src/` **does** run it. Record both PR/run links in this ticket.
- [x] Observed: exit 1 vs exit 2 produce visibly distinct check labels in the PR UI (both failed checks, differently labeled — spec §7 exit-code contract). Record how the distinction is rendered (e.g. separate named steps/annotations).
- [x] Observed: a `workflow_dispatch` demo run with the seed-regression input fails with the DEMO MODE banner in its log; the run log link is recorded in this ticket.
- [x] `uv run pytest` and `uv run ruff check` pass before commit.

## Notes
- Sequencing: requires T16's committed baselines (the gate exits 2 with an `--update-baseline` instruction if a baseline is missing — never auto-created) and T17's `docs/gate-design.md` so the summary link resolves in PR comments.
- Owner work (~30 min): configure the five repository secrets in GitHub; open/approve the two verification PRs (docs-only and `src/`-touching) and trigger the `workflow_dispatch` demo.
- Exit-code handling: GitHub Actions treats both 1 and 2 as failed checks; the workflow must surface them with different labels (spec §7) — e.g. distinct step names or annotations for "regression detected" vs "measurement error".
- `--seed-regression` applies `DEGRADED_DEMO_PROMPT` at runtime, skips the fingerprint check, and banners the entire output as DEMO MODE (spec §7); the workflow must not persist any demo output as a baseline.
- This ticket is workflow YAML, not library code — no new unit tests required, but the repo must still end green per Global constraints.

## Evidence (2026-07-19)

Setup: repo published at https://github.com/jluna777/Model-Evaluation-Harness
(public), `main` pushed at `3e5cfff`; the five repository secrets
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`) and the `LANGFUSE_BASE_URL`
repository variable (`https://us.cloud.langfuse.com`) configured 2026-07-19.
Workflow registered as "Eval Gate" (id 310103090), active.

**Gemini key tier attestation:** the owner attests (2026-07-19, in session)
that the configured `GEMINI_API_KEY` is a paid tier-1 key — 10,000
requests/day for the flash class, covering a ~600-call gate run with wide
margin.

**Docs-only PR skips the gate.** PR #1
(https://github.com/jluna777/Model-Evaluation-Harness/pull/1, adds
`CLAUDE.md` only). No Eval Gate run was created for the PR: `gh pr checks`
reported "no checks reported" at ~30s and ~75s after open, and the
workflow's run list stayed empty while the workflow itself was confirmed
registered and active. Merged (rebase) as `46d9904`.

**src-touching PR runs the gate, exit 0.** PR #2
(https://github.com/jluna777/Model-Evaluation-Harness/pull/2, docstring-only
change to `src/harness/models/gemini_client.py`). Run:
https://github.com/jluna777/Model-Evaluation-Harness/actions/runs/29700593699
— the gate's first remote execution. Attempt 1: exit 0, "PASS: gate result"
step, gate markdown in the job summary and posted as a PR comment by
`github-actions[bot]`. Measured: candidate a Δ +0.07 (p = 0.5625, m = 4,
sparse-delta warning printed), candidate b Δ +0.52 (p = 0.7041, m = 10),
adversarial guardrail not tripped for either, calibration-certificate header
rendered (κ = 0.749, adequate). Attempt 1 carried a disclosed tracing
incident: ~125 `Failed to export span batch code: 401` stderr lines in the
report — the repository variable had been created under the deprecated name
`LANGFUSE_HOST`, which the workflow does not forward, so the harness fell
back to `configs/default.yaml`'s EU host while the keys are US-region.
Measurement was unaffected (spec §8 bounded degradation: the gate still
measured and passed; only span export failed). Fixed the same day
(`LANGFUSE_BASE_URL` variable created, misnamed variable deleted) and the
run re-run: attempt 2 exit 0, PASS, report clean of export noise, traces
exporting. Merged (rebase) as `0f259b9`.

**Seed-regression demo, exit 1.** Run:
https://github.com/jluna777/Model-Evaluation-Harness/actions/runs/29702596084
(`workflow_dispatch`, demo=true, 2026-07-19 20:29→20:42 UTC). The DEMO MODE
banner rendered at report head and foot, overall verdict FAIL, and
exit_code=1 routed to the failing step named **"FAIL: regression detected"**
with annotation `::error title=Gate: regression detected::...`. Baselines
untouched (demo mode has no baseline write path; `--seed-regression` and
`--update-baseline` are CLI-mutually-exclusive).

**Missing-credential demo, exit 2, distinct from exit 1.**
`LANGFUSE_PUBLIC_KEY` was temporarily deleted (restored from the local
`.env` at 21:04 UTC the same day, byte-identical, value never displayed).
Run: https://github.com/jluna777/Model-Evaluation-Harness/actions/runs/29703649212
— completed in 17 seconds: the gate failed fast at the spec §8 contract
(`Reportable runs require Langfuse credentials (spec §8): ...`) before
constructing any provider client, so the run spent zero API calls.
exit_code=2 routed to the failing step named **"FAIL: measurement error
(gate could not measure)"** with annotation `::error title=Gate: measurement
error::... This is NOT a candidate regression ...`. The exit-1 vs exit-2
distinction is rendered as differently named failing steps plus differently
titled error annotations — both visible in the run's step list and the
annotations block (spec §7 "CI renders each state distinctly").

Minor for T20 triage: the workflow's `2>&1` capture routes
tracing-degradation stderr (the 401 lines above) into the PR-comment report
— honest but noisy; a post-v1 candidate is splitting stderr from the
report body. Also noted for T19: local `.env` uses the deprecated
`LANGFUSE_HOST` alias (honored by the harness env fallback) while CI uses
the canonical `LANGFUSE_BASE_URL` variable — worth one README sentence to
prevent the same misname recurring.
