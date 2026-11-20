import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
class Node {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.events={}; this.attributes={}; this.disabled=false; this.checked=false; this.textContent=''; this.classList={toggle(){}}; }
  append(...nodes) { for(const node of nodes) { node.parent=this; this.children.push(node); } }
  after(...nodes) { const index=this.parent.children.indexOf(this); this.parent.children.splice(index+1,0,...nodes); for(const node of nodes) node.parent=this.parent; }
  setAttribute(key,value) { this.attributes[key]=value; }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(event,handler) { this.events[event]=handler; }
  querySelectorAll(selector) { const all=this.children.flatMap(node=>[node,...node.querySelectorAll(selector)]); return all.filter(node=> selector==='input[data-history-job]' ? node.tag==='input' && node.dataset.historyJob : selector==='button, input' ? ['button','input'].includes(node.tag) : node.tag===selector); }
  closest(tag) { return this.tag===tag ? this : this.parent.closest(tag); }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this),1); }
  replaceChildren() { this.children=[]; }
  pause() {}
}
const ids=Object.fromEntries(['usage-history-panel','usage-history-records','usage-history-status','usage-history-refresh','usage-history-more','console-panel'].map(id=>[id,Object.assign(new Node(),{id})]));
const panel=ids['usage-history-panel'];
const toolbar=new Node(); panel.append(toolbar,ids['usage-history-records'],ids['usage-history-more']); toolbar.append(ids['usage-history-refresh'],ids['usage-history-status']);
const tab=new Node('button'); tab.setAttribute('aria-controls','usage-history-panel');
globalThis.document={getElementById:id=>ids[id],createElement:tag=>new Node(tag),createTextNode:text=>Object.assign(new Node('text'),{textContent:text}),querySelectorAll:selector=>selector==='[data-console-view]' ? [tab] : []};
let confirmed=true;
globalThis.window={confirm:()=>confirmed};
let fail=true;
const calls=[];
const items=['a','b','c'].map(jobId=>({jobId,createdAt:0,state:'completed',previs:[],videos:[{id:'failed-video',state:'failed'},{id:'running-video',state:'running'},{id:'success-video',state:'completed',videoUrl:'/success.mp4'}]}));
const request=async(url,options)=>{ calls.push([url,options]); if(options?.method==='DELETE') { if(url.endsWith('/b')&&fail) throw Error('temporary'); return {}; } return {items,nextOffset:20}; };
const source=await readFile(new URL('../ai_video/video_higgsfield_jutsu/public/usage-history.js',import.meta.url),'utf8');
const {initUsageHistory}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const activateHistory=initUsageHistory({request,apiUrl:url=>url});
activateHistory(); await new Promise(setImmediate);
const videoIds=ids['usage-history-records'].querySelectorAll('p').filter(node=>node.className==='usage-history-video-id').map(node=>node.textContent);
assert.deepEqual(videoIds,Array(3).fill('영상 Job success-video'));
const selectAll=toolbar.querySelectorAll('input')[0];
const bulk=toolbar.children.find(node=>node.tag==='button'&&node.textContent==='선택 삭제');
assert.equal(bulk.disabled,true);
selectAll.checked=true; selectAll.events.change();
assert.equal(bulk.textContent,'선택 삭제 (3)');
confirmed=false; await bulk.events.click(); assert.equal(calls.filter(call=>call[1]?.method==='DELETE').length,0);
confirmed=true; await bulk.events.click();
assert.equal(ids['usage-history-records'].children.length,1);
assert.equal(ids['usage-history-records'].querySelectorAll('input[data-history-job]')[0].dataset.historyJob,'b');
assert.equal(bulk.textContent,'선택 삭제 (1)');
assert.match(ids['usage-history-status'].textContent,/2개 기록 삭제 완료/);
assert.match(ids['usage-history-status'].textContent,/삭제하지 못한 1개/);
fail=false; await bulk.events.click();
assert.equal(ids['usage-history-records'].children.length,0);
assert.equal(bulk.disabled,true);
assert.equal(selectAll.disabled,true);
console.log('PASS bulk selection, cancel, partial failure, retry, empty state');

