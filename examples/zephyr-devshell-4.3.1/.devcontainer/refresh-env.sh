#!/bin/bash -e
# Run by postAttachCommand (see devcontainer.json). Not also by
# postStartCommand: VS Code attaches after every start, so that would only
# run it twice in a row.
#
# Brings up conan/uv/zephyr/uv-zephyr -- fast on repeat calls, thanks to each
# stage's own skip-on-success check -- and writes the resulting environment
# to /tmp/denver.env, then makes sure every new bash, zsh and fish picks it up.
#
# Why this dance at all: a VS Code terminal is never a child process of
# whatever ran this script, so the environment variables denver just built
# for its own process can't reach it any other way -- env only flows
# parent->child, never to a sibling process started later. Writing them out
# as 'export KEY=VALUE' lines and sourcing that file from every new shell's
# rc is the standard workaround (same trick direnv/nvm use). Run again on
# every attach rather than once, since neither /tmp nor a bare append
# to ~/.bashrc is guaranteed to survive a container recreation.

SELF_DIR=$(dirname $(realpath "${BASH_SOURCE[0]}"))
ENV_DIR=$(realpath "$SELF_DIR/..")

# '--skip docker': this already runs inside the container the 'docker' stage
# would relocate into -- its own setup() refuses to run a second time from
# in there.
"$SELF_DIR/denver.sh" run "$ENV_DIR" --skip docker --export-env /tmp/denver.env -- true

SOURCE_LINE='[ -f /tmp/denver.env ] && . /tmp/denver.env'
grep -qxF "$SOURCE_LINE" ~/.bashrc 2>/dev/null || echo "$SOURCE_LINE" >> ~/.bashrc

# zsh can source the same file. Not from ~/.zshrc, though: that is a bind
# mount of a tracked file (zephyr-docker/configs/.zshrc), so the line goes
# into ~/.zshenv, limited to interactive shells like the ~/.bashrc one.
ZSH_LINE='[[ -o interactive && -f /tmp/denver.env ]] && . /tmp/denver.env'
grep -qxF "$ZSH_LINE" ~/.zshenv 2>/dev/null || echo "$ZSH_LINE" >> ~/.zshenv

# fish can't source bash syntax -- denver-env.fish translates it (see there)
mkdir -p ~/.config/fish/conf.d
cp "$SELF_DIR/denver-env.fish" ~/.config/fish/conf.d/denver-env.fish
