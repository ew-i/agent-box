from __future__ import annotations

from pathlib import Path

import pytest
from microsandbox import Action, MountKind, Network, Protocol, Rule, VolumeKind

from agentbox.config import AgentProfile, HostSecret, NetworkMode, WorkspaceMount
from agentbox.errors import SandboxStartupError
from agentbox.microsandbox_adapter import _EGRESS, DIRECTORY_MODE, HTTPS_PORT, to_sandbox_kwargs
from agentbox.profiles import BUILTIN_PROFILES

DNS_PORT = 53
METADATA = "metadata"


def network_of(profile: AgentProfile) -> Network:
    network: Network = to_sandbox_kwargs(profile)["network"]
    return network


def destinations(rules: tuple[Rule, ...], action: Action) -> set[tuple[str, str]]:
    """``{(kind, value)}`` of every rule with ``action``, e.g. ``{("domain", "a.io")}``."""
    return {(rule.destination.kind.value, rule.destination.value) for rule in rules if rule.action is action}


# --------------------------------------------------------------------------- sandbox kwargs


def test_given_a_profile_when_translating_then_passes_resources_and_label_through(profile: AgentProfile) -> None:
    kwargs = to_sandbox_kwargs(profile)

    assert kwargs["image"] == "python:3.11-slim"
    assert kwargs["cpus"] == profile.cpus
    assert kwargs["memory"] == profile.memory_mib
    assert kwargs["workdir"] == profile.workdir
    assert kwargs["labels"]["agentbox.profile"] == "test"
    assert kwargs["labels"]["agentbox.network"].startswith("allowlist:")


def test_given_edited_network_settings_when_translating_then_changes_the_label_a_restart_must_match(profile: AgentProfile) -> None:
    """A restart keeps the old network, so tightening the allowlist must not pass as the same box."""
    tightened = profile.model_copy(update={"allowed_domains": ()})

    assert _network_label(profile) == _network_label(profile.model_copy(update={"cpus": 8}))
    assert _network_label(profile) != _network_label(tightened)


def test_given_an_edited_home_volume_when_translating_then_changes_the_label_a_restart_must_match(profile: AgentProfile) -> None:
    """A restart keeps the old volumes, so moving an untrusted project off the shared home must not pass as the same box."""
    shared = profile.model_copy(update={"named_volumes": {"/home/agent-box": "agent-box-home"}})
    own = profile.model_copy(update={"named_volumes": {"/home/agent-box": "agent-box-home-untrusted"}})

    assert _network_label(shared) != _network_label(own)


def _network_label(profile: AgentProfile) -> str:
    label: str = to_sandbox_kwargs(profile)["labels"]["agentbox.network"]
    return label


def test_given_a_workspace_when_translating_then_labels_the_mount_a_restart_must_match(tmp_path: Path, profile: AgentProfile) -> None:
    mounted = profile.model_copy(update={"workspace": WorkspaceMount(host_path=tmp_path, readonly=True)})

    assert to_sandbox_kwargs(mounted)["labels"]["agentbox.workspace"] == f"{tmp_path.resolve()}:/workspace:ro"


def test_given_lifetime_limits_when_translating_then_sends_them_as_seconds(profile: AgentProfile) -> None:
    kwargs = to_sandbox_kwargs(profile)

    assert kwargs["max_duration"] == float(profile.max_duration_s or 0)
    assert kwargs["idle_timeout"] == float(profile.idle_timeout_s or 0)


@pytest.mark.parametrize("unbounded", [None, 0])
def test_given_disabled_limits_when_translating_then_omits_them(profile: AgentProfile, unbounded: int | None) -> None:
    """``None`` means "unset" and ``0`` means "disabled"; both mean "send no limit"."""
    forever = profile.model_copy(update={"max_duration_s": unbounded, "idle_timeout_s": unbounded})

    kwargs = to_sandbox_kwargs(forever)

    assert "max_duration" not in kwargs
    assert "idle_timeout" not in kwargs


def test_given_env_when_mutating_the_translation_then_leaves_the_profile_intact(profile: AgentProfile) -> None:
    with_env = profile.model_copy(update={"env": {"TOKEN": "secret"}})

    to_sandbox_kwargs(with_env)["env"]["TOKEN"] = "tampered"

    assert with_env.env == {"TOKEN": "secret"}


# --------------------------------------------------------------------------- workspace


def test_given_no_workspace_when_translating_then_mounts_no_volumes(profile: AgentProfile) -> None:
    assert "volumes" not in to_sandbox_kwargs(profile)


