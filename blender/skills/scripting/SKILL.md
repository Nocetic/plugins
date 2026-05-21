---
name: scripting
description: Patterns for writing Blender Python (bpy) scripts via the blender_run tool. Use when the user asks for 3D work, custom rendering, scene generation, asset processing, or anything that needs the bpy API. Read this BEFORE composing a script — it covers conventions (output paths, scene clearing, save points), common operators across Blender 4.x, and recovery patterns for the failures you'll actually hit.
metadata: {"flowly":{"emoji":"🎨"}}
---

# Blender Scripting

Write bpy scripts the agent can ship via `blender_run`. Headless,
deterministic, no GUI assumptions.

## Render presets — pick the right speed/quality tradeoff

`blender_render` has three presets. **Use `preview` by default** when
the user just says "render", "show me", "preview", or anything casual.
Cycles full quality is for explicit requests only.

| User said | Preset | Engine | Settings | Time |
|---|---|---|---|---|
| "render et", "show me", "preview", "quick render" | `preview` (default) | Eevee Next | 1280×720 | **2-10 sec** |
| "high quality render", "kaliteli", "make it look good" | `quality` | Cycles | 1920×1080, 128 samples | 1-3 min |
| "final render", "production", "publish quality", "son render" | `final` | (uses .blend defaults) | typically 1080p+ × 4096 samples | 10-30 min |

**Default is `preview` — if you don't pass a preset, you get Eevee.**
The user almost never wants to wait 10 minutes for a default render.
Only escalate to `quality` or `final` when they explicitly ask.

### Examples

```python
# Casual "render et" → preview (Eevee 720p, ~5 sec)
blender_render(blend_file="...", auto_open=True)

# "give me a high-quality render" → quality (Cycles 1080p@128, ~2 min)
blender_render(blend_file="...", preset="quality", auto_open=True)

# "render at 4K with 1024 samples" → mix preset + overrides
blender_render(blend_file="...", preset="quality", resolution="4K", samples=1024)

# "final production render" → final (use .blend's own settings)
# Tell the user FIRST: "This may take 15-30 min, OK?"
blender_render(blend_file="...", preset="final", auto_open=True)
```

### Resolution shortcuts

`resolution` accepts `1280x720`, `1920x1080`, `3840x2160`, OR aliases:
`720p`, `1080p`, `2K`, `4K`, `UHD`, `QHD`, `FHD`, `HD`, `SD`.

### When the user already has render settings in the .blend

