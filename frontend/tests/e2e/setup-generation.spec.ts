import { test, expect, type Route } from '@playwright/test'
import type { SetupDagRun, SetupDagStage, SetupStageId } from '../../src/api/setupDag'

const stageIds: SetupStageId[] = ['worldbuilding', 'characters', 'locations', 'plot_outline']
const labels = ['文风与世界观', '人物', '地图', '剧情总纲']
function graph(stage: SetupStageId) {
  const title = labels[stageIds.indexOf(stage)]!
  const steps = [
    ['prepare', '准备梗概与已确认设定'], ['generate', `生成${title}`],
    ['validate', `校验${title}结构与关联`], ['deviation', '偏离审核与修正'], ['anti_ai', '去 AI 味审核与修正'],
    ['commit', '保存资料与关联'], ['verify', '回读确认保存结果'],
  ]
  return {
    nodes: steps.map(([action, label], i) => ({ id: `${stage}_${action}`, type: 'setup_step', label: label!, position: { x: 0, y: i * 100 }, config: { prompt_variables: { action, unit: stage } } })),
    edges: steps.slice(1).map(([action], i) => ({ id: `${stage}_edge_${i}`, source: `${stage}_${steps[i]![0]}`, target: `${stage}_${action}` })),
  }
}
function snapshot(status: SetupDagRun['status']): SetupDagRun {
  const failed = status === 'error'
  const complete = status === 'completed'
  return {
    run_id: 'map-run', novel_id: 'book', stage: 'locations', status, workflow_version: 3, graph: graph('locations'),
    node_states: Object.fromEntries(graph('locations').nodes.map((node, i) => [node.id, {
      status: complete || i === 0 ? 'success' : i === 1 ? failed ? 'error' : 'running' : 'pending',
    }])),
    progress: { completed: complete ? 7 : 1, total: 7, percent: complete ? 100 : 14,
      current_node_id: complete ? null : 'locations_generate', current_node_label: complete ? null : '正在生成地图',
      message: complete ? '地图生成完成' : '正在生成地图' },
    error: failed ? '地点父子关系校验失败' : null,
    started_at: new Date().toISOString(), completed_at: status === 'running' ? null : new Date().toISOString(),
  }
}
async function json(route: Route, data: unknown) {
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
}

function worldSnapshot(status: SetupDagRun['status']): SetupDagRun {
  const units = ['style', 'core_rules', 'geography', 'society', 'culture', 'daily_life', 'world_review']
  const actions = ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
  const nodes = units.flatMap(unit => (unit === 'world_review' ? ['prepare', 'validate', 'deviation', 'anti_ai', 'commit', 'verify'] : actions)
    .map((action, i) => ({ id: `worldbuilding_${unit}_${action}`, type: 'setup_step', label: `${unit} ${action}`, position: { x: i * 260, y: 80 }, config: { prompt_variables: { unit, action } } })))
  return { ...snapshot(status), stage: 'worldbuilding', run_id: 'world-run',
    graph: { nodes, edges: nodes.slice(1).map((node, i) => ({ id: `edge-${i}`, source: nodes[i]!.id, target: node.id })) },
    node_states: Object.fromEntries(nodes.map((node, i) => [node.id, { status: i < 18 ? 'success' : i === 18 ? status === 'running' ? 'running' : 'error' : 'pending', outputs: { draft: { style: '未经审核的候选，禁止作为可编辑结果' } } }])),
    progress: { completed: 18, total: 48, percent: 37, current_node_id: nodes[18]!.id, current_node_label: '地理生态去 AI 味审核', message: '前两项已保存，地理生态等待审核通过' },
    error: status === 'error' ? '地理生态去 AI 味审核失败，请继续生成' : null }
}

