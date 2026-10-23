import unittest
from dataclasses import replace

from memory import AuthContext, IngestRoute, MemoryService, Store
from memory.models import Evidence, EvidenceBundle, SourceRef
from memory.focused_evidence import complete_source_passage, select_origin
from memory.query_plan import plan_query


def source(sid, text):
    ref = SourceRef(sid, 1, 0, len(text), 'hash')
    return Evidence(sid, 'source', text, (ref,), 1)


class FocusedEvidenceTests(unittest.TestCase):
    def test_focus_keeps_coordinated_conclusion(self):
        query = ('Please explain the conclusion that Acme will focus on freight customers, '
                 'dropping the consumer marketplace, and for each item identify the evidence.')
        self.assertEqual(plan_query(query).focus,
                         'Acme will focus on freight customers, dropping the consumer marketplace')

    def test_focus_handles_date_before_topic_and_keeps_official_date(self):
        query = ('Reconstruct the facts on record as of 2023-02-19 about the appointment '
                 'of a developer to the Platform division. Indicate subsequent replacements.')
        plan = plan_query(query)
        self.assertEqual(plan.focus, 'the appointment of a developer to the Platform division')
        self.assertEqual(plan.as_of, '2023-02-19')
        self.assertTrue(plan.compare_changes)

    def test_origin_uses_strong_topic_match_and_does_not_equate_newer_with_replacement(self):
        items = [source('a', 'Orion launch delayed because vendor shipment failed.'),
                 source('b', 'Orion launch delayed because vendor shipment failed. A later different launch.'),
                 source('c', 'Orion cafeteria was delayed.')]
        selected, d = select_origin('Orion launch vendor shipment', items,
            {'a': 'original', 'b': 'later', 'c': 'unrelated'},
            {'a': '2023-02-01', 'b': '2023-04-01', 'c': '2023-01-01'},
            {'a': .8, 'b': .82, 'c': .79})
        self.assertEqual(d['root_group'], 'original')
        self.assertEqual([e.refs[0].source_id for e in selected], ['a'])

    def test_explicit_correction_is_a_candidate_not_an_inferred_relation(self):
        items = [source('a', 'Orion launch vendor shipment delayed.'), source('b',
            'The previous Orion launch shipment report was incorrect. Vendor shipment arrived.')]
        selected, d = select_origin('Orion launch vendor shipment', items,
            {'a': 'g1', 'b': 'g2'}, {'a': '2023-02-01', 'b': '2023-04-01'},
            {'a': .8, 'b': .81}, as_of='2023-03-01')
        self.assertEqual(d['selected_groups'], ['g1', 'g2'])
        self.assertEqual(selected[1].metadata['event_focus_role'], 'explicit_change_candidate')

    def test_complete_short_source_retains_attribution_condition_and_exact_ref(self):
        item = source('a', '[09:00] Ali: Orion is approved only after security review.\n[09:01] Bea: Agreed.')
        result = complete_source_passage(item, 'Orion approval', max_chars=200)
        self.assertEqual(result.text, item.text)
        self.assertEqual(result.refs, item.refs)

    def test_strong_semantics_not_overruled_by_unrelated_common_word_coverage(self):
        items = [source('a', 'Bulk shipment feature deferred to next sprint.'),
                 source('b', 'The API capability delay will follow the shipment sprint.')]
        selected, d = select_origin('bulk shipment capability delayed to the following sprint', items,
            {'a': 'bulk', 'b': 'api'}, {'a': '2023-02-01', 'b': '2023-01-01'}, {'a': .8, 'b': .65})
        self.assertEqual(d['root_group'], 'bulk')

    def test_person_name_in_mail_header_alone_does_not_establish_event_identity(self):
        items = [source('a', 'From: Riley Chen\nTo: Maya Patel\nA different employee left.'),
                 source('b', 'Riley Chen was hired as an engineer.')]
        selected, d = select_origin('Riley Chen hiring engineer', items,
            {'a': 'wrong', 'b': 'right'}, {'a': '2023-01-01', 'b': '2023-02-01'}, {'a': .8, 'b': .81})
        self.assertEqual(d['root_group'], 'right')

    def test_focus_vectors_are_independent_and_legacy_vector_is_not_reused(self):
        store = Store.sqlite(); store.migrate()
        memory = MemoryService(store); actor = AuthContext('t', 'a', True)
        memory.create_project(actor, 'p', 'P')
        original = memory.recall
        seen = []
        def spy(*args, **kwargs):
            seen.append(kwargs)
            return original(*args, **kwargs)
        memory.recall = spy
        memory.recall_planned(actor, 'p', 'Explain the evidence for the conclusion about Orion launch.',
                             query_vector=(1.0,), focus_vector=(0.0, 1.0))
        self.assertEqual(seen[0]['query_vector'], (1.0,))
        self.assertEqual(seen[1]['query_vector'], (0.0, 1.0))
        store.close()

    def test_memory_navigation_rechecks_sibling_acl_and_revisions(self):
        store = Store.sqlite(); store.migrate()
        memory = MemoryService(store); owner = AuthContext('t', 'owner', True)
        reader = AuthContext('t', 'reader')
        memory.create_project(owner, 'p', 'P'); memory.set_member(owner, 'p', 'reader', 'reader')
        for sid, readers in [('seed', ()), ('private', ('owner',))]:
            text = 'Date: 2023-02-01\nOrion launch delayed by vendor shipment.'
            receipt = memory.ingest(owner, 'p', text, source_id=sid, request_key=sid,
                                    readers=readers, route=IngestRoute(durable=True, event_id='event'))
            if sid == 'seed':
                seed = replace(source(sid, text), refs=(SourceRef(sid, receipt.revision, 0, len(text), receipt.content_hash),))
        result = memory.focus_event_evidence(reader, 'p', 'Orion launch vendor shipment',
                                             EvidenceBundle('q', (seed,), 0, {}))
        self.assertEqual([e.refs[0].source_id for e in result.items], ['seed'])
        memory.set_source_readers(owner, 'p', 'seed', ('owner',))
        result = memory.focus_event_evidence(reader, 'p', 'Orion launch vendor shipment',
                                             EvidenceBundle('q', (seed,), 0, {}))
        self.assertFalse(result.items)
        store.close()

    def test_full_planned_recall_uses_complete_event_without_nested_transaction(self):
        store = Store.sqlite(); store.migrate()
        memory = MemoryService(store); actor = AuthContext('t', 'a', True)
        memory.create_project(actor, 'p', 'P')
        text = 'Date: 2023-02-01\nOrion launch delayed by vendor shipment. Approval requires a security review.'
        memory.ingest(actor, 'p', text, source_id='one', request_key='one',
                      route=IngestRoute(durable=True, event_id='g'))
        result = memory.recall_planned(actor, 'p', 'Explain the evidence for the conclusion about Orion launch vendor shipment.')
        self.assertTrue(result.diagnostics['event_focus']['applied'])
        self.assertEqual(result.items[0].text, text)
        store.close()


if __name__ == '__main__':
    unittest.main()
