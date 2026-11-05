from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentbox.boxes import created_profile, destroy_sandbox, list_sandboxes, stop_sandbox
from agentbox.errors import NotAgentboxSandboxError, SandboxOperationError
from fakes import FakeExecEvent, FakeHandle, FakePage, FakeSandbox, FakeSandboxFactory

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentbox.config import AgentProfile

_MADE_FROM_P = {"agentbox.profile": "p"}
_CLEANUP = {"named_volumes": {"/h": "home"}, "env": {"DB": "/h/db-{sandbox}"}}


async def test_given_an_agentbox_sandbox_when_reading_its_profile_then_returns_the_label(runtime: FakeSandboxFactory) -> None:
    runtime.add(FakeHandle("dev", "stopped", {"agentbox.profile": "dev"}))

    assert await created_profile("dev") == "dev"


async def test_given_a_sandbox_without_a_profile_label_when_reading_its_profile_then_refuses_it(runtime: FakeSandboxFactory) -> None:
    runtime.add(FakeHandle("other", "stopped", {}))

    with pytest.raises(NotAgentboxSandboxError, match="'other' has no agentbox profile label") as caught:
        await created_profile("other")

    assert caught.value.name == "other"


async def test_given_unreadable_labels_when_reading_its_profile_then_reports_the_real_error(runtime: FakeSandboxFactory) -> None:
    """A runtime failure must not pass for a box `agentbox up` didn't make."""
    handle = FakeHandle("dev", "stopped", {"agentbox.profile": "dev"})
    handle.config_error = RuntimeError("runtime down")
    runtime.add(handle)

    with pytest.raises(SandboxOperationError, match="could not read sandbox 'dev': runtime down"):
        await created_profile("dev")


async def test_given_an_unknown_sandbox_when_reading_its_profile_then_raises_operation_error(runtime: FakeSandboxFactory) -> None:
    with pytest.raises(SandboxOperationError, match="not found"):
        await created_profile("ghost")


async def test_given_several_pages_when_listing_sandboxes_then_follows_every_cursor(runtime: FakeSandboxFactory) -> None:
    pages = [
        FakePage([FakeHandle("a", "running", {"agentbox.profile": "python"})], "cursor-1"),
        FakePage([FakeHandle("b", "stopped", {})], None),
    ]
    runtime.pages = pages

    infos = await list_sandboxes()

    assert [(i.name, i.status, i.profile) for i in infos] == [
        ("a", "running", "python"),
        ("b", "stopped", None),
    ]
    assert runtime.cursors == [None, "cursor-1"]


