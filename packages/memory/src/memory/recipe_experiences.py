"""Private procedural-learning views over versioned episodic Memory.

RSI supplies trusted execution/judge evidence. Validation partitions are stored
for audit but cannot enter the training view or runtime procedural recall.
No procedure experience is compiled into Wiki or exposed through Memory MCP.
"""
from copy import deepcopy

from .experiences import ExperienceService, fingerprint
from .models import AccessDenied, Conflict

SCHEMA = 'rsi-recipe-experiences-v1'
REWARD = 'independent-recipe-quality-v1'


class RecipeExperienceService:
    def __init__(self, memory):
        self.experiences = ExperienceService(memory)

    def observe(self, actor, project, *, principal, family, experiment_id,
                source_run_id, partition, record):
        if not actor.administrator:
            raise AccessDenied('trusted RSI observation required')
        if not all(isinstance(v, str) and v.strip() and len(v) <= 256
                   for v in (principal, family, experiment_id, source_run_id)):
            raise ValueError('recipe observation scope required')
        if partition not in {'train', 'screen', 'confirm', 'production'}:
            raise ValueError('invalid recipe observation partition')
        record = deepcopy(record)
        release = record.get('kind') == 'release'
        if release:
            if partition != 'production' or record.get('decision') != 'accepted' or not record.get('recipe_id'):
                raise ValueError('runtime lessons require an accepted published Recipe')
            identity = 'release:' + record['recipe_id']
            sha = record['bundle_hash']
            objective = record['objective']
            valid, score = True, 1
        else:
            required = {'evaluation_id', 'task_id', 'task_lineage_id', 'task_sha256',
                        'bundle_sha256', 'protocol_sha256', 'session_sha256', 'task_prompt',
                        'judge_valid', 'score', 'task_success'}
            if not required <= set(record) or partition == 'production':
                raise ValueError('frozen evaluation evidence required')
            if any(k in record for k in ('tests', 'expected', 'reference_answer')):
                raise ValueError('hidden test material cannot be learning evidence')
            identity = record['evaluation_id'];sha = record['bundle_sha256']
            objective = record['task_prompt'];valid = record['judge_valid'] is True
            score = record['score']
        digest = fingerprint([principal, family, experiment_id, identity])
        eid = 'recipe_exp_' + digest
        payload = {'run_id': source_run_id, 'task_family': family, 'phase': 'recipe-learning',
                   'environment': {'origin_principal': principal}, 'action_schema': SCHEMA,
                   'reward_spec': REWARD, 'partition': partition, 'contract_hash': SCHEMA,
                   'task_lineage_id': record.get('task_lineage_id', source_run_id),
                   'experiment_id': experiment_id, 'kind': 'release' if release else 'evaluation',
                   'state': {'observation': objective, 'learning_evidence': record},
                   'candidates': [{'id': sha, 'version': '1', 'description': '执行冻结的分层 Recipe'}],
                   'actual_action_id': sha, 'source_refs': []}
        first = self.experiences.upsert(actor, project, payload, experience_id=eid,
                                       expected_revision=0, request_key=digest + ':observe')
        evidence_hash = fingerprint(record)
        outcome = {'evaluation_id': identity, 'reward_spec': REWARD,
                   'status': 'verified' if valid else 'inconclusive',
                   'value': 2 * score - 1 if valid else None,
                   'evidence_ref': 'recipe-audit:' + experiment_id + ':' + identity,
                   'evidence_content_hash': evidence_hash}
        receipt = self.experiences.append_outcome(actor, project, eid, outcome,
                    expected_revision=first['revision'], request_key=digest + ':judge')
        return {**receipt, 'record_hash': evidence_hash, 'partition': partition,
                'evaluation_id': identity, 'kind': payload['kind']}

    def learning_view(self, actor, project, *, principal, family, experiment_id,
                      references, task_ids, watermark):
        if not actor.administrator:
            raise AccessDenied('trusted RSI training view required')
        if not references or len(references) > 128 or not task_ids or type(watermark) is not int:
            raise ValueError('bounded frozen training references required')
        items, refs, seen = [], [], set()
        for ref in references:
            if ref['experience_id'] in seen:
                raise ValueError('duplicate training reference')
            seen.add(ref['experience_id'])
            item = self.experiences.get(actor, project, ref['experience_id'], revision=ref['revision'])
            r = item['record'];e = r['state']['learning_evidence']
            if (r['action_schema'] != SCHEMA or r['task_family'] != family
                    or r['environment'] != {'origin_principal': principal}
                    or r.get('experiment_id') != experiment_id or r['partition'] != 'train'
                    or r.get('kind') != 'evaluation' or e['task_id'] not in task_ids
                    or item['watermark'] > watermark or r['outcome']['status'] != 'verified'):
                raise AccessDenied('experience outside frozen training view')
            if fingerprint(e) != ref['record_hash'] or item['content_hash'] != ref['content_hash']:
                raise Conflict('training reference changed')
            if not item['is_current_revision']:
                raise Conflict('training evidence was superseded')
            items.append(deepcopy(e));refs.append(deepcopy(ref))
        if {e['task_id'] for e in items} != set(task_ids):
            raise ValueError('training view does not cover every requested task')
        result = {'items': items, 'references': refs, 'watermark': watermark,
                  'profile': SCHEMA, 'experiment_id': experiment_id}
        result['view_hash'] = fingerprint(result)
        return result

    def recall(self, actor, project, *, principal, family, query, bundle_hash, watermark):
        recalled = self.experiences.search(actor, project, task_family=family,
            phase='recipe-learning', environment={'origin_principal': principal},
            action_schema=SCHEMA, reward_spec=REWARD, query=query, watermark=watermark,
            contract_hash=SCHEMA, partitions=('production',), limit=50)
        items = []
        for item in recalled['items']:
            record = item['record'];e = record['state']['learning_evidence']
            if record.get('kind') != 'release' or e.get('bundle_hash') != bundle_hash:
                continue
            items.append({'experience_id': item['experience_id'], 'revision': item['revision'],
                          'content_hash': item['content_hash'], 'lesson': deepcopy(e)})
        result = {'items': items, 'watermark': recalled['watermark'], 'profile': SCHEMA}
        result['view_hash'] = fingerprint(result)
        return result
