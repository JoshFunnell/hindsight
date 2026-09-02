"""The #2894 clobber guard: when it must refuse, and when it must stay out of the way.

The incident (measured 2026-08-01): with the embedding index away, reflect retrieves
nothing, the model writes fluent generic no-info text, every emptiness check upstream
has reads it as a real answer, and the working document is overwritten. That is why
mm_refresh_safe.py had to snapshot and restore around every refresh. The local guard
refuses such a refresh and preserves the previous content.

Two properties of the guard are decided here rather than in prose, because both were
got wrong first:

1. It is keyed on a TRANSITION, not a state. "This refresh retrieved nothing" also
   describes a model whose first grounded refresh comes back empty, and overwriting a
   hand-seeded document there is a bootstrap, not a clobber. The guard fires only when
   a PREVIOUS refresh had grounded the document in facts and this one did not.
2. It is OFF unless the deployment turns it on. Upstream's ``patch_reflect`` fixture
   (tests/test_mental_model_delta.py) returns real text with an all-empty ``based_on``,
   which is the same input shape as the incident, so an on-by-default guard refuses
   upstream's own suite: measured 2026-09-02 (S107) on the main port at 828a6e55d, it
   was part of putting 26 upstream tests red that were green on pristine main.

These are unit tests of the predicate, not of the refresh: they build the inputs the
guard reads and assert which way it goes. The predicate lives inside the refresh
closure and cannot be imported, so it is mirrored below and the mirror is pinned to
the engine source by the last test -- a mirror that drifts tests nothing. The
end-to-end behaviour (a refusal preserves content and raises MentalModelRefreshError)
is upstream's ``test_empty_reflect_answer_preserves_existing_content``.
"""

import inspect
import re
import unittest

from hindsight_api.config import (
    DEFAULT_MENTAL_MODEL_EMPTY_RETRIEVAL_GUARD,
    ENV_MENTAL_MODEL_EMPTY_RETRIEVAL_GUARD,
)


def _guard_fires(
    *,
    enabled: bool,
    has_delta_baseline: bool,
    this_run_based_on: dict,
    previous_reflect_response: dict | None,
) -> bool:
    """The guard's predicate, in the order the engine evaluates it."""
    fresh_retrieval_empty = not any(v for v in this_run_based_on.values() if v)
    prior_had_facts = any(
        isinstance(facts, list) and facts
        for facts in ((previous_reflect_response or {}).get("based_on") or {}).values()
    )
    return enabled and has_delta_baseline and fresh_retrieval_empty and prior_had_facts


GROUNDED = {"based_on": {"observation": [{"id": "f1", "text": "Alice is the lead."}], "world": []}}
EMPTY_NOW = {"observation": [], "world": [], "experience": [], "mental-models": [], "directives": []}


class EmptyRetrievalGuardTests(unittest.TestCase):
    def test_fires_when_a_grounded_document_suddenly_retrieves_nothing(self):
        """The incident itself."""
        self.assertTrue(
            _guard_fires(
                enabled=True,
                has_delta_baseline=True,
                this_run_based_on=EMPTY_NOW,
                previous_reflect_response=GROUNDED,
            )
        )

    def test_silent_on_a_first_grounded_refresh(self):
        """No previous refresh retrieved anything, so writing is a bootstrap."""
        self.assertFalse(
            _guard_fires(
                enabled=True,
                has_delta_baseline=True,
                this_run_based_on=EMPTY_NOW,
                previous_reflect_response=None,
            )
        )

    def test_silent_when_the_previous_payload_has_the_keys_but_no_facts(self):
        """A stored payload with empty lists is not evidence of grounding."""
        self.assertFalse(
            _guard_fires(
                enabled=True,
                has_delta_baseline=True,
                this_run_based_on=EMPTY_NOW,
                previous_reflect_response={"based_on": {"observation": [], "world": []}},
            )
        )

    def test_silent_when_this_refresh_did_retrieve_something(self):
        self.assertFalse(
            _guard_fires(
                enabled=True,
                has_delta_baseline=True,
                this_run_based_on={"observation": [{"id": "f2", "text": "Bob joined."}]},
                previous_reflect_response=GROUNDED,
            )
        )

    def test_silent_without_existing_real_content(self):
        """Nothing to clobber."""
        self.assertFalse(
            _guard_fires(
                enabled=True,
                has_delta_baseline=False,
                this_run_based_on=EMPTY_NOW,
                previous_reflect_response=GROUNDED,
            )
        )

    def test_off_unless_the_deployment_turns_it_on(self):
        self.assertFalse(DEFAULT_MENTAL_MODEL_EMPTY_RETRIEVAL_GUARD)
        self.assertEqual(
            ENV_MENTAL_MODEL_EMPTY_RETRIEVAL_GUARD,
            "HINDSIGHT_API_MENTAL_MODEL_EMPTY_RETRIEVAL_GUARD",
        )
        self.assertFalse(
            _guard_fires(
                enabled=False,
                has_delta_baseline=True,
                this_run_based_on=EMPTY_NOW,
                previous_reflect_response=GROUNDED,
            )
        )

    def test_engine_still_spells_the_predicate_this_way(self):
        """Pin the mirror to the engine, whitespace-insensitively.

        Matching the four conjuncts in order rather than a formatted block, so a
        reformat does not fail this while an edited conjunct does.
        """
        from hindsight_api.engine import memory_engine

        source = " ".join(inspect.getsource(memory_engine).split())
        self.assertRegex(
            source,
            re.compile(
                r'mental_model_empty_retrieval_guard", False\)\s*\)?\s*and has_delta_baseline'
                r"\s*and _patch_fresh_retrieval_empty\s*and _prior_retrieval_had_facts"
            ),
        )


if __name__ == "__main__":
    unittest.main()
