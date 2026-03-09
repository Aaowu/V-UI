function readJsonScript(id) {
  const node = document.getElementById(id);
  if (!node) return null;
  try {
    return JSON.parse(node.textContent);
  } catch {
    return null;
  }
}

function copyText(text, button) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const old = button.textContent;
    button.textContent = '已复制';
    setTimeout(() => { button.textContent = old; }, 1500);
  }).catch(() => {
    alert('复制失败，请手动复制');
  });
}

function bindCopyButtons() {
  document.querySelectorAll('.copy-btn').forEach((button) => {
    button.addEventListener('click', () => {
      const direct = button.dataset.copy;
      const target = button.dataset.copyTarget;
      const text = direct || (target ? document.querySelector(target)?.value : '') || '';
      copyText(text, button);
    });
  });
}

function bindQrButtons() {
  const modal = document.getElementById('qr-modal');
  if (!modal || typeof QRious === 'undefined') return;
  const title = document.getElementById('qr-title');
  const subtitle = document.getElementById('qr-subtitle');
  const textInput = document.getElementById('qr-text');
  const qr = new QRious({ element: document.getElementById('qr-canvas'), size: 260, value: '' });
  const openModal = (text, label) => {
    qr.value = text;
    title.textContent = label || '二维码';
    subtitle.textContent = '使用客户端扫码导入，或先复制下方内容。';
    textInput.value = text;
    modal.classList.remove('hidden');
  };
  const closeModal = () => modal.classList.add('hidden');
  document.querySelectorAll('.qr-btn').forEach((button) => {
    button.addEventListener('click', () => openModal(button.dataset.qrText || '', button.dataset.qrTitle || '二维码'));
  });
  document.querySelectorAll('[data-close-modal]').forEach((button) => button.addEventListener('click', closeModal));
}

function bindMobileMenu() {
  const body = document.body;
  const button = document.getElementById('mobile-menu-btn');
  const sidebar = document.getElementById('mobile-sidebar');
  const backdrop = document.getElementById('mobile-nav-backdrop');
  if (!button || !sidebar || !backdrop) return;

  const closeMenu = () => {
    body.classList.remove('mobile-nav-open');
    backdrop.classList.add('hidden');
    button.setAttribute('aria-expanded', 'false');
  };

  const openMenu = () => {
    body.classList.add('mobile-nav-open');
    backdrop.classList.remove('hidden');
    button.setAttribute('aria-expanded', 'true');
  };

  button.addEventListener('click', () => {
    if (body.classList.contains('mobile-nav-open')) closeMenu();
    else openMenu();
  });

  backdrop.addEventListener('click', closeMenu);
  sidebar.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeMenu));
  window.addEventListener('resize', () => {
    if (window.innerWidth > 880) closeMenu();
  });
}

function createUsageLineChart(canvas, usage) {
  if (!canvas || typeof Chart === 'undefined') return;
  const labels = usage?.labels || [];
  const uplink = usage?.uplink || [];
  const downlink = usage?.downlink || [];
  new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: '上传', data: uplink, borderColor: '#7fb0ff', backgroundColor: 'rgba(127, 176, 255, 0.18)', fill: true, tension: 0.35, borderWidth: 2, pointRadius: 2 },
        { label: '下载', data: downlink, borderColor: '#2d74ff', backgroundColor: 'rgba(45, 116, 255, 0.10)', fill: true, tension: 0.35, borderWidth: 2, pointRadius: 2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom' } },
      scales: {
        x: { grid: { color: '#eef4ff' }, ticks: { color: '#6a86ad' } },
        y: { grid: { color: '#eef4ff' }, ticks: { color: '#6a86ad' } },
      },
    },
  });
}

function createLinkShareChart(canvas, usage) {
  if (!canvas || typeof Chart === 'undefined') return;
  const rawLabels = usage?.labels || [];
  const rawUsed = usage?.used || [];
  const filtered = rawLabels.map((label, index) => ({ label, value: rawUsed[index] || 0 })).filter((item) => item.value > 0);
  const labels = filtered.length ? filtered.map((item) => item.label) : ['暂无流量'];
  const data = filtered.length ? filtered.map((item) => item.value) : [1];
  const colors = ['#4d86ff', '#79a8ff', '#a8c7ff', '#bfd8ff', '#d7e8ff', '#8ab8ff'];
  new Chart(canvas, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: labels.map((_, i) => colors[i % colors.length]), borderColor: '#ffffff', borderWidth: 4, hoverOffset: 6 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: '62%', plugins: { legend: { position: 'bottom' } } },
  });
}

function bindCharts() {
  const payload = readJsonScript('overview-charts');
  if (!payload) return;
  createUsageLineChart(document.getElementById('usage-line-chart'), payload.usage_line);
  createLinkShareChart(document.getElementById('link-usage-chart'), payload.link_usage);
}

document.addEventListener('DOMContentLoaded', () => {
  bindCopyButtons();
  bindQrButtons();
  bindMobileMenu();
  bindCharts();
});
