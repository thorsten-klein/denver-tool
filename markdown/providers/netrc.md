# netrc provider

A `netrc` stage looks after a `.netrc` file. It does four things:

1. Makes sure the file exists, with mode `0600`.
2. Writes the `machines:` you list into it (passwords usually come from environment variables).
3. Exports `DENVER_NETRC_FILE` (and `NETRC`) so later stages can use the file.
4. Checks that the tokens in it still work, and asks for a new one if not.

It runs on the **host only**. Inside the container the stage does nothing, because the file is
already mounted there.

```yaml
stages:
- netrc
- docker

netrc:
  provider: netrc
  path: ~/.denver/my-env/.netrc
  seed-from: ~/.netrc
  machines:
  - url: artifacts.example.com
    username: ci-bot
    token: ${ARTIFACTS_TOKEN}
```

(`provider:`, `description:`, `disabled:`, `env:` and the other generic keys work as for every
stage, see [Configuration](../configuration/config-file.md).)

## Keys

- **`path`** — the netrc file. Default: `~/.netrc`. Parent folders are created. It must not be a
  directory.
- **`seed-from`** — a file to copy into `path` **once**, when `path` is empty. Default: none.
- **`machines`** — a list of entries to write into `path`:
  - **`url`** (required) — a URL or a host name. Only the host is used (`https://a.b:8443/x` → `a.b`).
  - **`username`** (required) — the `login`.
  - **`token`** (required) — the `password`. Write `${NAME}` to read an environment variable.
  - **`overwrite`** — default `false`. If the host is already in the file, `false` keeps it and
    `true` replaces it.
  - **`verify`** — `false` skips the check for this host.
- **`verify`** — check the tokens. Default: `true`. A boolean turns the check on or off for every
  host. A list of URLs or host names (only the host is used) checks just those hosts and leaves the
  others alone; an empty list checks nothing.
- **`prompt-interactive`** — ask for a token on a terminal. Default: `true`. See “Prompts”.
- **`endpoints`** — `host: url` pairs: the URL to check that host against (Basic auth).
- **`expiry-warning`** — warn when a GitHub token ends within this many days. Default: `7`.
- **`recheck-after`** — seconds before an unchanged file is checked again. Default: `86400`.
- **`timeout`** — seconds per request. Default: `5`.

## Tokens

- Prefer `${NAME}` over a literal token. `--show-config` shows `${NAME}`, never its value.
- If `${NAME}` is empty and the entry is needed, the stage stops (or asks, see “Prompts”).
  An entry that is already in the file and has `overwrite: false` does not need it.
- User names and tokens may not contain spaces, quotes or backslashes.
- Only the entry you name is changed. Comments and other entries in the file stay.

## Verification

Each host is asked at a URL that needs a login:

| Host                                                    | URL                                          | Sent as   |
|---------------------------------------------------------|----------------------------------------------|-----------|
| `github.com`, `*.github.com`, `*.githubusercontent.com` | `https://api.github.com/user`                | Bearer    |
| contains `github`                                       | `https://<host>/api/v3/user`                 | Bearer    |
| contains `gitlab`                                       | `https://<host>/api/v4/user`                 | Bearer    |
| contains `artifactory`                                  | `https://<host>/artifactory/api/system/ping` | Basic     |
| listed in `endpoints:`                                  | that URL                                     | Basic     |
| anything else                                           | `https://<host>/`                            | Basic     |

Results: `valid`, `no permission` (403, not a login problem), `REJECTED` (401), `unverified`
(unknown host type, only a 401 means something) and `unchecked` (unreachable, server error or
redirect). Only `REJECTED` stops the run, so working offline still works.

A redirect is never followed, so a token only goes to the host it belongs to. A run with no
`unchecked` result is remembered and not repeated until the file changes or `recheck-after`
passes.

## Prompts

With `prompt-interactive: true` and a terminal, denver asks for a token (no echo) when:

- a needed token is empty, or
- a token is `REJECTED`.

A typed token is **checked before it is saved**. If it is rejected, nothing is written and you
are asked again. You get 3 tries per host, then the run stops. A token is also accepted when the
host can’t be checked (for example, it is unreachable), or when `verify` is off. Press enter to
skip.

There is no question without a terminal, with `--ci`, or with `prompt-interactive: false`: the
run just stops.

If a machine has `overwrite: true`, its `${NAME}` replaces the typed token on the next run.
Denver warns about this.

## Notes

- **Docker:** put the stage before `docker` and mount `${DENVER_NETRC_FILE}` in the compose file.
- **curl:** curl ignores `NETRC`. Use `curl --netrc-file "$DENVER_NETRC_FILE"`.
- **`--fast`** skips writing and checking, and needs the file to exist.
- **`--dry-run`** only exports the variables.
