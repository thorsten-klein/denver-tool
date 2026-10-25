"""Tests for the prompt hooks: the env marker, and the red marker of an outdated env (see prompt.py)."""

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import denver
import denver_providers as providers
from denver_providers import prompt
from denver_providers.base import Provider

RED = "\x1b[31m"


@pytest.fixture
def hooked(make_context, tmp_path):
    """A Context with its hooks installed, built from one input file ``tmp_path/denver.yml``."""

    def _make(**kwargs):
        ctx = make_context(**kwargs)
        ctx.install_prompt_hooks()
        config = tmp_path / "denver.yml"
        config.write_text("stages: []\n")
        old = time.time() - 60
        os.utime(config, (old, old))
        ctx.write_env_inputs([config])
        # the list is older than now too: a touch right after it may get the same mtime
        os.utime(ctx.env["DENVER_ENV_INPUTS"], (old + 1, old + 1))
        return ctx, config

    return _make


def _touch(path):
    os.utime(path, None)


# ---- the env --------------------------------------------------------------- #
def test_prompt_command_set_when_none_inherited(make_context):
    ctx = make_context()
    ctx.install_prompt_hooks()
    assert ctx.env["PROMPT_COMMAND"] == ctx.prompt_command
    assert str(ctx.shell_hooks_dir / "prompt.sh") in ctx.prompt_command


def test_prompt_command_appended_to_existing(make_context):
    ctx = make_context(env={"PROMPT_COMMAND": "__set_title"})
    ctx.install_prompt_hooks()
    assert ctx.env["PROMPT_COMMAND"] == f"__set_title; {ctx.prompt_command}"


def test_prompt_command_trailing_semicolon_normalised(make_context):
    # '<existing>; <snippet>' with a trailing ';' would be a bash syntax error
    ctx = make_context(env={"PROMPT_COMMAND": "__set_title ; "})
    ctx.install_prompt_hooks()
    assert ctx.env["PROMPT_COMMAND"] == f"__set_title; {ctx.prompt_command}"


def test_install_twice_adds_nothing(make_context):
    # a wrapper runs denver again inside the container, with this env applied
    ctx = make_context(env={"PROMPT_COMMAND": "__set_title", "ZDOTDIR": "/home/me/.config/zsh"})
    ctx.install_prompt_hooks()
    first = dict(ctx.env)
    ctx.install_prompt_hooks()
    assert ctx.env == first
    assert ctx.env["DENVER_ZDOTDIR"] == "/home/me/.config/zsh"


def test_zdotdir_points_to_the_hook(make_context, monkeypatch):
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setenv("DENVER_ZDOTDIR", "stale")
    ctx = make_context()
    ctx.install_prompt_hooks()
    assert ctx.env["ZDOTDIR"] == str(ctx.shell_hooks_dir / "zsh")
    # no ZDOTDIR of the user's own: the hook unsets it again
    assert "DENVER_ZDOTDIR" not in ctx.env


def test_xdg_data_dirs_default_kept(make_context, monkeypatch):
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    ctx = make_context()
    ctx.install_prompt_hooks()
    assert ctx.env["XDG_DATA_DIRS"] == f"{ctx.shell_hooks_dir}:/usr/local/share:/usr/share"


def test_xdg_data_dirs_prepended(make_context):
    ctx = make_context(env={"XDG_DATA_DIRS": "/opt/share"})
    ctx.install_prompt_hooks()
    assert ctx.env["XDG_DATA_DIRS"] == f"{ctx.shell_hooks_dir}:/opt/share"


def test_dry_run_writes_no_hooks(make_context):
    ctx = make_context(dry_run=True)
    ctx.install_prompt_hooks()
    ctx.write_env_inputs([])
    assert not ctx.shell_hooks_dir.exists()
    assert "PROMPT_COMMAND" not in ctx.env
    assert "DENVER_ENV_INPUTS" not in ctx.env


