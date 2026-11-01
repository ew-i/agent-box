from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentbox.errors import ProfileConfigError, ProfileNotFoundError
from agentbox.profiles import (
    BUILTIN_PROFILES,
    CONFIG_ENV_VAR,
    XDG_CONFIG_ENV_VAR,
    default_config_path,
    get_profile,
    load_profiles,
)

CUSTOM = """
[profiles.rust]
image = "rust:1-slim"
cpus = 8
network = "offline"
"""

MISSING_PATH = Path("/nonexistent/profiles.toml")


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return tmp_path / "profiles.toml"


# --------------------------------------------------------------------------- built-ins


def test_given_the_builtins_when_looking_up_airgapped_then_finds_it_without_domains() -> None:
    assert "airgapped" in BUILTIN_PROFILES
    assert BUILTIN_PROFILES["airgapped"].allowed_domains == ()


def test_given_the_builtins_when_comparing_names_then_matches_their_keys() -> None:
    assert all(name == profile.name for name, profile in BUILTIN_PROFILES.items())


def test_given_the_php_docker_builtin_when_looking_it_up_then_gives_dockerd_its_own_disk() -> None:
    """On the guest's overlayfs root ``docker run`` fails; the plain image has no dockerd at all."""
    docker, plain = BUILTIN_PROFILES["agent-box-php-docker"], BUILTIN_PROFILES["agent-box"]

    assert "/var/lib/docker" in docker.disks
    assert "agent-box-start-dockerd" in (docker.setup_script or "")
    assert docker.named_volumes == plain.named_volumes
    assert (plain.image, plain.disks) == ("agent-box", {})


def test_given_no_config_when_loading_profiles_then_returns_the_builtins() -> None:
    assert load_profiles() == BUILTIN_PROFILES


# --------------------------------------------------------------------------- XDG lookup


def test_given_xdg_config_home_when_resolving_the_config_path_then_uses_it(isolated_config_home: Path) -> None:
    config = isolated_config_home / "agentbox" / "profiles.toml"
    config.parent.mkdir(parents=True)
    config.write_text(CUSTOM, encoding="utf-8")

    assert default_config_path() == config
    assert "rust" in load_profiles()


def test_given_xdg_unset_when_resolving_the_config_path_then_falls_back_to_dot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(XDG_CONFIG_ENV_VAR, raising=False)

    assert default_config_path() == Path.home() / ".config" / "agentbox" / "profiles.toml"


@pytest.mark.parametrize("invalid", ["", "relative/config"])
def test_given_an_invalid_xdg_config_home_when_resolving_the_config_path_then_ignores_it(invalid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The XDG spec says a value that is not an absolute path must be treated as unset."""
    monkeypatch.setenv(XDG_CONFIG_ENV_VAR, invalid)

    assert default_config_path() == Path.home() / ".config" / "agentbox" / "profiles.toml"


def test_given_a_changed_environment_when_resolving_the_config_path_then_follows_it(tmp_path: Path) -> None:
    """Resolved per call, so it is not frozen to whatever $HOME was at import time."""
    before = default_config_path()
    os.environ[XDG_CONFIG_ENV_VAR] = str(tmp_path / "elsewhere")

    assert default_config_path() != before


def test_given_agentbox_config_and_xdg_when_loading_profiles_then_prefers_agentbox_config(isolated_config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ignored = isolated_config_home / "agentbox" / "profiles.toml"
    ignored.parent.mkdir(parents=True)
    ignored.write_text('[profiles.rust]\nimage = "ignored:1"\n', encoding="utf-8")
    preferred = tmp_path / "preferred.toml"
    preferred.write_text(CUSTOM, encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(preferred))

    assert load_profiles()["rust"].image == "rust:1-slim"


def test_given_a_mutated_result_when_loading_again_then_returns_pristine_builtins(config_file: Path) -> None:
    """Callers get a copy; mutating it must not poison the next lookup."""
    load_profiles()["airgapped"] = BUILTIN_PROFILES["agent-box"]

    assert load_profiles()["airgapped"].name == "airgapped"


def test_given_a_missing_explicit_path_when_loading_profiles_then_refuses_it() -> None:
    """A typo must not boot the built-in in place of the user's stricter override."""
    with pytest.raises(ProfileConfigError, match=r"/nonexistent/profiles\.toml: no such file"):
        load_profiles(MISSING_PATH)


def test_given_agentbox_config_naming_a_missing_file_when_loading_profiles_then_refuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(MISSING_PATH))

    with pytest.raises(ProfileConfigError, match="no such file"):
        load_profiles()


# --------------------------------------------------------------------------- user config


def test_given_a_user_profile_when_loading_profiles_then_adds_it_to_the_builtins(config_file: Path) -> None:
    config_file.write_text(CUSTOM, encoding="utf-8")

    profiles = load_profiles(config_file)

    assert profiles["rust"].cpus == 8
    assert "airgapped" in profiles


def test_given_a_user_profile_named_like_a_builtin_when_loading_then_overrides_it(config_file: Path) -> None:
    config_file.write_text('[profiles.airgapped]\nimage = "python:3.13-slim"\n', encoding="utf-8")

    assert load_profiles(config_file)["airgapped"].image == "python:3.13-slim"


