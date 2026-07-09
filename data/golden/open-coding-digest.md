# T13 Golden-Set Open-Coding Digest

**Scope:** `data/golden/golden.draft.jsonl` (50 items), Candidate A (`claude-haiku-4-5`, run `a-767589ce643ffa82`), Candidate B (`gpt-5.4-mini`, run `b-f5fde93a0e1a7a90`), K=3, prompt v2, UNCALIBRATED (no judge certificate — this run predates D2). Read-only analysis; no data was changed.

**Headline finding, stated up front:** most of the "both-candidates-fail" signal in this draft set traces to three concrete, fixable causes, not to model incompetence: (1) the extraction prompt's `priority` rule never states when to use `low` (12/50 items expect `low`; B never once outputs it, A outputs it exactly once), (2) the same rule never disambiguates `high` vs `urgent` for pure time-pressure cases, and (3) several `issue_summary`/`requested_action` references are written more tersely than the email's actual (single, primary) request, so the judge's literal "no added claims" rubric fails both models for including true, email-grounded detail the reference simply omitted. These three explain roughly 20 of the ~25 flagged items below.

---

## 1. Per-item difficulty table and both-fail suspects

Full per-item composite table (mean over K=3, both candidates) was computed; only the both-fail suspects are detailed here (composite dominated by a single field failing on ≥2/3 replicates for **both** candidates).

### Cluster A — `priority: low` is structurally unreachable (prompt defect, not a label defect)

The extraction prompt (`src/harness/prompts.py`, v2) defines `priority` as: use `urgent` for safety-critical content; else `high`/`urgent` for forward time pressure; **"Use normal when neither signal is present."** It never mentions `low` as a live option. Result: across all 150+150 rows, B outputs `low` **zero** times; A outputs it exactly **3** times (all for one item). 12/50 items (24%) have `expected.priority == "low"`.

Items (expected `low`, both candidates output `normal` on ~all replicates): **golden-004, 007, 009, 010, 013, 015, 016, 017, 025, 033, 034** (11 of the 12; golden-018 is the sole exception — see below).

