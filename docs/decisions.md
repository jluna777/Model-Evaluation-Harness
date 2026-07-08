# Decision Log

Owner-decided per `docs/constitution.md` §6. Each record states the decision, the options weighed, and why. **Ownership rule:** this log owns choices, rationale, and revisit-triggers; `docs/spec.md` owns every operational number and rule text. Amendments are new dated entries, never edits.

---

## D1 — Judge protocol and judge model

**Status:** Decided 2026-07-04 · Amended 2026-07-04a
**Decision:** Pointwise, reference-guided judging with a third-provider judge (`gemini-3.5-flash`; escalation `gemini-2.5-pro`). The judge grades each candidate's free-text fields independently against the golden reference — binary rubric, one field per judge call, temperature 0. Model comparison is computed from paired per-item score deltas, never from head-to-head judging. Operational parameters: spec §4, §6.

**Options considered:** (1) pointwise + third-provider judge *(chosen)*; (2) head-to-head pairwise with two-pass position swap (Arena-Hard protocol); (3) pointwise + offline head-to-head diagnostic.

**Rationale:**
- Position bias is a pairwise/listwise phenomenon — it structurally cannot arise when one output is graded alone against a reference (Zheng et al. 2023, arXiv:2306.05685; Wang et al. 2023, arXiv:2305.17926). Pointwise eliminates the bias instead of mitigating it.
- Pointwise is markedly more robust to distractor and verbosity exploitation than pairwise (~9% vs ~35% verdict-flip rates; Tripathi et al., COLM 2025, arXiv:2504.14716). Pairwise's sensitivity advantage accrues to subjective open-ended quality (Liu et al., COLM 2024) — not criterion-anchored grading.
- Paired per-item deltas are the recommended statistical frame for two models on a shared item set (Miller, "Adding Error Bars to Evals," arXiv:2411.00640) and yield absolute per-model scores; head-to-head win rates cannot.
- Self-preference and same-family favoritism are large, documented effects (Panickssery et al., NeurIPS 2024; Goel et al., ICML 2025). With Anthropic and OpenAI candidates, the judge comes from a third provider — the standard baseline mitigation, explicitly incomplete (familiarity bias persists; Wataoka et al., arXiv:2410.21819) — so judge–human agreement is additionally reported **separately per candidate** (D2): differential judge error is what corrupts an A-vs-B comparison.
- Residual pointwise biases (rubric-option ordering) are controlled by the binary rubric, one field per call, and pinned rubric order identical for both candidates, so residual effects cancel in the paired difference.
- Verbosity: reference-guided grading with a binary rubric is the fit mitigation for 1–2-sentence fields; a score-vs-length correlation is logged as a judge-health diagnostic.

**Amendment 2026-07-04a (post-review):** few-shot pass/fail examples with critiques are part of the judge protocol; they are hand-written or drawn exclusively from `data/dev/` — never golden or calibration items — and are versioned inside the judge prompt (leakage guard; spec §4). Judge self-consistency is measured by repeated judge calls on identical (email, reference, value) triples — cross-replicate verdict variance is a different quantity and feeds the variance decomposition (spec §4, §6).

**Revisit if:** calibration (D2) shows inadequate agreement after judge escalation; per-candidate agreement diverges (differential-bias flag); or v2 adds subjective quality dimensions where pairwise sensitivity matters.

---

## D2 — Judge calibration and agreement reporting

**Status:** Decided 2026-07-04 · Amended 2026-07-04a
**Decision:** Binary pass/fail rubric with written rationale. Calibration data is disjoint from the golden set: dedicated calibration emails, both candidates' outputs, every free-text field labeled by the owner, stratified toward borderline/failing outputs. Cohen's kappa (with cluster-bootstrap CI) is the single deciding agreement statistic, reported per candidate; raw agreement and prevalence are descriptive context. Owner test-retest relabeling provides an estimated consistency ceiling. Operational parameters: spec §5.

