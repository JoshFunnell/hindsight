## Per-thread LocalSTCrossEncoder instances

### Motivation

`LocalSTCrossEncoder` shared one SentenceTransformers `CrossEncoder` (and its
HuggingFace fast tokenizer) across a class-level `ThreadPoolExecutor`. Concurrent
`predict()` races `set_truncation_and_padding` + `encode_batch`. Observed live
(2026-08-15) as `ValueError: Unable to create tensor` wrapping
`TypeError: 'int' object is not callable` during a 3-bank recall fan-out.

This is an engine bug, independent of multi-bank recall. Multi-bank made the
race more likely (N parallel CE windows) but a single-bank host with
`max_concurrent>1` has the same shape.

### Change

Each executor thread owns its own `CrossEncoder`, loaded under a lock.
`to(device)` / `eval()` run once at load. Load failures raise; there is no
fallback to a shared instance. `initialize()` warms every pool worker so a
load failure is a startup error and first recall does not pay N cold loads.

`max_workers` is still `HINDSIGHT_API_RERANKER_LOCAL_MAX_CONCURRENT` (default 4).
Memory cost is ~+290 MiB private for 3 extra MiniLM copies (measured on the
overlay).

### Tests

`tests/test_local_cross_encoder.py`:
- Existing mocked predict / bucket-batching / FlashRank tests still pass
  (`_make_encoder` stubs the per-thread loader).
- New isolation tests: exclusivity vs a legacy shared-model subclass (the
  attacking RED on the old design), score identity, both predict arms, load
  failure, warmup vs a smaller existing pool.
- Optional real-MiniLM soak: `HS_RERANKER_STRESS=1`.

### Non-goals

- Not a multi-bank change. File separately from the multi-bank recall PR.
- Does not change the public reranker provider interface.
