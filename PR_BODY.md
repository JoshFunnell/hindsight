## Multi-bank recall with score/interleave merge

### Motivation

Banks are deliberate isolation boundaries, but an operator who splits memory across
per-domain banks (work / personal / project) currently has to issue one recall per bank
and merge client-side — with no principled way to order the union, since each bank's
RRF/`final` scores are rank-relative to that bank alone. This PR adds server-side
multi-bank recall that merges on comparable scores where they exist, and falls back to a
fair round-robin where they do not.

### What's added

- `MemoryEngine.recall_multi_async(bank_ids, query, *, merge=..., ...)` — a thin
  orchestrator above `recall_async`. One sub-call per bank runs concurrently (`asyncio`
  task per bank, so each keeps its own `@_bind_bank_id` ContextVar binding for tracing and
  attribution). `recall_async`'s own signature and body are unchanged, so existing call
  sites need no edits.
- Pure merge helpers in `engine/multi_bank_recall.py`:
  - `merge="score"` (default): sort the union by each result's normalized cross-encoder
    score. Those are per-(query, document) pair scores normalized to [0, 1] by the same
    model, so for one query they order sensibly across banks without a second reranker
    pass. They are *relative* scores, not calibrated absolutes — the same caveat the
    existing `min_scores` documentation makes.
  - `merge="interleave"`: round-robin by per-bank rank (guaranteed per-bank
    representation).
  - Score-merge falls back to interleave, recording the reason in response metadata, when
    it would not be honest: a bank with `enable_reranking=false`, a caller-requested
    `rrf`/`interleave` reranking, **or** — checked after the sub-calls return, not
    predicted from config — results that carry no usable `reranker` score, as happens with
    a passthrough (`rrf`) cross-encoder provider.
- `POST /v1/{tenant}/memories/recall` — additive endpoint taking `bank_ids` + `merge` +
  the standard recall params. The existing per-bank route keeps its path, handler and
  behavior; its response-mapping code is extracted into a helper shared by both.
- MCP: the `recall` tool gains optional `bank_ids` / `merge`. Two or more ids route to
  multi-bank; a single id selects that bank; omitting it is unchanged.

### Semantics

- Token budget: each sub-call receives the caller's full `max_tokens`; the merged list is
  then cut with the same stop-before-exceeding rule as single-bank.
- Every merged result carries a `bank_id`; `RecallResult` gains an optional `metadata`
  field holding a `multi_bank` block (merge requested/applied, fallback reason, per-bank
  status and counts). **Response-shape note:** because both endpoints share the mapping
  helper, single-bank responses now also serialize `bank_id` and `metadata` as `null`.
  Existing fields are unchanged; clients that reject unknown fields would see the two new
  ones.
- Failure handling is tiered, so that "partial success" can never mask a decision the
  caller needs to see:
  - **Cancellation** (`OperationCancelledError`, client disconnect) propagates — the
    request still maps to 499 rather than returning a partial 200.
  - **Authorization / validation denials** (`OperationValidationError` from an
    extension's `validate_recall`) propagate too, so a denial maps to the same status the
    single-bank endpoint would return, instead of being downgraded to a per-bank error
    inside a 200.
  - **Ordinary infrastructure errors** (DB, timeout) soft-fail per bank: the other banks'
    results are returned and that bank is marked failed. The client-visible message is
    generic; the exception detail is logged server-side rather than echoed back, so the
    response cannot be used to probe which bank ids exist.
- The endpoint's precheck runs for every distinct bank in `bank_ids`, not just the first,
  so a gated bank cannot be reached by ordering an allowed one ahead of it.
- `include_entities` / `include_chunks` / `include_source_facts` are union-merged across
  banks; on a key collision the bank ranked higher in the merged results wins.
- `bank_ids` is capped (10) so one request cannot fan out into unbounded parallel recalls
  (rejected at the request model and again in the engine).

### Limitations (documented in code)

- No cross-bank dedup in v1: the same fact retained in two banks can appear twice.
  Exact-text dedup would be a cheap follow-up.
- Per-bank traces are not merged into the multi-bank response.
- The multi endpoint takes banks in the body rather than the path, so it cannot use the
  same `Depends(precheck_for(...))` wiring as the per-bank route; it loops the same
  precheck in-handler instead. If maintainers would rather see a dedicated dependency or
  a different auth shape here, say so and I will rework it.

### Tests

Unit tests for the orchestrator and merge helpers (score order and ties, interleave order,
each fallback trigger including the post-hoc no-usable-scores path, token cut, `bank_id`
attribution, partial-failure metadata, cancellation propagation, single-bank equivalence,
`include_*` union-merge and collision, the fan-out cap), HTTP endpoint tests, and MCP
routing tests.
