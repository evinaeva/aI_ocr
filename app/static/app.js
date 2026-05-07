/* OCR Localization Checker — frontend (dark theme, multi-engine) */
'use strict';

// ── Engine config ───────────────────────────────────────────────────────
const ENGINE_COLORS = {
  google:   '#4285f4',
  azure:    '#0078d4',
  ocrspace: '#ff6b35',
  none:     '#666',
};
const ENGINE_LABELS = {
  google:   'Google Vision',
  azure:    'Azure CV',
  ocrspace: 'OCR.Space',
  none:     'None',
};

// ── State ───────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  page: 1,
  perPage: 20,
  hidePass: false,
  totalPages: 1,
  status: 'idle',
  startTime: null,
  timerInterval: null,
  engines: [],   // engines used in current session
};

// ── DOM refs ────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const $uploadSection   = $('upload-section');
const $progressSection = $('progress-section');
const $summarySection  = $('summary-section');
const $resultsSection  = $('results-section');
const $uploadForm      = $('upload-form');
const $zipFile         = $('zip-file');
const $fileLabelText   = $('file-label-text');
const $dropZone        = $('drop-zone');
const $submitBtn       = $('submit-btn');
const $progressBar     = $('progress-bar');
const $progressMsg     = $('progress-msg');
const $progressTimer   = $('progress-timer');
const $sTotal          = $('s-total');
const $sPass           = $('s-pass');
const $sFail           = $('s-fail');
const $sManual         = $('s-manual');
const $hidePass        = $('hide-pass');
const $btnDownload     = $('btn-download');
const $btnNew          = $('btn-new');
const $resultsThead    = $('results-thead');
const $resultsBody     = $('results-body');
const $pagination      = $('pagination');
const $lightbox        = $('lightbox');
const $lightboxImg     = $('lightbox-img');
const $lightboxStage   = $('lightbox-stage');
const $lightboxClose   = $('lightbox-close');

function setSectionVisible(el, visible) {
  el.classList.toggle('is-hidden', !visible);
  el.style.display = visible ? '' : 'none';
}

// ── Engine chip selection (checkboxes) ─────────────────────────────────
function initEngineChips() {
  document.querySelectorAll('.engine-chip').forEach(chip => {
    const cb = chip.querySelector('input[type=checkbox]');
    if (cb.checked) chip.classList.add('selected');
    chip.addEventListener('click', () => {
      // toggle is handled by browser for checkbox; sync class
      setTimeout(() => {
        chip.classList.toggle('selected', cb.checked);
        updateSubmitBtn();
      }, 0);
    });
  });
}
initEngineChips();

function getSelectedEngines() {
  return Array.from(document.querySelectorAll('input[name=engine]:checked')).map(el => el.value);
}

function updateSubmitBtn() {
  $submitBtn.disabled = !($zipFile.files.length > 0 && getSelectedEngines().length > 0);
}

// ── Drop zone ───────────────────────────────────────────────────────────
$zipFile.addEventListener('change', () => {
  if ($zipFile.files[0]) {
    $fileLabelText.textContent = '\uD83D\uDCE6 ' + $zipFile.files[0].name;
    $dropZone.classList.add('has-file');
  } else {
    $fileLabelText.textContent = 'Drop ZIP here or click to browse';
    $dropZone.classList.remove('has-file');
  }
  updateSubmitBtn();
});
$dropZone.addEventListener('dragover', e => { e.preventDefault(); $dropZone.classList.add('drag-over'); });
$dropZone.addEventListener('dragleave', () => $dropZone.classList.remove('drag-over'));
$dropZone.addEventListener('drop', e => {
  e.preventDefault();
  $dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) {
    $zipFile.files = e.dataTransfer.files;
    $zipFile.dispatchEvent(new Event('change'));
  }
});

// ── Timer ───────────────────────────────────────────────────────────────
function startTimer() {
  state.startTime = Date.now();
  if (state.timerInterval) clearInterval(state.timerInterval);
  state.timerInterval = setInterval(() => {
    $progressTimer.textContent = Math.round((Date.now() - state.startTime) / 1000) + 's';
  }, 1000);
}

