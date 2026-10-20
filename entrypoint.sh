#!/usr/bin/env bash
# Entrypoint of every agent-box image: installs the mise tools selected with
# MISE_ENV (presets), then runs the command.
# Installs land in the home volume, so only the first start is slow.
set -uo pipefail

# From / so the project's mise.toml isn't read: `mise install` runs its code even when untrusted
if ! (cd / && mise install --quiet); then
  echo "agent-box: 'mise install' failed, continuing without those tools" >&2
fi
exec "$@"
