"""What a command's flags and purpose do to a profile before it boots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agentbox.config import AgentProfile, first_problem

if TYPE_CHECKING:
    from pathlib import Path

    from agentbox.config import NetworkMode


class SandboxUse(StrEnum):
    """What a sandbox is for, which decides whether it keeps state and auto-stops.

    The values are the command names, so ``agentbox show airgapped --for up`` reads as a
    question about what ``agentbox up airgapped`` would boot.
    """

    ONE_SHOT = "run"
    LONG_LIVED = "up"


@dataclass(frozen=True)
class Overrides:
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
        """The profile that would actually boot; raises ``ValueError`` if it is invalid."""
        updates: dict[str, object] = {}
        if self.workspace is not None:
            # A dict, not a WorkspaceMount: its checks then run in the one validation below.
            mount: dict[str, object] = {"host_path": self.workspace}
            if (own := profile.workspace) is not None:
                # Only the host side changes: the profile's workdir may point at its guest path.
                mount["guest_path"] = own.guest_path
                mount["readonly"] = self.readonly or own.readonly
            updates["workspace"] = mount
        elif self.readonly:
            # Without --workspace the flag applies to the profile's own mount, never silently lost.
            if profile.workspace is None:
                raise ValueError("--readonly: nothing to mount read-only; pass --workspace")
            updates["workspace"] = profile.workspace.model_copy(update={"readonly": True})
        if self.network is not None:
            updates["network"] = self.network
        # Validated again, not model_copy: the profile's checks must hold for what the flags made.
        try:
            effective = AgentProfile.model_validate(
                profile.model_dump() | updates | self._lifetime_updates()
            )
        except ValidationError as exc:
            raise ValueError(first_problem(exc)) from exc
        if effective.workspace is not None:
            effective.workspace.check_not_containing_home()
        return effective

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
