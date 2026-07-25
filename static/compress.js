(function () {
  const moon = document.getElementById('moon');
  const fileInput = document.getElementById('fileInput');

  const settingsPanel = document.getElementById('settingsPanel');
  const settingsFileName = document.getElementById('settingsFileName');
  const codecSelect = document.getElementById('codecSelect');
  const fpsSelect = document.getElementById('fpsSelect');
  const targetInput = document.getElementById('targetInput');
  const startCompressBtn = document.getElementById('startCompressBtn');

  const progressPanel = document.getElementById('progressPanel');
  const fileNameEl = document.getElementById('fileName');
  const progressFill = document.getElementById('progressFill');
  const statusText = document.getElementById('statusText');

  const resultPanel = document.getElementById('resultPanel');
  const origSizeEl = document.getElementById('origSize');
  const newSizeEl = document.getElementById('newSize');
  const reductionBadge = document.getElementById('reductionBadge');
  const downloadResultBtn = document.getElementById('downloadResultBtn');
  const countdownEl = document.getElementById('countdown');

  const errorPanel = document.getElementById('errorPanel');
  const errorText = document.getElementById('errorText');

  let selectedFile = null;
  let pollTimer = null;
  let expiresAt = null;
  let countdownTimer = null;

  moon.addEventListener('click', () => fileInput.click());

  ['dragenter', 'dragover'].forEach(evt =>
    moon.addEventListener(evt, e => { e.preventDefault(); moon.classList.add('dragover'); })
  );
  ['dragleave', 'drop'].forEach(evt =>
    moon.addEventListener(evt, e => { e.preventDefault(); moon.classList.remove('dragover'); })
  );
  moon.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) selectFile(f);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) selectFile(fileInput.files[0]);
  });

  function hideAllPanels() {
    settingsPanel.hidden = true;
    progressPanel.hidden = true;
    resultPanel.hidden = true;
    errorPanel.hidden = true;
  }

  function formatBytes(bytes) {
    const mb = bytes / (1024 * 1024);
    return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(2) + ' MB';
  }

  function selectFile(file) {
    if (pollTimer) clearInterval(pollTimer);
    if (countdownTimer) clearInterval(countdownTimer);

    selectedFile = file;
    hideAllPanels();
    settingsFileName.textContent = `${file.name}  ·  ${formatBytes(file.size)}`;
    settingsPanel.hidden = false;
  }

  startCompressBtn.addEventListener('click', () => {
    if (!selectedFile) return;
    const targetMb = parseFloat(targetInput.value) || 99;
    startUploadThenCompress(selectedFile, targetMb, codecSelect.value, fpsSelect.value);
  });

  function startUploadThenCompress(file, targetMb, codec, fps) {
    hideAllPanels();
    progressPanel.hidden = false;
    fileNameEl.textContent = file.name;
    statusText.textContent = 'იტვირთება სერვერზე…';
    progressFill.style.width = '0%';

    fetch('/api/request-upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size: file.size,
      }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) throw new Error(data.error);
        uploadToR2(file, data.upload_url, () =>
          beginCompression(data.file_id, targetMb, codec, fps)
        );
      })
      .catch(err => showError('ატვირთვის შეცდომა: ' + err.message));
  }

  function uploadToR2(file, uploadUrl, onDone) {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', uploadUrl);
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');

    // uploading counts as the first 40% of the bar — compression fills the rest
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const uploadPct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = Math.round(uploadPct * 0.4) + '%';
        statusText.textContent = `იტვირთება: ${uploadPct}%`;
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) onDone();
      else showError('ატვირთვა ვერ შესრულდა');
    };
    xhr.onerror = () => showError('კავშირის შეცდომა ატვირთვისას');
    xhr.send(file);
  }

  function beginCompression(fileId, targetMb, codec, fps) {
    statusText.textContent = 'შეკუმშვა იწყება…';
    fetch('/api/compress/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: fileId, target_mb: targetMb, codec, fps }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) throw new Error(data.error);
        pollStatus(data.job_id);
      })
      .catch(err => showError('ვერ დაიწყო შეკუმშვა: ' + err.message));
  }

  function pollStatus(jobId) {
    pollTimer = setInterval(() => {
      fetch(`/api/compress/status/${jobId}`)
        .then(r => r.json())
        .then(data => {
          if (data.error || data.status === 'error') {
            clearInterval(pollTimer);
            showError(data.error || 'შეკუმშვა ვერ შესრულდა');
            return;
          }

          const barPct = 40 + Math.round((data.progress || 0) * 0.6);
          progressFill.style.width = Math.min(barPct, 99) + '%';
          statusText.textContent = data.message || 'მიმდინარეობს…';

          if (data.status === 'done') {
            clearInterval(pollTimer);
            progressFill.style.width = '100%';
            showResult(data);
          }
        })
        .catch(() => { /* transient network hiccup — next tick retries */ });
    }, 1200);
  }

  function showResult(data) {
    hideAllPanels();
    resultPanel.hidden = false;
    origSizeEl.textContent = formatBytes(data.original_size || 0);
    newSizeEl.textContent = formatBytes(data.output_size || 0);
    const reduction = data.original_size
      ? Math.max(0, Math.round((1 - data.output_size / data.original_size) * 100))
      : 0;
    reductionBadge.textContent = `-${reduction}%`;
    downloadResultBtn.href = data.download_page;

    fetch(`/api/status/${data.file_id}`)
      .then(r => r.json())
      .then(info => {
        if (info.expires_at) {
          expiresAt = new Date(info.expires_at + 'Z');
          startCountdown();
        }
      })
      .catch(() => {});
  }

  function startCountdown() {
    if (countdownTimer) clearInterval(countdownTimer);
    updateCountdown();
    countdownTimer = setInterval(updateCountdown, 1000);
  }

  function updateCountdown() {
    const diff = expiresAt - new Date();
    if (diff <= 0) {
      countdownEl.textContent = 'ვადა ამოიწურა';
      clearInterval(countdownTimer);
      return;
    }
    const hours = Math.floor(diff / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    countdownEl.textContent = `ქრება: ${hours}სთ ${mins}წთ ${secs}წმ`;
  }

  function showError(msg) {
    if (pollTimer) clearInterval(pollTimer);
    hideAllPanels();
    errorPanel.hidden = false;
    errorText.textContent = msg;
  }
})();
