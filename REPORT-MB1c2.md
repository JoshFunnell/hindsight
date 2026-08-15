# REPORT-MB1c2

Worktree: `D:\HQ_runtime\grok_worktrees\mb1c2-reranker-fix`
Branch: `mb1c2-reranker-fix` (off `mb1bd-engine-fixes` = `multi-bank-recall` @ `ace16881` + MB1bd commits)
Base: v0.9.0-based (`hindsight-api-slim` package version `0.9.0`; same family as the pinned live image)
Mode: code. No push. No self-merge. Live overlays / compose / container / `D:\HQ_runtime\patches\**` / `D:\HQ_runtime\*.py` not touched.

Grok output is evidence. Planner decides deploy-by-COPY.

## Commits

| order | sha | subject |
|---|---|---|
| 1 | `fd059ba7` | `fix(engine): give each reranker worker its own CrossEncoder` (includes tests) |
| 2 | `14364386` | `docs: add REPORT-MB1c2` |

## Design chosen: A (per-executor-thread model instances)

**Why A, not B.** The live crash is concurrent `CrossEncoder.predict` on one shared instance: ST 5.2.0 `predict` does `self.model.to(device)`, `self.model.eval()`, then `self.tokenizer(pairs, padding=True, truncation=True, return_tensors="pt")` on one HF fast tokenizer. `set_truncation_and_padding` + `encode_batch` are not safe under concurrent use. Design B (lock only around `tokenizer(...)`, inline the ST 5.2.0 batch loop) would keep 1x weights and still overlap the 5s forwards, but it re-implements ST internals and breaks on the next image bump. A is feasible here: `ThreadPoolExecutor` threads persist, `threading.local()` is per wrapper instance, and MiniLM is small. No blocker. Default = A.

**What changed** (`hindsight-api-slim/hindsight_api/engine/cross_encoder.py`, class `LocalSTCrossEncoder` only):

- Class-level `ThreadPoolExecutor` is unchanged in cap semantics (`max_workers` from `RERANKER_LOCAL_MAX_CONCURRENT`, last-writer-wins, **not** resized after create).
- The shared object is now **only** that executor. Each worker thread owns its own `CrossEncoder` (own tokenizer + weights).
- First use on a thread loads under class `_load_lock` so HF cache / torch init do not race.
- `to(device)` / `eval()` run once per instance at load (`_pin_eval_and_device`). ST `predict` still calls them every batch; after this they are idempotent writes on a **thread-private** model.
- `initialize()` still fail-fasts: it creates the pool, then barrier-warms **every existing worker** so a load failure is a startup `RuntimeError`, not a surprise on first recall. Worker replacement still lazy-loads.
- Warmup uses `executor._max_workers`, not the (possibly later, larger) `_max_concurrent`. Warming N>pool-size tasks on a smaller pool deadlocks a barrier — that is a new footgun this change would have introduced; a test covers it.
- Load failure **raises** (`RuntimeError` … "Refusing to share another thread's model"). `predict` never reads `self._model`. There is no silent fallback to a shared instance.
- Both `_predict_sync` arms (plain and `bucket_batching`) score through `_get_thread_model()`.

**What did not change.** `memory_engine.py` still has one `MemoryEngine`, one `self._cross_encoder_reranker = CrossEncoderReranker(cross_encoder=…)` (this tree: `:1815`; brief's `:1797` is the probe-era line). Rerank is still `await reranker_instance.rerank(...)` (this tree: `:5940–5982`; brief's `:5856` is similarly stale). `create_cross_encoder` still passes `member.local_max_concurrent`. Multi-bank fan-out still `asyncio.gather`s per-bank `recall_async`, so CE forwards still overlap up to `max_workers`.

## Memory measurement

Fresh process. Interpreter: `D:\HQ_runtime\grok_worktrees\mb1f-multibank-auth\.venv\Scripts\python.exe` (not the worktree `.venv` — that one has no `local-ml` extra). **Same versions as live:** sentence-transformers 5.2.0, transformers 5.12.1, tokenizers 0.22.2, torch 2.10.0+cpu. Model `cross-encoder/ms-marco-MiniLM-L-6-v2` from the host HF cache (`HF_HUB_OFFLINE=1`). Script: `C:\Temp\mb1c2_rss_measure.py`. Four distinct `id(model)` and four distinct `id(tokenizer)`.

