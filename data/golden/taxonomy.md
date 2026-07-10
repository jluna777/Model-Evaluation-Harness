# Golden set taxonomy — coverage contract

**Status: DRAFT.** This is the T13 offline-prep coverage contract (spec §3, D4, ticket
`docs/tickets/T13-golden-set.md`). Counts below are the *target* per category and are
final only at freeze, after Gemini generation, owner open-coding on both candidates'
draft-set outputs, and any resulting adds/edits. Do not treat this file as the frozen
`dataset_version: 1` record until the ◆ owner sign-off lands.

This file is the coverage contract `tests/unit/test_golden_dataset.py` reconciles
against `data/golden/golden.jsonl` — the two must always agree; the machine-readable
block at the bottom is what the test parses.

## Size and split

- 50 items total: **32 nominal / 18 adversarial** (spec §3, D4).
- Every taxonomy category has **≥2 items**, except the four multi-request variant
  tags, which the ticket explicitly relaxes to **≥1 item each** (this plan comfortably
  clears that floor at 2 each — see below).
- Difficulty is tagged 1–3 per item (`meta.difficulty`); ranges below are the planned
  spread per category.

## Category table

| # | Category (`meta.categories` tag) | Axis (D4 / spec §3) | Slice | Target | Difficulty | Brief IDs |
|---|---|---|---|---:|---|---|
| 1 | `baseline` | — (plain nominal coverage across all 5 ticket categories: billing 4 / shipping 4 / account 3 / product 4 / other 3) | nominal | 18 | 1 | golden-001..018 |
| 2 | `date_number_normalization` | semantic failure: date/number normalization | nominal | 2 | 2 | golden-019..020 |
| 3 | `implicit_urgency` | input stressor: implicit urgency | nominal | 2 | 2 | golden-021..022 |
| 4 | `fact_density` | input stressor: fact density | nominal | 2 | 2 | golden-023..024 |
| 5 | `structural_absent_field` | structural: genuinely-absent fields vs null | nominal | 2 | 1–2 | golden-025..026 |
| 6 | `multi_request_plain` | input stressor: multi-request variant (plain) | nominal | 2 | 2 | golden-027..028 |
| 7 | `multi_request_within_supersession` | input stressor: multi-request variant (within-message supersession) | nominal | 2 | 2 | golden-029..030 |
| 8 | `multi_request_reference_resolution` | input stressor: multi-request variant (reference resolution) | nominal | 2 | 2 | golden-031..032 |
| 9 | `hallucinated_value` | semantic failure: hallucinated values | adversarial | 2 | 3 | golden-033..034 |
| 10 | `contradiction_with_source` | semantic failure: contradiction-with-source | adversarial | 2 | 3 | golden-035..036 |
| 11 | `wrong_field_confusion` | semantic failure: wrong-field/entity confusion | adversarial | 2 | 3 | golden-037..038 |
| 12 | `under_extraction` | semantic failure: under-extraction | adversarial | 2 | 3 | golden-039..040 |
| 13 | `distractor_entities` | input stressor: distractor entities | adversarial | 2 | 3 | golden-041..042 |
| 14 | `mid_thread_burial` | input stressor: mid-thread burial | adversarial | 2 | 3 | golden-043..044 |
| 15 | `embedded_instructions` | input stressor: embedded instructions (prompt-injection-style) | adversarial | 2 | 3 | golden-045..046 |
| 16 | `tone_vs_content` | input stressor: tone-vs-content | adversarial | 2 | 3 | golden-047..048 |
| 17 | `multi_request_threaded_supersession` | input stressor: multi-request variant (threaded supersession) | adversarial | 2 | 3 | golden-049..050 |

**Sum check:** row 1–8 (nominal) = 18+2+2+2+2+2+2+2 = **32**. Row 9–17 (adversarial) =
2×9 = **18**. Total = **50**.

## Priority labeling convention (owner ruling, severity-aware, 2026-07-08; refined 2026-07-09)

Extends, and does not reverse, the dev-004 forward-time-pressure convention
(`docs/decisions.md` D4, 2026-07-07): `priority: urgent` whenever the email's
*content* is safety-critical (a real risk of injury, fire, gas, electrical, or
structural-failure hazard), **regardless of stated timing or tone** — a calmly
worded "no rush" report of a gas leak is still `urgent`. Absent a
safety-critical signal, the forward-time-pressure tiers split on how soon the
stated date or event falls: `urgent` when the resolution must precede
something **same-day or next-day**; `high` for other **genuine forward time
pressure** — a stated date or event, roughly within a **two-week window**,
that the resolution must precede, but not same-day or next-day. A delay that
has **already occurred**, with no upcoming date or event the resolution must
precede, is **not** forward time pressure — such requests stay `normal`
(absent a safety signal) no matter how eager the language ("get it moving,"
"expedite," "at your earliest convenience"); see the
`retrospective_delay_no_deadline` note below. `normal` applies otherwise —
non-safety defects with no forward deadline stay `normal` (dev-004
precedent). Tone is never the signal, content is. Full brief-level rationale
and worked examples: `data/golden/briefs.md`'s "Urgency convention" and
golden-046/golden-048.

