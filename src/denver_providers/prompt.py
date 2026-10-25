"""Prompt hooks: show the env in the prompt, and mark it red once the env is outdated.

denver writes one hook per shell into ``<env workdir>/.shell/``. Each runs
before every prompt. It sets ``SHELL_PROMPT_PREFIX`` (and the prompt itself
in bash and zsh) to ``(<env>) ``. When a file the env was built from changed
since, the prefix turns red and names the file, and the shell prints a
warning once.

The files the env was built from are listed in the file ``DENVER_ENV_INPUTS``
points to. The hook compares each file's mtime with that list file's own
mtime (``test -nt``, a builtin in bash and zsh, so a prompt starts no
process; fish runs ``find`` once per prompt).
"""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

INPUTS_VAR = "DENVER_ENV_INPUTS"
# the user's own ZDOTDIR, for denver's .zshenv to restore (unset if they had none)
ZDOTDIR_VAR = "DENVER_ZDOTDIR"
# how many input lists are kept; a shell whose list was removed just shows no warning
KEEP_INPUTS = 20

# bash and zsh source this before each prompt. Variables start with _denver_
# because it runs in the user's shell.
SH_HOOK = r"""# Written by denver. Sourced before each prompt (bash: PROMPT_COMMAND, zsh: precmd).
_denver_reason=
if [ -f "${DENVER_ENV_INPUTS-}" ]; then
    while IFS= read -r _denver_file; do
        if [ ! -e "$_denver_file" ] || [ "$_denver_file" -nt "$DENVER_ENV_INPUTS" ]; then
            _denver_reason="${_denver_file##*/} changed"
            break
        fi
    done <"$DENVER_ENV_INPUTS"
fi
if [ -z "$_denver_reason" ]; then
    SHELL_PROMPT_PREFIX="($DENVER_ENV_NAME) "
    _denver_ps1=$SHELL_PROMPT_PREFIX
else
    SHELL_PROMPT_PREFIX=$'\e[31m'"($DENVER_ENV_NAME: $_denver_reason)"$'\e[0m '
    # the prompt must know which characters take no space
    if [ -n "${ZSH_VERSION-}" ]; then _denver_on='%{' _denver_off='%}'; else _denver_on='\[' _denver_off='\]'; fi
    _denver_ps1="$_denver_on"$'\e[31m'"$_denver_off($DENVER_ENV_NAME: $_denver_reason)$_denver_on"$'\e[0m'"$_denver_off "
    if [ -z "${_denver_warned-}" ]; then
        _denver_warned=1
        printf 'denver: %s since this shell started. Start the env again to use it.\n' "$_denver_reason" >&2
    fi
fi
export SHELL_PROMPT_PREFIX
# replace the prefix set last time, in place: VS Code puts its own codes before it
case "$PS1" in
*"$_denver_ps1"*) ;;
*"${_denver_last_ps1:-$_denver_ps1}"*) PS1=${PS1/"$_denver_last_ps1"/"$_denver_ps1"} ;;
*) PS1="$_denver_ps1$PS1" ;;
esac
_denver_last_ps1=$_denver_ps1
"""

# zsh reads this from ZDOTDIR. It restores the user's ZDOTDIR, so zsh reads
# their own startup files, and adds the hook. {hook} is the quoted SH_HOOK path.
ZSHENV = """# Written by denver: run your own .zshenv, then add denver's prompt hook.
_denver_zdotdir=$ZDOTDIR
if [ -n "${{DENVER_ZDOTDIR+x}}" ]; then ZDOTDIR=$DENVER_ZDOTDIR; else unset ZDOTDIR; fi
[ -f "${{ZDOTDIR:-$HOME}}/.zshenv" ] && . "${{ZDOTDIR:-$HOME}}/.zshenv"
if [[ -o interactive ]]; then
    _denver_prompt() {{ [ -f {hook} ] && . {hook}; }}
    precmd_functions+=(_denver_prompt)
else
    # a zsh this one starts (e.g. 'zsh -c "...; exec zsh -i"') still gets the hook
    export ZDOTDIR=$_denver_zdotdir
fi
unset _denver_zdotdir
"""