def test_given_a_readonly_workspace_when_translating_then_binds_it_at_the_guest_path(tmp_path: Path, profile: AgentProfile) -> None:
    mounted = profile.model_copy(update={"workspace": WorkspaceMount(host_path=tmp_path, readonly=True)})

    volumes = to_sandbox_kwargs(mounted)["volumes"]

    assert set(volumes) == {"/workspace"}
    assert volumes["/workspace"].bind == str(tmp_path.resolve())
    assert volumes["/workspace"].readonly is True


def test_given_a_workspace_when_translating_then_mounts_it_nosuid_and_nodev(tmp_path: Path, profile: AgentProfile) -> None:
    """A setuid binary on the host must not become a privilege escalation in the guest."""
    mounted = profile.model_copy(update={"workspace": WorkspaceMount(host_path=tmp_path)})

    mount = to_sandbox_kwargs(mounted)["volumes"]["/workspace"]

    assert mount.nosuid is True
    assert mount.nodev is True


def test_given_a_disk_when_translating_then_mounts_a_disk_owned_by_the_sandbox(profile: AgentProfile) -> None:
    """Owned, not named: a named disk can't attach to two sandboxes, so a second box would fail."""
    mount = to_sandbox_kwargs(profile.model_copy(update={"disks": {"/var/lib/docker": 2048}}))["volumes"]["/var/lib/docker"]

    assert (mount.kind, mount.owned_kind, mount.size_mib) == (MountKind.OWNED, VolumeKind.DISK, 2048)
    assert mount.nosuid is True
    assert mount.nodev is True


def test_given_named_volumes_and_a_workspace_when_translating_then_mounts_both(tmp_path: Path, profile: AgentProfile) -> None:
    mounted = profile.model_copy(update={"named_volumes": {"/home/agent": "agent-home"}, "workspace": WorkspaceMount(host_path=tmp_path)})

    volumes = to_sandbox_kwargs(mounted)["volumes"]

    assert set(volumes) == {"/home/agent", "/workspace"}
    assert volumes["/home/agent"].named == "agent-home"
    assert volumes["/home/agent"].nosuid is True


# --------------------------------------------------------------------------- patches


def test_given_a_nested_workdir_when_translating_then_patches_in_every_directory(profile: AgentProfile) -> None:
    deep = profile.model_copy(update={"workdir": "/srv/agent/work"})

    patches = to_sandbox_kwargs(deep)["patches"]

    assert [patch.path for patch in patches] == deep.required_directories()
    assert {patch.mode for patch in patches} == {DIRECTORY_MODE}


# --------------------------------------------------------------------------- network


