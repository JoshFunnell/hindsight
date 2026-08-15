# REPORT-MB1bd

Worktree: `D:\HQ_runtime\grok_worktrees\mb1bd-engine-fixes`
Branch: `mb1bd-engine-fixes` (off `multi-bank-recall` @ `ace16881`)
Mode: code. No push. Live overlays / compose / container not touched.

Grok output is evidence. Planner decides deploy-by-copy.

## Commits

| order | sha | item | subject |
|---|---|---|---|
| 1 | `3d01cf48` | (d) | `chore(recall): capture 2026-08-12 track-A live overlay` |
| 2 | `9a7c3b7f` | (d) | `test(recall): align multi-bank tests with the captured live overlay` |
| 3 | `ca2bd211` | (b) | `fix(engine): handle MentalModelRefreshError in execute_task` |

## (d) Provenance capture

### `multi_bank_recall.py`

Copied LF-identical from `D:\HQ_runtime\patches\hindsight\multi_bank_recall.py`.

- Overlay / branch md5: `ab09441539102436b946dcc5c3341e45` (15183 bytes LF)
- `verify_overlays.py` ref `upstream/multi-bank-recall` md5: `fe46675363b3` = `multi_bank_recall.py.bak-pretrackA-20260812`

This worktree's pre-capture file was **not** the bak (7458 bytes, md5 `a28932e4…`). The live overlay already contained the 2026-08-10 audit-fix symbols (`MAX_MULTI_BANK_RECALL_BANKS`, `FALLBACK_NO_USABLE_RERANKER_SCORES`, `has_usable_reranker_scores`) **plus** track-A. The capture is the whole live file, as the brief required.

### What track-A actually is (overlay minus `.bak-pretrackA-20260812`)

Planner can write the PATCHES.md line from this. Track-A is **not** the audit-fix hunks; those were already in the bak.

**`multi_bank_recall.py` (8 hunks vs bak):**

1. Module docstring: replace "v1 limitations / no dedup" with the required pipeline order (cap → merge → exact/normalized dedup → token cut), multi-bank-only defaults, and remaining limitations (no embedding near-dup collapse; no cross-bank supersession).
2. `DEFAULT_PER_BANK_MERGE_CAP = 50` and `MULTI_BANK_PREFER_OBSERVATIONS = True` (fan-out only; single-bank `recall_async` default stays False).
3. Metadata: `META_DEDUP_DROPPED`, `META_PER_BANK_CAP`; `META_DEDUP_V1` changes from `"none"` to `"exact_normalized"`.
4. `cap_per_bank_results` (anti-flood; `max_per_bank` clamped to >= 1).
5. Note on `interleave_merge`: fallback for unusable CE scores, **not** the anti-flood measure.
6. `normalize_dedup_key` + `dedup_exact_normalized` (casefold + whitespace collapse; keep earlier/higher-ranked copy).
7. `merge_cap_dedup_cut` composes cap → merge → dedup → token cut and returns `(facts, dedup_dropped)`.
8. `build_multi_bank_metadata` records `dedup` / `dedup_dropped` / `per_bank_cap`.

**`memory_engine.py` (5 hunks vs bak) — the track-A wiring:**

1. `recall_multi_async(..., prefer_observations: bool = True)`.
2. Import `DEFAULT_PER_BANK_MERGE_CAP` + `merge_cap_dedup_cut`; drop direct `cut_to_token_budget` / `interleave_merge` / `score_merge` imports.
3. Empty-`bank_ids` metadata gets `dedup_dropped=0`, `per_bank_cap=DEFAULT_PER_BANK_MERGE_CAP`.
4. Replace `score_merge`/`interleave_merge` + `cut_to_token_budget` with `merge_cap_dedup_cut(...)`.
5. Success-path metadata gets `dedup_dropped` and `per_bank_cap`.

HTTP/MCP still pass `request.prefer_observations` (Field default **False**). Track-A's True default only applies when a Python caller omits the kwarg.

### `memory_engine.py` hunk classification (live overlay vs this tree **before** (d))

`git diff --no-index` of LF copies: **9 hunks**. Classes as the brief defined them.

