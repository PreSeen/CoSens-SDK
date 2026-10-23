"""Real browser acceptance for Recipe changes and independently paired quality."""
import json
from pathlib import Path
import httpx
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parent;PROJECT=ROOT.parents[1]
cfg=dotenv_values(PROJECT/'var/local.env');headers={'Authorization':'Bearer '+cfg['COSENS_RSI_TOKEN']}
with httpx.Client(base_url='http://127.0.0.1:18104',headers=headers,timeout=15) as c:
 cases=c.get('/v1/cases').raise_for_status().json()['items']
 selected=next(x for x in cases if x['title']=='中文退款审核：Recipe 版本迭代与质量对照')
 case=c.get('/v1/cases/'+selected['case_id']).raise_for_status().json();x=case['recipe_evolution']
 assert x['available'] and x['status']=='published' and x['acceptance_path']=='quality'
 assert x['before_revision']==1 and x['after_revision']==2 and x['base_hash']!=x['candidate_hash']
 assert len(x['pairs'])==4 and x['after_score']>x['before_score']
 other=next(i for i in cases if i['case_id']!=selected['case_id'])
 ref=x['pairs'][0]['after_files'][0]['artifact_id']
 assert c.get('/v1/cases/'+other['case_id']+'/artifacts/'+ref).status_code==404
checks=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path='/tmp/cosens-dashboard-browsers/chromium-1248/chrome-linux64/chrome',args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1440,'height':1100},locale='zh-CN',timezone_id='Asia/Shanghai',accept_downloads=True)
 page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:18104/dashboard?case='+selected['case_id'])
 page.evaluate('(v)=>localStorage.setItem("cosens.rsi.token",v)',cfg['COSENS_RSI_TOKEN']);page.reload()
 expect(page.locator('.heading h1')).to_have_text(selected['title'])
 expect(page.locator('#recipe-evolution .gain .number')).to_contain_text(f"{x['delta']*100:.2f}")
 expect(page.locator('#recipe-evolution')).to_contain_text('新版 Recipe 已发布')
 expect(page.locator('#recipe-evolution')).to_contain_text('新工单独立确认')
 expect(page.locator('.run-tab')).to_have_count(2)
 checks.append('published v1 to v2 Recipe, real independent confirm quality, two actual task deliveries')
 page.screenshot(path=str(ROOT/'recipe-demo-overview.png'),full_page=True)
 summary=page.locator('#recipe-evolution details.history > summary').filter(has_text='采用的变更').first
 summary.click();expect(page.locator('#recipe-evolution')).to_contain_text(next(c['slot'] for c in x['changes'] if c['selected']))
 checks.append('actual chosen Recipe method change can be expanded')
 pair_index=next(i for i,pair in enumerate(x['pairs']) if any(c['before']!=c['after'] for c in pair['checks']))
 key=selected['case_id']+':recipe-evolution:pair:'+str(pair_index)
 detail=page.locator('details[data-open="'+key+'"]');detail.locator(':scope > summary').click()
 expect(detail).to_contain_text('查看完整正文')
 expect(detail).to_contain_text('发生变化的业务检查')
 detail.locator('button.file').filter(has_text='审核报告.md').last.click()
 expect(page.locator('#artifact-content')).to_contain_text('退款')
 page.locator('#close-modal').click()
 checks.append('confirm inputs, old/new reports and changed business checks readable without backend JSON')
 json_ref=x['pairs'][pair_index]['after_files'][0]['artifact_id']
 page.evaluate('(id)=>preview(id)',json_ref)
 expect(page.locator('#artifact-content')).to_contain_text('本次退款')
 assert 'cash_total_cents' not in page.locator('#artifact-content').inner_text()
 page.locator('#close-modal').click()
 versions=[]
 for button in page.locator('#recipe-evolution button[data-recipe-run]').all():
  with page.expect_download() as event:button.click()
  value=json.loads(Path(event.value.path()).read_text());versions.append(value)
 assert {v['revision'] for v in versions}=={1,2}
 assert len({v['bundle_hash'] for v in versions})==2
 checks.append('downloaded old and new immutable Recipes have distinct versions and hashes')
 page.locator('.run-tab').first.click();expect(page.locator('#content')).to_contain_text('Recipe v1')
 page.locator('.run-tab').last.click();expect(page.locator('#content')).to_contain_text('Recipe v2')
 checks.append('source and new-method execution traces selectable')
 detail=page.locator('details[data-open="'+key+'"]')
 if detail.get_attribute('open') is not None:detail.locator(':scope > summary').click()
 page.set_viewport_size({'width':390,'height':844})
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
 page.screenshot(path=str(ROOT/'recipe-demo-mobile.png'),full_page=True)
 assert not errors,errors
 checks.append('390px no page overflow, no browser errors, cross-case audit access rejected')
 browser.close()
(ROOT/'browser-check.json').write_text(json.dumps({'case_id':selected['case_id'],'checks':checks,'errors':errors},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'case_id':selected['case_id'],'checks':checks,'errors':errors},ensure_ascii=False))
