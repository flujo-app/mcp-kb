from pathlib import Path

import pytest

from mcp_school.config import ConfigError, EnvRef, FileSource, Include, load_config


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


def test_an_env_ref_resolves_to_a_secret(monkeypatch):
    monkeypatch.setenv("TOKEN", "hunter2")
    secret = EnvRef(env="TOKEN").resolve()
    assert secret.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(secret)


def test_an_unset_env_ref_is_a_config_error(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        EnvRef(env="NOPE").resolve()
