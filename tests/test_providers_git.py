"""Tests for providers.git.GitProvider.

Activation (env:/env-prepend:/env-append:) is not this provider's own
concern any more -- it's the generic per-stage mechanism every provider
gets, exercised in test_denver_orchestration.py instead.
"""

from __future__ import annotations

import pytest

from denver_errors import DenverError
from denver_providers.git import GitProvider

URL = "https://example.invalid/repo.git"
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


def config_for(stage_cfg, stage="pico-sdk"):
    return {stage: {"provider": "git", **stage_cfg}}


def run_git(config, ctx, stage="pico-sdk"):
    """Resolve this stage's defaults the way denver.py does, then run its setup()."""
    provider = GitProvider(config)
    provider.stage = stage
    config[stage] = GitProvider.resolve_defaults(ctx, config.get(stage) or {}, config)
    provider.setup(ctx)
    return provider


def resolved(ctx, entry):
    return GitProvider.resolve_defaults(ctx, entry, {})


def make_checkout(path, *, git=True):
    path.mkdir(parents=True, exist_ok=True)
    if git:
        (path / ".git").mkdir()
    return path


def fake_proc(stdout="", returncode=0):
    return type("R", (), {"stdout": stdout, "returncode": returncode})()


# ---- config defaults --------------------------------------------------------#
def test_defaults_fill_every_key(make_context):
    ctx = make_context()
    cfg = resolved(ctx, {"url": URL, "path": "checkout", "revision": "1.0"})
    assert set(cfg) == set(GitProvider.KEYS)
    assert cfg["remote"] == "origin"
    assert cfg["submodules"] is False


def test_path_resolved_absolute(make_context):
    ctx = make_context()
    cfg = resolved(ctx, {"url": URL, "path": "checkout", "revision": "1.0"})
    assert cfg["path"] == str(ctx.env_dir / "checkout")


def test_url_and_revision_interpolated(make_context):
    ctx = make_context(env={"PIN": "2.3.0"})
    cfg = resolved(ctx, {"url": URL, "path": "checkout", "revision": "${PIN}"})
    assert cfg["revision"] == "2.3.0"


@pytest.mark.parametrize("missing", ["url", "path", "revision"])
def test_missing_required_key_dies(make_context, missing):
    ctx = make_context()
    entry = {"url": URL, "path": "checkout", "revision": "1.0"}
    del entry[missing]
    with pytest.raises(DenverError):
        resolved(ctx, entry)


def test_submodules_must_be_bool(make_context):
    ctx = make_context()
    with pytest.raises(DenverError):
        resolved(ctx, {"url": URL, "path": "checkout", "revision": "1.0", "submodules": "true"})


def test_remote_must_be_a_string(make_context):
    ctx = make_context()
    with pytest.raises(DenverError):
        resolved(ctx, {"url": URL, "path": "checkout", "revision": "1.0", "remote": 1})


# ---- setup(): fresh clone ----------------------------------------------------#
def test_fresh_path_clones_and_checks_out_without_a_fetch(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = ctx.env_dir / "checkout"
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)

    argvs = run_recorder.argvs()
    assert ["git", "clone", "--origin", "origin", "--", URL, str(path)] in argvs
    assert not any("fetch" in a for a in argvs)  # a fresh clone already has everything
    assert any(a[:5] == ["git", "-C", str(path), "checkout", "--detach"] for a in argvs)


def test_already_cloned_is_not_re_cloned(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)

    assert not any(a[:2] == ["git", "clone"] for a in run_recorder.argvs())


def test_already_at_revision_skips_checkout(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_OLD + "\n")
    run_recorder.responses["rev-parse HEAD"] = lambda cmd: fake_proc(stdout=SHA_OLD + "\n")
    run_git(config, ctx)
    assert not any(a[:4] == ["git", "-C", str(path), "checkout"] for a in run_recorder.argvs())


