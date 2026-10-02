"""Log files for --log.

``StageTee`` copies everything written to stdout and stderr into two files
(``<name>.stdout.log`` and ``<name>.stderr.log``), and still shows it on the terminal. It works on the file descriptors, so the
output of subprocesses is caught too. On a terminal it uses a pty, so tools
keep their colours and progress bars. The files have no colours: the escape
codes are removed.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import re
import shutil
import sys
import termios
import threading
from pathlib import Path

# how many run folders are kept; older ones are deleted
KEEP_RUNS = 20

# seconds to wait for the reader threads at the end (a background process can keep the pipe open)
_JOIN_TIMEOUT = 2.0

_CHUNK = 65536

# colour and other escape codes: ESC [ ... (CSI), ESC ] ... (OSC), and short ESC codes
_ESCAPE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# the start of an escape code at the end of the data (the rest comes in the next chunk)
_ESCAPE_START = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?)?$")

# the StageTees that are running right now (the run files and the current stage's files)
_active: list[StageTee] = []


def logging_active():
    """Whether any log file is currently being written."""
    return bool(_active)


def strip_escapes(data):
    """``data`` (bytes) without escape codes."""
    return _ESCAPE.sub(b"", data)


def sync_window_size():
    """Give the log ptys the size of the terminal again (after the window was resized)."""
    for tee in _active:  # the outer one first: the inner one copies from it
        tee.sync_size()


def _split_escape_start(data):
    """``(data, b"")``, or without an escape code cut off at the end: ``(the rest, that start)``."""
    start = _ESCAPE_START.search(data)
    return (data[: start.start()], data[start.start() :]) if start else (data, b"")


def log_only(text, fd=2):
    """Write ``text`` to the log files only, not to the terminal. ``fd`` is 1 (stdout) or 2 (stderr)."""
    for tee in _active:
        tee.write_log(strip_escapes(f"{text}\n".encode(errors="replace")), fd)


def prune_runs(runs_dir, keep):
    """Delete all but the newest ``keep`` run folders (the names start with the time, so they sort by age)."""
    runs = sorted(p for p in Path(runs_dir).iterdir() if p.is_dir())
    for old in runs[: max(len(runs) - keep, 0)]:
        shutil.rmtree(old, ignore_errors=True)


def _open_channel(real_fd):
    """``(master, slave)``: a pty if ``real_fd`` is a terminal, else a pipe."""
    if not os.isatty(real_fd):
        return os.pipe()
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    # the terminal adds '\r' itself; without this the log would get it too
    attrs[1] &= ~termios.ONLCR
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    with contextlib.suppress(OSError):
        size = fcntl.ioctl(real_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
    return master, slave


class StageTee:
    """Copy stdout and stderr to two log files (and still to the terminal)."""

    def __init__(self, log_base, *, append=False):
        """Open ``<log_base>.stdout.log`` and ``<log_base>.stderr.log`` (``append`` keeps what is in them)."""
        base = Path(log_base)
        base.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if append else "wb"
        self._logs = {
            1: base.with_name(f"{base.name}.stdout.log").open(mode),
            2: base.with_name(f"{base.name}.stderr.log").open(mode),
        }
        self._saved = {}
        self._masters = {}
        self._threads = []
        self._lock = threading.Lock()

    def __enter__(self):
        """Start copying."""
        _active.append(self)
        for fd in (1, 2):
            sys.stdout.flush()
            sys.stderr.flush()
            self._saved[fd] = os.dup(fd)
            master, slave = _open_channel(self._saved[fd])
            self._masters[fd] = master
            os.dup2(slave, fd)
            os.close(slave)
            thread = threading.Thread(target=self._pump, args=(master, self._saved[fd], fd), daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc_info):
        """Stop copying and close the files."""
        _active.remove(self)
        sys.stdout.flush()
        sys.stderr.flush()
        for fd, saved in self._saved.items():
            os.dup2(saved, fd)
        for thread in self._threads:
            thread.join(_JOIN_TIMEOUT)
        for fd, saved in self._saved.items():
            os.close(saved)
            with contextlib.suppress(OSError):
                os.close(self._masters[fd])
        for log in self._logs.values():
            log.close()

    def sync_size(self):
        """Copy the window size of the stream we copy to onto our pty."""
        for fd, saved in self._saved.items():
            with contextlib.suppress(OSError):  # a pipe has no size
                size = fcntl.ioctl(saved, termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(self._masters[fd], termios.TIOCSWINSZ, size)

    def write_log(self, data, fd):
        """Write ``data`` to the stdout (1) or stderr (2) file. False if it is already closed."""
        with self._lock:
            try:
                self._logs[fd].write(data)
                self._logs[fd].flush()
            except ValueError:
                return False
        return True

    def _pump(self, master, real_fd, fd):
        """Copy data from ``master`` to the terminal and the file of ``fd`` until it ends."""
        carry = b""  # an escape code that was cut at the end of the last chunk
        while True:
            try:
                data = os.read(master, _CHUNK)
            except OSError:  # a pty reports "closed" like this
                return
            if not data:
                return
            complete, carry = _split_escape_start(carry + data)
            if not self.write_log(strip_escapes(complete), fd):
                return
            with contextlib.suppress(OSError):
                os.write(real_fd, data)
