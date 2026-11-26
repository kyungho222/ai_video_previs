// Per-tab recovery and independent polling state; execution limits belong to the server.
export function createPrevisTasks(storage, key) {
  const jobs = new Map();
  const watching = new Map();
  try {
    for (const job of JSON.parse(storage.getItem(key) || '[]')) {
      if (typeof job?.jobId === 'string') jobs.set(job.jobId, job);
    }
  } catch {}
  function persist() {
    try { storage.setItem(key, JSON.stringify([...jobs.values()])); } catch {}
  }
  return {
    watching,
    saved: () => [...jobs.values()],
    remember(job) { jobs.set(job.jobId, job); persist(); },
    begin(id) {
      if (watching.has(id)) return null;
      const state = {cursor: -1};
      watching.set(id, state);
      return state;
    },
    stop(id) { watching.delete(id); },
    complete(id) { watching.delete(id); jobs.delete(id); persist(); }
  };
}

export function initPrevis({request, apiUrl, renderVideoValidation, getWorkSettings}) {
  const $ = id => document.getElementById(id);
  const previsTasks = createPrevisTasks(sessionStorage, 'previs.activeJobs.v2:' + location.pathname);
  const jobChoices = new Map();
  const jobPicker = document.createElement('select');
  jobPicker.id = 'console-job-picker';
  jobPicker.setAttribute('aria-label', '진행 중 또는 이전 작업 선택');
  jobPicker.style.maxWidth = '30rem';
  jobPicker.append(new Option('작업 목록 확인 중…', ''));
  const openJobButton = document.createElement('button');
  openJobButton.type = 'button';
  openJobButton.textContent = '선택 작업 불러오기';
  $('load-previs')?.after(jobPicker, openJobButton);
  let pendingScenarioTitles;
  let scenarioPrompt = '';
  let scenarioSessionId = null;
  let revisionSourceJobId = null;
  let editorVersion = 0;
  let sourceLoadEpoch = 0;
  let sourcesLoading = false;
  let sessionId = null,
    busy = false,
    currentTab = 'payload';
  const debug = {
    payload: null,
    response: null
  };
  const debugHistory = [];
  let debugSelected = -1;
  let hideRecovered = false,
    chatBusy = false,
    chatSubmitting = false,
    shortformBusy = false,
    maxConcurrentJobs = 2;
  const sourceCards = new Set(),
    wanCards = new Map(),
    wanWatching = new Set();
  const wanSubmitting = new Map(),
    wanActiveSources = new Map(),
    wanPanels = new Map();
  const assetPreviewURLs = new Set();
  const videoConfig = request('/api/video/config').then(config => {
    if (Number.isInteger(config.maxConcurrentJobs) && config.maxConcurrentJobs > 0) maxConcurrentJobs = config.maxConcurrentJobs;
    updateBusy();
    return config;
  }).catch(error => ({
    configured: false,
    message: error.message
  }));
  try {
    hideRecovered = sessionStorage.getItem('previs.hideRecovered') === '1';
  } catch {}

  function sourceKey(source) {
    return source.jobId ? 'job:' + source.jobId + ':' + source.mediaIndex : source.uploadedId ? 'uploaded:' + source.uploadedId : 'recovered:' + source.recoveredId;
  }

  function sourceBusy(source) {
    const key = sourceKey(source);
    return [...wanSubmitting.values(), ...wanActiveSources.values()].includes(key);
  }

  function activeCount() {
    return Number(chatSubmitting) + previsTasks.watching.size + Number(shortformBusy) + wanSubmitting.size + new Set([...wanWatching, ...wanActiveSources.keys()]).size;
  }

  function updateBusy() {
    busy = activeCount() > 0;
    $('send').disabled = chatBusy;
    $('reset').disabled = chatSubmitting || shortformBusy || wanSubmitting.size > 0;
    $('refresh').disabled = chatSubmitting || shortformBusy || wanSubmitting.size > 0;
    if ($('load-previs')) $('load-previs').disabled = sourcesLoading;
    if ($('queue-status')) $('queue-status').textContent = '이 화면에서 추적 중인 작업 ' + activeCount() + '개 · 서버 동시 실행 최대 ' + maxConcurrentJobs + '개 · 초과 요청은 순서대로 대기';
    for (const [panel, update] of wanPanels) {
      if (panel.isConnected) update();
      else wanPanels.delete(panel);
    }
  }

  function setBusy(value) {
    chatBusy = value;
    updateBusy();
  }

  function resetConsole() {
    if (chatSubmitting || shortformBusy || wanSubmitting.size) return;
    editorVersion++;
    sourceLoadEpoch++;
    sourcesLoading = false;
    if ($('load-previs')) $('load-previs').disabled = false;
    activePrevis = null;
    sessionId = null;
    pendingScenarioTitles = undefined;
    scenarioPrompt = '';
    scenarioSessionId = null;
    revisionSourceJobId = null;
    hideRecovered = true;
    sourceCards.clear();
    for (const [id, card] of wanCards) {
      if ($('messages').contains(card.container)) wanCards.delete(id);
    }
    for (const url of assetPreviewURLs) URL.revokeObjectURL(url);
    assetPreviewURLs.clear();
    try {
      sessionStorage.setItem('previs.hideRecovered', '1');
    } catch {}
    for (const video of $('messages').querySelectorAll('video')) video.pause();
    const welcome = document.createElement('p');
    welcome.className = 'welcome';
    welcome.textContent = '새 대화를 시작합니다. 원하는 장면과 동작, 배경, 카메라 구도를 입력하세요.';
    $('messages').replaceChildren(welcome);
    $('logs').replaceChildren();
    lastConnectionLog = '';
    $('input').value = '';
    $('input').placeholder = '원하는 장면과 요청사항을 입력하세요.';
    debug.payload = null;
    debug.response = null;
    debugHistory.length = 0;
    debugSelected = -1;
    if ($('debug-step')) $('debug-step').replaceChildren();
    debugView();
    $('input').focus();
  }

  function applyScenario({prompt, sceneTitles, sessionId: savedSessionId}) {
    resetConsole();
    pendingScenarioTitles = sceneTitles;
    scenarioPrompt = prompt.trim();
    scenarioSessionId = savedSessionId || null;
    $('input').placeholder = '추가 요청사항 (선택) · 비워 두고 보내기를 눌러도 생성됩니다.';
    message('시나리오 준비 완료 · 보내기를 누르면 프리비즈를 생성합니다.\n\n' + scenarioPrompt, 'user');
    if ($('sf-status')) $('sf-status').textContent = '새 시나리오가 준비되었습니다. 추가 요청 없이 보내기를 눌러도 생성됩니다.';
    $('console-chat-tab').click();
    $('input').focus();
  }
  $('reset').onclick = resetConsole;
  $('refresh').onclick = () => {
    if (!chatSubmitting && !shortformBusy && !wanSubmitting.size) window.location.reload();
  };

  function debugView() {
    const item = debugHistory[debugSelected] || debug;
    $('debug').textContent = item[currentTab] ? JSON.stringify(item[currentTab], null, 2) : '아직 ' + currentTab + ' 정보가 없습니다.';
  }

  function recordDebug(type, value) {
    const following = debugSelected === debugHistory.length - 1;
    if (type === 'payload') {
      const last = debugHistory.at(-1);
      const repeatedPoll = last?.payload?.tool?.endsWith('get_operation') && value.tool === last.payload.tool && value.arguments?.operationId === last.payload.arguments?.operationId;
      if (repeatedPoll) {
        last.payload = value;
        last.response = null;
      } else debugHistory.push({
        payload: value,
        response: null
      });
    } else if (debugHistory.length) debugHistory.at(-1).response = value;
    debug[type] = value;
    if (following || debugSelected < 0) debugSelected = debugHistory.length - 1;
    let selector = $('debug-step');
    if (!selector) {
      selector = document.createElement('select');
      selector.id = 'debug-step';
      selector.setAttribute('aria-label', '도구 호출 기록');
      $('debug').before(selector);
    }
    selector.replaceChildren(...debugHistory.map((item, index) => new Option((index + 1) + '. ' + (item.payload?.tool || '응답'), String(index))));
    selector.value = String(debugSelected);
    selector.onchange = () => {
      debugSelected = Number(selector.value);
      debugView();
    };
    debugView();
  }
  for (const button of document.querySelectorAll('[data-tab]')) button.onclick = () => {
    currentTab = button.dataset.tab;
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button));
    debugView();
  };

  function message(text, kind = '') {
    const el = document.createElement('div');
    el.className = 'message ' + kind;
    el.textContent = text;
    $('messages').append(el);
    el.scrollIntoView({
      block: 'end'
    });
    return el;
  }

  function log(text, error = false) {
    const el = document.createElement('div');
    el.className = 'log' + (error ? ' error' : '');
    el.textContent = text;
    $('logs').append(el);
    el.scrollIntoView({
      block: 'nearest'
    });
  }
  let lastConnectionLog = '',
    lastAuthState = null;
  async function status() {
    try {
      const res = await fetch(apiUrl('/api/jutsu/status'));
      if (!res.ok) throw Error('연결 상태 조회 실패');
      const data = await res.json();
      lastAuthState = data.state;
      const count = (data.tools || []).length;
      const detail = data.state === 'connected' ? '도구 확인 완료 · ' + count + '개 도구' : data.message;
      const key = JSON.stringify([data.state, detail]);
      if (key !== lastConnectionLog) {
        log(detail || '연결 상태 확인', data.state === 'unavailable');
        lastConnectionLog = key;
      }
      $('status').textContent = data.state === 'connected' ? 'Higgsfield 연결됨' :
        data.state === 'connecting' ? 'Higgsfield 연결 준비 중' :
        data.state === 'unavailable' ? 'Higgsfield 연결을 확인해 주세요.' : data.message;
      $('connect').textContent = data.state === 'connected' ? 'Higgsfield 3D Jutsu 연결됨' : 'Higgsfield 연결';
      // Authentication persistence and reconnect are managed by the backend.
      return data;
    } catch {
      lastAuthState = null;
      $('status').textContent = '콘솔 서버에 연결할 수 없습니다.';
      if (lastConnectionLog !== 'network-error') {
        log('연결 상태를 조회할 수 없습니다.', true);
        lastConnectionLog = 'network-error';
      }
      return null;
    }
  }
  let authWindow = null;
  async function beginLogin() {
    if (lastAuthState === 'connected') {
      await status();
      return;
    }
    if (authWindow && !authWindow.closed) {
      authWindow.focus();
      return;
    }
    const popup = window.open('about:blank', '_blank', 'popup,width=560,height=760');
    if (!popup) {
      $('status').textContent = '팝업을 허용한 뒤 Higgsfield 연결을 다시 눌러주세요.';
      return;
    }
    popup.opener = null;
    authWindow = popup;
    try {
      $('status').textContent = 'Higgsfield 로그인 연결을 준비하고 있습니다…';
      const result = await request('/api/jutsu/connect-browser', {
        method: 'POST'
      });
      if (result.status?.state === 'connected') {
        popup.close();
        await status();
        return;
      }
      if (result.authorizationUrl) {
        $('status').textContent = '별도 창에서 Higgsfield 권한 승인을 완료해 주세요.';
        if (!popup.closed) popup.location.replace(result.authorizationUrl);
        return;
      }
      popup.close();
      await status();
    } catch (error) {
      popup.close();
      $('status').textContent = error.message;
      log(error.message, true);
    }
  }
  $('connect').onclick = async event => {
    event.preventDefault();
    await beginLogin();
  };
  // The backend restores tokens independently of the console.
  async function refreshAuthDisplay() {
    if (document.visibilityState === 'visible' || authWindow) {
      const connection = await status();
      if (connection?.state === 'connected' && authWindow) {
        authWindow.close();
        authWindow = null;
      } else if (authWindow?.closed) {
        authWindow = null;
      }
    }
    setTimeout(() => void refreshAuthDisplay(), lastAuthState === 'connected' ? 1800000 : 5000);
  }
  let activePrevis;

  function saveActivePrevis(job) {
    activePrevis = job;
    previsTasks.remember(job);
  }

  function clearActivePrevis(jobId) {
    if (activePrevis?.jobId === jobId) activePrevis = null;
    previsTasks.complete(jobId);
  }

  function pendingPrevis(label) {
    const pending = message('', 'pending');
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    pending.append(spinner, document.createTextNode(label));
    return pending;
  }

  async function followPrevis(job, tracker) {
    if (activePrevis?.jobId === job.jobId) sessionId = job.sessionId;
    if (job.version) log('v' + job.version + (job.baseVersion ? ' · v' + job.baseVersion + ' 기준 수정' : ' · 새 장면 생성'));
    if (activePrevis?.jobId === job.jobId && [5, 10, 15].includes(job.duration)) $('duration').value = String(job.duration);
    for (;;) {
      const data = await request('/api/jobs/' + encodeURIComponent(job.jobId) + '?after=' + tracker.cursor + '&completeOnly=true');
      if (data === null) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        continue;
      }
      for (const entry of data.events) {
        if (entry.type === 'log') log(entry.value);
        if (entry.type === 'payload' || entry.type === 'response') recordDebug(entry.type, entry.value);
        if (entry.type === 'error') {
          log(entry.value, true);
          message(entry.value, 'error');
        }
        if (entry.type === 'complete') {
          if (activePrevis?.jobId === job.jobId) revisionSourceJobId = job.jobId;
          renderOutput(entry.value);
        }
        tracker.cursor = entry.sequence;
      }
      if (!['running', 'queued'].includes(data.state)) {
        recordDebug('response', data);
        clearActivePrevis(job.jobId);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  }

  async function showPrevis(job, pending) {
    const tracker = previsTasks.begin(job.jobId);
    if (!tracker) { pending.remove(); return; }
    tracker.pending = pending;
    updateBusy();
    try {
      await followPrevis(job, tracker);
    } catch (error) {
      if (error.status === 404) {
        if (activePrevis?.jobId === job.jobId) sessionId = null;
        clearActivePrevis(job.jobId);
      }
      message(error.message, 'error');
      log(error.message, true);
    } finally {
      pending.remove();
      previsTasks.stop(job.jobId);
      updateBusy();
      void status();
    }
  }

  function restorePrevis() {
    for (const job of previsTasks.saved()) {
      if (!activePrevis && !scenarioPrompt) activePrevis = job;
      void showPrevis(job, pendingPrevis('프리비즈 ' + job.jobId.slice(0,8) + ' · 작업을 불러오고 있습니다.'));
    }
  }

  $('form').onsubmit = async event => {
    event.preventDefault();
    if (chatBusy) return;
    const requirements = $('input').value.trim();
    const text = revisionSourceJobId ? requirements : [scenarioPrompt, requirements && '추가 요청사항:\n' + requirements]
      .filter(Boolean).join('\n\n');
    if (!text) return;
    if (text.length > 8000) {
      message('시나리오와 요청사항은 합계 8,000자 이내로 입력하세요.', 'error');
      return;
    }
    const settings = getWorkSettings();
    if (!settings.model) {
      message('채팅 모델 목록을 확인할 수 없습니다. 페이지를 새로고침하세요.', 'error');
      return;
    }
    const alongsideRunningPrevis = activePrevis?.jobId && previsTasks.watching.has(activePrevis.jobId);
    const submissionSessionId = alongsideRunningPrevis && !revisionSourceJobId ? null : sessionId;
    const payload = scenarioSessionId ? {
      model: settings.model, usageLogEnabled: settings.usageLogEnabled === true,
      sessionId: scenarioSessionId,
      ...(revisionSourceJobId ? {sourceJobId:revisionSourceJobId} : {}),
      message:requirements, duration:$('duration').value, aspect:$('aspect').value
    } : {
      message:text, sessionId:submissionSessionId,
      ...(revisionSourceJobId ? {sourceJobId:revisionSourceJobId} : {}),
      ...settings, duration:$('duration').value, aspect:$('aspect').value,
      scenarioTitles:pendingScenarioTitles
    };
    const submittedScenarioSessionId = scenarioSessionId;
    chatSubmitting = true;
    editorVersion++;
    activePrevis = null;
    setBusy(true);
    const connection = await status();
    if (connection?.state !== 'connected') {
      if (connection?.state === 'authorizing') {
        $('status').textContent = 'Higgsfield 연결을 눌러 별도 창에서 권한을 승인해 주세요.';
      }
      // Connection and tool-discovery details are recorded by status(), never as chat messages.
      chatSubmitting = false;
      setBusy(false);
      return;
    }
    message(requirements || '저장된 시나리오로 프리비즈를 생성합니다.', 'user');
    const pending = pendingPrevis('요청하신 프리비즈를 구성하고 있습니다.');
    let job;
    try {
      job = await request('/api/previs/generate', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(submittedScenarioSessionId ? {sessionId: submittedScenarioSessionId} : {})
        },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      message(error.message, 'error');
      log(error.message, true);
    } finally {
      chatSubmitting = false;
      if (job) {
        $('input').value = '';
        saveActivePrevis(job);
        pending.append(document.createTextNode(' · 작업 ' + job.jobId.slice(0,8)));
        void refreshJobPicker();
        setBusy(false);
        updateBusy();
      } else {
        pending.remove();
        setBusy(false);
        void status();
      }
    }
    if (job) await showPrevis(job, pending);
  };
  $('input').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('form').requestSubmit();
    }
  });
  void refreshAuthDisplay();
  restorePrevis();
  void loadSources(false);
  void restoreWanJobs(false);
  void refreshJobPicker();

  function mediaSource(media) {
    if (media.videoSource) return media.videoSource;
    if (media.wanSource) return media.wanSource;
    let match = media.url.match(/^\/api\/jobs\/([a-f0-9-]{36})\/media\/(\d+)$/);
    if (match) return {
      jobId: match[1],
      mediaIndex: Number(match[2])
    };
    match = media.url.match(/^\/api\/recovered\/([a-f0-9-]{36})\.mp4$/);
    return match ? {
      recoveredId: match[1]
    } : null;
  }

  function renderOutput(output) {
    const items = (output.media ?? []).filter(media => {
      if (media.mediaType !== 'video/mp4') return true;
      const source = mediaSource(media),
        key = source && JSON.stringify(source);
      if (key && sourceCards.has(key)) return false;
      if (key) sourceCards.add(key);
      return true;
    });
    if (!items.length && output.media?.length) return;
    const result = message((output.version ? 'v' + output.version + ' · ' : '') + output.message),
      links = document.createElement('div');
    const firstSource = items.map(mediaSource).find(Boolean);
    if (firstSource) {
      result.dataset.previsJob = firstSource.jobId ?? firstSource.recoveredId ?? firstSource.uploadedId;
      const label = document.createElement('strong');
      label.textContent = '프리비즈 작업 ' + result.dataset.previsJob.slice(0,8) + ' · ';
      result.prepend(label);
    }
    const quality = output.intentQuality;
    if (quality) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '요청 반영 확인 · 불일치 ' + (quality.issues?.length ?? 0) + '건 · 의미 일치 확인 불가';
      details.append(summary);
      const labels = {framing: '화면 크기·구도', locked_drift: '고정 샷 이동', action_missing: '동작 누락',
        action_off_camera: '동작 중 촬영 누락', camera_instruction_changed: '카메라 지시 변경',
        requirement_not_represented: '요청 지시 누락', invalid_source_quote: '요청 근거 불일치'};
      for (const issue of quality.issues ?? []) {
        const row = document.createElement('p');
        row.textContent = (labels[issue.kind] ?? issue.message ?? issue.kind ?? '실행 검증 경고') +
          ' · ' + [issue.subject, ...(issue.subjects ?? []), issue.value, issue.field, ...(issue.reasons ?? [])].filter(Boolean).join(' / ');
        details.append(row);
      }
      for (const limitation of quality.limitations ?? []) {
        const row = document.createElement('p');
        row.textContent = '확인 불가 · ' + (limitation.subject ?? '') + ' 회전 중 정면 유지 · 시작 정면만 적용';
        details.append(row);
      }
      for (const requirement of quality.requirements ?? []) {
        const row = document.createElement('p');
        row.textContent = ({matched: '충족', mismatch: '불일치', unknown: '확인 불가'}[requirement.status] ?? '확인 불가') +
          ' · ' + (requirement.sourceQuote ?? '전체 요청의 의미·외형·배경');
        details.append(row);
      }
      result.append(details);
    }
    links.className = 'downloads';
    const expiry = Math.min(...items.map(media => media.expiresAt).filter(value => Number.isFinite(value)));
    if (Number.isFinite(expiry)) {
      const note = document.createElement('small');
      note.textContent = '보관 기한: ' + new Date(expiry * 1000).toLocaleString();
      result.append(note);
    } else if (items.length && items.every(media => media.expiresAt === null)) {
      const note = document.createElement('small');
      note.textContent = '영구 보관';
      result.append(note);
    }
    for (const media of items) {
      if (media.expiresAt && Date.now() >= media.expiresAt * 1000) {
        const expired = document.createElement('p');
        expired.textContent = media.name + ' · 보관기간이 만료되었습니다.';
        result.append(expired);
        continue;
      }
      if (media.mediaType === 'video/mp4') {
        const player = document.createElement('video');
        player.controls = true;
        player.preload = 'metadata';
        player.src = apiUrl(media.url);
        result.append(player);
        const source = mediaSource(media);
        if (source?.jobId) {
          const revise = document.createElement('button');
          revise.type = 'button';
          revise.textContent = '이 프리비즈 수정';
          revise.onclick = async () => {
            if (chatBusy) return;
            const version = ++editorVersion;
            activePrevis = null;
            try {
              const context = await request('/api/jobs/' + source.jobId + '/revision-context');
              if (version !== editorVersion) return;
              revisionSourceJobId = context.sourceJobId;
              sessionId = context.sessionId;
              scenarioSessionId = null;
              scenarioPrompt = context.scenePrompt || media.scenePrompt || '';
              pendingScenarioTitles = context.scenarioTitles || undefined;
              if ([5, 10, 15].includes(context.duration)) $('duration').value = String(context.duration);
              if (context.aspect) $('aspect').value = context.aspect;
              $('input').placeholder = '선택한 프리비즈의 수정 요청사항을 입력하세요.';
              message('수정 기준 프리비즈 선택 · ' + source.jobId.slice(0, 8));
              $('input').focus();
            } catch (error) { message(error.message, 'error'); }
          };
          result.append(revise);
        }
        if (source) void attachWan(result, source, media.scenePrompt ?? '', media.sourceDuration, media.workSettings);
      }
      const link = document.createElement('a');
      link.href = apiUrl(media.url) + (media.url.includes('?') ? '&' : '?') + 'download=1';
      link.download = media.name;
      link.textContent = media.mediaType === 'video/mp4' ? 'MP4 다운로드' : '.blend 다운로드';
      links.append(link);
    }
    if (links.children.length) result.append(links);
  }
  async function loadSources(explicit) {
    if (sourcesLoading) return;
    if (explicit) {
      hideRecovered = false;
      try {
        sessionStorage.removeItem('previs.hideRecovered');
      } catch {}
    }
    if (hideRecovered) return;
    const epoch = ++sourceLoadEpoch;
    sourcesLoading = true;
    updateBusy();
    try {
      const responses = await Promise.allSettled([request('/api/recovered'), request('/api/video/sources')]);
      if (hideRecovered || epoch !== sourceLoadEpoch) return;
      const groups = responses.filter(result => result.status === 'fulfilled').map(result => result.value);
      for (const response of responses)
        if (response.status === 'rejected') log('프리비즈 목록 일부 조회 오류: ' + response.reason.message, true);
      for (const data of groups)
        for (const output of data.results ?? []) renderOutput(output);
      if (explicit && groups.length) log('프리비즈 목록 확인 완료 · 조회 결과 ' + groups.reduce((count, data) => count + (data.results?.length ?? 0), 0) + '개');
      if (explicit && responses.every(result => result.status === 'fulfilled') && !groups.some(data => data.results?.length)) message('저장된 프리비즈가 없습니다. 먼저 장면을 생성하세요.');
    } catch (error) {
      if (epoch === sourceLoadEpoch) log(error.message, true);
    } finally {
      if (epoch === sourceLoadEpoch) {
        sourcesLoading = false;
        updateBusy();
      }
    }
  }
  if ($('load-previs')) $('load-previs').onclick = () => {
    restorePrevis();
    void loadSources(true);
    void restoreWanJobs(true);
    void refreshJobPicker();
  };

  async function refreshJobPicker() {
    const previous = jobPicker.value;
    const responses = await Promise.allSettled([
      request('/api/usage-history?offset=0&limit=100&hidden=false'), request('/api/video/jobs')
    ]);
    jobChoices.clear();
    const labels = {running:'진행 중',queued:'대기',preparing:'준비 중',submitting:'요청 중',
      downloading:'저장 중',completed:'완료',archived:'보관',failed:'실패'};
    const items = responses[0].status === 'fulfilled' ? responses[0].value.items ?? [] : [];
    for (const item of items) jobChoices.set('previs:' + item.jobId, {kind:'previs', item});
    const videos = responses[1].status === 'fulfilled' ? responses[1].value.jobs ?? [] : [];
    for (const item of videos) jobChoices.set('video:' + item.id, {kind:'video', item});
    jobPicker.replaceChildren(new Option('최근 작업 선택 · 이전 기록은 History에서 불러오기', ''));
    for (const [key, {kind,item}] of jobChoices) {
      const id = item.jobId ?? item.id;
      const title = (item.request || item.scenePrompt || '').replace(/\s+/g, ' ').slice(0,40);
      jobPicker.append(new Option((kind === 'video' ? '영상' : '프리비즈') + ' · ' +
        (labels[item.state] ?? item.state) + ' · ' + id.slice(0,8) + (title ? ' · ' + title : ''), key));
    }
    if (jobChoices.has(previous)) jobPicker.value = previous;
    openJobButton.disabled = !jobChoices.size;
    if (responses.every(result => result.status === 'rejected')) log('작업 목록을 불러오지 못했습니다.', true);
  }

  async function openHistoryJob(item) {
    if (chatSubmitting) throw new Error('요청 접수가 끝난 후 작업을 선택해 주세요.');
    editorVersion++;
    $('console-chat-tab').click();
    if (['running','queued'].includes(item.state)) {
      const job = {jobId:item.jobId,sessionId:item.sessionId,version:item.version};
      saveActivePrevis(job);
      sessionId = item.sessionId;
      scenarioSessionId = null;
      scenarioPrompt = item.scenePrompt || item.request || '';
      revisionSourceJobId = null;
      void showPrevis(job,pendingPrevis('프리비즈 ' + item.jobId.slice(0,8) + ' · 작업 상태를 불러옵니다.'));
    } else {
      activePrevis = null;
      const media = (item.previs ?? []).map(value => ({...value, name:'previs.mp4',mediaType:'video/mp4',
        videoSource:{[item.kind === 'uploaded' ? 'uploadedId' : item.kind === 'recovered' ? 'recoveredId' : 'jobId']:item.jobId,
                     mediaIndex:value.index ?? 0},
        scenePrompt:item.scenePrompt || item.request || '',workSettings:item.workSettings}));
      if (media.length) renderOutput({message:'불러온 프리비즈 · ' + item.jobId,media,version:item.version});
      else message('작업 ' + item.jobId + ' · ' + (item.state === 'failed' ? '생성 실패 · History의 응답 JSON을 확인하세요.' : '보관된 프리비즈 파일이 없습니다.'));
      if (item.kind === 'job' && media.length) {
        revisionSourceJobId = item.jobId;
        sessionId = item.sessionId;
        scenarioSessionId = null;
        scenarioPrompt = item.scenePrompt || item.request || '';
        $('input').placeholder = '불러온 프리비즈의 수정 요청사항을 입력하세요.';
      }
      for (const video of item.videos ?? []) {
        void request('/api/video/jobs/' + encodeURIComponent(video.id)).then(job => watchWan(job))
          .catch(error => message(error.message,'error'));
      }
    }
    $('messages').querySelector('[data-previs-job="' + CSS.escape(item.jobId) + '"]')?.scrollIntoView({block:'nearest'});
  }

  async function openVideoJob(item) {
    $('console-chat-tab').click();
    const job = await request('/api/video/jobs/' + encodeURIComponent(item.id));
    void watchWan(job);
    wanCards.get(job.id)?.container.scrollIntoView({block:'nearest'});
  }
  openJobButton.onclick = async () => {
    const selection = jobChoices.get(jobPicker.value);
    if (!selection) return;
    try {
      if (selection.kind === 'video') await openVideoJob(selection.item);
      else await openHistoryJob(selection.item);
    } catch (error) { message(error.message,'error'); }
  };

  function field(label, control) {
    const el = document.createElement('label');
    const text = document.createElement('span');
    text.textContent = label;
    el.append(text, control);
    control.setAttribute('aria-label', label);
    return el;
  }

  function select(values) {
    const el = document.createElement('select');
    for (const [value, label] of values) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      el.append(option);
    }
    return el;
  }
  async function attachWan(result, source, scenePrompt = '', sourceDuration = 5, workSettings) {
    let config = await videoConfig;
    let panelBusy = false;

    function panelLocked() {
      return panelBusy || sourceBusy(source);
    }
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'wan-open';
    open.textContent = '영상모델로 영상 생성';
    open.setAttribute('aria-expanded', 'false');
    const panel = document.createElement('form');
    panel.className = 'wan-panel';
    panel.hidden = true;
    const description = document.createElement('p');
    description.className = 'wan-help';
    description.textContent = '프리비즈의 카메라·구도·위치·주요 행동과 씬 흐름을 유지하고, 외형은 에셋으로 교체하며 관절 움직임과 동작 연결은 자연스럽게 재구성하도록 공통 지시를 적용합니다. 공간 가이드 방식은 이미지 생성·분석 요금과 영상 생성 요금이 발생합니다.';
    const prompt = document.createElement('textarea');
    prompt.rows = 6;
    prompt.required = true;
    prompt.maxLength = 8000;
    prompt.value = scenePrompt || (config.defaultPrompt ?? 'Preserve the supplied previs direction and reconstruct detailed movement naturally.');
    prompt.placeholder = '예: 이 캐릭터로 더 자연스럽고 사실적으로 만들어줘. 카메라와 행동 흐름은 유지해줘.';
    const duration = document.createElement('input');
    duration.type = 'number';
    duration.min = '2';
    duration.max = '30';
    duration.step = '1';
    duration.value = String(Number.isFinite(sourceDuration) ? Math.max(2, Math.min(15, Math.round(sourceDuration))) : 5);
    duration.required = true;
    const resolution = select([
      ['480P', '480p'],
      ['720P', '720p'],
      ['1080P', '1080p']
    ]);
    resolution.value = '480P';
    const providerSelect = select((config.models ?? [{
      provider: 'wan',
      label: 'Wan 3.0'
    }]).map(model => [model.provider, model.label]));
    providerSelect.value = config.provider ?? 'wan';
    const referenceMode = select([
      ['text_endpoints', '시작 이미지 고정 + 카메라·마무리 텍스트'],
      ['text_continuity', '첫 씬 기준 · 후속 씬 연속 연결 (테스트)'],
      ['video', '카메라·배치 MP4 + 에셋'],
      ['compare_first_frame', '합성 구도 이미지 + MP4'],
      ['compare_references', '멤버별 참조 이미지 + MP4']
    ]);
    if (!config.identityComparisonModes) {
      for (const option of [...referenceMode.options])
        if (option.value.startsWith('compare_')) option.remove();
    }
    const spatialInput = select([
      ['references', '참고 이미지'],
      ['first_frame', '시작 이미지'],
      ['first_last', '시작·종료 이미지']
    ]);
    const guideCount = select([
      ['auto', 'AI 자동 · 남은 한도 이내'], ...Array.from({
        length: 9
      }, (_, i) => [String(i + 1), String(i + 1)])
    ]);
    guideCount.value = 'auto';

    function effectiveMode() {
      return referenceMode.value;
    }
    const settings = document.createElement('div');
    settings.className = 'wan-settings';
    settings.append(field('영상모델', providerSelect), field('생성 방식', referenceMode), field('생성 길이(초)', duration), field('해상도', resolution));
    const note = document.createElement('p');
    note.className = 'wan-help';
    const guideOverride = document.createElement('input');
    guideOverride.type = 'checkbox';
    const guideOverrideLabel = document.createElement('label');
    guideOverrideLabel.append(guideOverride, document.createTextNode(' 공간 가이드 검증 실패 시에도 영상 생성 (테스트용)'));
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'wan-submit';
    submit.textContent = 'MP4 전달하고 생성';
    submit.dataset.configured = String(config.configured);
    submit.disabled = panelLocked() || !config.configured;
    const feedback = document.createElement('p');
    feedback.className = 'wan-feedback';
    feedback.setAttribute('role', 'status');
    const selectedAssets = [];
    const assets = document.createElement('fieldset');
    assets.className = 'wan-assets';
    const legend = document.createElement('legend');
    legend.textContent = '참고 에셋 (선택)';
    const assetHelp = document.createElement('p');
    assetHelp.className = 'wan-help';
    assetHelp.textContent = '에셋 전용 방식은 캐릭터·배경 이미지가 각각 필요합니다. 실사를 원하면 사실적인 비율의 캐릭터 이미지를 권장합니다. 과장된 눈과 인형 비율은 결과에도 영향을 줍니다. JPG, PNG, WEBP, BMP · 이미지당 20MB 이하 · 가로·세로 240~8000px. 배경음악은 MP3·WAV, 파일당 10MB 이하입니다. 각 음원의 앞부분을 합계 15초까지 사용합니다.';
    const assetPickers = document.createElement('div');
    assetPickers.className = 'wan-asset-pickers';
    const previews = document.createElement('div');
    previews.className = 'wan-asset-previews';
    const assetCount = document.createElement('p');
    assetCount.className = 'wan-help';
    assetCount.setAttribute('role', 'status');
    const roleNames = {
      background: '배경',
      character: '캐릭터',
      object: '사물',
      audio: '음원'
    };
    let audioPicker;
    const countAssets = audio => selectedAssets.filter(asset => (asset.role === 'audio') === audio).length;

    function renderAssets() {
      previews.replaceChildren();
      const audioLimit = config.referenceAudioLimit ?? 0;
      assetCount.textContent = '이미지 ' + countAssets(false) + '/10개 · 음원 ' + countAssets(true) + '/' + audioLimit + '개 · 이미지 에셋은 Image 번호로 참조할 수 있습니다.';
      let imageIndex = 0, audioIndex = 0;
      selectedAssets.forEach(asset => {
        const tile = document.createElement('div');
        tile.className = 'wan-asset-tile';
        const audio = asset.role === 'audio';
        const reference = audio ? 'Audio ' + (++audioIndex) : 'Image ' + (++imageIndex);
        const preview = document.createElement(audio ? 'audio' : 'img');
        preview.src = asset.preview;
        if (audio) {
          preview.controls = true;
          preview.preload = 'none';
          preview.style.width = '100%';
          preview.setAttribute('aria-label', reference + ' 미리듣기');
        } else {
          preview.alt = roleNames[asset.role] + ' 에셋 미리보기';
        }
        const title = document.createElement('strong');
        title.textContent = reference + ' · ' + roleNames[asset.role];
        const name = document.createElement('span');
        name.textContent = asset.file.name;
        name.title = asset.file.name;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'wan-asset-remove';
        remove.textContent = '삭제';
        remove.setAttribute('aria-label', reference + ' 에셋 삭제');
        remove.disabled = panelLocked();
        remove.onclick = () => {
          if (panelLocked()) return;
          selectedAssets.splice(selectedAssets.indexOf(asset), 1);
          URL.revokeObjectURL(asset.preview);
          assetPreviewURLs.delete(asset.preview);
          renderAssets();
          updatePanel();
        };
        tile.append(preview, title, name, remove);
        previews.append(tile);
      });
    }
    for (const [role, label] of Object.entries(roleNames)) {
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = true;
      const audio = role === 'audio';
      if (audio) audioPicker = input;
      input.accept = audio ? '.mp3,.wav,audio/mpeg,audio/wav' : 'image/jpeg,image/png,image/webp,image/bmp';
      input.className = 'wan-asset-input';
      input.disabled = panelLocked();
      input.onchange = () => {
        const files = Array.from(input.files ?? []);
        input.value = '';
        if (panelLocked()) return;
        const additions = files.filter(file => !selectedAssets.some(asset => asset.role === role && asset.file.name === file.name && asset.file.size === file.size && asset.file.lastModified === file.lastModified));
        const limit = audio ? config.referenceAudioLimit ?? 0 : 10;
        if (countAssets(audio) + additions.length > limit) {
          feedback.textContent = audio ? '참고 음원은 최대 ' + limit + '개까지 추가할 수 있습니다.' : '참고 이미지는 전체 유형을 합해 10개까지 추가할 수 있습니다.';
          return;
        }
        if (additions.some(file => file.size > (audio ? 10 : 20) * 1024 * 1024)) {
          feedback.textContent = audio ? '음원 파일은 각각 10MB 이하여야 합니다.' : '이미지 파일은 각각 20MB 이하여야 합니다.';
          return;
        }
        for (const file of additions) {
          const preview = URL.createObjectURL(file);
          assetPreviewURLs.add(preview);
          selectedAssets.push({
            role,
            file,
            preview,
            id: null
          });
        }
        feedback.textContent = '';
        renderAssets();
        updatePanel();
      };
      assetPickers.append(field(label + ' 에셋 추가', input));
    }
    renderAssets();
    assets.append(legend, assetHelp, assetPickers, previews, assetCount);
    const promptHelp = document.createElement('p');
    promptHelp.className = 'wan-help';
    promptHelp.textContent = '짧은 수정 요청만 입력해도 됩니다. 생략한 카메라·행동·씬 흐름은 프리비즈와 기존 장면 설명에서 이어받습니다. 에셋 전용 방식은 영상의 실제 동작을 참조할 수 없습니다.';
    panel.append(description, field('영상 요청 · 간단한 설명 또는 수정사항', prompt), promptHelp, settings, note, assets, guideOverrideLabel, submit, feedback);

    function updatePanel() {
      const locked = panelLocked(),
        mode = effectiveMode();
      const opening = mode === 'compare_first_frame',
        comparing = mode.startsWith('compare_');
      const endpoints = mode === 'text_endpoints';
      const continuous = mode === 'text_continuity';
      const spatial = endpoints || continuous || opening || mode.startsWith('spatial');
      const audioCount = countAssets(true);
      for (const option of referenceMode.options) {
        const modeName = ['text_endpoints', 'text_continuity'].includes(option.value) ? 'spatial' : option.value === 'compare_references' ? 'video' : option.value === 'compare_first_frame' ? 'spatial_motion' : option.value;
        option.disabled = Boolean(config.referenceModes && !config.referenceModes.includes(modeName)) ||
          (option.value.startsWith('compare_') && !(config.identityComparisonModes ?? []).includes(option.value));
      }
      if (referenceMode.selectedOptions[0]?.disabled && [...referenceMode.options].some(option => !option.disabled)) {
        referenceMode.value = [...referenceMode.options].find(option => !option.disabled)?.value ?? '';
        updatePanel();
        return;
      }
      const available = !(spatial || comparing) || config.spatialGuidesConfigured;
      submit.disabled = locked || !config.configured || !available;
      if (endpoints && !config.spatialInputFormats?.includes('first_frame')) submit.disabled = true;
      note.textContent = !config.configured ? config.message : !available ? '공간 가이드에는 서버의 OPENAI_API_KEY 설정이 필요합니다.' : mode === 'spatial' ? '에셋을 적용한 공간 이미지만 전달합니다. MP4는 영상모델에 보내지 않고 세부 동작은 모델이 구성합니다.' : mode === 'spatial_motion' ? '공간 이미지는 외형·구도, MP4는 동작 참고로 전달합니다. 동작이 더 강하게 고정될 수 있습니다.' : mode === 'images' ? '에셋과 텍스트를 전달합니다. MP4와 공간 가이드는 전달하지 않습니다.' : '원본 3D 장면으로 카메라·배치 전용 MP4를 만들고 에셋과 함께 전달합니다. 캐릭터의 세부 동작은 영상모델이 생성합니다.';
      if (endpoints) note.textContent = '프리비즈를 카메라·동선 타임라인으로 분석하고 에셋을 적용한 시작 이미지 1장을 고정하고 마무리 구도는 텍스트로 전달합니다. 캐릭터 세부 동작은 영상모델이 구성합니다.';
      submit.textContent = locked ? '생성 대기·진행 중' : activeCount() >= maxConcurrentJobs ? '대기열에 추가' : spatial ? '공간 가이드 생성 후 영상 생성' : mode === 'images' ? '에셋과 텍스트로 생성' : 'MP4 전달하고 생성';
      for (const control of panel.querySelectorAll('textarea,input,select,button'))
        if (control !== submit) control.disabled = locked;
      if (endpoints) spatialInput.value = 'first_frame';
      if (continuous) {
        spatialInput.value = 'references';
        note.textContent = '첫 씬을 기준으로 시작하고 후속 씬은 행동·대사·구도 의도를 참고합니다. 위치와 카메라는 직전 흐름에서 연결하며 명시적인 컷·장소 변경은 유지합니다.';
      }
      if (opening) spatialInput.value = 'first_frame';
      if (comparing && available) note.textContent = opening ?
        '합성 구도 이미지와 프리비즈 MP4를 함께 전달합니다. MP4의 카메라 경로를 유지하고 세부 동작은 자연스럽게 재구성합니다.' :
        '멤버별 참조 이미지와 배경 에셋, 프리비즈 MP4를 함께 전달합니다.';
      if (comparing) submit.textContent = locked ? '생성 대기·진행 중' : '선택한 방식으로 생성';
      if (endpoints && !config.spatialInputFormats?.includes('first_frame')) note.textContent = '선택 모델이 시작 이미지 입력을 지원하지 않습니다.';
      if (mode === 'spatial_motion') spatialInput.value = 'references';
      audioPicker.disabled = locked || !config.referenceAudioLimit;
      if (audioCount > (config.referenceAudioLimit ?? 0)) {
        submit.disabled = true;
        note.textContent += ' 선택한 모델의 배경음악 지원 범위를 확인하세요.';
      } else if (audioCount) {
        note.textContent += ' 생성된 보이스와 첨부 배경음악을 최종 영상에 합성합니다.';
      } else {
        note.textContent += ' 영상모델이 배경음악과 보이스를 생성합니다.';
      }
      spatialInput.disabled = locked || !spatial || mode === 'spatial_motion' || opening || endpoints;
      if (spatialInput.value === 'first_frame') guideCount.value = '1';
      if (spatialInput.value === 'first_last') guideCount.value = '2';
      guideCount.disabled = locked || !spatial || spatialInput.value !== 'references';
      if (spatial && spatialInput.value === 'references') note.textContent += ' AI가 씬·카메라·등장·배치 변화와 최종 구도를 선택합니다. 에셋 ' + countAssets(false) + '장 기준 가이드는 최대 ' + Math.max(0, 10 - countAssets(false)) + '장입니다. 이미지 생성 수에 따라 요금과 시간이 늘어납니다.';
      if (spatial && spatialInput.value !== 'references') note.textContent += ' 시작·종료 방식은 별도 에셋 이미지를 보내지 않으므로, 해당 프레임 밖 캐릭터의 외형 유지가 약해질 수 있습니다.';
    }
    providerSelect.onchange = async () => {
      panelBusy = true;
      updatePanel();
      try {
        config = await request('/api/video/config?provider=' + encodeURIComponent(providerSelect.value));
        resolution.replaceChildren(...config.resolutions.map(value => new Option(value, value)));
        resolution.value = config.resolutions.includes('480P') ? '480P' : config.resolutions[0];
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        panelBusy = false;
        renderAssets();
        updatePanel();
      }
    };
    referenceMode.onchange = () => {
      if (referenceMode.value === 'spatial_motion' && spatialInput.value !== 'references') guideCount.value = 'auto';
      updatePanel();
    };
    spatialInput.onchange = () => {
      if (spatialInput.value === 'references') guideCount.value = 'auto';
      updatePanel();
    };
    prompt.addEventListener('input', updatePanel);
    wanPanels.set(panel, updatePanel);
    updatePanel();
    open.onclick = () => {
      panel.hidden = !panel.hidden;
      open.setAttribute('aria-expanded', String(!panel.hidden));
    };
    panel.onsubmit = async event => {
      event.preventDefault();
      if (panelLocked() || submit.disabled) return;
      if ((effectiveMode() === 'images' || effectiveMode().startsWith('compare_')) && !['character', 'background'].every(role => selectedAssets.some(asset => asset.role === role))) {
        feedback.textContent = '에셋 전용 생성에는 캐릭터와 배경 이미지를 각각 추가하세요.';
        return;
      }
      if ((['text_endpoints', 'text_continuity'].includes(effectiveMode()) || effectiveMode().startsWith('spatial')) && !countAssets(false)) {
        feedback.textContent = '공간 가이드에 적용할 에셋을 추가하세요.';
        return;
      }
      const requestId = crypto.randomUUID();
      panelBusy = true;
      wanSubmitting.set(requestId, sourceKey(source));
      updateBusy();
      feedback.textContent = '영상 생성 요청을 준비합니다…';
      try {
        const selection = [...selectedAssets];
        const {model: chatModel, db_name, chatbotid} = workSettings ?? getWorkSettings();
        const body = {
          usageLogEnabled: getWorkSettings().usageLogEnabled === true,
          requestId,
          source,
          prompt: prompt.value.trim(),
          duration: Number(duration.value),
          resolution: resolution.value,
          provider: providerSelect.value,
          model: config.model,
          chatModel: chatModel || undefined,
          db_name,
          chatbotid,
          referenceMode: referenceMode.value,
          allowUnverifiedGuides: guideOverride.checked,
          characterMotion: 'independent',
          spatialInput: spatialInput.value,
          spatialGuideCount: guideCount.value === 'auto' ? 'auto' : Number(guideCount.value)
        };
        for (const [index, asset] of selection.entries()) {
          if (!asset.id) {
            feedback.textContent = '참고 에셋 ' + (index + 1) + '/' + selection.length + '개를 준비합니다…';
            const form = new FormData();
            form.append('file', asset.file);
            form.append('role', asset.role);
            const saved = await request('/api/video/assets', {
              method: 'POST',
              body: form
            });
            asset.id = saved.id;
          }
        }
        if (selection.length) body.assets = selection.map(asset => asset.id);
        const job = await request('/api/video/generate', {
          method: 'POST',
          headers: {
            'content-type': 'application/json'
          },
          body: JSON.stringify(body)
        });
        feedback.textContent = '영상 작업이 접수되었습니다. 아래에서 진행 상태를 확인하세요.';
        wanSubmitting.delete(requestId);
        await watchWan(job);
      } catch (error) {
        feedback.textContent = error.message;
        // A lost HTTP response is never grounds to submit another paid request.
        try {
          const job = await request('/api/video/jobs/' + requestId);
          feedback.textContent = '기존 영상 작업의 상태를 확인합니다.';
          wanSubmitting.delete(requestId);
          await watchWan(job);
        } catch {}
      } finally {
        wanSubmitting.delete(requestId);
        panelBusy = false;
        updateBusy();
      }
    };
    result.append(open, panel);
  }

  function renderGuides(card, job) {
    const bundle = job.metadata?.spatialGuides;
    if (!bundle || job.expired) {
      if (card.guides) card.guides.remove();
      card.guides = null;
      return;
    }
    if (!card.guides) {
      card.guides = document.createElement('div');
      card.container.append(card.guides);
    }
    card.guides.replaceChildren();
    if (bundle.selection) {
      const summary = document.createElement('p');
      summary.textContent = 'AI 주요 씬 ' + bundle.selection.scenes.length + '개 / 최대 ' + bundle.selection.budget + '개 · ' + bundle.selection.coverageNotes;
      card.guides.append(summary);
    }
    const title = document.createElement('p');
    title.textContent = job.options.identityComparison === 'B' ? (job.options.referenceVideo ? '영상 생성에 사용할 합성 구도 이미지' : '영상 생성에 사용할 합성 시작 프레임') : '참조 이미지 구성';
    card.guides.append(title);
    const fallback = job.metadata?.guideFallback;
    if (fallback) {
      const note = document.createElement('p');
      const used = fallback.usedGuideIndices.length;
      note.textContent = used ?
        '검증 통과 가이드 ' + used + '장으로 영상 생성 진행 · 미사용 ' + fallback.excludedGuideIndices.length + '장' :
        '사용 가능한 공간 가이드가 없어 원본 참조 자료로 영상 생성 진행';
      if (job.options.identityComparison === 'B') note.textContent += ' · 합성 이미지 보정 미완료';
      card.guides.append(note);
    }
    for (const guide of bundle.guides ?? []) {
      if (!['generated', 'verified', 'invalid', 'excluded'].includes(guide.status) || !guide.sha256) continue;
      const figure = document.createElement('figure'),
        image = document.createElement('img'),
        caption = document.createElement('figcaption');
      image.src = apiUrl('/api/video/jobs/' + job.id + '/guides/' + guide.index);
      image.alt = '공간 가이드 ' + (guide.index + 1);
      image.style.maxWidth = '100%';
      image.loading = 'lazy';
      const reasonLabels = {
        scene_transition: '씬 전환',
        camera_change: '카메라 변화',
        character_appearance: '캐릭터 등장·재등장',
        staging_change: '배치 변화',
        final_framing: '최종 구도'
      };
      caption.textContent = (guide.reasons ?? []).map(reason => reasonLabels[reason] ?? reason).join(' · ') + ' · ' + Number(guide.time).toFixed(2) + '초 구도 · ' + (guide.status === 'verified' ? '검증 완료' : guide.status === 'excluded' ? (job.options?.previsTranscript ? '보정 필요 · 생성에 사용' : '가이드 미사용') : guide.status === 'invalid' ? (job.state === 'failed' ? '검증 실패' : '보정 중') : '검증 중');
      figure.append(image, caption);
      card.guides.append(figure);
    }
  }

  function renderWanJob(job, outputTarget) {
    let card = wanCards.get(job.id);
    if (!card) {
      const title = '영상 작업 ' + job.id.slice(0,8) + ' · ' + job.options.duration + '초 · ' + job.options.resolution;
      const container = message(title, 'wan-result');
      if (outputTarget?.isConnected) outputTarget.append(container);
      const label = document.createElement('small');
      label.textContent = '참고 프리비즈: ' + (job.source.jobId ?? job.source.recoveredId ?? job.source.uploadedId).slice(0, 8);
      if (job.options.assets?.length) label.textContent += ' · 참고 에셋 ' + job.options.assets.length + '개';
      if (job.options.audioAssets?.length) label.textContent += (job.options.audioMode === 'bgm' ? ' · 배경음악 ' : ' · 참고 음원 ') + job.options.audioAssets.length + '개';
      const comparisonLabel = (job.options.referenceVideo ? {
        A: '멤버별 참조 이미지 + MP4',
        B: '합성 구도 이미지 + MP4'
      } : {
        A: '멤버별 참조 이미지',
        B: '합성 시작 프레임'
      })[job.options.identityComparison];
      label.textContent += ' · ' + (comparisonLabel ?? ({
        spatial: job.options.sceneFlowPolicy ? '첫 씬 기준 · 후속 씬 연속 연결' : job.options.previsTranscript ? '시작 이미지 고정 + 카메라·마무리 텍스트' : '공간 이미지',
        spatial_motion: '공간 이미지 + 동작 MP4',
        images: '에셋 전용',
        video: job.metadata.referenceVideoKind === 'camera_staging' ? '카메라·배치 MP4' : '기존 MP4 참조'
      } [job.options.referenceMode] ?? 'MP4 참조'));
      const state = document.createElement('p');
      state.setAttribute('role', 'status');
      container.append(label, state);
      card = {
        container,
        state,
        hasVideo: false
      };
      wanCards.set(job.id, card);
    }
    card.state.textContent = job.expired ? '보관기간이 만료되었습니다.' : job.message;
    card.state.classList.toggle('error', job.state === 'failed' || job.expired);

    if (!card.sourceInfo) {
      card.sourceInfo = document.createElement('small');
      card.state.before(card.sourceInfo);
    }
    const identity = job.metadata?.sourceIdentity;
    const source = identity?.source ?? job.source;
    const sourceId = source?.jobId ?? source?.uploadedId ?? source?.recoveredId ?? '';
    card.sourceInfo.textContent = '참조 프리비즈 · ' + sourceId
      + (source?.mediaIndex !== undefined ? ' · 파일 ' + (source.mediaIndex + 1) : '')
      + (identity?.sha256 ? ' · SHA-256 ' + identity.sha256.slice(0, 12) : ' · 원본 해시 기록 없음');
    if ((job.expiresAt === null || Number.isFinite(job.expiresAt)) && !card.expiry) {
      card.expiry = document.createElement('small');
      card.container.append(card.expiry);
    }
    if (card.expiry) card.expiry.textContent = job.expiresAt === null ? '영구 보관' : Number.isFinite(job.expiresAt) ? '보관 기한: ' + new Date(job.expiresAt * 1000).toLocaleString() : '';
    if (job.expired) {
      for (const media of card.container.querySelectorAll('video,a')) media.remove();
      card.hasVideo = false;
    }
    const snapshots = job.expired ? [] : job.metadata?.scenarioSnapshots ?? [];
    const snapshotSignature = JSON.stringify(snapshots);
    if (card.snapshotSignature !== snapshotSignature) {
      card.snapshots?.remove();
      if (snapshots.length) {
        const group = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = '시나리오 씬 스냅샷 ' + snapshots.length + '장';
        group.append(summary);
        for (const snapshot of snapshots) {
          const figure = document.createElement('figure');
          const image = document.createElement('img');
          const caption = document.createElement('figcaption');
          caption.textContent = (snapshot.index + 1) + '. ' + snapshot.title + ' · ' + snapshot.time.toFixed(2) + '초';
          image.src = apiUrl(snapshot.url);
          image.alt = snapshot.title;
          image.loading = 'lazy';
          image.style.maxWidth = '100%';
          figure.append(image, caption);
          group.append(figure);
        }
        card.state.after(group);
        card.snapshots = group;
      }
      card.snapshotSignature = snapshotSignature;
    }
    const guideSignature = JSON.stringify([job.expired, job.state, job.metadata?.spatialGuides, job.metadata?.guideFallback]);
    if (card.guideSignature !== guideSignature) {
      renderGuides(card, job);
      card.guideSignature = guideSignature;
    }
    const characterDirection = job.metadata?.characterDirection;
    if (characterDirection?.cast?.length) {
      if (!card.characterPlanText) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = '캐릭터별 ID·행동·대사·음성';
        card.characterPlanText = document.createElement('pre');
        details.append(summary, card.characterPlanText);
        card.container.append(details);
      }
      card.characterPlanText.textContent = JSON.stringify({
        status: characterDirection.status,
        warning: characterDirection.warning,
        characters: characterDirection.cast,
        direction: characterDirection.plan
      }, null, 2);
    }
    renderVideoValidation(card, job.metadata);
    if (job.metadata?.submittedPrompt) {
      if (!card.promptText) {
        const details = document.createElement('details');
        details.className = 'wan-prompt-audit';
        const summary = document.createElement('summary');
        summary.textContent = '생성에 사용한 프롬프트';
        card.promptText = document.createElement('pre');
        details.append(summary, card.promptText);
        card.container.append(details);
      }
      card.promptText.textContent = job.metadata.submittedPrompt;
    }
    if (job.state === 'completed' && job.videoUrl && !card.hasVideo) {
      const video = document.createElement('video');
      video.controls = true;
      video.preload = 'metadata';
      video.src = apiUrl(job.videoUrl);
      const link = document.createElement('a');
      link.href = apiUrl(job.videoUrl);
      link.download = 'wan-' + job.id + '.mp4';
      link.textContent = 'Wan 영상 다운로드';
      card.container.append(video, link);
      card.hasVideo = true;
    }
  }
  async function watchWan(job, outputTarget, trace) {
    trace?.('response', job);
    renderWanJob(job, outputTarget);
    if (['completed', 'failed'].includes(job.state)) {
      wanActiveSources.delete(job.id);
      updateBusy();
      return;
    }
    if (wanWatching.has(job.id)) return;
    wanWatching.add(job.id);
    wanActiveSources.set(job.id, sourceKey(job.source));
    updateBusy();
    try {
      while (!['completed', 'failed'].includes(job.state)) {
        await new Promise(resolve => setTimeout(resolve, 30_000));
        job = await request('/api/video/jobs/' + job.id);
        trace?.('response', job);
        renderWanJob(job);
      }
    } catch (error) {
      trace?.('response', {jobId: job.id, error: error.message});
      log('Wan 상태 조회 실패: ' + error.message + ' 기존 작업은 유지됩니다. 프리비즈 불러오기 또는 새로고침으로 상태를 다시 확인하세요.', true);
    } finally {
      wanWatching.delete(job.id);
      if (['completed', 'failed'].includes(job.state)) wanActiveSources.delete(job.id);
      updateBusy();
    }
  }
  async function restoreWanJobs(explicit) {
    try {
      const data = await request('/api/video/jobs');
      for (const job of data.jobs ?? []) {
        if (explicit || !hideRecovered || !['completed', 'failed'].includes(job.state)) void watchWan(job);
      }
    } catch (error) {
      log(error.message, true);
    }
  }

  $('versions').onclick = async () => {
    if (!sessionId) {
      message('현재 세션에 생성된 버전이 없습니다.');
      return;
    }
    try {
      const data = await request('/api/sessions/' + sessionId + '/versions');
      const box = message('장면 버전 기록');
      for (const version of data.versions) {
        const row = document.createElement('div');
        const label = document.createElement('p');
        label.textContent = '장면 ' + version.sceneId.slice(0, 8) + ' · v' + version.version + ' · ' + version.state + (version.baseVersion ? ' · 기준 v' + version.baseVersion : '') + ' · ' + version.request;
        const details = document.createElement('details'),
          summary = document.createElement('summary');
        summary.textContent = '계획 및 코드 보기';
        details.append(summary);
        details.addEventListener('toggle', async () => {
          if (!details.open || details.dataset.loaded) return;
          details.dataset.loaded = '1';
          try {
            const source = await request('/api/jobs/' + version.jobId + '/source');
            const pre = document.createElement('pre');
            pre.style.whiteSpace = 'pre-wrap';
            pre.textContent = JSON.stringify(source, null, 2);
            details.append(pre);
          } catch (error) {
            details.dataset.loaded = '';
            const text = document.createElement('p');
            text.textContent = error.message;
            details.append(text);
          }
        });
        row.append(label, details);
        box.append(row);
      }
    } catch (error) {
      message(error.message, 'error');
    }
  };

  return {
    openHistoryJob,
    openVideoJob,
    mediaSource,
    attachWan,
    getModel: () => $('chat-model').value,
    prepareScenario({prompt, sceneTitles, sessionId: savedSessionId}) {
      if (!prompt.trim()) throw new Error('시나리오 내용을 입력하세요.');
      const scenario = {prompt: prompt.trim(), sceneTitles: [...sceneTitles], sessionId: savedSessionId};
      if (chatSubmitting || shortformBusy || wanSubmitting.size) throw new Error('요청을 접수하고 있습니다. 잠시 후 다시 시도하세요.');
      applyScenario(scenario);
      return {queued: false};
    },
    setPrevisBusy(value) {
      shortformBusy = value;
      updateBusy();
    },
    restoreVideoCards(container) {
      $('messages').append(...container.querySelectorAll('.wan-result'));
    }
  };
}
