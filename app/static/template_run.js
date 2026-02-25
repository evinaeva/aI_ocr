/* Phase 4: Run page JS */

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

  try {
    const resp = await fetch(
      '/api/templates/' + encodeURIComponent(TEMPLATE_NAME) + '/run',
      { method: 'POST', body: formData }
    );
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
  card.appendChild(body);
  return card;
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
