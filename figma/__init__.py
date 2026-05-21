"""Flowly Figma plugin — read, search, export, and comment on Figma
files via the public REST API.

The Figma REST API is read-only at the file-content level (the write
API only exists inside Figma's in-app plugin runtime, which we can't
reach from here). What we *can* do covers most "agent reads a design"
workflows: file structure, image exports, comments, design tokens.

All endpoints require a personal access token. Users put it in
``~/.flowly/.env`` as ``FIGMA_ACCESS_TOKEN=…`` — generated at
https://www.figma.com/developers/api#access-tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

try:
    from flowly.profile import get_flowly_home  # type: ignore
except Exception:  # pragma: no cover — older Flowly versions
    def get_flowly_home() -> Path:
        return Path.home() / ".flowly"


# ── Constants ──────────────────────────────────────────────────────────

_API_BASE = "https://api.figma.com/v1"
_TOKEN_ENV = "FIGMA_ACCESS_TOKEN"
_DEFAULT_TIMEOUT = 30.0
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF = (1.0, 3.0)  # seconds between retries
_MAX_RESPONSE_CHARS = 8000  # truncate large JSON tool results

# Figma node IDs come back in two flavours depending on the endpoint:
#   - ":" form  (``1:2`` — what the API and most file URLs return)
#   - "-" form  (``1-2`` — what some web URLs / query-string node-id="…"
#               use; Figma's URL builder URL-encodes the colon)
# We normalise to ":" before talking to the API.
_NODE_ID_DASH_RE = re.compile(r"^(\d+)-(\d+)$")

# Figma file URL patterns:
#   https://www.figma.com/file/<key>/<title>
#   https://www.figma.com/design/<key>/<title>
#   https://www.figma.com/proto/<key>/<title>
_FILE_URL_RE = re.compile(
    r"figma\.com/(?:file|design|proto)/([A-Za-z0-9]{18,32})(?:/|\?|$)"
)


# ── Helpers ────────────────────────────────────────────────────────────


def _token() -> str | None:
    """Pick up the user's PAT from env. We *don't* cache because the
    user might rotate it without restarting the gateway."""
    return os.environ.get(_TOKEN_ENV)


def _figma_available() -> bool:
    """``check_fn`` for tool registration. Tools degrade to a clear
    error string rather than disappearing when the token is missing,
    so the model can ask the user to set it up."""
    return bool(_token())


def _parse_file_key(ref: str) -> str:
    """Accept either a raw file key (``ABC123…``) or a full Figma URL
    and return the bare key. We're generous on inputs because users
    paste both forms from the address bar interchangeably."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("File reference is empty.")
    # Already a bare key?
    if re.fullmatch(r"[A-Za-z0-9]{18,32}", ref):
        return ref
    match = _FILE_URL_RE.search(ref)
    if not match:
        raise ValueError(
            f"Could not extract a Figma file key from {ref!r}. Paste a "
            "URL like https://www.figma.com/file/ABC123/Title or just "
            "the key (ABC123)."
        )
    return match.group(1)


def _normalise_node_id(node_id: str | None) -> str | None:
    """Convert ``1-2`` to ``1:2`` so the API recognises it. Pass-through
    for already-colon IDs and falsy values."""
    if not node_id:
        return None
    node_id = node_id.strip()
    match = _NODE_ID_DASH_RE.match(node_id)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return node_id


