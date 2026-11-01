# agent-box

Sandboxed execution environments for coding agents, backed by
[microsandbox](https://microsandbox.dev/) microVMs.

[![build](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml) [![coverage](https://raw.githubusercontent.com/ew-i/badges/main/agent-box/coverage.svg)](https://github.com/ew-i/agent-box) [![codeql](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml) ![Python](https://img.shields.io/badge/python-3.11_%E2%80%93_3.15-yellow?labelColor=blue)

`agentbox` boots microVMs from **declarative profiles**: image, CPU/memory, lifetime,
workspace mount and the network policy the agent may use. Each sandbox has its own kernel,
so generated code can't escape the way a container breakout can.

**The workspace is the exception.** The agent can write to it, `.git/` included, and a hook
in `.git/hooks/` or a `core.fsmonitor` in `.git/config` runs on the host the next time you
(or your shell prompt) run git there. Mount with `--readonly` when the agent only needs to
read; the host enforces it, even against root in the box. Otherwise check `.git/hooks` and
`.git/config` before using git on the host.

`--readonly` doesn't cover the home volume, which all `agent-box` boxes share (see
[Working in it](#working-in-it)). Give an untrusted project its own home:
`named_volumes = { "/home/agent-box" = "agent-box-home-untrusted" }`.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Linux with KVM or macOS on
Apple Silicon (check with `msb doctor`).

```bash
uv tool install --editable . --with-executables-from microsandbox
agentbox profiles
```

This puts `agentbox` and the bundled `msb` in `~/.local/bin`, so you can run them from any
project. Being editable, a `git pull` takes effect at once; after a dependency change, rerun
with `--reinstall`. Inside this checkout, `uv run agentbox` works too.

To load secrets from `~/.config/agentbox/.env`, wrap it in your shell rc:

```bash
agentbox() { (set -a; [ -f ~/.config/agentbox/.env ] && . ~/.config/agentbox/.env; command agentbox "$@") }
```

Or copy `Makefile.example` to `Makefile` and run `make -f ~/code/agent-box/Makefile start`
from a project: it mounts that folder, names the box after it, and loads the `.env`.

## Usage

One-shot (boot, run, tear down):

```bash
agentbox run airgapped 'python -c "print(1)"'
agentbox run airgapped 'ls /workspace' -w ./my-project
```

Long-lived:

```bash
cd ~/code/my-app
agentbox up agent-box --name my-app -w .
msb exec my-app -- bash -l
agentbox ls
agentbox stop my-app                     # stop, keep state (like msb stop)
agentbox start my-app                    # start again, rerun the setup script
agentbox down my-app                     # stop + delete state
```

`up` prints only the sandbox name on stdout; hints and warnings go to stderr:

```bash
BOX=$(agentbox up airgapped)
msb exec "$BOX" -- python -c "print('hi')"
agentbox down "$BOX"
```

`up` with the name of a stopped box restarts it with its original settings. It refuses if
you change the profile, workspace, `--readonly`, `--network`, or the profile's `network`,
`allowed_domains`, `tls_bypass`, `secrets` or `named_volumes`; `agentbox down` it first.
Rotated secrets and new time limits also only apply after `down`.

`agentbox start NAME` does the same from just the name: it starts the stopped box as it was
created (mounts, network, secrets) and reruns its profile's `setup_script`, which
`msb start` skips.

`show` prints the profile that would actually boot, with `env` values masked. It takes the
same flags as `run` and `up`:

```bash
agentbox show airgapped -w ./my-project --readonly -n public
agentbox show airgapped --for up        # as `up` would boot it
```

### `run` vs `up`

|                   | `run`                          | `up`                                     |
|-------------------|--------------------------------|------------------------------------------|
| Script argument   | required                       | none, connect with `msb exec`            |
| Lifetime          | ends with the command          | until `agentbox down`                    |
| Idle / max limits | profile's, as a backstop       | none, unless you pass the flags          |
| `ephemeral`       | `True` (profile default)       | forced `False`, so state survives a stop |

`--idle-timeout` stops an unused box, `--max-duration` stops it regardless; `0` disables
either:

```bash
agentbox up agent-box --name capped --idle-timeout 600 --max-duration 7200
```

## The `agent-box` image

Debian 13, dev tools, [mise](https://mise.jdx.dev) for toolchains, and the coding agents you
pick. Build with Docker and load into microsandbox after every change to the `Dockerfile` or
`mise/`:

```bash
docker build -t agent-box .
docker save agent-box | msb image load -t agent-box
```

To check microsandbox has your latest build, compare layers from `docker image inspect` and
`msb image inspect`; `msb image ls` CREATED doesn't change on reload.

**Agents:** uncomment them in the `Coding agents` section of the `Dockerfile` (Claude Code
and opencode are on by default), or add a `RUN` line. They install into `~/.local`, so they
can update themselves.

| Build arg               | Default   | Effect                                                                    |
|-------------------------|-----------|---------------------------------------------------------------------------|
| `MISE_VERSION`          | `2026.10` | Latest mise of that month                                                 |
| `PHP_VERSION`           | `8.5`     | PHP CLI from [Sury](https://packages.sury.org/php/) (PHP image only)      |
| `COMPOSER_VERSION`      | `2`       | Latest Composer of that major (PHP image only)                            |
| `USER_UID` / `USER_GID` | `1000`    | Owner of files the agent creates                                          |

### Working in it

```bash
agentbox up agent-box --name my-app -w ~/code/my-app
msb exec my-app -- bash -l
```

Log in to your agents once and set your git identity. `/home/agent-box` is the named volume
`agent-box-home`, shared by every `agent-box` box and kept across `down`, so logins,
updates, history and toolchains carry over.

microsandbox volumes start empty, so the image keeps its home in `/opt/agent-box-home` and
`agent-box-entrypoint` copies it into a volume without `.bashrc`. The profile's
`setup_script` runs the entrypoint on every boot, since `Sandbox.create` skips the image
entrypoint. The profile uses `public` networking: mise downloads from too many hosts for an
allowlist.

### Toolchain presets

mise installs toolchains on first use into the home volume. Node.js and `gh` are always on;
the rest are presets selected with `MISE_ENV`:

| Preset   | Tools                                                    |
|----------|----------------------------------------------------------|
| `java`   | Temurin JDK 27, Maven, Gradle, jbang                     |
| `go`     | Go, golangci-lint                                        |
| `node`   | corepack (`yarn`, `pnpm` per `packageManager`), bun      |
| `python` | Python 3.14, uv                                          |

```bash
msb exec -e MISE_ENV=java,go my-app -- agent-box-entrypoint bash -l
```

To make it permanent, copy the `agent-box` profile into your `profiles.toml` with
`env = { MISE_ENV = "java,go" }`. The first start with a new preset takes up to a minute.
Presets are pinned mise configs in [`mise/`](mise/); add e.g. `mise/config.rust.toml` and
rebuild. There's no `python3` without the `python` preset.

A project's `mise.toml` overrides presets. mise ignores it until trusted, since it can run
code: read it, then `mise trust && mise install`.

### PHP image

mise can only compile PHP from source, so PHP is a separate image from Sury's packages:

```bash
docker build --target php -t agent-box:php .
docker save agent-box:php | msb image load -t agent-box:php
```

Use it via a profile with `image = "agent-box:php"`. It has bcmath, curl, gd, imagick, intl,
mbstring, mysql, readline, redis, sqlite3, xml and zip; add more to the `apt-get install`
line (e.g. `php${PHP_VERSION}-pgsql`).

**Xdebug** is installed but not loaded, since loading it slows PHP ~60 %. Use
`with-xdebug php artisan test --coverage`, or `PHP_INI_SCAN_DIR=:/etc/php/xdebug.d` for a
shell. Step debugging needs `network = "unrestricted"` and
`XDEBUG_CONFIG="client_host=<host address>"`.

### Docker inside the sandbox

The `docker` target adds Docker Engine; `agent-box-php-docker` boots it on the PHP image:

```bash
docker build --target docker --build-arg DOCKER_BASE=php -t agent-box:php-docker .
docker save agent-box:php-docker | msb image load -t agent-box:php-docker
agentbox up agent-box-php-docker --name my-app -w ~/code/my-app
```

A profile of your own needs both `setup_script` and `disks`:

```toml
[profiles.docker]
image = "agent-box:docker"
cpus = 4
memory_mib = 4096
network = "public"
named_volumes = { "/home/agent-box" = "agent-box-home" }
disks = { "/var/lib/docker" = 20480 }
setup_script = "agent-box-entrypoint agent-box-start-dockerd"
```

- `setup_script` starts dockerd via [`start-dockerd.sh`](start-dockerd.sh) and waits for it;
  on failure the boot fails with the tail of `/tmp/dockerd.log`.
- `disks` gives `/var/lib/docker` its own sparse 20 GiB ext4 disk, because Docker can't run
  on the microVM's overlayfs root. It survives a stop; `agentbox down` deletes it.

Restart a stopped box with `agentbox start my-app`, not `msb start`: `msb start` skips
`setup_script`, so dockerd stays down (then run `msb exec my-app -- agent-box-start-dockerd`).

### Updating

- **Agents:** update inside the box (`claude update`, `opencode update`).
- **Toolchains:** bump versions in `mise/*.toml` and rebuild. `mise outdated --bump` lists
  newer ones, `mise prune` cleans up.
- **apt packages** are pinned to the [snapshot.debian.org](https://snapshot.debian.org) date
  of the pinned `debian:trixie` digest. Bump the digest and update the pins
  (`apt-cache policy <package>`). Sury has no snapshot, so only the PHP minor is pinned.
- `docker build --pull --no-cache -t agent-box .` picks up the newest mise and Composer.

The home volume is seeded once, so an agent added to the `Dockerfile` later must be installed
inside the box, or reset the volume with `msb volume rm agent-box-home`.

### Secrets

Put API keys in `secrets`, not `env`. The value is read from your shell variable of the same
name at boot:

```toml
secrets = { OPENROUTER_API_KEY = { hosts = ["openrouter.ai"] } }
```

Inside, the variable holds a placeholder. microsandbox swaps in the real key in the
`Authorization` header (or `header = "..."`) of HTTPS requests to `hosts`, so the agent never
sees it, and blocks the placeholder going anywhere else. Hosts in `tls_bypass` keep their
real certificate (needed e.g. for containers in Docker) but are never inspected.

With `allowlist`, every allowed domain except the secret's hosts is bypassed automatically,
and the secret's hosts must be in `allowed_domains`. With `public`, `corporate` or
`unrestricted`, list bypasses yourself:

```toml
network = "public"
tls_bypass = ["*.docker.io", "*.docker.com"]
```

Boot fails if a secret host is missing from the allowlist, is in `tls_bypass`, or the
variable isn't set. A name can't be in both `env` and `secrets`. `offline` skips secrets.

Don't pass keys with `msb exec -e` (the guest sees them) or mount host credential folders
(`~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.claude`). For git push, use HTTPS with
a fine-grained token, e.g. `GH_TOKEN` plus `gh auth setup-git`.

## Profiles

Built-ins (`src/agentbox/profiles.py`):

| Profile                | Image                                                             | Network |
|------------------------|-------------------------------------------------------------------|---------|
| `airgapped`            | `python:3.11-slim`                                                | offline |
| `agent-box`            | `agent-box` ([built here](#the-agent-box-image))                  | public  |
| `agent-box-php-docker` | `agent-box:php-docker` ([built here](#docker-inside-the-sandbox)) | public  |

Network modes:

- `offline`: no egress; `exec` and `fs` still work.
- `allowlist`: DNS and TCP/443 to `allowed_domains` only (leading dot = subdomains too).
  Matched by TLS hostname and only on IPs resolved from an allowed domain. Metadata and
  plain HTTP (including Debian's apt mirrors) stay blocked.
- `public`: public internet; private, host-local and metadata ranges blocked.
- `corporate`: public plus private networks (e.g. VPN intranet), DNS rebind protection off.
  Host and metadata stay blocked. For a few internal hosts, prefer `allowlist` with
  `allow_private_dns`.
- `unrestricted`: everything, including the host. Local debugging only.

Add your own in `profiles.toml`; user profiles override built-ins of the same name:

```toml
[profiles.rust]
image = "rust:1-slim"
cpus = 8
memory_mib = 8192
network = "allowlist"
allowed_domains = [".crates.io", ".static.crates.io"]
setup_script = "rustup component add clippy"

[profiles.corporate]   # intranet hosts resolve to private IPs
image = "agent-box"
network = "allowlist"
allowed_domains = [".corp.com", ".pypi.org", ".files.pythonhosted.org"]
allow_private_dns = true   # every allowed domain may now resolve privately
```

Persistent storage: `named_volumes = { "/home/me" = "my-home" }` is shared by every sandbox
naming it; `disks = { "/var/lib/docker" = 20480 }` is an ext4 disk (MiB) owned by one sandbox
and deleted with it.

Config lookup order:

1. `--config` / `-c`
2. `$AGENTBOX_CONFIG`
3. `$XDG_CONFIG_HOME/agentbox/profiles.toml` (if absolute)
4. `~/.config/agentbox/profiles.toml`

agentbox never writes these. A missing default file (3, 4) just means built-ins only; a file
named by 1 or 2 must exist. An invalid file exits 2 with the file and field named.

## Development

```bash
uv sync
uv run ruff check --fix .
uv run ruff format .
uv run mypy
uv run pytest                         # unit tests; no microVM needed
uv run pytest -m integration          # boots real microVMs
```

Integration tests are skipped on hosts without a microsandbox runtime. Coverage is enforced
at 100% (statements and branches); unit tests use the fakes in `tests/fakes.py`.

Dependencies are pinned in `pyproject.toml` and locked in `uv.lock`. To bump: `uv lock
--upgrade && uv sync`, then copy the versions back into the `==` pins.

## Layout

```
src/agentbox/
  config.py                 AgentProfile / WorkspaceMount / NetworkMode — pure data
  microsandbox_adapter.py   profile -> Sandbox.create kwargs; the only SDK-aware module
  profiles.py               built-in profiles + TOML loading
  session.py                AgentSession, open_session, launch_detached, list/destroy
  cli.py                    typer entry point: run, up, start, stop, ls, down, profiles, show
  errors.py                 AgentboxError hierarchy
Dockerfile                  the agent-box image, with entrypoint.sh, mise/, start-dockerd.sh,
                            docker-entrypoint.sh
tests/
  fakes.py                  test doubles for the microsandbox SDK
  conftest.py               fixtures + integration-test gating
```

The SDK is imported lazily, so `version`, `profiles` and `show` start fast.

## References

- [microsandbox docs](https://docs.microsandbox.dev)
- [Python SDK reference](https://docs.microsandbox.dev/sdk/python/sandbox)
