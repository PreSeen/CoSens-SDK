import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as app from '../dist/app.mjs';
import {readFile} from 'node:fs/promises';
function fakeClock(){let now=0,next=0;const jobs=new Map();return {schedule(fn,delay){jobs.set(++next,{fn,due:now+delay});return next;},cancel(id){jobs.delete(id);},tick(ms){const target=now+ms;while(true){const entry=[...jobs].filter(([,j])=>j.due<=target).sort((a,b)=>a[1].due-b[1].due)[0];if(!entry)break;now=entry[1].due;jobs.delete(entry[0]);entry[1].fn();}now=target;}};}
function fixture(){assert.equal(typeof app.createAutoRotation,'function');const clock=fakeClock();let advances=0;const controller=app.createAutoRotation({onAdvance:()=>advances++,...clock});return {clock,controller,count:()=>advances};}
test('rotation advances every ten seconds, independently per region',()=>{const a=fixture(),b=fixture();a.clock.tick(9999);assert.equal(a.count(),0);a.clock.tick(1);assert.equal(a.count(),1);assert.equal(b.count(),0);a.clock.tick(20000);assert.equal(a.count(),3);b.clock.tick(10000);assert.equal(b.count(),1);});
test('hover pauses and leaving starts a fresh ten-second reading interval',()=>{const f=fixture();f.clock.tick(9000);f.controller.pause('hover');f.clock.tick(30000);assert.equal(f.count(),0);f.controller.resume('hover');f.clock.tick(9999);assert.equal(f.count(),0);f.clock.tick(1);assert.equal(f.count(),1);});
test('touch selection permanently stops automatic rotation',()=>{const f=fixture();f.controller.stop();f.controller.pause('hover');f.controller.resume('hover');f.clock.tick(60000);assert.equal(f.count(),0);});
test('overlapping focus, reduced-motion and hover holds cannot restart each other',()=>{const f=fixture();f.controller.pause('focus');f.controller.pause('reduced');f.controller.resume('focus');f.clock.tick(20000);assert.equal(f.count(),0);f.controller.pause('hover');f.controller.resume('reduced');f.clock.tick(10000);assert.equal(f.count(),0);f.controller.resume('hover');f.clock.tick(10000);assert.equal(f.count(),1);});
test('all objects shift toward the front at 60 percent and navigation stays frosted',async()=>{const html=await readFile(new URL('../dist/index.html',import.meta.url),'utf8');for(const cls of ['reason-cube','memory-cluster regular-cluster','learn-pyramid'])assert.ok(html.includes(`class="${cls}" transform="translate(0 17)"`));const css=await readFile(new URL('../dist/styles.css',import.meta.url),'utf8');assert.match(css,/\.nav\{position:sticky;top:0;z-index:30/);assert.ok(css.includes('backdrop-filter:blur(18px)'));assert.ok(css.includes('animation:directional-travel 1.6s linear infinite'));});

// Exercise the actual page initialization and event bindings with a small DOM surface.
async function pageFixture(withDinner=true){
 const {runInNewContext}=await import('node:vm');
 class Element extends EventTarget {
  constructor(dataset={}){super();this.dataset=dataset;this.attrs={};const names=new Set();this.classList={contains:n=>names.has(n),toggle:n=>names.has(n)?names.delete(n):names.add(n)};this.style={};}
  setAttribute(k,v){this.attrs[k]=v;} getAttribute(k){return this.attrs[k];}
  querySelector(){return null;} querySelectorAll(){return [];} contains(){return false;}
 }
 const layers=[0,1,2].map(layer=>new Element({layer:String(layer)}));
 const tabs=[0,1,2,3].map(step=>new Element({step:String(step)}));
 const ids=new Map(['brain-diagram','motion-toggle','demo-panel','layer-detail','layer-leader','layer-anchor','layer-end','how-it-works','reset-demo'].map(id=>['#'+id,new Element()]));
 if(!withDinner)ids.delete('#demo-panel');
 const doc=new Element();doc.hidden=false;doc.querySelector=id=>ids.get(id);doc.querySelectorAll=selector=>selector==='[data-layer]'?layers:selector==='[data-step]'?tabs:[];
 const clock=fakeClock();const source=await readFile(new URL('../dist/app.mjs',import.meta.url),'utf8');
 runInNewContext(source.replaceAll('export ',''),{document:doc,matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout:clock.schedule,clearTimeout:clock.cancel});
 const selected=()=>layers.find(e=>e.attrs['aria-pressed']==='true').dataset.layer;
 const step=()=>tabs.find(e=>e.attrs['aria-selected']==='true').dataset.step;
 const fire=(id,type,extra={})=>{const event=new Event(type);for(const [k,v]of Object.entries(extra))Object.defineProperty(event,k,{value:v});ids.get(id).dispatchEvent(event);};
 return {clock,selected,step,fire,layers};
}
test('page initialization binds exactly one independent timer to each region',async()=>{const f=await pageFixture();assert.equal(f.selected(),'0');assert.equal(f.step(),'0');f.clock.tick(10000);assert.equal(f.selected(),'1');assert.equal(f.step(),'1');f.clock.tick(10000);assert.equal(f.selected(),'2');assert.equal(f.step(),'2');});
test('actual hover and touch bindings pause only their own region',async()=>{const f=await pageFixture();f.fire('#brain-diagram','pointerenter',{pointerType:'mouse'});f.clock.tick(10000);assert.equal(f.selected(),'0');assert.equal(f.step(),'1');f.fire('#brain-diagram','pointerleave',{pointerType:'mouse'});f.fire('#how-it-works','pointerdown',{pointerType:'touch'});f.clock.tick(10000);assert.equal(f.selected(),'1');assert.equal(f.step(),'1');f.fire('#how-it-works','pointerleave',{pointerType:'mouse'});f.clock.tick(20000);assert.equal(f.selected(),'0');assert.equal(f.step(),'1');});

test('Layer controls respond independently of the dinner panel',async()=>{
 const f=await pageFixture(false);
 f.layers[2].dispatchEvent(new Event('click'));
 assert.equal(f.selected(),'2');
 f.layers[1].dispatchEvent(new Event('focus'));
 assert.equal(f.selected(),'1');
});
