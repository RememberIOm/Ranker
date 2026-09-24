// 랭킹 화면: 항목 메뉴, 이름 편집, 점수 분포 차트.

pageActions['toggle-rank-edit'] = function (el) {
  closeAllDropdowns();
  toggleRow('rank-view-' + el.dataset.id, 'rank-edit-' + el.dataset.id, 'input[name="new_name"]');
};
pageActions['toggle-menu'] = function (el) { toggleDropdown(el); };

// 메뉴를 body로 옮겨 스크롤되는 표에 잘리지 않게 합니다.
function closeAllDropdowns(restoreFocus) {
  document.querySelectorAll('[role="menu"]').forEach(function (menu) {
    if (menu.classList.contains('hidden')) return;
    menu.classList.add('hidden');
    var anchor = menu._anchor;
    if (anchor) {
      anchor.setAttribute('aria-expanded', 'false');
      anchor.insertAdjacentElement('afterend', menu);
      if (restoreFocus) anchor.focus();
    }
  });
}
function toggleDropdown(btn) {
  var menu = btn._menu || btn.nextElementSibling;
  var wasOpen = menu && !menu.classList.contains('hidden');
  closeAllDropdowns();
  if (!menu || wasOpen) return;
  btn._menu = menu;
  menu._anchor = btn;
  document.body.appendChild(menu);
  menu.classList.remove('hidden');
  btn.setAttribute('aria-expanded', 'true');
  var anchor = btn.getBoundingClientRect();
  menu.style.position = 'fixed';
  menu.style.right = 'auto';
  menu.style.bottom = 'auto';
  menu.style.margin = '0';
  menu.style.left = Math.max(8, Math.min(anchor.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(anchor.bottom + 4, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  menu.querySelector('[role="menuitem"]').focus();
}
document.addEventListener('click', function (e) {
  if (e.target.closest('[aria-haspopup]') || e.target.closest('[role="menu"]')) return;
  closeAllDropdowns();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeAllDropdowns(true); return; }
  var trigger = e.target.closest('[aria-haspopup]');
  if (trigger && e.key === 'ArrowDown') { e.preventDefault(); toggleDropdown(trigger); return; }
  var menu = e.target.closest('[role="menu"]');
  if (!menu || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
  e.preventDefault();
  var choices = Array.from(menu.querySelectorAll('[role="menuitem"]'));
  var index = choices.indexOf(document.activeElement);
  if (e.key === 'Home') index = 0;
  else if (e.key === 'End') index = choices.length - 1;
  else index = (index + (e.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
  choices[index].focus();
});
window.addEventListener('resize', function () { closeAllDropdowns(); });
document.addEventListener('scroll', function () { closeAllDropdowns(); }, true);

(function () {
  var chartData = JSON.parse(document.getElementById('chart-data').textContent);
  var chartInstance = null;
  function renderChart() {
    var canvas = document.getElementById('distributionChart');
    if (!canvas || typeof Chart === 'undefined') return;
    if (chartInstance) chartInstance.destroy();
    var isDark = document.documentElement.classList.contains('dark');
    var gridColor = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)';
    var tickColor = isDark ? '#a1a1aa' : '#71717a';
    chartInstance = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels: chartData.labels,
        datasets: [{
          data: chartData.counts,
          borderColor: chartData.color,
          backgroundColor: chartData.color + '22',
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        interaction: { mode: 'nearest', axis: 'x', intersect: false },
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        scales: {
          x: { grid: { color: gridColor }, ticks: { color: tickColor } },
          y: { beginAtZero: true, grid: { color: gridColor }, ticks: { color: tickColor, precision: 0 } }
        }
      }
    });
  }
  renderChart();
  window.addEventListener('theme-changed', renderChart);
})();
