"""execute_task must handle MentalModelRefreshError without a stderr traceback.

refresh_mental_model raises MentalModelRefreshError from _preserve_and_fail
(#3112/#3182): content and watermark stay untouched, retry is intended.
Before this handler the exception fell through execute_task's generic
``except Exception``, which calls ``traceback.print_exc()`` and is what
the soak watcher's unhandled-in-overlay pattern flags.

These tests isolate execute_task's exception classification (same shape as
test_worker_retry_knobs / test_integrity_violation_not_retried) by stubbing
_handle_refresh_mental_model. No DB: omit operation_id so the cancel check
is skipped.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from hindsight_api.config import get_config
from hindsight_api.engine.memory_engine import MemoryEngine, MentalModelRefreshError
from hindsight_api.worker.exceptions import RetryTaskAt

MM_ID = "mm-test-delta-ops"
BANK_ID = "bank-test-mmrefresh"
REFRESH_ERROR = MentalModelRefreshError(
    f"Refresh failed for mental_model_id={MM_ID}: delta operations did not reach "
    "the document, and the reflect candidate covers only memories newer than the "
    "last refresh, so writing it would drop the rest of the document. Previous "
    "content preserved in DB; reflect_response.refresh_skipped == "
    "'delta_ops_all_skipped' for audit."
)


def _engine() -> MemoryEngine:
    engine = object.__new__(MemoryEngine)
    engine._audit_logger = None
    return engine


def _task(*, retry_count: int) -> dict:
    return {
        "type": "refresh_mental_model",
        "bank_id": BANK_ID,
        "mental_model_id": MM_ID,
        "_retry_count": retry_count,
    }


@pytest.mark.asyncio
async def test_refresh_error_retries_without_traceback(caplog, capsys):
    """_retry_count=0 -> RetryTaskAt; no Traceback on stderr; WARNING names the skip."""
    engine = _engine()
    with (
        caplog.at_level(logging.WARNING, logger="hindsight_api.engine.memory_engine"),
        patch.object(engine, "_handle_refresh_mental_model", side_effect=REFRESH_ERROR),
        pytest.raises(RetryTaskAt),
    ):
        await engine.execute_task(_task(retry_count=0))

    err = capsys.readouterr().err
    assert "Traceback" not in err
    warning_text = "\n".join(r.message for r in caplog.records if r.levelno == logging.WARNING)
    assert MM_ID in warning_text
    assert "delta_ops_all_skipped" in warning_text
    assert BANK_ID in warning_text
    assert "refresh_mental_model" in warning_text


@pytest.mark.asyncio
async def test_refresh_error_propagates_at_retry_cap(capsys):
    """At _retry_count == worker_max_retries the MentalModelRefreshError escapes."""
    engine = _engine()
    cap = get_config().worker_max_retries
    with patch.object(engine, "_handle_refresh_mental_model", side_effect=REFRESH_ERROR):
        with pytest.raises(MentalModelRefreshError, match="delta_ops_all_skipped") as excinfo:
            await engine.execute_task(_task(retry_count=cap))
    assert not isinstance(excinfo.value, RetryTaskAt)
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_generic_runtime_error_on_refresh_still_prints_traceback(capsys):
    """A generic RuntimeError on the same path still print_exc -- silence is not widened."""
    engine = _engine()
    with patch.object(
        engine,
        "_handle_refresh_mental_model",
        side_effect=RuntimeError("unexpected refresh boom"),
    ):
        with pytest.raises(RetryTaskAt):
            await engine.execute_task(_task(retry_count=0))
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "unexpected refresh boom" in err
    assert "RuntimeError" in err
