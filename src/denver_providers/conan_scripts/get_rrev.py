#!/usr/bin/env python3
"""Computes a conan recipe's RREV (recipe revision) by replicating conan's own conanmanifest.txt hashing.

Also ensures each of the recipe's exports_sources is present locally (via
git, a configured url, or a custom fetch command). A downloaded source gets a
pinned md5 in conandata.yml; a git-tracked one gets none, since git and the
manifest already cover it. conandata.yml is rewritten when a pin changes. Used by build_catalog.py to keep catalog.yml's pinned revisions in sync
with recipe content; also runnable standalone (see __main__ below).
"""

from __future__ import annotations

import argparse
import functools
import re
import shlex
import subprocess
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from urllib.request import urlretrieve

import yaml
from conan.api import conan_api
from conan.internal.model.manifest import FileTreeManifest
from conan.internal.util.files import load, md5sum, save

try:
    # invoked as a real subprocess (its own sys.path[0] is this directory) --
    # see the [[tool.mypy.overrides]] for this same module in pyproject.toml.
    import DenverConanFile  # pyright: ignore[reportMissingImports]
except ImportError:
    DenverConanFile = None


@functools.cache
def _local_api():
    """Construct ConanAPI().local on first use, not at import time.

    See recipes.py's _real_conan_api for why (ConanAPI() has real on-disk
    side effects that must not happen just by importing this module).
    functools.cache makes this a memoized singleton with no hand-rolled
    instance bookkeeping.
    """
    return conan_api.ConanAPI().local


class _LocalAPIProxy:
    """Delegates every attribute access to the lazily-constructed local API.

    Every existing ``cli.<attr>`` call site keeps working unchanged; tests
    instead monkeypatch the module-level ``cli`` name outright.
    """

    def __getattr__(self, name):
        return getattr(_local_api(), name)


cli = _LocalAPIProxy()


class GetRREVError(Exception):
    """Raised for recipe-revision calculation failures in this script."""


def inspect(conanfile_path, attributes):
    """Return a {attribute: value} dict for ``attributes`` read off the inspected conanfile."""
    conanfile = cli.inspect(conanfile_path, None, None)
    conanfile_serialized = conanfile.serialize()

    result = {}
    for attribute in attributes:
        if hasattr(conanfile, attribute):
            result[attribute] = getattr(conanfile, attribute)
        elif attribute in conanfile_serialized:
            result[attribute] = conanfile_serialized[attribute]
        else:
            raise GetRREVError(f"Error: conanfile '{conanfile_path}' has not attribute '{attribute}'!")
    return result


def _base_manifest_entries(conanfile_py: Path, conandata_yml: Path) -> list[tuple[str, str]]:
    """Return the manifest (filename, md5) entries that don't depend on exports_sources."""
    entries = [
        (conanfile_py.name, md5sum(conanfile_py)),
        (conandata_yml.name, md5sum(conandata_yml)),
    ]
    if DenverConanFile is not None and "DenverConanFile" in conanfile_py.read_text():
        denver_conan_file_py = Path(cast(str, DenverConanFile.__file__))
        entries.append((denver_conan_file_py.name, md5sum(denver_conan_file_py)))
    return entries


def _is_git_tracked(conanfile_dir: Path, path_abs: Path) -> bool:
    output = subprocess.check_output(["git", "-C", str(conanfile_dir), "ls-files", str(path_abs)])
    return bool(output.decode("utf-8"))


