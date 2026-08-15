"""HTTP tests for multi-bank recall (POST /v1/default/memories/recall).

Mocks the engine; no DB required. Mirrors patterns used by other lightweight HTTP tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from hindsight_api.api import create_app
from hindsight_api.engine.response_models import (
    ChunkInfo,
    EntityState,
    MemoryFact,
    RecallResult,
    RecallScores,
)
from hindsight_api.extensions import AuthenticationError, OperationValidationError


def _fact(id: str, text: str, *, bank_id: str | None = None, reranker: float = 0.5) -> MemoryFact:
    return MemoryFact(
        id=id,
        text=text,
        fact_type="world",
        bank_id=bank_id,
        scores=RecallScores(final=reranker, reranker=reranker),
    )


@pytest.fixture
def mock_memory():
    memory = MagicMock()
    memory._operation_validator = None
    memory.audit_logger = None  # disable @audited path (MagicMock is await-hostile)
    memory._authenticate_tenant = AsyncMock()
    memory.recall_multi_async = AsyncMock(
        return_value=RecallResult(
            results=[
                _fact("a1", "from A", bank_id="bank-a", reranker=0.9),
                _fact("b1", "from B", bank_id="bank-b", reranker=0.5),
            ],
            entities={"Alice": EntityState(entity_id="e1", canonical_name="Alice")},
            chunks={"chunk-1": ChunkInfo(chunk_text="chunk text", chunk_index=0)},
            source_facts={"sf1": _fact("sf1", "source")},
            metadata={
                "multi_bank": {
                    "merge_requested": "score",
                    "merge_applied": "score",
                    "merge_fallback_reason": None,
                    "banks": {
                        "bank-a": {"status": "ok", "count": 1},
                        "bank-b": {"status": "ok", "count": 1},
                    },
                    "dedup": "none",
                }
            },
        )
    )
    return memory


@pytest_asyncio.fixture
async def api_client(mock_memory):
    app = create_app(mock_memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_multi_bank_recall_endpoint_happy_path(api_client, mock_memory):
    resp = await api_client.post(
        "/v1/default/memories/recall",
        json={
            "bank_ids": ["bank-a", "bank-b"],
            "query": "what did we decide?",
            "merge": "score",
            "max_tokens": 2048,
            "include": {"entities": {}, "chunks": {}, "source_facts": {}},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["results"]] == ["a1", "b1"]
    assert body["results"][0]["bank_id"] == "bank-a"
    assert body["results"][1]["bank_id"] == "bank-b"
    assert "Alice" in body["entities"]
    assert "chunk-1" in body["chunks"]
    assert "sf1" in body["source_facts"]
    assert body["metadata"]["multi_bank"]["merge_applied"] == "score"

    kwargs = mock_memory.recall_multi_async.call_args.kwargs
    assert kwargs["bank_ids"] == ["bank-a", "bank-b"]
    assert kwargs["merge"] == "score"
    assert kwargs["query"] == "what did we decide?"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["include_entities"] is True
    assert kwargs["include_chunks"] is True
    assert kwargs["include_source_facts"] is True


@pytest.mark.asyncio
async def test_multi_bank_recall_default_merge_is_score(api_client, mock_memory):
    resp = await api_client.post(
        "/v1/default/memories/recall",
        json={"bank_ids": ["x", "y"], "query": "hello world"},
    )
    assert resp.status_code == 200, resp.text
    assert mock_memory.recall_multi_async.call_args.kwargs["merge"] == "score"


@pytest.mark.asyncio
async def test_multi_bank_recall_rejects_empty_bank_ids(api_client, mock_memory):
    resp = await api_client.post(
        "/v1/default/memories/recall",
        json={"bank_ids": ["  ", ""], "query": "hello world"},
    )
    assert resp.status_code == 422
    mock_memory.recall_multi_async.assert_not_called()


@pytest.mark.asyncio
async def test_multi_bank_recall_rejects_empty_query(api_client, mock_memory):
    resp = await api_client.post(
        "/v1/default/memories/recall",
        json={"bank_ids": ["a", "b"], "query": "   "},
    )
    assert resp.status_code == 422
    mock_memory.recall_multi_async.assert_not_called()


@pytest.mark.asyncio
async def test_multi_bank_recall_maps_operation_validation_error(mock_memory):
    mock_memory.recall_multi_async = AsyncMock(side_effect=OperationValidationError("nope", status_code=403))
    app = create_app(mock_memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/default/memories/recall",
            json={"bank_ids": ["a", "b"], "query": "hello world"},
        )
    assert resp.status_code == 403
    assert "nope" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_multi_bank_recall_maps_authentication_error(mock_memory):
    """Same AuthenticationError mapping as single-bank: global handler -> 401."""
    mock_memory.recall_multi_async = AsyncMock(side_effect=AuthenticationError("Invalid API key"))
    mock_memory.recall_async = AsyncMock(side_effect=AuthenticationError("Invalid API key"))
    app = create_app(mock_memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        multi = await client.post(
            "/v1/default/memories/recall",
            json={"bank_ids": ["a", "b"], "query": "hello world"},
        )
        single = await client.post(
            "/v1/default/banks/a/memories/recall",
            json={"query": "hello world"},
        )
    assert multi.status_code == 401
    assert single.status_code == 401
    assert multi.json() == single.json()
    assert multi.json()["detail"] == "Authentication failed: Invalid API key"


@pytest.mark.asyncio
async def test_single_bank_recall_path_unchanged(api_client, mock_memory):
    """Existing single-bank endpoint still calls recall_async, not multi."""
    mock_memory.recall_async = AsyncMock(return_value=RecallResult(results=[_fact("s1", "solo", bank_id=None)]))
    resp = await api_client.post(
        "/v1/default/banks/solo-bank/memories/recall",
        json={"query": "hello world"},
    )
    assert resp.status_code == 200, resp.text
    mock_memory.recall_async.assert_called_once()
    mock_memory.recall_multi_async.assert_not_called()
    assert resp.json()["results"][0]["id"] == "s1"