**Options considered:** (1) binary rubric + stratified set + test-retest *(chosen)*; (2) 1–5 graded rubric (weighted kappa + Spearman); (3) binary with minimal calibration, no retest.

**Rationale:**
- Binary verdicts are harder to game, force a definition of "acceptable," and correlate better with expert judgment in practitioner experience (Husain, hamel.dev 2024; Evals FAQ 2026). Graded scales invite boundary drift for a single annotator.
- Raw agreement alone overstates judge quality by 10–40pp depending on label balance (arXiv:2606.19544, preprint); chance-corrected agreement is the headline, prevalence printed because curated sets skew toward pass — the regime where kappa alone misleads (Feinstein & Cicchetti 1990).
- ~50 judgments per candidate is within practitioner floors (30–50: Evidently, Husain) but resolves agreement coarsely (κ CIs ≈ ±0.15–0.25 before clustering; cf. Sim & Wright 2005: ~107 items to separate κ=0.7 from 0.4). The report prints the honest-resolution statement.
- Single-annotator labeling follows the critique-shadowing precedent (Husain): the judge is calibrated to one expert's documented standard, disclosed plainly; test-retest stability is the standard partial substitute for the inter-annotator ceiling a solo design cannot measure (constitution §6 D2).
- Calibration emails are disjoint from the golden set: reported judge scores never come from items used to select or tune the judge (constitution Principle 6).

**Amendment 2026-07-04a (post-review):**
- Adequacy is decided on the overall Cohen's κ **point estimate** (pragmatic v1 rule); a defined gray zone yields *adequate-with-caveat*, flagged downstream (spec §5). The earlier per-candidate hard floor is replaced by a per-candidate divergence **flag** (triggers D1 review, never gates) — a hard floor sat below the calibration's own resolving power.
- Gwet's AC1 is dropped for constitution-§5 conformance (one agreement method); raw agreement + prevalence remain as descriptive counts, not methods.
- All calibration CIs use a **cluster bootstrap resampling emails** — judgments within an email are correlated; applying D3's clustering lesson to D2's own CIs.
- **Re-certification after any judge change uses freshly drafted calibration emails** — re-certifying on the emails whose disagreements drove a rubric revision would make the published kappa selected, not measured (spec §5).
- The certificate pins the judge only; candidate-prompt changes shift the judged-output distribution and are accepted as a stated v1 limitation.
- Fail-label enrichment means agreement is measured on a harder-than-operational distribution (conservative); the report acknowledges the shift.

**Revisit if:** the judge fails adequacy twice; a second annotator becomes available; per-candidate agreement diverges (→ D1 review).

---

## D3 — CI gate decision rule

**Status:** Decided 2026-07-04 · Amended 2026-07-04a
**Decision:** The gate operates on one composite score per email, on the nominal slice, as paired per-item deltas against a pinned baseline. Decision rule: one-sided sign-flip permutation test; fail only when p < α **and** the mean regression exceeds a practical margin. Bootstrap CI reported as the error bar, never the decision. MDE computed and printed on every run. Replicated runs averaged per item before pairing. Operational parameters: spec §6, §7.

**Options considered:** (1) permutation test + margin + MDE *(chosen)*; (2) BCa bootstrap CI as the decision rule; (3) fixed score threshold (industry default).

**Rationale:**
- Paired per-item analysis is the biggest power win available at this n (Miller, arXiv:2411.00640); composite-per-email avoids the clustered-SE trap (naive per-field SEs understate variance up to ~3x).
- The sign-flip permutation test gives finite-sample false-alarm control under exchangeability (Dror et al., ACL 2018); bootstrap p-values are anti-conservative at small n (Berg-Kirkpatrick et al., EMNLP 2012) — so the bootstrap CI reports, the permutation test decides. Mirrors the conservative small-n protocol of arXiv:2511.19794.
- MDE printing (Miller Eq. 10) makes the gate's resolution auditable per run, honoring constitution Principle 5's stated-resolution requirement.
- Temperature-0 API inference is not deterministic (Thinking Machines 2025-09) and run-to-run benchmark variance is material (arXiv:2407.10457): a single run is not a measurement; replicates are averaged per item, and the variance decomposition (spec §6) makes the replicate count evidence-based.
- Survey of existing harnesses (verified 2026-07-04): promptfoo, Braintrust, LangSmith, DeepEval gate on fixed thresholds; OpenAI Evals is threshold-based and shutting down 2026-11-30; lm-eval reports stderr but has no gate. A statistically controlled gate is a deliberate differentiator.

