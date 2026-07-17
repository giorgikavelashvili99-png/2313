(function () {
  const moon = document.getElementById('moon');
  const fileInput = document.getElementById('fileInput');
  const moonShadow = document.getElementById('moonShadow');
  const moonLabel = document.getElementById('moonLabel');
  const progressPanel = document.getElementById('progressPanel');
  const resultPanel = document.getElementById('resultPanel');
  const fileNameEl = document.getElementById('fileName');
  const progressFill = document.getElementById('progressFill');
  const statusText = document.getElementById('statusText');
  const linkOutput = document.getElementById('linkOutput');
  const copyBtn = document.getElementById('copyBtn');
  const countdownEl = document.getElementById('countdown');
  const emailInput = document.getElementById('emailInput');
  const sendEmailBtn = document.getElementById('sendEmailBtn');
  const emailStatus = document.getElementById('emailStatus');

  let expiresAt = null;
  let countdownTimer = null;
  let currentFileId = null;

  moon.addEventListener('click', () => fileInput.click());

  ['dragenter', 'dragover'].forEach(evt =>
    moon.addEventListener(evt, e => { e.preventDefault(); moon.classList.add('dragover'); })
  );
  ['dragleave', 'drop'].forEach(evt =>
    moon.addEventListener(evt, e => { e.preventDefault(); moon.classList.remove('dragover'); })
  );
  moon.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) handleFile(fileInput.files[0]);
  });

  function handleFile(file) {
    moonLabel.style.display = 'none';
    progressPanel.hidden = false;
    resultPanel.hidden = true;
    fileNameEl.textContent = file.name;
    statusText.textContent = 'იტვირთება…';
    progressFill.style.width = '0%';

    fetch('/api/request-upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size: file.size
      })
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) throw new Error(data.error);
        currentFileId = data.file_id;
        uploadToR2(file, data.upload_url);
      })
      .catch(err => {
        statusText.textContent = 'შეცდომა ატვირთვისას: ' + err.message;
      });
  }

  function uploadToR2(file, uploadUrl) {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', uploadUrl);
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = pct + '%';
        statusText.textContent = pct + '%';
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        finalizeUpload();
      } else {
        statusText.textContent = 'ატვირთვა ვერ შესრულდა (სცადეთ თავიდან)';
      }
    };
    xhr.onerror = () => {
      statusText.textContent = 'კავშირის შეცდომა ატვირთვისას';
    };
    xhr.send(file);
  }

  function finalizeUpload() {
    statusText.textContent = 'ბმულის მომზადება…';
    fetch('/api/finalize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: currentFileId })
    })
      .then(r => r.json())
      .then(data => {
        progressPanel.hidden = true;
        resultPanel.hidden = false;
        linkOutput.value = data.link;
        expiresAt = new Date(data.expires_at + 'Z');
        startCountdown();
      });
  }

  function startCountdown() {
    if (countdownTimer) clearInterval(countdownTimer);
    updateCountdown();
    countdownTimer = setInterval(updateCountdown, 1000);
  }

  function updateCountdown() {
    const now = new Date();
    const diff = expiresAt - now;
    if (diff <= 0) {
      countdownEl.textContent = 'ვადა ამოიწურა';
      moonShadow.style.width = '100%';
      clearInterval(countdownTimer);
      return;
    }
    const totalMs = expiresAt - (expiresAt - diff);
    const hours = Math.floor(diff / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    countdownEl.textContent = `ქრება: ${hours}სთ ${mins}წთ ${secs}წმ`;

    // moon shadow grows as time elapses (assumes fixed window from server config)
    const totalWindowMs = window.MOONFADE_TOTAL_MS || (6 * 3600000);
    const elapsedRatio = 1 - Math.min(1, diff / totalWindowMs);
    moonShadow.style.width = (elapsedRatio * 100) + '%';
  }

  copyBtn.addEventListener('click', () => {
    linkOutput.select();
    navigator.clipboard.writeText(linkOutput.value).then(() => {
      copyBtn.textContent = 'დაკოპირდა';
      setTimeout(() => (copyBtn.textContent = 'კოპირება'), 1500);
    });
  });

  sendEmailBtn.addEventListener('click', () => {
    const email = emailInput.value.trim();
    if (!email) return;
    emailStatus.textContent = 'იგზავნება…';
    fetch('/api/finalize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: currentFileId, email })
    })
      .then(r => r.json())
      .then(() => {
        emailStatus.textContent = 'გაიგზავნა ' + email + '-ზე';
      })
      .catch(() => {
        emailStatus.textContent = 'ვერ გაიგზავნა — სცადეთ თავიდან';
      });
  });
})();