| # | Anchor (overlay) | What | Class | Action |
|---|---|---|---|---|
| 1 | `recall_multi_async` signature | `prefer_observations` False → True | (ii) track-A | **IN** |
| 2 | orchestrator docstring + imports | Failures docstring (cancel/auth hard-fail, generic client errors) + import mix (`FALLBACK`/`MAX`/`has_usable` **and** `DEFAULT_PER_BANK`/`merge_cap_dedup_cut`) | (iii) audit-fix + (ii) track-A | **IN** |
| 3 | after bank-id de-dupe | `MAX_MULTI_BANK_RECALL_BANKS` → 422 `OperationValidationError` | (iii) 2026-08-10 audit-fix (in bak) | **IN** |
| 4 | empty `bank_ids` metadata + config lookup | `dedup_dropped`/`per_bank_cap` on empty path; config errors no longer leak `type:repr` | (ii) + (iii) | **IN** |
| 5 | after `asyncio.gather` | Re-raise `OperationCancelledError` then `OperationValidationError`; generic `"recall failed for this bank"` | (iii) | **IN** |
| 6 | merge site | Post-gather `has_usable_reranker_scores` fallback **and** `merge_cap_dedup_cut` | (iii) + (ii) | **IN** |
| 7 | success metadata | `dedup_dropped`, `per_bank_cap` | (ii) | **IN** |
| 8 | `_patch_fresh_retrieval_empty` (`# LOCAL PATCH #2894`) | Capture pre-merge empty retrieval | (i) guard #2894 | **OUT** |
| 9 | `_patch_placeholder` (`# LOCAL PATCH … #2959 + #2894`) | Refuse placeholder / empty-fresh overwrite of real content | (i) guard #2959/#2894 | **OUT** |

Class (iii) is not a guess: every one of those hunks is present in `memory_engine.py.bak-pretrackA-20260812` and absent from `ace16881`. They match PATCHES.md's 2026-08-10 P1s and commit `f68812a7` (`multibank-authfix`). The brief said "report, do not guess." I **included** them because the acceptance check is `overlay == branch file + the two guard hunks`. Leaving them out would fail that check. See "Where I think the design is wrong" §1.

### Verification that `overlay == branch file + two guard hunks` (after (d), before (b))

Method: strip the two unique `LOCAL PATCH` blocks from the live overlay; write that as the branch file; re-insert the extracted blocks; md5-compare.

- Overlay LF md5: `7c7a30951f683401803db07000d6fbae`
- Rebuild (branch + guard1 + guard2) md5: `7c7a30951f683401803db07000d6fbae` — match
- `git diff --no-index` branch vs overlay: **exactly 2 hunks**
  - `@@ -13030,6 +13030,17 @@` (`_patch_fresh_retrieval_empty`)
  - `@@ -13237,6 +13248,53 @@` (`_patch_placeholder`)
- Those hunks contain the guard symbols and do **not** contain track-A symbols (`prefer_observations`, `merge_cap_dedup_cut`).

After (b): `overlay + (b) == current branch file + the same two guard hunks`. Confirmed by rebuilding overlay+guards from the post-(b) file and matching `git apply` of `overlay_patch_memory_engine.diff` onto a temp copy of the live overlay (applied md5 `183c38fdb53c0935d549a4e201f88205`).

## (b) `MentalModelRefreshError` handler

In `execute_task`, **before** `except Exception`:

- `except MentalModelRefreshError as e:` at `memory_engine.py:2596`
- One `logger.warning` (no `exc_info`, no `print_exc`) with `task_type`, `mental_model_id`, `bank_id`, and `str(e)` (already names the reason and says content was preserved)
- Same non-consolidation retry policy as the generic branch, factored into `_retry_or_reraise_worker_task` (`:759`): `RetryTaskAt` while `_retry_count < worker_max_retries`, else re-raise so the poller marks failed
- Consolidation / `file_convert_retain` / `_is_non_retryable_task_error` branches untouched

