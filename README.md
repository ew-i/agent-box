# agent-box

[![build](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml) [![coverage](https://raw.githubusercontent.com/ew-i/badges/main/agent-box/coverage.svg)](https://github.com/ew-i/agent-box) [![codeql](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml) ![Python](https://img.shields.io/badge/python-3.11_%E2%80%93_3.15-yellow?labelColor=blue)

`agentbox` runs coding agents and their generated code inside [microsandbox](https://microsandbox.dev/) microVMs. Declarative profiles define each VM's image, resources, workspace, lifetime, secrets, storage, and network policy. Each sandbox has its own kernel, providing a stronger isolation boundary than a container alone.

## Key features

- One-shot commands (`run`) and persistent development boxes (`up`, `start`, `stop`, `down`).
- Built-in and TOML-defined profiles, with validated per-run overrides.
- Offline, allowlisted, public, corporate, and unrestricted network modes.
- Host-held secrets that are only substituted into approved HTTPS request headers and destinations.
- A Debian-based coding image with mise-managed toolchain presets, plus optional PHP and Docker-in-the-sandbox images.

> [!CAUTION]
> **Workspace mounts are read-only by default.** To let an agent edit a checkout, explicitly set `readonly = false` in that profile's `workspace` settings. A writable mount lets the agent change `.git/hooks/` or set `core.fsmonitor` in `.git/config`; those can execute on the host the next time Git (or a shell prompt) runs there.
>
> Read-only applies only to the workspace, not named volumes. The built-in `agent-box` profiles share `/home/agent-box` across boxes, which may contain agent logins, history, and toolchains. For an untrusted project, assign it a separate named home volume in a custom profile.

## Contents

- [Requirements and installation](#requirements-and-installation)
- [Quick start](#quick-start)
- [Commands and box lifecycle](#commands-and-box-lifecycle)
- [Profiles and configuration](#profiles-and-configuration)
- [Network and secrets](#network-and-secrets)
- [Storage and isolation](#storage-and-isolation)
- [The `agent-box` images](#the-agent-box-images)
- [Development and tests](#development-and-tests)
- [Deployment and package builds](#deployment-and-package-builds)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [References and license](#references-and-license)

## Requirements and installation

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
- A supported microsandbox host: Linux with KVM, or macOS on Apple Silicon. Run `msb doctor` to check the runtime and host setup.
- Docker is optional; it is needed only to build the bundled images.

From a clone of this repository, install the editable CLI and the `msb` executable:

```bash
uv tool install --editable . --with-executables-from microsandbox
agentbox profiles
```

The install adds `agentbox` and `msb` to `~/.local/bin`. After pulling source changes, the editable CLI uses the updated code immediately. If dependencies changed, reinstall with `--reinstall`:

```bash
uv tool install --editable . --with-executables-from microsandbox --reinstall
```

To load secrets from `~/.config/agentbox/.env` when invoking `agentbox` directly, add this function to your shell rc file:

```bash
agentbox() { (set -a; [ -f ~/.config/agentbox/.env ] && . ~/.config/agentbox/.env; command agentbox "$@"); }
```

The `.env` file is sourced as shell code; keep it private and use only trusted contents.

The development setup using `uv sync` is described under [Development and tests](#development-and-tests).

## Quick start

Run a command in a fresh, offline Python sandbox:

```bash
agentbox run airgapped 'python -c "print(1)"'
```

Mount a project folder at `/workspace` for the command:

```bash
agentbox run airgapped 'ls -la /workspace' --workspace ./my-project
```

The `airgapped` profile above uses a registry image, so it is the quickest first check. To start a persistent box from this repository's `agent-box` image, run these from the repository root (the directory containing `Dockerfile`) to build it with Docker and load it into microsandbox:

```bash
docker build -t agent-box .
docker save agent-box | msb image load -t agent-box
```

Then start and connect to the box. Its workspace mount is read-only; see [Defining a profile](#defining-a-profile) to explicitly allow writes.

```bash
cd ~/code/my-app
agentbox up agent-box --name my-app --workspace .
msb exec my-app -- bash -l
agentbox stop my-app       # keep its state
agentbox start my-app      # start it again and rerun setup_script
agentbox down my-app       # delete its state, keep chat history
```

`agentbox up` prints the box name on stdout, so it can be captured by scripts. Hints go to stderr:

```bash
BOX=$(agentbox up airgapped)
msb exec "$BOX" -- python -c "print('hi')"
agentbox down "$BOX"
```

For a project-oriented shortcut, `Makefile.example` expects a profile named `dev` and runs from the project directory:

```bash
make -f /path/to/agent-box/Makefile.example start
make -f /path/to/agent-box/Makefile.example exec
```

It also provides `stop` and `del` targets (`del` removes the box). By default it sources `~/.config/agentbox/.env` if present; that file is trusted shell code, so keep it private and use it only for values you trust. `NAME`, `WORKSPACE`, `READONLY` (which repeats the read-only default), and `ENV_FILE` can be overridden on the make command line. Writes require `readonly = false` in the `dev` profile.

## Commands and box lifecycle

| Command | What it does |
| --- | --- |
| `agentbox run PROFILE SCRIPT` | Start a fresh box, run a shell snippet, stream its output, then stop the box. The default profile behavior also removes it. |
| `agentbox up PROFILE` | Start a detached, persistent box. Connect with `msb exec`. |
| `agentbox start NAME` | Restart a stopped `up` box and rerun its profile's `setup_script`. |
| `agentbox stop NAME` | Stop a box while keeping its state when possible. A default ephemeral `run` box is removed instead. |
| `agentbox down NAME` | Stop and delete the box and its persistent state, preserving history in shared volumes. |
| `agentbox ls` | List sandboxes known to the microsandbox runtime. |
| `agentbox profiles` | List built-in and configured profiles. |
| `agentbox show PROFILE` | Print the resolved profile as JSON; values in `env` are masked. |
| `agentbox version` | Print the installed version. |

`run` and `up` accept `--workspace`/`-w`, `--readonly`, `--network`/`-n`, `--idle-timeout`, `--max-duration`, and `--config`/`-c`. A workspace supplied on the command line is read-only by default; write access is configured explicitly in the profile. `run` also accepts `--timeout`, which limits the command itself. `up` also accepts `--name`; names may contain letters, digits, `.`, `_`, and `-`, must start with a letter or digit, and may be at most 128 characters. `show` accepts the same profile overrides and `--for run|up`; `profiles`, `start`, `stop`, and `down` accept `--config`. `stop` and `down` accept `--force` to kill instead of stopping gracefully; `down` preserves history in shared volumes by default (`--keep-history`); use `--no-keep-history` to delete it.

For example, preview the profile after applying flags without booting a sandbox:

```bash
agentbox show agent-box --workspace ./my-app --network public
agentbox show agent-box --for up --max-duration 7200
```

`--idle-timeout` stops an unused box; `--max-duration` stops it after the specified total lifetime, even if busy. Both accept seconds, and `0` disables that limit.

### `run` versus `up`

| | `run` | `up` |
| --- | --- | --- |
| Command | Required shell snippet | No snippet; connect with `msb exec` |
| State | Stops after the command; the default `ephemeral = true` removes it | Kept until `agentbox down` |
| Time limits | Uses the profile's limits unless overridden | No limits by default; set them explicitly if desired |
| Failure/stop behavior | A default ephemeral box is removed when stopped; `ephemeral = false` keeps stopped state | `stop` keeps it for a later `start` |

A stopped box brought up again with the same name uses its saved runtime settings. New command-line values or edited profiles do not replace those settings. `up` detects changes to the profile name, workspace, and stored network/home-volume settings; if it reports a conflict—or if you need to apply changed settings—remove the box with `agentbox down NAME` and create it again. Rotated secrets and changed time limits also require recreating the box.

`agentbox start NAME` starts a stopped box using its saved VM state and reruns `setup_script`. Use it instead of `msb start` for an agentbox-managed box; `msb start` does not rerun the profile setup script (for example, it will not restart dockerd in the Docker image).

## Profiles and configuration

Profiles are TOML data. User-defined profiles are merged with the built-ins and override a built-in with the same name. `agentbox` does not write or modify the profile file.

Review profile files before using them: `setup_script` is executed inside the VM after boot. A project `mise.toml` can also run code once trusted; see [Toolchain presets](#toolchain-presets).

Configuration lookup order:

1. `--config` / `-c` on the command.
2. `$AGENTBOX_CONFIG`.
3. `$XDG_CONFIG_HOME/agentbox/profiles.toml`, if `XDG_CONFIG_HOME` is an absolute path.
4. `~/.config/agentbox/profiles.toml`.

A missing default file means “built-ins only.” A missing file explicitly named with `--config` or `AGENTBOX_CONFIG` is an error. Invalid TOML and unknown profile fields are reported with the file and field name.

The repository includes a sample profile at [`.config/agentbox/profiles.toml.example`](.config/agentbox/profiles.toml.example). To use it as your default config:

```bash
mkdir -p ~/.config/agentbox
cp .config/agentbox/profiles.toml.example ~/.config/agentbox/profiles.toml
```

It defines a `dev` profile for the `agent-box:php-docker` image, with an allowlisted network, an OpenRouter secret, and an explicitly writable workspace for the Makefile helper. Build and load that image first, and provide `OPENROUTER_API_KEY` in the host environment; the example file intentionally contains no key value. The [Makefile helper](#quick-start) can load that variable from `~/.config/agentbox/.env`.

### Built-in profiles

| Profile | Image | Network | Other settings |
| --- | --- | --- | --- |
| `airgapped` | `python:3.11-slim` | `offline` | Safest built-in for code that needs no network. |
| `agent-box` | `agent-box` | `public` | 4 CPUs, 4096 MiB memory, shared `agent-box-home` volume, 2-hour maximum for `run`. |
| `agent-box-php-docker` | `agent-box:php-docker` | `public` | Same home and resource settings; includes PHP and Docker Engine with a per-box Docker data disk. |

Profile defaults, unless a profile overrides them: image `python:3.11-slim`, 2 CPUs, 2048 MiB memory, workdir `/workspace`, shell `/bin/bash`, `network = "allowlist"`, no allowed domains, 600-second idle timeout, 3600-second maximum duration, and `ephemeral = true`. The built-in `agent-box` profiles have their own explicit settings.

### Defining a profile

Save this as `~/.config/agentbox/profiles.toml`:

```toml
[profiles.rust]
image = "rust:1-slim"
cpus = 8
memory_mib = 8192
network = "allowlist"
allowed_domains = [".crates.io", ".static.crates.io", ".static.rust-lang.org"]
setup_script = "rustup component add clippy"

[profiles.rust.workspace]
host_path = "~/code/my-rust-project"
guest_path = "/workspace"
readonly = false
```

Run it:

```bash
agentbox run rust 'cargo test' --workspace ~/code/my-rust-project
agentbox up rust --name rust-work --workspace ~/code/my-rust-project
```

The profile table key is the profile name. Profile names start with a letter or digit, may contain letters, digits, `.`, `_`, and `-`, and are at most 64 characters. Runtime box names follow the same character rule and are at most 128 characters. The host workspace path may be relative or use `~`; paths that mount the home directory itself or a parent containing it are refused. A workspace can have a different absolute guest path, but it cannot share a guest mount path with a named volume or disk.

### Profile fields

Guest paths (`workdir`, workspace/volume/disk paths) must be absolute.

| Field | Meaning / default |
| --- | --- |
| `description` | Optional text shown by `agentbox profiles`. |
| `image` | VM image; defaults to `python:3.11-slim`. |
| `cpus`, `memory_mib` | Resources; defaults to `2` CPUs and `2048` MiB. Valid ranges are 1–32 CPUs and 256–65536 MiB. |
| `workdir`, `shell` | Guest working directory and shell; default `/workspace` and `/bin/bash`. |
| `workspace` | Optional host bind mount: `host_path`, optional `guest_path` (default `/workspace`) and `readonly` (default `true`). Set `false` explicitly to allow writes. |
| `env` | Environment variables passed into the guest. Do not put API keys here; the guest can read them. `{sandbox}` in a value is replaced with the box name. |
| `network`, `allowed_domains` | Egress mode and domains for `allowlist`; default mode is `allowlist` with no allowed domains. |
| `allow_private_dns`, `allow_http` | Extra allowlist behavior; both default to `false` and only apply to `allowlist`. See [Network and secrets](#network-and-secrets). |
| `tls_bypass` | Hosts that keep their real TLS certificate when secret substitution enables TLS inspection. |
| `secrets` | Host environment variables permitted for substitution only to declared hosts and a single request header (`authorization` by default; set `header` to change it). |
| `named_volumes` | Guest path → named volume. A named volume is shared by every box that names it. |
| `disks` | Guest path → owned ext4 disk size in MiB. The disk belongs to one box and is deleted with it. |
| `idle_timeout_s`, `max_duration_s` | Automatic idle and total lifetime limits in seconds; defaults `600` and `3600`. `0` means no limit. |
| `ephemeral` | Whether stopping removes the box; default `true`. `up` forces it to `false`. |
| `setup_script` | Optional shell snippet run after each boot, including when `agentbox start` restarts a box. |

## Network and secrets

| Mode | Access |
| --- | --- |
| `offline` | No egress. Local `exec` and filesystem operations still work. Secrets are inactive. |
| `allowlist` | DNS plus TCP/443 to `allowed_domains` only. A leading dot, such as `.example.com`, allows the domain and its subdomains. Other traffic is denied by default. |
| `public` | Public internet; private, host-local, and metadata ranges are blocked. |
| `corporate` | Public and private networks, for example an intranet behind a VPN; the host and metadata remain blocked. DNS rebind protection is off. |
| `unrestricted` | All destinations, including the host. Use only for local debugging. |

In `allowlist` mode, `allowed_domains` accepts exact hostnames or a leading dot for the domain and its subdomains; `*` wildcards are not supported. `allow_http = true` also permits TCP/80 to allowed domains. Plain HTTP has no TLS hostname to check, so it is matched by resolved IP; other sites sharing a CDN IP can consequently be reachable over HTTP. `allow_private_dns = true` permits allowlisted names to resolve to private IPs and disables DNS rebinding protection for the sandbox. Use it only when that tradeoff is understood.

### Passing API keys safely

Declare secrets in a profile, without storing their values in TOML:

```toml
[profiles.agent]
image = "agent-box"
network = "allowlist"
allowed_domains = ["openrouter.ai"]
secrets = { OPENROUTER_API_KEY = { hosts = ["openrouter.ai"] } }
```

Set `OPENROUTER_API_KEY` in the host environment before starting the box, using your local secret manager or another protected method. The variable name in `secrets` identifies the host variable to read. The guest sees a placeholder; microsandbox substitutes the value into the configured request header (default `authorization`) only for the declared hosts and blocks that placeholder from being sent elsewhere. A missing host variable prevents startup.

Every secret host must be reachable by the profile's network policy. In `allowlist` mode it must also appear in `allowed_domains`. A secret host cannot be listed in `tls_bypass`, because bypassed HTTPS traffic is not inspected and cannot receive the value. Do not pass keys with `msb exec -e`, put them in `env`, or mount host credential directories such as `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, or `~/.claude`.

For Git over HTTPS, use a fine-grained token supplied as a secret and configure `gh auth setup-git` inside the box; do not mount host credentials.

## Storage and isolation

- **Workspace:** A host directory is mounted at `/workspace` by `--workspace`/`-w`, or at the profile's `workspace.guest_path`. It is read-only by default; set `workspace.readonly = false` in the profile to allow writes. `--readonly` requires a workspace mount and is enforced by the host mount, including against root in the VM.
- **Named volumes:** Shared between boxes that use the same volume name. The built-in `agent-box` profiles share `agent-box-home`, preserving agent logins and installed mise toolchains across `stop` and `down`. Per-box history such as OpenCode's database is preserved by default on `down`; use `--no-keep-history` to delete it.
- **Owned disks:** Ext4 disks belong to one box, persist across `stop`/`start`, and are deleted with the box. The Docker image uses one for `/var/lib/docker` because Docker cannot use the VM's overlay filesystem as its data root.
- **Per-box files:** Put files that should live in a shared volume but remain separate for each box under a path containing `{sandbox}` in an `env` value. The built-in image does this for OpenCode's SQLite database; the name is replaced at boot. `agentbox down NAME --no-keep-history` removes these files when it can. `agentbox down NAME` preserves them by default so `agentbox up PROFILE --name NAME` can reuse them. If a box is already stopped, its files cannot be cleaned from inside it and may remain in the volume.

OpenCode's database is per box because multiple microVMs cannot safely share one SQLite database. OpenCode background services are disabled in the seeded home because multiple boxes share that home and would otherwise stop one another.

For a separate home on a custom profile, keep the image's setup script and give the volume a unique name:

```toml
[profiles.untrusted]
image = "agent-box"
network = "offline"
named_volumes = { "/home/agent-box" = "agent-box-home-untrusted" }
env = { OPENCODE_DB = "/home/agent-box/.local/share/opencode/opencode-{sandbox}.db" }
setup_script = "agent-box-entrypoint true"
```

Choose the network mode to fit the task; `offline` avoids egress, while `public` permits internet access but still blocks private and host-local ranges.

## The `agent-box` images

The default image is based on Debian 13 and includes Git, curl, CA certificates, ripgrep, `fd-find`, jq, less, nano, htop, vim, sudo, unzip, procps, rsync, and build tools. The `agent-box` user has passwordless sudo inside the VM. Node.js and GitHub CLI are installed via mise, along with Claude Code and OpenCode; Codex CLI and Gemini CLI are optional in the Dockerfile. More language toolchains are installed on first use into the persistent home volume. Microsandbox named volumes start empty; the image includes a seed home that `agent-box-entrypoint` copies without overwriting existing files. When the bundled agent launcher set changes, the entrypoint merges missing files from the new seed into an existing home as well, so newly enabled agents appear on the next start without replacing existing installs or logins. To build and load the image for microsandbox:

```bash
docker build -t agent-box .
docker save agent-box | msb image load -t agent-box
```

Rebuild and load again after changing `Dockerfile` or `mise/`. Other agent installation lines are commented in the Dockerfile and can be enabled there. Installed agents live under the home volume and can self-update. Newly enabled agents are added to an existing home the next time the VM starts; existing files are never overwritten, and removing an installer from the image does not uninstall that agent from existing homes.

### Toolchain presets

Node.js and `gh` are always installed. Select other mise toolchains with `MISE_ENV`:

| Preset | Tools |
| --- | --- |
| `java` | Temurin JDK, Maven, Gradle, jbang |
| `go` | Go and golangci-lint |
| `node` | corepack (Yarn/pnpm follow the project's `packageManager` pin) and bun |
| `python` | Python and uv |

```bash
msb exec -e MISE_ENV=java,go my-app -- agent-box-entrypoint bash -l
```

To make a preset permanent, add the `MISE_ENV` setting to the profile's `env` table. Tool versions are pinned in [`mise/`](mise/); add a config such as `mise/config.rust.toml` and rebuild to add another preset. There is no `python3` in the base image unless the `python` preset is selected.

A project `mise.toml` can run code. Review it before trusting it, then run `mise trust` and `mise install` inside the box.

### PHP image

Build the PHP variant from Sury packages and load it into microsandbox:

```bash
docker build --target php -t agent-box:php .
docker save agent-box:php | msb image load -t agent-box:php
```

It includes Composer and PHP extensions for bcmath, curl, gd, imagick, intl, mbstring, MySQL, readline, Redis, SQLite, XML, and zip. Add more PHP packages in the `php` stage of the Dockerfile if required.

Xdebug is installed but disabled by default because it slows PHP. Enable it for one command with `with-xdebug php artisan test --coverage`, or for a shell with `PHP_INI_SCAN_DIR=:/etc/php/xdebug.d`. Step debugging also requires the sandbox to reach the debug host; configure the network and `XDEBUG_CONFIG="client_host=<host address>"` accordingly.

### Docker inside a sandbox

The `docker` image target adds Docker Engine. To build the PHP + Docker image used by the `agent-box-php-docker` profile:

```bash
docker build --target docker --build-arg DOCKER_BASE=php -t agent-box:php-docker .
docker save agent-box:php-docker | msb image load -t agent-box:php-docker
agentbox up agent-box-php-docker --name my-app --workspace ~/code/my-app
```

The built-in profile starts dockerd on boot and assigns `/var/lib/docker` a private 20 GiB disk. `agentbox start` reruns the startup script after a stop. If you define your own Docker profile, configure both `setup_script` and `disks`:

```toml
[profiles.docker]
image = "agent-box:docker"
cpus = 4
memory_mib = 4096
network = "public"
named_volumes = { "/home/agent-box" = "agent-box-home" }
env = { OPENCODE_DB = "/home/agent-box/.local/share/opencode/opencode-{sandbox}.db" }
disks = { "/var/lib/docker" = 20480 }
setup_script = "agent-box-entrypoint agent-box-start-dockerd"
```

Docker-in-Docker in an ordinary Docker container requires a privileged container. The microsandbox profile runs inside a VM and does not need that outer Docker setup.

### Image build arguments and updates

| Build argument | Default | Applies to |
| --- | --- | --- |
| `MISE_VERSION` | `2026.10` | mise release month |
| `PHP_VERSION` | `8.5` | PHP image |
| `COMPOSER_VERSION` | `2` | PHP image |
| `USER_UID`, `USER_GID` | `1000` | Guest account IDs, useful for matching host file ownership on Linux |
| `CLAUDE_CODE_VERSION` | `2.1.322` | Default-installed Claude Code |
| `OPENCODE_VERSION` | `2.2.7` | Default-installed OpenCode |
| `CODEX_VERSION` | `0.167.1` | Optional Codex CLI |
| `GEMINI_CLI_VERSION` | `0.64.0` | Optional Gemini CLI |

Update agents inside a box (`claude update`, `opencode update`, or the agent's own update command). To update base packages or toolchains, change the Dockerfile or `mise/` pins, rebuild, and reload the image. The base Debian packages use a pinned Debian snapshot; bumping the base image digest is part of updating those pins. Rebuilding the image does not overwrite files in an existing home volume; it only adds missing seed files when the bundled agent launcher set changes.

## Development and tests

Install the locked development dependencies:

```bash
uv sync
```

Run the checks used by CI:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

`pytest` runs unit tests against the fakes in `tests/fakes.py`, excludes integration tests by default, and enforces 100% statement and branch coverage. Integration tests boot a real microVM and may be skipped when the host lacks a supported microsandbox runtime:

```bash
uv run pytest -m integration
```

Dependencies are pinned in `pyproject.toml` and locked in `uv.lock`. To upgrade them, update the lock and sync, then update the direct version pins in `pyproject.toml`:

```bash
uv lock --upgrade
uv sync
```

## Deployment and package builds

This repository has no configured production deployment target. The GitHub Actions workflow runs lint, type checks, tests, and `uv build`; it uploads the built wheel and source distribution as workflow artifacts but does not publish them to a package index. To build locally:

```bash
uv build
```

The `agent-box` Docker images are local microsandbox VM images, not a deployment of a hosted service. See [The `agent-box` images](#the-agent-box-images) for build and load commands.

## Project layout

```text
src/agentbox/
  config.py                 Profile, workspace, network, and validation data
  overrides.py              How CLI flags change a profile for run/up/show
  profiles.py               Built-in profiles and TOML configuration loading
  microsandbox_adapter.py   Translation from profiles to microsandbox SDK objects
  session.py                VM lifecycle and streamed command execution
  boxes.py                  Listing, stopping, and deleting named sandboxes
  commands.py               CLI hints for agentbox and msb commands
  cli.py                    Typer command-line interface
  errors.py                 User-facing error types and SDK error wrapping
  __main__.py               python -m agentbox entry point
mise/                       Base and optional mise toolchain configurations
Dockerfile                  Default, PHP, and Docker image targets
entrypoint.sh               Seed the home volume and install selected mise tools
start-dockerd.sh            Start Docker Engine inside the Docker image
docker-entrypoint.sh        Run a command while managing dockerd's lifetime
tests/                      Unit, CLI, adapter, session, and integration tests
```

The microsandbox SDK is imported lazily for CLI commands that need the runtime, so `agentbox version`, `agentbox profiles`, and `agentbox show` can run without starting a microVM.

## Troubleshooting

- **Runtime or virtualization errors:** run `msb doctor`; on Linux, confirm KVM is available and accessible. On macOS, use Apple Silicon.
- **`msb` or `agentbox` not found:** ensure `~/.local/bin` is on `PATH`, or use the project environment with `uv run`.
- **Profile file error:** check TOML syntax and field names. When using a non-default file, pass the same `--config` path used to create the box.
- **A secret is missing:** set the corresponding host environment variable before boot. The secret value is not stored in the profile.
- **A stopped box conflicts with current settings:** it keeps the VM configuration it was created with. Use `agentbox down NAME` and create it again to apply new settings; this deletes its box state.
- **Image not found:** build the image and load it with `msb image load` before starting the corresponding profile.
- **Docker daemon is unavailable in the Docker image:** use the `agent-box-php-docker` profile or configure its `setup_script` and private `/var/lib/docker` disk in a custom profile. Restart with `agentbox start`, which reruns setup.

## References and license

- [microsandbox documentation](https://docs.microsandbox.dev)
- [Python SDK reference](https://docs.microsandbox.dev/sdk/python/sandbox)
- [uv documentation](https://docs.astral.sh/uv/)
- [mise documentation](https://mise.jdx.dev/)
- [MIT License](LICENSE)
