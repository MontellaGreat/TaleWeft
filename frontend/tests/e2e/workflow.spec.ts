import { test, expect, type Page, type Route } from '@playwright/test'
import { createHash } from 'node:crypto'

const chapter = { id: 'chapter-1', novel_id: 'book', number: 1, title: '第一章', content: '原正文',
  revision: 1, content_hash: createHash('sha256').update('原正文').digest('hex'), etag: 'etag-1', status: 'draft', word_count: 3,
  created_at: '2026-10-13', updated_at: '2026-10-13' }
async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}
async function chapterFixture(page: Page, save: (route: Route) => Promise<void>) {
  await page.route(/\/api\/(?:v1|stats|book)\//, async route => {
    const path = new URL(route.request().url()).pathname
    expect(route.request().headers().authorization).toBe('Bearer browser-fixture-session-token-32-characters')
    if (path.endsWith('/chapters/1') && route.request().method() === 'PUT') return save(route)
    if (path.endsWith('/chapters/1')) return json(route, chapter)
    if (path.endsWith('/chapters')) return json(route, [chapter])
    if (path.endsWith('/review')) return json(route, { status: 'pending', memo: '' })
    if (path.endsWith('/structure')) return json(route, { word_count: 3, paragraph_count: 1 })
    if (path.endsWith('/inference-evidence')) return json(route, { success: true, data: { story_node_id: null, facts: [] } })
    return json(route, { facts: [], provenance: [], chapters: [], total_words: 3 })
  })
}

test('save ACK preserves edits typed while the request is pending, then saves against the new revision', async ({ page }) => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  const writes: Record<string, unknown>[] = []
  await chapterFixture(page, async route => {
    const body = route.request().postDataJSON()
    writes.push(body)
    expect(route.request().headers()['if-match']).toBe(writes.length === 1 ? 'etag-1' : 'etag-2')
    if (writes.length === 1) await pending
    await json(route, { ...chapter, content: body.content, revision: writes.length + 1,
      content_hash: createHash('sha256').update(String(body.content)).digest('hex'),
      save_request_id: body.save_request_id, etag: `etag-${writes.length + 1}` })
  })
  await page.goto('/book/book/chapter/1')
  const editor = page.locator('.content-editor textarea')
  await expect(editor).toHaveValue('原正文')
  await editor.fill('第一次编辑')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(1)
  await editor.fill('保存期间继续编辑')
  release()
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await expect(editor).toHaveValue('保存期间继续编辑')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(2)
  expect(writes.map(body => body.expected_revision)).toEqual([1, 2])
  expect(writes.map(body => body.content)).toEqual(['第一次编辑', '保存期间继续编辑'])
  await expect(page.locator('.chapter-header')).toContainText('已保存')
})

test('409 shows a real conflict dialog and retains local prose when the author declines reload', async ({ page }) => {
  await chapterFixture(page, route => json(route, { detail: {
    code: 'chapter_revision_conflict', message: 'stale', current: { ...chapter, revision: 2, content: '服务器正文' },
    diff: '- 本地正文\n+ 服务器正文',
  } }, 409))
  await page.goto('/book/book/chapter/1')
  const editor = page.locator('.content-editor textarea')
  await expect(editor).toHaveValue('原正文')
  await editor.fill('本地正文')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('章节版本冲突：本地正文已保留')).toBeVisible()
  await page.getByRole('button', { name: '保留本地编辑', exact: true }).click()
  await expect(editor).toHaveValue('本地正文')
  await expect(page.locator('.chapter-header')).toContainText('未保存')
})

test('chapter title and prose save together and edits during the request remain dirty', async ({ page }) => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  const writes: Record<string, unknown>[] = []
  await chapterFixture(page, async route => {
    const body = route.request().postDataJSON()
    writes.push(body)
    if (writes.length === 1) await pending
    await json(route, { ...chapter, title: body.title, content: body.content,
      revision: writes.length + 1, save_request_id: body.save_request_id,
      content_hash: createHash('sha256').update(String(body.content)).digest('hex'),
      etag: `etag-${writes.length + 1}` })
  })
  await page.goto('/book/book/chapter/1')
  const title = page.getByRole('textbox', { name: '章节标题', exact: true })
  const editor = page.locator('.content-editor textarea')
  await expect(title).toHaveValue('第一章')
  await title.fill('雨夜的铜铃')
  await editor.fill('林澈把铜铃交给了沈岚。')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(1)
  await title.fill('雨夜交接')
  release()
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await expect(title).toHaveValue('雨夜交接')
  await expect(editor).toHaveValue('林澈把铜铃交给了沈岚。')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => writes.length).toBe(2)
  expect(writes.map(body => body.expected_revision)).toEqual([1, 2])
  expect(writes.map(body => body.title)).toEqual(['雨夜的铜铃', '雨夜交接'])
  expect(writes.map(body => body.content)).toEqual(['林澈把铜铃交给了沈岚。', '林澈把铜铃交给了沈岚。'])
  await expect(page.locator('.chapter-header')).toContainText('已保存')
})

