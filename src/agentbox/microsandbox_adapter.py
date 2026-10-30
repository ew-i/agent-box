"""Translate an :class:`~agentbox.config.AgentProfile` into microsandbox arguments.

This is the only module that knows the microsandbox object model. Confining the SDK here
keeps :mod:`agentbox.config` pure data, and keeps the import at the edge of the system where
it is cheap to replace. Import this module lazily: ``agentbox version``, ``agentbox profiles``
and ``agentbox show`` never need the SDK, and loading it costs real startup time.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from microsandbox import (
    Action,
    DestGroup,
    Destination,
    MountConfig,
    NamedVolumeMode,
    Network,
    NetworkDestination,
    NetworkPolicy,
    NetworkProfile,
    Patch,
    PatchConfig,
    Protocol,
    Rule,
    Secret,
    SecretSubstitution,
    TlsConfig,
    Volume,
    VolumeKind,
)
from microsandbox.types import DnsConfig

from agentbox.config import NetworkMode
from agentbox.errors import SandboxStartupError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentbox.config import AgentProfile, WorkspaceMount

HTTPS_PORT = 443
"""Allowlisted domains are reachable over TLS only; plaintext egress stays denied."""

DIRECTORY_MODE = 0o755
"""Owner-writable, world-readable — the usual mode for a workspace directory."""

_DIGEST_LENGTH = 12
"""Hex characters of the network-settings hash kept in a label; enough to tell edits apart."""

_PRIVATE_DNS = DnsConfig(rebind_protection=False)
"""Rebind protection drops DNS answers with private IPs, so names on a corporate network or
VPN would not resolve even where the policy lets the traffic through."""


def to_sandbox_kwargs(profile: AgentProfile) -> dict[str, Any]:
    """Render ``profile`` as ``microsandbox.Sandbox.create(**kwargs)`` arguments."""
    kwargs: dict[str, Any] = {
        "image": profile.image,
        "cpus": profile.cpus,
        "memory": profile.memory_mib,
        "workdir": profile.workdir,
        "shell": profile.shell,
        "env": dict(profile.env),
        "ephemeral": profile.ephemeral,
        "network": _build_network(profile),
        "patches": _build_patches(profile),
        "labels": _labels(profile),
    }
    if profile.max_duration_s:
        kwargs["max_duration"] = float(profile.max_duration_s)
    if profile.idle_timeout_s:
        kwargs["idle_timeout"] = float(profile.idle_timeout_s)
    volumes = {
        path: Volume.named(name, mode=NamedVolumeMode.ENSURE_EXISTS, nosuid=True, nodev=True)
        for path, name in profile.named_volumes.items()
    }
    volumes |= {
        path: Volume.owned(kind=VolumeKind.DISK, size_mib=size, nosuid=True, nodev=True)
        for path, size in profile.disks.items()
    }
    if profile.workspace is not None:
        volumes[profile.workspace.guest_path] = _build_mount(profile.workspace)
    if volumes:
        kwargs["volumes"] = volumes
    # Offline nothing leaves the box, so a secret has nowhere to go and its key isn't needed.
    if profile.secrets and profile.network is not NetworkMode.OFFLINE:
        kwargs["secrets"] = [
            Secret.env(
                name,
                value=_host_value(name),
                allow=secret.hosts,
                substitution=SecretSubstitution(header_fields=(secret.header,)),
            )
            for name, secret in profile.secrets.items()
        ]
    return kwargs


def _labels(profile: AgentProfile) -> dict[str, str]:
    """The profile name, plus the settings a restart must match: ``Sandbox.start`` keeps the
    mounts and network the sandbox was created with, whatever was asked for this time."""
    # named_volumes too: a separate home is how an untrusted project avoids the shared one.
    network = profile.model_dump(
        mode="json",
        include={
            "network",
            "allowed_domains",
            "allow_private_dns",
            "tls_bypass",
            "secrets",
            "named_volumes",
        },
    )
    digest = hashlib.sha256(json.dumps(network, sort_keys=True).encode()).hexdigest()
    labels = {
        "agentbox.profile": profile.name,
        "agentbox.network": f"{profile.network}:{digest[:_DIGEST_LENGTH]}",
    }
    if (mount := profile.workspace) is not None:
        access = "ro" if mount.readonly else "rw"
        labels["agentbox.workspace"] = f"{mount.resolved_host_path}:{mount.guest_path}:{access}"
    return labels


def _host_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SandboxStartupError(f"secret {name} is not set in your environment; export it first")
    return value


def _build_patches(profile: AgentProfile) -> list[PatchConfig]:
    """Pre-boot ``mkdir`` patches; ``Patch.mkdir`` is idempotent but not recursive."""
    return [Patch.mkdir(path, mode=DIRECTORY_MODE) for path in profile.required_directories()]


def _build_network(profile: AgentProfile) -> Network:
    if profile.network is NetworkMode.OFFLINE:
        # No egress, so secrets and tls_bypass have nothing to apply to.
        return Network.none()
    secret_hosts = [host for secret in profile.secrets.values() for host in secret.hosts]
    if bypassed := [h for h in secret_hosts if any(_covers(p, h) for p in profile.tls_bypass)]:
        # Bypassed traffic is never decrypted, so it can't get the real value.
        raise SandboxStartupError(
            f"secret hosts {', '.join(bypassed)} are in tls_bypass, so they would never get "
            "the real value; remove them there"
        )
    egress = _EGRESS[profile.network](profile, secret_hosts)
    # A secret turns on interception; configuring TLS without one would turn it on for nothing.
    tls = TlsConfig(bypass=(*egress.tls_bypass, *profile.tls_bypass)) if secret_hosts else None
    return Network(policy=egress.policy, strict=egress.strict, dns=egress.dns, tls=tls)


@dataclass(frozen=True)
class _Egress:
    """What sets one network mode apart; everything shared is built in :func:`_build_network`."""

    policy: NetworkPolicy
    dns: DnsConfig | None = None
    strict: bool = True
    tls_bypass: tuple[str, ...] = ()
    """Bypassed on top of the profile's ``tls_bypass``."""


