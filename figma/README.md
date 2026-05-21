# flowly-figma

Read, search, comment on, and export from [Figma](https://www.figma.com/)
files via the public REST API, exposed to your Flowly agent as tools.

## Install

```bash
flowly plugins install Nocetic/plugins/figma
flowly plugins enable figma
flowly service restart
```

Confirm with:

```bash
/figma
```

## Setup

The plugin needs a Figma personal access token.

1. Visit <https://www.figma.com/developers/api#access-tokens> →
   **Personal access tokens** → **Generate new token**. Scopes:
   *file content (read)* and *comments (write)*.
2. Add it to `~/.flowly/.env`:

   ```
   FIGMA_ACCESS_TOKEN=figd_xxx...
   ```

3. Restart Flowly so the env var is picked up.

## Tools

| Tool | Purpose |
|---|---|
| `figma_file_info(file)` | Name, last modified, version, page list |
| `figma_read(file, node_id?, depth?)` | Node tree summary (id, name, type, text, bounds) |
| `figma_export(file, node_id, fmt?, scale?, output?)` | PNG / JPG / SVG / PDF export |
| `figma_search(file, query, limit?)` | Match node names + text content |
| `figma_comment_add(file, body, node_id? \| x?, y?)` | Post a comment |

All tools accept either a full Figma URL or the bare file key.

## Output

Exports default to `~/.flowly/figma-out/<file-key>_<node-id>.<fmt>`.
Override with the `output` argument.

## Slash command

```
/figma
```

Reports plugin status, token state, and the tool reference.

## Skill

`figma:design-handoff` ships alongside the plugin. The agent loads it
via `skill_view("figma:design-handoff")` whenever it works from a
Figma file. Covers:

- URL parsing (file URL → file key, node-id query param → node_id)
- Orientation flow (file_info → read → drill)
- Export defaults + when to use SVG vs PNG vs PDF
- Spec extraction patterns
- Comment-back workflow
- Edge cases (token missing, 403/404, rate limit, huge files)

## What it can't do

The Figma **REST** API is read + comment only. Modifying nodes
(creating frames, changing properties, rearranging the canvas) only
works inside Figma's in-app plugin runtime — Flowly can't reach that
from here. If you ask the plugin to "make the title bigger", it will
tell you and suggest opening Figma directly.

## License

Apache-2.0. Same as Flowly.
