"""Tests for the --log files."""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

import pytest
from test_denver_orchestration import RecordingSetup, RecordingWrapper

import denver
import denver_providers as providers
import denver_providers.context as ctx_module
import denver_providers.stagelog as stagelog
from denver_providers import Provider
from denver_providers.stagelog import StageTee, prune_runs


def _read(base, stream):
    """The text of ``<base>.<stream>.log``."""
    return Path(f"{base}.{stream}.log").read_text()


def test_prune_runs_keeps_only_the_newest(tmp_path):
    for name in ("20260101-000000-000001", "20260102-000000-000001", "20260103-000000-000001"):
        (tmp_path / name).mkdir()
    (tmp_path / "stray-file").write_text("x")

    prune_runs(tmp_path, 2)

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == [
        "20260102-000000-000001",
        "20260103-000000-000001",
    ]
    assert (tmp_path / "stray-file").exists()


def test_prune_runs_with_fewer_runs_than_the_limit_deletes_nothing(tmp_path):
    (tmp_path / "20260101-000000-000001").mkdir()
    prune_runs(tmp_path, 5)
    assert len(list(tmp_path.iterdir())) == 1


def test_stage_tee_copies_stdout_and_stderr_to_file_and_console(tmp_path, capfd):
    log = tmp_path / "sub" / "01-stage"
    with StageTee(log):
        os.write(1, b"to-stdout\n")
        os.write(2, b"to-stderr\n")
    captured = capfd.readouterr()

    assert "to-stdout" in captured.out
    assert "to-stderr" in captured.err
    # each stream has its own file
    assert _read(log, "stdout") == "to-stdout\n"
    assert _read(log, "stderr") == "to-stderr\n"


def test_the_files_have_no_colours(tmp_path, capfd):
    with StageTee(tmp_path / "a"):
        os.write(1, b"\x1b[93mred\x1b[39m and \x1b]8;;http://x\x07link\x1b]8;;\x07\n")
    assert b"\x1b[93m" in capfd.readouterr().out.encode()  # the terminal keeps the colours
    assert _read(tmp_path / "a", "stdout") == "red and link\n"


def test_a_colour_code_cut_between_two_chunks_is_still_removed(tmp_path):
    tee = StageTee(tmp_path / "a")
    read_end, write_end = os.pipe()
    out_read, out_write = os.pipe()
    os.write(write_end, b"one \x1b[9")
    thread = threading.Thread(target=tee._pump, args=(read_end, out_write, 1))
    thread.start()
    os.write(write_end, b"3mtwo\x1b[39m")
    os.close(write_end)
    thread.join()

    assert _read(tmp_path / "a", "stdout") == "one two"
    for fd in (read_end, out_read, out_write):
        os.close(fd)


def test_log_only_text_has_no_colours(tmp_path):
    with StageTee(tmp_path / "a"):
        stagelog.log_only("\033[93m-- stage\033[39m")
    assert _read(tmp_path / "a", "stderr") == "-- stage\n"


def test_stage_tee_restores_the_descriptors(tmp_path, capfd):
    with StageTee(tmp_path / "a"):
        pass
    os.write(1, b"after\n")
    assert "after" in capfd.readouterr().out
    assert _read(tmp_path / "a", "stdout") == ""


def test_stage_tee_captures_a_subprocess(tmp_path, capfd):
    import subprocess

    with StageTee(tmp_path / "a"):
        subprocess.run(["sh", "-c", "echo child-out; echo child-err >&2"], check=True)
    assert "child-out" in capfd.readouterr().out
    assert _read(tmp_path / "a", "stdout") == "child-out\n"
    assert _read(tmp_path / "a", "stderr") == "child-err\n"


