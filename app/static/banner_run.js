/* Banner QA runner — local CV pipeline. */

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showError(msg) {
  const el = document.getElementById('alert-error');
  el.textContent = msg;
  el.classList.add('show');
}

function clearError() {
  document.getElementById('alert-error').classList.remove('show');
}

function populateFontDropdowns() {
  const familySelect = document.getElementById('family-input');
  const weightSelect = document.getElementById('weight-input');
  const families = Object.keys(FONT_CATALOG);
  for (const family of families) {
    const opt = document.createElement('option');
    opt.value = family;
    opt.textContent = family;
    familySelect.appendChild(opt);
  }
  familySelect.addEventListener('change', () => populateWeights(familySelect.value));
  populateWeights(families[0]);
}

function populateWeights(family) {
  const weightSelect = document.getElementById('weight-input');
  weightSelect.innerHTML = '';
  for (const weight of FONT_CATALOG[family] || []) {
    const opt = document.createElement('option');
    opt.value = weight;
    opt.textContent = weight;
    weightSelect.appendChild(opt);
  }
}

async function runBannerQA() {
  clearError();
  const fileInput = document.getElementById('image-input');
  const refInput = document.getElementById('ref-input');
  if (!fileInput.files.length) { showError('Please select a banner image.'); return; }
  if (!refInput.value.trim()) { showError('Please enter the reference text.'); return; }

  const btn = document.getElementById('run-btn');
  const spinner = document.getElementById('spinner');
  btn.disabled = true;
  spinner.style.display = 'inline';
  document.getElementById('results-section').style.display = 'none';

  const formData = new FormData();
  formData.append('image', fileInput.files[0]);
  formData.append('reference_text', refInput.value.trim());
  formData.append('family', document.getElementById('family-input').value);
  formData.append('weight', document.getElementById('weight-input').value);
  const lang = document.getElementById('lang-input').value.trim();
  if (lang) formData.append('language', lang);
  const threshold = document.getElementById('threshold-input').value.trim();
  if (threshold) formData.append('threshold', threshold);

  try {
    const resp = await fetch('/api/banner/qa', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      showError(data.error || ('HTTP ' + resp.status));
      return;
    }
    renderResults(data);
  } catch (e) {
    showError('Request failed: ' + e.message);
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

function renderResults(data) {
  const meta = document.getElementById('run-meta');
  const status = data.overall_status || 'unknown';
  const badge = status === 'ok' ? 'badge-ok' : 'badge-manual';
  meta.innerHTML =
    '<span class="badge ' + badge + '">' + escHtml(status.toUpperCase()) + '</span>' +
    '<span style="margin-left:12px;">run_id: <code>' + escHtml(data.run_id) + '</code></span>' +
    '<span style="margin-left:12px;color:var(--text-secondary);">' +
        'blocks: ' + (data.blocks ? data.blocks.length : 0) + ' · ' +
        'font: ' + escHtml(data.font.family + ' ' + data.font.weight) +
    '</span>';

  const container = document.getElementById('blocks-container');
  container.innerHTML = '';
  for (const block of data.blocks || []) {
    container.appendChild(buildBlockCard(block, data.run_id));
  }
  document.getElementById('results-section').style.display = 'block';
}

function buildBlockCard(block, runId) {
  const c = block.compare;
  const card = document.createElement('div');
  card.className = 'zone-card';

  const flagged = c.flagged;
  const badgeClass = flagged ? 'badge-manual' : 'badge-ok';
  const statusText = flagged ? 'FLAG' : 'OK';

  const header = document.createElement('div');
  header.className = 'zone-header';
  header.innerHTML =
    '<span class="zone-name">block #' + block.idx + '</span>' +
    '<span class="badge ' + badgeClass + '">' + statusText + '</span>' +
    '<span style="font-size:0.8rem;color:#6b7280;">bbox: ' +
      block.bbox.join(', ') + '</span>';
  card.appendChild(header);

  const body = document.createElement('div');
  body.className = 'zone-body';

  const scoreRow = document.createElement('div');
  scoreRow.className = 'consensus-row';
  scoreRow.innerHTML =
    '<div><div class="label">score</div><span class="consensus-text">' + c.score.toFixed(3) + '</span></div>' +
    '<div><div class="label">iou</div><span class="consensus-text">' + c.iou.toFixed(3) + '</span></div>' +
    '<div><div class="label">chamfer (px)</div><span class="consensus-text">' + c.chamfer.toFixed(2) + '</span></div>' +
    '<div><div class="label">column_sim</div><span class="consensus-text">' + c.column_sim.toFixed(3) + '</span></div>';
  body.appendChild(scoreRow);

  if (block.viz_path) {
    const img = document.createElement('img');
    img.src = '/viz/' + encodeURIComponent(runId) + '/' + encodeURIComponent(block.viz_path);
    img.alt = 'block ' + block.idx + ' visualization';
    img.style.maxWidth = '100%';
    img.style.marginTop = '12px';
    img.style.border = '1px solid var(--border)';
    img.style.borderRadius = 'var(--radius)';
    body.appendChild(img);
  }

  card.appendChild(body);
  return card;
}

document.addEventListener('DOMContentLoaded', populateFontDropdowns);
