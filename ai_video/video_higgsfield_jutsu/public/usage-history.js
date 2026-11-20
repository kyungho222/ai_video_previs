export function initUsageHistory({request, apiUrl}) {
  const historyPanel = document.getElementById('usage-history-panel');
  const records = document.getElementById('usage-history-records');
  const status = document.getElementById('usage-history-status');
  const refresh = document.getElementById('usage-history-refresh');
  const more = document.getElementById('usage-history-more');
  let loading = false;
  let deleting = false;
  let nextOffset = null;
  let showHidden = false;
  const hiddenView = document.createElement('button');
  hiddenView.type = 'button';
  hiddenView.textContent = '숨긴 기록 보기';
  hiddenView.setAttribute('aria-pressed', 'false');
  refresh.after(hiddenView);
  status.setAttribute('role', 'status');
  const selectAll = document.createElement('input');
  selectAll.type = 'checkbox';
  const selectLabel = document.createElement('label');
  selectLabel.append(selectAll, document.createTextNode(' 표시된 기록 전체 선택'));
  const bulkDelete = document.createElement('button');
  bulkDelete.type = 'button';
  bulkDelete.disabled = true;
  bulkDelete.textContent = '선택 삭제';
  hiddenView.after(selectLabel, bulkDelete);

  function selections() {
    return [...records.querySelectorAll('input[data-history-job]')];
  }

  function updateSelection() {
    const inputs = selections();
    const count = inputs.filter(input => input.checked).length;
    selectAll.checked = inputs.length > 0 && count === inputs.length;
    selectAll.indeterminate = count > 0 && count < inputs.length;
    selectAll.disabled = deleting || loading || inputs.length === 0;
    bulkDelete.disabled = deleting || loading || count === 0;
    bulkDelete.textContent = deleting ? '삭제 중…' : count ? '선택 삭제 (' + count + ')' : '선택 삭제';
  }

  selectAll.addEventListener('change', () => {
    for (const input of selections()) input.checked = selectAll.checked;
    updateSelection();
  });

  bulkDelete.addEventListener('click', async () => {
    if (deleting || loading) return;
    const selected = selections().filter(input => input.checked);
    if (!selected.length || !window.confirm('선택한 ' + selected.length + '개 사용기록과 응답 JSON을 삭제할까요?\n영상 파일은 삭제되지 않습니다.')) return;
    deleting = true;
    const controls = [...historyPanel.querySelectorAll('button, input')].map(node => ({node, disabled:node.disabled}));
    for (const {node} of controls) node.disabled = true;
    updateSelection();
    let removed = 0;
    const failures = [];
    try {
      for (const input of selected) {
        try {
          await request('/api/usage-history/' + encodeURIComponent(input.dataset.historyJob), {method:'DELETE'});
          const article = input.closest('article');
          for (const player of article.querySelectorAll('video')) player.pause();
          article.remove();
          removed += 1;
        } catch (error) {
          failures.push(input.dataset.historyJob + ' · ' + error.message);
        }
      }
      if (nextOffset !== null) nextOffset = Math.max(0, nextOffset - removed);
      status.textContent = removed + '개 기록 삭제 완료' + (failures.length ? ' · 삭제하지 못한 ' + failures.length + '개는 선택 상태로 유지됩니다. ' + failures.join(' / ') : '');
    } finally {
      deleting = false;
      for (const {node, disabled} of controls) node.disabled = disabled;
      updateSelection();
    }
  });
  updateSelection();
  const stateLabels = {completed:'완료', failed:'실패', running:'진행 중', preparing:'준비 중',
    queued:'대기', submitting:'요청 중', downloading:'저장 중', archived:'보관 기록'};

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function video(url, label) {
    const player = document.createElement('video');
    player.controls = true;
    player.preload = 'none';
    player.src = apiUrl(url);
    player.setAttribute('aria-label', label);
    return player;
  }

  function card(item) {
    const article = element('article', undefined, 'usage-history-record');
    const heading = element('div', undefined, 'usage-history-heading');
    const selection = document.createElement('input');
    selection.type = 'checkbox';
    selection.dataset.historyJob = item.jobId;
    selection.setAttribute('aria-label', 'Job ' + item.jobId + ' 선택');
    selection.addEventListener('change', updateSelection);
    heading.append(selection, element('h2', 'Job ' + item.jobId));
    const date = new Date(item.createdAt * 1000).toLocaleString('ko-KR');
    heading.append(element('span', date + ' · ' + (stateLabels[item.state] || item.state)));
    const grid = element('div', undefined, 'usage-history-grid');
    const previs = element('section');
    previs.append(element('h3', '프리비즈'));
    if (item.previs.length) {
      for (const media of item.previs) previs.append(video(media.url, '프리비즈 ' + item.jobId));
    } else {
      previs.append(element('p', item.state === 'running' ? '생성 중입니다.' : '보관된 영상 파일이 없습니다.'));
    }
    const prompts = element('section');
    prompts.append(element('h3', '채팅 프롬프트'));
    prompts.append(element('pre', item.request || item.scenePrompt || '저장된 프롬프트가 없습니다.'));
    const applied = item.appliedPrompt || item.scenePrompt;
    if (applied && applied !== (item.request || item.scenePrompt)) {
      prompts.append(element('h4', '적용된 장면 프롬프트'), element('pre', applied));
    }
    const generated = element('section');
    generated.append(element('h3', '생성 영상'));
    const completedVideos = item.videos.filter(job => job.state === 'completed');
    if (!completedVideos.length) generated.append(element('p', '완료된 생성 영상이 없습니다.'));
    for (const job of completedVideos) {
      generated.append(element('p', '영상 Job ' + job.id, 'usage-history-video-id'));
      generated.append(element('span', stateLabels[job.state] || job.state));
      if (job.videoUrl) {
        generated.append(video(job.videoUrl, '생성 영상 ' + job.id));
        if (job.expiresAt === null) generated.append(element('small', '영구 보관'));
      }
      else if (job.state === 'completed') generated.append(element('p', '영상 보관기간이 만료됐거나 파일이 없습니다.'));
    }
    grid.append(previs, prompts, generated);
    const detailsButton = element('button', '응답 JSON 보기');
    detailsButton.type = 'button';
    detailsButton.setAttribute('aria-expanded', 'false');
    const json = element('pre', '', 'usage-history-json');
    json.id = 'history-json-' + item.jobId;
    json.hidden = true;
    detailsButton.setAttribute('aria-controls', json.id);
    detailsButton.addEventListener('click', async () => {
      if (!json.hidden) {
        json.hidden = true;
        detailsButton.setAttribute('aria-expanded', 'false');
        detailsButton.textContent = '응답 JSON 보기';
        return;
      }
      detailsButton.disabled = true;
      detailsButton.textContent = 'JSON 조회 중…';
      try {
        const data = await request('/api/usage-history/' + encodeURIComponent(item.jobId));
        json.textContent = JSON.stringify(data, null, 2);
        json.hidden = false;
        detailsButton.setAttribute('aria-expanded', 'true');
        detailsButton.textContent = '응답 JSON 닫기';
      } catch (error) {
        status.textContent = '응답 JSON을 불러오지 못했습니다. ' + error.message;
        detailsButton.textContent = '응답 JSON 다시 보기';
      } finally {
        detailsButton.disabled = false;
      }
    });
    const deleteButton = element('button', '기록 삭제');
    deleteButton.type = 'button';
    deleteButton.setAttribute('aria-label', 'Job ' + item.jobId + ' 기록 삭제');
    deleteButton.addEventListener('click', async () => {
      if (!window.confirm('Job ' + item.jobId + '의 사용기록과 응답 JSON을 삭제할까요?\n영상 파일은 삭제되지 않습니다.')) return;
      deleteButton.disabled = true;
      hideButton.disabled = true;
      deleteButton.textContent = '삭제 중…';
      try {
        await request('/api/usage-history/' + encodeURIComponent(item.jobId), {method:'DELETE'});
        for (const player of article.querySelectorAll('video')) player.pause();
        article.remove();
        updateSelection();
        if (nextOffset !== null) nextOffset = Math.max(0, nextOffset - 1);
        status.textContent = '기록을 삭제했습니다. 표시 중인 기록 ' + records.children.length + '개';
      } catch (error) {
        status.textContent = '기록을 삭제하지 못했습니다. ' + error.message;
      } finally {
        deleteButton.disabled = false;
        hideButton.disabled = false;
        deleteButton.textContent = '기록 삭제';
      }
    });
    const hideButton = element('button', item.hidden ? '숨기기 해제' : '숨기기');
    hideButton.type = 'button';
    hideButton.setAttribute('aria-label', 'Job ' + item.jobId + (item.hidden ? ' 숨기기 해제' : ' 숨기기'));
    hideButton.addEventListener('click', async () => {
      hideButton.disabled = true;
      deleteButton.disabled = true;
      try {
        await request('/api/usage-history/' + encodeURIComponent(item.jobId) + '/visibility', {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({hidden:!item.hidden})
        });
        for (const player of article.querySelectorAll('video')) player.pause();
        article.remove();
        updateSelection();
        if (nextOffset !== null) nextOffset = Math.max(0, nextOffset - 1);
        status.textContent = item.hidden ? '기록을 다시 표시했습니다.' : '기록을 숨겼습니다. 숨긴 기록 보기에서 확인할 수 있습니다.';
      } catch (error) {
        status.textContent = '기록 표시 상태를 변경하지 못했습니다. ' + error.message;
      } finally {
        hideButton.disabled = false;
        deleteButton.disabled = false;
      }
    });
    article.append(heading, grid, detailsButton, hideButton, deleteButton, json);
    return article;
  }

  async function load(reset) {
    if (loading || deleting) return;
    loading = true;
    updateSelection();
    refresh.disabled = true;
    hiddenView.disabled = true;
    more.disabled = true;
    status.textContent = '사용기록을 불러오는 중…';
    try {
      const data = await request('/api/usage-history?offset=' + (reset ? 0 : nextOffset) + '&limit=20&hidden=' + showHidden);
      if (reset) records.replaceChildren();
      for (const item of data.items) records.append(card(item));
      nextOffset = data.nextOffset;
      more.hidden = nextOffset === null;
      status.textContent = (records.children.length ? records.children.length + '개 기록' :
        (showHidden ? '숨긴 기록이 없습니다.' : '표시할 사용기록이 없습니다.'));
    } catch (error) {
      status.textContent = '사용기록을 불러오지 못했습니다. ' + error.message;
    } finally {
      loading = false;
      refresh.disabled = false;
      hiddenView.disabled = false;
      more.disabled = false;
      updateSelection();
    }
  }

  refresh.addEventListener('click', () => void load(true));
  hiddenView.addEventListener('click', () => {
    showHidden = !showHidden;
    hiddenView.textContent = showHidden ? '일반 기록 보기' : '숨긴 기록 보기';
    hiddenView.setAttribute('aria-pressed', String(showHidden));
    void load(true);
  });
  more.addEventListener('click', () => void load(false));
  return () => void load(true);
}
