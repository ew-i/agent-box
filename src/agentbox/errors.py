from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class AgentboxError(Exception):
    """Base class for every error raised by agentbox."""


class ProfileNotFoundError(AgentboxError):
    """Raised when a requested agent profile does not exist."""

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(f"unknown profile {name!r}; available: {', '.join(sorted(available))}")


class ProfileConfigError(AgentboxError):
    """Raised when a profile config file exists but cannot be read or understood."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"invalid profile config {path}: {reason}")


class SandboxStartupError(AgentboxError):
    """Raised when the microVM could not be created or reached."""


class SandboxConflictError(SandboxStartupError):
    """Raised when a stopped sandbox of that name was created with other settings."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"could not start sandbox {name!r}: it exists with another profile, workspace, "
            "home volume or network"
        )


class SecretNotSetError(SandboxStartupError):
    """Raised when a profile's secret has no value in the host environment."""

    def __init__(self, var: str, sandbox: str | None) -> None:
        """``sandbox`` is a box a restart could bring back instead, ``None`` for a `run` box."""
        self.name = sandbox
        super().__init__(f"secret {var} is not set in your environment; export it first")


class NotAgentboxSandboxError(AgentboxError):
    """Raised when a sandbox exists but carries no agentbox profile label."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"{name!r} has no agentbox profile label, so it wasn't made by `agentbox up`"
        )


class SandboxOperationError(AgentboxError):
    """Raised when running a command in, listing, stopping or removing a sandbox fails."""


@contextlib.contextmanager
def wrap_errors(message: str, error: type[AgentboxError] = SandboxOperationError) -> Iterator[None]:
    """Report any exception raised inside as ``error``, reading ``message: what went wrong``.

    An ``AgentboxError`` passes through as it is: it already says what went wrong, and the
    CLI tells its kinds apart to say what to do next.
    """
    try:
        yield
    except AgentboxError:
        raise
    except Exception as exc:
        raise error(f"{message}: {exc}") from exc
