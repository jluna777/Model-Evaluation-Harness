"""Versioned prompt plumbing for the extraction task (spec §1).

``PromptTemplate`` pairs a version number with rendering logic so the version
that produced any given output is always recoverable and feeds the run
fingerprint (``config.py``). ``EXTRACTION_PROMPT`` was originally frozen as
``prompt_version: 1`` (T12), iterated against ``data/dev/`` only, never
against golden or calibration items (spec §3, constitution Principle 6). It
was re-frozen as ``prompt_version: 2`` on 2026-07-08 (owner ruling) to state
the severity-aware priority rule in full -- the only wording change is the
``- priority:`` definition line; the only consumers of v1 were dev-scratch
runs, never a golden or baseline run, so no re-baseline was required. It was
re-frozen again as ``prompt_version: 3`` on 2026-07-09 (owner ruling, T13
open-coding round, Cluster A defect) because v2's priority rule stated
``urgent``, ``high``, and a ``normal`` fallback but never defined ``low`` --
structurally unreachable for the 24% of golden items whose reference
priority is ``low``. The only wording change is again the ``- priority:``
definition line, adding a ``low`` clause and tightening the ``normal``
clause so the two compose; the only consumers of v2 were dev-scratch runs
and the uncalibrated open-coding draft run, never a golden or baseline run,
so no re-baseline was required. It was re-frozen again as
``prompt_version: 4`` on 2026-07-09 (owner ruling, T13 open-coding round)
for three semantics changes surfaced by the same round: (1) Cluster B --
v3's "use high or urgent only under genuine forward time pressure" never
said which of the two applied, so both candidates defaulted to ``urgent``;
the ``- priority:`` line now splits the boundary explicitly on a
same-day/next-day line (``urgent``) vs. other genuine forward pressure
within roughly two weeks (``high``). (2) Cluster C -- an explicit clause
now states that a delay which has already occurred, with no upcoming date
or event the resolution must precede, is not forward time pressure and
stays ``normal`` regardless of eager language (golden-008 is the designed
probe; golden-027/040 exhibit the pattern incidentally). (3) the bare
``- category:`` enum line now carries a one-line definition per value,
preserving the boundary that a general, non-product-specific inquiry
(golden-018) is ``other``, not ``product``. The only wording changes are
the ``- priority:`` and ``- category:`` definition lines; the only
consumers of v3 were dev-scratch runs and the uncalibrated open-coding
draft run, never a golden or baseline run, so no re-baseline was required.
Any future wording change must bump ``version`` and go through the
``eval gate --update-baseline`` procedure (spec §7) -- that discipline is
absolute, the judge_version analog for prompts.

``DEGRADED_DEMO_PROMPT`` (T16) exists solely for ``eval gate
--seed-regression``'s documented demo mode: it is a deliberately weakened
variant of ``EXTRACTION_PROMPT`` -- stripped of the three-step
primary-request rule (newest-text-only eligibility, within-text supersession,
quoted-content reference resolution) and of every per-field definition -- so
a candidate run against it measurably regresses (worse primary-request
selection on multi-request/threaded emails, worse entity resolution) without
touching any real, committed prompt version. It is never used for a real run,
never affects ``EXTRACTION_PROMPT``, and its ``version`` is a deliberately
out-of-band negative sentinel (never a real, incrementing prompt version) so
its run directory can never collide with, or be mistaken for, a real prompt
version's run (run identity folds in ``prompt_version``, ``runner.py``).
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
# Re-frozen as v2 on 2026-07-08 (owner ruling): the `- priority:` line below
# now states the severity-aware rule in full. No other wording changed.
# Re-frozen as v3 on 2026-07-09 (owner ruling, T13 open-coding round, Cluster
# A defect): the `- priority:` line now also defines `low`. No other wording
# changed.
# Re-frozen as v4 on 2026-07-09 (owner ruling, T13 open-coding round): the
# `- priority:` line now splits `high`/`urgent` on a same-day/next-day line
# and states the already-late clause; the `- category:` line now carries a
# one-line definition per enum value. No other wording changed.
EXTRACTION_PROMPT = PromptTemplate(
    version=4,
    template=(
        "Extract a structured support ticket from the customer support email below.\n\n"
        "From: {from_}\n"
        "Subject: {subject}\n"
        "Body:\n{body}\n\n"
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
        "anywhere in the email, including quoted or forwarded sections.\n\n"
        "Fields to extract:\n"
        "- category: one of billing | shipping | account | product | other. billing = "
        "charges, refunds, invoices, payment methods; shipping = delivery, tracking, "
        "arrival, or shipping/customs questions for an order; account = login, "
        "credentials, profile, or administrative changes; product = a problem or "
        "question about a specific product -- owned, ordered, or asked about by name "
        "(defects, usage, fit); other = anything else, including general, catalog, or "
        "policy inquiries not about one specific product or order.\n"
        "- priority: one of low | normal | high | urgent. Use urgent whenever the content "
        "describes a safety-critical issue (real risk of injury, fire, gas, electrical, or "
        "structural-failure hazard) regardless of stated timing or tone -- a calm \"no rush\" "
        "report of a gas leak is still urgent. Absent a safety-critical signal, urgent also "
        "applies when a stated forward deadline or event is same-day or next-day -- the "
        "resolution must precede something happening today or tomorrow. Use high for other "
        "genuine forward time pressure: a stated date or event, roughly within the next two "
        "weeks, that the resolution must precede, but not same-day or next-day. A delay that "
        "has already happened, with no upcoming date or event the resolution must precede, "
        "is not forward time pressure -- it stays normal (absent a safety signal) no matter "
        "how eager the language (\"get it moving,\" \"expedite,\" \"at your earliest "
        "convenience\"). Use low for requests with no concrete, unresolved problem "
        "and no time pressure -- general inquiries, pre-sale product questions, feedback, or "
        "account/administrative asks, including anything the customer explicitly marks as "
        "no-hurry. Use normal for actionable issues -- a concrete problem needing resolution -- "
        "when neither a safety nor a time-pressure signal is present; it remains the default "
        "when nothing else applies. Tone alone is never the signal -- content is.\n"
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


# Demo-only (T16, `eval gate --seed-regression`): deliberately degraded --
# see the module docstring. Never iterate this against any dataset; it exists
# only to produce a demonstrable regression at gate time.
DEGRADED_DEMO_PROMPT = PromptTemplate(
    version=-1,
    template=(
        "Extract a structured support ticket from the customer support email below.\n\n"
        "From: {from_}\n"
        "Subject: {subject}\n"
        "Body:\n{body}\n\n"
        "Fields to extract: category, priority, customer_name, order_id, product_name, "
        "issue_summary, requested_action.\n\n"
        "Output must conform exactly to the provided schema: no text, commentary, or "
        "markdown outside the schema's fields."
    ),
)
