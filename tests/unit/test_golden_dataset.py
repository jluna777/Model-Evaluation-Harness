"""Reconciliation test for the T13 golden dataset (spec §3; ticket
`docs/tickets/T13-golden-set.md`).

``data/golden/golden.jsonl`` does not exist yet during OFFLINE PREP -- Gemini
generation and owner curation/open-coding happen after this commit, once
billing is enabled. Every test that reads the real file SKIPs gracefully with
a clear reason until it lands; once it exists, the same tests fully enforce
the freeze contract (50 items, 32/18 split, per-category counts reconciled
against ``taxonomy.md``, the four multi-request variants, the >=80%
generator-family bound, and strict ``GoldenItem`` validation).

The reconciliation *logic* itself (``reconcile`` and friends) is proven now,
independently of the real file, against small synthetic fixtures built in
``tmp_path`` -- so it is known-correct before the 50-item file is committed,
per the ticket's TDD instruction ("write it failing against the draft data
expectations ... go green").
"""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.schema import GoldenItem

REPO_ROOT = Path(__file__).parents[2]
GOLDEN_PATH = REPO_ROOT / "data" / "golden" / "golden.jsonl"
TAXONOMY_PATH = REPO_ROOT / "data" / "golden" / "taxonomy.md"

EXPECTED_TOTAL = 50
EXPECTED_NOMINAL = 32
EXPECTED_ADVERSARIAL = 18
MIN_DISTINCT_FAMILY_FRACTION = 0.8

# The four multi-request variants (spec §1 amendment 2026-07-07 / ticket AC):
# the general "every category >=2" rule is relaxed to ">=1" for these tags
# specifically, since they are fine-grained sub-splits of one stressor axis.
MULTI_REQUEST_VARIANTS = frozenset(
    {
        "multi_request_plain",
        "multi_request_within_supersession",
        "multi_request_threaded_supersession",
        "multi_request_reference_resolution",
    }
)

# Generator families considered "same family as a candidate" (spec §2/§3:
# Candidate A = Claude Haiku 4.5 / Anthropic, Candidate B = GPT-5.4 mini /
# OpenAI). Matched by substring against `meta.generator`, case-insensitive.
_CANDIDATE_FAMILY_MARKERS = {
    "anthropic": ("claude", "anthropic"),
    "openai": ("gpt", "openai"),
}

_COUNTS_BLOCK_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)


def generator_family(generator: str) -> str:
    """Classify a ``meta.generator`` string as 'anthropic', 'openai', or 'other'.

    'other' includes Gemini and any generator distinct from both candidates --
    it is the family the spec §3 >=80% bound counts toward.
    """

    lowered = generator.lower()
    for family, markers in _CANDIDATE_FAMILY_MARKERS.items():
        if any(marker in lowered for marker in markers):
            return family
    return "other"


def load_taxonomy_counts(taxonomy_path: Path) -> dict[str, int]:
    """Parse the fenced ` ```text ` category-count block from taxonomy.md."""

    text = taxonomy_path.read_text(encoding="utf-8")
    match = _COUNTS_BLOCK_RE.search(text)
    if not match:
        raise ValueError(f"no machine-readable ```text counts block found in {taxonomy_path}")
    counts: dict[str, int] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, value = line.partition(":")
        counts[name.strip()] = int(value.strip())
    return counts


