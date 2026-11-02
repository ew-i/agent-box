"""Session layer: boot a sandbox from a profile and drive it from Python."""

from __future__ import annotations

import asyncio
import contextlib
import io
import shlex
import sys
import uuid
import warnings
from typing import TYPE_CHECKING, Any, BinaryIO, Self

from agentbox.config import sandbox_labels
from agentbox.errors import (
    SandboxConflictError,
    SandboxOperationError,
    SandboxStartupError,
    wrap_errors,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from agentbox.config import AgentProfile

_NAME_SUFFIX_LENGTH = 8
"""Hex characters of randomness appended to a generated sandbox name."""

_SIDE_FILES = ("", "-wal", "-shm", "-journal")
"""A per-box file itself, then the files SQLite keeps next to a database."""


class AgentSession:
    """A running microVM, scoped to one coding-agent task.

    Prefer :func:`open_session`, which handles setup and teardown::

        async with open_session(profile) as session:
            code = await session.shell_stream("pytest -q", stdout=out, stderr=err)
    """

    def __init__(self, profile: AgentProfile, name: str, *, sandbox: Any | None = None) -> None:
        """``sandbox`` wraps one that already runs, e.g. from ``handle.connect()``."""
        self.profile = profile
        self.name = name
        self._sandbox: Any | None = sandbox
        self.restarted = False

    @property
    def sandbox(self) -> Any:
        """The underlying ``microsandbox.Sandbox``, for APIs agentbox does not wrap."""
        if self._sandbox is None:
            raise SandboxStartupError(f"session {self.name!r} is not started")
        return self._sandbox

    async def start(self, *, detached: bool = False, existing: bool = False) -> Self:
        """Boot the sandbox, then run the profile's setup script in it.

        ``existing`` starts a stopped sandbox of this name as it was created, without
        checking it against this profile: only the setup script and workdir come from it.
        """
        with wrap_errors(f"could not start sandbox {self.name!r}", SandboxStartupError):
            if existing:
                await self._restart(detached=detached)
            else:
                await self._create(detached=detached)
        return await self._run_setup()

    async def _restart(self, *, detached: bool) -> None:
        """Start the stopped sandbox of this name, with the settings it was created with."""
        from microsandbox import Sandbox

        self._sandbox = await Sandbox.start(self.name, detached=detached)
        self.restarted = True

    async def _create(self, *, detached: bool) -> None:
        from microsandbox import Sandbox, SandboxAlreadyExistsError

        from agentbox.microsandbox_adapter import to_sandbox_kwargs

        kwargs = to_sandbox_kwargs(self.profile, self.name)
        if detached:
            kwargs["detached"] = True
        existed = await sandbox_exists(self.name)
        try:
            self._sandbox = await Sandbox.create(self.name, **kwargs)
        except asyncio.CancelledError:
            # Ctrl-C mid-create: the box may exist without this session holding it, and a
            # detached one would then run unowned. Only one that wasn't there before is ours.
            if not existed:
                with contextlib.suppress(Exception):
                    await (await Sandbox.get(self.name)).destroy(force=True)
            raise
        except SandboxAlreadyExistsError:
            # A stopped `up` box keeps its name and state: start it again, with the settings
            # it was created with. A running one raises "already running" here.
            if sandbox_labels(await Sandbox.get(self.name)) != kwargs["labels"]:
                raise SandboxConflictError(self.name) from None
            await self._restart(detached=detached)

    async def _run_setup(self) -> Self:
        if script := self.profile.setup_script:
            # __aexit__ never runs when __aenter__ raises, so nothing else would stop it.
            try:
                # All to stderr: `up` keeps stdout for the sandbox name.
                out = sys.stderr.buffer
                if code := await self.shell_stream(script, stdout=out, stderr=out):
                    raise SandboxStartupError(f"setup script {script!r} exited with {code}")
            except BaseException:
                await self.stop(remove=not self.restarted)
                raise
        return self

    async def detach(self) -> str:
        """Release the sandbox and leave it running, so :meth:`stop` no longer owns it.

        Returns the sandbox name, ready for ``msb exec <name> -- ...``.
        """
        with wrap_errors(f"could not detach sandbox {self.name!r}"):
            await self.sandbox.detach()
        self._sandbox = None
        return self.name

    async def stop(self, *, remove: bool = False) -> None:
        """Stop the sandbox; ``remove`` deletes it too, e.g. a new box whose start failed.

        A new box has no state worth keeping, and nobody may know its generated name.
        """
        if self._sandbox is None:
            return
        if (self.profile.ephemeral or remove) and not await self.delete_box_files():
            # Nothing reuses a `run` box's files under its generated name, so say what stays,
            # e.g. when its time limit stopped it before rm could run.
            warnings.warn(
                f"these stay in the shared volume of {self.name!r}: "
                + " ".join(self.profile.per_box_files(self.name)),
                RuntimeWarning,
                stacklevel=2,
            )
        try:
            await (self._sandbox.destroy(force=True) if remove else self._sandbox.stop())
        except Exception as exc:
            # Raising here would mask the error that ended the session; a warning still says
            # the microVM may be left running.
            verb = "remove" if remove else "stop"
            warnings.warn(
                f"could not {verb} sandbox {self.name!r}: {exc}; it may still be running",
                RuntimeWarning,
                stacklevel=2,
            )
        self._sandbox = None

    async def delete_box_files(self) -> bool:
        """Delete this box's files in shared volumes (see ``AgentProfile.per_box_files``).

        SQLite's side files go too, named exactly: a ``path-*`` glob would also take the files
        of a box whose name starts with this one's. Best effort: returns whether it worked,
        never raises, since stopping or removing the box matters more.
        """
        if not (paths := self.profile.per_box_files(self.name)):
            return True
        files = " ".join(shlex.quote(path + suffix) for path in paths for suffix in _SIDE_FILES)
        try:
            code = await self.shell_stream(
                f"rm -f -- {files}", stdout=io.BytesIO(), stderr=io.BytesIO(), timeout=10
            )
        except Exception:
            return False
        return code == 0

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
        with wrap_errors(f"could not run {script!r}"):
            handle = await self.sandbox.shell_stream(
                script, cwd=self.profile.workdir, timeout=timeout
            )
            async for event in handle:
                if event.event_type == ExecEventType.EXITED:
                    exit_code = int(event.code)
                elif (target := targets.get(event.event_type)) is not None:
                    target.write(event.data)
                    target.flush()
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


async def sandbox_exists(name: str) -> bool:
    """Whether the runtime knows a sandbox ``name``; raises what else its lookup raises."""
    from microsandbox import Sandbox, SandboxNotFoundError

    try:
        await Sandbox.get(name)
    except SandboxNotFoundError:
        return False
    return True


def generate_name(profile: AgentProfile) -> str:
    """A collision-resistant sandbox name derived from the profile."""
    return f"agentbox-{profile.name}-{uuid.uuid4().hex[:_NAME_SUFFIX_LENGTH]}"


async def launch_detached(profile: AgentProfile, name: str, *, existing: bool = False) -> bool:
    """Boot a sandbox that keeps running after this process exits.

    Returns whether a stopped sandbox of that name was started again instead of created.
    ``existing`` only starts a stopped one, as it was created (see :meth:`AgentSession.start`).
    """
    session = AgentSession(profile, name)
    await session.start(detached=True, existing=existing)
    try:
        await session.detach()
    except SandboxOperationError:
        # Nobody would own a box left running here; stop() warns if even that fails.
        await session.stop(remove=not session.restarted)
        raise
    return session.restarted
