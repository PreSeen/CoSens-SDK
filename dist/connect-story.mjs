import {createAutoRotation,keyboardTabs} from './app.mjs?v=85a37c256a13';

// A scripted explanation, with no live model or memory requests.
export const storyFrames=[
 {title:'You ask.',flow:'prompt',label:'Your question',user:'Can we launch the pilot on October 26?',answer:'Let’s look at the project.',memory:'Your saved decisions and documents.',learn:'Feedback can shape a better approach.'},
 {title:'Memory brings the background.',flow:'recall',label:'Relevant context',user:'Can we launch the pilot on October 26?',answer:'Checking your saved launch note…',memory:'October 26 target. The security review still needs approval.',learn:'Feedback can shape a better approach.',source:true},
 {title:'The answer has context.',flow:'reply',label:'A grounded reply',user:'Can we launch the pilot on October 26?',answer:'The pilot targets October 26. Security review is still pending.',memory:'Pilot launch note · decision + open approval.',learn:'Feedback can shape a better approach.',source:true},
 {title:'You give feedback.',flow:'feedback',label:'Your feedback',user:'Check approvals and testing before recommending a date.',answer:'That check should shape the next recommendation.',memory:'The project facts stay in Memory.',learn:'A correction to learn from.'},
 {title:'Learn develops and checks a method.',flow:'learning',label:'Task + feedback',user:'Check approvals and testing before recommending a date.',answer:'I’ll check approvals and testing before proposing launch dates.',memory:'Keep the lesson separate from this project’s date.',learn:'Check the candidate: pending → hold; approved → check testing.'},
 {title:'Next time, a new question.',flow:'prompt',label:'A different task',user:'Can we roll out the dashboard on Monday?',answer:'A new project needs its own context.',memory:'Use this project’s facts, not the pilot’s date.',learn:'Launch-date check · preview.'},
 {title:'The approach carries forward.',flow:'reuse',label:'Checked approach · preview',user:'Can we roll out the dashboard on Monday?',answer:'I’ll check this project’s approvals and testing first.',memory:'Use this project’s facts, not the pilot’s date.',learn:'After the example checks, reuse within launch-date tasks.'},
 {title:'A better next step.',flow:'reply',label:'Experience in the answer',user:'Can we roll out the dashboard on Monday?',answer:'What still needs approval or testing? Let’s check before choosing a date.',memory:'New task. Relevant context. No copied old answer.',learn:'The lesson changes how your AI helps.'}
];

export function createStoryPlayback({onChange,schedule=setTimeout,cancel=clearTimeout}){
 let index=0;
 const holds=new Set();
 const state=()=>({index,paused:holds.size>0,reasons:[...holds]});
 const emit=()=>onChange(state());
 const rotation=createAutoRotation({delay:6000,schedule,cancel,onAdvance:()=>{index=(index+1)%storyFrames.length;emit();}});
 emit();
 return {
  state,
  hold(reason,enabled){if(enabled){holds.add(reason);rotation.pause(reason);}else{holds.delete(reason);rotation.resume(reason);}emit();},
  select(next){index=(next+storyFrames.length)%storyFrames.length;rotation.pause('seek');rotation.resume('seek');emit();}
 };
}

export function setupConversation(root){
 const steps=[...root.querySelectorAll('[data-story-step]')];
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const play=root.querySelector('[data-story-play]');
 const player=createStoryPlayback({onChange:state=>{
  const frame=storyFrames[state.index];
  root.dataset.flow=frame.flow;
  root.dataset.motion=state.paused?'paused':'running';
  for(const name of ['title','label','user','answer','memory','learn'])root.querySelector(`[data-story-${name}]`).textContent=frame[name];
  root.querySelector('[data-story-source]').hidden=!frame.source;
  root.querySelector('[data-story-count]').textContent=`${String(state.index+1).padStart(2,'0')} / 08`;
  steps.forEach((button,index)=>{button.setAttribute('aria-pressed',String(index===state.index));button.tabIndex=index===state.index?0:-1;});
  const manual=state.reasons.includes('manual');
  play.setAttribute('aria-pressed',String(manual));
  play.disabled=reduced.matches;
  play.textContent=reduced.matches?'Motion off':manual?'Resume':'Pause';
  play.setAttribute('aria-label',reduced.matches?'Automatic motion disabled by system preference':manual?'Resume conversation animation':'Pause conversation animation');
 }});
 const announce=()=>{root.querySelector('[data-story-status]').textContent=`Step ${player.state().index+1}: ${storyFrames[player.state().index].title}`;};
 steps.forEach((button,index)=>button.addEventListener('click',()=>{player.select(index);announce();}));
 keyboardTabs(steps);
 play.addEventListener('click',()=>player.hold('manual',!player.state().reasons.includes('manual')));
 root.querySelector('[data-story-next]').addEventListener('click',()=>{player.select(player.state().index+1);announce();});
 root.querySelector('[data-story-restart]').addEventListener('click',()=>{player.select(0);announce();});
 root.addEventListener('pointerenter',event=>{if(event.pointerType!=='touch')player.hold('hover',true);});
 root.addEventListener('pointerleave',event=>{if(event.pointerType!=='touch')player.hold('hover',false);});
 root.addEventListener('focusin',event=>{if(event.target.hasAttribute('data-story-step'))player.hold('focus',true);});
 root.addEventListener('focusout',event=>{if(!event.relatedTarget?.hasAttribute('data-story-step'))player.hold('focus',false);});
 const syncReduced=()=>player.hold('reduced',reduced.matches);
 const syncHidden=()=>player.hold('hidden',document.hidden);
 reduced.addEventListener('change',syncReduced);
 document.addEventListener('visibilitychange',syncHidden);
 syncReduced();syncHidden();player.hold('offscreen',true);
 const observer=new IntersectionObserver(entries=>{for(const entry of entries)player.hold('offscreen',!entry.isIntersecting);},{threshold:.15});
 observer.observe(root);
 return player;
}
if(typeof document!=='undefined'){
 const root=document.querySelector('#conversation-story');
 if(root)setupConversation(root);
}
