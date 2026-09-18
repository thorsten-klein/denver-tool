"""Repo-level invariants for ``denver.DEV_VERSION``.

DEV_VERSION (see its comment in ``src/denver.py``) is what an untagged
checkout reports instead of its stale ``git describe`` output, so that
running denver from source works at every commit rather than only after a
release. It is hand-maintained, so it is exactly the kind of thing that gets
forgotten.

* ``test_examples_run_from_a_checkout`` is the requirement itself, stated
  directly: every example must be runnable from this working tree, right
  now. It resolves the version this checkout really reports (DEV_VERSION
  applied or not) and checks every example's pin against it -- so it fails
  whether the cause was a bumped pin, a forgotten DEV_VERSION bump, or
  DEV_VERSION being switched off while something still needed it.
* ``test_dev_version_is_a_version`` only checks the constant parses. A
  DEV_VERSION behind the newest tag is fine: the tag has overtaken it and
  it simply has no effect until the next bump.

Unlike the rest of the suite these read the real repository (its tags, its
checked-in golden files) rather than synthetic fixtures -- that's the point:
the constant is only ever wrong *relative to this repo*.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import denver

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _tracked_env_names():
    """Env dir names with a denver.yml tracked in git -- mirrors test_golden_show_config."""
    result = subprocess.run(
        ["git", "ls-files", "examples/*/denver.yml"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return sorted(Path(line).parent.name for line in result.stdout.splitlines() if line)


def _git_describe():
    """This checkout's raw `git describe` output (``1.0.4`` or ``1.0.4-17-gabc1234``), or None.

    None whenever git can't answer -- a shallow/tagless clone, a source
    tarball, no git binary -- in which case there is no release history to
    judge DEV_VERSION against and the tests below skip rather than invent one.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--match", "*.*.*"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover -- environment-dependent
        return None
    return (completed.stdout.strip() or None) if completed.returncode == 0 else None


@pytest.mark.parametrize("env_name", _tracked_env_names())
def test_examples_run_from_a_checkout(env_name, monkeypatch):
    """Every example must run straight out of this working tree, tagged or not.

    An example's 'denver-version:' pin routinely names a release that is not
    tagged yet (see doc/development.md, "Releasing"), and DEV_VERSION is what
    honours it from source in the meantime. This asserts the outcome rather
    than the mechanism: whatever ``scm_version()`` reports for this checkout
    -- re-based onto DEV_VERSION or not -- has to satisfy every pin, or
    `python src/denver.py examples/<env>` is broken for everyone right now.

    The pin is read from the golden file, i.e. the *merged* config, so a pin
    an env only inherits through 'import:' is covered too.
    """
    config = yaml.safe_load((GOLDEN_DIR / f"{env_name}.yml").read_text())
    if config.get("denver-version") is None:
        pytest.skip(f"examples/{env_name} pins no 'denver-version:'")

    running = denver.scm_version()
    if running is None:  # pragma: no cover -- only in a tagless clone/tarball
        pytest.skip("this checkout has no reachable tags (shallow clone or tarball)")

    monkeypatch.setattr(denver, "package_version", lambda: running)
    denver.validate_denver_version(config)  # dies if the pin is unmet


def test_dev_version_is_a_version():
    """DEV_VERSION, when set, must parse -- _dev_version compares it against the tags.

    Deliberately *not* checked: DEV_VERSION lagging the newest tag (or
    equalling it with commits past it). Once a tag has caught up, _dev_version
    leaves ``git describe``'s own output untouched, so a stale value is inert
    rather than wrong -- and a bump that is actually needed (an example
    pinning an untagged release) is what ``test_examples_run_from_a_checkout``
    fails on.
    """
    if denver.DEV_VERSION is None:
        pytest.skip("DEV_VERSION is None -- the mechanism is off")
    assert denver.parse_version(denver.DEV_VERSION) is not None, (
        f"DEV_VERSION = {denver.DEV_VERSION!r} is not a version"
    )
