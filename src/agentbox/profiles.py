from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentbox.config import AgentProfile, NetworkMode
from agentbox.errors import ProfileConfigError, ProfileNotFoundError

CONFIG_ENV_VAR = "AGENTBOX_CONFIG"
XDG_CONFIG_ENV_VAR = "XDG_CONFIG_HOME"
CONFIG_FILE_NAME = "profiles.toml"

_AGENT_BOX = AgentProfile(
    name="agent-box",
    description="This repo's Dockerfile: mise toolchains and agents, home in a volume.",
    image="agent-box",
    cpus=4,
    memory_mib=4096,
    # mise pulls toolchains from many hosts: nodejs.org, GitHub, Adoptium, Maven Central
    network=NetworkMode.PUBLIC,
    named_volumes={"/home/agent-box": "agent-box-home"},
    # Sandbox.create skips the image entrypoint: it seeds the volume and runs `mise install`
    setup_script="agent-box-entrypoint true",
    max_duration_s=7200,
)

BUILTIN_PROFILES: dict[str, AgentProfile] = {
    profile.name: profile
    for profile in (
        AgentProfile(
            name="airgapped",
            description="No network at all. Safest place to execute agent-generated code.",
            network=NetworkMode.OFFLINE,
        ),
        _AGENT_BOX,
        _AGENT_BOX.model_copy(
            update={
                "name": "agent-box-php-docker",
                "description": "agent-box plus PHP and Docker Engine, dockerd started on boot.",
                "image": "agent-box:php-docker",
                # dockerd can't stack overlayfs on the guest's overlayfs root, and two daemons
                # must never share one data folder: each sandbox gets its own disk
                "disks": {"/var/lib/docker": 20_480},
                # start-dockerd.sh in this repo, run after the entrypoint's seeding and mise
                "setup_script": "agent-box-entrypoint agent-box-start-dockerd",
            }
        ),
    )
}


class _ProfilesFile(BaseModel):
    """Shape of ``profiles.toml``, so pydantic reports structural errors uniformly."""

    model_config = ConfigDict(extra="forbid")
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)


def load_profiles(path: Path | None = None) -> dict[str, AgentProfile]:
    """Return the built-in profiles merged with user-defined ones.

    The config file is looked up in ``path``, then ``$AGENTBOX_CONFIG``, then
    :func:`default_config_path`. User profiles override built-ins of the same name.

    Raises :class:`~agentbox.errors.ProfileConfigError` when a config file is present but
    unreadable or invalid, or when ``path`` or ``$AGENTBOX_CONFIG`` names a missing file.
    Only a missing default file is not an error.
    """
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        if path is not None or os.environ.get(CONFIG_ENV_VAR):
            # A typo here would silently boot a built-in in place of the user's override.
            raise ProfileConfigError(config_path, "no such file")
        return dict(BUILTIN_PROFILES)
    return dict(BUILTIN_PROFILES) | _read_user_profiles(config_path)


def default_config_path() -> Path:
    """Where agentbox looks when no path and no ``$AGENTBOX_CONFIG`` are given.

    ``$XDG_CONFIG_HOME/agentbox/profiles.toml`` when that variable names an absolute
    directory, otherwise ``~/.config/agentbox/profiles.toml``. Resolved per call rather than
    once at import, so a changed environment takes effect.
    """
    return _config_home() / "agentbox" / CONFIG_FILE_NAME


def _config_home() -> Path:
    """The XDG base directory for config, ignoring a relative value as the spec requires."""
    configured = os.environ.get(XDG_CONFIG_ENV_VAR)
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".config"


def get_profile(name: str, path: Path | None = None) -> AgentProfile:
    """Look up a single profile by name."""
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError:
        raise ProfileNotFoundError(name, list(profiles)) from None


def _read_user_profiles(config_path: Path) -> dict[str, AgentProfile]:
    """Parse and validate one config file, reporting every failure as a AgentboxError."""
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileConfigError(config_path, str(exc)) from exc

    try:
        tables = _ProfilesFile.model_validate(document).profiles
    except ValidationError as exc:
        raise ProfileConfigError(config_path, _first_problem(exc)) from exc

    profiles: dict[str, AgentProfile] = {}
    for name, table in tables.items():
        try:
            # The table key is the name everything else looks the profile up by, so it wins.
            profiles[name] = AgentProfile.model_validate({**table, "name": name})
        except ValidationError as exc:
            raise ProfileConfigError(config_path, _first_problem(exc, "profiles", name)) from exc
    return profiles


def _first_problem(error: ValidationError, *prefix: str) -> str:
    """One readable line out of pydantic's multi-error report, located from the file's root."""
    problem = error.errors()[0]
    location = ".".join([*prefix, *(str(part) for part in problem["loc"])])
    return f"{location or 'profiles'}: {problem['msg']}"


def _resolve_config_path(path: Path | None) -> Path:
    if path is not None:
        return path
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    return default_config_path()
