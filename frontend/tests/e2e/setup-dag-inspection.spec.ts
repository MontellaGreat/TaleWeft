import { test, expect, type Page, type Route, type Locator } from '@playwright/test'
import { inspectionCatalog, inspectionLabels, inspectionStages } from '../fixtures/setupDagInspection'
import type { SetupDagStage, SetupStageId } from '../../src/api/setupDag'

test('semantic review shows actual evidence and advice, while v2 stays historical', async ({ page }) => {
  await setup(page, { mutate: stages => {
    const people = stages[1]!.latest_run!
    const semantic = { status: 'completed', review_version: 'semantic-style-v2', attempt_id: 'semantic-model-call', passed: false,
      checks: { vague_lyricism: true, expository_voice: true, mechanical_structure: true, emotion_labeling: true, template_closure: true },
      coverage: [{ path: '$.characters[0].description', input_hash: 'text-hash' }],
      issues: [{ path: '$.characters[0].description', category: 'vague_lyricism', evidence: '命运的浪潮推动他前行。', reason: '空泛感悟缺少实际人物行动。', suggestion: '写明他拿走账本的具体动作。' }] }
    people.node_states.characters_anti_ai = { status: 'error', outputs: { anti_ai_audit: { passed: false, semantic_review: semantic, scan_status: 'completed', reports: [] } } }
    stages[2]!.latest_run!.workflow_version = 2
    const old = stages[2]!.latest_run!.node_states.locations_anti_ai!.outputs!.anti_ai_audit as Record<string, unknown>
    delete old.semantic_review
  } })
  await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
  await tab(page, 'characters').click()
  await openNode(page, 'characters', 'anti_ai')
  const report = page.locator('.setup-node-detail [aria-label="本节点的审核报告"]')
  await expect(report).toContainText('模型语义审稿：发现问题')
  await expect(report.getByText('命运的浪潮推动他前行。', { exact: true })).toBeVisible()
  await expect(report).toContainText('写明他拿走账本的具体动作。')
  await page.screenshot({ path: 'test-results/setup-semantic-evidence.png', fullPage: true })
  await closeDetail(page)
  await tab(page, 'locations').click()
  await expect(page.locator('.setup-dag-notice')).toContainText('未包含新版模型语义审稿')
  await openNode(page, 'locations', 'anti_ai')
  await expect(report).toContainText('缺少模型语义审稿')
  await expect(report).not.toContainText('报告结果：通过')
})

