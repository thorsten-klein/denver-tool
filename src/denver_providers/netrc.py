"""netrc provider: keeps a .netrc file in order and checks that its tokens still work.

Configured from a stage with ``provider: netrc``. It creates the file (mode 0600),
writes the ``machines:`` into it, exports ``DENVER_NETRC_FILE``, and verifies the
tokens. It runs on the host only.

Keys, examples and notes: ``doc/providers/netrc.md``.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from netrc import NetrcParseError
from pathlib import Path
from urllib.parse import urlsplit

from . import netrc_file, netrc_verify
from .base import Provider, fill_unset
from .context import (
    banner,
    die,
    die_on_unknown_keys,
    die_unless_flat_str_map,
    die_unless_required_strings,
    info,
    interpolate,
    warn,
)

DEFAULT_PATH = "~/.netrc"
# how often a rejected token is asked for again before giving up
MAX_PROMPTS = 3


def host_of(url):
    """The host a 'url:' stands for: a netrc entry names a host, not a scheme, port or path."""
    return (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()


def ask_token(prompt):
    """One token typed at the terminal, without echo. Empty if the user just pressed enter."""
    return getpass.getpass(prompt).strip()


class NetrcProvider(Provider):
    """Maintains and verifies a .netrc file -- see doc/providers/netrc.md for its config keys."""

    name = "netrc"
    host_side = True

    #: every key this provider's stage section understands
    KEYS = (
        "path",
        "seed-from",
        "machines",
        "verify",
        "prompt-interactive",
        "expiry-warning",
        "recheck-after",
        "timeout",
        "endpoints",
    )

    #: every key one 'machines:' entry understands
    MACHINE_KEYS = ("url", "username", "token", "overwrite", "verify")

    # ---- config defaults --------------------------------------------------- #
    @classmethod
    def resolve_defaults(cls, ctx, cfg, config):  # noqa: ARG003  # shared (ctx, cfg, config) signature
        """Resolve this stage's complete config: absolute paths, every default filled, 'machines:' validated.

        A machine's ``token:`` stays exactly as written (typically
        ``${NAME}``): this resolved section is what --show-config prints, and
        a secret must not end up there. setup() expands it.
        """
        resolved = dict(cfg)
        resolved["path"] = str(ctx.resolve_path(cls._optional_string(cfg, "path") or DEFAULT_PATH))
        seed = cls._optional_string(cfg, "seed-from")
        resolved["seed-from"] = str(ctx.resolve_path(seed)) if seed else None
        resolved["verify"] = cls._verify_setting(ctx, cfg.get("verify"))
        resolved["prompt-interactive"] = cls._bool(cfg, "prompt-interactive", default=True)
        resolved["expiry-warning"] = cls._number(cfg, "expiry-warning", default=7, whole=True)
        resolved["recheck-after"] = cls._number(cfg, "recheck-after", default=24 * 3600)
        resolved["timeout"] = cls._number(cfg, "timeout", default=5, positive=True)
        resolved["endpoints"] = cls._endpoints(ctx, cfg.get("endpoints"))
        resolved["machines"] = cls._machines(ctx, cfg.get("machines"))
        return fill_unset(resolved, cls.KEYS)

    # ---- config validation -------------------------------------------------- #
    @staticmethod
    def _optional_string(cfg, key):
        """``cfg[key]`` if it is a non-empty string, None if unset; dies for anything else."""
        value = cfg.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            die(f"netrc: '{key}:' must be a non-empty string (got {value!r})")
        return value

    @staticmethod
    def _bool(cfg, key, *, default):
        """``cfg[key]`` as a bool, ``default`` if unset; dies for anything else."""
        value = cfg.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            die(f"netrc: '{key}:' must be a boolean (got {value!r})")
        return value

    @staticmethod
    def _verify_setting(ctx, value):
        """'verify:' as a bool (default True) or a list of hosts, from the URLs/hosts written; dies for anything else."""
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            die(f"netrc: 'verify:' must be a boolean or a list of URLs (got {value!r})")
        hosts = [host_of(interpolate(v, ctx.variables)) for v in value]
        if not all(hosts):
            die(f"netrc: 'verify:' has an entry without a host (got {value!r})")
        return list(dict.fromkeys(hosts))

    @staticmethod
    def _number(cfg, key, *, default, whole=False, positive=False):
        """``cfg[key]`` as a number (not a bool), ``default`` if unset; dies for anything else."""
        value = cfg.get(key)
        if value is None:
            return default
        kind = int if whole else (int, float)
        if isinstance(value, bool) or not isinstance(value, kind) or value < 0 or (positive and value == 0):
            die(
                f"netrc: '{key}:' must be a {'whole ' if whole else ''}number >= {1 if positive else 0} (got {value!r})"
            )
        return value

    @staticmethod
    def _endpoints(ctx, endpoints):
        """'endpoints:' (host -> URL) with its URLs interpolated; {} if unset."""
        if endpoints is None:
            return {}
        die_unless_flat_str_map(endpoints, "netrc: 'endpoints:'")
        return {host: interpolate(url, ctx.variables) for host, url in endpoints.items()}

    @classmethod
    def _machines(cls, ctx, machines):
        """'machines:' as a list of complete entries (url/username interpolated, token left alone); [] if unset."""
        if machines is None:
            return []
        if not isinstance(machines, list) or not all(isinstance(m, dict) for m in machines):
            die(f"netrc: 'machines:' must be a list of mappings (got {machines!r})")
        resolved = [cls._machine(ctx, machine) for machine in machines]
        hosts = [host_of(m["url"]) for m in resolved]
        duplicates = sorted({host for host in hosts if hosts.count(host) > 1})
        if duplicates:
            die(f"netrc: 'machines:' lists {', '.join(duplicates)} more than once")
        return resolved

    @classmethod
    def _machine(cls, ctx, machine):
        """One 'machines:' entry, validated and with every key present."""
        die_on_unknown_keys(machine, cls.MACHINE_KEYS, "netrc: 'machines:' entry")
        die_unless_required_strings(machine, ("url", "username", "token"), "netrc: 'machines:' entry")
        url = interpolate(machine["url"], ctx.variables)
        if not host_of(url):
            die(f"netrc: 'machines:' entry: no host in url {machine['url']!r}")
        username = interpolate(machine["username"], ctx.variables)
        if not username.strip():
            die(f"netrc: machine '{host_of(url)}': 'username:' is empty after expanding {machine['username']!r}")
        verify = machine.get("verify")
        if verify is not None and not isinstance(verify, bool):
            die(f"netrc: machine '{host_of(url)}': 'verify:' must be a boolean (got {verify!r})")
        return {
            "url": url,
            "username": username,
            "token": machine["token"],
            "overwrite": cls._bool(machine, "overwrite", default=False),
            "verify": verify,
        }

    # ---- lifecycle ----------------------------------------------------------- #
    def setup(self, ctx):
        """Create/seed the file, write the machines into it, export where it is, verify the tokens."""
        if ctx.in_container:
            banner(ctx, self.stage, "skipped inside the container (done on the host)")
            return
        cfg = self.config_section(ctx)
        path = Path(cfg["path"])
        ctx.set("DENVER_NETRC_FILE", path)
        ctx.set("NETRC", path)
        if ctx.dry_run:
            ctx.dry_note("~", f"{self.stage}: would prepare and verify {path}")
            return
        if ctx.fast:
            banner(ctx, self.stage, "prepare and verify (skipped by --fast)")
            if not path.is_file():
                die(f"netrc[{self.stage}]: --fast needs '{path}' already there -- run once without --fast first")
            return
        banner(ctx, self.stage, "prepare")
        self._create(path, cfg["seed-from"])
        machines = self._stage_machines()
        self._write_machines(ctx, cfg, path, machines)
        if cfg["verify"]:
            banner(ctx, self.stage, "verify")
            self._verify(ctx, cfg, path, machines)

    def _stage_machines(self):
        """This stage's resolved 'machines:', tokens unexpanded -- see resolve_defaults."""
        return (self.config.get(self.stage) or {}).get("machines") or []

    # ---- the file ------------------------------------------------------------ #
    def _create(self, path, seed):
        """Make sure ``path`` is a file with mode 0600 -- python's netrc refuses one others can read -- seeded once."""
        if path.is_dir():
            die(f"netrc[{self.stage}]: '{path}' is a directory, expected a file")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        path.chmod(0o600)
        source = Path(os.path.realpath(seed)) if seed else None
        if source and source != path and path.stat().st_size == 0 and source.is_file():
            shutil.copyfile(source, path)
            info(f"netrc[{self.stage}]: seeded {path} from {source}")

    @staticmethod
    def _write(path, text):
        """Replace ``path``'s content atomically, so a crash never leaves a half-written credentials file."""
        temporary = path.with_name(f"{path.name}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        temporary.replace(path)

    def _read_entries(self, path):
        """The password entries of ``path``; dies if it is not a valid netrc."""
        try:
            return netrc_verify.read_entries(path)
        except NetrcParseError as e:
            die(f"netrc[{self.stage}]: {path} is not a valid netrc file: {e}")
            return []  # pragma: no cover -- unreachable, die() above never returns; satisfies ruff's RET503

    # ---- machines ------------------------------------------------------------- #
    def _interactive(self, ctx, cfg):
        """True if a token may be asked for: allowed by the config, on a terminal, and not a CI run."""
        return bool(cfg["prompt-interactive"] and sys.stdin.isatty() and not ctx.ci)

    def _write_machines(self, ctx, cfg, path, machines):
        """Write every 'machines:' entry that is missing from ``path`` (or, with 'overwrite:', differs) into it."""
        if not machines:
            return
        have = {e.host: (e.login, e.password) for e in self._read_entries(path)}
        before = path.read_text()
        text = before
        for machine in machines:
            text = self._apply_machine(ctx, cfg, machine, text, have)
        if text != before:
            self._write(path, text)
            info(f"netrc[{self.stage}]: updated {path}")

    def _apply_machine(self, ctx, cfg, machine, text, have):
        """``text`` with one machine written into it, or unchanged if there is nothing to do."""
        host = host_of(machine["url"])
        if netrc_file.has_machine(text, host) and not machine["overwrite"]:
            return text
        token = self._token(ctx, cfg, machine, host)
        if have.get(host) == (machine["username"], token):
            return text
        self._check_writable(host, machine["username"], token)
        return netrc_file.set_machine(text, host, machine["username"], token)

    def _token(self, ctx, cfg, machine, host):
        """A machine's token, expanded; asked for if that came out empty and a terminal is there, else a die."""
        token = interpolate(machine["token"], ctx.variables)
        if token.strip():
            return token.strip()
        if self._interactive(ctx, cfg):
            prompt = f"netrc[{self.stage}]: token for {machine['username']}@{host} ({machine['token']} is empty): "
            check = self._is_verified(cfg, host) and machine["verify"] is not False
            token, _ = self._ask_checked(cfg, host, machine["username"], prompt, check=check)
            if token:
                return token
            die(f"netrc[{self.stage}]: machine '{host}': no working token entered ({machine['token']} is empty)")
        die(f"netrc[{self.stage}]: machine '{host}': {machine['token']} is empty -- is the variable set?")
        return ""  # pragma: no cover -- unreachable, die() above never returns; satisfies ruff's RET503

    def _ask_checked(self, cfg, host, login, prompt, *, check):
        """Ask for a token until one is not rejected (``MAX_PROMPTS`` tries). (token, its verdict), or ("", None).

        Nothing is written here: the token is only returned once it passed.
        """
        for left in range(MAX_PROMPTS - 1, -1, -1):
            token = ask_token(prompt)
            if not token:
                break
            self._check_writable(host, login, token)
            if not check:
                return token, None
            verdict = self._check_token(cfg, host, login, token)
            if verdict.label != netrc_verify.REJECTED:
                return token, verdict
            warn(f"netrc[{self.stage}]: the token for {login}@{host} was rejected ({left} more tries)")
        return "", None

    @staticmethod
    def _check_token(cfg, host, login, token):
        """The verdict for one typed-in token, shown as a table line."""
        entry = netrc_verify.Entry(host, login, token)
        verdict = netrc_verify.check([entry], cfg["endpoints"], cfg["timeout"], cfg["expiry-warning"])[0]
        for line in netrc_verify.table([verdict]):
            info(line)
        return verdict

    def _check_writable(self, host, login, token):
        """Die if ``login``/``token`` could not be written as bare words. The value is never part of the message."""
        for key, value in (("username", login), ("token", token)):
            if netrc_file.UNSAFE_VALUE.search(value):
                die(f"netrc[{self.stage}]: machine '{host}': the {key} contains whitespace, a quote or a backslash")

    # ---- verification --------------------------------------------------------- #
    def _verify(self, ctx, cfg, path, machines):
        """Check every token in ``path``; ask for a new one where it was rejected; fail if one stays rejected."""
        skip = self._skipped_hosts(machines)
        entries = [e for e in self._read_entries(path) if e.host not in skip and self._is_verified(cfg, e.host)]
        state = ctx.env_workdir / f"{self.stage}.netrc-verified.json"
        only = ",".join(cfg["verify"]) if isinstance(cfg["verify"], list) else ""
        key = f"{netrc_verify.digest(path)}:{','.join(sorted(skip))}:{only}"
        if not entries or netrc_verify.remembered(state, key, cfg["recheck-after"]):
            return
        verdicts = self._renew_rejected(ctx, cfg, path, self._check(cfg, path, entries), machines)
        rejected = self._rejected(verdicts)
        if rejected:
            die(f"netrc[{self.stage}]: token rejected for {', '.join(v.host for v in rejected)} -- update it in {path}")
        self._remember(state, key, verdicts)

    @staticmethod
    def _skipped_hosts(machines):
        """The hosts with 'verify: false'."""
        return {host_of(m["url"]) for m in machines if m["verify"] is False}

    @staticmethod
    def _is_verified(cfg, host):
        """True if the stage's 'verify:' covers ``host``: true, or a list that names it."""
        verify = cfg["verify"]
        return host in verify if isinstance(verify, list) else bool(verify)

    @staticmethod
    def _remember(state, key, verdicts):
        """Remember this result, unless some host could not be checked."""
        if all(v.label != netrc_verify.UNCHECKED for v in verdicts):
            state.parent.mkdir(parents=True, exist_ok=True)
            netrc_verify.remember(state, key)

    @staticmethod
    def _rejected(verdicts):
        return [v for v in verdicts if v.label == netrc_verify.REJECTED]

    def _renew_rejected(self, ctx, cfg, path, verdicts, machines):
        """On a terminal, ask for a new token for every rejected host. The verdicts after that."""
        if not self._interactive(ctx, cfg):
            return verdicts
        by_host = {host_of(m["url"]): m for m in machines}
        return [
            self._renew(cfg, path, v, by_host.get(v.host)) if v.label == netrc_verify.REJECTED else v for v in verdicts
        ]

    def _check(self, cfg, path, entries):
        """Verdicts for ``entries``, shown as a table."""
        verdicts = netrc_verify.check(entries, cfg["endpoints"], cfg["timeout"], cfg["expiry-warning"])
        info(f"netrc[{self.stage}]: {path}")
        for line in netrc_verify.table(verdicts):
            info(line)
        return verdicts

    def _renew(self, cfg, path, verdict, machine):
        """Ask for a new token for a rejected entry. It is saved only if it is not rejected. The verdict after that."""
        prompt = f"netrc[{self.stage}]: new token for {verdict.login}@{verdict.host} (enter to skip): "
        token, checked = self._ask_checked(cfg, verdict.host, verdict.login, prompt, check=True)
        if not token:
            return verdict
        self._warn_if_overwritten(verdict.host, machine)
        self._write(path, netrc_file.set_machine(path.read_text(), verdict.host, verdict.login, token))
        info(f"netrc[{self.stage}]: saved the new token for {verdict.host}")
        return checked

    def _warn_if_overwritten(self, host, machine):
        """Warn if 'overwrite: true' will put the old token back on the next run."""
        if machine and machine["overwrite"]:
            warn(
                f"netrc[{self.stage}]: '{host}' has 'overwrite: true', so {machine['token']} replaces this token on the next run"
            )
