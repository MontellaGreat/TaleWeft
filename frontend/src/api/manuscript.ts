import { apiClient } from './config'
import type { PropDTO } from './propApi'

export interface LexiconCharacter { id: string; name: string; aliases: string[] }
export interface LexiconLocation { id: string; name: string; location_type: string; aliases: string[] }
export interface ChapterEntityMention { entity_kind: string; entity_id: string; display_label: string; mention_count: number; updated_at: string }

export interface ManuscriptReceipt {
  status: 'imported'
  preview_id: string
  novel_id: string
  title: string
  chapter_count: number
  content_character_count: number
  payload_sha256: string
  original_novels_changed: false
}
export interface ManuscriptPreview {
  preview_id: string
  new_novel_id: string
  status: string
  expired: boolean
  payload_sha256: string
  novel: { title: string; author: string; premise: string }
  validation: {
    status: string; chapter_count: number; content_character_count: number; empty_chapter_count: number
    input_sha256: string; warnings: string[]; errors: string[]
    chapters: { number: number; title: string; characters: number; content_sha256: string }[]
  }
  receipt: ManuscriptReceipt | null
}
const base = '/export/manuscript'
export const manuscriptApi = {
  getEntityLexicon: (novelId: string) => apiClient.get<{ characters: LexiconCharacter[]; locations: LexiconLocation[]; props: PropDTO[] }>(`/novels/${novelId}/manuscript/entity-lexicon`) as Promise<{ characters: LexiconCharacter[]; locations: LexiconLocation[]; props: PropDTO[] }>,
  listChapterMentions: (novelId: string, chapterNumber: number) => apiClient.get<{ mentions: ChapterEntityMention[] }>(`/novels/${novelId}/chapters/${chapterNumber}/entity-mentions`) as Promise<{ mentions: ChapterEntityMention[] }>,
  reindexChapterMentions: (novelId: string, chapterNumber: number, content?: string | null) => apiClient.post<{ ok: boolean; mentions: ChapterEntityMention[] }>(`/novels/${novelId}/chapters/${chapterNumber}/entity-mentions/reindex`, {}, content != null && content !== '' ? { params: { content } as Record<string, string> } : undefined) as Promise<{ ok: boolean; mentions: ChapterEntityMention[] }>,
  preview: (raw: string) => apiClient.post<ManuscriptPreview>(`${base}/preview`, { document_json: raw }) as Promise<ManuscriptPreview>,
  get: (id: string) => apiClient.get<ManuscriptPreview>(`${base}/previews/${encodeURIComponent(id)}`) as Promise<ManuscriptPreview>,
  confirm: (preview: ManuscriptPreview) => apiClient.post<ManuscriptReceipt>(`${base}/previews/${encodeURIComponent(preview.preview_id)}/confirm`, { expected_payload_sha256: preview.payload_sha256 }) as Promise<ManuscriptReceipt>,
}
