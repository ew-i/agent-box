from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


class SandboxOperationError(AgentboxError):
    """Raised when running a command in, listing, stopping or removing a sandbox fails."""
