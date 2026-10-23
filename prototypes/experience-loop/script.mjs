// Storyboard and playback script. No model calls or customer data.
export const frames=[
 {title:'You ask.',caption:'Start with the question you actually need help with.',duration:4500,active:['you','agent'],flows:{conversation:{label:'Prompt',direction:'forward'}},question:['Can we launch the pilot','on Friday?'],answer:['Let’s work out the next step.'],memory:'Your project decisions and constraints.',method:'A better approach, for next time.'},
 {title:'Your experience comes into context.',caption:'Your agent brings back the background that matters to this question.',duration:5500,active:['world','memory','agent'],flows:{import:{label:'Documents',direction:'forward'},recall:{label:'Relevant context',direction:'forward'}},question:['Can we launch the pilot','on Friday?'],answer:['The pilot should stay small.','Onboarding should stay simple.','Offline export is still unresolved.'],memory:'Offline export must be confirmed first.',method:'A better approach, for next time.'},
 {title:'The answer fits your situation.',caption:'Relevant experience changes the next step—not just the wording.',duration:5000,active:['agent','you'],flows:{conversation:{label:'Reply',direction:'reverse'}},question:['Can we launch the pilot','on Friday?'],answer:['Confirm offline export first.','Keep the pilot small and onboarding simple.','Then we can set a launch date.'],memory:'Pilot plan · Launch review',method:'A better approach, for next time.'},
 {title:'You shape how it helps.',caption:'Your correction becomes feedback tied to the task and its result.',duration:5000,active:['you','agent'],flows:{conversation:{label:'Feedback',direction:'forward'}},question:['Before suggesting dates,','check unresolved','dependencies.'],answer:['Understood. Check what is unresolved','before proposing a date.'],memory:'Pilot plan · Launch review',method:'A better approach, for next time.'},
 {title:'Experience becomes a better method.',caption:'Develop a candidate approach, validate it, then reuse it where it applies.',duration:6000,active:['agent','memory','learn'],flows:{learn:{label:'Task + feedback',direction:'forward'},experience:{label:'Past experience',direction:'forward'}},question:['Before suggesting dates,','check unresolved','dependencies.'],answer:['A lesson to carry forward:','check dependencies before dates.'],memory:'Pilot plan · Launch review',method:'Check open dependencies before dates.',validation:true},
 {title:'A new task. Experience carried forward.',caption:'A method can transfer to a new task. The old project’s facts do not.',duration:5000,active:['you','agent','memory','learn'],flows:{conversation:{label:'Prompt',direction:'forward'},learn:{label:'Validated method',direction:'reverse'},experience:{label:'Validated lesson',direction:'reverse'}},question:['Can we roll out the','dashboard on Monday?'],answer:['Apply the dependency check','to this new task.'],memory:'New task → use its own project context.',method:'Check open dependencies before dates.',validation:true},
 {title:'A better next step.',caption:'Your agent uses what it learned to approach the next problem more carefully.',duration:6500,active:['agent','you','learn'],flows:{learn:{label:'Validated method',direction:'reverse'},conversation:{label:'Reply',direction:'reverse'}},question:['Can we roll out the','dashboard on Monday?'],answer:['Let’s check unresolved dependencies first.','What is still waiting on approval or testing?','Then we can choose a date.'],memory:'New task → use its own project context.',method:'Check open dependencies before dates.',validation:true}
];

export function advance(index){return index<frames.length-1?index+1:null;}

if(typeof document!=='undefined'){
 const svg=document.querySelector('svg');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const play=document.querySelector('#play');
 const steps=[...document.querySelectorAll('[data-frame]')];
 let index=0,playing=!reduced.matches,timer;
 const writeLines=(id,lines)=>{
  const text=document.getElementById(id);
  text.replaceChildren(...lines.map((line,i)=>{const span=document.createElementNS('http://www.w3.org/2000/svg','tspan');span.setAttribute('x',text.getAttribute('x'));span.setAttribute('dy',i?'25':'0');span.textContent=line;return span;}));
 };
 function schedule(){clearTimeout(timer);if(playing&&!document.hidden)timer=setTimeout(()=>{const next=advance(index);if(next===null){playing=false;render();}else{index=next;render();}},frames[index].duration);}
 function render(){
  const f=frames[index];
  document.querySelector('#story-title').textContent=f.title;
  document.querySelector('#story-caption').textContent=f.caption;
  document.querySelector('#position').textContent=`${String(index+1).padStart(2,'0')} / 07`;
  writeLines('question',f.question);writeLines('answer',f.answer);
  document.querySelector('#memory-line').textContent=f.memory;
  document.querySelector('#method-line').textContent=f.method;
  document.querySelector('#validation').classList.toggle('visible',Boolean(f.validation));
  document.querySelector('#you-kicker').textContent=index===3||index===4?'YOUR FEEDBACK':'YOUR QUESTION · YOUR GOAL';
  document.querySelector('#agent-kicker').textContent=index===1?'RELEVANT CONTEXT':index>=5?'NEXT TASK':'YOUR AGENT';
  document.querySelectorAll('[data-node]').forEach(node=>node.classList.toggle('active',f.active.includes(node.dataset.node)));
  document.querySelectorAll('[data-path]').forEach(path=>{
   const flow=f.flows[path.dataset.path];
   path.classList.toggle('active',Boolean(flow));
   path.classList.toggle('reverse',flow?.direction==='reverse');
   path.setAttribute('marker-start',flow?.direction==='reverse'?'url(#arrow)':'none');
   path.setAttribute('marker-end',flow?.direction==='reverse'?'none':'url(#arrow)');
  });
  document.querySelectorAll('[data-flow-label]').forEach(label=>{
   const flow=f.flows[label.dataset.flowLabel];
   label.textContent=flow?.label??'';
   label.classList.toggle('visible',Boolean(flow));
  });
  steps.forEach((step,i)=>{step.setAttribute('aria-current',i===index?'step':'false');step.classList.toggle('done',i<index);});
  svg.classList.toggle('paused',!playing);
  play.textContent=playing?'Pause Ⅱ':index===frames.length-1?'Replay ↺':'Play ▷';
  play.setAttribute('aria-pressed',String(playing));
  schedule();
 }
 steps.forEach(step=>step.addEventListener('click',()=>{index=Number(step.dataset.frame);playing=false;render();}));
 play.addEventListener('click',()=>{if(!playing&&index===frames.length-1)index=0;playing=!playing;render();});
 document.querySelector('#restart').addEventListener('click',()=>{index=0;playing=!reduced.matches;render();});
 document.querySelector('#next').addEventListener('click',()=>{index=advance(index)??0;playing=false;render();});
 document.querySelector('#theme').addEventListener('click',()=>{document.documentElement.classList.toggle('light');});
 reduced.addEventListener('change',()=>{if(reduced.matches){playing=false;render();}});
 document.addEventListener('visibilitychange',schedule);
 render();
}
