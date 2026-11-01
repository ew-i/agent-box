from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from microsandbox import SandboxAlreadyExistsError, SandboxStillRunningError

from agentbox.errors import SandboxOperationError, SandboxStartupError
from agentbox.session import (
    AgentSession,
    created_profile,
    destroy_sandbox,
    generate_name,
    launch_detached,
    list_sandboxes,
    open_session,
    stop_sandbox,
)
from fakes import ExecCall, FakeExecEvent, FakeHandle, FakeListing, FakeLookup, FakePage, FakeSandbox, FakeSandboxFactory

if TYPE_CHECKING:
    from agentbox.config import AgentProfile
    from conftest import StartSession


# --------------------------------------------------------------------------- lifecycle


def test_given_an_unstarted_session_when_accessing_the_sandbox_then_raises(profile: AgentProfile) -> None:
    with pytest.raises(SandboxStartupError, match="not started"):
        _ = AgentSession(profile, "unstarted").sandbox


async def test_given_a_profile_when_starting_then_creates_the_sandbox_from_it(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    session = await AgentSession(profile, "sess").start()

    assert runtime.names == ["sess"]
    assert runtime.kwargs[0]["image"] == profile.image
    assert session.sandbox is runtime.started


async def test_given_a_session_when_starting_then_stays_attached(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    await AgentSession(profile, "sess").start()

    assert "detached" not in runtime.kwargs[0]


async def test_given_a_failing_runtime_when_starting_then_raises_startup_error(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    runtime.error = RuntimeError("no kvm")

    with pytest.raises(SandboxStartupError, match="no kvm"):
        await AgentSession(profile, "sess").start()


async def test_given_a_setup_script_when_starting_then_runs_it(start_session: StartSession) -> None:
    _, sandbox = await start_session(setup_script="pip install ruff")

    assert sandbox.commands == ["pip install ruff"]


async def test_given_a_setup_script_when_starting_then_streams_all_its_output_to_stderr(
    start_session: StartSession, runtime: FakeSandboxFactory, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """`up` prints only the sandbox name on stdout, so setup output must not land there."""
    runtime.events = [FakeExecEvent("stdout", b"installing\n"), FakeExecEvent("stderr", b"warn\n"), FakeExecEvent("exited", code=0)]

    await start_session(setup_script="mise install")

    assert capsysbinary.readouterr() == (b"", b"installing\nwarn\n")


async def test_given_a_failing_setup_script_when_starting_then_raises_and_stops_the_sandbox(start_session: StartSession, runtime: FakeSandboxFactory) -> None:
    runtime.events = [FakeExecEvent("exited", code=1)]

    with pytest.raises(SandboxStartupError, match="setup script 'bad' exited with 1"):
        await start_session(setup_script="bad")

    assert runtime.started.stopped is True


async def test_given_a_started_session_when_stopping_twice_then_stays_stopped(start_session: StartSession) -> None:
    session, sandbox = await start_session()

    await session.stop()
    await session.stop()

    assert sandbox.stopped is True


async def test_given_a_dead_sandbox_when_stopping_then_warns_instead_of_raising(start_session: StartSession) -> None:
    """Teardown must not crash, but a microVM that may still be running must not go unmentioned."""
    session, sandbox = await start_session()

    async def boom() -> None:
        raise RuntimeError("already gone")

    sandbox.stop = boom  # type: ignore[method-assign]
    with pytest.warns(RuntimeWarning, match="agentbox down test-session"):
        await session.stop()

    with pytest.raises(SandboxStartupError, match="not started"):
        _ = session.sandbox


async def test_given_a_context_manager_when_exiting_then_stops_the_sandbox(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    async with AgentSession(profile, "sess") as session:
        assert session.sandbox is runtime.started

    assert runtime.started.stopped is True


async def test_given_no_name_when_opening_a_session_then_generates_one(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    async with open_session(profile):
        pass

    assert runtime.names[0].startswith(f"agentbox-{profile.name}-")


# --------------------------------------------------------------------------- detached


async def test_given_a_started_session_when_detaching_then_releases_without_stopping(start_session: StartSession) -> None:
    session, sandbox = await start_session()

    assert await session.detach() == "test-session"
    assert sandbox.detached is True
    assert sandbox.stopped is False


async def test_given_a_runtime_error_when_detaching_then_raises_operation_error(start_session: StartSession) -> None:
    session, sandbox = await start_session()

    async def boom() -> None:
        raise RuntimeError("channel closed")

    sandbox.detach = boom  # type: ignore[method-assign]

    with pytest.raises(SandboxOperationError, match="channel closed"):
        await session.detach()


async def test_given_a_detached_session_when_stopping_then_leaves_the_sandbox_running(start_session: StartSession) -> None:
    """Leaving the context manager must not kill a sandbox the caller detached."""
    session, sandbox = await start_session()

    await session.detach()
    await session.stop()

    assert sandbox.stopped is False


async def test_given_a_new_name_when_launching_detached_then_creates_and_detaches(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    restarted = await launch_detached(profile, "long-lived")

    assert restarted is False
    assert runtime.names == ["long-lived"]
    assert runtime.kwargs[0]["detached"] is True
    assert runtime.started.detached is True
    assert runtime.started.stopped is False


async def test_given_a_failing_detach_when_launching_detached_then_stops_the_sandbox_and_raises(
    profile: AgentProfile, runtime: FakeSandboxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`up` reports a failure, so it must not leave behind a running box nobody owns."""

    async def boom(self: object) -> None:
        raise RuntimeError("channel closed")

    monkeypatch.setattr(FakeSandbox, "detach", boom)

    with pytest.raises(SandboxOperationError, match="channel closed"):
        await launch_detached(profile, "dev")

    assert runtime.started.stopped is True


async def test_given_a_stopped_sandbox_of_that_name_when_launching_detached_then_starts_it_again_and_reruns_setup(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """`make stop` then `make start` must bring the box back, not fail with "already exists"."""
    runtime.error = SandboxAlreadyExistsError("sandbox 'dev' already exists")

    restarted = await launch_detached(profile.model_copy(update={"setup_script": "start-dockerd"}), "dev")

    assert restarted is True
    assert runtime.restarts == [("dev", True)]
    assert runtime.started.commands == ["start-dockerd"]
    assert runtime.started.detached is True


async def test_given_a_stopped_sandbox_with_other_settings_when_launching_detached_then_refuses_to_start_it(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """A restart keeps the old mount and network, so a new --readonly or -w must not be dropped."""
    runtime.error = SandboxAlreadyExistsError("sandbox 'dev' already exists")
    runtime.existing_labels = {"agentbox.profile": "test", "agentbox.network": "allowlist", "agentbox.workspace": "/old:/workspace:rw"}

    with pytest.raises(SandboxStartupError, match=r"another profile, workspace, home volume or network.*agentbox down dev"):
        await launch_detached(profile, "dev")

    assert runtime.restarts == []


async def test_given_an_existing_sandbox_when_starting_it_then_skips_create_and_reruns_setup(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """`agentbox start` after `msb stop`: boot the box as created, then start dockerd again."""
    restarted = await launch_detached(profile.model_copy(update={"setup_script": "start-dockerd"}), "dev", existing=True)

    assert restarted is True
    assert runtime.names == []
    assert runtime.restarts == [("dev", True)]
    assert runtime.started.commands == ["start-dockerd"]
    assert runtime.started.detached is True


async def test_given_an_existing_sandbox_and_a_failing_setup_when_starting_it_then_stops_it(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    runtime.events = [FakeExecEvent("exited", code=1)]

    with pytest.raises(SandboxStartupError, match="exited with 1"):
        await launch_detached(profile.model_copy(update={"setup_script": "start-dockerd"}), "dev", existing=True)

    assert runtime.started.stopped is True


async def test_given_a_running_sandbox_when_starting_it_as_existing_then_raises_startup_error(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    runtime.restart_error = SandboxStillRunningError("already running")

    with pytest.raises(SandboxStartupError, match="already running"):
        await launch_detached(profile, "dev", existing=True)


async def test_given_an_agentbox_sandbox_when_reading_its_profile_then_returns_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(FakeHandle("dev", "stopped", {"agentbox.profile": "dev"})))

    assert await created_profile("dev") == "dev"


async def test_given_a_sandbox_without_a_profile_label_when_reading_its_profile_then_points_to_msb_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(FakeHandle("other", "stopped", {})))

    with pytest.raises(SandboxStartupError, match="msb start other"):
        await created_profile("other")


async def test_given_unreadable_labels_when_reading_its_profile_then_reports_the_real_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime failure must not pass for a box `agentbox up` didn't make."""
    handle = FakeHandle("dev", "stopped", {"agentbox.profile": "dev"})
    handle.config_error = RuntimeError("runtime down")
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    with pytest.raises(SandboxOperationError, match="could not read sandbox 'dev': runtime down"):
        await created_profile("dev")


async def test_given_an_unknown_sandbox_when_reading_its_profile_then_raises_operation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(None, error=RuntimeError("not found")))

    with pytest.raises(SandboxOperationError, match="not found"):
        await created_profile("ghost")


async def test_given_a_running_sandbox_of_that_name_when_starting_then_raises_startup_error(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    runtime.error = SandboxAlreadyExistsError("exists")
    runtime.restart_error = SandboxStillRunningError("already running")

    with pytest.raises(SandboxStartupError, match="already running"):
        await AgentSession(profile, "dev").start(detached=True)


def test_given_a_profile_when_generating_names_then_returns_unique_prefixed_names(profile: AgentProfile) -> None:
    first, second = generate_name(profile), generate_name(profile)

    assert first.startswith(f"agentbox-{profile.name}-")
    assert first != second


# --------------------------------------------------------------------------- runtime queries


async def test_given_several_pages_when_listing_sandboxes_then_follows_every_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        FakePage([FakeHandle("a", "running", {"agentbox.profile": "python"})], "cursor-1"),
        FakePage([FakeHandle("b", "stopped", {})], None),
    ]
    listing = FakeListing(pages)
    monkeypatch.setattr("microsandbox.Sandbox", listing)

    infos = await list_sandboxes()

    assert [(i.name, i.status, i.profile) for i in infos] == [
        ("a", "running", "python"),
        ("b", "stopped", None),
    ]
    assert listing.cursors == [None, "cursor-1"]


async def test_given_an_unreadable_config_when_listing_sandboxes_then_reports_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "running", {})
    handle.config_error = RuntimeError("cloud backend")
    monkeypatch.setattr("microsandbox.Sandbox", FakeListing([FakePage([handle], None)]))

    assert (await list_sandboxes())[0].profile is None


async def test_given_a_failing_runtime_when_listing_sandboxes_then_raises_operation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("microsandbox.Sandbox", FakeListing([], error=RuntimeError("no runtime")))

    with pytest.raises(SandboxOperationError, match="no runtime"):
        await list_sandboxes()


async def test_given_no_force_flag_when_stopping_by_name_then_stops_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "running", {})
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    await stop_sandbox("a")

    assert handle.stopped_with == "stop"
    assert handle.destroyed is None


async def test_given_the_force_flag_when_stopping_by_name_then_kills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "running", {})
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    await stop_sandbox("a", force=True)

    assert handle.stopped_with == "kill"


async def test_given_a_failing_stop_when_stopping_by_name_then_raises_operation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "stopped", {})
    handle.stop_error = RuntimeError("not running")
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    with pytest.raises(SandboxOperationError, match="could not stop sandbox 'a': not running"):
        await stop_sandbox("a")


async def test_given_no_force_flag_when_destroying_then_destroys_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "running", {})
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    await destroy_sandbox("a")

    assert handle.destroyed == {"force": False}


async def test_given_the_force_flag_when_destroying_then_destroys_forcefully(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = FakeHandle("a", "running", {})
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(handle))

    await destroy_sandbox("a", force=True)

    assert handle.destroyed == {"force": True}


async def test_given_a_failing_runtime_when_destroying_then_raises_operation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("microsandbox.Sandbox", FakeLookup(None, error=RuntimeError("gone")))

    with pytest.raises(SandboxOperationError, match="gone"):
        await destroy_sandbox("missing")


# --------------------------------------------------------------------------- commands


async def test_given_a_streamed_command_when_running_in_shell_then_writes_output_as_it_arrives(start_session: StartSession) -> None:
    session, sandbox = await start_session()
    sandbox.events = [
        FakeExecEvent("started"),
        FakeExecEvent("stdout", b"out\n"),
        FakeExecEvent("stderr", b"err\n"),
        FakeExecEvent("exited", code=3),
    ]
    out, err = io.BytesIO(), io.BytesIO()

    assert await session.shell_stream("make", stdout=out, stderr=err, timeout=5) == 3
    assert (out.getvalue(), err.getvalue()) == (b"out\n", b"err\n")
    assert sandbox.calls == [ExecCall("make", session.profile.workdir, None, 5)]


async def test_given_a_stream_without_an_exit_event_when_running_in_shell_then_raises(start_session: StartSession) -> None:
    """A spawn failure ends the stream without an exit code; it must not pass as exit 0."""
    session, sandbox = await start_session()
    sandbox.events = [FakeExecEvent("failed")]

    with pytest.raises(SandboxOperationError, match="without an exit code"):
        await session.shell_stream("nope", stdout=io.BytesIO(), stderr=io.BytesIO())


async def test_given_a_runtime_error_when_streaming_then_raises_operation_error(start_session: StartSession) -> None:
    session, sandbox = await start_session()

    async def timed_out(*_: object, **__: object) -> None:
        raise RuntimeError("exec timed out")

    sandbox.shell_stream = timed_out  # type: ignore[method-assign]

    with pytest.raises(SandboxOperationError, match="exec timed out"):
        await session.shell_stream("sleep 10", stdout=io.BytesIO(), stderr=io.BytesIO())


# --------------------------------------------------------------------------- integration


@pytest.mark.integration
async def test_given_a_real_runtime_when_streaming_then_splits_output_and_returns_the_exit_code(profile: AgentProfile) -> None:
    """Pins the SDK's event stream: the unit tests only see a fake of it."""
    out, err = io.BytesIO(), io.BytesIO()
    async with open_session(profile) as session:
        code = await session.shell_stream("echo out; echo err >&2; exit 3", stdout=out, stderr=err)

    assert (code, out.getvalue(), err.getvalue()) == (3, b"out\n", b"err\n")
