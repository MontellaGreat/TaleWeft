import { describe, expect, it, vi } from 'vitest'
import { autopilotApi, type ContinuationProposal } from '../src/api/autopilot'
import { fetchJson } from '../src/api/http'
vi.mock('../src/api/http', () => ({ fetchJson: vi.fn(), fetchOk: vi.fn(), fetchUrl: vi.fn(), HttpError: class extends Error {} }))
describe('continuation structure API', () => {
  it('binds explicit confirmation to the displayed hashes and run epoch', async () => {
    const proposal = { proposal_id: 'proposal/a', structure_review: { base_hash: 'base', proposal_hash: 'hash', run_epoch: 8 } } as ContinuationProposal
    await autopilotApi.confirmContinuationStructure('novel/a', proposal)
    expect(fetchJson).toHaveBeenCalledWith('/api/v1/autopilot/novel%2Fa/continuation-proposals/proposal%2Fa/structure/confirm', {
      method: 'POST', body: { expected_hash: 'base', proposal_hash: 'hash', run_epoch: 8 },
    })
  })
  it('never sends a confirmation without structure review credentials', async () => {
    vi.mocked(fetchJson).mockClear()
    await expect(autopilotApi.confirmContinuationStructure('novel', { proposal_id: 'proposal' } as ContinuationProposal)).rejects.toThrow('凭据缺失')
    expect(fetchJson).not.toHaveBeenCalled()
  })
})
