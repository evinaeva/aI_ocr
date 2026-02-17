/* OCR Localization Checker — frontend */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  page: 1,
  perPage: 20,
  hidePass: false,
  totalPages: 1,
  status: 'idle',   // idle | processing | done | error
};

// ── DOM refs ──────────────────────────────────────────────────────────────
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

// ── File input label ───────────────────────────────────────────────────────
$zipFile.addEventListener('change', () => {
  $fileLabelText.textContent = $zipFile.files[0]?.name || 'Choose ZIP file…';
});

// ── Upload form submit ─────────────────────────────────────────────────────
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

// ── SSE ────────────────────────────────────────────────────────────────────
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
      setProgress(done, total, `Processed ${msg.lang} → ${msg.status}`);
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
    // If done already, ignore
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

// ── Summary ────────────────────────────────────────────────────────────────
function updateSummary({ pass = 0, fail = 0, manual = 0 }) {
  const total = pass + fail + manual;
  $sTotal.textContent  = total;
  $sPass.textContent   = pass;
  $sFail.textContent   = fail;
  $sManual.textContent = manual;
  if (fail > 0 || manual > 0) $btnDownload.style.display = '';
}

// ── Load results ───────────────────────────────────────────────────────────
async function loadResults() {
  if (!state.sessionId) return;
  const url = `/results/${state.sessionId}?page=${state.page}&hide_pass=${state.hidePass}&per_page=${state.perPage}`;
  const resp = await fetch(url);
  const data = await resp.json();

  if (data.error) { showError(data.error); return; }

  // Update summary from session (in case SSE was missed)
  const s = data.session;
  $sTotal.textContent  = s.total;
  $sPass.textContent   = s.pass_count;
  $sFail.textContent   = s.fail_count;
  $sManual.textContent = s.manual_count;

  state.totalPages = data.total_pages;
  renderTable(data.results);
  renderPagination(data.page, data.total_pages);
}

// ── Table rendering ────────────────────────────────────────────────────────
function renderTable(rows) {
  $resultsBody.innerHTML = '';
  if (!rows.length) {
    $resultsBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;padding:20px">No results</td></tr>';
    return;
  }
  rows.forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="img-cell">
        ${row.image_name
          ? `<img class="thumb" src="/image/${state.sessionId}/${encodeURIComponent(row.image_name)}"
                   alt="${esc(row.image_name)}"
                   data-full="/image/${state.sessionId}/${encodeURIComponent(row.image_name)}">
             <div class="thumb-label">${esc(row.lang?.toUpperCase() || '')}</div>`
          : '<span style="color:#ccc">—</span>'}
      </td>
      <td class="text-cell">${alignedDiff(row.ocr_text || '', row.ref_text || '', 'ocr')}</td>
      <td class="text-cell">${alignedDiff(row.ocr_text || '', row.ref_text || '', 'ref')}</td>
      <td>
        <span class="badge badge-${(row.status||'manual').toLowerCase()}">${esc(row.status||'MANUAL')}</span>
        ${row.section_name
          ? `<span class="badge-section">${row.section_number ? row.section_number + '. ' : ''}${esc(row.section_name)}</span>`
          : ''}
      </td>
      <td class="review-cell" data-id="${row.id}" data-decision="${row.manual_decision || ''}">
        ${renderReviewButtons(row)}
      </td>
    `;
    $resultsBody.appendChild(tr);
  });

  // Bind thumbnail clicks
  document.querySelectorAll('.thumb').forEach(img => {
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
  // Re-render just this row's review cell
  const cell = btn.closest('td');
  cell.innerHTML = action === 'ok'
    ? '<span class="decision-ok">✓ OK</span>'
    : '<span class="decision-err">✗ ERROR</span>';
  // Show download button if needed
  if (action === 'error') $btnDownload.style.display = '';
}

// ── Diff alignment (DP line alignment + char diff) ────────────────────────
function alignedDiff(ocrText, refText, side) {
  const ocrLines = ocrText.split('\n');
  const refLines = refText.split('\n');
  const aligned = dpAlignLines(ocrLines, refLines);

  return aligned.map(([oLine, rLine]) => {
    const line = side === 'ocr' ? oLine : rLine;
    if (line === null) {
      return `<span class="diff-line diff-del">&nbsp;</span>`;
    }
    if (oLine === null || rLine === null) {
      const cls = side === 'ocr' && oLine !== null ? 'diff-add'
                : side === 'ref' && rLine !== null ? 'diff-add' : 'diff-del';
      return `<span class="diff-line ${cls}">${esc(line)}</span>`;
    }
    if (oLine === rLine) {
      return `<span class="diff-line">${esc(line)}</span>`;
    }
    // char-level diff for changed lines
    return `<span class="diff-line">${charDiff(oLine, rLine, side)}</span>`;
  }).join('');
}

// Simple greedy DP line alignment (LCS-based)
function dpAlignLines(a, b) {
  const m = a.length, n = b.length;
  const dp = Array.from({length: m+1}, () => new Array(n+1).fill(0));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1]+1 : Math.max(dp[i-1][j], dp[i][j-1]);

  // Traceback
  const result = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) {
      result.push([a[i-1], b[j-1]]); i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      result.push([null, b[j-1]]); j--;
    } else {
      result.push([a[i-1], null]); i--;
    }
  }
  return result.reverse();
}

// Char-level diff using Myers-like (simple)
function charDiff(aStr, bStr, side) {
  const a = [...aStr], b = [...bStr];
  const m = a.length, n = b.length;
  const dp = Array.from({length: m+1}, () => new Array(n+1).fill(0));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1]+1 : Math.max(dp[i-1][j], dp[i][j-1]);

  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) {
      ops.push(['eq', a[i-1]]); i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      ops.push(['ins', b[j-1]]); j--;
    } else {
      ops.push(['del', a[i-1]]); i--;
    }
  }
  ops.reverse();

  return ops.map(([op, ch]) => {
    const escaped = esc(ch);
    if (op === 'eq') return escaped;
    if (op === 'ins' && side === 'ref') return `<span class="diff-char-add">${escaped}</span>`;
    if (op === 'del' && side === 'ocr') return `<span class="diff-char-del">${escaped}</span>`;
    return escaped;
  }).join('');
}

// ── Pagination ────────────────────────────────────────────────────────────
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

// ── Controls ──────────────────────────────────────────────────────────────
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

// ── Lightbox ──────────────────────────────────────────────────────────────
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

// ── Utils ─────────────────────────────────────────────────────────────────
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
