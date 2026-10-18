# agent-box: a disposable Linux workspace for terminal coding agents.
# Works with any Docker-compatible engine: OrbStack, Docker Desktop, Podman,
# Colima, Rancher Desktop, or Docker Engine on Linux.

ARG NODE_VERSION=26
ARG UV_VERSION=0.13
ARG COMPOSER_VERSION=2
FROM node:${NODE_VERSION}-trixie-slim AS node
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM composer:${COMPOSER_VERSION} AS composer

FROM debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5 AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates ripgrep fd-find jq less vim sudo unzip procps rsync \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node.js, since many agents ship via npm. Copied from the official image
# because Debian's own package is too old for some of them.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG PYTHON_VERSION=3.14
COPY --from=uv /uv /uvx /usr/local/bin/
RUN UV_PYTHON_INSTALL_DIR=/opt/uv/python UV_PYTHON_BIN_DIR=/usr/local/bin \
    uv python install "$PYTHON_VERSION" --default

ARG PHP_VERSION=8.5
RUN curl -fsSLo /tmp/debsuryorg-archive-keyring.deb https://packages.sury.org/debsuryorg-archive-keyring.deb \
 && dpkg -i /tmp/debsuryorg-archive-keyring.deb && rm /tmp/debsuryorg-archive-keyring.deb \
 && echo "deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ trixie main" \
      > /etc/apt/sources.list.d/php.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      php${PHP_VERSION}-cli php${PHP_VERSION}-bcmath php${PHP_VERSION}-curl php${PHP_VERSION}-gd \
      php${PHP_VERSION}-imagick php${PHP_VERSION}-intl php${PHP_VERSION}-mbstring php${PHP_VERSION}-mysql \
      php${PHP_VERSION}-readline php${PHP_VERSION}-redis php${PHP_VERSION}-sqlite3 php${PHP_VERSION}-xml \
      php${PHP_VERSION}-zip php${PHP_VERSION}-xdebug \
 && rm -rf /var/lib/apt/lists/*
COPY --from=composer /usr/bin/composer /usr/local/bin/composer

# Xdebug is installed but not loaded by default: just loading it makes PHP
# ~60% slower even with xdebug.mode=off. Load it on demand from
# /etc/php/xdebug.d, for one command with `with-xdebug php artisan test --coverage`,
# or for a whole session with PHP_INI_SCAN_DIR=:/etc/php/xdebug.d (see README).
RUN phpdismod -v "$PHP_VERSION" -s cli xdebug \
 && mkdir -p /etc/php/xdebug.d \
 && printf 'zend_extension=xdebug.so\nxdebug.mode=coverage\nxdebug.client_host=host.docker.internal\n' \
      > /etc/php/xdebug.d/xdebug.ini \
 && printf '#!/bin/sh\n# Run a command with Xdebug loaded (default mode: coverage; override with XDEBUG_MODE)\nexport PHP_INI_SCAN_DIR=":/etc/php/xdebug.d"\nexec "$@"\n' \
      > /usr/local/bin/with-xdebug \
 && chmod 755 /usr/local/bin/with-xdebug

# Non-root user with passwordless sudo (sudo only affects the container).
# On native Linux, pass your own IDs so project files stay owned by you:
#   docker build --build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g) -t agent-box .
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -o -g "$USER_GID" agent-box \
 && useradd -o -m -s /bin/bash -u "$USER_UID" -g "$USER_GID" agent-box \
 && echo "agent-box ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent-box
USER agent-box

# Everything the user installs (agents, npm globals, uv tools, composer global
# packages) lives under $HOME, which is a named volume at runtime: it persists
# and agents can self-update.
ENV NPM_CONFIG_PREFIX=/home/agent-box/.local \
    PATH=/home/agent-box/.local/bin:/home/agent-box/.opencode/bin:/home/agent-box/.config/composer/vendor/bin:$PATH
# Login shells (bash -l) reset PATH via /etc/profile, so set it there too
RUN echo 'export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$HOME/.config/composer/vendor/bin:$PATH"' >> ~/.profile

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

# ---- Optional: Docker inside the sandbox ----
# Build with:  docker build --target docker -t agent-box:docker .
# Run with:    AGENTBOX_DOCKER=1 agentbox   (needs --privileged, see README)
FROM base AS docker
USER root
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-compose-plugin iptables \
 && rm -rf /var/lib/apt/lists/* \
 && usermod -aG docker agent-box
COPY docker-entrypoint.sh /usr/local/bin/agent-box-docker-entrypoint
RUN chmod 755 /usr/local/bin/agent-box-docker-entrypoint
USER agent-box
ENTRYPOINT ["agent-box-docker-entrypoint"]
CMD ["bash"]

# ---- Default image (plain `docker build`): no Docker inside ----
FROM base AS default
