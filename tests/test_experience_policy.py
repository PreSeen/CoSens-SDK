from cosens_rsi.experience_policy import DEFAULT_POLICY, rank_candidates
from memory.experiences import action_signature

ACTIONS = [{'id': 'scan', 'version': '1', 'description': 'scan input', 'confidence': .5, 'cost': 0},
           {'id': 'index', 'version': '1', 'description': 'build index', 'confidence': .5, 'cost': 0}]


def experience(value):
    return {'experience_id': 'exp-a', 'revision': 2, 'content_hash': 'hash', 'similarity': 1,
            'record': {'run_id': 'first-run', 'action_signature': action_signature(ACTIONS[0]),
                       'outcome': {'status': 'verified', 'value': value}}}


def test_one_verified_failure_can_change_a_later_decision():
    cold = rank_candidates(ACTIONS, [], DEFAULT_POLICY)
    learned = rank_candidates(ACTIONS, [experience(-1)], DEFAULT_POLICY)
    assert cold['recommended_action_id'] == 'scan'
    assert learned['recommended_action_id'] == 'index'
    assert learned['scores'][1]['q'] is None  # never pretend untried means proven
    assert learned['scores'][0]['experience_refs'][0]['experience_id'] == 'exp-a'


def test_pending_feedback_never_changes_decision():
    item = experience(-1)
    item['record']['outcome']['status'] = 'pending'
    assert rank_candidates(ACTIONS, [item], DEFAULT_POLICY)['cold_start']


def test_positive_or_neutral_single_evidence_keeps_successful_route():
    for value in [0, 1]:
        assert rank_candidates(ACTIONS, [experience(value)], DEFAULT_POLICY)['recommended_action_id'] == 'scan'


def test_existing_frozen_v1_policy_keeps_its_original_semantics():
    old = {**DEFAULT_POLICY, 'schema': 'experience-rerank-v1'}
    assert rank_candidates(ACTIONS, [experience(-1)], old)['recommended_action_id'] == 'scan'
