from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


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
    env: dict[str, str] | None
    timeout: float | None


class FakeSandbox:
    """A started sandbox that records every command instead of running it."""

    def __init__(self, events: list[FakeExecEvent] | None = None) -> None:
        self.events = events if events is not None else [FakeExecEvent("exited", code=0)]
        self.calls: list[ExecCall] = []
        self.stopped = False
        self.detached = False

    @property
    def commands(self) -> list[str]:
        return [call.command for call in self.calls]

    async def shell_stream(
        self,
        script: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[FakeExecEvent]:
        self.calls.append(ExecCall(script, cwd, env, timeout))

        async def events() -> AsyncIterator[FakeExecEvent]:
            for event in self.events:
                yield event

        return events()

    async def detach(self) -> None:
        self.detached = True

    async def stop(self) -> None:
        self.stopped = True


class FakeSandboxFactory:
    """Stands in for ``microsandbox.Sandbox`` while a session starts."""

    def __init__(self) -> None:
        self.events: list[FakeExecEvent] | None = None
        self.existing_labels: dict[str, str] | None = None
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

    async def get(self, name: str) -> FakeHandle:
        """The existing sandbox; created from the same settings unless ``existing_labels`` says not."""
        labels = self.existing_labels if self.existing_labels is not None else self.kwargs[-1]["labels"]
        return FakeHandle(name, "stopped", labels)

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

    def config(self) -> dict[str, Any]:
        if self.config_error is not None:
            raise self.config_error
        return {"labels": self.labels}

    async def destroy(self, *, force: bool = False) -> None:
        self.destroyed = {"force": force}


@dataclass
class FakePage:
    sandboxes: list[FakeHandle]
    next_cursor: str | None


class FakeListing:
    """Stands in for ``microsandbox.Sandbox`` during ``list_sandboxes()``."""

    def __init__(self, pages: list[FakePage], error: Exception | None = None) -> None:
        self.pages = pages
        self.error = error
        self.cursors: list[str | None] = []

    async def list_with(self, *, cursor: str | None = None) -> FakePage:
        if self.error is not None:
            raise self.error
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]


class FakeLookup:
    """Stands in for ``microsandbox.Sandbox`` during ``destroy_sandbox()``."""

    def __init__(self, handle: FakeHandle | None, error: Exception | None = None) -> None:
        self.handle = handle
        self.error = error

    async def get(self, name: str) -> FakeHandle:
        if self.error is not None:
            raise self.error
        assert self.handle is not None, "configure a handle or an error"
        return self.handle
