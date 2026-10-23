import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,readFileSync,writeFileSync,copyFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,delimiter} from 'node:path';
import {spawnSync} from 'node:child_process';

function exercise(failStep,dirty=false){
  const temporary=mkdtempSync(join(tmpdir(),'cosens-deploy-test-'));
  const root=join(temporary,'site'),bin=join(temporary,'bin'),events=join(temporary,'events');
  mkdirSync(join(root,'scripts'),{recursive:true});mkdirSync(bin);
  copyFileSync(new URL('../scripts/deploy.sh',import.meta.url),join(root,'scripts/deploy.sh'));
  writeFileSync(join(root,'README.md'),'Deployment fixture\n');
  const git=(...args)=>{
    const result=spawnSync('git',args,{cwd:root,encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
  };
  git('init','-b','main');git('add','.');
  git('-c','user.name=Deploy Test','-c','user.email=deploy-test@example.invalid','commit','-m','fixture');
  const stub=`#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path');
const args=process.argv.slice(2);
const step=path.basename(process.argv[1])==='npm'?(args[0]==='run'?args[1]:args[0]):args[0];
fs.appendFileSync(process.env.EVENT_LOG,step+'\\n');
process.exit(process.env.FAIL_STEP===step?7:0);
`;
  for(const name of ['npm','wrangler'])writeFileSync(join(bin,name),stub,{mode:0o755});
  if(dirty)writeFileSync(join(root,'README.md'),'Uncommitted change\n');
  const result=spawnSync('bash',['scripts/deploy.sh'],{cwd:root,encoding:'utf8',env:{...process.env,PATH:bin+delimiter+process.env.PATH,EVENT_LOG:events,FAIL_STEP:failStep??''}});
  const recorded=spawnSync('test',['-f',events]).status===0?readFileSync(events,'utf8').trim().split('\n'):[];
  rmSync(temporary,{recursive:true,force:true});
  return {...result,recorded};
}

test('one-command deployment verifies production only after a successful publish',()=>{
  const result=exercise();
  assert.equal(result.status,0,result.stderr);
  assert.deepEqual(result.recorded,['build','test','whoami','pages','verify:live']);
});
test('failed tests cannot publish production',()=>{
  const result=exercise('test');
  assert.equal(result.status,7);
  assert.deepEqual(result.recorded,['build','test']);
});
test('failed authentication cannot publish production',()=>{
  const result=exercise('whoami');
  assert.equal(result.status,7);
  assert.deepEqual(result.recorded,['build','test','whoami']);
});
test('uncommitted changes stop before build or external actions',()=>{
  const result=exercise(undefined,true);
  assert.equal(result.status,1);
  assert.deepEqual(result.recorded,[]);
});