def test_stage_tee_uses_a_pty_when_the_stream_is_a_terminal(tmp_path, capfd, monkeypatch):
    # the fake "terminal" is a file: reading its size fails, and that must be ignored
    monkeypatch.setattr(stagelog.os, "isatty", lambda fd: True)
    with StageTee(tmp_path / "a"):
        os.write(1, b"pty-out\n")
        os.write(2, b"pty-err\n")
    captured = capfd.readouterr()

    assert "pty-out" in captured.out
    assert "pty-err" in captured.err
    # no '\r' in the file
    assert (tmp_path / "a.stdout.log").read_bytes() == b"pty-out\n"
    assert _read(tmp_path / "a", "stderr") == "pty-err\n"


def test_open_channel_gives_the_pty_the_terminals_window_size():
    import fcntl
    import pty
    import struct
    import termios

    real_master, real_slave = pty.openpty()
    fcntl.ioctl(real_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    master, slave = stagelog._open_channel(real_slave)

    assert struct.unpack("HHHH", fcntl.ioctl(slave, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (24, 80)
    for fd in (real_master, real_slave, master, slave):
        os.close(fd)


def test_pump_stops_quietly_when_the_log_is_already_closed(tmp_path):
    tee = StageTee(tmp_path / "a")
    tee._logs[1].close()
    read_end, write_end = os.pipe()
    out_read, out_write = os.pipe()
    os.write(write_end, b"late output")
    os.close(write_end)

    tee._pump(read_end, out_write, 1)  # returns instead of raising ValueError

    os.close(read_end)
    os.close(out_read)
    os.close(out_write)


def test_stage_tee_is_thread_safe_enough_to_run_on_a_worker_thread(tmp_path, capfd):
    # also works from another thread
    def work():
        with StageTee(tmp_path / "t"):
            os.write(1, b"from-thread\n")

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert "from-thread" in _read(tmp_path / "t", "stdout")
    capfd.readouterr()


# ---- Context ---------------------------------------------------------------#
def test_run_log_dir_is_named_after_the_invocation_time(make_context):
    ctx = make_context()
    run_dir = ctx.run_log_dir

    assert re.fullmatch(r"\d{8}-\d{6}-\d{6}", run_dir.name)
    assert run_dir.parent == ctx.logs_dir / "runs"
    assert run_dir.is_dir()
    assert ctx.run_log_dir == run_dir  # one folder per invocation, not per stage


def test_run_log_dir_prunes_older_invocations(make_context, monkeypatch):
    ctx = make_context()
    runs = ctx.logs_dir / "runs"
    for day in range(1, 4):
        (runs / f"2026010{day}-000000-000000").mkdir(parents=True)
    monkeypatch.setattr("denver_providers.context.KEEP_RUNS", 3)

    ctx.run_log_dir  # noqa: B018 - creating it is the point

    names = sorted(p.name for p in runs.iterdir())
    assert len(names) == 3
    assert "20260101-000000-000000" not in names


def test_stage_log_is_a_no_op_unless_enabled(make_context):
    ctx = make_context()
    with ctx.stage_log("conan", 3):
        pass
    assert not (ctx.logs_dir / "runs").exists()


def test_stage_log_writes_a_numbered_file_per_stage(make_context, capfd):
    ctx = make_context()
    ctx.stage_logs = True
    with ctx.stage_log("conan", 3):
        os.write(1, b"building\n")

    assert _read(ctx.run_log_dir / "03-conan", "stdout") == "building\n"
    capfd.readouterr()


# ---- run_stages / main wiring ------------------------------------------------#
class Chatty(Provider):
    name = "chatty"
    kind = "setup"

    def setup(self, ctx):
        os.write(1, f"hello from {self.stage}\n".encode())


@pytest.fixture
def chatty(monkeypatch):
    monkeypatch.setitem(providers.PROVIDERS, "chatty", Chatty)


def _run_stages_logged(env_dir, config, cfg_path, options):
    """run_stages with a real final command. With --log it runs as a child and denver then exits."""
    if not options.logs_stages:
        denver.run_stages(env_dir, config, cfg_path, ["true"], options=options)
        return
    with pytest.raises(SystemExit) as exit_info:
        denver.run_stages(env_dir, config, cfg_path, ["true"], options=options)
    assert exit_info.value.code == 0


def _run(tmp_path, exec_recorder, *, log=False, **options):
    import yaml

    env_dir = tmp_path / "env"
    env_dir.mkdir()
    config = {
        "stages": ["one", "two"],
        "one": {"provider": "chatty"},
        "two": {"provider": "chatty"},
        "command": "myshell",
    }
    cfg_path = env_dir / "denver.yml"
    cfg_path.write_text(yaml.safe_dump(config))
    _run_stages_logged(env_dir, config, cfg_path, denver.RunOptions(**options).with_log(log))
    return env_dir


def test_run_stages_logs_each_stage_into_one_folder(tmp_path, chatty, exec_recorder, capfd, monkeypatch):
    # pytest replaces sys.stderr; a real run writes to fd 2
    monkeypatch.setattr(sys, "stderr", open(2, "w", closefd=False))  # noqa: SIM115
    env_dir = _run(tmp_path, exec_recorder, log=True)
    capfd.readouterr()

    (run_dir,) = list(env_dir.glob(".denver/*/.logs/runs/*"))
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "01-one.stderr.log",
        "01-one.stdout.log",
        "02-two.stderr.log",
        "02-two.stdout.log",
        "03-cmd.stderr.log",
        "03-cmd.stdout.log",
        "run.stderr.log",
        "run.stdout.log",
    ]
    one = _read(run_dir / "01-one", "stdout")
    assert "hello from one" in one
    assert "hello from two" not in one
    assert "stage 'one'" in _read(run_dir / "01-one", "stderr")  # denver's own banner


def test_run_stages_does_not_log_by_default(tmp_path, chatty, exec_recorder, capfd):
    env_dir = _run(tmp_path, exec_recorder)
    capfd.readouterr()
    assert not list(env_dir.glob(".denver/*/.logs/runs"))


def test_run_stages_does_not_log_a_dry_run(tmp_path, chatty, exec_recorder, capfd):
    env_dir = _run(tmp_path, exec_recorder, log=True, dry_run=True)
    capfd.readouterr()
    assert not list(env_dir.glob(".denver/*/.logs/runs"))


def test_main_enables_stage_logging_only_with_log(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(denver, "run_stages", lambda *a, **kw: seen.update(kw))
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "denver.yml").write_text("stages: [hello]\nhello: {provider: custom, cmd: echo hello}\n")

    denver.main(["run", str(env_dir), "--", "echo", "hi"])
    assert seen["options"].log is False

    denver.main(["run", str(env_dir), "--log", "--", "echo", "hi"])
    assert seen["options"].log is True


# ---- the logs have everything, even what -q/-v/capture hide -------------------#
class Scripted(Provider):
    """A stage that runs ``Scripted.action``."""

    name = "scripted"
    kind = "setup"
    action = None

    def setup(self, ctx):
        type(self).action(ctx)


def _scripted_run(tmp_path, monkeypatch, exec_recorder, action, **options):
    """Run a one-stage env that does ``action(ctx)``. Returns the run folder."""
    import yaml

    monkeypatch.setattr(sys, "stderr", open(2, "w", closefd=False))  # noqa: SIM115
    monkeypatch.setitem(providers.PROVIDERS, "scripted", Scripted)
    monkeypatch.setattr(Scripted, "action", staticmethod(action))
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    config = {"stages": ["one"], "one": {"provider": "scripted"}, "command": "myshell"}
    cfg_path = env_dir / "denver.yml"
    cfg_path.write_text(yaml.safe_dump(config))
    _run_stages_logged(env_dir, config, cfg_path, denver.RunOptions(**options).with_log())
    (run_dir,) = list(env_dir.glob(".denver/*/.logs/runs/*"))
    return run_dir


def test_log_has_denver_lines_the_console_hides_under_q(tmp_path, monkeypatch, exec_recorder, capfd):
    from denver_providers.context import info

    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, lambda ctx: info("an info line"), quiet=1)
    captured = capfd.readouterr()

    assert "an info line" not in captured.err
    assert "stage 'one'" not in captured.err
    stage_log = _read(run_dir / "01-one", "stderr")
    assert "INFO: an info line" in stage_log
    assert "stage 'one' (scripted)" in stage_log
    assert "an info line" in _read(run_dir / "run", "stderr")


def test_log_has_the_verbose_only_detail_without_v(tmp_path, monkeypatch, exec_recorder, capfd):
    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, lambda ctx: ctx.run(["echo", "hi"], step="saying hi"))
    captured = capfd.readouterr()

    assert "saying hi" not in captured.err
    assert "+ echo hi" not in captured.err
    stage_log = _read(run_dir / "01-one", "stderr")
    assert "one - saying hi" in stage_log
    assert "+ echo hi" in stage_log
    assert "stage 'one' (scripted) finished in" in stage_log


