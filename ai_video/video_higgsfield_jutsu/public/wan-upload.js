import {
  renderVideoValidation
} from './video-validation.js';
import {
  createApiClient
} from './api-client.js';
const {
  apiUrl,
  request
} = createApiClient({
  errorOrder: ['detail', 'error'],
  defaultMessage: '요청을 처리하지 못했습니다.',
  attachStatus: true
});
const $ = id => document.getElementById(id);
const roles = {
  character: '캐릭터',
  background: '배경',
  other: '소품·기타'
};
const draftKey = 'wan-upload.draft.v1',
  pendingKey = 'wan-upload.pending.v1';
let config = null,
  sources = [],
  selectedId = '',
  assets = [],
  busy = false,
  pending = null,
  restoredRegion = false;
const jobs = new Map(),
  cards = new Map();
const terminal = job => ['completed', 'failed'].includes(job.state);

function feedback(text, error = false) {
  $('feedback').textContent = text;
  $('feedback').classList.toggle('error', error);
}

function uploadFeedback(text, error = false) {
  $('upload-feedback').textContent = text;
  $('upload-feedback').classList.toggle('error', error);
  feedback(text, error);
}

function formatNumber(value, digits) {
  return Number.isFinite(value) ? value.toFixed(digits) : '확인 불가';
}

function selected() {
  return sources.find(source => source.id === selectedId);
}

function remember() {
  try {
    localStorage.setItem(draftKey, JSON.stringify({
      selectedId,
      assets,
      guideSelection: 'key_scenes_v1',
      inputPolicy: 'camera-text-endpoints-v1',
      ...Object.fromEntries(['prompt', 'duration', 'resolution', 'region', 'reference-mode', 'spatial-input', 'guide-count', 'scene-project'].map(id => [id, $(id).value]))
    }));
  } catch {}
}

function savePending() {
  try {
    if (pending) localStorage.setItem(pendingKey, JSON.stringify(pending));
    else localStorage.removeItem(pendingKey);
  } catch {}
}

