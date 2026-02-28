(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const canvas = $('editor-canvas');
  const ctx = canvas.getContext('2d');

  const state = {
    uploadId: null,
    sessionId: null,
    targets: [],
    currentTarget: null,
    zonesByTarget: {},
    image: null,
    scale: 1,
    scaleByTarget: {},
    drawing: null,
    selected: -1,
    currentResults: [],
    unresolvedCount: 0,
    progressSource: null,
  };

  function setStatus(step, text) {
    const box = $('status-box');
    if (!box) return;
    $('status-step').textContent = step || '';
    $('status-text').textContent = text || '';
    box.style.display = (step || text) ? 'block' : 'none';
  }

  function showError(step, details) {
    const box = $('error-box');
    if (!box) return;
    const lines = [];
    if (step) lines.push('[' + step + ']');
    if (details) lines.push(details);
    $('error-text').textContent = lines.join('\n');
    box.style.display = 'block';
  }

  function clearError() {
    const box = $('error-box');
    if (!box) return;
    box.style.display = 'none';
    $('error-text').textContent = '';
  }

  function zones() { return state.zonesByTarget[state.currentTarget] || []; }
  function ensureTarget(targetId) { state.zonesByTarget[targetId] = state.zonesByTarget[targetId] || []; }

  $('btn-parse').addEventListener('click', parseZip);
  $('btn-check').addEventListener('click', startCheck);
  $('btn-clear-error').addEventListener('click', clearError);
  $('hide-pass').addEventListener('change', () => {
    if (!state.sessionId) return;
    renderResultsTable();
    updateErrorList();
    updateFinishButtons();
  });
  $('btn-finish-top').addEventListener('click', finishReview);
  $('btn-finish-bottom').addEventListener('click', finishReview);
  $('btn-expand-results').addEventListener('click', expandResults);

  async function parseZip() {
    clearError();
    const f = $('zip-input').files[0];

    if (!f) {
      setStatus('Parse ZIP', 'FAILED');
      showError('parseZip', 'ZIP file is not selected');
      return;
    }

    setStatus('Parse ZIP', 'Uploading...');

    const fd = new FormData();
    fd.append('zip_file', f);
    if ($('section-number').value) fd.append('section_number', $('section-number').value);
    if ($('section-name').value) fd.append('section_name', $('section-name').value);

    try {
      const resp = await fetch('/api/phase2/manifest', { method: 'POST', body: fd });
      const text = await resp.text();

      if (!resp.ok) {
        setStatus('Parse ZIP', 'FAILED');
        showError('parseZip', 'HTTP ' + resp.status + '\n' + text.slice(0, 1000));
        return;
      }

      const data = JSON.parse(text);
      state.uploadId = data.upload_id;
      state.targets = data.targets || [];
      renderTargets();
      // После загрузки манифеста зон ещё нет. Держим кнопку «Проверить локализацию»
      // заблокированной пока пользователь не определит хотя бы одну зону для каждого target.
      // Не включать её только по en_available.
      updateCheckButton();
      setStatus('Parse ZIP', 'OK');
    } catch (e) {
      setStatus('Parse ZIP', 'FAILED');
      showError('parseZip', String(e));
    }
  }

  function renderTargets() {
    const el = $('targets-list');
    el.innerHTML = '';
    state.targets.forEach((t) => {
      ensureTarget(t.target_id);
      const row = document.createElement('div');
      row.className = 'zone-row';
      const dataTarget = String(t.target_id || '');
      // Отображаем полный target_id на нескольких строках, разделяя по "/".
      const parts = dataTarget.split('/').filter((p) => p);
      const display = parts.map((p) => esc(p)).join('<br>');
      row.innerHTML = `<button class="btn btn-xs" data-target="${esc(dataTarget)}" title="${esc(dataTarget)}">${display}</button> ${t.en_available ? '' : '<span style="color:#dc2626">en не найден</span>'}`;
      row.querySelector('button').onclick = () => openTarget(t);
      el.appendChild(row);
    });
    if (state.targets.length) openTarget(state.targets[0]);
  }

  // Проверяет, можно ли включать кнопку «Проверить локализацию».
  // Учитываем только runnable-targets (с доступным en preview), иначе кнопку
  // можно заблокировать навсегда на таргете, где зоны физически нельзя разметить.
  function updateCheckButton() {
    const runnableTargets = state.targets.filter((t) => t.en_available);
    if (!runnableTargets.length) {
      $('btn-check').disabled = true;
      return;
    }
    const allHaveZones = runnableTargets.every((t) => {
      const zonesForTarget = state.zonesByTarget[t.target_id] || [];
      return zonesForTarget.length > 0;
    });
    $('btn-check').disabled = !allHaveZones;
  }

  async function openTarget(t) {
    state.currentTarget = t.target_id;
    state.selected = -1;
    // Пересчитываем состояние кнопки при смене target; preview availability не влияет.
    updateCheckButton();
    $('target-msg').textContent = t.en_available ? '' : 'en не найден';
    if (!t.en_available) {
      state.image = null;
      draw();
      renderZones();
      return;
    }
    const img = new Image();
    img.onload = () => {
      state.image = img;
      const fitScale = Math.min(1, 800 / img.naturalWidth, 500 / img.naturalHeight);
      state.scale = state.scaleByTarget[t.target_id] || fitScale;
      canvas.width = Math.round(img.naturalWidth * state.scale);
      canvas.height = Math.round(img.naturalHeight * state.scale);
      draw();
      renderZones();
    };
    // В src используем полный encodeURIComponent(target_id) без split().
    img.src = `/api/phase2/preview/${encodeURIComponent(state.uploadId)}/${encodeURIComponent(t.target_id)}`;
  }

  canvas.addEventListener('mousedown', (e) => {
    if (!state.image) return;
    const p = pos(e);
    state.drawing = { x: p.x, y: p.y };
  });
  canvas.addEventListener('mousemove', (e) => {
    if (!state.drawing) return;
    draw();
    const p = pos(e);
    ctx.strokeStyle = '#10b981';
    ctx.strokeRect(state.drawing.x, state.drawing.y, p.x - state.drawing.x, p.y - state.drawing.y);
  });
  canvas.addEventListener('mouseup', (e) => {
    if (!state.drawing) return;
    const p = pos(e);
    const x1 = Math.min(state.drawing.x, p.x) / state.scale;
    const y1 = Math.min(state.drawing.y, p.y) / state.scale;
    const x2 = Math.max(state.drawing.x, p.x) / state.scale;
    const y2 = Math.max(state.drawing.y, p.y) / state.scale;
    state.drawing = null;
    if (x2 - x1 < 5 || y2 - y1 < 5) return draw();
    zones().push({ name: `zone_${zones().length + 1}`, zone_type: 'text', google_mode: 'TEXT_DETECTION', bbox: [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)] });
    state.selected = zones().length - 1;
    renderZones();
    draw();
  });

  canvas.addEventListener('wheel', (e) => {
    if (!state.image) return;
    const p = pos(e);
    if (p.x < 0 || p.y < 0 || p.x > canvas.width || p.y > canvas.height) return;
    e.preventDefault();
    const zoomStep = e.deltaY < 0 ? 1.1 : 0.9;
    const nextScale = Math.min(6, Math.max(0.1, state.scale * zoomStep));
    if (nextScale === state.scale) return;
    state.scale = nextScale;
    state.scaleByTarget[state.currentTarget] = nextScale;
    canvas.width = Math.round(state.image.naturalWidth * state.scale);
    canvas.height = Math.round(state.image.naturalHeight * state.scale);
    draw();
  }, { passive: false });

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (state.image) ctx.drawImage(state.image, 0, 0, canvas.width, canvas.height);
    zones().forEach((z, i) => {
      const [x1, y1, x2, y2] = z.bbox.map((v) => Math.round(v * state.scale));
      ctx.strokeStyle = i === state.selected ? '#f59e0b' : '#2563eb';
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    });
  }

  function renderZones() {
    const zl = $('zones-list');
    zl.innerHTML = '';
    zones().forEach((z, i) => {
      const row = document.createElement('div');
      row.className = 'zone-row';
      row.innerHTML = `<button class='btn btn-xs'>${esc(z.name)}</button> ${esc(z.zone_type)}`;
      row.querySelector('button').onclick = () => { state.selected = i; syncForm(); draw(); };
      zl.appendChild(row);
    });
    syncForm();
    // После создания или удаления зон обновляем состояние кнопки.
    updateCheckButton();
  }

  function syncForm() {
    const z = zones()[state.selected];
    $('sidebar-form').style.display = z ? 'block' : 'none';
    if (!z) return;
    $('sf-name').value = z.name;
    $('sf-type').value = z.zone_type;
    $('sf-google-mode').value = z.google_mode;
    $('sf-bbox').value = `[${z.bbox.join(', ')}]`;
    $('google-mode-row').style.display = z.zone_type === 'text' ? 'block' : 'none';
  }

  $('sf-name').addEventListener('input', () => {
    const z = zones()[state.selected];
    if (!z) return;
    z.name = $('sf-name').value;
    renderZones();
  });
  $('sf-type').addEventListener('change', () => {
    const z = zones()[state.selected];
    if (!z) return;
    z.zone_type = $('sf-type').value;
    syncForm();
    renderZones();
  });
  $('sf-google-mode').addEventListener('change', () => {
    const z = zones()[state.selected];
    if (!z) return;
    z.google_mode = $('sf-google-mode').value;
  });
  $('btn-delete-zone').addEventListener('click', () => {
    if (state.selected < 0) return;
    zones().splice(state.selected, 1);
    state.selected = -1;
    renderZones();
    draw();
  });

  async function startCheck() {
    clearError();

    if (!state.uploadId) {
      showError('startCheck', 'No uploadId. Press Загрузить архив first.');
      return;
    }

    const active = state.targets.find((t) => t.target_id === state.currentTarget);
    if (active && !active.en_available) {
      setStatus('Run Check', 'FAILED');
      showError('startCheck', 'en не найден');
      return;
    }

    setStatus('Run Check', 'Starting...');
    $('editor-canvas').style.pointerEvents = 'none';
    $('editor-canvas').style.opacity = '0.75';
    $('progress-block').style.display = 'block';
    document.querySelector('.editor-layout').style.display = 'none';
    $('results-section').style.display = 'none';
    $('final-errors').style.display = 'none';
    setProgress(0, 0);

    try {
      const resp = await fetch(`/api/phase2/run/${encodeURIComponent(state.uploadId)}`, { method: 'POST' });
      const text = await resp.text();

      if (!resp.ok) {
        setStatus('Run Check', 'FAILED');
        showError('startCheck', 'HTTP ' + resp.status + '\n' + text.slice(0, 1000));
        return;
      }

      const data = JSON.parse(text);
      state.sessionId = data.session_id;
      setStatus('Run Check', 'Session started. Waiting for SSE...');
      subscribeSSE();
    } catch (e) {
      setStatus('Run Check', 'FAILED');
      showError('startCheck', String(e));
    }
  }

  function subscribeSSE() {
    if (state.progressSource) {
      state.progressSource.close();
    }
    const es = new EventSource(`/api/progress/${state.sessionId}`);
    state.progressSource = es;

    es.onerror = function () {
      showError('SSE', 'EventSource connection error.');
    };

    es.onmessage = async (ev) => {
      const m = JSON.parse(ev.data);
      if (m.event === 'start') {
        setProgress(0, Number(m.total || 0));
      }
      if (m.event === 'item') {
        const total = Number($('progress-count').dataset.total || 0);
        setProgress(Number(m.idx || 0) + 1, total);
      }
      if (m.event === 'done') {
        es.close();
        state.progressSource = null;
        await loadResults();
        $('progress-block').style.display = 'none';
        $('results-section').style.display = 'block';
      }
    };
  }

  function setProgress(done, total) {
    const safeTotal = total > 0 ? total : 0;
    const safeDone = done > safeTotal ? safeTotal : done;
    $('progress-count').textContent = `${safeDone} / ${safeTotal}`;
    $('progress-count').dataset.total = String(safeTotal);
    const pct = safeTotal ? Math.round((safeDone / safeTotal) * 100) : 0;
    $('progress-percent').textContent = `${pct}%`;
    $('progress-bar').style.width = `${pct}%`;
  }

  function aggregateEngineText(row, engine) {
    const data = (row.ocr_results || {})[engine];
    if (!data) return '';
    if (typeof data.text === 'string') return data.text;
    const zones = Array.isArray(data.zones) ? data.zones.slice() : [];
    zones.sort((a, b) => Number(a.zone_index || 0) - Number(b.zone_index || 0));
    return zones.map((z) => z.text || '').join('');
  }

  async function loadResults() {
    clearError();
    setStatus('Results', 'loading...');

    try {
      const url = `/api/results/${state.sessionId}?per_page=200`;
      const resp = await fetch(url);
      const text = await resp.text();

      if (!resp.ok) {
        setStatus('Results', 'FAILED');
        showError('loadResults', 'HTTP ' + resp.status + '\n' + text.slice(0, 1000));
        return;
      }

      const data = JSON.parse(text);
      state.currentResults = data.results || [];
      renderResultsTable();
      updateErrorList();
      setStatus('Results', 'OK');
    } catch (e) {
      setStatus('Results', 'FAILED');
      showError('loadResults', String(e));
    }
  }

  function renderResultsTable() {
    const tbody = $('results-body');
    tbody.innerHTML = '';
    const hidePass = $('hide-pass').checked;
    const visibleRows = hidePass ? state.currentResults.filter((r) => r.status !== 'PASS') : state.currentResults;

    for (const row of visibleRows) {
      const tr = document.createElement('tr');
      const lang = (row.lang || '').toLowerCase();
      const rtl = isRtl(lang) ? 'rtl' : 'ltr';
      const st = row.status === 'PASS' ? 'PASS' : 'MANUAL';
      const imgUrl = `/image/${encodeURIComponent(state.sessionId)}/${encodeURIComponent(row.image_name || '')}`;
      tr.innerHTML = `
        <td><a href="${imgUrl}" target="_blank" rel="noopener"><img class="result-thumb" src="${imgUrl}" alt="${esc(row.image_name || '')}"></a></td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'google'))}</td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'azure'))}</td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'ocrspace'))}</td>
        <td dir='${rtl}'>${esc(row.ref_text || '')}</td>
        <td><span class="status-badge ${st === 'PASS' ? 'status-pass' : 'status-manual'}">${st}</span></td>
        <td>${reviewHtml(row, st)}</td>`;
      tbody.appendChild(tr);
    }

    state.unresolvedCount = state.currentResults.filter((r) => r.status !== 'PASS' && !r.manual_decision).length;
    bindReviewButtons();
    updateFinishButtons();
  }

  function reviewHtml(row, st) {
    if (st !== 'MANUAL') return row.manual_decision || '';
    const okClass = row.manual_decision === 'ok' ? 'btn-primary' : 'btn-secondary';
    const errClass = row.manual_decision === 'error' ? 'btn-primary' : 'btn-secondary';
    return `<div class="review-actions"><button class='btn btn-xs ${okClass}' data-id='${row.id}' data-d='ok'>OK</button><button class='btn btn-xs ${errClass}' data-id='${row.id}' data-d='error'>ERROR</button></div>`;
  }

  function bindReviewButtons() {
    document.querySelectorAll('[data-id][data-d]').forEach((b) => {
      b.onclick = async () => {
        const fd = new FormData();
        fd.append('decision', b.dataset.d);
        await fetch(`/api/decide/${b.dataset.id}`, { method: 'POST', body: fd });
        await loadResults();
      };
    });
  }

  function updateErrorList() {
    const errorPaths = state.currentResults
      .filter((r) => r.status !== 'PASS' && r.manual_decision === 'error')
      .map((r) => r.image_name || '');
    $('error-paths').textContent = errorPaths.join('\n');
  }

  function updateFinishButtons() {
    $('btn-finish-top').disabled = false;
    $('btn-finish-bottom').disabled = false;
  }

  function finishReview() {
    if (state.progressSource) {
      state.progressSource.close();
      state.progressSource = null;
    }
    state.uploadId = null;
    state.sessionId = null;
    state.currentResults = [];
    state.targets = [];
    state.currentTarget = null;
    state.unresolvedCount = 0;
    state.image = null;
    state.selected = -1;
    state.drawing = null;
    state.scale = 1;
    state.zonesByTarget = {};

    $('results-body').innerHTML = '';
    $('results-section').style.display = 'none';
    $('final-errors').style.display = 'none';
    $('progress-block').style.display = 'none';
    $('error-paths').textContent = '';
    $('targets-list').innerHTML = '';
    $('zones-list').innerHTML = '';
    $('target-msg').textContent = '';
    $('sidebar-form').style.display = 'none';
    document.querySelector('.editor-layout').style.display = '';
    $('btn-check').disabled = true;
    $('btn-finish-top').disabled = false;
    $('btn-finish-bottom').disabled = false;
    $('editor-canvas').style.pointerEvents = 'auto';
    $('editor-canvas').style.opacity = '1';
    setProgress(0, 0);
    draw();
  }

  function expandResults() {
    $('final-errors').style.display = 'none';
    $('results-section').style.display = 'block';
  }

  function pos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function isRtl(lang) {
    const b = lang.split('-')[0];
    return b === 'ar' || b === 'he' || b === 'il';
  }
  function esc(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
})();
