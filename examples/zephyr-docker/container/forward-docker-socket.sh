#!/bin/bash -e
# Makes the host's docker daemon usable from inside the container, then runs
# the given command (if any). The image's ENTRYPOINT runs it after fixuid on
# every container start -- `docker start` as well as `docker run`. The
# zephyr-devshell devcontainers run the same `fixuid -q forward-docker-socket.sh`
# from their postStartCommand instead, without a command: 'overrideCommand:
# true' replaces the image's ENTRYPOINT there. fixuid is needed for sudo
# below, which refuses a UID that has no passwd entry; it only remaps once
# per container, a second call just runs the command.
#
# docker-compose.yml mounts the host socket at /var/run/docker-host.sock;
# socat forwards it to /var/run/docker.sock, owned by the container user's
# group. Mounting it at /var/run/docker.sock directly isn't enough: it
# belongs to the host's 'docker' GID, which the container user (running as
# HOST_UID:HOST_GID) usually has no group for.

HOST_SOCKET=/var/run/docker-host.sock
CONTAINER_SOCKET=/var/run/docker.sock
LOG_FILE=/tmp/docker-socket-forward.log

if [ -S "$HOST_SOCKET" ]; then
    # /var/run is not a tmpfs: after a stop/start the old socket file is still
    # there, but the socat behind it is gone -- so always replace it instead
    # of only starting socat when the socket is missing. setsid: socat gets a
    # session of its own, so it outlives sudo even when sudo ran it in a pty.
    # A failure is only reported, it must not keep the container from starting.
    # shellcheck disable=SC2016 # $1..$4 are expanded by the inner bash
    sudo bash -c 'rm -f "$1" && setsid socat \
        UNIX-LISTEN:"$1",fork,mode=660,group="$3" \
        UNIX-CONNECT:"$2" \
        < /dev/null >> "$4" 2>&1 &' \
        _ "$CONTAINER_SOCKET" "$HOST_SOCKET" "$(id -g)" "$LOG_FILE" \
        || echo "Warning: could not forward the host docker socket to $CONTAINER_SOCKET" >&2
fi

[ $# -eq 0 ] || exec "$@"
