"""Check if the tokens in a .netrc still work. Used by the ``netrc`` provider.

Each host is asked at an endpoint that needs login (GitHub, GitLab, Artifactory,
or one from ``endpoints:``). Other hosts are only "unverified".

Labels: valid, no permission (403), REJECTED (401), unverified, unchecked
(unreachable, 5xx or a redirect). A redirect is never followed, so a token only
goes to the host it belongs to.

Details: ``doc/providers/netrc.md``.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import netrc
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

REJECTED = "REJECTED"
UNCHECKED = "unchecked"


class Endpoint(NamedTuple):
    """Where to check a host, how to send the credentials, and whether a 2xx there proves them."""

    url: str
    bearer: bool
    proves_auth: bool


class Entry(NamedTuple):
    """One netrc 'machine' entry with a password."""

    host: str
    login: str
    password: str


class Verdict(NamedTuple):
    """What checking one entry found: a label (see the module docstring) and a note on why."""

    host: str
    login: str
    label: str
    note: str


def _github_com(host):
    return host == "github.com" or host.endswith((".github.com", ".githubusercontent.com"))


# (matches host, endpoint for host): first match wins
RULES = (
    (_github_com, lambda _host: Endpoint("https://api.github.com/user", True, True)),
    (
        lambda host: "github" in host,
        lambda host: Endpoint(f"https://{host.removeprefix('raw.')}/api/v3/user", True, True),
    ),
    (lambda host: "gitlab" in host, lambda host: Endpoint(f"https://{host}/api/v4/user", True, True)),
    (
        lambda host: "artifactory" in host,
        lambda host: Endpoint(f"https://{host}/artifactory/api/system/ping", False, True),
    ),
)


def endpoint_for(host, custom):
    """The Endpoint for ``host``: one of ``custom`` (host -> URL) first, then the first matching rule, else its root."""
    if host in custom:
        return Endpoint(custom[host], False, True)
    for matches, endpoint in RULES:
        if matches(host):
            return endpoint(host)
    return Endpoint(f"https://{host}/", False, False)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Surface a redirect as its 3xx response: the token goes to the listed host or nowhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002  # urllib's signature
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def ask(endpoint, login, password, timeout):
    """(status, headers) of one authenticated GET, or (None, reason) if there was no HTTP answer."""
    if endpoint.bearer:
        authorization = f"Bearer {password}"
    else:
        authorization = "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()
    request = urllib.request.Request(
        endpoint.url, headers={"Authorization": authorization, "User-Agent": "denver-verify-netrc"}
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers
    except OSError as e:  # includes URLError
        return None, str(getattr(e, "reason", e))


def days_left(headers):
    """Days until GitHub's 'github-authentication-token-expiration' header, or None without one."""
    raw = headers.get("github-authentication-token-expiration") if headers else None
    if not raw:
        return None
    try:
        expires = datetime.datetime.strptime(raw.replace("UTC", "+0000"), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return (expires - datetime.datetime.now(datetime.timezone.utc)).days


def judge(host, login, endpoint, status, info, expiry_warning):
    """Turn one answer into a Verdict."""
    if status is None:
        return Verdict(host, login, UNCHECKED, f"unreachable: {info}")
    if status == 401:
        return Verdict(host, login, REJECTED, f"401 from {endpoint.url}")
    if 300 <= status < 400:
        return Verdict(host, login, UNCHECKED, f"{status} redirect, not followed")
    if status >= 500:
        return Verdict(host, login, UNCHECKED, f"server error {status}")
    if not endpoint.proves_auth:
        return Verdict(host, login, "unverified", f"{status}; unknown host type, see 'endpoints:'")
    if status == 403:
        return Verdict(host, login, "no permission", "403: authenticated, but the account lacks a right")
    left = days_left(info)
    if left is not None and left < expiry_warning:
        return Verdict(host, login, "valid", f"WARNING: token expires in {left} day(s)")
    return Verdict(host, login, "valid", str(status))


def read_entries(path):
    """Every 'machine' entry of the netrc at ``path`` with a password ('default' is not a host and is left out).

    Raises netrc.NetrcParseError for a file that is not a valid netrc.
    """
    return [
        Entry(host, login or "", password)
        for host, (login, _account, password) in netrc.netrc(path).hosts.items()
        if password and host != "default"
    ]


def check(entries, custom, timeout, expiry_warning):
    """A Verdict for every entry, asked in parallel: the run takes as long as the slowest host, not their sum."""

    def one(entry):
        endpoint = endpoint_for(entry.host, custom)
        status, info = ask(endpoint, entry.login, entry.password, timeout)
        return judge(entry.host, entry.login, endpoint, status, info, expiry_warning)

    if not entries:
        return []
    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        return list(pool.map(one, entries))


def table(verdicts):
    """The verdicts as aligned text lines. Never contains a password."""
    host_width = max(len(v.host) for v in verdicts)
    login_width = max(len(v.login) for v in verdicts)
    return [f"  {v.host:<{host_width}}  {v.login:<{login_width}}  {v.label:<13}  {v.note}" for v in verdicts]


def digest(path):
    """sha256 of the file at ``path``."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remembered(state, sha, recheck_after):
    """True if ``state`` records a complete verification of exactly ``sha`` less than ``recheck_after`` seconds ago."""
    try:
        last = json.loads(Path(state).read_text())
    except (OSError, ValueError):
        return False
    return last.get("netrc-sha256") == sha and time.time() - last.get("verified-at", 0) < recheck_after


def remember(state, sha):
    """Record that exactly ``sha`` was completely verified now, in a file only the user can read."""
    with os.fdopen(os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"netrc-sha256": sha, "verified-at": time.time()}, f)
