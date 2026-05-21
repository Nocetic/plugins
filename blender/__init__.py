"""Blender integration for Flowly.

Drives Blender headlessly via `blender -b -P script.py`. Five tools
plus a slash command and an on-demand bpy scripting skill.

Tier-1 production hardening:
  * GPU auto-detection (Metal / CUDA / OPTIX / HIP / oneAPI) injected
    into every render — Cycles uses GPU when available without the
    agent or user setting it manually.
  * Output validation — every render checks the produced PNG exists
    and is non-empty before reporting success. No silent failures.
  * Error classification — stderr is parsed into a known-pattern label
    (compile / api-version / gpu / missing-file / memory) with a fix
    hint placed at the TOP of the response so the agent reads it
    before the verbose log.
  * Auto-open — `blender_render(auto_open=True)` opens the produced
    PNG in the OS default viewer, eliminating the need for the agent
    to use `computer` / `exec open` to "show" the result.
  * Orientation hook — `pre_llm_call` injects a brief reminder to use
    blender_* tools exclusively when the user mentions 3D / render /
    Blender keywords. Stops the agent from grabbing `computer` or
    `exec open` to interact with Blender.

When Blender is not installed, every tool returns a polite
"unavailable" string — the agent can still respond gracefully.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any


# ── Path detection ────────────────────────────────────────────────


def _version_key(path_str: str) -> tuple[int, int]:
    """Extract (major, minor) version from a Blender install path.
    Higher = newer. Falls back to (0, 0) when no match."""
    m = re.search(r"[Bb]lender[\s_-]*(\d+)\.(\d+)", path_str)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def _macos_candidates() -> list[str]:
    """macOS install locations. /Applications first, ~/Applications second."""
    return [
        "/Applications/Blender.app/Contents/MacOS/Blender",
        str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender"),
    ]


def _linux_candidates() -> list[str]:
    """Linux: package managers, snap, flatpak, user-local."""
    paths = [
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        "/opt/blender/blender",
        "/snap/bin/blender",
        "/var/lib/flatpak/exports/bin/org.blender.Blender",
        str(Path.home() / ".local/bin/blender"),
        str(Path.home() / ".var/app/org.blender.Blender/data/blender/blender"),
    ]
    # Glob /opt for any "blender-X.Y" subdirectories
    try:
        for sub in Path("/opt").glob("blender-*"):
            exe = sub / "blender"
            if exe.is_file():
                paths.append(str(exe))
    except OSError:
        pass
    return paths


def _windows_candidates() -> list[str]:
    """Windows: glob across Program Files / ProgramFiles(x86) / LOCALAPPDATA,
    plus Steam, plus a Windows registry probe."""
    candidates: list[str] = []
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ]

    def _add_glob(root: str | None, *segments: str) -> None:
        """Glob `<root>/<segments>/Blender*/blender.exe` and append matches."""
        if not root:
            return
        try:
            base = Path(root, *segments)
            if not base.is_dir():
                return
            for sub in base.glob("Blender*"):
                exe = sub / "blender.exe"
                if exe.is_file():
                    candidates.append(str(exe))
        except OSError:
            pass

    for root in roots:
        # Standard Foundation install: Program Files\Blender Foundation\Blender X.Y
        _add_glob(root, "Blender Foundation")
        # User-local: LOCALAPPDATA\Programs\Blender Foundation\...
        _add_glob(root, "Programs", "Blender Foundation")
        # Microsoft Store / WindowsApps style (rare but possible)
        _add_glob(root, "WindowsApps")

    # Steam install
    for root in roots:
        if not root:
            continue
        try:
            steam = Path(root) / "Steam/steamapps/common/Blender/blender.exe"
            if steam.is_file():
                candidates.append(str(steam))
        except OSError:
            pass

    # Windows registry probe (last resort) — handles installs in odd places
    candidates.extend(_windows_registry_blender_paths())

    # Newest version first when multiple installs are found
    return sorted(set(candidates), key=_version_key, reverse=True)


def _windows_registry_blender_paths() -> list[str]:
    """Probe Windows Uninstall registry keys for any Blender install."""
    if sys.platform != "win32":
        return []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []

    found: list[str] = []
    hives_subkeys = [
        ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    hive_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

    for hive_name, subkey_path in hives_subkeys:
        try:
            with winreg.OpenKey(hive_map[hive_name], subkey_path) as parent:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(parent, idx)
                    except OSError:
                        break
                    idx += 1
                    if not sub.lower().startswith("blender"):
                        continue
                    try:
                        with winreg.OpenKey(parent, sub) as key:
                            try:
                                install_loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                            except FileNotFoundError:
                                continue
                            if not install_loc:
                                continue
                            exe = Path(install_loc) / "blender.exe"
                            if exe.is_file():
                                found.append(str(exe))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _platform_candidates() -> list[str]:
    """Build a platform-specific list of executable candidates, newest first."""
    if sys.platform == "darwin":
        return _macos_candidates()
    if sys.platform.startswith("linux"):
        return _linux_candidates()
    if sys.platform == "win32":
        return _windows_candidates()
    return []


def _blender_path() -> str | None:
    """Resolve the Blender executable, or None if not installed.

    Resolution order:
      1. ``FLOWLY_BLENDER_PATH`` env var (explicit override — always wins)
      2. ``blender`` on PATH
      3. Platform-specific install locations (newest version first when multiple)
      4. Windows registry probe (last resort)
    """
    override = os.getenv("FLOWLY_BLENDER_PATH")
    if override and Path(override).is_file():
        return override

    on_path = shutil.which("blender")
    if on_path:
        return on_path

    for candidate in _platform_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


def _blender_available() -> bool:
    return _blender_path() is not None


def _output_dir() -> Path:
    """Default landing zone for renders, exports, scratch scripts."""
    home = Path(os.getenv("FLOWLY_HOME", str(Path.home() / ".flowly")))
    out = home / "blender-out"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _open_in_default_viewer(path: str | Path) -> None:
    """Open a file with the OS default app. Best-effort, no exceptions."""
    p = str(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", p], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", p], check=False)
        elif sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
    except Exception:
        pass


# ── Subprocess runner ─────────────────────────────────────────────


async def _run_blender(
    args: list[str], timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Run Blender with the given args. Returns (returncode, stdout, stderr)."""
    blender = _blender_path()
    if not blender:
        return 127, "", "Blender executable not found. Set FLOWLY_BLENDER_PATH or install Blender."

    proc = await asyncio.create_subprocess_exec(
        blender, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Blender run exceeded {timeout:.0f}s timeout."

    return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")


async def _run_blender_with_script(
    script: str, blend_file: str = "", timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Internal: dump script to /tmp, invoke Blender, return raw rc/out/err."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    args: list[str] = ["-b"]
    if blend_file:
        args.append(blend_file)
    args.extend(["-P", tmp_path])
    try:
        return await _run_blender(args, timeout=timeout)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n…\n[truncated {len(text) - max_chars} chars]\n…\n{tail}"


# ── Error classification ──────────────────────────────────────────

_ERROR_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"SyntaxError|invalid syntax", re.IGNORECASE),
        "script_syntax",
        "Your bpy script has a Python syntax error. Check the line indicated in the traceback above.",
    ),
    (
        re.compile(r"AttributeError.*bpy\.ops\.export_mesh\.stl", re.IGNORECASE),
        "bpy_api_version",
        "bpy.ops.export_mesh.stl was removed in Blender 4.2. Use bpy.ops.wm.stl_export instead.",
    ),
    (
        re.compile(r"AttributeError.*bpy\.ops", re.IGNORECASE),
        "bpy_api_version",
        "Operator name might differ across Blender versions. Verify against the current bpy reference.",
    ),
    (
        re.compile(r"(CUDA|OPTIX|HIP|Metal|OpenCL).*(error|fail|init)", re.IGNORECASE),
        "gpu_init",
        "GPU device init failed. Retry with cpu=True or engine='BLENDER_EEVEE_NEXT' for a non-Cycles fallback.",
    ),
    (
        re.compile(r"FileNotFoundError|No such file or directory", re.IGNORECASE),
        "missing_file",
        "A path used in the script does not exist. Check ~ expansion (use Path.expanduser) and absolute vs relative paths.",
    ),
    (
        re.compile(r"out of memory|MemoryError|killed|signal 9", re.IGNORECASE),
        "memory",
        "Blender ran out of memory or was killed. Try lower resolution, fewer Cycles samples, or simpler geometry.",
    ),
    (
        re.compile(r"poll\(\)\s*failed", re.IGNORECASE),
        "operator_context",
        "An operator's poll() failed — usually nothing is selected or active. Set selection/active object before calling the operator.",
    ),
]


