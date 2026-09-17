"""The config file schema: sources, libraries, includes, and what gets refused."""

from pathlib import Path

import pytest

from kubed.mcp_kb.config import (
    BasicAuth,
    Config,
    ConfigError,
    EnvRef,
    FileSource,
    GitSource,
    Include,
    WebdavSource,
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


def test_a_github_source_produces_the_clone_url(tmp_path):
    config = load_config(
        write(tmp_path, "sources:\n- name: skills\n  url: github://grafana/skills\n")
    )
    [source] = config.sources
    assert isinstance(source, GitSource)
    assert source.clone_url == "https://github.com/grafana/skills.git"


def test_a_github_url_already_ending_in_git_is_not_double_suffixed(tmp_path):
    config = load_config(
        write(
            tmp_path, "sources:\n- name: skills\n  url: github://grafana/skills.git\n"
        )
    )
    assert config.sources[0].clone_url == "https://github.com/grafana/skills.git"


def test_a_github_url_missing_the_repo_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="github://org/repo"):
        load_config(
            write(tmp_path, "sources:\n- name: a\n  url: github://only-org\n")
        )


def test_a_git_plus_https_source_with_a_ref_parses(tmp_path):
    config = load_config(
        write(
            tmp_path,
            "sources:\n- name: a\n  url: git+https://x/y.git\n  ref: v1\n",
        )
    )
    [source] = config.sources
    assert isinstance(source, GitSource)
    assert source.ref == "v1"
    assert source.clone_url == "https://x/y.git"


