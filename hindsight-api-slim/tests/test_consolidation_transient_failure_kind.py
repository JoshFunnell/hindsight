"""Who is to blame when a consolidation batch's retries are exhausted.

Upstream's `_classify_batch_failure` answers a different question: should the retry
loop re-send this payload (RETRY), give up on re-sending it (FAIL_FAST), or not treat
it as a batch failure at all (PROPAGATE). It handles the two provider conditions it
can recognise structurally -- a ProviderRateLimitResetError, which carries a reopening
time, and a 401/403 status attribute -- by re-raising them so the task handler
reschedules or surfaces them.

Everything else it cannot recognise becomes RETRY, and a RETRY that exhausts its
attempts is reported as a batch failure. The caller then bisects and, at batch size 1,
stamps `consolidation_failed_at`. That stamp is the exclusion predicate for pending
consolidation and is cleared only by an explicit bank-wide reset, so a plain 429 with
no reset time -- the common shape -- permanently orphans healthy facts.

`classify_llm_failure` is the second question, asked only once the retries are gone:
did the memories cause this? Only "content" may end in the stamp. These tests pin the
classification order, because the order is the whole design: a typed RateLimitError
with an empty message is common, and reading the message first classifies it as
content -- the single worst answer available here.
"""

import unittest

from hindsight_api.engine.consolidation.consolidator import (
    _MAX_CONSECUTIVE_TRANSIENT_SUB_BATCHES,
    classify_llm_failure,
)


class RateLimitError(Exception):
    """Shaped like a provider SDK's typed error: right class name, no message."""


class AuthenticationError(Exception):
    pass


class _WithStatus(Exception):
    def __init__(self, status_code):
        super().__init__("")
        self.status_code = status_code


class TestClassifyLLMFailure(unittest.TestCase):
    def test_typed_rate_limit_with_no_message_is_transient(self):
        """The case a message-first classifier gets exactly backwards."""
        self.assertEqual(classify_llm_failure(RateLimitError()), "transient")

    def test_typed_auth_error_is_auth_not_transient(self):
        """Credentials do not heal by retrying, and the rows did not cause it."""
        self.assertEqual(classify_llm_failure(AuthenticationError()), "auth")

    def test_status_code_is_read_when_the_type_says_nothing(self):
        self.assertEqual(classify_llm_failure(_WithStatus(429)), "transient")
        self.assertEqual(classify_llm_failure(_WithStatus(503)), "transient")
        self.assertEqual(classify_llm_failure(_WithStatus(401)), "auth")
        self.assertEqual(classify_llm_failure(_WithStatus(403)), "auth")

    def test_message_markers_are_the_last_resort(self):
        self.assertEqual(classify_llm_failure(Exception("429 Too Many Requests")), "transient")
        self.assertEqual(classify_llm_failure(Exception("usage limit reached for this key")), "transient")
        self.assertEqual(classify_llm_failure(Exception("invalid api key")), "auth")

    def test_a_bare_number_in_prose_does_not_read_as_a_status(self):
        """Word boundaries, because `"429" in text` also fires on 14290 tokens."""
        self.assertEqual(classify_llm_failure(Exception("the batch used 14290 tokens")), "content")

    def test_unknown_failures_stay_content(self):
        """Conservative default: anything unrecognised keeps bisect-then-stamp."""
        self.assertEqual(classify_llm_failure(Exception("could not parse the model's JSON")), "content")
        self.assertEqual(classify_llm_failure(None), "content")

    def test_the_transient_streak_is_bounded(self):
        """Not stamping a transient failure removes the accidental rate limiting the
        permanent exclusion used to provide, so the streak has to be capped or every
        run re-selects the same rows and re-hits a provider already refusing traffic."""
        self.assertGreater(_MAX_CONSECUTIVE_TRANSIENT_SUB_BATCHES, 0)
        self.assertLessEqual(_MAX_CONSECUTIVE_TRANSIENT_SUB_BATCHES, 5)


class TestBatchResultCarriesTheKind(unittest.TestCase):
    """The kind has to survive the return, or the caller cannot act on it.

    Upstream narrowed `_process_memory_batch`'s third element to a bool. A bool cannot
    distinguish "these rows are unprocessable" from "the provider is down", which is
    the distinction `consolidation_failed_at` turns on, so the port widened it back to
    `LLMFailureKind | None` -- None where the bool was False.
    """

    def test_batch_llm_result_defaults_to_no_kind_when_it_did_not_fail(self):
        from hindsight_api.engine.consolidation.consolidator import _BatchLLMResult

        result = _BatchLLMResult()
        self.assertFalse(result.failed)
        self.assertIsNone(result.failure_kind)

    def test_process_memory_batch_returns_a_kind_not_a_bool(self):
        import inspect

        from hindsight_api.engine.consolidation.consolidator import _process_memory_batch

        annotation = str(inspect.signature(_process_memory_batch).return_annotation)
        self.assertIn("LLMFailureKind", annotation)


if __name__ == "__main__":
    unittest.main()
