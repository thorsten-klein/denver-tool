"""Tests for netrc_verify. Every host points at a local HTTP server."""

from __future__ import annotations

import base64
import datetime
import http.server
import json
import sys
import threading
from typing import ClassVar

import pytest

from denver_providers import netrc_verify as nv

GOOD_BASIC = "Basic " + base64.b64encode(b"alice:good").decode()


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_GET(self):
        auth = self.headers.get("Authorization")
        type(self).seen.append((self.path, auth))
        if self.path == "/basic":
            self._answer(200 if auth == GOOD_BASIC else 401)
        elif self.path == "/expiring":
            soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3, hours=1)
            self._answer(200, {"github-authentication-token-expiration": soon.strftime("%Y-%m-%d %H:%M:%S UTC")})
        elif self.path == "/forbidden":
            self._answer(403)
        elif self.path == "/redirect":
            self._answer(302, {"Location": "/basic"})
        elif self.path == "/broken":
            self._answer(503)
        else:
            self._answer(200)

    def _answer(self, status, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(_Handler, "seen", [])
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def run_check(server, path, host="h", password="good"):
    entry = nv.Entry(host, "alice", password)
    return nv.check([entry], {host: f"{server}{path}"}, 5, 7)[0]


@pytest.mark.parametrize(
    ("host", "url", "bearer", "proves"),
    [
        ("github.com", "https://api.github.com/user", True, True),
        ("raw.githubusercontent.com", "https://api.github.com/user", True, True),
        ("github.example.com", "https://github.example.com/api/v3/user", True, True),
        ("raw.github.example.com", "https://github.example.com/api/v3/user", True, True),
        ("gitlab.example.com", "https://gitlab.example.com/api/v4/user", True, True),
        ("artifactory.example.com", "https://artifactory.example.com/artifactory/api/system/ping", False, True),
        ("files.example.com", "https://files.example.com/", False, False),
    ],
)
def test_endpoint_for_rules(host, url, bearer, proves):
    assert nv.endpoint_for(host, {}) == nv.Endpoint(url, bearer, proves)


def test_endpoint_for_custom_wins():
    assert nv.endpoint_for("github.com", {"github.com": "https://mirror/x"}) == nv.Endpoint(
        "https://mirror/x", False, True
    )


def test_valid_and_rejected(server):
    assert run_check(server, "/basic").label == "valid"
    assert run_check(server, "/basic", password="nope").label == nv.REJECTED


def test_redirect_is_not_followed(server):
    verdict = run_check(server, "/redirect")
    assert verdict.label == nv.UNCHECKED
    assert "redirect" in verdict.note
    assert [p for p, _ in _Handler.seen] == ["/redirect"]


@pytest.mark.parametrize(
    ("path", "label", "note"),
    [
        ("/forbidden", "no permission", "403"),
        ("/broken", nv.UNCHECKED, "server error 503"),
        ("/expiring", "valid", "WARNING: token expires in 3 day(s)"),
    ],
)
def test_verdicts(server, path, label, note):
    verdict = run_check(server, path)
    assert verdict.label == label
    assert note in verdict.note


def test_bearer_token_sent(server, monkeypatch):
    monkeypatch.setattr(nv, "endpoint_for", lambda host, custom: nv.Endpoint(f"{server}/any", True, True))
    nv.check([nv.Entry("github.com", "x", "tok")], {}, 5, 7)
    assert _Handler.seen == [("/any", "Bearer tok")]


def test_unknown_host_is_unverified(server, monkeypatch):
    monkeypatch.setattr(nv, "endpoint_for", lambda host, custom: nv.Endpoint(f"{server}/", False, False))
    assert nv.check([nv.Entry("files", "alice", "good")], {}, 5, 7)[0].label == "unverified"


def test_unreachable_is_unchecked():
    verdict = nv.check([nv.Entry("h", "a", "b")], {"h": "http://127.0.0.1:9/"}, 5, 7)[0]
    assert verdict.label == nv.UNCHECKED
    assert "unreachable" in verdict.note


def test_check_without_entries():
    assert nv.check([], {}, 5, 7) == []


def test_table_never_shows_the_password():
    lines = nv.table([nv.Verdict("host", "alice", "valid", "200"), nv.Verdict("h2", "bob", "REJECTED", "401")])
    assert len(lines) == 2
    assert "host" in lines[0]


def test_read_entries_skips_default(tmp_path):
    path = tmp_path / "netrc"
    path.write_text("machine a login x password y\ndefault login d password p\n")
    assert nv.read_entries(path) == [nv.Entry("a", "x", "y")]


# Python before 3.11 refuses an entry without a password
@pytest.mark.skipif(sys.version_info < (3, 11), reason="netrc needs a password before 3.11")
def test_read_entries_skips_no_password(tmp_path):
    path = tmp_path / "netrc"
    path.write_text("machine a login x password y\nmachine b login x\n")
    assert nv.read_entries(path) == [nv.Entry("a", "x", "y")]


def test_state_roundtrip(tmp_path):
    state = tmp_path / "state"
    assert not nv.remembered(state, "k", 60)  # no file
    nv.remember(state, "k")
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert nv.remembered(state, "k", 60)
    assert not nv.remembered(state, "other", 60)
    assert not nv.remembered(state, "k", 0)  # too old


def test_state_garbage_is_not_remembered(tmp_path):
    state = tmp_path / "state"
    state.write_text("not json")
    assert not nv.remembered(state, "k", 60)
    assert json.loads(_write(state))["netrc-sha256"] == "k"


def _write(state):
    nv.remember(state, "k")
    return state.read_text()


def test_digest_changes_with_content(tmp_path):
    path = tmp_path / "netrc"
    path.write_text("a")
    first = nv.digest(path)
    path.write_text("b")
    assert nv.digest(path) != first


def test_days_left_without_or_with_garbage_header():
    assert nv.days_left(None) is None
    assert nv.days_left({}) is None
    assert nv.days_left({"github-authentication-token-expiration": "soon"}) is None
