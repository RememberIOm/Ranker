const { test, expect } = require('@playwright/test');

async function setup(page, threeWay = false) {
  await page.request.post('/start');
  await page.request.post('/manage/add-bulk', { form: { names: 'Alpha\nBeta\nGamma' } });
  if (threeWay) await page.request.post('/manage/settings', { form: { battle_mode: '3way', blind_mode: 'on' } });
  await page.goto('/battle');
}

test('투표와 partial 교체, 건너뛰기, 실행 취소', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await setup(page);
  const token = await page.locator('#battle-state').getAttribute('data-round-token');
  await page.getByRole('button', { name: '전부 A 선택', exact: true }).click();
  await page.locator('#btn-story-skip').click();
  await page.locator('#submit-btn').click();
  await expect(page.locator('#result-modal')).toBeVisible();
  await expect(page.locator('#result-modal')).toContainText('건너뜀');
  await page.locator('#next-battle-btn').click();
  await expect(page.locator('#submit-btn')).toBeDisabled();
  await expect(page.locator('#battle-state')).not.toHaveAttribute('data-round-token', token);
  await page.goto('/history');
  await page.getByRole('button', { name: '마지막 투표 실행 취소' }).click();
  await page.locator('[data-confirm-ok]').click();
  await expect(page.getByRole('button', { name: '마지막 투표 실행 취소' })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('3way 최고만 선택은 꼴찌를 만들지 않고 명시적 동률로 제출', async ({ page }) => {
  await setup(page, true);
  await page.getByRole('button', { name: '전부 A 최고만 선택' }).click();
  await expect(page.locator('#submit-btn')).toBeDisabled();
  await expect(page.locator('.sel-worst')).toHaveCount(0);
  for (const key of ['story','visual','ost','voice','char','fun']) await page.locator(`#tied-btn-${key}`).click();
  await expect(page.locator('#submit-btn')).toBeEnabled();
  await page.locator('#submit-btn').click();
  await expect(page.locator('#result-modal')).toBeVisible();
});

test('빈 상태, 새 항목 추가·삭제와 검색', async ({ page }) => {
  await page.request.post('/start');
  await page.goto('/ranking');
  await expect(page.locator('main')).toContainText('항목');
  await page.goto('/manage');
  await page.getByRole('textbox', { name: '추가할 항목 이름' }).fill('테스트 항목');
  await page.locator('form[action="/manage/add"] button[type="submit"]').click();
  await expect(page.locator('#item-list')).toContainText('테스트 항목');
  await expect(page.locator('#manage-counts')).toContainText('항목 1개');
  await page.goto('/ranking?q=없는이름');
  await expect(page.locator('table')).toHaveCount(0);
  await expect(page.locator('main')).not.toContainText('테스트 항목');
  await page.goto('/manage');
  await page.locator('#item-list').getByRole('button', { name: '삭제', exact: true }).click();
  await page.locator('[data-confirm-ok]').click();
  await expect(page.locator('#item-list')).toContainText('항목이 없습니다');
  await expect(page.locator('#manage-counts')).toContainText('항목 0개');
});

test('새 기준 key가 저장 응답에서 갱신됨', async ({ page }) => {
  await setup(page);
  await page.goto('/manage?tab=criteria');
  await page.getByRole('button', { name: '+ 기준 추가' }).click();
  const row = page.locator('#criteria-rows > div').last();
  await row.locator('input[name="label"]').fill('새 기준');
  await page.locator('#criteria-form button[type="submit"]').click();
  await expect(page.locator('#criteria-rows input[name="key"]').last()).not.toHaveValue('');
  await expect(page.locator('#manage-counts')).toContainText('기준 7개');
});

test('이름 있는 목록과 복구 코드로 다시 열기', async ({ page, context }) => {
  await page.goto('/collections');
  await page.getByRole('textbox', { name: '새 랭킹 이름', exact: true }).fill('영화 목록');
  await page.getByRole('button', { name: '만들기', exact: true }).click();
  await page.goto('/collections');
  const code = await page.locator('#recovery-code').inputValue();
  await expect(page.getByRole('heading', { name: '영화 목록', exact: true })).toBeVisible();
  await context.clearCookies();
  await page.goto('/collections');
  await page.getByLabel('보관한 복구 코드', { exact: true }).fill(code);
  await page.getByRole('button', { name: '불러오기', exact: true }).click();
  await expect(page.getByRole('heading', { name: '영화 목록', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '열기', exact: true }).click();
  await expect(page).toHaveURL(/\/ranking$/);
});

test('백업 미리보기 후 교체', async ({ page }) => {
  await setup(page);
  const backup = await (await page.request.get('/manage/export')).text();
  await page.request.post('/manage/add', { form: { name: '나중에 추가한 항목' } });
  await page.goto('/manage?tab=data');
  await page.locator('input[type="file"]').setInputFiles({ name: 'backup.json', mimeType: 'application/json', buffer: Buffer.from(backup) });
  await page.locator('#import-form button[type="submit"]').click();
  await expect(page.getByRole('heading', { name: '가져오기 미리보기' })).toBeVisible();
  await page.getByRole('button', { name: '확인한 데이터로 교체' }).click();
  await expect(page).toHaveURL(/\/manage\?tab=data$/);
  const after = await (await page.request.get('/manage/export')).text();
  expect(after).not.toContain('나중에 추가한 항목');
  expect(JSON.parse(after).items).toHaveLength(3);
});
