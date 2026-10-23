"""Browser acceptance for the live two-run demo; no credentials in output."""
import json
from pathlib import Path
from dotenv import dotenv_values
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parents[1]
cfg=dotenv_values(PROJECT/'var/local.env')
headers={'Authorization':'Bearer '+cfg['COSENS_RSI_TOKEN']}
with httpx.Client(base_url='http://127.0.0.1:18104',headers=headers,timeout=10) as c:
    cases=c.get('/v1/cases').raise_for_status().json()['items']
    item=next(x for x in cases if x['title']=='中文供应商月结对账：一次反馈后的 RSI 选路改善')
    case=c.get('/v1/cases/'+item['case_id']).raise_for_status().json()
    assert len(case['runs'])==2 and all(r['status']=='completed' for r in case['runs'])
    assert case['comparison']['available']
    assert case['comparison']['quality_equal']
    expected_recipe=next(r['recipe_id'] for r in case['runs'] if r['run_id']==case['comparison']['after_run_id'])
    assert c.get('/v1/cases/'+item['case_id']+'/runs/nonexistent/recipe').status_code==404
checks=[];errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/tmp/cosens-dashboard-browsers/chromium-1248/chrome-linux64/chrome',args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1100},locale='zh-CN',timezone_id='Asia/Shanghai',accept_downloads=True)
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:18104/dashboard?case='+item['case_id'])
    page.evaluate('(token)=>localStorage.setItem("cosens.rsi.token",token)',cfg['COSENS_RSI_TOKEN'])
    page.reload()
    expect(page.locator('.heading h1')).to_have_text(item['title'])
    expect(page.locator('.run-tab')).to_have_count(2)
    expect(page.locator('.gain .caption')).to_contain_text('含索引构建')
    expect(page.locator('.gain .number')).to_contain_text(f"{case['comparison']['speedup']:.2f}")
    expect(page.locator('.step-body')).to_contain_text('负反馈降低了旧路线评分')
    checks.append('two real runs, actual gain, cold index cost and negative-evidence explanation')
    page.screenshot(path=str(ROOT/'demo-overview.png'),full_page=True)
    page.locator('[data-step="memory"]').click()
    expect(page.locator('.step-body')).to_contain_text('1 条已验证经验')
    checks.append('one verified baseline experience recalled by RSI')
    page.locator('[data-step="produce"]').click()
    page.locator('button.file').filter(has_text='供应商对账报告.md').first.click()
    expect(page.locator('#artifact-content')).to_contain_text('876')
    page.locator('#close-modal').click()
    checks.append('Chinese report can be read without raw JSON')
    with page.expect_download() as event:
        page.locator('[data-recipe-run]').click()
    download=event.value
    downloaded=json.loads(Path(download.path()).read_text())
    assert downloaded['recipe_id']==expected_recipe
    checks.append('downloaded Recipe is the selected Run frozen version')
    page.locator('[data-step="decision:produce"]').click()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(ROOT/'demo-mobile.png'),full_page=True)
    assert not errors,errors
    checks.append('mobile no overflow; no browser JavaScript errors')
    browser.close()
result={'case_id':item['case_id'],'checks':checks,'comparison':case['comparison'],'errors':errors}
(ROOT/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'case_id':item['case_id'],'checks':checks,'errors':errors},ensure_ascii=False))
