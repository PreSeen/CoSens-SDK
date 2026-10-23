export const homeJourneys={
 avatar:[
  {by:'Maya · Your team',title:'Can we confirm the pilot date?',body:'A familiar question. The answer depends on the decisions and work behind it.',tags:['Project Atlas','Pilot launch'],state:'Your request'},
  {by:'CoSens · Relevant experience',title:'The target is October 26.',body:'Security approval and the final test are still pending. The date remains provisional.',tags:['Pilot plan','Security review','Latest test update'],state:'Sources brought into view'},
  {by:'CoSens · Client update draft',title:'Here’s an update your team can review.',body:'We’re targeting October 26, subject to security approval and final testing. We’ll confirm when both checks are complete.',tags:['Source-aware draft','No message sent'],state:'Ready for your review'}
 ],
 connect:[
  {by:'01 / Your AI',title:'Keep the AI you already use.',body:'Choose ChatGPT or Claude through MCP, or install the plugin in Codex or Claude Code. Use the same CoSens account.',tags:['Your AI','CoSens account'],state:'Connect'},
  {by:'02 / One source',title:'Save a decision you’ll need again.',body:'Upload a project note through a supported client or the Memory Console. Wait until it is ready to recall.',tags:['pilot-plan.md','Source and permissions'],state:'Bring one source'},
  {by:'03 / A new conversation',title:'What still needs approval?',body:'Your saved pilot note says security approval is still pending. Open the source and check the answer.',tags:['Relevant context','Original source'],state:'Recall and verify'}
 ]
};
export function renderHomeJourney(frame){return `<div class="journey-message"><span class="small-label">${frame.by}</span><h3>${frame.title}</h3><p>${frame.body}</p></div><div class="journey-source-row">${frame.tags.map(t=>`<span>${t}</span>`).join('')}</div><div class="journey-state"><span class="journey-dot"></span>${frame.state}</div>`.replaceAll('CoSens','<span class="brand-word"><span class="brand-co">Co</span><span class="brand-sens">Sens</span></span>');}
if(typeof document!=='undefined')for(const root of document.querySelectorAll('[data-home-journey]')){
 const frames=homeJourneys[root.dataset.homeJourney];const output=root.querySelector('[data-journey-output]');
 const buttons=[...root.querySelectorAll('[data-journey-step]')];
 const select=i=>{output.innerHTML=renderHomeJourney(frames[i]);buttons.forEach((b,n)=>b.setAttribute('aria-pressed',String(n===i)));};
 buttons.forEach((b,i)=>b.addEventListener('click',()=>select(i)));select(0);
}
