from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from microsandbox import SandboxNotFoundError


@dataclass
class FakeExecEvent:
    """One event from ``Sandbox.shell_stream``; ``event_type`` is an ``ExecEventType`` value."""

    event_type: str
    data: bytes | None = None
    code: int | None = None


@dataclass
class ExecCall:
    """One command the session asked the sandbox to run, and how it was framed."""

    command: str
    cwd: str | None
    timeout: float | None


class FakeSandbox:
    """A started sandbox that records every command instead of running it."""

    def __init__(self, events: list[FakeExecEvent] | None = None) -> None:
        self.events = events if events is not None else [FakeExecEvent("exited", code=0)]
        self.calls: list[ExecCall] = []
        self.stopped = False
        self.detached = False
        self.destroyed: dict[str, Any] | None = None

    @property
    def commands(self) -> list[str]:
        return [call.command for call in self.calls]

    async def shell_stream(
        self,
        script: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[FakeExecEvent]:
        self.calls.append(ExecCall(script, cwd, timeout))

        async def events() -> AsyncIterator[FakeExecEvent]:
            for event in self.events:
                yield event

        return events()

    async def detach(self) -> None:
        self.detached = True

    async def stop(self) -> None:
        self.stopped = True

    async def destroy(self, *, force: bool = False) -> None:
        self.destroyed = {"force": force}


class FakeSandboxFactory:
    """Stands in for the ``microsandbox.Sandbox`` class: ``create``, ``start``, ``get``, ``list_with``."""

    def __init__(self) -> None:
        self.events: list[FakeExecEvent] | None = None
        self.handles: dict[str, FakeHandle] = {}
        self.pages: list[FakePage] = []
        self.list_error: Exception | None = None
        self.cursors: list[str | None] = []
        self.error: Exception | None = None
        self.restart_error: Exception | None = None
        self.names: list[str] = []
        self.restarts: list[tuple[str, bool]] = []
        self.kwargs: list[dict[str, Any]] = []
        self._created: FakeSandbox | None = None

    @property
    def started(self) -> FakeSandbox:
        """The sandbox most recently created; fails loudly if ``create`` never ran."""
        assert self._created is not None, "no sandbox was created"
        return self._created

    async def create(self, name: str, **kwargs: Any) -> FakeSandbox:
        self.names.append(name)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        self._created = FakeSandbox(self.events)
        return self._created

    def add(self, handle: FakeHandle) -> FakeHandle:
        """Make ``handle`` a sandbox the runtime knows by its name."""
        self.handles[handle.name] = handle
        return handle

    async def get(self, name: str) -> FakeHandle:
        """An added sandbox, else a stopped one created with the last ``create``'s settings."""
        if name in self.handles:
            return self.handles[name]
        if not self.kwargs:
            raise SandboxNotFoundError(f"sandbox {name!r} not found")
        return FakeHandle(name, "stopped", self.kwargs[-1]["labels"])

    async def list_with(self, *, cursor: str | None = None) -> FakePage:
        if self.list_error is not None:
            raise self.list_error
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]

    async def start(self, name: str, *, detached: bool = False) -> FakeSandbox:
        """``Sandbox.start``: boot a stopped sandbox that already exists."""
        self.restarts.append((name, detached))
        if self.restart_error is not None:
            raise self.restart_error
        self._created = FakeSandbox(self.events)
        return self._created


class FakeHandle:
    """One sandbox as the runtime reports it to ``list``/``get``."""

    def __init__(self, name: str, status: str, labels: dict[str, str]) -> None:
        self.name = name
        self.status = status
        self.labels = labels
        self.config_error: Exception | None = None
        self.destroyed: dict[str, Any] | None = None
        self.destroy_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.stopped_with: str | None = None
        self.connected: FakeSandbox | None = None

    async def connect(self) -> FakeSandbox:
        """A running box, ready for commands; ``connected`` keeps what it was asked to run."""
        self.connected = FakeSandbox()
        return self.connected

    def config(self) -> dict[str, Any]:
        if self.config_error is not None:
            raise self.config_error
        return {"labels": self.labels}

    async def stop(self) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped_with = "stop"

    async def kill(self) -> None:
        self.stopped_with = "kill"

    async def destroy(self, *, force: bool = False) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed = {"force": force}


@dataclass
class FakePage:
    sandboxes: list[FakeHandle]
    next_cursor: str | None
