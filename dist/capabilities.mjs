import {keyboardTabs} from './app.mjs?v=85a37c256a13';

// Scripted examples of context and scoped method learning, not live task results.
export const capabilities=[
 {id:'research',label:'Research',title:'Reach a decision, not just a summary.',need:'Start from what you already know, then investigate what could change the decision.',prompt:'What changed since our last competitor review?',memory:'Past findings, accepted sources, decision criteria and the questions still open.',feedback:'Check publication dates and trace claims to their original sources.',method:'Check evidence freshness and provenance before comparing new findings with earlier conclusions.',check:'Try a new topic and an older source: current claims need fresh evidence; historical claims keep their original period.',nextTask:'Review a new supplier for a different project.',nextAnswer:'I’ll check current primary sources, flag older claims and compare findings against this project’s criteria.',scope:'Freshness checks apply to current claims. Keep historical evidence when the question is about the past.'},
 {id:'writing',label:'Writing',title:'Make the message work for its audience.',need:'Use your positioning and examples, then improve the argument from the edits you make.',prompt:'Turn this product update into a launch post.',memory:'Your audience, positioning, accepted examples, brand standards and previous edits.',feedback:'Start with the user’s problem. Support the benefit before explaining the technology.',method:'Build the argument from the audience’s problem, choose supporting evidence, then explain the product’s role.',check:'Try a different launch brief and an internal status note. Apply the external narrative only where it fits.',nextTask:'Write the announcement for a different product.',nextAnswer:'I’ll identify the audience’s problem, select evidence for this product’s benefit and build the story around it.',scope:'Use this approach for external launch writing. Internal progress notes can start with status; do not copy the old product’s claims.'},
 {id:'analysis',label:'Analysis',title:'Explain the change before recommending action.',need:'Bring business definitions into the analysis and refine the checks behind each conclusion.',prompt:'Why did conversion change this month?',memory:'Metric definitions, baseline periods, customer segments and past business decisions.',feedback:'Check whether the customer mix changed before interpreting the overall rate.',method:'Check denominators, missing data and segment composition before attributing a change to performance.',check:'Try a new period with a changed segment mix, plus a stable-mix case. Distinguish composition effects from within-segment changes.',nextTask:'Analyze the next campaign’s conversion results.',nextAnswer:'I’ll compare each segment and the customer mix before attributing the change to the campaign.',scope:'Apply composition checks to aggregated rates. Do not assume the same explanation fits counts, every metric or every dataset.'},
 {id:'clients',label:'Client work',title:'Know what to ask before the call.',need:'Bring the relationship into context and improve how you uncover what is still unresolved.',prompt:'Prepare me for tomorrow’s customer call.',memory:'Customer goals, previous conversations, confirmed commitments, objections and product boundaries.',feedback:'Separate what the customer requested from what we actually promised.',method:'Distinguish requests, confirmed commitments and open questions before preparing a brief or next step.',check:'Try another account brief. Reject commitments without supporting evidence and preserve questions that remain unanswered.',nextTask:'Draft a follow-up for a different customer.',nextAnswer:'I’ll separate their requests, our confirmed commitments and the questions we still need to answer.',scope:'Use each account’s own evidence. Never transfer another customer’s commitments or infer a promise from a request.'}
];

export function setupCapabilities(root){
 const tabs=[...root.querySelectorAll('[data-capability]')];
 const panel=root.querySelector('[data-capability-panel]');
 const fields=['title','need','prompt','memory','feedback','method','check','nextTask','nextAnswer','scope'];
 function select(index){
  const example=capabilities[index];
  root.dataset.capability=example.id;
  panel.setAttribute('aria-labelledby',`capability-tab-${example.id}`);
  for(const key of fields)root.querySelector(`[data-example="${key}"]`).textContent=example[key];
  tabs.forEach((tab,position)=>{const active=position===index;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});
 }
 tabs.forEach((tab,index)=>tab.addEventListener('click',()=>select(index)));
 keyboardTabs(tabs);select(0);
 return {select};
}
if(typeof document!=='undefined'){
 const root=document.querySelector('[data-capabilities]');
 if(root)setupCapabilities(root);
}

// Hash links to compact guides should reveal their contents, including on direct entry.
if(typeof window!=='undefined'){
 const reveal=()=>{const target=document.getElementById(decodeURIComponent(window.location.hash.slice(1)));if(!target)return;let element=target;while(element){if(element.tagName==='DETAILS')element.open=true;element=element.parentElement;}};
 window.addEventListener('hashchange',reveal);reveal();
}