// ── Upload form submit ──────────────────────────────────────────────────
$uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = $zipFile.files[0];
  const engines = getSelectedEngines();
  if (!file || !engines.length) return;

  const fd = new FormData();
  fd.append('zip_file', file);
  fd.append('engines', engines.join(','));
  const sn  = $('section-number').value;
  const snm = $('section-name').value;
  if (sn)  fd.append('section_number', sn);
  if (snm) fd.append('section_name', snm);

  $submitBtn.disabled = true;
  setSectionVisible($uploadSection, false);
  setSectionVisible($progressSection, true);
  setSectionVisible($summarySection, false);
  setSectionVisible($resultsSection, false);
  state.engines = engines;
  startTimer();

  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Upload failed');
    state.sessionId = data.session_id;
    state.engines   = data.engines || engines;
    state.page = 1;
    subscribeSSE(state.sessionId);
  } catch (err) {
    showError(err.message);
  }
});

// ── SSE ─────────────────────────────────────────────────────────────────
function subscribeSSE(sessionId) {
  const es = new EventSource('/api/progress/' + sessionId);
  let total = 0, done = 0;

  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event === 'ping') return;
    if (msg.event === 'start') { total = msg.total; setProgress(0, total, 'Starting\u2026'); }
    if (msg.event === 'progress') { setProgress(done, total, msg.message || ''); }
    if (msg.event === 'item') {
      done++;
      setProgress(done, total, msg.lang + ' \u2192 ' + msg.status);
    }
    if (msg.event === 'done') {
      es.close();
      if (msg.engines) state.engines = msg.engines;
      setProgress(total, total, 'Done!');
      state.status = 'done';
      updateSummary(msg);
      setSectionVisible($progressSection, false);
      setSectionVisible($summarySection, true);
      setSectionVisible($resultsSection, true);
      loadResults();
    }
    if (msg.event === 'error') {
      es.close();
      setSectionVisible($progressSection, false);
      showError(msg.message || 'Unknown error');
      $submitBtn.disabled = false;
      setSectionVisible($uploadSection, true);
    }
  };
  es.onerror = () => {
    es.close();
    if (state.status !== 'done') showError('Connection lost. Reload to retry.');
  };
}

function setProgress(done, total, msg) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  $progressBar.style.width = pct + '%';
  $progressMsg.textContent = done + ' / ' + total + ' (' + pct + '%) — ' + msg;
}

// ── Summary ─────────────────────────────────────────────────────────────
function updateSummary(msg) {
  const p = msg.pass || 0, fail = msg.fail || 0, manual = msg.manual || 0;
  $sTotal.textContent  = p + fail + manual;
  $sPass.textContent   = p;
  $sFail.textContent   = fail;
  $sManual.textContent = manual;
  if (fail > 0 || manual > 0) $btnDownload.style.display = '';
}

// ── Load results ────────────────────────────────────────────────────────
async function loadResults() {
  if (!state.sessionId) return;
  const url = '/api/results/' + state.sessionId + '?page=' + state.page +
              '&hide_pass=' + state.hidePass + '&per_page=' + state.perPage;
  const resp = await fetch(url);
  const data = await resp.json();
  if (data.error) { showError(data.error); return; }

  const s = data.session;
  $sTotal.textContent  = s.total;
  $sPass.textContent   = s.pass_count;
  $sFail.textContent   = s.fail_count;
  $sManual.textContent = s.manual_count;
  if (data.session.engines && data.session.engines.length) {
    state.engines = data.session.engines;
  }

  state.totalPages = data.total_pages;
  renderTableHeader(state.engines);
  renderTable(data.results, state.engines);
  renderPagination(data.page, data.total_pages);
}

