"""One Memory-backed lifecycle for task execution and Recipe evolution.

SQLite holds delivery state and receipts; versioned learning evidence lives in
Friday Memory. Actor-side Memory tools are never needed or exposed.
"""
from copy import deepcopy
from contextlib import nullcontext
import json
import os
import time
from uuid import uuid4

from .algorithms.hierarchical import fingerprint


class RecipeMemory:
    def init_recipe_memory_schema(self):
        self.recipe_memory_enabled = os.environ.get('COSENS_RSI_RECIPE_MEMORY_ENABLED', '0') == '1'
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS recipe_memory_outbox(
              id TEXT PRIMARY KEY, job_id TEXT, run_id TEXT NOT NULL REFERENCES runs(id),
              operation_key TEXT NOT NULL UNIQUE, partition_name TEXT NOT NULL,
              evaluation_id TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
              lease_token TEXT, lease_until REAL, receipt_json TEXT, last_error TEXT,
              created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS recipe_learning_views(
              job_id TEXT NOT NULL, view_hash TEXT NOT NULL, refs_json TEXT NOT NULL,
              watermark INTEGER NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(job_id,view_hash));
            CREATE TABLE IF NOT EXISTS run_recipe_evaluations(
              run_id TEXT PRIMARY KEY REFERENCES runs(id), evaluation_id TEXT NOT NULL,
              evidence_artifact_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
              record_hash TEXT NOT NULL, outbox_id TEXT NOT NULL);
            ''')

    def _enqueue_recipe_episode(self, job_id, run, partition, record, *, db=None):
        body = {'principal': run['principal'], 'family': run['task_package']['task_family'],
                'experiment_id': job_id or 'run:' + run['run_id'], 'source_run_id': run['run_id'],
                'partition': partition, 'record': record}
        key = fingerprint([run['principal'], job_id, record.get('evaluation_id') or record['recipe_id']])
        data = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False)
        oid = 'recipe_outbox_' + uuid4().hex
        owns_transaction=db is None
        with (self._db() if owns_transaction else nullcontext(db)) as db:
            if owns_transaction:db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM recipe_memory_outbox WHERE operation_key=?', (key,)).fetchone()
            if old:
                if old['payload_json'] not in ('{}', data):
                    raise ValueError('recipe observation identity reused')
                if old['state'] == 'done' and json.loads(old['receipt_json'])['record_hash'] != fingerprint(record):
                    raise ValueError('recipe observation changed after delivery')
                return old['id']
            db.execute('''INSERT INTO recipe_memory_outbox
              (id,job_id,run_id,operation_key,partition_name,evaluation_id,payload_json,state,available_at,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)''', (oid, job_id, run['run_id'], key, partition,
                record.get('evaluation_id', 'release:' + record.get('recipe_id', '')), data, 'queued', time.time(), time.time()))
            self._event(db, run['run_id'], 'recipe_memory_queued',
                        {'job_id': job_id, 'evaluation_id': record.get('evaluation_id'), 'partition': partition})
        return oid

    def flush_recipe_memory_outbox(self, *, limit=20):
        count = 0
        for _ in range(limit):
            lease = uuid4().hex;current = time.time()
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE recipe_memory_outbox SET state='failed',last_error='LeaseExpired' WHERE state='running' AND lease_until<? AND attempts>=5", (current,))
                row = db.execute('''SELECT * FROM recipe_memory_outbox WHERE attempts<5 AND
                  ((state IN ('queued','retry_wait') AND available_at<=?) OR (state='running' AND lease_until<?))
                  ORDER BY created_at LIMIT 1''', (current, current)).fetchone()
                if not row:break
                db.execute("UPDATE recipe_memory_outbox SET state='running',lease_token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                           (lease, current+60, row['id']))
            try:
                body = json.loads(row['payload_json'])
                receipt = self._memory('POST', '/internal/v1/recipe-experiences/observe', body)
                if receipt.get('record_hash') != fingerprint(body['record']) or not receipt.get('experience_id'):
                    raise ValueError('Memory recipe receipt does not bind evidence')
            except Exception as exc:
                permanent = getattr(exc, 'status_code', None) in {400,401,403,404,409,422} or row['attempts']+1 >= 5
                with self._db() as db:
                    db.execute('''UPDATE recipe_memory_outbox SET state=?,last_error=?,available_at=?,lease_token=NULL,lease_until=NULL
                      WHERE id=? AND state='running' AND lease_token=?''',
                      ('failed' if permanent else 'retry_wait', type(exc).__name__, time.time()+2**(row['attempts']+1), row['id'], lease))
                continue
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute('''UPDATE recipe_memory_outbox SET state='done',receipt_json=?,payload_json='{}',lease_token=NULL,lease_until=NULL
                  WHERE id=? AND state='running' AND lease_token=?''', (json.dumps(receipt), row['id'], lease)).rowcount
                if changed:
                    count += 1
                    self._event(db, row['run_id'], 'recipe_memory_written',
                        {'job_id': row['job_id'], 'partition': row['partition_name'], 'experience_id': receipt['experience_id'],
                         'revision': receipt['revision'], 'reward_status': receipt['reward_status']})
        return {'delivered': count}

    def _recipe_training_view(self, job_id, run, payload):
        expected = []
        for record in payload['train_evidence']:
            expected.extend(record.get('rollouts') or [record])
        self.flush_recipe_memory_outbox(limit=128)
        with self._db() as db:
            rows = {r['evaluation_id']: r for r in db.execute(
                "SELECT * FROM recipe_memory_outbox WHERE job_id=? AND partition_name='train'", (job_id,))}
        refs = []
        for record in expected:
            row = rows.get(record['evaluation_id'])
            if not row or row['state'] != 'done':
                raise RuntimeError('Recipe learning awaits durable Memory training evidence')
            receipt = json.loads(row['receipt_json'])
            if receipt['record_hash'] != fingerprint(record):
                # Aggregated train wrappers may carry annotations; actual per-rollout evidence is immutable.
                raise ValueError('Memory train evidence differs from executed rollout')
            refs.append(receipt)
        watermark = self._memory('GET', '/internal/v1/experiences/watermark')['watermark']
        result = self._memory('POST', '/internal/v1/recipe-experiences/learning-view',
            {'principal': run['principal'], 'family': run['task_package']['task_family'],
             'experiment_id': job_id, 'references': refs, 'task_ids': [r['task_id'] for r in payload['train_evidence']],
             'watermark': watermark})
        if result.get('references') != refs or result.get('view_hash') != fingerprint({k:v for k,v in result.items() if k!='view_hash'}):
            raise ValueError('invalid Memory learning view')
        returned = {r['evaluation_id']: r for r in result['items']}
        if set(returned) != {r['evaluation_id'] for r in expected}:
            raise ValueError('Memory learning view changed training coverage')
        replacement = []
        for original in payload['train_evidence']:
            if original.get('rollouts'):
                replacement.append({**original, 'rollouts': [returned[r['evaluation_id']] for r in original['rollouts']]})
            else:
                replacement.append(returned[original['evaluation_id']])
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO recipe_learning_views VALUES(?,?,?,?,?)',
                       (job_id, result['view_hash'], json.dumps(refs), watermark, time.time()))
            self._event(db, run['run_id'], 'recipe_memory_learning_read',
                        {'job_id': job_id, 'count': len(refs), 'view_hash': result['view_hash'], 'watermark': watermark})
        return {**payload, 'train_evidence': replacement,
                'memory_learning': {'view_hash': result['view_hash'], 'watermark': watermark, 'references': refs}}

    def attach_recipe_memory(self, session, job_id, source_run_id, principal):
        if not self.recipe_memory_enabled:return session
        run = {**self.get_run(source_run_id, principal), 'principal': principal}
        original = session.proposer
        session.on_evaluation = lambda partition, record: self._observe_recipe_evaluation(job_id, run, partition, record)
        session.proposer = lambda payload: original(self._recipe_training_view(job_id, run, payload))
        return session

    def _observe_recipe_evaluation(self, job_id, run, partition, record):
        self._enqueue_recipe_episode(job_id, run, partition, deepcopy(record))
        self.flush_recipe_memory_outbox(limit=20)

    def recall_recipe_memory(self, principal, family, objective, bundle_hash, watermark):
        if not self.recipe_memory_enabled:return None
        return self._memory('POST', '/internal/v1/recipe-experiences/recall',
            {'principal': principal, 'family': family, 'query': objective,
             'bundle_hash': bundle_hash, 'watermark': watermark})

    def evaluate_recipe_run(self, run_id, principal, record, artifact_id, content_hash, *, admin_token):
        if admin_token != self.admin_token:raise PermissionError('trusted evaluator required')
        if not self.recipe_memory_enabled:raise ValueError('Recipe Memory lifecycle is disabled')
        run = self.get_run(run_id, principal)
        if run['status'] != 'completed':raise ValueError('completed real execution required')
        with self._db() as db:
            artifact = db.execute('SELECT content,content_hash FROM artifacts WHERE id=? AND principal=?', (artifact_id, principal)).fetchone()
            recipe = db.execute('SELECT bundle_hash FROM recipes WHERE id=?', (run['recipe_id'],)).fetchone()
        if not artifact or artifact['content_hash'] != content_hash or json.loads(artifact['content']) != record:
            raise ValueError('trusted evaluation must bind saved evidence')
        if not recipe or record.get('bundle_sha256') != recipe['bundle_hash'].removeprefix('sha256:'):
            raise ValueError('evaluation uses a different frozen Recipe')
        if record.get('task_prompt') != run['task_package']['objective']:
            raise ValueError('evaluation task does not match Run')
        with self._db() as db:
            old = db.execute('SELECT * FROM run_recipe_evaluations WHERE run_id=?', (run_id,)).fetchone()
        if old:
            if old['record_hash'] != fingerprint(record):raise ValueError('Run evaluation already frozen')
            return {'outbox_id': old['outbox_id'], 'status': 'recorded'}
        oid = self._enqueue_recipe_episode(None, {**run, 'principal': principal}, 'train', record)
        with self._db() as db:
            db.execute('INSERT INTO run_recipe_evaluations VALUES(?,?,?,?,?,?)',
                (run_id, record['evaluation_id'], artifact_id, content_hash, fingerprint(record), oid))
        self.flush_recipe_memory_outbox()
        return {'outbox_id': oid, 'status': 'recorded'}

    def record_recipe_release_memory(self, job_id, released, *, db=None):
        if not self.recipe_memory_enabled:return
        with (self._db() if db is None else nullcontext(db)) as db:
            row = db.execute('SELECT j.resource_id,h.principal,h.report_json FROM jobs j JOIN harness_jobs h ON h.job_id=j.id WHERE j.id=?', (job_id,)).fetchone()
            recipe = db.execute('SELECT bundle_hash FROM recipes WHERE id=?', (released['recipe_id'],)).fetchone()
            run = {**self.get_run(row['resource_id'], row['principal']), 'principal': row['principal']}
            report = json.loads(row['report_json']);rounds = report['result']['rounds']
            record = {'kind':'release', 'recipe_id':released['recipe_id'], 'revision':released['revision'],
                  'bundle_hash':recipe['bundle_hash'], 'objective':run['task_package']['objective'],
                  'decision':'accepted', 'experiment_id':job_id, 'base_hash':report['base_sha256'],
                  'diagnosis':rounds[-1].get('diagnosis',{}).get('reason',''),
                  'changes':[c['change'] for r in rounds for c in r.get('candidates',[]) if c['sha']==recipe['bundle_hash'].removeprefix('sha256:')],
                  'quality':report['release_decision']['comparison']['mean_delta']}
            self._enqueue_recipe_episode(job_id, run, 'production', record, db=db)
