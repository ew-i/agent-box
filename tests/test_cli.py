from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import click
import pytest
from typer.testing import CliRunner

from agentbox import __version__
from agentbox.cli import main
from agentbox.errors import SandboxOperationError, SandboxStartupError
from agentbox.session import SandboxInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

runner = CliRunner()


@dataclass
class Recorder:
    """Captures what the CLI asked the session layer to do."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Exception | None = None
    profiles: list[Any] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[Recorder]:
    rec = Recorder()

    class FakeSession:
        async def shell_stream(self, script: str, *, stdout: BinaryIO, stderr: BinaryIO, timeout: float | None = None) -> int:
            rec.scripts.append(script)
            rec.timeouts.append(timeout)
            stdout.write(rec.stdout.encode())
            stderr.write(rec.stderr.encode())
            return rec.exit_code

    @contextlib.asynccontextmanager
    async def fake_open_session(profile: Any) -> AsyncIterator[Any]:
        rec.profiles.append(profile)
        if rec.error is not None:
            raise rec.error
        yield FakeSession()

    monkeypatch.setattr("agentbox.cli.open_session", fake_open_session)
    yield rec


def test_given_no_args_when_invoking_then_shows_help() -> None:
    result = runner.invoke(main, [])

    assert result.exit_code != 0
    assert "Usage" in result.output


def test_given_the_version_command_when_invoking_then_prints_the_version() -> None:
    result = runner.invoke(main, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_given_no_config_when_listing_profiles_then_shows_the_builtins() -> None:
    result = runner.invoke(main, ["profiles"])

    assert result.exit_code == 0
    assert "airgapped" in result.output
    assert "net=offline" in result.output


def test_given_a_user_config_when_listing_profiles_then_includes_it(tmp_path: Path) -> None:
    config = tmp_path / "profiles.toml"
    config.write_text('[profiles.rust]\nimage = "rust:1-slim"\n', encoding="utf-8")

    result = runner.invoke(main, ["profiles", "--config", str(config)])

    assert result.exit_code == 0
    assert "rust:1-slim" in result.output


def test_given_a_profile_when_showing_it_then_prints_json() -> None:
    result = runner.invoke(main, ["show", "airgapped"])

    assert result.exit_code == 0
    assert '"name": "airgapped"' in result.output


def shown(*args: str) -> dict[str, Any]:
    result = runner.invoke(main, ["show", *args])

    assert result.exit_code == 0
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def test_given_zero_limits_when_showing_then_prints_them_as_null() -> None:
    """``0`` and ``null`` both mean "no limit", so only one of them is ever stored."""
    profile = shown("airgapped", "--idle-timeout", "0", "--max-duration", "0")

    assert profile["idle_timeout_s"] is None
    assert profile["max_duration_s"] is None


def test_given_real_limits_when_showing_then_prints_them_verbatim() -> None:
    profile = shown("airgapped", "--idle-timeout", "30", "--max-duration", "90")

    assert profile["idle_timeout_s"] == 30
    assert profile["max_duration_s"] == 90


@pytest.mark.parametrize("flag", ["--idle-timeout", "--max-duration"])
def test_given_a_negative_limit_when_showing_then_rejects_it(flag: str) -> None:
    """``model_copy`` skips the profile's ``ge=0`` check, so the CLI must enforce it."""
    result = runner.invoke(main, ["show", "airgapped", flag, "-5"])

    assert result.exit_code == 2


def test_given_env_when_showing_then_masks_its_values(tmp_path: Path) -> None:
    """Profiles hold API keys in ``env``; printing them would leak them to screen and logs."""
    config = tmp_path / "profiles.toml"
    config.write_text('[profiles.k]\nenv = { ANTHROPIC_API_KEY = "sk-secret" }\n', encoding="utf-8")

    result = runner.invoke(main, ["show", "k", "--config", str(config)])

    assert result.exit_code == 0
    assert "sk-secret" not in result.output
    assert json.loads(result.stdout)["env"] == {"ANTHROPIC_API_KEY": "***"}