// ── Table header (dynamic per engines) ─────────────────────────────────
function renderTableHeader(engines) {
  const cols = ['<th>Image</th>'];
  cols.push('<th>Zone</th>');
  engines.forEach(eng => {
    const color = ENGINE_COLORS[eng] || '#666';
    const label = ENGINE_LABELS[eng] || eng;
    cols.push('<th><span class="engine-dot" style="background:' + color + '"></span>' + esc(label) + '</th>');
  });
  cols.push('<th>Reference text</th>');
  cols.push('<th>Status</th>');
  cols.push('<th>Review</th>');
  $resultsThead.innerHTML = '<tr>' + cols.join('') + '</tr>';
}

// ── Table rendering ─────────────────────────────────────────────────────
function renderTable(rows, engines) {
  $resultsBody.innerHTML = '';
  const colspan = engines.length + 5;  // image + zone + engines + ref + status + review
  if (!rows.length) {
    $resultsBody.innerHTML = '<tr><td colspan="' + colspan + '" style="text-align:center;color:var(--muted);padding:20px">No results</td></tr>';
    return;
  }
  const seenImageNames = new Set();
  rows.forEach(row => {
    const tr = document.createElement('tr');
    const imageName = row.image_name || '';
    const imgSrc = imageName
      ? '/image/' + state.sessionId + '/' + encodeURIComponent(row.image_name)
      : null;
    const showImageThumb = !!imageName && !seenImageNames.has(imageName);
    if (imageName) seenImageNames.add(imageName);

    // Image cell
    let html = '<td class="img-cell">';
    if (imgSrc && showImageThumb) {
      html += '<div class="thumb-wrap">' +
        '<img class="thumb" src="' + imgSrc + '" alt="' + esc(row.image_name) + '" data-full="' + imgSrc + '">' +
        '<div class="thumb-missing" style="display:none">no image</div>' +
        '</div>' +
        '<div class="thumb-label">' + esc(getThumbLangCode(row.image_name, row.lang)) + '</div>';
    } else if (imgSrc) {
      html += '<span class="img-repeat-placeholder">\u2014</span>';
    } else {
      html += '<span style="color:var(--muted)">\u2014</span>';
    }
    html += '</td>';

    // Zone column
    html += '<td class="zone-cell" title="' + esc(row.target_id || '') + '">' + esc(row.zone_name || '-') + '</td>';

    // One OCR text column per engine — char-level diff against
    // the reference (red = chars only in OCR; chars only in ref are
    // not drawn on the OCR side).
    const refText = row.ref_text || '';
    engines.forEach(eng => {
      const engData = (row.ocr_results || {})[eng] || {};
      const text = engData.text || '';
      const conf = engData.confidence != null ? Math.round(engData.confidence * 100) + '%' : '';
      const isBest = row.best_engine === eng;
      html += '<td class="text-cell">';
      if (text) {
        if (refText) {
          html += diffOcrVsRef(text, refText).ocrHtml;
        } else {
          html += formatText(text);
        }
        if (conf) html += '<span class="conf-badge' + (isBest ? ' conf-best' : '') + '">' + conf + (isBest ? ' ★' : '') + '</span>';
      } else {
        html += '<span style="color:var(--muted)">\u2014</span>';
      }
      html += '</td>';
    });

    // Reference text column — diff against the consensus / best
    // engine OCR (yellow = chars in ref that the best engine missed).
    const bestEng = row.best_engine || null;
    const bestText = bestEng ? (((row.ocr_results || {})[bestEng] || {}).text || '') : '';
    if (refText && bestText) {
      html += '<td class="text-cell">' + diffOcrVsRef(bestText, refText).refHtml + '</td>';
    } else {
      html += '<td class="text-cell">' + formatText(refText) + '</td>';
    }

    // Status column
    html += '<td>' +
      '<span class="badge badge-' + (row.status || 'manual').toLowerCase() + '">' + esc(row.status || 'MANUAL') + '</span>' +
      (row.section_name
        ? '<span class="badge-section">' + (row.section_number ? row.section_number + '. ' : '') + esc(row.section_name) + '</span>'
        : '') +
      '</td>';

    // Review column
    html += '<td class="review-cell" data-id="' + row.id + '">' + renderReviewButtons(row) + '</td>';

    tr.innerHTML = html;
    $resultsBody.appendChild(tr);
  });

  document.querySelectorAll('.thumb-wrap img.thumb').forEach(img => {
    img.addEventListener('error', () => {
      img.style.display = 'none';
      const m = img.nextElementSibling;
      if (m) m.style.display = 'flex';
    });
    img.addEventListener('click', () => openLightbox(img.dataset.full));
  });
  document.querySelectorAll('.btn-ok, .btn-err').forEach(btn => {
    btn.addEventListener('click', handleDecision);
  });
}