def _classify_error(stderr: str, stdout: str = "") -> tuple[str, str]:
    """Return (label, fix_hint) — empty hint when no pattern matched."""
    blob = stderr + "\n" + stdout
    for pattern, label, hint in _ERROR_PATTERNS:
        if pattern.search(blob):
            return (label, hint)
    return ("unknown", "")


def _format_run_response(
    rc: int, stdout: str, stderr: str, *, intent: str = "",
) -> str:
    """Build the response body. On failure, error class + hint go FIRST so
    the agent reads them before the verbose log and can react sensibly."""
    if rc == 0:
        body = ""
        if intent:
            body += f"--- {intent} ---\n"
        if stdout:
            body += f"--- stdout ---\n{_truncate(stdout)}\n"
        if stderr.strip():
            body += f"--- stderr (warnings) ---\n{_truncate(stderr, 1500)}\n"
        body += f"--- exit code: 0 ---"
        return body

    label, hint = _classify_error(stderr, stdout)
    header = f"⚠️ Blender run failed (exit {rc}, error class: {label})"
    if hint:
        header += f"\n\nFix hint: {hint}"

    body = header + "\n\n"
    if intent:
        body += f"--- {intent} ---\n"
    if stderr.strip():
        body += f"--- stderr ---\n{_truncate(stderr, 3000)}\n"
    if stdout.strip():
        body += f"--- stdout ---\n{_truncate(stdout, 1500)}\n"
    return body


