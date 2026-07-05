"""Versioned prompt plumbing for the extraction task (spec §1).

``PromptTemplate`` pairs a version number with rendering logic so the version
that produced any given output is always recoverable and feeds the run
fingerprint (``config.py``). ``EXTRACTION_PROMPT`` is a placeholder here: its
text and ``version`` are frozen in T12 against ``data/dev/`` only, never
against golden or calibration items (spec §3, constitution Principle 6).
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


# TODO(T12): freeze wording and version against data/dev/ only. The frozen
# text must contain the spec §1 tie-break sentence verbatim: "the ticket
# describes the primary request -- the first actionable request in the
# newest, non-quoted part of the email."
EXTRACTION_PROMPT = PromptTemplate(
    version=1,
    template=(
        "TODO(T12): placeholder extraction prompt -- wording is not yet frozen.\n\n"
        "From: {from_}\n"
        "Subject: {subject}\n"
        "Body:\n{body}\n"
    ),
)
