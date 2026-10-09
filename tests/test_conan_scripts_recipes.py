"""Tests for providers.conan_scripts.recipes -- the bundled recipe pipeline tool
conan.py invokes as a subprocess (see providers/conan.py).

recipes.py talks to a real ConanAPI() when actually used; here every test
either exercises pure logic (ConfigFile-free path resolution, argument
parsing) or replaces the module-level ``conan_api`` with a small fake, so no
test ever touches a real conan installation or home directory (see
recipes._real_conan_api's docstring for why that matters).
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest
import yaml
from conan.api.model import PkgReference, RecipeReference, Remote
from conan.errors import ConanException
from conan.internal.errors import AuthenticationException, ConanConnectionError, ForbiddenException, NotFoundException

from denver_providers.conan_scripts import recipes

# the real one, before _fresh_per_process_state points it into tmp_path
_real_usable_remotes_file = recipes._usable_remotes_file

# what conan raises once a remote kept answering HTTP 503 (urllib3's retry
# error, wrapped by conan's _call_remote), and for a 503 it didn't retry
UNAVAILABLE_ERRORS = [
    ConanException(
        "HTTPSConnectionPool(host='mirror', port=443): Max retries exceeded with url: /v2/users/check_credentials "
        "(Caused by ResponseError('too many 503 error responses'))"
    ),
    ConanException("Server exception 503: Service Unavailable"),
]


@pytest.fixture(autouse=True)
def _fresh_per_process_state(monkeypatch, tmp_path):
    """recipes.py keeps a few one-per-process switches/caches at module level (see main()) -- reset for every test.

    Its usable/unavailable remotes record goes to tmp_path: main() reads it
    on every run but --prepare, which must never reach a real conan home.
    """
    monkeypatch.setattr(recipes, "_usable_remotes_cache", None)
    monkeypatch.setattr(recipes, "_auth_may_fail", False)
    monkeypatch.setattr(recipes, "_no_remote", False)
    monkeypatch.setattr(recipes, "_usable_remote_names", None)
    monkeypatch.setattr(recipes, "_unavailable_remotes", set())
    monkeypatch.setattr(recipes, "_usable_remotes_file", lambda: tmp_path / recipes.USABLE_REMOTES_FILENAME)
    # never use the real ~/.netrc
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


# --------------------------------------------------------------------------- #
# conan_api: lazy, memoized construction (through the public interface, not
# implementation details -- see _real_conan_api's docstring)
# --------------------------------------------------------------------------- #
def test_conan_api_constructs_lazily_and_reuses_instance(monkeypatch):
    constructed = []

    class Fake:
        def __init__(self):
            constructed.append(True)
            self.remotes = "the-remotes-object"

    monkeypatch.setattr(recipes, "ConanAPI", Fake)
    recipes._real_conan_api.cache_clear()
    try:
        assert constructed == []
        assert recipes.conan_api.remotes == "the-remotes-object"
        assert constructed == [True]
        assert recipes.conan_api.remotes == "the-remotes-object"
        assert constructed == [True]  # reused, not reconstructed
    finally:
        recipes._real_conan_api.cache_clear()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def test_redirect_swaps_and_restores_streams():
    original_out, original_err = sys.stdout, sys.stderr
    with recipes.redirect():
        assert sys.stdout is not original_out
        assert sys.stderr is not original_err
    assert sys.stdout is original_out
    assert sys.stderr is original_err


# --------------------------------------------------------------------------- #
# get_cache_path
# --------------------------------------------------------------------------- #
def test_get_cache_path_recipe_reference(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")

    class FakeCache:
        def export_path(self, r):
            return "/some/path"

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(cache=FakeCache()))
    assert recipes.get_cache_path(ref) == Path("/some/path")


def test_get_cache_path_package_reference(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "pkgid", "rev")

    class FakeCache:
        def package_path(self, p):
            return "/some/pkg/path"

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(cache=FakeCache()))
    assert recipes.get_cache_path(pref) == Path("/some/pkg/path")


def test_get_cache_path_unsupported_type():
    with pytest.raises(recipes.CatalogError):
        recipes.get_cache_path("not-a-ref")


def test_get_cache_path_swallows_conan_not_found(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")

    class FakeCache:
        def export_path(self, r):
            raise recipes.ConanException("not found in cache")

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(cache=FakeCache()))
    assert recipes.get_cache_path(ref) is None


def test_get_cache_path_propagates_other_errors(monkeypatch):
    # a permission error or corrupt cache must not look like "not cached"
    # (B7) -- only conan's own ConanException is treated as a cache miss.
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")

    class FakeCache:
        def export_path(self, r):
            raise RuntimeError("boom")

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(cache=FakeCache()))
    with pytest.raises(RuntimeError, match="boom"):
        recipes.get_cache_path(ref)


# --------------------------------------------------------------------------- #
# catalog / recipe resolution
# --------------------------------------------------------------------------- #
def test_get_recipes_from_entries_skips_dotted_keys(tmp_path):
    entries = {
        ".version": "1.2.3",
        "foo/1.0": "foo/1.0@denver/snapshot",
    }
    recipes_ref = recipes.get_recipes_from_entries([tmp_path], entries)
    assert len(recipes_ref) == 1
    (path, ref) = next(iter(recipes_ref.items()))
    assert path == (tmp_path / "foo" / "1.0" / "conanfile.py").absolute()
    assert ref.name == "foo"


def test_read_catalog_loads_yaml_from_disk(tmp_path):
    catalog_yml = tmp_path / "catalog.yml"
    catalog_yml.write_text(yaml.safe_dump({"foo/1.0": "foo/1.0@denver/snapshot"}))
    assert recipes.read_catalog(catalog_yml) == {"foo/1.0": "foo/1.0@denver/snapshot"}


def test_handle_args_recipe_by_dir(tmp_path):
    all_recipes = recipes.get_recipes_from_entries([tmp_path], {"foo/1.0": "foo/1.0@denver/snapshot"})
    recipe_dir = tmp_path / "foo" / "1.0"
    recipe_dir.mkdir(parents=True)
    filtered = recipes.handle_args_recipe(all_recipes, [str(recipe_dir)])
    assert len(filtered) == 1


def test_handle_args_recipe_unknown_dies(tmp_path):
    all_recipes = recipes.get_recipes_from_entries([tmp_path], {"foo/1.0": "foo/1.0@denver/snapshot"})
    with pytest.raises(recipes.CatalogError):
        recipes.handle_args_recipe(all_recipes, ["bar/2.0"])


# --------------------------------------------------------------------------- #
# authenticate_remote
# --------------------------------------------------------------------------- #
def test_authenticate_remote_success_no_prompt(monkeypatch):
    calls = []
    fake_api = types.SimpleNamespace(
        remotes=types.SimpleNamespace(user_auth=lambda r, force=False: calls.append((r.name, force)))
    )
    monkeypatch.setattr(recipes, "conan_api", fake_api)
    prompted = []
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))

    recipes.authenticate_remote(Remote("conancenter", "http://conancenter"))

    assert calls == [("conancenter", False)]
    assert prompted == []


def test_authenticate_remote_force_passed_through(monkeypatch):
    calls = []
    fake_api = types.SimpleNamespace(
        remotes=types.SimpleNamespace(user_auth=lambda r, force=False: calls.append(force))
    )
    monkeypatch.setattr(recipes, "conan_api", fake_api)

    recipes.authenticate_remote(Remote("conancenter", "http://conancenter"), force=True)

    assert calls == [True]


def test_authenticate_remote_prompts_on_failure_when_interactive(monkeypatch):
    # stale credentials.json entry -> conan's own user_auth fails without
    # ever prompting itself (see authenticate_remote's docstring); denver
    # falls back to its own interactive prompt when stdin is a TTY.
    def failing_user_auth(r, force=False):
        raise AuthenticationException("Wrong user or password")

    fake_api = types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=failing_user_auth))
    monkeypatch.setattr(recipes, "conan_api", fake_api)
    monkeypatch.setattr(recipes.sys.stdin, "isatty", lambda: True)
    prompted = []
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))

    recipes.authenticate_remote(Remote("conancenter", "http://conancenter"))

    assert prompted == ["conancenter"]


def test_authenticate_remote_reraises_when_not_interactive(monkeypatch):
    def failing_user_auth(r, force=False):
        raise AuthenticationException("Wrong user or password")

    fake_api = types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=failing_user_auth))
    monkeypatch.setattr(recipes, "conan_api", fake_api)
    monkeypatch.setattr(recipes.sys.stdin, "isatty", lambda: False)
    prompted = []
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))

    remote = Remote("conancenter", "http://conancenter")
    with pytest.raises(AuthenticationException):
        recipes.authenticate_remote(remote)
    assert prompted == []


# --------------------------------------------------------------------------- #
# netrc
# --------------------------------------------------------------------------- #
def _write_netrc(monkeypatch, tmp_path, text="machine artifacts.example.com login bot password tok\n"):
    path = tmp_path / "netrc"
    path.write_text(text)
    monkeypatch.setenv("NETRC", str(path))


ARTIFACTS = "https://artifacts.example.com/artifactory/api/conan/team"


@pytest.mark.parametrize(
    ("text", "url", "expected"),
    [
        ("machine Artifacts.example.com login bot password tok\n", ARTIFACTS, ("bot", "tok")),
        (
            "machine artifacts.example.com:8443 login bot password tok\n",
            "https://artifacts.example.com:8443/x",
            ("bot", "tok"),
        ),
        (
            "machine artifacts.example.com login bot password tok\n",
            "https://artifacts.example.com:8443/x",
            ("bot", "tok"),
        ),
        ("machine other.example.com login bot password tok\n", ARTIFACTS, None),
        ("default login bot password tok\n", ARTIFACTS, None),
    ],
    ids=["host", "host-port", "host-ignores-port", "other-host", "default-ignored"],
)
def test_netrc_credentials(monkeypatch, tmp_path, text, url, expected):
    _write_netrc(monkeypatch, tmp_path, text)
    assert recipes.netrc_credentials(url) == expected


def test_netrc_credentials_without_file_is_none():
    assert recipes.netrc_credentials(ARTIFACTS) is None


def test_authenticate_remote_offers_netrc_as_conan_env(monkeypatch, tmp_path):
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.delenv("CONAN_PASSWORD", raising=False)
    seen = []

    def user_auth(r, force=False):
        seen.append((os.environ.get("CONAN_LOGIN_USERNAME_TEAM_REMOTE"), os.environ.get("CONAN_PASSWORD_TEAM_REMOTE")))

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=user_auth)))

    recipes.authenticate_remote(Remote("team-remote", ARTIFACTS))

    assert seen == [("bot", "tok")]
    assert "CONAN_PASSWORD_TEAM_REMOTE" not in os.environ  # removed again


def test_authenticate_remote_keeps_a_username_env_already_set(monkeypatch, tmp_path):
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.delenv("CONAN_PASSWORD", raising=False)
    monkeypatch.setenv("CONAN_LOGIN_USERNAME_TEAM", "mine")
    user_auth = lambda r, force=False: None  # noqa: E731
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=user_auth)))

    recipes.authenticate_remote(Remote("team", ARTIFACTS))

    assert os.environ["CONAN_LOGIN_USERNAME_TEAM"] == "mine"


def test_authenticate_remote_conan_password_env_wins_over_netrc(monkeypatch, tmp_path):
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.setenv("CONAN_PASSWORD_TEAM", "from-env")
    seen = []
    user_auth = lambda r, force=False: seen.append(os.environ.get("CONAN_PASSWORD_TEAM"))  # noqa: E731
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=user_auth)))

    recipes.authenticate_remote(Remote("team", ARTIFACTS))

    assert seen == ["from-env"]


def _rejecting_api(logged_in, login_error=None):
    def user_auth(r, force=False):
        raise AuthenticationException("Wrong user or password")

    def user_login(r, user, password):
        logged_in.append((r.name, user, password))
        if login_error:
            raise login_error

    return types.SimpleNamespace(remotes=types.SimpleNamespace(user_auth=user_auth, user_login=user_login))


def test_authenticate_remote_netrc_after_stale_conan_credentials(monkeypatch, tmp_path):
    # stale conan login -> netrc, no prompt, also in CI
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.setenv("CONAN_PASSWORD_TEAM", "stale")
    logged_in = []
    monkeypatch.setattr(recipes, "conan_api", _rejecting_api(logged_in))
    monkeypatch.setattr(recipes.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: pytest.fail("prompted"))

    recipes.authenticate_remote(Remote("team", ARTIFACTS))

    assert logged_in == [("team", "bot", "tok")]


def test_authenticate_remote_rejected_netrc_is_not_retried(monkeypatch, tmp_path):
    # netrc itself failed -> prompt, no second netrc try
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.delenv("CONAN_PASSWORD", raising=False)
    logged_in = []
    monkeypatch.setattr(recipes, "conan_api", _rejecting_api(logged_in))
    monkeypatch.setattr(recipes.sys.stdin, "isatty", lambda: True)
    prompted = []
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))

    recipes.authenticate_remote(Remote("team", ARTIFACTS))

    assert logged_in == []
    assert prompted == ["team"]


def test_authenticate_remote_rejected_netrc_reraises_when_not_interactive(monkeypatch, tmp_path):
    _write_netrc(monkeypatch, tmp_path)
    monkeypatch.setenv("CONAN_PASSWORD_TEAM", "stale")
    logged_in = []
    monkeypatch.setattr(recipes, "conan_api", _rejecting_api(logged_in, AuthenticationException("no")))
    monkeypatch.setattr(recipes.sys.stdin, "isatty", lambda: False)

    with pytest.raises(AuthenticationException):
        recipes.authenticate_remote(Remote("team", ARTIFACTS))
    assert logged_in == [("team", "bot", "tok")]


def test_conan_login_skips_when_netrc_username_unchanged(monkeypatch, tmp_path):
    _write_netrc(monkeypatch, tmp_path)
    api = FakeRemotesAPI([Remote("team", ARTIFACTS)])
    api._user_info["team"] = {"username": "bot"}
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"team": {}})
    assert api.auth_calls == []


def test_prompt_and_login_uses_input_and_getpass(monkeypatch):
    logged_in = []
    fake_api = types.SimpleNamespace(
        remotes=types.SimpleNamespace(user_login=lambda r, u, p: logged_in.append((r.name, u, p)))
    )
    monkeypatch.setattr(recipes, "conan_api", fake_api)
    monkeypatch.setattr("builtins.input", lambda prompt="": "alice")
    monkeypatch.setattr(recipes.getpass, "getpass", lambda prompt="": "s3cret")

    recipes._prompt_and_login(Remote("conancenter", "http://conancenter"))

    assert logged_in == [("conancenter", "alice", "s3cret")]


# --------------------------------------------------------------------------- #
# get_deps_graph_remote / get_deps_graph_local / get_pref_from_ref
# --------------------------------------------------------------------------- #
class FakeProfilesAPI:
    def get_default_host(self):
        return "host-profile"

    def get_default_build(self):
        return "build-profile"

    def get_profile(self, paths):
        return f"profile:{paths[0]}"


def test_get_deps_graph_remote_authenticates_each_remote(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    remotes = [Remote("a", "http://a"), Remote("b", "http://b")]
    authed = []

    fake_api = types.SimpleNamespace(
        lockfile=types.SimpleNamespace(get_lockfile=lambda: "lockfile"),
        profiles=FakeProfilesAPI(),
        remotes=types.SimpleNamespace(
            list=lambda only_enabled: remotes, user_auth=lambda r, force=False: authed.append(r.name)
        ),
        graph=types.SimpleNamespace(load_graph_requires=lambda *a, **k: "deps-graph"),
    )
    monkeypatch.setattr(recipes, "conan_api", fake_api)

    result = recipes.get_deps_graph_remote(ref)
    assert result == "deps-graph"
    assert authed == ["a", "b"]


def test_usable_remotes_raises_on_auth_failure_by_default(monkeypatch):
    api = _AuthFailsFor([Remote("a", "http://a")], {"a"}, AuthenticationException("wrong password"))
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(recipes.LoginError, match=r"remote 'a' .*wrong password") as exc:
        recipes._usable_remotes()
    assert isinstance(exc.value.__cause__, AuthenticationException)


def test_usable_remotes_keeps_a_403_as_itself_by_default(monkeypatch):
    # _cli has its own, more specific hint for a 403
    api = _AuthFailsFor([Remote("a", "http://a")], {"a"}, ForbiddenException("403"))
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    with pytest.raises(ForbiddenException):
        recipes._usable_remotes()


@pytest.mark.parametrize("may_fail", [False, True], ids=["auth-true", "may-fail"])
@pytest.mark.parametrize("error", UNAVAILABLE_ERRORS, ids=["retried", "not-retried"])
def test_usable_remotes_skips_an_unavailable_remote_in_every_mode(monkeypatch, capsys, error, may_fail):
    remotes = [Remote("a", "http://a"), Remote("mirror", "http://mirror")]
    api = _AuthFailsFor(remotes, {"mirror"}, error)
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_auth_may_fail", may_fail)

    assert [r.name for r in recipes._usable_remotes()] == ["a"]
    assert recipes._unavailable_remotes == {"mirror"}
    out = capsys.readouterr().out
    assert "skipping conan remote 'mirror' (http://mirror): temporarily unavailable (HTTP 503)" in out
    assert "conan.authentication: false" in out


def test_usable_remotes_does_not_retry_a_remote_already_found_unavailable(monkeypatch):
    api = FakeRemotesAPI([Remote("a", "http://a"), Remote("mirror", "http://mirror")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes._unavailable_remotes.add("mirror")
    assert [r.name for r in recipes._usable_remotes()] == ["a"]
    assert api.auth_calls == [("a", False)]


@pytest.mark.parametrize(
    "error",
    [ConanException("Server exception 500: oops"), ConanException("too many 502 error responses"), EOFError()],
    ids=["500", "502", "eof"],
)
def test_other_errors_are_not_unavailable(error):
    assert not recipes._is_unavailable(error)


@pytest.mark.parametrize(
    "error",
    [AuthenticationException("wrong password"), ForbiddenException("403"), ConanConnectionError("unreachable")],
    ids=["auth", "forbidden", "connection"],
)
def test_usable_remotes_skips_a_failing_remote_when_may_fail(monkeypatch, capsys, error):
    remotes = [Remote("a", "http://a"), Remote("locked", "http://locked"), Remote("c", "http://c")]
    api = _AuthFailsFor(remotes, {"locked"}, error)
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert [r.name for r in recipes._usable_remotes()] == ["a", "c"]
    assert "skipping conan remote 'locked'" in capsys.readouterr().out


def test_usable_remotes_prompts_on_a_tty_when_may_fail(monkeypatch):
    # e.g. `docker compose run`, which gives the container a TTY: the
    # credentials prompt is still asked, and a working login keeps the remote
    api = _AuthFailsFor(
        [Remote("a", "http://a"), Remote("locked", "http://locked")], {"locked"}, AuthenticationException("x")
    )
    prompted = []
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    assert [r.name for r in recipes._usable_remotes()] == ["a", "locked"]
    assert prompted == ["locked"]


@pytest.mark.parametrize(
    "prompt_error",
    [AuthenticationException("Wrong user or password"), EOFError()],
    ids=["wrong-login", "eof"],
)
def test_usable_remotes_skips_a_remote_whose_prompted_login_fails_too(monkeypatch, capsys, prompt_error):
    api = _AuthFailsFor(
        [Remote("a", "http://a"), Remote("locked", "http://locked")], {"locked"}, AuthenticationException("x")
    )

    def failing_prompt(remote):
        raise prompt_error

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_prompt_and_login", failing_prompt)
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    assert [r.name for r in recipes._usable_remotes()] == ["a"]
    assert "skipping conan remote 'locked'" in capsys.readouterr().out


def test_usable_remotes_skips_without_prompting_when_not_a_tty(monkeypatch):
    api = _AuthFailsFor([Remote("locked", "http://locked")], {"locked"}, AuthenticationException("x"))
    prompted = []
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_prompt_and_login", lambda r: prompted.append(r.name))
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert recipes._usable_remotes() == []
    assert prompted == []


def test_usable_remotes_authenticates_each_remote_once_per_process(monkeypatch):
    api = FakeRemotesAPI([Remote("a", "http://a")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes._usable_remotes()
    recipes._usable_remotes()
    assert api.auth_calls == [("a", False)]


def test_usable_remotes_only_considers_the_names_prepare_found_usable(monkeypatch):
    # export after prepare: a remote prepare gave up on is not retried (or
    # prompted for) a second time
    api = FakeRemotesAPI([Remote("a", "http://a"), Remote("locked", "http://locked")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_usable_remote_names", {"a"})
    assert [r.name for r in recipes._usable_remotes()] == ["a"]
    assert api.auth_calls == [("a", False)]


def test_usable_remotes_file_lives_in_the_conan_home(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(home_folder=str(tmp_path)))
    assert _real_usable_remotes_file() == tmp_path / recipes.USABLE_REMOTES_FILENAME


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, (None, set())),
        ('{"usable": ["a", "b"], "unavailable": ["mirror"]}', ({"a", "b"}, {"mirror"})),
    ],
    ids=["missing", "written"],
)
def test_load_usable_remotes(tmp_path, content, expected):
    if content is not None:
        (tmp_path / recipes.USABLE_REMOTES_FILENAME).write_text(content)
    assert recipes._load_usable_remotes() == expected


def test_write_usable_remotes(monkeypatch, tmp_path):
    remotes = [Remote("a", "http://a"), Remote("b", "http://b"), Remote("mirror", "http://mirror")]
    api = _AuthFailsFor(remotes, {"b"}, ForbiddenException("403"))
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    recipes._unavailable_remotes.add("mirror")
    recipes.write_usable_remotes()
    record = json.loads((tmp_path / recipes.USABLE_REMOTES_FILENAME).read_text())
    assert record == {"usable": ["a"], "unavailable": ["mirror"]}


def test_write_usable_remotes_without_may_fail_contacts_no_remote(monkeypatch, tmp_path):
    # every remote not found unavailable has authenticated already (or the
    # run would have stopped) -- nothing to ask any of them again
    api = FakeRemotesAPI([Remote("a", "http://a"), Remote("mirror", "http://mirror")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes._unavailable_remotes.add("mirror")
    recipes.write_usable_remotes()
    record = json.loads((tmp_path / recipes.USABLE_REMOTES_FILENAME).read_text())
    assert record == {"usable": ["a"], "unavailable": ["mirror"]}
    assert api.auth_calls == []


def test_get_deps_graph_local(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    analyzed = []
    fake_api = types.SimpleNamespace(
        lockfile=types.SimpleNamespace(get_lockfile=lambda: "lockfile"),
        profiles=FakeProfilesAPI(),
        graph=types.SimpleNamespace(
            load_graph_consumer=lambda *a, **k: "deps-graph",
            analyze_binaries=lambda g, remotes: analyzed.append((g, remotes)),
        ),
    )
    monkeypatch.setattr(recipes, "conan_api", fake_api)

    result = recipes.get_deps_graph_local(Path("/recipe"), ref)
    assert result == "deps-graph"
    assert analyzed == [("deps-graph", [])]


def test_get_pref_from_ref_success(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    fake_pref = types.SimpleNamespace(package_id="pkgid", revision="rev")
    fake_graph = types.SimpleNamespace(root=types.SimpleNamespace(pref=fake_pref))
    monkeypatch.setattr(recipes, "get_deps_graph_local", lambda recipe_path, r: fake_graph)

    result = recipes.get_pref_from_ref(Path("/recipe"), ref)
    assert result.ref == ref
    assert result.package_id == "pkgid"
    assert result.revision == "rev"


def test_get_pref_from_ref_wraps_exception(monkeypatch, capsys):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")

    def boom(recipe_path, r):
        raise RuntimeError("no local export")

    monkeypatch.setattr(recipes, "get_deps_graph_local", boom)
    with pytest.raises(recipes.CatalogError, match="Cannot resolve"):
        recipes.get_pref_from_ref(Path("/recipe"), ref)
    assert "no local export" in capsys.readouterr().err


def test_get_recipes_prefs_uses_get_pref_from_ref(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")
    monkeypatch.setattr(recipes, "get_pref_from_ref", lambda recipe_path, r: pref)
    result = recipes.get_recipes_prefs({Path("/recipe"): ref})
    assert result == {Path("/recipe").absolute(): pref}


# --------------------------------------------------------------------------- #
# conan_list / find_pref
# --------------------------------------------------------------------------- #
def _pref():
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    return PkgReference(ref, "pkgid", "rev")


def test_conan_list_local_cache(monkeypatch):
    pref = _pref()

    class FakeList:
        def package_revisions(self, p, remote=None):
            assert remote is None
            return ["rev1"]

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(list=FakeList(), remotes=None))
    found, remote = recipes.conan_list(pref)
    assert found == ["rev1"]
    assert remote is None


def test_conan_list_skips_disabled_remote(monkeypatch):
    pref = _pref()
    disabled_remote = Remote("d", "http://d", disabled=True)

    class FakeRemotes:
        def get(self, name):
            return disabled_remote

    class FakeList:
        def package_revisions(self, p, remote=None):
            raise AssertionError("must not be called for a disabled remote")

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(list=FakeList(), remotes=FakeRemotes()))
    found, remote = recipes.conan_list(pref, remotes=["d"])
    assert found == []
    assert remote is None


def test_conan_list_not_found_tolerated(monkeypatch):
    pref = _pref()

    class FakeList:
        def package_revisions(self, p, remote=None):
            raise NotFoundException("nope")

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(list=FakeList(), remotes=None))
    found, remote = recipes.conan_list(pref)
    assert found == []
    assert remote is None


def test_find_pref_reports_local_and_remote(monkeypatch, capsys):
    pref = _pref()
    remote = Remote("r", "http://r")

    class FakeList:
        def package_revisions(self, p, remote=None):
            return ["rev"]

    class FakeRemotes:
        def get(self, name):
            return remote

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(list=FakeList(), remotes=FakeRemotes()))
    found, found_remote = recipes.find_pref(pref, remotes=["r"])
    assert found == ["rev"]
    assert found_remote is remote
    assert "already in remote" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# conan_remotes_list / conan_ensure_remotes / conan_enable_remotes / conan_login
# --------------------------------------------------------------------------- #
class FakeRemotesAPI:
    def __init__(self, remotes):
        self._remotes = list(remotes)
        self.added = []
        self.removed = []
        self.enabled = []
        self.disabled = []
        self.auth_calls = []
        self._user_info = {}

    def list(self, only_enabled=False):
        return list(self._remotes)

    def get(self, name):
        return next(r for r in self._remotes if r.name == name)

    def add(self, remote, index=0):
        self._remotes.insert(index, remote)
        self.added.append(remote)

    def remove(self, name):
        self._remotes = [r for r in self._remotes if r.name != name]
        self.removed.append(name)

    def enable(self, name):
        self.enabled.append(name)

    def disable(self, name):
        self.disabled.append(name)

    def user_auth(self, remote, force=False):
        self.auth_calls.append((remote.name, force))

    def user_info(self, remote):
        return self._user_info.get(remote.name, {})


class _AuthFailsFor(FakeRemotesAPI):
    """FakeRemotesAPI whose user_auth() raises ``error`` for the remotes named in ``failing``."""

    def __init__(self, remotes, failing, error):
        super().__init__(remotes)
        self._failing = set(failing)
        self._error = error

    def user_auth(self, remote, force=False):
        super().user_auth(remote, force)
        if remote.name in self._failing:
            raise self._error


def test_conan_remotes_list(monkeypatch):
    remotes = [Remote("a", "http://a"), Remote("b", "http://b")]
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=FakeRemotesAPI(remotes)))
    result = recipes.conan_remotes_list()
    assert set(result) == {"a", "b"}


def test_conan_ensure_remotes_adds_new(monkeypatch):
    api = FakeRemotesAPI([])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_ensure_remotes({"conancenter": {"url": "http://conancenter", "verify_ssl": False}})
    assert [r.name for r in api.added] == ["conancenter"]
    assert api.added[0].verify_ssl is False


def test_conan_ensure_remotes_renames_on_url_match(monkeypatch, capsys):
    api = FakeRemotesAPI([Remote("old-name", "http://conancenter")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_ensure_remotes({"new-name": {"url": "http://conancenter"}})
    assert "old-name" in api.removed
    assert "Old name: old-name" in capsys.readouterr().out
    assert any(r.name == "new-name" for r in api.added)


def test_conan_ensure_remotes_skips_when_already_correct(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter", verify_ssl=True)])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_ensure_remotes({"conancenter": {"url": "http://conancenter", "verify_ssl": True}})
    assert api.removed == []
    assert api.added == []


def test_conan_ensure_remotes_replaces_when_different(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter", verify_ssl=True)])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_ensure_remotes({"conancenter": {"url": "http://conancenter", "verify_ssl": False}})
    assert "conancenter" in api.removed
    assert any(r.name == "conancenter" and r.verify_ssl is False for r in api.added)


@pytest.mark.parametrize(
    "remote_name, config, env, expect_enabled, expect_disabled",
    [
        ("unmanaged", {}, None, [], ["unmanaged"]),
        ("conancenter", {"conancenter": {"enabled": True}}, None, ["conancenter"], []),
        ("conancenter", {"conancenter": {"enabled": True}}, "OFF", [], ["conancenter"]),
        ("conancenter", {}, "ON", ["conancenter"], []),
    ],
    ids=["default-disabled", "configured-enabled", "env-var-overrides-off", "env-var-overrides-on"],
)
def test_conan_enable_remotes(monkeypatch, remote_name, config, env, expect_enabled, expect_disabled):
    api = FakeRemotesAPI([Remote(remote_name, f"http://{remote_name}")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    if env is not None:
        monkeypatch.setenv(f"CONAN_REMOTE_ENABLE_{remote_name.upper()}", env)
    recipes.conan_enable_remotes(config)
    assert api.enabled == expect_enabled
    assert api.disabled == expect_disabled


def test_conan_login_skips_disabled(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter", disabled=True)])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"conancenter": {}})
    assert api.auth_calls == []


def test_conan_login_authenticates_when_needed(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"conancenter": {}})
    assert api.auth_calls == [("conancenter", True)]


def test_conan_login_skips_when_already_authenticated(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter")])
    api._user_info["conancenter"] = {"authenticated": True, "username": "u"}
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"conancenter": {}})
    assert api.auth_calls == []


def test_conan_login_force_reauthenticates(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter")])
    api._user_info["conancenter"] = {"authenticated": True, "username": "u"}
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"conancenter": {}}, force=True)
    assert api.auth_calls == [("conancenter", True)]


def test_conan_login_skips_when_username_unchanged(monkeypatch):
    api = FakeRemotesAPI([Remote("conancenter", "http://conancenter")])
    api._user_info["conancenter"] = {"username": "denver-bot"}
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setenv("CONAN_LOGIN_USERNAME_CONANCENTER", "denver-bot")
    recipes.conan_login({"conancenter": {}})
    assert api.auth_calls == []


def test_conan_login_warns_on_connection_error(monkeypatch, capsys):
    class FailingAuthRemotesAPI(FakeRemotesAPI):
        def user_auth(self, remote, force=False):
            raise ConanConnectionError("unreachable")

    api = FailingAuthRemotesAPI([Remote("conancenter", "http://conancenter")])
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"conancenter": {}})
    assert "Unable to connect" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error", [AuthenticationException("wrong"), ForbiddenException("403")], ids=["auth", "forbidden"]
)
def test_conan_login_auth_failure_raises_by_default(monkeypatch, error):
    api = _AuthFailsFor([Remote("conancenter", "http://conancenter")], {"conancenter"}, error)
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    expected = ForbiddenException if isinstance(error, ForbiddenException) else recipes.LoginError
    with pytest.raises(expected):
        recipes.conan_login({"conancenter": {}})


@pytest.mark.parametrize("error", UNAVAILABLE_ERRORS, ids=["retried", "not-retried"])
def test_conan_login_skips_an_unavailable_remote_by_default(monkeypatch, capsys, error):
    api = _AuthFailsFor([Remote("mirror", "http://mirror")], {"mirror"}, error)
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    recipes.conan_login({"mirror": {}})
    assert recipes._unavailable_remotes == {"mirror"}
    assert "temporarily unavailable (HTTP 503)" in capsys.readouterr().out


def test_conan_login_auth_failure_warns_when_may_fail(monkeypatch, capsys):
    api = _AuthFailsFor([Remote("conancenter", "http://conancenter")], {"conancenter"}, ForbiddenException("403"))
    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(remotes=api))
    monkeypatch.setattr(recipes, "_auth_may_fail", True)
    recipes.conan_login({"conancenter": {}})
    assert "skipping conan remote 'conancenter'" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# prepare()
# --------------------------------------------------------------------------- #
def test_prepare_noop_on_empty_remotes(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(recipes, "conan_ensure_remotes", lambda r: calls.append("ensure"))
    monkeypatch.setattr(recipes, "conan_enable_remotes", lambda r: calls.append("enable"))
    monkeypatch.setattr(recipes, "conan_login", lambda r, force=False: calls.append("login"))

    recipes.prepare({})

    assert calls == []
    assert "leaving conan's remote config as-is" in capsys.readouterr().out


def test_prepare_cleanup_runs_even_on_empty_remotes(monkeypatch):
    # cleanup=True (denver.toml's not conan.keep-remotes:, default on) treats
    # {} as the exhaustive list too, so conan_enable_remotes({}) still runs
    # -- it's what actually disables every already-present remote.
    calls = []
    monkeypatch.setattr(recipes, "conan_ensure_remotes", lambda r: calls.append(("ensure", r)))
    monkeypatch.setattr(recipes, "conan_enable_remotes", lambda r: calls.append(("enable", r)))
    monkeypatch.setattr(recipes, "conan_login", lambda r, force=False: calls.append(("login", r)))

    recipes.prepare({}, cleanup=True)

    assert [c[0] for c in calls] == ["ensure", "enable", "login"]
    assert all(c[1] == {} for c in calls)


def test_prepare_runs_all_three_when_remotes_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(recipes, "conan_ensure_remotes", lambda r: calls.append(("ensure", r)))
    monkeypatch.setattr(recipes, "conan_enable_remotes", lambda r: calls.append(("enable", r)))
    monkeypatch.setattr(recipes, "conan_login", lambda r, force=False: calls.append(("login", r)))

    remotes = {"conancenter": {"url": "http://conancenter"}}
    recipes.prepare(remotes)

    assert [c[0] for c in calls] == ["ensure", "enable", "login"]
    assert all(c[1] == remotes for c in calls)


def test_prepare_passes_force_to_login(monkeypatch):
    seen = {}
    monkeypatch.setattr(recipes, "conan_ensure_remotes", lambda r: None)
    monkeypatch.setattr(recipes, "conan_enable_remotes", lambda r: None)
    monkeypatch.setattr(recipes, "conan_login", lambda r, force=False: seen.setdefault("force", force))

    recipes.prepare({"conancenter": {}}, force=True)

    assert seen["force"] is True


def test_prepare_no_remote_reconciles_but_skips_login(monkeypatch):
    calls = []
    monkeypatch.setattr(recipes, "conan_ensure_remotes", lambda r: calls.append("ensure"))
    monkeypatch.setattr(recipes, "conan_enable_remotes", lambda r: calls.append("enable"))
    monkeypatch.setattr(recipes, "conan_login", lambda r, force=False: calls.append("login"))
    monkeypatch.setattr(recipes, "_no_remote", True)

    recipes.prepare({"conancenter": {"url": "http://conancenter"}})

    assert calls == ["ensure", "enable"]


# --------------------------------------------------------------------------- #
# generate_catalog: built in memory; a file only when --export-catalog says so
# --------------------------------------------------------------------------- #
def _stub_build_catalog(monkeypatch, references=None):
    """Stand in for build_catalog.build(), recording its args and what got written."""
    seen = {"written": []}

    class FakeCatalog:
        def get_references(self):
            return references if references is not None else {"foo/1.0": "foo/1.0@denver/snapshot"}

        def write_catalog(self, output_file_path):
            seen["written"].append(output_file_path)

    def fake_build(recipes_dirs, *, user, channel):
        seen.update(recipes_dirs=recipes_dirs, user=user, channel=channel)
        return FakeCatalog()

    monkeypatch.setattr(
        recipes,
        "_import_build_catalog",
        lambda: types.SimpleNamespace(build=fake_build),
    )
    return seen


def test_generate_catalog_returns_references_without_writing(monkeypatch, tmp_path):
    seen = _stub_build_catalog(monkeypatch)

    entries = recipes.generate_catalog([tmp_path / "recipes"])

    assert entries == {"foo/1.0": "foo/1.0@denver/snapshot"}
    assert seen["written"] == []  # no --export-catalog -> no catalog.yml anywhere
    assert seen["user"] == "denver"
    assert seen["channel"] == "snapshot"


def test_generate_catalog_writes_only_when_export_to_given(monkeypatch, tmp_path):
    seen = _stub_build_catalog(monkeypatch)
    export_to = tmp_path / "recipes" / "catalog.yml"

    recipes.generate_catalog([tmp_path / "recipes"], export_to=export_to)

    assert seen["written"] == [export_to]


def test_generate_catalog_passes_custom_user_channel(monkeypatch, tmp_path):
    seen = _stub_build_catalog(monkeypatch)

    recipes.generate_catalog([tmp_path / "recipes"], user="acme", channel="stable")

    assert seen["user"] == "acme"
    assert seen["channel"] == "stable"


def test_import_build_catalog_resolves_the_real_module():
    # both spellings are tried (script vs. package import) -- whichever wins,
    # it must be the real build_catalog with build() on it.
    assert callable(recipes._import_build_catalog().build)


# --------------------------------------------------------------------------- #
# export / test / create / upload / run_ci / needs_export
# --------------------------------------------------------------------------- #
def test_export_skips_when_already_cached(monkeypatch, capsys):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    monkeypatch.setattr(recipes, "get_cache_path", lambda r: Path("/cached"))
    recipes.export(Path("/recipe"), ref)
    assert "already exported" in capsys.readouterr().out


def test_export_exports_and_warns_if_verification_fails(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    calls = {"n": 0}

    def fake_get_cache_path(r):
        calls["n"] += 1  # never verified as cached -> triggers the warning

    exported = []
    monkeypatch.setattr(recipes, "get_cache_path", fake_get_cache_path)
    monkeypatch.setattr(
        recipes,
        "conan_api",
        types.SimpleNamespace(export=types.SimpleNamespace(export=lambda *a, **k: exported.append((a, k)))),
    )
    recipes.export(Path("/recipe"), ref)
    assert len(exported) == 1


def test_test_skips_when_no_test_package(tmp_path, capsys):
    recipe_path = tmp_path / "conanfile.py"
    recipe_path.write_text("x")
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")
    recipes.test(recipe_path, pref)
    assert "No test_package exists" in capsys.readouterr().out


def test_test_runs_run_test_when_test_package_present(monkeypatch, tmp_path):
    recipe_path = tmp_path / "conanfile.py"
    recipe_path.write_text("x")
    (tmp_path / "test_package").mkdir()
    (tmp_path / "test_package" / "conanfile.py").write_text("x")
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")

    monkeypatch.setattr(
        recipes,
        "conan_api",
        types.SimpleNamespace(
            profiles=FakeProfilesAPI(), lockfile=types.SimpleNamespace(get_lockfile=lambda: "lockfile")
        ),
    )
    called = []
    monkeypatch.setattr(recipes, "run_test", lambda *a, **k: called.append((a, k)))

    recipes.test(recipe_path, pref)
    assert len(called) == 1


def test_find_pref_reports_local_cache(monkeypatch, capsys):
    pref = _pref()

    class FakeList:
        def package_revisions(self, p, remote=None):
            return ["rev"]

    monkeypatch.setattr(recipes, "conan_api", types.SimpleNamespace(list=FakeList(), remotes=None))
    found, remote = recipes.find_pref(pref, remotes=None)
    assert found == ["rev"]
    assert remote is None
    assert "already in local cache" in capsys.readouterr().out


def test_needs_export_false_when_cached(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    monkeypatch.setattr(recipes, "get_cache_path", lambda r: Path("/cached"))
    assert recipes.needs_export(ref) is False


def test_needs_export_true_when_graph_errors(monkeypatch):
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    monkeypatch.setattr(recipes, "get_cache_path", lambda r: None)
    monkeypatch.setattr(recipes, "get_deps_graph_remote", lambda r: types.SimpleNamespace(error="boom"))
    assert recipes.needs_export(ref) is True


def test_needs_export_no_remote_never_queries_remotes(monkeypatch):
    # conan.authentication: false -> anything missing from the local cache
    # is exported, without logging in to (or even listing) any remote.
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    monkeypatch.setattr(recipes, "get_cache_path", lambda r: None)
    monkeypatch.setattr(recipes, "_no_remote", True)

    def unexpected(r):
        raise AssertionError("remote queried under --no-remote")

    monkeypatch.setattr(recipes, "get_deps_graph_remote", unexpected)
    assert recipes.needs_export(ref) is True


def _configured_remotes(*names):
    """What conan_remotes_list() returns for ``names``: a {name: Remote} of real conan Remotes.

    Real Remote objects rather than placeholders, because _validate_remote_name()
    hands back the registry's own ``Remote.name`` -- a stub without one would
    pass a test the production path could not.
    """
    return {name: Remote(name, f"http://{name}") for name in names}


def test_run_ci_skips_when_found_remote(monkeypatch):
    called = {"create": False}
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: (["found"], None))
    monkeypatch.setattr(recipes, "create", lambda *a: called.__setitem__("create", True))
    recipes.run_ci(Path("/r"), object(), ["remote"])
    assert called["create"] is False


def test_run_ci_creates_when_missing_everywhere(monkeypatch):
    results = iter([([], None), ([], None)])
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: next(results))
    created = []
    monkeypatch.setattr(recipes, "create", lambda recipe_path, pref: created.append((recipe_path, pref)))
    recipes.run_ci(Path("/r"), "pref", ["remote"])
    assert created == [(Path("/r"), "pref")]


def test_upload_skips_when_already_present(monkeypatch):
    called = []
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes("remote"))
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: (["found"], None))
    monkeypatch.setattr(recipes.subprocess, "run", lambda *a, **k: called.append(a))
    recipes.upload("pref", "remote")
    assert called == []


def test_upload_runs_conan_upload(monkeypatch):
    called = []
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes("remote"))
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: ([], None))
    monkeypatch.setattr(recipes.subprocess, "run", lambda cmd, check: called.append(cmd))
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")
    recipes.upload(pref, "remote")
    assert called
    assert called[0][0] == "conan"


def test_create_skips_and_tests_when_found(monkeypatch):
    tested = []
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: (["found"], None))
    monkeypatch.setattr(recipes, "test", lambda recipe_path, pref: tested.append(recipe_path))
    recipes.create(Path("/r"), "pref")
    assert tested == [Path("/r")]


def test_create_runs_conan_create_when_missing(monkeypatch):
    called = []
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: ([], None))
    monkeypatch.setattr(recipes.subprocess, "run", lambda cmd, check: called.append(cmd))
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")
    recipes.create(Path("/r"), pref)
    assert called
    assert called[0][0] == "conan"


def test_run_conan_cli_rejects_non_string_arg(monkeypatch):
    monkeypatch.setattr(recipes.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="invalid conan CLI arguments"):
        recipes._run_conan_cli("create", None)


def test_run_conan_cli_rejects_nul_byte_in_arg(monkeypatch):
    monkeypatch.setattr(recipes.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="invalid conan CLI arguments"):
        recipes._run_conan_cli("create", "foo\0bar")


@pytest.mark.parametrize(
    "args",
    [
        ("create", "/recipes/foo/1.0/conanfile.py", "--name=foo", "--version=1.0", "--test-missing"),
        ("upload", "-r=conancenter", "foo/1.0@denver/snapshot#rev:pkgid#prev"),
        # a path is whatever the filesystem allows -- spaces, dots, '+',
        # a Windows drive -- and none of it is option-shaped
        ("create", "/home/some one/recipes/foo+bar/1.0/conanfile.py"),
        ("create", r"C:\recipes\foo\1.0\conanfile.py"),
    ],
)
def test_validated_conan_argv_passes_what_this_module_builds(args):
    assert recipes._validated_conan_argv(args) == ["conan", *args]


@pytest.mark.parametrize(
    "arg",
    [
        "--config-install=evil",  # an option denver never passes, smuggled in as a value
        "-r=--config-install",  # ... or as one of denver's own options' values
        "--name=--version",
        "--name=",  # an option denver does pass, with nothing behind it
        "",  # not an argument at all
        "\x1b[31m",  # control characters: not a reference, not a path
        "foo\nbar",
    ],
)
def test_validated_conan_argv_rejects_anything_option_shaped_or_unprintable(arg):
    with pytest.raises(ValueError, match="invalid conan CLI arguments"):
        recipes._validated_conan_argv(("create", arg))


def test_reject_option_like_passes_through_normal_value():
    assert recipes._reject_option_like("/some/path", "recipe path") == "/some/path"


def test_reject_option_like_rejects_leading_dash():
    with pytest.raises(ValueError, match="looks like a CLI option"):
        recipes._reject_option_like("--evil", "recipe path")


def test_validate_remote_name_accepts_a_configured_remote(monkeypatch):
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes("conancenter", "team"))
    assert recipes._validate_remote_name("team") == "team"


def test_validate_remote_name_returns_conans_own_name_not_the_one_passed_in(monkeypatch):
    # equal strings, but what comes back is the registry's object -- that is
    # the name that goes on to be interpolated into conan's '-r=' argument
    registry = _configured_remotes("conancenter")
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: registry)
    assert recipes._validate_remote_name("".join(["conan", "center"])) is registry["conancenter"].name


def test_upload_passes_conans_own_remote_name(monkeypatch):
    called = []
    registry = _configured_remotes("conancenter")
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: registry)
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: ([], None))
    monkeypatch.setattr(recipes.subprocess, "run", lambda cmd, check: called.append(cmd))
    pref = PkgReference(RecipeReference.loads("foo/1.0@denver/snapshot"), "id", "rev")

    recipes.upload(pref, "".join(["conan", "center"]))

    assert f"-r={registry['conancenter'].name}" in called[0]


@pytest.mark.parametrize("name", ["typo", "-r", "team;rm -rf /", ""])
def test_validate_remote_name_rejects_anything_conan_does_not_know(monkeypatch, name):
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes("conancenter"))
    with pytest.raises(ValueError, match="not a configured conan remote"):
        recipes._validate_remote_name(name)


def test_upload_refuses_a_remote_conan_does_not_know(monkeypatch):
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes("conancenter"))
    monkeypatch.setattr(recipes, "find_pref", lambda pref, remotes: pytest.fail("must not look it up"))
    monkeypatch.setattr(recipes.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    pref = PkgReference(RecipeReference.loads("foo/1.0@denver/snapshot"), "id", "rev")
    with pytest.raises(ValueError, match="not a configured conan remote"):
        recipes.upload(pref, "--evil")


# --------------------------------------------------------------------------- #
# main(): argument parsing
# --------------------------------------------------------------------------- #
def _stub_pipeline(monkeypatch, remotes=("conancenter",)):
    """Neutralise everything main() calls beyond argument handling +
    prepare(), so these tests exercise CLI/config wiring only.

    ``remotes`` is what conan is pretended to have configured, which main()
    checks --remote against. Stubbed like everything else here: left real it
    would read the machine's own conan home, so whether a test passed would
    depend on which remotes the developer running it happens to have."""
    monkeypatch.setattr(recipes, "conan_remotes_list", lambda: _configured_remotes(*remotes))
    monkeypatch.setattr(recipes, "prepare", lambda remotes, cleanup=False, force=False: None)
    monkeypatch.setattr(recipes, "generate_catalog", lambda *a, **k: {})
    monkeypatch.setattr(recipes, "read_catalog", lambda *a: {})
    monkeypatch.setattr(recipes, "get_recipes_from_entries", lambda *a: {})
    monkeypatch.setattr(recipes, "handle_args_recipe", lambda *a: {})
    monkeypatch.setattr(recipes, "get_recipes_prefs", lambda *a: {})


def test_main_ci_without_remote_errors(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--ci"])
    with pytest.raises(SystemExit):
        recipes.main()
    assert "--remote" in capsys.readouterr().err


def test_main_rejects_a_remote_conan_does_not_know(monkeypatch, tmp_path, capsys):
    _stub_pipeline(monkeypatch)
    # '--remote=...' rather than two argv entries: argparse would read a
    # bare '--evil' as a missing value for --remote and never get this far
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--upload", "--remote=--evil", "--recipes-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        recipes.main()
    assert "not a configured conan remote" in capsys.readouterr().err


def test_main_upload_without_remote_errors(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--upload"])
    with pytest.raises(SystemExit):
        recipes.main()


def test_main_prepare_only_returns_before_generate(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    generate_called = []
    monkeypatch.setattr(recipes, "generate_catalog", lambda *a, **k: generate_called.append(a) or {})
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare"])
    recipes.main()
    assert generate_called == []


def test_main_prepare_writes_usable_remotes_under_may_fail(monkeypatch):
    _stub_pipeline(monkeypatch)
    written = []
    monkeypatch.setattr(recipes, "write_usable_remotes", lambda: written.append(True))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare", "--authentication-may-fail"])
    recipes.main()
    assert written == [True]
    assert recipes._auth_may_fail is True


@pytest.mark.parametrize("flags", [["--authentication-may-fail"], []], ids=["may-fail", "auth-true"])
def test_main_export_reads_the_remotes_record(monkeypatch, tmp_path, flags):
    _stub_pipeline(monkeypatch)
    written = []
    monkeypatch.setattr(recipes, "_load_usable_remotes", lambda: ({"a"}, {"mirror"}))
    monkeypatch.setattr(recipes, "write_usable_remotes", lambda: written.append(True))
    monkeypatch.setattr(sys, "argv", ["recipes.py", *flags, "--recipes-dir", str(tmp_path)])
    recipes.main()
    assert recipes._usable_remote_names == {"a"}
    assert recipes._unavailable_remotes == {"mirror"}
    assert written == []  # nothing new to record


def test_main_no_remote_ignores_the_remotes_record(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(recipes, "_load_usable_remotes", lambda: pytest.fail("must not be read"))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--no-remote", "--recipes-dir", str(tmp_path)])
    recipes.main()
    assert recipes._unavailable_remotes == set()


def test_main_export_records_a_remote_first_found_unavailable(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    written = []
    monkeypatch.setattr(recipes, "_process_catalog", lambda *a: recipes._unavailable_remotes.add("mirror"))
    monkeypatch.setattr(recipes, "write_usable_remotes", lambda: written.append(set(recipes._unavailable_remotes)))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--recipes-dir", str(tmp_path)])
    recipes.main()
    assert written == [{"mirror"}]


def test_main_prepare_writes_no_usable_remotes_without_the_flag(monkeypatch):
    _stub_pipeline(monkeypatch)
    written = []
    monkeypatch.setattr(recipes, "write_usable_remotes", lambda: written.append(True))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare"])
    recipes.main()
    assert written == []


def test_main_prepare_records_an_unavailable_remote_without_the_flag(monkeypatch):
    _stub_pipeline(monkeypatch)
    written = []
    monkeypatch.setattr(
        recipes, "prepare", lambda remotes, cleanup=False, force=False: recipes._unavailable_remotes.add("mirror")
    )
    monkeypatch.setattr(recipes, "write_usable_remotes", lambda: written.append(True))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare"])
    recipes.main()
    assert written == [True]


def test_main_loads_remotes_json(monkeypatch, tmp_path):
    remotes = {"conancenter": {"url": "http://conancenter"}}
    remotes_json = tmp_path / "remotes.json"
    remotes_json.write_text(json.dumps(remotes))

    seen = {}
    monkeypatch.setattr(recipes, "prepare", lambda r, cleanup=False, force=False: seen.setdefault("remotes", r))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare", f"--remotes-json={remotes_json}"])
    recipes.main()

    assert seen["remotes"] == remotes


def test_main_no_remotes_json_passes_empty_dict(monkeypatch):
    seen = {}
    monkeypatch.setattr(recipes, "prepare", lambda r, cleanup=False, force=False: seen.setdefault("remotes", r))
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--prepare"])
    recipes.main()
    assert seen["remotes"] == {}


def test_main_export_pipeline_default_catalog(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    monkeypatch.setattr(recipes, "get_recipes_from_entries", lambda *a: {Path("/r"): ref})
    monkeypatch.setattr(recipes, "needs_export", lambda r: True)
    exported = []
    monkeypatch.setattr(recipes, "export", lambda recipe_path, r: exported.append((recipe_path, r)))
    monkeypatch.setattr(recipes, "get_recipes_prefs", lambda refs: {})

    monkeypatch.setattr(sys, "argv", ["recipes.py", "--export", f"--recipes-dir={tmp_path}"])
    recipes.main()

    assert exported == [(Path("/r"), ref)]


def test_main_without_export_catalog_writes_nothing(monkeypatch, tmp_path):
    # the default: the catalog is built in memory and handed straight to the
    # export step -- no catalog.yml is written into the recipe dir.
    _stub_pipeline(monkeypatch)
    seen = {}
    monkeypatch.setattr(recipes, "generate_catalog", lambda d, **k: seen.update(k) or {})
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--export", f"--recipes-dir={tmp_path}"])
    recipes.main()

    assert seen["export_to"] is None
    assert list(tmp_path.iterdir()) == []


def test_main_export_catalog_is_passed_through(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    seen = {}
    monkeypatch.setattr(recipes, "generate_catalog", lambda d, **k: seen.update(k) or {})
    export_to = tmp_path / "catalog.yml"
    monkeypatch.setattr(
        sys,
        "argv",
        ["recipes.py", "--export", f"--recipes-dir={tmp_path}", f"--export-catalog={export_to}"],
    )
    recipes.main()

    assert seen["export_to"] == export_to


def test_main_recipes_positional_uses_handle_args_recipe(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    handled = []
    monkeypatch.setattr(recipes, "handle_args_recipe", lambda all_recipes, recipes: handled.append(recipes) or {})
    monkeypatch.setattr(sys, "argv", ["recipes.py", f"--recipes-dir={tmp_path}", "foo/1.0"])
    recipes.main()
    assert handled == [["foo/1.0"]]


def test_main_no_generate_reads_catalog_instead_of_generating(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    generated, read = [], []
    monkeypatch.setattr(recipes, "generate_catalog", lambda *a, **k: generated.append(a) or {})
    monkeypatch.setattr(recipes, "read_catalog", lambda p: read.append(p) or {})
    catalog_yml = tmp_path / "catalog.yml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recipes.py",
            "--no-generate",
            f"--recipes-dir={tmp_path}",
            f"--catalog-yml={catalog_yml}",
        ],
    )
    recipes.main()
    assert generated == []
    assert read == [catalog_yml.resolve()]


def test_main_requires_recipes_dir_without_prepare(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["recipes.py"])
    with pytest.raises(SystemExit):
        recipes.main()
    assert "--recipes-dir" in capsys.readouterr().err


def test_main_no_generate_requires_catalog_yml(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["recipes.py", "--no-generate", f"--recipes-dir={tmp_path}"])
    with pytest.raises(SystemExit):
        recipes.main()
    assert "--no-generate needs --catalog-yml" in capsys.readouterr().err


def test_main_catalog_yml_without_no_generate_errors(monkeypatch, tmp_path, capsys):
    # --catalog-yml is an *input* now; writing the generated one is
    # --export-catalog's job, so mixing them up is rejected outright.
    monkeypatch.setattr(
        sys,
        "argv",
        ["recipes.py", f"--recipes-dir={tmp_path}", f"--catalog-yml={tmp_path / 'catalog.yml'}"],
    )
    with pytest.raises(SystemExit):
        recipes.main()
    assert "--export-catalog" in capsys.readouterr().err


def test_main_ci_and_upload_flow(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    ref = RecipeReference.loads("foo/1.0@denver/snapshot")
    pref = PkgReference(ref, "id", "rev")
    monkeypatch.setattr(recipes, "get_recipes_from_entries", lambda *a: {Path("/r"): ref})
    monkeypatch.setattr(recipes, "get_recipes_prefs", lambda refs: {Path("/r"): pref})
    ci_calls, upload_calls, create_calls = [], [], []
    monkeypatch.setattr(recipes, "run_ci", lambda recipe_path, p, remotes: ci_calls.append((recipe_path, p, remotes)))
    monkeypatch.setattr(recipes, "upload", lambda p, remote: upload_calls.append((p, remote)))
    monkeypatch.setattr(recipes, "create", lambda recipe_path, p: create_calls.append((recipe_path, p)))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recipes.py",
            "--create",
            "--ci",
            "--upload",
            "--remote=conancenter",
            f"--recipes-dir={tmp_path}",
        ],
    )
    recipes.main()

    assert create_calls == [(Path("/r"), pref)]
    assert ci_calls == [(Path("/r"), pref, ["conancenter"])]
    assert upload_calls == [(pref, "conancenter")]


def test_main_base_classes_dir_explicit(monkeypatch, tmp_path):
    _stub_pipeline(monkeypatch)
    base_classes = tmp_path / "bc"
    base_classes.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recipes.py",
            "--prepare",
            f"--base-classes-dir={base_classes}",
        ],
    )
    recipes.main()
    assert str(base_classes.resolve()) in sys.path


def test_main_base_classes_dir_repeatable(monkeypatch, tmp_path):
    # --base-classes-dir is repeatable (denver.yml's 'base-classes:' is a
    # list): every dir lands on sys.path/PYTHONPATH, first one first.
    _stub_pipeline(monkeypatch)
    first = tmp_path / "bc-own"
    second = tmp_path / "bc-shared"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("PYTHONPATH", "/pre-existing")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recipes.py",
            "--prepare",
            f"--base-classes-dir={first}",
            f"--base-classes-dir={second}",
        ],
    )
    recipes.main()
    assert sys.path[:2] == [str(first.resolve()), str(second.resolve())]
    assert os.environ["PYTHONPATH"] == f"/pre-existing:{first.resolve()}:{second.resolve()}"


def test_get_recipes_from_entries_finds_recipes_wherever_they_live(tmp_path):
    # a recipe-dirs entry may be a whole tree or a single recipe -- both are
    # located by their conandata.yml, not by assuming <dir>/<name>/<version>.
    tree = tmp_path / "recipes"
    single = tmp_path / "shared" / "recipes" / "cmake"
    for recipe_dir in (tree / "foo" / "1.0", single / "3.31.0"):
        recipe_dir.mkdir(parents=True)
        (recipe_dir / "conandata.yml").write_text("{}")

    recipes_ref = recipes.get_recipes_from_entries(
        [tree, single],
        {"foo/1.0": "foo/1.0@denver/snapshot", "cmake/3.31.0": "cmake/3.31.0@denver/snapshot"},
    )

    assert set(recipes_ref) == {
        (tree / "foo" / "1.0" / "conanfile.py").absolute(),
        (single / "3.31.0" / "conanfile.py").absolute(),
    }


def test_get_recipes_from_entries_falls_back_for_unknown_recipe(tmp_path):
    # a stale checked-in catalog (--no-generate) names something no dir holds:
    # the path still points somewhere concrete, for the error further down.
    recipes_ref = recipes.get_recipes_from_entries([tmp_path], {"gone/9.9": "gone/9.9@denver/snapshot"})
    assert next(iter(recipes_ref)) == (tmp_path / "gone" / "9.9" / "conanfile.py").absolute()


def test_cli_reports_forbidden_without_traceback(monkeypatch, capsys):
    def forbidden():
        raise recipes.ForbiddenException("Permission denied for user: 'None': 403: Forbidden. [Remote: mirror]")

    monkeypatch.setattr(recipes, "main", forbidden)
    with pytest.raises(SystemExit) as exc:
        recipes._cli()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR: Permission denied for user: 'None'" in err
    assert "conan remote login" in err
    assert "`-c conan.authentication=false`" in err
    assert "Traceback" not in err


def test_cli_reports_a_login_error_with_the_local_cache_hint(monkeypatch, capsys):
    def failing_login():
        raise recipes.LoginError("authenticating conan remote 'mirror' (http://mirror) failed: wrong password")

    monkeypatch.setattr(recipes, "main", failing_login)
    with pytest.raises(SystemExit) as exc:
        recipes._cli()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR: authenticating conan remote 'mirror'" in err
    assert f"Hint: log in with 'conan remote login <remote>', or {recipes.LOCAL_CACHE_HINT}." in err


def test_cli_says_which_remotes_were_unavailable_when_the_local_cache_falls_short(monkeypatch, capsys):
    def missing_from_cache():
        recipes._unavailable_remotes.add("mirror")
        raise recipes.CatalogError("Cannot resolve foo/1.0. Please export all recipes to local cache.")

    monkeypatch.setattr(recipes, "main", missing_from_cache)
    with pytest.raises(SystemExit):
        recipes._cli()
    err = capsys.readouterr().err
    assert "ERROR: Cannot resolve foo/1.0" in err
    assert "conan remote(s) 'mirror' answered HTTP 503 (temporarily unavailable)" in err


def test_cli_reports_catalog_error(monkeypatch, capsys):
    def broken():
        raise recipes.CatalogError("Recipe path /nope does not exist!")

    monkeypatch.setattr(recipes, "main", broken)
    with pytest.raises(SystemExit) as exc:
        recipes._cli()
    assert exc.value.code == 1
    assert capsys.readouterr().err == "ERROR: Recipe path /nope does not exist!\n"


def test_cli_reports_get_rrev_error_without_traceback(monkeypatch, capsys):
    rrev_error = recipes._import_build_catalog().get_rrev.GetRREVError

    def broken():
        raise rrev_error("Error: 'url' must be specified for x.sh in /recipe")

    monkeypatch.setattr(recipes, "main", broken)
    with pytest.raises(SystemExit) as exc:
        recipes._cli()
    assert exc.value.code == 1
    assert capsys.readouterr().err == "ERROR: Error: 'url' must be specified for x.sh in /recipe\n"


def test_cli_lets_unrelated_errors_through(monkeypatch):
    def broken():
        raise RuntimeError("a genuine bug")

    monkeypatch.setattr(recipes, "main", broken)
    with pytest.raises(RuntimeError, match="a genuine bug"):
        recipes._cli()
