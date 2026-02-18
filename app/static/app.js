/* OCR Localization Checker — frontend (dark theme) */
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
const $resultsBody     = $('results-body');
const $pagination      = $('pagination');
const $lightbox        = $('lightbox');
const $lightboxImg     = $('lightbox-img');
const $lightboxClose   = $('lightbox-close');

// ── Engine chip selection ───────────────────────────────────────────────
function initEngineChips() {
  document.querySelectorAll('.engine-chip').forEach(chip => {
    const radio = chip.querySelector('input[type=radio]');
    if (radio.checked) chip.classList.add('selected');
    chip.addEventListener('click', () => {
      document.querySelectorAll('.engine-chip').forEach(c => c.classList.remove('selected'));
      radio.checked = true;
      chip.classList.add('selected');
      updateSubmitBtn();
    });
  });
}
initEngineChips();

function getSelectedEngine() {
  const checked = document.querySelector('input[name=engine]:checked');
  return checked ? checked.value : null;
}

function updateSubmitBtn() {
  $submitBtn.disabled = !($zipFile.files.length > 0 && getSelectedEngine());
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

$dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  $dropZone.classList.add('drag-over');
});
$dropZone.addEventListener('dragleave', () => {
  $dropZone.classList.remove('drag-over');
});
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
    const elapsed = Math.round((Date.now() - state.startTime) / 1000);
    $progressTimer.textContent = elapsed + 's';
  }, 1000);
}

// ── Upload form submit ──────────────────────────────────────────────────
$uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = $zipFile.files[0];
  const engine = getSelectedEngine();
  if (!file || !engine) return;

  const fd = new FormData();
  fd.append('zip_file', file);
  fd.append('engine', engine);
  const sn = $('section-number').value;
  const snm = $('section-name').value;
  if (sn) fd.append('section_number', sn);
  if (snm) fd.append('section_name', snm);

  $submitBtn.disabled = true;
  $uploadSection.style.display = 'none';
  $progressSection.style.display = '';
  $summarySection.style.display = 'none';
  $resultsSection.style.display = 'none';
  startTimer();

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

// ── SSE ─────────────────────────────────────────────────────────────────
function subscribeSSE(sessionId) {
  const es = new EventSource('/progress/' + sessionId);
  let total = 0;
  let done = 0;

  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event === 'ping') return;

    if (msg.event === 'start') {
      total = msg.total;
      setProgress(0, total, 'Starting\u2026');
    }
    if (msg.event === 'progress') {
      setProgress(done, total, msg.message || '');
    }
    if (msg.event === 'item') {
      done++;
      const eng = msg.engine ? ' [' + (ENGINE_LABELS[msg.engine] || msg.engine) + ']' : '';
      setProgress(done, total, msg.lang + ' \u2192 ' + msg.status + eng);
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
  $progressMsg.textContent = done + ' / ' + total + ' (' + pct + '%) \u2014 ' + msg;
}

// ── Summary ─────────────────────────────────────────────────────────────
function updateSummary(msg) {
  var p = msg.pass || 0, fail = msg.fail || 0, manual = msg.manual || 0;
  var total = p + fail + manual;
  $sTotal.textContent  = total;
  $sPass.textContent   = p;
  $sFail.textContent   = fail;
  $sManual.textContent = manual;
  if (fail > 0 || manual > 0) $btnDownload.style.display = '';
}

// ── Load results ────────────────────────────────────────────────────────
async function loadResults() {
  if (!state.sessionId) return;
  const url = '/results/' + state.sessionId + '?page=' + state.page + '&hide_pass=' + state.hidePass + '&per_page=' + state.perPage;
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
  const color = ENGINE_COLORS[engine] || '#666';
  const label = ENGINE_LABELS[engine] || engine;
  const confStr = confidence != null ? ' ' + Math.round(confidence * 100) + '%' : '';
  return '<span class="engine-result-badge">' +
    '<span class="engine-result-dot" style="background:' + color + '"></span>' +
    esc(label) + confStr +
  '</span>';
}

// ── Table rendering ─────────────────────────────────────────────────────
function renderTable(rows) {
  $resultsBody.innerHTML = '';
  if (!rows.length) {
    $resultsBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">No results</td></tr>';
    return;
  }
  rows.forEach(function(row) {
    const tr = document.createElement('tr');
    const imgSrc = row.image_name
      ? '/image/' + state.sessionId + '/' + encodeURIComponent(row.image_name)
      : null;

    tr.innerHTML =
      '<td class="img-cell">' +
        (imgSrc
          ? '<div class="thumb-wrap">' +
               '<img class="thumb" src="' + imgSrc + '" alt="' + esc(row.image_name) + '"' +
                    ' data-full="' + imgSrc + '">' +
               '<div class="thumb-missing" style="display:none">no image</div>' +
             '</div>' +
             '<div class="thumb-label">' + esc((row.lang || '').toUpperCase()) + '</div>'
          : '<span style="color:var(--muted)">\u2014</span>') +
      '</td>' +
      '<td class="text-cell">' + formatText(row.ocr_text || '') + '</td>' +
      '<td class="text-cell">' + formatText(row.ref_text || '') + '</td>' +
      '<td>' +
        '<span class="badge badge-' + (row.status || 'manual').toLowerCase() + '">' + esc(row.status || 'MANUAL') + '</span>' +
        (row.section_name
          ? '<span class="badge-section">' + (row.section_number ? row.section_number + '. ' : '') + esc(row.section_name) + '</span>'
          : '') +
        engineBadge(row.ocr_engine, row.ocr_confidence) +
      '</td>' +
      '<td class="review-cell" data-id="' + row.id + '">' +
        renderReviewButtons(row) +
      '</td>';
    $resultsBody.appendChild(tr);
  });

  document.querySelectorAll('.thumb-wrap img.thumb').forEach(function(img) {
    img.addEventListener('error', function() {
      img.style.display = 'none';
      var missing = img.nextElementSibling;
      if (missing) missing.style.display = 'flex';
    });
    img.addEventListener('click', function() { openLightbox(img.dataset.full); });
  });

  document.querySelectorAll('.btn-ok, .btn-err').forEach(function(btn) {
    btn.addEventListener('click', handleDecision);
  });
}

