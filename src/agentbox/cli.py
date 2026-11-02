from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

from agentbox import __version__
from agentbox.boxes import created_profile, destroy_sandbox, list_sandboxes, stop_sandbox
from agentbox.commands import agentbox_command, msb_command
from agentbox.config import AgentProfile, NetworkMode, check_sandbox_name
from agentbox.errors import (
    AgentboxError,
    NotAgentboxSandboxError,
    ProfileNotFoundError,
    SandboxConflictError,
    SecretNotSetError,
)
from agentbox.overrides import Overrides, SandboxUse
from agentbox.profiles import get_profile, load_profiles
from agentbox.session import generate_name, launch_detached, open_session

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

ForceOption = Annotated[
    bool, typer.Option("--force", "-f", help="Kill instead of shutting down gracefully.")
]


def _boot_profile(profile_name: str, config: Path | None, overrides: Overrides) -> AgentProfile:
    """The profile ``run``, ``up`` and ``show`` work with: looked up, then the flags applied."""
    return _apply(overrides, _guard(lambda: get_profile(profile_name, config)))


def _apply(overrides: Overrides, profile: AgentProfile) -> AgentProfile:
    return _bad_parameter(lambda: overrides.apply_to(profile))


def _sandbox_name(name: str | None) -> str | None:
    """Typer callback: refuse a ``--name`` that is unsafe as a path part or shell word."""
    return None if name is None else _bad_parameter(lambda: check_sandbox_name(name))


def _bad_parameter(check: Callable[[], T]) -> T:
    """Report a ``ValueError`` from ``check`` as a usage error, the way typer does its own."""
    try:
        return check()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


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
    effective = _boot_profile(
        profile_name,
        config,
        Overrides(
            use=use,
            workspace=workspace,
            readonly=readonly,
            network=network,
            idle_timeout=idle_timeout,
            max_duration=max_duration,
        ),
    )
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
        float | None,
        typer.Option(
            "--timeout", min=0, help="Seconds before the command is killed; 0 means no limit."
        ),
    ] = None,
    idle_timeout: IdleTimeoutOption = None,
    max_duration: MaxDurationOption = None,
    config: ConfigOption = None,
) -> None:
    """Run a shell command inside a fresh sandbox and print its output."""
    profile = _boot_profile(
        profile_name,
        config,
        Overrides(
            use=SandboxUse.ONE_SHOT,
            workspace=workspace,
            readonly=readonly,
            network=network,
            idle_timeout=idle_timeout,
            max_duration=max_duration,
        ),
    )

    async def execute() -> int:
        async with open_session(profile) as session:
            return await session.shell_stream(
                # 0 is "no limit", like --idle-timeout and --max-duration.
                script,
                stdout=sys.stdout.buffer,
                stderr=sys.stderr.buffer,
                timeout=timeout or None,
            )

    raise typer.Exit(_run(execute()))


