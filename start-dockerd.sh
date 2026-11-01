#!/usr/bin/env bash
# Starts the Docker daemon in the background and waits until it answers.
# Used by agent-box-docker-entrypoint under Docker, and as the setup script of
# the agent-box-php-docker profile under microsandbox, which skips ENTRYPOINT.
set -uo pipefail

docker info >/dev/null 2>&1 && exit 0

# cgroup v2: move our processes into a child cgroup so dockerd can hand CPU and
# memory controllers to inner containers (same trick as the official docker:dind)
if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
  sudo sh -c '
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
    sed -e "s/ / +/g" -e "s/^/+/" < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
  ' 2>/dev/null || true
fi

# A restarted box (`agentbox start`) keeps its root filesystem, pid files included, and
# after the reboot their PIDs belong to other processes: dockerd then refuses to start.
# Drop a pid file unless its process really is the daemon it names.
drop_stale_pidfile() {
  local file=$1 name=$2 pid
  pid="$(sudo cat "$file" 2>/dev/null)" || return 0
  [[ "$(cat "/proc/$pid/comm" 2>/dev/null)" == "$name" ]] || sudo rm -f "$file"
}
drop_stale_pidfile /var/run/docker.pid dockerd
drop_stale_pidfile /var/run/docker/containerd/containerd.pid containerd

# setsid: a microsandbox setup script ends with its session, dockerd must outlive it
sudo setsid dockerd >/tmp/dockerd.log 2>&1 &
for _ in $(seq 1 30); do
  sleep 1
  docker info >/dev/null 2>&1 && exit 0
  pgrep -x dockerd >/dev/null || break
done
echo "agent-box: the Docker daemon didn't start, last lines of /tmp/dockerd.log:" >&2
tail -n 5 /tmp/dockerd.log >&2
exit 1
