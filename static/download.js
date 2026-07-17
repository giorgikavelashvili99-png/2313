(function () {
  const countdownEl = document.getElementById('countdown');
  const downloadBtn = document.getElementById('downloadBtn');
  const downloadStatus = document.getElementById('downloadStatus');
  const moonShadow = document.getElementById('moonShadow');

  const fileId = window.MOONFADE_FILE_ID;
  const expiresAt = new Date(window.MOONFADE_EXPIRES_AT + 'Z');
  const TOTAL_WINDOW_MS = 6 * 3600000; // matches server EXPIRY_HOURS default; cosmetic only

  function tick() {
    const now = new Date();
    const diff = expiresAt - now;
    if (diff <= 0) {
      countdownEl.textContent = 'ვადა ამოიწურა';
      moonShadow.style.width = '100%';
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'ვადა გასულია';
      clearInterval(timer);
      return;
    }
    const hours = Math.floor(diff / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    countdownEl.textContent = `ქრება: ${hours}სთ ${mins}წთ ${secs}წმ`;
    const elapsedRatio = 1 - Math.min(1, diff / TOTAL_WINDOW_MS);
    moonShadow.style.width = (elapsedRatio * 100) + '%';
  }

  const timer = setInterval(tick, 1000);
  tick();

  downloadBtn.addEventListener('click', () => {
    downloadStatus.textContent = 'მზადდება…';
    fetch(`/api/download-url/${fileId}`)
      .then(r => {
        if (!r.ok) throw new Error('expired');
        return r.json();
      })
      .then(data => {
        downloadStatus.textContent = '';
        window.location.href = data.url;
      })
      .catch(() => {
        downloadStatus.textContent = 'ბმულს ვადა გაუვიდა';
        downloadBtn.disabled = true;
      });
  });
})();
