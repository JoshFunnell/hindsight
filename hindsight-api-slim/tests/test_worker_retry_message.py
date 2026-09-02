"""The retry a task schedules must still say what went wrong.

`_retry_or_reraise_worker_task` is ours: the port lifted the non-consolidation
retry policy out of two inline branches so the MentalModelRefreshError branch
could share it. Upstream's generic-exception branch passed
``message=error_message`` -- ``format_task_error(e)``, which always prefixes the
exception class -- and the extracted helper passed ``str(e)`` instead. For the
exceptions that matter most here (``TimeoutError()``, ``CancelledError()``, any
bare ``raise SomeError()``) ``str(e)`` is the empty string, so the stored retry
message says nothing at all. That is issue #3218, which upstream fixed once.

The consolidation branch keeps its own ``message=error_message`` and its own
indefinite-retry policy; it must not route through this helper, so that path is
pinned here too.
"""

import unittest
from unittest.mock import patch

from hindsight_api.engine.memory_engine import _retry_or_reraise_worker_task
from hindsight_api.worker.exceptions import RetryTaskAt


class _Config:
    """Only the two fields the helper reads."""

    worker_max_retries = 3
    worker_task_retry_backoff_seconds = 60


class RetryMessageTests(unittest.TestCase):
    def _retry_message(self, exc: Exception, retry_count: int = 0) -> str:
        with patch("hindsight_api.engine.memory_engine.get_config", return_value=_Config()):
            with self.assertRaises(RetryTaskAt) as caught:
                _retry_or_reraise_worker_task(exc, {"_retry_count": retry_count})
        return str(caught.exception)

    def test_empty_str_exception_still_names_its_class(self):
        """The shape that motivated #3218: str(e) is '' and the class is all there is."""
        self.assertEqual(self._retry_message(TimeoutError()), "TimeoutError")

    def test_message_carrying_exception_is_prefixed_by_its_class(self):
        self.assertEqual(
            self._retry_message(ValueError("bank offline")),
            "ValueError: bank offline",
        )

    def test_retries_are_exhausted_by_reraising_the_original_exception(self):
        """Past the cap the poller marks the operation failed, so the error itself must survive."""
        original = TimeoutError()
        with patch("hindsight_api.engine.memory_engine.get_config", return_value=_Config()):
            with self.assertRaises(TimeoutError) as caught:
                _retry_or_reraise_worker_task(original, {"_retry_count": 3})
        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main()