async def test_given_an_unreadable_config_when_listing_sandboxes_then_reports_no_profile(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    handle.config_error = RuntimeError("cloud backend")
    runtime.pages = [FakePage([handle], None)]

    assert (await list_sandboxes())[0].profile is None


async def test_given_a_failing_runtime_when_listing_sandboxes_then_raises_operation_error(runtime: FakeSandboxFactory) -> None:
    runtime.list_error = RuntimeError("no runtime")

    with pytest.raises(SandboxOperationError, match="no runtime"):
        await list_sandboxes()


def _loads(profile: AgentProfile) -> Callable[[str], AgentProfile]:
    """A profile loader, as the CLI passes to stop_sandbox and destroy_sandbox."""

    def load(name: str) -> AgentProfile:
        assert name == "p", "the loader gets the profile name from the box's label"
        return profile

    return load


async def test_given_no_force_flag_when_stopping_by_name_then_stops_gracefully(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    runtime.add(handle)

    await stop_sandbox("a")

    assert handle.stopped_with == "stop"
    assert handle.destroyed is None


async def test_given_the_force_flag_when_stopping_by_name_then_kills_it(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    runtime.add(handle)

    await stop_sandbox("a", force=True)

    assert handle.stopped_with == "kill"


async def test_given_a_failing_stop_when_stopping_by_name_then_raises_operation_error(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "stopped", {})
    handle.stop_error = RuntimeError("not running")
    runtime.add(handle)

    with pytest.raises(SandboxOperationError, match="could not stop sandbox 'a': not running"):
        await stop_sandbox("a")


async def test_given_a_running_run_box_when_stopping_by_name_then_removes_it_with_its_own_file(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """Stopping deletes an ephemeral box anyway; its files must go while it can still run rm."""
    handle = FakeHandle("a", "running", {"agentbox.ephemeral": "true", "agentbox.profile": "p"})
    runtime.add(handle)

    deleted, left = await stop_sandbox("a", force=True, profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert handle.connected is not None
    assert handle.connected.commands == ["rm -f -- /h/db-a /h/db-a-wal /h/db-a-shm /h/db-a-journal"]
    assert (deleted, left, handle.destroyed, handle.stopped_with) == (True, [], {"force": True}, None)


async def test_given_an_up_box_when_stopping_by_name_then_keeps_it_without_loading_its_profile(runtime: FakeSandboxFactory) -> None:
    """Only a `run` box needs its profile; loading it for an `up` box can only fail needlessly."""
    runtime.add(FakeHandle("a", "running", {}))

    def never(name: str) -> AgentProfile:
        raise AssertionError("profile loaded for a box that keeps its state")

    assert await stop_sandbox("a", profile=never) == (False, [])


def _deleted_on_stop(runtime: FakeSandboxFactory, handle: FakeHandle) -> FakeHandle:
    """An ephemeral box as the runtime treats it: stopping removes it."""
    runtime.add(handle)

    async def stop() -> None:
        del runtime.handles[handle.name]

    handle.stop = stop  # type: ignore[method-assign]
    return handle


async def test_given_an_unlabelled_run_box_when_stopping_by_name_then_reports_it_deleted_with_what_stays(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """A `run` box made before the ephemeral label looks like an `up` box until stopping deletes it."""
    _deleted_on_stop(runtime, FakeHandle("a", "running", {"agentbox.profile": "p"}))

    deleted, left = await stop_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert (deleted, left) == (True, ["/h/db-a"])


async def test_given_an_unlabelled_box_not_made_by_agentbox_when_stopping_deletes_it_then_reports_it_deleted(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    _deleted_on_stop(runtime, FakeHandle("a", "running", {}))

    assert await stop_sandbox("a", profile=_loads(profile)) == (True, [])


async def test_given_a_failing_lookup_after_stopping_when_stopping_by_name_then_reports_it_stopped(runtime: FakeSandboxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    handle = runtime.add(FakeHandle("a", "running", {}))

    async def stop() -> None:
        async def down(name: str) -> FakeHandle:
            raise RuntimeError("runtime busy")

        monkeypatch.setattr(runtime, "get", down)

    handle.stop = stop  # type: ignore[method-assign]

    assert await stop_sandbox("a") == (False, [])


async def test_given_unreadable_labels_when_stopping_by_name_then_raises_without_stopping(runtime: FakeSandboxFactory) -> None:
    """It may be a `run` box: stopping it like an `up` box would delete it and leave its files."""
    handle = FakeHandle("a", "running", {"agentbox.ephemeral": "true"})
    handle.config_error = RuntimeError("runtime down")
    runtime.add(handle)

    with pytest.raises(SandboxOperationError, match="could not read sandbox 'a': runtime down"):
        await stop_sandbox("a")

    assert (handle.stopped_with, handle.destroyed) == (None, None)


async def test_given_unreadable_labels_when_destroying_with_a_loader_then_raises_without_destroying(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    """Its own files can't be found without the profile label, so nothing is silently left."""
    handle = FakeHandle("a", "running", _MADE_FROM_P)
    handle.config_error = RuntimeError("runtime down")
    runtime.add(handle)

    with pytest.raises(SandboxOperationError, match="could not read sandbox 'a': runtime down"):
        await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert handle.destroyed is None


async def test_given_a_failing_destroy_when_destroying_then_raises_operation_error(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    handle.destroy_error = RuntimeError("busy")
    runtime.add(handle)

    with pytest.raises(SandboxOperationError, match="could not remove sandbox 'a': busy"):
        await destroy_sandbox("a")


async def test_given_no_force_flag_when_destroying_then_destroys_gracefully(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    runtime.add(handle)

    await destroy_sandbox("a")

    assert handle.destroyed == {"force": False}


async def test_given_the_force_flag_when_destroying_then_destroys_forcefully(runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", {})
    runtime.add(handle)

    await destroy_sandbox("a", force=True)

    assert handle.destroyed == {"force": True}


async def test_given_a_running_box_with_its_own_file_when_destroying_then_deletes_it_first(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", _MADE_FROM_P)
    runtime.add(handle)

    left = await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert handle.connected is not None
    assert handle.connected.commands == ["rm -f -- /h/db-a /h/db-a-wal /h/db-a-shm /h/db-a-journal"]
    assert (left, handle.destroyed) == ([], {"force": False})


async def test_given_keep_files_when_destroying_then_leaves_its_own_files(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", _MADE_FROM_P)
    runtime.add(handle)

    left = await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)), keep_files=True)

    assert handle.connected is None
    assert (left, handle.destroyed) == ([], {"force": False})


async def test_given_a_stopped_box_with_its_own_file_when_destroying_then_returns_what_it_left(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "stopped", _MADE_FROM_P)
    runtime.add(handle)

    left = await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert handle.connected is None
    assert (left, handle.destroyed) == (["/h/db-a"], {"force": False})


async def test_given_a_failing_file_cleanup_when_destroying_then_still_destroys_and_returns_it(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", _MADE_FROM_P)

    async def boom() -> None:
        raise RuntimeError("guest gone")

    handle.connect = boom  # type: ignore[method-assign,assignment]
    runtime.add(handle)

    left = await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert (left, handle.destroyed) == (["/h/db-a"], {"force": False})


async def test_given_a_failing_rm_when_destroying_then_still_destroys_and_returns_the_files(profile: AgentProfile, runtime: FakeSandboxFactory) -> None:
    handle = FakeHandle("a", "running", _MADE_FROM_P)
    failing = FakeSandbox([FakeExecEvent("exited", code=1)])

    async def connect() -> FakeSandbox:
        return failing

    handle.connect = connect  # type: ignore[method-assign]
    runtime.add(handle)

    left = await destroy_sandbox("a", profile=_loads(profile.model_copy(update=_CLEANUP)))

    assert failing.commands == ["rm -f -- /h/db-a /h/db-a-wal /h/db-a-shm /h/db-a-journal"]
    assert (left, handle.destroyed) == (["/h/db-a"], {"force": False})


async def test_given_a_box_not_made_by_agentbox_when_destroying_then_loads_no_profile(runtime: FakeSandboxFactory) -> None:
    """No profile label, so nothing of its own to clean up, and no profile to look up."""
    handle = FakeHandle("a", "running", {})
    runtime.add(handle)

    def never(name: str) -> AgentProfile:
        raise AssertionError("profile loaded for a box without a profile label")

    assert await destroy_sandbox("a", profile=never) == []
    assert handle.destroyed == {"force": False}


async def test_given_a_failing_runtime_when_destroying_then_raises_operation_error(runtime: FakeSandboxFactory) -> None:
    with pytest.raises(SandboxOperationError, match="not found"):
        await destroy_sandbox("missing")