def _output_dir() -> Path:
    """Standard landing zone for exports — same convention the Blender
    plugin uses so the docs / agent skill can refer to a single place."""
    out = get_flowly_home() / "figma-out"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _truncate(text: str, limit: int = _MAX_RESPONSE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return (
        text[: limit - 80]
        + f"\n\n[…truncated, {len(text) - limit + 80} chars omitted; "
        "ask for a specific node_id to drill down…]"
    )


def _format_error(resp: httpx.Response) -> str:
    """Translate Figma's API errors into something the agent can act on
    without parsing nested JSON."""
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    err = payload.get("err") or payload.get("error") or resp.text[:200]
    if resp.status_code == 401:
        return (
            "Error: Figma rejected the token (401). The PAT is missing, "
            "expired, or doesn't have the right scopes. Generate a new "
            "one at https://www.figma.com/developers/api#access-tokens "
            f"and set FIGMA_ACCESS_TOKEN in ~/.flowly/.env. ({err})"
        )
    if resp.status_code == 403:
        return (
            "Error: Figma denied access (403). The token doesn't have "
            f"permission for this resource. ({err})"
        )
    if resp.status_code == 404:
        return (
            "Error: Figma returned 404 — file/node not found, or the "
            f"token can't see it. ({err})"
        )
    if resp.status_code == 429:
        return (
            "Error: Figma rate limit hit (429). Wait a minute and "
            f"retry, or batch fewer node IDs per call. ({err})"
        )
    return f"Error: Figma API {resp.status_code}: {err}"


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[bool, Any]:
    """Single HTTP request with built-in 429 backoff. Returns
    ``(success, payload_or_error_str)``. Auth is read at call time."""
    token = _token()
    if not token:
        return False, (
            "Error: FIGMA_ACCESS_TOKEN is not set. Add it to "
            "~/.flowly/.env (generate at "
            "https://www.figma.com/developers/api#access-tokens)."
        )

    headers = {"X-Figma-Token": token, "Accept": "application/json"}
    url = f"{_API_BASE}{path}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                return False, f"Error: network failure talking to Figma: {exc}"

            if resp.status_code == 429 and attempt < _RATE_LIMIT_RETRIES:
                await asyncio.sleep(_RATE_LIMIT_BACKOFF[attempt])
                continue
            if resp.is_success:
                try:
                    return True, resp.json()
                except Exception:
                    return True, resp.text
            return False, _format_error(resp)

    return False, "Error: exhausted rate-limit retries."


# ── Node tree summarisation ────────────────────────────────────────────


def _summarise_node(node: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Project a Figma node down to the fields an agent actually uses:
    id, name, type, plus children + characters. The raw node objects
    are huge (per-property style refs, layout grids, fills…) and dump
    most of that into context for no benefit."""
    out: dict[str, Any] = {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
    }
    if "characters" in node:
        # Text node payload — cap at ~400 chars so a "specs" page
        # doesn't pollute the tool result.
        chars = str(node.get("characters") or "")
        out["text"] = chars[:400] + ("…" if len(chars) > 400 else "")
    if "absoluteBoundingBox" in node:
        box = node["absoluteBoundingBox"] or {}
        if box:
            out["box"] = {
                "x": box.get("x"),
                "y": box.get("y"),
                "w": box.get("width"),
                "h": box.get("height"),
            }
    if depth < 4 and "children" in node:
        out["children"] = [
            _summarise_node(child, depth + 1)
            for child in node.get("children", [])
        ]
    return out


def _walk_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []) or []:
        yield from _walk_nodes(child)


# ── Tools ──────────────────────────────────────────────────────────────


async def _tool_file_info(file: str) -> str:
    """High-level snapshot of a file — enough to orient the agent
    before it decides whether to drill into a specific page/frame."""
    try:
        key = _parse_file_key(file)
    except ValueError as exc:
        return f"Error: {exc}"

    ok, payload = await _request("GET", f"/files/{key}", params={"depth": 1})
    if not ok:
        return str(payload)

    doc = payload.get("document", {}) or {}
    pages = doc.get("children", []) or []
    summary = {
        "key": key,
        "name": payload.get("name"),
        "lastModified": payload.get("lastModified"),
        "version": payload.get("version"),
        "thumbnailUrl": payload.get("thumbnailUrl"),
        "role": payload.get("role"),
        "pages": [
            {"id": p.get("id"), "name": p.get("name"), "type": p.get("type")}
            for p in pages
        ],
    }
    return json.dumps(summary, indent=2, ensure_ascii=False)


async def _tool_read(file: str, node_id: str = "", depth: int = 3) -> str:
    """Read part of a file's node tree. With ``node_id`` empty we
    return the top-level pages (depth-truncated). With ``node_id``
    set we pull just that subtree via the dedicated ``/nodes``
    endpoint, which is cheaper than fetching the whole file."""
    try:
        key = _parse_file_key(file)
    except ValueError as exc:
        return f"Error: {exc}"

    requested_depth = max(1, min(int(depth or 3), 6))
    norm = _normalise_node_id(node_id)
    if norm:
        ok, payload = await _request(
            "GET", f"/files/{key}/nodes", params={"ids": norm, "depth": requested_depth}
        )
        if not ok:
            return str(payload)
        node_block = (payload.get("nodes") or {}).get(norm, {}) or {}
        document = node_block.get("document") or {}
        if not document:
            return f"Error: node {norm!r} not found in file {key}."
        return _truncate(
            json.dumps(_summarise_node(document), indent=2, ensure_ascii=False)
        )

    ok, payload = await _request(
        "GET", f"/files/{key}", params={"depth": requested_depth}
    )
    if not ok:
        return str(payload)
    document = payload.get("document") or {}
    return _truncate(
        json.dumps(_summarise_node(document), indent=2, ensure_ascii=False)
    )


async def _tool_export(
    file: str,
    node_id: str,
    fmt: str = "png",
    scale: float = 2.0,
    output: str = "",
) -> str:
    """Export one or more node ids as images. Figma returns a signed
    S3-style URL we download to ``~/.flowly/figma-out/``."""
    try:
        key = _parse_file_key(file)
    except ValueError as exc:
        return f"Error: {exc}"

    ids = [
        _normalise_node_id(part.strip())
        for part in node_id.split(",")
        if part.strip()
    ]
    ids = [i for i in ids if i]
    if not ids:
        return "Error: at least one node_id is required (comma-separated for multiple)."

    fmt_lower = (fmt or "png").lower()
    if fmt_lower not in {"png", "jpg", "svg", "pdf"}:
        return f"Error: unsupported format {fmt!r}. Use png, jpg, svg, or pdf."

    # Scale only applies to raster outputs; Figma rejects it for svg/pdf.
    params: dict[str, Any] = {"ids": ",".join(ids), "format": fmt_lower}
    if fmt_lower in {"png", "jpg"}:
        try:
            scale_value = float(scale or 2.0)
        except (TypeError, ValueError):
            return "Error: scale must be a number."
        if scale_value < 0.01 or scale_value > 4.0:
            return "Error: scale must be between 0.01 and 4.0."
        params["scale"] = scale_value

    ok, payload = await _request("GET", f"/images/{key}", params=params)
    if not ok:
        return str(payload)

    images = payload.get("images") or {}
    out_dir = Path(output).expanduser() if output else _output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.is_file():
        return f"Error: output path {out_dir} is a file, expected a directory."

    written: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for node, url in images.items():
            if not url:
                written.append({"nodeId": node, "error": "render returned no URL"})
                continue
            try:
                r = await client.get(url)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                written.append({"nodeId": node, "error": f"download failed: {exc}"})
                continue
            safe_node = node.replace(":", "-")
            file_path = out_dir / f"{key}_{safe_node}.{fmt_lower}"
            file_path.write_bytes(r.content)
            written.append(
                {
                    "nodeId": node,
                    "path": str(file_path),
                    "bytes": str(len(r.content)),
                }
            )

    return json.dumps(
        {"file": key, "format": fmt_lower, "exports": written},
        indent=2,
        ensure_ascii=False,
    )


async def _tool_search(file: str, query: str, limit: int = 20) -> str:
    """Search every text/frame/component name + text content in the
    file for ``query`` (case-insensitive). Cheap client-side — Figma
    doesn't have a server-side search endpoint."""
    try:
        key = _parse_file_key(file)
    except ValueError as exc:
        return f"Error: {exc}"
    q = (query or "").strip().lower()
    if not q:
        return "Error: query is required."

    ok, payload = await _request(
        "GET", f"/files/{key}", params={"depth": 6}
    )
    if not ok:
        return str(payload)

    document = payload.get("document") or {}
    matches: list[dict[str, Any]] = []
    limit_n = max(1, min(int(limit or 20), 100))

    for node in _walk_nodes(document):
        if len(matches) >= limit_n:
            break
        name = (node.get("name") or "").lower()
        chars = (node.get("characters") or "")
        chars_l = chars.lower()
        if q in name or q in chars_l:
            matches.append(
                {
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "type": node.get("type"),
                    "matchIn": "text" if q in chars_l else "name",
                    "text": (chars[:200] + "…") if len(chars) > 200 else chars,
                }
            )

    return json.dumps(
        {"file": key, "query": query, "count": len(matches), "results": matches},
        indent=2,
        ensure_ascii=False,
    )


async def _tool_comment_add(
    file: str,
    body: str,
    node_id: str = "",
    x: float | None = None,
    y: float | None = None,
) -> str:
    """Post a comment on a file. With ``node_id`` the comment pins to
    that node (default anchor: top-left + small offset). With ``x``/
    ``y`` you pin to absolute canvas coordinates instead. Plain
    ``body`` with neither becomes a file-level comment."""
    try:
        key = _parse_file_key(file)
    except ValueError as exc:
        return f"Error: {exc}"
    if not (body or "").strip():
        return "Error: comment body is required."

    payload: dict[str, Any] = {"message": body.strip()}
    norm = _normalise_node_id(node_id)
    if norm:
        # Figma accepts an anchor frame_offset relative to the node;
        # 0,0 puts the pin at the top-left corner of the node, which is
        # almost always what the user wants when they say "comment on
        # this frame".
        payload["client_meta"] = {
            "node_id": norm,
            "node_offset": {"x": 0, "y": 0},
        }
    elif x is not None and y is not None:
        payload["client_meta"] = {"x": float(x), "y": float(y)}

    ok, result = await _request(
        "POST", f"/files/{key}/comments", json_body=payload
    )
    if not ok:
        return str(result)
    return json.dumps(
        {
            "file": key,
            "commentId": result.get("id"),
            "url": result.get("file_key"),
            "message": result.get("message"),
            "createdAt": result.get("created_at"),
        },
        indent=2,
        ensure_ascii=False,
    )


# ── Slash command ──────────────────────────────────────────────────────


def _slash_handler(args: str) -> str:
    """``/figma`` status + quick tool reference."""
    token = _token()
    if not token:
        return (
            "Figma plugin loaded, but FIGMA_ACCESS_TOKEN is **not set**.\n\n"
            "1. Generate a personal access token at "
            "https://www.figma.com/developers/api#access-tokens (Personal "
            "access tokens → Generate new token; scopes: file content + "
            "comments).\n"
            "2. Add `FIGMA_ACCESS_TOKEN=…` to `~/.flowly/.env`.\n"
            "3. Restart Flowly so the env var is picked up.\n\n"
            "Once the token is in place: paste a Figma URL in chat and I "
            "can read the file, export frames, search content, and post "
            "comments."
        )
    masked = f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "set"
    out_dir = _output_dir()
    return (
        f"Figma plugin ready. Token: {masked}. Exports land in `{out_dir}`.\n\n"
        "Tools:\n"
        "- `figma_file_info(file)` — file overview + page list\n"
        "- `figma_read(file, node_id?, depth?)` — node tree summary\n"
        "- `figma_export(file, node_id, fmt?, scale?, output?)` — PNG/SVG/JPG/PDF\n"
        "- `figma_search(file, query, limit?)` — match names + text\n"
        "- `figma_comment_add(file, body, node_id? | x?, y?)` — post a comment"
    )


# ── Registration ───────────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — wires tools, slash command, and skill."""

    ctx.register_tool(
        name="figma_file_info",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Figma file URL or bare key.",
                    },
                },
                "required": ["file"],
            },
        },
        handler=_tool_file_info,
        check_fn=_figma_available,
        description=(
            "Get a high-level snapshot of a Figma file: name, last "
            "modified time, version, plus a list of top-level pages "
            "(id/name). Use this BEFORE figma_read on an unfamiliar "
            "file so you know which page/frame to drill into."
        ),
    )

    ctx.register_tool(
        name="figma_read",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Figma file URL or bare key.",
                    },
                    "node_id": {
                        "type": "string",
                        "description": (
                            "Optional. Single node id (e.g. '1:2' or '1-2'). "
                            "Empty returns the file's top-level node tree."
                        ),
                    },
                    "depth": {
                        "type": "integer",
                        "description": "How deep to traverse (1-6, default 3).",
                    },
                },
                "required": ["file"],
            },
        },
        handler=_tool_read,
        check_fn=_figma_available,
        description=(
            "Read a Figma file's node tree as structured JSON: ids, "
            "names, types, text content, bounding boxes. Pass node_id "
            "to drill into a specific frame/component. Depth caps at 6 "
            "to keep responses sane; if you need deeper, paginate by "
            "node_id."
        ),
    )

    ctx.register_tool(
        name="figma_export",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Figma file URL or bare key.",
                    },
                    "node_id": {
                        "type": "string",
                        "description": (
                            "One or more node ids, comma-separated (e.g. "
                            "'1:2,3:4'). Accepts ':' or '-' separators."
                        ),
                    },
                    "fmt": {
                        "type": "string",
                        "enum": ["png", "jpg", "svg", "pdf"],
                        "description": "Output format. Default png.",
                    },
                    "scale": {
                        "type": "number",
                        "description": (
                            "Raster scale (PNG/JPG only). 0.01–4.0, "
                            "default 2.0. Ignored for svg/pdf."
                        ),
                    },
                    "output": {
                        "type": "string",
                        "description": (
                            "Output directory. Default ~/.flowly/figma-out/."
                        ),
                    },
                },
                "required": ["file", "node_id"],
            },
        },
        handler=_tool_export,
        check_fn=_figma_available,
        description=(
            "Export one or more Figma nodes (frames, groups, "
            "components) as PNG/JPG/SVG/PDF. Files land in "
            "~/.flowly/figma-out/ by default. Returns the saved paths "
            "+ byte counts; use a file viewer / Markdown image link to "
            "show the user."
        ),
    )

    ctx.register_tool(
        name="figma_search",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Figma file URL or bare key.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring to match.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max hits to return (1-100, default 20).",
                    },
                },
                "required": ["file", "query"],
            },
        },
        handler=_tool_search,
        check_fn=_figma_available,
        description=(
            "Search a Figma file for nodes whose name OR text content "
            "contains a substring. Useful to locate specific frames or "
            "find every place a piece of copy is used. Returns id, "
            "name, type, and the matching text snippet for each hit."
        ),
    )

    ctx.register_tool(
        name="figma_comment_add",
        schema={
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Figma file URL or bare key.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Comment text (plain string).",
                    },
                    "node_id": {
                        "type": "string",
                        "description": (
                            "Optional. Pin the comment to this node. "
                            "Mutually exclusive with x/y."
                        ),
                    },
                    "x": {
                        "type": "number",
                        "description": (
                            "Optional. Absolute canvas X coordinate "
                            "(used with y for free-floating pins)."
                        ),
                    },
                    "y": {
                        "type": "number",
                        "description": "Absolute canvas Y coordinate.",
                    },
                },
                "required": ["file", "body"],
            },
        },
        handler=_tool_comment_add,
        check_fn=_figma_available,
        description=(
            "Post a comment on a Figma file. Pin to a node with "
            "node_id, or to canvas coordinates with x/y, or omit both "
            "for a file-level comment. Requires a token whose owner "
            "can edit the file. Returns the new comment id."
        ),
    )

    ctx.register_command(
        "figma",
        handler=_slash_handler,
        description="Show Figma plugin status, token state, and tool reference.",
    )

    ctx.register_skill(
        name="design-handoff",
        path=Path(__file__).parent / "skills" / "design-handoff" / "SKILL.md",
        description=(
            "Load when working from a Figma file — design-to-code "
            "patterns, exporting frames as assets, pulling text and "
            "spec details, posting structured review comments."
        ),
    )
