/**
 * Template Editor — Phase 2
 * Pure vanilla JS + HTML5 canvas. No external libraries.
 */

(function () {
  'use strict';

  // ─── State ───────────────────────────────────────────────────────────────
  let sourceImage = null;      // HTMLImageElement
  let sourceWidth = 0;
  let sourceHeight = 0;
  let displayScale = 1;

  let zones = [];              // Array of ZoneObj
  let selectedZoneIdx = -1;
  let zoneCounter = 0;

  // loaded template metadata for timestamps
  let loadedCreatedAt = null;
  let loadedTemplateName = null;

  // interaction
  let mode = 'draw';           // 'draw' | 'select' | 'resize'
  let isDragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartBbox = null;    // for move
  let resizeHandle = -1;       // 0..3 corner index
  let HANDLE_SIZE = 8;

  // ─── DOM refs ────────────────────────────────────────────────────────────
  const canvas = document.getElementById('editor-canvas');
  const ctx = canvas.getContext('2d');
  const imgInput = document.getElementById('img-input');
  const templateNameInput = document.getElementById('template-name');
  const loadSelect = document.getElementById('load-select');
  const zonesList = document.getElementById('zones-list');
  const saveErrorsEl = document.getElementById('save-errors');
  const msgEl = document.getElementById('canvas-msg');
  const sidebarForm = document.getElementById('sidebar-form');

  // sidebar form fields
  const sfName = document.getElementById('sf-name');
  const sfType = document.getElementById('sf-type');
  const sfNotes = document.getElementById('sf-notes');
  const sfBbox = document.getElementById('sf-bbox');
  const sfEngineGoogle = document.getElementById('sf-engine-google');
  const sfEngineAzure = document.getElementById('sf-engine-azure');
  const sfEngineOcrspace = document.getElementById('sf-engine-ocrspace');

  // ─── ZoneObj ─────────────────────────────────────────────────────────────
  function makeZone(x1, y1, x2, y2) {
    zoneCounter++;
    return {
      name: 'zone_' + zoneCounter,
      type: 'ocr',
      engines: ['google'],
      notes: '',
      bbox: [x1, y1, x2, y2],
    };
  }

  // ─── Canvas helpers ──────────────────────────────────────────────────────
  function toDisplay(sx, sy) {
    return [sx * displayScale, sy * displayScale];
  }
  function toSource(dx, dy) {
    return [Math.round(dx / displayScale), Math.round(dy / displayScale)];
  }

  function canvasPos(e) {
    const rect = canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  function clampSource(x, y) {
    return [
      Math.max(0, Math.min(sourceWidth, x)),
      Math.max(0, Math.min(sourceHeight, y)),
    ];
  }

  function getHandles(bbox) {
    const [x1, y1, x2, y2] = bbox;
    const [dx1, dy1] = toDisplay(x1, y1);
    const [dx2, dy2] = toDisplay(x2, y2);
    return [
      { x: dx1, y: dy1 },
      { x: dx2, y: dy1 },
      { x: dx2, y: dy2 },
      { x: dx1, y: dy2 },
    ];
  }

  function hitHandle(ex, ey, bbox) {
    const handles = getHandles(bbox);
    const hs = HANDLE_SIZE / 2;
    for (let i = 0; i < handles.length; i++) {
      const h = handles[i];
      if (ex >= h.x - hs && ex <= h.x + hs && ey >= h.y - hs && ey <= h.y + hs) {
        return i;
      }
    }
    return -1;
  }

  function hitZone(ex, ey) {
    // check in reverse order (topmost drawn last)
    for (let i = zones.length - 1; i >= 0; i--) {
      const [x1, y1, x2, y2] = zones[i].bbox;
      const [dx1, dy1] = toDisplay(x1, y1);
      const [dx2, dy2] = toDisplay(x2, y2);
      if (ex >= dx1 && ex <= dx2 && ey >= dy1 && ey <= dy2) return i;
    }
    return -1;
  }

  // ─── Draw ─────────────────────────────────────────────────────────────────
  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (sourceImage) {
      ctx.drawImage(sourceImage, 0, 0, canvas.width, canvas.height);
    }
    zones.forEach((z, i) => {
      const [x1, y1, x2, y2] = z.bbox;
      const [dx1, dy1] = toDisplay(x1, y1);
      const [dx2, dy2] = toDisplay(x2, y2);
      const isSelected = (i === selectedZoneIdx);
      ctx.strokeStyle = isSelected ? '#f59e0b' : '#2563eb';
      ctx.lineWidth = isSelected ? 2.5 : 1.5;
      ctx.fillStyle = isSelected ? 'rgba(245,158,11,0.12)' : 'rgba(37,99,235,0.08)';
      ctx.fillRect(dx1, dy1, dx2 - dx1, dy2 - dy1);
      ctx.strokeRect(dx1, dy1, dx2 - dx1, dy2 - dy1);

      // label
      ctx.fillStyle = isSelected ? '#f59e0b' : '#2563eb';
      ctx.font = '12px sans-serif';
      ctx.fillText(z.name, dx1 + 3, dy1 + 13);

      // resize handles on selected
      if (isSelected) {
        const handles = getHandles(z.bbox);
        const hs = HANDLE_SIZE / 2;
        handles.forEach(h => {
          ctx.fillStyle = '#f59e0b';
          ctx.fillRect(h.x - hs, h.y - hs, HANDLE_SIZE, HANDLE_SIZE);
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = 1;
          ctx.strokeRect(h.x - hs, h.y - hs, HANDLE_SIZE, HANDLE_SIZE);
        });
      }
    });
  }

  // ─── Canvas interaction ───────────────────────────────────────────────────
  canvas.addEventListener('mousedown', e => {
    if (!sourceImage) return;
    const [ex, ey] = canvasPos(e);

    // check handle first on selected zone
    if (selectedZoneIdx >= 0) {
      const h = hitHandle(ex, ey, zones[selectedZoneIdx].bbox);
      if (h >= 0) {
        mode = 'resize';
        resizeHandle = h;
        isDragging = true;
        dragStartX = ex;
        dragStartY = ey;
        dragStartBbox = [...zones[selectedZoneIdx].bbox];
        return;
      }
    }

    // check hit zone
    const hitIdx = hitZone(ex, ey);
    if (hitIdx >= 0) {
      selectZone(hitIdx);
      mode = 'select';
      isDragging = true;
      dragStartX = ex;
      dragStartY = ey;
      dragStartBbox = [...zones[hitIdx].bbox];
      return;
    }

    // draw new zone
    mode = 'draw';
    isDragging = true;
    dragStartX = ex;
    dragStartY = ey;
    selectZone(-1);
  });

  canvas.addEventListener('mousemove', e => {
    if (!isDragging || !sourceImage) return;
    const [ex, ey] = canvasPos(e);

    if (mode === 'draw') {
      const [sx1, sy1] = toSource(dragStartX, dragStartY);
      let [sx2, sy2] = toSource(ex, ey);
      [sx2, sy2] = clampSource(sx2, sy2);
      // draw preview
      redraw();
      const [dx1, dy1] = toDisplay(sx1, sy1);
      const [dx2, dy2] = toDisplay(sx2, sy2);
      ctx.strokeStyle = '#10b981';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(dx1, dy1, dx2 - dx1, dy2 - dy1);
      ctx.setLineDash([]);
    } else if (mode === 'select') {
      const dx = ex - dragStartX;
      const dy = ey - dragStartY;
      const [dsx, dsy] = toSource(dx, dy);
      const [dsx0, dsy0] = toSource(0, 0);
      const sdx = dsx - dsx0;
      const sdy = dsy - dsy0;
      let [ox1, oy1, ox2, oy2] = dragStartBbox;
      let nx1 = ox1 + sdx, ny1 = oy1 + sdy, nx2 = ox2 + sdx, ny2 = oy2 + sdy;
      const w = nx2 - nx1, h = ny2 - ny1;
      nx1 = Math.max(0, Math.min(sourceWidth - w, nx1));
      ny1 = Math.max(0, Math.min(sourceHeight - h, ny1));
      nx2 = nx1 + w;
      ny2 = ny1 + h;
      zones[selectedZoneIdx].bbox = [Math.round(nx1), Math.round(ny1), Math.round(nx2), Math.round(ny2)];
      redraw();
      updateSidebarBbox();
    } else if (mode === 'resize') {
      const [sx, sy] = toSource(ex, ey);
      const [csx, csy] = clampSource(sx, sy);
      let [x1, y1, x2, y2] = dragStartBbox;
      // resizeHandle: 0=TL, 1=TR, 2=BR, 3=BL
      if (resizeHandle === 0) { x1 = csx; y1 = csy; }
      else if (resizeHandle === 1) { x2 = csx; y1 = csy; }
      else if (resizeHandle === 2) { x2 = csx; y2 = csy; }
      else if (resizeHandle === 3) { x1 = csx; y2 = csy; }
      // ensure x1<x2, y1<y2
      const nx1 = Math.min(x1, x2), ny1 = Math.min(y1, y2);
      const nx2 = Math.max(x1, x2), ny2 = Math.max(y1, y2);
      zones[selectedZoneIdx].bbox = [nx1, ny1, nx2, ny2];
      redraw();
      updateSidebarBbox();
    }
  });

  canvas.addEventListener('mouseup', e => {
    if (!isDragging) return;
    const [ex, ey] = canvasPos(e);

    if (mode === 'draw') {
      const [sx1, sy1] = toSource(dragStartX, dragStartY);
      let [sx2, sy2] = toSource(ex, ey);
      [sx2, sy2] = clampSource(sx2, sy2);
      const x1 = Math.min(sx1, sx2), y1 = Math.min(sy1, sy2);
      const x2 = Math.max(sx1, sx2), y2 = Math.max(sy1, sy2);
      if (x2 - x1 < 5 || y2 - y1 < 5) {
        showCanvasMsg('Zone too small (min 5×5 px). Not created.', 'warn');
      } else {
        const z = makeZone(x1, y1, x2, y2);
        zones.push(z);
        selectZone(zones.length - 1);
        renderZonesList();
        redraw();
        sfName.focus();
      }
    } else if (mode === 'select' || mode === 'resize') {
      renderZonesList();
      redraw();
    }

    isDragging = false;
    mode = 'draw'; // back to draw after move/resize mouseup
    redraw();
  });

  canvas.addEventListener('mouseleave', () => {
    if (isDragging && mode === 'draw') {
      isDragging = false;
      redraw();
    }
  });

  // ─── Zone selection ───────────────────────────────────────────────────────
  function selectZone(idx) {
    selectedZoneIdx = idx;
    if (idx < 0) {
      sidebarForm.style.display = 'none';
    } else {
      sidebarForm.style.display = '';
      populateSidebarForm(zones[idx]);
    }
    renderZonesList();
    redraw();
  }

  function populateSidebarForm(z) {
    sfName.value = z.name;
    sfType.value = z.type;
    sfEngineGoogle.checked = z.engines.includes('google');
    sfEngineAzure.checked = z.engines.includes('azure');
    sfEngineOcrspace.checked = z.engines.includes('ocrspace');
    sfNotes.value = z.notes || '';
    updateSidebarBbox();
  }

  function updateSidebarBbox() {
    if (selectedZoneIdx < 0) return;
    const b = zones[selectedZoneIdx].bbox;
    sfBbox.value = '[' + b.join(', ') + ']';
  }

  // sidebar form → zone
  function applyFormToZone() {
    if (selectedZoneIdx < 0) return;
    const z = zones[selectedZoneIdx];
    z.name = sfName.value.trim();
    z.type = sfType.value;
    const engines = [];
    if (sfEngineGoogle.checked) engines.push('google');
    if (sfEngineAzure.checked) engines.push('azure');
    if (sfEngineOcrspace.checked) engines.push('ocrspace');
    z.engines = engines;
    z.notes = sfNotes.value.trim();
    renderZonesList();
    redraw();
  }

  sfName.addEventListener('input', applyFormToZone);
  sfType.addEventListener('change', applyFormToZone);
  sfEngineGoogle.addEventListener('change', applyFormToZone);
  sfEngineAzure.addEventListener('change', applyFormToZone);
  sfEngineOcrspace.addEventListener('change', applyFormToZone);
  sfNotes.addEventListener('input', applyFormToZone);

  // ─── Zones list ───────────────────────────────────────────────────────────
  function renderZonesList() {
    zonesList.innerHTML = '';
    if (!zones.length) {
      zonesList.innerHTML = '<div class="zones-empty">No zones yet. Draw on canvas.</div>';
      return;
    }
    zones.forEach((z, i) => {
      const row = document.createElement('div');
      row.className = 'zone-row' + (i === selectedZoneIdx ? ' selected' : '');
      row.innerHTML =
        '<div class="zone-info">' +
        '<span class="zone-name">' + escHtml(z.name) + '</span>' +
        '<span class="zone-type">' + escHtml(z.type) + '</span>' +
        '</div>' +
        '<button class="btn btn-xs" onclick="window._editorSelectZone(' + i + ')">Select</button>';
      zonesList.appendChild(row);
    });
  }
  window._editorSelectZone = (i) => selectZone(i);

  // ─── Delete zone ─────────────────────────────────────────────────────────
  document.getElementById('btn-delete-zone').addEventListener('click', () => {
    if (selectedZoneIdx < 0) return;
    zones.splice(selectedZoneIdx, 1);
    selectedZoneIdx = -1;
    sidebarForm.style.display = 'none';
    renderZonesList();
    redraw();
  });

  // ─── Image upload ─────────────────────────────────────────────────────────
  imgInput.addEventListener('change', () => {
    const file = imgInput.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const img = new Image();
      img.onload = () => {
        sourceImage = img;
        sourceWidth = img.naturalWidth;
        sourceHeight = img.naturalHeight;
        // scale to fit max 800px wide or 600px tall
        const maxW = 800, maxH = 600;
        displayScale = Math.min(1, maxW / sourceWidth, maxH / sourceHeight);
        canvas.width = Math.round(sourceWidth * displayScale);
        canvas.height = Math.round(sourceHeight * displayScale);
        zones = [];
        selectedZoneIdx = -1;
        zoneCounter = 0;
        sidebarForm.style.display = 'none';
        renderZonesList();
        redraw();
        showCanvasMsg('', '');
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
  });

  // ─── Load template dropdown ───────────────────────────────────────────────
  async function refreshLoadSelect() {
    try {
      const resp = await fetch('/api/templates');
      if (!resp.ok) return;
      const data = await resp.json();
      loadSelect.innerHTML = '<option value="">— Load template —</option>';
      (data.templates || data || []).forEach(t => {
        const name = t.template_name || t;
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        loadSelect.appendChild(opt);
      });
    } catch (e) { /* silently ignore */ }
  }
  refreshLoadSelect();

  loadSelect.addEventListener('change', async () => {
    const name = loadSelect.value;
    if (!name) return;
    window._logEditorLoad(name);
    const resp = await fetch('/api/templates/' + encodeURIComponent(name));
    if (!resp.ok) { alert('Could not load template: ' + name); loadSelect.value = ''; return; }
    const tmpl = await resp.json();
    const [sw, sh] = tmpl.source_size;

    if (!sourceImage) {
      alert('Upload reference image first (must match source_size ' + sw + '×' + sh + ')');
      loadSelect.value = '';
      return;
    }
    if (sourceWidth !== sw || sourceHeight !== sh) {
      alert('Image size mismatch: loaded image is ' + sourceWidth + '×' + sourceHeight +
        ' but template expects ' + sw + '×' + sh);
      loadSelect.value = '';
      return;
    }

    templateNameInput.value = tmpl.template_name;
    loadedTemplateName = tmpl.template_name;
    loadedCreatedAt = tmpl.created_at_utc || null;
    zones = (tmpl.zones || []).map(z => ({
      name: z.name,
      type: z.type,
      engines: z.engines || [],
      notes: z.notes || '',
      bbox: [...z.bbox],
    }));
    zoneCounter = zones.length;
    selectedZoneIdx = -1;
    sidebarForm.style.display = 'none';
    renderZonesList();
    redraw();
    loadSelect.value = '';
  });

  window._logEditorLoad = (name) => {
    fetch('/api/templates/_log_editor_load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_name: name }),
    }).catch(() => {});
  };

  // ─── Validation ───────────────────────────────────────────────────────────
  function validate() {
    const errors = [];
    if (!templateNameInput.value.trim()) errors.push('Template name is required.');
    if (!sourceImage) errors.push('Reference image not uploaded.');
    const emptyNames = zones.filter(z => !z.name.trim());
    if (emptyNames.length) errors.push('Some zones have empty names.');
    const names = zones.map(z => z.name.trim());
    const unique = new Set(names);
    if (unique.size !== names.length) errors.push('Duplicate zone names found.');
    zones.forEach(z => {
      const [x1, y1, x2, y2] = z.bbox;
      if (x1 >= x2 || y1 >= y2) errors.push('Zone "' + z.name + '" has invalid bbox.');
      if (z.type === 'ocr' && z.engines.length === 0)
        errors.push('Zone "' + z.name + '" (ocr) must have at least one engine.');
    });
    return errors;
  }

  function showSaveErrors(errors) {
    if (!errors.length) { saveErrorsEl.style.display = 'none'; return; }
    saveErrorsEl.innerHTML = '<ul>' + errors.map(e => '<li>' + escHtml(e) + '</li>').join('') + '</ul>';
    saveErrorsEl.style.display = 'block';
  }

  // ─── Save ─────────────────────────────────────────────────────────────────
  document.getElementById('btn-save').addEventListener('click', async () => {
    const errors = validate();
    showSaveErrors(errors);
    if (errors.length) return;

    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const tname = templateNameInput.value.trim();
    const isUpdate = (tname === loadedTemplateName && loadedCreatedAt);

    const payload = {
      template_name: tname,
      schema_version: 1,
      source_size: [sourceWidth, sourceHeight],
      zones: zones.map(z => ({
        name: z.name,
        type: z.type,
        bbox: z.bbox,
        engines: z.engines,
        engine_config: {},
        notes: z.notes || null,
      })),
      expected_texts: {},
      created_at_utc: isUpdate ? loadedCreatedAt : now,
      updated_at_utc: now,
    };

    // POST first, fall back to PUT on 409
    let resp = await fetch('/api/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp.status === 409) {
      resp = await fetch('/api/templates/' + encodeURIComponent(tname), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      showSaveErrors(['Save failed: ' + (d.error || resp.status) + ' ' + (d.details || '')]);
      return;
    }

    saveErrorsEl.style.display = 'none';
    loadedTemplateName = tname;
    loadedCreatedAt = payload.created_at_utc;

    // log
    fetch('/api/templates/_log_editor_save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_name: tname, zones_count: zones.length }),
    }).catch(() => {});

    showCanvasMsg('Saved: ' + tname, 'ok');
    await refreshLoadSelect();
  });

  // ─── Download JSON ────────────────────────────────────────────────────────
  document.getElementById('btn-download').addEventListener('click', () => {
    const errors = validate();
    if (errors.length) { showSaveErrors(errors); return; }
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const tname = templateNameInput.value.trim();
    const isUpdate = (tname === loadedTemplateName && loadedCreatedAt);
    const payload = {
      template_name: tname,
      schema_version: 1,
      source_size: [sourceWidth, sourceHeight],
      zones: zones.map(z => ({
        name: z.name,
        type: z.type,
        bbox: z.bbox,
        engines: z.engines,
        engine_config: {},
        notes: z.notes || null,
      })),
      expected_texts: {},
      created_at_utc: isUpdate ? loadedCreatedAt : now,
      updated_at_utc: now,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (tname || 'template') + '.json';
    a.click();
    URL.revokeObjectURL(url);
  });

  // ─── Clear ────────────────────────────────────────────────────────────────
  document.getElementById('btn-clear').addEventListener('click', () => {
    if (!confirm('Clear all zones?')) return;
    zones = [];
    selectedZoneIdx = -1;
    zoneCounter = 0;
    sidebarForm.style.display = 'none';
    renderZonesList();
    redraw();
  });

  // ─── Helpers ──────────────────────────────────────────────────────────────
  function showCanvasMsg(msg, type) {
    msgEl.textContent = msg;
    msgEl.className = 'canvas-msg ' + (type || '');
    msgEl.style.display = msg ? 'block' : 'none';
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Initial render
  renderZonesList();
  redraw();
})();
