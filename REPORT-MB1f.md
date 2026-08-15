# REPORT-MB1f

Worktree: `D:\HQ_runtime\grok_worktrees\mb1f-multibank-auth`
Branch: `mb1f-multibank-auth` (off `mb1bd-engine-fixes` @ `eb9aa6e3`)
Mode: code. No push. Live overlays / compose / container / `D:\HQ_runtime\*.py` not touched.

Grok output is evidence. Planner decides deploy-by-copy.

## FIRST: live overlay vs this tree (`recall_multi_async`)

Required before any edit. Compared newline-normalized function bodies:

| function | live overlay | this tree (pre-edit) | identical (LF-norm) |
|---|---|---|---|
| `recall_multi_async` | `D:\HQ_runtime\patches\hindsight\memory_engine.py` lines 5257-5513 | same line span | **yes** (sha `743ea448b3490506`) |
| `_authenticate_tenant` | lines 1988-2027 | same | **yes** (sha `2e471ee9c37adb6a`) |

Whole-file hashes differ (live LF 793208 bytes; tree CRLF 806644 bytes) because this branch omits the two live-only LOCAL PATCH guards and the files use different newlines. The **function under edit matched**. Did not STOP.

Live overlay is LF-only. This tree's `memory_engine.py` is CRLF (git autocrlf).

## The change

In `MemoryEngine.recall_multi_async`:

1. **Up-front tenant auth** (after empty-`bank_ids` return / over-cap 422, before config lookup and fan-out):

   `await self._authenticate_tenant(request_context)`

   Tenant auth is request-scoped. Fail the whole multi-recall before N parallel `recall_async` calls and before per-bank config lookups.

2. **Re-raise after `asyncio.gather(..., return_exceptions=True)`**, still before the generic soft-fail loop. Precedence:

   1. `OperationCancelledError` (HTTP 499) — unchanged first
   2. `AuthenticationError` (HTTP 401) — **new**; this is what `_authenticate_tenant` raises
   3. `OperationValidationError` (HTTP 403/422 bank-scoped denials) — unchanged

   Did **not** widen the re-raise to all exceptions. A per-bank `RuntimeError` still becomes `metadata.multi_bank.banks.<id> = {status: error, error: "recall failed for this bank"}`.

3. **HTTP:** no `http.py` change. `api_recall_multi` already has `except (AuthenticationError, HTTPException): raise`. The global `@app.exception_handler(AuthenticationError)` returns 401 `{"detail": str(exc)}` — same mapping as single-bank `api_recall`. No `overlay_patch_http_mb1f.diff`.

4. **Did not skip per-bank auth / precheck.** Each `recall_async` still calls `_authenticate_tenant` (schema ContextVar + the single-bank engine contract). Per-bank `OperationValidator` precheck stays inside `recall_async`.

Why both up-front and gather re-raise:

- Up-front: fail-fast for an unauthenticated request (the live no-auth / bad-Bearer case) without starting N recalls.
- Gather re-raise: the RED test (and the live-measured path) injects `AuthenticationError` from **one bank's** `recall_async`. Up-front cannot see that. 2026-08-10 P1#(3) re-raised only `OperationValidationError`; this is the missing sibling.

## Why fail-the-whole-request vs per-bank soft-fail

Authentication is per **tenant / request**, not per bank. A missing or invalid API key means the request itself is unauthenticated. Returning 200 with other banks' facts (or empty results plus `status: error`) would leak that those banks exist and would disagree with single-bank recall, which maps the same exception to 401.

Soft-fail remains correct for a bank that errors **inside** its own recall after the request is authenticated (`RuntimeError`, config lookup failure, etc.).

## RED / GREEN evidence

Convention here is **pytest** via `uv run pytest` from `hindsight-api-slim`.

### RED (pre-change engine, new auth test only)

Command (from `hindsight-api-slim`):

```
uv run pytest tests/test_multi_bank_recall.py::test_orchestrator_authentication_error_propagates_not_soft_fail tests/test_multi_bank_recall.py::test_orchestrator_validation_error_from_bank_propagates_not_soft_fail tests/test_multi_bank_recall.py::test_orchestrator_partial_failure_metadata tests/test_multi_bank_recall.py::test_orchestrator_cancellation_propagates_not_soft_fail -v --tb=short
```

Last lines:

```
[gw0] FAILED tests/test_multi_bank_recall.py::test_orchestrator_authentication_error_propagates_not_soft_fail
E   Failed: DID NOT RAISE <class 'hindsight_api.extensions.tenant.AuthenticationError'>
WARNING  hindsight_api.engine.memory_engine:memory_engine.py:5447 [RECALL MULTI bank-b] per-bank recall failed: AuthenticationError: AuthenticationError('Authentication failed: Invalid API key')
FAILED tests/test_multi_bank_recall.py::test_orchestrator_authentication_error_propagates_not_soft_fail - Failed: DID NOT RAISE <class 'hindsight_api.extensions.tenant.AuthenticationError'>
======================== 1 failed, 3 passed in 20.92s =========================
```

OVE / cancel / generic `RuntimeError` already green against the pre-change tree (the 2026-08-10 P1#(3) legs).

### GREEN (after the engine change)

```
uv run ruff check <three files>     # All checks passed!
uv run ruff format --check <three files>  # 3 files already formatted
uv run pytest tests/test_multi_bank_recall.py tests/test_multi_bank_recall_http.py --tb=line
```

Last lines:

```
============================= 51 passed in 22.87s =============================
```

`scripts/hooks/lint.sh` was also run; it failed on **pre-existing** Windows-host noise (control-plane eslint missing `@eslint/js`; `ty` unresolved `mlx.*` / `fcntl` / `termios`). None of those files are in this diff. Targeted ruff on the three touched Python files is clean.

### Tests added

| test | asserts |
|---|---|
| `test_orchestrator_authentication_error_propagates_not_soft_fail` | one bank raises `AuthenticationError` -> whole call raises (RED against parent) |
| `test_orchestrator_validation_error_from_bank_propagates_not_soft_fail` | one bank raises `OperationValidationError(403)` -> whole call raises (unchanged) |
| `test_orchestrator_partial_failure_metadata` | existing: `RuntimeError` still soft-fails with generic message; other bank kept |
| `test_orchestrator_upfront_tenant_auth_failure_skips_fanout` | up-front auth failure -> no `recall_async` calls |
| `test_multi_bank_recall_maps_authentication_error` | HTTP: engine `AuthenticationError` on multi and single -> both 401, **same body** (`Authentication failed: Invalid API key`) |

No real-tenant HTTP fixture exists in this repo (the multi HTTP tests mock the engine with `_operation_validator = None`). The live no-auth POST is the planner's one-line check after deploy.

## Overlay patch (planner deploy)

File: `overlay_patch_memory_engine_mb1f.diff` (worktree root). Unified diff, **3 hunks, only this fix**. LF. No `http.py` patch.

Verified on a **temp copy**, never the live file:

```
git apply --check   → exit 0   (cwd = temp dir containing a copy of the live overlay)
git -c core.autocrlf=false -c core.eol=lf apply
    → applied bytes == patched overlay (md5 f97257f28f9dd43909c72d5c0c35d2c1)
    → LF preserved
    → newline-normalized `recall_multi_async` == this tree's function
```

**CRLF warning:** default Windows `git apply` (autocrlf on) converted a temp copy of the live overlay to CRLF. Planner must apply with CRLF conversion disabled.

### Exact commands the planner runs

Do not apply from this agent. Live overlay dir stays planner-owned.

```
# 0. Backup the live overlay (planner)
copy D:\HQ_runtime\patches\hindsight\memory_engine.py D:\HQ_runtime\patches\hindsight\memory_engine.py.bak-pre-mb1f-20260815

# 1. Dry-run against a TEMP copy
mkdir C:\Temp\mb1f-planner-apply
copy /Y D:\HQ_runtime\patches\hindsight\memory_engine.py C:\Temp\mb1f-planner-apply\memory_engine.py
git -c core.autocrlf=false -c core.eol=lf -C C:\Temp\mb1f-planner-apply apply --check D:\HQ_runtime\grok_worktrees\mb1f-multibank-auth\overlay_patch_memory_engine_mb1f.diff

# 2. Apply to the live overlay (planner)
git -c core.autocrlf=false -c core.eol=lf -C D:\HQ_runtime\patches\hindsight apply D:\HQ_runtime\grok_worktrees\mb1f-multibank-auth\overlay_patch_memory_engine_mb1f.diff

# 3. Compile (syntax only; overlay is not imported as a package)
python -m py_compile D:\HQ_runtime\patches\hindsight\memory_engine.py

# 4. verify_overlays.py (read-only check)
python D:\HQ_runtime\patches\hindsight\verify_overlays.py

# 5. Recreate the API container (planner's run_compose.ps1 / usual recreate)

# 6. One-line live check — no-auth multi POST must be 401, not 200
curl.exe -sS -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:18888/v1/default/memories/recall -H "Content-Type: application/json" -d "{\"bank_ids\":[\"operator-joshf\"],\"query\":\"ping\"}"
```

Want: HTTP **401** and body `{"detail":"Authentication failed: ..."}` (wording comes from the tenant extension; live-measured was `Authentication failed: Invalid API key`).

Want not: HTTP **200** with `metadata.multi_bank.banks.operator-joshf.status == "error"` / `"recall failed for this bank"`.

Optional second probe (same 401):

```
curl.exe -sS -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:18888/v1/default/memories/recall -H "Content-Type: application/json" -H "Authorization: Bearer definitely-not-a-real-key" -d "{\"bank_ids\":[\"operator-joshf\"],\"query\":\"ping\"}"
```

Single-bank control (already 401 before this patch):

```
curl.exe -sS -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:18888/v1/default/banks/operator-joshf/memories/recall -H "Content-Type: application/json" -d "{\"query\":\"ping\"}"
```

## Upstream PR candidacy

**Yes — this belongs in the multi-bank PR itself** (`ace16881` / `feat(recall): multi-bank HTTP endpoint and MCP bank_ids`), not a follow-up.

`recall_multi_async` shipped `gather(..., return_exceptions=True)` and then only re-raised `OperationCancelledError` + (after 2026-08-10 P1#(3)) `OperationValidationError`. Tenant `AuthenticationError` was left in the soft-fail bucket. The design rule of record already says a denial the single-bank route maps to 401/403/422 must map the same way on the multi route. This is that gap, not a new policy.

An upstream PR should take the two engine hunks (up-front `_authenticate_tenant` + gather re-raise) and the tests. Do not copy this overlay patch verbatim onto `origin/main` if `recall_multi_async` has drifted; rebase the hunks.

## Code review (self)

**Must fix:** none found in this diff.

**Should fix / notes:**

- `bank_statuses: dict[str, dict]` in `recall_multi_async` is pre-existing; not introduced here.
- Imports of `AuthenticationError` / `OperationValidationError` stay inside the method, matching the surrounding engine style.
- HTTP unit test mocks `recall_multi_async`; it proves the existing handler mapping, not the gather path. The gather path is the orchestrator tests.
- `lint.sh` on this worktree is not a clean gate on Windows (eslint / mlx / fcntl). Targeted ruff on the touched files is the measurement.

## Where I think the design is wrong

1. **Double tenant auth is real (1 + N).** The brief said add up-front auth only if the single-bank structure allows it *without* a double-auth per bank. I could not skip the per-bank `_authenticate_tenant` inside `recall_async` without changing single-bank (schema ContextVar + "every engine method authenticates"). So I added one extra request-scoped call and left N per-bank calls. That matches single-bank-with-validator (precheck auth + `recall_async` auth). For a JWT/Supabase tenant extension this is N+1 verifies. If the planner wants exactly-once tenant auth, that needs a skip flag on `recall_async` — out of this slice.

2. **HTTP still only calls `_authenticate_tenant` when `_operation_validator is not None`.** That is why the live multi route reached gather at all (single-bank 401s because `recall_async` always auths; multi swallowed that). I did not change the HTTP precheck gate. The engine now covers HTTP-without-validator and MCP. If the planner wants the handler to auth even with no validator, that is a separate http.py hunk; the existing global 401 handler is already enough once the engine raises.

3. **Cancel still beats auth.** If gather returns both `OperationCancelledError` and `AuthenticationError`, the client gets 499. An unauthenticated disconnected client is arguably 401. I kept the 2026-08-10 cancel-first order rather than invent a new precedence. Say so if 401 should win.

4. **Empty `bank_ids` still returns 200 without auth** at the engine (early return before `_authenticate_tenant`). HTTP already 422s empty `bank_ids`. Fine unless someone calls the engine directly.

I did not silently "fix" 1–4.

## Commits

See `git log` on this branch after the two MB1f commits (engine+tests, then this report + overlay patch).
