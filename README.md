# agent-box

A simple disposable, isolated Linux container for running terminal coding agents on a project without giving them access to the rest of your computer.

- The container sees **only the project folder** you start it from.
- Agent logins, settings, history and the agents themselves live in a **named volume**, so they survive between runs.
- The container itself is thrown away on exit (`--rm`).

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
| `Dockerfile` | The image: Debian + common dev tools + Node.js + the agents you choose |
| `agentbox` | Launcher for macOS, Linux and WSL (bash) |
| `agentbox.ps1` | Launcher for Windows PowerShell |
| `.gitattributes` | Keeps LF line endings if you check this into git on Windows |
| `LICENSE` | MIT license |

---

## How the storage is laid out

| Inside the container | Comes from | What's in it | Persists? |
|---|---|---|---|
| `/workspace/<project>` | **Bind mount** of the folder you start from | Your code | Yes, it's your real folder |
| `/home/agent-box` | **Named volume** `agent-box-home` | Agent binaries, logins, config, session history, npm globals, caches, git config | Yes, until you delete the volume |
| everything else (`/usr`, `/tmp`, apt packages, …) | The image | OS + tools from the `Dockerfile` | No, it resets every run |

> **Bind mount vs named volume:** a bind mount maps a specific host folder into the container, so changes show up on both sides. A named volume is storage managed by the container engine. It doesn't appear in your normal filesystem, and the container can't use it to reach anything else.

Where common agents keep their data. All of it is under `/home/agent-box`, i.e. in the volume:

| Agent | Config / auth / history |
|---|---|
| Claude Code | `~/.claude/`, `~/.claude.json` |
| opencode | `~/.config/opencode/`, `~/.local/share/opencode/`, `~/.local/state/opencode/` |
| Codex CLI | `~/.codex/` |
| Gemini CLI | `~/.gemini/` |
| Aider | `~/.aider*`, plus `.aider*` files **in the project folder** |

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

To add another agent, add a `RUN` line with its Linux install command. Node.js/npm and Python are already in the image. `npm install -g` installs into `~/.local`, so it works without root, and agents can update themselves later.

### 2. Build the image

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

**Node.js version.** The default is Node 26. To use a different major version, add a build arg:

```bash
docker build --build-arg NODE_VERSION=26 -t agent-box .
```

### 3. Install the launcher

The launcher is a thin wrapper around `docker run` that always mounts things the same way.

**macOS, Linux, WSL:**

```bash
mkdir -p ~/.local/bin
cp agentbox ~/.local/bin/
chmod +x ~/.local/bin/agentbox
```

Make sure `~/.local/bin` is on your `PATH`. If it isn't, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc`.

**Windows PowerShell:** copy `agentbox.ps1` to a folder on your `PATH`, or call it by its full path. If PowerShell refuses to run local scripts, you'll need to allow them for your user (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`). Decide whether that's acceptable for your machine.

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

A second shell in the same running container, e.g. to run two agents side by side:

```bash
docker exec -it agent-box-my-app bash
```

You can run containers for several projects at once. They share the `agent-box-home` volume, so you're logged in everywhere.

### What the launcher runs

```bash
docker run -it --rm \
  --name agent-box-<project> --hostname agent-box-<project> \
  -v agent-box-home:/home/agent-box \
  -v "<current folder>":/workspace/<project> \
  -w /workspace/<project> \
  [--env-file ~/.config/agent-box/env]   # only if the file exists
  [$AGENTBOX_OPTS]                       # your extra flags
  agent-box [command, default: bash]
```

Each project is mounted at its own path (`/workspace/my-app`) rather than a shared `/workspace`. That's on purpose: most agents key session history by directory, so a single shared path would mix history from different projects.

### Launcher settings

Set these as environment variables, e.g. in your shell profile:

| Variable | Default | Use |
|---|---|---|
| `AGENTBOX_ENGINE` | `docker` | Set to `podman` to use Podman |
| `AGENTBOX_IMAGE` | `agent-box` | Use a different image, e.g. a per-language variant |
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

- **OrbStack:** no port flags needed. A server on port 3000 in `agent-box-my-app` is at `http://agent-box-my-app.orb.local:3000`.
- **Everything else:** publish the port, e.g. `AGENTBOX_OPTS="-p 3000:3000" agentbox`, then open `http://localhost:3000`.

---

## Secrets (API keys, tokens)

Put them in an env file on the host instead of mounting credential folders. The default location is:

- `~/.config/agent-box/env` on macOS, Linux and WSL
- `%USERPROFILE%\.config\agent-box\env` on Windows

Example contents (`KEY=value` per line, no quotes, no `export`):

```
ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token` on your host (Claude subscription)
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
GH_TOKEN=ghp_...                # git over HTTPS / GitHub CLI
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

If you need git push, use HTTPS with a fine-grained token that's limited to the repos you want, e.g. via `GH_TOKEN` + `gh auth setup-git` (install the `gh` CLI in the Dockerfile).

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

- **File ownership:** build with `--build-arg USER_UID=$(id -u) --build-arg USER_GID=$(id -g)` (see step 2). Otherwise, files created in your project will belong to UID 1000.
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
```

> **Important:** `/home/agent-box` comes from the **volume**, which was filled from the image only on its first use. System packages (`apt`, Node.js) update with every rebuild. But an agent you **add** to the Dockerfile later is installed into `/home/agent-box` in the image, so it won't appear in an existing volume.
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
docker run --rm -v agent-box-home:/data -v "$PWD":/backup debian:bookworm tar czf /backup/agent-box-home.tgz -C /data .
```

**Restore:**

```bash
docker run --rm -v agent-box-home:/data -v "$PWD":/backup debian:bookworm tar xzf /backup/agent-box-home.tgz -C /data
```

**Reset it** (refills from the image on the next run; you'll have to log in again):

```bash
docker volume rm agent-box-home
```

**Remove per-project `node_modules` volumes:**

```bash
docker volume ls -q | grep '^agent-box-nm-' | xargs docker volume rm
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

If you want a stricter setup, use a separate home volume per project or client, e.g. `AGENTBOX_HOME_VOLUME=agent-box-home-clientA`. The trade-off is that you'll have to log in once per volume.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `the input device is not a TTY` | The launcher needs an interactive terminal. On Windows with mintty/Git Bash, use Windows Terminal, PowerShell or WSL instead. |
| `Conflict. The container name "/agent-box-my-app" is already in use` | That project already has a running container. Use `docker exec -it agent-box-my-app bash` instead. |
| An agent's command isn't found | It was added to the Dockerfile after the volume was created. Install it inside the container, or reset the volume. |
| Permission denied in `/workspace/...` (Linux) | Rebuild with your UID/GID build args. With rootless Podman, use `--userns=keep-id`. With SELinux, use `--security-opt label=disable`. |
| Permission denied in `/home/agent-box` | The volume was created by a different image or user. Reset it. |
| `bash\r: No such file or directory` | CRLF line endings. Convert `agentbox` and `Dockerfile` to LF. |
| Login URL won't complete | Copy the full URL into your host browser, then paste the code it shows back into the container's terminal. Or use API keys in the env file. |
| Dev server not reachable | It must listen on `0.0.0.0`. Outside OrbStack, also publish the port with `-p`. |
| Two projects with the same folder name | They'd get the same container name and history path. Rename one folder. |
