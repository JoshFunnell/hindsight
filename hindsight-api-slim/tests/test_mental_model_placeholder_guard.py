"""Unit tests for the mental-model refresh placeholder / empty-retrieval guard.

These are the deterministic legs of #2959 / #2894. The dry-run refresh tests
exercise the same helpers through the write path when a full engine fixture
is available.
"""

from hindsight_api.engine.memory_engine import (
    is_placeholder_refresh_candidate,
    should_refuse_failed_refresh_overwrite,
)
from hindsight_api.engine.reflect.agent import NO_ANSWER_TEXT


def test_no_answer_text_is_a_placeholder():
    assert is_placeholder_refresh_candidate(NO_ANSWER_TEXT)
    assert is_placeholder_refresh_candidate(f"  {NO_ANSWER_TEXT}  ")


def test_iteration_limit_render_is_a_placeholder():
    assert is_placeholder_refresh_candidate(
        "I was unable to formulate a complete answer after 10 iterations."
    )


def test_real_document_is_not_a_placeholder():
    assert not is_placeholder_refresh_candidate("# Team\n\nAlice joined in 2024.")
    assert not is_placeholder_refresh_candidate("")
    assert not is_placeholder_refresh_candidate("   ")


def test_refuse_placeholder_or_empty_retrieval_over_real_content():
    assert should_refuse_failed_refresh_overwrite(
        has_delta_baseline=True, is_placeholder=True, fresh_retrieval_empty=False
    )
    assert should_refuse_failed_refresh_overwrite(
        has_delta_baseline=True, is_placeholder=False, fresh_retrieval_empty=True
    )


def test_allow_bootstrap_write_on_empty_or_pending_model():
    assert not should_refuse_failed_refresh_overwrite(
        has_delta_baseline=False, is_placeholder=True, fresh_retrieval_empty=True
    )


def test_allow_real_refresh_with_facts():
    assert not should_refuse_failed_refresh_overwrite(
        has_delta_baseline=True, is_placeholder=False, fresh_retrieval_empty=False
    )
