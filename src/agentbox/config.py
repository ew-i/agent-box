"""Declarative description of the microVM a coding agent is allowed to run in.

An :class:`AgentProfile` is pure data: it carries no microsandbox objects, so it can be
loaded from TOML, serialised, diffed and reviewed — and imported without the SDK
installed. Translating a profile into the arguments ``microsandbox.Sandbox.create``
expects is the job of :mod:`agentbox.microsandbox_adapter`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class NetworkMode(StrEnum):
    """How much of the network the sandboxed agent may reach."""

    OFFLINE = "offline"
    """No egress at all. Use for untrusted code that has everything it needs on disk."""

    ALLOWLIST = "allowlist"
    """Deny by default, plus DNS and TCP/443 to the domains in ``allowed_domains``."""

    PUBLIC = "public"
    """Public internet only; private, host-local and metadata ranges stay blocked."""

    UNRESTRICTED = "unrestricted"
    """Everything, including the host. Only for local debugging."""


def _absolute(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("must be an absolute guest path")
    return path


GuestPath = Annotated[str, AfterValidator(_absolute)]

EnvVarName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class HostSecret(BaseModel):
    """Where one secret may be sent. Its value comes from the host, never from a profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[str, ...] = Field(min_length=1)
    """Exact hostnames that receive the real value, e.g. ``openrouter.ai``."""

    header: str = "authorization"
    """The only request header the value is substituted into. One named header, not all of
    them: a host that echoes headers back would otherwise hand the real value to the guest."""


class WorkspaceMount(BaseModel):
    """A host directory exposed to the agent inside the sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_path: Path
    guest_path: GuestPath = "/workspace"
    readonly: bool = False

    @property
    def resolved_host_path(self) -> Path:
        """The absolute host directory, with ``~`` expanded and symlinks followed."""
        return self.host_path.expanduser().resolve()

    @model_validator(mode="after")
    def _not_containing_home(self) -> Self:
        # Mounting $HOME or above hands over ~/.ssh and cloud keys.
        # Reachability via samefile, not path comparison: /users (case-insensitive disk) and
        # /System/Volumes/Data (macOS firmlink) both contain /Users without being its parent.
        path = self.resolved_host_path
        home = Path.home().resolve()
        candidates = (path / home.relative_to(folder) for folder in (home, *home.parents))
        if any(_is_same(c, home) for c in candidates):
            raise ValueError(f"refusing to mount {path}, it contains your home folder")
        return self


def _is_same(candidate: Path, home: Path) -> bool:
    try:
        return candidate.samefile(home)
    except OSError:
        # Missing, or a folder we may not open: home can't be below it, we reach home ourselves.
        return False


class AgentProfile(BaseModel):
    """Everything agentbox needs to boot one sandbox for one coding agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = ""

    image: str = "python:3.11-slim"
    cpus: Annotated[int, Field(ge=1, le=32)] = 2
    memory_mib: Annotated[int, Field(ge=256, le=65_536)] = 2048

    workdir: GuestPath = "/workspace"
    shell: str = "/bin/bash"
    env: dict[str, str] = Field(default_factory=dict)

    secrets: dict[EnvVarName, HostSecret] = Field(default_factory=dict)
    """Environment variable -> where its value may go. The value is read from the host's
    variable of the same name at boot. The guest only sees a placeholder, which microsandbox
    swaps for the real value in requests to ``hosts`` and blocks everywhere else."""

    network: NetworkMode = NetworkMode.ALLOWLIST
    allowed_domains: tuple[str, ...] = ()

    tls_bypass: tuple[str, ...] = ()
    """Hosts that keep their real certificate once a secret turns on TLS interception, e.g.
    ``*.docker.io``. The allowlist already bypasses every allowed domain except the secret
    hosts; with ``public`` every other site is intercepted unless listed here."""

    workspace: WorkspaceMount | None = None

    named_volumes: dict[GuestPath, str] = Field(default_factory=dict)
    """Guest path -> microsandbox named volume, created on first use and shared by every
    sandbox that names it. Unlike Docker's, a new volume starts empty."""

    disks: dict[GuestPath, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    """Guest path -> size in MiB of an ext4 disk that belongs to this one sandbox: kept across
    a stop and start, deleted with the sandbox. For data no two sandboxes may share, and for
    tools that cannot stack overlayfs on the guest's overlayfs root, like ``/var/lib/docker``."""

    max_duration_s: Annotated[int, Field(ge=0)] | None = 3600
    idle_timeout_s: Annotated[int, Field(ge=0)] | None = 600
    ephemeral: bool = True

    setup_script: str | None = None
    """Shell snippet executed once after boot, e.g. to install the agent CLI."""

    @model_validator(mode="after")
    def _secrets_not_in_env(self) -> Self:
        # A real value in env would sit next to the placeholder, visible to the guest.
        if both := sorted(self.env.keys() & self.secrets.keys()):
            raise ValueError(f"{', '.join(both)} in both env and secrets; keep it only in secrets")
        return self

    def required_directories(self) -> list[str]:
        """Guest directories that must exist before boot, parents before children.

        Most base images have no ``/workspace``, and microsandbox validates the workdir
        before boot, so every ancestor is listed explicitly rather than assumed.
        """
        targets = [self.workdir]
        if self.workspace is not None:
            targets.append(self.workspace.guest_path)

        ordered: list[str] = []
        for target in targets:
            for ancestor in _ancestors(target):
                if ancestor not in ordered:
                    ordered.append(ancestor)
        return ordered


def _ancestors(path: str) -> list[str]:
    """``"/a/b/c"`` -> ``["/a", "/a/b", "/a/b/c"]``; root itself is skipped."""
    current = PurePosixPath("/")
    out: list[str] = []
    for part in PurePosixPath(path).parts[1:]:
        current = current / part
        out.append(str(current))
    return out