function renderReviewButtons(row) {
  if (row.status === 'PASS' && !row.manual_decision) {
    return '<span style="color:var(--muted);font-size:.75rem">\u2014</span>';
  }
  var dec = row.manual_decision;
  if (dec === 'ok')    return '<span class="decision-ok">\u2713 OK</span>';
  if (dec === 'error') return '<span class="decision-err">\u2717 ERROR</span>';
  return '<button class="btn btn-sm btn-ok" data-id="' + row.id + '" data-action="ok">OK</button>' +
         '<button class="btn btn-sm btn-err" data-id="' + row.id + '" data-action="error">ERROR</button>';
}

async function handleDecision(e) {
  var btn = e.currentTarget;
  var id = btn.dataset.id;
  var action = btn.dataset.action;
  var fd = new FormData();
  fd.append('decision', action);
  await fetch('/decide/' + id, { method: 'POST', body: fd });
  var cell = btn.closest('td');
  cell.innerHTML = action === 'ok'
    ? '<span class="decision-ok">\u2713 OK</span>'
    : '<span class="decision-err">\u2717 ERROR</span>';
  if (action === 'error') $btnDownload.style.display = '';
}

function formatText(text) {
  if (!text) return '<span style="color:var(--muted)">\u2014</span>';
  var lines = text.split('\n').map(function(l) { return l.trim(); }).filter(function(l) { return l.length > 0; });
  if (!lines.length) return '<span style="color:var(--muted)">\u2014</span>';
  return lines.map(function(l) { return '<span class="text-line">' + esc(l) + '</span>'; }).join('');
}

// ── Pagination ──────────────────────────────────────────────────────────
function renderPagination(current, total) {
  $pagination.innerHTML = '';
  if (total <= 1) return;
  function makeBtn(label, page, disabled, active) {
    var btn = document.createElement('button');
    btn.className = 'page-btn' + (active ? ' active' : '');
    btn.textContent = label;
    btn.disabled = disabled;
    if (!disabled) btn.addEventListener('click', function() { state.page = page; loadResults(); });
    return btn;
  }
  $pagination.appendChild(makeBtn('\u00ab', 1, current === 1, false));
  $pagination.appendChild(makeBtn('\u2039', current-1, current === 1, false));
  var start = Math.max(1, current - 2);
  var end   = Math.min(total, current + 2);
  for (var p = start; p <= end; p++) {
    $pagination.appendChild(makeBtn(p, p, false, p === current));
  }
  $pagination.appendChild(makeBtn('\u203a', current+1, current === total, false));
  $pagination.appendChild(makeBtn('\u00bb', total, current === total, false));
}

// ── Controls ────────────────────────────────────────────────────────────
$hidePass.addEventListener('change', function() {
  state.hidePass = $hidePass.checked;
  state.page = 1;
  loadResults();
});

$btnDownload.addEventListener('click', function() {
  if (state.sessionId)
    window.location.href = '/download/' + state.sessionId;
});

$btnNew.addEventListener('click', function() {
  state.sessionId = null;
  state.page = 1;
  state.status = 'idle';
  if (state.timerInterval) clearInterval(state.timerInterval);
  $submitBtn.disabled = true;
  $uploadForm.reset();
  $fileLabelText.textContent = 'Drop ZIP here or click to browse';
  $dropZone.classList.remove('has-file');
  document.querySelectorAll('.engine-chip').forEach(function(c) { c.classList.remove('selected'); });
  var firstChip = document.querySelectorAll('.engine-chip')[0];
  if (firstChip) firstChip.classList.add('selected');
  var firstRadio = document.querySelectorAll('.engine-chip input')[0];
  if (firstRadio) firstRadio.checked = true;
  $summarySection.style.display = 'none';
  $resultsSection.style.display = 'none';
  $progressSection.style.display = 'none';
  $uploadSection.style.display = '';
  $resultsBody.innerHTML = '';
  $pagination.innerHTML = '';
  updateSubmitBtn();
});

// ── Lightbox ────────────────────────────────────────────────────────────
function openLightbox(src) {
  $lightboxImg.src = src;
  $lightbox.classList.add('open');
}
function closeLightbox() {
  $lightbox.classList.remove('open');
}
$lightbox.addEventListener('click', function(e) {
  if (e.target === $lightbox) closeLightbox();
});
$lightboxClose.addEventListener('click', closeLightbox);
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeLightbox();
});

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
  var div = document.createElement('div');
  div.className = 'error-msg';
  div.textContent = '\u26a0 ' + msg;
  $uploadSection.parentNode.insertBefore(div, $uploadSection.nextSibling);
  setTimeout(function() { div.remove(); }, 8000);
  $submitBtn.disabled = false;
}
