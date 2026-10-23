import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const read=path=>readFile(new URL('../dist/'+path,import.meta.url),'utf8');

test('Connect keeps its visible dinner example while Build explains the SDK workflow',async()=>{
 const home=await read('index.html'),build=await read('build/index.html');
 assert.ok(home.includes('id="demo-panel"'));
 assert.ok(home.includes('Find somewhere for dinner'));
 assert.equal(home.includes('id="brain-diagram"'),true);
 assert.equal(build.includes('id="brain-diagram"'),false);
 assert.equal(home.includes('class="consumer-preview"'),false);
 assert.equal(home.includes('data-sdk-flow'),false);
 assert.equal(home.includes('id="connect-example"'),false);
 assert.ok(build.includes('id="sdk-workflow"'));
 assert.ok(build.indexOf('class="sdk-story"')<build.indexOf('class="sdk-diagram-scroll"'));
 const hero=build.slice(build.indexOf('<section id="build"'),build.indexOf('<div class="strip"'));
 assert.ok(hero.includes('</div><div class="code-window"'));
 assert.ok(build.includes('id="sdk-label"'));
 assert.equal(build.includes('id="demo-panel"'),false);
 assert.ok(build.includes('03 / FEEDBACK API'));
 assert.ok(build.includes('href="/docs/#feedback"'));
 assert.ok(build.indexOf('id="developer"')<build.indexOf('id="benchmarks"'));
});

test('Docs sends everyday examples to Connect and SDK flow readers to Build',async()=>{
 const docs=await read('docs/docs.mjs');
 assert.ok(docs.includes('href="/#how-it-works">Explore the dinner example'));
 assert.ok(docs.includes('href="/build/#how-it-works">Explore the SDK workflow'));
 assert.equal(docs.includes('Explore the pilot scenario'),false);
});

test('Feedback has its own reachable API article and consistent reference names',async()=>{
 const docs=await read('docs/docs.mjs'),shell=await read('docs/index.html');
 const pages=runInNewContext(docs.slice(docs.indexOf('const code='),docs.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');
 for(const [key,name]of [['memory','Memory API'],['recipes','Recipe API'],['feedback','Feedback API']]){
  assert.equal(pages[key].nav,name);assert.equal(pages[key].title,name);
  assert.ok(shell.includes(`href="#${key}">${name}</a>`));
 }
 const feedback=pages.feedback.sections.flat().join(' ');
 assert.ok(feedback.includes('client.feedback.submit'));
 assert.ok(feedback.includes('artifact_version / artifactVersion'));
 assert.ok(feedback.includes('does not confirm'));
 assert.ok(pages.quickstart.sections.flat().join(' ').includes('href="#feedback"'));
});
