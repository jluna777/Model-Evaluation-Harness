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
