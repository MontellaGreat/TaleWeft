import type { InvocationVariableBinding } from '@/api/aiInvocation'

export function parseJsonLikeRecord(raw: string): Record<string, unknown> | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const candidates = [
    trimmed,
    extractJsonFromMarkdown(trimmed),
    extractOuterJson(trimmed),
  ].filter(Boolean) as string[]
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
    } catch {
      // Try the next candidate. LLM output often includes prose or code fences.
    }
  }

  // Invocation attempts are persisted while a streaming provider response is
  // still being generated.  During that window the outer object/array is often
  // incomplete even though one or more array items are complete JSON objects.
  // Exposing those complete items lets setup screens show real progress instead
  // of appearing frozen until the provider finishes the whole response.
  for (const arrayKey of ['characters', 'locations']) {
    const partial = recoverTruncatedArrayObject(trimmed, arrayKey)
    if (partial) return partial
  }
  return null
}

/** Recover complete objects from a still-open JSON array in a streaming attempt. */
export function recoverTruncatedArrayObject(raw: string, arrayKey: string): Record<string, unknown> | null {
  const keyIndex = raw.indexOf(`"${arrayKey}"`)
  if (keyIndex < 0) return null
  const openIndex = raw.indexOf('[', keyIndex)
  if (openIndex < 0) return null

  const items: unknown[] = []
  let i = openIndex + 1
  while (i < raw.length) {
    while (i < raw.length && /[\s,]/.test(raw[i])) i += 1
    if (i >= raw.length || raw[i] === ']') break
    if (raw[i] !== '{' && raw[i] !== '[') break

    const itemStart = i
    let depth = 0
    let inString = false
    let escapeNext = false
    let consumed = false
    while (i < raw.length) {
      const ch = raw[i]
      if (escapeNext) {
        escapeNext = false
      } else if (ch === '\\' && inString) {
        escapeNext = true
      } else if (ch === '"') {
        inString = !inString
      } else if (!inString) {
        if (ch === '{' || ch === '[') depth += 1
        else if (ch === '}' || ch === ']') {
          depth -= 1
          if (depth === 0) {
            try {
              items.push(JSON.parse(raw.slice(itemStart, i + 1)))
            } catch {
              return items.length ? { [arrayKey]: items } : null
            }
            i += 1
            consumed = true
            break
          }
        }
      }
      i += 1
    }
    if (!consumed) break
  }
  return items.length ? { [arrayKey]: items } : null
}

export function extractJsonFromMarkdown(raw: string): string {
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i)
  return fenced?.[1]?.trim() || ''
}

export function extractOuterJson(raw: string): string {
  const start = raw.indexOf('{')
  const end = raw.lastIndexOf('}')
  if (start < 0 || end <= start) return ''
  return raw.slice(start, end + 1).trim()
}

export function pickPath(source: unknown, path: string): unknown {
  if (source == null || !path) return undefined
  const normalized = path.trim()
  if (!normalized || normalized === '$') return source
  const input = normalized.startsWith('$.')
    ? normalized.slice(2)
    : normalized.startsWith('$')
      ? normalized.slice(1).replace(/^\./, '')
      : normalized

  let current: unknown = source
  for (const segment of input.split('.').filter(Boolean)) {
    current = pickPathSegment(current, segment)
    if (current == null) return undefined
  }
  return current
}

function pickPathSegment(source: unknown, segment: string): unknown {
  const raw = segment.trim()
  if (!raw || raw === '$') return source
  if (raw === '[]' || raw === '[*]' || raw === '*') return Array.isArray(source) ? source : undefined

  if (Array.isArray(source)) {
    if (raw.startsWith('[') && raw.endsWith(']')) {
      return pickListIndex(source, raw.slice(1, -1))
    }
    const values = source
      .map(item => pickPathSegment(item, raw))
      .filter(item => item !== undefined)
    return values
  }

  let key = raw
  const selectors: string[] = []
  const bracketIndex = raw.indexOf('[')
  if (bracketIndex >= 0) {
    key = raw.slice(0, bracketIndex)
    let rest = raw.slice(bracketIndex)
    while (rest.startsWith('[')) {
      const close = rest.indexOf(']')
      if (close < 0) return undefined
      selectors.push(rest.slice(1, close))
      rest = rest.slice(close + 1)
    }
    if (rest) return undefined
  }

  let value: unknown = source
  if (key) {
    if (!value || typeof value !== 'object') return undefined
    value = (value as Record<string, unknown>)[key]
  }

  for (const selector of selectors) {
    if (selector === '' || selector === '*') {
      if (!Array.isArray(value)) return undefined
      continue
    }
    if (!Array.isArray(value)) return undefined
    value = pickListIndex(value, selector)
  }
  return value
}

function pickListIndex(values: unknown[], selector: string): unknown {
  const index = Number.parseInt(selector, 10)
  if (Number.isNaN(index)) return undefined
  const normalized = index < 0 ? values.length + index : index
  if (normalized < 0 || normalized >= values.length) return undefined
  return values[normalized]
}

export function pickExactOrDottedChildren(source: unknown, key: string): unknown {
  if (!source || typeof source !== 'object' || Array.isArray(source) || !key) return undefined
  const record = source as Record<string, unknown>
  if (key in record) return record[key]
  const prefix = `${key}.`
  const nestedEntries = Object.entries(record).filter(([entryKey]) => entryKey.startsWith(prefix))
  if (!nestedEntries.length) return undefined
  const root: Record<string, unknown> = {}
  for (const [entryKey, entryValue] of nestedEntries) {
    const remainder = entryKey.slice(prefix.length)
    if (!remainder) continue
    const parts = remainder.split('.').filter(Boolean)
    if (!parts.length) continue
    let cursor: Record<string, unknown> = root
    for (const part of parts.slice(0, -1)) {
      const next = cursor[part]
      if (!next || typeof next !== 'object' || Array.isArray(next)) {
        cursor[part] = {}
      }
      cursor = cursor[part] as Record<string, unknown>
    }
    cursor[parts[parts.length - 1]] = entryValue
  }
  return Object.keys(root).length ? root : undefined
}

export function resolveBoundOutputValue(
  source: unknown,
  binding: Pick<InvocationVariableBinding, 'source_path' | 'alias' | 'variable_key'>,
): unknown {
  const candidates = [binding.source_path, binding.alias, binding.variable_key]
  for (const candidate of candidates) {
    const normalized = String(candidate || '').trim()
    if (!normalized) continue
    const exact = pickExactOrDottedChildren(source, normalized)
    if (exact !== undefined) return exact
    const picked = pickPath(source, normalized)
    if (picked !== undefined) return picked
  }
  return undefined
}

export function extractBoundOutputMaps(
  source: unknown,
  bindings: InvocationVariableBinding[],
): { byAlias: Record<string, unknown>; byVariableKey: Record<string, unknown> } {
  const byAlias: Record<string, unknown> = {}
  const byVariableKey: Record<string, unknown> = {}
  for (const binding of bindings || []) {
    const value = resolveBoundOutputValue(source, binding)
    if (value === undefined) continue
    if (binding.alias) byAlias[binding.alias] = value
    if (binding.variable_key) byVariableKey[binding.variable_key] = value
  }
  return { byAlias, byVariableKey }
}

