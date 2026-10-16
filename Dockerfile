# agent-box: a disposable Linux workspace for terminal coding agents.
# Works with any Docker-compatible engine: OrbStack, Docker Desktop, Podman,
# Colima, Rancher Desktop, or Docker Engine on Linux.

ARG NODE_VERSION=26
FROM node:${NODE_VERSION}-bookworm-slim AS node

FROM debian:bookworm@sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates ripgrep fd-find jq less vim sudo unzip \
      build-essential python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Node.js, since many agents ship via npm. Copied from the official image
# because Debian's own package is too old for some of them.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Non-root user with passwordless sudo (sudo only affects the container).
# On native Linux, pass your own IDs so project files stay owned by you:
#   docker build --build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g) -t agent-box .
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -o -g "$USER_GID" agent-box \
 && useradd -o -m -s /bin/bash -u "$USER_UID" -g "$USER_GID" agent-box \
 && echo "agent-box ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent-box
USER agent-box

# Everything the user installs (agents, npm globals) lives under $HOME,
# which is a named volume at runtime: it persists and agents can self-update.
ENV NPM_CONFIG_PREFIX=/home/agent-box/.local \
    PATH=/home/agent-box/.local/bin:/home/agent-box/.opencode/bin:$PATH
# Login shells (bash -l) reset PATH via /etc/profile, so set it there too
RUN echo 'export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"' >> ~/.profile

# ---- Coding agents: keep the ones you want, comment out the rest ----

# Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash
# opencode
RUN curl -fsSL https://opencode.ai/v2/install | bash
# OpenAI Codex CLI
# RUN npm install -g @openai/codex
# Gemini CLI
# RUN npm install -g @google/gemini-cli

WORKDIR /workspace
