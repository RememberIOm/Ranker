const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// 실제 공통 스크립트를 불러오고 선택 상태와 단축키 계약을 검사합니다.
function loadBattle(mode = '3way') {
  const listeners = {};
  const submit = { innerHTML: '투표 저장', disabled: false };
  const state = { dataset: { item1Id: '1', item2Id: '2', item3Id: '3',
    roundToken: 'round', criteriaKeys: '["quality","taste"]', battleMode: mode } };
  const context = {
    document: {
      getElementById: (id) => id === 'submit-btn' ? submit : id === 'battle-state' ? state : null,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: (name, fn) => { listeners[name] = fn; },
    },
    CSS: { escape: (value) => value },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/battle.js', 'utf8'), context);
  context.updateCardUI = () => {};
  context.updateProgress = () => {};
  return { context, listeners, submit };
}

for (const selected of [1, 2, 3]) {
  test(`전체 최고 ${selected} 선택은 최하를 추측하지 않는다`, () => {
    const { context: ctx } = loadBattle();
    ctx.quickBest(selected);
    for (const key of ctx.criteriaKeys) {
      assert.equal(ctx.votes[key].best, selected);
      assert.equal(ctx.votes[key].worst, undefined);
      assert.equal(ctx.isComplete(ctx.votes[key]), false);
    }
    const worst = selected === 1 ? 2 : 1;
    ctx.selectItem('quality', worst);
    assert.equal(ctx.votes.quality.worst, worst);
    assert.equal(ctx.isComplete(ctx.votes.quality), true);
  });
}

test('명시적으로 나머지 동률을 고르면 동률 역할로 직렬화한다', () => {
  const { context: ctx } = loadBattle();
  ctx.quickBest(1);
  ctx.toggleTied('quality');
  const output = ctx.serializeVotes().quality;
  assert.equal(output['1'], 'best');
  assert.equal(output['2'], 'tied');
  assert.equal(output['3'], 'tied');
  assert.equal(ctx.isComplete(ctx.votes.quality), true);
});

test('3개 비교에서 건너뛰기는 동률과 구별하고 재선택할 수 있다', () => {
  const { context: ctx } = loadBattle();
  ctx.skipCriterion('quality');
  assert.equal(ctx.isComplete(ctx.votes.quality), true);
  assert.equal(ctx.serializeVotes().quality.skip, 'skip');
  ctx.selectItem('quality', 2);
  assert.equal(ctx.votes.quality.skip, undefined);
  assert.equal(ctx.votes.quality.best, 2);
  assert.equal(ctx.isComplete(ctx.votes.quality), false);
});

test('1대1에서 건너뛰기를 별도 값으로 직렬화한다', () => {
  const { context: ctx } = loadBattle('2way');
  ctx.skipCriterion('quality');
  assert.equal(ctx.serializeVotes().quality, 'skip');
  assert.equal(ctx.isComplete(ctx.votes.quality), true);
});

test('일반 Enter는 제출하지 않고 Ctrl+Enter만 제출한다', () => {
  const { context: ctx, listeners } = loadBattle();
  let submits = 0;
  let prevented = 0;
  ctx.submitAllVotes = () => { submits += 1; };
  const event = { key: 'Enter', preventDefault: () => { prevented += 1; } };
  listeners.keydown(event);
  assert.equal(submits, 0);
  assert.equal(prevented, 0);
  listeners.keydown({ ...event, ctrlKey: true });
  assert.equal(submits, 1);
  assert.equal(prevented, 1);
});

test('요청 중에는 3개 비교 선택 상태를 바꾸지 않는다', () => {
  const { context: ctx } = loadBattle();
  ctx.quickBest(1);
  ctx.isSubmitting = true;
  ctx.quickBest(2);
  ctx.selectItem('quality', 3);
  ctx.skipCriterion('quality');
  assert.equal(ctx.votes.quality.best, 1);
  assert.equal(ctx.votes.quality.worst, undefined);
  assert.equal(ctx.votes.quality.skip, undefined);
});
