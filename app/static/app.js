/* OCR Localization Checker — frontend */
'use strict';

// ── Engine colors ─────────────────────────────────────────────────────────
const ENGINE_COLORS = {
  google:   '#4285f4',
  azure:    '#0078d4',
  ocrspace: '#ff6b35',
  none:     '#999',
};
const ENGINE_LABELS = {
  google:   'Google Vision',
  azure:    'Azure CV',
  ocrspace: 'OCR.Space',
  none:     'None',
};

// ── State ──────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  page: 1,
  perPage: 20,
  hidePass: false,
  totalPages: 1,
  status: 'idle',
};

// ── DOM refs ────────────────────────────────────────────────────────────
const $uploadSection   = document.getElementById('upload-section');
const $progressSection = document.getElementById('progress-section');
const $summarySection  = document.getElementById('summary-section');
const $resultsSection  = document.getElementById('results-section');
const $uploadForm      = document.getElementById('upload-form');
const $zipFile         = document.getElementById('zip-file');
const $fileLabelText   = document.getElementById('file-label-text');
const $submitBtn       = document.getElementById('submit-btn');
const $progressBar     = document.getElementById('progress-bar');
const $progressMsg     = document.getElementById('progress-msg');
const $sTotal          = document.getElementById('s-total');
const $sPass           = document.getElementById('s-pass');
const $sFail           = document.getElementById('s-fail');
const $sManual         = document.getElementById('s-manual');
const $hidePass        = document.getElementById('hide-pass');
const $btnDownload     = document.getElementById('btn-download');
const $btnNew          = document.getElementById('btn-new');
const $resultsBody     = document.getElementById('results-body');
const $pagination      = document.getElementById('pagination');

// ── File input label ──────────────────────────────────────────────────────
$zipFile.addEventListener('change', () => {
  $fileLabelText.textContent = $zipFile.files[0]?.name || 'Choose ZIP file…';
});

// ── Upload form submit ────────────────────────────────────────────────────
$uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = $zipFile.files[0];
  if (!file) return;

  const fd = new FormData();
  fd.append('zip_file', file);
  const sn = document.getElementById('section-number').value;
  const snm = document.getElementById('section-name').value;
  if (sn) fd.append('section_number', sn);
  if (snm) fd.append('section_name', snm);

  $submitBtn.disabled = true;
  $uploadSection.style.display = 'none';
  $progressSection.style.display = '';
  $summarySection.style.display = 'none';
  $resultsSection.style.display = 'none';

  try {
    const resp = await fetch('/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Upload failed');
    state.sessionId = data.session_id;
    state.page = 1;
    subscribeSSE(state.sessionId);
  } catch (err) {
    showError(err.message);
  }
});

// ── SSE ────────────────────────────────────────────────────────────────
function subscribeSSE(sessionId) {
  const es = new EventSource(`/progress/${sessionId}`);
  let total = 0;
  let done = 0;

  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.event === 'ping') return;

    if (msg.event === 'start') {
      total = msg.total;
      setProgress(0, total, 'Starting…');
    }

    if (msg.event === 'progress') {
      setProgress(done, total, msg.message || '');
    }

    if (msg.event === 'item') {
      done++;
      const eng = msg.engine ? ` [${ENGINE_LABELS[msg.engine] || msg.engine}]` : '';
      setProgress(done, total, `Processed ${msg.lang} → ${msg.status}${eng}`);
    }

    if (msg.event === 'done') {
      es.close();
      setProgress(total, total, 'Done!');
      state.status = 'done';
      updateSummary(msg);
      $progressSection.style.display = 'none';
      $summarySection.style.display = '';
      $resultsSection.style.display = '';
      loadResults();
    }

    if (msg.event === 'error') {
      es.close();
      $progressSection.style.display = 'none';
      showError(msg.message || 'Unknown error');
      $submitBtn.disabled = false;
      $uploadSection.style.display = '';
    }
  };

  es.onerror = () => {
    es.close();
    if (state.status !== 'done') {
      showError('Connection lost. Reload to retry.');
    }
  };
}

