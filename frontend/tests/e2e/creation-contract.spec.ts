import { test, expect } from '@playwright/test'

test('support pane scrolls to every field and persists full edits across refresh', async ({ page }) => {
  let stored = { contract: { locked_genre: '玄幻 / 作者主题', locked_world_preset: '完整世界基调'.repeat(100),
    locked_story_structure: '多线并进'.repeat(60), locked_pacing_control: '循序渐进'.repeat(60),
    locked_writing_style: '写作规则'.repeat(60), locked_special_requirements: '硬性要求'.repeat(60),
    target_chapters: 120, target_words_per_chapter: 3500, style_notes: '文风公约正文'.repeat(120), premise_lock: '梗概锁定完整正文'.repeat(50) },
    version: 'v1', structured_market: false }
  let writes = 0
  await page.route('**/api/v1/**', async route => {
    if (route.request().url().endsWith('/creation-contract')) {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON()
        expect(body.version).toBe(stored.version)
        stored = { ...stored, contract: body.contract, version: `v${++writes + 1}` }
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(stored) })
    }
    return route.fulfill({ contentType: 'application/json', body: '{}' })
  })
  await page.setViewportSize({ width: 800, height: 640 })
  await page.goto('/support')
  await expect(page.getByRole('textbox', { name: '世界基调', exact: true })).toHaveValue(stored.contract.locked_world_preset)
  const footer = page.locator('.bible-panel > .pp-panel-footer')
  await expect(footer).toBeInViewport()
  const dimensions = await page.locator('.bible-body').evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight }))
  expect(dimensions.scroll).toBeGreaterThan(dimensions.height + 100)
  await page.getByRole('textbox', { name: '硬性要求', exact: true }).fill('完整的新硬性要求：结局回应开头')
  await page.getByRole('textbox', { name: '文风公约', exact: true }).fill('作者修改后的完整文风公约，修改后刷新仍然可见。')
  await page.getByRole('textbox', { name: '梗概锁定', exact: true }).fill('作者修改后的完整梗概，要求保存后真正持久化。')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('创作契约、文风与梗概已保存', { exact: true })).toBeVisible()
  expect(writes).toBe(1)
  await page.reload()
  await expect(page.getByRole('textbox', { name: '硬性要求', exact: true })).toHaveValue('完整的新硬性要求：结局回应开头')
  await expect(page.getByRole('textbox', { name: '文风公约', exact: true })).toHaveValue('作者修改后的完整文风公约，修改后刷新仍然可见。')
  await expect(page.getByRole('textbox', { name: '梗概锁定', exact: true })).toHaveValue('作者修改后的完整梗概，要求保存后真正持久化。')
  await expect(footer).toBeInViewport()
  await page.locator('.bible-body').evaluate(el => { el.scrollTop = 0 })
  await page.screenshot({ path: '../out/support-edit-20261027/contract-browser.png' })
})
