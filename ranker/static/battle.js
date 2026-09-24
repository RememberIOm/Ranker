// 대결의 선택 상태, 저장 요청, 결과 창 수명주기를 1대1과 3개 비교에서 함께 씁니다.
var item1Id, item2Id, item3Id, roundToken, redirectTo, criteriaKeys, battleMode;
var votes = Object.create(null);
var isSubmitting = false;
var nextArenaLoaded = true;
var SUBMIT_BTN_LABEL = document.getElementById('submit-btn').innerHTML;
var SPINNER_HTML = '<span>저장 중…</span>';
var RING_CIRCUMFERENCE = 87.96;
var ROLE_NAMES = { Best: '최고', Worst: '최하', Middle: '중간', Tied: '동률' };

function itemIds() { return [item1Id, item2Id, item3Id]; }

function readBattleState() {
  var st = document.getElementById('battle-state');
  item1Id = Number(st.dataset.item1Id);
  item2Id = Number(st.dataset.item2Id);
  item3Id = Number(st.dataset.item3Id);
  roundToken = st.dataset.roundToken;
  redirectTo = st.dataset.redirectTo || '';
  criteriaKeys = JSON.parse(st.dataset.criteriaKeys || '[]');
  battleMode = st.dataset.battleMode;
}
readBattleState();

function setBattleBusy(busy) {
  var arena = document.getElementById('battle-arena');
  arena.setAttribute('aria-busy', String(busy));
  arena.querySelectorAll('button').forEach(function (button) {
    if (button.id !== 'submit-btn') button.disabled = busy;
  });
}

/* ================= selection ================= */
function skipCriterion(key) {
  if (isSubmitting) return;
  if (battleMode === '2way') { selectVote(key, 'skip'); return; }
  votes[key] = votes[key] && votes[key].skip ? {} : { skip: true };
  updateCardUI(key);
  updateProgress();
}

function selectVote(key, choice) {
  if (isSubmitting) return;
  votes[key] = choice;
  ['1', 'draw', '2', 'skip'].forEach(function (c) {
    var btn = document.getElementById('btn-' + key + '-' + c);
    if (!btn) return;
    btn.setAttribute('aria-pressed', String(c === choice));
    btn.classList.remove('selected-1', 'selected-draw', 'selected-2', 'active-mode');
  });
  var chosen = document.getElementById('btn-' + key + '-' + choice);
  if (chosen) chosen.classList.add(choice === 'skip' ? 'active-mode' : 'selected-' + choice);
  var card = document.querySelector('.criteria-card[data-key="' + CSS.escape(key) + '"]');
  if (card) card.classList.add('voted');
  updateProgress();
}

function selectAllAs(choice) {
  criteriaKeys.forEach(function (k) { selectVote(k, choice); });
}

function isComplete(v) {
  if (!v) return false;
  if (battleMode === '2way') return ['1', 'draw', '2', 'skip'].includes(v);
  if (v.skip || v.allTied) return true;
  if (v.worstOnly) return v.worst != null;
  return v.best != null && (v.worst != null || v.tied === true);
}

function selectItem(key, id) {
  if (isSubmitting) return;
  var v = votes[key] || {};
  if (v.allTied || v.worstOnly || v.skip) {
    v = { best: id };
  } else {
    delete v.tied;
    if (v.best === id) delete v.best;
    else if (v.worst === id) delete v.worst;
    else if (v.best == null) v.best = id;
    else v.worst = id;
  }
  votes[key] = v;
  updateCardUI(key);
  updateProgress();
}

function toggleTied(key) {
  if (isSubmitting) return;
  var v = votes[key] || {};
  if (v.best == null) return;
  if (v.tied) {
    delete v.tied;
  } else {
    v.tied = true;
    delete v.worst;
  }
  votes[key] = v;
  updateCardUI(key);
  updateProgress();
}

function toggleWorstOnly(key) {
  if (isSubmitting) return;
  var v = votes[key] || {};
  if (v.worstOnly) {
    votes[key] = { best: v.worst };
  } else if (v.best != null) {
    votes[key] = { worst: v.best, tied: true, worstOnly: true };
  } else {
    return;
  }
  updateCardUI(key);
  updateProgress();
}

function toggleAllTied(key) {
  if (isSubmitting) return;
  var v = votes[key] || {};
  votes[key] = v.allTied ? {} : { allTied: true };
  updateCardUI(key);
  updateProgress();
}

function quickBest(bestId) {
  if (isSubmitting) return;
  criteriaKeys.forEach(function (key) {
    votes[key] = { best: bestId };
    updateCardUI(key);
  });
  updateProgress();
}

function quickAllTied() {
  if (isSubmitting) return;
  criteriaKeys.forEach(function (key) {
    votes[key] = { allTied: true };
    updateCardUI(key);
  });
  updateProgress();
}

