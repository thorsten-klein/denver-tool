"""Tests for denver.py's '.init: projects:' (run by the git/download providers before the config is resolved)."""

import io
import os
import subprocess
import tarfile
import textwrap

import pytest
import yaml

import denver
import denver_providers.download as download_provider
from denver_errors import DenverError

BASE_MANIFEST = "stages: [{name}]\n{name}:\n  provider: custom\n  cmd: 'true'\n"


def _git(*args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **env}
    ).stdout.strip()


def _commit(repo, stage):
    (repo / "denver.yml").write_text(BASE_MANIFEST.format(name=stage))
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", stage, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def remote(tmp_path):
    repo = tmp_path / "remote"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    return repo


def _write_env(env_dir, projects, extra="import: [ext]\n"):
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "denver.yml").write_text(yaml.safe_dump({".init": {"projects": projects}}) + extra)
    return env_dir / "denver.yml"


def _git_project(remote, revision="main", **extra):
    return {"provider": "git", "path": "ext", "url": str(remote), "revision": revision, **extra}


def _imported_stages(config, **ctx_flags):
    denver.fetch_init_projects(config, ctx_flags)
    return denver.load_config(config)["stages"]


# ---- provider: git ------------------------------------------------------------#
def test_git_project_is_cloned_and_importable(tmp_path, remote):
    _commit(remote, "one")
    config = _write_env(tmp_path / "env", [_git_project(remote, **{"clone-opts": ["--no-tags"]})])
    assert _imported_stages(config) == ["one"]
    assert (tmp_path / "env" / "ext" / ".git").is_dir()


def test_git_project_tag_revision(tmp_path, remote):
    _commit(remote, "one")
    _git("tag", "v1", cwd=remote)
    _commit(remote, "two")
    assert _imported_stages(_write_env(tmp_path / "env", [_git_project(remote, "v1")])) == ["one"]


def test_git_project_branch_follows_its_fetched_tip(tmp_path, remote):
    _commit(remote, "one")
    config = _write_env(tmp_path / "env", [_git_project(remote)])
    assert _imported_stages(config) == ["one"]
    _commit(remote, "two")
    assert _imported_stages(config) == ["two"]


def test_git_project_not_fetched_under_fast(tmp_path, remote):
    _commit(remote, "one")
    config = _write_env(tmp_path / "env", [_git_project(remote)])
    _imported_stages(config)
    _commit(remote, "two")
    assert _imported_stages(config, fast=True) == ["one"]


def test_git_project_path_interpolates_env_workdir(tmp_path, remote):
    _commit(remote, "one")
    project = {**_git_project(remote), "path": "${DENVER_ENV_WORKDIR}/ext"}
    config = _write_env(tmp_path / "env", [project], extra="import: [.denver/denver/ext]\n")
    assert _imported_stages(config) == ["one"]


def test_imported_layers_own_init_projects_run_too(tmp_path, remote):
    _commit(remote, "nested")
    _write_env(tmp_path / "base", [_git_project(remote)])
    config = _write_env(tmp_path / "env", [], extra="import: [../base]\n")
    assert _imported_stages(config) == ["nested"]


def test_git_project_unknown_revision_dies(tmp_path, remote):
    _commit(remote, "one")
    with pytest.raises(DenverError):
        denver.fetch_init_projects(_write_env(tmp_path / "env", [_git_project(remote, "no-such-ref")]))


# ---- provider: download -------------------------------------------------------#
def _tarball(manifest):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = manifest.encode()
        info = tarfile.TarInfo("denver.yml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_download_project_is_unpacked_and_importable(tmp_path, monkeypatch):
    payload = _tarball(BASE_MANIFEST.format(name="downloaded"))
    monkeypatch.setattr(download_provider, "urlopen", lambda url, *a, **kw: io.BytesIO(payload))
    project = {
        "provider": "download",
        "packages": [{"name": "base", "url": "https://example.com/base.tar.gz", "unpack-dir": "ext"}],
    }
    assert _imported_stages(_write_env(tmp_path / "env", [project])) == ["downloaded"]


# ---- validation ---------------------------------------------------------------#
@pytest.mark.parametrize(
    "init",
    [
        ["not-a-mapping"],
        {"projectz": []},
        {"projects": "p"},
        {"projects": ["p"]},
        {"projects": [{"path": "p", "url": "u", "revision": "main"}]},
        {"projects": [{"provider": "uv"}]},
        {"projects": [{"provider": "git", "path": "p", "url": "u", "revision": "main", "env": {}}]},
        {"projects": [{"provider": "git", "path": "p", "url": "u", "revision": "main", "fetch-opts": "-q"}]},
        {"projects": [{"provider": "git", "path": "p", "url": "u"}]},
    ],
    ids=[
        "not-mapping",
        "unknown-key",
        "projects-not-list",
        "entry-not-mapping",
        "no-provider",
        "provider-not-allowed",
        "generic-stage-key",
        "opts-not-list",
        "git-missing-revision",
    ],
)
def test_malformed_init_dies(tmp_path, init):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "denver.yml").write_text(yaml.safe_dump({".init": init}))
    with pytest.raises(DenverError):
        denver.fetch_init_projects(env_dir / "denver.yml")
    assert not (env_dir / "p").exists()


def test_init_is_a_known_top_level_key():
    denver.validate_top_level_keys({".init": {"projects": []}, "stages": []})


# ---- denver run ---------------------------------------------------------------#
def test_denver_run_brings_projects_in_before_resolving_imports(tmp_path, remote, capsys):
    _commit(remote, "one")
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "denver.yml").write_text(
        textwrap.dedent(f"""\
        .init:
          projects:
          - provider: git
            path: ext
            url: {remote}
            revision: main
        import: [ext]
        """)
    )
    assert denver.main(["run", str(env_dir), "--show-config"]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["stages"] == ["one"]


def test_denver_run_fast_does_not_fetch(tmp_path, remote):
    _commit(remote, "one")
    config = _write_env(tmp_path / "env", [_git_project(remote)])
    _imported_stages(config)
    _commit(remote, "two")
    assert denver.main(["run", str(config), "--fast", "--show-config"]) == 0
    assert denver.load_config(config)["stages"] == ["one"]


def test_git_project_remote_url_change_is_followed(tmp_path, remote):
    _commit(remote, "one")
    config = _write_env(tmp_path / "env", [_git_project(remote)])
    _imported_stages(config)
    moved = tmp_path / "moved"
    _git("clone", "-q", "--bare", str(remote), str(moved), cwd=tmp_path)
    _commit(remote, "two")  # only in the old remote
    config = _write_env(tmp_path / "env", [_git_project(moved)])
    assert _imported_stages(config) == ["one"]
    assert _git("config", "--get", "remote.origin.url", cwd=tmp_path / "env" / "ext") == str(moved)


def test_git_project_local_tag_needs_no_fetch(tmp_path, remote):
    _commit(remote, "one")
    _git("tag", "v1", cwd=remote)
    _commit(remote, "two")
    _git("tag", "v2", cwd=remote)
    config = _write_env(tmp_path / "env", [_git_project(remote, "v2")])
    _imported_stages(config)
    (tmp_path / "remote").rename(tmp_path / "gone")  # any fetch would now fail
    config = _write_env(tmp_path / "env", [_git_project(remote, "v1")])
    assert _imported_stages(config) == ["one"]
