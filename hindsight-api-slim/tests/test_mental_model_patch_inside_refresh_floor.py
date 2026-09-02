"""An explicit PATCH inside the min-refresh floor, and the parked refresh behind it.

The floor (#3480) parks an AUTOMATIC refresh rather than skipping it: the operation
stays queued with ``next_retry_at`` at the end of the window, and every trigger that
arrives meanwhile folds into that one. So a document can be edited by hand while a
refresh for it is parked, and the two must not fight:

1. the PATCH lands immediately -- the floor governs refreshes, not edits;
2. the PATCH does not advance ``last_memory_seen_at``, the data watermark staleness
   keys off. It only moves ``last_refreshed_at``, the wall clock. Writing the
   watermark here would tell the next refresh it had already read memories it has
   never seen, and those facts would never reach the document;
3. when the parked refresh finally runs, it edits the PATCHed text -- the baseline is
   read from the row at refresh time, not captured when the refresh was queued.

Property 2 is the one that fails silently: rows still update, content still changes,
and the loss shows up as facts quietly missing from a later refresh. The assertion is
on ``last_memory_seen_at`` for that reason, and it is what makes this test fail if the
PATCH path ever starts stamping the watermark.

Local test: the fast path, the compaction gate and the identifier gate are ours, and
all three sit on the write path a PATCH shares with a refresh.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from hindsight_api.engine.memory_engine import _REFRESH_AUTOMATIC_KEY, MemoryEngine
from hindsight_api.worker.exceptions import DeferOperation

INTERVAL = 1800


async def _make_bank(memory: MemoryEngine, request_context) -> str:
    bank_id = f"mmpatch-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    return bank_id


async def _insert_mm(conn, bank_id: str, *, refreshed_seconds_ago: int, seen_seconds_ago: int) -> str:
    mm_id = f"mm-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO mental_models
          (id, bank_id, subtype, name, source_query, content, tags, trigger,
           last_refreshed_at, last_memory_seen_at)
        VALUES ($1, $2, 'pinned', 'patched model', 'what changed', $3, $4, $5::jsonb, $6, $7)
        """,
        mm_id,
        bank_id,
        "# Routing\n\nThe original body.\n",
        [],
        json.dumps({"refresh_after_consolidation": True, "min_refresh_interval_seconds": INTERVAL}),
        now - timedelta(seconds=refreshed_seconds_ago),
        now - timedelta(seconds=seen_seconds_ago),
    )
    return mm_id


async def _row(memory: MemoryEngine, bank_id: str, mm_id: str) -> dict:
    async with memory._pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT content, structured_content, last_refreshed_at, last_memory_seen_at "
            "FROM mental_models WHERE bank_id = $1 AND id = $2",
            bank_id,
            mm_id,
        )


@pytest.mark.asyncio
async def test_patch_inside_the_floor_lands_and_leaves_the_watermark_alone(
    memory: MemoryEngine, request_context, monkeypatch
):
    bank = await _make_bank(memory, request_context)
    async with memory._pool.acquire() as conn:
        mm_id = await _insert_mm(conn, bank, refreshed_seconds_ago=60, seen_seconds_ago=3600)

    before = await _row(memory, bank, mm_id)

    # (1) The edit lands: the floor governs automatic refreshes, not explicit writes.
    await memory.update_mental_model(
        bank_id=bank,
        mental_model_id=mm_id,
        content="# Routing\n\nThe operator's own text.\n",
        request_context=request_context,
    )

    after = await _row(memory, bank, mm_id)
    assert "The operator's own text." in after["content"]

    # A markdown-only write derives its structure, so the two columns cannot diverge.
    stored = after["structured_content"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored is not None
    assert stored["version"] == 2

    # (2) The data watermark is untouched. This is the assertion that fails if the
    # PATCH path starts stamping it: a wall-clock bump is correct here, a watermark
    # bump means the next refresh skips every memory written since seen_seconds_ago.
    assert after["last_memory_seen_at"] == before["last_memory_seen_at"]
    assert after["last_refreshed_at"] > before["last_refreshed_at"]


@pytest.mark.asyncio
async def test_the_parked_refresh_does_not_overwrite_the_patched_text(
    memory: MemoryEngine, request_context, monkeypatch
):
    bank = await _make_bank(memory, request_context)
    async with memory._pool.acquire() as conn:
        mm_id = await _insert_mm(conn, bank, refreshed_seconds_ago=60, seen_seconds_ago=3600)

    # An automatic trigger inside the window is parked, not run.
    with pytest.raises(DeferOperation) as excinfo:
        await memory._handle_refresh_mental_model(
            {"bank_id": bank, "mental_model_id": mm_id, "operation_id": str(uuid.uuid4()), _REFRESH_AUTOMATIC_KEY: True}
        )
    assert "min_refresh_interval_seconds" in excinfo.value.reason

    # The operator edits the document while that refresh waits.
    patched = "# Routing\n\nThe operator's own text.\n"
    await memory.update_mental_model(
        bank_id=bank, mental_model_id=mm_id, content=patched, request_context=request_context
    )

    # The park expires; the refresh now runs. Record the baseline it was handed.
    async with memory._pool.acquire() as conn:
        await conn.execute(
            "UPDATE mental_models SET last_refreshed_at = $1 WHERE bank_id = $2 AND id = $3",
            datetime.now(UTC) - timedelta(seconds=INTERVAL + 60),
            bank,
            mm_id,
        )

    seen_baseline: list[str] = []

    async def _fake_refresh(bank_id, mental_model_id, **kwargs):
        row = await _row(memory, bank_id, mental_model_id)
        seen_baseline.append(row["content"])
        return {"id": mental_model_id, "content": row["content"], "reflect_response": {}, "source_query": "q"}

    async def _skip_outcome_metadata(operation_id, refreshed_model):
        return None

    monkeypatch.setattr(memory, "refresh_mental_model", _fake_refresh)
    monkeypatch.setattr(memory, "_write_refresh_outcome_metadata", _skip_outcome_metadata)

    await memory._handle_refresh_mental_model(
        {"bank_id": bank, "mental_model_id": mm_id, "operation_id": str(uuid.uuid4()), _REFRESH_AUTOMATIC_KEY: True}
    )

    # (3) The refresh read the row at refresh time, so it edits the PATCHed text
    # rather than a baseline captured when it was first queued.
    assert seen_baseline, "the parked refresh never ran after the window expired"
    assert "The operator's own text." in seen_baseline[0]
    assert "The original body." not in seen_baseline[0]

    final = await _row(memory, bank, mm_id)
    assert "The operator's own text." in final["content"]
