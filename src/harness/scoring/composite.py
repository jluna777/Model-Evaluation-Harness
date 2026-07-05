"""Per-email composite score (spec §6).

Both modes share one definition -- unweighted mean of the included fields'
0/1 scores, scaled to 0-100 -- and differ only in which fields are included:
``FULL_7`` averages all seven scored fields; ``DETERMINISTIC_5`` excludes the
two judge-scored fields for judge-excluded reporting (e.g. an inadequate
calibration verdict). The composite definition is part of the run
fingerprint (``config.fingerprint``'s ``composite_mode`` argument) and its
mode names are consumed verbatim by the CI gate (T15/T16) -- do not rename.
"""

from collections.abc import Mapping
from enum import StrEnum

DETERMINISTIC_FIELDS: tuple[str, ...] = (
    "category",
    "priority",
    "customer_name",
    "order_id",
    "product_name",
)
JUDGED_FIELDS: tuple[str, ...] = ("issue_summary", "requested_action")


class CompositeMode(StrEnum):
    FULL_7 = "FULL_7"
    DETERMINISTIC_5 = "DETERMINISTIC_5"


_MODE_FIELDS: dict[CompositeMode, tuple[str, ...]] = {
    CompositeMode.FULL_7: DETERMINISTIC_FIELDS + JUDGED_FIELDS,
    CompositeMode.DETERMINISTIC_5: DETERMINISTIC_FIELDS,
}


def composite(field_scores: Mapping[str, int], mode: CompositeMode) -> float:
    """Unweighted mean of the fields included by ``mode``, on a 0-100 scale."""

    included = [field_scores[field] for field in _MODE_FIELDS[mode]]
    return sum(included) / len(included) * 100
