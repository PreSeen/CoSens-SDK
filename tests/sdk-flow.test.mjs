import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

async function fixture(){
 class Element extends EventTarget{
  constructor(dataset={}){super();this.dataset=dataset;this.attrs={x:'52'};const names=new Set();this.classList={toggle(name,active){if(active)names.add(name);else names.delete(name);},contains:name=>names.has(name)};}
  setAttribute(key,value){this.attrs[key]=value;}
  getAttribute(key){return this.attrs[key];}
  replaceChildren(){}
 }
 const ids=new Map(['svg','.sdk-scenes','#play','#question','#answer','#story-title','#story-caption','#position','#memory-line','#method-line','#validation','#you-kicker','#agent-kicker','#restart','#next'].map(key=>[key,new Element()]));
 const paths=['conversation','import','recall','learn','experience'];
 const groups={
  '[data-frame]':Array.from({length:7},(_,index)=>new Element({frame:String(index)})),
  '[data-node]':['you','world','agent','memory','learn'].map(node=>new Element({node})),
  '[data-path]':paths.map(path=>new Element({path})),
  '[data-flow-label]':paths.map(flowLabel=>new Element({flowLabel}))
 };
 const root=new Element();root.querySelector=key=>ids.get(key);root.querySelectorAll=key=>groups[key];
 const doc=new Element();doc.hidden=false;doc.querySelector=()=>root;doc.createElementNS=()=>new Element();
 let now=0,nextId=0,intersection;
 const timers=new Map();
 const source=await readFile(new URL('../dist/sdk-flow.mjs',import.meta.url),'utf8');
 runInNewContext(source.replaceAll('export ',''),{
  document:doc,matchMedia:()=>({matches:false,addEventListener(){}}),
  IntersectionObserver:class{constructor(callback){intersection=callback;}observe(){}},
  setTimeout:(fn,delay)=>{timers.set(++nextId,{fn,due:now+delay});return nextId;},clearTimeout:id=>timers.delete(id)
 });
 function tick(ms){const end=now+ms;while(true){const job=[...timers].filter(([,item])=>item.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];if(!job)break;timers.delete(job[0]);now=job[1].due;job[1].fn();}now=end;}
 return{ids,groups,root,tick,visible:value=>intersection([{isIntersecting:value}]),hidden:value=>{doc.hidden=value;doc.dispatchEvent(new Event('visibilitychange'));}};
}

test('production SDK playback waits for the figure, pauses offscreen and respects hidden tabs',async()=>{
 const f=await fixture();
 f.tick(60000);assert.equal(f.ids.get('#position').textContent,'01 / 07');
 assert.equal(f.ids.get('svg').classList.contains('paused'),true);
 f.visible(true);f.tick(4500);assert.equal(f.ids.get('#position').textContent,'02 / 07');
 f.visible(false);f.tick(60000);assert.equal(f.ids.get('#position').textContent,'02 / 07');
 f.visible(true);f.hidden(true);f.tick(60000);assert.equal(f.ids.get('#position').textContent,'02 / 07');
 f.hidden(false);f.tick(5500);assert.equal(f.ids.get('#position').textContent,'03 / 07');
});

test('actual scene controls reverse one conversation path and expose the matching label',async()=>{
 const f=await fixture();
 f.visible(true);
 f.groups['[data-frame]'][2].dispatchEvent(new Event('click'));
 const conversation=f.groups['[data-path]'].find(item=>item.dataset.path==='conversation');
 assert.equal(conversation.attrs['marker-start'],'url(#sdk-flow-arrow)');
 assert.equal(conversation.attrs['marker-end'],'none');
 assert.equal(f.groups['[data-flow-label]'][0].textContent,'Reply');
 f.groups['[data-frame]'][3].dispatchEvent(new Event('click'));
 assert.equal(conversation.attrs['marker-end'],'url(#sdk-flow-arrow)');
 assert.equal(f.groups['[data-flow-label]'][0].textContent,'Feedback');
});

test('SDK story loops through the last scene and continues after selecting a scene',async()=>{
 const f=await fixture();f.visible(true);
 f.groups['[data-frame]'][6].dispatchEvent(new Event('click'));
 f.tick(6500);assert.equal(f.ids.get('#position').textContent,'01 / 07');
 f.tick(4500);assert.equal(f.ids.get('#position').textContent,'02 / 07');
});

test('hover holds playback and leaving never overrides a manual pause',async()=>{
 const f=await fixture();f.visible(true);
 const progress=f.ids.get('.sdk-scenes');
 progress.dispatchEvent(new Event('pointerenter'));
 f.tick(60000);assert.equal(f.ids.get('#position').textContent,'01 / 07');
 assert.equal(f.ids.get('svg').classList.contains('paused'),true);
 progress.dispatchEvent(new Event('pointerleave'));
 f.tick(4500);assert.equal(f.ids.get('#position').textContent,'02 / 07');
 f.ids.get('#play').dispatchEvent(new Event('click'));
 progress.dispatchEvent(new Event('pointerenter'));progress.dispatchEvent(new Event('pointerleave'));
 f.tick(60000);assert.equal(f.ids.get('#position').textContent,'02 / 07');
 f.ids.get('#play').dispatchEvent(new Event('click'));
 f.tick(5500);assert.equal(f.ids.get('#position').textContent,'03 / 07');
});

test('hovering the SDK figure or story does not stop playback',async()=>{
 const f=await fixture();f.visible(true);
 f.root.dispatchEvent(new Event('pointerenter'));
 f.ids.get('svg').dispatchEvent(new Event('pointerenter'));
 f.ids.get('#story-caption').dispatchEvent(new Event('pointerenter'));
 f.tick(4500);
 assert.equal(f.ids.get('#position').textContent,'02 / 07');
 assert.equal(f.ids.get('svg').classList.contains('paused'),false);
});