Windows `GetProcessMemoryInfo` (this is **not** Linux container RSS):

| point | WorkingSet (RSS) | PrivateUsage (commit) |
|---|---|---|
| process start | 16.9 MiB | 9.4 MiB |
| after ST/torch import | 403.7 MiB | 1839.0 MiB |
| after 1 CrossEncoder | 422.6 MiB | 1972.0 MiB |
| after 2 | 433.1 MiB | 2069.1 MiB |
| after 3 | 443.4 MiB | 2166.0 MiB |
| after 4 | 453.2 MiB | 2262.3 MiB |

- Extra instances 2–4: **+30.6 MiB WorkingSet**, **+290.3 MiB PrivateUsage** (~97 MiB private each). The brief's "~90 MB fp32 each" matches **PrivateUsage**, not Windows WorkingSet (the working set is trimmed; committed pages for four copies of the weights are still ~97 MiB each).
- Versus today's single instance: plan on **~+290 MiB** resident-anonymous in the **Linux** container (RSS there tracks private dirty pages more closely than Windows WS). Live worker RSS at the incident was 3064 MB; +290 is ~10%.
- First instance is more expensive (+133 MiB private) than each extra (~97 MiB) — tokenizer/vocab + first CUDA/CPU allocator warmup sit in the first load.

Four `id(tokenizer)` values were distinct. Design A does **not** share the Rust tokenizer.

## RED / GREEN evidence

**Not reproduced as the live `TypeError: 'int' object is not callable`.** Two soaks, both clean:

- Planner: 4 threads x (6+12) concurrent `CrossEncoder.predict` (64 then 100 pairs) = 0 errors in ~2.5 min.
- This worktree: mixed `batch_size` 64 / 32 / 8 and mixed pair lengths, 4 threads x 30 iters, same ST 5.2.0 / transformers 5.12.1 / tokenizers 0.22.2 / torch 2.10.0+cpu stack, `C:\Temp\mb1c2_repro_attempt.py`, **417s, `CONCURRENT_ERRORS 0`**.

Treat the live 2026-08-15 15:33:08Z crash + the shared mutable tokenizer as the defect. **This change closes it structurally.** Do not claim a reproduction we did not get.

**GREEN (stub model, pytest, worktree `.venv` CPython 3.11.15):**

```
cd hindsight-api-slim
uv run pytest tests/test_local_cross_encoder.py --timeout=60 -q
# 23 passed, 1 skipped in 2.48s
```

Last lines from that run: `23 passed, 1 skipped in 2.48s` (xdist `-n 8`, default `addopts`). Serial `-n0` was also 22 passed + 1 skipped (before the pool-size warmup test).

| test | what it proves |
|---|---|
| `test_concurrent_predict_instance_exclusivity[legacy-False]` | Pre-change class (replica): one shared stub, `max_inside > 1` under a 4-thread Barrier. **This is the RED shape** of the old code. |
| `test_concurrent_predict_instance_exclusivity[current-True]` | New class: 4 distinct instances, each `max_inside == 1`, planted `self._model` **never** entered. Reverting `_predict_sync` to `self._model.predict` makes this parametrization fail. |
| `test_scores_match_single_instance_path` | Concurrent scores == serial scores == deterministic stub formula (order + values). |
| `test_plain_and_bucket_arms_use_per_thread_instance[False/True]` | Both arms call the per-thread instance; bucket arm sorts by length then restores caller order. |
| `test_load_failure_raises_and_does_not_share` | Loader exception → `RuntimeError`, `_initialized` stays False, planted shared mock never `predict`ed. |
| `test_load_none_raises_not_fallback` | Loader `None` → "Refusing to share", no fallback. |
| `test_warmup_matches_existing_pool_size_not_later_max` | `max_concurrent=4` on a 2-worker existing pool initializes in <5s and loads **2** instances (no barrier deadlock). |

**Slow real-MiniLM soak** (skip-by-default):

```
cd hindsight-api-slim
# PowerShell
$env:HS_RERANKER_STRESS="1"
uv run pytest tests/test_local_cross_encoder.py::TestLocalSTMiniLMStress::test_real_minilm_four_concurrent -v -n0 --timeout=600
```

