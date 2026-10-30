# agent-box

Sandboxed execution environments for coding agents, backed by
[microsandbox](https://microsandbox.dev/) microVMs.

[![build](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/deploy.yml) [![coverage](https://raw.githubusercontent.com/ew-i/badges/main/agent-box/coverage.svg)](https://github.com/ew-i/agent-box) [![codeql](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml/badge.svg)](https://github.com/ew-i/agent-box/actions/workflows/codeql.yml) ![Python](https://img.shields.io/badge/python-3.11_%E2%80%93_3.15-yellow?labelColor=blue)

A coding agent that can run commands is only as safe as the box it runs them in. `agentbox`
is a CLI that boots microsandbox microVMs from **declarative profiles**: each profile pins the
image, CPU/memory limits, lifetime, workspace mount and — most importantly — the network
policy an agent is allowed to use. Booting a sandbox is then a one-liner, and what the
agent may reach is reviewable data instead of scattered command-line flags.

Each sandbox is a hardware-isolated microVM with its own kernel, so generated code cannot
escape into the host the way a container breakout can.

The workspace you mount is the exception: the agent can write to it, `.git/` included. A hook
in `.git/hooks/` or a `core.fsmonitor` command in `.git/config` runs on the host the next
time you use git there, and many shell prompts run `git status` on their own. Mount with
`--readonly` when the agent only needs to read: the host enforces it, so even root in the box
can't undo it. A read-only mount of just `.git` doesn't help, since root in the box can unmount
it and write through the workspace below. With a writable workspace, check `.git/hooks` and
`.git/config` before running git on the host.

`--readonly` covers the workspace only. The `agent-box` profiles share one writable home
volume (see [Working in it](#working-in-it)), so an agent in one box can change `~/.bashrc` or
agent settings that every other box runs, including a box with a writable workspace. Give an
untrusted project a profile with its own home, e.g.
`named_volumes = { "/home/agent-box" = "agent-box-home-untrusted" }`.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A host that can run microVMs: Linux with KVM (`/dev/kvm`), or macOS on Apple Silicon.
  Check with `msb doctor`.

Most people already have `msb` from the microsandbox installer
(`curl -fsSL https://install.microsandbox.dev | sh`). It is not strictly required: the
`microsandbox` wheel bundles the runtime and an `msb` binary, so `uv sync` alone is enough
to boot sandboxes — you just reach that copy as `uv run msb`.

## Setup

```bash
uv sync
uv run agentbox profiles
```

## Usage

One-shot — boot, run, tear down:

```bash
uv run agentbox run airgapped 'python -c "print(1)"'
uv run agentbox run airgapped 'ls /workspace' -w ./my-project
```

Long-lived — boot once, connect as often as you like:

```bash
uv run agentbox up agent-box --name mybox -w ./my-project
msb exec mybox -- bash                 # interactive shell
msb exec mybox -- git status           # one command
uv run agentbox ls                       # name, status, profile
uv run agentbox down mybox               # stop + delete state
```

`up` prints **only the sandbox name** on stdout, so it composes:

```bash
BOX=$(uv run agentbox up airgapped)
msb exec "$BOX" -- python -c "print('hi')"
uv run agentbox down "$BOX"
```

Hints, warnings, `setup_script` output and `ls`-is-empty messages go to stderr.

`up` with the name of a stopped box starts that box again, with the settings it was created
with. If you now ask for another profile, workspace, `--readonly` or `--network`, or you edited
the profile's `network`, `allowed_domains`, `tls_bypass`, `secrets` or `named_volumes`, it
refuses instead, since the box would keep its old mounts and network: `agentbox down` it first. It also keeps
its secret values and time limits: a rotated key or a new `--idle-timeout` or
`--max-duration` only applies after `agentbox down`.

> **Why `uv run agentbox` but bare `msb`?** `agentbox` is a project console script living in
> `.venv/bin`, so it needs `uv run` (or an activated venv). `msb` is normally installed
> system-wide by the microsandbox installer and works as-is. If you only ran `uv sync`,
> the wheel's bundled `msb` is in `.venv/bin` too and needs the prefix — the hints
> `agentbox up` prints detect which case applies.

Other commands:

```bash
uv run agentbox profiles                 # list profiles
uv run agentbox show agent-box           # inspect one as JSON
uv run agentbox version
```

`show` prints the profile that would **actually** boot, not the stored defaults, so it
takes the same override flags as `run` and `up`. `env` values print as `***`, so API keys
in a profile don't end up in your terminal:

```bash
uv run agentbox show airgapped --idle-timeout 0 --max-duration 0   # both print null
uv run agentbox show airgapped -w ./my-project --readonly -n public
```

Because `up` reshapes a profile, ask about it explicitly with `--for up`:

```bash
uv run agentbox show airgapped         # ephemeral true,  idle 600,  max 3600  (as `run` boots it)
uv run agentbox show airgapped --for up # ephemeral false, idle null, max null  (as `up` boots it)
```

### `run` vs `up`

|                   | `run`                          | `up`                                     |
|-------------------|--------------------------------|------------------------------------------|
| Script argument   | required                       | none — you connect later                 |
| Lifetime          | dies when the command finishes | runs until `agentbox down`               |
| Idle / max limits | profile's, as a backstop       | none, unless you pass the flags          |
| `ephemeral`       | `True` (profile default)       | forced `False`, so state survives a stop |
| Output            | the command's stdout/stderr    | the sandbox name                         |

`up` is for development boxes, so it drops the profile's guard rails: the sandbox stays
alive until you run `agentbox down`, no matter how long you leave it idle.

```
$ uv run agentbox up agent-box --name agentbox
agentbox
msb exec agentbox -- /bin/bash
agentbox down agentbox
no time limit; runs until you stop it with the command above
```

Opt back into a limit when you want one — for example on a shared machine:

```bash
uv run agentbox up agent-box --name capped --idle-timeout 600 --max-duration 7200
# auto-stops after 10m idle; --idle-timeout 0 disables it
# stops after 2h even if busy; --max-duration 0 disables it
```

The two limits are independent: `--idle-timeout` fires when nobody is using the sandbox,
`--max-duration` fires regardless of how busy it is. `0` means the same as omitting the
flag. `run` is the opposite case — it executes a one-shot command, so it keeps the
profile's `idle_timeout_s` and `max_duration_s` as a backstop against a hung command.
Use `agentbox show PROFILE --for up` to see the result before booting anything.

## The `agent-box` image

The `Dockerfile` builds the image the `agent-box` profile boots: Debian 13, common dev
tools, [mise](https://mise.jdx.dev) for language toolchains, and the coding agents you pick.
microsandbox runs OCI images, so build it with Docker and load it into microsandbox's own
image store. No registry is needed:

```bash
docker build -t agent-box .
docker save agent-box | msb image load -t agent-box
```

Repeat both steps after every change to the `Dockerfile` or `mise/`. On Apple Silicon the
build is arm64, which is what the microVM runs. To check that microsandbox has your latest
build, compare the layers `docker image inspect` and `msb image inspect` list. The CREATED
column of `msb image ls` doesn't change when you load the image again.

**Choose your agents** in the `Coding agents` section of the `Dockerfile`: uncomment the
ones you want. Claude Code and opencode are on by default. To add another agent, add a
`RUN` line with its Linux install command. `npm install -g` and `uv tool install` both
install into `~/.local`, so agents can update themselves later.

**Build args:**

| Build arg               | Default   | What you get                                                                        |
|-------------------------|-----------|-------------------------------------------------------------------------------------|
| `MISE_VERSION`          | `2026.10` | Latest mise of that month, from the official `jdxcode/mise` image                   |
| `PHP_VERSION`           | `8.5`     | PHP CLI from the [Sury repository](https://packages.sury.org/php/) (PHP image only) |
| `COMPOSER_VERSION`      | `2`       | Latest Composer of that major version (PHP image only)                              |
| `USER_UID` / `USER_GID` | `1000`    | Owner of files the agent creates                                                    |

### Working in it

```bash
uv run agentbox up agent-box --name my-app -w ~/code/my-app
msb exec my-app -- bash -l
```

Inside, log in to your agents once (`claude`, `opencode auth login`) and set your git
identity (`git config --global user.name ...`). The `/home/agent-box` folder is the named
volume `agent-box-home`. It's shared by every `agent-box` sandbox and survives `agentbox
down`, so logins, agent self-updates, session history and installed toolchains carry over.

A new microsandbox volume starts empty: unlike a Docker volume, it isn't filled from the
image. The image therefore keeps a copy of its built home in `/opt/agent-box-home`, and
`agent-box-entrypoint` copies it into a volume that has no `.bashrc` yet. The profile's
`setup_script` runs the entrypoint on every boot, because `Sandbox.create` skips the image
entrypoint.

The profile uses the `public` network mode: mise downloads from too many hosts
(nodejs.org, GitHub, Adoptium, Maven Central, ...) to list in an allowlist.

### Toolchain presets (Java, Go, Node, Python)

mise installs toolchains on first use into the home volume, not into the image. Node.js
and the GitHub CLI `gh` are always on (`mise/config.toml`). Everything else is a preset,
selected with `MISE_ENV`:

| Preset   | Tools                                                                 |
|----------|-----------------------------------------------------------------------|
| `java`   | Temurin JDK 27, Maven, Gradle, jbang                                  |
| `go`     | Go, golangci-lint                                                     |
| `node`   | corepack (`yarn`, `pnpm` as the project's `packageManager` pins), bun |
| `python` | Python 3.14, uv                                                       |

For one shell, run it through the entrypoint so the preset installs first:

```bash
msb exec -e MISE_ENV=java,go my-app -- agent-box-entrypoint bash -l
```

To always use some presets, add a profile in your `profiles.toml` (see
[Profiles](#profiles)) with `env = { MISE_ENV = "java,go" }` and the fields of the
`agent-box` profile.

The first start with a new preset takes up to a minute; after that the tools are cached
in the volume. Presets are plain mise config files in [`mise/`](mise/), pinned to exact
versions. Add your own, e.g. `mise/config.rust.toml` for `MISE_ENV=rust`, then rebuild.
There's no `python3` without the `python` preset. Prefer `uv` over `pip install`.

**Per project:** a `mise.toml` in the project overrides the presets (`mise use
java@temurin-21 maven`). A `mise.toml` can run code, so mise ignores one it hasn't been
told to trust. Read it first, especially in a repo you cloned, then `mise trust && mise
install`.

### PHP image

PHP isn't a mise preset: mise can only compile PHP from source. It's a separate image
built from Sury's prebuilt packages:

```bash
docker build --target php -t agent-box:php .
docker save agent-box:php | msb image load -t agent-box:php
```

Boot it with a user profile that copies `agent-box` and sets `image = "agent-box:php"`.
It has the extensions a typical Laravel app needs: bcmath, curl, gd, imagick, intl,
mbstring, mysql, readline, redis, sqlite3, xml and zip. To add one, append its package
(e.g. `php${PHP_VERSION}-pgsql`) to the PHP `apt-get install` line.

**Xdebug** is installed but not loaded: just loading it makes PHP about 60 % slower, even
with `xdebug.mode=off`. Load it for one command with `with-xdebug php artisan test
--coverage`, or for a whole shell with `PHP_INI_SCAN_DIR=:/etc/php/xdebug.d`. Step
debugging (`XDEBUG_MODE=debug`) has to reach your IDE on the host, which the `public` network
blocks: it needs a profile with `network = "unrestricted"` and
`XDEBUG_CONFIG="client_host=<host address>"`.

### Docker inside the sandbox

The `docker` target adds Docker Engine. The `agent-box-php-docker` profile boots it built on
top of the PHP image:

```bash
docker build --target docker --build-arg DOCKER_BASE=php -t agent-box:php-docker .
docker save agent-box:php-docker | msb image load -t agent-box:php-docker
uv run agentbox up agent-box-php-docker --name my-app -w ~/code/my-app
```

Two things make Docker work in a microVM, and a profile of your own needs both. This one
uses the `docker` target without PHP:

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

- **`setup_script`** runs [`start-dockerd.sh`](start-dockerd.sh), installed in the image
  as `agent-box-start-dockerd`, after the entrypoint's usual home seeding and `mise
  install`. It starts dockerd in the background and waits until it answers. If dockerd
  doesn't start, the boot fails and shows the last lines of `/tmp/dockerd.log`. The image's
  `docker-entrypoint.sh` uses the same script under plain Docker.
- **`disks`** gives `/var/lib/docker` a 20 GiB ext4 disk that belongs to that one sandbox.
  Docker can't keep its data on the microVM's root, which is overlayfs already: `docker run`
  fails with "failed to mount ... overlay ... invalid argument". The disk file is sparse, so
  it only takes the space actually used. Pulled images survive a stop and start, and
  `agentbox down` deletes the disk. Every sandbox has its own, so two boxes don't share an
  image cache.

A box restarted with `msb start` instead of `agentbox up` skips `setup_script`. Start
dockerd there with `msb exec my-app -- agent-box-start-dockerd`.

### Updating

- **Agents** update themselves inside the volume (`claude update`, `opencode update`).
- **Toolchains** are pinned in `mise/*.toml`. Raise a version and rebuild; it installs on
  the next start. `mise outdated --bump` lists what's newer, `mise prune` cleans up.
- **apt packages** are pinned and read from the [snapshot.debian.org](https://snapshot.debian.org)
  date the pinned `debian:trixie` digest names. To update, bump the digest and copy the
  new versions (`apt-cache policy <package>`) into the pins. PHP (Sury) has no snapshot, so
  only the PHP minor is pinned.
- `docker build --pull --no-cache -t agent-box .` picks up the newest mise and Composer.

An agent you **add** to the `Dockerfile` later won't appear in an existing home volume,
which is only seeded once. Install it inside the sandbox, or reset the volume with
`msb volume rm agent-box-home` (you'll have to log in again).

### Secrets

Give an API key to a profile's `secrets`, not its `env`. The key itself never goes into
the profile: agentbox reads it from your shell's variable of the same name at boot.

```toml
secrets = { OPENROUTER_API_KEY = { hosts = ["openrouter.ai"] } }
```

```bash
export OPENROUTER_API_KEY=$(security find-generic-password -s openrouter -a "$USER" -w)
uv run agentbox up dev --name my-app
```

Inside, `OPENROUTER_API_KEY` holds only a placeholder (`$MSB_OPENROUTER_API_KEY`). Tools
send it as usual, and microsandbox swaps in the real key in the `Authorization` header
(`header = "x-api-key"` picks another one) of requests to `hosts`, so neither the agent nor
the code it runs ever sees the key. To do that swap, microsandbox decrypts HTTPS once a
profile has a secret, and a decrypted request that carries the placeholder to any other host
is blocked. Hosts in `tls_bypass` keep their real certificate, which clients that don't trust
microsandbox's CA need, like containers inside Docker. Bypassed traffic is never inspected,
so it can't carry the real key either.

With `network = "allowlist"`, agentbox bypasses every allowed domain except the secret's
hosts, so you only list extras in `tls_bypass`. The secret's hosts must be in
`allowed_domains`, or every request to them would be blocked. With `public` (or
`unrestricted`), every site not in `tls_bypass` is decrypted:

```toml
network = "public"
tls_bypass = ["*.docker.io", "*.docker.com"]
```

Boot fails if a secret host is missing from `allowed_domains` (allowlist mode), is in
`tls_bypass`, or if the variable isn't set. A name can't be in both `env` and `secrets`.
With `network = "offline"` (or `-n offline`) secrets are skipped, so the variable isn't needed.

`msb exec -e ANTHROPIC_API_KEY=... my-app -- claude` passes a key to one command, but
the guest sees the real value. Don't mount host credential folders into a sandbox
(`~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, your host's `~/.claude`): that undoes
the isolation. For git push, use HTTPS with a fine-grained token, e.g. `GH_TOKEN` plus
`gh auth setup-git`.

## Profiles

Built-ins live in `src/agentbox/profiles.py`:

| Profile                | Image                                                             | Network         |
|------------------------|-------------------------------------------------------------------|-----------------|
| `airgapped`            | `python:3.11-slim`                                                | none            |
| `agent-box`            | `agent-box` ([built here](#the-agent-box-image))                  | public internet |
| `agent-box-php-docker` | `agent-box:php-docker` ([built here](#docker-inside-the-sandbox)) | public internet |

Network modes (`NetworkMode`):

- `offline` — no egress; `exec` and `fs` still work over the host-guest channel.
- `allowlist` — deny by default, plus DNS and TCP/443 to `allowed_domains`
  (a leading dot means "and all subdomains"). Cloud metadata is explicitly denied. A
  domain is matched by the hostname a TLS client names, and only on an IP the guest got
  by looking up an allowed domain, so pointing an allowed name at another server doesn't
  get through. Plain HTTP on port 80 stays blocked, including Debian's default apt mirrors.
- `public` — public internet only; private, host-local and metadata ranges stay blocked.
- `unrestricted` — everything, including the host. Local debugging only.

Add your own in `$XDG_CONFIG_HOME/agentbox/profiles.toml` — falling back to
`~/.config/agentbox/profiles.toml` when that variable is unset — or point `$AGENTBOX_CONFIG`
at any file:

```toml
[profiles.rust]
image = "rust:1-slim"
cpus = 8
memory_mib = 8192
network = "allowlist"
allowed_domains = [".crates.io", ".static.crates.io"]
setup_script = "rustup component add clippy"
```

Two kinds of storage outlive a stop. `named_volumes = { "/home/me" = "my-home" }` is a
folder shared by every sandbox that names it. `disks = { "/var/lib/docker" = 20480 }` is an
ext4 disk of that many MiB that belongs to one sandbox and is deleted with it.

User profiles override built-ins with the same name. The lookup order is:

| # | Source                                    |
|---|-------------------------------------------|
| 1 | `--config` / `-c`                         |
| 2 | `$AGENTBOX_CONFIG`                        |
| 3 | `$XDG_CONFIG_HOME/agentbox/profiles.toml` |
| 4 | `~/.config/agentbox/profiles.toml`        |

A relative `$XDG_CONFIG_HOME` is ignored, as the XDG spec requires. Nothing is ever
written to these paths — you create the file yourself, and `agentbox` only reads it. Check
where it is looking with `python -c "from agentbox.profiles import default_config_path;
print(default_config_path())"`.

A config file that is present but unreadable or invalid is reported as a
`ProfileConfigError` naming the file and the offending field, so the CLI exits 2 with a
message instead of a traceback. A missing file at the default path (3 or 4) is not an
error — you just get the built-ins. A file named by `--config` or `$AGENTBOX_CONFIG` must
exist, so a typo can't boot a built-in in place of your own profile of the same name.

## Development

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy
uv run pytest                         # unit tests only; no microVM needed
uv run pytest -m integration          # boots real microVMs
```

Tests marked `integration` are excluded by default via `addopts`. Passing `-m integration`
on the command line overrides that. They are skipped with a reason, rather than failed,
on hosts without a microsandbox runtime (no `/dev/kvm`, no `msb` on `PATH`, Intel macOS).

Coverage is enforced at **100%**, statements and branches, via `fail_under = 100`. Unit
tests never boot a microVM: `tests/fakes.py` provides stand-ins for `microsandbox.Sandbox`
(the `runtime` and `start_session` fixtures wire them in), and `tests/test_cli.py` fakes
`open_session`.

Dependencies are pinned exactly in `pyproject.toml` and locked in `uv.lock`. To bump them:

```bash
uv lock --upgrade     # resolve newest versions
uv sync
```

then copy the resolved versions from `uv.lock` back into the `==` pins.

## Layout

```
src/agentbox/
  config.py                 AgentProfile / WorkspaceMount / NetworkMode — pure data
  microsandbox_adapter.py   profile -> Sandbox.create kwargs; the only SDK-aware module
  profiles.py               built-in profiles + TOML loading
  session.py                AgentSession, open_session, launch_detached, list/destroy
  cli.py                    typer entry point (`agentbox`): run, up, ls, down, profiles, show
  errors.py                 agentboxError hierarchy
Dockerfile                  the agent-box image; entrypoint.sh, mise/, start-dockerd.sh and
                            docker-entrypoint.sh go with it
tests/
  fakes.py                  test doubles for the microsandbox SDK
  conftest.py               fixtures + integration-test gating
```

`config.py` imports nothing from microsandbox, so profiles can be loaded, validated and
printed without the SDK. The SDK is imported lazily, only once a sandbox is actually
booted — which is why `agentbox version`, `agentbox profiles` and `agentbox show` start fast.

## References

- [microsandbox docs](https://docs.microsandbox.dev)
- [Python SDK reference](https://docs.microsandbox.dev/sdk/python/sandbox)