def test_log_has_a_captured_commands_output(tmp_path, monkeypatch, exec_recorder, capfd):
    def action(ctx):
        result = ctx.run(["sh", "-c", "echo to-stdout; echo to-stderr >&2"], capture=True)
        assert "to-stdout" in result.stdout

    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, action)
    captured = capfd.readouterr()

    assert "to-stdout" not in captured.out
    assert "to-stdout" in _read(run_dir / "01-one", "stdout")
    assert "to-stderr" in _read(run_dir / "01-one", "stderr")


def test_log_has_the_output_of_a_command_qq_discards(tmp_path, monkeypatch, exec_recorder, capfd):
    run_dir = _scripted_run(
        tmp_path, monkeypatch, exec_recorder, lambda ctx: ctx.run(["sh", "-c", "echo shh"]), quiet=2
    )
    captured = capfd.readouterr()

    assert "shh" not in captured.out
    assert "shh" in _read(run_dir / "01-one", "stdout")


def test_a_silenced_command_keeps_its_output_out_of_the_log(tmp_path, monkeypatch, exec_recorder, capfd):
    def action(ctx):
        ctx.run(["sh", "-c", "echo s3cret"], capture=True, echo=False)

    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, action)
    capfd.readouterr()

    assert "s3cret" not in _read(run_dir / "01-one", "stdout")