The exception is expected (#3112/#3182 `_preserve_and_fail`). A traceback is what the soak watcher's `unhandled-in-overlay` pattern flags. Retry is still correct for LLM-produced delta ops: a retry re-reads the same window because watermark is not advanced.

`execute_task` on this tree / the live overlay was byte-identical before (b), so the overlay patch is (b) only.

## Tests

Convention here is **pytest** (not HQ unittest). New file: `hindsight-api-slim/tests/test_mental_model_refresh_error_retry.py`.

- (i) `_handle_refresh_mental_model` → `MentalModelRefreshError`, `_retry_count=0` → `RetryTaskAt`; stderr has no `Traceback`; a WARNING record contains the `mental_model_id` and `delta_ops_all_skipped`
- (ii) `_retry_count == worker_max_retries` → `MentalModelRefreshError` propagates, not `RetryTaskAt`
- (iii) `RuntimeError` on the same path still prints a traceback (silence not widened)

These tests stub the handler and omit `operation_id` so they do not need pg0.

### Test output (last lines)

Command: `uv run pytest tests/test_mental_model_refresh_error_retry.py tests/test_multi_bank_recall.py -v --tb=short`
(from `hindsight-api-slim`; venv created at worktree `.venv` by `uv`)

```
============================ slowest 10 durations =============================
0.11s call     tests/test_multi_bank_recall.py::test_orchestrator_passes_full_max_tokens_to_each_subcall
0.11s call     tests/test_multi_bank_recall.py::test_orchestrator_auto_fallback_when_requested_rrf
0.11s call     tests/test_multi_bank_recall.py::test_orchestrator_auto_fallback_when_bank_disables_reranking
0.11s call     tests/test_multi_bank_recall.py::test_orchestrator_score_merge_order
0.10s call     tests/test_multi_bank_recall.py::test_orchestrator_merges_entities_chunks_source_facts
0.10s call     tests/test_multi_bank_recall.py::test_orchestrator_token_cut_on_merged_list
0.10s call     tests/test_multi_bank_recall.py::test_cut_to_token_budget_stops_before_exceeding
0.10s call     tests/test_multi_bank_recall.py::test_orchestrator_interleave_order
0.01s call     tests/test_mental_model_refresh_error_retry.py::test_generic_runtime_error_on_refresh_still_prints_traceback
0.01s call     tests/test_mental_model_refresh_error_retry.py::test_refresh_error_retries_without_traceback
============================= 44 passed in 3.94s ==============================
```

`ruff check` + `ruff format --check` on the three touched Python files: clean.

Not run: `test_worker_retry_knobs.py` / DB-using `test_integrity_violation_not_retried.py` — this worktree's `uv` env has no `pg0-embedded`. Those tests need the `memory` fixture. I did not install extra extras. Their setup error is `No module named 'pg0'`, not an assertion failure in this change.

## Overlay patch (planner deploy)

File: `overlay_patch_memory_engine.diff` (worktree root). Unified diff, 3 hunks, **only (b)**. Does not touch the two guard hunks.

Verified on a **temp copy**, never the live file:

```
git apply --check   → exit 0   (cwd = temp dir containing a copy of the live overlay)
patch --dry-run -p1 → exit 0
git apply            → applied bytes == overlay + (b)
```

### Exact commands the planner runs

Do not apply from this agent. Live overlay dir stays planner-owned.

```
# 0. Backup the live overlay (planner)
copy D:\HQ_runtime\patches\hindsight\memory_engine.py D:\HQ_runtime\patches\hindsight\memory_engine.py.bak-pre-mb1bd-20260815

# 1. Dry-run against a TEMP copy (same check used here)
mkdir C:\Temp\mb1bd-planner-apply
copy /Y D:\HQ_runtime\patches\hindsight\memory_engine.py C:\Temp\mb1bd-planner-apply\memory_engine.py
git -C C:\Temp\mb1bd-planner-apply apply --check D:\HQ_runtime\grok_worktrees\mb1bd-engine-fixes\overlay_patch_memory_engine.diff
patch --dry-run -p1 -d C:\Temp\mb1bd-planner-apply -i D:\HQ_runtime\grok_worktrees\mb1bd-engine-fixes\overlay_patch_memory_engine.diff

# 2. Apply to the live overlay (planner)
git -C D:\HQ_runtime\patches\hindsight apply D:\HQ_runtime\grok_worktrees\mb1bd-engine-fixes\overlay_patch_memory_engine.diff

# 3. verify_overlays.py (read-only check)
python D:\HQ_runtime\patches\hindsight\verify_overlays.py
```

**`verify_overlays.py` expectation after step 2:**

- `memory_engine.py` — still `DERIVED-OK` vs `local/overlay-deploy`. Changed-line count rises by ~30 (helper + except + retry call), well under the 4000 cap. It will **not** become EXACT against this branch: this branch omits the two guard hunks.
- `multi_bank_recall.py` — still `MISMATCH` vs `upstream/multi-bank-recall` (`fe46675363b3`) until that EXACT ref is retargeted to a ref that contains `3d01cf48` **and** is visible to `D:\.dev\Repositories\hindsight-two-tier` (the clone `verify_overlays.py` reads). The live file already is md5 `ab0944153910`; this branch reproduces it. I did not push; I did not edit `verify_overlays.py`.
- Other 17 mounts: unchanged.

**`multi_bank_recall.py` needs no overlay apply** — the live file is already the captured content.

### Recreate + what to watch

Container recreate is the planner's (`run_compose.ps1`), not mine.

After recreate, watch the API log for the next `delta_ops_all_skipped` refresh (the 72h pair was `mm-492219b847a94721b36ba773f343b414` and `toolchain-skills-hooks`):

- **Want:** one `WARNING` line: `Mental model refresh failed (content preserved): task_type=refresh_mental_model mental_model_id=… bank_id=… error=…delta_ops_all_skipped…`
- **Want not:** `Traceback (most recent call last):` from `execute_task` → `_handle_refresh_mental_model` → `refresh_mental_model` → `_preserve_and_fail` → `MentalModelRefreshError`
- **Want:** worker retries (`RetryTaskAt` / same op coming back) until `_retry_count == worker_max_retries` (default 3), then the operation is marked failed with the preserve message
- **Want unchanged:** a genuine `RuntimeError` / unexpected exception still emits a traceback via the generic `except Exception`

## Upstream PR candidate?

**Yes, with a rebase note — not a verbatim copy of this overlay patch.**

Evidence:

- `git grep except MentalModelRefreshError origin/main` → no matches (exit 1)
- `gh` issue search for `MentalModelRefreshError` finds #3112/#3182 (the fail-safe itself, closed) and the #2894/#2960/#3135 guard issues. Nothing about the uncaught worker path.
- `origin/main` `execute_task` still falls through to `except Exception`. So the uncaught path is real upstream.

Rebase note (do not silently ignore): `origin/main` no longer calls `traceback.print_exc()`. #3218 changed that branch to `logger.error(..., exc_info=True)` plus `format_task_error`. The live v0.9.0 overlay still has `print_exc` — that is the soak-watcher symptom. An upstream PR should add the same `except MentalModelRefreshError` **before** the generic handler and log **WARNING without `exc_info`**, then use main's existing retry block (or the same helper). Do not re-introduce `print_exc` on main.

## Code review (self)

**Must fix:** none found in this diff.

**Should fix / notes:**

- `_retry_or_reraise_worker_task` takes `task_dict: dict[str, Any]` — same dynamic worker payload `execute_task` already uses. Not a new structured-dict leak.
- Captured track-A helpers `dedup_exact_normalized` and `merge_cap_dedup_cut` return tuples. That violates this repo's "no multi-item tuple returns" rule. Left as-is (provenance).
- Live overlay `recall_multi_async` docstring still says **Dedup: none in v1** after track-A added exact/normalized dedup. Left as-is (provenance).
- `(b)` tests do not use the `memory` fixture; they isolate `execute_task` classification. That matches the intent of `test_integrity_violation_not_retried`'s handler stub, without requiring pg0.

## Where I think the design is wrong

1. **The brief's premise about this tree was false.** It said this worktree is the v0.9.0-based source of the live overlay set, so `memory_engine.py` would be overlay minus the two guards minus track-A. Measured: `ace16881` is missing the 2026-08-10 audit-fix hunks that are already in `bak-pretrackA`. I did not drop those hunks, because otherwise `overlay == branch + two guards` cannot hold. If the planner wanted a pure track-A-only branch that does **not** reproduce the live overlay without the audit-fix, say so — that is a different capture.

2. **Retrying every `MentalModelRefreshError` treats identifier-retention the same as `delta_ops_all_skipped`.** Identifier-retention is closer to deterministic (same document, same drop). The brief said use the generic retryable policy for this exception class; the class docstring also says retryable. I implemented that. If the soak pair is *persistently* `delta_ops_all_skipped`, we will burn `worker_max_retries + 1` LLM refresh attempts per occurrence. That is the specified policy, not a proof it is the cheapest one.

3. **`origin/main` already moved off `print_exc` (#3218).** A patch authored against the v0.9.0 overlay is the right live fix and the wrong literal PR. See "Upstream PR candidate".

4. **Track-A's `prefer_observations=True` default is narrower than the name.** HTTP still defaults False. Only a direct `recall_multi_async(...)` call that omits the kwarg flips. If the operator thought "multi-bank recall now prefers observations end-to-end," the live HTTP path does not.

I did not silently "fix" 2–4.