test('342 clean fields stay compact, remain fully reachable and do not duplicate the checkpoint report', async ({ page }) => {
  const writes = await setup(page, { mutate: stages => {
    const run = stages[1]!.latest_run!
    const report = run.node_states.characters_anti_ai!.outputs!.anti_ai_audit as Record<string, unknown>
    report.reports = Array.from({ length: 342 }, (_, index) => ({ path: `$.characters[${index}].description`,
      scan_status: 'completed', effective_rule_count: 51, rule_version: 'rules-v1', content_hash: `field-${index}`, total_hits: 0, hits: [] }))
    const semantic = report.semantic_review as Record<string, unknown>
    semantic.coverage = Array.from({ length: 342 }, (_, index) => ({ path: `$.characters[${index}].description`, input_hash: `field-${index}` }))
    run.checkpoint!.units!.characters!.validated_hash = report.input_hash as string
    run.checkpoint!.units!.characters!.audits!.anti_ai = JSON.parse(JSON.stringify(report))
  } })
  await page.getByRole('button', { name: '查看人物 DAG 工作流', exact: true }).click()
  await openNode(page, 'characters', 'anti_ai')
  const detail = page.locator('.setup-node-detail')
  const actual = detail.locator('[aria-label="本节点的审核报告"]')
  await expect(actual).toContainText('342 个字段 · 342 个完成 · 0 处命中')
  await expect(detail.locator('.setup-audit-report')).toHaveCount(1)
  await expect(detail.locator('.setup-audit-report__leaf')).toHaveCount(0)
  await expect(detail.locator('pre')).toHaveCount(0)
  expect(await detail.locator('*').count()).toBeLessThan(200)
  await page.screenshot({ path: 'test-results/setup-node-compact.png', fullPage: true })
  await actual.getByRole('button', { name: '查看全部扫描字段', exact: true }).click()
  await expect(actual.locator('.setup-audit-report__leaf')).toHaveCount(8)
  await actual.getByRole('button', { name: '扫描字段末页', exact: true }).click()
  await expect(actual.locator('.setup-audit-report__leaf')).toHaveCount(6)
  await expect(actual).toContainText('$.characters[341].description')
  await expand(detail, '该单元当前候选')
  await expand(detail, '检查点当前审核（可能晚于本节点）')
  await expect(detail).toContainText('与本节点报告相同，已在上方展示')
  await expect(detail.locator('.setup-audit-report')).toHaveCount(1)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(actual.getByRole('button', { name: '扫描字段首页', exact: true })).toBeVisible()
  expect(await detail.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
  expect(writes).toEqual([])
})

async function json(route: Route, data: unknown) { await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) }) }
async function setup(page: Page, options: { legacy?: boolean; empty?: boolean; step?: number; mutate?: (stages: SetupDagStage[]) => void } = {}) {
  await page.addInitScript(step => localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: step })), options.step || 1)
  const stages = inspectionCatalog(options.legacy)
  if (options.empty) stages.forEach(stage => { stage.latest_run = null })
  if (options.legacy) {
    const characters = stages[1]!.latest_run!
    characters.status = 'interrupted'
    characters.node_states.characters_generate = { status: 'interrupted', error: '旧版人物任务中断' }
  }
  options.mutate?.(stages)
  const writes: string[] = []
  await page.route(/\/api\/v1\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(route.request().method())) writes.push(path)
    if (path.endsWith('/dag/book/setup')) return json(route, { stages, active_run: null })
    if (path.endsWith('/bible')) return json(route, { id: 'b', novel_id: 'book', world_settings: [], characters: [], locations: [], style_notes: [] })
    return json(route, {})
  })
  await page.goto('/setup')
  await expect(page.locator('.setup-dag-stage')).toHaveCount(4)
  return writes
}
async function closeDetail(page: Page) {
  await page.locator('.setup-node-detail .n-base-close').click()
  await expect(page.locator('.setup-node-detail')).toHaveCount(0)
}
async function openNode(page: Page, stage: SetupStageId, action: string, unit = stage as string) {
  const id = stage === 'worldbuilding' ? `${stage}_${unit}_${action}` : `${stage}_${action}`
  await page.locator(`.vue-flow__node[data-id="${id}"] .dag-custom-node`).click()
  await expect(page.locator('.setup-node-detail')).toBeVisible()
}
function tab(page: Page, stage: SetupStageId) { return page.locator('[aria-label="查看阶段工作流"]').getByRole('button', { name: inspectionLabels[stage], exact: true }) }
async function expand(scope: Page | Locator, title: string) {
  const summary = scope.locator('summary').filter({ hasText: new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) })
  await summary.click()
}

test('all four stages expose actual deviation and Anti-AI reports without mixing candidate versions', async ({ page }) => {
  const writes = await setup(page)
  await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
  const units = ['style', 'core_rules', 'geography', 'society', 'culture', 'daily_life', 'world_review']
  for (const stage of inspectionStages) {
    await tab(page, stage).click()
    await expect(page.locator('.vue-flow__node')).toHaveCount(stage === 'worldbuilding' ? 48 : 7)
    await expect(page.locator('.n-card-header__main').last()).toHaveText(`${inspectionLabels[stage]} · 新书设置 DAG 工作流`)
    for (const unit of stage === 'worldbuilding' ? units : [stage]) {
      for (const action of ['deviation', 'anti_ai']) {
        await openNode(page, stage, action, unit)
        const key = `${stage}-${unit}`
        const actual = page.locator('.setup-node-detail [aria-label="本节点的审核报告"]')
        if (action === 'anti_ai') await expand(actual, '完整审核报告（原始证据）')
        await expect(actual).toContainText(`${key}-${action === 'deviation' ? 'deviation' : 'anti-ai'}-node-report`)
        await expect(actual).toContainText('不一致；本报告不代表当前候选的审核结果')
        await expand(page.locator('.setup-node-detail'), '该单元当前候选')
        await expect(page.locator('.setup-node-detail')).toContainText('不代表本节点执行时的候选')
        await expand(page.locator('.setup-node-detail'), '查看当前候选内容')
        await expect(page.locator('.setup-node-detail')).toContainText(`${key}-current-candidate`)
        if (action === 'deviation') {
          await expect(actual.locator('.setup-audit-report__checks')).toContainText('故事梗概符合')
          await expect(actual.locator('.setup-audit-report__checks')).toContainText('前置设定符合')
        } else {
          await actual.getByRole('button', { name: '查看全部扫描字段', exact: true }).click()
          await expect(actual.locator('.setup-audit-report__leaf')).toContainText('字段：description')
          await expect(actual.locator('.setup-audit-report__leaf')).toContainText('命中 0')
        }
        if (stage === 'worldbuilding' && unit === 'style' && action === 'deviation') await page.screenshot({ path: 'test-results/setup-node-deviation-current-candidate.png', fullPage: true })
        await closeDetail(page)
      }
    }
  }
  for (const stage of ['characters', 'locations', 'worldbuilding', 'plot_outline', 'locations'] as SetupStageId[]) await tab(page, stage).click()
  await openNode(page, 'locations', 'anti_ai')
  await expand(page.locator('.setup-node-detail [aria-label="本节点的审核报告"]'), '完整审核报告（原始证据）')
  await expect(page.locator('.setup-node-detail')).toContainText('locations-locations-anti-ai-node-report')
  await expect(page.locator('.setup-node-detail')).not.toContainText('characters-characters')
  expect(writes).toEqual([])
})

