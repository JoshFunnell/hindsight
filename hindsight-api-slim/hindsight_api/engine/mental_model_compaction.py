"""Honour a mental model's ``max_tokens`` without dropping anchored identifiers.

WHY
---
``max_tokens`` is stored on every mental model (256-8192) and is used as the
*generation* output cap, but nothing applies it to the *stored* document.
Measured on bank operator-joshf (2026-08-18): models with max_tokens=1536 or
4096 hold 85-435 KB of markdown, and mental_model_history is monotone-growing
across the 50 retained versions of the three largest models. The write-time
identifier-retention gate (``engine/identifier_retention.py``) refuses a
refresh that *drops* three or more identifiers, so it cannot stop growth --
and a naive truncate would trip that gate on the next refresh.

The first version of this module treated the budget as a target to undershoot
and kept identifier-bearing *lines* first, then collapsed to a
``## Preserved identifiers`` bullet list whenever those lines still overflowed.
Dry-runs on the live bank (2026-08-18 s31) produced husks at 5-17% of budget
(16_340 -> 832 against 1536; 121_091 -> 5_300 against 8192) that were no
longer mental models. That collapse is gone.

This module is the write-time size *ceiling*: if the document already fits it
is left unchanged; if it does not, the oldest / most superseded prose is
dropped until the remainder plus a packed identifier sentence fits, landing
close under the ceiling. Identifiers the shared taxonomy extracts are still
kept (the operator's rule). If the protected identifiers *alone* exceed the
budget, prose-hosted identifiers are kept first, then the remainder in taxonomy
order until the budget is met; excess identifiers are dropped, reported in
identifiers_lost, and named in the warning. A compacted document never persists
over budget.

The identifier taxonomy is imported from ``identifier_retention`` on purpose
-- two instruments that disagreed about what counts as an identifier would
contradict each other on the same write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .identifier_retention import (
    count_prose_and_residue,
    extract_identifiers,
    protected_identifiers,
    strip_salvage,
)
from .reflect.tokenization import count_prompt_tokens

# Live operator-joshf mental models are current-state-first: the opening is
# the standing rule, the tail is superseded history. Date/session averages
# flipped that (s31) because later restatements still carry today's date.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[#*])")

#: Compacted output should land in [this fraction of budget, budget] whenever
#: the input is larger than the budget. Undershooting is a last resort when
#: the next whole sentence will not fit.
_FLOOR_RATIO = 0.6


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of honouring a token budget on one document."""

    content: str
    tokens_before: int
    tokens_after: int
    changed: bool
    identifiers_kept: int
    identifiers_lost: int
    dropped_prose_units: int
    identifiers_exceed_budget: bool
    warning: str | None
    identifiers_lost_set: frozenset[str] = frozenset()

    def as_metadata(self) -> dict[str, int | bool]:
        """Machine-readable slice stored on reflect_response.size_budget."""
        return {
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "changed": self.changed,
            "identifiers_kept": self.identifiers_kept,
            "identifiers_lost": self.identifiers_lost,
            "dropped_prose_units": self.dropped_prose_units,
            "identifiers_exceed_budget": self.identifiers_exceed_budget,
        }


@dataclass(frozen=True)
class _Unit:
    """One keep-or-drop atom: a paragraph, a sentence, or a heading."""

    text: str
    kind: Literal["heading", "block", "sentence"]
    identifiers: frozenset[str]
    tokens: int


def _render_kept_identifier(name: str) -> str:
    """Write an identifier so the shared taxonomy still extracts it.

    Port matches are ``\\b:\\d{4,5}\\b`` and need a word character before the
    colon (see identifier_retention.ExtractionTests). A bare ``:18888`` in a
    comma list would be dropped on the next gate pass.
    """
    if name.startswith(":"):
        return f"localhost{name}"
    return name


