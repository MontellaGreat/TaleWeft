import type { ChapterDTO, ChapterVersion, UpdateChapterRequest } from '../api/chapter'

export type ChapterSaveTransport = (
  slug: string,
  chapterNumber: number,
  data: UpdateChapterRequest,
  version: ChapterVersion,
) => Promise<ChapterDTO>

export function chapterVersionOf(chapter: ChapterVersion): ChapterVersion {
  return {
    revision: chapter.revision,
    content_hash: chapter.content_hash,
    etag: chapter.etag,
  }
}

/** One editor document owns one CAS token.  Requests remain attached to this
 * document when the view changes; an ACK never marks later keystrokes saved. */
export class ChapterDraftSession {
  readonly slug: string
  readonly chapterNumber: number
  content: string
  originalContent: string
  title: string | undefined
  originalTitle: string | undefined
  version: ChapterVersion
  private pending: Promise<ChapterDTO> | null = null

  constructor(
    slug: string,
    chapterNumber: number,
    content: string,
    version: ChapterVersion & { title?: string },
  ) {
    this.slug = slug
    this.chapterNumber = chapterNumber
    this.content = content
    this.originalContent = content
    this.title = version.title
    this.originalTitle = version.title
    this.version = chapterVersionOf(version)
  }

  get dirty() { return this.content !== this.originalContent || this.title !== this.originalTitle }
  get saving() { return this.pending !== null }

  async save(
    transport: ChapterSaveTransport,
    extra: Omit<UpdateChapterRequest, 'content'> = {},
  ): Promise<ChapterDTO | null> {
    // Multiple manual/autosave/leave requests share the in-flight commit and
    // then save any newer edits against its returned revision, in order.
    while (this.pending) await this.pending
    if (!this.dirty && !extra.micro_beats?.length) return null
    const submitted = this.content
    const submittedTitle = this.title
    const request = transport(this.slug, this.chapterNumber, {
      ...extra, content: submitted, ...(submittedTitle === undefined ? {} : { title: submittedTitle }),
    }, chapterVersionOf(this.version))
    this.pending = request
    try {
      const saved = await request
      this.version = chapterVersionOf(saved)
      this.originalContent = saved.content
      this.originalTitle = saved.title
      if (this.title === submittedTitle) this.title = saved.title
      if (this.content === submitted) this.content = saved.content
      return saved
    } finally {
      if (this.pending === request) this.pending = null
    }
  }
}

function draftKey(slug: string, chapterNumber: number, kind: 'editor' | 'generated') {
  return `plotpilot:chapter-${kind}-draft:${encodeURIComponent(slug)}:${chapterNumber}`
}

/** Retain failed/local edits if a component is destroyed outside router guards. */
export function rememberChapterDraft(session: ChapterDraftSession, kind: 'editor' | 'generated' = 'editor'): void {
  try {
    const key = draftKey(session.slug, session.chapterNumber, kind)
    if (!session.dirty) {
      sessionStorage.removeItem(key)
    } else {
      sessionStorage.setItem(key, JSON.stringify({
        content: session.content,
        originalContent: session.originalContent,
        title: session.title,
        originalTitle: session.originalTitle,
        version: session.version,
      }))
    }
  } catch { /* Editing and saving must still work if browser storage is full. */ }
}

export function restoreChapterDraft(session: ChapterDraftSession, kind: 'editor' | 'generated' = 'editor'): boolean {
  try {
    const key = draftKey(session.slug, session.chapterNumber, kind)
    const raw = sessionStorage.getItem(key)
    if (!raw) return false
    const draft = JSON.parse(raw)
    if (typeof draft.content !== 'string' || typeof draft.originalContent !== 'string'
      || !Number.isInteger(draft.version?.revision) || draft.version.revision < 0
      || typeof draft.version.content_hash !== 'string' || typeof draft.version.etag !== 'string') return false
    if (draft.content === session.content && (draft.title === undefined || draft.title === session.title)) {
      sessionStorage.removeItem(key)
      return false
    }
    session.content = draft.content
    session.originalContent = draft.originalContent
    if (typeof draft.title === 'string') session.title = draft.title
    if (typeof draft.originalTitle === 'string') session.originalTitle = draft.originalTitle
    session.version = chapterVersionOf(draft.version)
    return session.dirty
  } catch { return false }
}