def test_given_other_overrides_when_showing_then_reflects_them(tmp_path: Path) -> None:
    profile = shown("airgapped", "--network", "public", "--workspace", str(tmp_path), "--readonly")

    assert profile["network"] == "public"
    assert profile["workspace"]["host_path"] == str(tmp_path)
    assert profile["workspace"]["readonly"] is True


def test_given_no_target_when_showing_then_reflects_what_run_boots() -> None:
    """Without --for, the profile's own guard rails apply, exactly as `run` uses them."""
    profile = shown("airgapped")

    assert profile["ephemeral"] is True
    assert profile["idle_timeout_s"] == 600
    assert profile["max_duration_s"] == 3600


def test_given_the_up_target_when_showing_then_reflects_what_up_forces() -> None:
    """`up` keeps state and never auto-stops, so `show --for up` must say so."""
    profile = shown("airgapped", "--for", "up")

    assert profile["ephemeral"] is False
    assert profile["idle_timeout_s"] is None
    assert profile["max_duration_s"] is None


def test_given_the_up_target_and_a_limit_when_showing_then_keeps_the_limit() -> None:
    profile = shown("airgapped", "--for", "up", "--max-duration", "7200")

    assert profile["max_duration_s"] == 7200
    assert profile["idle_timeout_s"] is None


def test_given_the_same_flags_when_showing_for_up_then_matches_what_up_launches(launcher: list[tuple[Any, str]], tmp_path: Path) -> None:
    """The preview and the real thing must not drift apart."""
    flags = ["--network", "offline", "--workspace", str(tmp_path), "--max-duration", "60"]
    runner.invoke(main, ["up", "airgapped", *flags])

    assert json.loads(launcher[0][0].model_dump_json()) == shown("airgapped", "--for", "up", *flags)


def test_given_an_unknown_target_when_showing_then_fails() -> None:
    result = runner.invoke(main, ["show", "airgapped", "--for", "sideways"])

    assert result.exit_code != 0


def test_given_an_unknown_profile_when_showing_then_exits_with_2() -> None:
    result = runner.invoke(main, ["show", "nope"])

    assert result.exit_code == 2
    assert "unknown profile" in result.output


@pytest.mark.parametrize("command", [["profiles"], ["show", "airgapped"], ["run", "airgapped", "ls"]])
def test_given_a_broken_config_when_invoking_then_exits_without_a_traceback(tmp_path: Path, command: list[str]) -> None:
    """A hand-edited config with a typo must print a message, not a traceback."""
    config = tmp_path / "profiles.toml"
    config.write_text("[profiles.rust\n", encoding="utf-8")

    result = runner.invoke(main, [*command, "--config", str(config)])

    assert result.exit_code == 2
    assert "invalid profile config" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_given_a_command_when_running_then_streams_its_output_and_exit_code(recorder: Recorder) -> None:
    recorder.stdout = "hi\n"

    result = runner.invoke(main, ["run", "airgapped", "echo hi"])

    assert result.exit_code == 0
    assert recorder.scripts == ["echo hi"]
    assert "hi\n" in result.output


def test_given_a_failing_command_when_running_then_propagates_its_exit_code(recorder: Recorder) -> None:
    recorder.exit_code, recorder.stderr = 3, "boom\n"

    result = runner.invoke(main, ["run", "airgapped", "false"])

    assert result.exit_code == 3


def test_given_a_readonly_workspace_when_running_then_mounts_it_readonly(tmp_path: Path, recorder: Recorder) -> None:
    result = runner.invoke(main, ["run", "airgapped", "ls", "--workspace", str(tmp_path), "--readonly"])

    assert result.exit_code == 0
    workspace = recorder.profiles[0].workspace
    assert workspace.host_path == tmp_path
    assert workspace.readonly is True