def _packed_identifier_sentences(ids: set[str]) -> str:
    """Prose sentences that still carry every identifier.

    A bullet list of bare identifiers is not a mental model (s31 dry-runs).
    Packing into ordinary sentences keeps the identifier gate happy without
    replacing the document with a husk.
    """
    if not ids:
        return ""
    rendered = [_render_kept_identifier(name) for name in sorted(ids)]
    chunk_size = 20
    sentences: list[str] = []
    for start in range(0, len(rendered), chunk_size):
        chunk = rendered[start : start + chunk_size]
        if len(chunk) == 1:
            sentences.append(f"Also recorded (compaction salvage): {chunk[0]}.")
        elif len(chunk) == 2:
            sentences.append(f"Also recorded (compaction salvage): {chunk[0]} and {chunk[1]}.")
        else:
            sentences.append(f"Also recorded (compaction salvage): {', '.join(chunk[:-1])}, and {chunk[-1]}.")
    return "\n\n".join(sentences)


def _identifiers_document(ids: set[str]) -> str:
    """Last-resort document when even packed sentences cannot be hosted.

    Kept so a port-only identifier still round-trips through the shared
    taxonomy. Not used on the happy path.
    """
    if not ids:
        return ""
    lines = "\n".join(f"- {_render_kept_identifier(name)}" for name in sorted(ids))
    return f"## Preserved identifiers\n\n{lines}"


def _split_blocks(text: str) -> list[str]:
    """Blank-line blocks, but never split inside a fenced code block."""
    if not text:
        return []
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and stripped == "" and buf:
            chunk = "\n".join(buf)
            if chunk != "":
                blocks.append(chunk)
            buf = []
            continue
        buf.append(line)
    if buf:
        chunk = "\n".join(buf)
        if chunk != "":
            blocks.append(chunk)
    return blocks


def _split_sentences(block: str) -> list[str]:
    """Whole sentences. Headings, tables, and fences stay atomic."""
    stripped = block.lstrip()
    if stripped.startswith("#") or stripped.startswith("```") or stripped.startswith("|"):
        return [block]
    parts = _SENTENCE_RE.split(block.strip())
    return [part.strip() for part in parts if part.strip()]


def _kind_of(text: str, sentence_split: bool) -> Literal["heading", "block", "sentence"]:
    if text.lstrip().startswith("#"):
        return "heading"
    if sentence_split:
        return "sentence"
    return "block"


def _make_unit(text: str, *, sentence_split: bool = False) -> _Unit:
    return _Unit(
        text=text,
        kind=_kind_of(text, sentence_split),
        identifiers=frozenset(extract_identifiers(text)),
        tokens=count_prompt_tokens(text),
    )


def _units_for_budget(text: str, budget: int) -> list[_Unit]:
    """Blocks, sentence-split only when a single block cannot fit the budget."""
    units: list[_Unit] = []
    for block in _split_blocks(text):
        block_tokens = count_prompt_tokens(block)
        if block_tokens > budget:
            sentences = _split_sentences(block)
            if len(sentences) == 1:
                units.append(_make_unit(block))
            else:
                units.extend(_make_unit(sentence, sentence_split=True) for sentence in sentences)
        else:
            units.append(_make_unit(block))
    return units


def _join_units(units: list[_Unit]) -> str:
    if not units:
        return ""
    parts: list[str] = [units[0].text]
    for prev, current in zip(units, units[1:]):
        if prev.kind == "sentence" and current.kind == "sentence":
            parts.append(" ")
        else:
            parts.append("\n\n")
        parts.append(current.text)
    return "".join(parts)


def _prefix(units: list[_Unit], length: int) -> list[_Unit]:
    if length <= 0:
        return []
    return units[:length]


def _compose(kept: list[_Unit], packed: str) -> str:
    body = _join_units(kept)
    if not packed:
        return body
    if not body.strip():
        return packed
    return body.rstrip() + "\n\n" + packed


