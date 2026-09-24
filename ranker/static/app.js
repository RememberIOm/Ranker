// 모든 페이지에서 쓰는 테마 전환, 알림, 확인 창, HTMX 오류 처리와 data-action 처리.

/* ============ theme toggle ============ */
(function () {
  var btn = document.getElementById('theme-toggle');
  var lightIcon = document.getElementById('theme-toggle-light-icon');
  var darkIcon = document.getElementById('theme-toggle-dark-icon');
  function syncIcons() {
    var isDark = document.documentElement.classList.contains('dark');
    lightIcon.classList.toggle('hidden', !isDark);
    darkIcon.classList.toggle('hidden', isDark);
  }
  syncIcons();
  btn.addEventListener('click', function () {
    var isDark = document.documentElement.classList.toggle('dark');
    try { localStorage.setItem('color-theme', isDark ? 'dark' : 'light'); } catch (e) {}
    syncIcons();
    window.dispatchEvent(new Event('theme-changed'));
  });
})();

/* ============ active nav marking ============ */
document.querySelectorAll('#desktop-nav a[data-path], #mobile-nav a[data-path]').forEach(function (a) {
  if (location.pathname.indexOf(a.dataset.path) === 0) {
    a.classList.add('nav-active');
    a.setAttribute('aria-current', 'page');
  }
});

/* ============ toast ============ */
function showToast(message, type, duration) {
  type = type || 'info';
  duration = duration || 3000;
  var container = document.getElementById('toast-container');
  if (!container) return;
  var el = document.createElement('div');
  el.setAttribute('role', 'alert');
  el.className = 'toast animate-toast-in toast-' + (['success', 'error', 'warning', 'info'].indexOf(type) >= 0 ? type : 'info');
  el.textContent = message;
  container.appendChild(el);
  setTimeout(function () {
    el.classList.remove('animate-toast-in');
    el.classList.add('animate-toast-out');
    el.addEventListener('animationend', function () { el.remove(); }, { once: true });
    setTimeout(function () { el.remove(); }, 500);
  }, duration);
}

/* ============ confirm dialog ============ */
function showConfirm(message, onConfirm) {
  var previouslyFocused = document.activeElement;
  var overlay = document.createElement('div');
  overlay.className = 'fixed inset-0 z-[120] flex items-center justify-center bg-zinc-950/60 p-4 backdrop-blur-sm animate-fade-in';
  overlay.innerHTML =
    '<div role="alertdialog" aria-modal="true" aria-label="확인" class="card w-full max-w-sm p-6 animate-bounce-in">' +
      '<p class="whitespace-pre-line text-sm leading-relaxed text-zinc-800 dark:text-zinc-200"></p>' +
      '<div class="mt-6 flex justify-end gap-2">' +
        '<button type="button" data-confirm-cancel class="btn-secondary">취소</button>' +
        '<button type="button" data-confirm-ok class="btn-primary">확인</button>' +
      '</div>' +
    '</div>';
  overlay.querySelector('p').textContent = message;
  var cancelBtn = overlay.querySelector('[data-confirm-cancel]');
  var okBtn = overlay.querySelector('[data-confirm-ok]');

  function close() {
    document.removeEventListener('keydown', onKey, true);
    overlay.remove();
    if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Tab') {
      e.preventDefault();
      (document.activeElement === okBtn ? cancelBtn : okBtn).focus();
    }
  }
  overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
  cancelBtn.addEventListener('click', close);
  okBtn.addEventListener('click', function () { onConfirm(); close(); });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(overlay);
  cancelBtn.focus();
}

document.addEventListener('submit', function (event) {
  var form = event.target;
  if (form.dataset.confirmMessage && !form.dataset.confirmed) {
    event.preventDefault();
    showConfirm(form.dataset.confirmMessage, function () {
      form.dataset.confirmed = 'true';
      form.requestSubmit();
    });
    return;
  }
  // 오래 걸리는 제출은 버튼을 잠가 두 번 보내지 않게 합니다.
  var button = form.dataset.busyLabel && form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = true;
    button.textContent = form.dataset.busyLabel;
  }
});

