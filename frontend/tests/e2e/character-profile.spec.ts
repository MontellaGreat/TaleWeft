import { test, expect } from '@playwright/test'

test('real character UI persists edits, preserves a conflict draft, refreshes cast, and confirms deletion', async ({ page }) => {
  test.setTimeout(90000)
  let character: Record<string, any> | null = { id: 'c1', name: '林舟', description: '旧角色简介', relationships: [],
    gender: '男', age: '20', appearance: '黑发', personality: '谨慎', background: '海边长大',
    core_motivation: '找到家人', inner_lack: '信任', voice_profile: { style: '简短' }, moral_taboos: ['不背叛'], active_wounds: [] }
  let rejectSave = false, patches = 0, deletions = 0
  const snapshot = () => ({ id: 'b1', novel_id: 'book', characters: character ? [character] : [], world_settings: [], locations: [], timeline_notes: [], style_notes: [] })
  await page.route(/\/api\/v1\//, async route => {
    const path = new URL(route.request().url()).pathname, method = route.request().method()
    if (path.endsWith('/bible/characters/c1') && method === 'PATCH') {
      patches++
      if (rejectSave) { await route.fulfill({ status: 409, json: { detail: '世界观生成任务运行中，当前不能修改角色' } }); return }
      const payload = route.request().postDataJSON()
      expect(payload.expected_character.name).toBe(character?.name)
      expect(payload.changes.id).toBeUndefined()
      character = { ...character, ...payload.changes }
      await route.fulfill({ json: { ...snapshot(), character_mutation: { derivation_jobs: 1 } } }); return
    }
    if (path.endsWith('/bible/characters/c1') && method === 'DELETE') {
      deletions++; character = null
      await route.fulfill({ json: snapshot() }); return
    }
    let data: unknown = {}
    if (path.endsWith('/bible')) data = snapshot()
    else if (path.endsWith('/cast/schedule')) data = { cast: character ? [{ character_id: 'c1', name: character.name, importance: 'major', is_new_suggestion: false }] : [], new_character_candidates: [] }
    else if (path.includes('/character-psyches/')) data = null
    else if (path.endsWith('/projection')) data = { active_scars: [], candidate_memories: [] }
    else if (path.endsWith('/dialogue-whitelist')) data = { dialogues: [], total_dialogues: 0 }
    await route.fulfill({ json: data })
  })
  await page.goto('/character-profile.html', { waitUntil: 'domcontentloaded' })
  await page.locator('.cn-item').filter({ hasText: '林舟' }).click()
  await page.getByRole('button', { name: '编辑档案', exact: true }).click()
  await page.getByTestId('character-name').locator('input').fill('林川')
  await page.getByTestId('character-description').locator('textarea').fill('新的角色简介')
  await expect(page.getByText('未保存', { exact: true })).toBeVisible()
  await page.screenshot({ path: '../out/support-edit-20261027/character-profile-edit.png', fullPage: true })
  await page.getByRole('button', { name: '保存档案', exact: true }).click()
  await expect(page.getByRole('button', { name: '保存档案', exact: true })).toBeHidden()
  await expect(page.locator('.cn-name')).toHaveText('林川')
  await expect(page.locator('.cp-id-name')).toHaveText('林川')
  await expect(page.locator('.ccm-name')).toHaveText('林川')
  expect(patches).toBe(1)

  await page.reload()
  await page.locator('.cn-item').filter({ hasText: '林川' }).click()
  await page.getByRole('button', { name: '编辑档案', exact: true }).click()
  await expect(page.getByTestId('character-description').locator('textarea')).toHaveValue('新的角色简介')
  await page.getByTestId('character-description').locator('textarea').fill('冲突仍保留的草稿')
  rejectSave = true
  await page.getByRole('button', { name: '保存档案', exact: true }).click()
  await expect(page.getByText(/世界观生成任务运行中.*409/)).toBeVisible()
  await expect(page.getByTestId('character-description').locator('textarea')).toHaveValue('冲突仍保留的草稿')
  await page.screenshot({ path: '../out/support-edit-20261027/character-profile-conflict.png', fullPage: true })
  expect(patches).toBe(2)
  await page.getByRole('button', { name: '删除角色', exact: true }).click()
  expect(deletions).toBe(0)
  await expect(page.getByText(/历史章节正文、历史版本和审稿原始证据会保留/)).toBeVisible()
  await page.getByRole('button', { name: '保留角色', exact: true }).click()
  expect(deletions).toBe(0)
  await page.getByRole('button', { name: '删除角色', exact: true }).click()
  await page.getByRole('button', { name: '确认删除角色', exact: true }).click()
  await expect(page.locator('.cn-item')).toHaveCount(0)
  await expect(page.locator('.cp-id-name')).toHaveCount(0)
  await expect(page.locator('.ccm-name')).toHaveCount(0)
  expect(deletions).toBe(1)
})
