/** Real source Vue acceptance. Chapter edits/reruns use visible controls.
 * Registered prop setup is separately declared by the launcher public APIs.
 * No API mocks.
 * Never enable trace/HAR/storageState or print raw errors: launcher fragments
 * and request headers carry the private local-session capability.
 */
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import fs from 'node:fs/promises'
import path from 'node:path'

const env = process.env
const output = env.PLOTPILOT_QA_OUTPUT
const origin = env.PLOTPILOT_QA_UI_ORIGIN
const novel = env.PLOTPILOT_QA_NOVEL
const token = env.PLOTPILOT_SESSION_TOKEN
const timeout = Number(env.PLOTPILOT_QA_TIMEOUT_MS || 1200000)
const title = '浏览器验收·一百零七号钥匙'
const result = { status: 'running', novel_id: novel, passed: [], screenshots: [], evidence: {} }
let stage = 'configuration'
let browser
let pageForDiagnostics

class SafeCheckFailed extends Error {}
function requireCheck(condition, message) {
  if (!condition) throw new SafeCheckFailed(message)
}
const sha = text => createHash('sha256').update(text, 'utf8').digest('hex')
const mark = (name, evidence = {}) => {
  result.passed.push(name)
  result.evidence[name] = evidence
  process.stdout.write('UI_PASS ' + name + '\n')
}
async function persist() {
  await fs.writeFile(path.join(output, 'browser-result.json'), JSON.stringify(result, null, 2), 'utf8')
}

