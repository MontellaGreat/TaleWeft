import type { DialogApi } from 'naive-ui'
import { dataProtectionApi, type DataChangeProposal, type DataChangeReceipt } from '../api/dataProtection'

export function confirmDataChange(dialog: DialogApi, novelId: string, proposal: DataChangeProposal): Promise<DataChangeReceipt | null> {
  const chapters = proposal.proposal.affected_chapters
  const nodes = proposal.proposal.node_ids?.length ?? proposal.proposal.deletes?.length ?? 0
  const details = chapters.map(chapter => `第 ${chapter.number} 章 ${chapter.title || ''}（${chapter.has_content ? '有正文' : '空稿'}，版本 ${chapter.revision}）`).join('\n')
  return new Promise((resolve, reject) => {
    dialog.warning({
      title: '确认结构变更影响',
      content: `将移除 ${nodes} 个结构节点，影响 ${chapters.length} 个章节，其中 ${chapters.filter(chapter => chapter.has_content).length} 个有正文。${proposal.operation === 'macro_merge' ? `另新增 ${proposal.proposal.creates?.length ?? 0}、更新 ${proposal.proposal.updates?.length ?? 0} 个节点。` : ''}\n${details}\n保护快照已保存，可在编年史的变更记录恢复。提交前会再次核验作品版本。`,
      positiveText: '确认并保留恢复记录', negativeText: '取消',
      onNegativeClick: () => resolve(null), onClose: () => resolve(null), onMaskClick: () => resolve(null),
      onPositiveClick: async () => {
        try { const receipt = await dataProtectionApi.confirm(novelId, proposal); resolve(receipt) }
        catch (exc) { reject(exc); return false }
      },
    })
  })
}
