from harness.prompts import DEGRADED_DEMO_PROMPT, EXTRACTION_PROMPT, PromptTemplate
from harness.schema import EmailInput


def _email() -> EmailInput:
    return EmailInput(
        **{
            "from": "customer@example.com",
            "subject": "Where is my order?",
            "body": "I never received order ORD-12345.",
        }
    )


class TestPromptTemplate:
    def test_render_returns_string_containing_email_content(self):
        template = PromptTemplate(version=1, template="Subject: {subject}\nBody: {body}")

        rendered = template.render(_email())

        assert isinstance(rendered, str)
        assert "Where is my order?" in rendered
        assert "ORD-12345" in rendered

    def test_version_is_exposed(self):
        template = PromptTemplate(version=7, template="{subject}")
        assert template.version == 7


class TestExtractionPromptPlaceholder:
    def test_is_a_prompt_template(self):
        assert isinstance(EXTRACTION_PROMPT, PromptTemplate)

    def test_renders_without_error(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        assert isinstance(rendered, str)
        assert "Where is my order?" in rendered


# spec §1's canonical tie-break sentence, copied verbatim -- this is the exact
# string that must appear in the frozen prompt (T12 acceptance criterion).
TIE_BREAK_SENTENCE = (
    "The ticket describes ONE primary request. Determine it as follows:\n"
    "1. Consider only the newest, non-quoted part of the email. Quoted or forwarded "
    "content below it (lines starting with '>', earlier messages introduced by headers "
    "like \"On ... wrote:\", or trailing prior threads) is earlier conversation: any "
    "request made there is already superseded by the newest message and is never the "
    "primary request.\n"
    "2. Within the newest, non-quoted text, the primary request is the first actionable "
    "request — unless a later statement in that same text explicitly retracts or "
    "supersedes it, in which case the superseding request is primary.\n"
    "3. When the newest text refers to quoted content — such as accepting an option "
    "support offered earlier — use the quoted content to describe the request precisely. "
    "Entity fields (customer_name, order_id, product_name) may likewise be resolved from "
    "anywhere in the email, including quoted or forwarded sections."
)


class TestExtractionPromptFrozen:
    """T12: EXTRACTION_PROMPT is frozen against data/dev/ only. Re-frozen as
    prompt_version 2 on 2026-07-08 (owner ruling) to state the severity-aware
    priority rule in full -- any prompt wording change must bump the version,
    no exceptions (the judge_version analog), so this pin moved 1 -> 2 here.
    Re-frozen again as prompt_version 3 on 2026-07-09 (owner ruling, T13
    open-coding round, Cluster A defect) to define `low`, so this pin moved
    2 -> 3. Re-frozen again as prompt_version 4 on 2026-07-09 (owner ruling,
    T13 open-coding round, Clusters B/C plus the category boundary) to split
    the `high`/`urgent` boundary on a same/next-day line, state the
    already-late clause, and define the `category` enum values, so this pin
    moves 3 -> 4."""

    def test_version_is_frozen_at_four(self):
        assert EXTRACTION_PROMPT.version == 4

    def test_rendered_prompt_contains_tie_break_sentence_verbatim(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        assert TIE_BREAK_SENTENCE in rendered

    def test_rendered_prompt_contains_all_category_enum_values(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        for value in ("billing", "shipping", "account", "product", "other"):
            assert value in rendered

    def test_rendered_prompt_contains_all_priority_enum_values(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        for value in ("low", "normal", "high", "urgent"):
            assert value in rendered

    def test_rendered_prompt_states_none_means_not_present(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "null" in rendered
        assert "genuinely does not mention" in rendered

    def test_rendered_prompt_states_severity_aware_priority_rule(self):
        # v2 re-freeze (owner ruling, 2026-07-08): safety-critical content is
        # urgent regardless of stated timing or tone -- a stable substring of
        # that clause must survive in the rendered prompt.
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "safety-critical issue" in rendered
        assert "regardless of stated timing or tone" in rendered

    def test_rendered_prompt_defines_low_priority(self):
        # v3 re-freeze (owner ruling, 2026-07-09, T13 open-coding Cluster A):
        # v2 never defined `low`, leaving it structurally unreachable for the
        # 24% of golden items whose reference priority is `low`. A stable
        # substring of the new `low` clause must survive in the rendered
        # prompt, and it must compose with an unconditional `normal` default.
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "Use low for requests with no concrete, unresolved problem" in rendered
        assert "Use normal for actionable issues" in rendered

    def test_rendered_prompt_splits_high_and_urgent_on_same_or_next_day(self):
        # v4 re-freeze (owner ruling, 2026-07-09, T13 open-coding Cluster B):
        # v3's "use high or urgent only under genuine forward time pressure"
        # never said which -- both candidates defaulted to urgent. The
        # rendered prompt must now state the same/next-day split explicitly:
        # urgent for a same-day/next-day forward deadline (absent safety),
        # high for other genuine forward pressure within roughly two weeks.
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "same-day or next-day" in rendered
        assert "Use high for other genuine forward time pressure" in rendered

    def test_rendered_prompt_states_already_late_is_not_forward_pressure(self):
        # v4 re-freeze (owner ruling, 2026-07-09, T13 open-coding Cluster C):
        # a delay that already happened, with no upcoming date or event the
        # resolution must precede, is not forward time pressure -- normal
        # applies regardless of eager language (golden-008 is the canonical
        # probe; golden-027/040 exhibit the same pattern incidentally).
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "already" in rendered
        assert "is not forward time pressure" in rendered
        assert "get it moving" in rendered
        assert "expedite" in rendered
        assert "at your earliest convenience" in rendered

    def test_rendered_prompt_defines_category_values(self):
        # v4 re-freeze (owner ruling, 2026-07-09, T13 open-coding round): the
        # bare `category` enum line never defined its five values, leaving
        # the product/other boundary undocumented (golden-018: a general,
        # non-product-specific inquiry is `other`, not `product`). The
        # rendered prompt must now carry a one-line definition per value.
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "billing = charges, refunds, invoices" in rendered
        assert "shipping = delivery, tracking" in rendered
        assert "account = login, credentials, profile" in rendered
        assert "product = a problem or question about a specific product" in rendered
        assert "other = anything else, including general, catalog" in rendered
        # The three boundary clauses below are what reconcile the definitions
        # with the existing labels (named-product pre-sale asks are `product`
        # per golden-013/015/033/034/dev-010; pre-sale customs questions are
        # `shipping` per golden-007; golden-018 stays `other`). They must be
        # pinned verbatim or a wording "simplification" can silently move the
        # category boundary while every other test stays green.
        assert "owned, ordered, or asked about by name" in rendered
        assert "or shipping/customs questions for an order" in rendered
        assert "not about one specific product or order" in rendered

    def test_rendered_prompt_instructs_schema_only_output(self):
        rendered = EXTRACTION_PROMPT.render(_email())
        assert "conform exactly to the provided schema" in rendered

    def test_prompt_template_contains_no_few_shot_examples(self):
        # The schema is provider-enforced; the extraction prompt itself must
        # stay free of worked examples (task brief). Checked on the static
        # template text, not the rendered email -- an interpolated email
        # address like "customer@example.com" would be an unrelated false
        # positive for the substring "example".
        assert "example" not in EXTRACTION_PROMPT.template.lower()


class TestDegradedDemoPrompt:
    """T16: DEGRADED_DEMO_PROMPT exists solely for `eval gate
    --seed-regression`'s demo mode -- a deliberately weakened variant of
    EXTRACTION_PROMPT, out-of-band prompt_version so it can never collide
    with (or be mistaken for) a real prompt version's run identity."""

    def test_is_a_prompt_template_and_renders(self):
        assert isinstance(DEGRADED_DEMO_PROMPT, PromptTemplate)
        rendered = DEGRADED_DEMO_PROMPT.render(_email())
        assert isinstance(rendered, str)
        assert "Where is my order?" in rendered

    def test_version_is_a_negative_out_of_band_sentinel(self):
        # Never a real, incrementing prompt version (those start at 1).
        assert DEGRADED_DEMO_PROMPT.version < 0
        assert DEGRADED_DEMO_PROMPT.version != EXTRACTION_PROMPT.version

    def test_strips_the_three_step_tie_break_rule_and_field_definitions(self):
        rendered = DEGRADED_DEMO_PROMPT.render(_email())
        assert TIE_BREAK_SENTENCE not in rendered
        # Field definitions (e.g. the order_id format hint) are gone too --
        # only the bare field names remain (module docstring: "stripped of
        # ... every per-field definition").
        assert "form ORD-NNNNN" not in rendered
        assert "order_id" in rendered  # the field name itself is still listed

    def test_still_instructs_schema_only_output(self):
        rendered = DEGRADED_DEMO_PROMPT.render(_email())
        assert "conform exactly to the provided schema" in rendered
