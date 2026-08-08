"""Unit tests for entity-name intake hygiene.

Covers data-quality fixes at candidate-entity intake, in
`hindsight_api.engine.retain.link_utils`:

1. `_normalize_entity_name` -- collapses internal whitespace runs (including
   newlines/tabs from extraction artifacts) to a single space and strips ends,
   so stored canonical_name values never contain embedded newlines.

Exercised directly (pure function, no DB/LLM) and through the intake function
`_prepare_entities_for_resolution`, which applies it to every candidate name.
"""

import pytest

from hindsight_api.engine.retain.link_utils import (
    _normalize_entity_name,
    _prepare_entities_for_resolution,
)


class _FakeEntity:
    """Minimal object-style entity: has a `.text` attribute, no `.get`."""

    def __init__(self, text: str):
        self.text = text


def _entity_texts(all_entities_flat: list[dict]) -> list[str]:
    return [e["text"] for e in all_entities_flat]


# --- _normalize_entity_name ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("foo\nbar", "foo bar"),
        ("a\r\n b\tc", "a b c"),
        ("Normal Name", "Normal Name"),
        ("  leading and trailing  ", "leading and trailing"),
        ("multiple   spaces    inside", "multiple spaces inside"),
    ],
)
def test_normalize_entity_name_collapses_whitespace(raw, expected):
    assert _normalize_entity_name(raw) == expected


def test_normalize_entity_name_preserves_case():
    assert _normalize_entity_name("MiXeD\nCaSe") == "MiXeD CaSe"


def test_normalize_entity_name_all_whitespace_becomes_empty():
    # Matches the existing code's (lack of) special-casing for empty names:
    # normalization just yields "" like any other already-empty candidate
    # name would -- no new "skip empty" behavior is invented here.
    assert _normalize_entity_name("   \n\t  ") == ""


# --- Intake integration: _prepare_entities_for_resolution applies normalization ---


def test_intake_normalizes_whitespace_in_stored_text():
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[{"text": "foo\nbar", "type": "CONCEPT"}]],
    )
    assert _entity_texts(all_entities_flat) == ["foo bar"]


def test_intake_normalizes_object_style_entities_with_text_attribute():
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[_FakeEntity("a\r\n b\tc")]],
    )
    assert _entity_texts(all_entities_flat) == ["a b c"]


def test_intake_leaves_normal_name_unchanged():
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[{"text": "re: subject", "type": "CONCEPT"}]],
    )
    assert _entity_texts(all_entities_flat) == ["re: subject"]
