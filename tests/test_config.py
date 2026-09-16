"""The config file schema: sources, libraries, includes, and what gets refused."""

from pathlib import Path

import pytest

from mcp_school.config import (
    Config,
    ConfigError,
    EnvRef,
    FileSource,
    Include,
    load_config,
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_a_file_source_loads_with_conventional_includes(tmp_path):
    config = load_config(
        write(tmp_path, "sources:\n- name: kubed\n  url: file:///srv/prompts\n")
    )
    [source] = config.sources
    assert isinstance(source, FileSource)
    assert source.path == Path("/srv/prompts")
    assert source.include == Include()
    assert source.include.skills is None  # None means "use the conventions"


def test_a_source_joins_its_own_library_by_default(tmp_path):
    config = load_config(write(tmp_path, "sources:\n- name: kubed\n  url: file:///a\n"))
    assert config.sources[0].library_name == "kubed"
    assert config.library("kubed").name == "kubed"
    assert config.library("kubed").tags == []


def test_a_source_may_join_a_declared_library(tmp_path):
    text = (
        "libraries:\n- name: grafana\n  description: LGTM\n  tags: [observability]\n"
        "sources:\n- name: grafana-skills\n  library: grafana\n  url: file:///a\n  tags: [upstream]\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.sources[0].library_name == "grafana"
    assert config.library("grafana").description == "LGTM"
    assert config.library("grafana").tags == ["observability"]
    assert config.sources[0].tags == ["upstream"]


def test_an_unknown_key_is_refused_not_ignored(tmp_path):
    with pytest.raises(ConfigError, match="inculde"):
        load_config(
            write(tmp_path, "sources:\n- name: a\n  url: file:///a\n  inculde: {}\n")
        )


def test_an_unknown_scheme_names_the_schemes_that_exist(tmp_path):
    with pytest.raises(ConfigError, match="file://"):
        load_config(write(tmp_path, "sources:\n- name: a\n  url: ftp://a\n"))


def test_a_relative_file_url_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="absolute"):
        load_config(
            write(tmp_path, "sources:\n- name: a\n  url: file://relative/path\n")
        )


@pytest.mark.parametrize("name", ["Bad", "-a", "a-", "a" * 65, "a/b", ".."])
def test_a_bad_source_name_is_rejected_not_slugged(tmp_path, name):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"sources:\n- name: '{name}'\n  url: file:///a\n"))


def test_duplicate_source_names_are_refused(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n- name: a\n  url: file:///b\n"
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_duplicate_library_names_are_refused(tmp_path):
    text = "libraries:\n- name: a\n- name: a\nsources: []\n"
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_a_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_an_absolute_include_pattern_is_refused(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n  include:\n    files: ['/etc/**']\n"
    with pytest.raises(ConfigError, match="relative"):
        load_config(write(tmp_path, text))


def test_a_parent_relative_include_pattern_is_refused(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n  include:\n    skills: ['../**']\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_a_normal_include_pattern_still_loads(tmp_path):
    text = (
        "sources:\n- name: a\n  url: file:///a\n  include:\n    files: ['shared/**']\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.sources[0].include.files == ["shared/**"]


def test_a_refresh_interval_in_minutes_is_seconds(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n  refresh: 5m\n"
    config = load_config(write(tmp_path, text))
    assert config.sources[0].refresh_seconds == 300


def test_a_refresh_interval_in_hours_is_seconds(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n  refresh: 2h\n"
    config = load_config(write(tmp_path, text))
    assert config.sources[0].refresh_seconds == 7200


def test_no_refresh_interval_means_no_refresh(tmp_path):
    config = load_config(write(tmp_path, "sources:\n- name: a\n  url: file:///a\n"))
    assert config.sources[0].refresh is None
    assert config.sources[0].refresh_seconds is None


@pytest.mark.parametrize("refresh", ["0s", "5", "5d", "-5m", "5ms", ""])
def test_a_malformed_refresh_interval_is_a_config_error(tmp_path, refresh):
    text = f"sources:\n- name: a\n  url: file:///a\n  refresh: '{refresh}'\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_min_refresh_seconds_is_the_smallest_among_sources(tmp_path):
    text = (
        "sources:\n"
        "- name: a\n  url: file:///a\n  refresh: 5m\n"
        "- name: b\n  url: file:///b\n"
        "- name: c\n  url: file:///c\n  refresh: 30s\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.min_refresh_seconds == 30


def test_min_refresh_seconds_is_none_when_no_source_has_one(tmp_path):
    config = load_config(write(tmp_path, "sources:\n- name: a\n  url: file:///a\n"))
    assert config.min_refresh_seconds is None


def test_config_constructs_from_source_model_instances_not_only_dicts():
    config = Config(sources=[FileSource(name="a", url="file:///a")])
    assert config.sources[0].url == "file:///a"


def test_an_env_ref_resolves_to_a_secret(monkeypatch):
    monkeypatch.setenv("TOKEN", "hunter2")
    secret = EnvRef(env="TOKEN").resolve()
    assert secret.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(secret)


def test_an_unset_env_ref_is_a_config_error(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        EnvRef(env="NOPE").resolve()
