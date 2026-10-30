"""Session layer: boot a sandbox from a profile and drive it from Python."""

from __future__ import annotations

import contextlib
import sys
import uuid
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Self

from agentbox.errors import SandboxOperationError, SandboxStartupError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from agentbox.config import AgentProfile

_NAME_SUFFIX_LENGTH = 8
"""Hex characters of randomness appended to a generated sandbox name."""


class AgentSession:
    """A running microVM, scoped to one coding-agent task.

    Prefer :func:`open_session`, which handles setup and teardown::

        async with open_session(profile) as session:
            code = await session.shell_stream("pytest -q", stdout=out, stderr=err)
    """

    def __init__(self, profile: AgentProfile, name: str) -> None:
        self.profile = profile
        self.name = name
        self._sandbox: Any | None = None
        self.restarted = False

    @property
    def sandbox(self) -> Any:
        """The underlying ``microsandbox.Sandbox``, for APIs agentbox does not wrap."""
        if self._sandbox is None:
            raise SandboxStartupError(f"session {self.name!r} is not started")
        return self._sandbox

    async def start(self, *, detached: bool = False) -> Self:
        from microsandbox import Sandbox, SandboxAlreadyExistsError

        from agentbox.microsandbox_adapter import to_sandbox_kwargs

        kwargs = to_sandbox_kwargs(self.profile)
        if detached:
            kwargs["detached"] = True

        try:
            try:
                self._sandbox = await Sandbox.create(self.name, **kwargs)
            except SandboxAlreadyExistsError:
                # A stopped `up` box keeps its name and state: start it again, with the settings
                # it was created with. A running one raises "already running" here.
                existing = (await Sandbox.get(self.name)).config().get("labels") or {}
                if existing != kwargs["labels"]:
                    raise SandboxStartupError(
                        "it exists with another profile, workspace, home volume or network; "
                        "remove it with "
                        f"`agentbox down {self.name}` first"
                    ) from None
                self._sandbox = await Sandbox.start(self.name, detached=detached)
                self.restarted = True
        except Exception as exc:
            raise SandboxStartupError(f"could not start sandbox {self.name!r}: {exc}") from exc

        if script := self.profile.setup_script:
            # __aexit__ never runs when __aenter__ raises, so nothing else would stop it.
            try:
                # All to stderr: `up` keeps stdout for the sandbox name.
                out = sys.stderr.buffer
                if code := await self.shell_stream(script, stdout=out, stderr=out):
                    raise SandboxStartupError(f"setup script {script!r} exited with {code}")
            except BaseException:
                await self.stop()
                raise
        return self

    async def detach(self) -> str:
        """Release the sandbox and leave it running, so :meth:`stop` no longer owns it.

        Returns the sandbox name, ready for ``msb exec <name> -- ...``.
        """
        try:
            await self.sandbox.detach()
        except Exception as exc:
            raise SandboxOperationError(f"could not detach sandbox {self.name!r}: {exc}") from exc
        self._sandbox = None
        return self.name

    async def stop(self) -> None:
        if self._sandbox is None:
            return
        try:
            await self._sandbox.stop()
        except Exception as exc:
            # Raising here would mask the error that ended the session; a warning still says
            # the microVM may be left running.
            warnings.warn(
                f"could not stop sandbox {self.name!r}: {exc}; "
                f"remove it with `agentbox down {self.name}`",
                RuntimeWarning,
                stacklevel=2,
            )
        self._sandbox = None

    async def shell_stream(
        self,
        script: str,
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        timeout: float | None = None,
    ) -> int:
        """Run a shell snippet, writing its output as it arrives. Returns the exit code."""
        from microsandbox import ExecEventType

        targets = {ExecEventType.STDOUT: stdout, ExecEventType.STDERR: stderr}
        exit_code: int | None = None
        try:
            handle = await self.sandbox.shell_stream(
                script, cwd=self.profile.workdir, timeout=timeout
            )
            async for event in handle:
                if event.event_type == ExecEventType.EXITED:
                    exit_code = int(event.code)
                elif (target := targets.get(event.event_type)) is not None:
                    target.write(event.data)
                    target.flush()
        except Exception as exc:
            raise SandboxOperationError(f"could not run {script!r}: {exc}") from exc
        if exit_code is None:
            raise SandboxOperationError(f"could not run {script!r}: it ended without an exit code")
        return exit_code

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()


@contextlib.asynccontextmanager
async def open_session(profile: AgentProfile) -> AsyncIterator[AgentSession]:
    """Boot a sandbox for ``profile`` under a generated name and tear it down on exit."""
    session = AgentSession(profile, generate_name(profile))
    async with session:
        yield session


def generate_name(profile: AgentProfile) -> str:
    """A collision-resistant sandbox name derived from the profile."""
    return f"agentbox-{profile.name}-{uuid.uuid4().hex[:_NAME_SUFFIX_LENGTH]}"


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    """One sandbox known to the microsandbox runtime."""

    name: str
    status: str
    profile: str | None


async def launch_detached(profile: AgentProfile, name: str) -> bool:
    """Boot a sandbox that keeps running after this process exits.

    Returns whether a stopped sandbox of that name was started again instead of created.
    """
    session = AgentSession(profile, name)
    await session.start(detached=True)
    try:
        await session.detach()
    except SandboxOperationError:
        # Nobody would own a box left running here; stop() warns if even that fails.
        await session.stop()
        raise
    return session.restarted


async def list_sandboxes() -> list[SandboxInfo]:
    """Every sandbox the runtime knows about, newest first."""
    from microsandbox import Sandbox

    infos: list[SandboxInfo] = []
    cursor: str | None = None
    try:
        while True:
            page = await Sandbox.list_with(cursor=cursor)
            for handle in page.sandboxes:
                infos.append(SandboxInfo(handle.name, str(handle.status), _profile_label(handle)))
            cursor = page.next_cursor
            if cursor is None:
                return infos
    except Exception as exc:
        raise SandboxOperationError(f"could not list sandboxes: {exc}") from exc


async def destroy_sandbox(name: str, *, force: bool = False) -> None:
    """Stop a sandbox and delete its persisted state."""
    from microsandbox import Sandbox

    try:
        handle = await Sandbox.get(name)
        await handle.destroy(force=force)
    except Exception as exc:
        raise SandboxOperationError(f"could not remove sandbox {name!r}: {exc}") from exc


def _profile_label(handle: Any) -> str | None:
    """Best-effort read of the ``agentbox.profile`` label; not every backend exposes it."""
    try:
        labels = handle.config().get("labels") or {}
    except Exception:
        return None
    label: str | None = labels.get("agentbox.profile")
    return label
