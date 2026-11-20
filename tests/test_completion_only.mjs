import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const root=process.argv[2];
const source=await readFile(resolve(root,'api-client.js'),'utf8');
const {createApiClient}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const client=createApiClient({hostname:'localhost',fetchImpl:async()=>new Response(null,{status:204})});
assert.equal(await client.request('/api/jobs/any'),null,'204 is an empty wait response, not JSON');
const script=await readFile(resolve(root,'previs-console.js'),'utf8');
const follow=script.slice(script.indexOf('  async function followPrevis('),script.indexOf('  async function showPrevis('));
const calls=[], rendered=[], debug=[];
const result={state:'completed',events:[{sequence:2,type:'complete',value:{message:'Done',media:[]}}],ai_usage:[]};
await new Function('request','renderOutput','recordDebug','log','message',`
  let sessionId=null,lastPrevisEvent=-1; const $=()=>({});
  let cleared=false; const clearActivePrevis=()=>{cleared=true;};
  ${follow}
  return followPrevis({jobId:'id',sessionId:'session'});
`)(async url=>{calls.push(url);return calls.length===1?null:result;},value=>rendered.push(value),(...args)=>debug.push(args),()=>{},()=>{});
assert.equal(calls.length,2);
assert.ok(calls.every(url=>url.includes('completeOnly=true')));
assert.equal(rendered.length,1);
assert.equal(debug.length,1);
assert.equal(debug[0][1].state,'completed');
console.log('PASS 204 handling, completion-only waiting and terminal response display');
