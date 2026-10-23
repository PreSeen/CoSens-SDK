"""Verify actual Recipe quality observations while preserving deferred release."""
import json
from pathlib import Path
import httpx
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parent;PROJECT=ROOT.parents[1]
cfg=dotenv_values(PROJECT/'var/local.env');headers={'Authorization':'Bearer '+cfg['COSENS_RSI_TOKEN']}
with httpx.Client(base_url='http://127.0.0.1:18104',headers=headers,timeout=15) as c:
 cases=c.get('/v1/cases').raise_for_status().json()['items'];selected=next(x for x in cases if x['title']=='中文退款审核：Recipe 版本迭代与质量对照')
 case=c.get('/v1/cases/'+selected['case_id']).raise_for_status().json();x=case['recipe_evolution']
 assert x['available'] and x['status']=='deferred' and x['decision']=='deferred'
 assert len(x['pairs'])==4 and x['after_score']>x['before_score'] and x['after_revision'] is None
 active=c.get('/v1/recipes/active?task_family=refund-recipe-quality-luna-v1').raise_for_status().json()
 assert active['recipe_id']==x['before_recipe_id'] and active['revision']==1
 draft=c.get(f"/v1/cases/{selected['case_id']}/improvements/{x['job_id']}/recipe").raise_for_status().json()
 assert draft['published'] is False and draft['bundle_hash']!=active['bundle_hash']
 other=next(i for i in cases if i['case_id']!=selected['case_id'])
 assert c.get(f"/v1/cases/{other['case_id']}/improvements/{x['job_id']}/recipe").status_code==404
 artifact=x['pairs'][0]['after_files'][0]['artifact_id']
 assert c.get('/v1/cases/'+other['case_id']+'/artifacts/'+artifact).status_code==404
