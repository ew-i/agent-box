#!/usr/bin/env bash
# Entrypoint of every agent-box image: installs the mise tools selected with
# MISE_ENV (presets), then runs the command.
# Installs land in the home volume, so only the first start is slow.
set -uo pipefail

# A new microsandbox volume is empty (Docker's fills itself from the image). When the
# image's bundled agent launchers change, merge the seed into the home, never overwriting
# existing files: this adds newly enabled agents without undoing user updates or logins.
# Write the marker last, so an interrupted copy is retried on the next start. An old empty
# marker from earlier images intentionally differs from the new seed ID and triggers a merge.
seed_id="$(cat /opt/agent-box-home-seed-id 2>/dev/null || true)"
if [ -n "$seed_id" ] && [ "$(cat "$HOME/.agent-box-seeded" 2>/dev/null || true)" != "$seed_id" ] \
   && [ -d /opt/agent-box-home ]; then
  cp -a --update=none /opt/agent-box-home/. "$HOME"/ \
    && printf '%s\n' "$seed_id" > "$HOME/.agent-box-seeded"
fi

# From / so the project's mise.toml isn't read: `mise install` runs its code even when untrusted
if ! (cd / && mise install --quiet); then
  echo "agent-box: 'mise install' failed, continuing without those tools" >&2
fi
exec "$@"