**Amendment 2026-07-04a (post-review):**
- **Margin honesty:** at v1's n the 2-point margin is dominated by the significance condition — any significant regression already exceeds it — so it is not a false-alarm mitigation (earlier claim withdrawn). It stays as explicit bookkeeping that binds only if n grows or variance shrinks. *Alternative for owner consideration at validation: a non-inferiority framing (shifted null H0: regression ≤ margin) would make the margin do real statistical work at the cost of changing the decided semantics toward a blunter, more conservative gate.*
- **Sparse-delta disclosure:** a sign-flip test with m nonzero deltas has minimum p = 2^-m — with m ≤ 4 the gate cannot fail at any regression size. The gate prints m and warns below the rejection threshold; sign assignments are fully enumerated for small m; Monte Carlo p uses (b+1)/(B+1) (spec §7).
- **Per-field McNemar dropped** for constitution-§5 conformance; replaced by per-field pass-rate delta count tables — same diagnostic value, no additional inferential machinery.
- **Frozen-baseline honesty:** the per-run α is exact unconditionally but approximate conditional on the frozen baseline realization; baselines therefore use a larger replicate count (K_baseline > K_run) to keep baseline noise small, and the false-alarm demonstration is presented as a conditional check (spec §7).
- **Two-candidate combination:** the gate fails if either candidate trips; the per-PR family false-alarm rate is stated in the summary.
- **Adversarial guardrail (with D4):** a coarse, deterministic hard-fail threshold on the adversarial-slice composite — set far above measured run noise, claiming no statistical control — closes the injection-category blind spot that nominal-only gating left open (spec §7).
- **Threat model stated:** the gate covers code changes, provider drift, judge drift; prompt changes go through an explicit re-baseline PR with an attached comparison report (spec §7).
- **Exit-code contract:** pass / regression / measurement-error are distinct CI outcomes; judge failures are excluded from deltas and can never register as candidate regressions (spec §7).

**Revisit if:** the golden set grows enough for finer margins; replicate data shows run variance negligible (drop K); repeated-gating false-alarm budget needs formal control; the owner prefers the non-inferiority framing above.

---

## D4 — Golden set design

**Status:** Decided 2026-07-04 · Amended 2026-07-04a
**Decision:** ~50 items, roughly 65/35 nominal vs adversarial/edge. Taxonomy seeded from documented failure axes — semantic failures, input stressors, structural cases — with every item tagged by slice, categories, difficulty, and generator. The statistical gate scores the nominal slice; adversarial slices are reported separately and covered by the coarse guardrail (D3 amendment). Synthetic emails from mixed generator families, generator recorded per item, human-curated. One open/axial-coding round on real candidate outputs before freeze. Operational parameters: spec §3; canonical tie-break wording lives in spec §1.

**Options considered:** (1) 65/35, gate on nominal slice *(chosen)*; (2) 50/50 blended, gate on everything; (3) 80/20 mostly nominal.

