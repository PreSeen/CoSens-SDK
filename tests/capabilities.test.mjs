import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {capabilities,setupCapabilities} from '../dist/capabilities.mjs';

test('four capabilities connect specific context, feedback, checks and a different next task',()=>{
 assert.deepEqual(capabilities.map(item=>item.id),['research','writing','analysis','clients']);
 for(const item of capabilities){
  for(const key of ['prompt','memory','feedback','method','check','nextTask','nextAnswer','scope'])assert.ok(item[key].length>15,`${item.id}: ${key}`);
  assert.notEqual(item.prompt,item.nextTask);
 }
 assert.ok(capabilities[0].scope.includes('historical'));
 assert.ok(capabilities[1].scope.includes('Internal'));
 assert.ok(capabilities[2].nextAnswer.includes('customer mix'));
 assert.ok(capabilities[3].scope.includes('another customer'));
});

test('actual tab handlers update all fields and support keyboard movement without leaking previous case text',()=>{
 class Button extends EventTarget{
  constructor(index){super();this.dataset={capability:String(index)};this.attributes={};}
  setAttribute(key,value){this.attributes[key]=value;}
  focus(){this.focused=true;}click(){this.dispatchEvent(new Event('click'));}
 }
 const buttons=capabilities.map((_,index)=>new Button(index));
 const fields=new Map(['title','need','prompt','memory','feedback','method','check','nextTask','nextAnswer','scope'].map(key=>[key,{textContent:''}]));
 const panel={setAttribute(key,value){this[key]=value;}};
 const root={dataset:{},querySelectorAll:()=>buttons,querySelector:selector=>selector==='[data-capability-panel]'?panel:fields.get(selector.slice('[data-example="'.length,-2))};
 setupCapabilities(root);
 assert.equal(fields.get('prompt').textContent,capabilities[0].prompt);
 buttons[2].click();
 for(const [key,node]of fields)assert.equal(node.textContent,capabilities[2][key]);
 assert.equal(buttons[2].attributes['aria-selected'],'true');
 const event=new Event('keydown',{cancelable:true});Object.defineProperty(event,'key',{value:'ArrowRight'});buttons[2].dispatchEvent(event);
 assert.equal(root.dataset.capability,'clients');
 assert.equal(panel['aria-labelledby'],'capability-tab-clients');
 assert.equal(buttons[3].focused,true);
 assert.equal(buttons[2].attributes['aria-selected'],'false');
});

test('hero restores the three layers while capability examples replace abstract repeated sections',async()=>{
 const home=await readFile(new URL('../dist/index.html',import.meta.url),'utf8');
 const hero=home.slice(home.indexOf('<section class="hero connect-hero"'),home.indexOf('</section>')+10);
 assert.ok(hero.includes('id="brain-diagram"'));
 assert.equal(hero.includes('id="conversation-story"'),false);
 assert.ok(home.includes('Learn · RSI'));
 assert.ok(home.includes('Better at your kind of work.'));
 assert.equal(home.includes('AI knows a lot.'),false);
 assert.equal(home.includes('Pick up<br>where you left off.'),false);
 assert.equal(home.includes('Choose what<br>becomes memory.'),false);
 assert.ok(home.includes('Your experience,<br><span class="headline-accent">always evolving.</span>'));
 assert.ok(home.includes('Bring what you know. Build on what you learn.'));
 assert.ok(home.indexOf('id="experience"')<home.indexOf('id="choose-ai"'));
 assert.ok(home.includes('id="first-recall"'));
 assert.ok(home.includes('href="/assets/first-memory.md"'));
 assert.ok(home.includes('data-capability-panel'));
});