function clearAll() {
  if (isSubmitting) return;
  votes = Object.create(null);
  if (battleMode === '3way') {
    criteriaKeys.forEach(updateCardUI);
  } else {
    document.querySelectorAll('#criteria-list .vote-btn, #criteria-list [data-skip]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', 'false');
      btn.classList.remove('selected-1', 'selected-draw', 'selected-2', 'active-mode');
    });
    document.querySelectorAll('#criteria-list .criteria-card').forEach(function (card) { card.classList.remove('voted'); });
  }
  updateProgress();
}

/* ================= card UI ================= */
function updateCardUI(key) {
  var v = votes[key] || {};
  var card = document.querySelector('.criteria-card[data-key="' + CSS.escape(key) + '"]');
  if (!card) return;
  var ids = itemIds();

  ids.forEach(function (id) {
    var btn = document.getElementById('btn-' + key + '-' + id);
    if (!btn) return;
    btn.classList.remove('sel-best', 'sel-worst', 'sel-middle', 'sel-tied');
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', btn.dataset.label);
  });
  card.querySelectorAll('.role-icon').forEach(function (s) { s.textContent = ''; });
  card.classList.remove('voted');

  var tiedBtn = document.getElementById('tied-btn-' + key);
  var worstBtn = document.getElementById('worst-only-btn-' + key);
  var allTiedBtn = document.getElementById('all-tied-btn-' + key);
  if (tiedBtn) { tiedBtn.classList.add('hidden'); tiedBtn.classList.remove('active-mode'); }
  if (worstBtn) { worstBtn.classList.add('hidden'); worstBtn.classList.remove('active-mode'); }
  if (allTiedBtn) allTiedBtn.classList.remove('active-mode');
  [tiedBtn, worstBtn, allTiedBtn].forEach(function (btn) { if (btn) btn.setAttribute('aria-pressed', 'false'); });
  var skipBtn = document.getElementById('btn-' + key + '-skip');
  if (skipBtn) {
    skipBtn.classList.toggle('active-mode', !!v.skip);
    skipBtn.setAttribute('aria-pressed', String(!!v.skip));
  }

  function setRole(id, role, cls, pressed) {
    var btn = document.getElementById('btn-' + key + '-' + id);
    if (!btn) return;
    btn.classList.add(cls);
    btn.setAttribute('aria-label', btn.dataset.label + ' — ' + ROLE_NAMES[role]);
    if (pressed) btn.setAttribute('aria-pressed', 'true');
    var badge = btn.querySelector('.role-icon');
    if (badge) {
      var span = document.createElement('span');
      span.textContent = ROLE_NAMES[role];
      badge.appendChild(span);
    }
  }
  function others(id) { return ids.filter(function (i) { return i !== id; }); }

  if (v.skip) {
    card.classList.add('voted');
  } else if (v.allTied) {
    ids.forEach(function (id) { setRole(id, 'Tied', 'sel-tied', false); });
    card.classList.add('voted');
    if (allTiedBtn) { allTiedBtn.classList.add('active-mode'); allTiedBtn.setAttribute('aria-pressed', 'true'); }
  } else if (v.worstOnly && v.worst != null) {
    setRole(v.worst, 'Worst', 'sel-worst', true);
    others(v.worst).forEach(function (i) { setRole(i, 'Tied', 'sel-tied', false); });
    card.classList.add('voted');
    if (worstBtn) { worstBtn.classList.remove('hidden'); worstBtn.classList.add('active-mode'); worstBtn.setAttribute('aria-pressed', 'true'); }
  } else if (v.best != null && v.tied) {
    setRole(v.best, 'Best', 'sel-best', true);
    others(v.best).forEach(function (i) { setRole(i, 'Tied', 'sel-tied', false); });
    card.classList.add('voted');
    if (tiedBtn) { tiedBtn.classList.remove('hidden'); tiedBtn.classList.add('active-mode'); tiedBtn.setAttribute('aria-pressed', 'true'); }
  } else if (v.best != null && v.worst != null) {
    setRole(v.best, 'Best', 'sel-best', true);
    setRole(v.worst, 'Worst', 'sel-worst', true);
    ids.filter(function (i) { return i !== v.best && i !== v.worst; }).forEach(function (i) { setRole(i, 'Middle', 'sel-middle', false); });
    card.classList.add('voted');
  } else if (v.best != null) {
    setRole(v.best, 'Best', 'sel-best', true);
    if (tiedBtn) tiedBtn.classList.remove('hidden');
    if (worstBtn) worstBtn.classList.remove('hidden');
  } else if (v.worst != null) {
    setRole(v.worst, 'Worst', 'sel-worst', true);
  }
}