def _unrestricted(_profile: AgentProfile, _secret_hosts: Sequence[str]) -> _Egress:
    return _Egress(NetworkPolicy.allow_all(), dns=_PRIVATE_DNS)


def _corporate(_profile: AgentProfile, _secret_hosts: Sequence[str]) -> _Egress:
    return _Egress(_deny_by_default(_corporate_rules()), dns=_PRIVATE_DNS)


def _public(_profile: AgentProfile, _secret_hosts: Sequence[str]) -> _Egress:
    return _Egress(NetworkPolicy.from_profiles([NetworkProfile.PUBLIC]))


def _allowlist(profile: AgentProfile, secret_hosts: Sequence[str]) -> _Egress:
    patterns = _domain_patterns(profile.allowed_domains)
    if blocked := [h for h in secret_hosts if not any(_covers(p, h) for p in patterns)]:
        raise SandboxStartupError(
            f"secret hosts {', '.join(blocked)} are not in allowed_domains, so every "
            "request to them would be blocked; add them there"
        )
    # strict=True only lets a domain rule match traffic microsandbox decrypts; without
    # that every allowed domain is blocked. Decrypting all of them breaks clients that
    # don't trust its CA, like containers inside Docker. Non-strict matching still ties
    # the TLS hostname to an IP the guest resolved through an allowed domain.
    return _Egress(
        _deny_by_default(_allowlist_rules(profile.allowed_domains)),
        dns=_PRIVATE_DNS if profile.allow_private_dns else None,
        strict=False,
        tls_bypass=_tls_bypass(patterns, secret_hosts),
    )


_EGRESS: dict[NetworkMode, Callable[[AgentProfile, Sequence[str]], _Egress]] = {
    NetworkMode.UNRESTRICTED: _unrestricted,
    NetworkMode.CORPORATE: _corporate,
    NetworkMode.PUBLIC: _public,
    NetworkMode.ALLOWLIST: _allowlist,
}
"""Every mode that has egress; ``offline`` has none, see :func:`_build_network`."""


def _deny_by_default(rules: tuple[Rule, ...]) -> NetworkPolicy:
    return NetworkPolicy(default_egress=Action.DENY, default_ingress=Action.DENY, rules=rules)


def _corporate_rules() -> tuple[Rule, ...]:
    """An explicit deny of cloud metadata, then DNS, then public and private networks.

    The metadata deny comes first because the first matching rule wins. The host is a
    separate group and is never allowed, so services listening on it stay out of reach.
    """
    return (
        Rule.deny(destination=Destination.group(DestGroup.METADATA)),
        *Rule.allow_dns(),
        Rule.allow(destination=Destination.group(DestGroup.PUBLIC)),
        Rule.allow(destination=Destination.group(DestGroup.PRIVATE)),
    )


def _allowlist_rules(allowed_domains: Sequence[str]) -> tuple[Rule, ...]:
    """An explicit deny of cloud metadata, then DNS, then HTTPS to each allowed domain.

    The first matching rule wins, so the metadata deny comes first: no allowlist entry that
    resolves to the metadata address can expose instance credentials. DNS is allowed because
    domain rules are useless without name resolution.
    """
    return (
        Rule.deny(destination=Destination.group(DestGroup.METADATA)),
        *Rule.allow_dns(),
        *(
            Rule.allow(protocol=Protocol.TCP, port=HTTPS_PORT, destination=_destination(domain))
            for domain in allowed_domains
        ),
    )


def _domain_patterns(allowed_domains: Sequence[str]) -> list[str]:
    """Allowlist patterns: ``.x`` becomes ``*.x``, which also covers ``x`` (see :func:`_covers`)."""
    return [f"*{domain}" if domain.startswith(".") else domain for domain in allowed_domains]


def _tls_bypass(patterns: Sequence[str], secret_hosts: Sequence[str]) -> tuple[str, ...]:
    """Allowed domains that skip TLS interception: all of them except the secret hosts.

    A secret switches interception on, and only intercepted requests get the real value.
    Everything else keeps its real certificate, so clients without microsandbox's CA work.
    """
    return tuple(p for p in patterns if not any(_covers(p, host) for host in secret_hosts))


def _covers(pattern: str, host: str) -> bool:
    """Whether microsandbox applies ``pattern`` to ``host``: ``*.x`` also covers ``x`` itself,
    unlike ``fnmatch`` (seen on a real microVM: ``*.openrouter.ai`` bypassed ``openrouter.ai``)."""
    host, pattern = host.lower(), pattern.lower()  # domain names ignore case, fnmatch doesn't
    return fnmatch(host, pattern) or (pattern.startswith("*.") and host == pattern[2:])


def _destination(domain: str) -> NetworkDestination:
    """A leading dot means "this domain and all of its subdomains"."""
    if domain.startswith("."):
        return Destination.domain_suffix(domain)
    return Destination.domain(domain)


def _build_mount(workspace: WorkspaceMount) -> MountConfig:
    """Bind-mount the host workspace, never trusting it to carry privileges."""
    return Volume.bind(
        str(workspace.resolved_host_path),
        readonly=workspace.readonly,
        nosuid=True,
        nodev=True,
    )
