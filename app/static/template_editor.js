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
    currentSessionMeta: null,
    unresolvedCount: 0,
    page: 1,
    progressSource: null,
    modalScale: 1,
    modalBaseScale: 1,
    modalNaturalWidth: 0,
    modalNaturalHeight: 0,
    zipFilename: '',
    templatesLoaded: false,
    matchedTemplateName: '',
    autoApplyMatchedTemplate: false,
    zonesBeforeAutoApply: null,
    autoAppliedMatchedTemplate: '',
    autoApplyLockedByManualChanges: false,
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
  $('template-select').addEventListener('change', onTemplateSelect);
  const autoApplyCheckbox = $('template-auto-apply');
  if (autoApplyCheckbox) autoApplyCheckbox.addEventListener('change', onAutoApplyToggle);
  $('btn-clear-error').addEventListener('click', clearError);
  $('hide-pass').addEventListener('change', () => {
    if (!state.sessionId) return;
    state.page = 1;
    renderResultsTable();
    updateErrorList();
    updateFinishButtons();
  });
  $('btn-finish-top').addEventListener('click', finishReview);
  $('btn-finish-bottom').addEventListener('click', showDesignerIssues);
  $('btn-expand-results').addEventListener('click', expandResults);
  $('image-modal').addEventListener('click', (e) => {
    if (e.target.id === 'image-modal') closeImageModal();
  });
  $('image-modal-stage').addEventListener('click', (e) => {
    if (e.target.id === 'image-modal-stage') closeImageModal();
  });
  $('image-modal-img').addEventListener('wheel', handleModalZoom, { passive: false });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeImageModal();
  });

  loadEngineUsageWidget();

  async function loadEngineUsageWidget() {
    const monthEl = $('engine-usage-month');
    if (!monthEl) return;
    try {
      const resp = await fetch('/api/metrics/engine-usage/current_month');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      monthEl.textContent = data.month_label || 'Current month';
      const available = Boolean(data.available);
      const fallback = available ? '0' : 'n/a';
      $('engine-usage-google').textContent = available && data.google_requests != null ? String(data.google_requests) : fallback;
      $('engine-usage-azure').textContent = available && data.azure_requests != null ? String(data.azure_requests) : fallback;
      $('engine-usage-ocrspace').textContent = available && data.ocrspace_requests != null ? String(data.ocrspace_requests) : fallback;
    } catch (_) {
      monthEl.textContent = 'Current month';
      $('engine-usage-google').textContent = 'n/a';
      $('engine-usage-azure').textContent = 'n/a';
      $('engine-usage-ocrspace').textContent = 'n/a';
    }
  }

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
      state.zipFilename = (f && f.name) ? String(f.name) : '';
      state.targets = data.targets || [];
      renderTargets();
      await loadSavedTemplates();
      await autoSelectMatchingTemplate();
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

  function _phase2Meta(targetId, zoneName) {
    return JSON.stringify({ phase2_target_id: targetId, phase2_zone_name: zoneName });
  }

  function _parsePhase2Meta(notes) {
    if (!notes || typeof notes !== 'string') return null;
    try {
      const meta = JSON.parse(notes);
      if (!meta || typeof meta !== 'object') return null;
      if (!meta.phase2_target_id || !meta.phase2_zone_name) return null;
      return { targetId: String(meta.phase2_target_id), zoneName: String(meta.phase2_zone_name) };
    } catch (_) {
      return null;
    }
  }

  function editorZonesFromTemplate(template) {
    const byTarget = {};
    const parsedZones = Array.isArray(template?.zones) ? template.zones : [];

    parsedZones.forEach((z) => {
      const meta = _parsePhase2Meta(z.notes);
      if (!meta) return;
      byTarget[meta.targetId] = byTarget[meta.targetId] || [];
      byTarget[meta.targetId].push({
        name: meta.zoneName,
        zone_type: z.type === 'logo' ? 'logo' : 'text',
        google_mode: z.engine_config && z.engine_config.google_mode ? z.engine_config.google_mode : 'TEXT_DETECTION',
        bbox: Array.isArray(z.bbox) ? z.bbox.slice() : [0, 0, 1, 1],
      });
    });

    if (Object.keys(byTarget).length > 0) {
      return byTarget;
    }

    const fallbackZones = parsedZones.map((z) => ({
      name: z.name,
      zone_type: z.type === 'logo' ? 'logo' : 'text',
      google_mode: z.engine_config && z.engine_config.google_mode ? z.engine_config.google_mode : 'TEXT_DETECTION',
      bbox: Array.isArray(z.bbox) ? z.bbox.slice() : [0, 0, 1, 1],
    }));
    state.targets.forEach((t) => {
      byTarget[t.target_id] = fallbackZones.map((z) => ({ ...z, bbox: z.bbox.slice() }));
    });
    return byTarget;
  }

  function buildTemplatePayload(templateName) {
    const sourceWidth = state.image ? state.image.naturalWidth : Math.max(1, Math.round(canvas.width / (state.scale || 1)));
    const sourceHeight = state.image ? state.image.naturalHeight : Math.max(1, Math.round(canvas.height / (state.scale || 1)));
    const zones = [];
    Object.entries(state.zonesByTarget).forEach(([targetId, targetZones]) => {
      (targetZones || []).forEach((z) => {
        zones.push({
          name: `${targetId}::${z.name}`,
          type: z.zone_type === 'logo' ? 'logo' : 'ocr',
          bbox: z.bbox,
          engines: z.zone_type === 'logo' ? [] : ['google'],
          engine_config: z.zone_type === 'text' ? { google_mode: z.google_mode || 'TEXT_DETECTION' } : {},
          notes: _phase2Meta(targetId, z.name),
        });
      });
    });
    return {
      template_name: templateName,
      schema_version: 1,
      source_size: [Math.max(1, sourceWidth), Math.max(1, sourceHeight)],
      zones,
      expected_texts: {},
    };
  }

  async function saveCurrentTemplate() {
    const templateName = state.zipFilename;
    if (!templateName) return;
    const payload = buildTemplatePayload(templateName);
    let resp = await fetch('/api/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.status === 409) {
      resp = await fetch(`/api/templates/${encodeURIComponent(templateName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error('Template save failed: HTTP ' + resp.status + ' ' + text.slice(0, 250));
    }
  }

  async function loadSavedTemplates() {
    const resp = await fetch('/api/templates');
    if (!resp.ok) return;
    const data = await resp.json();
    const list = Array.isArray(data.templates) ? data.templates : [];
    const sel = $('template-select');
    sel.innerHTML = '<option value="">— выбрать сохранённый шаблон —</option>';
    list.forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    state.templatesLoaded = true;
    const autoApply = $('template-auto-apply');
    if (autoApply) autoApply.checked = false;
    showAutoApplyNote('');
    showTemplateMatch(false);
  }

  function showTemplateMatch(show) {
    $('template-match').style.display = show ? 'inline-flex' : 'none';
    $('template-auto-apply-wrap').style.display = show ? 'inline-flex' : 'none';
    if (!show) showAutoApplyNote('');
  }

  function deepCopyZonesByTarget(zonesByTarget) {
    return JSON.parse(JSON.stringify(zonesByTarget || {}));
  }

  function showAutoApplyNote(text) {
    const note = $('template-auto-apply-note');
    if (!note) return;
    note.textContent = text || '';
    note.style.display = text ? 'inline-flex' : 'none';
  }

  function markManualZoneChange() {
    if (!state.autoAppliedMatchedTemplate) return;
    state.autoApplyLockedByManualChanges = true;
    showAutoApplyNote('Автошаблон уже изменён вручную; отключение недоступно.');
  }

  function clearTemplateZones() {
    Object.keys(state.zonesByTarget).forEach((targetId) => {
      state.zonesByTarget[targetId] = [];
    });
    renderZones();
    draw();
    updateCheckButton();
  }

  function onAutoApplyToggle() {
    const checkbox = $('template-auto-apply');
    if (!checkbox) return;
    const checked = checkbox.checked;
    state.autoApplyMatchedTemplate = checked;
    if (!state.matchedTemplateName) return;

    if (!checked) {
      if (state.autoApplyLockedByManualChanges) {
        checkbox.checked = true;
        state.autoApplyMatchedTemplate = true;
        showAutoApplyNote('Сначала уберите ручные изменения или загрузите архив заново.');
        return;
      }
      if (state.autoAppliedMatchedTemplate === state.matchedTemplateName && state.zonesBeforeAutoApply) {
        state.zonesByTarget = deepCopyZonesByTarget(state.zonesBeforeAutoApply);
        $('template-select').value = '';
        state.autoAppliedMatchedTemplate = '';
        renderZones();
        draw();
        updateCheckButton();
      }
      showAutoApplyNote('');
      return;
    }

    showAutoApplyNote('');
    $('template-select').value = state.matchedTemplateName;
    onTemplateSelect();
  }

  async function applyTemplateByName(name) {
    if (!name) return;
    const resp = await fetch(`/api/templates/${encodeURIComponent(name)}`);
    if (!resp.ok) return;
    const tmpl = await resp.json();
    const byTarget = editorZonesFromTemplate(tmpl);
    Object.keys(state.zonesByTarget).forEach((targetId) => {
      state.zonesByTarget[targetId] = [];
    });
    state.targets.forEach((t) => {
      const targetZones = byTarget[t.target_id] || [];
      state.zonesByTarget[t.target_id] = targetZones.map((z) => ({
        name: z.name,
        zone_type: z.zone_type === 'logo' ? 'logo' : 'text',
        google_mode: z.google_mode || 'TEXT_DETECTION',
        bbox: Array.isArray(z.bbox) ? z.bbox.slice() : [0, 0, 1, 1],
      }));
    });
    renderZones();
    draw();
  }

  async function onTemplateSelect() {
    const name = $('template-select').value || '';
    showTemplateMatch(Boolean(state.matchedTemplateName));
    if (!name) {
      updateCheckButton();
      return;
    }
    await applyTemplateByName(name);
    if (name !== state.matchedTemplateName) {
      state.autoAppliedMatchedTemplate = '';
      state.zonesBeforeAutoApply = null;
      state.autoApplyLockedByManualChanges = false;
      showAutoApplyNote('');
    }
    updateCheckButton();
  }

  async function autoSelectMatchingTemplate() {
    const sel = $('template-select');
    const autoApply = $('template-auto-apply');
    state.matchedTemplateName = '';
    state.autoApplyMatchedTemplate = false;
    state.zonesBeforeAutoApply = null;
    state.autoAppliedMatchedTemplate = '';
    state.autoApplyLockedByManualChanges = false;
    if (autoApply) autoApply.checked = false;

    if (!sel || !state.zipFilename) {
      showTemplateMatch(false);
      return;
    }

    const match = Array.from(sel.options).find((o) => o.value === state.zipFilename);
    if (!match) {
      sel.value = '';
      showTemplateMatch(false);
      updateCheckButton();
      return;
    }

    state.matchedTemplateName = state.zipFilename;
    state.autoApplyMatchedTemplate = true;
    if (autoApply) autoApply.checked = true;
    showTemplateMatch(true);
    showAutoApplyNote('');

    if (state.autoApplyMatchedTemplate) {
      state.zonesBeforeAutoApply = deepCopyZonesByTarget(state.zonesByTarget);
      sel.value = state.matchedTemplateName;
      await onTemplateSelect();
      state.autoAppliedMatchedTemplate = state.matchedTemplateName;
      return;
    }

    sel.value = '';
    updateCheckButton();
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
    markManualZoneChange();
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
    markManualZoneChange();
    renderZones();
  });
  $('sf-type').addEventListener('change', () => {
    const z = zones()[state.selected];
    if (!z) return;
    z.zone_type = $('sf-type').value;
    markManualZoneChange();
    syncForm();
    renderZones();
  });
  $('sf-google-mode').addEventListener('change', () => {
    const z = zones()[state.selected];
    if (!z) return;
    z.google_mode = $('sf-google-mode').value;
    markManualZoneChange();
  });
  $('btn-delete-zone').addEventListener('click', () => {
    if (state.selected < 0) return;
    zones().splice(state.selected, 1);
    markManualZoneChange();
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

    try {
      await saveCurrentTemplate();
    } catch (e) {
      setStatus('Run Check', 'FAILED');
      showError('startCheck', String(e));
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
        const loaded = await loadResults();
        if (loaded) {
          setStatus('', '');
        }
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

    if (!state.sessionId) {
      setStatus('Results', 'FAILED');
      showError('loadResults', 'No session_id. Start check first.');
      return false;
    }

    try {
      const url = `/api/results/${state.sessionId}?per_page=200`;
      const resp = await fetch(url);
      const text = await resp.text();

      if (!resp.ok) {
        showError('loadResults', 'HTTP ' + resp.status + '\n' + text.slice(0, 1000));
        return false;
      }

      const data = JSON.parse(text);
      if (data && data.session && data.session.session_id) {
        state.sessionId = data.session.session_id;
      }
      state.currentResults = data.results || [];
      state.currentSessionMeta = data.session || null;
      renderResultsTable();
      updateErrorList();
      return true;
    } catch (e) {
      showError('loadResults', String(e));
      return false;
    }
  }

  function renderResultsTable() {
    const tbody = $('results-body');
    const pagination = $('results-pagination');
    tbody.innerHTML = '';
    pagination.innerHTML = '';
    const hidePass = $('hide-pass').checked;
    const visibleRows = hidePass ? state.currentResults.filter((r) => r.status !== 'PASS') : state.currentResults;
    const orderedRows = visibleRows
      .map((row, originalIndex) => ({ row, originalIndex }))
      .sort((a, b) => {
        const aEn = isEnglishLang(a.row.lang) ? 1 : 0;
        const bEn = isEnglishLang(b.row.lang) ? 1 : 0;
        if (aEn !== bEn) return bEn - aEn;
        return a.originalIndex - b.originalIndex;
      });

    const perPage = 20;
    const totalPages = Math.max(1, Math.ceil(orderedRows.length / perPage));
    const currentPage = Math.min(Math.max(1, state.page || 1), totalPages);
    state.page = currentPage;
    const start = (currentPage - 1) * perPage;
    const pagedRows = orderedRows.slice(start, start + perPage);

    pagedRows.forEach(({ row }, rowIndex) => {
      const tr = document.createElement('tr');
      const lang = (row.lang || '').toLowerCase();
      const rtl = isRtl(lang) ? 'rtl' : 'ltr';
      const st = row.status === 'PASS' ? 'PASS' : 'MANUAL';
      const statusReason = getStatusReason(row, st);
      const imgUrl = `/image/${encodeURIComponent(state.sessionId)}/${encodeURIComponent(row.image_name || '')}`;
      tr.innerHTML = `
        <td><img class="result-thumb js-modal-thumb" src="${imgUrl}" alt="${esc(row.image_name || '')}" data-full="${imgUrl}"></td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'google'))}${renderConfidence(row, 'google')}</td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'azure'))}${renderConfidence(row, 'azure')}</td>
        <td dir='${rtl}'>${esc(aggregateEngineText(row, 'ocrspace'))}${renderConfidence(row, 'ocrspace')}</td>
        <td dir='${rtl}'>${esc(row.ref_text || '')}${renderReferenceConfidence(row, start + rowIndex)}</td>
        <td class="status-cell" data-tooltip="${esc(statusReason)}"><span class="status-badge ${st === 'PASS' ? 'status-pass' : 'status-manual'}">${st}</span></td>
        <td>${reviewHtml(row, st)}</td>`;
      tbody.appendChild(tr);
    });

    renderResultsPagination(currentPage, totalPages);

    state.unresolvedCount = state.currentResults.filter((r) => r.status !== 'PASS' && !r.manual_decision).length;
    bindReviewButtons();
    bindImageThumbnails();
    updateFinishButtons();
  }

  function renderResultsPagination(current, total) {
    const pagination = $('results-pagination');
    pagination.innerHTML = '';
    if (total <= 1) return;

    function makeBtn(label, page, disabled, active) {
      const btn = document.createElement('button');
      btn.className = 'page-btn' + (active ? ' active' : '');
      btn.textContent = label;
      btn.disabled = disabled;
      if (!disabled) {
        btn.addEventListener('click', () => {
          state.page = page;
          renderResultsTable();
        });
      }
      return btn;
    }

    pagination.appendChild(makeBtn('«', 1, current === 1, false));
    pagination.appendChild(makeBtn('‹', current - 1, current === 1, false));
    for (let page = 1; page <= total; page++) {
      pagination.appendChild(makeBtn(String(page), page, false, page === current));
    }
    pagination.appendChild(makeBtn('›', current + 1, current === total, false));
    pagination.appendChild(makeBtn('»', total, current === total, false));
  }

  function renderConfidence(row, engine) {
    if (engine === 'ocrspace') return '';
    const conf = extractConfidence(row, engine);
    return `<span class="engine-confidence" dir="ltr">confidence: ${conf === null ? '—' : conf.toFixed(2)}</span>`;
  }

  function renderReferenceConfidence(row, rowIndex) {
    if (rowIndex !== 0) return '';
    const referenceBlock = row.reference || {};
    const candidates = [row.ref_confidence, row.reference_confidence, referenceBlock.confidence];
    const val = pickNumber(candidates);

    const s1 = pickNumber([referenceBlock.score_top1, row.reference_score_top1]);
    const s2 = pickNumber([referenceBlock.score_top2, row.reference_score_top2]);
    const margin = pickNumber([referenceBlock.margin, row.reference_margin, (s1 !== null && s2 !== null ? s1 - s2 : null)]);

    const conf = val === null ? 0 : val;
    let band = 'LOW';
    if (conf >= 0.8) band = 'HIGH';
    else if (conf >= 0.5) band = 'MEDIUM';

    const tooltip = `confidence=${conf.toFixed(2)}