# fish reads this from <XDG_DATA_DIRS entry>/fish/vendor_conf.d. fish >= 4.8
# shows SHELL_PROMPT_PREFIX itself.
FISH_HOOK = r"""# Written by denver: set SHELL_PROMPT_PREFIX before each prompt.
# fish < 3.4 has no "return" outside a function
if status is-interactive
    function __denver_prompt --on-event fish_prompt
        set -l reason
        set -l files
        test -f "$DENVER_ENV_INPUTS"; and set files (string split \n <$DENVER_ENV_INPUTS)
        for file in $files
            if not test -e "$file"
                set reason (string replace -r '.*/' '' -- $file)" changed"
                break
            end
        end
        # fish < 4 has no 'test -nt', so ask find once for all files
        if test -z "$reason"; and set -q files[1]
            set -l newer (command find $files -newer $DENVER_ENV_INPUTS 2>/dev/null)
            set -q newer[1]; and set reason (string replace -r '.*/' '' -- $newer[1])" changed"
        end
        if test -z "$reason"
            set -gx SHELL_PROMPT_PREFIX "($DENVER_ENV_NAME) "
            return
        end
        set -gx SHELL_PROMPT_PREFIX \e"[31m($DENVER_ENV_NAME: $reason)"\e"[0m "
        if not set -q __denver_warned
            set -g __denver_warned 1
            printf 'denver: %s since this shell started. Start the env again to use it.\n' $reason >&2
        end
    end
end
"""


def sh_hook_path(hooks_dir):
    """Where the bash/zsh hook is written."""
    return Path(hooks_dir) / "prompt.sh"


def prompt_command(hooks_dir):
    """The PROMPT_COMMAND snippet: source the hook, if it is still there."""
    hook = shlex.quote(str(sh_hook_path(hooks_dir)))
    return f"[ -f {hook} ] && . {hook}"


def install(env, hooks_dir):
    """Write the hooks to ``hooks_dir`` and point bash, zsh and fish at them in ``env``.

    Safe to run again on an env that has them already (a wrapper runs denver a
    second time inside the container): nothing is added twice.
    """
    hooks_dir = Path(hooks_dir)
    zsh_dir = hooks_dir / "zsh"
    fish_dir = hooks_dir / "fish" / "vendor_conf.d"
    zsh_dir.mkdir(parents=True, exist_ok=True)
    fish_dir.mkdir(parents=True, exist_ok=True)
    sh_hook_path(hooks_dir).write_text(SH_HOOK)
    (zsh_dir / ".zshenv").write_text(ZSHENV.format(hook=shlex.quote(str(sh_hook_path(hooks_dir)))))
    (fish_dir / "denver.fish").write_text(FISH_HOOK)

    _add_prompt_command(env, prompt_command(hooks_dir))
    _point_zdotdir(env, zsh_dir)
    _add_data_dir(env, hooks_dir)


def _add_prompt_command(env, snippet):
    """Append ``snippet`` to PROMPT_COMMAND, unless it is there already."""
    # a trailing ';' on the inherited value would make '<existing>; <snippet>'
    # a bash syntax error ('cmd; ; [ -f ...'), so it's normalised away
    existing = env.get("PROMPT_COMMAND", "").strip().rstrip(";").strip()
    if snippet not in existing:
        env["PROMPT_COMMAND"] = f"{existing}; {snippet}" if existing else snippet


def _point_zdotdir(env, zsh_dir):
    """Point ZDOTDIR at ``zsh_dir``, keeping the user's own in DENVER_ZDOTDIR."""
    if env.get("ZDOTDIR") == str(zsh_dir):
        return
    if "ZDOTDIR" in env:
        env[ZDOTDIR_VAR] = env["ZDOTDIR"]
    else:
        env.pop(ZDOTDIR_VAR, None)
    env["ZDOTDIR"] = str(zsh_dir)


def _add_data_dir(env, hooks_dir):
    """Put ``hooks_dir`` first in XDG_DATA_DIRS, where fish looks for vendor_conf.d."""
    # unset means '/usr/local/share:/usr/share' (XDG base directory spec)
    data_dirs = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    if str(hooks_dir) not in data_dirs.split(os.pathsep):
        env["XDG_DATA_DIRS"] = f"{hooks_dir}{os.pathsep}{data_dirs}"


def write_inputs(env, hooks_dir, files):
    """Write the list of ``files`` the env was built from, and point ``DENVER_ENV_INPUTS`` at it.

    The list file is named after the content of the files. Its mtime is the
    time the env was built: the hook warns about a file newer than that.
    Another shell built from the same content rewrites the same list, which is
    right: the content it describes is the same. A shell built from other
    content gets its own list, so it cannot hide the warning of an older shell.
    """
    paths = sorted({str(Path(f).resolve()) for f in files if Path(f).is_file()})
    digest = hashlib.sha256()
    for path in paths:
        digest.update(f"{path}\n".encode())
        digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
    inputs_dir = Path(hooks_dir) / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    inputs = inputs_dir / digest.hexdigest()[:16]
    inputs.write_text("".join(f"{path}\n" for path in paths))
    env[INPUTS_VAR] = str(inputs)
    _prune(inputs_dir, KEEP_INPUTS)


def _prune(inputs_dir, keep):
    """Delete all but the ``keep`` newest input lists."""
    lists = sorted(inputs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in lists[keep:]:
        old.unlink(missing_ok=True)