test('world units stay locked after reopen, continue checkpoints, and confirm a full restart', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: 5 })))
  let latest = worldSnapshot('error')
  const starts: unknown[] = []
  let saved = 0
  await page.route(/\/api\/v1\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/dag/book/setup/run')) {
      starts.push(route.request().postDataJSON())
      latest = { ...worldSnapshot('running'), resumed_node_count: starts.length === 1 ? 18 : 0 }
      return json(route, latest)
    }
    if (path.endsWith('/dag/book/setup/stop')) { latest = worldSnapshot('cancelled'); return json(route, latest) }
    if (path.endsWith('/dag/book/setup/runs/world-run')) return json(route, latest)
    if (path.endsWith('/dag/book/setup')) return json(route, {
      stages: stageIds.map((id, i) => ({ id, label: labels[i], dependencies: stageIds.slice(0, i), graph: id === 'worldbuilding' ? latest.graph : graph(id), latest_run: id === 'worldbuilding' ? latest : null })),
      active_run: latest.status === 'running' ? latest : null,
    })
    if (route.request().method() === 'PUT') { saved++; return json(route, {}) }
    if (path.endsWith('/bible')) return json(route, { id: 'b', novel_id: 'book', world_settings: [], characters: [], locations: [], style_notes: [{ content: '已通过审核的文风公约' }] })
    if (path.includes('/worldbuilding')) return json(route, { dimensions: { core_rules: { energy_system: '核心规则已保存' } } })
    return json(route, {})
  })
  await page.goto('/setup')
  await expect(page.getByRole('button', { name: '继续生成', exact: true })).toBeEnabled()
  await expect(page.locator('.n-input')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认修改并继续', exact: true })).toHaveCount(0)
  await expect(page.getByText('审核通过，已保存并回读', { exact: true })).toHaveCount(2)
  await page.getByText('查看已保存内容（只读）', { exact: true }).click()
  await expect(page.getByText('已通过审核的文风公约', { exact: true })).toBeVisible()
  await expect(page.getByText('未经审核的候选，禁止作为可编辑结果', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
  await expect(page.locator('.vue-flow__node')).toHaveCount(48)
  await expect(page.locator('[data-id="worldbuilding_geography_anti_ai"] .dag-custom-node')).toHaveClass(/node-error/)
  await expect(page.locator('[data-id="worldbuilding_core_rules_verify"] .dag-custom-node')).toHaveClass(/node-success/)
  await page.screenshot({ path: 'test-results/setup-world-48-nodes.png', fullPage: true })
  await page.locator('.n-modal').last().locator('.n-base-close').click()
  await page.getByRole('button', { name: '继续生成', exact: true }).click()
  await expect(page.getByText('已沿用 18 个成功步骤，从上次进度继续。', { exact: true })).toBeVisible()
  expect(starts).toEqual([{ stage: 'worldbuilding' }])
  await page.reload()
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toBeVisible()
  expect(starts).toHaveLength(1)
  await page.getByRole('button', { name: '停止生成', exact: true }).click()
  await page.getByRole('button', { name: '从头重新生成', exact: true }).click()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.locator('.n-dialog')).toHaveCount(0)
  expect(starts).toHaveLength(1)
  await page.getByRole('button', { name: '从头重新生成', exact: true }).click()
  await page.locator('.n-dialog').getByRole('button', { name: '从头重新生成', exact: true }).click()
  await expect.poll(() => starts).toEqual([{ stage: 'worldbuilding' }, { stage: 'worldbuilding', restart: true }])
  expect(saved).toBe(0)
})

