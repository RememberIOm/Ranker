// 관리 화면: 항목 이름 편집과 평가 기준 편집.

pageActions['toggle-edit'] = function (el) {
  toggleRow('view-row-' + el.dataset.id, 'edit-row-' + el.dataset.id, 'input[name="new_name"]');
};

pageActions['pick-color'] = function (el) {
  var row = el.closest('.criteria-row');
  row.querySelector('input[name="color"]').value = el.dataset.color;
  row.querySelectorAll('.color-swatch').forEach(function (s) {
    s.setAttribute('aria-pressed', 'false');
    s.classList.remove('swatch-selected');
  });
  el.setAttribute('aria-pressed', 'true');
  el.classList.add('swatch-selected');
};

pageActions['remove-criterion'] = function (el) {
  var row = el.closest('.criteria-row');
  var form = el.closest('#criteria-form');
  var keyInput = row && row.querySelector('input[name="key"]');
  if (form && keyInput && keyInput.value) {
    form.setAttribute(
      'hx-confirm',
      '삭제한 평가 기준의 투표 결과도 함께 지워지며 되돌릴 수 없습니다. 저장할까요?'
    );
  }
  if (row) row.remove();
};

pageActions['add-criterion'] = function () {
  var tpl = document.getElementById('criteria-row-template');
  var row = tpl.content.firstElementChild.cloneNode(true);
  document.getElementById('criteria-rows').appendChild(row);
  row.querySelector('input[name="label"]').focus();
};
