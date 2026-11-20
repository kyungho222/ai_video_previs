const statusLabels = {
  passed: '확인 완료',
  issues_found: '보정 필요',
  incomplete: '일부 확인',
  unavailable: '확인 미완료',
};

const categoryLabels = {
  cast: '캐릭터 수',
  identity: '외형·의상',
  anatomy: '신체 구성',
  motion: '동작',
  staging: '위치·배치',
  camera: '카메라·구도',
};

export function renderVideoValidation(card, metadata = {}) {
  const reports = [metadata.inputValidation, metadata.outputValidation];
  const signature = JSON.stringify(reports);
  if (signature === card.validationSignature) return;
  card.validationSignature = signature;
  if (!reports.some(Boolean)) return;

  if (!card.validation) {
    card.validation = document.createElement('details');
    card.validation.className = 'wan-validation';
    card.container.append(card.validation);
  }
  card.validation.replaceChildren();
  const summary = document.createElement('summary');
  const latest = metadata.outputValidation ?? metadata.inputValidation;
  summary.textContent = '영상 검증 · ' + (statusLabels[latest.status] ?? '확인 중');
  card.validation.append(summary);

  for (const [index, report] of reports.entries()) {
    if (!report) continue;
    const title = document.createElement('p');
    title.textContent = (index === 0 ? '입력 검증 · ' : '생성 영상 표본 검증 · ') +
      (statusLabels[report.status] ?? '확인 중');
    const description = document.createElement('p');
    description.textContent = report.summary ?? '';
    card.validation.append(title, description);

    const findings = document.createElement('ul');
    for (const issue of report.issues ?? []) {
      const item = document.createElement('li');
      item.textContent = issue;
      findings.append(item);
    }
    for (const check of report.checks ?? []) {
      if (check.status === 'pass') continue;
      const item = document.createElement('li');
      const times = (check.times ?? []).map(time => Number(time).toFixed(2) + '초');
      item.textContent = (categoryLabels[check.category] ?? check.category) +
        (times.length ? ' · ' + times.join(', ') : '') + ' · ' + check.detail;
      findings.append(item);
    }
    if (findings.children.length) card.validation.append(findings);
    if (report.repairPrompt) {
      const label = document.createElement('p');
      label.textContent = '다음 생성에 사용할 보정 지시';
      const prompt = document.createElement('pre');
      prompt.style.whiteSpace = 'pre-wrap';
      prompt.textContent = report.repairPrompt;
      card.validation.append(label, prompt);
    }
  }
}