try {
  requireCheck(output && origin && novel && token && env.PLOTPILOT_QA_FRONTEND, 'Run through the Python isolation launcher')
  const require = createRequire(path.join(env.PLOTPILOT_QA_FRONTEND, 'package.json'))
  const { chromium, expect } = require('@playwright/test')
  const setup = JSON.parse(await fs.readFile(path.join(output, 'public-api-baseline-setup.json'), 'utf8'))
  requireCheck(setup.scenario === 'registered_prop_lifecycle' && setup.prop_id && setup.baseline_holder === '沈舟',
    'Registered prop public API setup receipt is missing')
  mark('public_API_baseline_setup', setup)
  stage = 'browser_launch'
  browser = await chromium.launch({
    headless: env.PLOTPILOT_QA_HEADED !== '1',
    ...(env.PLOTPILOT_QA_BROWSER_EXECUTABLE ? { executablePath: env.PLOTPILOT_QA_BROWSER_EXECUTABLE } : {}),
  })
  const context = await browser.newContext({ viewport: { width: 1536, height: 1100 }, locale: 'zh-CN' })
  const page = await context.newPage()
  pageForDiagnostics = page
  page.setDefaultTimeout(30000)
  page.setDefaultNavigationTimeout(60000)
  const pageErrorCounts = {}
  page.on('pageerror', error => {
    const name = error.name || 'Error'
    pageErrorCounts[name] = (pageErrorCounts[name] || 0) + 1
  })
  // The public session bootstrap consumes and removes this real fragment.
  stage = 'real_session_bootstrap'
  await page.goto(`${origin}/book/${encodeURIComponent(novel)}/chapter/1#plotpilot-session=${encodeURIComponent(token)}`)
  const editor = page.locator('.content-editor textarea')
  const titleInput = page.getByRole('textbox', { name: '章节标题', exact: true })
  await expect(editor).toBeEditable()
  const bootUrl = await page.evaluate(() => ({ pathname: location.pathname, hashLength: location.hash.length, hashPrefix: location.hash.slice(0, 22) }))
  await fs.writeFile(path.join(output, 'boot-url.json'), JSON.stringify(bootUrl, null, 2), 'utf8')
  process.stdout.write('UI_BOOT_URL ' + JSON.stringify(bootUrl) + '\n')
  requireCheck(!new URL(page.url()).hash.includes('plotpilot-session'), 'Session fragment was not removed before editing')

  // Real GETs provide independent baseline/read-back evidence. Chapter writes
  // occur through UI controls; prop registration was separately declared.
  const api = async route => {
    const response = await context.request.get(origin + route, { headers: { Authorization: 'Bearer ' + token }, timeout: 30000 })
    requireCheck(response.ok(), `Local read-back GET failed with HTTP ${response.status()}`)
    return response.json()
  }
  const chapterPath = number => `/api/v1/novels/${encodeURIComponent(novel)}/chapters/${number}`
  const chapter = number => api(chapterPath(number))
  const jobs = number => api(chapterPath(number) + '/derivation-jobs')
  const latest = (rows, revision) => rows.find(row => row.revision === revision)
  const evidenceJob = row => Object.fromEntries(['job_id', 'revision', 'attempts', 'status', 'cascade_status'].filter(key => row[key] !== undefined).map(key => [key, row[key]]))
  const pollJob = async (number, revision, minimumAttempts, cascade) => {
    const deadline = Date.now() + timeout
    let nextProgress = 0
    while (Date.now() < deadline) {
      const row = latest(await jobs(number), revision)
      if (row && row.attempts >= minimumAttempts) {
        requireCheck(!['unavailable', 'failed', 'superseded'].includes(row.status), `Chapter ${number} derivation did not complete`)
        requireCheck(!cascade || row.cascade_status !== 'unavailable', `Chapter ${number} downstream cascade did not complete`)
        if (row.status === 'completed' && (!cascade || row.cascade_status === 'completed')) return row
      }
      if (Date.now() >= nextProgress) {
        process.stdout.write('UI_WAIT chapter_' + number + '_derivation\n')
        nextProgress = Date.now() + 30000
      }
      await new Promise(resolve => setTimeout(resolve, 1000))
    }
    throw new SafeCheckFailed(`Chapter ${number} derivation timed out`)
  }
  const screenshot = async name => {
    requireCheck(!new URL(page.url()).hash, 'Refuse screenshot while a launcher fragment is present')
    const file = 'ui-' + name + '.png'
    await page.screenshot({ path: path.join(output, file), fullPage: true })
    result.screenshots.push(file)
  }
  const first = await chapter(1)
  const second = await chapter(2)
  const secondBeforeJob = latest(await jobs(2), second.revision)
  requireCheck(first.content && second.content && secondBeforeJob?.status === 'completed', 'Two live saved chapters and derived baseline are required')
  await expect(editor).toHaveValue(first.content)
  await expect(titleInput).toHaveValue(first.title)
  await screenshot('before')
  mark('real_session_bootstrap', { fragment_consumed: true, no_api_mocks: true })

  stage = 'ui_edit_and_save'
  const edited = first.content.replaceAll('七十四', '一百零七').replaceAll('九十二', '一百零七').replace(/(?<!\d)(?:74|92)(?!\d)/g, '107')
    + '\n\n沈舟打开工具箱，将编号一百零七的验潮钥匙交给林雁。林雁把钥匙锁进自己的随身匣子。钥匙现在由林雁独自保管，沈舟不再保管。两人同时在交接簿中更正了编号和持有人。'
  await titleInput.fill(title)
  await editor.fill(edited)
  await expect(page.locator('.chapter-header')).toContainText('未保存')
  await screenshot('unsaved')
  const saveResponse = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === chapterPath(1) && response.request().method() === 'PUT'
  }, { timeout: 60000 })
  await page.getByRole('button', { name: '保存', exact: true }).click()
  const response = await saveResponse
  requireCheck(response.ok(), `UI save returned HTTP ${response.status()}`)
  const saved = await response.json()
  requireCheck(saved.title === title && saved.content === edited, 'UI save returned a different title or body')
  requireCheck(saved.revision === first.revision + 1 && saved.content_hash === sha(edited), 'UI save revision or hash differs from the submitted edit')
  requireCheck(Boolean(saved.save_request_id), 'UI save has no request receipt identifier')
  const receipt = await api(chapterPath(1) + '/save-requests/' + encodeURIComponent(saved.save_request_id))
  requireCheck(receipt.status === 'committed' && receipt.revision === saved.revision && receipt.content_hash === saved.content_hash,
    'UI save is missing its matching durable committed receipt')
  await expect(page.locator('.chapter-header')).toContainText('已保存')
  await screenshot('saved')
  mark('ui_edit_and_save', { title_saved: true, body_saved: true, before_revision: first.revision, revision: saved.revision,
    content_hash: saved.content_hash, durable_receipt_committed: true })
  await persist()

  stage = 'ui_reload_readback'
  await page.reload()
  await expect(titleInput).toHaveValue(title)
  await expect(editor).toHaveValue(edited)
  const reloaded = await chapter(1)
  requireCheck(reloaded.title === title && reloaded.content_hash === sha(edited) && reloaded.revision === saved.revision,
    'Saved edit did not survive UI reload and independent HTTP read-back')
  await screenshot('reloaded')
  const versions = await api(chapterPath(1) + '/versions')
  requireCheck(versions.some(row => row.revision === first.revision && row.title === first.title)
    && versions.some(row => row.revision === saved.revision && row.title === title), 'Old and edited titles were not both retained in version history')
  mark('ui_reload_readback', { title_and_body_match: true, old_and_new_titles_in_history: true })

  stage = 'automatic_derivation_and_cascade'
  await page.getByRole('button', { name: '历史版本', exact: true }).click()
  const modal = page.locator('.n-modal').filter({ hasText: '章节历史版本' })
  await expect(modal).toBeVisible()
  const automatic = await pollJob(1, saved.revision, 1, true)
  const following = await pollJob(2, second.revision, secondBeforeJob.attempts + 1, false)
  const secondAfter = await chapter(2)
  requireCheck(secondAfter.content_hash === second.content_hash && secondAfter.revision === second.revision,
    'Automatic cascade rewrote the existing later chapter prose')
  await modal.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(modal).toContainText('已完成')
  await screenshot('automatic-cascade-complete')
  mark('automatic_derivation_and_cascade', { chapter_1: evidenceJob(automatic), chapter_2: evidenceJob(following), later_prose_preserved: true })
  await persist()

  stage = 'ui_manual_rerun'
  const rerun = modal.getByRole('button', { name: '重跑章后关联分析', exact: true })
  await expect(rerun).toBeEnabled()
  const rerunResponse = page.waitForResponse(response => new URL(response.url()).pathname === chapterPath(1) + '/derivation-jobs'
    && response.request().method() === 'POST', { timeout: 60000 })
  await rerun.click()
  const rerunResult = await rerunResponse
  requireCheck(rerunResult.ok(), `UI rerun returned HTTP ${rerunResult.status()}`)
  const repeated = await pollJob(1, saved.revision, automatic.attempts + 1, true)
  const followingRepeated = await pollJob(2, second.revision, following.attempts + 1, false)
  requireCheck((await chapter(2)).content_hash === second.content_hash, 'Manual rerun rewrote the later chapter prose')
  await modal.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(modal).toContainText('已完成')
  await screenshot('manual-rerun-complete')
  mark('ui_manual_rerun', { chapter_1: evidenceJob(repeated), chapter_2: evidenceJob(followingRepeated), later_prose_preserved: true })
  result.evidence.browser_page_error_counts = pageErrorCounts
  requireCheck(Object.keys(pageErrorCounts).length === 0, 'Browser raised an uncaught page exception')
  result.status = 'passed_real_browser'
} catch (error) {
  result.status = 'failed'
  result.stage = stage
  result.error_type = error?.name || 'Error'
  result.message = error instanceof SafeCheckFailed ? error.message : 'A browser check failed; inspect the saved safe stage and screenshots'
  result.diagnostic = {
    error_message: typeof error?.message === 'string' ? error.message.slice(0, 300) : '',
    page_url: pageForDiagnostics ? pageForDiagnostics.url().replace(/#.*$/, '') : '',
  }
  process.exitCode = 1
} finally {
  if (output) await persist()
  if (browser) await browser.close()
  process.stdout.write('UI_RESULT ' + result.status + '\n')
}
