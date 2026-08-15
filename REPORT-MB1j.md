# REPORT-MB1j — port s14 overlay fixes onto main-based multi-bank

## EXEC SUMMARY

**Verdict:** PORT COMPLETE. Reviewable. No upstream PR filed (HELD). Not merged.

**Branch tips + fork push (2026-08-15):**
- `mb1j-upstream-port` = `025f8ac2408be9b74b9f0237792e597791355f2b` then +this report commit
  `git push fork mb1j-upstream-port` → `* [new branch] mb1j-upstream-port -> mb1j-upstream-port`
- `upstream/reranker-per-thread` = `7986c71ea5b6d02d218848e3fa4372a4829cda0d`
  `git push fork upstream/reranker-per-thread` → `* [new branch] upstream/reranker-per-thread -> upstream/reranker-per-thread`

**Tests last line (after `git merge origin/main`):**
`============================= 64 passed in 8.21s ==============================`
(`test_multi_bank_recall.py` + `_http.py` + `test_mental_model_refresh_error_retry.py`)

Reranker branch: `======================== 29 passed, 1 skipped in 3.12s ========================`

**94b831d3:** NOT already on base. Ported. e0a56d80 re-raises OVE only.

**Where the brief is wrong (headlines):**
1. e0a56d80 is not the same fix as 94b831d3 (OVE 403 vs tenant 401).
2. Overlay/PATCHES log MM refresh as WARNING; brief said logger.error.
3. Overlay `prefer_observations=True` is Python-orchestrator default only; HTTP/MCP stay False.
4. Overlay tuple returns violate this repo's no-tuple-return bar; used `DedupedFacts`.
5. `META_DEDUP_V1` name is now a lie (value is `exact_normalized`).

**FF `upstream/multi-bank-recall` onto this branch:**
```
git fetch fork
git checkout upstream/multi-bank-recall
git merge --ff-only fork/mb1j-upstream-port
```

---

## Full detail

### Base and inputs (read, not re-derived)

- Worktree started at `e0a56d80` (`upstream/multi-bank-recall`).
- First commit: `PR_BODY.md` copied byte-identical from
  `D:\HQ_runtime\grok_worktrees\upstream-multibank\PR_BODY.md` (`fc /b` identical)
  as `361a7f9d`.
- Overlay lineage `5019f2cd` is v0.9.0-based. Ported by intent, not cherry-pick.

`#3218` on this base (verified by reading `execute_task` at `e0a56d80` before port):
the generic path is already `logger.error(..., exc_info=True)` with an explicit
`#3218` comment. Notes were correct. Overlay `ca2bd211` still used `print_exc()`.

### 1. 3d01cf48 track-A — ported

**Overlay vs this port**

| Overlay | This branch | Why |
|---|---|---|
| Whole-file copy of live `multi_bank_recall.py` + `memory_engine.py` minus MM guards | Incremental edit of main-based files | Main already had fan-out cap, cancel/OVE hard-fail, generic errors, post-gather CE fallback |
| `dedup_exact_normalized` / `merge_cap_dedup_cut` return `(list, int)` | `DedupedFacts` dataclass | Repo code-review: no multi-item tuple returns |
| Docstring still said "Dedup: none in v1" in overlay `memory_engine` (3d01 left it) | Docstring + HTTP + MCP + interface updated | Brief item 1 |
| `prefer_observations: bool = True` on `recall_multi_async` | Same | HTTP/MCP request models still default False and pass through |

Pipeline: cap 50 → score/interleave merge → `exact_normalized` dedup → token cut.

`metadata.multi_bank` keys now match live overlay:
`merge_requested`, `merge_applied`, `merge_fallback_reason`,
`banks{status,count}`, `dedup`, `dedup_dropped`, `per_bank_cap`.

`banks.count` is still the pre-cap/pre-dedup per-bank list length (overlay
behavior; test asserts bank-a count 60 while only 50 results survive).

### 2. ca2bd211 MentalModelRefreshError — ported, logger form

