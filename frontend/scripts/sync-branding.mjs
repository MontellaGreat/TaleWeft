// Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
// TaleWeft: keep packaged legal notices identical to their source files.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const target = path.join(root, 'frontend/public/legal')
const check = process.argv.includes('--check')
const sources = [
  ['LICENSE', 'LICENSE.txt'],
  ['NOTICE', 'NOTICE.txt'],
  ['CONTRIBUTORS.md', 'CONTRIBUTORS.txt'],
  ['MODIFICATIONS.md', 'MODIFICATIONS.md'],
]
for (const [source, output] of sources) {
  const bytes = fs.readFileSync(path.join(root, source))
  const destination = path.join(target, output)
  if (fs.existsSync(destination) && fs.readFileSync(destination).equals(bytes)) continue
  if (check) throw new Error(`Packaged legal notice is out of sync: ${output}`)
  fs.mkdirSync(target, { recursive: true })
  fs.writeFileSync(destination, bytes)
}
console.log(`TaleWeft legal notices ${check ? 'verified' : 'synchronized'}`)
// Generated assets live in the project so normal builds need no Python/Pillow.
const brandingSource = path.join(root, 'frontend/branding-source')
const manifest = JSON.parse(fs.readFileSync(path.join(brandingSource, 'manifest.json'), 'utf8'))
const sha = bytes => createHash('sha256').update(bytes).digest('hex')
if (sha(fs.readFileSync(path.join(brandingSource, 'woven-book-original.png'))) !== manifest.source_sha256) {
  throw new Error('Brand artwork changed: regenerate with frontend/scripts/generate-branding.py')
}
for (const [relative, expected] of Object.entries(manifest.outputs)) {
  const source = path.join(brandingSource, 'generated', relative)
  const bytes = fs.readFileSync(source)
  if (sha(bytes) !== expected) throw new Error(`Generated brand asset is stale: ${relative}`)
  const destination = path.join(root, relative)
  if (fs.existsSync(destination) && fs.readFileSync(destination).equals(bytes)) continue
  if (check) throw new Error(`Brand asset is out of sync: ${relative}`)
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  fs.writeFileSync(destination, bytes)
}
console.log(`TaleWeft artwork ${check ? 'verified' : 'synchronized'}`)
