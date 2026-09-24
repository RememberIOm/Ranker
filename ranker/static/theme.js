// 첫 화면을 그리기 전에 다크 테마를 적용해 깜빡임을 막습니다.
(function () {
  try {
    var t = localStorage.getItem('color-theme');
    if (t === 'dark' || (!t && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.classList.add('dark');
    }
  } catch (e) { /* storage unavailable */ }
})();
