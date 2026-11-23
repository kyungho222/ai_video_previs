import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const html=await readFile(process.argv[2],'utf8');
const start=html.indexOf('async function loadChatModels()');
const code=html.slice(start,html.indexOf('void loadChatModels();',start));
const selects=['chat-model','sf-model'].map(id=>({id,replaceChildren(...options){this.options=options;}}));
const options=[
  {label:'GPT',value:'gpt-6.1-terra',configured:true},
  {label:'DeepSeek',value:'deepseek-chat',configured:true},
  {label:'Google',value:'gemini-2.5-flash',configured:false},
  {label:'Gemma',value:'google/gemma-4-26B-A4B-it',configured:true},
];
await new Function('request','modelSelects','Option','$',`${code}; return loadChatModels();`)(
  async()=>({options,default:'gpt-6.1-terra'}),selects,
  function Option(text,value){this.text=text;this.value=value;},()=>({})
);
for(const select of selects){
  assert.deepEqual(select.options.map(item=>item.value),options.map(item=>item.value));
  assert.equal(select.options[2].disabled,true);
  assert.equal(select.value,'google/gemma-4-26B-A4B-it', 'both console tabs use the scenario model');
}
await new Function('request','modelSelects','Option','$',`${code}; return loadChatModels();`)(
  async()=>({options:options.map(item=>item.value==='google/gemma-4-26B-A4B-it' ? {...item,configured:false} : item),default:'gpt-6.1-terra'}),selects,
  function Option(text,value){this.text=text;this.value=value;},()=>({})
);
assert.ok(selects.every(select=>select.value==='gpt-6.1-terra'), 'both fall back when Gemma is unavailable');
const syncStart=html.indexOf('for (const select of modelSelects)');
const sync=html.slice(syncStart,html.indexOf('const query =',syncStart));
for(const select of selects) select.addEventListener=(event,handler)=>{select.onchange=handler;};
new Function('modelSelects',sync)(selects);
for(const select of selects){
  select.value='deepseek-chat';
  select.onchange();
  assert.ok(selects.every(other=>other.value==='deepseek-chat'), 'console selections stay synchronized');
  select.value='gpt-6.1-terra';
  select.onchange();
}
const settingsStart=html.indexOf('const getWorkSettings =');
const settings=html.slice(settingsStart,html.indexOf('\nconst previs =',settingsStart));
const nodes={'sf-model':{value:'deepseek-chat'},'sf-db-name':{value:''},'sf-chatbotid':{value:''}};
const value=new Function('$',`${settings};return getWorkSettings();`)(id=>nodes[id]);
assert.equal(value.model,'deepseek-chat');
console.log('PASS actual model option values, unavailable model state, default, frontend payload');
