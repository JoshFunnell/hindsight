"""Tests for the end-of-run dead-letter warning.

The warning exists because ``consolidation_failed_at`` is silent: a stamped memory
leaves the pending set and no later run touches it, so a provider outage can park a
whole batch with nothing in the logs to prompt the operator to call
``/consolidation/recover``.

The decision logic is a pure function, so these tests need no database and no LLM. They
pin three things: that it fires when a run really did park a large share of its work,
that it stays quiet on the shapes that would otherwise make it noise (which is what
decides whether operators keep reading it), and that the message actually tells the
operator what to run.
"""

import pytest

from hindsight_api.config import ENV_CONSOLIDATION_DEAD_LETTER_WARN_FRACTION
from hindsight_api.engine.consolidation.consolidator import (
    DEAD_LETTER_WARN_MIN_MEMORIES,
    dead_letter_warning,
)

BANK = "bank-under-test"
HALF = 0.5


class TestFires:
    """Shapes that must produce a warning."""

    def test_whole_batch_parked(self):
        # The case the warning exists for: a quota window takes out an entire run.
        msg = dead_letter_warning(BANK, processed=0, failed=40, warn_fraction=HALF)
        assert msg is not None
        assert "40/40" in msg
        assert "100%" in msg

    def test_majority_parked(self):
        msg = dead_letter_warning(BANK, processed=10, failed=30, warn_fraction=HALF)
        assert msg is not None
        assert "30/40" in msg
        assert "75%" in msg

    def test_exactly_at_threshold_fires(self):
        # Boundary: fraction == threshold must fire, not fall through the >= edge.
        msg = dead_letter_warning(BANK, processed=20, failed=20, warn_fraction=HALF)
        assert msg is not None
        assert "50%" in msg

    def test_exactly_at_the_floor_fires(self):
        # The floor is inclusive: failed == DEAD_LETTER_WARN_MIN_MEMORIES must warn.
        # Without this, tightening `failed < FLOOR` to `failed <= FLOOR` (requiring 4)
        # would pass the whole suite -- the quiet-side tests only cover 1 and 2.
        msg = dead_letter_warning(BANK, processed=0, failed=DEAD_LETTER_WARN_MIN_MEMORIES, warn_fraction=HALF)
        assert msg is not None
        assert "3/3" in msg

    def test_low_threshold_catches_a_small_share(self):
        # An operator who wants to hear about any material loss can lower the bar; the
        # absolute floor still applies, so failed=4 is above DEAD_LETTER_WARN_MIN_MEMORIES.
        msg = dead_letter_warning(BANK, processed=96, failed=4, warn_fraction=0.01)
        assert msg is not None
        assert "4/100" in msg


class TestStaysQuiet:
    """Shapes that must NOT warn.

    These matter more than the firing cases. A guard that cries wolf gets ignored, and
    an ignored guard is indistinguishable from a missing one except that it also
    supplies false confidence.
    """

    def test_clean_run(self):
        assert dead_letter_warning(BANK, processed=100, failed=0, warn_fraction=HALF) is None

    def test_empty_run(self):
        # No work attempted at all: 0/0 must not divide, and must not warn.
        assert dead_letter_warning(BANK, processed=0, failed=0, warn_fraction=HALF) is None

    def test_below_the_fraction(self):
        assert dead_letter_warning(BANK, processed=90, failed=10, warn_fraction=HALF) is None

    def test_the_floor_is_three(self):
        # Pinned as a literal so the cases below cannot drift with the constant. If this
        # value is changed deliberately, this test is the place that records it.
        assert DEAD_LETTER_WARN_MIN_MEMORIES == 3

    @pytest.mark.parametrize("failed", [1, 2])
    def test_below_the_absolute_floor_even_at_100_percent(self, failed):
        # A bank consolidating one or two memories at a time hits 100% constantly, so
        # the floor is the whole reason this warning stays readable.
        #
        # These inputs are LITERAL on purpose. They were originally
        # `range(1, DEAD_LETTER_WARN_MIN_MEMORIES)`, which derives the test's own inputs
        # from the constant under test: setting the floor to 0 emptied the range, and
        # the test SKIPPED instead of failing. Caught by planting exactly that
        # regression -- a guard whose trigger disappears along with the thing it guards
        # is not a guard.
        assert dead_letter_warning(BANK, processed=0, failed=failed, warn_fraction=HALF) is None

    def test_zero_fraction_disables(self):
        assert dead_letter_warning(BANK, processed=0, failed=1000, warn_fraction=0.0) is None

    def test_negative_fraction_disables(self):
        assert dead_letter_warning(BANK, processed=0, failed=1000, warn_fraction=-1.0) is None

    def test_negative_failed_count_is_not_a_signal(self):
        # Defensive: a counter regression must not be reported as a dead-letter event.
        assert dead_letter_warning(BANK, processed=10, failed=-5, warn_fraction=HALF) is None

    def test_negative_processed_count_is_not_a_signal(self):
        # The asymmetric case: guarding only `failed` leaves processed=-5, failed=10
        # summing to attempted=5, so the fraction reads 2.0 and the message would claim
        # "10/5 (200%)". A broken counter must produce silence, not a nonsense number.
        assert dead_letter_warning(BANK, processed=-5, failed=10, warn_fraction=HALF) is None
        assert dead_letter_warning(BANK, processed=-1, failed=3, warn_fraction=HALF) is None


