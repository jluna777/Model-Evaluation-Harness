from dataclasses import replace

from harness.judge import rubric

RUBRIC_SENTENCE = (
    "pass = same factual content as the reference — same issue/action, "
    "no added claims, no missing essentials; wording may differ freely."
)


class TestRubricTextVerbatim:
    def test_matches_spec_section_4_exactly(self):
        assert rubric.RUBRIC_TEXT == RUBRIC_SENTENCE


class TestFewShotProvenance:
    def test_module_docstring_states_the_provenance_rule(self):
        doc = rubric.__doc__ or ""
        assert "data/dev" in doc
        assert "golden" in doc.lower()
        assert "calibration" in doc.lower()

    def test_at_least_two_few_shots_with_both_verdicts(self):
        assert len(rubric.FEW_SHOTS) >= 2
        verdicts = {fs.verdict for fs in rubric.FEW_SHOTS}
        assert verdicts == {"pass", "fail"}

    def test_every_few_shot_has_a_one_line_critique(self):
        for fs in rubric.FEW_SHOTS:
            assert fs.critique.strip()
            assert "\n" not in fs.critique.strip()


class TestJudgeVersionHash:
    def test_is_a_sha256_hex_digest(self):
        value = rubric.judge_version()
        assert len(value) == 64
        int(value, 16)  # raises ValueError if not hex

    def test_stable_across_repeated_calls(self):
        assert rubric.judge_version() == rubric.judge_version()

    def test_changes_when_a_few_shot_changes(self, monkeypatch):
        before = rubric.judge_version()
        mutated = list(rubric.FEW_SHOTS)
        mutated[0] = replace(mutated[0], critique=mutated[0].critique + " (edited)")
        monkeypatch.setattr(rubric, "FEW_SHOTS", tuple(mutated))

        assert rubric.judge_version() != before

    def test_changes_when_the_rubric_text_changes(self, monkeypatch):
        before = rubric.judge_version()
        monkeypatch.setattr(rubric, "RUBRIC_TEXT", rubric.RUBRIC_TEXT + " (edited)")

        assert rubric.judge_version() != before

    def test_changes_when_the_prompt_preamble_changes(self, monkeypatch):
        before = rubric.judge_version()
        monkeypatch.setattr(rubric, "PROMPT_PREAMBLE", rubric.PROMPT_PREAMBLE + " (edited)")

        assert rubric.judge_version() != before

    def test_changes_when_the_judge_model_id_changes(self, monkeypatch):
        before = rubric.judge_version()
        monkeypatch.setattr(rubric, "JUDGE_MODEL_ID", "gemini-2.5-pro")

        assert rubric.judge_version() != before

    def test_unchanged_inputs_give_stable_hash_across_reimport_shaped_calls(self):
        # Recomputing from the same module-level constants (no monkeypatching)
        # must reproduce the exact same digest every time.
        digests = {rubric.judge_version() for _ in range(5)}
        assert len(digests) == 1
