import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const root=process.argv[2] || new URL('../ai_video/video_higgsfield_jutsu/public/',import.meta.url);
const consoleSource=await readFile(new URL('previs-console.js',root),'utf8');
const wan=await readFile(new URL('wan-upload.js',root),'utf8');
const html=await readFile(new URL('wan-upload.html',root),'utf8');
for(const source of [consoleSource,wan,html]) assert.doesNotMatch(source,/보관기간(?:\(5일\)| 3일)|생성 완료 후 5일/);
for(const source of [consoleSource,wan]) {
 const match=source.match(/card\.expiry\.textContent = ([^;]+);/);
 assert.ok(match);
 const render=new Function('job',`return ${match[1]};`);
 assert.match(render({expiresAt:null}),/영구 보관/);
 assert.doesNotMatch(render({expiresAt:null}),/1970|Invalid Date/);
 assert.match(render({expiresAt:1900000000}),/기한/);
}
const sourceInfo=wan.match(/\$\('source-info'\)\.textContent = ([^;]+);/);
const renderSource=new Function('source','meta','formatNumber',`return ${sourceInfo[1]};`);
assert.match(renderSource({name:'source',expiresAt:null},{duration:3,width:20,height:10,fps:24},String),/영구 보관/);
console.log('PASS permanent and finite expiry labels');