- Added `_retry_or_reraise_worker_task` using `format_task_error` (main/#3218),
  not overlay `str(e)`.
- Dedicated `except MentalModelRefreshError` before generic `Exception`.
- Logs `logger.error(...)` **without** `exc_info` (no traceback). Overlay used
  `logger.warning`. Brief asked for logger.error. Soak grep for
  `MentalModelRefreshError` is the class name in a traceback, not this message.
- Generic path unchanged: `logger.error(..., exc_info=True)`.
- Did **not** copy overlay `REPORT-MB1bd.md` or `overlay_patch_memory_engine.diff`.

### 3. 94b831d3 AuthenticationError — PORT (not already on base)

**Evidence e0a56d80 does not cover it:**

- Commit message: "Re-raise OperationValidationError after gather".
- `git show e0a56d80 -- memory_engine.py` has no `AuthenticationError`.
- Pre-port gather loop only re-raised `OperationCancelledError` then
  `OperationValidationError`. A tenant `AuthenticationError` from
  `_authenticate_tenant` inside `recall_async` became
  `{"status":"error","error":"recall failed for this bank"}` inside a 200.

**Nuance the brief's "same fix class" misses:** HTTP `api_recall_multi` already
calls `_authenticate_tenant` when `_operation_validator` is not None (the
precheck-all-banks path). A live API-key tenant therefore 401s at HTTP *before*
the engine. MCP, no-validator, and direct engine calls did not. Overlay live
200 was measured on the v0.9.0 overlay, which lacked the engine re-raise.

**What we ported:**
- `await self._authenticate_tenant(request_context)` after empty-list return,
  before config fan-out.
- Gather precedence: cancel → `AuthenticationError` → OVE → soft-fail.

### 4. PR_BODY.md

Rewritten for the current branch (not v1 "no dedup"). Claims mapped to test
files. Still short.

### 5. fd059ba7 — separate branch `upstream/reranker-per-thread`

Off `origin/main` @ `396f63aa` (after fetch). **Not** on the multi-bank branch.

Port vs overlay:
- Same per-thread loader / warmup / no-share-on-failure design.
- Kept origin/main FlashRank batching tests (#3355) that the overlay file
  did not have (v0.9.0 base).
- `PR_BODY_reranker.md` is **committed** on that branch root
  (`7986c71e`). Brief said untracked is fine; committing it keeps the
  worktree clean when switching back.

Tests: `29 passed, 1 skipped` (`HS_RERANKER_STRESS` MiniLM soak skipped).

### 6. `git fetch origin` / merge

Merged `origin/main` (`396f63aa`, 96 commits) after the port. Tests re-run
green after the merge.

**Conflicts and how they were resolved:**

1. `hindsight_api/api/http.py`
   - HEAD: `metadata` on HTTP `RecallResponse`, `MultiBankRecallRequest`,
     shared `_core_recall_to_http_response`.
   - main: `source_facts_truncated` field; inlined mapper on single-bank.
   - Kept HEAD structure + main's field. Mapper now forwards
     `source_facts_truncated`. Single-bank stays on the shared helper.

2. `hindsight_api/engine/response_models.py`
   - Kept **both** `source_facts_truncated` (main) and `metadata` (multi-bank).

`memory_engine.py` / `mcp_tools.py` merged clean. After merge, multi-bank
OR-merges `source_facts_truncated` across successful banks
(`test_orchestrator_source_facts_truncated_or_across_banks`).

Both branches are based on current `origin/main` (reranker was created from
it; mb1j merged it).

### Tests added / extended

- Dedup exact-normalized across banks
- Per-bank cap (60 in → 50 out; empty-cap clamp)
- `prefer_observations` default True on fan-out; False passed through;
  single-bank `recall_async` default still False; dedup independent of the flag
- Metadata keys present
- Auth re-raise + skip fan-out
- HTTP 401 mapping vs single-bank
- MM refresh: RetryTaskAt, no traceback, generic path still `exc_info=True`

### Fast-forward command for the planner

```
git fetch fork
git checkout upstream/multi-bank-recall
git merge --ff-only fork/mb1j-upstream-port
```

Do **not** FF the reranker branch into multi-bank-recall. Separate PR candidate.

### Where I think the design / brief is wrong

1. **94b831d3 vs e0a56d80.** Brief said "same fix class" and allowed skip.
   OVE (bank-scoped 403/422 from `validate_recall`) ≠ tenant
   `AuthenticationError` (401 from missing/bad API key). Skipping would leave
   MCP/engine 200-on-bad-key. Ported. HTTP-with-validator was already 401.

2. **MM refresh log level.** PATCHES / overlay: one WARNING, no traceback,
   so soak `MentalModelRefreshError` grep stays quiet. Brief: `logger.error`.
   Used `logger.error` without `exc_info`. If soak later greps ERROR lines
   containing the exception *message*, this is noisier than overlay. Planner
   call: WARNING vs ERROR.

3. **prefer_observations split brain.** Python `recall_multi_async` default
   True; HTTP/MCP default False. Live 3-bank MCP recall this session still
   sent False unless the client set it. PATCHES calls this "fan-out only".
   Easy to document as "multi-bank prefers observations" when HTTP does not.

4. **Tuple returns.** Overlay returned `(facts, dropped)`. This repo's
   code-review skill forbids that even internally. `DedupedFacts` is a
   deliberate deviation from overlay shape.

5. **`META_DEDUP_V1 = "exact_normalized"`.** Kept overlay alias. The `_V1`
   name now means the opposite of "v1: none".

6. **Dedup of empty text.** `normalize_dedup_key(None)` and whitespace-only
   collapse to `""`. Two empty facts from different banks become one. Overlay
   does this. Cheap and maybe wrong.

7. **No OpenAPI regen.** HTTP description text changed; schema gained
   `source_facts_truncated` from main (already in main's OpenAPI). Multi-bank
   metadata is a free-form dict. Did not run `generate-openapi.sh`.

8. **Did not open the upstream PR.** Per brief (soak gate + operator go-ahead).

### Out of scope / not done

- No writes outside the worktree except the two `git push fork` calls.
- Live container / overlay / `~/.claude` / `~/.grok` / `D:\HQ_runtime\*.py` untouched.
- No self-merge.