def load_golden_items(golden_path: Path) -> list[GoldenItem]:
    """Load and strictly validate every line of a golden-format JSONL file."""

    lines = [ln for ln in golden_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [GoldenItem.model_validate(json.loads(ln)) for ln in lines]


def category_counts(items: list[GoldenItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for category in item.meta.categories:
            counts[category] = counts.get(category, 0) + 1
    return counts


def distinct_family_fraction(items: list[GoldenItem]) -> float:
    if not items:
        return 0.0
    distinct = sum(1 for item in items if generator_family(item.meta.generator) == "other")
    return distinct / len(items)


def _min_count_for(category: str) -> int:
    """The per-category floor: 1 for multi-request variant sub-tags, else 2."""

    return 1 if category in MULTI_REQUEST_VARIANTS else 2


def reconcile(
    items: list[GoldenItem],
    taxonomy_counts: dict[str, int],
    *,
    expected_total: int = EXPECTED_TOTAL,
    expected_nominal: int = EXPECTED_NOMINAL,
    expected_adversarial: int = EXPECTED_ADVERSARIAL,
    min_distinct_fraction: float = MIN_DISTINCT_FAMILY_FRACTION,
) -> None:
    """Every T13 reconciliation assertion; raises ``AssertionError`` on the
    first violation found. Parameterized on the expected totals so the same
    logic exercises both the real 50-item contract and small synthetic
    fixtures."""

    assert len(items) == expected_total, f"expected {expected_total} items, got {len(items)}"

    ids = [item.id for item in items]
    assert len(ids) == len(set(ids)), "duplicate item ids"

    nominal = [i for i in items if i.meta.slice == "nominal"]
    adversarial = [i for i in items if i.meta.slice == "adversarial"]
    assert len(nominal) == expected_nominal, (
        f"expected {expected_nominal} nominal items, got {len(nominal)}"
    )
    assert len(adversarial) == expected_adversarial, (
        f"expected {expected_adversarial} adversarial items, got {len(adversarial)}"
    )

    assert sum(taxonomy_counts.values()) == expected_total, (
        f"taxonomy.md category counts sum to {sum(taxonomy_counts.values())}, "
        f"expected {expected_total}"
    )

    actual_counts = category_counts(items)
    for category, target in taxonomy_counts.items():
        actual = actual_counts.get(category, 0)
        assert actual == target, (
            f"category {category!r}: taxonomy.md declares {target}, golden.jsonl has {actual}"
        )
        floor = _min_count_for(category)
        assert actual >= floor, f"category {category!r} has fewer than {floor} item(s) ({actual})"

    fraction = distinct_family_fraction(items)
    assert fraction >= min_distinct_fraction, (
        f"only {fraction:.0%} of items are from a generator family distinct from both "
        f"candidates; need >= {min_distinct_fraction:.0%}"
    )


def assert_multi_request_variants_covered(actual_counts: dict[str, int]) -> None:
    """Ticket AC: each of the four multi-request variants has >=1 item."""

    missing = [v for v in sorted(MULTI_REQUEST_VARIANTS) if actual_counts.get(v, 0) < 1]
    assert not missing, f"missing required multi-request variant(s): {missing}"


# --------------------------------------------------------------------------
# Real-file tests: SKIP gracefully until data/golden/golden.jsonl exists.
# --------------------------------------------------------------------------


def _skip_if_golden_dataset_absent() -> None:
    if not GOLDEN_PATH.is_file():
        pytest.skip(
            "data/golden/golden.jsonl does not exist yet -- T13 golden-set freeze "
            "happens after Gemini generation (post-billing) and owner open-coding/"
            "curation. This test enforces the freeze contract once the file lands."
        )


class TestGoldenDatasetReconciliation:
    def test_reconciles_against_taxonomy(self):
        _skip_if_golden_dataset_absent()
        items = load_golden_items(GOLDEN_PATH)
        counts = load_taxonomy_counts(TAXONOMY_PATH)
        reconcile(items, counts)
        assert_multi_request_variants_covered(category_counts(items))

    def test_every_item_is_a_strict_golden_item(self):
        _skip_if_golden_dataset_absent()
        # load_golden_items calls GoldenItem.model_validate per line; a
        # validation error (e.g. a lowercase order_id) surfaces here as a
        # test failure rather than silently passing.
        items = load_golden_items(GOLDEN_PATH)
        assert len(items) == EXPECTED_TOTAL


# --------------------------------------------------------------------------
# Synthetic-fixture tests: prove the reconciliation logic now, independent of
# the real file, via small tmp_path fixtures.
# --------------------------------------------------------------------------


def _item_dict(
    item_id: str,
    slice_: str,
    categories: list[str],
    *,
    order_id: str | None = None,
    generator: str = "gemini-3.5-flash",
    difficulty: int = 1,
) -> dict:
    return {
        "id": item_id,
        "email": {"from": "a@example.com", "subject": "s", "body": "b"},
        "expected": {
            "category": "other",
            "priority": "low",
            "customer_name": None,
            "order_id": order_id,
            "product_name": None,
            "issue_summary": "x",
            "requested_action": "y",
        },
        "meta": {
            "slice": slice_,
            "categories": categories,
            "difficulty": difficulty,
            "generator": generator,
            "edited": False,
            "notes": "",
        },
    }


def _write_jsonl(path: Path, items: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")
    return path


class TestGeneratorFamilyClassification:
    @pytest.mark.parametrize(
        ("generator", "family"),
        [
            ("claude-fable-5", "anthropic"),
            ("claude-haiku-4-5-20251001", "anthropic"),
            ("gpt-5.4-mini", "openai"),
            ("openai-gpt-4o", "openai"),
            ("gemini-3.5-flash", "other"),
            ("human-owner", "other"),
        ],
    )
    def test_classifies_known_generators(self, generator, family):
        assert generator_family(generator) == family


class TestLoadTaxonomyCounts:
    def test_parses_fenced_text_block(self, tmp_path):
        taxonomy_path = tmp_path / "taxonomy.md"
        taxonomy_path.write_text(
            "# Taxonomy\n\nsome prose\n\n```text\ncat_a: 3\ncat_b: 2\n```\n",
            encoding="utf-8",
        )
        assert load_taxonomy_counts(taxonomy_path) == {"cat_a": 3, "cat_b": 2}

    def test_raises_when_block_missing(self, tmp_path):
        taxonomy_path = tmp_path / "taxonomy.md"
        taxonomy_path.write_text("# Taxonomy\n\nno fenced block here\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no machine-readable"):
            load_taxonomy_counts(taxonomy_path)


class TestReconciliationLogicSynthetic:
    """Proves the reconciliation logic before the real 50-item file exists."""

    def _passing_fixture(self, tmp_path):
        items_raw = [
            _item_dict("g-001", "nominal", ["cat_a"]),
            _item_dict("g-002", "nominal", ["cat_a"]),
            _item_dict("g-003", "adversarial", ["cat_b"], generator="claude-fable-5"),
            _item_dict("g-004", "adversarial", ["cat_b"]),
        ]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        return load_golden_items(golden_path)

    def test_passing_fixture_reconciles_cleanly(self, tmp_path):
        items = self._passing_fixture(tmp_path)
        counts = {"cat_a": 2, "cat_b": 2}
        # 1/4 Claude-family = 75% distinct, so relax the fraction for this
        # small fixture; the bound itself is exercised in its own test below.
        reconcile(
            items,
            counts,
            expected_total=4,
            expected_nominal=2,
            expected_adversarial=2,
            min_distinct_fraction=0.5,
        )

    def test_fails_when_total_count_wrong(self, tmp_path):
        items_raw = [_item_dict("g-001", "nominal", ["cat_a"])]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        items = load_golden_items(golden_path)
        with pytest.raises(AssertionError, match="expected 4 items"):
            reconcile(
                items,
                {"cat_a": 1},
                expected_total=4,
                expected_nominal=1,
                expected_adversarial=0,
            )

    def test_fails_when_slice_split_wrong(self, tmp_path):
        items_raw = [
            _item_dict("g-001", "nominal", ["cat_a"]),
            _item_dict("g-002", "nominal", ["cat_a"]),
            _item_dict("g-003", "nominal", ["cat_b"]),  # should be adversarial
            _item_dict("g-004", "adversarial", ["cat_b"]),
        ]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        items = load_golden_items(golden_path)
        with pytest.raises(AssertionError, match="nominal"):
            reconcile(
                items,
                {"cat_a": 2, "cat_b": 2},
                expected_total=4,
                expected_nominal=2,
                expected_adversarial=2,
            )

    def test_fails_when_category_count_mismatches_taxonomy(self, tmp_path):
        items = self._passing_fixture(tmp_path)
        # taxonomy.md over-declares cat_a: 3, but only 2 items carry that tag.
        with pytest.raises(AssertionError, match="cat_a"):
            reconcile(
                items,
                {"cat_a": 3, "cat_b": 2},
                expected_total=4,
                expected_nominal=2,
                expected_adversarial=2,
                min_distinct_fraction=0.5,
            )

    def test_fails_when_category_below_general_floor_of_two(self, tmp_path):
        items_raw = [
            _item_dict("g-001", "nominal", ["cat_a"]),
            _item_dict("g-002", "nominal", ["cat_c"]),  # only 1 item, not a multi-request tag
            _item_dict("g-003", "adversarial", ["cat_b"]),
            _item_dict("g-004", "adversarial", ["cat_b"]),
        ]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        items = load_golden_items(golden_path)
        with pytest.raises(AssertionError, match="fewer than 2"):
            reconcile(
                items,
                {"cat_a": 1, "cat_c": 1, "cat_b": 2},
                expected_total=4,
                expected_nominal=2,
                expected_adversarial=2,
                min_distinct_fraction=0.5,
            )

    def test_multi_request_variant_tag_allowed_at_one_item(self, tmp_path):
        # A multi-request variant tag at count 1 must NOT trip the general
        # >=2 floor (the ticket's explicit relaxation to >=1 for these tags):
        # a non-variant category ("cat_a") at the same count of 1 would fail
        # (see test_fails_when_category_below_general_floor_of_two).
        items_raw = [
            _item_dict("g-001", "nominal", ["multi_request_plain"]),
            _item_dict("g-002", "nominal", ["cat_a"]),
            _item_dict("g-003", "nominal", ["cat_a"]),
            _item_dict("g-004", "adversarial", ["cat_b"]),
            _item_dict("g-005", "adversarial", ["cat_b"]),
        ]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        items = load_golden_items(golden_path)
        reconcile(
            items,
            {"multi_request_plain": 1, "cat_a": 2, "cat_b": 2},
            expected_total=5,
            expected_nominal=3,
            expected_adversarial=2,
        )

    def test_fails_generator_family_bound(self, tmp_path):
        items_raw = [
            _item_dict("g-001", "nominal", ["cat_a"], generator="claude-fable-5"),
            _item_dict("g-002", "nominal", ["cat_a"], generator="claude-fable-5"),
            _item_dict("g-003", "adversarial", ["cat_b"], generator="gpt-5.4-mini"),
            _item_dict("g-004", "adversarial", ["cat_b"], generator="gemini-3.5-flash"),
        ]
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", items_raw)
        items = load_golden_items(golden_path)
        with pytest.raises(AssertionError, match="generator family"):
            reconcile(
                items,
                {"cat_a": 2, "cat_b": 2},
                expected_total=4,
                expected_nominal=2,
                expected_adversarial=2,
                # default 0.8 bound: only 1/4 = 25% distinct here -> must fail
            )

    def test_assert_multi_request_variants_covered_passes_when_all_present(self):
        counts = {
            "multi_request_plain": 2,
            "multi_request_within_supersession": 1,
            "multi_request_threaded_supersession": 2,
            "multi_request_reference_resolution": 1,
        }
        assert_multi_request_variants_covered(counts)  # no raise

    def test_assert_multi_request_variants_covered_fails_when_one_missing(self):
        counts = {
            "multi_request_plain": 2,
            "multi_request_within_supersession": 1,
            "multi_request_threaded_supersession": 0,
            "multi_request_reference_resolution": 1,
        }
        with pytest.raises(AssertionError, match="multi_request_threaded_supersession"):
            assert_multi_request_variants_covered(counts)

    def test_strict_validation_rejects_lowercase_order_id(self, tmp_path):
        raw = _item_dict("g-001", "nominal", ["cat_a"], order_id="ord-12345")
        golden_path = _write_jsonl(tmp_path / "golden.jsonl", [raw])
        with pytest.raises(ValidationError):
            load_golden_items(golden_path)
