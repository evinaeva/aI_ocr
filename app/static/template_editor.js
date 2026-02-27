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
    drawing: null,
    selected: -1,
  };

  function setStatus(step, text) {
    const box = document.getElementById('status-box');
    if (!box) return;
    document.getElementById('status-step').textContent = step || '';
    document.getElementById('status-text').textContent = text || '';
    box.style.display = (step || text) ? 'block' : 'none';
  }

  function showError(step, details) {
    const box = document.getElementById('error-box');
    if (!box) return;
    const lines = [];
    if (step) lines.push("[" + step + "]");
    if (details) lines.push(details);
    document.getElementById('error-text').textContent = lines.join('\n');
    box.style.display = 'block';
  }

  function clearError() {
    const box = document.getElementById('error-box');
    if (!box) return;
    box.style.display = 'none';
    document.getElementById('error-text').textContent = '';
  }

  function zones() { return state.zonesByTarget[state.currentTarget] || []; }
  function ensureTarget(targetId) { state.zonesByTarget[targetId] = state.zonesByTarget[targetId] || []; }

  $('btn-parse').addEventListener('click', parseZip);
  $('btn-check').addEventListener('click', startCheck);
  $('btn-clear-error').addEventListener('click', clearError);

  async function parseZip() {
    clearError();
    const f = document.getElementById('zip-input').files[0];

    if (!f) {
      setStatus("Parse ZIP", "FAILED");
      showError("parseZip", "ZIP file is not selected");
      return;
    }

    setStatus("Parse ZIP", "Uploading...");

    const fd = new FormData();
    fd.append('zip_file', f);
    if ($('section-number').value) fd.append('section_number', $('section-number').value);
    if ($('section-name').value) fd.append('section_name', $('section-name').value);

    try {
      const resp = await fetch('/api/phase2/manifest', {
        method: 'POST',
        body: fd
      });

      const text = await resp.text();

      if (!resp.ok) {
        setStatus("Parse ZIP", "FAILED");
        showError("parseZip", "HTTP " + resp.status + "\n" + text.slice(0, 1000));
        return;
      }

      const data = JSON.parse(text);

      state.uploadId = data.upload_id;
      state.targets = data.targets || [];
      renderTargets();

      const hasRunnable = state.targets.some(t => t.en_available);
      document.getElementById('btn-check').disabled = !hasRunnable;

      setStatus("Parse ZIP", "OK");
    } catch (e) {
      setStatus("Parse ZIP", "FAILED");
      showError("parseZip", String(e));
    }
  }

  function renderTargets() {
    const el = $('targets-list');
    el.innerHTML = '';
    state.targets.forEach((t) => {
      ensureTarget(t.target_id);
      const row = document.createElement('div');
      row.className = 'zone-row';
      row.innerHTML = `<button class="btn btn-xs" data-target="${esc(t.target_id)}">${esc(t.target_id)}</button> ${t.en_available ? '' : '<span style="color:#dc2626">en не найден</span>'}`;
      row.querySelector('button').onclick = () => openTarget(t);
      el.appendChild(row);
    });
    if (state.targets.length) openTarget(state.targets[0]);
  }

  async function openTarget(t) {
    state.currentTarget = t.target_id;
    state.selected = -1;
    $('target-msg').textContent = t.en_available ? '' : 'en не найден';
    if (!t.en_available) {
      $('btn-check').disabled = true;
      state.image = null;
      draw();
      renderZones();
      return;
    }
    $('btn-check').disabled = false;
    const img = new Image();
    img.onload = () => {
      state.image = img;
      state.scale = Math.min(1, 800 / img.naturalWidth, 500 / img.naturalHeight);
      canvas.width = Math.round(img.naturalWidth * state.scale);
      canvas.height = Math.round(img.naturalHeight * state.scale);
      draw();
      renderZones();
    };
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

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (state.image) ctx.drawImage(state.image, 0, 0, canvas.width, canvas.height);
    zones().forEach((z, i) => {
      const [x1, y1, x2, y2] = z.bbox.map(v => Math.round(v * state.scale));
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

  $('sf-name').addEventListener('input', () => { const z = zones()[state.selected]; if (!z) return; z.name = $('sf-name').value; renderZones(); });
  $('sf-type').addEventListener('change', () => { const z = zones()[state.selected]; if (!z) return; z.zone_type = $('sf-type').value; syncForm(); renderZones(); });
  $('sf-google-mode').addEventListener('change', () => { const z = zones()[state.selected]; if (!z) return; z.google_mode = $('sf-google-mode').value; });
  $('btn-delete-zone').addEventListener('click', () => { if (state.selected < 0) return; zones().splice(state.selected, 1); state.selected = -1; renderZones(); draw(); });

  async function startCheck() {
    clearError();

    if (!state.uploadId) {
      showError("startCheck", "No uploadId. Press Continue/Parse first.");
      return;
    }

    const active = state.targets.find(t => t.target_id === state.currentTarget);
    if (active && !active.en_available) {
      setStatus("Run Check", "FAILED");
      showError("startCheck", "en не найден");
      return;
    }

    setStatus("Run Check", "Starting...");

    try {
      const resp = await fetch(`/api/phase2/run/${encodeURIComponent(state.uploadId)}`, {
        method: 'POST'
      });

      const text = await resp.text();

      if (!resp.ok) {
        setStatus("Run Check", "FAILED");
        showError("startCheck", "HTTP " + resp.status + "\n" + text.slice(0, 1000));
        return;
      }

      const data = JSON.parse(text);

      state.sessionId = data.session_id;
      setStatus("Run Check", "Session started. Waiting for SSE...");
      subscribeSSE();

    } catch (e) {
      setStatus("Run Check", "FAILED");
      showError("startCheck", String(e));
    }
  }

  function subscribeSSE() {
    const es = new EventSource(`/api/progress/${state.sessionId}`);

    es.onerror = function () {
      showError("SSE", "EventSource connection error.");
    };

    es.onmessage = async (ev) => {
      const m = JSON.parse(ev.data);
      if (m.event === 'done') {
        es.close();
        await loadResults();
      }
    };
  }


  function aggregateEngineText(row, engine) {
    const data = (row.ocr_results || {})[engine];
    if (!data) return '';
    if (typeof data.text === 'string') return data.text;
    const zones = Array.isArray(data.zones) ? data.zones.slice() : [];
    zones.sort((a, b) => Number(a.zone_index || 0) - Number(b.zone_index || 0));
    return zones.map(z => z.text || '').join('');
  }

  async function loadResults() {
    clearError();
    setStatus("Results", "loading...");

    try {
      const resp = await fetch(`/api/results/${state.sessionId}?per_page=200`);
      const text = await resp.text();

      if (!resp.ok) {
        setStatus("Results", "FAILED");
        showError("loadResults", "HTTP " + resp.status + "\n" + text.slice(0, 1000));
        return;
      }

      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        setStatus("Results", "FAILED");
        showError("loadResults", "JSON parse error " + String(e) + "\n" + text.slice(0, 1000));
        return;
      }

      $('results-section').style.display = 'block';
      const tbody = $('results-body');
      tbody.innerHTML = '';
      for (const row of (data.results || [])) {
        const tr = document.createElement('tr');
        const lang = (row.lang || '').toLowerCase();
        const rtl = isRtl(lang) ? 'rtl' : 'ltr';
        const st = row.status === 'PASS' ? 'PASS' : 'MANUAL';
        tr.innerHTML = `
          <td>${esc(row.image_name || '')}</td>
          <td dir='${rtl}'>${esc(aggregateEngineText(row, 'google'))}</td>
          <td dir='${rtl}'>${esc(aggregateEngineText(row, 'azure'))}</td>
          <td dir='${rtl}'>${esc(aggregateEngineText(row, 'ocrspace'))}</td>
          <td dir='${rtl}'>${esc(row.ref_text || '')}</td>
          <td>${st}</td>
          <td>${reviewHtml(row, st)}</td>`;
        tbody.appendChild(tr);
      }
      bindReviewButtons();
      setStatus("Results", "OK");
    } catch (e) {
      setStatus("Results", "FAILED");
      showError("loadResults", String(e));
    }
  }

  function reviewHtml(row, st) {
    if (st !== 'MANUAL') return row.manual_decision || '';
    if (row.manual_decision === 'ok') return 'OK';
    if (row.manual_decision === 'error') return 'ERROR';
    return `<button class='btn btn-xs' data-id='${row.id}' data-d='ok'>OK</button> <button class='btn btn-xs' data-id='${row.id}' data-d='error'>ERROR</button>`;
  }

  function bindReviewButtons() {
    document.querySelectorAll('[data-id][data-d]').forEach((b) => {
      b.onclick = async () => {
        const fd = new FormData();
        fd.append('decision', b.dataset.d);
        await fetch(`/api/decide/${b.dataset.id}`, { method: 'POST', body: fd });
        await loadResults();
        await maybeShowErrors();
      };
    });
  }

  async function maybeShowErrors() {
    const resp = await fetch(`/api/results/${state.sessionId}?per_page=200`);
    const data = await resp.json();
    const unresolved = (data.results || []).filter(r => (r.status !== 'PASS') && !r.manual_decision).length;
    if (unresolved > 0) return;
    const p = await fetch(`/api/phase2/error_paths/${state.sessionId}`);
    const d = await p.json();
    $('error-paths').textContent = (d.paths || []).join('\n');
  }

  function pos(e) { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  function isRtl(lang) { const b = lang.split('-')[0]; return b === 'ar' || b === 'he' || b === 'il'; }
  function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
})();