function updateProgress() {
  var total = criteriaKeys.length;
  var done = criteriaKeys.filter(function (k) { return isComplete(votes[k]); }).length;
  var ring = document.getElementById('progress-ring');
  if (ring) ring.style.strokeDashoffset = (RING_CIRCUMFERENCE * (1 - (total ? done / total : 0))).toFixed(2);
  var count = document.getElementById('progress-count');
  if (count) count.textContent = done + '/' + total;
  var label = document.getElementById('progress-label');
  if (label) {
    label.textContent = done === 0
      ? (battleMode === '3way' ? '기준마다 최고와 최하를 고르세요' : '기준마다 하나씩 고르세요')
      : done < total ? (total - done) + '개 남음'
      : '모두 골랐습니다';
  }
  var bar = document.querySelector('[role="progressbar"]');
  if (bar) bar.setAttribute('aria-valuenow', String(done));
  var btn = document.getElementById('submit-btn');
  if (btn) btn.disabled = isSubmitting || total === 0 || done !== total;
}

function serializeVotes() {
  if (battleMode === '2way') return votes;
  var out = Object.create(null);
  var ids = itemIds();
  criteriaKeys.forEach(function (key) {
    var v = votes[key] || {};
    if (v.skip) { out[key] = 'skip'; return; }
    var m = {};
    if (v.allTied) {
      ids.forEach(function (i) { m[String(i)] = 'tied'; });
    } else if (v.worstOnly) {
      m[String(v.worst)] = 'worst';
      ids.filter(function (i) { return i !== v.worst; }).forEach(function (i) { m[String(i)] = 'tied'; });
    } else if (v.tied) {
      m[String(v.best)] = 'best';
      ids.filter(function (i) { return i !== v.best; }).forEach(function (i) { m[String(i)] = 'tied'; });
    } else {
      m[String(v.best)] = 'best';
      m[String(v.worst)] = 'worst';
    }
    out[key] = m;
  });
  return out;
}

/* ================= submit ================= */
function handleVoteError(status, data) {
  var detail = data && typeof data.detail === 'string' ? data.detail : '';
  if (status === 401 || status === 409) {
    showToast(detail || '화면이 오래되었습니다. 새로고침해주세요.', 'warning', 7000);
    return;
  }
  showToast(detail || '투표를 저장하지 못했습니다. 다시 시도해주세요.', 'error', 5000);
}

async function submitAllVotes() {
  var btn = document.getElementById('submit-btn');
  if (isSubmitting || !btn || btn.disabled) return;
  isSubmitting = true;
  setBattleBusy(true);
  btn.disabled = true;
  btn.innerHTML = SPINNER_HTML;
  var payload = { item1_id: item1Id, item2_id: item2Id, round_token: roundToken,
    votes: serializeVotes(), redirect_to: redirectTo || null };
  if (battleMode === '3way') payload.item3_id = item3Id;
  try {
    var res = await fetchWithTimeout(battleMode === '3way' ? '/battle/vote/3way' : '/battle/vote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      var data = {};
      try { data = await res.json(); } catch (e) {}
      handleVoteError(res.status, data);
      return;
    }
    applyVoteResponse(await res.text());
  } catch (err) {
    showToast((err && err.message) || '연결을 확인한 뒤 다시 시도해주세요.', 'error', 5000);
  } finally {
    if (!document.getElementById('result-modal')) {
      isSubmitting = false;
      setBattleBusy(false);
      var current = document.getElementById('submit-btn');
      if (current) current.innerHTML = SUBMIT_BTN_LABEL;
      updateProgress();
    }
  }
}

// 결과 창을 넣고, 응답에 다음 대결(OOB)이 있으면 htmx가 대결 영역을 바꿉니다.
function applyVoteResponse(html) {
  var previousToken = roundToken;
  htmx.swap('#result-modal-container', html, { swapStyle: 'innerHTML' });
  var st = document.getElementById('battle-state');
  nextArenaLoaded = !!st && st.dataset.roundToken !== previousToken;
  initResultModal();
  if (!document.getElementById('result-modal')) reinitBattleState();
}

/* ================= result modal ================= */
var modalTimers = [];
var modalKeyHandler = null;
var autoSkipRunning = false;

