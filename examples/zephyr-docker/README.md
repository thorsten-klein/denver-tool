# examples/zephyr-docker

**A single `docker` stage: build a Compose service and run everything inside
it. The container half of the Zephyr setup, kept in an env of its own.**

## What it does

`denver run examples/zephyr-docker` builds the `dev` service from
`docker-compose.yml` and drops you into a `fish` shell inside the resulting
container — as your own host UID/GID, with your workspace, home directory and
caches mounted at *the same absolute paths* they have outside.

That last detail is the point of most of the configuration here. If
`/home/you/project` is `/home/you/project` on both sides, then absolute paths
in build outputs, compile databases, IDE configuration and ccache entries stay
valid whether they were produced inside or outside the container.

## Why it exists

**Because some problems can't be fixed from inside a virtualenv.** A `uv` or
`conan` stage can pin a package or a toolchain; neither can give you a
specific glibc, a system library, or a distribution that a vendor tool refuses
to run without. That is the whole job of a `docker` stage.

**Because it is a wrapper, not an installer.** `docker` doesn't install a
toolchain, a compiler or a package — it *relocates the rest of the pipeline*.
Everything listed after `docker` in a `stages:` list runs inside the
container. Which is also why it can be removed: `--skip docker` runs the very
same stack directly on the host, and no other stage needs to know.

**Because it is separable.** This env exists apart from
[`../zephyr-devshell`](../zephyr-devshell) so that "how do we build and enter
the container" is one reusable answer, imported wherever it's needed rather
than copied. The devshell inherits this whole section with a
*section-level* import:

```toml
# ../zephyr-devshell/denver.toml
[docker]
import = ["../zephyr-docker"]  # inherit this env's docker: config, default-cmd included
```

## Purpose as an example

**1. `hooks: pre-docker:` — computing what a static file can't express.**
Compose needs an `.env` file, and half its contents can only be known at
runtime: the host UID/GID, the workspace root, cache locations, git
credentials, whether this is CI. So `denver.toml` names a script instead of
values, under the generic `[hooks]` mechanism rather than anything
docker-specific:

```toml
[hooks]
pre-docker = ["create-env.sh"]     # denver sources it (no arguments) right before the 'docker' stage's setup()
```

`create-env.sh` renders the whole env-file itself — including walking up the
directory tree to find the outermost `.git` as the workspace root, and asking
`docker compose config` for the image tag so `docker-compose.yml` stays the
single source of truth for it.
It also passes the X11 cookie (`$XAUTHORITY`, or `~/.Xauthority`) to the
container, so GUI apps can reach the display.

**2. `scripts: setup:` — the things a container genuinely cannot do.** You
cannot install Docker from inside Docker, and udev rules belong to the host
kernel. Those live in a named script list that is **not** run on every start:

```bash
denver run examples/zephyr-docker --scripts setup    # once per machine
```

`--scripts <name>` is open-ended, not a fixed set of flags — an env can declare
`scripts: migrate:` and get `--scripts migrate` without denver changing.

**3. What a real `docker-compose.yml` ends up carrying.** It is heavily
commented and worth skimming as a catalogue of the problems that show up once
a container is a *development* environment rather than a deployment target:
caches kept outside the container lifecycle, per-tool state (IDE plugins,
accepted EULAs, shell history, AI-assistant credentials) surviving a rebuild,
`/dev` passthrough for flashing hardware, and the git-credentials file that
has to be *copied* in because git rewrites it in place rather than editing it
— which a bind mount cannot satisfy.

**4. The host's docker daemon, from inside the container.** The image has the
docker CLI and compose plugin, so you can build or run containers, run a
nested denver env with a `docker` stage, or use the VS Code Docker extension
from inside it. Mounting the host's `/var/run/docker.sock` alone isn't enough:
it belongs to `root:docker` with the *host's* `docker` GID, and the container
user (your host UID/GID) has no group with that GID, so it gets
`permission denied`.

So `docker-compose.yml` mounts it as `/var/run/docker-host.sock` instead, and
the image's entrypoint (`fixuid`, then `container/forward-docker-socket.sh`)
starts `socat` to forward it to `/var/run/docker.sock`, owned by the
container user's group. This runs on every container start — `docker start`
too, not only `docker run` — and always replaces an existing
`/var/run/docker.sock` first: `/var/run` is not a tmpfs here, so after a
stop/start (e.g. a WSL or Docker Desktop restart) the old socket file is still
there, but the `socat` behind it is gone. `socat` logs to
`/tmp/docker-socket-forward.log`. The zephyr-devshell devcontainers run the
same script from their `postStartCommand`, since their
`overrideCommand: true` replaces the image's entrypoint.

> **This gives the container full access to the host's docker daemon** — the
> same as being a member of the host's `docker` group, which is effectively
> root on the host. Anything running in the container can start a privileged
> container or mount any host path. Remove the `docker-host.sock` mount from
> `docker-compose.yml` if you don't want that.

## Files

| Path | What it is |
|---|---|
| `denver.yml` | A `netrc` stage (the container's `.netrc`, tokens checked) and a `docker` stage |
| `docker-compose.yml` | The `dev` service: image, mounts, user, devices, host docker socket |
| `create-env.sh` | Renders the `.env` Compose reads (`hooks: pre-docker:`) |
| `container/Dockerfile` | The image itself |
| `container/fixuid/` | Maps the container user onto your host UID/GID |
| `container/forward-docker-socket.sh` | Entrypoint: forwards the host docker socket for the container user |
| `configs/` | Shell/git config mounted into the container |
| `setup/install_host_tools.sh` | Host bootstrap, run via `--scripts setup` |

## Note

This env is runnable on its own, but it is not part of the `Examples` CI
matrix — building the image is slow and Docker-heavy. `simple-env`,
`raspberry-pico`, `zephyr-devshell-4.3.1` and `zephyr-devshell-4.4.1` are the
ones that run there; the last two exercise this configuration transitively through its
import chain.

## Next

- [`doc/providers/docker.md`](../../doc/providers/docker.md) — every `docker:`
  key, and how relocation works
- [`doc/configuration/config-file.md`](../../doc/configuration/config-file.md) — the wrapper/relocation
  model in general
- [`../zephyr-devshell`](../zephyr-devshell) — the env that imports this one
