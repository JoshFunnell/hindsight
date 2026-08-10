"""Multi-bank recall: pure merge helpers + MemoryEngine.recall_multi_async orchestrator.

Covers the 2026-08-10 multi-bank plan:
- score-merge order (incl. ties)
- interleave order
- auto-fallback to interleave when CE is not comparable
- token cut on the merged list
- bank_id attribution
- partial-failure metadata
- single-bank equivalence with recall_async
- empty bank member
- ContextVar isolation across parallel sub-calls (via @_bind_bank_id on each task)

No DB / embeddings required — sub-calls are mocked; pure helpers are unit-tested directly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine, _bind_bank_id, get_current_bank_id
from hindsight_api.engine.multi_bank_recall import (
    META_BANKS,
    META_DEDUP,
    META_DEDUP_V1,
    META_MERGE_APPLIED,
    META_MERGE_FALLBACK_REASON,
    META_MERGE_REQUESTED,
    META_MULTI_BANK,
    build_multi_bank_metadata,
    cross_encoder_eligible,
    cut_to_token_budget,
    interleave_merge,
    score_merge,
    stamp_bank_id,
)
from hindsight_api.engine.response_models import MemoryFact, RecallResult, RecallScores
from hindsight_api.models import RequestContext

RC = RequestContext(tenant_id="default")


def _fact(
    id: str,
    text: str,
    *,
    reranker: float | None = None,
    final: float | None = None,
    bank_id: str | None = None,
) -> MemoryFact:
    scores = None
    if reranker is not None or final is not None:
        scores = RecallScores(
            final=final if final is not None else (reranker or 0.0),
            reranker=reranker,
        )
    return MemoryFact(id=id, text=text, fact_type="world", scores=scores, bank_id=bank_id)


# --- pure helpers -------------------------------------------------------------


def test_score_merge_orders_by_reranker_descending():
    bank_a = [
        _fact("a1", "low from A", reranker=0.2),
        _fact("a2", "high from A", reranker=0.9),
    ]
    bank_b = [
        _fact("b1", "mid from B", reranker=0.5),
        _fact("b2", "higher from B", reranker=0.8),
    ]
    merged = score_merge([("bank-a", bank_a), ("bank-b", bank_b)])
    assert [f.id for f in merged] == ["a2", "b2", "b1", "a1"]
    assert [f.bank_id for f in merged] == ["bank-a", "bank-b", "bank-b", "bank-a"]


def test_score_merge_ties_break_by_bank_order_then_rank():
    """Equal reranker scores: earlier bank, then earlier within-bank rank wins."""
    bank_a = [
        _fact("a1", "A first", reranker=0.7),
        _fact("a2", "A second", reranker=0.7),
    ]
    bank_b = [
        _fact("b1", "B first", reranker=0.7),
    ]
    merged = score_merge([("bank-a", bank_a), ("bank-b", bank_b)])
    assert [f.id for f in merged] == ["a1", "a2", "b1"]


def test_score_merge_missing_reranker_sorts_last():
    bank_a = [_fact("a1", "no ce", reranker=None, final=0.9)]
    bank_b = [_fact("b1", "has ce", reranker=0.1)]
    merged = score_merge([("bank-a", bank_a), ("bank-b", bank_b)])
    assert [f.id for f in merged] == ["b1", "a1"]


def test_interleave_merge_round_robin_by_rank():
    bank_a = [
        _fact("a1", "A1"),
        _fact("a2", "A2"),
        _fact("a3", "A3"),
    ]
    bank_b = [
        _fact("b1", "B1"),
        _fact("b2", "B2"),
    ]
    merged = interleave_merge([("bank-a", bank_a), ("bank-b", bank_b)])
    assert [f.id for f in merged] == ["a1", "b1", "a2", "b2", "a3"]
    assert all(f.bank_id in ("bank-a", "bank-b") for f in merged)


def test_interleave_merge_empty_bank_member():
    bank_a = [_fact("a1", "only A")]
    bank_b: list[MemoryFact] = []
    merged = interleave_merge([("bank-a", bank_a), ("bank-b", bank_b)])
    assert [f.id for f in merged] == ["a1"]
    assert merged[0].bank_id == "bank-a"


def test_stamp_bank_id_does_not_mutate_original():
    original = _fact("x", "text")
    stamped = stamp_bank_id(original, "bank-z")
    assert stamped.bank_id == "bank-z"
    assert original.bank_id is None


def test_cut_to_token_budget_stops_before_exceeding():
    from hindsight_api.engine.memory_engine import count_tokens

    f1 = _fact("1", "alpha")
    f2 = _fact("2", "beta gamma")
    f3 = _fact("3", "delta")
    budget = count_tokens(f1.text) + count_tokens(f2.text)
    cut = cut_to_token_budget([f1, f2, f3], budget)
    assert [f.id for f in cut] == ["1", "2"]
    assert sum(count_tokens(f.text) for f in cut) <= budget

    # Budget too small for the first fact → empty (stop-before-exceeding).
    under_first = max(0, count_tokens(f1.text) - 1)
    assert cut_to_token_budget([f1, f2], under_first) == []


def test_cut_to_token_budget_zero_is_empty():
    assert cut_to_token_budget([_fact("1", "hello")], 0) == []


def test_cross_encoder_eligible_requires_cross_encoder_request():
    ok, reason = cross_encoder_eligible(
        requested_reranking="rrf",
        bank_enable_reranking=[True, True],
    )
    assert ok is False
    assert reason is not None
    assert "rrf" in reason


def test_cross_encoder_eligible_rejects_disabled_reranking_bank():
    ok, reason = cross_encoder_eligible(
        requested_reranking="cross_encoder",
        bank_enable_reranking=[True, False],
    )
    assert ok is False
    assert reason is not None
    assert "enable_reranking" in reason


def test_cross_encoder_eligible_all_ce():
    ok, reason = cross_encoder_eligible(
        requested_reranking="cross_encoder",
        bank_enable_reranking=[True, True],
    )
    assert ok is True
    assert reason is None


def test_build_multi_bank_metadata_shape():
    meta = build_multi_bank_metadata(
        merge_requested="score",
        merge_applied="interleave",
        merge_fallback_reason="test reason",
        bank_statuses={"a": {"status": "ok", "count": 1}},
    )
    block = meta[META_MULTI_BANK]
    assert block[META_MERGE_REQUESTED] == "score"
    assert block[META_MERGE_APPLIED] == "interleave"
    assert block[META_MERGE_FALLBACK_REASON] == "test reason"
    assert block[META_BANKS]["a"]["status"] == "ok"
    assert block[META_DEDUP] == META_DEDUP_V1


# --- orchestrator (mocked recall_async) ---------------------------------------


def _harness(
    *,
    bank_results: dict[str, list[MemoryFact] | Exception],
    enable_reranking: dict[str, bool] | None = None,
) -> MemoryEngine:
    """Minimal MemoryEngine shell: real recall_multi_async, mocked sub-calls + config."""
    engine = object.__new__(MemoryEngine)

    async def fake_recall(bank_id: str, query: str, **kwargs) -> RecallResult:
        outcome = bank_results[bank_id]
        if isinstance(outcome, Exception):
            raise outcome
        return RecallResult(results=list(outcome))

    engine.recall_async = fake_recall  # type: ignore[method-assign]

    enable_reranking = enable_reranking or {bid: True for bid in bank_results}

    async def fake_config(bank_id: str, request_context):
        return {"enable_reranking": enable_reranking.get(bank_id, True)}

    engine._config_resolver = SimpleNamespace(get_bank_config=fake_config)  # type: ignore[attr-defined]
    return engine


@pytest.mark.asyncio
async def test_orchestrator_score_merge_order():
    engine = _harness(
        bank_results={
            "bank-a": [
                _fact("a1", "low", reranker=0.2),
                _fact("a2", "high", reranker=0.95),
            ],
            "bank-b": [
                _fact("b1", "mid", reranker=0.6),
            ],
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        merge="score",
        request_context=RC,
        max_tokens=10_000,
    )
    assert [f.id for f in result.results] == ["a2", "b1", "a1"]
    assert result.metadata[META_MULTI_BANK][META_MERGE_APPLIED] == "score"
    assert result.metadata[META_MULTI_BANK][META_MERGE_FALLBACK_REASON] is None


@pytest.mark.asyncio
async def test_orchestrator_interleave_order():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "A1"), _fact("a2", "A2")],
            "bank-b": [_fact("b1", "B1"), _fact("b2", "B2")],
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        merge="interleave",
        request_context=RC,
        max_tokens=10_000,
    )
    assert [f.id for f in result.results] == ["a1", "b1", "a2", "b2"]
    assert result.metadata[META_MULTI_BANK][META_MERGE_APPLIED] == "interleave"


@pytest.mark.asyncio
async def test_orchestrator_auto_fallback_when_bank_disables_reranking():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "A1", reranker=0.9), _fact("a2", "A2", reranker=0.1)],
            "bank-b": [_fact("b1", "B1", reranker=None, final=0.5)],
        },
        enable_reranking={"bank-a": True, "bank-b": False},
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        merge="score",
        request_context=RC,
        max_tokens=10_000,
    )
    mb = result.metadata[META_MULTI_BANK]
    assert mb[META_MERGE_REQUESTED] == "score"
    assert mb[META_MERGE_APPLIED] == "interleave"
    assert mb[META_MERGE_FALLBACK_REASON] is not None
    # Interleave order, not score order (score would put a1 first and maybe a2 before b1).
    assert [f.id for f in result.results] == ["a1", "b1", "a2"]


@pytest.mark.asyncio
async def test_orchestrator_auto_fallback_when_requested_rrf():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "A1")],
            "bank-b": [_fact("b1", "B1")],
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        merge="score",
        reranking="rrf",
        request_context=RC,
        max_tokens=10_000,
    )
    mb = result.metadata[META_MULTI_BANK]
    assert mb[META_MERGE_APPLIED] == "interleave"
    assert "rrf" in (mb[META_MERGE_FALLBACK_REASON] or "")


@pytest.mark.asyncio
async def test_orchestrator_token_cut_on_merged_list():
    from hindsight_api.engine.memory_engine import count_tokens

    # Distinct short texts so ordering is stable and budget math is simple.
    a1 = _fact("a1", "alpha alpha", reranker=0.9)
    b1 = _fact("b1", "beta beta beta", reranker=0.8)
    a2 = _fact("a2", "gamma", reranker=0.7)
    engine = _harness(bank_results={"bank-a": [a1, a2], "bank-b": [b1]})

    # Fit a1 + b1 but not a2 after score-merge order a1, b1, a2.
    budget = count_tokens(a1.text) + count_tokens(b1.text)
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        merge="score",
        request_context=RC,
        max_tokens=budget,
    )
    assert [f.id for f in result.results] == ["a1", "b1"]
    assert sum(count_tokens(f.text) for f in result.results) <= budget


@pytest.mark.asyncio
async def test_orchestrator_bank_id_attribution():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "from A", reranker=0.5)],
            "bank-b": [_fact("b1", "from B", reranker=0.6)],
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        request_context=RC,
        max_tokens=10_000,
    )
    by_id = {f.id: f.bank_id for f in result.results}
    assert by_id == {"b1": "bank-b", "a1": "bank-a"}


@pytest.mark.asyncio
async def test_orchestrator_partial_failure_metadata():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "ok", reranker=0.5)],
            "bank-b": RuntimeError("boom"),
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-b"],
        "query",
        request_context=RC,
        max_tokens=10_000,
    )
    banks = result.metadata[META_MULTI_BANK][META_BANKS]
    assert banks["bank-a"]["status"] == "ok"
    assert banks["bank-a"]["count"] == 1
    assert banks["bank-b"]["status"] == "error"
    assert "boom" in banks["bank-b"]["error"]
    assert [f.id for f in result.results] == ["a1"]


@pytest.mark.asyncio
async def test_orchestrator_empty_bank_member():
    engine = _harness(
        bank_results={
            "bank-a": [_fact("a1", "only", reranker=0.5)],
            "bank-empty": [],
        }
    )
    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-a", "bank-empty"],
        "query",
        request_context=RC,
        max_tokens=10_000,
    )
    banks = result.metadata[META_MULTI_BANK][META_BANKS]
    assert banks["bank-empty"] == {"status": "ok", "count": 0}
    assert [f.id for f in result.results] == ["a1"]
    assert result.results[0].bank_id == "bank-a"


@pytest.mark.asyncio
async def test_orchestrator_single_bank_equivalence():
    """Single-bank multi-recall matches recall_async content order (plus bank_id stamp)."""
    facts = [
        _fact("s1", "first", reranker=0.9),
        _fact("s2", "second", reranker=0.5),
    ]
    engine = _harness(bank_results={"only": facts})

    single = await engine.recall_async("only", "query", request_context=RC, max_tokens=10_000)
    multi = await MemoryEngine.recall_multi_async(
        engine,
        ["only"],
        "query",
        merge="score",
        request_context=RC,
        max_tokens=10_000,
    )
    assert [f.id for f in multi.results] == [f.id for f in single.results]
    assert [f.text for f in multi.results] == [f.text for f in single.results]
    assert all(f.bank_id == "only" for f in multi.results)


@pytest.mark.asyncio
async def test_orchestrator_empty_bank_ids_list():
    engine = _harness(bank_results={})
    result = await MemoryEngine.recall_multi_async(
        engine,
        [],
        "query",
        request_context=RC,
    )
    assert result.results == []
    assert result.metadata[META_MULTI_BANK][META_BANKS] == {}


@pytest.mark.asyncio
async def test_orchestrator_contextvar_isolation_across_parallel_subcalls():
    """Each parallel sub-call sees its own bank_id via @_bind_bank_id (task context)."""
    engine = object.__new__(MemoryEngine)
    observed: dict[str, str | None] = {}
    barrier = asyncio.Barrier(2)

    @_bind_bank_id()
    async def bound_recall(bank_id: str, query: str, **kwargs) -> RecallResult:
        # Wait so both tasks are concurrent before reading ContextVar.
        await barrier.wait()
        observed[bank_id] = get_current_bank_id()
        return RecallResult(results=[_fact(f"{bank_id}-1", bank_id, reranker=0.5)])

    engine.recall_async = bound_recall  # type: ignore[method-assign]
    engine._config_resolver = SimpleNamespace(  # type: ignore[attr-defined]
        get_bank_config=AsyncMock(return_value={"enable_reranking": True})
    )

    result = await MemoryEngine.recall_multi_async(
        engine,
        ["bank-x", "bank-y"],
        "query",
        request_context=RC,
        max_tokens=10_000,
    )
    assert observed["bank-x"] == "bank-x"
    assert observed["bank-y"] == "bank-y"
    assert {f.bank_id for f in result.results} == {"bank-x", "bank-y"}


@pytest.mark.asyncio
async def test_orchestrator_invalid_merge_raises():
    engine = _harness(bank_results={"a": []})
    with pytest.raises(ValueError, match="merge must be"):
        await MemoryEngine.recall_multi_async(
            engine,
            ["a"],
            "query",
            merge="nope",  # type: ignore[arg-type]
            request_context=RC,
        )


@pytest.mark.asyncio
async def test_orchestrator_passes_full_max_tokens_to_each_subcall():
    """Each sub-call receives the caller's full max_tokens (cut happens after merge)."""
    seen_max: dict[str, int] = {}

    engine = object.__new__(MemoryEngine)

    async def tracking_recall(bank_id: str, query: str, **kwargs) -> RecallResult:
        seen_max[bank_id] = kwargs.get("max_tokens", -1)
        return RecallResult(results=[_fact(f"{bank_id}-1", "x" * 50, reranker=0.5)])

    engine.recall_async = tracking_recall  # type: ignore[method-assign]
    engine._config_resolver = SimpleNamespace(  # type: ignore[attr-defined]
        get_bank_config=AsyncMock(return_value={"enable_reranking": True})
    )

    await MemoryEngine.recall_multi_async(
        engine,
        ["b1", "b2"],
        "query",
        request_context=RC,
        max_tokens=1234,
    )
    assert seen_max == {"b1": 1234, "b2": 1234}
