# flowly-blender

Drive [Blender](https://www.blender.org/) from your Flowly agent — render
scenes, export 3D assets, and run arbitrary `bpy` scripts via chat. Headless
under the hood; no GUI required.

## Install

```bash
flowly plugins install nocetic/flowly-blender
flowly plugins enable blender
flowly service restart
```

Verify:

```bash
/blender
```

If Blender is on your machine, you'll see its path + a tool reference. If not,
you'll get install instructions.

## Requirements

- Blender 3.6 LTS or later (4.x recommended for the new export operators)
- Auto-detected on:
  - macOS — `/Applications/Blender.app/...`
  - Linux — `blender` on PATH, `/opt/blender`, `/usr/local/bin`
  - Windows — `C:\Program Files\Blender Foundation\Blender 4.x\...`
- Override with `FLOWLY_BLENDER_PATH` in `~/.flowly/.env` if installed
  somewhere unusual.

## Tools

| Tool | What it does |
|---|---|
| `blender_run(script, blend_file?)` | Execute an arbitrary `bpy` script in headless mode. Generic escape hatch. |
| `blender_scene_info(blend_file)` | Inspect a `.blend` — meshes, cameras, lights, materials, frame range, render engine. |
| `blender_render(blend_file, preset?, output?, frame?, engine?, samples?, resolution?, cpu?, auto_open?)` | Render a frame or animation. **Three presets**: `preview` (Eevee 720p, ~sec — default), `quality` (Cycles 1080p@128, ~min), `final` (.blend defaults, ~10-30 min). **Auto-detects GPU**. **Validates output**. Pass `auto_open=True` to open the PNG. |
| `blender_export(blend_file, fmt, output?)` | Export to GLB / FBX / OBJ / USDZ / STL / PLY. Validates output. |
| `blender_create(description)` | High-level scene creation pattern — returns the convention the agent should follow. |

## Slash command

```
/blender
```

Returns Blender plugin status — installed path, output directory, and a quick
tool reference.

## Skill

`blender:scripting` ships with the plugin. The agent loads it via
`skill_view("blender:scripting")` when writing non-trivial bpy. It covers:

- **DO NOT rules** — never use `computer.*` or `exec open` to interact with
  Blender (the plugin is headless; use `auto_open=True` instead)
- Scene clearing + primitive creation
- Materials (4.x node-based BSDF)
- Lights + camera setup
- Render settings (Cycles / Eevee / Workbench)
- Animation keyframes
- Common errors and recovery

## pre_llm_call orientation hook

When the user's message contains 3D / Blender / render / mesh / model
keywords, the plugin injects a brief reminder telling the agent to use
`blender_*` tools exclusively (no `computer.activate_app`, no
`exec("open *.blend")`). Saves tokens by firing only when relevant.

## GPU rendering

Cycles renders auto-detect GPU on first call:

- **macOS** — Metal
- **NVIDIA** — OPTIX (preferred), CUDA fallback
- **AMD** — HIP
- **Intel** — oneAPI

If GPU init fails, the render falls back to CPU silently. Pass
`cpu=True` to `blender_render` to force CPU.

## Render presets

Default `blender_render` behaviour is `preset="preview"` — Eevee at
720p, finishing in seconds. The agent escalates only when the user
explicitly asks for higher quality.

| Preset | Engine | Resolution | Samples | Typical time (M-series Mac) |
|---|---|---|---|---|
| `preview` (default) | Eevee | 1280×720 | n/a | **2-10 sec** |
| `quality` | Cycles | 1920×1080 | 128 | 1-3 min |
| `final` | .blend's saved settings | (whatever's set) | (whatever's set) | 10-30 min |

Individual `engine`, `samples`, `resolution` parameters override the
preset. Resolution accepts `1280x720`, `1920x1080`, `4K`, `720p`,
`1080p`, `2K`, `UHD`, `QHD`, `FHD`, `HD`, `SD`.

## Cross-version Eevee compatibility

Blender 3.x and 5.x call the engine `BLENDER_EEVEE`. Blender 4.2-4.3
briefly used `BLENDER_EEVEE_NEXT`. The plugin auto-resolves the right
name at runtime — pass either, both work. Cycles is always `CYCLES`.

## Auto-detection across platforms

The plugin tries:

- **macOS** — `/Applications/Blender.app/...` and `~/Applications/...`
- **Linux** — `blender` on PATH, `/opt/blender`, `/snap/bin/blender`,
  Flatpak (`/var/lib/flatpak/...`), `~/.local/bin/blender`,
  `~/.var/app/org.blender.Blender/...`
- **Windows** — globs across `Program Files\Blender Foundation\Blender*\`,
  `Program Files (x86)`, `LOCALAPPDATA`, Steam install path,
  `WindowsApps`, plus a Windows registry probe of Uninstall keys
  for non-standard install locations. Multi-version installs
  resolve to the newest version.

Override anywhere with `FLOWLY_BLENDER_PATH=/path/to/blender` in
`~/.flowly/.env`.

## Examples

**"Render this .blend"**

```
You:    Render ~/Desktop/product-shot.blend at frame 60, Cycles, save to /tmp/out.png
Agent:  [calls blender_render(blend_file=..., frame=60, engine="CYCLES", output="/tmp/out.png")]
        Rendered. /tmp/out.png — 1920×1080, Cycles 128 samples.
```

**"Generate a low-poly tree"**

```
You:    Make a low-poly tree, export it as GLB.
Agent:  [writes a bpy script with cylinder trunk + scaled spheres for foliage]
        [calls blender_run(script=..., blend_file="")]
        [calls blender_export(blend_file="~/.flowly/blender-out/scene.blend", fmt="glb")]
        Done. ~/.flowly/blender-out/scene.glb — single mesh, 1.8 KB.
```

**"What's in this .blend?"**

```
You:    What's in ~/Downloads/character.blend?
Agent:  [calls blender_scene_info(...)]
        15 meshes, 1 camera, 3 lights, 4 materials. 250-frame timeline at 1080p Cycles.
```

## Output convention

Default landing zone: **`~/.flowly/blender-out/`**.

- `scene.blend` — the working file the script saves
- `<name>_render.png` — rendered frames
- `<name>.glb` / `.fbx` / `.usdz` / etc. — exports

The agent will use the user-provided path when given, otherwise fall back to
this directory. Override the parent via `FLOWLY_HOME=/your/path`.

## Troubleshooting

**"Blender not found"** in `/blender` output

- Verify Blender is installed: `which blender` or check the auto-detected paths
- Set `FLOWLY_BLENDER_PATH=/path/to/blender` in `~/.flowly/.env`
- Restart the gateway: `flowly service restart`

**Cycles GPU rendering doesn't work headless**

- The plugin doesn't force GPU. The agent's bpy script should set:
  ```python
  prefs = bpy.context.preferences.addons['cycles'].preferences
  prefs.compute_device_type = 'METAL'  # or 'CUDA' / 'OPTIX' / 'HIP'
  prefs.refresh_devices()
  for d in prefs.devices: d.use = True
  ```

**`bpy.ops.export_mesh.stl` AttributeError**

- Blender 4.2 retired the legacy STL operator. The plugin already falls back
  to `bpy.ops.wm.stl_export`. If you see this error, your Blender is even
  older than 3.6 — upgrade.

**Scripts time out**

- Default timeout is 300s for `blender_run`, 600s for `blender_render`. Bump
  the `timeout` parameter for batch jobs.

## License

Apache-2.0. Same as flowlyai.

## Author

Flowly contributors. Plugin is community-maintained — PRs welcome.
