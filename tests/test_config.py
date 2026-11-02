from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentbox.config import AgentProfile, HostSecret, NetworkMode, WorkspaceMount, first_problem

# --------------------------------------------------------------------------- error lines


@pytest.mark.parametrize(
    ("fields", "prefix", "line"),
    [
        ({"cpus": 0}, ("profiles", "x"), "profiles.x.cpus: Input should be greater than or equal to 1"),
        ({"network": "public", "allow_private_dns": True}, (), "allow_private_dns only applies to network = 'allowlist', not 'public'"),
        ({"network": "public", "allow_private_dns": True}, ("profiles", "x"), "profiles.x: allow_private_dns only applies"),
    ],
)
def test_given_a_validation_error_when_rendering_then_gives_where_and_what_without_pydantic_noise(fields: dict[str, Any], prefix: tuple[str, ...], line: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AgentProfile(name="x", **fields)

    assert first_problem(excinfo.value, *prefix).startswith(line)


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


def test_given_env_paths_when_listing_per_box_files_then_keeps_only_named_ones_in_a_named_volume() -> None:
    """A shared file, or one in the user's workspace, is never a `run` box's to delete."""
    profile = AgentProfile(
        name="p",
        named_volumes={"/home/a": "home"},
        env={
            "OWN": "/home/a/db-{sandbox}",
            "SHARED": "/home/a/db",
            "WORKSPACE": "/workspace/out-{sandbox}",
            "ESCAPING": "/home/a/../workspace/out-{sandbox}",
            "NOT_A_PATH": "box-{sandbox}",
        },
    )

    assert profile.per_box_files("b1") == ["/home/a/db-b1"]


def test_given_a_name_that_climbs_out_when_listing_per_box_files_then_leaves_the_path_out() -> None:
    """The check runs on the filled-in path: the name itself must not escape the volume."""
    profile = AgentProfile(name="p", named_volumes={"/home/a": "home"}, env={"OWN": "/home/a/db-{sandbox}"})

    assert profile.per_box_files("x/../../../workspace/notes") == []


# --------------------------------------------------------------------------- validation


def test_given_an_unknown_field_when_creating_a_profile_then_rejects_it() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(name="typo", memroy_mib=512)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", ["", "a/b", "my box", ".hidden", "-rf", "x" * 65])
def test_given_an_unsafe_name_when_creating_a_profile_then_rejects_it(name: str) -> None:
    """The profile name goes into every generated sandbox name."""
    with pytest.raises(ValidationError):
        AgentProfile(name=name)


def test_given_an_offline_profile_when_reading_its_secrets_then_none_apply() -> None:
    secrets = {"KEY": HostSecret(hosts=("openrouter.ai",))}
    offline = AgentProfile(name="x", network=NetworkMode.OFFLINE, secrets=secrets)
    public = AgentProfile(name="x", network=NetworkMode.PUBLIC, secrets=secrets)

    assert (offline.active_secrets, offline.secret_hosts) == ({}, [])
    assert (public.active_secrets, public.secret_hosts) == (secrets, ["openrouter.ai"])


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
def test_given_a_folder_containing_home_when_checking_a_mount_then_rejects_it(host_path: Path) -> None:
    with pytest.raises(ValueError, match="contains your home folder"):
        WorkspaceMount(host_path=host_path).check_not_containing_home()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_given_a_folder_we_may_not_open_when_creating_a_mount_then_accepts_it(tmp_path: Path) -> None:
    """Checking for home below it must not crash with PermissionError; the boot reports it."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)

    try:
        assert WorkspaceMount(host_path=locked).check_not_containing_home().host_path == locked
    finally:
        locked.chmod(0o755)


def test_given_a_name_in_both_env_and_secrets_when_creating_a_profile_then_rejects_it() -> None:
    """The real value in env would sit next to the placeholder, visible to the guest."""
    with pytest.raises(ValidationError, match="KEY in both env and secrets"):
        AgentProfile(name="x", env={"KEY": "sk-real"}, secrets={"KEY": HostSecret(hosts=("a.example",))})


def test_given_a_secret_host_outside_allowed_domains_when_creating_a_profile_then_rejects_it() -> None:
    """The allowlist would block every request to it, so the secret could never be used."""
    with pytest.raises(ValidationError, match=r"secret hosts openrouter\.ai are not in allowed_domains"):
        AgentProfile(name="x", allowed_domains=(".pypi.org",), secrets={"KEY": HostSecret(hosts=("api.pypi.org", "openrouter.ai"))})


@pytest.mark.parametrize("pattern", ["openrouter.ai", "*.openrouter.ai", "*.ai", "OpenRouter.AI"])
def test_given_a_secret_host_in_tls_bypass_when_creating_a_profile_then_rejects_it(pattern: str) -> None:
    """Bypassed traffic is never decrypted, so the secret could never be used."""
    with pytest.raises(ValidationError, match=r"secret hosts openrouter\.ai are in tls_bypass"):
        AgentProfile(name="x", network=NetworkMode.PUBLIC, tls_bypass=(pattern,), secrets={"KEY": HostSecret(hosts=("openrouter.ai",))})


def test_given_an_offline_profile_with_unreachable_secrets_when_creating_it_then_accepts_it() -> None:
    """Offline skips secrets, so there is nothing for them to reach."""
    profile = AgentProfile(name="x", network=NetworkMode.OFFLINE, tls_bypass=("openrouter.ai",), secrets={"KEY": HostSecret(hosts=("openrouter.ai",))})

    assert profile.secrets


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


def test_given_a_wildcard_in_allowed_domains_when_creating_a_profile_then_rejects_it() -> None:
    """The network rule knows exact names and dot-suffixes only, so a glob would block the host."""
    with pytest.raises(ValidationError, match=r"use 'x' or '.x'"):
        AgentProfile(name="x", allowed_domains=("*.openrouter.ai",))
