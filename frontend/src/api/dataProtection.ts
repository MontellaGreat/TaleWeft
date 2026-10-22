import { apiClient } from './config'

export interface DataChangeProposal {
  proposal_id: string
  novel_id: string
  operation: string
  base_hash: string
  proposal_hash: string
  after_hash?: string
  snapshot_id: string
  run_epoch: number
  current_run_epoch: number | null
  status: string
  expired: boolean
  created_at: string
  proposal: {
    chapter_ids?: string[]
    node_ids?: string[]
    creates?: unknown[]
    updates?: unknown[]
    deletes?: string[]
    affected_chapters: { id: string; number: number; title?: string; has_content: boolean; revision: number }[]
  }
  audits: { audit_id: string; event: string; created_at: string }[]
}

export interface DataChangeReceipt {
  status: string
  proposal_id: string
  snapshot_id: string
  audit_id: string
  state_hash: string
}

const book = (novelId: string) => `/novels/${encodeURIComponent(novelId)}`

export const dataProtectionApi = {
  list: (novelId: string) => apiClient.get<DataChangeProposal[]>(`${book(novelId)}/data-changes`) as Promise<DataChangeProposal[]>,
  deletePreview: (novelId: string, nodeId: string) => apiClient.post<DataChangeProposal>(`${book(novelId)}/structure/nodes/${encodeURIComponent(nodeId)}/deletion-proposal`, {}) as Promise<DataChangeProposal>,
  snapshotPreview: (novelId: string, snapshotId: string) => apiClient.post<DataChangeProposal>(`${book(novelId)}/snapshots/${encodeURIComponent(snapshotId)}/rollback-proposal`, {}) as Promise<DataChangeProposal>,
  macroPreview: (novelId: string, structure: Record<string, unknown>[]) => apiClient.post<DataChangeProposal>(`/planning/novels/${encodeURIComponent(novelId)}/macro/proposal`, { structure }) as Promise<DataChangeProposal>,
  confirm: (novelId: string, proposal: DataChangeProposal) => apiClient.post<DataChangeReceipt>(`${book(novelId)}/data-changes/${proposal.proposal_id}/confirm`, {
    expected_hash: proposal.base_hash, proposal_hash: proposal.proposal_hash, run_epoch: proposal.run_epoch,
  }) as Promise<DataChangeReceipt>,
  rollback: (novelId: string, proposal: DataChangeProposal) => apiClient.post<DataChangeReceipt>(`${book(novelId)}/data-changes/${proposal.proposal_id}/rollback`, {
    expected_hash: proposal.after_hash, proposal_hash: proposal.proposal_hash, run_epoch: proposal.current_run_epoch,
  }) as Promise<DataChangeReceipt>,
}
