"""Manage sandboxes the runtime already knows, by name: list, find, stop and remove them."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentbox.config import PROFILE_LABEL, is_ephemeral, sandbox_labels
from agentbox.errors import NotAgentboxSandboxError, wrap_errors
from agentbox.session import AgentSession, sandbox_exists

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentbox.config import AgentProfile

    # Loads a box's profile by the name in its label, or None if it can't; called only when needed.
    ProfileLoader = Callable[[str], AgentProfile | None]


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    """One sandbox known to the microsandbox runtime."""

    name: str
    status: str
    profile: str | None


async def list_sandboxes() -> list[SandboxInfo]:
    """Every sandbox the runtime knows about, in the order it reports them."""
    from microsandbox import Sandbox

    infos: list[SandboxInfo] = []
    cursor: str | None = None
    with wrap_errors("could not list sandboxes"):
        while True:
            page = await Sandbox.list_with(cursor=cursor)
            for handle in page.sandboxes:
                infos.append(
                    SandboxInfo(handle.name, str(handle.status), _label(handle, PROFILE_LABEL))
                )
            cursor = page.next_cursor
            if cursor is None:
                return infos


async def created_profile(name: str) -> str:
    """The name of the profile an existing sandbox was created from by ``agentbox up``."""
    profile = _read_labels(await _get(name, "find"), name).get(PROFILE_LABEL)
    if profile is None:
        raise NotAgentboxSandboxError(name)
    return profile


async def stop_sandbox(
    name: str, *, force: bool = False, profile: ProfileLoader | None = None
) -> tuple[bool, list[str]]:
    """Stop a sandbox. Returns whether it was deleted, and the files it had to leave behind.

    An ``up`` box keeps its state for ``agentbox start``: ``(False, [])``. A ``run`` box is
    ephemeral, so stopping deletes it: it is removed like :func:`destroy_sandbox` does, its
    files in shared volumes too, and ``profile`` is only called for such a box.
    """
    handle = await _get(name, "stop")
    if is_ephemeral(_read_labels(handle, name)):
        return True, await _destroy(handle, name, force=force, profile=profile)
    with wrap_errors(f"could not stop sandbox {name!r}"):
        await (handle.kill() if force else handle.stop())
    try:
        gone = not await sandbox_exists(name)
    except Exception:
        gone = False  # The stop worked; a failed lookup must not report otherwise.
    if gone:
        # Ephemeral without the label, e.g. a `run` box from before it: stopping deleted it,
        # too late to remove its own files in shared volumes.
        made_from = _made_from(handle, name, profile)
        return True, made_from.per_box_files(name) if made_from is not None else []
    return False, []


async def destroy_sandbox(
    name: str,
    *,
    force: bool = False,
    profile: ProfileLoader | None = None,
    keep_files: bool = False,
) -> list[str]:
    """Stop a sandbox and delete its persisted state.

    With the ``profile`` it was made from, its files in shared volumes go too, but only while
    it runs: a stopped box can't run ``rm``. Returns the files it had to leave behind.
    ``keep_files`` leaves them on purpose (e.g. OpenCode history for a later ``up`` of the
    same name) and returns ``[]``.
    """
    return await _destroy(
        await _get(name, "remove"), name, force=force, profile=profile, keep_files=keep_files
    )


async def _destroy(
    handle: Any, name: str, *, force: bool, profile: ProfileLoader | None, keep_files: bool = False
) -> list[str]:
    from microsandbox import SandboxStatus

    made_from = None if keep_files else _made_from(handle, name, profile)
    left = made_from.per_box_files(name) if made_from is not None else []
    with wrap_errors(f"could not remove sandbox {name!r}"):
        if made_from is not None and left and handle.status == SandboxStatus.RUNNING:
            with contextlib.suppress(Exception):
                session = AgentSession(made_from, name, sandbox=await handle.connect())
                if await session.delete_box_files():
                    left = []
        await handle.destroy(force=force)
    return left


def _made_from(handle: Any, name: str, profile: ProfileLoader | None) -> AgentProfile | None:
    """The profile box ``name`` was made from, read from the handle in hand: no second lookup.

    No label means not made by agentbox, so there is nothing of its own beyond the box.
    """
    if profile is None or not (profile_name := _read_labels(handle, name).get(PROFILE_LABEL)):
        return None
    return profile(profile_name)


async def _get(name: str, verb: str) -> Any:
    """The runtime's handle on sandbox ``name``; a failed lookup says what it was for."""
    from microsandbox import Sandbox

    with wrap_errors(f"could not {verb} sandbox {name!r}"):
        return await Sandbox.get(name)


def _read_labels(handle: Any, name: str) -> dict[str, str]:
    """Box ``name``'s labels. A failed read is an error: it must not pass for a box without
    them, e.g. a `run` box taken for one that keeps its state."""
    with wrap_errors(f"could not read sandbox {name!r}"):
        return sandbox_labels(handle)


def _label(handle: Any, key: str) -> str | None:
    """Best-effort read of one label; not every backend exposes them."""
    try:
        return sandbox_labels(handle).get(key)
    except Exception:
        return None
