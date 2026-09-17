"""End-to-end golden-file tests: for each real env under examples/, --show-config-full's
output must match a checked-in golden file.

This is the gap the rest of the suite leaves open -- everything else mocks
subprocess.run/shutil.which/os.execvpe and drives providers through small,
synthetic configs. Nothing takes a real denver.yml from examples/, runs it
through the whole resolver, and checks the result against a known-good
snapshot -- so a resolver regression in a real env's config only shows up by
manually diffing --show-config output. These tests catch that automatically.

Only shutil.which (via the shared ``which`` fixture), the zephyr provider's
workspace-root lookups (WEST_TOPDIR, and the outermost-``.git`` walk its
'west-yml:' fallback uses independently of WEST_TOPDIR), whether this machine
looks like a container, and the running denver's own version (which the envs'
'denver-version:' pins are checked against) are faked, for determinism across
machines/CI -- everything else (conan recipe dirs,
the conanfile, patches files, ...) is resolved against the real examples/ tree,
so a real "file not found" in an env's own config still fails here.

test_regenerate_golden_files runs first and overwrites tests/golden/*.yml with
the live output, then fails if that leaves an *unstaged* change -- so a golden
gone stale (e.g. examples/*/denver.yml's 'denver-version:' pin moving without
tests/golden/ being updated to match) is caught the same way it would be by a
developer regenerating by hand and running `git status`. test_show_config_matches_golden
then re-checks the (now current) golden as a second, more direct assertion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import denver
import denver_providers.context as context_provider
import denver_providers.zephyr as zephyr_provider

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
FAKE_WEST_TOPDIR = "/fake-west-topdir"
# newer than any 'denver-version:' an example can plausibly pin
FAKE_DENVER_VERSION = "999.0.0"


def _tracked_env_names():
    """Env dir names with a denver.yml tracked in git -- not e.g. a developer's local scratch env under examples/."""
    result = subprocess.run(
        ["git", "ls-files", "examples/*/denver.yml"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return sorted(Path(line).parent.name for line in result.stdout.splitlines() if line)


def _normalize(text):
    """Replace this checkout's own absolute path with a portable placeholder."""
    return text.replace(str(REPO_ROOT), "<REPO>")


def _resolve_show_config_full(env_name, monkeypatch, capsys, which):
    """--show-config-full's (normalized) output for a tracked example env.

    Shared by test_regenerate_golden_files and test_show_config_matches_golden
    so both fake the same things the same way.
    """
    monkeypatch.setenv("WEST_TOPDIR", FAKE_WEST_TOPDIR)
    # zephyr's 'west-yml:' fallback walks for the outermost enclosing .git
    # independently of WEST_TOPDIR -- real on this machine (this checkout may
    # itself be nested inside another .git, e.g. via git-nested), so it's
    # faked directly rather than left to depend on the surrounding filesystem.
    monkeypatch.setattr(zephyr_provider, "find_outermost_in_parents", lambda start, name: Path(FAKE_WEST_TOPDIR))
    # the running denver's version, which the envs' 'denver-version:' pins are
    # checked against, comes from this checkout's git tags -- absent in a
    # shallow clone or a tarball (CI's default checkout has no tags at all).
    # It's also legitimately *behind* an example's pin between the pin bump
    # and the release it anticipates. Neither says anything about whether
    # --show-config still resolves the env correctly, so it's faked.
    monkeypatch.setattr(denver, "package_version", lambda: FAKE_DENVER_VERSION)
    # 'no-index: auto' is shown as the literal 'auto' in --show-config (it resolves to a
    # bool lazily, per uv command, not here) -- but other resolved fields still depend on
    # whether this looks like a container (e.g. the uv provider's per-venv host suffix), so
    # an unfaked answer here would still make those depend on the machine running the test
    # rather than on the env's config. It is a real container check against the real
    # filesystem -- and it says "yes" on more than just docker: WSL2 with systemd, for
    # instance, has /run/systemd/container, one of the markers it looks for. Pinned to the
    # host answer, which is what the golden files record.
    monkeypatch.setattr(context_provider, "in_container", lambda env=None: False)

    env_dir = REPO_ROOT / "examples" / env_name
    # --show-config-full, not the (now minimal-by-default) plain --show-config:
    # this test exists to catch a resolver dropping/changing a default, which
    # a minimal render would just hide by omitting the key entirely.
    assert denver.main(["run", str(env_dir), "--show-config-full"]) == 0
    return _normalize(capsys.readouterr().out)


@pytest.mark.parametrize("env_name", _tracked_env_names())
def test_regenerate_golden_files(env_name, capsys, monkeypatch, which):
    """Regenerate tests/golden/<env_name>.yml from the live resolver, then require
    that doing so left no *unstaged* change behind.

    A real resolver regression, or an example that changed (e.g. a
    'denver-version:' pin bump) without tests/golden/ being updated to match,
    shows up here as a diff the working tree has that the index doesn't --
    exactly what `git status` would show a developer who just regenerated by
    hand. A change that's already staged (`git add`ed) is treated as
    reviewed and intentional, and passes.
    """
    actual = _resolve_show_config_full(env_name, monkeypatch, capsys, which)
    golden_path = GOLDEN_DIR / f"{env_name}.yml"
    golden_path.write_text(actual)

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(golden_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # porcelain format is "XY <path>": X is index-vs-HEAD, Y is worktree-vs-index.
    # a blank Y means the working tree (what we just wrote) matches the index --
    # whether or not the index itself is staged relative to HEAD -- so only a
    # non-blank Y is an unacknowledged change.
    assert not status or status[1] == " ", (
        f"tests/golden/{env_name}.yml no longer matched the live resolver output and was "
        f"just regenerated, but the update isn't staged -- review `git diff -- {golden_path}` "
        f"and `git add` it if the change is intentional:\n{status}"
    )


@pytest.mark.parametrize("env_name", _tracked_env_names())
def test_show_config_matches_golden(env_name, capsys, monkeypatch, which):
    actual = _resolve_show_config_full(env_name, monkeypatch, capsys, which)

    golden_path = GOLDEN_DIR / f"{env_name}.yml"
    expected = golden_path.read_text()
    assert actual == expected, (
        f"--show-config-full for examples/{env_name} no longer matches {golden_path} -- if this change is "
        f"intentional, regenerate it (see this test module's docstring)."
    )
