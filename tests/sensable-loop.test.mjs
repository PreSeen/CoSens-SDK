import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as app from '../dist/app.mjs';

test('the concept loop can be paused and resumed without changing the story',()=>{
 const button=new EventTarget();button.attrs={};button.setAttribute=(key,value)=>button.attrs[key]=value;
 const figure={dataset:{},querySelector:()=>button};
 assert.equal(typeof app.setupSensableLoop,'function');
 app.setupSensableLoop({querySelector:()=>figure});
 button.dispatchEvent(new Event('click'));
 assert.equal(figure.dataset.paused,'true');
 assert.equal(button.attrs['aria-pressed'],'true');
 assert.equal(button.attrs['aria-label'],'Resume Sensable AI animation');
 button.dispatchEvent(new Event('click'));
 assert.equal(figure.dataset.paused,'false');
 assert.equal(button.attrs['aria-pressed'],'false');
 assert.equal(button.attrs['aria-label'],'Pause Sensable AI animation');
});
