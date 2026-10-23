export const avatarSteps = [
  {label:'The question', duration:3000},
  {label:'Relevant context', duration:3000},
  {label:'An answer to review', duration:4000},
  {label:'Your judgment', duration:3000},
  {label:'A checked method', duration:4000},
  {label:'The next task', duration:5000}
];

export function createAvatarPlayback({onChange, schedule=setTimeout, cancel=clearTimeout}) {
  let index=0, timer;
  const holds=new Set();
  let destroyed=false;
  const snapshot=()=>({index,playing:!holds.size,manualPaused:holds.has('manual')});
  const emit=()=>onChange(snapshot());
  function clear(){if(timer!==undefined){cancel(timer);timer=undefined;}}
  function queue(){
    clear();
    if(destroyed||holds.size)return;
    timer=schedule(()=>{timer=undefined;index=(index+1)%avatarSteps.length;emit();queue();},avatarSteps[index].duration);
  }
  function pause(reason){holds.add(reason);clear();emit();}
  function resume(reason){holds.delete(reason);queue();emit();}
  function select(next){index=((next%avatarSteps.length)+avatarSteps.length)%avatarSteps.length;emit();queue();}
  emit();queue();
  return {get state(){return snapshot();},pause,resume,select,toggleManual(){holds.has('manual')?resume('manual'):pause('manual');},reset(){select(0);},destroy(){destroyed=true;clear();}};
}

export function setupAvatarPreview(root=document) {
  const preview=root.querySelector('[data-avatar-preview]');
  if(!preview)return;
  const scenes=[...preview.querySelectorAll('[data-avatar-stage]')];
  const steps=[...preview.querySelectorAll('[data-avatar-step]')];
  const toggle=preview.querySelector('[data-avatar-toggle]');
  const reset=preview.querySelector('[data-avatar-reset]');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const observer=new IntersectionObserver(entries=>{
    entries[0].isIntersecting?player.resume('offscreen'):player.pause('offscreen');
  },{threshold:.08});
  const player=createAvatarPlayback({onChange({index,manualPaused}){
    preview.dataset.step=String(index);
    scenes.forEach((scene,sceneIndex)=>{scene.hidden=sceneIndex!==index;});
    steps.forEach((step,stepIndex)=>{
      step.setAttribute('aria-current',stepIndex===index?'step':'false');
      step.tabIndex=stepIndex===index?0:-1;
    });
    toggle.textContent=manualPaused?'Play preview ▷':'Pause preview Ⅱ';
    toggle.setAttribute('aria-label',manualPaused?'Play Avatar preview':'Pause Avatar preview');
    toggle.setAttribute('aria-pressed',String(manualPaused));
  }});
  const syncMotion=()=>reduced.matches?player.pause('reduced'):player.resume('reduced');
  const syncVisibility=()=>root.hidden?player.pause('hidden'):player.resume('hidden');
  const stopHover=event=>{if(event.pointerType!=='touch')player.pause('hover');};
  const startHover=event=>{if(event.pointerType!=='touch')player.resume('hover');};
  preview.querySelector('[data-avatar-controls]').addEventListener('pointerenter',stopHover);
  preview.querySelector('[data-avatar-controls]').addEventListener('pointerleave',startHover);
  preview.querySelector('[data-avatar-controls]').addEventListener('focusin',()=>player.pause('focus'));
  preview.querySelector('[data-avatar-controls]').addEventListener('focusout',event=>{
    if(!event.currentTarget.contains(event.relatedTarget))player.resume('focus');
  });
  toggle.addEventListener('click',()=>player.toggleManual());
  reset.addEventListener('click',()=>{player.reset();player.resume('manual');});
  steps.forEach((step,index)=>{
    step.addEventListener('click',()=>{player.pause('manual');player.select(index);});
    step.addEventListener('keydown',event=>{
      let next;
      if(event.key==='ArrowRight'||event.key==='ArrowDown')next=(index+1)%steps.length;
      if(event.key==='ArrowLeft'||event.key==='ArrowUp')next=(index+steps.length-1)%steps.length;
      if(event.key==='Home')next=0;
      if(event.key==='End')next=steps.length-1;
      if(next!==undefined){event.preventDefault();steps[next].focus();player.pause('manual');player.select(next);}
    });
  });
  root.addEventListener('visibilitychange',syncVisibility);
  reduced.addEventListener('change',syncMotion);
  observer.observe(preview);
  syncVisibility();syncMotion();
  return player;
}

if(typeof document!=='undefined')setupAvatarPreview();
