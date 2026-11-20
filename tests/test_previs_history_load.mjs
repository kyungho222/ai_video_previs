import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';

const source = await readFile(process.argv[2], 'utf8');
const loader = source.slice(source.indexOf('  async function loadSources('), source.indexOf("  if ($('load-previs')) $('load-previs').onclick"));

function setup(request) {
  const rendered = [], logs = [], messages = [], removed = [];
  const instance = new Function('request','renderOutput','log','message','sessionStorage', `
    let hideRecovered=true, sourceLoadEpoch=0, sourcesLoading=false;
    const sourceCards = new Set();
    let scenarioPrompt='new scenario', scenarioSessionId='new-session', sessionId=null;
    const updateBusy=()=>{};
    ${loader}
    return {
      load:explicit=>loadSources(explicit),
      reset(){hideRecovered=true; sourceLoadEpoch++; sourcesLoading=false;},
      state:()=>({scenarioPrompt,scenarioSessionId,sessionId,sourcesLoading,hideRecovered})
    };
  `)(request, value=>rendered.push(value), value=>logs.push(value), value=>messages.push(value), {removeItem:key=>removed.push(key)});
  return Object.assign(instance, {rendered, logs, messages, removed});
}

test('explicit load shows old previs and preserves pending scenario', async () => {
  const calls = [];
  const state = setup(async url => {calls.push(url); return {results:[{message:url,media:[]}]};});
  await state.load(false);
  assert.equal(calls.length,0);
  await state.load(true);
  assert.deepEqual(calls,['/api/recovered','/api/video/sources']);
  assert.equal(state.rendered.length,2);
  assert.deepEqual(state.removed,['previs.hideRecovered']);
  assert.equal(state.state().scenarioPrompt,'new scenario');
  assert.equal(state.state().scenarioSessionId,'new-session');
});

test('repeated click shares in-flight load', async () => {
  const callbacks = [];
  const state = setup(() => new Promise(resolve=>callbacks.push(resolve)));
  const first = state.load(true);
  const second = state.load(true);
  assert.equal(callbacks.length,2,'one request per endpoint');
  callbacks.forEach(resolve=>resolve({results:[]}));
  await Promise.all([first,second]);
});

test('reset prevents a stale request from restoring old cards', async () => {
  const callbacks = [], rendered = [];
  const instance = setup(() => new Promise(resolve=>callbacks.push(resolve)));
  // Observe the renderer separately through a request result getter.
  const result = {get results(){rendered.push('read');return [{message:'old',media:[]}];}};
  const loading = instance.load(true);
  instance.reset();
  // A newer explicit load reenables display before the old response arrives.
  const newer = instance.load(true);
  callbacks[0](result); callbacks[1](result);
  await loading;
  assert.equal(rendered.length,0,'stale responses must be discarded even after a new load');
  callbacks[2]({results:[]}); callbacks[3]({results:[]});
  await newer;
});