# ── GPU init helper (function-based — injected into render scripts) ──
#
# Defining as a function inside the script avoids f-string conditional
# block-body issues. The render script just calls _gpu_init(force_cpu).

_GPU_INIT_FUNCTION = textwrap.dedent('''
def _gpu_init(force_cpu=False):
    """Auto-enable GPU for Cycles when available; CPU otherwise.
    Called once per render before bpy.ops.render.render(...)."""
    sc = bpy.context.scene
    if sc.render.engine != 'CYCLES':
        return
    if force_cpu:
        sc.cycles.device = 'CPU'
        print('GPU_BACKEND: CPU (forced)')
        return
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for backend in ('METAL', 'OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
            try:
                prefs.compute_device_type = backend
                prefs.refresh_devices()
                gpu_devices = [d for d in prefs.devices if d.type != 'CPU']
                if gpu_devices:
                    for d in prefs.devices:
                        d.use = (d.type != 'CPU')
                    sc.cycles.device = 'GPU'
                    print(f'GPU_BACKEND: {backend} ({len(gpu_devices)} device(s))')
                    return
            except Exception:
                continue
        sc.cycles.device = 'CPU'
        print('GPU_BACKEND: CPU (no GPU detected)')
    except Exception as _gpu_e:
        print(f'GPU_INIT_ERROR: {_gpu_e}')
''')


# ── Tool implementations ──────────────────────────────────────────


async def _tool_run(script: str, blend_file: str = "", timeout: float = 300.0) -> str:
    """Execute an arbitrary bpy script in headless mode."""
    if not script.strip():
        return "Error: script is empty."

    bf_arg = ""
    if blend_file:
        bf = str(Path(blend_file).expanduser().resolve())
        if not Path(bf).is_file():
            return f"Error: blend_file not found: {bf}"
        bf_arg = bf

    rc, out, err = await _run_blender_with_script(script, blend_file=bf_arg, timeout=timeout)
    return _format_run_response(rc, out, err, intent="blender_run")


async def _tool_scene_info(blend_file: str) -> str:
    """Introspect a .blend file."""
    bf = Path(blend_file).expanduser().resolve()
    if not bf.is_file():
        return f"Error: blend_file not found: {bf}"

    script = textwrap.dedent("""
        import bpy

        sc = bpy.context.scene
        meshes = [o.name for o in bpy.data.objects if o.type == 'MESH']
        cams = [o.name for o in bpy.data.objects if o.type == 'CAMERA']
        lights = [o.name for o in bpy.data.objects if o.type == 'LIGHT']
        materials = [m.name for m in bpy.data.materials]

        print("FLOWLY_INFO_BEGIN")
        print(f"file: {bpy.data.filepath}")
        print(f"blender_version: {bpy.app.version_string}")
        print(f"scene: {sc.name}")
        print(f"frames: {sc.frame_start}..{sc.frame_end} (current: {sc.frame_current})")
        print(f"resolution: {sc.render.resolution_x}x{sc.render.resolution_y}")
        print(f"engine: {sc.render.engine}")
        print(f"meshes: {len(meshes)}: {', '.join(meshes[:20])}")
        print(f"cameras: {len(cams)}: {', '.join(cams)}")
        print(f"lights: {len(lights)}: {', '.join(lights)}")
        print(f"materials: {len(materials)}: {', '.join(materials[:20])}")
        print("FLOWLY_INFO_END")
    """)
    rc, out, err = await _run_blender_with_script(script, blend_file=str(bf), timeout=60.0)
    return _format_run_response(rc, out, err, intent="blender_scene_info")


