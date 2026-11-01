from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentbox.config import AgentProfile, HostSecret, NetworkMode, WorkspaceMount

# --------------------------------------------------------------------------- defaults


def test_given_no_options_when_creating_a_profile_then_applies_restrictive_defaults() -> None:
    profile = AgentProfile(name="default")

    assert profile.network is NetworkMode.ALLOWLIST
    assert profile.allowed_domains == ()
    assert profile.ephemeral is True


def test_given_a_profile_when_assigning_a_field_then_raises_validation_error() -> None:
    profile = AgentProfile(name="default")

    with pytest.raises(ValidationError):
        profile.image = "evil:latest"  # type: ignore[misc]


# --------------------------------------------------------------------------- validation


def test_given_an_unknown_field_when_creating_a_profile_then_rejects_it() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="typo", memroy_mib=512)  # type: ignore[call-arg]


def test_given_an_empty_name_when_creating_a_profile_then_rejects_it() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="")


@pytest.mark.parametrize(
    ("field", "value"),
    [("cpus", 0), ("cpus", 33), ("memory_mib", 128), ("memory_mib", 1_000_000)],
)
def test_given_out_of_bounds_resources_when_creating_a_profile_then_rejects_them(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="greedy", **{field: value})


@pytest.mark.parametrize("field", ["max_duration_s", "idle_timeout_s"])
def test_given_a_negative_lifetime_when_creating_a_profile_then_rejects_it(field: str) -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="past", **{field: -1})


@pytest.mark.parametrize("disks", [{"/var/lib/docker": 0}, {"var/lib/docker": 1024}])
def test_given_an_empty_or_relative_disk_when_creating_a_profile_then_rejects_it(disks: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="disky", disks=disks)


@pytest.mark.parametrize("secrets", [{"NOT-A-VAR": {"hosts": ["a.io"]}}, {"KEY": {"hosts": []}}, {"KEY": {"hosts": ["a.io"], "value": "sk"}}])
def test_given_a_bad_secret_when_creating_a_profile_then_rejects_it(secrets: dict[str, Any]) -> None:
    """A value field is refused too: a profile must never carry the secret itself."""
    with pytest.raises(ValidationError):
        AgentProfile(name="leaky", secrets=secrets)


def test_given_a_relative_guest_path_when_creating_a_mount_then_rejects_it() -> None:
    with pytest.raises(ValidationError):
        WorkspaceMount(host_path=Path(), guest_path="workspace")


def test_given_a_relative_workdir_when_creating_a_profile_then_rejects_it() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        AgentProfile(name="lost", workdir="work")


@pytest.mark.parametrize("guest_path", ["/var/lib/docker", "/var/lib/docker/"])
def test_given_a_workspace_and_disk_at_the_same_guest_path_when_creating_a_profile_then_rejects_them(tmp_path: Path, guest_path: str) -> None:
    with pytest.raises(ValidationError, match="different guest paths"):
        AgentProfile(
            name="collision",
            disks={"/var/lib/docker": 20_480},
            workspace=WorkspaceMount(host_path=tmp_path, guest_path=guest_path),
        )


def alias(path: Path) -> object:
    """A second name for a folder above home, which only some filesystems have."""
    return pytest.param(path, marks=pytest.mark.skipif(not path.exists(), reason=f"no {path} on this filesystem"))


@pytest.mark.parametrize(
    "host_path",
    [
        Path("~"),
        Path("/"),
        alias(Path(str(Path.home().parent).swapcase())),  # case-insensitive disks
        alias(Path("/System/Volumes/Data")),  # macOS firmlink to /Users
    ],
)
def test_given_a_folder_containing_home_when_creating_a_mount_then_rejects_it(host_path: Path) -> None:
    with pytest.raises(ValidationError, match="contains your home folder"):
        WorkspaceMount(host_path=host_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_given_a_folder_we_may_not_open_when_creating_a_mount_then_accepts_it(tmp_path: Path) -> None:
    """Checking for home below it must not crash with PermissionError; the boot reports it."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)

    try:
        assert WorkspaceMount(host_path=locked).host_path == locked
    finally:
        locked.chmod(0o755)


def test_given_a_name_in_both_env_and_secrets_when_creating_a_profile_then_rejects_it() -> None:
    """The real value in env would sit next to the placeholder, visible to the guest."""
    with pytest.raises(ValidationError, match="KEY in both env and secrets"):
        AgentProfile(name="x", env={"KEY": "sk-real"}, secrets={"KEY": HostSecret(hosts=("a.example",))})


@pytest.mark.parametrize("mode", [NetworkMode.OFFLINE, NetworkMode.PUBLIC, NetworkMode.CORPORATE, NetworkMode.UNRESTRICTED])
def test_given_allow_private_dns_outside_allowlist_when_creating_a_profile_then_rejects_it(mode: NetworkMode) -> None:
    """It would do nothing there, so a profile that sets it is a misunderstanding."""
    with pytest.raises(ValidationError, match="allow_private_dns only applies to network = 'allowlist'"):
        AgentProfile(name="x", network=mode, allow_private_dns=True)


def test_given_allow_private_dns_with_allowlist_when_creating_a_profile_then_accepts_it() -> None:
    assert AgentProfile(name="x", allowed_domains=(".corp.com",), allow_private_dns=True).allow_private_dns is True


# --------------------------------------------------------------------------- host paths


def test_given_a_tilde_host_path_when_resolving_then_expands_home() -> None:
    mount = WorkspaceMount(host_path=Path("~/projects/demo"))

    assert mount.resolved_host_path == Path.home() / "projects/demo"


def test_given_a_relative_host_path_when_resolving_then_makes_it_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert WorkspaceMount(host_path=Path()).resolved_host_path == tmp_path.resolve()


# --------------------------------------------------------------------------- directories


def test_given_the_default_workdir_when_listing_directories_then_returns_only_workdir(profile: AgentProfile) -> None:
    assert profile.required_directories() == ["/workspace"]


def test_given_a_nested_workdir_when_listing_directories_then_returns_every_ancestor(profile: AgentProfile) -> None:
    deep = profile.model_copy(update={"workdir": "/srv/agent/work"})

    assert deep.required_directories() == ["/srv", "/srv/agent", "/srv/agent/work"]


def test_given_a_workspace_mount_when_listing_directories_then_includes_the_mount_point(tmp_path: Path, profile: AgentProfile) -> None:
    mounted = profile.model_copy(
        update={
            "workdir": "/workspace",
            "workspace": WorkspaceMount(host_path=tmp_path, guest_path="/mnt/repo"),
        }
    )

    assert mounted.required_directories() == ["/workspace", "/mnt", "/mnt/repo"]


def test_given_a_shared_ancestor_when_listing_directories_then_lists_it_once(tmp_path: Path, profile: AgentProfile) -> None:
    mounted = profile.model_copy(
        update={
            "workdir": "/srv/work",
            "workspace": WorkspaceMount(host_path=tmp_path, guest_path="/srv/repo"),
        }
    )

    assert mounted.required_directories() == ["/srv", "/srv/work", "/srv/repo"]
