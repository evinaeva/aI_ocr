/* Phase 4 + Phase 5: Run page JS */

async function runTemplate() {
  const input = document.getElementById('image-input');
  if (!input.files.length) {
    showError('Please select an image file.');
    return;
  }

  const btn = document.getElementById('run-btn');
  const spinner = document.getElementById('spinner');
  btn.disabled = true;
  spinner.style.display = 'inline';
  document.getElementById('results-section').style.display = 'none';
  document.getElementById('alert-error').classList.remove('show');

  const formData = new FormData();
  formData.append('image', input.files[0]);

  // Phase 5: read optional lang param
  const langInput = document.getElementById('lang-input');
  const lang = langInput ? langInput.value.trim() : '';

  let url = '/api/templates/' + encodeURIComponent(TEMPLATE_NAME) + '/run';
  if (lang) {
    url += '?lang=' + encodeURIComponent(lang);
  }

  try {
    const resp = await fetch(url, { method: 'POST', body: formData });
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
  document.getElementById('run-meta').textContent =
    'run_id: ' + data.run_id + '  |  template: ' + data.template_name;

  const container = document.getElementById('zones-container');
  container.innerHTML = '';

  for (const zone of (data.zones || [])) {
    container.appendChild(buildZoneCard(zone));
  }

  document.getElementById('results-section').style.display = 'block';
}

function buildZoneCard(zone) {
  const consensus = zone.consensus || {};
  const status = consensus.zone_status || 'MANUAL';
  const badgeClass = status === 'OK' ? 'badge-ok' : 'badge-manual';

  const card = document.createElement('div');
  card.className = 'zone-card';

  // Header
  const header = document.createElement('div');
  header.className = 'zone-header';
  header.innerHTML =
    '<span class="zone-name">' + escHtml(zone.zone_name) + '</span>' +
    '<span class="badge ' + badgeClass + '">' + escHtml(status) + '</span>';
  if (consensus.reason) {
    header.innerHTML += '<span style="font-size:0.8rem;color:#6b7280;">' + escHtml(consensus.reason) + '</span>';
  }
  card.appendChild(header);

  // Body
  const body = document.createElement('div');
  body.className = 'zone-body';

  // Engine chips
  const chipsDiv = document.createElement('div');
  chipsDiv.className = 'engine-results';

  const engineResults = zone.engine_results || [];
  const selectedEngine = consensus.selected_engine;

  if (engineResults.length === 0 && (consensus.reason === 'no_engines_configured' || consensus.reason === 'all_engines_failed')) {
    chipsDiv.innerHTML = '<span style="color:#6b7280;font-size:0.85rem;">selected_engine: -&nbsp;&nbsp;selected_text: -</span>';
  } else {
    for (const er of engineResults) {
      const isSelected = er.engine === selectedEngine;
      const chip = document.createElement('div');
      chip.className = 'engine-chip' + (isSelected ? ' selected' : '');
      let inner = '<span class="eng-name">' + escHtml(er.engine) + '</span>';
      if (er.error) {
        inner += ' <span class="eng-err">✗ ' + escHtml(er.error) + '</span>';
      } else {
        const conf = er.confidence !== null ? (er.confidence * 100).toFixed(0) + '%' : 'n/a';
        inner += ' <span class="eng-conf">' + conf + '</span>';
      }
      chip.innerHTML = inner;
      chipsDiv.appendChild(chip);
    }
  }
  body.appendChild(chipsDiv);

  // Consensus text
  const consRow = document.createElement('div');
  consRow.className = 'consensus-row';

  const selText = (consensus.selected_text === null || consensus.selected_text === undefined)
    ? '-' : consensus.selected_text;
  const selEngine = consensus.selected_engine || '-';
  const rule = consensus.rule_used || '-';

  consRow.innerHTML =
    '<div><div class="label">selected_engine</div><span class="consensus-text">' + escHtml(selEngine) + '</span></div>' +
    '<div><div class="label">rule_used</div><span class="consensus-text">' + escHtml(rule) + '</span></div>' +
    '<div style="flex:1;min-width:160px;"><div class="label">selected_text</div><span class="consensus-text">' + escHtml(selText) + '</span></div>';

  body.appendChild(consRow);

  // Phase 5: validation block
  const vBlock = zone.validation;
  if (vBlock) {
    body.appendChild(buildValidationBlock(vBlock, consensus));
  }

  card.appendChild(body);
  return card;
}

function buildValidationBlock(v, consensus) {
  const div = document.createElement('div');

  if (!v.validation_applied) {
    div.className = v.skip_reason === 'similarity_error'
      ? 'validation-block error'
      : 'validation-block skip';

    let msg;
    if (v.skip_reason === 'lang_missing') {
      msg = 'Validation not applied: lang not provided';
    } else if (v.skip_reason === 'expected_text_missing') {
      msg = 'Validation not applied: expected text missing';
    } else if (v.skip_reason === 'similarity_error') {
      msg = 'Validation error during similarity computation';
    } else {
      msg = 'Validation not applied';
    }
    div.textContent = msg;
    return div;
  }

  // Computed case
  const sim = v.similarity;
  const threshold = v.threshold;
  const isLow = sim < threshold;

  // Check if zone is MANUAL for a DIFFERENT reason (not low_similarity)
  const differentManualReason = (
    consensus.zone_status === 'MANUAL' &&
    consensus.reason &&
    consensus.reason !== 'low_similarity'
  );

  div.className = 'validation-block computed' + (isLow ? ' low' : '');

  const simPct = (sim * 100).toFixed(2) + '%';
  const threshPct = (threshold * 100).toFixed(0) + '%';
  const simClass = isLow ? 'sim-value low' : 'sim-value ok';

  let html =
    '<div><span class="validation-label">Similarity: </span>' +
    '<span class="' + simClass + '">' + escHtml(simPct) + '</span>' +
    ' <span class="validation-label">(threshold: ' + escHtml(threshPct) + ')</span></div>';

  html +=
    '<div class="validation-row">' +
    '<div><div class="validation-label">expected_text</div><span class="vtext">' + escHtml(v.expected_text) + '</span></div>' +
    '<div><div class="validation-label">normalized_ocr</div><span class="vtext">' + escHtml(v.normalized_ocr) + '</span></div>' +
    '<div><div class="validation-label">normalized_expected</div><span class="vtext">' + escHtml(v.normalized_expected) + '</span></div>' +
    '</div>';

  if (differentManualReason) {
    html += '<div style="margin-top:0.4rem;font-size:0.8rem;color:#6b7280;">Note: zone is MANUAL due to &ldquo;' +
      escHtml(consensus.reason) + '&rdquo;, not similarity.</div>';
  }

  div.innerHTML = html;
  return div;
}

function showError(msg) {
  const el = document.getElementById('alert-error');
  el.textContent = msg;
  el.classList.add('show');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