def test_given_a_name_field_in_a_table_when_loading_then_the_table_key_wins(config_file: Path) -> None:
    """Lookup, listing, labels and sandbox names must all agree on one name."""
    config_file.write_text('[profiles.mine]\nname = "other"\n', encoding="utf-8")

    assert load_profiles(config_file)["mine"].name == "mine"


def test_given_a_config_without_profiles_when_loading_then_returns_the_builtins(config_file: Path) -> None:
    config_file.write_text("# nothing here\n", encoding="utf-8")

    assert load_profiles(config_file) == BUILTIN_PROFILES


def test_given_the_config_env_var_when_loading_profiles_then_reads_that_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "from-env.toml"
    config.write_text(CUSTOM, encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))

    assert "rust" in load_profiles()


def test_given_an_explicit_path_and_env_var_when_loading_then_prefers_the_path(config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file.write_text(CUSTOM, encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "ignored.toml"))

    assert "rust" in load_profiles(config_file)


# --------------------------------------------------------------------------- broken config


def test_given_malformed_toml_when_loading_profiles_then_raises_config_error(config_file: Path) -> None:
    """A typo in a hand-edited config must not surface as a raw traceback."""
    config_file.write_text("[profiles.rust\nimage = 1", encoding="utf-8")

    with pytest.raises(ProfileConfigError) as excinfo:
        load_profiles(config_file)

    assert str(config_file) in str(excinfo.value)


def test_given_invalid_utf8_when_loading_profiles_then_raises_config_error(config_file: Path) -> None:
    config_file.write_bytes(b"\xff")

    with pytest.raises(ProfileConfigError, match="utf-8"):
        load_profiles(config_file)


def test_given_an_invalid_field_value_when_loading_profiles_then_names_the_field(config_file: Path) -> None:
    config_file.write_text("[profiles.rust]\ncpus = 999\n", encoding="utf-8")

    with pytest.raises(ProfileConfigError, match="cpus"):
        load_profiles(config_file)


def test_given_several_profiles_when_one_is_invalid_then_names_that_profile(config_file: Path) -> None:
    """In a long config, "cpus: ..." alone leaves you hunting for the bad table."""
    config_file.write_text("[profiles.a]\ncpus = 2\n[profiles.b]\ncpus = 999\n", encoding="utf-8")

    with pytest.raises(ProfileConfigError, match=r"profiles\.b\.cpus: "):
        load_profiles(config_file)


def test_given_an_unknown_field_when_loading_profiles_then_rejects_it(config_file: Path) -> None:
    config_file.write_text("[profiles.rust]\nmemroy_mib = 512\n", encoding="utf-8")

    with pytest.raises(ProfileConfigError, match="memroy_mib"):
        load_profiles(config_file)


@pytest.mark.parametrize(
    ("toml", "field"),
    [
        ('[profiles.rust]\nworkspace = { host_path = "/tmp", read_only = true }\n', r"profiles\.rust\.workspace\.read_only"),
        ("[profile.rust]\ncpus = 2\n", r"profile: "),
    ],
)
def test_given_a_misspelled_key_when_loading_profiles_then_rejects_it(config_file: Path, toml: str, field: str) -> None:
    """A `read_only` typo would otherwise mount the workspace writable, and `[profile.x]` would vanish."""
    config_file.write_text(toml, encoding="utf-8")

    with pytest.raises(ProfileConfigError, match=field):
        load_profiles(config_file)


def test_given_a_non_table_profile_entry_when_loading_then_rejects_it(config_file: Path) -> None:
    config_file.write_text("[profiles]\nrust = 1\n", encoding="utf-8")

    with pytest.raises(ProfileConfigError, match="rust"):
        load_profiles(config_file)


def test_given_a_non_table_profiles_key_when_loading_then_rejects_it(config_file: Path) -> None:
    config_file.write_text("profiles = 1\n", encoding="utf-8")

    with pytest.raises(ProfileConfigError, match="profiles"):
        load_profiles(config_file)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_given_an_unreadable_config_when_loading_profiles_then_raises_config_error(config_file: Path) -> None:
    config_file.write_text(CUSTOM, encoding="utf-8")
    config_file.chmod(0o000)

    try:
        with pytest.raises(ProfileConfigError):
            load_profiles(config_file)
    finally:
        config_file.chmod(0o644)


# --------------------------------------------------------------------------- lookup


def test_given_an_unknown_name_when_getting_a_profile_then_lists_alternatives() -> None:
    with pytest.raises(ProfileNotFoundError) as excinfo:
        get_profile("does-not-exist")

    assert "airgapped" in str(excinfo.value)


def test_given_a_user_profile_when_getting_it_then_returns_the_user_version(config_file: Path) -> None:
    config_file.write_text(CUSTOM, encoding="utf-8")

    assert get_profile("rust", config_file).image == "rust:1-slim"


def test_given_the_package_when_importing_the_main_module_then_exposes_main() -> None:
    import agentbox.__main__

    assert agentbox.__main__.main is not None
