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
