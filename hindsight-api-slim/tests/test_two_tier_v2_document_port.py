"""The two-tier delta path on schema v2 — the properties the main port had to preserve.

These are the `two_tier_probe.py` expectations as a unit test. The probe ran against a
live 0.9.1 container and checked schema v1 documents; the port moved the fast path onto
upstream's v2 structured document, where blocks are opaque markdown fragments addressed
by a persisted id instead of typed blocks addressed by index.

Deliberately no database and no LLM: every property below is about the document
algebra the delta fast path rests on, and each one failed on the pre-port tree for a
different reason — the v1 symbols the fast path imported no longer exist, the stored
structure it validated is cleared by migration `d1e2f3a4b5c6`, and an operation
addressing a block by index has no meaning in v2.
"""

import unittest

from hindsight_api.engine.memory_engine import _load_delta_baseline
from hindsight_api.engine.reflect.delta_ops import apply_operations, parse_delta_operation_list
from hindsight_api.engine.reflect.structured_doc import (
    SCHEMA_VERSION,
    render_document,
    split_markdown,
    structured_document_from_stored,
)

DOC = """# Routing

Opus 5 takes long-context document work.

Terra takes high-volume work.

# Holds

The Grok lane is on hold.
"""


class TestV2DocumentRoundTrip(unittest.TestCase):
    """render(split(markdown)) is the identity the delta baseline depends on."""

    def test_split_render_round_trip_and_version(self):
        doc = split_markdown(DOC)
        self.assertEqual(doc.version, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(render_document(doc), DOC)

    def test_round_trip_survives_one_applied_operation(self):
        """A delta on a v2 document must round-trip before AND after the edit.

        The "after" half is the load-bearing one: the next refresh re-reads the stored
        structure and renders it, so an operation that produced a document which no
        longer renders to its own markdown would corrupt the model one refresh later —
        silently, because the write itself succeeds.
        """
        doc = split_markdown(DOC)
        self.assertEqual(render_document(doc), DOC)

        target = doc.sections[0].blocks[1]
        ops = parse_delta_operation_list(
            {
                "operations": [
                    {
                        "op": "replace_block",
                        "section_id": doc.sections[0].id,
                        "block_id": target.id,
                        "text": "Terra takes high-volume, latency-sensitive work.",
                    }
                ]
            }
        )
        applied = apply_operations(doc, ops.operations)
        self.assertEqual(applied.skipped, [])
        self.assertTrue(applied.changed)

        edited = applied.document
        self.assertEqual(edited.version, SCHEMA_VERSION)
        markdown = render_document(edited)
        self.assertIn("latency-sensitive", markdown)
        self.assertNotIn("Terra takes high-volume work.", markdown)
        # The round trip, on the edited document.
        self.assertEqual(render_document(split_markdown(markdown)), markdown)

    def test_second_delta_applies_to_the_rebuilt_baseline(self):
        """A2/A6: the property is not "content survives", it is "v2 can be edited again"."""
        doc = split_markdown(DOC)
        first = apply_operations(
            doc,
            parse_delta_operation_list(
                {
                    "operations": [
                        {
                            "op": "append_block",
                            "section_id": doc.sections[1].id,
                            "text": "Lifted only by the operator, or by a Grok plan.",
                        }
                    ]
                }
            ).operations,
        )
        self.assertEqual(first.skipped, [])

        # Round-trip through storage the way a refresh does: dump the structure,
        # render the markdown, then rebuild the baseline from what was stored.
        stored = first.document.model_dump()
        markdown = render_document(first.document)
        rebuilt = structured_document_from_stored(stored, markdown)
        self.assertEqual(rebuilt.version, SCHEMA_VERSION)

        second = apply_operations(
            rebuilt,
            parse_delta_operation_list(
                {
                    "operations": [
                        {
                            "op": "replace_block",
                            "section_id": rebuilt.sections[1].id,
                            "block_id": rebuilt.sections[1].blocks[-1].id,
                            "text": "Lifted only by the operator's own words.",
                        }
                    ]
                }
            ).operations,
        )
        self.assertEqual(second.skipped, [])
        self.assertTrue(second.changed)
        self.assertEqual(
            render_document(split_markdown(render_document(second.document))),
            render_document(second.document),
        )


class TestDeltaBaselineAfterMigration(unittest.TestCase):
    """`_load_delta_baseline` is what the fast path and the agentic path share."""

    def test_null_structure_rebuilds_from_content_at_v2(self):
        """The state migration d1e2f3a4b5c6 leaves every pre-upgrade model in.

        It NULLs structured_content on every non-v2 row and never touches `content`, so
        on the first refresh after the upgrade the markdown is the only baseline there
        is. A loader that gave up here would leave the model unrefreshable, because
        nothing else repopulates the column.
        """
        doc = _load_delta_baseline("mm-null", DOC, None)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.version, SCHEMA_VERSION)
        self.assertEqual(render_document(doc), DOC)

    def test_v1_blob_is_rebuilt_from_content_not_upgraded(self):
        """A stored v1 structure is not field-mapped: the markdown is the better source."""
        v1_blob = {
            "version": 1,
            "sections": [
                {
                    "id": "routing",
                    "heading": "Routing",
                    "level": 1,
                    "blocks": [{"type": "paragraph", "text": "stale v1 text"}],
                }
            ],
        }
        doc = _load_delta_baseline("mm-v1", DOC, v1_blob)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.version, SCHEMA_VERSION)
        self.assertEqual(render_document(doc), DOC)
        self.assertNotIn("stale v1 text", render_document(doc))

    def test_unusable_blob_falls_back_rather_than_refusing(self):
        doc = _load_delta_baseline("mm-bad", DOC, {"version": 2, "sections": "not a list"})
        self.assertIsNotNone(doc)
        self.assertEqual(doc.version, SCHEMA_VERSION)
        self.assertEqual(render_document(doc), DOC)


class TestFastPathEscapeHatch(unittest.TestCase):
    """`needs_full_context` is ours and had to survive the v1->v2 op rewrite."""

    def test_flag_is_carried_through_every_parse_branch(self):
        from_dict = parse_delta_operation_list({"operations": [], "needs_full_context": True})
        self.assertTrue(from_dict.needs_full_context)
        from_text = parse_delta_operation_list('{"operations": [], "needs_full_context": "true"}')
        self.assertTrue(from_text.needs_full_context)
        self.assertFalse(parse_delta_operation_list({"operations": []}).needs_full_context)
        self.assertFalse(parse_delta_operation_list({"operations": [], "needs_full_context": 1}).needs_full_context)


if __name__ == "__main__":
    unittest.main()
