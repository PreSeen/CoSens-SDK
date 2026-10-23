import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createStoryPlayback,storyFrames} from '../dist/connect-story.mjs';
function fixture(){let job;const seen=[];const clock={schedule:fn=>(job=fn,1),cancel:()=>{job=undefined;}};const player=createStoryPlayback({onChange:state=>seen.push(state),...clock});return {player,seen,tick(){const fn=job;job=undefined;fn?.();}};}
test('story carries a method into a different question without inventing its project facts',()=>{
 assert.equal(storyFrames.length,8);
 assert.ok(storyFrames[1].memory.includes('security review'));
 assert.ok(storyFrames[4].learn.includes('candidate'));
 assert.ok(storyFrames[4].learn.includes('check'));
 assert.notEqual(storyFrames[0].user,storyFrames[5].user);
 assert.ok(storyFrames[7].answer.includes('approval or testing'));
 assert.equal(storyFrames[7].answer.includes('October 26'),false);
});
test('conversation loops and seeking does not release independent pause reasons',()=>{
 const f=fixture();for(let n=0;n<8;n++)f.tick();assert.equal(f.player.state().index,0);
 f.player.hold('manual',true);f.player.hold('hidden',true);f.player.hold('hidden',false);f.tick();assert.equal(f.player.state().index,0);
 f.player.select(5);assert.equal(f.player.state().index,5);f.tick();assert.equal(f.player.state().index,5);
 f.player.hold('manual',false);f.tick();assert.equal(f.player.state().index,6);
});
test('reduced motion and focus allow manual exploration without automatically resuming',()=>{
 const f=fixture();f.player.hold('reduced',true);f.player.hold('focus',true);f.player.select(7);f.tick();assert.equal(f.player.state().index,7);
 f.player.hold('focus',false);f.tick();assert.equal(f.player.state().index,7);
 f.player.hold('reduced',false);f.tick();assert.equal(f.player.state().index,0);
});
test('Connect leads with the three layers and work examples, with a visible conversation and collapsed dinner example',async()=>{
 const html=await readFile(new URL('../dist/index.html',import.meta.url),'utf8');
 assert.ok(html.includes('AI that builds on your experience.'));
 assert.ok(html.indexOf('id="brain-diagram"')<html.indexOf('id="experience"'));
 assert.ok(html.includes('<h3 class="example-heading">Watch an example learning loop</h3>'));
 assert.ok(html.indexOf('id="experience"')<html.indexOf('id="choose-ai"'));
 assert.ok(html.includes('id="choose-ai"'));
 assert.ok(html.includes('<details class="optional-example" id="how-it-works"><summary>Another example: planning dinner</summary>'));
 assert.ok(html.includes('</details><div class="sensable-takeaway"><h3>That’s Sensable AI.</h3>'));
 assert.equal(html.includes('<details open class="optional-example"'),false);
 assert.equal(html.includes('class="principles"'),false);
 assert.equal(html.includes('overview-grid three-layers'),false);
 const module=await readFile(new URL('../dist/connect-story.mjs',import.meta.url));
 const digest=createHash('sha256').update(module).digest('hex').slice(0,12);
 assert.ok(html.includes(`/connect-story.mjs?v=${digest}`));
});
