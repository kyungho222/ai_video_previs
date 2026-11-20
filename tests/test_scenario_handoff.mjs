import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const publicRoot=process.argv[2] || new URL('../ai_video/video_higgsfield_jutsu/public/',import.meta.url);
const read=name=>readFile(typeof publicRoot==='string' ? resolve(publicRoot,name) : new URL(name,publicRoot),'utf8');
class Field {
  constructor() { this.value=''; this.textContent=''; this.children=[]; this.events={}; }
  addEventListener(key,handler) { this.events[key]=handler; }
  setAttribute() {}
  setSelectionRange() {}
  focus() {}
  append(row) { row.parent=this; this.children.push(row); }
  replaceChildren() { this.children=[]; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this),1); }
}
function row() {
  const node=new Field();
  const parts=Object.fromEntries(['input','textarea','.sf-index','.sf-remove','[data-move="-1"]','[data-move="1"]'].map(key=>[key,new Field()]));
  node.querySelector=key=>parts[key];
  node.querySelectorAll=()=>[parts['[data-move="-1"]'],parts['[data-move="1"]']];
  return node;
}
const fields=Object.fromEntries(['fields','scenes','title','summary','model','draft','generate','reset','add','status','payload','response','scene'].map(id=>['sf-'+id,new Field()]));
fields['sf-scene'].content={firstElementChild:{cloneNode:row}};
globalThis.document={getElementById:id=>fields[id]};
const apiCalls=[];
const prepared=[];
const script=await read('shortform.js');
const {initShortform}=await import('data:text/javascript;base64,'+Buffer.from(script).toString('base64'));
initShortform({request:async(...args)=>{apiCalls.push(args);return {scenes:[{title:'시작',prompt:'와이드샷'},{title:'중간',prompt:'측면 이동'},{title:'끝',prompt:'전체 10초, 16:9'}]};},getWorkSettings:()=>({model:'gpt-6.1-terra'}),setPrevisBusy(){},prepareScenario:options=>{prepared.push(options);return {queued:true};}});
fields['sf-title'].value='하늘을 나는 새'; fields['sf-summary'].value='숲에서 날아오른다.'; fields['sf-model'].value='gpt-6.1-terra';
await fields['sf-draft'].onclick();
assert.equal(apiCalls.length,1);
assert.equal(apiCalls[0][0],'/api/shortform/scenario');
fields['sf-scenes'].children[1].querySelector('textarea').value='편집된 측면 이동';
await fields['sf-generate'].onclick();
assert.equal(apiCalls.length,1,'handoff must not call the generation API');
assert.equal(prepared.length,1);
assert.match(prepared[0].prompt,/제목: 하늘을 나는 새/);
assert.match(prepared[0].prompt,/개요: 숲에서 날아오른다/);
assert.match(prepared[0].prompt,/1\. 시작\n와이드샷[\s\S]*2\. 중간\n편집된 측면 이동[\s\S]*3\. 끝/);
assert.match(fields['sf-status'].textContent,/진행 중인 작업이 끝나면/);
assert.match(fields['sf-status'].textContent,/추가 요청 없이/);
console.log('PASS edited scenario handoff, no auto generation, queued transition notice');