/* ============ fetch with timeout ============ */
async function fetchWithTimeout(url, options, timeoutMs) {
  options = options || {};
  timeoutMs = timeoutMs || 15000;
  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
  try {
    return await fetch(url, Object.assign({}, options, { signal: controller.signal }));
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw new Error('서버 응답이 늦습니다. 잠시 후 다시 시도해주세요.');
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/* ============ inline edit rows ============ */
// 보기 줄과 편집 폼을 바꿔 보여주고 편집 칸에 포커스를 줍니다.
function toggleRow(viewId, editId, input) {
  var view = document.getElementById(viewId);
  var edit = document.getElementById(editId);
  if (!view || !edit) return;
  var opening = edit.classList.contains('hidden');
  view.classList.toggle('hidden', opening);
  edit.classList.toggle('hidden', !opening);
  edit.classList.toggle('flex', opening);
  var field = opening && edit.querySelector(input);
  if (field) { field.focus(); field.select(); }
}

/* ============ data-action ============ */
// 페이지 스크립트는 pageActions에 처리기를 추가합니다.
var pageActions = {
  copy: async function (el) {
    var field = document.getElementById(el.dataset.target);
    try {
      await navigator.clipboard.writeText(field.value);
      showToast('복사했습니다.', 'success');
    } catch (error) {
      field.focus();
      field.select();
      showToast('선택된 내용을 직접 복사해주세요.', 'info');
    }
  }
};
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-action]');
  var handler = el && pageActions[el.dataset.action];
  if (handler) handler(el, e);
});

/* ============ HTMX ============ */
(function () {
  function hxTargetOf(evt) {
    if (evt.detail && evt.detail.target) return evt.detail.target;
    var elt = evt.detail && evt.detail.elt;
    if (!elt || !elt.getAttribute) return null;
    var sel = elt.getAttribute('hx-target');
    if (!sel || sel === 'none') return null;
    try {
      if (sel === 'this') return elt;
      if (sel.indexOf('closest ') === 0) return elt.closest(sel.slice(8));
      if (sel.indexOf('find ') === 0) return elt.querySelector(sel.slice(5));
      return document.querySelector(sel);
    } catch (e) { return null; }
  }
  function setFormBusy(evt, busy) {
    var elt = evt.detail && evt.detail.elt;
    var form = elt && (elt.tagName === 'FORM' ? elt : elt.closest('form'));
    if (!form) return;
    form.querySelectorAll('button[type="submit"]').forEach(function (button) {
      button.disabled = busy;
      button.classList.toggle('opacity-60', busy);
    });
  }
  ['htmx:sendError', 'htmx:timeout'].forEach(function (name) {
    document.body.addEventListener(name, function () {
      showToast('서버에 연결하지 못했습니다. 연결을 확인해주세요.', 'error', 5000);
    });
  });
  document.body.addEventListener('htmx:responseError', function (evt) {
    var xhr = evt.detail.xhr;
    var msg = '요청을 처리하지 못했습니다.';
    try {
      var data = JSON.parse(xhr.responseText);
      if (data && typeof data.detail === 'string') msg = data.detail;
    } catch (e) {}
    // 401·409는 화면의 상태가 서버와 달라진 경우라 새로고침합니다.
    var reload = xhr.status === 401 || xhr.status === 409;
    showToast(msg, reload ? 'warning' : 'error', reload ? 4000 : 5000);
    if (reload) setTimeout(function () { location.reload(); }, 2000);
  });
  document.body.addEventListener('htmx:confirm', function (evt) {
    if (!evt.detail.question) return;
    evt.preventDefault();
    showConfirm(evt.detail.question, function () { evt.detail.issueRequest(true); });
  });
  document.body.addEventListener('showToast', function (evt) {
    var d = evt.detail || {};
    showToast(d.message || d.value || '완료했습니다.', d.type || 'success');
  });
  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    setFormBusy(evt, true);
    var t = hxTargetOf(evt);
    if (t) {
      t.setAttribute('aria-busy', 'true');
      if (t.id === 'item-list' && t.contains(evt.detail.elt)) t.dataset.restoreFocus = 'true';
    }
  });
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var target = evt.detail.target;
    if (target && target.id === 'item-list' && target.dataset.restoreFocus) {
      delete target.dataset.restoreFocus;
      var next = target.querySelector('a, button');
      if (next) next.focus();
      else { var input = document.querySelector('input[name="name"]'); if (input) input.focus(); }
    }
  });
  document.body.addEventListener('htmx:afterRequest', function (evt) {
    setFormBusy(evt, false);
    var t = hxTargetOf(evt);
    if (t) t.removeAttribute('aria-busy');
  });
})();
