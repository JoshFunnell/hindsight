"""The delta fast path, exercised through a real refresh — the coverage the flag flip removed.

Until S107 the fast path was on by default, so upstream's mental-model tests ran through
it incidentally. Making it opt-in (DESIGN-S107-OVERLAY-OPT-IN.md D1) fixed those tests and
left the feature with no end-to-end test of its own: the S107 refute caught that
"our own tests still pass" would have been true whether or not the fast path existed.

These use the per-model `trigger.delta_fast_path` override rather than the env, so they
pin the feature itself and stay true whatever the deployment default becomes.

tier 0 is the cheapest outcome the fast path has: no new facts in the window, so the
content is preserved without any LLM call at all. Serving it is worth ~13.5k input tokens
against ~150k for the agentic path (measured 2026-08-19), which is the whole reason the
overlay exists.
"""

import uuid

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine


async def _model_with_content(memory: MemoryEngine, request_context, *, bank_id: str, fast_path: bool) -> dict:
    return await memory.create_mental_model(
        bank_id=bank_id,
        name="Team Info",
        source_query="Tell me about the team",
        content="# Team\n\nAlice is the lead.\n",
        trigger={"mode": "delta", "delta_fast_path": fast_path},
        request_context=request_context,
    )


@pytest.mark.asyncio
class TestFastPathServesTierZero:
    async def test_no_new_facts_is_served_on_tier0_without_an_llm_call(
        self, memory: MemoryEngine, request_context, monkeypatch
    ):
        """The bank has no memories at all, so the window is empty by construction."""
        bank_id = f"fastpath-{uuid.uuid4().hex[:8]}"
        await memory.get_bank_profile(bank_id, request_context=request_context)
        mm = await _model_with_content(memory, request_context, bank_id=bank_id, fast_path=True)

        async def _must_not_run(**kwargs):
            raise AssertionError("tier 0 must preserve content without calling reflect")

        monkeypatch.setattr(memory, "reflect_async", _must_not_run)

        refreshed = await memory.refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

        rr = refreshed.get("reflect_response") or {}
        assert rr.get("fast_path") == "tier0", rr.get("fast_path")
        assert rr.get("delta_skipped_reason") == "no_new_facts"
        assert refreshed["content"] == "# Team\n\nAlice is the lead.\n"

    async def test_the_same_refresh_with_the_feature_off_reports_no_tier(
        self, memory: MemoryEngine, request_context, monkeypatch
    ):
        """The attacking half: the tier above must come from the fast path, not from anywhere else.

        Narrowed deliberately, after the first version of this test failed for a reason
        worth writing down: it asserted that the refresh would REACH ``reflect_async`` with
        the feature off, and it did not. With a bank that holds no memories at all,
        ``has_sources`` is false and the agentic path also returns without calling reflect
        -- so "reflect was not called" cannot tell the two paths apart here, and a test
        asserting it would have been measuring the empty bank rather than the feature.

        What DOES tell them apart is the reported tier: ``tier0`` is only reachable through
        the fast path. Owed strengthening: seed the bank with a memory older than the
        model's watermark, which gives sources without new facts in the window, and then
        the reflect-reached assertion becomes meaningful for both halves.
        """
        bank_id = f"fastpath-off-{uuid.uuid4().hex[:8]}"
        await memory.get_bank_profile(bank_id, request_context=request_context)
        mm = await _model_with_content(memory, request_context, bank_id=bank_id, fast_path=False)

        refreshed = await memory.refresh_mental_model(
            bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context
        )

        rr = refreshed.get("reflect_response") or {}
        assert rr.get("fast_path") != "tier0", rr.get("fast_path")
