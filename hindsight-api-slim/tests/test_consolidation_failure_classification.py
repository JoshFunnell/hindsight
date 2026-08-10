"""Tests for consolidation LLM-failure classification.

``consolidation_failed_at`` is a PERMANENT exclusion: a stamped row is never
selected again. So the only failure the stamp may follow is one the memories
themselves caused. ``classify_llm_failure`` is what separates that case from a
provider that is merely refusing traffic right now, and it is a pure function
precisely so this distinction can be tested without a database or an LLM.

The cases that matter are the two directions of misclassification:

  * calling a provider outage ``content`` dead-letters healthy rows -- the bug
    upstream #2973 reports, and the reason a typed error with an EMPTY message
    is tested here (the cleanest provider errors carry the least text, so a
    message-only classifier fails exactly where it should be most confident);
  * calling a content failure ``transient`` leaves genuinely unprocessable rows
    retrying forever, which is why an unrecognised error must stay ``content``.
"""

from __future__ import annotations

import unittest

from hindsight_api.engine.consolidation.consolidator import classify_llm_failure


class _RateLimitError(Exception):
    """A typed provider error whose str() is empty, as SDKs commonly raise."""


class _AuthenticationError(Exception):
    pass


class _WeirdProviderError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ClassifyByExceptionTypeTests(unittest.TestCase):
    def test_typed_rate_limit_with_empty_message_is_transient(self):
        """The regression that motivates type-before-text ordering.

        A message-only classifier sees "" here and returns content, which
        bisects and then permanently stamps rows for the single most
        unambiguous transient signal a provider can send.
        """
        self.assertEqual(classify_llm_failure(_RateLimitError()), "transient")

    def test_typed_auth_error_is_auth_not_transient(self):
        self.assertEqual(classify_llm_failure(_AuthenticationError()), "auth")

    def test_status_code_attribute_is_read(self):
        self.assertEqual(classify_llm_failure(_WeirdProviderError("upstream said no", 503)), "transient")
        self.assertEqual(classify_llm_failure(_WeirdProviderError("upstream said no", 403)), "auth")


class ClassifyByMessageTests(unittest.TestCase):
    def test_rate_limit_phrasings_are_all_transient(self):
        for message in (
            "Error code: 429 - too many requests",
            "rate_limit_exceeded",
            "ratelimited, retry later",
            "Resource exhausted",
            "usage limit reached for this window",
            "service unavailable",
            "connection reset by peer",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_llm_failure(RuntimeError(message)), "transient")

    def test_auth_phrasings_are_auth(self):
        for message in ("authentication failed", "invalid api key", "Error code: 401"):
            with self.subTest(message=message):
                self.assertEqual(classify_llm_failure(RuntimeError(message)), "auth")

    def test_status_codes_need_word_boundaries(self):
        """A bare substring test fires on any number that CONTAINS the code.

        ``"429" in "14290"`` is true, so an untrimmed classifier would call a
        content-shaped error transient and retry an unprocessable batch forever.
        """
        self.assertEqual(
            classify_llm_failure(RuntimeError("model produced 14290 tokens of malformed JSON")),
            "content",
        )
        self.assertEqual(classify_llm_failure(RuntimeError("offset 5031 invalid")), "content")
        self.assertEqual(classify_llm_failure(RuntimeError("Error code: 429")), "transient")

    def test_unrecognised_failure_stays_content(self):
        """Unknown means keep the pre-existing bisect-then-stamp behaviour."""
        self.assertEqual(classify_llm_failure(ValueError("could not parse operation at index 3")), "content")

    def test_no_exception_is_content(self):
        self.assertEqual(classify_llm_failure(None), "content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