test('an old title in a successful save response cannot acknowledge a rename', async ({ page }) => {
  await chapterFixture(page, async route => {
    const body = route.request().postDataJSON()
    return json(route, { ...chapter, content: body.content, revision: 2,
      content_hash: createHash('sha256').update(String(body.content)).digest('hex'),
      save_request_id: body.save_request_id })
  })
  await page.route('**/save-requests/*', route => json(route, { status: 'unconfirmed' }))
  await page.goto('/book/book/chapter/1')
  const title = page.getByRole('textbox', { name: '章节标题', exact: true })
  await expect(title).toHaveValue('第一章')
  await title.fill('必须被确认的新章名')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText(/尚未得到提交回执/)).toBeVisible()
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await expect(title).toHaveValue('必须被确认的新章名')
})

test('a mismatched successful response and unconfirmed receipt keep the editor dirty', async ({ page }) => {
  await chapterFixture(page, async route => {
    const submitted = route.request().postDataJSON()
    return json(route, { ...chapter, content: submitted.content, revision: 2,
      content_hash: 'wrong-hash', save_request_id: submitted.save_request_id })
  })
  await page.route('**/save-requests/*', route => json(route, { status: 'unconfirmed' }))
  await page.goto('/book/book/chapter/1')
  const editor = page.locator('.content-editor textarea')
  await expect(editor).toHaveValue('原正文')
  await editor.fill('不得误报已保存的稿件')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText(/尚未得到提交回执/)).toBeVisible()
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await expect(editor).toHaveValue('不得误报已保存的稿件')
})

test('lost save response reconciles this request receipt and preserves later edits', async ({ page }) => {
  let submitted: Record<string, unknown> = {}
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await chapterFixture(page, async route => {
    submitted = route.request().postDataJSON()
    return json(route, { detail: { code: 'chapter_save_unconfirmed', save_request_id: submitted.save_request_id } }, 503)
  })
  await page.route('**/save-requests/*', async route => {
    expect(route.request().url()).toContain(String(submitted.save_request_id))
    await pending
    const hash = createHash('sha256').update(String(submitted.content)).digest('hex')
    return json(route, { request_id: submitted.save_request_id, novel_id: 'book', chapter_number: 1,
      chapter_id: chapter.id, revision: 2, status: 'committed', content_hash: hash,
      chapter: { ...chapter, revision: 2, content_hash: hash,
      etag: 'etag-2', content: submitted.content, save_request_id: submitted.save_request_id } })
  })
  await page.goto('/book/book/chapter/1')
  const editor = page.locator('.content-editor textarea')
  await expect(editor).toHaveValue('原正文')
  await editor.fill('已提交但回包丢失的稿件')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => submitted.content).toBe('已提交但回包丢失的稿件')
  await editor.fill('查询回执期间继续编辑')
  release()
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeEnabled()
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await expect(editor).toHaveValue('查询回执期间继续编辑')
})

test('committed candidate with failed final audit never appears archived and can be retried', async ({ page }) => {
  const candidate = { candidate_id: 'candidate-1', run_id: 'run-1', novel_id: 'book', chapter_number: 1,
    attempt: 1, base_revision: 1, base_hash: 'hash-1', base_content: '原正文', content: '候选正文',
    content_hash: 'candidate-hash', diff: '- 原正文\n+ 候选正文', status: 'committed', created_at: '2026-10-13',
    can_confirm: false, stale: false, is_generating: false, run_epoch: 'epoch-1', current_revision: 2,
    audit: { committed_revision: 2, reasons: [], final_audit: { scan_status: 'completed',
      persistence_status: 'failed', run_id: 'audit-1', content_hash: 'candidate-hash' } } }
  let retries = 0
  await page.route('**/api/v1/anti-ai/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/rewrite-policy')) return json(route, { mode: 'review', max_attempts: 2, max_severity: 10 })
    if (path.endsWith('/confirm')) { retries++; return json(route, { status: 'committed', candidate }) }
    return json(route, [candidate])
  })
  await page.goto('/review')
  await expect(page.locator('.candidate-card h4')).toContainText('最终审计待核验')
  await expect(page.locator('.candidate-card')).not.toContainText('已提交并归档')
  await page.getByRole('button', { name: '重新核验最终审计', exact: true }).click()
  await expect.poll(() => retries).toBe(1)
  await expect(page.getByText('提交或最终审计尚未确认完成，请刷新候选记录并重新核验')).toBeVisible()
  await expect(page.locator('.candidate-card h4')).toContainText('最终审计待核验')
})

