"""hello — a minimal Flowly plugin used as a smoke test for the
marketplace auto-sync pipeline. Registers a single ``/hello`` slash
command and nothing else.

Safe to keep around, safe to delete once the pipeline is verified.
"""

from __future__ import annotations


def _slash_handler(args: str) -> str:
    target = args.strip() or "world"
    return f"Hello, {target}! — flowly plugin pipeline is alive."


def register(ctx) -> None:
    ctx.register_command(
        "hello",
        handler=_slash_handler,
        description="Echo back a greeting. Used to verify the plugin install + marketplace sync pipeline.",
    )