_PRESETS: dict[str, dict[str, Any]] = {
    # Eevee 720p — seconds, GPU rasterizer. The default for "render et"
    # / "show me" / "preview" because nobody wants to wait 10 minutes.
    # Note: 'BLENDER_EEVEE' is the engine id used by Blender 3.x AND 5.x;
    # 4.2-4.3 briefly used 'BLENDER_EEVEE_NEXT'. Our render script
    # resolves the right name at runtime so callers can pass either.
    "preview": {
        "engine": "BLENDER_EEVEE",
        "resolution": "1280x720",
        "samples": None,
    },
    # Cycles 1080p with a sane sample count — minutes, real lighting.
    "quality": {
        "engine": "CYCLES",
        "resolution": "1920x1080",
        "samples": 128,
    },
    # No overrides — use whatever the .blend has saved. Long, expensive,
    # production runs. Only when explicitly asked.
    "final": {
        "engine": "",
        "resolution": "",
        "samples": None,
    },
}


# Inserted into render scripts so engine assignment works across
# Blender 3.x / 4.x / 5.x without the caller knowing which name is
# valid in the running version.
_ENGINE_RESOLVER = textwrap.dedent('''
def _set_engine(target):
    """Set the render engine, falling back across Eevee name changes.

    Blender 3.x and 5.x use 'BLENDER_EEVEE'. Blender 4.2-4.3 use
    'BLENDER_EEVEE_NEXT'. Cycles is always 'CYCLES'. We try the
    requested name first and fall through known alternates."""
    if not target:
        return
    candidates = [target]
    if 'EEVEE' in target.upper():
        candidates += ['BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT']
    for name in candidates:
        try:
            bpy.context.scene.render.engine = name
            if name != target:
                print(f"ENGINE_RESOLVED: {target} -> {name}")
            return
        except TypeError:
            continue
    print(f"ENGINE_FAILED: {target!r} not available in this Blender version")
''')