def test_different_commit_re_checks_out(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_recorder.responses["rev-parse HEAD"] = lambda cmd: fake_proc(stdout=SHA_OLD + "\n")
    run_git(config, ctx)
    assert ["git", "-C", str(path), "checkout", "--detach", SHA_NEW] in run_recorder.argvs()


def test_revision_found_only_after_targeted_fetch(make_context, run_recorder):
    # 'fetch --tags' alone doesn't see a raw commit sha that isn't the tip of
    # any branch/tag -- resolves only once the second, targeted 'fetch
    # <remote> <revision>' has run (see _resolve_revision).
    config = config_for({"url": URL, "path": "checkout", "revision": "deadbeef"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    calls = {"n": 0}

    def rev_parse_response(cmd):
        calls["n"] += 1
        # the first two lookups (remote branch, then the plain revision) precede the targeted fetch
        return fake_proc(returncode=1) if calls["n"] <= 2 else fake_proc(stdout=SHA_NEW + "\n")

    run_recorder.responses["rev-parse --verify -q"] = rev_parse_response
    run_git(config, ctx)
    argvs = run_recorder.argvs()
    assert ["git", "-C", str(path), "fetch", "origin", "deadbeef"] in argvs
    assert ["git", "-C", str(path), "checkout", "--detach", SHA_NEW] in argvs


def test_unresolvable_revision_dies(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "nope"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    with pytest.raises(DenverError):
        run_git(config, ctx)


# ---- --fast ------------------------------------------------------------------#
def test_fast_dies_when_never_checked_out(make_context):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config, fast=True)
    with pytest.raises(DenverError):
        run_git(config, ctx)


def test_fast_skips_git_entirely_when_already_checked_out(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config, fast=True)
    make_checkout(ctx.env_dir / "checkout")
    run_git(config, ctx)
    assert run_recorder.calls == []


# ---- submodules ---------------------------------------------------------------#
def test_submodules_true_runs_submodule_update(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0", "submodules": True})
    ctx = make_context(config=config)
    path = ctx.env_dir / "checkout"
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    argvs = run_recorder.argvs()
    assert ["git", "-C", str(path), "submodule", "sync"] in argvs
    assert ["git", "-C", str(path), "submodule", "update", "--init"] in argvs


def test_submodules_false_by_default(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    # checked as a distinct argv token, not a substring of the whole joined
    # command: the tmp env dir pytest hands this test is itself named
    # 'test_submodules_false_by_default0', which contains "submodule" too.
    assert not any("submodule" in a for a in run_recorder.argvs())


# ---- --force -------------------------------------------------------------------#
def test_force_resets_and_cleans_before_checkout(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config, force=True)
    path = make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_recorder.responses["rev-parse HEAD"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    argvs = run_recorder.argvs()
    assert ["git", "-C", str(path), "reset", "--hard"] in argvs
    assert ["git", "-C", str(path), "clean", "-fdx"] in argvs


# ---- --dry-run -------------------------------------------------------------------#
def test_dry_run_never_clones_or_checks_out(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config, dry_run=True)
    path = ctx.env_dir / "checkout"
    run_git(config, ctx)
    assert not any(a[:2] == ["git", "clone"] for a in run_recorder.argvs())
    assert not path.exists()


def test_clone_opts_are_passed_through(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0", "clone-opts": ["--depth", "1"]})
    ctx = make_context(config=config)
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    assert ["git", "clone", "--origin", "origin", "--depth", "1", "--", URL, str(ctx.env_dir / "checkout")] in (
        run_recorder.argvs()
    )


def test_fetch_opts_are_passed_through(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0", "fetch-opts": ["-q"]})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder, tag_after_fetch=True)
    run_git(config, ctx)
    assert ["git", "-C", str(path), "fetch", "--tags", "--prune", "-q", "origin"] in run_recorder.argvs()


def test_branch_revision_resolves_to_fetched_remote_tip_first(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "main"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["rev-parse --verify -q"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    first_lookup = next(argv for argv in run_recorder.argvs() if "--verify" in argv)
    assert first_lookup[-1] == "refs/remotes/origin/main^{commit}"


@pytest.mark.parametrize("key", ["clone-opts", "fetch-opts"])
def test_opts_must_be_a_list_of_strings(make_context, key):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0", key: "--depth 1"})
    ctx = make_context(config=config)
    with pytest.raises(DenverError):
        run_git(config, ctx)


# ---- setup(): existing checkout -- remote url, local revision, fetch ---------#
def _existing_remote(run_recorder, url):
    run_recorder.responses["config --get remote.origin.url"] = lambda cmd: fake_proc(stdout=url + "\n")


def _revisions(run_recorder, *, branch=False, tag_after_fetch=False):
    """Answer rev-parse like a checkout where 'revision:' is a branch, a local tag, or a tag only a fetch brings."""
    state = {"fetched": False}

    def rev_parse(cmd):
        ref = cmd[-1]
        if ref.startswith("refs/remotes/"):
            return fake_proc(stdout=SHA_NEW + "\n") if branch else fake_proc(returncode=1)
        if tag_after_fetch and not state["fetched"]:
            return fake_proc(returncode=1)
        return fake_proc(stdout=SHA_NEW + "\n")

    def fetch(cmd):
        state["fetched"] = True
        return fake_proc()

    run_recorder.responses["rev-parse --verify -q"] = rev_parse
    run_recorder.responses["fetch"] = fetch
    run_recorder.responses["rev-parse HEAD"] = lambda cmd: fake_proc(stdout=SHA_OLD + "\n")


def _fetched(run_recorder):
    return [a for a in run_recorder.argvs() if "fetch" in a]


def test_remote_url_unchanged_is_left_alone(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder)
    run_git(config, ctx)
    assert not any("remote" in a for a in run_recorder.argvs())


def test_remote_url_changed_is_updated_and_fetched(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, "https://old.invalid/repo.git")
    _revisions(run_recorder)
    run_git(config, ctx)
    argvs = run_recorder.argvs()
    assert ["git", "-C", str(path), "remote", "set-url", "origin", URL] in argvs
    assert _fetched(run_recorder)  # a locally known revision may come from the old remote


def test_missing_remote_is_added(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    run_recorder.responses["config --get remote.origin.url"] = lambda cmd: fake_proc(returncode=1)
    _revisions(run_recorder)
    run_git(config, ctx)
    assert ["git", "-C", str(path), "remote", "add", "origin", URL] in run_recorder.argvs()


def test_locally_available_tag_is_checked_out_without_a_fetch(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder)
    run_git(config, ctx)
    assert not _fetched(run_recorder)
    assert ["git", "-C", str(path), "checkout", "--detach", SHA_NEW] in run_recorder.argvs()


def test_revision_already_checked_out_needs_no_fetch_and_no_checkout(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder)
    run_recorder.responses["rev-parse HEAD"] = lambda cmd: fake_proc(stdout=SHA_NEW + "\n")
    run_git(config, ctx)
    assert not _fetched(run_recorder)
    assert not any("checkout" in a for a in run_recorder.argvs())


def test_revision_not_available_locally_is_fetched_first(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder, tag_after_fetch=True)
    run_git(config, ctx)
    argvs = run_recorder.argvs()
    fetch = ["git", "-C", str(path), "fetch", "--tags", "--prune", "origin"]
    checkout = ["git", "-C", str(path), "checkout", "--detach", SHA_NEW]
    assert argvs.index(fetch) < argvs.index(checkout)


def test_branch_revision_is_always_fetched(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "main"})
    ctx = make_context(config=config)
    make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder, branch=True)
    run_git(config, ctx)
    assert _fetched(run_recorder)


def test_force_always_fetches(make_context, run_recorder):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config, force=True)
    make_checkout(ctx.env_dir / "checkout")
    _existing_remote(run_recorder, URL)
    _revisions(run_recorder)
    run_git(config, ctx)
    assert _fetched(run_recorder)


@pytest.mark.parametrize("content", [False, True], ids=["empty-dir", "non-empty-dir"])
def test_existing_path_that_is_not_a_checkout_dies_without_running_git(make_context, run_recorder, content):
    config = config_for({"url": URL, "path": "checkout", "revision": "1.0"})
    ctx = make_context(config=config)
    path = make_checkout(ctx.env_dir / "checkout", git=False)
    if content:
        (path / "file").write_text("x")
    with pytest.raises(DenverError):
        run_git(config, ctx)
    assert not any(a[:1] == ["git"] for a in run_recorder.argvs())
