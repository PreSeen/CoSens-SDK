import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createAvatarPlayback, avatarSteps} from '../dist/avatar/avatar.mjs';

const read=path=>readFile(new URL('../dist/avatar/'+path,import.meta.url),'utf8');
function clock(){
 const tasks=new Map(); let id=0;
 return {tasks,schedule(fn,delay){const key=++id;tasks.set(key,{fn,delay});return key;},cancel(key){tasks.delete(key);},tick(){const [key,task]=tasks.entries().next().value;tasks.delete(key);task.fn();}};
}

test('preview progresses through the 22 second story and loops',()=>{
 const timer=clock(),seen=[];
 const player=createAvatarPlayback({onChange:state=>seen.push(state),schedule:timer.schedule.bind(timer),cancel:timer.cancel.bind(timer)});
 assert.equal(avatarSteps.reduce((sum,step)=>sum+step.duration,0),22000);
 assert.equal(seen.at(-1).index,0);
 for(let index=1;index<avatarSteps.length;index++){assert.equal([...timer.tasks.values()][0].delay,avatarSteps[index-1].duration);timer.tick();assert.equal(seen.at(-1).index,index);}
 timer.tick();assert.equal(seen.at(-1).index,0);
 player.destroy();assert.equal(timer.tasks.size,0);
});

test('manual, hover, visibility, and reduced motion holds are independent',()=>{
 const timer=clock(),player=createAvatarPlayback({onChange:()=>{},schedule:timer.schedule.bind(timer),cancel:timer.cancel.bind(timer)});
 player.pause('manual');player.pause('hover');player.resume('hover');assert.equal(timer.tasks.size,0);
 player.resume('manual');assert.equal(timer.tasks.size,1);
 player.pause('hidden');assert.equal(timer.tasks.size,0);
 player.resume('hidden');assert.equal(timer.tasks.size,1);
 player.pause('reduced');assert.equal(timer.tasks.size,0);
 player.select(4);assert.equal(player.state.index,4);assert.equal(timer.tasks.size,0);
 player.resume('reduced');assert.equal(timer.tasks.size,1);
 player.destroy();
});

test('page carries the three stories, preview boundary, and working destinations',async()=>{
 const html=await read('index.html');
 for(const copy of ['Your experience.','Put to work.','Your team needs an answer.','Keep the next step from getting lost.','Make your judgment part of the next task.','Learn preview','Product preview','Ready for your review'])assert.ok(html.includes(copy),copy);
 assert.match(html,/href="\/request-access\/\?interest=avatar"/);
 assert.match(html,/href="\/#connect"/);
 assert.match(html,/href="\/build\/"/);
 assert.match(html,/data-avatar-stage/);
 assert.match(html,/data-avatar-step="5"/);
 assert.match(html,/aria-label="Avatar preview controls"/);
 assert.ok(!html.includes('Confirm available controls for each workflow'));
 const css=await read('avatar.css');assert.match(css,/overflow-x:auto/);assert.match(css,/prefers-reduced-motion/);
});

test('manual pause remains set when controls already hold playback for hover and focus',()=>{
 const timer=clock(),player=createAvatarPlayback({onChange:()=>{},schedule:timer.schedule.bind(timer),cancel:timer.cancel.bind(timer)});
 player.pause('hover');player.pause('focus');
 player.toggleManual();
 assert.equal(player.state.manualPaused,true);
 player.resume('hover');player.resume('focus');
 assert.equal(timer.tasks.size,0);
 player.toggleManual();assert.equal(timer.tasks.size,1);
 player.destroy();
});
