from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from harness.config import Config, fingerprint, load_config

DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "configs" / "default.yaml"


class TestLoadConfig:
    def test_round_trips_default_yaml(self):
        config = load_config(DEFAULT_CONFIG_PATH)

        assert config.k == 3
        assert config.k_baseline == 6
        assert config.retry_max_attempts == 4
        assert config.price_snapshot.date == date(2026, 7, 16)
        assert config.price_snapshot.label == "approximate-at-snapshot"

    def test_price_snapshot_survives_dump_and_reload(self, tmp_path):
        config = load_config(DEFAULT_CONFIG_PATH)

        dumped_path = tmp_path / "roundtrip.yaml"
        dumped_path.write_text(
            yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
        )
        reloaded = load_config(dumped_path)

        assert reloaded == config
        assert reloaded.price_snapshot.date == date(2026, 7, 16)
        assert reloaded.price_snapshot.label == "approximate-at-snapshot"

    def test_unknown_top_level_key_raises_validation_error(self, tmp_path):
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["not_a_real_field"] = True
        bad_path = tmp_path / "bad.yaml"
        bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        with pytest.raises(ValidationError):
            load_config(bad_path)

    def test_unknown_nested_key_raises_validation_error(self, tmp_path):
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["gate"]["unexpected"] = 1
        bad_path = tmp_path / "bad_nested.yaml"
        bad_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        with pytest.raises(ValidationError):
            load_config(bad_path)


class TestFingerprint:
    def _base_kwargs(self, config: Config):
        return dict(
            config=config,
            served_versions={"candidate_a": "v1", "candidate_b": "v2"},
            judge_version="judge-v1",
            composite_mode="FULL_7",
            calibration_verdict="adequate",
        )

    def test_stable_for_identical_inputs(self):
        config = load_config(DEFAULT_CONFIG_PATH)
        kwargs = self._base_kwargs(config)

        assert fingerprint(**kwargs) == fingerprint(**kwargs)

    def test_stable_across_dict_key_ordering(self):
        config = load_config(DEFAULT_CONFIG_PATH)
        kwargs = self._base_kwargs(config)

        reordered = dict(kwargs)
        reordered["served_versions"] = {
            "candidate_b": "v2",
            "candidate_a": "v1",
        }

        assert fingerprint(**kwargs) == fingerprint(**reordered)

    @pytest.mark.parametrize(
        "override",
        [
            {"judge_version": "judge-v2"},
            {"composite_mode": "DETERMINISTIC_5"},
            {"calibration_verdict": "inadequate"},
            {"served_versions": {"candidate_a": "v1-different", "candidate_b": "v2"}},
        ],
    )
    def test_changes_when_any_single_component_changes(self, override):
        config = load_config(DEFAULT_CONFIG_PATH)
        kwargs = self._base_kwargs(config)
        changed = dict(kwargs)
        changed.update(override)

        assert fingerprint(**kwargs) != fingerprint(**changed)

    def test_changes_when_prompt_version_changes(self):
        config = load_config(DEFAULT_CONFIG_PATH)
        other_config = config.model_copy(update={"prompt_version": config.prompt_version + 1})
        kwargs = self._base_kwargs(config)

        other_kwargs = dict(kwargs)
        other_kwargs["config"] = other_config

        assert fingerprint(**kwargs) != fingerprint(**other_kwargs)

    def test_changes_when_dataset_version_changes(self):
        config = load_config(DEFAULT_CONFIG_PATH)
        other_dataset = config.dataset.model_copy(update={"version": config.dataset.version + 1})
        other_config = config.model_copy(update={"dataset": other_dataset})
        kwargs = self._base_kwargs(config)

        other_kwargs = dict(kwargs)
        other_kwargs["config"] = other_config

        assert fingerprint(**kwargs) != fingerprint(**other_kwargs)

    def test_pinned_fingerprint_value_is_unchanged_by_the_sort_keys_refactor(self):
        # Pins the exact hex digest `fingerprint()` produced for these fixed
        # inputs BEFORE removing the redundant `dict(sorted(...))` around
        # `served_versions` (json.dumps(..., sort_keys=True) already sorts
        # every dict's keys, including nested ones, so the extra sort was a
        # no-op) -- computed by running the current (pre-refactor) code once
        # and hard-coding the result, so this test fails loudly if the
        # refactor ever changes a fingerprint's value, not just its
        # stability/reproducibility (already covered above).
        # Recomputed 2026-07-09 (T13 open-coding round): `configs/default.yaml`
        # `prompt_version` moved 2 -> 3 (Cluster A defect, `low` now defined),
        # which is itself a fingerprint component -- an intentional hash
        # change, not a refactor regression.
        # Recomputed again 2026-07-09 (T13 open-coding round): `prompt_version`
        # moved 3 -> 4 (Clusters B/C plus the category boundary), another
        # intentional hash change for the same reason.
        # Checked 2026-07-16 (D1 amendment 2026-07-16d, judge re-certified and
        # confirmed gemini-2.5-pro -> gemini-3-flash-preview): the digest
        # below is UNCHANGED. `fingerprint()`'s payload only reads
        # `config.prompt_version`/`config.dataset.version` off `config`;
        # `judge_version` here is the fixed literal "judge-v1", not
        # `rubric.judge_version()` -- so neither `models.judge` nor
        # `price_snapshot` (both changed by this amendment) are hashed inputs.
        # Verified by re-running this test against the amended
        # `configs/default.yaml`, not assumed.
        config = load_config(DEFAULT_CONFIG_PATH)

        fp = fingerprint(
            config=config,
            served_versions={"candidate_b": "v2", "candidate_a": "v1"},
            judge_version="judge-v1",
            composite_mode="FULL_7",
            calibration_verdict="adequate",
        )

        assert fp == "b1ec3406d1562a03c4883b95ce660cf949f0f1e128736bfcd057683d3ce1c9fc"
