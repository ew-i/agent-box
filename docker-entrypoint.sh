#!/usr/bin/env bash
# Entrypoint of the agent-box:docker image: starts a private Docker daemon inside
# the container, runs the requested command, then stops the daemon cleanly so
# compose stacks shut down properly before the container goes away.
set -uo pipefail

# cgroup v2: move our processes into a child cgroup so dockerd can hand CPU and
# memory controllers to inner containers (same trick as the official docker:dind)
enable_cgroup_nesting() {
  [[ -f /sys/fs/cgroup/cgroup.controllers ]] || return 0
  sudo sh -c '
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
    sed -e "s/ / +/g" -e "s/^/+/" < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
  ' 2>/dev/null || true
}

start_dockerd() {
  enable_cgroup_nesting
  sudo dockerd >/tmp/dockerd.log 2>&1 &
  local pid=$!
  for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && return 0
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  echo "agent-box: the Docker daemon didn't start, see /tmp/dockerd.log" >&2
  echo "agent-box: it needs a privileged container, start it with AGENTBOX_DOCKER=1 agentbox" >&2
  return 1
}

stop_dockerd() {
  local pid
  pid="$(sudo cat /var/run/docker.pid 2>/dev/null)" || return 0
  sudo kill -TERM "$pid" 2>/dev/null || return 0
  for _ in $(seq 1 20); do
    sudo kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
}

started=0
if ! docker info >/dev/null 2>&1; then
  start_dockerd && started=1
fi

status=0
"$@" || status=$?

if [[ $started -eq 1 ]]; then
  stop_dockerd
fi
exit "$status"