function renderReviewButtons(row) {
  if (row.status === 'PASS' && !row.manual_decision) {
    return '<span style="color:var(--muted);font-size:.75rem">\u2014</span>';
  }
  if (row.manual_decision === 'ok')    return '<span class="decision-ok">\u2713 OK</span>';
  if (row.manual_decision === 'error') return '<span class="decision-err">\u2717 ERROR</span>';
  return '<button class="btn btn-sm btn-ok"   data-id="' + row.id + '" data-action="ok">OK</button>' +
         '<button class="btn btn-sm btn-err" data-id="' + row.id + '" data-action="error">ERROR</button>';
}

async function handleDecision(e) {
  const btn = e.currentTarget;
  const id = btn.dataset.id, action = btn.dataset.action;
  const fd = new FormData(); fd.append('decision', action);
  await fetch('/api/decide/' + id, { method: 'POST', body: fd });
  const cell = btn.closest('td');
  cell.innerHTML = action === 'ok'
    ? '<span class="decision-ok">\u2713 OK</span>'
    : '<span class="decision-err">\u2717 ERROR</span>';
  if (action === 'error') $btnDownload.style.display = '';
}

// Char-level diff (LCS) used to highlight OCR vs reference.
// Output ops: {op: 'eq'|'del'|'add', char}
//   'eq'  — char in both OCR and reference
//   'del' — char in OCR only (red on the OCR side)
//   'add' — char in reference only (yellow on the reference side)
function charDiff(a, b) {
  a = a || '';
  b = b || '';
  const m = a.length, n = b.length;
  if (m === 0 && n === 0) return [];
  if (m === 0) {
    const out = new Array(n);
    for (let k = 0; k < n; k++) out[k] = { op: 'add', char: b[k] };
    return out;
  }
  if (n === 0) {
    const out = new Array(m);
    for (let k = 0; k < m; k++) out[k] = { op: 'del', char: a[k] };
    return out;
  }
  const dp = new Array(m + 1);
  for (let i = 0; i <= m; i++) dp[i] = new Int32Array(n + 1);
  for (let i = 1; i <= m; i++) {
    const ai = a[i - 1];
    for (let j = 1; j <= n; j++) {
      if (ai === b[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1;
      else dp[i][j] = dp[i - 1][j] >= dp[i][j - 1] ? dp[i - 1][j] : dp[i][j - 1];
    }
  }
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      ops.push({ op: 'eq', char: a[i - 1] }); i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push({ op: 'add', char: b[j - 1] }); j--;
    } else {
      ops.push({ op: 'del', char: a[i - 1] }); i--;
    }
  }
  return ops.reverse();
}

// Pre-clean for visual diff: collapse Windows newlines and trim trailing
// spaces on each line so trailing-whitespace doesn't get a yellow/red bar.
function diffPreClean(s) {
  if (!s) return '';
  return String(s)
    .replace(/\r\n?/g, '\n')
    .replace(/[\t ]+\n/g, '\n')
    .replace(/[\t ]+$/g, '')
    .replace(/^﻿/, '');
}

