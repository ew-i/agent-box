from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

import typer
from pydantic import ValidationError

from agentbox import __version__
from agentbox.config import AgentProfile, NetworkMode, WorkspaceMount
from agentbox.errors import AgentboxError, ProfileNotFoundError
from agentbox.profiles import get_profile, load_profiles
from agentbox.session import (
    created_profile,
    destroy_sandbox,
    generate_name,
    launch_detached,
    list_sandboxes,
    open_session,
    stop_sandbox,
)

T = TypeVar("T")

main = typer.Typer(
    name="agentbox",
    help="Run coding agents and their generated code inside microsandbox microVMs.",
    no_args_is_help=True,
    add_completion=False,
)

ProfileArgument = Annotated[str, typer.Argument(metavar="PROFILE")]

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to a profiles.toml file."),
]

WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", "-w", help="Host directory to mount at /workspace."),
]

ReadonlyOption = Annotated[bool, typer.Option("--readonly", help="Mount the workspace read-only.")]

NetworkOption = Annotated[
    NetworkMode | None,
    typer.Option("--network", "-n", help="Override the network mode."),
]

IdleTimeoutOption = Annotated[
    int | None,
    typer.Option("--idle-timeout", min=0, help="Idle seconds before auto-stop; 0 means no limit."),
]

MaxDurationOption = Annotated[
    int | None,
    typer.Option("--max-duration", min=0, help="Total lifetime in seconds; 0 means no limit."),
]


class SandboxUse(StrEnum):
    """What a sandbox is for, which decides whether it keeps state and auto-stops.

    The values are the command names, so ``agentbox show airgapped --for up`` reads as a
    question about what ``agentbox up airgapped`` would boot.
    """

    ONE_SHOT = "run"
    LONG_LIVED = "up"


@dataclass(frozen=True)
class _Overrides:
    """Profile fields a command may override, and how the sandbox will be used.

    One place decides what each command does to a profile, so ``show`` can render exactly
    what ``run`` or ``up`` would boot instead of guessing.
    """

    use: SandboxUse = SandboxUse.ONE_SHOT
    workspace: Path | None = None
    readonly: bool = False
    network: NetworkMode | None = None
    idle_timeout: int | None = None
    max_duration: int | None = None

    def apply_to(self, profile: AgentProfile) -> AgentProfile:
        """The profile that would actually boot."""
        updates: dict[str, object] = {}
        if self.workspace is not None:
            try:
                updates["workspace"] = WorkspaceMount(
                    host_path=self.workspace, readonly=self.readonly
                )
            except ValidationError as exc:
                raise typer.BadParameter(
                    str(exc.errors()[0]["ctx"]["error"]), param_hint="--workspace"
                ) from exc
        elif self.readonly:
            # Without --workspace the flag applies to the profile's own mount, never silently lost.
            if profile.workspace is None:
                raise typer.BadParameter(
                    "nothing to mount read-only; pass --workspace", param_hint="--readonly"
                )
            updates["workspace"] = profile.workspace.model_copy(update={"readonly": True})
        if self.network is not None:
            updates["network"] = self.network
        # Validated again, not model_copy: the profile's checks must hold for what the flags made.
        try:
            return AgentProfile.model_validate(
                profile.model_dump() | updates | self._lifetime_updates()
            )
        except ValidationError as exc:
            problem = exc.errors()[0]
            message = problem.get("ctx", {}).get("error", problem["msg"])
            raise typer.BadParameter(str(message)) from exc

    def _lifetime_updates(self) -> dict[str, object]:
        if self.use is SandboxUse.LONG_LIVED:
            # A development box keeps its state and runs until `agentbox down` stops it.
            return {
                "ephemeral": False,
                "idle_timeout_s": _limit(self.idle_timeout),
                "max_duration_s": _limit(self.max_duration),
            }

        # One-shot runs keep the profile's guard rails unless a limit was named.
        updates: dict[str, object] = {}
        if self.idle_timeout is not None:
            updates["idle_timeout_s"] = _limit(self.idle_timeout)
        if self.max_duration is not None:
            updates["max_duration_s"] = _limit(self.max_duration)
        return updates


def _limit(seconds: int | None) -> int | None:
    """``0`` and ``None`` both mean "no limit"; keep one representation of it."""
    return seconds or None


