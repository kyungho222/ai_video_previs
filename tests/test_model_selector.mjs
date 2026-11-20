import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const html=await readFile(process.argv[2],'utf8');
const start=html.indexOf('async function loadChatModels()');
const code=html.slice(start,html.indexOf('void loadChatModels();',start));
const selects=[0,1].map(()=>({replaceChildren(...options){this.options=options;}}));
const options=[
  {label:'GPT',value:'gpt-6.1-terra',configured:true},
  {label:'DeepSeek',value:'deepseek-chat',configured:true},
  {label:'Google',value:'gemini-2.5-flash',configured:false},
];
await new Function('request','modelSelects','Option','$',`${code}; return loadChatModels();`)(
  async()=>({options,default:'gpt-6.1-terra'}),selects,
  function Option(text,value){this.text=text;this.value=value;},()=>({})
);
for(const select of selects){
  assert.deepEqual(select.options.map(item=>item.value),['gpt-6.1-terra','deepseek-chat','gemini-2.5-flash']);
  assert.equal(select.options[2].disabled,true);
  assert.equal(select.value,'gpt-6.1-terra');
}
const settingsStart=html.indexOf('const getWorkSettings =');
const settings=html.slice(settingsStart,html.indexOf('\nconst previs =',settingsStart));
const nodes={'sf-model':{value:'deepseek-chat'},'sf-db-name':{value:''},'sf-chatbotid':{value:''}};
const value=new Function('$',`${settings};return getWorkSettings();`)(id=>nodes[id]);
assert.equal(value.model,'deepseek-chat');
console.log('PASS actual model option values, unavailable model state, default, frontend payload');