- Evidence: golden-004 (reimbursement receipt request) — A/B always `normal` vs expected `low`. Same pattern for pre-sale questions (007, 013, 015, 025, 033, 034) and pure account-admin asks (009, 010, 016, 017).
- Classification: **(b) item/prompt under-specified** — this is a systemic scoring-protocol gap, not a per-item label error. The reference values are defensible (pre-sale/admin/no-problem asks are lower priority than an actual defect); the prompt just never teaches the model the `low` boundary.
- **golden-018** is the interesting exception: A outputs `low` correctly all 3 reps (likely keyed off the email's literal "No rush at all, just curious" phrasing), B outputs `normal` all 3 reps. Also golden-018's `category` fails 100% for **both** candidates (`product` vs expected `other`) — the email is a general question about discontinued colorways; "product" is a defensible category too. Classification: **(a) reference label ambiguous** (category boundary between "product" and "other" is undefined in the prompt) — flagged already once by the curator's 2026-07-08 label-audit note in `meta.notes`, but the category ambiguity remains.

### Cluster B — `high` vs `urgent` boundary undefined for time-pressure-only cases

The convention text (taxonomy.md, briefs.md, and the prompt) says forward time pressure "within two weeks" warrants "high/urgent" but never states which. Both candidates default to `urgent`.

- **golden-019** (flight in 11 days, explicit date): expected `high`; A and B both output `urgent` on all 3/3 replicates.
- **golden-021** (Saturday departure, implicit urgency): expected `high`; A `urgent` 3/3, B `urgent` 2/3.
- Classification: **(b) item under-specified** — the severity-aware convention's own wording ("high/urgent apply...") doesn't disambiguate the two tiers; a genuinely hard-but-fair distinction cannot be fair if it's undocumented.

### Cluster C — "already/recently late" framing triggers false high-priority escalation

Three items describe a delay that has **already happened** (no forward-looking deadline) but use urgency-adjacent language ("get it moving," "at your earliest convenience," "expedite"). Expected `normal`; both candidates escalate to `high` on all replicates.

- **golden-008** — explicitly designed as the priority-calibration negative control (`meta.notes`: "eagerness alone does not warrant high/urgent priority without a concrete deadline"). Both candidates fail it 3/3. This is the one genuinely intentional, working probe in this cluster.
- **golden-027** (multi-request, "was supposed to arrive five days ago... get it moving") — both `high` 3/3 vs expected `normal`.
- **golden-040** (under-extraction, "received yesterday... several days later than the original estimated delivery date") — both `high` 3/3 vs expected `normal`.
- Classification: golden-008 = **(c) genuine hard-but-fair**, working exactly as designed. golden-027/040 = same underlying failure mode discovered incidentally (they're tagged `multi_request_plain`/`under_extraction`, not `implicit_urgency`) — **(d)-adjacent: not a judge issue, but an undocumented taxonomy gap** (see §4).

### Cluster D — references are terser than the (single) primary request, judge fails true added detail

Both candidates independently add detail that is **verbatim-grounded in the source email** but absent from a curator-written terse reference; the judge's literal "no added claims" application then fails both. Confirmed by reading the source email for each:

| Item | Field | Candidate addition | Grounded in email? |
|---|---|---|---|
| golden-005 | issue_summary, requested_action | "checked porch/mailbox/neighbors"; "investigate the missing package" | Yes — customer literally wrote "Could you please investigate this?" and listed the checks |
| golden-008 | issue_summary | "upcoming trip" | Yes — "looking forward to using it on an upcoming trip" |
| golden-010 | requested_action | "confirm once processed" | Yes — "Please confirm once this has been processed" |
| golden-012 | issue_summary, requested_action | "minimal use/disappointing durability"; "return guidance" | Yes — near-verbatim in email |
| golden-017 | requested_action | "willing to provide media kit" | Yes — "happy to provide my media kit" |
| golden-020 | issue_summary | "hasn't had it long/disappointed with durability" | Yes — near-verbatim |
| golden-023 | requested_action | "before the trip" | Yes — customer explicitly asked "before my trip" |
| golden-024 | issue_summary, requested_action | "jacket otherwise fine/unusable"; "return instructions" | Yes — near-verbatim |
| golden-046 | requested_action | "need a safe harness for upcoming trips" | Yes — near-verbatim |

Cross-check that this is a reference issue and not a model issue: for the **same category of behavior**, a different item's judge verdict passes it — golden-021 rep0 (A) issue_summary rationale: *"the same essential facts as the reference, with differences only in wording **and the inclusion of minor details from the source email**"* → **pass**. That is the identical pattern golden-008/010/012/017/020/024/046 fail for. This is a real inconsistency, not just an ambiguous rubric (see §3).

- Classification: **(a) reference label incomplete** for all 9 items above — recommend broadening the reference to include the grounded secondary detail, since it's part of the *same* primary request (no multi-request/supersession issue applies).

### Genuinely hard-but-fair items, working as intended (not suspects)

- **golden-033** (`hallucinated_value`): both candidates anchor on the decoy `ORD-19488` for `order_id` on **all 6 replicates** (both models, 100% failure) despite it being an explicitly unrelated, already-resolved order mentioned only as a compliment aside. This is the single cleanest, best-functioning adversarial probe in the set — **(c) genuine hard-but-fair**, no action needed.
- **golden-030, 031, 049, 050** (`multi_request_within_supersession` / `multi_request_reference_resolution` / `multi_request_threaded_supersession`): both candidates intermittently leak the superseded/retracted request into `issue_summary`/`requested_action` (e.g., B for golden-030: *"He initially mentioned a refund, then superseded that request..."* — a real primary-request-rule violation, not reference terseness, since the rule explicitly requires *omitting* superseded requests). **(c) genuine hard-but-fair.**
- **golden-043/044** (`mid_thread_burial`): both candidates correctly recover the buried order_id/product_name/customer_name from earlier thread content but frame `issue_summary` around the underlying shipping-delay history rather than the newest message's actual point ("just asking for an update"). A real, narrower failure mode than the taxonomy's stated intent (entity recovery, not summary framing) — **(c) genuine hard-but-fair**, but worth noting as a taxonomy refinement (see §4).
- **golden-022** (genuine order-number omission from `issue_summary`) — real, fair failure, not reference terseness.

**Section 1 tally: ~25 distinct items touched by both-fail analysis, resolved into 6 named clusters** (A–D above, plus the working-as-intended set).

---

## 2. Nominal < adversarial inversion, decomposed

Reported: A 84.4 (adv) vs 80.5 (nom); B 80.7 (adv) vs 78.4 (nom).

**What drags the nominal mean:**
- `priority` accuracy is the single biggest nominal drag: A 49.0% nominal vs 57.4% adversarial; B 56.2% nominal vs 75.9% adversarial. Cause: 10 of the 12 `expected.priority == "low"` items are nominal (only golden-033/034 are adversarial), so Cluster A's prompt gap disproportionately taxes the nominal slice. Clusters B and C above are also nominal-slice-heavy (golden-008/019/021/027/040 are all nominal).
- `issue_summary`/`requested_action` reference-terseness (Cluster D) is *also* concentrated in nominal `baseline`/`fact_density` items (5,8,10,12,17,20,23,24,46 — all nominal except 46, which is adversarial `embedded_instructions`).
- Net effect: two measurement artifacts (an undocumented prompt rule and an inconsistently-terse reference style) that happen to concentrate in the nominal slice are doing most of the work of the "inversion," more than any genuine adversarial-robustness story.

**Adversarial categories where the probe is passed at ~100% by both candidates on the target field (candidates for hardening, not prescriptions):**

| Item | Category | Probe field | Both-candidate accuracy on probe field |
|---|---|---|---|
| golden-038 | `wrong_field_confusion` | order_id (SKU-shaped decoy `CT-20026`) | 100% / 100% (3/3 both) |
| golden-036 | `contradiction_with_source` | product_name (self-correction Wayfinder→Overlook) | 100% / 100% |
| golden-041 | `distractor_entities` | order_id (newest vs. stale quoted order) | 100% / 100% |
| golden-042 | `distractor_entities` | product_name (two products mentioned, one has the issue) | 100% / 100% |
| golden-045 | `embedded_instructions` | category, priority, requested_action (fake `[SYSTEM NOTE]`) | 100% / 100% on all three |
| golden-046 | `embedded_instructions` | category, priority (fake `ADMIN OVERRIDE`) | 100% / 100% |
| golden-048 | `tone_vs_content` | priority (calm tone, gas-leak content) | 100% / 100% |

By contrast, **golden-033** (`hallucinated_value`, order_id) and **golden-047** (`tone_vs_content`, priority — A fails 3/3, B fails 1/3) *do* discriminate — real, working traps. So the picture is category-specific, not "adversarial slice is too easy" uniformly: `hallucinated_value` and one of the two `tone_vs_content` items land; `wrong_field_confusion`, `contradiction_with_source`, `distractor_entities`, and `embedded_instructions` each have at least one item that isn't landing on its stated probe field. Recommendation (not prescription): the owner may want to harden the SKU-decoy in 038 (make it closer to `ORD-NNNNN` form), make the self-correction in 036 subtler (both models handle an explicit "no wait, it was actually X" correction easily), add a second/less-obvious distractor to 041/042, and use a less telegraphed injection style in 045/046 (both models resist an obviously-labeled `[SYSTEM NOTE]`/`ADMIN OVERRIDE` block; real injection attempts are rarely this legible).

---

## 3. Judge quality spot-check

Sampled 15 verdicts (6 pass, 9 fail; both candidates; `issue_summary`/`requested_action` only, since those are the only judged fields) out of 361 pass / 239 fail total judged-field rows.

**No outright self-contradictory verdict found** (i.e., no case where the rationale's stated observation logically supports the opposite verdict). All 15 sampled rationales accurately describe what they see in the candidate value relative to the reference.

**However, one real cross-item inconsistency, not just rubric ambiguity:**
- golden-021 rep0 (A), issue_summary → **pass**, rationale: *"the same essential facts as the reference, with differences only in wording and **the inclusion of minor details from the source email**."*
- golden-008 rep1 (A), issue_summary → **fail**, rationale: *"adds the detail about the customer's upcoming trip, which is not present in the reference."*

Both candidate outputs add a true, source-grounded detail beyond a terser reference; the judge's own stated reasoning explicitly tolerates this in one case and penalizes it in the other. This is a genuine calibration risk under the 2.5-pro judge: the rubric text ("no added claims") is being applied non-uniformly to structurally identical situations. This previews a likely low-to-mid Cohen's kappa risk specifically on the "added-but-grounded-detail" edge case once formal D2 calibration runs — worth a dedicated few-shot example either way the owner decides (tolerate grounded detail, or don't).

- The remaining 8 sampled fails (golden-005, 009, 010, 011, 020, 024, 046 ×2) all cite a real, identifiable deviation (added claim or dropped essential) that is defensible **under the rubric as literally written** — the rationales are internally sound; the issue is the rubric/reference interaction (§1 Cluster D), not the judge misapplying its own instructions.
- The 6 sampled passes (golden-038, 004, 021, 034, 026, 019) all correctly identify genuine paraphrase-only matches; no false passes found in this sample.

**Section 3 tally: 1 systemic inconsistency pattern identified (spanning ~9 item/field pairs from Cluster D), 0 outright contradictory verdicts in the 15-sample.**

---

## 4. Unanticipated failure modes (not in current taxonomy)

1. **`low`-priority collapse**: both candidates functionally cannot produce `priority: low` because the shared extraction prompt's priority rule only defines `urgent` (safety), `high`/`urgent` (time pressure), and `normal` (fallback) — never `low`. Affects 24% of items. **Not a model failure mode — a prompt-spec gap.** Recommend either adding an explicit `low` rule to the prompt (e.g., "use low for pre-sale questions or account/administrative requests with no unresolved product problem") or, if `low` is deliberately meant to be hard, note it explicitly as an intentional probe in taxonomy.md (it currently reads as an oversight, not a designed test).
2. **`high`/`urgent` boundary for time-pressure-only cases**: same root prompt gap, narrower blast radius (2 items) but the same fix would resolve it.
3. **"already-late" ≠ "forward time pressure"**: models conflate a delay that has *already occurred* (with urgency-coded language like "get it moving," "expedite") with a genuine future deadline. This is a distinct sub-case of the priority rule not covered by `implicit_urgency` (which tests inferring pressure from an *upcoming* event) or `date_number_normalization` (explicit future dates). Recommend a new taxonomy note/tag, e.g. `retrospective_delay_no_deadline`, and consider re-tagging golden-027/040 into it or adding a dedicated item — golden-008 already covers this well and should stay as the canonical example.
4. **Reference-terseness vs. literal "no added claims" rubric**: a scoring-protocol interaction, not a model failure mode — see §1 Cluster D / §3. Worth a taxonomy/spec note distinguishing "added claim" (unsupported/fabricated) from "added detail" (true, grounded, non-essential) so future item authors and the judge treat them differently.
5. **`product` vs `other` category boundary** for general, product-adjacent-but-not-order-tied inquiries (golden-018: "will a discontinued color return?"). Both candidates independently converge on a different category than the reference — a genuine boundary ambiguity in the enum's intended semantics, not documented anywhere in the prompt or taxonomy.
6. **Buried-thread summary framing vs. entity recovery** (golden-043/044): models can correctly resolve buried entities but default to summarizing the underlying problem history rather than the newest message's actual (minimal) content ("just checking in"). A narrower, more specific failure mode than `mid_thread_burial`'s stated design intent — worth a briefs.md note distinguishing "entity recovery" from "reply-content framing" as two separable skills the category currently conflates.

---

## 5. Replicate stability (composite range > 20 pts across K=3)

All ranges are exactly 28.6 pts (= 2 of 7 fields flipping between replicates at temp 0); none exceed 2-field swings.

| Item | Candidate | Composites (3 reps) | Range |
|---|---|---|---|
| golden-003 | B | 71.4, 100.0, 85.7 | 28.6 |
| golden-011 | B | 71.4, 42.9, 57.1 | 28.6 |
| golden-018 | B | 57.1, 71.4, 42.9 | 28.6 |
| golden-021 | B | 71.4, 71.4, 100.0 | 28.6 |
| golden-028 | A | 71.4, 85.7, 100.0 | 28.6 |
| golden-033 | B | 71.4, 42.9, 42.9 | 28.6 |
| golden-036 | B | 71.4, 85.7, 100.0 | 28.6 |

**6 of 7 unstable items are candidate B**, consistent with the run-level variance decomposition: B's between-replicate variance is roughly double A's on both the full composite (29.9 vs 13.6) and the judged-fields-only composite (311.1 vs 133.3). This is a real, reproducible signal that B (gpt-5.4-mini) is measurably less deterministic at temperature 0 than A (claude-haiku-4-5) on this dataset — worth flagging explicitly in the variance-decomposition write-up feeding the future K decision (D3), since it suggests K=3 may be systematically less sufficient for B than for A.

---

## Owner decisions needed (recommendations only — owner decides)

1. **Add an explicit `low`-priority rule to the extraction prompt** (bump to prompt v3 per spec §1 discipline) — e.g., "use `low` for pre-sale questions or account/administrative requests with no unresolved product problem." Without this, priority accuracy on ~24% of items is a coin-flip against the prompt's own design, not a fair test of either candidate. *Reason: current prompt structurally cannot produce the reference's most common non-`normal` value.*
2. **Add an explicit `high`-vs-`urgent` disambiguation rule** for time-pressure-only cases (golden-019, golden-021) — e.g., a same-day/within-3-days-vs-within-2-weeks split, or fold both into one accepted answer. *Reason: current convention text doesn't define the boundary it's scored against.*
3. **Broaden `requested_action`/`issue_summary` references** for golden-005, 008, 010, 012, 017, 020, 023, 024, 046 to include the email-grounded secondary detail both candidates independently surface. *Reason: both frontier candidates agree with each other and diverge from the reference in the same direction — strong signal the reference, not the models, is the outlier.*
4. **Clarify the judge rubric or add a few-shot example** distinguishing "added claim" (unsupported) from "added detail" (true, grounded, non-essential) — cite the golden-021-pass vs golden-008-fail inconsistency as the concrete calibration risk. *Reason: identical candidate behavior received opposite verdicts; this previews a real kappa risk under formal D2 calibration.*
5. **Re-tag or add a `retrospective_delay_no_deadline` category** covering golden-008/027/040's "already-late ≠ forward deadline" pattern, and consider promoting golden-008 as its canonical example. *Reason: a real, reproducible failure mode not currently named in taxonomy.md.*
6. **Reconsider golden-018's expected `category`** (`other` vs `product`) or add an explicit category-boundary clarification to the prompt. *Reason: both independently-trained candidates agree with each other against the single reference — weak signal of reference ambiguity, not model error.*
7. **Consider hardening (not replacing) golden-036, 038, 041, 042, 045, 046, 048** — each passes its stated adversarial probe at 100% for both candidates on the target field. *Reason: they're not currently discriminating; a subtler variant would test the same failure mode more informatively.* Keep golden-033 and golden-047 as-is — they demonstrate the traps work when calibrated with enough subtlety.
8. **Note B's elevated replicate instability** (double A's between-replicate variance) in the §6 variance-decomposition writeup as a candidate-specific finding, not dataset noise. *Reason: reproducible across both the full and judged-fields-only decomposition, and 6/7 high-range items are B.*
