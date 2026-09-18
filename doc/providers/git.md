# git provider

A `git` stage brings one git checkout into the environment, pinned to one
revision: clone if it isn't there yet, otherwise move it — always detached,
never on a branch — onto whatever `revision:` names now, fetching only when
that commit isn't available locally (or is a branch, which moves). It is
what the "git clone, then fetch/checkout a pinned tag by hand" shell script
(see the worked example in [`custom`](custom.md)) looks like once it is a
provider — the same job, but idempotent and `--fast`-aware without every
project writing that logic again.

```toml
[pico-sdk]
provider = "git"
url = "https://github.com/raspberrypi/pico-sdk.git"
path = "${DENVER_ENV_WORKDIR}/pico-sdk/2.3.0"
revision = "2.3.0"
submodules = true
env-prepend = { PICO_SDK_PATH = "${DENVER_ENV_WORKDIR}/pico-sdk/2.3.0" }
```

(`provider:`/`description:`/`disabled:`/`scripts:`/`env:`/`env-prepend:`/`env-append:`/`env-sep:` are generic
keys every stage has — see "Generic stage keys" in [Configuration](../configuration/config-file.md). Everything
below is specific to `git`; `env-prepend:` above is that generic mechanism, not something this provider
implements itself — see "Where things go" below for why its value is the checkout's own `path:`, spelled out,
rather than `download`'s package-relative `"."`.)

## Key reference

- **`url`** (**required**) — the repository to clone. Anything `git clone`
  itself accepts (`https://`, `git@host:...`, a local path).
- **`path`** (**required**) — where the checkout lives. Resolves like every
  other denver path (against the env dir, then imported base envs), so it is
  almost always written against `${DENVER_ENV_WORKDIR}` (denver's per-env
  state dir) — a checkout that size has no business inside the project's own
  working tree, and deleting the env deletes it.
- **`revision`** (**required**) — a tag, a branch, or a raw commit sha. This
  provider always ends up with `path:` in a **detached** state at exactly
  this commit — never on a branch — so a moving branch name is pinned to
  whatever it resolved to *this run*, the same guarantee a tag gives.
- **`remote`** — the remote name a fresh clone is created under, and the one
  `revision:` is fetched/resolved against (default: `"origin"`).
- **`clone-opts`** — extra arguments for the clone, as a list:
  `git clone --origin <remote> <clone-opts> -- <url> <path>` (e.g.
  `[--depth, "1"]`, `[--filter=blob:none]`). Default: none.
- **`fetch-opts`** — extra arguments for every fetch, as a list:
  `git fetch --tags --prune <fetch-opts> <remote>`. Default: none.
- **`submodules`** — `true` runs `git submodule update --init` after
  checkout (default: `false`). Not recursive — a submodule that itself
  declares submodules needs those handled separately (most projects don't
  need them merely to build against the checkout; see "Submodules" below).

This provider has no `env-prepend:`/`env-append:` keys of its own: a
checkout's own location is already known in full wherever `path:` is
written, so the generic per-stage `env:`/`env-prepend:`/`env-append:` keys
(see "Generic stage keys" in [Configuration](../configuration/config-file.md))
already cover exporting it — spell out `path:`'s own value again (as the
worked example above does for `PICO_SDK_PATH`), rather than the
package-relative `"."` a [`download`](download.md) package's own
`env-prepend:` can use.

## What runs, and what is skipped

Per stage, in order:

1. **Clone** — only when `path:` doesn't exist at all:
   `git clone --origin <remote> <clone-opts> -- <url> <path>`. A fresh clone
   already has every branch and tag, so no fetch follows it. An existing
   `path:` is never cloned into. If it isn't a git checkout (no `path/.git`),
   the stage fails, because every git command below would otherwise act on
   whatever repository encloses it.
2. **Remote url** — when the configured url of `remote:` (`git config
   remote.<remote>.url`, not `git remote get-url`, which applies `insteadOf`
   rewrites) differs from `url:`, it is updated with `git remote set-url`, or
   added with `git remote add` if the remote doesn't exist. Nothing is
   re-cloned.
3. **Checkout, fetching only if needed**:
   - `revision:` is looked up in what is already local. A tag or a commit sha
     found there is used as is, with no network access.
   - A branch (a `<remote>/<revision>` ref exists) moves, so it is always
     fetched first, and resolves to the fetched `<remote>/<branch>` tip. That
     is how a moving branch is followed.
   - Otherwise (not local yet, the remote url just changed, or `--force`):
     `git fetch --tags --prune <fetch-opts> <remote>`, then the lookup again.
     A commit sha that fetch still didn't bring (see "Unreachable commits"
     below) is fetched explicitly.
   - Skipped when `path:` is already detached at exactly that commit;
     otherwise `git checkout --detach <sha>`.
