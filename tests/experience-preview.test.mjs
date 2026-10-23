import {test} from 'node:test';
import assert from 'node:assert/strict';
import {frames,advance} from '../prototypes/experience-loop/script.mjs';
import {readFile} from 'node:fs/promises';

test('the experience story changes task facts while carrying the method into the next task',()=>{
 assert.equal(frames.length,7);
 assert.ok(frames[0].question.join(' ').includes('pilot'));
 assert.ok(frames[2].answer.join(' ').includes('Confirm offline export first'));
 assert.ok(frames[5].question.join(' ').includes('dashboard'));
 assert.equal(frames[6].answer.join(' ').includes('offline export'),false);
 assert.ok(frames[6].answer.join(' ').includes('unresolved dependencies'));
 const firstReuse=frames.findIndex(frame=>frame.flows.learn?.direction==='reverse');
 const validation=frames.findIndex(frame=>frame.validation);
 assert.ok(validation>=0&&validation<firstReuse);
});

test('experience, memory and learn use aligned single straight connections',async()=>{
 const html=await readFile(new URL('../prototypes/experience-loop/index.html',import.meta.url),'utf8');
 assert.ok(html.includes('id="sdk-label"'));
 assert.ok(html.includes('Sens</tspan> SDK</text>'));
 assert.equal(html.includes('Your second brain'),false);
 assert.ok(html.includes('BUILD / HOW IT WORKS'));
 assert.equal(html.includes('>SENSABLE AI<'),false);
 assert.ok(html.includes('class="node-title">Your experience</text>'));
 const rect=id=>{
  const attributes=html.match(new RegExp(`data-node="${id}"><rect([^>]+)`))[1];
  return Object.fromEntries([...attributes.matchAll(/\b(x|y|width|height)="([\d.]+)"/g)].map(([,key,value])=>[key,Number(value)]));
 };
 const world=rect('world'),memory=rect('memory'),learn=rect('learn'),agent=rect('agent'),you=rect('you');
 assert.equal(you.x,world.x);
 assert.equal(you.width,world.width);
 assert.equal(you.y,agent.y);
 assert.equal(you.height,agent.height);
 assert.equal(agent.x,memory.x);
 assert.equal(agent.x+agent.width,learn.x+learn.width);
 assert.equal(world.width,memory.width);
 assert.equal(memory.width,learn.width);
 assert.equal(world.height,memory.height);
 assert.equal(memory.height,learn.height);
 assert.equal(world.y,memory.y);
 assert.equal(memory.y,learn.y);
 assert.equal(memory.x-world.x-world.width,learn.x-memory.x-memory.width);
 assert.equal(world.y+world.height/2,memory.y+memory.height/2);
 for(const id of ['import','experience','learn']){
  const matches=[...html.matchAll(new RegExp(`data-path="${id}" d="([^"]+)"`,'g'))];
  assert.equal(matches.length,1);
  assert.match(matches[0][1],/^M[\d.]+ [\d.]+[HV][\d.]+$/);
 }
 assert.equal(html.includes('data-path="reuse"'),false);
 assert.ok(html.includes(`data-path="experience" d="M${memory.x+memory.width} ${memory.y+memory.height/2}H${learn.x}"`));
 assert.ok(html.includes(`data-path="learn" d="M${learn.x+learn.width/2} ${agent.y+agent.height}V${learn.y}"`));
 assert.deepEqual(frames[4].flows.learn,{label:'Task + feedback',direction:'forward'});
 assert.deepEqual(frames[5].flows.learn,{label:'Validated method',direction:'reverse'});
});

test('prompt, reply and feedback share one conversation edge with the correct direction',async()=>{
 assert.deepEqual(frames[0].flows.conversation,{label:'Prompt',direction:'forward'});
 assert.deepEqual(frames[2].flows.conversation,{label:'Reply',direction:'reverse'});
 assert.deepEqual(frames[3].flows.conversation,{label:'Feedback',direction:'forward'});
 const html=await readFile(new URL('../prototypes/experience-loop/index.html',import.meta.url),'utf8');
 assert.equal([...html.matchAll(/data-path="conversation"/g)].length,1);
 assert.equal(html.includes('data-node="feedback"'),false);
 assert.equal(html.includes('data-path="answer"'),false);
 assert.equal(html.includes('data-path="ask"'),false);
 assert.ok(html.indexOf('data-node="you"')<html.indexOf('data-node="world"'));
 const region=html.slice(html.indexOf('id="sensable-ai"'));
 for(const node of ['agent','memory','learn'])assert.ok(region.includes(`data-node="${node}"`));
 for(const frame of frames)for(const [path,flow]of Object.entries(frame.flows)){
  assert.ok(html.includes(`data-path="${path}"`));
  assert.ok(html.includes(`data-flow-label="${path}"`));
  assert.ok(flow.label);
 }
 assert.equal(frames[4].flows.experience.direction,'forward');
 assert.equal(frames[5].flows.experience.direction,'reverse');
});

test('playback finishes after the last scene instead of silently looping or skipping feedback',()=>{
 const seen=[];
 for(let index=0;index!==null;index=advance(index))seen.push(index);
 assert.deepEqual(seen,[0,1,2,3,4,5,6]);
 assert.ok(frames[3].flows.conversation.label==='Feedback');
 assert.equal(frames.reduce((total,frame)=>total+frame.duration,0),37500);
});