# ---- the input list --------------------------------------------------------- #
def test_resolve_path_records_existing_files_only(make_context):
    ctx = make_context()
    (ctx.env_dir / "setup.sh").write_text("")
    (ctx.env_dir / "dir").mkdir()
    ctx.resolve_path("setup.sh")
    ctx.resolve_path("dir")
    ctx.resolve_path("missing.sh")
    ctx.resolve_path(str(ctx.env_dir / "setup.sh"))
    assert ctx.input_files == {ctx.env_dir / "setup.sh"}


def test_input_list_names_config_and_resolved_files(make_context, tmp_path):
    ctx = make_context()
    script = ctx.env_dir / "setup.sh"
    script.write_text("")
    ctx.resolve_path("setup.sh")
    config = tmp_path / "denver.yml"
    config.write_text("")
    ctx.install_prompt_hooks()
    ctx.write_env_inputs([config, tmp_path / "gone.yml"])
    listed = Path(ctx.env["DENVER_ENV_INPUTS"]).read_text().splitlines()
    assert listed == sorted([str(config), str(script)])


def test_input_list_named_after_content(hooked):
    ctx, config = hooked()
    first = ctx.env["DENVER_ENV_INPUTS"]
    ctx.write_env_inputs([config])
    assert ctx.env["DENVER_ENV_INPUTS"] == first
    config.write_text("stages: [x]\n")
    ctx.write_env_inputs([config])
    assert ctx.env["DENVER_ENV_INPUTS"] != first


def test_old_input_lists_removed(make_context, tmp_path, monkeypatch):
    monkeypatch.setattr(prompt, "KEEP_INPUTS", 2)
    ctx = make_context()
    config = tmp_path / "denver.yml"
    for n in range(4):
        config.write_text(str(n))
        ctx.write_env_inputs([config])
    assert len(list((ctx.shell_hooks_dir / "inputs").iterdir())) == 2


def test_run_writes_input_list(tmp_path, exec_recorder):
    class Fake(Provider):
        name = "fakesetup"
        kind = "setup"

        def setup(self, ctx):
            pass

    providers.PROVIDERS["fakesetup"] = Fake
    try:
        env_dir = tmp_path / "e"
        env_dir.mkdir()
        (env_dir / "base.yml").write_text("fakesetup:\n  provider: fakesetup\n")
        (env_dir / "denver.yml").write_text(
            textwrap.dedent("""\
            import: [base.yml]
            stages:
            - fakesetup
            """)
        )
        denver.main(["run", str(env_dir), "--", "echo", "hi"])
    finally:
        del providers.PROVIDERS["fakesetup"]
    listed = Path(exec_recorder["env"]["DENVER_ENV_INPUTS"]).read_text().splitlines()
    assert str(env_dir / "denver.yml") in listed
    assert str(env_dir / "base.yml") in listed


# ---- the hooks in real shells ---------------------------------------------- #
def _bash(ctx, script):
    return subprocess.run(["bash", "-c", script], env=ctx.env, capture_output=True, text=True, check=True)


def test_bash_prefix_applied_once(hooked):
    # bash runs PROMPT_COMMAND before every prompt: PS1 must not grow
    ctx, _ = hooked()
    pc = ctx.prompt_command
    result = _bash(ctx, f"PS1='$ '; {pc}; {pc}; {pc}; printf %s \"$PS1\"")
    assert result.stdout == "(myenv) $ "
    assert result.stderr == ""


def test_bash_prefix_not_restacked_when_ps1_is_wrapped(hooked):
    # VS Code puts its own escape sequence in front of PS1
    ctx, _ = hooked()
    pc = ctx.prompt_command
    result = _bash(ctx, f"PS1=$'\\\\[\\\\e]633;A\\\\a\\\\](myenv) $ '; {pc}; {pc}; printf %s \"$PS1\"")
    assert result.stdout.count("(myenv) ") == 1