// Returns { ocrHtml, refHtml } — two HTML fragments with mismatching
// characters wrapped in <span class="diff-del"> / <span class="diff-add">.
// Each non-empty line is wrapped in <span class="text-line"> to match
// the rest of the table's typography.
function diffOcrVsRef(ocrText, refText) {
  const a = diffPreClean(ocrText);
  const b = diffPreClean(refText);
  const ops = charDiff(a, b);

  const ocrLines = [];
  const refLines = [];
  let curOcr = '';
  let curRef = '';
  const flushOcr = () => { ocrLines.push(curOcr); curOcr = ''; };
  const flushRef = () => { refLines.push(curRef); curRef = ''; };

  for (const op of ops) {
    const c = op.char;
    if (op.op === 'eq') {
      if (c === '\n') { flushOcr(); flushRef(); continue; }
      const e = esc(c);
      curOcr += e;
      curRef += e;
    } else if (op.op === 'del') {
      if (c === '\n') { flushOcr(); continue; }
      curOcr += '<span class="diff-del">' + esc(c) + '</span>';
    } else { // 'add'
      if (c === '\n') { flushRef(); continue; }
      curRef += '<span class="diff-add">' + esc(c) + '</span>';
    }
  }
  flushOcr(); flushRef();

  const wrap = (lines) => {
    const out = [];
    for (const l of lines) {
      const stripped = l.replace(/<[^>]+>/g, '').replace(/\s+/g, '');
      if (!stripped.length) continue;
      out.push('<span class="text-line">' + l + '</span>');
    }
    if (!out.length) return '<span style="color:var(--muted)">—</span>';
    return out.join('');
  };

  return { ocrHtml: wrap(ocrLines), refHtml: wrap(refLines) };
}

function formatText(text) {
  if (!text) return '<span style="color:var(--muted)">\u2014</span>';
  const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  if (!lines.length) return '<span style="color:var(--muted)">\u2014</span>';
  return lines.map(l => '<span class="text-line">' + esc(l) + '</span>').join('');
}

function normalizeLangForLabel(lang) {
  const l = String(lang || '').toLowerCase();
  const map = { cn: 'zh-hans', kr: 'ko', ua: 'uk' };
  const norm = map[l] || l;
  if (!norm) return 'und';
  if (norm === 'und') return 'und';
  if (/^[a-z]{2}$/.test(norm)) return norm;
  const alpha = norm.replace(/[^a-z]/g, '');
  return alpha.length >= 2 ? alpha.slice(0, 2) : 'und';
}

function getThumbLangCode(imageName, rowLang) {
  const preferred = normalizeLangForLabel(rowLang);
  if (preferred !== 'und') return preferred;
  const base = String(imageName || '').split('/').pop().split('\\').pop().replace(/\.[^.]+$/, '');
  return normalizeLangForLabel(base);
}


// ── Pagination ──────────────────────────────────────────────────────────
function renderPagination(current, total) {
  $pagination.innerHTML = '';
  if (total <= 1) return;
  function makeBtn(label, page, disabled, active) {
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (active ? ' active' : '');
    btn.textContent = label;
    btn.disabled = disabled;
    if (!disabled) btn.addEventListener('click', () => { state.page = page; loadResults(); });
    return btn;
  }
  $pagination.appendChild(makeBtn('\u00ab', 1, current === 1, false));
  $pagination.appendChild(makeBtn('\u2039', current - 1, current === 1, false));
  const start = Math.max(1, current - 2), end = Math.min(total, current + 2);
  for (let p = start; p <= end; p++) $pagination.appendChild(makeBtn(p, p, false, p === current));
  $pagination.appendChild(makeBtn('\u203a', current + 1, current === total, false));
  $pagination.appendChild(makeBtn('\u00bb', total, current === total, false));
}

