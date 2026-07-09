"""Run configuration (spec §9) and run fingerprinting (spec §7).

``Config`` is the Pydantic-validated shape of ``configs/default.yaml``: model
IDs, prompt version, dataset path/version, replicate counts, gate decision
parameters, a dated price snapshot, and Langfuse settings. Every nested model
forbids unknown keys so a typo or stale field in the YAML fails loudly rather
than being silently ignored.

``fingerprint`` hashes everything a run must match against a baseline before
they are comparable (spec §7): prompt version, dataset version, resolved/served
model versions, judge version, composite definition, calibration verdict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class ModelIds(BaseModel):
    """Pinned model IDs for both candidates and the judge (spec §2)."""

    model_config = ConfigDict(extra="forbid")

    candidate_a: str
    candidate_b: str
    judge: str


class DatasetConfig(BaseModel):
    """Golden-dataset location and version (spec §3, §7)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    version: int


class GateConfig(BaseModel):
    """CI gate decision-rule parameters (D3). Changing these needs a dated
    decision-log amendment (spec §9)."""

    model_config = ConfigDict(extra="forbid")

    margin: float
    alpha: float


class ModelPrice(BaseModel):
    """List price in USD per million tokens (MTok)."""

    model_config = ConfigDict(extra="forbid")

    input_per_mtok: float
    output_per_mtok: float


class PriceSnapshot(BaseModel):
    """Dated, labeled list-price snapshot for gate cost reporting (spec §7, §9).

    Prices are approximate-at-snapshot, not a live feed -- ``label`` records
    that explicitly so reports never present them as current.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    label: str
    candidate_a: ModelPrice
    candidate_b: ModelPrice
    judge: ModelPrice


class LangfuseSettings(BaseModel):
    """Non-secret Langfuse settings; credentials come from environment
    variables, never from config (spec §8)."""

    model_config = ConfigDict(extra="forbid")

    host: str


class Config(BaseModel):
    """Validated run configuration (spec §9).

    Defaults baked into ``configs/default.yaml`` are the decided D2/D3 values
    (K=3, K_baseline=6, margin=2.0, alpha=0.05).
    """

    model_config = ConfigDict(extra="forbid")

    models: ModelIds
    prompt_version: int
    dataset: DatasetConfig
    k: int
    k_baseline: int
    retry_max_attempts: int
    gate: GateConfig
    price_snapshot: PriceSnapshot
    langfuse: LangfuseSettings


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file (e.g. ``configs/default.yaml``)."""

    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Config.model_validate(data)


def fingerprint(
    config: Config,
    served_versions: Mapping[str, str],
    judge_version: str,
    composite_mode: str,
    calibration_verdict: str,
) -> str:
    """Stable hash of everything a run must match to compare against a baseline.

    Fields per spec §7: prompt version, dataset version, resolved/served model
    versions (``served_versions``, the alias-drift guard of spec §2), judge
    version, composite definition, and calibration verdict. The result is
    stable across dict key ordering in ``served_versions``.

    ``judge_version`` is accepted as an opaque string here -- the real
    ``judge_version()`` hash implementation arrives in T7 and is wired in at
    the T15/T16 call sites. ``composite_mode`` and ``calibration_verdict`` are
    likewise accepted as any value with a stable ``str()`` (e.g. an enum)
    since their defining modules (T2, T14) do not exist yet.
    """

    payload = {
        "prompt_version": config.prompt_version,
        "dataset_version": config.dataset.version,
        # `json.dumps(..., sort_keys=True)` below already sorts every dict's
        # keys, including this one's -- pre-sorting `served_versions` here
        # was redundant (a no-op given sort_keys=True downstream).
        "served_versions": dict(served_versions),
        "judge_version": judge_version,
        "composite_mode": str(composite_mode),
        "calibration_verdict": str(calibration_verdict),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
