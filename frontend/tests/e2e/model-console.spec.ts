import { test, expect } from '@playwright/test'

for (const missing of ['llm', 'embedding', 'none'] as const) {
  test(`new book checks saved models and opens the ${missing} destination`, async ({ page }) => {
    let readinessCalls = 0
    let created = 0
    await page.route(/\/api\/v1\//, async route => {
      const path = new URL(route.request().url()).pathname
      let data: unknown = {}
      if (path.endsWith('/settings/model-readiness')) {
        readinessCalls++
        data = { ready: missing === 'none', llm: { ready: missing !== 'llm', reasons: missing === 'llm' ? ['未激活语言模型'] : [] },
          embedding: { ready: missing !== 'embedding', reasons: missing === 'embedding' ? ['请下载本地模型权重'] : [], mode: 'local' } }
      } else if (/\/novels\/?$/.test(path)) {
        if (route.request().method() === 'POST') created++
        data = []
      } else if (path.endsWith('/settings/embedding')) {
        data = { mode: 'local', api_key: '', base_url: '', model: '', use_gpu: false, model_path: 'models/bge-small-zh-v1.5' }
      } else if (path.endsWith('/system/extensions-status')) {
        data = { all_installed: true, faiss: true, numpy: true, sentence_transformers: true, install_running: false,
          install_progress: '', local_ready: false, model_ready: false, model_path: 'models/bge-small-zh-v1.5' }
      } else if (path.endsWith('/llm-control/panel')) {
        data = { config: { version: 1, active_profile_id: null, profiles: [], endpoint_mode: 'unified' }, presets: [],
          runtime: { status: 'unavailable', source: 'unavailable', using_mock: false, model: null, reason: '未配置模型' } }
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    })
    await page.goto('/library')
    await page.getByRole('button', { name: '创建第一本书', exact: true }).click()
    expect(readinessCalls).toBe(1)
    expect(created).toBe(0)
    if (missing === 'none') {
      await expect(page.getByRole('heading', { name: '新建书目', exact: true }).first()).toBeVisible()
      await expect(page.getByText('请先配置好模型', { exact: true })).toHaveCount(0)
    } else {
      await expect(page.getByText('请先配置好模型', { exact: true })).toBeVisible()
      await page.getByRole('dialog').getByRole('button', { name: '打开模型控制台', exact: true }).click()
      if (missing === 'embedding') {
        await expect(page.getByText('本地模型文件尚未就绪', { exact: true })).toBeVisible()
        await expect(page.getByRole('button', { name: '下载并准备本地模型', exact: true })).toBeVisible()
      } else {
        await expect(page.getByRole('heading', { name: '模型控制台', exact: true })).toBeVisible()
      }
      await page.screenshot({ path: `test-results/model-console-${missing}.png`, fullPage: true })
    }
  })
}