@main.command()
def version() -> None:
    """Print the agentbox version."""
    typer.echo(__version__)


@main.command("profiles")
def list_profiles(config: ConfigOption = None) -> None:
    """List the available agent profiles."""
    for name, profile in sorted(_guard(lambda: load_profiles(config)).items()):
        typer.echo(f"{name:<22} {profile.image:<22} net={profile.network}  {profile.description}")


@main.command()
def show(
    profile_name: ProfileArgument,
    use: Annotated[
        SandboxUse,
        typer.Option("--for", help="Render the profile the way this command would boot it."),
    ] = SandboxUse.ONE_SHOT,
    workspace: WorkspaceOption = None,
    readonly: ReadonlyOption = False,
    network: NetworkOption = None,
    idle_timeout: IdleTimeoutOption = None,
    max_duration: MaxDurationOption = None,
    config: ConfigOption = None,
) -> None:
    """Print the profile that would actually boot, as JSON.

    Overrides are applied before printing, so `show` answers the same question the other
    commands do. `--for up` additionally applies what `up` forces: kept state and no
    automatic shutdown. `env` values are masked: they often hold API keys.
    """
    profile = _guard(lambda: get_profile(profile_name, config))
    effective = _Overrides(
        use=use,
        workspace=workspace,
        readonly=readonly,
        network=network,
        idle_timeout=idle_timeout,
        max_duration=max_duration,
    ).apply_to(profile)
    masked = effective.model_copy(update={"env": dict.fromkeys(effective.env, "***")})
    typer.echo(masked.model_dump_json(indent=2))