def test_given_a_profile_workspace_when_running_readonly_then_mounts_it_readonly(tmp_path: Path, recorder: Recorder) -> None:
    """--readonly without --workspace must protect the profile's own mount, not be ignored."""
    config = tmp_path / "profiles.toml"
    config.write_text(f'[profiles.mine]\nworkspace = {{ host_path = "{tmp_path}" }}\n', encoding="utf-8")

    result = runner.invoke(main, ["run", "mine", "ls", "--readonly", "--config", str(config)])

    assert result.exit_code == 0
    workspace = recorder.profiles[0].workspace
    assert workspace.host_path == tmp_path
    assert workspace.readonly is True


def test_given_no_workspace_when_running_readonly_then_refuses(recorder: Recorder) -> None:
    result = runner.invoke(main, ["run", "airgapped", "ls", "--readonly"])

    assert result.exit_code == 2
    assert "nothing to mount read-only" in " ".join(click.unstyle(result.output).replace("│", " ").split())
    assert recorder.profiles == []


def test_given_the_home_folder_when_running_then_refuses_to_mount_it(recorder: Recorder) -> None:
    result = runner.invoke(main, ["run", "airgapped", "ls", "--workspace", str(Path.home())])

    assert result.exit_code == 2
    assert "contains your home folder" in " ".join(click.unstyle(result.output).replace("│", " ").split())
    assert recorder.profiles == []


def test_given_a_network_flag_when_running_then_overrides_the_network(recorder: Recorder) -> None:
    result = runner.invoke(main, ["run", "airgapped", "ls", "--network", "public"])

    assert result.exit_code == 0
    assert recorder.profiles[0].network == "public"


def test_given_a_timeout_when_running_then_forwards_it(recorder: Recorder) -> None:
    result = runner.invoke(main, ["run", "airgapped", "sleep 1", "--timeout", "2.5"])

    assert result.exit_code == 0
    assert recorder.timeouts == [2.5]


def test_given_a_startup_failure_when_running_then_reports_it(recorder: Recorder) -> None:
    recorder.error = SandboxStartupError("no kvm")

    result = runner.invoke(main, ["run", "airgapped", "ls"])

    assert result.exit_code == 2
    assert "no kvm" in result.output


def test_given_an_unknown_profile_when_running_then_exits_with_2() -> None:
    result = runner.invoke(main, ["run", "nope", "ls"])

    assert result.exit_code == 2
    assert "unknown profile" in result.output


# --------------------------------------------------------------------------- up / ls / down


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    launched: list[tuple[Any, str]] = []

    async def fake_launch(profile: Any, name: str) -> bool:
        launched.append((profile, name))
        return False

    monkeypatch.setattr("agentbox.cli.launch_detached", fake_launch)
    return launched


def test_given_a_name_when_bringing_up_then_prints_only_the_name_on_stdout(launcher: list[tuple[Any, str]]) -> None:
    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "mybox"
    assert launcher[0][1] == "mybox"


