#!/bin/bash -e
# One-time host bootstrap for the zephyr-docker env, run via
# `denver run examples/zephyr-docker --scripts setup` -- deliberately not part of the
# normal stage pipeline, because everything here touches the host system
# (apt, systemd) and needs sudo.

TOTAL=8

cat <<'EOF'
======================================================================
 zephyr-docker -- one-time host setup
======================================================================
This installs the host-side tools the docker provider needs to build
and run the dev container. Nothing is installed *into* the container
here, and nothing in your workspace is touched.

What will be installed (each step is skipped if already satisfied):

  1. containerd       container runtime                (apt: containerd)
  2. docker           container engine + CLI           (apt: docker.io)
  3. docker buildx    BuildKit builder used by
                      `docker compose build`           (apt: docker-buildx)
  4. make             drives this example's build
                      targets                          (apt: make)
  5. jq               create-env.sh parses
                      `docker compose config` JSON     (apt: jq)
  6. binfmt-support   QEMU/binfmt registration, so
                      foreign-arch images can run      (apt: binfmt-support)
  7. docker group     use docker without sudo          (usermod)
  8. docker proxy     pull and build behind a proxy,
                      from $http_proxy/$https_proxy    (daemon + client)

Steps 7 and 8 are skipped on CI (--ci). Every change goes through sudo,
so you may be asked for your password. Nothing is removed or upgraded.
======================================================================

EOF

step=0

# apt_install_if_missing <apt-package> <why it is needed> <check command...>
# Installs the package only when the check command fails, and says either way.
apt_install_if_missing() {
    local pkg=$1 reason=$2
    shift 2
    step=$((step + 1))
    printf '[%d/%d] %s -- %s\n' "$step" "$TOTAL" "$pkg" "$reason"
    if "$@" > /dev/null 2>&1; then
        echo "       already present -- skipping"
        return
    fi
    echo "       missing -- running: sudo apt-get install -y $pkg"
    sudo apt-get install -y "$pkg"
}

# Ensure that minimal tools are installed
apt_install_if_missing containerd     "container runtime"                    command -v containerd
apt_install_if_missing docker.io      "container engine and CLI"             command -v docker
apt_install_if_missing docker-buildx  "BuildKit builder for compose build"   docker buildx --help
apt_install_if_missing make           "build targets of this example"        command -v make
apt_install_if_missing jq             "JSON parsing in create-env.sh"        command -v jq

# Ensure QEMU is installed in the host system
apt_install_if_missing binfmt-support "QEMU binfmt registration" \
    test -e /lib/systemd/system/binfmt-support.service

# step_header <name> <why it is needed>
step_header() {
    step=$((step + 1))
    printf '[%d/%d] %s -- %s\n' "$step" "$TOTAL" "$1" "$2"
}

# Add the user to the docker group, so docker runs without sudo.
step_header "docker group" "use docker without sudo"
if [ -n "$DENVER_CI" ]; then
    echo "       CI run -- skipping"
elif id -nG "$USER" | grep -qw docker; then
    echo "       already a member -- skipping"
else
    echo "       missing -- running: sudo usermod -aG docker $USER"
    sudo usermod -aG docker "$USER"
fi

# Pass the proxy of this shell to the docker daemon (pulls) and to the
# docker client (builds and containers).
step_header "docker proxy" "pull and build behind a proxy"
http=${http_proxy:-$HTTP_PROXY}
https=${https_proxy:-$HTTPS_PROXY}
no=${no_proxy:-$NO_PROXY}
if [ -n "$DENVER_CI" ]; then
    echo "       CI run -- skipping"
elif [ -z "$http$https" ]; then
    echo "       no proxy set -- skipping"
else
    dropin=/etc/systemd/system/docker.service.d/http-proxy.conf
    want="[Service]
Environment=\"HTTP_PROXY=$http\" \"HTTPS_PROXY=$https\" \"NO_PROXY=$no\""
    if [ "$(cat "$dropin" 2>/dev/null)" = "$want" ]; then
        echo "       daemon already set -- skipping"
    else
        echo "       daemon: writing $dropin and restarting docker (stops running containers)"
        sudo mkdir -p "$(dirname "$dropin")"
        echo "$want" | sudo tee "$dropin" > /dev/null
        sudo systemctl daemon-reload
        sudo systemctl restart docker
    fi

    client=~/.docker/config.json
    proxies=$(jq -n --arg h "$http" --arg s "$https" --arg n "$no" \
        '{default: {httpProxy: $h, httpsProxy: $s, noProxy: $n}}')
    if [ -f "$client" ] && jq -e --argjson p "$proxies" '.proxies == $p' "$client" > /dev/null; then
        echo "       client already set -- skipping"
    else
        # keep the other entries, e.g. registry logins
        echo "       client: setting proxies in $client"
        mkdir -p ~/.docker
        [ -s "$client" ] || echo '{}' > "$client"
        # cat, not mv: the file keeps its mode (it may hold credentials)
        jq --argjson p "$proxies" '.proxies = $p' "$client" > "$client.tmp"
        cat "$client.tmp" > "$client"
        rm "$client.tmp"
    fi
fi

cat <<'EOF'

Host setup complete. You can now start the env with:

    denver run examples/zephyr-docker

If you were added to the docker group just now, log out and back in
(or run `newgrp docker`) before you use docker without sudo.
EOF
