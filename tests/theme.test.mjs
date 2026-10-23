import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolveTheme,localHour} from '../dist/theme.mjs';
test('local day boundaries',()=>{for(const [h,t] of [[0,'dark'],[6,'dark'],[7,'light'],[18,'light'],[19,'dark'],[23,'dark']]) assert.equal(resolveTheme('auto',h),t);});
test('explicit choices take priority',()=>{assert.equal(resolveTheme('dark',12),'dark');assert.equal(resolveTheme('light',23),'light');});
test('the same moment adapts to different timezones',()=>{const date=new Date('2026-10-17T15:00:00Z');assert.equal(localHour(date,'America/Los_Angeles'),8);assert.equal(localHour(date,'Asia/Tokyo'),0);assert.equal(resolveTheme('auto',localHour(date,'America/Los_Angeles')),'light');assert.equal(resolveTheme('auto',localHour(date,'Asia/Tokyo')),'dark');});
test('DST transition preserves local boundaries',()=>{assert.equal(localHour(new Date('2026-11-01T15:00:00Z'),'America/Los_Angeles'),7);assert.equal(localHour(new Date('2026-03-08T14:00:00Z'),'America/Los_Angeles'),7);});
