"""Write-time identifier-retention gate for mental-model refreshes.

WHY
---
A refresh can silently drop anchored identifiers -- dates, file paths, commit
shas, registry ids, env vars -- from a mental model while the document still
GROWS, so no length or emptiness check can see it. Measured over 364 real
refresh events on one production bank:

    lost 0 identifiers: 337 events (92.6%)
    lost 1:              17
    lost 2:               3
    lost 3 or more:       7   <-- the class worth refusing

One of those seven dropped ``TRIAL-CLOSE-RUNBOOK.md``,
``TRIAL-EVIDENCE-PLAN.md``, ``build_spec_a_overlay.py`` and a commit sha from a
single model in one write. Losing one or two is frequently LEGITIMATE churn --
a superseded date, a path that genuinely stopped being relevant -- which is why
this gate is GRADED rather than absolute: refusing on any loss at all would
fail roughly one refresh in fourteen and quickly be switched off.

This is a graded sibling of the existing placeholder/empty-retrieval guard on
the same write path: same trigger condition (only when there is existing real
content to clobber), same preserve-and-fail shape, under its own outcome value
(``refresh_failed_identifier_retention``) so the refusal is never mistaken for
an empty LLM answer. It runs AFTER the #3112 delta-window guard: a failed
delta keeps its own, more precise refusal instead of being relabelled as
identifier loss.

The identifier taxonomy is deliberately IDENTICAL to the offline retention
probe's. Two instruments that disagree about what counts as an identifier
would produce contradictory evidence about the same event.
"""

from __future__ import annotations

import os
import re

#: The audit taxonomy, one compiled alternation. Order matters only for
#: overlap (longer, more specific first). Kept byte-identical to the offline
#: probe's pattern on purpose -- see the module docstring.
_IDENTIFIER_RE = re.compile(
    r"(?:[A-Za-z]:\\[\w\\.\-]+"  # windows paths
    # env-rooted paths ($HOME/x, ${VAR}/x, %VAR%/x) -- listed BEFORE the
    # posix branch so the whole token wins at the "$"/"%" position.
    r"|(?:\$\{?[A-Za-z_]\w*\}?|%[A-Za-z_]\w*%)(?:/[\w.\-]+)+"
    # posix-ish paths. The letters-only lookbehind stops the branch firing
    # MID-WORD on slash-separated word lists in prose ("INV/PROP/G/HQ rows"
    # minted "/PROP/G/HQ"; "Claude/Fable/Opus" minted "/Fable/Opus"): S105
    # 2026-09-02 repos/repo-slices was refused 4x on three such fragments
    # from its own disclaimer sentences. Digits stay allowed before the
    # slash so port-rooted routes (":8790/stm/jobs") keep their tail.
    # Measured 2026-09-02 over all 72 live models (scratch regex_probe3.py,
    # pre-change file as baseline): 133 of 2,975 protected tokens stop
    # matching, 36 keep their file name via the extension branch, ~5 of the
    # other 97 were path-like (e.g. ".opencode/skills/hq-finish",
    # "docs/status/hq_console.html"), and 29 real file names the old branch
    # had swallowed whole ("REF-operator-doctrine.md") become protected.
    r"|(?<![A-Za-z_])(?:~|/)[\w/.\-]*/[\w.\-]+"
    r"|[\w-]+\.(?:py|md|json|jsonl|yml|yaml|toml|rs|ts|tsx|cmd|ps1|vbs|sh|exe)\b"
    r"|https?://[^\s)\"'\]]+"  # urls
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # uuids
    r"|\b[0-9a-f]{7,40}\b(?<![0-9]{7})"  # hex ids (not pure digits)
    r"|\b(?:G|INV|PROP|HQ|BUG|PKT|REF|WO)-?\d+[\w-]*"  # registry ids
    r"|\bHINDSIGHT_[A-Z0-9_]+|\bHQ_[A-Z0-9_]+"  # env vars
    r"|\b\d{4}-\d{2}-\d{2}\b"  # dates
    r"|\bv\d+\.\d+(?:\.\d+)?\b"  # versions
    r"|\b:\d{4,5}\b|\b\d{4,5}/(?:tcp|udp)\b"  # ports
    r")"
)

