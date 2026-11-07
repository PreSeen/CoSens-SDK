"""Compact rendering of injected Recipe, Memory and execution-request context."""
from cosens.context_render import render_execution_request, render_guidance
from cosens.responses_adapter import inject

RECIPE = {'title': 'Quarterly report', 'when': 'Writing a\n report', 'when_not': 'Casual chat',
          'steps': [{'id': 's1', 'instruction': 'Collect figures', 'evidence_refs': [{'event_id': 'e1'}]},
                    {'id': 's2', 'instruction': 'Fill the template', 'evidence_refs': [{'event_id': 'e2'}]}],
          'lessons': [{'when': 'x', 'do': 'Cite each figure', 'why': 'y', 'when_not': 'z', 'evidence_refs': []}]}
MEMORY = [{'id': 'm1', 'kind': 'source', 'text': 'Use the Q3 template.', 'refs': [{'source_id': 's'}], 'score': 0.4}]


def test_guidance_lists_steps_memory_and_lessons_without_references():
    text = render_guidance(RECIPE, MEMORY)
    assert text.splitlines()[1:] == [
        'Recipe (provisional): Quarterly report', 'Use when: Writing a report', 'Do not use when: Casual chat',
        'Steps:', '1. Collect figures', '2. Fill the template',
        'Related Memory:', '- Use the Q3 template.', 'Lessons:', '- Cite each figure']
    assert 'evidence' not in text and 'score' not in text and 'm1' not in text


def test_guidance_truncation_is_marked_and_bounded():
    recipe = dict(RECIPE, steps=[{'instruction': 'step ' + 'x' * 200} for _ in range(30)])
    memory = [{'text': 'memory ' + 'y' * 2000}] * 10
    text = render_guidance(recipe, memory, max_chars=1000)
    assert len(text) <= 1000
    assert 'Lessons' not in text and 'Related Memory' not in text
    assert text.splitlines()[-1].startswith('[CoSens context truncated to 1000 characters;')


def test_long_memory_item_is_clipped():
    text = render_guidance(None, [{'text': 'z' * 5000}])
    assert len(text) < 1000 and text.endswith('…')


def test_inject_skips_empty_context_and_uses_matched_bundle_only():
    kwargs = {'model': 'm', 'input': 'task'}
    empty = {'recipe': {'status': 'not_found'}, 'snapshot': {'evidence': []}, 'context': {'source': 'lookup'}}
    assert inject(('responses', 'create'), kwargs, empty) == kwargs
    matched = {'recipe': {'status': 'matched', 'bundle': RECIPE}, 'snapshot': {'evidence': []}}
    updated = inject(('chat', 'completions', 'create'), {'messages': []}, matched)
    assert updated['messages'][0]['role'] == 'developer'
    assert '1. Collect figures' in updated['messages'][0]['content']


def test_execution_request_rendering_is_compact():
    request = {'task_id': 'task_1', 'kind': 'revision', 'context_version': 3, 'trace_origin': 'revision',
               'context': {'objective': 'Write the Q3 report',
                           'artifacts': [{'artifact_id': 'a1', 'role': 'original_input', 'content_hash': 'sha256:abc'}]},
               'feedback': [{'feedback': 'Use euros'}],
               'method': {'recipe_id': 'r1', 'recipe_candidate_id': 'c1', 'version': 2, 'recipe_sha256': 'f' * 64,
                          'recipe': RECIPE},
               'memory': {'status': 'captured', 'scope': 'u', 'items': MEMORY}}
    text = render_execution_request(request)
    assert text.splitlines() == [
        '[CoSens frozen task execution request]', 'Task: task_1 (revision)', 'Objective: Write the Q3 report',
        'Input artifacts: a1 (original_input)', 'Feedback on the previous result:', '- Use euros',
        'Recipe (provisional): Quarterly report', 'Use when: Writing a report', 'Do not use when: Casual chat',
        'Steps:', '1. Collect figures', '2. Fill the template', 'Related Memory:', '- Use the Q3 template.',
        'Lessons:', '- Cite each figure']
    long_request = dict(request, context={'objective': 'o ' * 5000})
    assert len(render_execution_request(long_request, max_chars=500)) <= 500
