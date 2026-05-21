#!/usr/bin/env node
/**
 * Build the top-level registry.json from each plugin's
 * marketplace.json. Triggered by CI on every push to main.
 *
 * Source of truth: each plugin folder owns ITS slice of the
 * marketplace metadata. This script just concatenates, derives the
 * install command (so plugin authors can't typo it), and writes the
 * combined file.
 *
 * Schema mirrors the PluginEntry type in flowly-app:
 *   lib/plugins/registry.ts
 *
 * Adding a plugin:
 *   1. Create `<name>/marketplace.json` with the fields below.
 *   2. Push — CI runs this script and commits registry.json back.
 *
 * Removing a plugin:
 *   Delete its `marketplace.json`. The next CI run drops it from the
 *   registry. (The plugin code can stay if you want to preserve
 *   install commands — it just won't appear in the marketplace.)
 */

import { promises as fs } from 'node:fs'
import path from 'node:path'

const REPO_ROOT = path.resolve(new URL('.', import.meta.url).pathname, '..')
const OUTPUT = path.join(REPO_ROOT, 'registry.json')

const REQUIRED_FIELDS = [
  'slug',
  'name',
  'tagline',
  'description',
  'category',
  'version',
  'repo',
  'author',
]

const KNOWN_CATEGORIES = new Set([
  'creative',
  'productivity',
  'development',
  'data',
  'automation',
  'integration',
  'utility',
  'other',
])

async function readPluginMetadata(pluginDir) {
  const metaPath = path.join(pluginDir, 'marketplace.json')
  try {
    const raw = await fs.readFile(metaPath, 'utf-8')
    return { metaPath, data: JSON.parse(raw) }
  } catch (err) {
    if (err.code === 'ENOENT') return null
    throw new Error(`Failed to parse ${metaPath}: ${err.message}`)
  }
}

function validate(entry, source) {
  for (const field of REQUIRED_FIELDS) {
    if (!entry[field]) {
      throw new Error(`${source}: missing required field "${field}"`)
    }
  }
  if (!KNOWN_CATEGORIES.has(entry.category)) {
    throw new Error(
      `${source}: unknown category "${entry.category}" — must be one of ${[...KNOWN_CATEGORIES].join(', ')}`,
    )
  }
  if (entry.source && entry.source !== 'github' && entry.source !== 'zip') {
    throw new Error(`${source}: source must be 'github' or 'zip'`)
  }
  if (typeof entry.author !== 'object' || !entry.author?.name) {
    throw new Error(`${source}: author.name is required`)
  }
}

function deriveInstallCommand(entry) {
  // The plugin author shouldn't have to maintain installCommand by
  // hand — derive it from repo + subpath so it's always correct.
  // ZIP-source plugins use the slug; GitHub-source uses repo +
  // optional /subpath.
  if (entry.source === 'zip') return `flowly plugins install ${entry.slug}`
  const subpath = (entry.subpath ?? '').trim().replace(/^\/+|\/+$/g, '')
  return subpath
    ? `flowly plugins install ${entry.repo}/${subpath}`
    : `flowly plugins install ${entry.repo}`
}

async function main() {
  const entries = (await fs.readdir(REPO_ROOT, { withFileTypes: true }))
    .filter((d) => d.isDirectory())
    .filter((d) => !d.name.startsWith('.') && d.name !== 'node_modules' && d.name !== 'scripts')

  const plugins = []
  for (const entry of entries) {
    const pluginDir = path.join(REPO_ROOT, entry.name)
    const meta = await readPluginMetadata(pluginDir)
    if (!meta) continue
    validate(meta.data, meta.metaPath)
    // Sanity: the folder name should match the manifest name so an
    // install command of `repo/<dir>` lands on the right plugin.
    if (meta.data.slug !== entry.name) {
      throw new Error(
        `${meta.metaPath}: slug "${meta.data.slug}" does not match folder name "${entry.name}"`,
      )
    }
    plugins.push({
      ...meta.data,
      installCommand: meta.data.installCommand ?? deriveInstallCommand(meta.data),
    })
  }

  // Featured first, then alphabetical — stable ordering so diffs are
  // small + featured entries surface at the top of curated lists.
  plugins.sort((a, b) => {
    const featuredDiff = Number(!!b.featured) - Number(!!a.featured)
    if (featuredDiff !== 0) return featuredDiff
    return a.slug.localeCompare(b.slug)
  })

  const registry = {
    generatedAt: new Date().toISOString(),
    schemaVersion: 1,
    plugins,
  }
  await fs.writeFile(
    OUTPUT,
    JSON.stringify(registry, null, 2) + '\n',
    'utf-8',
  )
  console.log(
    `Wrote ${plugins.length} plugin(s) to ${path.relative(REPO_ROOT, OUTPUT)}`,
  )
  for (const p of plugins) {
    console.log(`  - ${p.slug} v${p.version} — ${p.installCommand}`)
  }
}

main().catch((err) => {
  console.error('build-registry failed:', err)
  process.exit(1)
})