SALVAGE_SENTINEL = "Also recorded (compaction salvage):"
LEGACY_SALVAGE = "Also recorded:"
_PRESERVED_HEADING_RE = re.compile(r"^#{1,6}\s+preserved identifiers\s*$", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
RESIDUE_BULLET = re.compile(r"^\s*[-*]\s+\S+\s*$")

ENV_IDENTIFIER_LOSS_REFUSE = "HINDSIGHT_API_MENTAL_MODEL_IDENTIFIER_LOSS_REFUSE"

#: Refuse a refresh that drops this many DISTINCT identifiers. Default 3 from
#: the distribution above: it blocks the 7 catastrophic events (1.9% of
#: refreshes) and lets the 20 one-or-two-identifier events (5.5%) through with
#: a warning. 0 disables refusal entirely (warn-only).
DEFAULT_IDENTIFIER_LOSS_REFUSE = 3

#: Cap on how many lost identifiers are named in a warning before it is
#: summarised. The warning exists to be read.
_MAX_NAMED = 10


def refuse_threshold() -> int:
    """Read the refusal threshold from the environment, clamped at >= 0.

    Read per call rather than at import so a running worker picks up a change
    without a restart, and so tests can set it without reloading the module.
    A non-numeric value falls back to the default rather than raising: this
    sits on a write path, and a typo'd env var must not break refreshes.
    """
    raw = os.getenv(ENV_IDENTIFIER_LOSS_REFUSE)
    if raw is None or not raw.strip():
        return DEFAULT_IDENTIFIER_LOSS_REFUSE
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return DEFAULT_IDENTIFIER_LOSS_REFUSE


def _normalize_token(token: str) -> str | None:
    """Normalise an extracted token: strip trailing punctuation and drop empty paths."""
    s = token
    while True:
        stripped = s.rstrip(".,;:")
        while stripped:
            last = stripped[-1]
            if last == ")" and stripped.count("(") < stripped.count(")"):
                stripped = stripped[:-1]
            elif last == "]" and stripped.count("[") < stripped.count("]"):
                stripped = stripped[:-1]
            elif last == "'" and stripped.count("'") % 2 == 1:
                stripped = stripped[:-1]
            elif last == '"' and stripped.count('"') % 2 == 1:
                stripped = stripped[:-1]
            else:
                break
        if stripped == s:
            break
        s = stripped

    if not s:
        return None

    # Drop a path match with no \w segment left (`~/...`, `~/`, `/`, `C:\`, etc.)
    if "/" in s or "\\" in s:
        segments = re.split(r"[/\\]", s)
        has_w_seg = False
        for seg in segments:
            if seg == "~" or (len(seg) == 2 and seg[1] == ":"):
                continue
            if re.search(r"\w", seg):
                has_w_seg = True
                break
        if not has_w_seg:
            return None

    return s


def extract_identifiers(text: str | None) -> set[str]:
    """Every distinct identifier in ``text``. Set semantics are the point:
    an identifier that MOVES within the document counts as kept."""
    if not text:
        return set()
    raw_matches = _IDENTIFIER_RE.findall(text)
    result: set[str] = set()
    for m in raw_matches:
        norm = _normalize_token(m)
        if norm:
            result.add(norm)
    return result


def _split_markdown_blocks(text: str) -> list[str]:
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


def strip_salvage(text: str | None) -> str:
    """Remove compaction salvage paragraphs and legacy preserved-identifiers blocks.

    Removes:
    - paragraphs starting with 'Also recorded (compaction salvage):'
    - legacy paragraphs starting with 'Also recorded:'
    - '## Preserved identifiers' heading and all content up to the next heading
    - blocks consisting purely of residue bullets
    """
    if not text:
        return ""
    blocks = _split_markdown_blocks(text)
    kept_blocks: list[str] = []
    in_preserved_section = False

    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        first_line = lines[0].strip()

        if _PRESERVED_HEADING_RE.match(first_line):
            in_preserved_section = True
            continue

        if in_preserved_section:
            if _ANY_HEADING_RE.match(first_line):
                in_preserved_section = False
            else:
                continue

        if first_line.startswith(SALVAGE_SENTINEL) or first_line.startswith(LEGACY_SALVAGE):
            continue

        if all(RESIDUE_BULLET.match(ln.strip()) for ln in lines):
            continue

        kept_blocks.append(block)

    return "\n\n".join(kept_blocks)


def count_prose_and_residue(text: str | None) -> tuple[int, int]:
    """Count salvage (residue) and prose paragraphs in ``text``.

    Returns ``(residue_count, prose_count)``.
    A paragraph is one block from markdown block splitting.
    Residue: salvage sentences, preserved-identifier sections, residue bullets.
    Heading-only blocks are ignored in the prose count.
    Prose: non-empty blocks that are neither residue nor heading-only.
    """
    if not text:
        return 0, 0
    blocks = _split_markdown_blocks(text)
    salvage_count = 0
    prose_count = 0
    in_preserved = False
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        first_line = lines[0]
        if _PRESERVED_HEADING_RE.match(first_line):
            in_preserved = True
            salvage_count += 1
            continue
        if in_preserved:
            if _ANY_HEADING_RE.match(first_line):
                in_preserved = False
            else:
                salvage_count += 1
                continue
        if first_line.startswith(SALVAGE_SENTINEL) or first_line.startswith(LEGACY_SALVAGE):
            salvage_count += 1
            continue
        if all(RESIDUE_BULLET.match(ln) for ln in lines):
            salvage_count += 1
            continue
        if all(ln.startswith("#") for ln in lines):
            continue
        prose_count += 1
    return salvage_count, prose_count


def protected_identifiers(previous: str | None) -> set[str]:
    """Identifiers present in the non-salvage prose of ``previous``."""
    return extract_identifiers(strip_salvage(previous))


def lost_identifiers(before: str | None, after: str | None) -> set[str]:
    """Identifiers present in non-salvage prose of ``before`` and absent from ``after``."""
    return protected_identifiers(before) - extract_identifiers(after)


def format_warning(lost: set[str]) -> str:
    """A warning that names the lost identifiers verbatim.

    Verbatim matters: "3 identifiers lost" sends a human digging through two
    document versions, whereas the names usually make the cause obvious at a
    glance.
    """
    names = sorted(lost)
    shown = ", ".join(names[:_MAX_NAMED])
    if len(names) > _MAX_NAMED:
        shown += f", +{len(names) - _MAX_NAMED} more"
    return f"identifier-retention: refresh dropped {len(names)} identifier(s) present in the previous content: {shown}."


def evaluate(
    previous_content: str | None,
    candidate_content: str | None,
    has_delta_baseline: bool,
    threshold: int | None = None,
    exempt: set[str] | None = None,
) -> tuple[bool, str | None]:
    """Grade one candidate write.

    Returns ``(should_refuse, warning_or_None)``.

    ``has_delta_baseline`` false means there is no existing real content to
    clobber -- a bootstrap write over an empty or PENDING model cannot "lose"
    anything, and blocking it would stop a model ever being populated. That is
    the same condition the sibling placeholder guard uses.

    ``exempt`` contains identifiers reported dropped by the compactor. These
    are subtracted before the refusal threshold test, but still named in the
    warning so the loss remains visible in logs without refusing the write.

    A threshold of 0 never refuses but still warns, so the signal stays
    visible while the refusal is disabled.
    """
    if not has_delta_baseline:
        return False, None
    lost = lost_identifiers(previous_content, candidate_content)
    if not lost:
        return False, None
    limit = refuse_threshold() if threshold is None else max(0, threshold)
    warning = format_warning(lost)
    exempt_set = exempt if exempt is not None else set()
    unexempt_lost = lost - exempt_set
    should_refuse = limit > 0 and len(unexempt_lost) >= limit
    return should_refuse, warning