checks=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path='/tmp/cosens-dashboard-browsers/chromium-1248/chrome-linux64/chrome',args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1440,'height':1100},locale='zh-CN',timezone_id='Asia/Shanghai',accept_downloads=True)
 page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:18104/dashboard?case='+selected['case_id']);page.evaluate('(v)=>localStorage.setItem("cosens.rsi.token",v)',cfg['COSENS_RSI_TOKEN']);page.reload()
 panel=page.locator('#recipe-evolution');expect(panel).to_contain_text('Recipe 版本迭代与任务质量')
 expect(panel).to_contain_text('证据不足，未发布');expect(panel.locator('.gain .number')).to_contain_text(f"{x['delta']*100:.2f}")
 expect(panel).to_contain_text(f"{x['before_score']*100:.2f}");expect(panel).to_contain_text('100.00')
 expect(panel.locator('button[data-candidate-recipe]')).to_be_visible()
 checks.append('four real confirm pairs, actual 6.34-point observed gain and clearly deferred release')
 # The main Trace now contains the real paired evaluations, with no fabricated Run.
 assert len(case['runs'])==1
 expect(page.locator('.run-tabs')).to_contain_text('RSI 前 · 原始 Recipe')
 expect(page.locator('.run-tabs')).to_contain_text('RSI 后 · 候选 Recipe（未发布）')
 target=next(pair for pair in x['pairs'] if pair['task_id']=='confirm-old-policy')
 page.locator('#trace-task').select_option(target['task_id'])
 expect(page.locator('.run-tab.active')).to_contain_text('RSI 前')
 node=next(s for s in target['before_trace']['stages'] if s['id'].startswith('node:'))
 page.locator('[data-step="'+node['id']+'"]').click()
 expect(page.locator('.step-body')).to_contain_text('执行方法')
 before_method=target['before_trace']['stages'][1]['inputs'][0]['text']
 expect(page.locator('.step-body')).to_contain_text(before_method[:70])
 page.locator('[data-run="'+target['after_trace']['trace_id']+'"]').click()
 page.locator('[data-step="node:0"]').click()
 expect(page.locator('.step-body')).to_contain_text('补证')
 expect(page.locator('.step-body')).to_contain_text('审核报告.md')
 record=next(a for a in target['after_files'] if a['name']=='执行记录.md')
 page.evaluate('(id)=>preview(id)',record['artifact_id'])
 expect(page.locator('#artifact-content')).to_contain_text('执行')
 page.locator('#close-modal').click()
 page.locator('[data-step="acceptance"]').click()
 expect(page.locator('.step-body')).to_contain_text('32/32 项业务检查通过')
 page.evaluate("async()=>{state.last='';await loadCase(state.caseId)}")
 expect(page.locator('.run-tab.active')).to_contain_text('RSI 后')
 expect(page.locator('.step-body')).to_contain_text('独立业务验收')
 assert page.locator('#trace-task').input_value()==target['task_id']
 page.locator('[data-run="'+case['runs'][0]['run_id']+'"]').click()
 expect(page.locator('.flow-nav')).to_contain_text('Memory')
 # A comparison row provides a direct link to the matching paired Trace.
 panel.locator('[data-trace-pair="'+target['task_id']+'"]').click()
 expect(page.locator('.run-tab.active')).to_contain_text('RSI 前')
 assert page.locator('#trace-task').input_value()==target['task_id']
 checks.append('paired before/after Trace: frozen methods, actual deliverables and execution log, 32/32 acceptance; selection survives refresh; formal Memory trace retained')

 page.screenshot(path=str(ROOT/'recipe-candidate-overview.png'),full_page=True)
 summary=panel.locator('details.history > summary').filter(has_text='采用的变更').first;summary.click()
 expect(panel).to_contain_text('修改后的方法')
 checks.append('model-generated chosen Recipe change can be compared with original method')
 index=next(i for i,pair in enumerate(x['pairs']) if pair['before_score']<pair['after_score'])
 key=selected['case_id']+':recipe-evolution:pair:'+str(index)
 detail=page.locator('details[data-open="'+key+'"]');detail.locator(':scope > summary').click()
 expect(detail).to_contain_text('发生变化的业务检查');expect(detail).to_contain_text('查看完整正文')
 detail.locator('button.file').filter(has_text='审核报告.md').last.click();expect(page.locator('#artifact-content')).to_contain_text('退款');page.locator('#close-modal').click()
 page.evaluate('(id)=>preview(id)',x['pairs'][index]['after_files'][0]['artifact_id']);expect(page.locator('#artifact-content')).to_contain_text('本次退款');assert 'cash_total_cents' not in page.locator('#artifact-content').inner_text();page.locator('#close-modal').click()
 checks.append('confirm task inputs, old/new output reports, per-check changes and readable financial result preview')
 with page.expect_download() as event:panel.locator('button[data-candidate-recipe]').click()
 downloaded=json.loads(Path(event.value.path()).read_text());assert downloaded['published'] is False and downloaded['bundle_hash']==draft['bundle_hash']
 with page.expect_download() as event:panel.locator('button[data-recipe-run]').first.click()
 original=json.loads(Path(event.value.path()).read_text());assert original['recipe_id']==active['recipe_id'] and original['bundle_hash']!=downloaded['bundle_hash']
 checks.append('old active Recipe and actual unpublished candidate download; no pointer promotion')
 detail.locator(':scope > summary').click();page.set_viewport_size({'width':390,'height':844})
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
 page.screenshot(path=str(ROOT/'recipe-candidate-mobile.png'),full_page=True)
 assert not errors,errors;checks.append('mobile fits viewport; no JavaScript errors; cross-case artifacts and candidate rejected')
 browser.close()
result={'case_id':selected['case_id'],'job_id':x['job_id'],'checks':checks,'errors':errors,'comparison':x,'active_recipe_id':active['recipe_id']}
(ROOT/'candidate-browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
(ROOT/'recipe-candidate.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'case_id':selected['case_id'],'checks':checks,'errors':errors},ensure_ascii=False))
