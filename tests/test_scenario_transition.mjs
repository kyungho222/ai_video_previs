import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';

const source = await readFile(process.argv[2], 'utf8');
const prepare = source.slice(source.indexOf('    prepareScenario('), source.indexOf('    setPrevisBusy(value)'));
const applyStart = source.indexOf('  function applyScenario(');
const helpers = applyStart < 0 ? '' : source.slice(applyStart, source.indexOf("  $('reset').onclick"));
const update = source.slice(source.indexOf('  function updateBusy()'), source.indexOf('  function setBusy('));

function consoleState({running = false, video = false, restoring = false} = {}) {
  const nodes = Object.fromEntries(['input','console-chat-tab','sf-status','send','reset','refresh','load-previs','queue-status'].map(id => [id, {
    value: 'old input', textContent: '', focus() {}, click() { this.clicked = true; }
  }]));
  return new Function('$', 'running', 'video', 'restoring', `
    let sessionId = 'old-session', pendingScenarioTitles, scenarioPrompt = '', scenarioSessionId = null;
    let queuedScenario = null, sourcesLoading = false, busy = running || video, chatBusy = running, shortformBusy = false, chatSubmitting = false;
    let activePrevis = restoring ? {jobId:'old-job'} : null;
    const wanSubmitting = new Map(), wanWatching = new Set(video ? ['video-job'] : []);
    const wanActiveSources = new Map(), wanPanels = new Map(), maxConcurrentJobs = 2;
    let resets = 0, restores = 0;
    const history = ['old-result'];
    const log = () => {}, message = () => {};
    const activeCount = () => Number(chatBusy) + Number(shortformBusy) + wanWatching.size;
    function resetConsole() {
      if (busy) throw new Error('reset during live job');
      resets++; sessionId = null; scenarioPrompt = ''; scenarioSessionId = null;
      pendingScenarioTitles = undefined; $('input').value = '';
    }
    function restorePrevis() { restores++; chatBusy = true; }
    ${helpers}
    ${update}
    const bridge = {${prepare}};
    return {
      prepare: value => bridge.prepareScenario(value),
      finish() { chatBusy = false; wanWatching.clear(); activePrevis = null; updateBusy(); },
      state: () => ({resets, restores, sessionId, scenarioPrompt, scenarioSessionId, queuedScenario, history, nodes:$('input')})
    };
  `)(id => nodes[id], running, video, restoring);
}
const scenario = {prompt: '새 시나리오', sceneTitles: ['시작'], sessionId: 'scenario-session'};

test('new scenario clears the previous screen and keeps History', () => {
  const console = consoleState();
  console.prepare(scenario);
  const state = console.state();
  assert.equal(state.resets, 1);
  assert.equal(state.sessionId, null);
  assert.equal(state.scenarioPrompt, scenario.prompt);
  assert.equal(state.scenarioSessionId, scenario.sessionId);
  assert.deepEqual(state.history, ['old-result']);
  assert.equal(state.nodes.value, '');
});

for (const options of [{running:true}, {video:true}, {restoring:true}]) {
  test('waits for live or resumed jobs before switching ' + JSON.stringify(options), () => {
    const console = consoleState(options);
    assert.equal(console.prepare(scenario).queued, true);
    assert.equal(console.state().resets, 0);
    assert.equal(console.state().scenarioPrompt, '');
    console.finish();
    assert.equal(console.state().resets, 1);
    assert.equal(console.state().scenarioPrompt, scenario.prompt);
    assert.equal(console.state().queuedScenario, null);
    console.finish();
    assert.equal(console.state().resets, 1, 'must switch only once');
  });
}

test('empty scenario leaves the previous screen intact', () => {
  const console = consoleState();
  assert.throws(() => console.prepare({...scenario, prompt:' '}), /시나리오/);
  assert.equal(console.state().resets, 0);
});

test('send without extra requirements submits the saved scenario', async () => {
  const fields = {form:{}, input:{value:''}, duration:{value:'10'}, aspect:{value:'16:9'}};
  const calls = [];
  const submit = source.slice(source.indexOf("  $('form').onsubmit ="), source.indexOf("  $('input').addEventListener"));
  const run = new Function('$','request', `
    let chatBusy=false, activePrevis=null, scenarioPrompt='새 시나리오', scenarioSessionId='saved-scenario';
    let sessionId=null, chatSubmitting=false, pendingScenarioTitles=['시작'];
    const status=async()=>({state:'connected'}), getWorkSettings=()=>({model:'selected-model'});
    const setBusy=()=>{}, message=()=>{}, log=()=>{}, updateBusy=()=>{};
    const pendingPrevis=()=>({remove(){}}), saveActivePrevis=()=>{}, showPrevis=async()=>{};
    ${submit}
    return $('form').onsubmit({preventDefault(){}});
  `);
  await run(id=>fields[id], async (url, options) => { calls.push({url, options}); return {jobId:'new-job'}; });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/previs/generate');
  assert.equal(calls[0].options.headers.sessionId, 'saved-scenario');
  assert.equal(JSON.parse(calls[0].options.body).sessionId, 'saved-scenario',
    'scenario context must survive a proxy dropping the custom header');
  assert.equal(JSON.parse(calls[0].options.body).message, '');
});

test('actual reset releases selected asset previews and removes chat cards only', () => {
  const reset = source.slice(source.indexOf('  function resetConsole()'), source.indexOf('  function applyScenario('));
  const fields = Object.fromEntries(['messages','logs','input','debug-step'].map(id => [id, {
    value:'old', replaceChildren(...values) { this.children = values; },
    contains(node) { return node === 'chat-card'; },
    querySelectorAll() { return [{pause() {}}]; }, focus() {}
  }]));
  const revoked = [];
  const state = new Function('$','URL','document','sessionStorage', `
    let busy=false, queuedScenario=null, activePrevis=null, lastPrevisEvent=10, sourceLoadEpoch=0, sourcesLoading=false;
    let sessionId='old', pendingScenarioTitles=['old'], scenarioPrompt='old', scenarioSessionId='old';
    let hideRecovered=false, lastConnectionLog='old', debugSelected=1;
    const sourceCards=new Set(['old']), wanCards=new Map([
      ['chat',{container:'chat-card'}], ['history',{container:'history-card'}]
    ]);
    const assetPreviewURLs=new Set(['blob:asset']), debug={payload:'old',response:'old'}, debugHistory=['old'];
    const debugView=()=>{};
    ${reset}
    resetConsole();
    return {sessionId, scenarioPrompt, sourceCount:sourceCards.size, cards:[...wanCards.keys()], assets:assetPreviewURLs.size};
  `)(id=>fields[id], {revokeObjectURL:url=>revoked.push(url)}, {createElement:()=>({})}, {setItem(){}});
  assert.deepEqual(state, {sessionId:null, scenarioPrompt:'', sourceCount:0, cards:['history'], assets:0});
  assert.deepEqual(revoked, ['blob:asset']);
  assert.equal(fields.messages.children.length, 1);
});
