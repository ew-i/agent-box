#!/usr/bin/env bash
# Entrypoint of the agent-box:docker image: starts a private Docker daemon inside
# the container, runs the requested command, then stops the daemon cleanly so
# compose stacks shut down properly before the container goes away.
set -uo pipefail

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
  if agent-box-start-dockerd; then
    started=1
  else
    echo "agent-box: under Docker, dockerd needs a privileged container (docker run --privileged)" >&2
  fi
fi

# docker stop (TERM) and a closed terminal (HUP) only reach this script, and the
# container dies with it. A trap only fires while bash waits in `wait`, not while
# a foreground command runs, so the command runs in the background (<&0 keeps the tty;
# env restores Ctrl-C and Ctrl-\, which bash makes background jobs ignore).
on_signal() {
  kill -HUP "$child" 2>/dev/null   # HUP, not TERM: an interactive bash ignores TERM
  [[ $started -eq 1 ]] && stop_dockerd
  exit $((128 + $1))
}
trap 'on_signal 1' HUP
trap 'on_signal 15' TERM
trap : INT   # Ctrl-C is for the command; a handler (unlike ignoring) isn't inherited by it

env --default-signal=INT,QUIT "$@" <&0 &
child=$!
while kill -0 "$child" 2>/dev/null; do
  wait "$child"
done
wait "$child"
status=$?

if [[ $started -eq 1 ]]; then
  stop_dockerd
fi
exit "$status"
