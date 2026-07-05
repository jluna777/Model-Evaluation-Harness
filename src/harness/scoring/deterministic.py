"""Deterministic per-field scoring (spec §1, §6).

Normalized exact match for the five deterministic fields of
``TicketExtraction``. The two free-text fields (``issue_summary``,
``requested_action``) are judge-scored (T7) and are intentionally not
covered here -- ``composite`` (composite.py) takes their scores as inputs
once T7 lands.
"""

from harness.schema import TicketExtraction

EXACT_MATCH_FIELDS: tuple[str, ...] = ("category", "priority")
NORMALIZED_MATCH_FIELDS: tuple[str, ...] = ("customer_name", "order_id", "product_name")


def normalize(s: str | None) -> str | None:
    """Trim, casefold, and collapse internal whitespace (spec §1).

    ``None`` passes through unchanged and matches only ``None`` -- an empty
    string is a distinct, present value and never matches ``None``.
    """

    if s is None:
        return None
    return " ".join(s.casefold().split())


def score_deterministic(expected: TicketExtraction, actual: TicketExtraction) -> dict[str, int]:
    """Score the five deterministic fields as 1 (match) or 0 (no match).

    ``category`` and ``priority`` compare exactly; ``customer_name``,
    ``order_id``, and ``product_name`` compare after ``normalize``.
    """

    scores: dict[str, int] = {}
    for field in EXACT_MATCH_FIELDS:
        scores[field] = int(getattr(expected, field) == getattr(actual, field))
    for field in NORMALIZED_MATCH_FIELDS:
        scores[field] = int(
            normalize(getattr(expected, field)) == normalize(getattr(actual, field))
        )
    return scores
