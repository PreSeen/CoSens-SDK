import unittest

from memory.models import Evidence, SourceRef
from memory.event_evidence import sibling_evidence


def _evidence(source_id, text, score=1.0):
    ref = SourceRef(source_id, 1, 0, len(text), f"hash-{source_id}")
    return Evidence(f"e-{source_id}", "source", text, (ref,), 1, score)


class SiblingEvidenceTests(unittest.TestCase):



    def test_expands_only_recalled_event_groups_with_literal_refs(self):
        query = "Why was the launch date moved?"
        recalled_text = "Riley announced that the launch date moved."
        texts = {
            "seed": recalled_text,
            "sibling": "Supplier update: the launch date moved because the shipment was delayed.",
            "other": "The launch date moved after a planning review.",
        }
        metadata = {
            "seed": {"thread_id": "event-1", "slot_id": "ART-1"},
            "sibling": {"thread_id": "event-1", "slot_id": "ART-2"},
            "other": {"thread_id": "event-2", "slot_id": "ART-3"},
        }
        versions = {key: (1, f"hash-{key}") for key in texts}

        result = sibling_evidence(query, [_evidence("seed", recalled_text)], texts, metadata, versions,
                                  max_items=4, max_chars=1000)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].refs[0].source_id, "sibling")
        self.assertIn(result[0].text, texts["sibling"])
        self.assertNotIn(texts["other"], result[0].text)
        ref = result[0].refs[0]
        self.assertEqual(result[0].text, texts["sibling"][ref.start:ref.end])

    def test_requires_a_recalled_group_seed_and_respects_limits(self):
        text = "The launch moved because the vendor shipped late."
        metadata = {"candidate": {"thread_id": "event-1", "slot_id": "ART-2"}}
        versions = {"candidate": (1, "hash-candidate")}

        self.assertEqual(
            sibling_evidence("Why did the launch move?", [], {"candidate": text}, metadata, versions), ()
        )
        seeded = sibling_evidence(
            "Why did the launch move?", [_evidence("seed", "Launch moved.")],
            {"seed": "Launch moved.", "candidate": text},
            {**metadata, "seed": {"thread_id": "event-1"}},
            {**versions, "seed": (1, "hash-seed")}, max_items=1, max_chars=120,
        )
        self.assertLessEqual(len(seeded), 1)
        self.assertLessEqual(sum(len(item.text) for item in seeded), 120)

    def test_group_limit_prefers_highest_ranked_recalled_groups(self):
        query = "What caused the launch delay?"
        texts = {
            "strong-seed": "Launch delay was discussed.",
            "weak-seed": "Launch delay was also discussed.",
            "strong-sibling": "The launch delay was caused by the vendor shipment.",
            "weak-sibling": "The launch delay was caused by a staffing change.",
        }
        metadata = {
            "strong-seed": {"thread_id": "strong", "slot_id": "ART-1"},
            "weak-seed": {"thread_id": "weak", "slot_id": "ART-2"},
            "strong-sibling": {"thread_id": "strong", "slot_id": "ART-3"},
            "weak-sibling": {"thread_id": "weak", "slot_id": "ART-4"},
        }
        versions = {key: (1, f"hash-{key}") for key in texts}

        result = sibling_evidence(
            query,
            [_evidence("strong-seed", texts["strong-seed"], 2.0),
             _evidence("weak-seed", texts["weak-seed"], 1.0)],
            texts,
            metadata,
            versions,
            max_groups=1,
            max_chars=1000,
        )

        self.assertEqual([item.refs[0].source_id for item in result], ["strong-sibling"])


if __name__ == "__main__":
    unittest.main()