score_top1=${s1 === null ? 'none' : s1.toFixed(2)}
score_top2=${s2 === null ? 'none' : s2.toFixed(2)}
margin=${(margin === null ? 0 : margin).toFixed(2)}`;

    return `<span class="ref-confidence-badge ref-${band.toLowerCase()}" data-tooltip="${esc(tooltip)}">${band}</span>`;
  }

  function isEnglishLang(lang) {
    const norm = String(lang || '').toLowerCase().replace(/_/g, '-');
    return norm.startsWith('en');
  }

  function extractConfidence(row, engine) {
    const data = (row.ocr_results || {})[engine] || {};
    const zones = Array.isArray(data.zones) ? data.zones : [];
    if (typeof data.confidence === 'number') return data.confidence;
    if (!zones.length) return null;
    const nums = zones.map((z) => (typeof z.confidence === 'number' ? z.confidence : null)).filter((v) => v !== null);
    if (!nums.length) return null;
    return nums.reduce((a, b) => a + b, 0) / nums.length;
  }

  function pickNumber(values) {
    for (const val of values) {
      if (typeof val === 'number') return val;
    }
    return null;
  }



  function getStatusReason(row, status) {
    const validationReason = row.validation?.reason || row.validation_reason || '';
    const consensusReason = row.consensus?.reason || row.consensus_reason || row.reason || '';
    if (status === 'PASS') return consensusReason || 'All engines matched';
    return validationReason || consensusReason || 'Manual review required';
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
        const resp = await fetch(`/api/decide/${b.dataset.id}`, { method: 'POST', body: fd });
        if (!resp.ok) {
          const text = await resp.text();
          showError('decide', 'HTTP ' + resp.status + '\n' + text.slice(0, 1000));
          return;
        }
        const data = await resp.json();
        if (data && data.session_id) {
          state.sessionId = data.session_id;
        }
        const row = state.currentResults.find((r) => String(r.id) === String(b.dataset.id));
        if (row) {
          row.manual_decision = b.dataset.d;
          updateFinishButtons();
        }
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
    const designerIssuesBtn = $('btn-finish-bottom');
    const allFinalized = state.currentResults.every((r) => {
      if (r.status === 'PASS') return true;
      return r.manual_decision === 'ok' || r.manual_decision === 'error';
    });
    designerIssuesBtn.disabled = !allFinalized;
  }

  function showDesignerIssues() {
    const errorPaths = state.currentResults
      .filter((r) => r.manual_decision === 'error')
      .map((r) => r.image_name || '')
      .filter(Boolean);

    $('error-paths').textContent = errorPaths.length ? errorPaths.join('\n') : 'Косяков нет';
    $('results-section').style.display = 'none';
    $('final-errors').style.display = 'block';
  }

  function finishReview() {
    if (state.progressSource) {
      state.progressSource.close();
      state.progressSource = null;
    }
    state.uploadId = null;
    state.sessionId = null;
    state.currentResults = [];
    state.currentSessionMeta = null;
    state.targets = [];
    state.currentTarget = null;
    state.unresolvedCount = 0;
    state.page = 1;
    state.image = null;
    state.selected = -1;
    state.drawing = null;
    state.scale = 1;
    state.zonesByTarget = {};
    state.matchedTemplateName = '';
    state.autoApplyMatchedTemplate = false;
    state.zonesBeforeAutoApply = null;
    state.autoAppliedMatchedTemplate = '';
    state.autoApplyLockedByManualChanges = false;

    $('results-body').innerHTML = '';
    $('results-section').style.display = 'none';
    $('final-errors').style.display = 'none';
    $('progress-block').style.display = 'none';
    $('error-paths').textContent = '';
    $('targets-list').innerHTML = '';
    $('zones-list').innerHTML = '';
    $('target-msg').textContent = '';
    $('sidebar-form').style.display = 'none';
    $('template-select').value = '';
    const autoApply = $('template-auto-apply');
    if (autoApply) autoApply.checked = false;
    showAutoApplyNote('');
    showTemplateMatch(false);
    document.querySelector('.editor-layout').style.display = '';
    $('btn-check').disabled = true;
    $('btn-finish-top').disabled = false;
    $('btn-finish-bottom').disabled = true;
    $('editor-canvas').style.pointerEvents = 'auto';
    $('editor-canvas').style.opacity = '1';
    setProgress(0, 0);
    draw();
  }

  function expandResults() {
    $('final-errors').style.display = 'none';
    $('results-section').style.display = 'block';
  }

  function bindImageThumbnails() {
    document.querySelectorAll('.js-modal-thumb').forEach((img) => {
      img.onclick = () => openImageModal(img.dataset.full);
    });
  }

  function openImageModal(src) {
    if (!src) return;
    state.modalScale = 1;
    state.modalBaseScale = 1;
    state.modalNaturalWidth = 0;
    state.modalNaturalHeight = 0;
    const img = $('image-modal-img');
    img.src = src;
    img.style.transform = 'scale(1)';
    img.style.transformOrigin = 'top left';
    img.style.width = 'auto';
    img.style.height = 'auto';
    img.onload = () => {
      state.modalNaturalWidth = img.naturalWidth;
      state.modalNaturalHeight = img.naturalHeight;
      img.style.width = state.modalNaturalWidth + 'px';
      img.style.height = state.modalNaturalHeight + 'px';
      const stage = $('image-modal-stage');
      const fitScale = Math.min(1, stage.clientWidth / state.modalNaturalWidth, stage.clientHeight / state.modalNaturalHeight);
      state.modalBaseScale = fitScale;
      state.modalScale = fitScale;
      img.style.transformOrigin = 'top left';
      img.style.transform = `scale(${fitScale})`;
      stage.scrollLeft = 0;
      stage.scrollTop = 0;
    };
    $('image-modal').style.display = 'block';
    document.body.style.overflow = 'hidden';
  }

  function closeImageModal() {
    const modal = $('image-modal');
    if (!modal || modal.style.display === 'none') return;
    modal.style.display = 'none';
    const img = $('image-modal-img');
    img.src = '';
    img.style.transform = 'scale(1)';
    state.modalScale = 1;
    state.modalBaseScale = 1;
    state.modalNaturalWidth = 0;
    state.modalNaturalHeight = 0;
    document.body.style.overflow = '';
  }

  function handleModalZoom(e) {
    const img = $('image-modal-img');
    if (!img || !img.src) return;
    const rect = img.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) return;
    e.preventDefault();
    const stage = $('image-modal-stage');
    const prevScale = state.modalScale;
    const nextScale = Math.max(0.2, Math.min(5, prevScale * (e.deltaY < 0 ? 1.1 : 0.9)));
    if (nextScale === prevScale) return;

    const offsetX = e.clientX - rect.left;
    const offsetY = e.clientY - rect.top;
    const ratioX = offsetX / rect.width;
    const ratioY = offsetY / rect.height;

    state.modalScale = nextScale;
    img.style.transformOrigin = `${ratioX * 100}% ${ratioY * 100}%`;
    img.style.transform = `scale(${nextScale})`;
    img.style.cursor = nextScale > 1 ? 'zoom-out' : 'zoom-in';

    stage.scrollLeft += (rect.width * (nextScale / prevScale - 1)) * ratioX;
    stage.scrollTop += (rect.height * (nextScale / prevScale - 1)) * ratioY;
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
