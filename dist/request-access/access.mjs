export const interests=['avatar','mcp','sdk'];
export function selectedInterest(search){const value=new URLSearchParams(search).get('interest');return interests.includes(value)?value:null;}
export function previewMessage(values){return values.length?'Preview ready. Your request has not been sent.':'Select at least one way you’d like to use CoSens.';}
if(typeof document!=='undefined'){
 const form=document.querySelector('#access-form');const selected=selectedInterest(location.search);
 if(selected)form.querySelector(`input[value="${selected}"]`).checked=true;
 form.addEventListener('submit',event=>{
  event.preventDefault();const values=[...form.querySelectorAll('input[name="interest"]:checked')].map(x=>x.value);
  const result=document.querySelector('#access-result');result.textContent=previewMessage(values);result.dataset.state=values.length?'preview':'error';result.focus();
 });
}