def test_log_has_the_output_of_a_sourced_script(tmp_path, monkeypatch, exec_recorder, capfd):
    script = tmp_path / "act.sh"
    script.write_text("echo from-script; echo script-err >&2; export FOO=bar\n")

    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, lambda ctx: ctx.source(script))
    capfd.readouterr()

    assert "from-script" in _read(run_dir / "01-one", "stdout")
    assert "script-err" in _read(run_dir / "01-one", "stderr")


def test_run_log_has_everything_the_invocation_printed(tmp_path, monkeypatch, exec_recorder, capfd):
    run_dir = _scripted_run(tmp_path, monkeypatch, exec_recorder, lambda ctx: os.write(1, b"stage output\n"), quiet=1)
    capfd.readouterr()

    assert "stage output" in _read(run_dir / "run", "stdout")
    text = _read(run_dir / "run", "stderr")
    assert "stage 'one' (scripted)" in text
    assert "env env started" in text  # hidden by -q
    assert "one  " in text  # timing summary, hidden by -q


def test_command_failure_still_logs_its_captured_output(make_context, capfd):
    import subprocess

    ctx = make_context()
    ctx.stage_logs = True
    with ctx.stage_log("conan", 1), pytest.raises(subprocess.CalledProcessError):
        ctx.run(["sh", "-c", "echo oops; exit 3"], capture=True)
    capfd.readouterr()

    assert "oops" in _read(ctx.run_log_dir / "01-conan", "stdout")


