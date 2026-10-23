import json
from pathlib import Path

import pytest

from cosens_rsi.algorithms.hierarchical import seed_bundle
from cosens_rsi.algorithms.hierarchical_learning import LearningPolicy, compare, confirm_decision
from cosens_rsi.demo_refund import grade, reference
from cosens_rsi.service import RSIService


def documents():
    return {'order':{'source_id':'order','paid_at':'2026-09-12','lines':[
        {'line_id':'A','quantity':3,'unit_price_cents':101,'coupon_eligible':True},
        {'line_id':'B','quantity':3,'unit_price_cents':101,'coupon_eligible':True}],
        'coupon_cents':101,'shipping_paid_cents':99,'merchant_fault':True},
        'policies':[{'source_id':'old','policy_id':'v1','effective_from':'2026-01-01','old_allow_partial_shipping':True},
                    {'source_id':'new','policy_id':'v2','effective_from':'2026-09-01','old_allow_partial_shipping':False}],
        'request':{'source_id':'request','requested_total_qty':{'A':3,'B':3},'customer_phone':'13800138000','customer_bank':'6222000000000000000'},
        'approval_records':[{'source_id':'approval-A','line_id':'A','approved_total_qty':2,'status':'已批准','signed_by':'auditor','signed_at':'2026-10-01'},
                            {'source_id':'approval-B','line_id':'B','approved_total_qty':1,'status':'已批准','signed_by':'auditor','signed_at':'2026-10-02'}],
        'refund_ledger':{'source_id':'ledger','events':[
            {'refund_id':'x','line_id':'A','approved_total_qty':1,'amount_cents':55,'status':'已到账'},
            {'refund_id':'x','line_id':'A','approved_total_qty':1,'amount_cents':55,'status':'已到账'},
            {'refund_id':'y','line_id':'A','approved_total_qty':1,'amount_cents':999,'status':'处理中'}]},
        'invoice':{'source_id':'invoice'},'service_note':{'source_id':'chat'}}


def test_refund_reference_apportions_last_cent_deduplicates_and_never_uses_processing_money():
    value=reference(documents())
    assert [r['coupon_allocated_cents'] for r in value['lines']]==[51,50]
    assert value['lines'][0]['prior_refunded_cents']==55
    assert [r['refund_due_cents'] for r in value['lines']]==[113,84]
    assert value['cash_total_cents']==197
    assert value['shipping_refund_cents']==0


def test_unsigned_draft_does_not_authorize_refund():
    d=documents();d['approval_records'][0]['signed_by']=''
    value=reference(d)
    assert value['lines'][0]['approved_total_qty']==1
    assert value['lines'][0]['status']=='补证'
    assert 'request' in value['lines'][0]['evidence_refs']


def test_initial_bundle_import_is_admin_only_and_cannot_replace_a_family(tmp_path):
    service=RSIService(db_path=str(tmp_path/'rsi.db'),memory_token='unused')
    with pytest.raises(PermissionError):service.bootstrap_recipe_bundle('demo',seed_bundle(),'reason',admin_token='wrong')
    first=service.bootstrap_recipe_bundle('demo',seed_bundle(),'reason',admin_token=service.admin_token)
    with pytest.raises(ValueError,match='already initialized'):
        service.bootstrap_recipe_bundle('demo',seed_bundle(),'cannot bypass validation',admin_token=service.admin_token)
    assert service.get_active_recipe('demo')['recipe_id']==first['recipe_id']


def test_average_score_gain_cannot_hide_a_new_critical_business_failure():
    common={'task_id':'task','task_sha256':'task-hash','protocol_sha256':'fixed','judge_valid':True,
            'task_success':False,'elapsed_sec':1}
    base={**common,'bundle_sha256':'base','score':.7,'quality_checks':[{'key':'refund','critical':True,'passed':True}]}
    candidate={**common,'bundle_sha256':'candidate','score':.95,'quality_checks':[{'key':'refund','critical':True,'passed':False}]}
    comparison=compare([base],[candidate],['task'],base_sha='base',candidate_sha='candidate')
    assert comparison['critical_regressions']==1
    assert confirm_decision(comparison,LearningPolicy(),'candidate')['status']=='rejected'


def test_pending_approval_review_accepts_equivalent_chinese_wording(tmp_path):
    d=documents();result=reference(d)
    result['uncertainties']=['A超出已批准数量的部分仍待正式审批。','B其余申请尚待审批。']
    result['customer_reply']='本次退款1.97元；A与B其余申请仍待正式审批，到账以支付渠道回执为准。'
    (tmp_path/'result.json').write_text(json.dumps(result,ensure_ascii=False))
    (tmp_path/'审核报告.md').write_text('采用政策 v2，本次退款 1.97 元。')
    outcome=grade(tmp_path,{'documents':d})
    assert outcome['judge_valid'] and outcome['task_success'],outcome['feedback']
    result['uncertainties']=['A超出已批准数量的部分仍待正式审批。']
    (tmp_path/'result.json').write_text(json.dumps(result,ensure_ascii=False))
    assert not next(c for c in grade(tmp_path,{'documents':d})['quality_checks'] if c['key']=='uncertainty')['passed']


def test_missing_runner_artifacts_is_invalid_evaluation_not_poor_business_quality(tmp_path):
    outcome=grade(tmp_path,{'documents':documents()})
    assert outcome['judge_valid'] is False and outcome['score'] is None


def test_operator_can_authorize_source_edit_but_not_mutation_of_evidence(tmp_path):
    from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor
    from copy import deepcopy
    bundle=json.loads((Path(__file__).resolve().parents[1]/'business_cases/refund_recipe_quality/recipe-v1.json').read_text())
    (tmp_path/'source.py').write_text('broken')
    (tmp_path/'evidence.txt').write_text('frozen')
    class Edit:
        def execute(self,instruction,*,timeout):
            (tmp_path/'source.py').write_text('fixed')
            return {'completed':True,'outputs':{'files':['source.py']}}
    value=HierarchicalExecutor().run(bundle,'repair code',tmp_path,Edit(),editable_inputs=['source.py'])
    assert value['status']=='completed'
    class Bad(Edit):
        def execute(self,instruction,*,timeout):
            result=super().execute(instruction,timeout=timeout)
            (tmp_path/'evidence.txt').write_text('changed')
            return result
    assert HierarchicalExecutor().run(bundle,'repair code',tmp_path,Bad(),editable_inputs=['source.py'])['status']=='failed'
    with pytest.raises(ValueError):
        HierarchicalExecutor().run(bundle,'repair code',tmp_path,Edit(),editable_inputs=['../outside'])
