from harness.prompts import EXTRACTION_PROMPT, PromptTemplate
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
    "the ticket describes the primary request — the first actionable request "
    "in the newest, non-quoted part of the email, unless a later statement "
    "there explicitly retracts or supersedes it, in which case the superseding "
    "request is primary."
)


class TestExtractionPromptFrozen:
    """T12: EXTRACTION_PROMPT is frozen as prompt_version 1 against data/dev/ only."""

    def test_version_is_frozen_at_one(self):
        assert EXTRACTION_PROMPT.version == 1

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
