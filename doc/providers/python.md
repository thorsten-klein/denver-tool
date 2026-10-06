# python provider

A `python` stage calls a function from a Python file. It runs inside
denver's own Python process — no subprocess. So you can set a breakpoint in
your function and debug it together with denver.

```toml
[my-stage]
provider = "python"
file = "setup/tools.py"
function = "install"
args = { version = "1.2.3" }
```

```python
# setup/tools.py
def install(ctx, version):
    ctx.run(["echo", f"installing {version}"])
    ctx.set("TOOL_VERSION", version)
```

(`provider:`/`description:`/`disabled:`/`depends-on:`/`scripts:`/`env:`/`env-prepend:`/`env-append:` are generic keys every stage has —
see "Generic stage keys" in [Configuration](../configuration/config-file.md).)

## Key reference

- **`file`** (**required**) — the `.py` file. A relative path is resolved
  against the env dir, then against imported base envs.
- **`function`** (**required**) — name of a top-level function in `file`.
- **`args`** — a mapping. Each entry is passed as a keyword argument.
  `${VAR}` values are interpolated first.

denver calls `function(ctx, **args)`. `ctx` is denver's `Context`. Useful parts:

- `ctx.env` — the environment for later stages and the final command.
  Change it with `ctx.set()`, `ctx.prepend_path()`, `ctx.source()`.
- `ctx.run([...])` — run a command. Honours `--dry-run`.
- `ctx.resolve_path(...)`, `ctx.env_dir` — find files.
- `ctx.fast`, `ctx.dry_run` — the `--fast`/`--dry-run` flags.
- `die("message")` (from `denver_providers`) — stop with a clean error.

The return value is ignored.

## Behaviour

- **`--fast` and `--dry-run`**: the function is always called. denver
  cannot know what your function does, so the function checks `ctx.fast` and
  `ctx.dry_run` itself and skips its slow or risky parts.
- **Errors**: an exception stops denver with the message and the traceback.
  A failed `ctx.run()` is reported like in any other provider.
- **Imports**: the file's directory is added to `sys.path`, so the file can
  import modules next to it. The file is imported once per run; stages using
  the same file share it.
- **Docker**: when a `docker` stage relocates the run, the function runs
  inside the container, like every other setup stage after it.

## Debugging

Start denver with a debugger and set a breakpoint in your function, for
example:

```bash
python -m pdb -m denver run ...
python -m debugpy --listen 5678 --wait-for-client -m denver run ...   # VS Code: attach to port 5678
```

Or put `breakpoint()` in the function. This needs denver installed as a
Python package (e.g. `uv tool install` or a venv), not the single-file binary.

## python, custom or an extension provider?

- `custom` — a shell command or a sourced script.
- `python` — one function, in Python, easy to debug.
- [Extension provider](../configuration/config-file.md) — a full
  `Provider` class with its own keys, defaults and `wrap()`.