def _taxonomy_rank(token: str) -> int:
    """Classify token into priority groups for salvage packing.

    Paths/filenames/URLs/env vars/registry IDs/UUIDs/hex come before dates/versions/ports.
    """
    if (
        re.match(r"^[A-Za-z]:\\[\w\\.\-]+$", token)
        or re.match(r"^(?:~|/)[\w/.\-]*/[\w.\-]+$", token)
        or re.match(r"^[\w-]+\.(?:py|md|json|jsonl|yml|yaml|toml|rs|ts|tsx|cmd|ps1|vbs|sh|exe)$", token)
    ):
        return 0
    if re.match(r"^https?://", token):
        return 1
    if re.match(r"^(?:HINDSIGHT_|HQ_)[A-Z0-9_]+$", token):
        return 2
    if re.match(r"^(?:G|INV|PROP|HQ|BUG|PKT|REF|WO)-?\d+", token):
        return 3
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", token, re.IGNORECASE):
        return 4
    if re.match(r"^[0-9a-f]{7,40}$", token, re.IGNORECASE) and not re.match(r"^[0-9]{7,}$", token):
        return 5
    if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return 6
    if re.match(r"^v\d+\.\d+(?:\.\d+)?$", token):
        return 7
    if re.match(r"^:\d{4,5}$", token) or re.match(r"^\d{4,5}/(?:tcp|udp)$", token):
        return 8
    return 9


def _pack_for(source_ids: set[str], kept_text: str) -> str:
    missing = source_ids - extract_identifiers(kept_text)
    if not missing:
        return ""
    packed = _packed_identifier_sentences(missing)
    probe = kept_text + ("\n\n" + packed if packed else "")
    stubborn = source_ids - extract_identifiers(probe)
    if not stubborn:
        return packed
    extra = _identifiers_document(stubborn)
    return packed + "\n\n" + extra if packed else extra


def _pack_ordered_to_budget(
    kept: list[_Unit],
    source_ids: set[str],
    budget: int,
) -> tuple[str, set[str]]:
    """Pack missing identifiers in taxonomy order up to the token budget.

    Returns (packed_string, dropped_ids_set).
    """
    kept_body = _join_units(kept)
    already_hosted = extract_identifiers(kept_body)
    missing = source_ids - already_hosted
    if not missing:
        return "", set()

    ordered = sorted(missing, key=lambda x: (_taxonomy_rank(x), x))
    full_packed = _packed_identifier_sentences(set(ordered))
    if count_prompt_tokens(_compose(kept, full_packed)) <= budget:
        return full_packed, set()

    # Binary search for maximum prefix of ordered that fits
    low = 0
    high = len(ordered)
    best_k = 0
    while low <= high:
        mid = (low + high) // 2
        test_ids = set(ordered[:mid]) if mid > 0 else set()
        test_packed = _packed_identifier_sentences(test_ids) if test_ids else ""
        if count_prompt_tokens(_compose(kept, test_packed)) <= budget:
            best_k = mid
            low = mid + 1
        else:
            high = mid - 1

    to_pack = set(ordered[:best_k]) if best_k > 0 else set()
    dropped = set(ordered[best_k:])
    packed = _packed_identifier_sentences(to_pack) if to_pack else ""
    return packed, dropped


def _fits(kept: list[_Unit], packed: str, budget: int) -> bool:
    return count_prompt_tokens(_compose(kept, packed)) <= budget


def _longest_fitting_prefix(units: list[_Unit], budget: int, source_ids: set[str]) -> list[_Unit]:
    """Largest leading window whose body + packed leftover identifiers fit."""
    best: list[_Unit] = []
    for length in range(1, len(units) + 1):
        kept = _prefix(units, length)
        body = _join_units(kept)
        if count_prompt_tokens(body) > budget:
            break
        packed = _pack_for(source_ids, body)
        if count_prompt_tokens(_compose(kept, packed)) <= budget:
            best = kept

    if best:
        return best

    # If all identifiers cannot fit (identifiers alone exceed budget),
    # prioritize prose up to ~75% of budget to leave room for ordered salvage packing
    prose_target = max(1, int(budget * 0.75))
    for length in range(1, len(units) + 1):
        kept = _prefix(units, length)
        body = _join_units(kept)
        if count_prompt_tokens(body) <= prose_target:
            best = kept
        elif not best and count_prompt_tokens(body) <= budget:
            best = kept
            break
        else:
            break

    return best