class TestMessageIsActionable:
    """The message has to be usable on its own, in a log, by someone paged at 3am."""

    def test_names_the_recovery_endpoint_and_bank(self):
        msg = dead_letter_warning(BANK, processed=0, failed=25, warn_fraction=HALF)
        assert msg is not None
        assert f"/v1/default/banks/{BANK}/consolidation/recover" in msg
        assert f"hindsight bank consolidation-recover {BANK}" in msg

    def test_says_the_rows_will_not_retry(self):
        # Without this the reader cannot tell whether the situation is self-healing.
        msg = dead_letter_warning(BANK, processed=0, failed=25, warn_fraction=HALF)
        assert msg is not None
        assert "consolidation_failed_at" in msg
        assert "NOT be retried" in msg

    def test_says_how_to_silence_it(self):
        msg = dead_letter_warning(BANK, processed=0, failed=25, warn_fraction=HALF)
        assert msg is not None
        assert ENV_CONSOLIDATION_DEAD_LETTER_WARN_FRACTION in msg


class TestClassifiesNothing:
    """The reason #3309 was rejected: no provider-shaped surface may creep back in.

    The rejected approach read provider exception types, then status codes, then message
    substrings, so it needed updating as every provider's wire vocabulary changed. This
    replacement must depend on counters only -- a property worth pinning, because the
    natural "improvement" to this function is to start explaining WHY the failures
    happened.
    """

    def test_signature_takes_only_counters(self):
        import inspect

        params = set(inspect.signature(dead_letter_warning).parameters)
        assert params == {"bank_id", "processed", "failed", "warn_fraction"}

    def test_executable_code_mentions_no_provider_error_vocabulary(self):
        import ast
        import inspect
        import textwrap

        from hindsight_api.engine.consolidation import consolidator

        fn = ast.parse(textwrap.dedent(inspect.getsource(consolidator.dead_letter_warning))).body[0]
        # Scan the BODY only. The docstring explains that no classification happens, so
        # including prose would fail on the very sentence documenting the property under
        # test -- and would then be "fixed" by deleting the explanation, which is worse
        # than the test. ast.unparse also drops comments, for the same reason.
        first = fn.body[0] if fn.body else None
        has_docstring = (
            isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
        )
        body = fn.body[1:] if has_docstring else fn.body
        assert body, "function body is empty -- this check would pass vacuously"
        code = "\n".join(ast.unparse(node) for node in body).lower()
        for token in ("rate_limit", "ratelimited", "429", "quota_exceeded", "status_code", "exception"):
            assert token not in code, f"dead_letter_warning must not reason about {token!r}"


class TestCallSiteWiring:
    """The pure function is well covered; the glue that calls it is the weak seam.

    A correct decision that is never logged, or is logged from the process-global
    config instead of the bank's, is invisible in exactly the way this warning exists
    to prevent -- so the wiring is pinned rather than assumed.
    """

    def test_job_reads_the_bank_resolved_config_not_the_process_global(self):
        # `_run_consolidation_job` receives `config` already resolved for the bank.
        # Reading `get_config()` here instead would let a per-bank value store, export
        # and import cleanly while never taking effect -- a silent no-op, and the same
        # invisible-failure shape that got the classification approach rejected.
        import ast
        import inspect
        import textwrap

        from hindsight_api.engine.consolidation import consolidator

        src = textwrap.dedent(inspect.getsource(consolidator._run_consolidation_job))
        call = next(
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dead_letter_warning"
        )
        rendered = [ast.unparse(arg) for arg in call.args]
        assert "config.consolidation_dead_letter_warn_fraction" in rendered, rendered
        assert not any("get_config()" in arg for arg in rendered), rendered

    def test_the_decision_is_logged_as_a_warning(self):
        # Pins that a non-None decision actually reaches the log. The function could be
        # perfect and the caller could drop it on the floor.
        import ast
        import inspect
        import textwrap

        from hindsight_api.engine.consolidation import consolidator

        src = textwrap.dedent(inspect.getsource(consolidator._run_consolidation_job))
        guarded_logs = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "warning"
            and "logger.warning(warning)" in ast.unparse(node)
        ]
        assert guarded_logs, "the dead-letter decision is computed but never logged"
