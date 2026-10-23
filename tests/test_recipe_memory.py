import json
from copy import deepcopy

import pytest
from memory import AuthContext, ExperienceService, MemoryService, Store
from memory.recipe_experiences import RecipeExperienceService
from memory.models import AccessDenied
from cosens_rsi.algorithms.hierarchical import fingerprint
from cosens_rsi.service import RSIService, MemoryDependencyError
from test_harness import backend


@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setenv('COSENS_RSI_RECIPE_MEMORY_ENABLED','1')
    monkeypatch.delenv('COSENS_RSI_HARNESS_BACKEND',raising=False)
    store=Store.sqlite(str(tmp_path/'memory.sqlite'));store.migrate()
    memory=MemoryService(store);actor=AuthContext('local','rsi',True)
    memory.create_project(actor,'default','Test')
    recipes=RecipeExperienceService(memory);experiences=ExperienceService(memory)
    rsi=RSIService(str(tmp_path/'rsi.sqlite'),memory_token='internal')
    def bridge(method,path,body=None):
        if path.endswith('/watermark'):return {'watermark':experiences.watermark(actor,'default')}
        if path.endswith('/recipe-experiences/observe'):return recipes.observe(actor,'default',**body)
        if path.endswith('/recipe-experiences/learning-view'):return recipes.learning_view(actor,'default',**body)
        if path.endswith('/recipe-experiences/recall'):return recipes.recall(actor,'default',**body)
        return {'items':[]}
    rsi._memory=bridge
    yield rsi,recipes,actor,bridge
    store.close()


def run_job(rsi):
    rsi.bootstrap_seed('files','explicit integration seed')
    run=rsi.prepare_task({'objective':'Compute a file total','task_family':'files','environment':{'case_id':'closure'}},'owner')
    rsi.improvement_backends['files']=backend()
    job=rsi.start_improvement(run['run_id'],'owner','reuse observed execution in Memory')
    rsi.process_one_improvement()
    return run,job,rsi.get_improvement(job['job_id'],'owner')


def test_memory_is_the_proposer_evidence_source_and_release_reaches_new_run(connected):
    rsi,recipes,actor,_=connected
    run,job,finished=run_job(rsi)
    assert finished['status']=='validated',finished
    with rsi._db() as db:
        rows=db.execute('SELECT * FROM recipe_memory_outbox').fetchall()
        views=db.execute('SELECT * FROM recipe_learning_views').fetchall()
    assert len(rows)==20 and all(r['state']=='done' for r in rows)
    assert len(views)==1
    refs=json.loads(views[0]['refs_json'])
    assert len(refs)==4 and {r['partition'] for r in refs}=={'train'}
    release=rsi.publish_improvement(job['job_id'],1,'verified release',admin_token=rsi.admin_token)
    later=rsi.prepare_task({'objective':'Compute a file total','task_family':'files'},'owner')
    saved=rsi.get_run(later['run_id'],'owner')
    items=saved['snapshot']['recipe_memory']['items']
    assert later['recipe']['recipe_id']==release['recipe_id']
    assert len(items)==1 and items[0]['lesson']['recipe_id']==release['recipe_id']
    assert items[0]['lesson']['changes']
    other=rsi.prepare_task({'objective':'Compute a file total','task_family':'files'},'another-owner')
    assert rsi.get_run(other['run_id'],'another-owner')['snapshot']['recipe_memory']['items']==[]
    # Existing frozen source snapshot remains unchanged after the release.
    assert rsi.get_run(run['run_id'],'owner')['snapshot']['recipe_memory']['items']==[]


def test_screen_records_cannot_enter_training_view_and_runtime_recalls_only_releases(connected):
    rsi,recipes,actor,_=connected
    _,job,finished=run_job(rsi)
    with rsi._db() as db:
        rows=db.execute("SELECT receipt_json FROM recipe_memory_outbox WHERE partition_name='screen'").fetchall()
    refs=[json.loads(rows[0][0])]
    item=recipes.experiences.get(actor,'default',refs[0]['experience_id'])
    record=item['record']['state']['learning_evidence']
    with pytest.raises(AccessDenied):
        recipes.learning_view(actor,'default',principal='owner',family='files',experiment_id=job['job_id'],
             references=refs,task_ids=[record['task_id']],watermark=999999)
    assert recipes.recall(actor,'default',principal='owner',family='files',query='Compute a file total',
                          bundle_hash=rsi.get_active_recipe('files')['bundle_hash'],watermark=999999)['items']==[]


def test_lost_memory_response_retries_without_duplicate_versions(connected):
    rsi,recipes,actor,bridge=connected
    rsi.bootstrap_seed('files','seed')
    run=rsi.prepare_task({'objective':'Compute a file total','task_family':'files'},'owner')
    body={'evaluation_id':'eval_test','task_id':'train-test','task_lineage_id':'lineage',
          'task_sha256':'task','bundle_sha256':'bundle','protocol_sha256':'protocol','session_sha256':'session',
          'task_prompt':'Compute a file total','judge_valid':True,'score':.5,'task_success':False}
    oid=rsi._enqueue_recipe_episode('job-test',{**rsi.get_run(run['run_id'],'owner'),'principal':'owner'},'train',body)
    lost=[]
    def unreliable(method,path,body=None):
        result=bridge(method,path,body)
        if path.endswith('/observe') and not lost:
            lost.append(True);raise MemoryDependencyError()
        return result
    rsi._memory=unreliable
    assert rsi.flush_recipe_memory_outbox()['delivered']==0
    with rsi._db() as db:db.execute('UPDATE recipe_memory_outbox SET available_at=0')
    assert rsi.flush_recipe_memory_outbox()['delivered']==1
    with rsi._db() as db:row=db.execute('SELECT * FROM recipe_memory_outbox WHERE id=?',(oid,)).fetchone()
    ref=json.loads(row['receipt_json'])
    item=recipes.experiences.get(actor,'default',ref['experience_id'])
    assert item['revision']==2 and row['payload_json']=='{}'
    assert recipes.experiences.db.one('SELECT COUNT(*) n FROM experience_versions')['n']==2
