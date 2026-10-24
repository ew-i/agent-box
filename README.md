# agent-box

A simple disposable, isolated Linux container for running terminal coding agents on a project without giving them access to the rest of your computer.

- The container sees **only the project folder** you start it from.
- Agent logins, settings, history and the agents themselves live in a **named volume**, so they survive between runs.
- The container itself is thrown away on exit (`--rm`).
- Optional: a [Docker mode](#docker-inside-the-sandbox) gives the agent its own private Docker.

It isn't tied to one tool, OS or container engine:

| | Works with |
|---|---|
| **Coding agents** | Claude Code, opencode, OpenAI Codex CLI, Gemini CLI, Aider, or any other CLI agent you can install on Linux |
| **Host OS** | macOS, Linux, Windows (WSL2 or PowerShell) |
| **Container engine** | OrbStack, Docker Desktop, Podman, Colima, Rancher Desktop, Docker Engine on Linux |

---

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | The image: Debian 13 + common dev tools + mise + the agents you choose; optional PHP and Docker variants |
| `mise/` | Toolchains: always-on Node.js and GitHub CLI (`config.toml`) and [presets](#toolchain-presets-java-go-node-python) for Java, Go, Node and Python |
| `entrypoint.sh` | Installs the selected toolchains on start |
| `agentbox` | Launcher for macOS, Linux and WSL (bash) |
| `agentbox.ps1` | Launcher for Windows PowerShell |
| `docker-entrypoint.sh` | Starts and stops the private Docker daemon in [Docker mode](#docker-inside-the-sandbox) |

---

## How the storage is laid out

| Inside the container | Comes from | What's in it | Persists? |
|---|---|---|---|
| `/workspace/<project>` | **Bind mount** of the folder you start from | Your code | Yes, it's your real folder |
| `/home/agent-box` | **Named volume** `agent-box-home` | Agent binaries, logins, config, session history, npm globals, caches, git config | Yes, until you delete the volume |
| `/var/lib/docker` (Docker mode only) | **Named volume** `agent-box-docker-<project>-<hash>` | The private Docker's images, containers and volumes | Yes, until you delete the volume |
| everything else (`/usr`, `/tmp`, apt packages, …) | The image | OS + tools from the `Dockerfile` | No, it resets every run |

> **Bind mount vs named volume:** a bind mount maps a specific host folder into the container, so changes show up on both sides. A named volume is storage managed by the container engine. It doesn't appear in your normal filesystem, and the container can't use it to reach anything else.

Where common agents keep their data. All of it is under `/home/agent-box`, i.e. in the volume:

| Agent | Config / auth / history |
|---|---|
| Claude Code | `~/.claude/`, `~/.claude.json` |
| opencode | `~/.config/opencode/`, `~/.local/share/opencode/`, `~/.local/state/opencode/` |
| Codex CLI | `~/.codex/` |
| Gemini CLI | `~/.gemini/` |

Any other agent works the same way, as long as it stores its state under `$HOME`.

---

## Setup (once)

### 1. Choose your agents

Open the `Dockerfile` and find the `Coding agents` section. Uncomment the agents you want and comment out the rest:

```dockerfile
# Claude Code
RUN curl -fsSL https://claude.ai/install.sh | bash
# opencode
RUN curl -fsSL https://opencode.ai/v2/install | bash
# OpenAI Codex CLI
# RUN npm install -g @openai/codex
...
```

To add another agent, add a `RUN` line with its Linux install command. Node.js/npm is always available (from mise); for `uv tool install`, start with `agentbox -p python`. `npm install -g` and `uv tool install` both install into `~/.local`, so they work without root, and agents can update themselves later.

### 2. Install the launcher

The launcher is a thin wrapper around `docker run` that always mounts things the same way.

**macOS, Linux, WSL:**

```bash
mkdir -p ~/.local/bin
cp agentbox ~/.local/bin/
chmod +x ~/.local/bin/agentbox
```

Make sure `~/.local/bin` is on your `PATH`. If it isn't, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc`.

**Windows PowerShell:** copy `agentbox.ps1` to a folder on your `PATH`, or call it by its full path. If PowerShell refuses to run local scripts, you'll need to allow them for your user (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`). Decide whether that's acceptable for your machine. PowerShell also removes a bare `--` from the command line, so quote it when the command needs one: `agentbox npm test "--" --watch`.

### 3. Build the image

From the folder containing the `Dockerfile`:

**macOS / Windows** (OrbStack, Docker Desktop, Colima, Rancher Desktop):

```bash
docker build -t agent-box .
```

**Native Linux.** Pass your user and group IDs so files the agents create in your project are owned by you:

```bash
docker build --build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g) -t agent-box .
```

**Podman.** Use `podman build` with the same arguments.

**Language versions.** All toolchains come from [mise](#toolchain-presets-java-go-node-python), except PHP, which has its own [PHP image](#php-image). Build args:

| Build arg | Default | What you get |
|---|---|---|
| `MISE_VERSION` | `2026.10` | Latest [mise](https://mise.jdx.dev) of that month, from the official `jdxcode/mise` image |
| `PHP_VERSION` | `8.5` | PHP CLI from the [Sury repository](https://packages.sury.org/php/) (PHP image only) |
| `COMPOSER_VERSION` | `2` | Latest Composer of that major version, from the official `composer` image (PHP image only) |

#### Toolchain presets (Java, Go, Node, Python)

Instead of baking every language into the image, [mise](https://mise.jdx.dev) installs toolchains on first use into the home volume. Node.js (many agents ship via npm) and the GitHub CLI `gh` are always on (`mise/config.toml`). Everything else is a preset you pick per run with `-p` (or `AGENTBOX_PRESETS`):

```bash
agentbox -p java claude
agentbox -p go,node
```

| Preset | Tools |
|---|---|
| `java` | Temurin JDK 27, Maven, Gradle, jbang |
| `go` | Go, golangci-lint |
| `node` | corepack (provides `yarn` and `pnpm` in the version the project's `packageManager` field pins), bun. Node.js 26 itself is always on |
| `python` | Python 3.14, uv |

The first start with a new preset takes up to a minute while it downloads. After that the tools are cached in the volume. The presets are plain mise config files in [`mise/`](mise/), pinned to exact versions so every start gets the same tools. Edit a version there or add your own, e.g. `mise/config.rust.toml` for `agentbox -p rust`, then rebuild the image.

**Python** comes from the `python` preset, not from Debian, so without it there's no `python3` (npm packages that compile native code with node-gyp need it too). Prefer uv over `pip install`:

```bash
uv init                 # new project
uv add requests         # add a dependency
uv run pytest           # run in the project's venv
uvx ruff check          # one-off tools
uv tool install ruff    # persistent tools (into ~/.local/bin)
```

**Per project:** a `mise.toml` in the project overrides the presets. You (or the agent) can create one inside the container:

```bash
mise use java@temurin-21 maven   # writes mise.toml, commit it with the project
mise use ruby@3.4                # anything in `mise registry`
```

A `mise.toml` can run code, so mise ignores one it hasn't been told to trust, and tools then fail with `Config files in … are not trusted`. Read it first, especially in a repo you cloned, then trust it once (it's remembered in the volume):

```bash
mise trust && mise install
```

#### PHP image

PHP isn't a mise preset: mise can only compile PHP from source, which is slow and needs dozens of system packages. Instead it's a separate image built with `--target php`, using Sury's prebuilt packages:

```bash
docker build --target php -t agent-box:php .
agentbox --php claude
```

Presets work in it too, e.g. `agentbox --php -p node`.

PHP comes with the extensions a typical Laravel app needs: bcmath, curl, gd, imagick, intl, mbstring, mysql (MySQL/MariaDB), readline, redis, sqlite3, xml and zip, plus the ones built into PHP (pcntl, posix, opcache, …). To add one, append its package (e.g. `php${PHP_VERSION}-pgsql`) to the PHP `apt-get install` line in the `Dockerfile`. `composer global require` installs into `~/.config/composer/vendor/bin`, which is on the `PATH`.

**Xdebug (code coverage, step debugging).** Xdebug is installed but **not loaded by default**. Just loading it makes PHP about 60 % slower, even with `xdebug.mode=off` (measured on a Laravel test suite: 122 s → 195 s). That would slow down every `composer` and `artisan` call. Load it only when you need it:

```bash
with-xdebug php artisan test --coverage          # one command, coverage mode
XDEBUG_MODE=debug with-xdebug php artisan serve  # one command, another mode
```

For a whole session (e.g. an agent that keeps running coverage), start agent-box with Xdebug loaded:

```bash
AGENTBOX_OPTS="-e PHP_INI_SCAN_DIR=:/etc/php/xdebug.d" agentbox --php claude
```

**Optional: the Docker-mode image.** Only needed if you want [Docker inside the sandbox](#docker-inside-the-sandbox). It's the same image plus Docker Engine, with the same build args. Add `--build-arg DOCKER_BASE=php` to build it on top of the PHP image:

```bash
docker build --target docker -t agent-box:docker .
docker build --target docker --build-arg DOCKER_BASE=php -t agent-box:php-docker .   # PHP + Docker
agentbox --php -d
```

### 4. Log in to your agents (once, the volume remembers)

```bash
cd ~/code/some-project
agentbox
```

Inside the container, log in to the agents you installed:

```bash
claude                  # prints a login URL → open it in your browser → paste the code back
opencode auth login     # or run `opencode` and use /connect
codex                   # Codex CLI
gemini                  # Gemini CLI
```

Then set your git identity:

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

Alternatively, skip interactive logins and use API keys (see [Secrets](#secrets-api-keys-tokens)).

---

## Daily use

```bash
cd ~/code/my-app
agentbox                    # shell in the container
agentbox claude             # start an agent directly
agentbox opencode
agentbox codex
agentbox claude --dangerously-skip-permissions   # reasonable here: the agent can only touch this project
```

Flags go before the command and pick the image and toolchains (`agentbox --help` lists them):

```bash
agentbox -p node,python claude    # toolchain presets
agentbox --php claude             # PHP image
agentbox --php -d -p node,python  # PHP + Docker inside the sandbox + presets
```

| Flag | Same as |
|---|---|
| `-d`, `--docker` | `AGENTBOX_DOCKER=1`, [Docker inside the sandbox](#docker-inside-the-sandbox) |
| `--php` | `AGENTBOX_IMAGE=agent-box:php` (`agent-box:php-docker` with `-d`), the [PHP image](#php-image) |
| `-p`, `--presets LIST` | `AGENTBOX_PRESETS=LIST`, [toolchain presets](#toolchain-presets-java-go-node-python) |

A second shell in the same running container, e.g. to run two agents side by side:

```bash
docker exec -it agent-box-code-my-app bash
```

You can run containers for several projects at once. They share the `agent-box-home` volume, so you're logged in everywhere. Each container is named after the project folder and its parent, e.g. `~/code/my-app` runs as `agent-box-code-my-app`, so `~/work/api` and `~/client/api` don't collide. Its Docker-mode volume adds a hash of the full path (`agent-box-docker-my-app-3f9a1c2e`), so same-named projects never share one; `docker volume ls` shows it.

### What the launcher runs

```bash
docker run -it --rm \
  --name agent-box-<parent>-<project> --hostname agent-box-<parent>-<project> \
  -v agent-box-home:/home/agent-box \
  -v "<current folder>":/workspace/<project> \
  -w /workspace/<project> \
  [--privileged --init --stop-timeout 30 -v agent-box-docker-<project>-<hash>:/var/lib/docker]   # only with -d
  [-e MISE_ENV=<presets>]                # only with -p
  [--env-file ~/.config/agent-box/env]   # only if the file exists
  [$AGENTBOX_OPTS]                       # your extra flags
  agent-box [command, default: bash]     # :php, :docker or :php-docker with --php / -d
```

Each project is mounted at its own path (`/workspace/my-app`) rather than a shared `/workspace`. That's on purpose: most agents key session history by directory, so a single shared path would mix history from different projects.

### Launcher settings

Set these as environment variables, e.g. in your shell profile. `-d` and `-p` override `AGENTBOX_DOCKER` and `AGENTBOX_PRESETS`:

| Variable | Default | Use |
|---|---|---|
| `AGENTBOX_ENGINE` | `docker` | Set to `podman` to use Podman |
| `AGENTBOX_DOCKER` | `0` | Set to `1` for [Docker inside the sandbox](#docker-inside-the-sandbox) |
| `AGENTBOX_IMAGE` | picked by `--php` / `-d` | Use a different image, e.g. `my-agent-box`. Overrides `--php` |
| `AGENTBOX_PRESETS` | — | [Toolchain presets](#toolchain-presets-java-go-node-python), comma-separated, e.g. `java,go` |
| `AGENTBOX_HOME_VOLUME` | `agent-box-home` | Use a separate home volume, e.g. per client or per project |
| `AGENTBOX_ENV_FILE` | `~/.config/agent-box/env` | Location of the secrets file |
| `AGENTBOX_OPTS` | — | Extra `docker run` flags, split on spaces, e.g. `-p 3000:3000` |

Examples:

```bash
AGENTBOX_OPTS="-p 3000:3000" agentbox
```

```powershell
$env:AGENTBOX_OPTS = "-p 3000:3000"; agentbox
```

---

## Dev servers

Make sure the server listens on `0.0.0.0`, not `127.0.0.1`.

- **OrbStack:** no port flags needed. A server on port 3000 in `agent-box-code-my-app` is at `http://agent-box-code-my-app.orb.local:3000`.
- **Everything else:** publish the port, e.g. `AGENTBOX_OPTS="-p 3000:3000" agentbox`, then open `http://localhost:3000`.

Most dev servers listen on `127.0.0.1` by default. For example:

```bash
php artisan serve --host=0.0.0.0     # Laravel, port 8000
npm run dev -- --host 0.0.0.0        # Vite, port 5173
```

---

## Databases and other services on your machine

Inside the sandbox, `127.0.0.1` is the container itself. A project whose `.env` points at `127.0.0.1` for its database, Redis or mail server therefore can't reach them, e.g. a MariaDB running in another container or directly on your machine.

You don't need to edit `.env`. Environment variables set on the container override the values in `.env` (Laravel, and most dotenv loaders, never overwrite a variable that's already set). Two ways to point them at the real service:

**Through your machine:** works for any service that publishes its port on your machine (`-p 3306:3306`, or a local install):

```bash
AGENTBOX_OPTS="-e DB_HOST=host.docker.internal -e REDIS_HOST=host.docker.internal" agentbox claude
```

**Through the service's Docker network:** reach another compose stack's containers by name. The network name is shown by `docker ps --format '{{.Names}} {{.Networks}}'`:

```bash
AGENTBOX_OPTS="--network lamp_default -e DB_HOST=lamp-mariadb118" agentbox claude
```

`host.docker.internal` works out of the box with OrbStack and Docker Desktop. On native Linux, add `--add-host=host.docker.internal:host-gateway` to `AGENTBOX_OPTS`.

To keep these settings per project, put the variables in a separate env file (one `KEY=value` per line, e.g. `DB_HOST=host.docker.internal`) and select it with `AGENTBOX_ENV_FILE`. That file replaces the default secrets file for that run.

---

## Docker inside the sandbox

By default there's no Docker in the sandbox, so an agent can't run `docker compose up`.

**Docker mode** fixes that. It gives the sandbox its own private Docker daemon, separate from the one on your machine.

```bash
docker build --target docker -t agent-box:docker .   # once
```

```bash
cd ~/code/my-app
agentbox -d claude
```

Inside the sandbox, `docker` and `docker compose` then work as on a normal machine:

```bash
docker compose up -d
curl http://localhost:9000/api/system   # published ports are on localhost inside the sandbox
docker compose down
```

### Ports

| From | How to reach a published port, e.g. `9000` |
|---|---|
| Inside the sandbox (agent, tests) | `http://localhost:9000` |
| Your machine, with OrbStack | `http://agent-box-code-my-app.orb.local:9000`, no extra flags |
| Your machine, other engines | Publish it on the sandbox as well: `AGENTBOX_OPTS="-p 9000:9000" agentbox -d` |

### Lifetime

- The stack runs as long as the sandbox does. When you leave the sandbox, the entrypoint stops the private daemon, which shuts the containers down cleanly.
- Images and named compose volumes stay in `agent-box-docker-<project>-<hash>` for next time.
- Run `docker compose down -v` inside the sandbox for a fresh state.
- A second shell (`docker exec -it agent-box-code-my-app bash`) uses the same private daemon.

### Security trade-off

`--privileged` removes most of the walls between the container and the machine running it. Docker mode is therefore **much less isolated** than the default:

- **macOS / Windows:** containers run in the engine's Linux VM. A privileged container could break out into that VM, and the VM can usually see your files (OrbStack and Docker Desktop share your home folder with it). An agent won't do this by accident, but a malicious prompt or dependency could.
- **Native Linux:** there's no VM in between. A privileged container is close to root on your actual machine.
- **Rootless Podman:** running a Docker daemon inside usually doesn't work there. This setup hasn't been tested with it.

Use Docker mode only for projects where you need it. Keep the default mode for everything else. If the agent only needs to *call* the stack and not start or stop it, a safer option is to start the stack yourself on your machine and attach the sandbox to its network:

```bash
docker compose up -d
```

```bash
AGENTBOX_OPTS="--network my-app_default" agentbox claude
```

The agent then reaches the stack by service name, e.g. `http://graylog:9000`.

---

## Secrets (API keys, tokens)

Put them in an env file on the host instead of mounting credential folders. The default location is:

- `~/.config/agent-box/env` on macOS, Linux and WSL
- `%USERPROFILE%\.config\agent-box\env` on Windows

Example contents (`KEY=value` per line, no quotes, no `export`). Comments go on their own line: a `#` after a value becomes part of the value:

```
ANTHROPIC_API_KEY=sk-ant-...
# Claude subscription instead of an API key: token from `claude setup-token` on your host
# CLAUDE_CODE_OAUTH_TOKEN=...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
# git over HTTPS / GitHub CLI
GH_TOKEN=ghp_...
```

On macOS and Linux, restrict who can read it:

```bash
chmod 600 ~/.config/agent-box/env
```

**Don't mount these into the container.** Doing so undoes the isolation:

- `~/.ssh`: gives access to every server and repo your keys can reach.
- `~/.aws`, `~/.config/gcloud`, `~/.azure`, `~/.kube`, and similar: cloud credentials.
- The host's own agent config folders (`~/.claude`, `~/.codex`, …): this mixes host and container state. On macOS, logins are stored in the Keychain anyway, so it wouldn't log you in.
- Your whole home folder or drive (`~`, `/Users`, `C:\`).

If you need git push, use HTTPS with a fine-grained token that's limited to the repos you want, e.g. via `GH_TOKEN` + `gh auth setup-git` (`gh` is always installed).

---

## Platform notes

### macOS

- **OrbStack** is the fastest option and adds the `*.orb.local` hostnames. Docker Desktop, Colima and Rancher Desktop work too.
- File ownership is handled automatically. Files the agents create show up as owned by your Mac user, so you don't need the UID build args.

### Windows

- **Recommended: use WSL2.** Keep your projects inside the WSL filesystem (e.g. `~/code` in Ubuntu, not `/mnt/c/...`), and use the bash `agentbox` launcher from a WSL shell. Bind mounts from `C:\` work, but they're much slower, and file watching (hot reload) is often unreliable.
- **PowerShell:** `agentbox.ps1` works with Docker Desktop or Podman Desktop and mounts the current folder (`C:\...`) directly. Expect the same slower file access as above.
- **Git Bash / MSYS:** not recommended, because it rewrites paths in `-v` arguments. If you must, prefix commands with `MSYS_NO_PATHCONV=1`.
- **Line endings:** files with Windows line endings (CRLF) break the bash launcher and the Dockerfile's shell steps. The included `.gitattributes` prevents this if you keep these files in git. Otherwise, save them with LF line endings.

### Linux

- **File ownership:** build with `--build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g)` (see step 3). Otherwise, files created in your project will belong to UID 1000.
- **Rootless Podman:** use `AGENTBOX_ENGINE=podman` and `AGENTBOX_OPTS="--userns=keep-id"` so files map to your user.
- **SELinux** (Fedora, RHEL): if you get "permission denied" on the project mount, add `AGENTBOX_OPTS="--security-opt label=disable"`.

---

## Optional mounts

Pass these via `AGENTBOX_OPTS`, or edit the launcher if you want them permanently.

**Share your host git config, read-only:**

```
-v ~/.gitconfig:/home/agent-box/.gitconfig:ro
```

(If you do this, skip the `git config --global` step above, since the file is read-only.)

**Keep the container's `node_modules` separate from the host** (recommended for JS projects):

```
-v agent-box-nm-my-app:/workspace/my-app/node_modules
```

Native npm packages built in the Linux container won't run on macOS or Windows, and the reverse is also true. This puts the container's `node_modules` in its own volume, one per project. On slow bind mounts (Windows, Docker Desktop) installs also run much faster. The same idea works for `.venv` (Python) or `target` (Rust).

**Reference material, read-only:**

```
-v ~/code/shared-docs:/ref/shared-docs:ro
```

Use `:ro` for anything the agents should read but never change.

---

## Updating

**Agents:** most of them update themselves inside the volume. To update by hand, run the agent's own update command inside the container, e.g. `claude update`, `opencode update`, `npm update -g`.

**Image** (after editing the `Dockerfile`, e.g. to add apt packages or another agent):

```bash
docker build -t agent-box .
docker build --target docker -t agent-box:docker .   # if you use Docker mode
```

**Toolchains from presets** are pinned to exact versions. To update, raise the versions in `mise/*.toml` and rebuild the image; the new versions install on the next start. `mise outdated --bump` inside the container lists what's newer, and `mise prune` removes old versions from the volume.

**apt packages** are pinned to exact versions in the `Dockerfile`. Debian packages come from the [snapshot.debian.org](https://snapshot.debian.org) date that the pinned `debian:trixie` image names, so they build the same every time. To update, bump the image digest, then read the new versions inside a container (`apt-cache policy <package>`) and put them in the pins. PHP (Sury) is the exception: it has no snapshot, keeps only its newest builds and publishes amd64 and arm64 days apart, so only the PHP minor (`PHP_VERSION`) is pinned and each build gets Sury's newest packages. Rebuild the PHP image to pick up a new PHP patch release.

To pick up the newest mise and Composer releases without changing the Dockerfile, add `--pull --no-cache`. Otherwise Docker reuses the cached base images and layers:

```bash
docker build --pull --no-cache -t agent-box .
```

> **Important:** `/home/agent-box` comes from the **volume**, which was filled from the image only on its first use. System packages (`apt`, mise, PHP, Composer) update when you rebuild after changing them. But an agent you **add** to the Dockerfile later is installed into `/home/agent-box` in the image, so it won't appear in an existing volume.
>
> To get it, either install it once inside a running container (same command as in the Dockerfile), or reset the volume (see below) and log in again.

---

## Managing the volume

```bash
docker volume ls                   # list volumes
docker volume inspect agent-box-home   # details
```

**Back it up** (run in the folder where the backup should go; in PowerShell use `${PWD}`):

```bash
docker run --rm -v agent-box-home:/data -v "$PWD":/backup debian:trixie tar czf /backup/agent-box-home.tgz -C /data .
```

**Restore:**

```bash
docker run --rm -v agent-box-home:/data -v "$PWD":/backup debian:trixie tar xzf /backup/agent-box-home.tgz -C /data
```

**Reset it** (refills from the image on the next run; you'll have to log in again):

```bash
docker volume rm agent-box-home
```

**Remove per-project `node_modules` volumes:**

```bash
docker volume ls -q | grep '^agent-box-nm-' | xargs docker volume rm
```

**Remove Docker-mode volumes** (cached images and stack data). They can get large:

```bash
docker system df -v | grep agent-box-docker-                              # see sizes
docker volume ls -q | grep '^agent-box-docker-' | xargs docker volume rm  # remove all
```

---

## What this does and doesn't protect

**Protected:**

- The rest of your filesystem.
- SSH keys, OS keychain and cloud credentials.
- Other projects.
- The host OS itself: `sudo` inside the container only affects the container.

**Not protected:**

- **The project folder.** It's mounted read-write, so commit or stash before letting an agent loose.
- **The network.** The container has full outbound internet access and can reach services on your LAN. It can usually also reach services on your host, via `host.docker.internal` or `host.orb.internal`.
- **Anything you pass in** via the env file or extra mounts.
- **The shared `agent-box-home` volume.** It's shared across projects, so one project's session can read another project's agent history.
- **Docker mode.** With `-d` the container is privileged, so most of the protection above no longer holds. See [Security trade-off](#security-trade-off).

If you want a stricter setup, use a separate home volume per project or client, e.g. `AGENTBOX_HOME_VOLUME=agent-box-home-clientA`. The trade-off is that you'll have to log in once per volume.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `the input device is not a TTY` | The launcher needs an interactive terminal. On Windows with mintty/Git Bash, use Windows Terminal, PowerShell or WSL instead. |
| `Conflict. The container name "/agent-box-code-my-app" is already in use` | That project already has a running container. Use `docker exec -it agent-box-code-my-app bash` instead. |
| An agent's command isn't found | It was added to the Dockerfile after the volume was created. Install it inside the container, or reset the volume. |
| Permission denied in `/workspace/...` (Linux) | Rebuild with your UID/GID build args. With rootless Podman, use `--userns=keep-id`. With SELinux, use `--security-opt label=disable`. |
| Permission denied in `/home/agent-box` | The volume was created by a different image or user. Reset it. |
| `bash\r: No such file or directory` | CRLF line endings. Convert `agentbox` and `Dockerfile` to LF. |
| Login URL won't complete | Copy the full URL into your host browser, then paste the code it shows back into the container's terminal. Or use API keys in the env file. |
| Dev server not reachable | It must listen on `0.0.0.0`. Outside OrbStack, also publish the port with `-p`. |
| `agent-box: the Docker daemon didn't start` | The container isn't privileged. Start it with `agentbox -d` rather than `docker run agent-box:docker` directly. Details are in `/tmp/dockerd.log` inside the sandbox. |
| `docker: command not found` in Docker mode | The `agent-box:docker` image is missing or outdated. Run `docker build --target docker -t agent-box:docker .` |
| Inner stack not reachable from your machine | Inside the sandbox use `localhost`. From your machine, use `agent-box-<parent>-<project>.orb.local` (OrbStack), or publish the port with `AGENTBOX_OPTS="-p 9000:9000"`. |
| `php: command not found` | PHP is only in the [PHP image](#php-image). Build it and start with `agentbox --php`. |
| `mise ERROR No version is set for shim: go` (or java, …) | That tool was installed by a preset you didn't select this time. Start with `agentbox -p go`, or add it to the project's `mise.toml`. |
| `Code coverage driver not available. Did you install Xdebug or PCOV?` | Xdebug isn't loaded by default. Run `with-xdebug php artisan test --coverage`, or see [Xdebug](#3-build-the-image) for a whole session. |
| Two projects with the same folder name | They'd get the same container name and history path. Rename one folder. |
