"""Independent post-submission checker. Scenario values are not Codex inputs.

This checker does not import a reference implementation or trust model test output.
Run in a subprocess without service credentials; stdout is a compact result.
"""
import argparse
import importlib.util
import itertools
import json
from pathlib import Path


def evaluate(path, variant):
    spec = importlib.util.spec_from_file_location('candidate_payment', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    new, apply, view = module.new_store, module.apply_event, module.order_view
    salt = {'holdout': 'omega-29', 'training_a': 'alpha-13', 'training_b': 'beta-71'}[variant]
    amount = {'holdout': 1573, 'training_a': 2401, 'training_b': 997}[variant]
    results = []

    def capture(e='c', o='o', p='p', n=amount):
        return dict(event_id=salt+e, order_id=salt+o, kind='capture', payment_id=salt+p, amount_cents=n)

    def refund(e='r', o='o', p='p', r='r', n=amount):
        return dict(event_id=salt+e, order_id=salt+o, kind='refund', payment_id=salt+p, refund_id=salt+r, amount_cents=n)

    def failed(e='f', o='o'):
        return dict(event_id=salt+e, order_id=salt+o, kind='failed')

    def expected(store, status, captured, refunded, fulfillment, o='o'):
        actual = view(store, salt+o)
        target = dict(status=status, captured_cents=captured, refunded_cents=refunded, fulfillments=fulfillment)
        assert actual == target, f'期望 {target}；实际 {actual}'

    def rejects(store, event):
        saved = json.dumps(store, sort_keys=True)
        try:
            apply(store, event)
        except ValueError:
            pass
        else:
            raise AssertionError('非法或冲突事件未被拒绝')
        assert json.dumps(store, sort_keys=True) == saved, '拒绝事件仍产生了状态副作用'

    def check(key, title, fn):
        try:
            fn()
            results.append(dict(key=key, title=title, passed=True, detail='独立回放符合公开业务契约'))
        except Exception as exc:
            results.append(dict(key=key, title=title, passed=False, detail=f'{type(exc).__name__}: {exc}'[:450]))

    def basic():
        s = new()
        result = apply(s, capture())
        expected(s, 'paid', amount, 0, 1)
        assert result == view(s, salt+'o')
    check('capture', '正常支付确认与接口兼容', basic)

    def exact_duplicate():
        s = new()
        for _ in range(4):
            apply(s, capture())
        expected(s, 'paid', amount, 0, 1)
    check('event_duplicate', '同一事件重复投递不重复入账/出库', exact_duplicate)

    def business_duplicate():
        s = new()
        for e in ['c1', 'c2', 'c3']:
            apply(s, capture(e=e))
        expected(s, 'paid', amount, 0, 1)
    check('capture_business_key', '不同事件 ID 的同笔支付仍幂等', business_duplicate)

    def capture_conflict():
        s = new()
        apply(s, capture())
        rejects(s, capture(e='new', n=amount+1))
        expected(s, 'paid', amount, 0, 1)
    check('capture_conflict', '支付金额冲突拒绝且状态不变', capture_conflict)

    def partial():
        s = new()
        apply(s, capture())
        apply(s, refund(n=137))
        expected(s, 'partially_refunded', amount, 137, 1)
    check('partial_refund', '部分退款金额和状态正确', partial)

    def refund_duplicate():
        s = new()
        apply(s, capture())
        apply(s, refund(e='r1', n=137))
        apply(s, refund(e='r2', n=137))
        expected(s, 'partially_refunded', amount, 137, 1)
    check('refund_business_key', '退款业务键去重，不依赖投递 ID', refund_duplicate)

    def refund_conflict():
        s = new()
        apply(s, capture())
        apply(s, refund(n=137))
        rejects(s, refund(e='new', n=138))
    check('refund_conflict', '退款金额冲突原子拒绝', refund_conflict)

    def pending_refund():
        s = new()
        apply(s, refund(n=137))
        expected(s, 'pending', 0, 0, 0)
        apply(s, capture())
        expected(s, 'partially_refunded', amount, 137, 1)
    check('refund_before_capture', '退款先于支付可挂起并正确归并', pending_refund)

    def fully_refunded_first():
        s = new()
        apply(s, refund())
        apply(s, capture())
        expected(s, 'refunded', amount, amount, 0)
        apply(s, capture(e='second', p='second', n=500))
        expected(s, 'partially_refunded', amount+500, amount, 1)
    check('no_phantom_fulfillment', '已先全额退款的支付不触发出库', fully_refunded_first)

    def over_refund():
        s = new()
        apply(s, capture())
        rejects(s, refund(n=amount+1))
        apply(s, refund(e='ok', n=amount))
        expected(s, 'refunded', amount, amount, 1)
    check('over_refund_atomic', '超额退款拒绝后仍可合法重试', over_refund)

    def under_capture():
        s = new()
        apply(s, refund(n=amount))
        rejects(s, capture(n=amount-1))
        apply(s, capture(e='retry', n=amount))
        expected(s, 'refunded', amount, amount, 0)
    check('pending_over_refund_atomic', '先到退款不被非法支付覆盖或丢失', under_capture)

    def failure_monotonic():
        s = new()
        apply(s, failed())
        expected(s, 'pending', 0, 0, 0)
        apply(s, capture())
        apply(s, refund(n=137))
        apply(s, failed(e='late-failure'))
        expected(s, 'partially_refunded', amount, 137, 1)
    check('failure_monotonic', '失败通知不能撤销支付与退款', failure_monotonic)

    def multi_payment():
        s = new()
        apply(s, capture())
        apply(s, capture(e='p2', p='p2', n=500))
        apply(s, refund(p='p2', n=500))
        expected(s, 'partially_refunded', amount+500, 500, 1)
        rejects(s, refund(e='too-much', p='p2', r='r2', n=1))
    check('payment_isolation', '多笔支付独立归并，不能跨支付抵扣', multi_payment)

    def scoped_refund_id():
        s = new()
        apply(s, capture())
        apply(s, capture(e='p2', p='p2', n=500))
        apply(s, capture(e='o2', o='o2', n=500))
        apply(s, refund())
        apply(s, refund(e='r2', p='p2', n=500))
        apply(s, refund(e='r3', o='o2', n=500))
        expected(s, 'refunded', amount+500, amount+500, 1)
        expected(s, 'refunded', 500, 500, 1, o='o2')
    check('refund_key_scope', '退款 ID 在订单和支付作用域内唯一', scoped_refund_id)

    def event_collision():
        for changed in [capture(o='other'), capture(p='other'), capture(n=amount+1), failed(e='c')]:
            s = new()
            apply(s, capture())
            rejects(s, changed)
    check('event_collision', '同投递 ID 语义冲突必须原子拒绝', event_collision)

    def validation():
        events = [capture(n=True), capture(n=0), capture(n=-1), capture(n=1.5), capture(n='100'),
                  {}, dict(event_id='x', order_id='o', kind='unknown'), {**capture(), 'payment_id': ''},
                  {**capture(), 'event_id': ''}, {**capture(), 'order_id': ''},
                  {**refund(), 'refund_id': ''}, {**capture(), 'payment_id': None}]
        s = new()
        for event in events:
            rejects(s, event)
        apply(s, capture())
        expected(s, 'paid', amount, 0, 1)
    check('validation_atomic', '输入校验严格且不污染去重状态', validation)

    def restart():
        s = new()
        apply(s, capture())
        apply(s, refund(n=137))
        s = json.loads(json.dumps(s))
        apply(s, capture(e='replayed'))
        apply(s, refund(e='replayed-r', n=137))
        expected(s, 'partially_refunded', amount, 137, 1)
        before = json.dumps(s, sort_keys=True)
        assert view(s, 'absent') == dict(status='pending', captured_cents=0, refunded_cents=0, fulfillments=0)
        assert json.dumps(s, sort_keys=True) == before
    check('restart_durable', '重启重放仍幂等，查询无副作用', restart)

    def permutations():
        events = [capture(), capture(e='retry'), refund(e='r1', r='r1', n=137),
                  refund(e='r2', r='r2', n=211), failed()]
        # The 120 orderings all have positive net capture, so fulfillment is 1.
        for order in itertools.permutations(events):
            s = new()
            for event in order:
                apply(s, event)
                s = json.loads(json.dumps(s))
            expected(s, 'partially_refunded', amount, 348, 1)
    check('permutation_convergence', '120 种乱序与重启组合收敛到相同结果', permutations)

    passed = sum(r['passed'] for r in results)
    return dict(schema='payment-acceptance-v1', variant=variant, passed=passed, total=len(results),
                failed=len(results)-passed, pass_rate=passed/len(results), checks=results,
                reward=2*passed/len(results)-1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('module')
    parser.add_argument('--variant', required=True, choices=['holdout', 'training_a', 'training_b'])
    args = parser.parse_args()
    try:
        result = evaluate(Path(args.module).resolve(), args.variant)
    except Exception as exc:
        result = dict(schema='payment-acceptance-v1', variant=args.variant, passed=0, total=18, failed=18,
                      pass_rate=0, reward=-1, checks=[dict(key='module_load', title='公开接口可加载', passed=False, detail=str(exc)[:450])])
    print(json.dumps(result, ensure_ascii=False))