def test_given_no_venv_when_bringing_up_then_prints_hints_to_stderr(launcher: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert "msb exec mybox" in result.stderr
    assert "agentbox down mybox" in result.stderr
    assert "uv run" not in result.stderr
    assert "no time limit" in result.stderr
    assert "settings it was created with" not in result.stderr


def test_given_a_stopped_box_when_bringing_it_up_again_then_says_profile_changes_need_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def restart(profile: Any, name: str) -> bool:
        return True

    monkeypatch.setattr("agentbox.cli.launch_detached", restart)
    monkeypatch.delenv("UV", raising=False)

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert result.exit_code == 0
    assert "started the stopped mybox again" in result.stderr
    assert "secrets and time limits included" in result.stderr
    assert "agentbox down mybox, then up again" in result.stderr
    # The box keeps the limits it was created with, so this profile's limits aren't hinted.
    assert "no time limit" not in result.stderr


def test_given_no_limits_when_bringing_up_then_runs_until_stopped(launcher: list[tuple[Any, str]]) -> None:
    """A development box should outlive a coffee break, so nothing auto-stops it."""
    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    profile = launcher[0][0]
    assert profile.idle_timeout_s is None
    assert profile.max_duration_s is None
    assert "no time limit" in result.stderr
    assert "auto-stops" not in result.stderr


def test_given_no_limits_when_running_then_keeps_the_profile_guard_rails(recorder: Recorder) -> None:
    """`run` executes agent-generated code one-shot, so its backstop stays in place."""
    runner.invoke(main, ["run", "airgapped", "ls"])

    assert recorder.profiles[0].idle_timeout_s == 600
    assert recorder.profiles[0].max_duration_s == 3600


def test_given_both_limits_when_bringing_up_then_hints_both(launcher: list[tuple[Any, str]]) -> None:
    """Idle and hard limits stop a sandbox for different reasons, so both are shown."""
    result = runner.invoke(main, ["up", "airgapped", "--idle-timeout", "600", "--max-duration", "7200"])

    assert "auto-stops after 10m idle" in result.stderr
    assert "stops after 2h even if busy" in result.stderr
    assert "no time limit" not in result.stderr


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [(45, "45s"), (90, "1m30s"), (600, "10m"), (3600, "1h"), (5400, "1h30m"), (7200, "2h")],
)
def test_given_a_max_duration_when_bringing_up_then_spells_it_in_hours_and_minutes(launcher: list[tuple[Any, str]], seconds: int, shown: str) -> None:
    result = runner.invoke(main, ["up", "airgapped", "--max-duration", str(seconds)])

    assert f"stops after {shown} even if busy" in result.stderr


@pytest.mark.parametrize("flags", [[], ["--idle-timeout", "0", "--max-duration", "0"]])
def test_given_no_effective_limit_when_bringing_up_then_says_there_is_no_time_limit(launcher: list[tuple[Any, str]], flags: list[str]) -> None:
    """Omitting the flags and explicitly zeroing them mean the same thing."""
    result = runner.invoke(main, ["up", "airgapped", *flags])

    assert "no time limit" in result.stderr
    assert "stops after" not in result.stderr


