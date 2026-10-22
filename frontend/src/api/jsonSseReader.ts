/** Read standard SSE frames across arbitrary UTF-8/CRLF boundaries. */
export async function readJsonSse(
  response: Response,
  consume: (event: Record<string, unknown>, eventType: string) => boolean | Promise<boolean>,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) throw new Error('流式响应没有正文')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const frame = async (block: string): Promise<boolean> => {
    const lines = block.split(/\r\n|\n|\r/)
    const eventType = lines.filter(line => line.startsWith('event:')).at(-1)?.slice(6).trim() || 'message'
    const data = lines.filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n')
    if (!data) return false
    const event = JSON.parse(data)
    if (!event || typeof event !== 'object' || Array.isArray(event)) throw new Error('无效流式事件')
    return consume(event as Record<string, unknown>, eventType)
  }
  try {
    while (true) {
      if (signal?.aborted) return
      const { done, value } = await reader.read()
      if (value) buffer += decoder.decode(value, { stream: true })
      if (done) buffer += decoder.decode()
      let separator: RegExpExecArray | null
      while ((separator = /\r\n\r\n|\n\n|\r\r/.exec(buffer))) {
        const block = buffer.slice(0, separator.index)
        buffer = buffer.slice(separator.index + separator[0].length)
        if (await frame(block)) return
      }
      if (buffer.length > 1024 * 1024) throw new Error('流式事件超出允许大小')
      if (done) {
        if (buffer.trim()) await frame(buffer)
        return
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
