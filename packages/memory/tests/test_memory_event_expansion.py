import unittest

from memory import AuthContext, IngestRoute, MemoryService, SourceRef, Store
from memory.models import Evidence


class MemoryEventExpansionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.sqlite()
        self.store.migrate()
        self.memory = MemoryService(self.store)
        self.owner = AuthContext("tenant", "owner", True)
        self.memory.create_project(self.owner, "project", "Project")

    def tearDown(self):
        self.store.close()

    def _ingest(self, source_id, text, event_id, readers=()):
        receipt = self.memory.ingest(
            self.owner, "project", text, source_id=source_id, request_key=source_id,
            readers=readers, route=IngestRoute(durable=True, event_id=event_id),
        )
        return receipt

    def test_expands_recalled_event_to_authorized_literal_sibling_only(self):
        seed_text = "Riley announced the launch date moved."
        sibling_text = "The vendor shipment delay caused the launch date shift."
        unrelated_text = "The vendor shipment delay caused a different launch date shift."
        seed = self._ingest("seed", seed_text, "event-a")
        sibling = self._ingest("sibling", sibling_text, "event-a")
        self._ingest("unrelated", unrelated_text, "event-b")
        seed_item = Evidence(
            "seed-evidence", "source", seed_text,
            (SourceRef("seed", seed.revision, 0, len(seed_text), seed.content_hash),), seed.revision, 1.0,
        )

        for policy in ("legacy",):
            with self.subTest(policy=policy):
                result = self.memory.event_sibling_evidence(
                    self.owner, "project", "Why did the launch date move?", (seed_item,),
                    max_items=4, max_chars=1000,
                )
                self.assertEqual(len(result.items), 1)
                item = result.items[0]
                self.assertEqual(item.refs[0].source_id, "sibling")
                self.assertEqual(item.revision, sibling.revision)
                self.assertEqual(item.text, sibling_text[item.refs[0].start:item.refs[0].end])
                self.assertEqual(result.diagnostics["event_evidence_policy"], policy)

    def test_expansion_rechecks_current_reader_acl(self):
        seed_text = "Riley announced the launch date moved."
        sibling_text = "The vendor shipment delay caused the launch date shift."
        seed = self._ingest("seed", seed_text, "event-a")
        self._ingest("private-sibling", sibling_text, "event-a", readers=("owner",))
        seed_item = Evidence(
            "seed-evidence", "source", seed_text,
            (SourceRef("seed", seed.revision, 0, len(seed_text), seed.content_hash),), seed.revision, 1.0,
        )
        reader = AuthContext("tenant", "reader", False)
        self.memory.set_member(self.owner, "project", "reader", "reader")

        for policy in ("legacy",):
            with self.subTest(policy=policy):
                result = self.memory.event_sibling_evidence(
                    reader, "project", "Why did the launch date move?", (seed_item,),
                    max_items=4, max_chars=1000,
                )
                self.assertEqual(result.items, ())


if __name__ == "__main__":
    unittest.main()
