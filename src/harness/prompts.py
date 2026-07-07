"""Versioned prompt plumbing for the extraction task (spec §1).

``PromptTemplate`` pairs a version number with rendering logic so the version
that produced any given output is always recoverable and feeds the run
fingerprint (``config.py``). ``EXTRACTION_PROMPT`` is frozen as
``prompt_version: 1`` (T12), iterated against ``data/dev/`` only, never
against golden or calibration items (spec §3, constitution Principle 6). Any
future wording change must bump ``version`` and go through the
``eval gate --update-baseline`` procedure (spec §7).
"""

from dataclasses import dataclass

from harness.schema import EmailInput


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned prompt template rendered against one email."""

    version: int
    template: str

    def render(self, email: EmailInput) -> str:
        return self.template.format(
            from_=email.from_,
            subject=email.subject,
            body=email.body,
        )


# Frozen against data/dev/ only (spec §3, constitution Principle 6). Any
# wording change bumps `version` and requires the eval gate --update-baseline
# procedure (spec §7) -- do not edit this text in place.
EXTRACTION_PROMPT = PromptTemplate(
    version=1,
    template=(
        "Extract a structured support ticket from the customer support email below.\n\n"
        "From: {from_}\n"
        "Subject: {subject}\n"
        "Body:\n{body}\n\n"
        "When the email raises more than one request, the ticket describes the primary "
        "request — the first actionable request in the newest, non-quoted part of the "
        "email. Treat quoted or forwarded content (lines starting with '>', earlier "
        "messages introduced by headers like \"On ... wrote:\", or trailing prior threads) "
        "as context only -- it never supplies the primary request.\n\n"
        "Fields to extract:\n"
        "- category: one of billing | shipping | account | product | other.\n"
        "- priority: one of low | normal | high | urgent, as conveyed by the email; use "
        "normal if the email gives no signal either way.\n"
        "- customer_name: the customer's name exactly as it appears in the email, or null "
        "if no name is given.\n"
        "- order_id: the order number exactly as it appears (form ORD-NNNNN), or null if "
        "no order is referenced.\n"
        "- product_name: the specific product name exactly as it appears, or null if no "
        "single product is named.\n"
        "- issue_summary: 1-2 sentences describing the primary issue.\n"
        "- requested_action: 1-2 sentences describing what the customer wants done about "
        "the primary issue.\n\n"
        "A field is null only when the email genuinely does not mention it -- never guess "
        "or invent a value to fill a missing field.\n\n"
        "Output must conform exactly to the provided schema: no text, commentary, or "
        "markdown outside the schema's fields."
    ),
)