function setProgress(done, total, msg) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  $progressBar.style.width = pct + '%';
  $progressMsg.textContent = msg;
}

// ── Summary ────────────────────────────────────────────────────────────
function updateSummary({ pass: p = 0, fail = 0, manual = 0 }) {
  const total = p + fail + manual;
  $sTotal.textContent  = total;
  $sPass.textContent   = p;
  $sFail.textContent   = fail;
  $sManual.textContent = manual;
  if (fail > 0 || manual > 0) $btnDownload.style.display = '';
}

// ── Load results ────────────────────────────────────────────────────────
async function loadResults() {
  if (!state.sessionId) return;
  const url = `/results/${state.sessionId}?page=${state.page}&hide_pass=${state.hidePass}&per_page=${state.perPage}`;
  const resp = await fetch(url);
  const data = await resp.json();

  if (data.error) { showError(data.error); return; }

  const s = data.session;
  $sTotal.textContent  = s.total;
  $sPass.textContent   = s.pass_count;
  $sFail.textContent   = s.fail_count;
  $sManual.textContent = s.manual_count;

  state.totalPages = data.total_pages;
  renderTable(data.results);
  renderPagination(data.page, data.total_pages);
}

// ── Engine badge helper ─────────────────────────────────────────────────
function engineBadge(engine, confidence) {
  if (!engine || engine === 'none') return '';
  const color = ENGINE_COLORS[engine] || '#999';
  const label = ENGINE_LABELS[engine] || engine;
  const confStr = confidence != null ? ` ${Math.round(confidence * 100)}%` : '';
  return `<span class="engine-badge" style="--eng-color:${color}">
    <span class="engine-dot" style="background:${color}"></span>
    ${esc(label)}${confStr}
  </span>`;
}

// ── Table rendering ─────────────────────────────────────────────────────
function renderTable(rows) {
  $resultsBody.innerHTML = '';
  if (!rows.length) {
    $resultsBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;padding:20px">No results</td></tr>';
    return;
  }
  rows.forEach(row => {
    const tr = document.createElement('tr');
    const imgSrc = row.image_name
      ? `/image/${state.sessionId}/${encodeURIComponent(row.image_name)}`
      : null;

    tr.innerHTML = `
      <td class="img-cell">
        ${imgSrc
          ? `<div class="thumb-wrap">
               <img class="thumb" src="${imgSrc}" alt="${esc(row.image_name)}"
                    data-full="${imgSrc}">
               <div class="thumb-missing" style="display:none">no image</div>
             </div>
             <div class="thumb-label">${esc(row.lang?.toUpperCase() || '')}</div>`
          : '<span style="color:#ccc">—</span>'}
      </td>
      <td class="text-cell">${formatText(row.ocr_text || '')}</td>
      <td class="text-cell">${formatText(row.ref_text || '')}</td>
      <td>
        <span class="badge badge-${(row.status||'manual').toLowerCase()}">${esc(row.status||'MANUAL')}</span>
        ${row.section_name
          ? `<span class="badge-section">${row.section_number ? row.section_number + '. ' : ''}${esc(row.section_name)}</span>`
          : ''}
        ${engineBadge(row.ocr_engine, row.ocr_confidence)}
      </td>
      <td class="review-cell" data-id="${row.id}" data-decision="${row.manual_decision || ''}">
        ${renderReviewButtons(row)}
      </td>
    `;
    $resultsBody.appendChild(tr);
  });

  // Attach image error handlers after DOM insertion
  document.querySelectorAll('.thumb-wrap img.thumb').forEach(img => {
    img.addEventListener('error', () => {
      img.style.display = 'none';
      const missing = img.nextElementSibling;
      if (missing) missing.style.display = 'flex';
    });
    img.addEventListener('click', () => openLightbox(img.dataset.full));
  });

  // Bind review buttons
  document.querySelectorAll('.btn-ok, .btn-err').forEach(btn => {
    btn.addEventListener('click', handleDecision);
  });
}

