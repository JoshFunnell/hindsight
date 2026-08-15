## Multi-bank recall with score/interleave merge

### Motivation

Banks are isolation boundaries, but an operator who splits memory across
per-domain banks currently has to issue one recall per bank and merge
client-side. Per-bank RRF/`final` scores are rank-relative to that bank
alone, so there is no principled client-side order. This PR adds
server-side multi-bank recall that merges on comparable cross-encoder
scores where they exist, and falls back to a fair round-robin where they
do not.

### What's added

- `MemoryEngine.recall_multi_async(bank_ids, query, *, merge=..., ...)` —
  thin orchestrator above unchanged `recall_async`. One concurrent
  `asyncio` task per bank (each keeps its own `@_bind_bank_id` ContextVar).
- Pure helpers in `engine/multi_bank_recall.py`:
  - `merge="score"` (default): sort the union by each result's normalized
    `scores.reranker` (per-(query, document) pair, same model).
  - `merge="interleave"`: round-robin by per-bank rank.
  - Score-merge falls back to interleave (reason in metadata) when CE is
    not honest: `enable_reranking=false`, caller-requested `rrf`/`interleave`,
    or returned facts with no usable `scores.reranker` (RRF passthrough).
- Pipeline after gather: **per-bank cap 50 -> merge -> exact/normalized
  text dedup -> token cut**. Dedup key is `casefold` + Unicode whitespace
  collapse. Mode string: `exact_normalized`.
- `prefer_observations` defaults **True on this orchestrator only**
  (fan-out into each `recall_async`). HTTP/MCP request models still
  default False and pass the caller's value through. Single-bank
  `recall_async` default is unchanged (False).
- `POST /v1/{tenant}/memories/recall` — additive; existing per-bank route
  unchanged. MCP `recall` gains optional `bank_ids` / `merge` (2+ = multi;
  length-1 selects that bank; omit = session/`bank_id`).

### Semantics

- Token budget: each sub-call gets the caller's full `max_tokens`; the
  merged list is then cut with the same stop-before-exceeding rule.
- Every merged result carries `bank_id`. `metadata.multi_bank` keys:
  `merge_requested`, `merge_applied`, `merge_fallback_reason`,
  `banks{status,count}`, `dedup`, `dedup_dropped`, `per_bank_cap`.
- Failure handling:
  - **Cancellation** (`OperationCancelledError`) propagates (HTTP 499).
  - **Tenant `AuthenticationError`** is authenticated once before fan-out
    and re-raised after gather (HTTP 401). Auth is per-request, not
    per-bank. Must not soft-fail into a 200.
  - **Authorization / validation** (`OperationValidationError`)
    propagates (same status as single-bank).
  - **Ordinary infrastructure errors** soft-fail per bank (generic client
    text; details logged server-side).
- HTTP precheck runs for every distinct `bank_id`, not just the first.
- `include_*` side dicts are union-merged; higher-ranked bank wins collisions.
- `bank_ids` capped at 10 (422 at the request model and again in the engine).
- Single-element and empty `bank_ids` do not change single-bank behaviour
  (length-1 multi-call stamps `bank_id` + metadata; empty list is empty).

### Limitations

- No embedding / near-duplicate collapse; punctuation-different facts stay.
- No cross-bank supersession (a stale fact in A can outrank its correction in B).
- Per-bank traces are not merged.
- The multi endpoint takes banks in the body, so it loops `precheck` in-handler
  rather than `Depends(precheck_for(...))`.

### Tests (claim -> file)

- Score / interleave / fallback / token cut / `bank_id` / include_* /
  cancel / fan-out cap / OVE: `tests/test_multi_bank_recall.py`
- Dedup exact-normalized, per-bank cap, prefer_observations fan-out-only,
  metadata keys, AuthenticationError re-raise + skip fan-out:
  `tests/test_multi_bank_recall.py`
- HTTP happy path, 422 cap, precheck-all-banks, 401 mapping:
  `tests/test_multi_bank_recall_http.py`
- MCP `bank_ids` routing: `tests/test_mcp_tools.py` / `tests/test_mcp_routing.py`
- `execute_task` MentalModelRefreshError via `logger.error` (no traceback),
  RetryTaskAt policy: `tests/test_mental_model_refresh_error_retry.py`