### `retrospective_delay_no_deadline` note (owner ruling, 2026-07-09)

Not a new `meta.categories` tag — no dataset item is re-tagged or otherwise
edited by this note. It names a pattern of the priority convention above for
future item authors and reviewers: an **already-occurred** delay, described
with urgency-coded language ("get it moving," "expedite," "at your earliest
convenience") but with **no forward date or event** the resolution must
precede, is not forward time pressure and scores `normal` (absent a safety
signal). **golden-008** (`meta.categories: baseline`) is the canonical,
deliberately designed probe for this pattern — it was authored explicitly as
the priority-calibration negative control ("eagerness alone does not
warrant high/urgent priority without a concrete deadline"). **golden-027**
(`multi_request_plain`) and **golden-040** (`under_extraction`) exhibit the
same pattern incidentally under their existing tags; their `priority:
normal` labels are correct under this convention.

Ticket categories (`expected.category`: billing/shipping/account/product/other) are
not a separate row here — they are the schema's own enum and are additionally spread
across every taxonomy category above (see `baseline`'s internal split and the
per-brief domain assignment in `briefs.md`); every one of the 5 appears well beyond
the ≥2 floor once the full set lands.

## Multi-request four-variant requirement (ticket AC, spec §1 amendment 2026-07-07)

The multi-request taxonomy is deliberately split into four tagged variants, each
probing a different clause of the canonical three-step primary-request rule
(spec §1):

| Variant tag | Spec §1 rule exercised | Slice | Count |
|---|---|---|---:|
| `multi_request_plain` | step 2 (first actionable request, no supersession) | nominal | 2 |
| `multi_request_within_supersession` | step 2 (later statement in the *same* non-quoted text retracts/supersedes the first) | nominal | 2 |
| `multi_request_threaded_supersession` | step 1 (a request made in quoted/forwarded content is already superseded by the newest message and never primary) | adversarial | 2 |
| `multi_request_reference_resolution` | step 3 (newest text accepts/refers to a quoted offer; entity fields resolved from quoted content) | nominal | 2 |

All four clear the ticket's "≥1 item" floor at 2 each. `briefs.md` and
`golden.draft.jsonl` cover all four; one Claude-authored item exists for each variant
(golden-027, 029, 031, 049).

## Difficulty tags

- **1** — straightforward, single-request, all-relevant-fields-present or
  cleanly-absent extraction (`baseline`, most of `structural_absent_field`).
- **2** — one clear stressor or multi-request structure a competent model should
  still resolve correctly (`date_number_normalization`, `implicit_urgency`,
  `fact_density`, the two nominal multi-request variants).
- **3** — adversarial/edge probes targeting a specific documented failure mode;
  correct extraction requires resolving a genuine trap (contradiction, injection,
  distractor entity, buried fact, tone/content mismatch, threaded supersession).

## Generator-family plan (spec §3, D4, D4 amendment 2026-07-04a)

Provenance is recorded per item in `meta.generator`. Spec §3 requires **≥80% of
items from a model family distinct from both candidate families** (Candidate A:
Claude Haiku 4.5 / Anthropic; Candidate B: GPT-5.4 mini / OpenAI).

| Family | `meta.generator` value | Planned count | Share |
|---|---|---:|---:|
| Gemini (distinct — third provider, matches the judge's family; see Provenance & coupling below) | `gemini-2.5-flash` | 40 | 80% |
| Claude (Anthropic — same family as Candidate A) | `claude-fable-5` | ≤10 (10 authored this batch) | ≤20% |

This plan sits exactly at the bound: 40/50 = 80% distinct-family (meets "≥80%"
at its floor), 10/50 = 20% Claude-family (at the ticket's "≤10" ceiling). No
`meta.generator` value in this dataset is `claude-haiku-4-5-...` or
`gpt-5.4-mini` (the candidates themselves) — generating golden items with a
candidate model would defeat the provenance bound entirely and is out of
scope regardless of family share.

The 10 Claude-authored items are complete now (`data/golden/golden.draft.jsonl`);
the 40 Gemini items are drafted after billing is enabled, via the scratchpad-only
generation script (constitution §5: no dataset-generation tooling ships in the
repo). `dataset_version: 1` is not set until the full 50-item set is curated and
frozen by the owner.

## Provenance & coupling

- **The judge is Gemini-family** (`gemini-2.5-pro` since 2026-07-09, fallback `gemini-3.5-flash`; see decisions.md D1; was `gemini-3.5-flash`, escalation `gemini-2.5-pro`;
  D1) — the same family planned to draft 80% of golden items. This is a real
  generator–judge coupling and is named here deliberately rather than left to a
  limitations paragraph (constitution Principle 3).
- **Why the coupling is accepted:**
  1. **Judging is reference-guided against a human-curated answer, not
     against Gemini's own generation.** D1's protocol (spec §4) scores each
     candidate's free-text field for factual agreement with `expected.issue_summary`
     / `expected.requested_action` — values the owner reviews and may heavily edit
     during open-coding (`meta.edited`, D4 amendment 2026-07-04a). The judge is
     never asked to rate the email or the reference; it rates a candidate's answer
     against a reference that has passed through human curation.
  2. **The measured artifacts are the candidates' outputs.** Every reported
     composite score, per-field delta, and gate verdict is a function of what
     Claude Haiku 4.5 and GPT-5.4 mini extract — not of the golden email's
     provenance. Gemini having authored the input is no different from any
     third-party dataset author; nowhere in the pipeline does Gemini judge its
     own output.
  3. **The ≥80% distinct-family bound targets the candidates, not the judge.**
     Its purpose (D4) is preventing a golden set that happens to favor whichever
     candidate shares a generator's family (the same-family inflation risk named
     in D4's rationale, arXiv:2505.20738) — a candidate-vs-candidate fairness
     concern. The judge's neutrality toward both candidates is D1's separate,
     already-decided concern (third-provider selection) and does not depend on
     who authored the input emails.
  4. **Residual risk, disclosed rather than assumed away:** Gemini-drafted
     emails could carry phrasing Gemini's own judging is marginally more
     comfortable parsing (a familiarity effect distinct from, but adjacent to,
     Wataoka et al. arXiv:2410.21819, which concerns a judge favoring its *own
     outputs* rather than inputs it authored). Universal human curation (every
     item, `edited` recorded) and owner-authored reference values are the
     mitigation actually in place; this is not claimed to eliminate the effect,
     only to bound it below the level a fully model-authored, model-graded
     pipeline would carry.
- **Candidates are Anthropic and OpenAI models**, which is exactly why the
  distinct-family bound is denominated against those two families rather than
  against the judge's family. My own contribution (Claude/Anthropic-family,
  `claude-fable-5`) is capped at ≤10 items (≤20%) precisely because it shares a
  family with Candidate A; Gemini and any other non-candidate-family generator
  count toward the ≥80% distinct share.

## Machine-readable category counts

The reconciliation test (`tests/unit/test_golden_dataset.py`) parses the block
below and checks it against `golden.jsonl`'s `meta.categories` tags. Keep this in
exact sync with the table above — changing either the table or this block without
updating the other is what the test is designed to catch.

```text
baseline: 18
date_number_normalization: 2
implicit_urgency: 2
fact_density: 2
structural_absent_field: 2
multi_request_plain: 2
multi_request_within_supersession: 2
multi_request_reference_resolution: 2
hallucinated_value: 2
contradiction_with_source: 2
wrong_field_confusion: 2
under_extraction: 2
distractor_entities: 2
mid_thread_burial: 2
embedded_instructions: 2
tone_vs_content: 2
multi_request_threaded_supersession: 2
```

## Machine-readable domain-category minimums

Ticket-schema domains (`expected.category`) are the schema's own five enum
values — orthogonal to the `meta.categories` taxonomy tags reconciled above
(a single item's taxonomy tags do not name its domain; see `baseline`'s
internal per-domain split and the per-brief domain assignment in
`briefs.md`). This block is a **floor per domain, not an exact reconciled
count**: unlike the taxonomy block above, these five values are not summed
against the 50-item total (domains and taxonomy tags are different axes over
the same items). The reconciliation test parses this block independently and
asserts every domain reaches at least its stated minimum.

```domains
billing: 2
shipping: 2
account: 2
product: 2
other: 2
```

---

**DRAFT — counts final at freeze.** This document is superseded by the frozen
version committed alongside `data/golden/golden.jsonl` when `dataset_version: 1`
is signed off (◆ owner gate, ticket T13).
