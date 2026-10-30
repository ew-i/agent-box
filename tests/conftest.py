from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentbox.config import AgentProfile, NetworkMode
from agentbox.profiles import CONFIG_ENV_VAR, XDG_CONFIG_ENV_VAR
from agentbox.session import AgentSession
from fakes import FakeSandbox, FakeSandboxFactory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    StartSession = Callable[..., Awaitable[tuple[AgentSession, FakeSandbox]]]


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point profile lookup at an empty directory for every test.

    Without this a developer's real ``~/.config/agentbox/profiles.toml`` leaks into the
    suite, where a user profile named ``python`` would silently break assertions about
    the built-ins.
    """
    config_home = tmp_path / "xdg-config"
    config_home.mkdir()
    monkeypatch.setenv(XDG_CONFIG_ENV_VAR, str(config_home))
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    return config_home


@pytest.fixture
def profile() -> AgentProfile:
    return AgentProfile(
        name="test",
        image="python:3.11-slim",
        network=NetworkMode.ALLOWLIST,
        allowed_domains=(".pypi.org",),
    )


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> FakeSandboxFactory:
    """Replace ``microsandbox.Sandbox`` so starting a session never boots a microVM."""
    factory = FakeSandboxFactory()
    monkeypatch.setattr("microsandbox.Sandbox", factory)
    return factory


@pytest.fixture
def start_session(profile: AgentProfile, runtime: FakeSandboxFactory) -> StartSession:
    """Start a session against the fake runtime and hand back both ends of it."""

    async def start(**profile_updates: object) -> tuple[AgentSession, FakeSandbox]:
        session = AgentSession(profile.model_copy(update=profile_updates), "test-session")
        await session.start()
        return session, runtime.started

    return start


def microsandbox_unavailable() -> str | None:
    """Return why a microVM cannot boot here, or ``None`` when the host is capable."""
    system = platform.system()
    if system == "Linux":
        kvm = Path("/dev/kvm")
        if not kvm.exists():
            return "no /dev/kvm: this host has no KVM virtualization"
        if not kvm.is_char_device():
            return "/dev/kvm is not a character device"
    elif system == "Darwin":
        if platform.machine() != "arm64":
            return "microsandbox on macOS requires Apple Silicon"
    elif system != "Windows":
        return f"unsupported platform {system!r}"

    if shutil.which("msb") is None:
        return "msb not on PATH: run via 'uv run pytest' so .venv/bin is resolvable"
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip ``integration`` tests instead of failing them on hosts without a runtime."""
    if "integration" not in item.keywords:
        return
    if reason := microsandbox_unavailable():
        pytest.skip(f"microsandbox runtime unavailable - {reason}")