test('completed world stays read only while failed characters resume from their own stage', async ({ page }) => {
  let characterRun: SetupDagRun = { ...snapshot('error'), stage: 'characters', run_id: 'character-run', graph: graph('characters'), error: '人物去 AI 味审核未完成',
    node_states: Object.fromEntries(graph('characters').nodes.map((node, i) => [node.id, { status: i < 4 ? 'success' : i === 4 ? 'error' : 'pending' }])),
    progress: { completed: 4, total: 7, percent: 57, current_node_id: 'characters_anti_ai', current_node_label: '人物去 AI 味审核', message: '人物去 AI 味审核与修正尚未通过' } }
  const starts: unknown[] = []
  let saves = 0
  await page.route(/\/api\/v1\//, async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/dag/book/setup/run')) {
      starts.push(route.request().postDataJSON())
      characterRun = { ...characterRun, status: 'running', error: null }
      return json(route, characterRun)
    }
    if (path.endsWith('/dag/book/setup')) return json(route, {
      stages: stageIds.map((id, i) => ({ id, label: labels[i], dependencies: stageIds.slice(0, i), graph: graph(id), latest_run: id === 'characters' ? characterRun : id === 'worldbuilding' ? { ...snapshot('completed'), stage: id, graph: graph(id), started_at: '2026-10-23T15:00:00Z' } : null })), active_run: null,
    })
    if (path.endsWith('/dag/book/setup/runs/character-run')) return json(route, characterRun)
    if (route.request().method() === 'PUT') { saves++; return json(route, {}) }
    if (path.endsWith('/bible')) return json(route, { id: 'b', novel_id: 'book', world_settings: [], characters: [], locations: [], style_notes: [{ content: '已审核并保存的世界观文风' }] })
    return json(route, {})
  })
  await page.goto('/setup')
  await expect(page.getByRole('button', { name: '继续生成', exact: true })).toBeVisible()
  await expect(page.getByText('人物去 AI 味审核未完成', { exact: true })).toHaveCount(2)
  await page.locator('.wizard-step-clickable').first().click()
  await expect(page.getByRole('button', { name: '前往人物继续', exact: true })).toBeVisible()
  await expect(page.getByText('人物阶段尚未完成，已使用的前置资料暂时只读。请先继续人物的审核与保存。', { exact: false })).toBeVisible()
  await expect(page.locator('.n-input')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^(继续生成|开始生成|重新生成|从头重新生成|确认修改并继续)$/ })).toHaveCount(0)
  await page.getByText('查看已保存内容（只读）', { exact: true }).click()
  await expect(page.getByText('已审核并保存的世界观文风', { exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/setup-prerequisite-readonly.png', fullPage: true })
  expect(starts).toHaveLength(0)
  expect(saves).toBe(0)
  await page.getByRole('button', { name: '前往人物继续', exact: true }).click()
  await page.getByRole('button', { name: '继续生成', exact: true }).click()
  await expect.poll(() => starts).toEqual([{ stage: 'characters' }])
})

for (const failure of ['persistence', 'network']) {
  test(`location ${failure} failure retries through visible DAG and preserves edited map hierarchy`, async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: 3 }))
    })
    let generations = 0, committed = false
    let savedLocations: unknown
    let finishRun!: () => void
    const completion = new Promise<void>(resolve => { finishRun = resolve })
    await page.route(/\/api\/v1\//, async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/dag/book/setup/run')) {
        expect(route.request().postDataJSON().stage).toBe('locations')
        generations++
        if (generations === 1) {
          if (failure === 'network') return route.abort('failed')
          return json(route, snapshot('error'))
        }
        return json(route, snapshot('running'))
      }
      if (path.endsWith('/dag/book/setup/runs/map-run')) {
        await completion
        committed = true
        return json(route, snapshot('completed'))
      }
      if (path.endsWith('/dag/book/setup')) {
        const stages: SetupDagStage[] = stageIds.map((id, i) => ({
          id, label: labels[i]!, dependencies: stageIds.slice(0, i), graph: graph(id), latest_run: null,
        }))
        return json(route, { stages, active_run: null })
      }
      if (path.endsWith('/bible') && route.request().method() === 'PUT') {
        savedLocations = route.request().postDataJSON().locations
        return json(route, {})
      }
      if (path.endsWith('/bible')) return json(route, {
        id: 'b', novel_id: 'book', world_settings: [], characters: [], timeline_notes: [], style_notes: [],
        locations: committed ? [
          { id: 'capital', name: '都城', location_type: '城市', description: '地图已保存', parent_id: null },
          { id: 'palace', name: '王宫', location_type: '建筑', description: '都城内', parent_id: 'capital' },
        ] : [],
      })
      return json(route, {})
    })
    await page.goto('/setup')
    await page.getByRole('button', { name: '开始生成', exact: true }).click()
    await expect(page.locator('.step-panel .n-alert')).toContainText(failure === 'persistence' ? '地点父子关系校验失败' : 'Network Error')
    await expect(page.locator('.step-generating')).toHaveCount(0)
    await expect(page.getByPlaceholder('地点名')).toHaveCount(0)
    await expect(page.getByText('暂不可编辑或进入下一步', { exact: false })).toBeVisible()
    await page.getByRole('button', { name: '继续生成', exact: true }).click()
    await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '14')
    await page.getByRole('button', { name: '查看 DAG 工作流', exact: true }).click()
    await expect(page.locator('.vue-flow__node')).toHaveCount(7)
    await expect(page.locator('.vue-flow__node[data-id="locations_generate"]')).toHaveClass(/is-current/)
    await expect(page.locator('.vue-flow__node[data-id="locations_generate"] .dag-custom-node')).toHaveClass(/node-running/)
    await expect(page.locator('.vue-flow__node[data-id="locations_prepare"] .dag-custom-node')).toHaveClass(/node-success/)
    await expect(page.locator('.vue-flow__minimap')).toBeVisible()
    await expect(page.locator('.vue-flow__edge')).toHaveCount(6)
    await page.screenshot({ path: `test-results/setup-dag-${failure}.png`, fullPage: true })
    await page.locator('.n-modal').last().locator('.n-base-close').click()
    finishRun()
    await expect(page.getByText('地图生成完成', { exact: true })).toBeVisible()
    await expect(page.getByPlaceholder('地点名').first()).toHaveValue('都城')
    await expect(page.locator('.step-generating')).toHaveCount(0)
    await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(generations).toBe(2)
    for (const width of [1400, 768, 375]) {
      await page.setViewportSize({ width, height: 720 })
      await page.screenshot({ path: `test-results/setup-footer-${failure}-${width}.png`, fullPage: true })
      await expect.poll(() => page.locator('.wizard-footer').evaluate(footer => {
        const card = footer.closest('.n-card')!.getBoundingClientRect()
        const buttons = [...footer.querySelectorAll('button')].map(button => button.getBoundingClientRect())
        return { fits: buttons.every(button => button.left >= card.left && button.right <= card.right && button.top >= card.top && button.bottom <= card.bottom && button.bottom <= window.innerHeight), width: window.innerWidth,
          card: [card.x, card.y, card.width, card.height], buttons: buttons.map(button => [button.x, button.y, button.width, button.height]) }
      })).toMatchObject({ fits: true })
      await expect(page.getByRole('button', { name: '确认修改并继续', exact: true })).toBeInViewport()
    }
    await page.screenshot({ path: `test-results/setup-footer-${failure}-375.png`, fullPage: true })
    await page.getByPlaceholder('地点名').first().fill('新都城')
    await page.getByRole('button', { name: '确认修改并继续', exact: true }).click()
    await expect.poll(() => savedLocations).toEqual([
      expect.objectContaining({ id: 'capital', name: '新都城', parent_id: null }),
      expect.objectContaining({ id: 'palace', parent_id: 'capital' }),
    ])
  })
}
