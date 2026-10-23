// Storyboard and playback script. No model calls or customer data.
export const frames=[
 {title:'You ask.',caption:'Start with the question you actually need help with.',duration:4500,active:['you','agent'],flows:{conversation:{label:'Prompt',direction:'forward'}},question:['Can we launch the pilot','on Friday?'],answer:['Let’s work out the next step.'],memory:'Your project decisions and constraints.',method:['A better approach, for next time.']},
 {title:'Your experience comes into context.',caption:'Your agent brings back the background that matters to this question.',duration:5500,active:['world','memory','agent'],flows:{import:{label:'Documents',direction:'forward'},recall:{label:'Relevant context',direction:'forward'}},question:['Can we launch the pilot','on Friday?'],answer:['The pilot should stay small.','Onboarding should stay simple.','Offline export is still unresolved.'],memory:'Offline export must be confirmed first.',method:['A better approach, for next time.']},
 {title:'The answer fits your situation.',caption:'Relevant experience changes the next step—not just the wording.',duration:5000,active:['agent','you'],flows:{conversation:{label:'Reply',direction:'reverse'}},question:['Can we launch the pilot','on Friday?'],answer:['Confirm offline export first.','Keep the pilot small and onboarding simple.','Then we can set a launch date.'],memory:'Pilot plan · Launch review',method:['A better approach, for next time.']},
 {title:'You shape how it helps.',caption:'Your correction becomes feedback tied to the task and its result.',duration:5000,active:['you','agent'],flows:{conversation:{label:'Feedback',direction:'forward'}},question:['Check open dependencies','before setting a date.'],answer:['Understood. Check what is unresolved','before proposing a date.'],memory:'Pilot plan · Launch review',method:['A better approach, for next time.']},
 {title:'Experience becomes a better method.',caption:'Develop a candidate approach, validate it, then reuse it where it applies.',duration:6000,active:['agent','memory','learn'],flows:{learn:{label:'Task + feedback',direction:'forward'},experience:{label:'Past experience',direction:'forward'}},question:['Check open dependencies','before setting a date.'],answer:['A lesson to carry forward:','check dependencies before setting a date.'],memory:'Pilot plan · Launch review',method:['Check open dependencies','before setting a date.'],validation:true},
 {title:'A new task. Experience carried forward.',caption:'A method can transfer to a new task. The old project’s facts do not.',duration:5000,active:['you','agent','memory','learn'],flows:{conversation:{label:'Prompt',direction:'forward'},learn:{label:'Validated method',direction:'reverse'},experience:{label:'Validated lesson',direction:'reverse'}},question:['Can we roll out the','dashboard on Monday?'],answer:['Apply the dependency check','to this new task.'],memory:'New task → use its own project context.',method:['Check open dependencies','before setting a date.'],validation:true},
 {title:'A better next step.',caption:'Your agent uses what it learned to approach the next problem more carefully.',duration:6500,active:['agent','you','learn'],flows:{learn:{label:'Validated method',direction:'reverse'},conversation:{label:'Reply',direction:'reverse'}},question:['Can we roll out the','dashboard on Monday?'],answer:['What still needs approval or testing?','Let’s check that before choosing a launch date.'],memory:'New task → use its own project context.',method:['Check open dependencies','before setting a date.'],validation:true}
];

export function advance(index){return (index+1)%frames.length;}

if(typeof document!=='undefined'&&document.querySelector('#sdk-workflow')){
 const root=document.querySelector('#sdk-workflow');
 const svg=root.querySelector('svg');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const play=root.querySelector('#play');
 const steps=[...root.querySelectorAll('[data-frame]')];
 const progress=root.querySelector('.sdk-scenes');
 let index=0,playing=!reduced.matches,inView=false,hovered=false,timer;
 const writeLines=(id,lines)=>{
  const text=root.querySelector('#'+id);
  text.replaceChildren(...lines.map((line,i)=>{const span=document.createElementNS('http://www.w3.org/2000/svg','tspan');span.setAttribute('x',text.getAttribute('x'));span.setAttribute('dy',i?'25':'0');span.textContent=line;return span;}));
 };
 function schedule(){clearTimeout(timer);const running=playing&&inView&&!document.hidden&&!hovered;svg.classList.toggle('paused',!running);if(running)timer=setTimeout(()=>{index=advance(index);render();},frames[index].duration);}
 function render(){
  const f=frames[index];
  root.querySelector('#story-title').textContent=f.title;
  root.querySelector('#story-caption').textContent=f.caption;
  root.querySelector('#position').textContent=`${String(index+1).padStart(2,'0')} / 07`;
  writeLines('question',f.question);writeLines('answer',f.answer);
  root.querySelector('#memory-line').textContent=f.memory;
  writeLines('method-line',f.method);
  root.querySelector('#validation').classList.toggle('visible',Boolean(f.validation));
  root.querySelector('#you-kicker').textContent=index===3||index===4?'YOUR FEEDBACK':'YOUR QUESTION · YOUR GOAL';
  root.querySelector('#agent-kicker').textContent=index===1?'RELEVANT CONTEXT':index>=5?'NEXT TASK':'YOUR AGENT';
  root.querySelectorAll('[data-node]').forEach(node=>node.classList.toggle('active',f.active.includes(node.dataset.node)));
  root.querySelectorAll('[data-path]').forEach(path=>{
   const flow=f.flows[path.dataset.path];
   path.classList.toggle('active',Boolean(flow));
   path.classList.toggle('reverse',flow?.direction==='reverse');
   path.setAttribute('marker-start',flow?.direction==='reverse'?'url(#sdk-flow-arrow)':'none');
   path.setAttribute('marker-end',flow?.direction==='reverse'?'none':'url(#sdk-flow-arrow)');
  });
  root.querySelectorAll('[data-flow-label]').forEach(label=>{
   const flow=f.flows[label.dataset.flowLabel];
   label.textContent=flow?.label??'';
   label.classList.toggle('visible',Boolean(flow));
  });
  steps.forEach((step,i)=>{step.setAttribute('aria-current',i===index?'step':'false');step.classList.toggle('done',i<index);});
  play.setAttribute('aria-label',playing?'Pause SDK animation':'Play SDK animation');
  play.textContent=playing?'Pause Ⅱ':'Play ▷';
  play.setAttribute('aria-pressed',String(playing));
  schedule();
 }
 steps.forEach(step=>step.addEventListener('click',()=>{index=Number(step.dataset.frame);render();}));
 play.addEventListener('click',()=>{playing=!playing;render();});
 root.querySelector('#restart').addEventListener('click',()=>{index=0;playing=!reduced.matches;render();});
 root.querySelector('#next').addEventListener('click',()=>{index=advance(index);render();});
 progress.addEventListener('pointerenter',event=>{if(event.pointerType==='touch')return;hovered=true;schedule();});
 progress.addEventListener('pointerleave',event=>{if(event.pointerType==='touch')return;hovered=false;schedule();});
 reduced.addEventListener('change',()=>{if(reduced.matches){playing=false;render();}});
 document.addEventListener('visibilitychange',schedule);
 const visibility=new IntersectionObserver(entries=>{inView=entries[0].isIntersecting;schedule();},{threshold:0.05});
 visibility.observe(root);
 render();
}