**Rationale:**
- Adversarially collected sets over-represent difficulty; blended scores stop reflecting production-like performance, and at this n the hard items would dominate delta variance and drive the gate (difficulty-controlled benchmark literature, 2023).
- Precedent from the task domain: Anthropic's ticket-routing guide names implicit requests, emotion-over-intent, and multi-issue tickets as edge cases and recommends a dedicated edge-case set with a lower target (~80%) vs nominal (90–95%).
- Stressor categories are evidence-backed: distractor clauses cause up to 65% drops (GSM-Symbolic, ICLR 2025 — math-reasoning evidence; transfer to extraction is a hypothesis the adversarial slice tests); mid-context degradation >30% ("Lost in the Middle," TACL 2024); embedded instructions are the indirect-prompt-injection class (Greshake et al. 2023) — exposing the judge too, not just candidates; entity/field confusion and hallucinated values dominate schema-based extraction error studies (PARSE, arXiv:2510.08623; arXiv:2601.05847).
- Generator provenance: same-family generated benchmarks can inflate the related candidate's scores (Silencer, arXiv:2505.20738), though not universally (DevBench, arXiv:2601.11895) — mixing families and recording the generator is cheap insurance that also makes the effect testable.
- Multi-request emails are structural ambiguity for single-value schemas; the tie-break rule is baked into both the extraction prompt and every reference answer so ambiguity cannot masquerade as model error.
- Open/axial coding before freezing follows failure-driven eval methodology (Husain & Shankar, 2026): literature seeds the taxonomy; observed candidate behavior finishes it.

**Amendment 2026-07-04a (post-review):** all items are synthetic-drafted then human-curated — heavy owner rewriting is curation and is recorded via an `edited` flag, keeping the constitution's "synthetic, human-curated" wording exact. The generator-mix commitment is bounded (≥80% of items from families distinct from both candidates) rather than "primarily" (spec §3). The nominal-only statistical gate is complemented by the coarse adversarial guardrail recorded under D3.

**Amendment 2026-07-07 (owner, at the T12 prompt-freeze gate):** the tie-break rule is refined to be supersession-aware — if a later statement in the newest, non-quoted text explicitly retracts or supersedes an earlier request, the superseding request is primary; otherwise the first actionable request is (canonical wording: spec §1). Secondary requests remain omitted from reference answers. **Multi-ticket extraction (one ticket per request, variable-length output) is recorded as a v2 direction** — the owner notes it would showcase set-valued scoring (alignment of predicted-to-gold ticket lists), which is exactly the machinery cut from v1. Related owner ruling from the same gate: implicit urgency maps to `high` only under genuine forward time pressure; past-tense incidents with no upcoming deadline are `normal` (dev-004 precedent). Later the same day the canonical wording was restructured into a three-step rule — newest-text-only eligibility (making threaded supersession explicit), within-text supersession, and quoted-content reference resolution + entity sourcing — because the single-sentence form under-communicated to both small candidate models and human readers; spec §1 owns the text. The multi-request taxonomy accordingly gains threaded-supersession and reference-resolution probes alongside within-message supersession.

**Amendment 2026-07-08 (owner, severity-aware priority ruling):** the dev-004 forward-time-pressure rule is extended, not reversed, to be severity-aware: `priority: urgent` whenever the email's content is safety-critical (real risk of injury, fire, gas, electrical, or structural-failure hazard), regardless of stated timing or tone — a calmly worded "no rush" report of a gas leak is still `urgent`. Absent a safety-critical signal, `high`/`urgent` apply only under genuine forward time pressure — a stated date or event, roughly within two weeks, that the resolution must precede — exactly as dev-004 already established; `normal` otherwise. Tone is never the signal, content is. **Labeling rule-of-thumb:** treat a stated deadline/event as "genuine forward time pressure" when it falls roughly within a two-week window of the email; vaguer or more distant timing (e.g. "in a few weeks," "sometime next month") does not qualify on its own. **`EXTRACTION_PROMPT` is re-frozen as `prompt_version: 2`** before any golden or baseline use — its `- priority:` definition line now states the full severity-aware rule; the only consumers of v1 were dev-scratch runs, so no re-baseline was required. **Rationale:** the prompt must state, in full, every rule its own references (`priority`, and any field graded against it) are labeled by — an ungrounded or partially-stated rule lets real labeling ambiguity get misattributed to model error at scoring time (D4: ambiguity never masquerades as model error).

**Revisit if:** open-coding reveals categories forcing a different mix; v2 adds real (non-synthetic) data.