def _parse_resolution(spec: str) -> tuple[int, int] | None:
    """Parse "1280x720" / "1920x1080" / "4K" into (w, h). None on bad input."""
    s = spec.strip().lower()
    aliases = {
        "4k": (3840, 2160), "uhd": (3840, 2160),
        "2k": (2560, 1440), "qhd": (2560, 1440),
        "1080p": (1920, 1080), "fhd": (1920, 1080),
        "720p": (1280, 720), "hd": (1280, 720),
        "480p": (854, 480), "sd": (854, 480),
    }
    if s in aliases:
        return aliases[s]
    m = re.match(r"^\s*(\d+)\s*[x×*]\s*(\d+)\s*$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


async def _tool_render(
    blend_file: str,
    output: str = "",
    frame: int | None = None,
    engine: str = "",
    samples: int | None = None,
    resolution: str = "",
    preset: str = "preview",
    cpu: bool = False,
    auto_open: bool = False,
    timeout: float = 600.0,
) -> str:
    """Render a frame from a .blend file.

    Default behaviour is `preset="preview"` — Eevee at 720p, finishing in
    seconds. Use `preset="quality"` for Cycles 1080p @ 128 samples (1-3 min)
    when the user wants a real render. `preset="final"` keeps the .blend's
    own settings — for production runs, expect 10-30 min.

    Explicit `engine` / `samples` / `resolution` parameters override the
    preset, so callers can mix-and-match (e.g. preset="quality" with
    samples=64 for a faster quality preview).

    Cycles auto-uses GPU when detected (set cpu=True to force CPU).
    Validates the produced file before reporting success.
    Pass auto_open=True to open the resulting PNG in the OS default viewer.
    """
    bf = Path(blend_file).expanduser().resolve()
    if not bf.is_file():
        return f"Error: blend_file not found: {bf}"

    # Resolve preset → fill in any unset overrides
    preset_key = (preset or "preview").lower()
    if preset_key not in _PRESETS:
        return (
            f"Error: unknown preset {preset!r}. Pick: preview / quality / final."
        )
    p = _PRESETS[preset_key]
    if not engine:
        engine = p["engine"]
    if samples is None:
        samples = p["samples"]
    if not resolution:
        resolution = p["resolution"]

    # Parse resolution if given
    res_xy: tuple[int, int] | None = None
    if resolution:
        res_xy = _parse_resolution(resolution)
        if res_xy is None:
            return (
                f"Error: invalid resolution {resolution!r}. "
                f"Use '1280x720', '1920x1080', or aliases like '720p', '1080p', '4K'."
            )

    out_path = (
        Path(output).expanduser().resolve()
        if output
        else _output_dir() / f"{bf.stem}.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    is_animation = frame is None

    if is_animation:
        # Animation: leave Blender's natural numbering. Frames land as
        # `<base>_0001.png`, `<base>_0002.png`, …
        out_no_ext = str(out_path).rsplit(".", 1)[0]
        scene_filepath = out_no_ext + "_"
        tmp_dir: Path | None = None
    else:
        # Single frame: render to a temp dir under a known base name,
        # then rename to the user's exact output path (single PNG, no
        # frame-number suffix). Blender's write_still=True behaviour is
        # to use filepath+extension verbatim, so we set base to "render".
        tmp_dir = Path(tempfile.mkdtemp(prefix="flowly-blender-"))
        scene_filepath = str(tmp_dir / "render")

    # Build script in three pieces:
    #   1. import + GPU init function definition
    #   2. engine override + GPU init invocation
    #   3. filepath setup + frame set + render call
    # Keeping these separate avoids f-string indentation surprises
    # when conditional code blocks would expand to empty.

    set_engine_line = f"_set_engine({engine!r})" if engine else ""
    set_resolution_lines = ""
    if res_xy is not None:
        set_resolution_lines = (
            f"bpy.context.scene.render.resolution_x = {res_xy[0]}\n"
            f"bpy.context.scene.render.resolution_y = {res_xy[1]}\n"
            "bpy.context.scene.render.resolution_percentage = 100\n"
        )
    set_samples_line = ""
    if samples is not None and samples > 0:
        # Cycles uses cycles.samples; both EEVEE versions use eevee.taa_render_samples
        set_samples_line = (
            "_engine_now = bpy.context.scene.render.engine\n"
            "if _engine_now == 'CYCLES':\n"
            f"    bpy.context.scene.cycles.samples = {int(samples)}\n"
            "elif _engine_now in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):\n"
            f"    bpy.context.scene.eevee.taa_render_samples = {int(samples)}\n"
        )
    frame_line = f"bpy.context.scene.frame_set({int(frame)})" if frame is not None else ""
    render_call = (
        "bpy.ops.render.render(animation=True)"
        if is_animation
        else "bpy.ops.render.render(write_still=True)"
    )
    done_message = (
        'print(f"RENDER_DONE_ANIMATION: {bpy.context.scene.frame_start}..{bpy.context.scene.frame_end}")'
        if is_animation
        else 'print(f"RENDER_DONE_FRAME: {bpy.context.scene.frame_current}")'
    )

    script = (
        "import bpy\n"
        + _ENGINE_RESOLVER
        + _GPU_INIT_FUNCTION
        + "\n"
        + (set_engine_line + "\n" if set_engine_line else "")
        + set_resolution_lines
        + set_samples_line
        + f"_gpu_init(force_cpu={cpu!r})\n"
        + f"bpy.context.scene.render.filepath = {scene_filepath!r}\n"
        + "bpy.context.scene.render.image_settings.file_format = 'PNG'\n"
        + "bpy.context.scene.render.use_file_extension = True\n"
        + (frame_line + "\n" if frame_line else "")
        + render_call + "\n"
        + done_message + "\n"
    )

    rc, out, err = await _run_blender_with_script(
        script, blend_file=str(bf), timeout=timeout,
    )

    if rc != 0:
        # Cleanup tmp dir if we made one
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return _format_run_response(rc, out, err, intent=f"render: {bf.name}")

    # Resolve actual output file(s) after render
    produced: list[Path] = []
    if is_animation:
        out_no_ext = str(out_path).rsplit(".", 1)[0]
        parent = Path(out_no_ext).parent
        prefix = Path(out_no_ext).name + "_"
        if parent.exists():
            produced = sorted(parent.glob(f"{prefix}*.png"))
    else:
        # Move the single-frame render from tmp dir to user's output path
        try:
            assert tmp_dir is not None
            tmp_render = tmp_dir / "render.png"
            if tmp_render.is_file():
                shutil.move(str(tmp_render), str(out_path))
                produced = [out_path]
        except Exception as exc:
            return (
                f"⚠️ Render reported success but produced output could not be located.\n"
                f"Tmp dir: {tmp_dir}\n"
                f"Error during move: {exc}\n\n"
                f"--- stdout ---\n{_truncate(out, 1500)}"
            )
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # Validate
    if not produced:
        return (
            f"⚠️ Render exited 0 but no output file found.\n"
            f"Expected at: {out_path}\n\n"
            f"--- stdout ---\n{_truncate(out, 1500)}\n"
            f"--- stderr ---\n{_truncate(err, 1000)}"
        )

    sizes = [(p, p.stat().st_size) for p in produced]
    empty = [p for p, sz in sizes if sz == 0]
    if empty:
        return (
            f"⚠️ Render produced empty file(s): {', '.join(str(p) for p in empty)}.\n"
            f"This usually means GPU init failed silently. Retry with cpu=True.\n\n"
            f"--- stderr ---\n{_truncate(err, 2000)}"
        )

    if auto_open and produced:
        _open_in_default_viewer(produced[0])

    # Build success summary
    total_bytes = sum(sz for _, sz in sizes)
    body = f"✓ Render complete: {bf.name}\n"
    if len(produced) == 1:
        p, sz = sizes[0]
        body += f"  Output: {p}\n  Size: {sz / 1024:.1f} KB\n"
    else:
        body += f"  Frames: {len(produced)} ({produced[0].name} … {produced[-1].name})\n"
        body += f"  Total: {total_bytes / 1024:.1f} KB\n"
    if auto_open and produced:
        body += f"  Auto-opened in default viewer.\n"

    gpu_match = re.search(r"GPU_BACKEND: (\S+)", out)
    if gpu_match:
        body += f"  Compute: {gpu_match.group(1)}\n"

    return body


async def _tool_export(
    blend_file: str,
    fmt: str,
    output: str = "",
    timeout: float = 300.0,
) -> str:
    """Export a .blend's scene to GLB / FBX / OBJ / USDZ / STL / PLY."""
    fmt = fmt.lower().lstrip(".")
    valid = {"glb", "gltf", "fbx", "obj", "usdz", "usd", "stl", "ply"}
    if fmt not in valid:
        return f"Error: unsupported format {fmt!r}. Pick one of: {', '.join(sorted(valid))}"

    bf = Path(blend_file).expanduser().resolve()
    if not bf.is_file():
        return f"Error: blend_file not found: {bf}"

    out_path = (
        Path(output).expanduser().resolve()
        if output
        else _output_dir() / f"{bf.stem}.{fmt}"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    script = textwrap.dedent(f"""
        import bpy

        out = r"{out_path}"
        fmt = "{fmt}"

        bpy.ops.object.select_all(action='DESELECT')
        for o in bpy.data.objects:
            if o.type == 'MESH':
                o.select_set(True)

        if fmt in ('glb', 'gltf'):
            bpy.ops.export_scene.gltf(
                filepath=out,
                export_format='GLB' if fmt == 'glb' else 'GLTF_SEPARATE',
                use_selection=True,
                export_apply=True,
            )
        elif fmt == 'fbx':
            bpy.ops.export_scene.fbx(filepath=out, use_selection=True, apply_unit_scale=True)
        elif fmt == 'obj':
            bpy.ops.wm.obj_export(filepath=out, export_selected_objects=True)
        elif fmt in ('usd', 'usdz'):
            bpy.ops.wm.usd_export(filepath=out, selected_objects_only=True)
        elif fmt == 'stl':
            try:
                bpy.ops.wm.stl_export(filepath=out, export_selected_objects=True)
            except AttributeError:
                bpy.ops.export_mesh.stl(filepath=out, use_selection=True)
        elif fmt == 'ply':
            bpy.ops.wm.ply_export(filepath=out, export_selected_objects=True)

        print(f"EXPORT_OK: {{out}}")
    """)

    rc, out, err = await _run_blender_with_script(
        script, blend_file=str(bf), timeout=timeout,
    )
    if rc != 0:
        return _format_run_response(rc, out, err, intent=f"export: {bf.name} → {fmt}")

    if not out_path.is_file() or out_path.stat().st_size == 0:
        return (
            f"⚠️ Export exited 0 but file is missing or empty: {out_path}\n\n"
            f"--- stderr ---\n{_truncate(err, 2000)}"
        )

    sz = out_path.stat().st_size
    return f"✓ Export complete: {out_path}\n  Size: {sz / 1024:.1f} KB ({fmt.upper()})"


async def _tool_create(description: str, output: str = "") -> str:
    """High-level scaffold guidance for scene creation."""
    out_path = (
        Path(output).expanduser().resolve()
        if output
        else _output_dir() / "scene.blend"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return (
        f"To create the scene, write a bpy script that builds the geometry described "
        f"({description!r}), then ends with:\n\n"
        f"    bpy.ops.wm.save_as_mainfile(filepath=r\"{out_path}\")\n\n"
        f"Run it via blender_run(script=<your-script>). Default output landing zone "
        f"is {_output_dir()} — use it unless the user specified otherwise. After "
        f"saving, optionally call blender_render(blend_file=..., auto_open=True) "
        f"to render and show a preview."
    )


# ── pre_llm_call orientation hook ─────────────────────────────────


_BLENDER_KEYWORDS = re.compile(
    r"\b(blender|render|3d|3-d|\.blend\b|mesh|geometry|model(?:ing|ling)?|"
    r"shader|material|uv|texture|cycles|eevee|gltf|glb|fbx|obj|usdz|stl|"
    r"polygon|polycount|low-?poly|primitive|keyframe|animat(?:e|ion)|"
    r"sahne|render\s*et|3 ?b?oyutlu)\b",
    re.IGNORECASE,
)

_ORIENTATION = (
    "Blender plugin is loaded. For ANY 3D / Blender / render / mesh / model / "
    "shader / GLB / FBX / OBJ work, use ONLY these tools:\n"
    "  - blender_run, blender_scene_info, blender_render, blender_export, blender_create.\n\n"
    "Render with auto_open=True to open the PNG in the default viewer for the user. "
    "DO NOT use 'computer' (activate_app, see, screenshot) or 'exec' (open *.blend, "
    "pkill Blender) to interact with Blender. The plugin runs HEADLESS — there is "
    "no GUI to inspect. To show the user a result: pass the file path in your reply, "
    "or pass auto_open=True so the OS opens it for them.\n\n"
    "If you need bpy patterns, load skill 'blender:scripting' via skill_view first."
)


def _orient_for_blender(hook_ctx: Any) -> str | None:
    """Inject orientation when the user's last message looks 3D-related."""
    try:
        text = (getattr(hook_ctx, "user_message", "") or "")
        if not isinstance(text, str) or not text.strip():
            return None
        if _BLENDER_KEYWORDS.search(text):
            return _ORIENTATION
    except Exception:
        pass
    return None


# ── Slash command ─────────────────────────────────────────────────


def _slash_handler(args: str) -> str:
    """`/blender` — show status + quick reference."""
    bp = _blender_path()
    if not bp:
        return (
            "**Blender not found**\n\n"
            "Install Blender from <https://www.blender.org/download/> or set "
            "`FLOWLY_BLENDER_PATH` in `~/.flowly/.env` if you have it elsewhere.\n\n"
            "Searched:\n"
            "- `FLOWLY_BLENDER_PATH` env\n"
            "- `which blender`\n"
            "- macOS `/Applications/Blender.app/...`\n"
            "- Linux `/opt/blender`, `/usr/local/bin`\n"
            "- Windows `Program Files\\Blender Foundation\\...`"
        )

    out_dir = _output_dir()
    return (
        f"**Blender ready** (v0.2.1)\n\n"
        f"- Path: `{bp}`\n"
        f"- Output dir: `{out_dir}`\n"
        f"- GPU auto-detect: Metal / CUDA / OPTIX / HIP / oneAPI\n"
        f"- Render presets: `preview` (Eevee 720p, sec) · `quality` "
        f"(Cycles 1080p@128, ~min) · `final` (.blend defaults)\n"
        f"- pre_llm_call orientation: on\n\n"
        f"**Tools**: `blender_run`, `blender_scene_info`, `blender_render` "
        f"(preset/auto_open), `blender_export`, `blender_create`.\n\n"
        f"Try: *\"Render `path/to/scene.blend` and show me\"* (uses preview) "
        f"or *\"high-quality render of …\"* (uses quality preset)."
    )


# ── Plugin registration ───────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point."""

    ctx.register_tool(
        name="blender_run",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "Python script using the bpy API."},
                    "blend_file": {
                        "type": "string",
                        "description": "Optional path to a .blend to open before running the script.",
                    },
                    "timeout": {"type": "number", "description": "Seconds before kill. Default 300."},
                },
                "required": ["script"],
            },
        },
        handler=_tool_run,
        check_fn=_blender_available,
        description=(
            "Run an arbitrary bpy (Blender Python API) script in headless mode. "
            "Optionally opens an existing .blend first. Use this for scene generation, "
            "asset processing, or anything the higher-level blender_* tools don't cover. "
            "Load skill 'blender:scripting' first for non-trivial scripts."
        ),
    )

    ctx.register_tool(
        name="blender_scene_info",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "blend_file": {"type": "string", "description": "Path to a .blend file."},
                },
                "required": ["blend_file"],
            },
        },
        handler=_tool_scene_info,
        check_fn=_blender_available,
        description=(
            "Inspect a .blend: lists meshes, cameras, lights, materials, render engine, "
            "resolution, frame range. Use BEFORE rendering or exporting to know contents."
        ),
    )

    ctx.register_tool(
        name="blender_render",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "blend_file": {"type": "string", "description": "Path to .blend."},
                    "output": {
                        "type": "string",
                        "description": "Output PNG path. Defaults to ~/.flowly/blender-out/<name>.png.",
                    },
                    "frame": {
                        "type": "integer",
                        "description": "Specific frame number. Omit to render the full timeline animation.",
                    },
                    "preset": {
                        "type": "string",
                        "enum": ["preview", "quality", "final"],
                        "description": (
                            "Render preset (default 'preview'). 'preview' = Eevee 720p, "
                            "seconds. 'quality' = Cycles 1080p @ 128 samples, 1-3 min. "
                            "'final' = use the .blend's own settings, may take 10-30 min. "
                            "Use preview for casual 'render et' / 'show me' requests; "
                            "only escalate to quality/final on explicit user request."
                        ),
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"],
                        "description": "Engine override. Empty inherits from preset / .blend.",
                    },
                    "samples": {
                        "type": "integer",
                        "description": (
                            "Sample count override. Cycles uses cycles.samples; "
                            "Eevee uses eevee.taa_render_samples. Empty = use preset / .blend default."
                        ),
                    },
                    "resolution": {
                        "type": "string",
                        "description": (
                            "Resolution override. '1280x720', '1920x1080', '4K', '720p', '1080p', etc. "
                            "Empty = use preset / .blend default."
                        ),
                    },
                    "cpu": {
                        "type": "boolean",
                        "description": "Force CPU rendering. Default False (auto-uses GPU when available).",
                    },
                    "auto_open": {
                        "type": "boolean",
                        "description": "Open the produced PNG in OS default viewer when done. Set TRUE when showing the user a result.",
                    },
                    "timeout": {"type": "number", "description": "Seconds. Default 600."},
                },
                "required": ["blend_file"],
            },
        },
        handler=_tool_render,
        check_fn=_blender_available,
        description=(
            "Render a .blend to PNG. DEFAULT preset is 'preview' (Eevee 720p, seconds) — "
            "use this for 'render et' / 'show me' requests. Use preset='quality' "
            "(Cycles 1080p @ 128 samples) when the user wants a real render. "
            "preset='final' uses the .blend's own settings — 10-30 min; only on "
            "explicit user request. Auto-detects GPU. Pass auto_open=True to open "
            "the PNG in the OS default viewer — the ONLY correct way to 'show' a "
            "render; do NOT use computer or exec tools."
        ),
    )

    ctx.register_tool(
        name="blender_export",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "blend_file": {"type": "string", "description": "Path to .blend."},
                    "fmt": {
                        "type": "string",
                        "enum": ["glb", "gltf", "fbx", "obj", "usdz", "usd", "stl", "ply"],
                    },
                    "output": {"type": "string", "description": "Output path. Default ~/.flowly/blender-out/<name>.<fmt>."},
                },
                "required": ["blend_file", "fmt"],
            },
        },
        handler=_tool_export,
        check_fn=_blender_available,
        description=(
            "Export the scene's meshes to a 3D interchange format. Selects all meshes, "
            "applies transforms, writes the file. Validates output exists + non-empty."
        ),
    )

    ctx.register_tool(
        name="blender_create",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to build, plain language."},
                    "output": {"type": "string", "description": "Where to save the .blend."},
                },
                "required": ["description"],
            },
        },
        handler=_tool_create,
        check_fn=_blender_available,
        description=(
            "Plan a scene-creation flow: returns the scaffold the agent should follow. "
            "After scaffolding, write the bpy script and call blender_run."
        ),
    )

    ctx.register_command(
        "blender",
        handler=_slash_handler,
        description="Show Blender plugin status and tool reference.",
    )

    ctx.register_skill(
        name="scripting",
        path=Path(__file__).parent / "skills" / "scripting" / "SKILL.md",
        description=(
            "Load when writing Blender Python (bpy) scripts. Patterns for primitives, "
            "materials, lights, render settings, animation, export, common errors, and "
            "the DO NOT rules for tool selection."
        ),
    )

    # Fires only when user message has 3D keywords — saves tokens otherwise
    ctx.register_hook("pre_llm_call", _orient_for_blender)
