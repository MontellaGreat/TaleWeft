import { test, expect, type Route } from '@playwright/test'

const session = (status: string, extras: Record<string, unknown> = {}) => ({
  session: { id: 'map-session', operation: 'bible.setup.locations', node_key: 'bible-locations', policy: 'FULL_INTERACTIVE', status }, ...extras,
})
async function json(route: Route, data: unknown) {
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
}

for (const failure of ['persistence', 'network']) {
  test(`location ${failure} failure exits loading and a retry displays the saved map`, async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('plotpilot:novel-wizard-ui:book', JSON.stringify({ v: 4, novelId: 'book', savedAt: Date.now(), lastStep: 3 }))
    })
    let generations = 0, committed = false
    let savedLocations: unknown
    const calls: string[] = []
    await page.route(/\/api\/v1\//, async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/generate-stream')) {
        generations++
        expect(new URL(route.request().url()).searchParams.get('stage')).toBe('locations')
        await route.fulfill({ contentType: 'text/event-stream', body: 'event: data\ndata: {"type":"approval_required","session_id":"map-session"}\n\n' })
        return
      }
      if (path.endsWith('/ai-invocations/map-session')) return json(route, session('awaiting_pre_call_review'))
      if (path.endsWith('/resume')) {
        calls.push('resume')
        if (failure === 'network' && generations === 1) return route.abort('failed')
        return json(route, session('awaiting_acceptance', { attempt: { id: 'a', session_id: 'map-session', status: 'succeeded', content: '{"locations":[{"id":"capital","name":"都城"}]}' } }))
      }
      if (path.endsWith('/accept')) {
        calls.push('accept')
        return json(route, session('awaiting_commit', { decision: { id: 'd' } }))
      }
      if (path.endsWith('/commits')) {
        calls.push('commit')
        if (failure === 'persistence' && generations === 1) return json(route, session('blocked', { commit: { id: 'c', status: 'failed', error: '地点父子关系校验失败', steps: [] } }))
        committed = true
        return json(route, session('completed', { commit: { id: 'c', status: 'succeeded', steps: [] } }))
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
    await expect(page.getByText(failure === 'persistence' ? '地点父子关系校验失败' : 'Network Error', { exact: true })).toBeVisible()
    await expect(page.locator('.step-generating')).toHaveCount(0)
    await page.getByRole('button', { name: '开始生成', exact: true }).click()
    await expect(page.getByText('地图生成完成', { exact: true })).toBeVisible()
    await expect(page.getByPlaceholder('地点名').first()).toHaveValue('都城')
    await expect(page.locator('.step-generating')).toHaveCount(0)
    expect(calls.slice(-3)).toEqual(['resume', 'accept', 'commit'])
    expect(generations).toBe(2)
    await page.getByRole('button', { name: '确认修改并继续', exact: true }).click()
    await expect.poll(() => savedLocations).toEqual([
      expect.objectContaining({ id: 'capital', parent_id: null }),
      expect.objectContaining({ id: 'palace', parent_id: 'capital' }),
    ])
  })
}