def test_git_plus_http_and_git_plus_file_clone_urls_drop_only_the_prefix(tmp_path):
    text = (
        "sources:\n"
        "- name: a\n  url: git+http://x/y.git\n"
        "- name: b\n  url: git+file:///srv/repo.git\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.sources[0].clone_url == "http://x/y.git"
    assert config.sources[1].clone_url == "file:///srv/repo.git"


def test_git_auth_password_must_be_an_env_ref_not_a_literal(tmp_path):
    text = (
        "sources:\n- name: a\n  url: github://o/r\n"
        "  auth:\n    username: x-access-token\n    password: hunter2\n"
    )
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_git_auth_with_an_env_ref_password_parses(tmp_path):
    text = (
        "sources:\n- name: a\n  url: github://o/r\n"
        "  auth:\n    username: x-access-token\n    password:\n      env: GITHUB_TOKEN\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.sources[0].auth.username == "x-access-token"
    assert config.sources[0].auth.password == EnvRef(env="GITHUB_TOKEN")


def test_refresh_is_still_accepted_on_a_git_source(tmp_path):
    config = load_config(
        write(tmp_path, "sources:\n- name: a\n  url: github://o/r\n  refresh: 5m\n")
    )
    assert config.sources[0].refresh_seconds == 300


def test_a_file_source_with_a_ref_is_refused_extra_forbidden(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            write(tmp_path, "sources:\n- name: a\n  url: file:///a\n  ref: v1\n")
        )


def test_a_git_subdirectory_escaping_the_export_is_refused(tmp_path):
    text = "sources:\n- name: a\n  url: github://o/r\n  subdirectory: ../x\n"
    with pytest.raises(ConfigError, match="relative"):
        load_config(write(tmp_path, text))


def test_a_git_subdirectory_that_stays_inside_parses(tmp_path):
    text = "sources:\n- name: a\n  url: github://o/r\n  subdirectory: docs/skills\n"
    config = load_config(write(tmp_path, text))
    assert config.sources[0].subdirectory == "docs/skills"


def test_min_refresh_seconds_considers_git_sources_too(tmp_path):
    text = (
        "sources:\n"
        "- name: a\n  url: file:///a\n  refresh: 5m\n"
        "- name: b\n  url: github://o/r\n  refresh: 10s\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.min_refresh_seconds == 10


def test_a_credential_embedded_in_a_url_is_refused(tmp_path):
    """`auth` is the one way a credential reaches a remote, and this is why.

    pygit2 saves the clone's remote URL verbatim under `<cache>/git/<name>/config`,
    and `source.url` is interpolated into the errors `/health` publishes — so a
    token in the URL is a token on disk and in a served body. Refused at the
    door instead, and the refusal itself must not repeat it back.
    """
    text = "sources:\n- name: p\n  url: git+http://x-access-token:ghp-secret@h/x.git\n"

    with pytest.raises(ConfigError, match="credential") as raised:
        load_config(write(tmp_path, text))

    assert "ghp-secret" not in str(raised.value)
    assert "x-access-token" not in str(raised.value)


def test_a_url_with_no_credential_in_it_is_reported_as_it_is(tmp_path):
    """The redaction must not eat an ordinary URL out of an ordinary message."""
    with pytest.raises(ConfigError, match="github://org/repo") as raised:
        load_config(write(tmp_path, "sources:\n- name: p\n  url: github://a/b/c\n"))

    assert "github://a/b/c" in str(raised.value)


# -- webdav --------------------------------------------------------------------


def test_a_webdav_source_parses_with_auth(tmp_path):
    text = (
        "sources:\n- name: notes\n  url: webdav+https://cloud.example/remote.php/dav/files/me/notes\n"
        "  auth:\n    username: me\n    password:\n      env: NEXTCLOUD_APP_PASSWORD\n"
    )
    config = load_config(write(tmp_path, text))
    [source] = config.sources
    assert isinstance(source, WebdavSource)
    assert source.auth.username == "me"
    assert source.auth.password == EnvRef(env="NEXTCLOUD_APP_PASSWORD")
    assert source.cache == "snapshot"  # the dial's default: copied, read from disk


def test_a_webdav_source_without_auth_is_refused(tmp_path):
    """WebDAV is an authenticated backend; an anonymous one is a typo, not a mode."""
    text = "sources:\n- name: notes\n  url: webdav+https://cloud.example/dav/notes\n"
    with pytest.raises(ConfigError, match="auth"):
        load_config(write(tmp_path, text))


def test_the_cache_dial_accepts_live(tmp_path):
    config = load_config(write(tmp_path, _webdav("  cache: live\n")))
    assert config.sources[0].cache == "live"


def test_an_unknown_cache_mode_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="cache"):
        load_config(write(tmp_path, _webdav("  cache: sometimes\n")))


def test_cache_is_not_a_field_on_a_git_source(tmp_path):
    """The dial is WebDAV's alone in this epic -- on git it is silently nothing,
    so it has to be refused rather than accepted and ignored."""
    text = "sources:\n- name: a\n  url: github://o/r\n  cache: live\n"
    with pytest.raises(ConfigError, match="cache"):
        load_config(write(tmp_path, text))


def test_the_base_url_drops_only_the_webdav_prefix(tmp_path):
    text = (
        "sources:\n"
        "- name: a\n  url: webdav+https://h/dav/a\n"
        "  auth: {username: u, password: {env: P}}\n"
        "- name: b\n  url: webdav+http://h:8080/dav/b\n"
        "  auth: {username: u, password: {env: P}}\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.sources[0].base_url == "https://h/dav/a"
    assert config.sources[1].base_url == "http://h:8080/dav/b"


def test_a_credential_embedded_in_a_webdav_url_is_refused(tmp_path):
    """A WebDAV URL reaches httpx and the errors /health publishes; the password
    belongs in `auth`, where it stays a reference to an environment variable."""
    text = (
        "sources:\n- name: notes\n  url: webdav+https://me:hunter2@cloud.example/dav\n"
        "  auth: {username: me, password: {env: P}}\n"
    )
    with pytest.raises(ConfigError, match="credential") as raised:
        load_config(write(tmp_path, text))
    assert "hunter2" not in str(raised.value)


def test_a_password_containing_an_at_is_not_half_echoed(tmp_path):
    """The scrub used to stop at the *first* `@`, so the tail of a password with
    an unencoded one came back in the error and hence in the startup log."""
    text = (
        "sources:\n- name: notes\n  url: webdav+https://me:hun@ter2@cloud.example/dav\n"
        "  auth: {username: me, password: {env: P}}\n"
    )
    with pytest.raises(ConfigError, match="credential") as raised:
        load_config(write(tmp_path, text))
    assert "ter2" not in str(raised.value)
    assert "hun" not in str(raised.value)


def test_refresh_is_still_accepted_on_a_webdav_source(tmp_path):
    config = load_config(write(tmp_path, _webdav("  refresh: 10m\n")))
    assert config.sources[0].refresh_seconds == 600


def _webdav(extra: str = "") -> str:
    return (
        "sources:\n- name: notes\n  url: webdav+https://cloud.example/dav/notes\n"
        "  auth:\n    username: me\n    password:\n      env: P\n" + extra
    )


@pytest.mark.unit
def test_a_username_can_be_a_literal_or_an_env_reference(monkeypatch):
    """A service account's name arrives in the same secret as its password.

    Writing it out in the config as well is how the two drift apart the day the
    account is recreated, so `{env:}` has to be accepted on both halves of the
    pair -- while GitHub's literal `x-access-token` keeps working.
    """
    monkeypatch.setenv("ACCOUNT", "mcp-kb")
    monkeypatch.setenv("SECRET", "hunter2")

    literal = BasicAuth(username="x-access-token", password={"env": "SECRET"})
    assert literal.user() == "x-access-token"

    referenced = BasicAuth(username={"env": "ACCOUNT"}, password={"env": "SECRET"})
    assert referenced.user() == "mcp-kb"
    assert referenced.password.resolve().get_secret_value() == "hunter2"


@pytest.mark.unit
def test_a_username_env_reference_that_is_unset_is_an_error(monkeypatch):
    monkeypatch.delenv("ABSENT", raising=False)
    auth = BasicAuth(username={"env": "ABSENT"}, password={"env": "ABSENT"})
    with pytest.raises(ConfigError):
        auth.user()