Needs `sentence-transformers` in the env that runs it (worktree `.venv` does **not** have `local-ml`; the mb1f venv above does). Not run as GREEN in this worktree venv.

Existing `TestLocalSTCrossEncoder` / `TestFlashRankCrossEncoder` still pass (stubs go through `_load_model_instance` / `_initialized`).

## Overlay deliverable

**File to COPY** (this tree, v0.9.0-based — do **not** take `origin/main`'s `cross_encoder.py`):

`D:\HQ_runtime\grok_worktrees\mb1c2-reranker-fix\hindsight-api-slim\hindsight_api\engine\cross_encoder.py`

- **md5 (LF):** `8bfe31773a0c4cf48f856840ec85611a`
- **bytes (LF):** 77756
- Working tree was written LF. If a later checkout on Windows turns it CRLF, hash the LF form (`git show HEAD:hindsight-api-slim/hindsight_api/engine/cross_encoder.py` piped to md5) before mounting.

**Compose mount line** (mirror existing `../patches/hindsight/<file>:/app/api/hindsight_api/...:ro` style in `D:\HQ_runtime\hindsight\docker-compose.yml`):

```yaml
      - ../patches/hindsight/cross_encoder.py:/app/api/hindsight_api/engine/cross_encoder.py:ro
```

**PATCHES.md entry text** (planner writes this; this worktree does not touch `D:\HQ_runtime\patches\**`):

```
### cross_encoder.py — LocalSTCrossEncoder per-thread instances (MB1c2)
- **CLASS: OURS**
- **Base:** v0.9.0-based worktree `D:\HQ_runtime\grok_worktrees\mb1c2-reranker-fix` (branch `mb1c2-reranker-fix`, off `mb1bd-engine-fixes` = `multi-bank-recall` @ `ace16881` + MB1bd). Same base family as the pinned live image. Do not overlay `origin/main`'s file — `main` is ahead of v0.9.0 and this class is still the shared-model shape (see below).
- **Mounted over:** `/app/api/hindsight_api/engine/cross_encoder.py`
- **Content:** each reranker executor thread owns its own SentenceTransformers `CrossEncoder` (thread-local, load-locked). Closes the shared HF fast-tokenizer race that produced live `ValueError: Unable to create tensor` wrapping `TypeError: 'int' object is not callable` (2026-08-15 15:33:08Z). `max_workers` still comes from `HINDSIGHT_API_RERANKER_LOCAL_MAX_CONCURRENT` (default 4). Search marker: `per-thread model instances`.
- **md5 (LF):** `8bfe31773a0c4cf48f856840ec85611a`
- **Retire when:** a released, digest-pinned image contains an equivalent per-thread (or otherwise non-shared-tokenizer) `LocalSTCrossEncoder`.
- **Upstream status:** candidate PR against `vectorize-io/hindsight`. File from a branch rebased onto `origin/main`, not this overlay tree. `origin/main` `hindsight-api-slim/hindsight_api/engine/cross_encoder.py` (`git show origin/main:...`, tip `396f63aa`) has the **same LocalSTCrossEncoder structure** as v0.9.0 / this tree's pre-change class: class-level `ThreadPoolExecutor`, one `self._model`, `_predict_sync` calls `self._model.predict` from the pool, no `_load_lock` / `_thread_models`. The defect exists on `main`. Do not copy `main`'s file onto the v0.9.0 image.
```

**What to watch in the log after recreate**

```
Reranker: initializing local provider with model cross-encoder/ms-marco-MiniLM-L-6-v2
Reranker: local provider initialized (max_concurrent=4, per-thread model instances)
```

If the executor already existed (unusual): `..., per-thread model instances, using existing executor`. A per-thread load failure logs as `Failed to create a per-thread LocalSTCrossEncoder instance for thread 'reranker_…'` and **must** fail startup / that recall — never continue on a shared model.

Also watch: 3-bank MULTI wall-clock stays in the ~3–5s band (CE forwards still overlap). A jump to ~15s+ means the overlay did not load (still one shared CE plus an accidental whole-`predict` lock) or `RERANKER_LOCAL_MAX_CONCURRENT=1` is set.

## OPS-ONLY interim: `HINDSIGHT_API_RERANKER_LOCAL_MAX_CONCURRENT=1`

From this tree's code, not from a live experiment.

1. `config.py` reads the env into `reranker_local_max_concurrent` (default `DEFAULT_RERANKER_LOCAL_MAX_CONCURRENT = 4`).
2. `create_cross_encoder` (`cross_encoder.py` ~1712) passes `max_concurrent=member.local_max_concurrent` into `LocalSTCrossEncoder`.
3. `__init__` writes `LocalSTCrossEncoder._max_concurrent`.
4. `_ensure_executor` builds `ThreadPoolExecutor(max_workers=_max_concurrent)` **once**. Changing the env without a **new process** does nothing (executor is not resized). A recreate is required.
5. Every local `predict` is `run_in_executor(_executor, _predict_sync, pairs)` (`cross_encoder.py` `predict`).
6. Multi-bank `recall_multi_async` still `gather`s per-bank `recall_async`. Retrieval overlaps. Rerank does not: with `max_workers=1` the three `_predict_sync` calls **queue**.
7. Wall-clock ≈ `max(retrieval_i) + sum(CE_i)`.

Incident numbers (MB1c probe, 15:33:07Z trio): planner CE **5.106s** for 77 pairs; operator entered CE with **100** pairs (cap) and died ~0.35s into tokenize — scale 100/77 × 5.106 ≈ **6.63s** if it had finished; systems never logged `[4] Reranking`, unknown count, ~5s if planner-like. Sum CE ≈ **16.7s**. Retrieval overlap was ~1s. Counterfactual 3-bank wall ≈ **~18s** vs today's overlapped **~3–5s**.

Typical 2-bank MULTI today is **~3.5–4.6s**. `MAX_CONCURRENT=1` adds the **smaller** bank's CE time (often +1–2s on short queries; more on 100-pair / 500-token queries). A leftover in-flight recall (the 15:32:58 operator call) also occupies the single worker.

After design A is mounted, `MAX_CONCURRENT=1` is **not** required for correctness. Leave it at 4 to keep CE overlap.

## Upstream PR candidacy

Yes — this is a general local-reranker engine bug (any concurrent `recall_async`, including 2-bank MULTI). File against `vectorize-io/hindsight` from a **main-rebased** branch. `git show origin/main:hindsight-api-slim/hindsight_api/engine/cross_encoder.py` still has the shared-`_model` `LocalSTCrossEncoder` (confirmed: `_thread_models` absent, `self._model.predict` present, `_load_lock` absent). Tests in this worktree (`test_local_cross_encoder.py` additions) should travel with the PR.

## Where I think the design is wrong

1. **The brief said "lazily loaded".** I still eager-warm every worker inside `initialize()`. Reasons: fail-fast at startup (today's property), no 5th leftover instance on the init thread, first 3-bank recall does not pay 4 cold loads. Replacement threads stay lazy. If the planner wants the letter of "lazy", delete the `_warmup_executor_threads()` call — `_get_thread_model` already lazy-loads under the lock.
2. **Windows WorkingSet is the wrong planning number for the Linux container.** +30.6 MiB WS vs +290 MiB private for instances 2–4. Budget **~+290 MiB** RSS in `hindsight-api`.
3. **Probe MB1c recommended B (tokenizer-only lock)** to keep 1x memory and overlap forwards. This brief defaulted to A. A is more robust (forward + `to`/`eval` also unsynchronized; we do not have a proof that tokenizer-only is enough on every device) and does not fork ST. The cost is real memory. If the box is tight, `MAX_CONCURRENT=2` is a middle path the brief did not ask for.
4. **I did not get a RED tokenizer crash.** Closing structurally. A "we reproduced it" sentence would be a lie.
5. **Brief line numbers `:1797` / `:5856` are stale** on this tree (`:1815` construct, `:5940` rerank). Same wiring.
6. **`self._model` is still on the instance** and unused in production `predict`. Left so a planted mock in old tests / a mistaken reader does not get an `AttributeError`. Production never falls back to it. Slightly messy; deleting it is a follow-up.
7. **Startup is 4 serial HF loads** under `_load_lock` (warm cache: extra loads were <1s each in the RSS process). Cold cache on a new image will be slower than today's 1 load. That is the fail-fast trade.
8. **Class-level executor last-writer-wins without resize is pre-existing.** I did not fix it. I only stopped warmup from deadlocking when someone constructs a second wrapper with a larger `max_concurrent`.