def test_given_uv_when_bringing_up_then_prefixes_the_agentbox_hint_with_uv_run(launcher: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentbox` is a project script, reachable only through uv or an activated venv."""
    monkeypatch.setenv("UV", "/usr/local/bin/uv")

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert "uv run agentbox down mybox" in result.stderr


def test_given_a_system_wide_msb_when_bringing_up_then_hints_bare_msb(tmp_path: Path, launcher: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A global msb and the venv copy drive the same runtime, so prefer the plain name."""
    system_bin = tmp_path / "usr-bin"
    system_bin.mkdir()
    msb = system_bin / "msb"
    msb.write_text("#!/bin/sh\n")
    msb.chmod(0o755)
    venv = tmp_path / "venv"
    monkeypatch.setenv("UV", "/usr/local/bin/uv")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv / "bin"), str(system_bin)]))

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert "\nmsb exec mybox" in f"\n{result.stderr}"
    assert "uv run msb" not in result.stderr


def test_given_msb_in_a_sibling_of_the_venv_when_bringing_up_then_hints_bare_msb(tmp_path: Path, launcher: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """`venv2/bin` only shares a name prefix with `venv`; it is not inside it."""
    venv, sibling = tmp_path / "venv", tmp_path / "venv2" / "bin"
    sibling.mkdir(parents=True)
    (sibling / "msb").write_text("#!/bin/sh\n")
    (sibling / "msb").chmod(0o755)
    monkeypatch.setenv("UV", "/usr/local/bin/uv")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", str(sibling))

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert "uv run msb" not in result.stderr


def test_given_msb_only_in_the_venv_when_bringing_up_then_hints_uv_run_msb(tmp_path: Path, launcher: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    msb = venv / "bin" / "msb"
    msb.write_text("#!/bin/sh\n")
    msb.chmod(0o755)
    monkeypatch.setenv("UV", "/usr/local/bin/uv")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", str(venv / "bin"))

    result = runner.invoke(main, ["up", "airgapped", "--name", "mybox"])

    assert "uv run msb exec mybox" in result.stderr


def test_given_no_name_when_bringing_up_then_generates_one(launcher: list[tuple[Any, str]]) -> None:
    result = runner.invoke(main, ["up", "airgapped"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("agentbox-airgapped-")


def test_given_a_profile_when_bringing_up_then_keeps_its_state(launcher: list[tuple[Any, str]]) -> None:
    runner.invoke(main, ["up", "airgapped"])

    assert launcher[0][0].ephemeral is False


def test_given_overrides_when_bringing_up_then_applies_them(tmp_path: Path, launcher: list[tuple[Any, str]]) -> None:
    result = runner.invoke(
        main,
        [
            "up",
            "airgapped",
            "--workspace",
            str(tmp_path),
            "--readonly",
            "--network",
            "offline",
            "--idle-timeout",
            "0",
            "--max-duration",
            "60",
        ],
    )

    assert result.exit_code == 0
    profile = launcher[0][0]
    assert profile.workspace.host_path == tmp_path
    assert profile.workspace.readonly is True
    assert profile.network == "offline"
    assert profile.idle_timeout_s is None
    assert profile.max_duration_s == 60
    assert "auto-stops" not in result.stderr
    assert "stops after 1m even if busy" in result.stderr


def test_given_an_unknown_profile_when_bringing_up_then_exits_with_2() -> None:
    result = runner.invoke(main, ["up", "nope"])

    assert result.exit_code == 2


def test_given_a_startup_failure_when_bringing_up_then_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(profile: Any, name: str) -> str:
        raise SandboxStartupError("no kvm")

    monkeypatch.setattr("agentbox.cli.launch_detached", boom)
    result = runner.invoke(main, ["up", "airgapped"])

    assert result.exit_code == 2
    assert "no kvm" in result.stderr


def test_given_sandboxes_when_listing_then_renders_a_row_each(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list() -> list[SandboxInfo]:
        return [SandboxInfo("a", "running", "python"), SandboxInfo("b", "stopped", None)]

    monkeypatch.setattr("agentbox.cli.list_sandboxes", fake_list)
    result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    assert "a" in result.stdout
    assert "running" in result.stdout
    assert "-" in result.stdout


def test_given_no_sandboxes_when_listing_then_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list() -> list[SandboxInfo]:
        return []

    monkeypatch.setattr("agentbox.cli.list_sandboxes", fake_list)
    result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    assert "no sandboxes" in result.stderr


def test_given_a_runtime_failure_when_listing_then_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> list[SandboxInfo]:
        raise SandboxOperationError("runtime down")

    monkeypatch.setattr("agentbox.cli.list_sandboxes", boom)
    result = runner.invoke(main, ["ls"])

    assert result.exit_code == 2
    assert "runtime down" in result.stderr


def test_given_the_force_flag_when_tearing_down_then_removes_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[tuple[str, bool]] = []

    async def fake_destroy(name: str, *, force: bool = False) -> None:
        removed.append((name, force))

    monkeypatch.setattr("agentbox.cli.destroy_sandbox", fake_destroy)
    result = runner.invoke(main, ["down", "mybox", "--force"])

    assert result.exit_code == 0
    assert removed == [("mybox", True)]
    assert "removed mybox" in result.stderr


def test_given_a_missing_sandbox_when_tearing_down_then_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(name: str, *, force: bool = False) -> None:
        raise SandboxOperationError("no such sandbox")

    monkeypatch.setattr("agentbox.cli.destroy_sandbox", boom)
    result = runner.invoke(main, ["down", "ghost"])

    assert result.exit_code == 2
    assert "no such sandbox" in result.stderr