If they hand you a .blend file with deliberate render settings (a
designer's working file), use `preset="final"` to respect those.
Don't override unless asked. For a fresh `blender_run`-built scene
without explicit settings, use `preview` or `quality`.

## DO NOT — tool selection rules

The Blender plugin is **headless-only**. Every tool runs `blender -b`,
no GUI ever opens. To show a rendered PNG to the user, use
`blender_render(auto_open=True)` — this opens the PNG in the OS
default viewer.

### Forbidden moves

- ❌ `computer.activate_app("Blender")` — there is no Blender
  *app* to activate; you'd be launching the GUI which the user
  doesn't want, and the agent has nothing to do with it.
- ❌ `computer.see({"app_name": "Blender"})` — pointless, the GUI
  isn't running. Will time out.
- ❌ `computer.screenshot()` to inspect render — the render output
  is a file on disk; just read its path or open it with `auto_open`.
- ❌ `exec("open *.blend")` / `exec("open *.png")` — duplicates
  what `auto_open=True` already does, but worse (no validation).
- ❌ `exec("pkill Blender")` — never kill the user's GUI Blender
  instance. The plugin's subprocesses are independent.

### Correct moves

- ✅ `blender_render(blend_file=..., auto_open=True)` — renders
  AND opens the PNG for the user. Single clean call.
- ✅ Reply text includes the PNG path so the user can find it
  later. Example:
  > Rendered to `~/.flowly/blender-out/scene.png` (1.2 MB).
- ✅ `blender_scene_info(blend_file=...)` BEFORE rendering an
  unfamiliar file — saves you one bad render.
- ✅ When uncertain about bpy syntax: `skill_view("blender:scripting")`
  loads this guide. Don't guess at operator names.

## Tool dispatch — pick the right one

| Goal | Use this tool |
|---|---|
| Render a `.blend` someone gave you | `blender_render` |
| Export a `.blend` to GLB / FBX / etc. | `blender_export` |
| Inspect what's in a `.blend` | `blender_scene_info` |
| Build a NEW scene from scratch | `blender_run` (with bpy script) |
| Modify an existing `.blend` (materials, lights, animations) | `blender_run` |
| Anything outside the above | `blender_run` |

`blender_run` is the escape hatch. It opens an optional `.blend`,
executes your script, returns stdout/stderr.

## Script template

Always start scripts with these conventions:

```python
import bpy
import sys

# 1. Clear default cube + light + camera unless we explicitly want them
def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    # Also clear orphan data so the file stays small
    for collection in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras):
        for item in list(collection):
            if item.users == 0:
                collection.remove(item)

clear_scene()

# 2. Build the scene
# ... your bpy code here ...

# 3. Save the .blend so the user has a working file
out = "/Users/<you>/.flowly/blender-out/scene.blend"
bpy.ops.wm.save_as_mainfile(filepath=out)
print(f"SAVED: {out}")
```

The `print(f"SAVED: …")` is the contract — the agent reads stdout to
verify the script reached the save point.

## Primitives + transforms

```python
# Cube
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 1))
cube = bpy.context.active_object
cube.name = "MyCube"
cube.rotation_euler = (0.3, 0, 0.5)
cube.scale = (1, 2, 0.5)

# UV sphere
bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(3, 0, 1))

# Cylinder
bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=2.0, location=(-3, 0, 1))

# Plane (ground)
bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, 0))
```

`bpy.context.active_object` is the most-recently-created object —
keep it on a local variable BEFORE any further `bpy.ops` calls,
because operators can change context.

## Materials (4.x — node-based)

```python
def make_material(name, base_color=(0.8, 0.2, 0.1, 1.0), roughness=0.5, metallic=0.0):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = base_color
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    return mat

red = make_material("RedMatte", (0.8, 0.1, 0.1, 1.0), 0.6)
cube.data.materials.append(red)
```

## Lights + camera

```python
# Sun
bpy.ops.object.light_add(type='SUN', location=(5, -5, 10))
sun = bpy.context.active_object
sun.data.energy = 3.0
sun.rotation_euler = (0.6, 0.2, -0.5)

# Camera
bpy.ops.object.camera_add(location=(7, -7, 5), rotation=(1.1, 0, 0.78))
cam = bpy.context.active_object
bpy.context.scene.camera = cam   # active camera for rendering
```

## Render settings

```python
sc = bpy.context.scene
sc.render.engine = 'CYCLES'      # or 'BLENDER_EEVEE_NEXT' for fast preview
sc.render.resolution_x = 1920
sc.render.resolution_y = 1080
sc.render.resolution_percentage = 100
sc.cycles.samples = 128          # quality vs speed
sc.cycles.device = 'GPU'         # if available; falls back to CPU
sc.frame_start = 1
sc.frame_end = 250
```

## Animation (keyframes)

```python
cube.location = (0, 0, 1)
cube.keyframe_insert(data_path="location", frame=1)

cube.location = (5, 0, 3)
cube.keyframe_insert(data_path="location", frame=60)

cube.rotation_euler = (0, 0, 6.28)  # one full rotation
cube.keyframe_insert(data_path="rotation_euler", frame=120)
```

## Export from inside a script

If you need export as part of a generation script (vs the
`blender_export` tool):

```python
bpy.ops.object.select_all(action='DESELECT')
for o in bpy.data.objects:
    if o.type == 'MESH':
        o.select_set(True)

bpy.ops.export_scene.gltf(
    filepath="/Users/.../out.glb",
    export_format='GLB',
    use_selection=True,
    export_apply=True,
)
```

## Output convention

Default output dir: **`~/.flowly/blender-out/`** (created automatically).

Use it unless the user gave a specific path. Sub-conventions:

- `scene.blend` — the working file (always save this)
- `<name>_render.png` — rendered frames
- `<name>.glb` / `<name>.fbx` / etc. — exports
- `preview.png` — quick screenshot of the scene from the active camera

## Common errors + fixes

**`AttributeError: 'NoneType' object has no attribute 'select_set'`**
→ `bpy.context.active_object` is None because the previous operator
left no selection. Re-select after critical operators:

```python
obj = bpy.context.active_object
if obj is None:
    raise RuntimeError("expected active object after primitive_cube_add")
```

**`RuntimeError: Operator bpy.ops.object.delete.poll() failed`**
→ Nothing selected to delete. `bpy.ops.object.select_all(action='SELECT')` first.

**`AttributeError: bpy.ops.export_mesh.stl`** (Blender 4.2+)
→ Legacy STL operator removed. Use `bpy.ops.wm.stl_export(...)` instead.
The same applies to obj (`bpy.ops.wm.obj_export`) and ply.

**`gltf` export silently does nothing**
→ No selection. Always `select_all(action='DESELECT')` then explicitly
select meshes before calling export.

**Cycles rendering with no GPU detected**
→ `sc.cycles.device = 'GPU'` requires Cycles to have queried devices.
If headless, GPU might not initialise. Either set device explicitly:

```python
prefs = bpy.context.preferences.addons['cycles'].preferences
prefs.compute_device_type = 'METAL'  # Mac. CUDA / OPTIX for NVIDIA, HIP for AMD
prefs.refresh_devices()
for d in prefs.devices:
    d.use = True
```

…or fall back to CPU explicitly: `sc.cycles.device = 'CPU'`.

**Script appears to succeed but the .blend wasn't saved**
→ You forgot `bpy.ops.wm.save_as_mainfile(filepath=...)`. Always save.

## Run-loop pattern (agent-side)

When generating a scene from a description, follow this shape:

1. Acknowledge the request, restate the goal in 1 sentence
2. Write the bpy script (using patterns above)
3. Call `blender_run(script=<your-script>)` with appropriate timeout
4. Read stdout — confirm `SAVED:` line appears
5. Optional: call `blender_render` on the produced .blend for a preview
6. Tell the user where the file is + summary of what was made

Keep scripts under 200 lines. If the request needs more, break into
phases (geometry, materials, lighting, animation) and run sequentially —
each phase saves its own checkpoint .blend.

## What NOT to do

- **Don't try to use `bpy.context.window_manager.windows[...]`** in
  headless mode — there are no windows. Operators that need them fail.
- **Don't assume the default scene is empty.** Blender opens with
  `Cube`, `Light`, `Camera`. `clear_scene()` first if you want clean
  state.
- **Don't run interactive operators** like `bpy.ops.transform.translate`
  with modal=True — they hang headless. Use direct attribute assignment
  (`obj.location = ...`) instead of `bpy.ops.transform.*` whenever
  possible.
- **Don't use deprecated APIs** without checking version. The user
  may have Blender 4.2 or 3.6 — patterns differ. When unsure, wrap
  in try/except and fall back.
- **Don't render at 8K + Cycles 1024 samples** without warning. Tell
  the user the expected time first.
