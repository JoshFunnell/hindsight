"""Pure helpers for multi-bank recall merge + token budget cut.

The orchestrator (``MemoryEngine.recall_multi_async``) fans out one ``recall_async``
per bank and then uses these helpers to order and truncate the union.

v1 limitations (documented for callers / PR):
- No cross-bank dedup — identical or near-identical facts from different banks can
  both appear in the merged list. Exact-text dedup is a cheap v2.
- Score-merge uses each result's existing normalized cross-encoder score
  (``scores.reranker``); it does not run a second cross-encoder pass over the union.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypeVar

from .response_models import MemoryFact
from .token_encoding import get_token_encoding

MultiBankMerge = Literal["score", "interleave"]

# Metadata keys written into ``RecallResult.metadata`` by the orchestrator.
META_MULTI_BANK = "multi_bank"
META_MERGE_REQUESTED = "merge_requested"
META_MERGE_APPLIED = "merge_applied"
META_MERGE_FALLBACK_REASON = "merge_fallback_reason"
META_BANKS = "banks"
META_DEDUP = "dedup"
META_DEDUP_V1 = "none"  # v1: no cross-bank dedup

_T = TypeVar("_T")

# Banks with no facts in the merged list still may contribute side dicts
# (e.g. chunks fetched independently of max_tokens); rank them after all others.
_SIDE_DICT_UNRANKED = 10**9


def stamp_bank_id(fact: MemoryFact, bank_id: str) -> MemoryFact:
    """Return a copy of ``fact`` with ``bank_id`` set (orchestrator attribution)."""
    return fact.model_copy(update={"bank_id": bank_id})


def bank_rank_from_merged(facts: Sequence[MemoryFact]) -> dict[str, int]:
    """Map each bank_id to its best (earliest) position in the merged result list.

    Lower rank wins on side-dict key collisions. Banks absent from ``facts`` are
    omitted (callers treat them as unranked).
    """
    ranks: dict[str, int] = {}
    for index, fact in enumerate(facts):
        bid = fact.bank_id
        if bid is not None and bid not in ranks:
            ranks[bid] = index
    return ranks


def union_merge_dicts(
    bank_dicts: Sequence[tuple[str, Mapping[str, _T] | None]],
    *,
    bank_rank: Mapping[str, int],
) -> dict[str, _T] | None:
    """Union-merge optional per-bank dicts; on key collision keep the higher-ranked bank.

    ``bank_rank`` maps bank_id → rank (lower is better), typically from
    :func:`bank_rank_from_merged`. Banks not present in ``bank_rank`` sort last.
    Returns ``None`` when no bank contributed any entries (mirrors single-bank
    ``include_*`` omitted behaviour).
    """
    winners: dict[str, tuple[int, _T]] = {}
    for bank_id, mapping in bank_dicts:
        if not mapping:
            continue
        rank = bank_rank.get(bank_id, _SIDE_DICT_UNRANKED)
        for key, value in mapping.items():
            previous = winners.get(key)
            if previous is None or rank < previous[0]:
                winners[key] = (rank, value)
    if not winners:
        return None
    return {key: pair[1] for key, pair in winners.items()}


def _reranker_score(fact: MemoryFact) -> float:
    """Normalized cross-encoder score used for score-merge; missing → -inf (sort last)."""
    if fact.scores is None or fact.scores.reranker is None:
        return float("-inf")
    return float(fact.scores.reranker)


def score_merge(bank_results: Sequence[tuple[str, Sequence[MemoryFact]]]) -> list[MemoryFact]:
    """Sort the union of per-bank results by normalized cross-encoder score descending.

    Tie-break (stable, deterministic): bank list order, then within-bank rank (input
    order). Each result is stamped with its source ``bank_id``.

    Score-merge is only honest when every contributing bank actually ran
    cross_encoder reranking (see ``cross_encoder_eligible`` / orchestrator auto-fallback).
    """
    tagged: list[tuple[float, int, int, MemoryFact]] = []
    for bank_idx, (bank_id, facts) in enumerate(bank_results):
        for rank, fact in enumerate(facts):
            stamped = stamp_bank_id(fact, bank_id)
            # Negate score so ascending sort yields highest first; bank_idx/rank break ties.
            tagged.append((-_reranker_score(stamped), bank_idx, rank, stamped))
    tagged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in tagged]


def interleave_merge(bank_results: Sequence[tuple[str, Sequence[MemoryFact]]]) -> list[MemoryFact]:
    """Round-robin merge by per-bank rank: bankA#1, bankB#1, bankA#2, bankB#2, ...

    Banks appear in the order of ``bank_results``. Empty banks contribute no slots.
    Each result is stamped with its source ``bank_id``. Guarantees per-bank
    representation when banks have results (unlike pure score sort).
    """
    stamped_lists: list[list[MemoryFact]] = [
        [stamp_bank_id(fact, bank_id) for fact in facts] for bank_id, facts in bank_results
    ]
    merged: list[MemoryFact] = []
    max_len = max((len(lst) for lst in stamped_lists), default=0)
    for rank in range(max_len):
        for lst in stamped_lists:
            if rank < len(lst):
                merged.append(lst[rank])
    return merged


def cut_to_token_budget(facts: Sequence[MemoryFact], max_tokens: int) -> list[MemoryFact]:
    """Keep results until ``max_tokens`` on the ``text`` field (same semantics as single-bank).

    Stops before including a fact that would exceed the budget. Counts tokens with
    the shared cl100k_base encoding. ``max_tokens <= 0`` yields an empty list.
    """
    if max_tokens <= 0:
        return []
    encoding = get_token_encoding()
    filtered: list[MemoryFact] = []
    total = 0
    for fact in facts:
        text_tokens = len(encoding.encode(fact.text or ""))
        if total + text_tokens <= max_tokens:
            filtered.append(fact)
            total += text_tokens
        else:
            break
    return filtered


def cross_encoder_eligible(
    *,
    requested_reranking: str,
    bank_enable_reranking: Sequence[bool],
) -> tuple[bool, str | None]:
    """Whether score-merge is valid for this multi-bank call.

    Returns ``(True, None)`` when every bank would resolve to cross_encoder, else
    ``(False, reason)`` describing why score-merge must fall back to interleave.

    Mirrors ``_resolve_reranking``: only ``cross_encoder`` is downgraded when
    ``enable_reranking`` is false (to ``rrf``). Caller-requested ``rrf`` / ``interleave``
    never produce comparable ``scores.reranker`` values.
    """
    if requested_reranking != "cross_encoder":
        return (
            False,
            f"requested reranking={requested_reranking!r} (score-merge requires cross_encoder)",
        )
    if not all(bank_enable_reranking):
        return (
            False,
            "one or more banks have enable_reranking=false (resolved reranking would be rrf)",
        )
    return True, None


def build_multi_bank_metadata(
    *,
    merge_requested: MultiBankMerge,
    merge_applied: MultiBankMerge,
    merge_fallback_reason: str | None,
    bank_statuses: dict[str, dict],
) -> dict:
    """Assemble the multi-bank block stored on ``RecallResult.metadata``."""
    return {
        META_MULTI_BANK: {
            META_MERGE_REQUESTED: merge_requested,
            META_MERGE_APPLIED: merge_applied,
            META_MERGE_FALLBACK_REASON: merge_fallback_reason,
            META_BANKS: bank_statuses,
            META_DEDUP: META_DEDUP_V1,
        }
    }
