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
| [figma](./figma) | Read, search, comment, and export from Figma files via the REST API | `flowly plugins install Nocetic/plugins/figma` |

## Repository layout

```
plugins/
├── README.md          (this file)
├── LICENSE
└── <name>/
    ├── plugin.yaml    (manifest)
    ├── __init__.py    (entry point)
    ├── README.md      (plugin docs)
    └── skills/        (optional — Markdown skills the agent loads on demand)
```

Each plugin is versioned independently via its `plugin.yaml`'s
`version` field. The repo carries a single SPDX-Apache-2.0 license
that applies to every subdirectory.

## Contributing

Open a PR with a new subdirectory containing at minimum:

- `plugin.yaml` with `name`, `version`, `description`, `author`, and the
  `provides_*` block declaring everything the plugin registers.
- `__init__.py` with a `register(ctx)` function.
- `README.md` explaining setup, env vars, and the tool surface.

See [`docs.useflowlyapp.com/docs/plugins`](https://useflowlyapp.com/docs/plugins)
for the full plugin authoring guide.

## License

[Apache-2.0](./LICENSE).