def _fetch_export_source(
    conanfile_dir: Path, exports_source: str, entry: dict, conandata_yml: Path, exports_source_abs: Path
) -> None:
    """Fetch a not-yet-present export source via its configured custom command or url.

    Always prints a warning first: with no md5 pinned yet there is nothing to verify
    the fetched content against, so this is necessarily trust-on-first-use, and the
    resulting conandata.yml diff needs to be reviewed like any other lockfile-style pin.
    """
    custom = entry.get("custom")
    url = entry.get("url") or ""
    if custom:
        print(
            f"Warning: no md5 pinned for '{exports_source}' in '{conandata_yml}' -- running "
            f"custom:'{custom}' to fetch it and trusting whatever it produces as the reference "
            "md5 for the first time. Review this conandata.yml diff before committing it."
        )
        subprocess.run(shlex.split(custom), check=True, cwd=conanfile_dir)
    else:  # _check_download_entry made sure there is a url
        if urlparse(url).scheme not in ("http", "https"):
            raise GetRREVError(f"Error: 'url' must be http(s), got: {url!r}")
        print(
            f"Warning: no md5 pinned for '{exports_source}' in '{conandata_yml}' -- downloading "
            f"'{url}' and trusting its content as the reference md5 for the first time. Review "
            "this conandata.yml diff before committing it."
        )
        # scheme validated above -- file:/ and other non-http(s) schemes are rejected
        urlretrieve(url, exports_source_abs)  # nosec B310


def _tracked_source_md5(exports_source: str, yml_sources: dict, exports_source_abs: Path) -> tuple[str, bool]:
    """Hash a git-tracked source from disk and drop its conandata.yml entry. Returns ``(md5, changed)``.

    No md5 is pinned for it: git versions the file, and its line in the
    manifest already changes the RREV when the file changes.
    """
    changed = yml_sources.pop(exports_source, None) is not None
    if not exports_source_abs.exists():
        raise GetRREVError(f"Error: '{exports_source_abs}' is tracked by git but missing on disk")
    return md5sum(exports_source_abs), changed


def _check_download_entry(conanfile_dir: Path, exports_source: str, entry: dict, exports_source_abs: Path) -> None:
    """Fail unless an untracked source says how to get it (``url`` or ``custom``)."""
    if not entry.get("url") and not entry.get("custom"):
        message = f"Error: 'url' must be specified for {exports_source} in {conanfile_dir}"
        if exports_source_abs.exists():
            # a file sitting right there just isn't known to git yet (e.g. a freshly copied recipe dir)
            message += (
                f"\nHint: '{exports_source}' exists but is not tracked by git, so it is treated as a "
                "download that needs a 'url' (or 'custom') entry in conandata.yml. If it is meant to be "
                f"part of the repo, stage it: git add {exports_source_abs}"
            )
        raise GetRREVError(message)


def _validate_pinned_md5(md5: str, exports_source: str, conandata_yml: Path) -> None:
    """Reject a pinned md5 that isn't a real 32-char hex digest.

    A raised error, not an assert, so the shape is enforced even under
    ``python -O`` (which strips assert statements).
    """
    if len(md5) != 32:
        raise GetRREVError(
            f"Error: md5 '{md5}' for '{exports_source}' in '{conandata_yml}' is not a valid "
            "md5 (expected 32 hex characters)."
        )


def _pin_fetched_md5(
    conanfile_dir: Path, exports_source: str, entry: dict, exports_source_abs: Path, conandata_yml: Path
) -> str:
    """Fetch the source if it isn't there yet, then compute and pin its md5 into ``entry``."""
    if not exports_source_abs.exists():
        _fetch_export_source(conanfile_dir, exports_source, entry, conandata_yml, exports_source_abs)
    md5 = md5sum(exports_source_abs)
    entry["md5"] = md5
    return md5


def _ensure_md5(
    conanfile_dir: Path, exports_source: str, entry: dict, exports_source_abs: Path, conandata_yml: Path
) -> tuple[str, bool]:
    """Validate ``entry``'s pinned md5, or fetch the source and compute+pin one. Returns ``(md5, changed)``."""
    md5 = entry.get("md5")
    changed = not md5
    if md5:
        _validate_pinned_md5(md5, exports_source, conandata_yml)
    else:
        md5 = _pin_fetched_md5(conanfile_dir, exports_source, entry, exports_source_abs, conandata_yml)

    if not md5:
        raise GetRREVError(
            f"Error: Could not get md5 for file '{exports_source_abs}'. Please make sure that you have "
            f"specified the md5sum for this file in '{conandata_yml}' or that the file is present locally."
        )
    return md5, changed