def test_log_streams_accepts_bytes_and_nothing():
    from denver_providers.context import _log_streams

    _log_streams(None, b"")  # must not fail
    _log_streams(b"bytes", "text")


def test_log_only_without_an_active_log_is_a_no_op():
    assert not stagelog.logging_active()
    stagelog.log_only("nobody is listening")


def test_stop_run_log_is_harmless_when_not_running(make_context):
    make_context().stop_run_log()


def test_exec_ends_the_run_log_and_restores_the_descriptors(make_context, exec_recorder, capfd):
    ctx = make_context()
    ctx.start_run_log()
    assert stagelog.logging_active()

    ctx.exec(["myshell"], log=False)

    assert not stagelog.logging_active()
    os.write(1, b"after exec\n")
    assert "after exec" in capfd.readouterr().out
    assert "after exec" not in _read(ctx.run_log_dir / "run", "stdout")


def test_log_handler_and_console_filter_follow_the_quiet_level(caplog):
    import logging

    from denver_providers.context import _console_filter, set_quiet

    info_record = logging.LogRecord("denver", logging.INFO, "f", 1, "m", None, None)
    error_record = logging.LogRecord("denver", logging.ERROR, "f", 1, "m", None, None)
    set_quiet(0)
    assert _console_filter(info_record)
    set_quiet(1, log_files=True)
    assert not _console_filter(info_record)
    assert _console_filter(error_record)
    assert logging.getLogger("denver").level == logging.INFO


def test_the_log_folder_is_printed_on_stdout(make_context, capfd):
    ctx = make_context()
    ctx.start_run_log()
    ctx.stop_run_log()

    assert capfd.readouterr().out == f"Log files: {ctx.run_log_dir}\n"


def test_the_log_folder_is_not_printed_with_q(make_context, capfd):
    ctx = make_context(quiet=1)
    ctx.start_run_log()
    ctx.stop_run_log()

    assert "Log files" not in capfd.readouterr().out


def test_the_inner_denver_does_not_print_the_log_folder(make_context, capfd):
    ctx = make_context()
    ctx.start_run_log("20261002-071712-123123")
    ctx.stop_run_log()

    assert "Log files" not in capfd.readouterr().out


def test_a_reinvoked_denver_joins_the_outer_invocations_folder(make_context, capfd):
    ctx = make_context()
    run_dir = ctx.logs_dir / "runs" / "20261002-071712-123123"
    run_dir.mkdir(parents=True)
    (run_dir / "run.stdout.log").write_text("from the outer run\n")

    ctx.start_run_log("20261002-071712-123123")
    os.write(1, b"from the inner run\n")
    ctx.stop_run_log()
    capfd.readouterr()

    assert ctx.run_log_dir == run_dir
    assert _read(run_dir / "run", "stdout") == "from the outer run\nfrom the inner run\n"
    assert sorted(p.name for p in run_dir.iterdir()) == ["run.stderr.log", "run.stdout.log"]


def test_the_wrapper_reinvocation_carries_the_run_folder(tmp_path, monkeypatch, exec_recorder, capfd):
    import yaml

    monkeypatch.setitem(providers.PROVIDERS, "fakewrap", RecordingWrapper)
    monkeypatch.setitem(providers.PROVIDERS, "fakesetup", RecordingSetup)
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    config = {
        "stages": ["fakewrap", "fakesetup"],
        "fakewrap": {"provider": "fakewrap"},
        "fakesetup": {"provider": "fakesetup"},
    }
    cfg_path = env_dir / "denver.yml"
    cfg_path.write_text(yaml.safe_dump(config))

    denver.run_stages(env_dir, config, cfg_path, ["echo", "hi"], options=denver.RunOptions().with_log())
    capfd.readouterr()

    (run_dir,) = list(env_dir.glob(".denver/*/.logs/runs/*"))
    args = exec_recorder["args"]
    assert args[args.index("--log-run") + 1] == run_dir.name
    assert "--log" in args


