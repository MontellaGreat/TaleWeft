import { apiClient, subscribeChapterStream as subscribeChapterStreamRequest } from './config'
import { apiRoutes } from './endpoints'
import type { GuardrailCheckResponse } from './engineCore'

export interface ChapterDTO {
  id: string
  novel_id: string
  number: number
  title: string
  content: string
  status: string
  word_count: number
  generation_hint?: string
  created_at: string
  updated_at: string
  /** Monotonic optimistic-concurrency revision assigned by SQLite. */
  revision: number
  content_hash: string
  etag: string
  derived_status?: 'not_requested' | 'deferred' | 'unavailable' | 'completed'
  derivation_job_id?: string | null
  save_request_id?: string | null
}

export interface ChapterMicroBeatPayload {
  description: string
  target_words?: number
  focus?: string
  location_id?: string
  function?: string
  pov?: string
  cast_refs?: string[]
  location_refs?: string[]
  prop_refs?: string[]
  knowledge_refs?: string[]
  visible_action?: string
  conflict?: string
  delta?: string
  handoff_to_next?: string
  must_include?: string[]
  must_not_include?: string[]
  active_action?: string
  emotion_gap?: string
  forbidden_drift?: string
}

export interface UpdateChapterRequest {
  content: string
  title?: string
  save_request_id?: string
  /** Server revision observed when the editor loaded the chapter. */
  expected_revision?: number
  /** Optional body hash guard for legacy writers and ABA protection. */
  expected_hash?: string
  /** 指挥器微观节拍；保存时写入 chapter_summaries.micro_beats */
  micro_beats?: ChapterMicroBeatPayload[]
}

export interface ChapterVersion {
  revision: number
  content_hash: string
  etag: string
}

export interface ChapterContentVersion {
  version_id: string
  chapter_id: string
  novel_id: string
  chapter_number: number
  revision: number
  content: string
  outline: string
  title: string
  status: string
  content_hash: string
  source: string
  created_at: string
}

export interface ChapterDraft {
  id: string
  novel_id: string
  chapter_id: string
  chapter_number: number
  content: string
  outline: string
  source: string
  word_count: number
  created_at: string
}

export interface ChapterDerivationJob {
  job_id: string
  revision: number
  status: 'pending' | 'running' | 'completed' | 'unavailable' | 'superseded'
  attempts: number
  last_error: string
  cascade_status?: 'not_requested' | 'running' | 'completed' | 'unavailable'
  cascade_error?: string
  created_at: string
  updated_at: string
}

export interface ChapterRevisionConflict {
  code: 'chapter_revision_conflict'
  message: string
  expected_revision?: number
  current: ChapterDTO & { content: string }
  diff?: string
}

/** Extract the structured 409 payload without coupling callers to Axios. */
export function getChapterRevisionConflict(error: unknown): ChapterRevisionConflict | null {
  if (!error || typeof error !== 'object') return null
  const record = error as {
    response?: { status?: number; data?: { detail?: unknown } }
    status?: number
  }
  if (record.response?.status !== 409 && record.status !== 409) return null
  const detail = record.response?.data?.detail
  if (!detail || typeof detail !== 'object') return null
  const candidate = detail as Partial<ChapterRevisionConflict>
  if (candidate.code !== 'chapter_revision_conflict' || !candidate.current) return null
  return candidate as ChapterRevisionConflict
}

export interface ChapterReviewDTO {
  status: string
  memo: string
  created_at: string
  updated_at: string
}

export interface ChapterStructureDTO {
  word_count: number
  paragraph_count: number
  dialogue_ratio: number
  scene_count: number
  pacing: string
}

export interface ChapterReviewAiResponse {
  ok: boolean
  status: string
  memo: string
  saved: boolean
}

export interface ChapterListResponse {
  chapters?: ChapterDTO[]
}