def test_bash_marks_outdated_env(hooked):
    ctx, config = hooked()
    pc = ctx.prompt_command
    _touch(config)
    result = _bash(ctx, f'PS1=\'$ \'; {pc}; {pc}; {pc}; printf %s "$PS1"; printf "|%s" "$SHELL_PROMPT_PREFIX"')
    ps1, prefix = result.stdout.split("|")
    assert ps1 == "\\[\x1b[31m\\](myenv: outdated env)\\[\x1b[0m\\] $ "
    assert prefix == f"{RED}(myenv: outdated env)\x1b[0m "
    assert result.stderr.count("denver: the env changed since this shell started") == 1


def test_bash_marker_replaced_in_place(hooked):
    ctx, config = hooked()
    pc = ctx.prompt_command
    result = _bash(ctx, f"PS1='X $ '; {pc}; touch {config}; {pc}; printf %s \"$PS1\"")
    assert result.stdout == "\\[\x1b[31m\\](myenv: outdated env)\\[\x1b[0m\\] X $ "


def test_bash_marks_deleted_input(hooked):
    ctx, config = hooked()
    config.unlink()
    result = _bash(ctx, f'{ctx.prompt_command}; printf %s "$SHELL_PROMPT_PREFIX"')
    assert "(myenv: outdated env)" in result.stdout


def test_bash_without_input_list_shows_plain_prefix(hooked):
    ctx, _ = hooked()
    Path(ctx.env["DENVER_ENV_INPUTS"]).unlink()
    result = _bash(ctx, f'{ctx.prompt_command}; printf %s "$SHELL_PROMPT_PREFIX"')
    assert result.stdout == "(myenv) "


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_zsh_runs_user_files_and_marks_outdated_env(hooked, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshenv").write_text("USER_ZSHENV=1\n")
    (home / ".zshrc").write_text("PROMPT='%% '\n")
    ctx, config = hooked(env={"HOME": str(home)})
    _touch(config)
    script = 'print -rn -- "$USER_ZSHENV|${ZDOTDIR-unset}|"; _denver_prompt; _denver_prompt; print -rn -- "$PS1"'
    result = subprocess.run(["zsh", "-i", "-c", script], env=ctx.env, capture_output=True, text=True, check=True)
    assert result.stdout == "1|unset|%{\x1b[31m%}(myenv: outdated env)%{\x1b[0m%} %% "
    assert result.stderr.count("denver: the env changed since this shell started") == 1


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_zsh_keeps_user_zdotdir_and_passes_hook_on(hooked, tmp_path):
    zdotdir = tmp_path / "zdot"
    zdotdir.mkdir()
    (zdotdir / ".zshenv").write_text("USER_ZSHENV=1\n")
    ctx, _ = hooked(env={"ZDOTDIR": str(zdotdir)})
    # 'zsh -c' (not interactive) keeps the hook for the 'zsh -i' it starts
    script = (
        'print -rn -- "$USER_ZSHENV|"; exec zsh -i -c \'print -rn -- "$ZDOTDIR|"; _denver_prompt; print -rn -- "$PS1"\''
    )
    result = subprocess.run(["zsh", "-c", script], env=ctx.env, capture_output=True, text=True, check=True)
    assert result.stdout.startswith(f"1|{zdotdir}|(myenv) ")


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_fish_marks_outdated_env(hooked, tmp_path):
    ctx, config = hooked(env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    show = 'emit fish_prompt; printf "<%s>" "$SHELL_PROMPT_PREFIX"'
    script = f"{show}; touch {config}; {show}; {show}"
    result = subprocess.run(["fish", "-i", "-c", script], env=ctx.env, capture_output=True, text=True, check=True)
    # an interactive fish prints its greeting and prompt around it
    shown = [part.split(">")[0] for part in result.stdout.split("<")[1:]]
    assert shown == ["(myenv) ", *[f"{RED}(myenv: outdated env)\x1b[0m "] * 2]
    assert result.stderr.count("denver: the env changed since this shell started") == 1
    assert "denver.fish" not in result.stderr  # the hook loaded without an error