function updateControls() {
  const locked = busy || Boolean(pending);
  for (const control of $('generation-form').querySelectorAll('input,select,textarea,button')) control.disabled = locked;
  const mode = $('reference-mode').value;
  const opening = mode === 'compare_first_frame',
    comparing = mode.startsWith('compare_');
  const endpoints = mode === 'text_endpoints';
  const continuous = mode === 'text_continuity';
  const spatial = endpoints || continuous || opening || mode.startsWith('spatial');
  if (continuous) {
    $('spatial-input').value = 'references';
    $('guide-count').value = 'auto';
  }
  if (endpoints) $('spatial-input').value = 'first_last';
  if (opening) $('spatial-input').value = 'first_frame';
  if (mode === 'spatial_motion') $('spatial-input').value = 'references';
  $('spatial-input').disabled = locked || !spatial || mode === 'spatial_motion' || opening || endpoints;
  if ($('spatial-input').value === 'first_frame') $('guide-count').value = '1';
  if ($('spatial-input').value === 'first_last') $('guide-count').value = '2';
  $('guide-count').disabled = locked || !spatial || $('spatial-input').value !== 'references';
  if (config?.referenceModes) {
    for (const option of $('reference-mode').options) {
      const modeName = ['text_endpoints', 'text_continuity'].includes(option.value) ? 'spatial' : option.value === 'compare_references' ? 'video' : option.value === 'compare_first_frame' ? 'spatial_motion' : option.value;
      option.disabled = !config.referenceModes.includes(modeName) ||
        (option.value.startsWith('compare_') && !(config.identityComparisonModes ?? []).includes(option.value));
    }
    if ($('reference-mode').selectedOptions[0]?.disabled && [...$('reference-mode').options].some(option => !option.disabled)) {
      $('reference-mode').value = [...$('reference-mode').options].find(option => !option.disabled)?.value ?? '';
      updateControls();
      return;
    }
  }
  const available = !(spatial || comparing) || config?.spatialGuidesConfigured;
  $('guide-help').textContent = spatial && !available ? '공간 가이드에는 서버의 OPENAI_API_KEY 설정이 필요합니다.' : mode === 'spatial' ? '에셋을 적용한 공간 이미지만 전달합니다. MP4는 영상모델에 보내지 않으며 세부 동작은 모델이 구성합니다.' : mode === 'spatial_motion' ? '공간 이미지는 외형·구도, MP4는 동작 참고로 전달합니다. 동작이 더 강하게 고정될 수 있습니다.' : '원본 3D 장면에서 카메라·배치 전용 MP4를 렌더링합니다. 에셋과 행동 텍스트로 캐릭터의 세부 동작을 새로 생성합니다.';
  if (endpoints) $('guide-help').textContent = !available ? '상세 분석·이미지 합성에는 서버의 OPENAI_API_KEY 설정이 필요합니다.' : '카메라·동선 상세 텍스트와 에셋이 적용된 시작·종료 이미지로 생성합니다. 캐릭터의 세부 동작은 영상모델이 구성합니다.';
  if (continuous) $('guide-help').textContent = '첫 씬을 기준으로 시작하며, 후속 씬은 행동·대사·구도 의도를 참고해 직전 위치와 움직임에서 이어갑니다. 명시적인 컷·장소 변경은 유지합니다.';
  if (comparing) {
    $('guide-help').textContent = !available ? '캐릭터 비교에는 서버의 OPENAI_API_KEY 설정이 필요합니다.' :
      opening ? '합성 구도 이미지와 프리비즈 MP4를 함께 전달합니다. MP4의 카메라 경로를 유지하고 세부 동작은 자연스럽게 재구성합니다.' :
      '멤버별 참조 이미지와 배경 에셋, 프리비즈 MP4를 함께 전달합니다.';
  }
  if (spatial && $('spatial-input').value === 'references') $('guide-help').textContent += ' AI가 씬·카메라·등장·배치 변화와 최종 구도를 선택합니다. 에셋 ' + assets.length + '장 기준 가이드는 최대 ' + Math.max(0, 10 - assets.length) + '장입니다. 이미지 생성 수에 따라 요금과 시간이 늘어납니다.';
  if (spatial && $('spatial-input').value !== 'references') $('guide-help').textContent += ' 시작·종료 방식은 별도 에셋 이미지를 보내지 않으므로, 해당 프레임 밖 캐릭터의 외형 유지가 약해질 수 있습니다.';
  const endpointsUnsupported = endpoints && config && !config.spatialInputFormats?.includes('first_last');
  if (endpointsUnsupported) $('guide-help').textContent = '선택 모델이 시작·종료 이미지 입력을 지원하지 않습니다.';
  const active = [...jobs.values()].some(job => !terminal(job) && job.source.uploadedId === selectedId);
  $('generate').disabled = endpointsUnsupported || busy || !config?.configured || (!pending && (!selected() || active || !available));
  $('generate').textContent = pending ? '동일 요청으로 접수 확인' : active ? '선택한 프리비즈 생성 진행 중' : spatial ? '공간 가이드 생성 후 영상 생성' : '영상 생성';
}

function renderAssets() {
  $('asset-previews').replaceChildren();
  $('asset-count').textContent = '등록한 에셋 ' + assets.length + '/10개';
  assets.forEach((asset, index) => {
    const tile = document.createElement('div');
    tile.className = 'asset-tile';
    const image = document.createElement('img');
    image.src = apiUrl('/api/video/assets/' + asset.id);
    image.alt = roles[asset.role] + ' 에셋';
    const title = document.createElement('strong');
    title.textContent = 'Image ' + (index + 1) + ' · ' + roles[asset.role];
    const name = document.createElement('span');
    name.textContent = asset.name;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '삭제';
    remove.setAttribute('aria-label', title.textContent + ' 삭제');
    remove.onclick = () => {
      if (busy || pending) return;
      assets.splice(index, 1);
      renderAssets();
      remember();
      updateControls();
    };
    tile.append(image, title, name, remove);
    $('asset-previews').append(tile);
  });
}