function initResultModal() {
  var modal = document.getElementById('result-modal');
  if (!modal) return;
  var autoSkip = modal.dataset.autoSkip === 'true';
  var skipSeconds = parseFloat(modal.dataset.skipSeconds || '3') || 3;
  var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var effectiveAutoSkip = autoSkip && !prefersReducedMotion;
  if (autoSkip && prefersReducedMotion) {
    var autoArea = document.getElementById('auto-skip-area');
    var clickArea = document.getElementById('click-skip-area');
    if (autoArea) autoArea.classList.add('hidden');
    if (clickArea) clickArea.classList.remove('hidden');
  }
  modal.setAttribute('tabindex', '-1');
  document.getElementById('battle-arena').inert = true;
  requestAnimationFrame(function () {
    modal.classList.remove('opacity-0');
    if (modal.firstElementChild) modal.firstElementChild.classList.remove('scale-95');
  });

  modal.addEventListener('click', function (e) {
    if (e.target === modal && !autoSkipRunning) dismissModal();
  });

  if (effectiveAutoSkip) {
    autoSkipRunning = true;
    modalTimers.push(setTimeout(function () {
      var bar = document.getElementById('modal-progress');
      if (bar) bar.style.width = '100%';
    }, 300));
    modalTimers.push(setTimeout(dismissModal, 300 + skipSeconds * 1000));
    modal.focus();
  } else {
    autoSkipRunning = false;
    var nextBtn = document.getElementById('next-battle-btn');
    (nextBtn || modal).focus();
  }

  modalKeyHandler = function (e) {
    var m = document.getElementById('result-modal');
    if (!m) return;
    if (e.key === 'Escape') { e.preventDefault(); dismissModal(); return; }
    if (e.key === 'Tab') {
      var focusables = Array.prototype.filter.call(
        m.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
        function (el) { return el.offsetParent !== null; }
      );
      if (!focusables.length) { e.preventDefault(); m.focus(); return; }
      var first = focusables[0], last = focusables[focusables.length - 1];
      if (!m.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if ((e.key === ' ' || e.key === 'Enter') && !autoSkipRunning && e.target === m) {
      e.preventDefault();
      dismissModal();
    }
  };
  document.addEventListener('keydown', modalKeyHandler);
}

function cancelAutoSkip() {
  modalTimers.forEach(clearTimeout);
  modalTimers = [];
  autoSkipRunning = false;
  var bar = document.getElementById('modal-progress');
  if (bar) {
    var w = getComputedStyle(bar).width;
    bar.style.transitionDuration = '0ms';
    bar.style.width = w;
  }
  var autoArea = document.getElementById('auto-skip-area');
  var clickArea = document.getElementById('click-skip-area');
  if (autoArea) autoArea.classList.add('hidden');
  if (clickArea) clickArea.classList.remove('hidden');
  var nextBtn = document.getElementById('next-battle-btn');
  if (nextBtn) nextBtn.focus();
}

function dismissModal() {
  if (modalKeyHandler) {
    document.removeEventListener('keydown', modalKeyHandler);
    modalKeyHandler = null;
  }
  modalTimers.forEach(clearTimeout);
  modalTimers = [];
  autoSkipRunning = false;
  var container = document.getElementById('result-modal-container');
  if (container) container.innerHTML = '';
  document.getElementById('battle-arena').inert = false;
  reinitBattleState();
}

function reinitBattleState() {
  var st = document.getElementById('battle-state');
  if (!nextArenaLoaded || !st || st.dataset.battleMode !== battleMode) {
    window.location.href = redirectTo || '/battle';
    return;
  }
  readBattleState();
  votes = Object.create(null);
  isSubmitting = false;
  updateProgress();
  setBattleBusy(false);
  var firstBtn = document.querySelector('#criteria-list .vote-btn');
  if (firstBtn) firstBtn.focus();
}

/* ================= events ================= */
var BATTLE_ACTIONS = {
  'skip': function (el) { skipCriterion(el.dataset.key); },
  'vote': function (el) { selectVote(el.dataset.key, el.dataset.choice); },
  'vote-all': function (el) { selectAllAs(el.dataset.choice); },
  'select-item': function (el) { selectItem(el.dataset.key, Number(el.dataset.id)); },
  'tied': function (el) { toggleTied(el.dataset.key); },
  'worst-only': function (el) { toggleWorstOnly(el.dataset.key); },
  'all-tied': function (el) { toggleAllTied(el.dataset.key); },
  'quick-best': function (el) { quickBest(Number(el.dataset.id)); },
  'quick-all-tied': function () { quickAllTied(); },
  'clear': function () { clearAll(); },
  'submit': function () { submitAllVotes(); },
  'keep-result': function () { cancelAutoSkip(); },
  'dismiss-result': function () { dismissModal(); }
};
document.addEventListener('click', function (e) {
  var el = e.target.closest && e.target.closest('[data-battle]');
  if (el && BATTLE_ACTIONS[el.dataset.battle]) BATTLE_ACTIONS[el.dataset.battle](el);
});

// 일반 Enter는 포커스된 버튼의 기본 동작을 유지하고, Ctrl+Enter만 저장합니다.
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Enter' || !e.ctrlKey || e.repeat || document.getElementById('result-modal')) return;
  var btn = document.getElementById('submit-btn');
  if (btn && !btn.disabled) { e.preventDefault(); submitAllVotes(); }
});
