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

const source=await readFile(process.argv[2] || new URL('../ai_video/video_higgsfield_jutsu/public/usage-history.js',import.meta.url),'utf8');
const {initUsageHistory}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const calls=[];
const request=async(url,options)=>{
  calls.push([url,options]);
  return {items:[{jobId:'visible',createdAt:0,state:'completed',previs:[],videos:[{id:'video',state:'completed',videoUrl:'/file.mp4',expiresAt:null}]}],nextOffset:20};
};
const activate=initUsageHistory({request,apiUrl:url=>url});
activate(); await new Promise(setImmediate);
assert.equal(toolbar.children.some(node=>node.textContent==='기존 자료 영구 보관'),false);
assert.equal(source.includes('/api/usage-history/retention/permanent'),false);
assert.match(ids['usage-history-records'].querySelectorAll('small').map(node=>node.textContent).join(' '),/영구 보관/);
ids['usage-history-refresh'].events.click(); await new Promise(setImmediate);
assert.equal(calls.length,2);
assert.ok(calls.every(call=>!call[1]?.method || call[1].method==='GET'));
assert.match(ids['usage-history-status'].textContent,/1개 기록/);
console.log('PASS conversion button removed, permanent labels and history refresh preserved');