function showSource(resetDuration = false) {
  const source = selected();
  $('source-select').value = source ? selectedId : '';
  if (source) {
    $('source-preview').src = apiUrl(source.videoUrl);
    $('source-preview').hidden = false;
    const meta = source.metadata;
    $('source-info').textContent = source.name + ' · ' + formatNumber(meta.duration, 2) + '초 · ' + meta.width + '×' + meta.height + ' · ' + formatNumber(meta.fps, 2) + 'fps' + (source.expiresAt === null ? ' · 영구 보관' : Number.isFinite(source.expiresAt) ? ' · 보관 기한 ' + new Date(source.expiresAt * 1000).toLocaleString() : '');
    if (resetDuration) $('duration').value = String(Math.max(1, Math.round(meta.duration) || 30));
  } else {
    $('source-preview').pause();
    $('source-preview').removeAttribute('src');
    $('source-preview').load();
    $('source-preview').hidden = true;
    $('source-info').textContent = '';
  }
  updateControls();
  remember();
}
async function loadSources() {
  const result = await request('/api/video/previs');
  sources = result.sources;
  $('source-select').replaceChildren(new Option('MP4를 등록하거나 선택하세요', ''));
  for (const source of sources) $('source-select').append(new Option(source.name + ' · ' + formatNumber(source.metadata.duration, 1) + '초', source.id));
  if (!selected()) selectedId = '';
  showSource();
}
$('source-select').onchange = () => {
  selectedId = $('source-select').value;
  showSource(true);
};
$('reload-sources').onclick = async () => {
  try {
    await loadSources();
  } catch (error) {
    feedback(error.message, true);
  }
};
$('previs-file').onchange = async () => {
  const file = $('previs-file').files?.[0];
  $('previs-file').value = '';
  if (!file || busy || pending) return;
  if (!file.name.toLowerCase().endsWith('.mp4') || file.size > 100 * 1024 * 1024) {
    uploadFeedback('100MB 이하의 MP4를 선택하세요.', true);
    return;
  }
  busy = true;
  updateControls();
  uploadFeedback('프리비즈를 등록하고 영상 정보를 확인합니다…');
  try {
    const form = new FormData();
    form.append('file', file);
    const source = await request('/api/video/previs', {
      method: 'POST',
      body: form
    });
    selectedId = source.id;
    await loadSources();
    showSource(true);
    uploadFeedback('프리비즈 등록 완료: ' + source.name + ' (' + formatNumber(source.metadata.fps, 2) + 'fps). 에셋과 요청을 확인한 뒤 영상 생성 버튼을 누르세요.');
  } catch (error) {
    uploadFeedback(error.message, true);
  } finally {
    busy = false;
    updateControls();
  }
};
for (const input of document.querySelectorAll('[data-role]')) input.onchange = async () => {
  const files = Array.from(input.files ?? []);
  input.value = '';
  if (!files.length || busy || pending) return;
  if (files.length + assets.length > 10 || files.some(file => file.size > 20 * 1024 * 1024)) {
    feedback('에셋은 총 10개까지, 이미지당 20MB 이하로 선택하세요.', true);
    return;
  }
  busy = true;
  updateControls();
  try {
    for (const [index, file] of files.entries()) {
      feedback('에셋 ' + (index + 1) + '/' + files.length + '개 등록 중…');
      const form = new FormData();
      form.append('file', file);
      form.append('role', input.dataset.role);
      const saved = await request('/api/video/assets', {
        method: 'POST',
        body: form
      });
      assets.push({
        id: saved.id,
        role: saved.role,
        name: saved.name
      });
      renderAssets();
      remember();
    }
    feedback('에셋 등록 완료. 아직 영상모델에는 전달하지 않았습니다.');
  } catch (error) {
    feedback(error.message, true);
  } finally {
    busy = false;
    updateControls();
  }
};
for (const id of ['prompt', 'duration', 'resolution', 'region', 'reference-mode', 'spatial-input', 'guide-count', 'scene-project']) $(id).addEventListener('change', () => {
  if (id === 'spatial-input' && $('spatial-input').value === 'references' || id === 'reference-mode' && $('reference-mode').value === 'spatial_motion' && $('spatial-input').value !== 'references') $('guide-count').value = 'auto';
  updateControls();
  remember();
});

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