// ── Controls ────────────────────────────────────────────────────────────
$hidePass.addEventListener('change', () => { state.hidePass = $hidePass.checked; state.page = 1; loadResults(); });
$btnDownload.addEventListener('click', () => { if (state.sessionId) window.location.href = '/api/download/' + state.sessionId; });
$btnNew.addEventListener('click', () => {
  state.sessionId = null; state.page = 1; state.status = 'idle'; state.engines = [];
  if (state.timerInterval) clearInterval(state.timerInterval);
  $submitBtn.disabled = true;
  $uploadForm.reset();
  $fileLabelText.textContent = 'Drop ZIP here or click to browse';
  $dropZone.classList.remove('has-file');
  document.querySelectorAll('.engine-chip').forEach(c => c.classList.remove('selected'));
  // Re-check first engine as default
  const firstChip = document.querySelector('.engine-chip');
  const firstCb   = firstChip && firstChip.querySelector('input[type=checkbox]');
  if (firstCb) { firstCb.checked = true; firstChip.classList.add('selected'); }
  setSectionVisible($summarySection, false);
  setSectionVisible($resultsSection, false);
  setSectionVisible($progressSection, false);
  setSectionVisible($uploadSection, true);
  $resultsBody.innerHTML = '';
  $resultsThead.innerHTML = '';
  $pagination.innerHTML = '';
  updateSubmitBtn();
});

// ── Lightbox ────────────────────────────────────────────────────────────
const lightboxState = {
  baseScale: 1,
  zoomScale: 1,
  naturalWidth: 0,
  naturalHeight: 0,
};

function applyLightboxTransform() {
  const scale = lightboxState.baseScale * lightboxState.zoomScale;
  $lightboxImg.style.transform = 'scale(' + scale + ')';
}

function fitLightboxToViewport() {
  if (!lightboxState.naturalWidth || !lightboxState.naturalHeight) return;
  const maxW = Math.max(100, $lightboxStage.clientWidth);
  const maxH = Math.max(100, $lightboxStage.clientHeight);
  lightboxState.baseScale = Math.min(1, maxW / lightboxState.naturalWidth, maxH / lightboxState.naturalHeight);
  $lightboxImg.style.transformOrigin = 'top left';
  applyLightboxTransform();
}

function openLightbox(src) {
  lightboxState.zoomScale = 1;
  $lightboxImg.style.transform = '';
  $lightboxImg.src = src;
  $lightbox.classList.add('open');
}

function closeLightbox() {
  $lightbox.classList.remove('open');
  $lightboxImg.src = '';
  $lightboxImg.style.transform = '';
}

$lightboxImg.addEventListener('load', () => {
  lightboxState.naturalWidth = $lightboxImg.naturalWidth;
  lightboxState.naturalHeight = $lightboxImg.naturalHeight;
  $lightboxImg.style.width = lightboxState.naturalWidth + 'px';
  $lightboxImg.style.height = lightboxState.naturalHeight + 'px';
  fitLightboxToViewport();
  $lightboxStage.scrollTop = 0;
  $lightboxStage.scrollLeft = 0;
});

$lightboxImg.addEventListener('wheel', (e) => {
  e.preventDefault();
  const zoomStep = e.deltaY < 0 ? 1.1 : 0.9;
  lightboxState.zoomScale = Math.min(6, Math.max(0.2, lightboxState.zoomScale * zoomStep));
  applyLightboxTransform();
}, { passive: false });

window.addEventListener('resize', () => {
  if ($lightbox.classList.contains('open')) fitLightboxToViewport();
});

$lightbox.addEventListener('click', (e) => {
  if (e.target === $lightbox) closeLightbox();
});
$lightboxStage.addEventListener('click', (e) => {
  if (!$lightboxImg.contains(e.target)) closeLightbox();
});
$lightboxClose.addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

// ── Utils ───────────────────────────────────────────────────────────────
function esc(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function showError(msg) {
  setSectionVisible($progressSection, false);
  setSectionVisible($uploadSection, true);
  const div = document.createElement('div');
  div.className = 'error-msg';
  div.textContent = '\u26a0 ' + msg;
  $uploadSection.parentNode.insertBefore(div, $uploadSection.nextSibling);
  setTimeout(() => div.remove(), 8000);
  $submitBtn.disabled = false;
}
