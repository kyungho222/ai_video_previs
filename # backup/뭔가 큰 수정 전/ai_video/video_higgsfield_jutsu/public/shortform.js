export function initShortform({request, getWorkSettings, setPrevisBusy, prepareScenario}) {
  const find = id => document.getElementById('sf-' + id);
  const fields = find('fields');
  const scenes = find('scenes');
  const exchange = [{}];
  const step = 0;
  let busy = false;

  function scenario() {
    return [...scenes.children].map(row => {
      const title = row.querySelector('input').value.trim();
      const prompt = row.querySelector('textarea').value.trim();
      return {title, prompt};
    }).filter(scene => scene.title || scene.prompt);
  }

  function renderExchange() {
    for (const key of ['payload', 'response']) {
      const text = exchange[step][key] ?? '';
      const panel = find(key);
      if (panel.textContent !== text) panel.textContent = text;
    }
  }

  function trace(index, key, value) {
    exchange[index][key] = JSON.stringify(value, null, 2);
    if (key === 'payload') delete exchange[index].response;
    if (index === step) renderExchange();
  }

  function render() {
    const content = scenario();
    find('draft').disabled = busy || !find('title').value.trim() || !find('summary').value.trim();
    find('generate').disabled = busy || !content.length;
    fields.disabled = busy;
    find('reset').disabled = busy;
    renderExchange();
    [...scenes.children].forEach((row, index, rows) => {
      const number = index + 1;
      row.querySelector('.sf-index').textContent = number;
      row.querySelector('input').setAttribute('aria-label', '장면 ' + number + ' 제목');
      row.querySelector('textarea').setAttribute('aria-label', '장면 ' + number + ' 내용');
      row.querySelector('.sf-remove').setAttribute('aria-label', '장면 ' + number + ' 삭제');
      const [up, down] = row.querySelectorAll('[data-move]');
      up.disabled = index === 0;
      down.disabled = index === rows.length - 1;
      up.setAttribute('aria-label', '장면 ' + number + ' 위로 이동');
      down.setAttribute('aria-label', '장면 ' + number + ' 아래로 이동');
    });
  }

  function edited() {
    render();
  }

  function addScene({title = '', prompt = ''} = {}) {
    const row = find('scene').content.firstElementChild.cloneNode(true);
    row.querySelector('input').value = title;
    const promptField = row.querySelector('textarea');
    promptField.value = prompt;
    promptField.setSelectionRange(0, 0);
    row.querySelector('.sf-remove').onclick = () => {
      row.remove();
      if (!scenes.children.length) addScene();
      edited();
      find('add').focus();
    };
    row.querySelector('[data-move="-1"]').onclick = () => {
      row.previousElementSibling?.before(row);
      edited();
      row.querySelector('input').focus();
    };
    row.querySelector('[data-move="1"]').onclick = () => {
      row.nextElementSibling?.after(row);
      edited();
      row.querySelector('input').focus();
    };
    scenes.append(row);
    return row;
  }

  function reset() {
    exchange[0] = {};
    find('status').textContent = '';
    renderExchange();
    find('title').value = '';
    find('summary').value = '';
    scenes.replaceChildren();
    addScene();
    render();
  }

  fields.addEventListener('input', edited);
  find('reset').onclick = reset;
  find('add').onclick = () => {
    const row = addScene();
    render();
    row.querySelector('input').focus();
  };
  find('draft').onclick = async () => {
    if (busy) return;
    const title = find('title').value.trim();
    const summary = find('summary').value.trim();
    const settings = getWorkSettings();
    if (!title || !summary) return;
    if (!settings.model) {
      find('status').textContent = '채팅 모델을 확인한 뒤 다시 시도하세요.';
      return;
    }
    busy = true;
    setPrevisBusy(true);
    find('draft').textContent = '시나리오 생성 중…';
    find('status').textContent = 'AI가 시나리오를 작성하고 있습니다…';
    render();
    let firstGeneratedRow;
    const payload = {title, summary, ...settings};
    trace(0, 'payload', payload);
    try {
      const result = await request('/api/shortform/scenario', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload)
      });
      trace(0, 'response', result);
      const generated = result.scenes;
      if (!Array.isArray(generated) || ![3, 4].includes(generated.length)) {
        throw new Error('AI가 완성된 씬 3~4개를 반환하지 않았습니다. 다시 시도하세요.');
      }
      for (const row of [...scenes.children]) {
        if (!row.querySelector('input').value.trim() && !row.querySelector('textarea').value.trim()) row.remove();
      }
      [firstGeneratedRow] = generated.map(scene => addScene(scene));
      find('status').textContent = '시나리오 카드 ' + generated.length + '개를 생성했습니다.';
    } catch (error) {
      trace(0, 'response', {error: error.message});
      find('status').textContent = error.message;
    } finally {
      busy = false;
      setPrevisBusy(false);
      find('draft').textContent = '시나리오 생성';
      render();
      firstGeneratedRow?.querySelector('input').focus();
    }
  };
  find('generate').onclick = async () => {
    if (busy) return;
    const title = find('title').value.trim();
    const summary = find('summary').value.trim();
    const cards = scenario();
    const sceneTitles = cards.map((scene, index) => scene.title || '장면 ' + (index + 1));
    const directions = cards.map((scene, index) =>
      (index + 1) + '. ' + [sceneTitles[index], scene.prompt].filter(Boolean).join('\n')).join('\n\n');
    const prompt = ['아래 장면 순서대로 숏폼 프리비즈를 만들어 주세요.',
      title && '제목: ' + title, summary && '개요: ' + summary, directions].filter(Boolean).join('\n\n');
    if (prompt.length > 8000) {
      find('status').textContent = '제목과 장면 내용을 줄여 전체 요청을 8,000자 이내로 작성하세요.';
      return;
    }
    if (!getWorkSettings().model) {
      find('status').textContent = '채팅 모델을 확인한 뒤 다시 시도하세요.';
      return;
    }
    busy = true;
    render();
    try {
      prepareScenario({prompt, sceneTitles});
      find('status').textContent = '프리비즈 채팅 입력란에 시나리오를 전달했습니다. 내용을 확인한 뒤 보내기를 누르세요.';
    } catch (error) {
      find('status').textContent = error.message;
    } finally {
      busy = false;
      render();
    }
  };

  reset();
}