test('mixed legacy histories preserve missing audits and the true historical graph', async ({ page }) => {
  const writes = await setup(page, { legacy: true, step: 2 })
  await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
  await expect(page.locator('.n-card-header__main').last()).toHaveText('人物 · 新书设置 DAG 工作流')
  await expect(page.locator('.vue-flow__node')).toHaveCount(7)
  for (const stage of inspectionStages) {
    await tab(page, stage).click()
    await expect(page.locator('.vue-flow__node')).toHaveCount(stage === 'worldbuilding' ? 48 : 7)
    const unit = stage === 'worldbuilding' ? 'style' : stage
    for (const action of ['deviation', 'anti_ai']) {
      await openNode(page, stage, action, unit)
      const actual = page.locator('.setup-node-detail [aria-label="本节点的审核报告"]')
      if (stage === 'locations') {
        if (action === 'anti_ai') await expand(actual, '完整审核报告（原始证据）')
        await expect(actual).toContainText(`locations-locations-${action === 'deviation' ? 'deviation' : 'anti-ai'}-node-report`)
      }
      else {
        await expect(actual).toContainText('暂无本节点的审核报告：旧版工作流未包含此项审核')
        await expect(page.locator('.setup-node-detail')).toContainText('尚未执行')
        await expect(actual).not.toContainText('报告结果：通过')
      }
      await closeDetail(page)
    }
  }
  await tab(page, 'worldbuilding').click()
  await expect(page.locator('.setup-dag-notice')).toContainText('旧版记录不含新增审核')
  await expect(page.locator('.dag-custom-node.node-pending')).toHaveCount(48)
  await page.screenshot({ path: 'test-results/setup-legacy-current-48-node-definition.png', fullPage: true })
  await page.getByRole('button', { name: '查看历史记录', exact: true }).click()
  await expect(page.locator('.vue-flow__node')).toHaveCount(5)
  await expect(page.locator('.dag-custom-node.node-success')).toHaveCount(5)
  await page.locator('[data-id="worldbuilding_generate"] .dag-custom-node').click()
  await expand(page.locator('.setup-node-detail'), '执行输出与回执')
  await expand(page.locator('.setup-node-detail'), '节点实际输出')
  await expect(page.locator('.setup-node-detail')).toContainText('worldbuilding-legacy-output')
  await expand(page.locator('.setup-node-detail'), '该单元当前候选')
  await expect(page.locator('.setup-node-detail')).toContainText('旧版记录没有当前候选与新增审核检查点')
  await page.screenshot({ path: 'test-results/setup-legacy-historical-output.png', fullPage: true })
  expect(writes).toEqual([])
})

test('every stage can open pending node details before any run exists', async ({ page }) => {
  const writes = await setup(page, { empty: true })
  for (const stage of inspectionStages) {
    await page.getByRole('button', { name: `查看${inspectionLabels[stage]} DAG 工作流`, exact: true }).click()
    await expect(page.locator('.vue-flow__node')).toHaveCount(stage === 'worldbuilding' ? 48 : 7)
    await openNode(page, stage, 'anti_ai', stage === 'worldbuilding' ? 'style' : stage)
    await expect(page.locator('.setup-node-detail')).toContainText('尚未执行')
    await expect(page.locator('.setup-node-detail')).toContainText('暂无本节点的审核报告')
    await expect(page.locator('.setup-node-detail')).not.toContainText('报告结果：通过')
    await closeDetail(page)
    await page.locator('.n-modal').last().locator('.n-base-close').click()
  }
  expect(writes).toEqual([])
})

