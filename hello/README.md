# hello

Minimal sanity-check plugin. Registers `/hello` and nothing else.

## Install

```bash
flowly plugins install Nocetic/plugins/hello
flowly plugins enable hello
flowly service restart
```

## Use

```
/hello              → Hello, world! — flowly plugin pipeline is alive.
/hello flowly       → Hello, flowly! — flowly plugin pipeline is alive.
```

## Why it exists

This plugin proves the monorepo → registry.json → marketplace
pipeline works end-to-end. If you can install it via the install
command above AND see it on `useflowlyapp.com/plugins`, the auto-sync
chain is healthy.

Feel free to delete it from the monorepo once that's confirmed —
nothing else depends on it.
