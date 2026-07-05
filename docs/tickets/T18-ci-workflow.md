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
- [ ] Workflow triggers on same-repo PRs only: fork PRs skip the gate, and the skip is documented (comment in the workflow file and/or README note). Verifiable: workflow condition guards on head repo == base repo (or equivalent), and the documentation line exists.
- [ ] Path filter covers exactly the measurement surface: `src/`, prompts, `data/`, `baselines/`, `configs/`, and the workflow file itself.
- [ ] Workflow declares `pull-requests: write` permission (required for the PR comment).
- [ ] Job timeout is 30 minutes (`timeout-minutes: 30`).
- [ ] Exactly five secrets are referenced: three provider API keys (Anthropic, OpenAI, Gemini) + two Langfuse keys — CI gate runs are traced and reportable (spec §8 fail-fast applies).
- [ ] Owner attests in this ticket's evidence that the configured Gemini key is paid-tier (spec §11: ~600 judge calls per gate run make free-tier limits a flake risk) — key tier is not verifiable from repo artifacts, so this is an attestation, not an automated check.
- [ ] Gate markdown is written to the GitHub Actions job summary **and** posted as a PR comment.
- [ ] `workflow_dispatch` trigger exists with a demo input that runs `eval gate --seed-regression`.
- [ ] Observed: a docs-only PR does **not** run the gate job (skipped by path filter); a PR touching `src/` **does** run it. Record both PR/run links in this ticket.
- [ ] Observed: exit 1 vs exit 2 produce visibly distinct check labels in the PR UI (both failed checks, differently labeled — spec §7 exit-code contract). Record how the distinction is rendered (e.g. separate named steps/annotations).
- [ ] Observed: a `workflow_dispatch` demo run with the seed-regression input fails with the DEMO MODE banner in its log; the run log link is recorded in this ticket.
- [ ] `uv run pytest` and `uv run ruff check` pass before commit.

## Notes
- Sequencing: requires T16's committed baselines (the gate exits 2 with an `--update-baseline` instruction if a baseline is missing — never auto-created) and T17's `docs/gate-design.md` so the summary link resolves in PR comments.
- Owner work (~30 min): configure the five repository secrets in GitHub; open/approve the two verification PRs (docs-only and `src/`-touching) and trigger the `workflow_dispatch` demo.
- Exit-code handling: GitHub Actions treats both 1 and 2 as failed checks; the workflow must surface them with different labels (spec §7) — e.g. distinct step names or annotations for "regression detected" vs "measurement error".
- `--seed-regression` applies `DEGRADED_DEMO_PROMPT` at runtime, skips the fingerprint check, and banners the entire output as DEMO MODE (spec §7); the workflow must not persist any demo output as a baseline.
- This ticket is workflow YAML, not library code — no new unit tests required, but the repo must still end green per Global constraints.
