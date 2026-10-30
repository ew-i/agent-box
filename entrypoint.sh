#!/usr/bin/env bash
# Entrypoint of every agent-box image: installs the mise tools selected with
# MISE_ENV (presets), then runs the command.
# Installs land in the home volume, so only the first start is slow.
set -uo pipefail

# A new microsandbox volume is empty (Docker's fills itself from the image): seed it
# once from the image's copy of the home, never overwriting a file that is there
if [ ! -e "$HOME/.bashrc" ] && [ -d /opt/agent-box-home ]; then
  cp -a --update=none /opt/agent-box-home/. "$HOME"/
fi

# From / so the project's mise.toml isn't read: `mise install` runs its code even when untrusted
if ! (cd / && mise install --quiet); then
  echo "agent-box: 'mise install' failed, continuing without those tools" >&2
fi
exec "$@"
