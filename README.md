# Nocetic plugins for Flowly

Official monorepo for [Flowly](https://useflowlyapp.com) plugins
maintained by Nocetic. Each subdirectory is a self-contained Flowly
plugin — install one directly with:

```bash
flowly plugins install Nocetic/plugins/<name>
```

## Available plugins

| Plugin | Description | Install |
|---|---|---|
| [blender](./blender) | Render, export, and run bpy scripts via chat — headless Blender automation with GPU auto-detect | `flowly plugins install Nocetic/plugins/blender` |
| [figma](./figma) | Read, search, comment, and export from Figma files via the REST API | `flowly plugins install Nocetic/plugins/figma` |

## Repository layout

```
plugins/
├── README.md                 (this file)
├── LICENSE                   (Apache-2.0, applies to every plugin)
├── registry.json             (auto-generated — feeds useflowlyapp.com/plugins)
├── scripts/
│   └── build-registry.mjs    (rebuilds registry.json from marketplace.json files)
├── .github/workflows/
│   └── build-registry.yml    (CI: regenerate on push to main)
└── <name>/
    ├── plugin.yaml           (Flowly manifest — declares tools/hooks/commands)
    ├── __init__.py           (entry point with register(ctx))
    ├── marketplace.json      (web marketplace metadata — feeds registry.json)
    ├── README.md             (plugin-specific docs)
    └── skills/               (optional Markdown skills the agent loads on demand)
```

Each plugin owns its slice of the marketplace metadata via
`marketplace.json`. The CI workflow (`build-registry.yml`)
re-derives the top-level `registry.json` whenever any
`marketplace.json` changes, and pushes the rebuilt file back to
main. The Flowly web marketplace fetches the rebuilt file from
`raw.githubusercontent.com/Nocetic/plugins/main/registry.json` with a
short cache, so a merged PR is live in the marketplace within
minutes — no flowly-app deploy needed.

## Adding a new plugin

1. Make a new subdirectory: `mkdir <name>`
2. Drop in `plugin.yaml`, `__init__.py`, `marketplace.json`, and
   `README.md`. Keep `marketplace.json`'s `slug` equal to the folder
   name — the CI build will refuse to commit otherwise.
3. Optional: `skills/<skill-name>/SKILL.md` for any Markdown skills.
4. Open a PR. Once merged, CI rebuilds `registry.json` and the
   plugin appears in the marketplace within ~5 minutes.

See
[`useflowlyapp.com/docs/plugins`](https://useflowlyapp.com/docs/plugins)
for the full plugin authoring guide (manifest schema, register
helpers, hook events, etc.).

## Updating an existing plugin

Bump the `version` in **both** the plugin's `plugin.yaml` and its
`marketplace.json` in the same PR, then merge. Users will see the
new version in the marketplace, and the next time they run
`flowly plugins install <name> --force` they'll pull the latest.

## License

[Apache-2.0](./LICENSE). Applies to every plugin in this repo.
