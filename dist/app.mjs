export const examples = {
  wrapper:{py:`# SDK preview
# Your existing client
client = openai()

# Add one line
client = cosens(client)`,ts:`// SDK preview
// Your existing client
let client = openai();

// Add one line
client = cosens(client);`},
  recall:{py:`# SDK preview · proposed API
context = await client.memory.recall(
    task="Plan dinner after my hotel meeting",
    background="Vegetarian; quiet places; starting from the hotel"
)`,ts:`// SDK preview · proposed API
const context = await client.memory.recall({
  task: "Plan dinner after my hotel meeting",
  background: "Vegetarian; quiet places; starting from the hotel"
});`},
  memory:{py:`# SDK preview · proposed management API
memories = await client.memory.list()

await client.memory.update(
    id="memory_demo",
    content="I am vegetarian and prefer quiet places."
)`,ts:`// SDK preview · proposed management API
const memories = await client.memory.list();

await client.memory.update({
  id: "memory_demo",
  content: "I am vegetarian and prefer quiet places."
});`},
  recipe:{py:`# SDK preview · proposed management API
recipes = await client.recipes.list()

recipe = await client.recipes.get(
    id="dinner_timing", version="v1"
)
# Inspect the method, status and applicable scope.`,ts:`// SDK preview · proposed management API
const recipes = await client.recipes.list();

const recipe = await client.recipes.get({
  id: "dinner_timing", version: "v1"
});
// Inspect the method, status and applicable scope.`},
  recipeEdit:{py:`# SDK preview · proposed management API
# Editing produces a candidate version, not adoption.
await client.recipes.update(
    id="dinner_timing", base_version="v1",
    method="Check arrival time against last-order time before recommending dinner.",
    scope="Dinner planning with reliable last-order data"
)`,ts:`// SDK preview · proposed management API
// Editing produces a candidate version, not adoption.
await client.recipes.update({
  id: "dinner_timing", baseVersion: "v1",
  method: "Check arrival time against last-order time before recommending dinner.",
  scope: "Dinner planning with reliable last-order data"
});`},
  feedback:{py:`# SDK preview · proposed API
await client.feedback.submit(
    task_id="dinner_plan_001",
    artifact_version="dinner_plan_v1",
    message="Check arrival time against last-order time before recommending dinner.",
    reason="A restaurant can be open after its kitchen stops taking orders."
)`,ts:`// SDK preview · proposed API
await client.feedback.submit({
  taskId: "dinner_plan_001",
  artifactVersion: "dinner_plan_v1",
  message: "Check arrival time against last-order time before recommending dinner.",
  reason: "A restaurant can be open after its kitchen stops taking orders."
});`}
};
export async function copyExample(text,clipboard){
  if(!clipboard?.writeText)return 'Copy unavailable — select code';
  return clipboard.writeText(text).then(()=> 'Copied ✓',()=> 'Copy blocked — select code');
}
export function keyboardTabs(tabs) {
  tabs.forEach((tab,index)=>tab.addEventListener('keydown',event=>{
    let target;
    if(event.key==='ArrowRight'||event.key==='ArrowDown')target=(index+1)%tabs.length;
    if(event.key==='ArrowLeft'||event.key==='ArrowUp')target=(index+tabs.length-1)%tabs.length;
    if(event.key==='Home')target=0;
    if(event.key==='End')target=tabs.length-1;
    if(target!==undefined){event.preventDefault();tabs[target].focus();tabs[target].click();}
  }));
}
export function setupCodeBlocks(root=document) {
  root.querySelectorAll('[data-code]').forEach(block => {
    const example=examples[block.dataset.code];
    const code=block.querySelector('code');
    let lang=block.querySelector('[data-lang]').dataset.lang;
    code.textContent=example[lang];
    const tabs=[...block.querySelectorAll('[data-lang]')];
    tabs.forEach((tab,index)=>tab.tabIndex=index===0?0:-1);
    keyboardTabs(tabs);
    tabs.forEach(tab => tab.addEventListener('click',()=>{
      lang=tab.dataset.lang;code.textContent=example[lang];
      tabs.forEach(item=>{item.setAttribute('aria-selected',String(item===tab));item.tabIndex=item===tab?0:-1;});
      block.querySelector('[data-copy]').textContent='Copy ⧉';
    }));
    block.querySelector('[data-copy]').addEventListener('click', async event=>{
      const copy=event.currentTarget;
      copy.textContent=await copyExample(example[lang],navigator.clipboard);
      setTimeout(()=>{copy.textContent='Copy ⧉';},1800);
    });
  });
}
export function decorateBrand(root) {
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  const nodes=[];
  while(walker.nextNode()){
    const node=walker.currentNode;
    if(node.textContent.includes('CoSens')&&!node.parentElement.closest('code,pre,script,style,.brand-word'))nodes.push(node);
  }
  for(const node of nodes){
    const fragment=document.createDocumentFragment();
    node.textContent.split('CoSens').forEach((part,index)=>{
      if(index){const word=document.createElement('span');word.className='brand-word';
        const co=document.createElement('span');co.className='brand-co';co.textContent='Co';
        const sens=document.createElement('span');sens.className='brand-sens';sens.textContent='Sens';
        word.append(co,sens);fragment.append(word);}
      fragment.append(document.createTextNode(part));
    });
    node.replaceWith(fragment);
  }
}
export function setupSensableLoop(root=document){
 const figure=root.querySelector('[data-sensable-loop]');
 if(!figure)return;
 const button=figure.querySelector('[data-loop-motion]');
 button.addEventListener('click',()=>{
  const paused=figure.dataset.paused!=='true';
  figure.dataset.paused=String(paused);
  button.setAttribute('aria-pressed',String(paused));
  button.setAttribute('aria-label',paused?'Resume Sensable AI animation':'Pause Sensable AI animation');
  button.textContent=paused?'Resume animation ▷':'Pause animation Ⅱ';
 });
}
export const stages=[
 {label:'YOUR REQUEST',title:'Turn the request into a plan.',body:'Start with the request alone. Compare dinner options, walking time and a sensible arrival time.',input:'Meeting ends at the hotel at 8pm. Find somewhere for dinner.',output:'First plan: a nearby casual restaurant, a 20-minute walk from the hotel. Arrive at 8:20pm.',why:'Your AI reasons over the information available. It has not yet brought in personal preferences or checked last-order times.',next:'Add remembered context →'},
 {label:'YOUR PREFERENCES',title:'Make the plan fit this person.',body:'Use the same request, now with relevant saved context. The task stays the same; the options change.',input:'Saved context: vegetarian; prefers quiet places; starting from the hotel.',output:'Updated plan: a quiet vegetarian restaurant, 15 minutes from the hotel. Arrive at 8:15pm.',why:'Your saved preferences supply personal context. It does not establish whether the kitchen will still take an order.',next:'Add feedback →'},
 {label:'YOUR FEEDBACK',title:'Make the next plan better.',body:'Feedback from a previous dinner reveals a missing check: “The restaurant was open, but the kitchen had stopped taking orders.”',input:'A better approach: before recommending dinner, check arrival time against last-order time, as well as diet and atmosphere.',output:'Test the method on another evening: reject a venue if arrival is after its last order. Keep the vegetarian and quiet preferences.',why:'Your feedback suggests a reusable check. The next step lets you test its application; this scripted example does not establish a measured learning gain.',next:'Try the next evening →'},
 {label:'NEXT TIME',title:'Which plan passes all the checks?',body:'Another hotel meeting ends at 8pm. Both options are quiet and vegetarian. Walking times start at the hotel. Apply the new arrival-before-last-order check.',input:'Option A: 15-minute walk · arrive 8:15pm · last order 8:10pm. Option B: 20-minute walk · arrive 8:20pm · last order 9pm.',output:'Choose an option below to inspect the result.',why:'This is a different evening using sample venue data. Your preferences stay in context; the new approach adds a check that the first plan missed.',next:'Start again ↺'}
];
export const dinnerOptions=[
 {id:'A',walkMinutes:15,lastOrderMinutes:20*60+10,vegetarian:true,quiet:true},
 {id:'B',walkMinutes:20,lastOrderMinutes:21*60,vegetarian:true,quiet:true}
];
export function checkDinnerOption(option,departureMinutes=20*60){
 const arrivalMinutes=departureMinutes+option.walkMinutes;
 return {arrivalMinutes,marginMinutes:option.lastOrderMinutes-arrivalMinutes,passes:arrivalMinutes<=option.lastOrderMinutes&&option.vegetarian&&option.quiet};
}
export const layerNotes=[
 {title:'Reason with LLM',body:'Compare options. Turn a request into a plan.'},
 {title:'Remember with Memory',body:'Bring in preferences and relevant past context.'},
 {title:'Improve with Learn',body:'Learn / RSI proposes methods from feedback, checks them on new tasks, and reuses accepted methods within their scope. In preview.'}
];
export function createAutoRotation({onAdvance,delay=10000,schedule=setTimeout,cancel=clearTimeout}){
  let timer,stopped=false;
  const holds=new Set();
  function clear(){if(timer!==undefined){cancel(timer);timer=undefined;}}
  function queue(){clear();if(stopped||holds.size)return;timer=schedule(()=>{timer=undefined;onAdvance();queue();},delay);}
  queue();
  return {
    pause(reason){holds.add(reason);clear();},
    resume(reason){holds.delete(reason);queue();},
    stop(){stopped=true;clear();}
  };
}
function setupAutoRotation(element,onAdvance){
  const rotation=createAutoRotation({onAdvance});
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const syncReduced=()=>reduced.matches?rotation.pause('reduced'):rotation.resume('reduced');
  const syncVisibility=()=>document.hidden?rotation.pause('hidden'):rotation.resume('hidden');
  element.addEventListener('pointerenter',event=>{if(event.pointerType!=='touch')rotation.pause('hover');});
  element.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')rotation.resume('hover');});
  element.addEventListener('focusin',()=>rotation.pause('focus'));
  element.addEventListener('focusout',event=>{if(!element.contains(event.relatedTarget))rotation.resume('focus');});
  element.addEventListener('pointerdown',event=>{if(event.pointerType==='touch')rotation.stop();});
  element.addEventListener('click',()=>{if(matchMedia('(hover: none)').matches)rotation.stop();});
  reduced.addEventListener('change',syncReduced);document.addEventListener('visibilitychange',syncVisibility);
  syncReduced();syncVisibility();return rotation;
}
if(typeof document!=='undefined'){
  setupSensableLoop();
  setupCodeBlocks();
  const diagram=document.querySelector('#brain-diagram');
  const motion=document.querySelector('#motion-toggle');
  const panel=document.querySelector('#demo-panel');
  const layers=[...document.querySelectorAll('[data-layer]')];
  let selectedLayer=0;
  function selectLayer(index){
    selectedLayer=index;
    layers.forEach(layer=>{const selected=Number(layer.dataset.layer)===index;layer.setAttribute('aria-pressed',String(selected));layer.tabIndex=selected?0:-1;});
    const detail=document.querySelector('#layer-detail');
    if(detail){
      const y=125+index*100;detail.style.top=`${(y-18)/480*100}%`;
      document.querySelector('#layer-leader').setAttribute('d',`M500 ${y}h45`);
      document.querySelector('#layer-anchor').setAttribute('cy',String(y));
      document.querySelector('#layer-end').setAttribute('cy',String(y));
      const note=layerNotes[index];detail.innerHTML=`<strong>${note.title}</strong><p>${note.body}</p><a class="text-link" href="#how-it-works">Try the example ↓</a>`;}
  }
  const diagramRotation=diagram?setupAutoRotation(diagram,()=>selectLayer((selectedLayer+1)%layerNotes.length)):null;
  if(diagram)selectLayer(0);
  if(motion){
    const reduced=matchMedia('(prefers-reduced-motion: reduce)');
    function syncMotion(){
      motion.disabled=reduced.matches;
      if(diagram.classList.contains('motion-paused'))diagramRotation.pause('manual');else diagramRotation.resume('manual');
      motion.textContent=reduced.matches?'Reduced motion':diagram.classList.contains('motion-paused')?'Resume motion ▷':'Pause motion Ⅱ';
      motion.setAttribute('aria-pressed',String(reduced.matches||diagram.classList.contains('motion-paused')));
      motion.setAttribute('aria-label',reduced.matches?'Diagram motion reduced by system preference':diagram.classList.contains('motion-paused')?'Resume diagram animation':'Pause diagram animation');
    }
    motion.addEventListener('click',()=>{diagram.classList.toggle('motion-paused');syncMotion();});
    reduced.addEventListener('change',syncMotion);syncMotion();
  }
  let renderDemo;
  if(panel){
    let active=0;
    const tabs=[...document.querySelectorAll('[data-step]')];
    tabs.forEach((tab,index)=>{tab.id=`dinner-tab-${index}`;tab.setAttribute('aria-controls','demo-panel');});
    function render(step){
      active=step;const s=stages[step];
      panel.setAttribute('aria-labelledby',`dinner-tab-${step}`);
      panel.innerHTML=`<div class="small-label">${s.label}</div><h3>${s.title}</h3><p>${s.body}</p><div class="case-item"><span>INPUT</span><p>${s.input}</p></div><div class="case-item"><span>RESULT</span><p>${s.output}</p></div><details class="case-why"><summary>What changed?</summary><p>${s.why}</p></details>${step===3?'<div class="dinner-options"><button class="btn secondary" data-option="A">Choose A</button><button class="btn secondary" data-option="B">Choose B</button></div><div id="choice-result" aria-live="polite"></div>':''}${step<3?`<div class="demo-controls"><button class="text-link" id="demo-next">${s.next}</button></div>`:''}`;
      tabs.forEach(btn=>{const selected=Number(btn.dataset.step)===active;btn.setAttribute('aria-selected',String(selected));btn.tabIndex=selected?0:-1;});
      panel.querySelector('#demo-next')?.addEventListener('click',()=>render((active+1)%stages.length));
      panel.querySelectorAll('[data-option]').forEach(button=>button.addEventListener('click',()=>{
        const option=dinnerOptions.find(item=>item.id===button.dataset.option);
        const {passes:pass}=checkDinnerOption(option);
        panel.querySelector('#choice-result').innerHTML=pass?'<p class="choice-pass"><strong>B passes.</strong> Arrive at 8:20pm, 40 minutes before the 9pm last order. Vegetarian and quiet preferences still match. The method caught the timing problem before making a recommendation.</p>':'<p><strong>A fails the timing check.</strong> Arrive at 8:15pm, five minutes after last order. Being open and matching preferences is not enough. Try B.</p>';
      }));
    }
    tabs.forEach(btn=>btn.addEventListener('click',()=>render(Number(btn.dataset.step))));
    keyboardTabs(tabs);
    setupAutoRotation(document.querySelector('#how-it-works'),()=>render((active+1)%stages.length));
    document.querySelector('#reset-demo').addEventListener('click',()=>render(0));renderDemo=render;render(0);
  }
  function chooseLayer(index){selectLayer(index);renderDemo?.(index);}
  layers.forEach(layer=>{
    layer.addEventListener('pointerenter',event=>{if(event.pointerType!=='touch')selectLayer(Number(layer.dataset.layer));});
    layer.addEventListener('focus',()=>selectLayer(Number(layer.dataset.layer)));
    layer.addEventListener('click',()=>chooseLayer(Number(layer.dataset.layer)));
    layer.addEventListener('keydown',event=>{
      const index=Number(layer.dataset.layer);
      if(event.key==='Enter'||event.key===' '){event.preventDefault();chooseLayer(index);}
      if(['ArrowDown','ArrowUp','ArrowRight','ArrowLeft'].includes(event.key)){
        event.preventDefault();const next=(index+(['ArrowDown','ArrowRight'].includes(event.key)?1:2))%3;
        layers.find(item=>Number(item.dataset.layer)===next).focus();chooseLayer(next);
      }
    });
  });

}
