"""Change one 'machine' entry of a netrc file and keep the rest of the file as it is.

Python's netrc module can only read. This works on the text, so comments, other
entries and their order stay. An entry runs up to the next 'machine' or
'default'. ``macdef`` is not supported.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import NamedTuple

_WORD = re.compile(r"\S+")
# keywords whose next word is their value, never a keyword itself
_WITH_VALUE = ("login", "password", "account", "port")
# a value written bare cannot hold these: they would need quoting, which curl, python and pip don't agree on
UNSAFE_VALUE = re.compile(r"[\s\"'\\]")


class _Word(NamedTuple):
    text: str
    start: int
    line_start: int


class _Entry(NamedTuple):
    host: str | None  # None for 'default'
    start: int
    end: int


def _words(text):
    """Every word of the file outside comment lines, with where it and its line start."""
    words = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("#"):
            words += [_Word(m.group(), offset + m.start(), offset) for m in _WORD.finditer(line)]
        offset += len(line)
    return words


def _entry_start(text, word):
    """Where an entry beginning at ``word`` starts: its line's start if nothing precedes it there."""
    return word.line_start if not text[word.line_start : word.start].strip() else word.start


def _keep_comments_above(text, begin, end):
    """``end`` moved up past the comment/blank lines right above it -- they belong to the entry that follows."""
    while end > 0:
        line_start = text.rfind("\n", 0, end - 1) + 1
        if line_start <= begin or (text[line_start:end].strip() and not text[line_start:end].lstrip().startswith("#")):
            break
        end = line_start
    return end


def _headers(words):
    """(host, word) for every 'machine <host>' (host None for 'default'), in file order."""
    found = []
    rest = iter(words)
    for word in rest:
        if word.text in _WITH_VALUE:
            next(rest, None)  # its value is not a keyword
        elif word.text == "default":
            found.append((None, word))
        elif word.text == "machine":
            host = next(rest, None)
            if host:
                found.append((host.text, word))
    return found


def _entries(text):
    """The 'machine'/'default' entries of ``text`` in file order, each with its text span."""
    found = _headers(_words(text))
    starts = [_entry_start(text, word) for _, word in found]
    ends = [_keep_comments_above(text, start, nxt) for start, nxt in pairwise(starts)] + [len(text)]
    return [_Entry(host, start, end) for (host, _), start, end in zip(found, starts, ends)]  # noqa: B905 -- ends has one extra entry when there are no entries


def has_machine(text, host):
    """True if ``text`` has a 'machine <host>' entry."""
    return any(entry.host == host for entry in _entries(text))


def set_machine(text, host, login, password):
    """``text`` with the entry for ``host`` replaced (or added) -- one 'machine <host> login <login> password <password>' line.

    A new entry goes before a 'default' entry, which curl only honours as the
    last one, else at the end of the file.
    """
    new = f"machine {host} login {login} password {password}\n"
    entries = _entries(text)
    for entry in entries:
        if entry.host == host:
            return text[: entry.start] + new + text[entry.end :]
    for entry in entries:
        if entry.host is None:
            return text[: entry.start] + new + text[entry.start :]
    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + new