export const chapterApi = {
  /**
   * List all chapters for a novel
   * GET /api/v1/novels/{novelId}/chapters
   */
  listChapters: (novelId: string) =>
    apiClient.get<ChapterDTO[]>(`/novels/${novelId}/chapters`) as Promise<ChapterDTO[]>,

  /**
   * Get the latest draft chapter for live preview fallback
   * GET /api/v1/novels/{novelId}/chapters?status=draft&limit=1
   */
  getLatestDraftChapter: async (novelId: string): Promise<ChapterDTO | null> => {
    const data = await apiClient.get<ChapterListResponse>(
      apiRoutes.novels.chaptersClient(novelId),
      { params: { status: 'draft', limit: 1 } },
    ) as ChapterListResponse
    return data.chapters?.[0] ?? null
  },

  /**
   * Get a specific chapter by number
   * GET /api/v1/novels/{novelId}/chapters/{chapterNumber}
   */
  getChapter: (novelId: string, chapterNumber: number) =>
    apiClient.get<ChapterDTO>(`/novels/${novelId}/chapters/${chapterNumber}`) as Promise<ChapterDTO>,

  /**
   * Update a chapter
   * PUT /api/v1/novels/{novelId}/chapters/{chapterNumber}
   */
  updateChapter: async (novelId: string, chapterNumber: number, data: UpdateChapterRequest, version?: ChapterVersion) => {
    // Snapshot the caller's request and version before hashing or awaiting I/O.
    // Later editor input cannot change the content this receipt must prove.
    const submission = JSON.parse(JSON.stringify(data)) as UpdateChapterRequest
    const expected = version ? { ...version } : (
      submission.expected_revision == null
        ? undefined
        : {
            revision: submission.expected_revision,
            content_hash: submission.expected_hash ?? '',
            etag: '',
          }
    )
    const headers = expected?.etag ? { 'If-Match': expected.etag } : undefined
    const requestId = submission.save_request_id || crypto.randomUUID().replaceAll('-', '')
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(submission.content))
    const submittedHash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
    const matchesSubmission = (chapter?: ChapterDTO) => Boolean(
      chapter && typeof chapter.id === 'string' && chapter.id && expected &&
      chapter.save_request_id === requestId && chapter.novel_id === novelId && chapter.number === chapterNumber &&
      chapter.content === submission.content && (submission.title == null || chapter.title === submission.title.trim()) &&
      chapter.revision === expected.revision + 1 && chapter.content_hash === submittedHash,
    )
    try {
      const result = await apiClient.put<ChapterDTO>(
        `/novels/${encodeURIComponent(novelId)}/chapters/${chapterNumber}`,
        {
          ...submission,
          save_request_id: requestId,
          ...(expected ? {
            expected_revision: expected.revision,
            ...(expected.content_hash ? { expected_hash: expected.content_hash } : {}),
          } : {}),
        },
        headers ? { headers } : undefined,
      )
      if (!matchesSubmission(result)) {
        throw new Error(`保存请求 ${requestId} 尚未得到提交回执，本地正文已保留`)
      }
      return result
    } catch (error) {
      if (getChapterRevisionConflict(error)) throw error
      // Reconcile exactly this request after a lost response, never merely
      // accept whichever body happens to be the latest chapter revision.
      let receiptStatus = 'unconfirmed'
      try {
        const receipt = await apiClient.get<{ request_id: string; novel_id: string; chapter_number: number; chapter_id?: string; revision?: number; status: string; content_hash?: string; chapter?: ChapterDTO }>(
          `/novels/${encodeURIComponent(novelId)}/chapters/${chapterNumber}/save-requests/${encodeURIComponent(requestId)}`,
        )
        receiptStatus = receipt.status
        if (receipt.request_id === requestId && receipt.status === 'committed'
            && receipt.novel_id === novelId && receipt.chapter_number === chapterNumber
            && receipt.chapter_id === receipt.chapter?.id && receipt.revision === receipt.chapter?.revision
            && receipt.content_hash === submittedHash && matchesSubmission(receipt.chapter)) return receipt.chapter!
      } catch { /* Unknown and failed requests preserve the original save error. */ }
      if ((error as { response?: { status?: number } })?.response?.status === 503) {
        throw new Error(`${receiptStatus === 'failed' ? '本次保存失败' : '保存尚未确认'}，本地正文已保留；保存请求 ${requestId}`, { cause: error })
      }
      throw error
    }
  },

  listVersions: (novelId: string, chapterNumber: number, beforeRevision?: number) =>
    apiClient.get<ChapterContentVersion[]>(`/novels/${novelId}/chapters/${chapterNumber}/versions`, {
      params: { limit: 50, ...(beforeRevision == null ? {} : { before_revision: beforeRevision }) },
    }) as Promise<ChapterContentVersion[]>,

  diffVersion: (novelId: string, chapterNumber: number, revision: number, againstRevision?: number) =>
    apiClient.get<{ revision: number; against_revision: number; diff: string }>(
      `/novels/${novelId}/chapters/${chapterNumber}/versions/${revision}/diff`,
      { params: againstRevision == null ? {} : { against_revision: againstRevision } },
    ) as Promise<{ revision: number; against_revision: number; diff: string }>,

  restoreVersion: (novelId: string, chapterNumber: number, revision: number, version: ChapterVersion) =>
    apiClient.post<ChapterDTO>(`/novels/${novelId}/chapters/${chapterNumber}/versions/${revision}/restore`, {
      expected_revision: version.revision,
      expected_hash: version.content_hash,
    }, version.etag ? { headers: { 'If-Match': version.etag } } : undefined) as Promise<ChapterDTO>,

  listDrafts: (novelId: string, chapterNumber: number) =>
    apiClient.get<ChapterDraft[]>(`/novels/${novelId}/chapters/${chapterNumber}/drafts`) as Promise<ChapterDraft[]>,

  diffDraft: (novelId: string, chapterNumber: number, draftId: string, againstRevision?: number, againstDraftId?: string) =>
    apiClient.get<{ draft_id: string; against_revision?: number; against_draft_id?: string; diff: string }>(
      `/novels/${novelId}/chapters/${chapterNumber}/drafts/${draftId}/diff`,
      { params: {
        ...(againstRevision == null ? {} : { against_revision: againstRevision }),
        ...(againstDraftId == null ? {} : { against_draft_id: againstDraftId }),
      } },
    ) as Promise<{ draft_id: string; against_revision?: number; against_draft_id?: string; diff: string }>,

  restoreDraft: (novelId: string, chapterNumber: number, draftId: string, version: ChapterVersion) =>
    apiClient.post<ChapterDTO>(`/novels/${novelId}/chapters/${chapterNumber}/drafts/${draftId}/restore`, {
      expected_revision: version.revision,
      expected_hash: version.content_hash,
    }, version.etag ? { headers: { 'If-Match': version.etag } } : undefined) as Promise<ChapterDTO>,

  listDerivationJobs: (novelId: string, chapterNumber: number) =>
    apiClient.get<ChapterDerivationJob[]>(`/novels/${novelId}/chapters/${chapterNumber}/derivation-jobs`) as Promise<ChapterDerivationJob[]>,

  requestDerivation: (novelId: string, chapterNumber: number, action: 'retry_aftermath' | 'rebuild_indexes') =>
    apiClient.post<ChapterDerivationJob>(`/novels/${novelId}/chapters/${chapterNumber}/derivation-jobs`, { action }) as Promise<ChapterDerivationJob>,

  /** 仅落库指挥器微观节拍（流式生成完成后可先于正文保存调用） */
  upsertChapterMicroBeats: (
    novelId: string,
    chapterNumber: number,
    micro_beats: ChapterMicroBeatPayload[],
  ) =>
    apiClient.put<{ ok: boolean; chapter_number: number; count: number }>(
      `/novels/${novelId}/chapters/${chapterNumber}/micro-beats`,
      { micro_beats },
    ) as Promise<{ ok: boolean; chapter_number: number; count: number }>,

  /**
   * 更新章节生成约束（用户手写指令，直注 AI 上下文）
   * PATCH /api/v1/novels/{novelId}/chapters/{chapterNumber}/hint
   */
  updateGenerationHint: (novelId: string, chapterNumber: number, generationHint: string) =>
    apiClient.patch<ChapterDTO>(
      `/novels/${novelId}/chapters/${chapterNumber}/hint`,
      { generation_hint: generationHint },
    ) as Promise<ChapterDTO>,

  /**
   * Get chapter review
   * GET /api/v1/novels/{novelId}/chapters/{chapterNumber}/review
   */
  getChapterReview: (novelId: string, chapterNumber: number) =>
    apiClient.get<ChapterReviewDTO>(`/novels/${novelId}/chapters/${chapterNumber}/review`) as Promise<ChapterReviewDTO>,

  /**
   * Save chapter review
   * PUT /api/v1/novels/{novelId}/chapters/{chapterNumber}/review
   */
  saveChapterReview: (novelId: string, chapterNumber: number, status: string, memo: string) =>
    apiClient.put<ChapterReviewDTO>(`/novels/${novelId}/chapters/${chapterNumber}/review`, { status, memo }) as Promise<ChapterReviewDTO>,

  /**
   * AI review chapter
   * POST /api/v1/novels/{novelId}/chapters/{chapterNumber}/review-ai
   */
  reviewChapterAi: (novelId: string, chapterNumber: number, save: boolean) =>
    apiClient.post<ChapterReviewAiResponse>(`/novels/${novelId}/chapters/${chapterNumber}/review-ai`, { save }) as Promise<ChapterReviewAiResponse>,

  /**
   * Get chapter structure analysis
   * GET /api/v1/novels/{novelId}/chapters/{chapterNumber}/structure
   */
  getChapterStructure: (novelId: string, chapterNumber: number) =>
    apiClient.get<ChapterStructureDTO>(`/novels/${novelId}/chapters/${chapterNumber}/structure`) as Promise<ChapterStructureDTO>,

  /**
   * 保存后自动护栏快照（建议模式）。尚无快照时服务端返回 JSON null（HTTP 200）。
   * GET /novels/{novelId}/chapters/{chapterNumber}/guardrail-snapshot
   */
  getGuardrailSnapshot: async (
    novelId: string,
    chapterNumber: number
  ): Promise<GuardrailCheckResponse | null> => {
    const data = (await apiClient.get(
      `/novels/${novelId}/chapters/${chapterNumber}/guardrail-snapshot`
    )) as GuardrailCheckResponse | null
    return data ?? null
  },

  /**
   * 确保章节在正文库中存在；若不存在则创建空白记录
   * POST /api/v1/novels/{novelId}/chapters/{chapterNumber}/ensure
   */
  ensureChapter: (novelId: string, chapterNumber: number, title = '') =>
    apiClient.post<ChapterDTO>(`/novels/${novelId}/chapters/${chapterNumber}/ensure`, { title }) as Promise<ChapterDTO>,

  subscribeStream: subscribeChapterStreamRequest,
}
