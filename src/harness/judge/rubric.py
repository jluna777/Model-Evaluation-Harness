"""Versioned judge rubric, few-shot examples, and version hash (spec §4).

The judge prompt sent on every call is assembled from four pinned
components: the judge model id, this module's instructional preamble, the
binary rubric text, and the few-shot examples below. All four are hashed by
``judge_version()`` -- changing any one of them (a wording tweak, a new
few-shot, a rubric edit, a judge model bump) changes the hash and, per spec
§5, invalidates the current calibration certificate.

**Few-shot provenance rule (binding):** every example in ``FEW_SHOTS`` is
either hand-written or drawn exclusively from ``data/dev/`` -- never from
``data/golden/`` or ``data/calibration/`` items. Golden and calibration data
exist solely to *measure* the judge and the candidates; using them here
would let the judge prompt be tuned against the very data used to score it
(constitution Principle 6). The three examples below are hand-written for
the support-email domain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# Must match `models.judge` in configs/default.yaml (spec §2). Kept as an
# independent, pinned constant rather than read from config so the hash is a
# pure function of code, not of an external YAML file.
JUDGE_MODEL_ID = "gemini-2.5-pro"

PROMPT_PREAMBLE = (
    "You are grading one free-text field of an automated customer-support-"
    "ticket extraction against a human-written reference answer. You will "
    "be given the original email, the name of the field being judged, the "
    "reference value for that field, and a candidate value produced by a "
    "model. Apply the rubric below and respond with a binary verdict and a "
    "one-sentence rationale that cites the specific difference driving the "
    "verdict (or says there is none)."
)

# Verbatim spec §4 rubric text -- do not reword.
# Amended 2026-07-09 (T13 open-coding round, Cluster D): the prior wording
# ("no added claims") was being applied inconsistently to additional detail
# that was true and grounded in the source email, failing both candidates
# for content the judge's own rationale sometimes tolerated. This wording
# targets hallucination, contradiction, and missing essentials -- not
# verbosity -- while keeping the underlying semantics identical: same
# issue/action, no missing essentials, wording free.
RUBRIC_TEXT = (
    "pass = same issue/action as the reference, with no missing essentials "
    "— additional detail is acceptable when it is accurate and grounded in "
    "the email; fail = content not grounded in the email (invented or "
    "hallucinated), contradicting the email or reference, or missing "
    "something essential; wording may differ freely."
)


@dataclass(frozen=True)
class FewShot:
    """One labeled example rendered into the judge prompt."""

    reference: str
    candidate_value: str
    verdict: str  # "pass" | "fail"
    critique: str  # one line


FEW_SHOTS: tuple[FewShot, ...] = (
    FewShot(
        reference=(
            "Customer's order arrived damaged and they want a full refund."
        ),
        candidate_value=(
            "The customer received a damaged order and is requesting a refund."
        ),
        verdict="pass",
        critique="Same issue (damaged order) and same action (refund); wording differs only.",
    ),
    FewShot(
        reference=(
            "Customer wants to change their shipping address before the order ships."
        ),
        candidate_value=(
            "Customer wants to change their shipping address before the order ships "
            "and is also requesting a discount for the inconvenience."
        ),
        verdict="fail",
        critique="Adds a discount request that is not present in the reference.",
    ),
    FewShot(
        reference=(
            "Customer's subscription renewal charged them twice and they want the "
            "duplicate charge refunded."
        ),
        candidate_value="Customer was charged twice for their subscription.",
        verdict="fail",
        critique=(
            "Drops the requested action (refund of the duplicate charge), a missing essential."
        ),
    ),
)


def judge_version() -> str:
    """Stable hash of {judge model id, prompt preamble, rubric text, few-shots}.

    Any change to a component changes the digest; unchanged components give
    the same digest across runs and processes (spec §5, §7). This value is
    passed as an opaque string into ``config.fingerprint`` at the T15/T16
    call sites.
    """

    payload = {
        "model": JUDGE_MODEL_ID,
        "prompt_preamble": PROMPT_PREAMBLE,
        "rubric": RUBRIC_TEXT,
        "few_shots": [asdict(fs) for fs in FEW_SHOTS],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
