"""Unit tests for entity-name intake hygiene.

Covers two data-quality fixes at candidate-entity intake, in
`hindsight_api.engine.retain.link_utils`:

1. `_normalize_entity_name` -- collapses internal whitespace runs (including
   newlines/tabs from extraction artifacts) to a single space and strips ends,
   so stored canonical_name values never contain embedded newlines.
2. `_is_tag_shaped_name` -- conservatively recognizes category/tag-shaped
   names (e.g. "domain:lens") so they are silently skipped instead of being
   minted as entities, which measurably poisoned entity-based scoping.

Both are exercised directly (pure functions, no DB/LLM) and through the
intake function `_prepare_entities_for_resolution`, which applies
normalization then the tag-shape check in that order.
"""

import pytest

from hindsight_api.engine.retain.link_utils import (
    _is_tag_shaped_name,
    _normalize_entity_name,
    _prepare_entities_for_resolution,
)


class _FakeEntity:
    """Minimal object-style entity: has a `.text` attribute, no `.get`."""

    def __init__(self, text: str):
        self.text = text


def _entity_texts(all_entities_flat: list[dict]) -> list[str]:
    return [e["text"] for e in all_entities_flat]


# --- Attacking tests first: real entity names that must survive the tag-shape filter ---


@pytest.mark.parametrize(
    "name",
    [
        "https://api.x.ai",  # URL: contains "//" right after the colon
        "C:\\HQ_Backups",  # Windows path: drive prefix + backslash
        "12:30",  # time: digits before the colon
        "re: subject",  # space immediately after the colon
        "backup",  # no colon at all
        "x-grok-conv-id",  # hyphenated, no colon
    ],
)
def test_is_tag_shaped_name_does_not_flag_real_entities(name):
    assert _is_tag_shaped_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "domain:lens",
        "topic:memory",
        "scope:global",
    ],
)
def test_is_tag_shaped_name_flags_measured_category_labels(name):
    assert _is_tag_shaped_name(name) is True


def test_is_tag_shaped_name_matches_against_lowercased_copy():
    # Matching happens against a lowercased copy; the check still recognizes
    # the shape even when the original name carries mixed case.
    assert _is_tag_shaped_name("Domain:Lens") is True


def test_is_tag_shaped_name_empty_string_not_flagged():
    assert _is_tag_shaped_name("") is False


def test_is_tag_shaped_name_rejects_short_single_char_segments():
    # Regex requires at least 2 chars on each side of the colon; single
    # letters (e.g. drive-letter-shaped "c:x") are not treated as tags.
    assert _is_tag_shaped_name("c:x") is False


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


# --- Intake integration: _prepare_entities_for_resolution applies both fixes, in order ---


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


def test_intake_keeps_real_entities_surviving_both_fixes():
    all_entities_flat, _all_entities, entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[
            [
                {"text": "domain:lens", "type": "CONCEPT"},
                {"text": "https://api.x.ai", "type": "CONCEPT"},
            ]
        ],
    )
    assert _entity_texts(all_entities_flat) == ["https://api.x.ai"]
    assert len(entity_to_unit) == 1


def test_intake_applies_normalization_before_tag_shape_check():
    # A tag-shaped name padded with stray whitespace/newlines must still be
    # recognized as tag-shaped -- exercising the documented ordering
    # (normalize first, then check the tag shape).
    all_entities_flat, _all_entities, entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[{"text": "  domain:lens\n", "type": "CONCEPT"}]],
    )
    assert _entity_texts(all_entities_flat) == []
    assert entity_to_unit == []


def test_intake_keeps_real_entity_with_space_after_colon():
    # "re: subject" survives both fixes: normalization leaves the single
    # interior space untouched, and the tag-shape check rejects it because of
    # the space right after the colon.
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[{"text": "re: subject", "type": "CONCEPT"}]],
    )
    assert _entity_texts(all_entities_flat) == ["re: subject"]


def test_intake_skipped_tag_shaped_entity_excluded_from_nearby_entities():
    # A skipped tag-shaped entity must not leak into another entity's
    # nearby_entities co-occurrence list either -- it never enters
    # `all_entities`, which is the source `_resolve_from_candidates` reads
    # nearby_entities from.
    all_entities_flat, all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[
            [
                {"text": "domain:lens", "type": "CONCEPT"},
                {"text": "Alice", "type": "CONCEPT"},
            ]
        ],
    )
    assert _entity_texts(all_entities_flat) == ["Alice"]
    assert [e["text"] for e in all_entities[0]] == ["Alice"]


def test_intake_keeps_tag_shaped_name_that_is_a_configured_label():
    # Attacking case for the tag-shape filter itself: "key:value" is exactly
    # how tag-type entity labels are named (GH-1558), so a candidate matching
    # a configured label value must survive even though it is tag-shaped.
    entity_labels = [
        {
            "key": "use",
            "type": "multi-values",
            "tag": True,
            "values": [{"value": "use-001"}, {"value": "use-002"}],
        }
    ]
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[
            [
                {"text": "use:use-001", "type": "CONCEPT"},
                {"text": "domain:lens", "type": "CONCEPT"},
            ]
        ],
        entity_labels=entity_labels,
    )
    assert _entity_texts(all_entities_flat) == ["use:use-001"]


def test_intake_tag_shape_filter_applies_without_entity_labels():
    # Same tag-shaped name as above, but with no configured label taxonomy --
    # the default (entity_labels=None) must still skip it.
    all_entities_flat, _all_entities, _entity_to_unit = _prepare_entities_for_resolution(
        unit_ids=["u1"],
        sentences=["fact text"],
        fact_dates=[None],
        llm_entities=[[{"text": "use:use-001", "type": "CONCEPT"}]],
    )
    assert _entity_texts(all_entities_flat) == []