def test_given_a_secret_offline_when_translating_then_neither_needs_nor_passes_the_key(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing leaves an offline box, so `run dev -n offline` must not demand the API key."""
    monkeypatch.delenv("KEY", raising=False)
    offline = profile.model_copy(update={"network": NetworkMode.OFFLINE, "tls_bypass": ("openrouter.ai",), "secrets": {"KEY": HostSecret(hosts=("openrouter.ai",))}})

    kwargs = to_sandbox_kwargs(offline)

    assert "secrets" not in kwargs
    assert kwargs["network"].tls is None


def test_given_offline_when_translating_then_allows_no_traffic(profile: AgentProfile) -> None:
    policy = network_of(profile.model_copy(update={"network": NetworkMode.OFFLINE})).policy

    assert policy.default_egress is Action.DENY
    assert policy.rules == ()


def test_given_unrestricted_when_translating_then_allows_all_traffic(profile: AgentProfile) -> None:
    policy = network_of(profile.model_copy(update={"network": NetworkMode.UNRESTRICTED})).policy

    assert policy.default_egress is Action.ALLOW


def test_given_unrestricted_when_translating_then_resolves_names_to_private_ips(profile: AgentProfile) -> None:
    """Rebind protection would drop the answer for a host on a corporate network or VPN."""
    network = network_of(profile.model_copy(update={"network": NetworkMode.UNRESTRICTED}))

    assert network.dns.rebind_protection is False


def test_given_corporate_when_translating_then_allows_public_and_private_networks(profile: AgentProfile) -> None:
    network = network_of(profile.model_copy(update={"network": NetworkMode.CORPORATE}))

    assert network.policy.default_egress is Action.DENY
    assert {("group", "public"), ("group", "private")} <= destinations(network.policy.rules, Action.ALLOW)
    assert network.dns.rebind_protection is False


def test_given_corporate_when_translating_then_keeps_the_host_and_metadata_out_of_reach(profile: AgentProfile) -> None:
    """The first matching rule wins, so the metadata deny must come before the private allow.
    The host is only reachable for DNS, through the gateway on port 53."""
    rules = network_of(profile.model_copy(update={"network": NetworkMode.CORPORATE})).policy.rules

    assert rules[0].action is Action.DENY
    assert (rules[0].destination.kind.value, rules[0].destination.value) == ("group", METADATA)
    assert {rule.port for rule in rules if rule.destination.value == "host"} == {DNS_PORT}


@pytest.mark.parametrize("mode", [NetworkMode.ALLOWLIST, NetworkMode.PUBLIC])
def test_given_a_restricted_mode_when_translating_then_keeps_rebind_protection(profile: AgentProfile, mode: NetworkMode) -> None:
    network = network_of(profile.model_copy(update={"network": mode}))

    assert network.dns is None or network.dns.rebind_protection is True


def test_given_allow_private_dns_when_translating_then_lets_allowed_domains_resolve_to_private_ips(profile: AgentProfile) -> None:
    """Intranet hosts behind a VPN resolve to private IPs, which rebind protection would drop."""
    network = network_of(profile.model_copy(update={"allowed_domains": (".corp.com",), "allow_private_dns": True}))

    assert network.dns.rebind_protection is False
    assert network.policy.default_egress is Action.DENY


def test_given_allow_private_dns_when_labelling_then_a_restart_notices_the_change(profile: AgentProfile) -> None:
    private = profile.model_copy(update={"allow_private_dns": True})

    assert to_sandbox_kwargs(private)["labels"]["agentbox.network"] != to_sandbox_kwargs(profile)["labels"]["agentbox.network"]


def test_given_allowlist_when_translating_then_matches_domains_without_decrypting_traffic(profile: AgentProfile) -> None:
    """Strict matching without interception blocks every allowed domain, checked on a real microVM."""
    network = network_of(profile)

    assert network.strict is False
    assert network.tls is None


def test_given_a_secret_when_translating_then_sends_the_host_value_only_to_its_hosts(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real")
    secret = HostSecret(hosts=("openrouter.ai",))
    with_secret = profile.model_copy(update={"allowed_domains": ("openrouter.ai",), "secrets": {"OPENROUTER_API_KEY": secret}})

    (entry,) = to_sandbox_kwargs(with_secret)["secrets"]

    assert (entry.env_var, entry.value, entry.allow) == ("OPENROUTER_API_KEY", "sk-real", ("openrouter.ai",))
    assert entry.substitution.header_fields == ("authorization",)


def test_given_a_secret_missing_from_the_host_when_translating_then_refuses_to_boot(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with_secret = profile.model_copy(update={"allowed_domains": ("openrouter.ai",), "secrets": {"OPENROUTER_API_KEY": HostSecret(hosts=("openrouter.ai",))}})

    with pytest.raises(SandboxStartupError, match="OPENROUTER_API_KEY is not set"):
        to_sandbox_kwargs(with_secret)


def test_given_a_secret_when_translating_then_decrypts_only_traffic_to_its_hosts(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything else keeps its real certificate, so containers inside Docker can still verify it."""
    monkeypatch.setenv("KEY", "v")
    with_secret = profile.model_copy(
        update={
            "allowed_domains": (".pypi.org", "openrouter.ai", ".openrouter.ai"),
            "secrets": {"KEY": HostSecret(hosts=("openrouter.ai",))},
        }
    )

    # *.openrouter.ai would bypass openrouter.ai too, and the key would never be swapped in
    assert network_of(with_secret).tls.bypass == ("*.pypi.org",)


def test_given_a_secret_host_outside_allowed_domains_when_translating_then_refuses_to_boot(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist would block every request to it, so the secret could never be used."""
    monkeypatch.setenv("KEY", "v")
    with_secret = profile.model_copy(update={"secrets": {"KEY": HostSecret(hosts=("api.pypi.org", "openrouter.ai"))}})

    with pytest.raises(SandboxStartupError, match=r"secret hosts openrouter\.ai are not in allowed_domains"):
        to_sandbox_kwargs(with_secret)


@pytest.mark.parametrize("mode", [NetworkMode.PUBLIC, NetworkMode.CORPORATE, NetworkMode.UNRESTRICTED])
def test_given_a_secret_outside_allowlist_mode_when_translating_then_bypasses_only_tls_bypass(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch, mode: NetworkMode) -> None:
    """No allowlist to derive exemptions from, so every site not in tls_bypass is decrypted."""
    monkeypatch.setenv("KEY", "v")
    with_secret = profile.model_copy(update={"network": mode, "tls_bypass": ("*.docker.io",), "secrets": {"KEY": HostSecret(hosts=("openrouter.ai",))}})

    assert network_of(with_secret).tls.bypass == ("*.docker.io",)


def test_given_an_allowlist_with_tls_bypass_when_translating_then_adds_it_to_the_derived_bypass(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEY", "v")
    with_secret = profile.model_copy(update={"allowed_domains": ("openrouter.ai",), "tls_bypass": ("pinned.example",), "secrets": {"KEY": HostSecret(hosts=("openrouter.ai",))}})

    assert network_of(with_secret).tls.bypass == ("pinned.example",)


@pytest.mark.parametrize("pattern", ["openrouter.ai", "*.openrouter.ai", "*.ai", "OpenRouter.AI"])
def test_given_a_secret_host_in_tls_bypass_when_translating_then_refuses_to_boot(profile: AgentProfile, monkeypatch: pytest.MonkeyPatch, pattern: str) -> None:
    """Bypassed traffic is never decrypted, so the secret could never be used."""
    monkeypatch.setenv("KEY", "v")
    with_secret = profile.model_copy(update={"network": NetworkMode.PUBLIC, "tls_bypass": (pattern,), "secrets": {"KEY": HostSecret(hosts=("openrouter.ai",))}})

    with pytest.raises(SandboxStartupError, match=r"secret hosts openrouter\.ai are in tls_bypass"):
        to_sandbox_kwargs(with_secret)


def test_given_tls_bypass_without_a_secret_when_translating_then_leaves_interception_off(profile: AgentProfile) -> None:
    network = network_of(profile.model_copy(update={"network": NetworkMode.PUBLIC, "tls_bypass": ("*.docker.io",)}))

    assert network.tls is None


def test_given_public_when_translating_then_allows_only_the_public_internet(profile: AgentProfile) -> None:
    policy = network_of(profile.model_copy(update={"network": NetworkMode.PUBLIC})).policy

    assert policy.default_egress is Action.DENY
    assert ("group", "public") in destinations(policy.rules, Action.ALLOW)


def test_given_allowlist_when_translating_then_denies_by_default(profile: AgentProfile) -> None:
    policy = network_of(profile).policy

    assert policy.default_egress is Action.DENY
    assert policy.default_ingress is Action.DENY


def test_given_allowlist_when_translating_then_permits_dns_or_domain_rules_would_be_useless(profile: AgentProfile) -> None:
    ports = {rule.port for rule in network_of(profile).policy.rules}

    assert DNS_PORT in ports


def test_given_exact_and_suffix_domains_when_translating_then_distinguishes_them(profile: AgentProfile) -> None:
    """A leading dot means "and all subdomains"; without it the match must be exact."""
    allowlisted = profile.model_copy(update={"allowed_domains": (".pypi.org", "api.openai.com")})

    allowed = destinations(network_of(allowlisted).policy.rules, Action.ALLOW)

    assert ("domain_suffix", ".pypi.org") in allowed
    assert ("domain", "api.openai.com") in allowed


def test_given_an_allowlisted_domain_when_translating_then_permits_only_https(profile: AgentProfile) -> None:
    domain_rules = [rule for rule in network_of(profile).policy.rules if rule.destination.value == ".pypi.org"]

    assert [(rule.protocol, rule.port) for rule in domain_rules] == [(Protocol.TCP, HTTPS_PORT)]


def test_given_no_allowed_domains_when_translating_then_still_denies_cloud_metadata(profile: AgentProfile) -> None:
    """Instance metadata would hand an agent the host's cloud credentials."""
    no_domains = profile.model_copy(update={"allowed_domains": ()})

    assert ("group", METADATA) in destinations(network_of(no_domains).policy.rules, Action.DENY)


def test_given_allowed_domains_when_translating_then_denies_cloud_metadata_before_any_allow(profile: AgentProfile) -> None:
    """The first matching rule wins, so a deny after the allows would never fire for them."""
    first = network_of(profile).policy.rules[0]

    assert (first.action, first.destination.kind.value, first.destination.value) == (Action.DENY, "group", METADATA)


# --------------------------------------------------------------------------- built-ins


@pytest.mark.parametrize("builtin", list(BUILTIN_PROFILES.values()), ids=lambda builtin: builtin.name)
def test_given_a_builtin_profile_when_translating_then_allowlists_its_own_domains(builtin: AgentProfile) -> None:
    allowed = destinations(network_of(builtin).policy.rules, Action.ALLOW)

    assert {value for _, value in allowed} >= set(builtin.allowed_domains)


def test_given_the_airgapped_profile_when_translating_then_has_no_rules() -> None:
    assert network_of(BUILTIN_PROFILES["airgapped"]).policy.rules == ()


def test_every_mode_with_egress_has_a_policy() -> None:
    """A new NetworkMode without an entry would fail at boot, not here."""
    assert set(_EGRESS) == set(NetworkMode) - {NetworkMode.OFFLINE}