@main.command()
def up(
    profile_name: ProfileArgument,
    name: Annotated[
        str | None,
        typer.Option(
            "--name", callback=_sandbox_name, help="Sandbox name. Generated when omitted."
        ),
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
    profile = _boot_profile(
        profile_name,
        config,
        Overrides(
            use=SandboxUse.LONG_LIVED,
            workspace=workspace,
            readonly=readonly,
            network=network,
            idle_timeout=idle_timeout,
            max_duration=max_duration,
        ),
    )

    sandbox_name = name or generate_name(profile)
    restarted = _run(launch_detached(profile, sandbox_name))

    typer.echo(sandbox_name)
    if restarted:
        _hint(f"started the stopped {sandbox_name} again, with the settings it was created with,")
        _hint("secrets and time limits included; new values and flags are ignored;")
        _hint(f"to apply them: {agentbox_command(f'down {sandbox_name}')}, then up again")
    _connect_hints(sandbox_name, profile)
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
        # What `up` booted: kept state, so a failed setup must not clean up like a `run` box.
        made_from = _made_from(name, await created_profile(name), config)
        profile = _apply(Overrides(use=SandboxUse.LONG_LIVED), made_from)
        await launch_detached(profile, name, existing=True)
        return profile

    profile = _run(execute())
    typer.echo(name)
    _connect_hints(name, profile)


@main.command()
def stop(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    force: ForceOption = False,
    config: ConfigOption = None,
) -> None:
    """Stop a sandbox but keep its state; `agentbox start` boots it again.

    A `run` box can't keep its state: stopping deletes it, so it is removed like
    `agentbox down` does, its own files in shared volumes too.
    """

    deleted, left = _run(stop_sandbox(name, force=force, profile=_cleanup_profile(name, config)))
    if deleted:
        _hint(f"{name} was made by `agentbox run`, so stopping deletes it")
        _removed_hints(name, left)
    else:
        _hint(
            f"stopped {name}; {agentbox_command(f'start {name}')} boots it again"
            f" ({msb_command(f'start {name}')} for a box not made by agentbox)"
        )


@main.command("ls")
def list_running() -> None:
    """List sandboxes known to the microsandbox runtime."""
    infos = _run(list_sandboxes())
    if not infos:
        _hint("no sandboxes")
        return
    for info in infos:
        typer.echo(f"{info.name:<34} {info.status:<10} {info.profile or '-'}")


@main.command()
def down(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    force: ForceOption = False,
    config: ConfigOption = None,
) -> None:
    """Stop a sandbox and delete its persisted state, its own files in shared volumes too."""

    left = _run(destroy_sandbox(name, force=force, profile=_cleanup_profile(name, config)))
    _removed_hints(name, left)


def _made_from(name: str, profile_name: str, config: Path | None) -> AgentProfile:
    """Box ``name``'s profile ``profile_name``, as ``config`` defines it now."""
    try:
        return get_profile(profile_name, config)
    except ProfileNotFoundError as exc:
        # Most likely `up` read another profiles.toml than this run does.
        raise AgentboxError(
            f"{name} was made from profile {profile_name!r}: {exc}; "
            "pass the --config it was brought up with"
        ) from exc


def _cleanup_profile(name: str, config: Path | None) -> Callable[[str], AgentProfile | None]:
    """Loads the profile that names box ``name``'s own files in shared volumes, or ``None``."""

    def load(profile_name: str) -> AgentProfile | None:
        try:
            return _made_from(name, profile_name, config)
        except AgentboxError as exc:
            # Its profile is gone or the config is broken: removing the box matters more,
            # but its own files in shared volumes stay, so say why.
            _hint(f"{exc}; its own files in shared volumes are not deleted")
            return None

    return load


def _removed_hints(name: str, left: list[str]) -> None:
    _hint(f"removed {name}")
    if left:
        _hint(f"it was stopped, so these stay in its shared volume: {' '.join(left)}")


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


def _connect_hints(name: str, profile: AgentProfile) -> None:
    """How to get into a detached box, and how to remove it."""
    _hint(msb_command(f"exec {name} -- {profile.shell}"))
    _hint(agentbox_command(f"down {name}"))


def _hint(message: str) -> None:
    """Human-facing text on stderr, so stdout stays pipeable."""
    typer.secho(message, fg=typer.colors.BRIGHT_BLACK, err=True)


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one command's async work, reporting an ``AgentboxError`` like :func:`_guard`."""
    return _guard(lambda: asyncio.run(coro))


def _guard(action: Callable[[], T]) -> T:
    try:
        return action()
    except AgentboxError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        if hint := _next_step(exc):
            _hint(hint)
        raise typer.Exit(2) from exc


def _next_step(exc: AgentboxError) -> str | None:
    """What to run after ``exc``: the commands are the CLI's to name, not the layers below."""
    match exc:
        case SandboxConflictError(name=name):
            # The box may be in use, or not agentbox's: removing it is the last resort.
            return (
                f"pick another --name, or delete it with {agentbox_command(f'down {name}')}"
                " first (its state too)"
            )
        case SecretNotSetError(name=str() as name):
            return (
                f"if {name} is a stopped box, {agentbox_command(f'start {name}')}"
                " restarts it with the secrets it has"
            )
        case NotAgentboxSandboxError(name=name):
            return f"start it with {msb_command(f'start {name}')}"
    return None