def _fill_toward_budget(
    units: list[_Unit],
    kept: list[_Unit],
    budget: int,
    source_ids: set[str],
) -> list[_Unit]:
    """Take leading sentences of the next dropped block if the whole block will not fit."""
    selected = {id(unit) for unit in kept}
    result = list(kept)
    candidates = [unit for unit in units if id(unit) not in selected]
    for unit in candidates:
        trial = result + [unit]
        packed = _pack_for(source_ids, _join_units(trial))
        if _fits(trial, packed, budget):
            result = trial
            continue
        if count_prompt_tokens(_join_units(trial)) <= max(1, int(budget * 0.75)):
            result = trial
            continue
        if unit.kind != "block":
            break
        for sentence in _split_sentences(unit.text):
            piece = _make_unit(sentence, sentence_split=True)
            trial = result + [piece]
            packed = _pack_for(source_ids, _join_units(trial))
            if _fits(trial, packed, budget):
                result = trial
                continue
            if count_prompt_tokens(_join_units(trial)) <= max(1, int(budget * 0.75)):
                result = trial
                continue
            return result
        return result
    return result


def _unchanged(text: str, tokens_before: int, warning: str | None = None) -> CompactionResult:
    ids = extract_identifiers(text)
    return CompactionResult(
        content=text,
        tokens_before=tokens_before,
        tokens_after=tokens_before,
        changed=False,
        identifiers_kept=len(ids),
        identifiers_lost=0,
        dropped_prose_units=0,
        identifiers_exceed_budget=False,
        warning=warning,
        identifiers_lost_set=frozenset(),
    )


def compact_to_budget(content: str, max_tokens: int) -> CompactionResult:
    """Shrink ``content`` to ``max_tokens`` (cl100k) while keeping identifiers.

    The budget is a ceiling, not a target to undershoot. Documents that
    already fit are returned unchanged (idempotent). Oversized documents
    keep the leading contiguous prose (current-state-first living documents)
    and drop the oldest tail. Identifiers that lived only in the dropped
    half are re-hosted as ordinary ``Also recorded (compaction salvage): …``
    sentences so the identifier-retention gate still sees a keep.

    ``max_tokens <= 0`` is treated as "no budget" and returns the input
    unchanged -- a zero/negative cap must not empty the document.

    On any internal failure the input is returned unchanged with a warning.
    """
    text = content or ""
    tokens_before = count_prompt_tokens(text)
    if max_tokens <= 0 or tokens_before <= max_tokens:
        return _unchanged(text, tokens_before)

    try:
        return _compact_oversized(text, tokens_before, max_tokens)
    except Exception as exc:  # noqa: BLE001 — write path must not raise
        return _unchanged(
            text,
            tokens_before,
            warning=f"size-budget: compact failed ({type(exc).__name__}: {exc}); left document unchanged.",
        )