4. **Submodules** — `git submodule sync && git submodule update --init`,
   only when `submodules: true`. Runs every time (there is no cheap way to
   tell "already up to date" apart from asking git, and asking is what
   these two commands do).

The generic per-stage `env:`/`env-prepend:`/`env-append:` keys run after
this, for every stage regardless of provider — see "Generic stage keys" in
[Configuration](../configuration/config-file.md).

## Unreachable commits

`git fetch --tags` sees every tag and every branch tip, but not a commit
pinned by raw sha that sits *behind* one — most servers refuse to serve an
object that isn't the tip of some advertised ref at all
(`uploadpack.allowReachableSHA1InWant`/`allowAnySHA1InWant`, off by default
on GitHub and most self-hosted setups). When the generic fetch above didn't
leave `revision:` resolvable, this provider tries once more with an
explicit `git fetch <remote> <revision>` — which succeeds for a sha the
remote is willing to serve directly, and fails the same way `git` itself
would otherwise. There is no third fallback: a revision neither fetch found
is reported as not found, naming the remote it was fetched from.

## Where things go

There is no separate "downloads vs. unpacked" split the way `download` has
one — a git checkout *is* both, and git's own object store is what makes an
unchanged fetch cheap. `path:` is the whole of it:

```
<path>/                # exactly what 'git clone <url> <path>' would leave,
├── .git/               # then moved (detached) onto 'revision:'
└── ...                  # the checkout itself
```

## Design notes

- **Why a provider and not a `custom: cmd:` script.** The shell version of
  this (see [`custom`](custom.md)'s worked example) is the same ten-odd
  lines every project migrating off a hand-pinned git checkout has to get
  right: recognise an existing clone, never re-clone over it, move a
  *moving* pin (a branch) forward without leaving the
  tree on a branch a later `git pull` could then drift. Here that logic
  exists once.
- **Always detached.** A stage's checkout is denver's own state, not
  something a person is meant to commit on top of — `--detach` makes that
  the checkout's actual state, not just a convention, so `git status`
  inside it never reads as "on branch main, 40 commits ahead" for a branch
  nobody is developing against.
- **`submodules:` is not recursive.** Recursing by default would fetch a
  submodule's own submodules whether or not anything actually needs them
  (mbedtls's own test/build tooling submodule, inside pico-sdk, is exactly
  such a case) — every extra clone is bandwidth and time spent on a stage
  every `denver run` re-checks. A project needing nested submodules reaches
  for `custom` (or asks for it — this provider doesn't have a
  `submodules-recursive:` key yet).
- **A tag is trusted once it is local.** A tag already in the checkout is
  never fetched again, so a tag re-pointed on the remote is not picked up
  until `--force` (which always fetches) — pin a moving target as a branch.
- **`--fast`** skips clone/fetch/checkout/submodules entirely; it dies with
  a clear message if `path:` was never checked out — run once without
  `--fast` first. The generic `env:`/`env-prepend:`/`env-append:` keys still
  apply regardless (see "Generic stage keys" in
  [Configuration](../configuration/config-file.md)) — that's the activation
  half, not a build step to skip.
- **`--force`** runs `git reset --hard` and `git clean -fdx` before
  checking out, discarding whatever local state (a hand-edited file, an
  untracked build artifact) would otherwise survive the move onto
  `revision:`. Without it, a checkout already sitting at the right commit is
  left completely alone, local edits included.
- **`--dry-run`** reports the clone/fetch/checkout/submodule commands
  instead of running them, and leaves whatever is already on disk
  untouched. Resolving `revision:` to a commit is still attempted for real
  (it is a read, not a write, the same way `download`'s checksum check is)
  so the preview can say "already there" when that's true; a checkout this
  preview would have created fresh has nothing to resolve against yet, and
  that limitation is reported rather than silently guessed past.
- **No built-in credentials.** Unlike `download`'s `[[download-auth]]`,
  this provider sends none of its own — a private repository is exactly
  what `git`'s own credential helper, an SSH agent, or a `url:` already
  carrying a token are for, all of which a plain `git clone`/`git fetch`
  already honours with no denver-specific configuration at all.