test('SSE reconnect reuses request identity and cursor and deduplicates replayed prose', async ({ page }) => {
  const requests: { id: string; cursor: number }[] = []
  await page.route('**/generate-chapter-stream', async route => {
    const body = route.request().postDataJSON(), id = body.request_id
    expect(route.request().headers()['idempotency-key']).toBe(id)
    requests.push({ id, cursor: body.resume_after })
    const events = requests.length === 1
      ? [{ type: 'request_started', sequence: 1 }, { type: 'chunk', sequence: 2, text: '第一段' }]
      : [{ type: 'chunk', sequence: 2, text: '第一段' }, { type: 'chunk', sequence: 3, text: '第二段' },
          { type: 'done', sequence: 4, content: '第一段第二段' }, { type: 'terminal', sequence: 5, status: 'completed' }]
    await route.fulfill({ contentType: 'text/event-stream', body: events.map(event =>
      `data: ${JSON.stringify({ ...event, request_id: id })}\r\n\r\n`).join('') })
  })
  await page.goto('/stream')
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByTestId('terminal')).toHaveText('completed')
  await expect(page.getByTestId('chunks')).toHaveText('第一段第二段')
  await expect(page.getByTestId('done')).toHaveText('第一段第二段')
  await expect(page.getByTestId('error')).toBeEmpty()
  expect(requests).toEqual([{ id: requests[0]!.id, cursor: 0 }, { id: requests[0]!.id, cursor: 2 }])
})

test('EOF without a terminal preserves prose and reports interruption instead of completion', async ({ page }) => {
  await page.route('**/generate-chapter-stream', async route => {
    const id = route.request().postDataJSON().request_id
    await route.fulfill({ contentType: 'text/event-stream', body:
      `data: ${JSON.stringify({ type: 'chunk', sequence: 1, request_id: id, text: '保留片段' })}\n\n` })
  })
  await page.goto('/stream')
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByTestId('error')).toContainText('未收到完成确认')
  await expect(page.getByTestId('chunks')).toHaveText('保留片段')
  await expect(page.getByTestId('done')).toBeEmpty()
  await expect(page.getByTestId('terminal')).toBeEmpty()
})

test('audit progress shows scanning, archiving and persistence failure, then recovers after reconnect', async ({ page }) => {
  let stage = 'scanning'
  let disconnected = false
  let queries = 0
  await page.route(/\/api\/(?:v1|stats|book)\//, async route => {
    const url = new URL(route.request().url()), path = url.pathname
    if (/\/novels\/?$/.test(path)) return json(route, [{ id: 'book', title: '浏览器验证作品' }])
    if (path.includes('/audit-progress/')) {
      queries++
      expect(path.endsWith('/book')).toBe(true)
      if (disconnected) return route.abort('connectionfailed')
      return json(route, [{ run_id: 'scan-run', chapter_number: 2, stage,
        scan_status: stage === 'scanning' ? 'running' : 'completed',
        persistence_status: stage === 'completed' ? 'committed' : stage === 'failed' ? 'failed' : 'not_requested',
        content_hash: createHash('sha256').update('本次正文').digest('hex'), rule_version: 'rules-version',
        job_id: 'write-job', chapter_revision: 3, error: stage === 'failed' ? '审计数据库提交失败' : null }])
    }
    if (path.endsWith('/stats')) return json(route, { total_prompts: 1, anti_ai_prompts: 1,
      cliche_patterns: 1, categories_count: 1, layers: {}, observed_audit_runs: 0, failed_audit_runs: 0 })
    return json(route, [])
  })
  await page.goto('/dashboard/book')
  await page.getByText('自动审计记录', { exact: true }).click()
  const progress = page.getByRole('status').filter({ hasText: '独立扫描进度' })
  await expect(progress).toContainText('正在扫描')
  await expect(progress).toContainText('审计归档 等待提交')
  await expect(progress).not.toContainText('扫描及归档完成')
  stage = 'persisting'
  await page.getByRole('button', { name: '刷新记录', exact: true }).click()
  await expect(progress).toContainText('扫描完成，正在归档')
  stage = 'failed'
  await page.getByRole('button', { name: '刷新记录', exact: true }).click()
  await expect(progress).toContainText('扫描或归档失败')
  await expect(progress).toContainText('审计数据库提交失败')
  await expect(progress).not.toContainText('审计归档 已提交')
  disconnected = true
  await page.getByRole('button', { name: '刷新记录', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('无法确认本次审计终态')
  await expect(progress).toHaveCount(0)
  disconnected = false
  stage = 'completed'
  // Real component timer re-queries the independent durable progress endpoint.
  await expect(progress).toContainText('扫描及归档完成', { timeout: 6000 })
  await expect(progress).toContainText('审计归档 已提交')
  expect(queries).toBeGreaterThanOrEqual(5)
})