def _compact_oversized(text: str, tokens_before: int, max_tokens: int) -> CompactionResult:
    n_salvage, n_prose = count_prose_and_residue(text)
    if n_prose == 0:
        return _unchanged(
            text,
            tokens_before,
            warning=f"size-budget: document is residue-only ({n_salvage} salvage paragraphs, 0 prose); left unchanged -- repair by hand (mm_compact_repair.py refuses too).",
        )

    clean_text = strip_salvage(text)
    source_ids = protected_identifiers(text)
    units = _units_for_budget(clean_text, max_tokens)

    kept = _longest_fitting_prefix(units, max_tokens, source_ids) if units else []
    if units:
        kept = _fill_toward_budget(units, kept, max_tokens, source_ids)

    packed, dropped_ids = _pack_ordered_to_budget(kept, source_ids, max_tokens)
    compacted = _compose(kept, packed)

    # Trim kept units if compose is still over ceiling
    while kept and count_prompt_tokens(compacted) > max_tokens:
        kept = kept[:-1]
        packed, dropped_ids = _pack_ordered_to_budget(kept, source_ids, max_tokens)
        compacted = _compose(kept, packed)

    if count_prompt_tokens(compacted) > max_tokens:
        kept = []
        packed, dropped_ids = _pack_ordered_to_budget([], source_ids, max_tokens)
        compacted = packed

    tokens_after = count_prompt_tokens(compacted)

    # Belt: output may never exceed max(4 * max_tokens chars, 65536)
    char_ceiling = max(4 * max_tokens, 65536)
    ceiling_cut = False
    if len(compacted) > char_ceiling:
        cut_pos = compacted.rfind("\n\n", 0, char_ceiling)
        if cut_pos != -1:
            compacted = compacted[:cut_pos].rstrip()
        else:
            compacted = compacted[:char_ceiling].rstrip()
        ceiling_cut = True
        ids_after = extract_identifiers(compacted)
        dropped_ids = source_ids - ids_after
        tokens_after = count_prompt_tokens(compacted)

    # Floor guard: if the compacted text has no prose paragraph OR not compacted.strip(), return unchanged
    compacted_salvage, compacted_prose = count_prose_and_residue(compacted)
    if not compacted.strip() or compacted_prose == 0:
        return _unchanged(
            text,
            tokens_before,
            warning=f"size-budget: document is residue-only ({n_salvage} salvage paragraphs, 0 prose); left unchanged -- repair by hand (mm_compact_repair.py refuses too).",
        )

    ids_after = extract_identifiers(compacted)
    lost_from_prose = source_ids - ids_after
    all_dropped_ids = lost_from_prose | dropped_ids
    identifiers_lost_set = frozenset(all_dropped_ids)
    identifiers_lost = len(identifiers_lost_set)

    kept_ids_set = {id(unit) for unit in kept}
    dropped_prose = sum(1 for unit in units if id(unit) not in kept_ids_set and not unit.identifiers)

    identifiers_exceed_budget = bool(all_dropped_ids) or (
        count_prompt_tokens(_packed_identifier_sentences(source_ids)) > max_tokens if source_ids else False
    )

    warning = (
        f"size-budget: compacted {tokens_before} -> {tokens_after} tokens "
        f"(budget {max_tokens}); kept {len(ids_after)} identifier(s), "
        f"dropped {dropped_prose} prose block(s)."
    )

    if all_dropped_ids:
        dropped_sorted = sorted(all_dropped_ids)
        shown = ", ".join(dropped_sorted[:10])
        if len(dropped_sorted) > 10:
            shown += f", +{len(dropped_sorted) - 10} more"
        warning += f" Dropped {len(dropped_sorted)} identifier(s): {shown}."
    elif identifiers_exceed_budget:
        warning += " Identifiers alone exceed the budget."

    if ceiling_cut:
        warning += f" Cut content at char ceiling {char_ceiling}."

    tokens_ratio = tokens_after / max_tokens if max_tokens else 0.0
    if tokens_after <= max_tokens and tokens_ratio < _FLOOR_RATIO and kept:
        warning += (
            f" Landed at {tokens_ratio:.0%} of budget; could not reach "
            f"{_FLOOR_RATIO:.0%} without splitting a whole sentence."
        )

    return CompactionResult(
        content=compacted,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        changed=compacted != text,
        identifiers_kept=len(ids_after),
        identifiers_lost=identifiers_lost,
        dropped_prose_units=dropped_prose,
        identifiers_exceed_budget=identifiers_exceed_budget,
        warning=warning,
        identifiers_lost_set=identifiers_lost_set,
    )