@main.command()
def run(
    profile_name: ProfileArgument,
    script: Annotated[str, typer.Argument(help="Shell snippet to run inside the sandbox.")],
    workspace: WorkspaceOption = None,
    readonly: ReadonlyOption = False,
    network: NetworkOption = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds before the command is killed.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Run a shell command inside a fresh sandbox and print its output."""
    base = _guard(lambda: get_profile(profile_name, config))
    profile = _Overrides(workspace=workspace, readonly=readonly, network=network).apply_to(base)

    async def execute() -> int:
        async with open_session(profile) as session:
            return await session.shell_stream(
                script, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer, timeout=timeout
            )

    raise typer.Exit(_guard(lambda: asyncio.run(execute())))


@main.command()
def up(
    profile_name: ProfileArgument,
    name: Annotated[
        str | None, typer.Option("--name", help="Sandbox name. Generated when omitted.")
    ] = None,
    workspace: WorkspaceOption = None,
    readonly: ReadonlyOption = False,
    network: NetworkOption = None,
    idle_timeout: IdleTimeoutOption = None,
    max_duration: MaxDurationOption = None,
    config: ConfigOption = None,
) -> None:
    """Start a detached sandbox and leave it running for `msb exec`.

    The sandbox runs until `agentbox down` stops it. Unlike `run`, it drops the profile's
    idle and lifetime guard rails: a development box should outlive a coffee break, and
    nothing here is executing a one-shot command that might hang.
    """
    base = _guard(lambda: get_profile(profile_name, config))
    profile = _Overrides(
        use=SandboxUse.LONG_LIVED,
        workspace=workspace,
        readonly=readonly,
        network=network,
        idle_timeout=idle_timeout,
        max_duration=max_duration,
    ).apply_to(base)

    sandbox_name = name or generate_name(profile)
    restarted = _guard(lambda: asyncio.run(launch_detached(profile, sandbox_name)))

    typer.echo(sandbox_name)
    if restarted:
        _hint(f"started the stopped {sandbox_name} again, with the settings it was created with,")
        _hint("secrets and time limits included; new values and flags are ignored;")
        _hint(f"to apply them: {_prefix()}agentbox down {sandbox_name}, then up again")
    _hint(f"{_msb_prefix()}msb exec {sandbox_name} -- {profile.shell}")
    _hint(f"{_prefix()}agentbox down {sandbox_name}")
    if not restarted:
        # A restart keeps its old limits, so hints built from this profile would be wrong.
        for hint in _lifetime_hints(profile):
            _hint(hint)


@main.command()
def start(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    config: ConfigOption = None,
) -> None:
    """Start a stopped `up` box again, as it was created, and rerun its setup script.

    Unlike `msb start`, which skips the profile's `setup_script` (and with it e.g. dockerd).
    The profile is the one the box was created from; mounts, network and secrets stay as
    they were.
    """

    async def execute() -> AgentProfile:
        profile_name = await created_profile(name)
        try:
            profile = get_profile(profile_name, config)
        except ProfileNotFoundError as exc:
            # Most likely `up` read another profiles.toml than this run does.
            raise AgentboxError(
                f"{name} was made from profile {profile_name!r}: {exc}; "
                "pass the --config it was brought up with"
            ) from exc
        await launch_detached(profile, name, existing=True)
        return profile

    profile = _guard(lambda: asyncio.run(execute()))
    typer.echo(name)
    _hint(f"{_msb_prefix()}msb exec {name} -- {profile.shell}")
    _hint(f"{_prefix()}agentbox down {name}")


@main.command()
def stop(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Kill instead of shutting down gracefully.")
    ] = False,
) -> None:
    """Stop a sandbox but keep its state; `agentbox start` boots it again."""
    _guard(lambda: asyncio.run(stop_sandbox(name, force=force)))
    # A `run` box is ephemeral: stopping deletes it, so only an `up` box can start again.
    _hint(
        f"stopped {name}; if `agentbox up` made it, {_prefix()}agentbox start {name} boots it again"
    )


@main.command("ls")
def list_running() -> None:
    """List sandboxes known to the microsandbox runtime."""
    infos = _guard(lambda: asyncio.run(list_sandboxes()))
    if not infos:
        _hint("no sandboxes")
        return
    for info in infos:
        typer.echo(f"{info.name:<34} {info.status:<10} {info.profile or '-'}")


@main.command()
def down(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Kill instead of shutting down gracefully.")
    ] = False,
) -> None:
    """Stop a sandbox and delete its persisted state."""
    _guard(lambda: asyncio.run(destroy_sandbox(name, force=force)))
    _hint(f"removed {name}")


SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60


def _lifetime_hints(profile: AgentProfile) -> list[str]:
    """How long the sandbox will live, and how to change that.

    Two independent limits can stop a sandbox, so both are worth saying out loud: one
    fires when nobody is using it, the other regardless of how busy it is.
    """
    hints: list[str] = []
    if profile.idle_timeout_s:
        hints.append(
            f"auto-stops after {_duration(profile.idle_timeout_s)} idle;"
            " --idle-timeout 0 disables it"
        )
    if profile.max_duration_s:
        hints.append(
            f"stops after {_duration(profile.max_duration_s)} even if busy;"
            " --max-duration 0 disables it"
        )
    if not hints:
        hints.append("no time limit; runs until you stop it with the command above")
    return hints


def _duration(seconds: int) -> str:
    """A compact, readable span: ``600`` -> ``10m``, ``5400`` -> ``1h30m``.

    ``seconds`` is always positive here: zero means "no limit" and never gets this far.
    """
    hours, rest = divmod(seconds, SECONDS_PER_HOUR)
    minutes, remaining_seconds = divmod(rest, SECONDS_PER_MINUTE)
    return "".join(
        f"{amount}{unit}"
        for amount, unit in ((hours, "h"), (minutes, "m"), (remaining_seconds, "s"))
        if amount
    )


def _prefix() -> str:
    """``"uv run "`` when launched through uv, so printed commands are copy-pasteable.

    ``agentbox`` is a project console script in ``.venv/bin``, which is on ``PATH`` only
    inside ``uv run`` or an activated venv. ``uv`` sets ``$UV`` for its subprocesses.
    """
    return "uv run " if os.environ.get("UV") else ""


def _msb_prefix() -> str:
    """Empty when ``msb`` is installed system-wide, otherwise the uv prefix.

    microsandbox's installer puts ``msb`` on ``PATH`` for good; the wheel additionally
    ships a copy inside the venv. Only that second case needs ``uv run``.
    """
    path = os.environ.get("PATH", "")
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        path = os.pathsep.join(
            p for p in path.split(os.pathsep) if Path(venv) not in Path(p).parents
        )
    return "" if shutil.which("msb", path=path) else _prefix()


def _hint(message: str) -> None:
    """Human-facing text on stderr, so stdout stays pipeable."""
    typer.secho(message, fg=typer.colors.BRIGHT_BLACK, err=True)


def _guard(action: Callable[[], T]) -> T:
    try:
        return action()
    except AgentboxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
