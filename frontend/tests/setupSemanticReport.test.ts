import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import SetupDAGAuditReport from '@/components/onboarding/SetupDAGAuditReport.vue'

const base = { passed: true, scan_status: 'completed', reports: [{ path: '$.style', scan_status: 'completed', total_hits: 0, hits: [], effective_rule_count: 51, rule_version: 'rules-v1' }] }
function largeReport(count = 342) {
  return { ...base, scan_id: 'large-scan', reports: Array.from({ length: count }, (_, index) => ({
    ...base.reports[0], path: `$.characters[${index}].description`, content_hash: `field-${index}`,
  })) }
}
describe('readable Anti-AI evidence', () => {
  it.each(['semantic-style-v1', 'semantic-style-v2'])('recognizes complete %s reports and preserves the model verdict', (version) => {
    const semantic = { status: 'completed', review_version: version, attempt_id: 'durable-model-attempt', passed: true,
      checks: { vague_lyricism: true, expository_voice: true, mechanical_structure: true, emotion_labeling: true, template_closure: true },
      coverage: [{ text_id: 't0001', path: '$.style', input_hash: 'body-hash' }], issues: [] }
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: { ...base, semantic_review: semantic } } })
    expect(view.text()).toContain('报告结果：通过')
    expect(view.text()).toContain('模型语义审稿：未发现问题')
  })
  it('does not label an incomplete semantic receipt as passed', () => {
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: { ...base,
      semantic_review: { status: 'completed', passed: true, issues: [] } } } })
    expect(view.text()).not.toContain('报告结果：通过')
    expect(view.text()).toContain('未完成')
  })
  it('does not describe a historical rule-only pass as complete semantic approval', () => {
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: base } })
    expect(view.text()).toContain('缺少模型语义审稿')
    expect(view.text()).not.toContain('报告结果：通过')
  })
  it('leads with semantic findings and separates rule coverage from technical hashes', () => {
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: { ...base, passed: false,
      semantic_review: { status: 'completed', passed: false, review_version: 'semantic-style-v1', attempt_id: 'model-attempt', checks: {
        vague_lyricism: true, expository_voice: true, mechanical_structure: true, emotion_labeling: true, template_closure: true,
      }, coverage: [{ path: '$.style', input_hash: 'body-hash' }], issues: [{ path: '$.style', category: 'vague_lyricism',
        evidence: '时代的浪潮推动着命运。', reason: '空泛总结，缺少实际信息。', suggestion: '写明具体约束与代价。' }] } } } })
    expect(view.text()).toContain('模型语义审稿')
    expect(view.text()).toContain('时代的浪潮推动着命运。')
    expect(view.text()).toContain('空泛总结，缺少实际信息。')
    expect(view.text()).toContain('写明具体约束与代价。')
    expect(view.text()).toContain('51')
    expect(view.text()).toContain('本地规则扫描')
  })

  it('summarizes 342 clean fields without mounting their cards and keeps the last field reachable', async () => {
    const report = largeReport()
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report } })
    expect(view.text()).toContain('342 个字段')
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(0)
    expect(view.findAll('pre')).toHaveLength(0)
    await view.get('button[aria-label="查看全部扫描字段"]').trigger('click')
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(8)
    await view.get('button[aria-label="扫描字段末页"]').trigger('click')
    expect(view.text()).toContain('$.characters[341].description')
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(6)
    // Polling the same scan must not throw the reader back to the first page.
    await view.setProps({ report: JSON.parse(JSON.stringify(report)) })
    expect(view.text()).toContain('$.characters[341].description')
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(6)
    const raw = view.findAll('summary').find(item => item.text() === '完整审核报告（原始证据）')!
    raw.element.parentElement!.setAttribute('open', '')
    await view.find('details.setup-audit-report__raw').trigger('toggle')
    expect(view.find('pre').text()).toContain('field-341')
  })

  it('brings late rule hits and incomplete scans into the default problem list', () => {
    const report = largeReport()
    Object.assign(report.reports[341]!, { total_hits: 1, hits: [{ text: '模板收尾证据', suggestion: '保留具体动作' }] })
    Object.assign(report.reports[340]!, { scan_status: 'failed', error: '规则扫描中断' })
    Object.assign(report.reports[339]!, { error: '扫描回执校验失败' })
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report } })
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(3)
    expect(view.text()).toContain('模板收尾证据')
    expect(view.text()).toContain('规则扫描中断')
    expect(view.text()).toContain('扫描回执校验失败')
    expect(view.text()).not.toContain('报告结果：通过')
  })

  it('paginates semantic findings without dropping their evidence or advice', async () => {
    const issues = Array.from({ length: 23 }, (_, i) => ({ path: `$.text[${i}]`, category: 'vague_lyricism', evidence: `原文证据-${i}`, reason: `具体理由-${i}`, suggestion: `修订建议-${i}` }))
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: { ...largeReport(), passed: false,
      semantic_review: { status: 'completed', passed: false, issues } } } })
    expect(view.findAll('.setup-audit-report__semantic blockquote')).toHaveLength(5)
    await view.get('button[aria-label="语义问题末页"]').trigger('click')
    expect(view.text()).toContain('原文证据-22')
    expect(view.text()).toContain('具体理由-22')
    expect(view.text()).toContain('修订建议-22')
    expect(view.findAll('.setup-audit-report__semantic blockquote')).toHaveLength(3)
  })

  it('resets large-list navigation when a different scan arrives', async () => {
    const view = mount(SetupDAGAuditReport, { props: { kind: 'anti_ai', report: largeReport() } })
    await view.get('button[aria-label="查看全部扫描字段"]').trigger('click')
    await view.get('button[aria-label="扫描字段末页"]').trigger('click')
    await view.setProps({ report: { ...largeReport(2), scan_id: 'replacement-scan' } })
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(0)
    await view.get('button[aria-label="查看全部扫描字段"]').trigger('click')
    expect(view.findAll('.setup-audit-report__leaf')).toHaveLength(2)
    expect(view.text()).toContain('$.characters[0].description')
  })
})
