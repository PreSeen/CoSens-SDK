import hashlib
import random

import pytest
from memory import AuthContext,ClaimDraft,MemoryService,SourceRef,Store
from memory.models import ContextAnchor,Conflict
from memory.segmentation import index_spans
from memory.retrieval import RetrievalConfig


def test_fixed_policy_exactly_preserves_legacy_spans():
    text='x'*2501
    expected=((0,1000),(800,1800),(1600,2501),(2400,2501))
    assert index_spans(text)==expected
    assert index_spans(text,policy='structural')==expected


def test_structural_spans_cover_nonwhitespace_without_crossing_boundaries():
    rng=random.Random(21)
    for _ in range(100):
        text=''.join(rng.choice(' abc') for _ in range(rng.randint(1,3000)))
        cuts=sorted({0,len(text),*(rng.randrange(len(text)+1) for _ in range(8))})
        anchors=tuple(ContextAnchor(a,a+1,a,b) for a,b in zip(cuts,cuts[1:]))
        spans=index_spans(text,anchors,policy='structural')
        assert spans==index_spans(text,tuple(reversed(anchors)),policy='structural')
        covered=set()
        for a,b in spans:
            assert 0<=a<b<=len(text) and b-a<=1000
            assert not any(a<c<b for c in cuts)
            covered.update(range(a,b))
        assert all(i in covered for i,ch in enumerate(text) if not ch.isspace())


@pytest.fixture
def memory():
    store=Store.sqlite();store.migrate();m=MemoryService(store,clock=lambda:'2026-01-01T00:00:00+00:00')
    owner=AuthContext('t','owner',True);reader=AuthContext('t','reader')
    m.create_project(owner,'p','p');m.set_member(owner,'p','reader','reader')
    yield m,owner,reader
    store.close()


def ingest(m,owner,**options):
    text='Date: today\nuser: The meeting is Monday.\nassistant: Noted.'
    start=text.index('user:');end=text.index('\nassistant:');last=end+1
    anchors=(ContextAnchor(0,11,0,len(text)),ContextAnchor(start,start+5,start,end),
             ContextAnchor(last,last+10,last,len(text)))
    receipt=m.ingest(owner,'p',text,request_key='one',context_anchors=anchors,**options)
    return text,receipt


def test_policy_is_idempotent_and_bound_to_request(memory):
    m,o,r=memory
    text,receipt=ingest(m,o,chunk_policy='structural')
    assert ingest(m,o,chunk_policy='structural')[1]==receipt
    with pytest.raises(Conflict):ingest(m,o,chunk_policy='fixed')
    chunks=m.db.all('SELECT * FROM chunks ORDER BY start_offset',())
    assert len(chunks)==3
    for chunk in chunks:
        a,b=chunk['start_offset'],chunk['end_offset']
        assert chunk['text']==text[a:b]
        assert chunk['id']==hashlib.sha256(f'{receipt.source_id}:1:{a}:structural:{b}'.encode()).hexdigest()


def test_structural_retrieval_keeps_literal_role_and_date(memory):
    m,o,r=memory;_,receipt=ingest(m,o,chunk_policy='structural')
    result=m.recall(r,'p','meeting',use_graph=False,use_wiki=False,rank_config=RetrievalConfig(source_context=True))
    assert 'user: The meeting is Monday.' in result.context
    assert 'Date: today' in result.context
    for e in result.items:
        source=m.read_source(r,'p',receipt.source_id)
        assert e.text==source['content'][e.refs[0].start:e.refs[0].end]


@pytest.mark.parametrize('change',['acl','delete','replace','revoke'])
def test_structural_chunks_use_existing_eligibility_checks(memory,change):
    m,o,r=memory;text,receipt=ingest(m,o,chunk_policy='structural')
    result=m.recall(r,'p','meeting',use_graph=False,use_wiki=False)
    witness=next(e for e in result.items if 'meeting' in e.text)
    if change=='acl':m.set_source_readers(o,'p',receipt.source_id,('owner',))
    elif change=='delete':m.delete_source(o,'p',receipt.source_id)
    elif change=='replace':m.ingest(o,'p','Different content',request_key='new',source_id=receipt.source_id,
                                   expected_revision=1,chunk_policy='structural')
    else:
        claim=m.commit(o,'p',[ClaimDraft(subject='s',predicate='p',value='v',text=witness.text,
            refs=witness.refs,status='supported')])[0]
        m.revoke(o,'p',claim['id'])
    assert 'meeting' not in m.recall(r,'p','meeting',use_graph=False,use_wiki=False).context


def test_bad_policy_has_no_writes(memory):
    m,o,r=memory
    with pytest.raises(ValueError):ingest(m,o,chunk_policy='unknown')
    assert m.db.all('SELECT * FROM sources',())==[]


def test_unstructured_fallback_keeps_original_chunk_ids(memory):
    m,o,r=memory
    receipt=m.ingest(o,'p','x'*1800,request_key='plain',source_id='plain',chunk_policy='structural')
    chunks=m.db.all('SELECT * FROM chunks ORDER BY start_offset',())
    assert [(c['start_offset'],c['end_offset']) for c in chunks]==list(index_spans('x'*1800))
    assert all(c['id']==hashlib.sha256(f'plain:1:{c["start_offset"]}'.encode()).hexdigest() for c in chunks)


def test_historical_revision_and_current_acl_both_apply(memory):
    m,o,r=memory;_,receipt=ingest(m,o,chunk_policy='structural')
    m.clock=lambda:'2026-01-03T00:00:00+00:00'
    m.ingest(o,'p','Replacement',request_key='new',source_id=receipt.source_id,expected_revision=1)
    assert 'meeting' in m.recall(r,'p','meeting',known_at='2026-01-02T00:00:00Z',use_graph=False,use_wiki=False).context
    m.set_source_readers(o,'p',receipt.source_id,('owner',))
    assert 'meeting' not in m.recall(r,'p','meeting',known_at='2026-01-02T00:00:00Z',use_graph=False,use_wiki=False).context
