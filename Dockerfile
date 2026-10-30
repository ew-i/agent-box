ARG COMPOSER_VERSION=2
ARG MISE_VERSION=2026.10
ARG DOCKER_BASE=base
FROM composer:${COMPOSER_VERSION} AS composer
FROM jdxcode/mise:${MISE_VERSION} AS mise

FROM debian:trixie@sha256:4c25275446ca371766b8b093c696782eabce06263fccacb8d5d813b3dc44b308 AS base

# apt packages are pinned to exact versions. Debian's mirrors drop old versions
# with every update, so apt reads from the snapshot.debian.org date the base
# image itself names in its sources (bump the digest above to move it). Plain
# http is fine: apt checks the signatures, and ca-certificates isn't installed yet.
RUN sed -i -E 's|^# (http://snapshot\.debian\.org/.+)$|URIs: \1\nCheck-Valid-Until: no|; /^URIs: http:\/\/deb\.debian\.org/d' \
      /etc/apt/sources.list.d/debian.sources \
 && grep -q '^URIs: http://snapshot' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
      git=1:2.47.3-0+deb13u1 curl=8.14.1-2+deb13u7 ca-certificates=20250419+deb13u1 \
      ripgrep=14.1.1-1+b4 fd-find=10.2.0-1+b5 jq=1.7.1-6+deb13u4 less=668-1 \
      vim=2:9.1.1230-2 sudo=1.9.16p2-3+deb13u2 unzip=6.0-29+deb13u1 procps=2:4.0.4-9 \
      rsync=3.5.0+ds1-0+deb13u1 build-essential=12.12 \
    && rm -rf /var/lib/apt/lists/*

# mise: all language toolchains, installed on demand into the home volume
# instead of into the image. /etc/mise/config.toml (Node.js, for npm-based
# agents) is always on; presets in /etc/mise are selected at runtime with
# MISE_ENV (e.g. MISE_ENV=java,go); a project's own mise.toml overrides both
# once you `mise trust` it. No MISE_TRUSTED_CONFIG_PATHS or MISE_YES: either
# would let a cloned repo's mise.toml run code just by being opened.

# Non-root user with passwordless sudo (sudo only affects the container).
# On native Linux, pass your own IDs so project files stay owned by you:
#   docker build --build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g) -t agent-box .
# Login shells (bash -l) reset PATH via /etc/profile, so set it there too (keep in
# sync with ENV PATH below). /etc/profile.d rather than ~/.profile, so it also
# reaches existing home volumes.
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -o -g "$USER_GID" agent-box \
 && useradd -o -m -s /bin/bash -u "$USER_UID" -g "$USER_GID" agent-box \
 && echo "agent-box ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent-box \
 && echo 'export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$HOME/.config/composer/vendor/bin:$HOME/.local/share/mise/shims:$PATH"' \
      > /etc/profile.d/agent-box.sh

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
COPY mise/ /etc/mise/
COPY --chmod=755 entrypoint.sh /usr/local/bin/agent-box-entrypoint
# corepack (node preset) would otherwise ask before downloading yarn/pnpm and hang agents
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
USER agent-box

# Everything the user installs (agents, npm globals, uv tools, composer global
# packages) lives under $HOME, which is a named volume at runtime: it persists
# and agents can self-update.
ENV NPM_CONFIG_PREFIX=/home/agent-box/.local \
    PATH=/home/agent-box/.local/bin:/home/agent-box/.opencode/bin:/home/agent-box/.config/composer/vendor/bin:/home/agent-box/.local/share/mise/shims:$PATH

# ---- Coding agents: keep the ones you want, comment out the rest ----

# Pinned versions; both self-update at runtime into the home volume.
ARG CLAUDE_CODE_VERSION=2.1.312
ARG OPENCODE_VERSION=2.2.1
ARG CODEX_VERSION=0.166.0
ARG GEMINI_CLI_VERSION=0.64.0
ARG GITHUB_COPILOT_VERSION=1.0.105
# One layer for the whole home, so the download caches deleted at the end never
# reach the image. Bumping any version above reinstalls all of it.
# Node.js from /etc/mise/config.toml comes first, so the npm-based agents can install.
# A microsandbox named volume starts empty and hides this home, so the last step
# keeps a copy for the entrypoint to seed a new home volume from.
RUN mise install \
# Claude Code
 && curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
# opencode
 && curl -fsSL https://opencode.ai/v2/install | bash -s -- --version "$OPENCODE_VERSION" \
# OpenAI Codex CLI
# && npm install -g @openai/codex@"$CODEX_VERSION" \
# Gemini CLI
# && npm install -g @google/gemini-cli@"$GEMINI_CLI_VERSION" \
# CoPilot CLI
# && npm install -g @github/copilot@"$GITHUB_COPILOT_VERSION" \
 && rm -rf "$HOME/.cache" "$HOME/.npm" \
 && sudo cp -a "$HOME" /opt/agent-box-home

WORKDIR /workspace
ENTRYPOINT ["agent-box-entrypoint"]
CMD ["bash"]

# ---- Optional: PHP + Composer ----
# Build with:  docker build --target php -t agent-box:php .
# Boot with:   a user profile that sets image = "agent-box:php" (see README)
# PHP stays apt-based (Sury) instead of a mise preset: mise only builds PHP from
# source, which is slow and needs dozens of -dev packages.
FROM base AS php
USER root
ARG PHP_VERSION=8.5
# Only the PHP minor (PHP_VERSION) is pinned, not exact package versions like
# the base packages: Sury has no snapshot, keeps only the newest builds, and
# publishes amd64 and arm64 days apart, so an exact pin breaks the build on one
# architecture or the other with every PHP release. You get Sury's newest build.
RUN curl -fsSLo /tmp/debsuryorg-archive-keyring.deb https://packages.sury.org/debsuryorg-archive-keyring.deb \
 && dpkg -i /tmp/debsuryorg-archive-keyring.deb && rm /tmp/debsuryorg-archive-keyring.deb \
 && echo "deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ trixie main" \
      > /etc/apt/sources.list.d/php.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      php${PHP_VERSION}-cli php${PHP_VERSION}-bcmath php${PHP_VERSION}-curl php${PHP_VERSION}-gd \
      php${PHP_VERSION}-intl php${PHP_VERSION}-mbstring php${PHP_VERSION}-mysql \
      php${PHP_VERSION}-readline php${PHP_VERSION}-sqlite3 php${PHP_VERSION}-xml php${PHP_VERSION}-zip \
      php${PHP_VERSION}-imagick php${PHP_VERSION}-redis php${PHP_VERSION}-xdebug \
 && rm -rf /var/lib/apt/lists/* \
# Xdebug is installed but not loaded by default: just loading it makes PHP
# ~60% slower even with xdebug.mode=off. Load it on demand from
# /etc/php/xdebug.d, for one command with `with-xdebug php artisan test --coverage`,
# or for a whole session with PHP_INI_SCAN_DIR=:/etc/php/xdebug.d (see README).
 && phpdismod -v "$PHP_VERSION" -s cli xdebug \
 && mkdir -p /etc/php/xdebug.d \
 && printf 'zend_extension=xdebug.so\nxdebug.mode=coverage\n' \
      > /etc/php/xdebug.d/xdebug.ini \
 && printf '#!/bin/sh\n# Run a command with Xdebug loaded (default mode: coverage; override with XDEBUG_MODE)\nexport PHP_INI_SCAN_DIR=":/etc/php/xdebug.d"\nexec "$@"\n' \
      > /usr/local/bin/with-xdebug \
 && chmod 755 /usr/local/bin/with-xdebug
COPY --from=composer /usr/bin/composer /usr/local/bin/composer
USER agent-box

# ---- Optional: Docker inside the sandbox ----
# Build with:  docker build --target docker -t agent-box:docker .
#   with PHP:  docker build --target docker --build-arg DOCKER_BASE=php -t agent-box:php-docker .
FROM ${DOCKER_BASE} AS docker
USER root
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce=5:29.9.2-1~debian.13~trixie docker-ce-cli=5:29.9.2-1~debian.13~trixie \
      containerd.io=2.4.2-3~debian.13~trixie docker-compose-plugin=5.6.1-1~debian.13~trixie \
      iptables=1.8.11-2 \
 && rm -rf /var/lib/apt/lists/* \
 && usermod -aG docker agent-box
COPY --chmod=755 start-dockerd.sh /usr/local/bin/agent-box-start-dockerd
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/agent-box-docker-entrypoint
USER agent-box
ENTRYPOINT ["agent-box-entrypoint", "agent-box-docker-entrypoint"]
CMD ["bash"]

# ---- Default image (plain `docker build`): no Docker inside ----
FROM base AS default
