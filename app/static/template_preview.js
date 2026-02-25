/* Phase 3 — Preview Crops UI logic */
(function () {
  var tmplSelect = document.getElementById('tmpl-select');
  var imgInput   = document.getElementById('img-input');
  var btn        = document.getElementById('generate-btn');
  var statusMsg  = document.getElementById('status-msg');
  var spinner    = document.getElementById('spinner');

  function updateBtn() {
    btn.disabled = !(tmplSelect.value && imgInput.files.length > 0);
  }
  tmplSelect.addEventListener('change', updateBtn);
  imgInput.addEventListener('change', updateBtn);

  window.generatePreview = async function () {
    statusMsg.textContent = '';
    document.getElementById('results').style.display = 'none';
    spinner.style.display = 'block';
    btn.disabled = true;

    var tmpl = tmplSelect.value;
    var fd = new FormData();
    fd.append('image', imgInput.files[0]);

    try {
      var resp = await fetch(
        '/api/templates/' + encodeURIComponent(tmpl) + '/preview-crops',
        { method: 'POST', body: fd }
      );
      var data = await resp.json();
      if (!resp.ok) {
        statusMsg.textContent = 'Error: ' + (data.details || data.error || resp.statusText);
        return;
      }
      renderResults(data);
    } catch (err) {
      statusMsg.textContent = 'Network error: ' + err.message;
    } finally {
      spinner.style.display = 'none';
      btn.disabled = false;
    }
  };

  function renderResults(data) {
    var metaBar = document.getElementById('meta-bar');
    metaBar.innerHTML = [
      chip('template', data.template_name),
      chip('source_size', data.source_size[0] + '\u00d7' + data.source_size[1]),
      chip('original_size', data.original_size[0] + '\u00d7' + data.original_size[1]),
      chip('processed_size', data.processed_size[0] + '\u00d7' + data.processed_size[1]),
      chip('upscaled', String(data.upscaled), data.upscaled),
    ].join('');

    var container = document.getElementById('zones-container');
    container.innerHTML = '';
    if (!data.zones || data.zones.length === 0) {
      container.innerHTML = '<p style="color:#6b7280;font-size:0.9rem;">No zones in this template.</p>';
    }
    for (var i = 0; i < data.zones.length; i++) {
      var zone = data.zones[i];
      var div = document.createElement('div');
      div.className = 'zone-card';
      var bs  = zone.bbox_source;
      var bsc = zone.bbox_scaled;
      div.innerHTML =
        '<h3>' + esc(zone.zone_name) + '</h3>' +
        '<img src="data:image/png;base64,' + zone.crop_png_base64 + '" alt="' + esc(zone.zone_name) + ' crop" />' +
        '<div class="zone-meta">' +
          'source: [' + bs.join(', ') + ']<br>' +
          'scaled: [' + bsc.join(', ') + ']' +
        '</div>';
      container.appendChild(div);
    }
    document.getElementById('results').style.display = 'block';
  }

  function chip(label, value, highlight) {
    var cls = 'meta-chip' + (highlight ? ' upscaled' : '');
    return '<span class="' + cls + '">' + esc(label) + ': <b>' + esc(value) + '</b></span>';
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}());
