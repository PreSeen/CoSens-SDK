"""Operator-only checker; source code under test receives no evaluation spec."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def main():
    candidate,spec_path=sys.argv[1:3];spec=json.loads(Path(spec_path).read_text())
    oracle=load(Path(__file__).with_name('reference.py'),'oracle')
    groups=oracle.scenarios(spec);outcomes=[]
    try:
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):module=load(candidate,'candidate')
        import_error=None
    except Exception as exc:module=None;import_error=type(exc).__name__
    for group in groups:
        passed=True;errors=[]
        for call in group['calls']:
            if module is None:passed=False;errors.append('module_import_'+import_error);continue
            original=deepcopy(call['args']);args=deepcopy(original)
            try:
                with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    value=getattr(module,call['fn'])(*args)
                ok='error' not in call and value==call['expected'] and type(value)==type(call['expected']) and (not call.get('assert_pure') or args==original)
                if not ok:errors.append('return_value_or_purity_mismatch')
            except Exception as exc:
                ok=call.get('error')=='ValueError' and type(exc) is ValueError
                if not ok:errors.append(type(exc).__name__)
            passed=passed and ok
        outcomes.append({'key':group['key'],'title':group['title'],'passed':passed,'critical':group['critical'],
                         'detail':'独立回放符合公开契约' if passed else ','.join(sorted(set(errors)))})
    passed=sum(x['passed'] for x in outcomes)
    print(json.dumps({'judge_valid':True,'score':passed/len(outcomes),'task_success':passed==len(outcomes),
        'passed':passed,'total':len(outcomes),'quality_checks':outcomes,
        'feedback':'独立代码回放：'+('全部通过' if passed==len(outcomes) else '未通过：'+'；'.join(x['title']+'('+x['detail']+')' for x in outcomes if not x['passed']))},ensure_ascii=False))


if __name__=='__main__':main()
