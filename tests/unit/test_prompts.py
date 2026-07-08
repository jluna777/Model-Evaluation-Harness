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
    no exceptions (the judge_version analog), so this pin moves 1 -> 2 here."""

    def test_version_is_frozen_at_two(self):
        assert EXTRACTION_PROMPT.version == 2

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
