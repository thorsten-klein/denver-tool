"""Tests for providers.python.PythonProvider."""

import subprocess

import pytest

import denver_providers as providers
from denver_errors import DenverError
from denver_providers.python import PythonProvider


def run_python(config, ctx, stage="py"):
    p = PythonProvider(config)
    p.stage = stage
    p.setup(ctx)
    return p


def write_file(ctx, name="tools.py", text=""):
    path = ctx.env_dir / name
    path.write_text(text)
    return path


# ---- registry ------------------------------------------------------------- #
def test_make_stage_picks_python():
    stage = providers.make_stage("py", {"py": {"provider": "python", "file": "a.py", "function": "f"}})
    assert isinstance(stage, PythonProvider)


# ---- guard clauses -------------------------------------------------------- #
@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"file": "tools.py"},
        {"function": "setup"},
        {"file": "", "function": "setup"},
        {"file": "tools.py", "function": ["setup"]},
        {"file": "tools.py", "function": "setup", "args": ["x"]},
    ],
)
def test_bad_config_dies(make_context, cfg):
    config = {"py": cfg}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx, **kw): pass\n")
    with pytest.raises(DenverError):
        run_python(config, ctx)


def test_missing_file_dies(make_context):
    config = {"py": {"file": "nope.py", "function": "setup"}}
    ctx = make_context(config=config)
    with pytest.raises(DenverError, match="not found"):
        run_python(config, ctx)


def test_missing_function_dies(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="def other(ctx): pass\n")
    with pytest.raises(DenverError, match="not defined"):
        run_python(config, ctx)


def test_not_callable_dies(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="setup = 42\n")
    with pytest.raises(DenverError, match="not a function"):
        run_python(config, ctx)


def test_import_error_dies(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="raise RuntimeError('boom at import')\n")
    with pytest.raises(DenverError, match="boom at import"):
        run_python(config, ctx)


def test_function_exception_dies_with_traceback(make_context, caplog):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx):\n    raise ValueError('bad value')\n")
    with pytest.raises(DenverError, match="bad value"):
        run_python(config, ctx)
    assert "Traceback" in caplog.text


def test_called_process_error_passes_through(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx):\n    ctx.run(['false'])\n")
    with pytest.raises(subprocess.CalledProcessError):
        run_python(config, ctx)


# ---- happy path ----------------------------------------------------------- #
def test_function_gets_ctx_and_can_set_env(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx):\n    ctx.set('FROM_PY', 'yes')\n")
    run_python(config, ctx)
    assert ctx.env["FROM_PY"] == "yes"


def test_args_are_keyword_arguments(make_context):
    config = {"py": {"file": "tools.py", "function": "setup", "args": {"name": "X", "count": 2}}}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx, name, count):\n    ctx.set(name, str(count))\n")
    run_python(config, ctx)
    assert ctx.env["X"] == "2"


def test_args_are_interpolated(make_context):
    config = {"py": {"file": "tools.py", "function": "setup", "args": {"where": "${DENVER_ENV_DIR}"}}}
    ctx = make_context(config=config)
    write_file(ctx, text="def setup(ctx, where):\n    ctx.set('WHERE', where)\n")
    run_python(config, ctx)
    assert ctx.env["WHERE"] == str(ctx.env_dir)


def test_file_can_import_its_neighbour(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config)
    write_file(ctx, "_helper_for_py_test.py", "VALUE = 'neighbour'\n")
    write_file(ctx, text="import _helper_for_py_test\ndef setup(ctx):\n    ctx.set('V', _helper_for_py_test.VALUE)\n")
    run_python(config, ctx)
    assert ctx.env["V"] == "neighbour"


def test_two_stages_share_one_module(make_context):
    config = {
        "a": {"file": "tools.py", "function": "first"},
        "b": {"file": "tools.py", "function": "second"},
    }
    ctx = make_context(config=config)
    write_file(
        ctx,
        text="seen = []\ndef first(ctx):\n    seen.append(1)\ndef second(ctx):\n    ctx.set('SEEN', str(len(seen)))\n",
    )
    run_python(config, ctx, stage="a")
    run_python(config, ctx, stage="b")
    assert ctx.env["SEEN"] == "1"


def test_runs_under_fast_and_dry_run(make_context):
    config = {"py": {"file": "tools.py", "function": "setup"}}
    ctx = make_context(config=config, fast=True, dry_run=True)
    write_file(ctx, text="def setup(ctx):\n    ctx.set('FLAGS', f'{ctx.fast} {ctx.dry_run}')\n")
    run_python(config, ctx)
    assert ctx.env["FLAGS"] == "True True"
