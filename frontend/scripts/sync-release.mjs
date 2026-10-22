// Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'release-manifest.json'), 'utf8'))
const check = process.argv.includes('--check')
const writes = []
function json(relative, apply) {
  const file = path.join(root, relative)
  const original = fs.readFileSync(file, 'utf8')
  const value = JSON.parse(original)
  apply(value)
  if (JSON.stringify(value) !== JSON.stringify(JSON.parse(original))) writes.push([file, JSON.stringify(value, null, 2) + '\n'])
}
json('frontend/package.json', v => { v.version = manifest.version; v.engines = { node: manifest.node } })
json('frontend/package-lock.json', v => {
  v.version = manifest.version
  v.packages[''].version = manifest.version
  v.packages[''].engines = { node: manifest.node }
})
json('frontend/src-tauri/tauri.conf.json', v => { v.version = manifest.version })
for (const relative of ['frontend/src-tauri/Cargo.toml', 'frontend/src-tauri/Cargo.lock']) {
  const file = path.join(root, relative)
  const before = fs.readFileSync(file, 'utf8')
  const after = before.replace(/(name = "taleweft"\r?\nversion = ")[^"]+(")/, `$1${manifest.version}$2`)
  if (after !== before) writes.push([file, after])
}
const generated = path.join(root, 'frontend/src/generated/release.ts')
const content = `// Generated from release-manifest.json by sync-release.mjs.\nexport const release = ${JSON.stringify(manifest, null, 2)} as const\n`
if (!fs.existsSync(generated) || fs.readFileSync(generated, 'utf8') !== content) writes.push([generated, content])
if (check && writes.length) throw new Error(`Release metadata is out of sync: ${writes.map(([p]) => path.relative(root, p)).join(', ')}`)
for (const [file, content] of writes) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, content)
}
console.log(`Release ${manifest.version} (${manifest.build_id}): ${check ? 'verified' : 'synchronized'}`)