def test_main_hands_the_run_folder_to_run_stages(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(denver, "run_stages", lambda *a, **kw: seen.update(kw))
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "denver.yml").write_text("stages: [hello]\nhello: {provider: custom, cmd: echo hello}\n")

    denver.main(["run", str(env_dir), "--log", "--log-run", "20261002-071712-123123", "--", "echo", "hi"])

    assert seen["options"].run_id == "20261002-071712-123123"


# ---- the final command -------------------------------------------------------#
def test_the_final_command_is_logged_and_its_exit_code_is_kept(make_context, capfd):
    ctx = make_context()
    ctx.stage_count = 8
    ctx.start_run_log()

    with pytest.raises(SystemExit) as exit_info:
        ctx.exec(["sh", "-c", "echo cmd-out; echo cmd-err >&2; exit 3"])

    assert exit_info.value.code == 3
    assert "cmd-out" in capfd.readouterr().out
    run_dir = ctx.run_log_dir
    assert _read(run_dir / "09-cmd", "stdout") == "cmd-out\n"
    assert _read(run_dir / "09-cmd", "stderr") == "cmd-err\n"
    assert "cmd-out" in _read(run_dir / "run", "stdout")  # also in the run files
    assert not stagelog.logging_active()


def test_a_command_killed_by_a_signal_exits_with_128_plus_the_signal(make_context, capfd):
    ctx = make_context()
    ctx.start_run_log()

    with pytest.raises(SystemExit) as exit_info:
        ctx.exec(["sh", "-c", "kill -9 $$"])

    capfd.readouterr()
    assert exit_info.value.code == 137


def test_the_final_command_that_cannot_start_is_an_error(make_context, monkeypatch, capfd):
    from denver_errors import DenverError

    ctx = make_context()
    ctx.start_run_log()
    monkeypatch.setattr(ctx_module.shutil, "which", lambda *a, **kw: "/nonexistent/program")

    with pytest.raises(DenverError, match="failed to run"):
        ctx.exec(["program"])

    ctx.stop_run_log()
    capfd.readouterr()


def test_exec_without_a_run_log_still_replaces_the_process(make_context, exec_recorder):
    make_context().exec(["myshell"])
    assert exec_recorder["args"] == ["myshell"]


def test_while_waiting_ctrl_c_is_ignored_term_is_passed_on_and_the_size_follows(make_context, capfd):
    import signal
    import subprocess
    import time

    from denver_providers.context import _wait_for

    seen = {}
    proc = subprocess.Popen(["sleep", "5"])

    def poke():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)  # must not raise KeyboardInterrupt
        os.kill(os.getpid(), signal.SIGWINCH)  # must not kill us either
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)  # goes to the child

    thread = threading.Thread(target=poke)
    thread.start()
    seen["code"] = _wait_for(proc)
    thread.join()

    assert seen["code"] == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # restored
    capfd.readouterr()


def test_sync_size_copies_the_terminal_size_onto_the_pty(tmp_path, capfd, monkeypatch):
    import fcntl
    import pty
    import struct
    import termios

    real_master, real_slave = pty.openpty()
    fcntl.ioctl(real_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    monkeypatch.setattr(stagelog.os, "isatty", lambda fd: True)
    tee = StageTee(tmp_path / "a")
    tee.__enter__()
    real_saved = tee._saved[1]
    try:
        # pretend the terminal behind stdout is the pty above, and that it was resized
        tee._saved[1] = real_slave
        fcntl.ioctl(real_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 100, 0, 0))
        stagelog.sync_window_size()
        size = struct.unpack("HHHH", fcntl.ioctl(tee._masters[1], termios.TIOCGWINSZ, b"\0" * 8))[:2]
    finally:
        tee._saved[1] = real_saved  # so __exit__ puts the real stdout back, not the log pty
        tee.__exit__()
        os.close(real_master)
        os.close(real_slave)
    capfd.readouterr()

    assert size == (50, 100)
