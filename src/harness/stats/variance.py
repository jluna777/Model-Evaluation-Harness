"""Between-item vs between-replicate variance decomposition (spec §6).

Backs the per-run variance decomposition in ``eval run`` reports: how much
score variability comes from genuine item-to-item difficulty differences
(``between_item``) versus run-to-run noise on the *same* item across
replicates (``between_replicate``) -- informing the future K decision (D3).

Standard one-way decomposition over an item x replicate score array:
  - ``between_item``: the (population) variance of the per-item means.
  - ``between_replicate``: the mean, across items, of each item's own
    (population) within-item variance across its replicates.

Both use ``numpy``'s default ``ddof=0`` (population variance) -- these are
literal descriptive decompositions of the observed array, not unbiased
estimators of latent random-effects parameters.

This module is pure array-in/dict-out computation with no notion of which
fields are judged vs deterministic: callers (T10 reports, T11 CLI) run it
twice -- once on the full composite score array, once on a judged-fields-only
composite array -- to separate judged-field run variance from the rest
(spec §6). Keeping that field-group logic out of this module is deliberate.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def variance_components(scores: Sequence[Sequence[float]] | np.ndarray) -> dict[str, float]:
    """Between-item and between-replicate variance components of ``scores``.

    ``scores`` is a 2-D item x replicate array (rows = items, columns =
    replicates). Returns ``{"between_item": ..., "between_replicate": ...}``.
    """

    arr = np.asarray(scores, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"scores must be a 2-D item x replicate array, got ndim={arr.ndim}")
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError("scores must have at least one item and one replicate")

    item_means = arr.mean(axis=1)
    between_item = float(np.var(item_means))

    within_item_variances = np.var(arr, axis=1)
    between_replicate = float(np.mean(within_item_variances))

    return {"between_item": between_item, "between_replicate": between_replicate}
