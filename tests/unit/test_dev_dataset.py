"""Offline checks for the T12 dev set (spec §3): authoring-time validation only,
zero API calls. ``data/dev/dev.jsonl`` is the prompt-iteration scratch set --
never scored or reported -- so its only contract is that every line is a
strict ``GoldenItem`` (the same schema golden/calibration items use)."""

import json
from pathlib import Path

from harness.schema import GoldenItem

DEV_DATASET_PATH = Path(__file__).parents[2] / "data" / "dev" / "dev.jsonl"


def _read_lines() -> list[str]:
    text = DEV_DATASET_PATH.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


class TestDevDatasetShape:
    def test_file_exists(self):
        assert DEV_DATASET_PATH.is_file()

    def test_has_exactly_ten_items(self):
        assert len(_read_lines()) == 10

    def test_every_line_is_valid_json(self):
        for line in _read_lines():
            json.loads(line)  # raises on malformed JSON

    def test_every_item_validates_as_strict_golden_item(self):
        for line in _read_lines():
            GoldenItem.model_validate(json.loads(line))

    def test_ids_are_unique(self):
        ids = [GoldenItem.model_validate(json.loads(line)).id for line in _read_lines()]
        assert len(ids) == len(set(ids))

    def test_all_items_are_dev_authored_and_never_reported(self):
        # spec §3 / Global constraints: dev items are the only permitted data
        # for prompt iteration and are excluded from all reported numbers.
        for line in _read_lines():
            item = GoldenItem.model_validate(json.loads(line))
            assert item.meta.slice == "nominal"
            assert item.meta.edited is False
            assert item.meta.difficulty in (1, 2)


class TestDevDatasetCoverage:
    def test_covers_every_category(self):
        categories = {
            GoldenItem.model_validate(json.loads(line)).expected.category for line in _read_lines()
        }
        assert categories == {"billing", "shipping", "account", "product", "other"}

    def test_has_items_with_absent_optional_fields(self):
        items = [GoldenItem.model_validate(json.loads(line)) for line in _read_lines()]
        assert any(item.expected.order_id is None for item in items)
        assert any(item.expected.product_name is None for item in items)

    def test_has_a_multi_request_tie_break_item(self):
        items = [GoldenItem.model_validate(json.loads(line)) for line in _read_lines()]
        assert any("tie_break" in item.meta.categories for item in items)

    def test_has_a_quoted_reply_item(self):
        items = [GoldenItem.model_validate(json.loads(line)) for line in _read_lines()]
        assert any("quoted_reply" in item.meta.categories for item in items)