def _sync_export_source(
    conanfile_dir: Path, exports_source: str, yml_sources: dict, conandata_yml: Path
) -> tuple[str, bool]:
    """Ensure ``exports_source`` is present locally; pin an md5 only for a download.

    Mutates ``yml_sources`` in place. Returns ``(md5, changed)`` where
    ``changed`` tells the caller whether conandata.yml needs to be rewritten.
    """
    exports_source_abs = conanfile_dir / exports_source
    if _is_git_tracked(conanfile_dir, exports_source_abs):
        return _tracked_source_md5(exports_source, yml_sources, exports_source_abs)

    changed = exports_source not in yml_sources
    entry = yml_sources.setdefault(exports_source, {}) or {}
    yml_sources[exports_source] = entry

    _check_download_entry(conanfile_dir, exports_source, entry, exports_source_abs)
    md5, md5_changed = _ensure_md5(conanfile_dir, exports_source, entry, exports_source_abs, conandata_yml)

    return md5, changed or md5_changed


def _sync_export_sources(
    conanfile_dir: Path, exports_sources: list, yml_data: dict, conandata_yml: Path
) -> list[tuple[str, str]]:
    """Pin every managed export source, rewriting conandata.yml if any pin changed.

    Only entries named in 'exports_sources' are denver's to manage. A recipe
    is free to pin sources in conandata.yml that it fetches itself at build
    time (conan's own `get()`) instead of staging the archive next to the
    recipe -- a bare conan recipe with no 'exports_sources' at all is the
    extreme case. Those entries are left exactly as written rather than
    deleted as "obsolete": conandata.yml belongs to the recipe author, and
    silently dropping what they put there would break such a recipe.

    Returns the manifest (filename, md5) entries the export sources contribute.
    """
    yml_sources = yml_data.get("sources") or {}
    yml_data["sources"] = yml_sources  # linked to yml_data, so a save() below keeps it
    entries = []
    changed = False

    for exports_source in exports_sources:  # NOTE: wildcard '*' entries aren't (yet) expanded; synced literally
        md5, source_changed = _sync_export_source(conanfile_dir, exports_source, yml_sources, conandata_yml)
        changed = changed or source_changed
        entries.append((f"export_source/{exports_source}", md5))

    if changed:
        text = yaml.safe_dump(yml_data, default_flow_style=False)
        # an empty 'sources:' instead of 'sources: {}'
        save(conandata_yml, re.sub(r"^sources: \{\}$", "sources:", text, flags=re.MULTILINE))
    return entries


def _summary_hash(manifest_entries: list[tuple[str, str]]) -> str:
    """The conanmanifest.txt summary hash over ``manifest_entries``."""
    manifest_str = "\n".join(f"{f}: {md5}" for f, md5 in manifest_entries) + "\n"  # trailing \n, as conan writes it
    return FileTreeManifest.loads("1\n" + manifest_str).summary_hash


def compute_rrev(conanfile_dir):
    """Compute (name, version, RREV) for the recipe at ``conanfile_dir``, ensuring its sources/md5s are pinned."""
    conanfile_dir = Path(conanfile_dir).resolve()  # ensure absolute path
    conanfile_py = conanfile_dir / "conanfile.py"

    conan_inspect = inspect(conanfile_py, ["name", "version", "exports_sources"])

    conandata_yml = conanfile_dir / "conandata.yml"
    if not conandata_yml.exists():
        conandata_yml.touch()

    # ----------------------------------
    # replicate the conanmanifest.txt
    # ----------------------------------
    yml_data = yaml.safe_load(load(conandata_yml)) or {}
    manifest_entries = _base_manifest_entries(conanfile_py, conandata_yml)
    manifest_entries += _sync_export_sources(
        conanfile_dir, conan_inspect["exports_sources"] or [], yml_data, conandata_yml
    )

    return conan_inspect["name"], conan_inspect["version"], _summary_hash(manifest_entries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("conanfile")
    args = parser.parse_args()

    conanfile_dir = args.conanfile if Path(args.conanfile).is_dir() else Path(args.conanfile).parent

    _, _, rrev = compute_rrev(conanfile_dir)
    print(rrev)
