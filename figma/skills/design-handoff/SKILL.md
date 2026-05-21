---
name: design-handoff
description: Patterns for working from a Figma file via the figma_* tools. Load BEFORE you start reading a design — covers URL parsing, page/frame orientation, export workflow, and how to surface results to the user without dumping raw API JSON. Use when the user asks about a Figma file, design specs, asset export, or design tokens.
metadata: {"flowly":{"emoji":"🎨"}}
---

# Figma design handoff

This skill is loaded explicitly via `skill_view("figma:design-handoff")`
whenever the user references a Figma file. The tools cover the
read-side of Figma's REST API — the write-side (modifying nodes) only
exists inside Figma's in-app plugin runtime, which Flowly cannot reach.

## DO NOT

- **Don't paste the raw `figma_read` JSON back at the user.** It's
  there for you to reason over, not for them to skim. Distill into
  prose / bullet list / table.
- **Don't try to create or edit frames** via Flowly. There is no API
  for that here. If the user wants edits, tell them clearly and
  suggest they open Figma directly — or have you produce a comment
  that describes the change.
- **Don't fetch the entire file** when the user only cares about one
  frame. Use `figma_file_info` to get page IDs, then
  `figma_read(file, node_id=...)` to drill in. Saves tokens + API
  budget.
- **Don't guess the scale or format.** Ask if the user hasn't said;
  defaults are PNG @ 2x.

## URL handling

Figma URLs in the wild look like:

```
https://www.figma.com/file/ABC123XYZ/Project-Name?node-id=1-23
https://www.figma.com/design/ABC123XYZ/Project-Name?node-id=1-23&t=…
https://www.figma.com/proto/ABC123XYZ/Project-Name?page-id=0%3A1
```

`figma_file_info` / `figma_read` / etc. accept the URL directly — they
extract the file key internally. But if you spot a `node-id=1-23` in
the URL, **use that as `node_id`** when reading — that's the frame
the user is currently looking at and is almost always what they mean
by "this".

Node IDs come in two notations:
- `1:23` — canonical (what the API returns)
- `1-23` — URL-encoded (what query strings use)

The tools accept both; you don't have to convert.

## Orientation workflow

When a user drops a Figma link and asks a vague question ("describe
this", "what's in here"), do this:

1. `figma_file_info(url)` — get name + page list. ~50 tokens.
2. Pick the relevant page (often the first non-empty one).
3. `figma_read(url, node_id=<page_id>, depth=3)` — pull the top of the
   tree. Identify the frame(s) the user probably means.
4. Drill further with smaller `node_id` calls only if needed.

For a specific frame request ("export this frame"), skip 1 and 2; go
straight to `figma_export(url, node_id=…)`.

## Export workflow

```
figma_export(file=..., node_id="1:2", fmt="png", scale=2)
```

- Defaults: PNG @ 2x, written to `~/.flowly/figma-out/`.
- Multiple frames in one call: `node_id="1:2,3:4,5:6"`.
- For dev handoff, prefer `fmt="svg"` when the asset is icon-like or
  vector-only — sharper at any size and Tailwind/component-friendly.
- For marketing renders, PNG @ 4x.
- For PDF (rare): pass `fmt="pdf"`; `scale` is ignored.

After export, surface the file paths to the user like:

> Exported `frame-name.png` (1.4 MB) → `~/.flowly/figma-out/ABC_1-2.png`

Do NOT auto-open the file unless the user explicitly asked to "see"
it — and even then prefer a Markdown image link the desktop client
can inline. The plugin does NOT auto-open like Blender's render
flow; it's a server-side download.

## Spec extraction patterns

Common requests + the structure that maps to them:

| User asks | Tool call | What to surface |
|---|---|---|
| "What are the colors?" | `figma_read(...)` then look at `fills` per node, or pull design tokens from the file's variables (if available) | Hex codes grouped by usage |
| "Type sizes" | `figma_read(node_id=<text-styles-page>)` | Heading sizes, body sizes, line heights |
| "Find all CTAs" | `figma_search(file, "CTA")` or `figma_search(file, "button")` | List of frame names + node ids |
| "Export icons" | `figma_search` to find them → `figma_export(fmt="svg")` | Output paths |
| "Spacing system" | `figma_read` of a "Spacing" or "Tokens" page | Token name → px value |
| "Component list" | `figma_search(file, "")` filtering type=="COMPONENT" | Names + ids |

## Comment-back workflow

Reviewers often want the agent to comment FOR them ("leave a note
on this frame that X"). The flow:

```
figma_comment_add(file=..., node_id="1:2", body="Spacing here should be 16 not 8 — matches the rest of the page.")
```

When the user describes vague feedback, write it in their voice
(succinct, specific). For multi-frame reviews, leave one focused
comment per frame rather than one giant comment that targets nothing.

Without a node_id the comment is "file-level" — fine for general
notes but harder to find in Figma's UI. Prefer pinned.

## Edge cases

- **Token missing**: every tool returns "Error: FIGMA_ACCESS_TOKEN is
  not set …". Don't retry — tell the user how to set it (the
  `/figma` slash command spells it out).
- **403 on a public-looking file**: the PAT might not have access. The
  file owner has to share with the PAT's user.
- **404 with a valid-looking key**: file deleted, or the user pasted a
  prototype URL whose file was archived. Confirm with the user.
- **Massive files**: `figma_read` truncates at ~8 KB of JSON. If you
  hit that, narrow by `node_id`.
- **Rate limit (429)**: the request layer auto-retries twice. If it
  still fails, wait a minute and try again — or batch fewer ids per
  call.

## Output style

Always summarise for the user. The model's value is in the
*translation* from API JSON into actionable design language. A good
reply looks like:

> The "Login" frame has a 24px title, 14px body, primary CTA fills
> with `#4F46E5`, and 16-pt corner radius. 14 child components,
> mostly tokens from the team library. Want the title styles
> exported as a token file?

A bad reply is the raw `figma_read` JSON dumped between code fences.