test('superseded world candidates and real scanner hits remain readable without claiming final approval', async ({ page }) => {
  const writes = await setup(page, { mutate: stages => {
    const world = stages[0]!.latest_run!
    Object.assign(world.checkpoint!.units!.style!, { superseded_by: 'world_review', revision_commit_id: 'review-commit', approved_value_hash: 'approved-style-hash', validated_hash: 'worldbuilding-style-node-hash' })
    world.checkpoint!.approved = { style: '完整复审后已确认的文风内容' }
    const people = stages[1]!.latest_run!
    const report = { passed: false, input_hash: 'characters-characters-node-hash', scan_status: 'completed', scan_id: 'failed-scan', reports: [{ path: 'characters[0].description', rule_version: 'real-scanner-version', total_hits: 1, hits: [{ pattern: '嘴角微表情', text: '嘴角上扬', start: 8, end: 12, severity: 'critical', category: '微表情', replacement_hint: '写完整姿态变化' }] }] }
    people.node_states.characters_anti_ai = { status: 'error', error: '候选仍含 AI 味条目', outputs: { anti_ai_audit: report, setup_unit: 'characters' } }
    people.node_states.characters_deviation = { status: 'error', outputs: { deviation_audit: { passed: false, input_hash: 'characters-characters-node-hash', checks: { synopsis: true, genre: true, requirements: false, predecessors: true }, issues: [{ description: '人物职业偏离作者要求' }] } } }
  } })
  await page.getByRole('button', { name: '查看文风 / 世界观 DAG 工作流', exact: true }).click()
  await openNode(page, 'worldbuilding', 'deviation', 'style')
  await expect(page.locator('.setup-node-detail')).toContainText('此候选已被完整复审替代')
  await expand(page.locator('.setup-node-detail'), '被完整复审替代的候选（历史）')
  await expand(page.locator('.setup-node-detail'), '查看被替代的候选与已确认内容')
  await expand(page.locator('.setup-node-detail'), '报告对应的内容版本（技术详情）')
  await expect(page.locator('.setup-node-detail')).toContainText('完整复审后已确认的文风内容')
  await expect(page.locator('.setup-node-detail')).toContainText('仅对应历史候选，不代表完整复审后的内容')
  await expect(page.locator('.setup-node-detail [aria-label="该单元当前候选"]')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/setup-superseded-candidate.png', fullPage: true })
  await closeDetail(page)
  await tab(page, 'characters').click()
  await openNode(page, 'characters', 'deviation')
  const actual = page.locator('.setup-node-detail [aria-label="本节点的审核报告"]')
  await expect(actual.locator('.setup-audit-report__checks')).toContainText('作者要求不符合')
  await expect(actual.getByText('人物职业偏离作者要求', { exact: true })).toBeVisible()
  await closeDetail(page)
  await openNode(page, 'characters', 'anti_ai')
  await expect(actual).toContainText('报告结果：未通过')
  await expect(actual.locator('.setup-audit-report__leaf')).toContainText('字段：characters[0].description')
  await expect(actual.getByText('嘴角微表情', { exact: true })).toBeVisible()
  await expect(actual.getByText('嘴角上扬', { exact: true })).toBeVisible()
  await expect(actual.getByText('修改建议：写完整姿态变化', { exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/setup-node-anti-ai-hits.png', fullPage: true })
  expect(writes).toEqual([])
})

test('audit shortcuts open every unit report at normal text size, including on a narrow viewport', async ({ page }) => {
  const writes = await setup(page)
  await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
  for (const stage of inspectionStages) {
    await tab(page, stage).click()
    const shortcuts = page.locator('.setup-dag-audit-shortcut')
    await expect(shortcuts).toHaveCount(stage === 'worldbuilding' ? 7 : 1)
    for (const shortcut of await shortcuts.all()) {
      for (const action of ['偏离检测', 'Anti-AI']) {
        await shortcut.getByRole('button', { name: new RegExp(` · ${action}$`) }).click()
        await expect(page.locator('.setup-node-detail [aria-label="本节点的审核报告"]')).toContainText('报告结果：通过')
        await closeDetail(page)
      }
    }
  }
  await tab(page, 'worldbuilding').click()
  await page.setViewportSize({ width: 375, height: 720 })
  const first = page.getByRole('button', { name: '文风公约 · Anti-AI', exact: true })
  await first.click()
  await expand(page.locator('.setup-node-detail [aria-label="本节点的审核报告"]'), '完整审核报告（原始证据）')
  await expect(page.locator('.setup-node-detail')).toContainText('worldbuilding-style-anti-ai-node-report')
  await closeDetail(page)
  await page.screenshot({ path: 'test-results/setup-audit-shortcuts-mobile.png', fullPage: true })
  expect(writes).toEqual([])
})

test('normal writing nodes still select, while external read-only nodes emit details without changing the writing selection', async ({ page }) => {
  await page.goto('/dag-canvas')
  await page.locator('[data-id="writer"] .dag-custom-node').click()
  await expect(page.getByTestId('selected-writing-node')).toHaveText('writer')
  await expect(page.getByTestId('opened-node-detail')).toHaveText('writer')
  await page.getByRole('button', { name: '查看设置流程', exact: true }).click()
  await page.locator('[data-id="setup-audit"] .dag-custom-node').click()
  await expect(page.getByTestId('opened-node-detail')).toHaveText('setup-audit')
  await expect(page.getByTestId('selected-writing-node')).toHaveText('writer')
})