function renderReviewButtons(row) {
  if (row.status === 'PASS' && !row.manual_decision) {
    return '<span style="color:#ccc;font-size:.75rem">—</span>';
  }
  const dec = row.manual_decision;
  if (dec === 'ok') {
    return '<span class="decision-ok">✓ OK</span>';
  }
  if (dec === 'error') {
    return '<span class="decision-err">✗ ERROR</span>';
  }
  return `
    <button class="btn btn-sm btn-ok" data-id="${row.id}" data-action="ok">OK</button>
    <button class="btn btn-sm btn-err" data-id="${row.id}" data-action="error">ERROR</button>
  `;
}

async function handleDecision(e) {
  const btn = e.currentTarget;
  const id = btn.dataset.id;
  const action = btn.dataset.action;
  const fd = new FormData();
  fd.append('decision', action);
  await fetch(`/decide/${id}`, { method: 'POST', body: fd });
  const cell = btn.closest('td');
  cell.innerHTML = action === 'ok'
    ? '<span class="decision-ok">✓ OK</span>'
    : '<span class="decision-err">✗ ERROR</span>';
  if (action === 'error') $btnDownload.style.display = '';
}

// ── Text formatting ─────────────────────────────────────────────────────
function formatText(text) {
  if (!text) return '<span style="color:#ccc">—</span>';
  const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  if (!lines.length) return '<span style="color:#ccc">—</span>';
  return lines.map(l => `<span class="text-line">${esc(l)}</span>`).join('');
}

// ── Pagination ──────────────────────────────────────────────────────────
function renderPagination(current, total) {
  $pagination.innerHTML = '';
  if (total <= 1) return;

  const makeBtn = (label, page, disabled, active) => {
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (active ? ' active' : '');
    btn.textContent = label;
    btn.disabled = disabled;
    if (!disabled) btn.addEventListener('click', () => { state.page = page; loadResults(); });
    return btn;
  };

  $pagination.appendChild(makeBtn('«', 1, current === 1, false));
  $pagination.appendChild(makeBtn('‹', current-1, current === 1, false));

  const start = Math.max(1, current - 2);
  const end   = Math.min(total, current + 2);
  for (let p = start; p <= end; p++) {
    $pagination.appendChild(makeBtn(p, p, false, p === current));
  }

  $pagination.appendChild(makeBtn('›', current+1, current === total, false));
  $pagination.appendChild(makeBtn('»', total, current === total, false));
}

// ── Controls ────────────────────────────────────────────────────────────
$hidePass.addEventListener('change', () => {
  state.hidePass = $hidePass.checked;
  state.page = 1;
  loadResults();
});

$btnDownload.addEventListener('click', () => {
  if (state.sessionId)
    window.location.href = `/download/${state.sessionId}`;
});

$btnNew.addEventListener('click', () => {
  state.sessionId = null;
  state.page = 1;
  state.status = 'idle';
  $submitBtn.disabled = false;
  $uploadForm.reset();
  $fileLabelText.textContent = 'Choose ZIP file…';
  $summarySection.style.display = 'none';
  $resultsSection.style.display = 'none';
  $progressSection.style.display = 'none';
  $uploadSection.style.display = '';
  $resultsBody.innerHTML = '';
  $pagination.innerHTML = '';
});

// ── Lightbox ────────────────────────────────────────────────────────────
const lightboxOverlay = document.createElement('div');
lightboxOverlay.className = 'lightbox-overlay';
const lightboxImg = document.createElement('img');
lightboxOverlay.appendChild(lightboxImg);
document.body.appendChild(lightboxOverlay);
lightboxOverlay.addEventListener('click', () => lightboxOverlay.classList.remove('open'));

function openLightbox(src) {
  lightboxImg.src = src;
  lightboxOverlay.classList.add('open');
}

// ── Utils ───────────────────────────────────────────────────────────────
function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showError(msg) {
  $progressSection.style.display = 'none';
  $uploadSection.style.display = '';
  const div = document.createElement('div');
  div.className = 'error-msg card';
  div.textContent = '⚠ ' + msg;
  $uploadSection.parentNode.insertBefore(div, $uploadSection.nextSibling);
  setTimeout(() => div.remove(), 8000);
  $submitBtn.disabled = false;
}
