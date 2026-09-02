"""Proof 1: every module the port touches must import against a main tree.

The four the brief names, plus the three local-only engine modules those reach
through a lazy in-function import (compaction, the identifier gate, multi-bank
recall). A package-level probe does not execute a lazy import, so listing them
here is the difference between "the package imports" and "the port's own code
imports" — mental_model_compaction's stale count_cl100k_tokens was invisible to
the first shape and was caught by the test suite instead.
"""

import hindsight_api.api.http  # noqa: F401
import hindsight_api.engine.identifier_retention  # noqa: F401
import hindsight_api.engine.memory_engine  # noqa: F401
import hindsight_api.engine.mental_model_compaction  # noqa: F401
import hindsight_api.engine.multi_bank_recall  # noqa: F401
import hindsight_api.engine.reflect.delta_ops  # noqa: F401
import hindsight_api.mcp_tools  # noqa: F401

print("IMPORT OK")