function renderJob(job) {
  jobs.set(job.id, job);
  let card = cards.get(job.id);
  if (!card) {
    const container = document.createElement('article');
    container.className = 'job';
    const title = document.createElement('h3');
    title.textContent = 'Wan · ' + job.options.duration + '초 · ' + job.options.resolution + ' · ' + job.id.slice(0, 8) + (job.options.referenceRange ? ' · 원본 ' + job.options.referenceRange.start + '~' + job.options.referenceRange.end + '초 구간' : '');
    const state = document.createElement('p');
    state.setAttribute('role', 'status');
    const expiry = document.createElement('p');
    expiry.className = 'help';
    container.append(title, state, expiry);
    $('jobs').prepend(container);
    card = {
      container,
      state,
      expiry
    };
    cards.set(job.id, card);
  }
  card.state.textContent = job.expired ? '보관기간이 만료되었습니다.' : job.message;
  card.state.classList.toggle('error', job.state === 'failed');

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
  card.expiry.textContent = job.expiresAt === null ? '결과 영구 보관' : Number.isFinite(job.expiresAt) ? '결과 보관 기한: ' + new Date(job.expiresAt * 1000).toLocaleString() : '';
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
    if (!card.prompt) {
      const details = document.createElement('details'),
        summary = document.createElement('summary');
      summary.textContent = '생성에 사용한 프롬프트';
      card.prompt = document.createElement('pre');
      details.append(summary, card.prompt);
      card.container.append(details);
    }
    card.prompt.textContent = job.metadata.submittedPrompt;
  }
  if (job.expired && card.video) {
    card.video.remove();
    card.link.remove();
    card.video = null;
  }
  if (job.videoUrl && !job.expired && !card.video) {
    card.video = document.createElement('video');
    card.video.controls = true;
    card.video.preload = 'metadata';
    card.video.src = apiUrl(job.videoUrl);
    card.link = document.createElement('a');
    card.link.href = apiUrl(job.videoUrl);
    card.link.download = 'wan-' + job.id + '.mp4';
    card.link.textContent = '완성 영상 다운로드';
    card.container.append(card.video, card.link);
  }
  updateControls();
}
async function refreshJobs() {
  const result = await request('/api/video/jobs');
  for (const job of [...result.jobs].reverse())
    if (job.source?.uploadedId) renderJob(job);
  if (pending && !busy) {
    const job = await request('/api/video/jobs/' + pending.requestId).catch(() => null);
    if (job) {
      renderJob(job);
      pending = null;
      savePending();
      updateControls();
    }
  }
}
$('generation-form').onsubmit = async event => {
  event.preventDefault();
  if (busy || !config?.configured) return;
  if (!pending) {
    if (!selected()) {
      feedback('프리비즈 MP4를 먼저 등록하거나 선택하세요.', true);
      return;
    }
    const duration = Number($('duration').value);
    if (!Number.isInteger(duration) || duration <= 0) {
      feedback('생성 길이는 양의 정수로 입력하세요.', true);
      return;
    }
    if ((['text_endpoints', 'text_continuity'].includes($('reference-mode').value) || $('reference-mode').value.startsWith('spatial')) && !assets.length) {
      feedback('공간 가이드에 적용할 에셋을 추가하세요.', true);
      return;
    }
    pending = {
      requestId: crypto.randomUUID(),
      source: selected().source,
      assets: assets.map(asset => asset.id),
      prompt: $('prompt').value.trim() || config.defaultPrompt,
      duration,
      resolution: $('resolution').value,
      region: $('region').value,
      provider: $('video-provider').value,
      model: config.model,
      referenceMode: $('reference-mode').value,
      characterMotion: 'independent',
      sceneProjectId: $('scene-project').value.trim() || undefined,
      spatialInput: $('spatial-input').value,
      spatialGuideCount: $('guide-count').value === 'auto' ? 'auto' : Number($('guide-count').value)
    };
    savePending();
  }
  busy = true;
  updateControls();
  feedback('영상 생성 요청을 접수합니다…');
  try {
    const job = await request('/api/video/generate', {
      method: 'POST',
      headers: {
        'content-type': 'application/json'
      },
      body: JSON.stringify(pending)
    });
    renderJob(job);
    pending = null;
    savePending();
    feedback('접수 완료. 아래에서 진행 상태와 결과를 확인하세요.');
  } catch (error) {
    const existing = await request('/api/video/jobs/' + pending.requestId).catch(() => null);
    if (existing) {
      renderJob(existing);
      pending = null;
      savePending();
      feedback('접수된 기존 작업을 확인했습니다.');
    } else if ([400, 404, 409, 410, 413, 422, 503].includes(error.status)) {
      pending = null;
      savePending();
      feedback(error.message + ' 입력을 확인한 뒤 다시 요청하세요.', true);
    } else feedback(error.message + ' 접수 여부가 불확실하면 같은 요청 ID로 다시 확인합니다. 새 유료 요청을 만들지 않습니다.', true);
  } finally {
    busy = false;
    updateControls();
  }
};
$('video-provider').onchange = async () => {
  busy = true;
  updateControls();
  try {
    config = await request('/api/video/config?provider=' + encodeURIComponent($('video-provider').value));
    $('region').replaceChildren(...config.regions.map(value => new Option(value, value)));
    $('region').value = config.region;
    $('resolution').replaceChildren(...config.resolutions.map(value => new Option(value, value)));
    $('resolution').value = config.resolutions.includes('480P') ? '480P' : config.resolutions[0];
    $('config-status').textContent = config.message;
  } catch (error) {
    feedback(error.message, true);
  } finally {
    busy = false;
    updateControls();
  }
};
async function start() {
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey) || 'null');
    if (draft) {
      selectedId = draft.selectedId ?? '';
      assets = Array.isArray(draft.assets) ? draft.assets.filter(asset => /^[a-f0-9-]{36}$/.test(asset.id) && roles[asset.role]).slice(0, 10) : [];
      for (const id of ['prompt', 'duration', 'resolution', 'region', 'reference-mode', 'spatial-input', 'guide-count', 'scene-project'])
        if ((id !== 'reference-mode' || draft.inputPolicy === 'camera-text-endpoints-v1') && (id !== 'guide-count' || draft.guideSelection === 'key_scenes_v1') && draft[id] && (!$(id).options || [...$(id).options].some(option => option.value === draft[id]))) $(id).value = draft[id];
      restoredRegion = ['singapore', 'beijing'].includes(draft.region);
    }
    const saved = JSON.parse(localStorage.getItem(pendingKey) || 'null');
    if (saved && /^[a-f0-9-]{36}$/.test(saved.requestId)) pending = saved;
  } catch {}
  renderAssets();
  updateControls();
  try {
    config = await request('/api/video/config');
    $('video-provider').replaceChildren(...(config.models ?? [{
      provider: 'wan',
      label: 'Wan 3.0'
    }]).map(model => new Option(model.label, model.provider)));
    $('video-provider').value = config.provider ?? 'wan';
    $('config-status').textContent = config.message;
    if (!restoredRegion) $('region').value = config.region;
    await loadSources();
    await refreshJobs();
    if (pending) feedback('확인이 필요한 이전 요청이 있습니다. 동일 요청으로 접수 확인 버튼을 사용하세요.');
  } catch (error) {
    feedback(error.message, true);
  } finally {
    updateControls();
  }
}
void start();
setInterval(() => {
  if (document.visibilityState === 'visible') void refreshJobs().catch(error => feedback(error.message, true));
}, 3000);
